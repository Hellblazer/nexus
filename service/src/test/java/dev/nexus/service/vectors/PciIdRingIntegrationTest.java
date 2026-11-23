// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (critic S1 and S3 of the re-check of 441fd17101): the canary's query ring follows the latch. Once the name indexes retire the ring must
 * admit on the id index, or no new query is admitted and the id canary starves; and the production binding of the latch
 * ({@link PciReconciler#create}) reaches the canary, the ring reader and the ring's admission.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciIdRingIntegrationTest extends PciIdIndexSupport {

    private static float[] vector() {
        float[] v = new float[384];
        v[3] = 1f;
        v[11] = 0.5f;
        return v;
    }

    @Test
    void aLatchedCollectionWithOnlyAnIdIndex_isAdmittedToTheRing_andTheCanaryReaderReadsItBack() throws Exception {
        String tenant = newTenant("ring");
        String coll = name("ring");
        long id = collection(tenant, coll, 300);
        var s = settings(16);
        PciIndexSweep sweep = PciIndexSweep.create(svcDs, s);
        TenantScope scope = new TenantScope(svcDs);
        Gate3 latched = () -> gate3 && readers;
        PciReconciler r = reconciler(s);
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        sweep.refresh();
        assertThat(validNameIndexed(tenant)).containsExactly(coll);
        assertThat(validIdIndexed(tenant)).containsExactly(id);

        // control, before the latch: the name index admits, as it always did
        var before = new PciQueryRing(scope, sweep);
        before.followLatch(latched);
        before.admit(tenant, QueryVector.alone(M384, vector(), coll));
        assertThat(before.queued()).as("the name index admits before the latch").isEqualTo(1);

        gate3 = true;
        r.reconcileOnce();                                   // the name index retires
        sweep.refresh();
        assertThat(validNameIndexed(tenant)).as("only the id index stands").isEmpty();
        assertThat(validIdIndexed(tenant)).containsExactly(id);

        var starved = new PciQueryRing(scope, sweep);        // never told about the latch: the name-keyed check of before
        starved.admit(tenant, QueryVector.alone(M384, vector(), coll));
        assertThat(starved.queued()).as("without the latch nothing is admitted once the name indexes retire").isZero();

        var ring = new PciQueryRing(scope, sweep, 16, 1, Duration.ofMillis(50));
        ring.followLatch(latched);
        ring.start();
        try {
            ring.admit(tenant, QueryVector.alone(M384, vector(), coll));
            assertThat(ring.admissionsSeen()).isEqualTo(1);
            var reader = new PciQueryRingReader(scope, latched);   // by id, as the canary's reader is once latched
            await(() -> reader.load(tenant, coll).size() == 1, "the admitted vector, read back by the id reader");
            assertThat(reader.load(tenant, coll).get(0).model()).isEqualTo(M384);
            assertThat(new PciQueryRingReader(scope).load(tenant, coll)).as("and it is the same row the name reader sees").hasSize(1);
        } finally {
            ring.stop();
        }
    }

    /**
     * The production binding: {@code PciReconciler.create} hands the canary, its ring reader and the ring's admission one latch that follows the
     * reconciler's own retire gate (latched gate 3 and the id readers present), read at each call. Deleting the binding leaves it false for ever.
     */
    @Test
    void theLatchCreateHandsOut_followsTheReconcilersOwnGate3AndReaders() throws Exception {
        var s = settings(16);
        Embedder none = texts -> java.util.List.of();
        var created = PciReconciler.create(svcDs, new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS), NONCE,
            PciIndexSweep.create(svcDs, s), s, tenants -> 0, new PgVectorRepository(new TenantScope(svcDs), none, none),
            new TenantScope(svcDs));
        Gate3 latch = created.canaryLatch();

        assertThat(latch.holds()).as("nothing latched").isFalse();
        created.retireNameIndexesWhen(() -> true);
        created.idReadersPresent(() -> false);
        assertThat(latch.holds()).as("latched but the id readers are absent: the retire does not run, nor does the id canary").isFalse();
        created.idReadersPresent(() -> true);
        assertThat(latch.holds()).as("latched and the readers present: the very gate the retire reads").isTrue();
        created.retireNameIndexesWhen(() -> false);
        assertThat(latch.holds()).as("read at each call, never captured").isFalse();
        assertThat(PciReconciler.create(svcDs, new AdminConnection(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS), NONCE,
            PciIndexSweep.create(svcDs, s), s, tenants -> 0, new PgVectorRepository(new TenantScope(svcDs), none, none),
            new TenantScope(svcDs)).canaryLatch()).as("each reconciler has its own").isNotSameAs(latch);
    }

    /**
     * Critic: the canary's vacuum trigger observes the family the canary measures. A leaf holding both an id index and a name index of one collection
     * (before the retire): before the latch the vacuum trigger arms the name index, once latched the id index, never both.
     */
    @Test
    void theVacuumTriggerObservesTheNameIndexBeforeTheLatch_andTheIdIndexAfterIt() throws Exception {
        String tenant = newTenant("vacuum");
        String coll = name("vacuum");
        long id = collection(tenant, coll, 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        var leaf = leaf(tenant);
        String nameIndex = PciCatalog.indexName(M384, tenant, coll);
        String idIndex = PciCatalog.indexNameById(M384, tenant, id);

        assertThat(PciReconciler.canaryFamily(leaf, java.util.Set.of(), false)).as("before the latch: the name index").containsExactly(nameIndex);
        assertThat(PciReconciler.canaryFamily(leaf, java.util.Set.of(), true)).as("latched: the id index").containsExactly(idIndex);
        assertThat(PciReconciler.canaryFamily(leaf, java.util.Set.of(idIndex), true)).as("one the plan drops is not observed").isEmpty();

        // through the schedule: a vacuum that reaped deletes arms the family the canary measures, and only it
        var t0 = java.time.Instant.parse("2026-11-23T00:00:00Z");
        for (boolean latched : new boolean[] {false, true}) {
            var schedule = new PciCanarySchedule(new PciReindexIntegrationTest.ScriptedCanary());
            var family = PciReconciler.canaryFamily(leaf, java.util.Set.of(), latched);
            schedule.observeLeaf("nexus.leaf", family, PciCanaryScheduleTest.stats(0, 0, 0, null, t0));                         // the baseline
            schedule.observeLeaf("nexus.leaf", family, PciCanaryScheduleTest.stats(10, 0, 0, null, t0.plusSeconds(600)));       // a vacuum after deletes
            assertThat(schedule.pending(latched ? idIndex : nameIndex)).as("latched=%s: the measured family is armed", latched)
                .isEqualTo(PciCanarySchedule.VACUUM);
            assertThat(schedule.pending(latched ? nameIndex : idIndex)).as("and the other is not").isNull();
        }
    }
}
