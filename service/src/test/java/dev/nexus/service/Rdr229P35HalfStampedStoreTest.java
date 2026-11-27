// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.RenameReadiness.Missing;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.OffsetDateTime;

/**
 * RDR-229 P3.5 / P3.7 joint test (residual R8, nexus-p2m1x.47 step 2), and the Test Plan scenario "an engine with the P3.5 and P3.7 cut boots on a store
 * with only half its chunks stamped (the stamp move paused part-way)", on a booted engine over HTTP, through the chain the engine really walks (the
 * harness runs the production id-unique-index pass as gate 3's extra; {@link Rdr229RenameEngine} states its one simulated clause).
 *
 * <p>The state is made with the engine's own move machinery: the worker starts after the walk, the {@code chunks} stamp move is paused
 * ({@code paused_at}) and the others complete, so the store is half stamped in the sense the completion check counts (gate 1 does not hold). In
 * that state the listing and a rename return what they returned before the cut: the rename is the fourteen-table name path and the registry gives the
 * collection a new id. The move is then resumed and the stamp completes, but the engine is still in boot 1 and still does not read by the id: gate 3
 * waits for changesets {@code vectors-078-8} and {@code vectors-084-1..3}, which the first walk skipped, so a rename is still the name path. Only the
 * restart (the boot after the stamp moves complete) latches gate 3; from there the drop move runs and the rename is the one-row path, the id surviving.
 *
 * <p>Not covered here: search, and the readers' own switch to the id. The harness engine has no vector repository ({@code has_pgvector=false}), and the
 * rename path observes the latch row, not the readers. Both are pinned at the repository, where they can run: {@code Rdr229P35cSearchByIdIntegrationTest},
 * {@code Rdr229P35cLatchedIdPathTest} (the in-process latch on one repository, each test with its unlatched control) and the SQL readers' half-stamped
 * shapes in {@code Rdr229P35bSqlReadersIntegrationTest} and {@code Rdr229P35bBranchPartitionIntegrationTest}. The chunk rows seeded here are stamped by
 * their writers; the half-stamped state is the incomplete stamp move, as in the scenario's text.
 */
class Rdr229P35HalfStampedStoreTest {

    private static final String CHUNKS_STAMP = "rdr229-stamp-chunks";

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    private static final Table<?> MOVES = DSL.table(DSL.name("nexus", "data_moves"));
    private static final Field<String> LABEL = DSL.field(DSL.name("label"), String.class);
    private static final Field<OffsetDateTime> COMPLETED_AT = DSL.field(DSL.name("completed_at"), OffsetDateTime.class);

    private static void setPaused(Rdr229RenameEngine engine, boolean paused) {
        int n = engine.su(ctx -> ctx.update(MOVES)
            .set(DSL.field(DSL.name("paused_at"), OffsetDateTime.class), paused ? OffsetDateTime.now() : null)
            .set(DSL.field(DSL.name("paused_by"), String.class), paused ? "p35-half-stamped-test" : null)
            .where(LABEL.eq(CHUNKS_STAMP)).execute());
        assertThat(n).as("non-vacuity: the chunks stamp move is registered").isEqualTo(1);
    }

    @Test
    void aHalfStampedStore_listsAndRenamesAsBeforeTheCut_andReadsByTheIdOnlyAfterTheRestartLatchesGate3() throws Exception {
        try (var engine = new Rdr229RenameEngine().workerAtBoot(false)) {
            engine.boot();
            setPaused(engine, true);
            engine.startWorker();
            assertThat(Rdr229RenameEngine.await(Duration.ofMinutes(2), () -> engine.su(ctx -> ctx.fetchCount(MOVES, LABEL.like("rdr229-stamp-%").and(COMPLETED_AT.isNotNull()))) > 0))
                .as("the other stamp moves complete while the chunks stamp stays paused").isTrue();
            assertThat(engine.moveCompleted(CHUNKS_STAMP)).as("non-vacuity: the chunks stamp move is not complete: the store is half stamped").isFalse();
            assertThat(engine.completion().gate1()).as("and gate 1 does not hold").isFalse();
            assertThat(engine.gate3Holds()).as("so gate 3 has not latched").isFalse();

            String tenant = "p35-half";
            String token = engine.newTenant(tenant);
            String old = coll("p35half", 1);
            String neu = coll("p35half", 2);
            engine.su(ctx -> {
                Rdr229RenameSeeds.full(ctx, tenant, old);
                return null;
            });
            long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, old));
            assertThat(engine.call("GET", "/v1/catalog/collections/list", token, null).body())
                .as("the listing names the collection, as before the cut").contains(old);

            var renamed = engine.rename(token, old, neu);

            assertThat(renamed.status()).as("a rename before gate 3 is the name path, as before the cut: " + renamed).isEqualTo(200);
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, neu)))
                .as("the name path gives the collection a new id").isNotNull().isNotEqualTo(idBefore);
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", neu))).as("the chunks were rewritten").isPositive();
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.naming(ctx, tenant, "chunks", "collection", old))).isZero();
            assertThat(engine.call("GET", "/v1/catalog/collections/list", token, null).body()).as("the listing names the new label").contains(neu);

            // the stamp resumes and completes: still boot 1, and gate 3 still cannot hold (the changesets its id indexes wait for were skipped by this walk)
            setPaused(engine, false);
            assertThat(Rdr229RenameEngine.await(Duration.ofMinutes(3), () -> engine.completion().gate2()))
                .as("the stamp completes and gates 1 and 2 hold").isTrue();
            assertThat(engine.moveCompleted(CHUNKS_STAMP)).isTrue();
            var boot1 = engine.completion();
            assertThat(boot1.gate3Latched()).as("boot 1: gate 3 has not latched [" + boot1.gate3Why() + "]").isFalse();
            assertThat(boot1.gate3Why()).as("it waits for the changesets only the next boot's walk applies").contains("vectors-084-1");
            assertThat(engine.boots()).isEqualTo(1);
            String second = coll("p35half2", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, second);
                return null;
            });
            var again = engine.rename(token, second, coll("p35half2", 2));
            assertThat(again.status()).as("boot 1, stamp complete: the engine does not read by the id yet, the rename is still the name path: " + again)
                .isEqualTo(200);
            assertThat(engine.body(again).path("ids_stable").asBoolean(true)).isFalse();

            // the restart: the boot after the stamp moves complete latches gate 3
            engine.workerAtBoot(true);
            engine.restart();
            assertThat(Rdr229RenameEngine.await(Rdr229RenameRestartPrecondition.LATCH_WAIT,
                () -> engine.completion().gate3Latched() && engine.readiness().filter(Missing.NONE::equals).isPresent()))
                .as("boot 2: gate 3 latched and the drop move done [" + engine.completion().gate3Why() + "; readiness "
                    + engine.readiness().map(Object::toString).orElse("?") + "]").isTrue();
            assertThat(engine.call("GET", "/v1/catalog/collections/list", token, null).body()).as("the listing still works").contains(neu);
            String third = coll("p35half3", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, third);
                return null;
            });
            long idThird = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, third));
            var one = engine.rename(token, third, coll("p35half3", 2));
            assertThat(one.status()).as("from the latch the rename is the one-row path: " + one).isEqualTo(200);
            assertThat(engine.body(one).path("ids_stable").asBoolean(false)).as("the id survived").isTrue();
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, coll("p35half3", 2)))).isEqualTo(idThird);
        }
    }
}
