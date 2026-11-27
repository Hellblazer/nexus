// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.time.Duration;

/**
 * RDR-229 P3.5 / P3.7 joint test (residual R8, nexus-p2m1x.47 step 2), and the Test Plan scenario "an engine with the P3.5 and P3.7 cut boots on a store
 * with only half its chunks stamped (the stamp move paused part-way)", on a booted engine over HTTP, boot 1 only.
 *
 * <p>The state is made with the engine's own move machinery: the worker starts after the walk, the {@code chunks} stamp move is paused
 * ({@code paused_at}) and the others complete, so the store is half stamped in the sense the completion check counts (gate 1 does not hold). In
 * that state the listing and a rename return what they returned before the cut: the rename is the fourteen-table name path and the registry gives the collection
 * a new id. Then the move is resumed and gate 3 latches IN PROCESS, with no restart ({@link Rdr229RenameEngine#boots()} stays 1): from the latch
 * the engine's readers are on the id (the signal {@code NexusService} hands them is the check's latch) and a rename is refused 409 naming a condition,
 * because the name path would give the collection a new id that the readers' id would not follow.
 *
 * <p>Not covered here: search. The harness engine has no vector repository ({@code has_pgvector=false}), so search before and after the latch is
 * pinned at the repository, where it can run: {@code Rdr229P35cSearchByIdIntegrationTest}, {@code Rdr229P35cLatchedIdPathTest} (the in-process latch on one
 * repository, each test with its unlatched control) and the SQL readers' half-stamped shapes in {@code Rdr229P35bSqlReadersIntegrationTest}
 * and {@code Rdr229P35bBranchPartitionIntegrationTest}.
 */
class Rdr229P35HalfStampedStoreTest {

    private static final String CHUNKS_STAMP = "rdr229-stamp-chunks";

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    private static void setPaused(Rdr229RenameEngine engine, boolean paused) {
        int n = engine.su(ctx -> ctx.execute(paused
            ? "update nexus.data_moves set paused_at = now(), paused_by = 'p35-half-stamped-test' where label = ?"
            : "update nexus.data_moves set paused_at = null, paused_by = null where label = ?", CHUNKS_STAMP));
        assertThat(n).as("non-vacuity: the chunks stamp move is registered").isEqualTo(1);
    }

    @Test
    void aHalfStampedStore_listsAndRenamesAsBeforeTheCut_thenReadsByTheIdWhenGate3LatchesInProcess() throws Exception {
        try (var engine = new Rdr229RenameEngine().workerAtBoot(false)) {
            engine.boot();
            setPaused(engine, true);
            engine.startWorker();
            assertThat(Rdr229RenameEngine.await(Duration.ofMinutes(2), () -> engine.su(ctx -> ctx.fetchOne(
                "select count(*) from nexus.data_moves where label like 'rdr229-stamp-%' and completed_at is not null").get(0, Long.class)) > 0))
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
            String listing = engine.call("GET", "/v1/catalog/collections/list", token, null).body();
            assertThat(listing).as("the listing names the new label").contains(neu);

            setPaused(engine, false);   // the stamp resumes; nothing restarts
            assertThat(Rdr229RenameEngine.await(Duration.ofMinutes(3), () -> engine.gate3Holds()))
                .as("gate 3 latches in this process once the stamp is done [" + engine.completion().gate3Why() + "]").isTrue();
            assertThat(engine.boots()).as("with no restart").isEqualTo(1);

            String second = coll("p35half2", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, second);
                return null;
            });
            var refused = engine.rename(token, second, coll("p35half2", 2));
            assertThat(refused.refused()).as("from the latch the engine reads by the id, so the name path is closed: " + refused).isTrue();
            assertThat(refused.missing()).isIn("name_key_drop_move", "restart");
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, second))).as("and nothing was written").isNotNull();
            assertThat(engine.call("GET", "/v1/catalog/collections/list", token, null).body()).as("the listing still works").contains(neu).contains(second);
        }
    }
}
