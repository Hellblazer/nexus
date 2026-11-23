// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.db.PgSession.PciSettings;
import org.jooq.DSLContext;
import org.jooq.ExecuteContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultExecuteListener;
import org.jooq.impl.DefaultExecuteListenerProvider;
import org.jooq.JSONB;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.4 (critic S-a and S-b of the confirmation of 2e283b91b4): the near-latch scan of gate 3's PCI clause. It asks for no more
 * unindexed collections than the planner can build on a leaf (the per-leaf cap, the planner's largest-first order), and it fails closed:
 * budget gone, a probe that fails, and a leaf holding rows the registry read cannot place each leave the clause unknown, so gate 3 is false. A
 * negative answer is never kept.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciGate3CoverageScanIntegrationTest extends PciIdIndexSupport {

    /** The registry's name keys are dropped once, for the near-latch scan's renamed-in-place case (the one-row rename P3.7 runs). */
    @org.junit.jupiter.api.BeforeAll
    void dropTheRegistryNameKeys() throws Exception {
        asSuperuser(su -> {
            assertThat(dev.nexus.service.VecPciRegistryNames.dropRegistryNameKeys(su)).as("non-vacuity: the registry's name keys are dropped").isNotEmpty();
            return null;
        });
    }

    private PciGate3Coverage coverage(PciSettings s, Clock clock) {
        return new PciGate3Coverage(catalog::read, s, clock);
    }

    private Optional<CompletionCheck.IdIndexCoverage> read(PciGate3Coverage source, java.util.function.UnaryOperator<DSLContext> via) throws Exception {
        try (Connection admin = adminDs.getConnection()) {
            admin.setReadOnly(true);
            return source.read(via.apply(DSL.using(admin, SQLDialect.POSTGRES)));
        }
    }

    private Optional<CompletionCheck.IdIndexCoverage> read(PciGate3Coverage source) throws Exception {
        return read(source, c -> c);
    }

    private CompletionCheck.Result evaluate(PciGate3Coverage source, java.util.function.UnaryOperator<DSLContext> via) throws Exception {
        try (Connection admin = adminDs.getConnection()) {
            DSLContext ctx = via.apply(DSL.using(admin, SQLDialect.POSTGRES));
            return CompletionCheck.evaluate(ctx, new CompletionCheck.ReadSet(List.of(), List.of()), source, List.of());
        }
    }

    /**
     * RDR-229 P3.5c (census V1): from the latch the near-latch scan probes a collection's rows by its id, so a collection renamed in place (its
     * chunk rows keep the old label) still qualifies under its new label; before it, the name probe finds none under the new label.
     */
    @Test
    void theNearLatchScanProbesByIdFromTheLatch_aRenamedCollectionStillQualifies() throws Exception {
        String tenant = newTenant("covid");
        String oldLabel = name("covid-old"), newLabel = name("covid-new");
        collection(tenant, oldLabel, 300);                       // qualifies (>= B), no id index yet
        PciReconciler r = reconciler(settings(2));
        r.reconcileOnce();
        int renamed = asSuperuser(su -> su.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, newLabel)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(oldLabel)).execute());
        assertThat(renamed).as("the registry row is renamed in place").isEqualTo(1);
        int carrying = asSuperuser(su -> su.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(newLabel))));
        assertThat(carrying).as("precondition: no chunk row carries the new label").isZero();

        var byId = read(new PciGate3Coverage(catalog::read, settings(2), Clock.systemUTC(), () -> true));
        var byName = read(new PciGate3Coverage(catalog::read, settings(2), Clock.systemUTC()));

        assertThat(byId).as("the scan answered").isPresent();
        assertThat(byId.get().planned()).as("by id the renamed collection qualifies and needs an id index").isNotEmpty();
        assertThat(byName.map(c -> c.planned().size()).orElse(0))
            .as("by name the new label counts no row, so nothing is required of it (the name path before the latch)")
            .isLessThan(byId.get().planned().size());
    }

    // -- S-a: the per-leaf cap ---------------------------------------------------------------------------------------------

    /**
     * A leaf at its cap (2 id indexes) with a third qualifying collection that the planner can never build: the third is not required, so gate 3
     * can latch once the planner's chosen ones are covered. Without the cap the third stays uncovered for ever.
     */
    @Test
    void aLeafOverItsCap_requiresOnlyWhatThePlannerCanBuild_andGate3LatchesOnceThoseAreCovered() throws Exception {
        String tenant = newTenant("cap");
        long big = collection(tenant, name("cap-big"), 500);
        long mid = collection(tenant, name("cap-mid"), 400);
        collection(tenant, name("cap-small"), 300);            // qualifies (>= B) but the cap leaves it no slot
        PciReconciler r = reconciler(settings(2));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        assertThat(validIdIndexed(tenant)).as("the planner builds the cap's worth, the largest first").containsExactlyInAnyOrder(big, mid);

        var read = coverage(r).orElseThrow();
        assertThat(uncovered(r, tenant)).as("the third has no slot, so it is not required").isEmpty();
        assertThat(read.planned()).as("two required, both covered").hasSize(2);
        assertThat(read.nothingToIndex()).isFalse();
        var again = r.reconcileOnce();
        assertThat(again.builds()).as("non-vacuity: the planner really cannot build a third id index here").isZero();
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(big, mid);
    }

    /** One free slot and two qualifying collections: the clause asks for the larger one only, the planner's order (recounted: every count stops at B). */
    @Test
    void withOneFreeSlot_theLargerOfTwoQualifyingCollectionsIsTheOneRequired() throws Exception {
        String tenant = newTenant("slot");
        long a = collection(tenant, name("slot-a"), 500);
        long b = collection(tenant, name("slot-b"), 400);
        PciReconciler r = reconciler(settings(2));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        assertThat(validIdIndexed(tenant)).containsExactlyInAnyOrder(a, b);
        collection(tenant, name("slot-c"), 300);
        long d = collection(tenant, name("slot-d"), 900);       // both are over B and below the recount cap; d is the larger, c sorts first

        var wide = new PciGate3Coverage(catalog::read, settings(3), Clock.systemUTC());     // a cap of 3: two held, one free
        var read = read(wide).orElseThrow();

        assertThat(read.uncovered().stream().filter(k -> k.contains("\0" + tenant + "\0")).toList())
            .as("one slot, so one collection, the largest").containsExactly(PciCatalog.idKey(M384, tenant, d));
    }

    // -- S-b: fail closed ----------------------------------------------------------------------------------------------------

    /** A clock that moves a minute and a second on every reading: the scan's budget is gone at its first check. */
    private static final class RunawayClock extends Clock {
        private final AtomicInteger readings = new AtomicInteger();

        @Override public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override public Instant instant() {
            return Instant.parse("2026-11-23T00:00:00Z").plusSeconds(61L * readings.getAndIncrement());
        }
    }

    /** One covered, indexed collection (so the scan is reached) and one unindexed small one (so there is a probe to make). */
    private PciReconciler aCoveredLeafWithAnUnindexedCollection(String tag) throws Exception {
        String tenant = newTenant(tag);
        collection(tenant, name(tag + "-idx"), 300);
        PciReconciler r = reconciler(settings(16));
        r.reconcileOnce();
        gate2 = true;
        r.reconcileOnce();
        assertThat(uncovered(r, tenant)).as("non-vacuity: everything that holds an index is covered, so the scan runs").isEmpty();
        collection(tenant, name(tag + "-plain"), 50);
        return r;
    }

    @Test
    void aScanThatRunsOutOfItsBudget_isUnknown_andGate3IsFalse() throws Exception {
        aCoveredLeafWithAnUnindexedCollection("budget");
        var runaway = new PciGate3Coverage(catalog::read, settings(16), new RunawayClock());

        assertThat(read(runaway)).as("unknown, never 'nothing to cover'").isEmpty();
        var result = evaluate(runaway, c -> c);
        assertThat(result.gate3()).isFalse();
        assertThat(result.gate3Latched()).isFalse();

        assertThat(read(coverage(settings(16), Clock.systemUTC()))).as("control: the same database, a clock that is not runaway, answers").isPresent();
    }

    @Test
    void aProbeThatFails_isUnknown_andGate3IsFalse() throws Exception {
        PciReconciler r = aCoveredLeafWithAnUnindexedCollection("probe");
        var failing = new DefaultExecuteListener() {
            @Override public void executeStart(ExecuteContext c) {
                if (c.sql() != null && c.sql().contains("\"chunks\"") && !c.sql().contains("pg_")) {
                    throw new org.jooq.exception.DataAccessException("the probe is refused");
                }
            }
        };
        java.util.function.UnaryOperator<DSLContext> failProbe = c -> DSL.using(c.configuration().derive(new DefaultExecuteListenerProvider(failing)));
        var source = coverage(settings(16), Clock.systemUTC());

        assertThat(read(source, failProbe)).as("unknown").isEmpty();
        assertThat(evaluate(source, failProbe).gate3()).isFalse();
        assertThat(read(source)).as("control: without the failure the same source answers").isPresent();
        assertThat(r).isNotNull();
    }

    @Test
    void aLeafHoldingRowsTheRegistryReadCannotPlace_isUnknown_andGate3IsFalse() throws Exception {
        aCoveredLeafWithAnUnindexedCollection("noreg");
        String stray = newTenant("noreg-stray");
        // rows whose registry row is not there (the foreign key is not enforced under replica): the registry read places none of this tenant's rows
        asSuperuser(su -> {
            su.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            return su.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT,
                CHUNKS.EMBEDDING_384, CHUNKS.METADATA)
            .values(stray, "knowledge__stray__minilm-l6-v2-384__v1", dev.nexus.service.db.Chash.fromHex(dev.nexus.service.db.Chash.ofText("stray").toHex()).toBytes(),
                M384, "stray", vector(1), JSONB.jsonb("{}")).execute();
        });
        var source = coverage(settings(16), Clock.systemUTC());

        assertThat(read(source)).as("rows in a leaf whose tenant the registry shows nothing of: not visible, so unknown").isEmpty();
        assertThat(evaluate(source, c -> c).gate3()).isFalse();
    }

    @Test
    void aNegativeAnswerIsNeverKept_theNextReadScansAgain() throws Exception {
        String tenant = newTenant("rescan");
        collection(tenant, name("rescan-small"), 50);
        var source = coverage(settings(16), Clock.systemUTC());

        var first = read(source).orElseThrow();
        assertThat(first.nothingToIndex()).as("nothing reaches B: a negative answer").isTrue();

        collection(tenant, name("rescan-big"), 300);
        var second = read(source).orElseThrow();    // the same instance
        assertThat(second.nothingToIndex()).as("rescanned, not served from the earlier negative").isFalse();
        assertThat(second.uncovered().stream().filter(k -> k.contains("\0" + tenant + "\0"))).hasSize(1);
    }
}
