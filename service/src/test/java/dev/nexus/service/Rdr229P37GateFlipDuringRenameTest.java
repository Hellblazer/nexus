// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.RenameLatchLock;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the RDR's name-path rename that is open when gate 3 flips in process (fix check 50164fafab10, A O-5), on a
 * booted engine over HTTP. A name-path rename is held at its source registry row by a session of the test, after it has taken
 * {@code RenameLatchLock} shared. Gate 3's input then opens and the completion check's latch write queues for the lock exclusive. The latch
 * row must not exist while the rename is in flight (the rename is one transaction that decided from the row it read under the lock), the rename
 * must finish on the name path (200, a new registry id), and only then does the latch row land, after which the next rename is refused.
 *
 * <p>No timing is assumed: the test goes on when {@code pg_locks} shows the rename holding the shared lock and the latch write waiting for the
 * exclusive one. Both sides are bounded at 10 s in the engine ({@code CollectionTombstones.ROW_LOCK_TIMEOUT_MS},
 * {@code CompletionCheck.LATCH_LOCK_TIMEOUT_MS}); each wait here is bounded well under that.
 */
class Rdr229P37GateFlipDuringRenameTest {

    private static String coll(String owner, int version) {
        return "knowledge__" + owner + "__minilm-l6-v2-384__v" + version;
    }

    private static long advisoryOn(Rdr229RenameEngine engine, String mode, boolean granted) {
        return engine.su(ctx -> ctx.fetchOne("select count(*) from pg_locks where locktype = 'advisory' and classid::bigint = ? "
            + "and objid::bigint = ? and mode = ? and granted = ?", (long) RenameLatchLock.NAMESPACE, (long) RenameLatchLock.GATE3_KEY,
            mode, granted).get(0, Long.class));
    }

    private static long latchRows(Rdr229RenameEngine engine) {
        return engine.su(ctx -> ctx.fetchOne("select count(*) from nexus.rdr229_gate_latch").get(0, Long.class));
    }

    @Test
    void aNamePathRenameOpenWhenGate3Flips_finishesOnTheNamePath_thenTheLatchLands_andTheNextRenameIsRefused() throws Exception {
        try (var engine = new Rdr229RenameEngine().gate3(false)) {
            engine.boot();
            Rdr229RenameRestartPrecondition.awaitStampMovesComplete(engine);
            String tenant = "p37-flip";
            String token = engine.newTenant(tenant);
            String old = coll("p37flip", 1);
            String next = coll("p37flip2", 1);
            engine.su(ctx -> {
                Rdr229RenameSeeds.full(ctx, tenant, old);
                Rdr229RenameSeeds.chunksAndRing(ctx, tenant, next);
                return null;
            });
            long idBefore = engine.su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, old));
            assertThat(latchRows(engine)).as("non-vacuity: gate 3 has not latched").isZero();

            CompletableFuture<Rdr229RenameEngine.Renamed> rename;
            CompletableFuture<Object> flip;
            try (Connection holder = engine.pg.createConnection("")) {
                holder.setAutoCommit(false);
                try (var st = holder.createStatement()) {
                    st.execute("select 1 from nexus.catalog_collections where tenant_id = '" + tenant + "' and name = '" + old + "' for update");
                }
                try {
                    rename = CompletableFuture.supplyAsync(() -> {
                        try {
                            return engine.rename(token, old, coll("p37flip", 2));
                        } catch (Exception e) {
                            throw new IllegalStateException(e);
                        }
                    });
                    assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(8), () -> advisoryOn(engine, "ShareLock", true) >= 1))
                        .as("the rename holds RenameLatchLock shared and waits at the held source row").isTrue();

                    engine.gate3(true);
                    flip = CompletableFuture.supplyAsync(() -> {
                        engine.completionCheck().refresh();   // the latch write runs in this call (Gate3LatchWriter.IMMEDIATE)
                        return null;
                    });
                    assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(8), () -> advisoryOn(engine, "ExclusiveLock", false) >= 1))
                        .as("gate 3 holds and the latch write waits for the rename in flight").isTrue();
                    assertThat(latchRows(engine)).as("no latch row while the name-path rename is in flight").isZero();
                    assertThat(rename.isDone()).as("the rename is still held at its source row").isFalse();
                } finally {
                    holder.rollback();
                }
            }

            var renamed = rename.get(30, TimeUnit.SECONDS);
            flip.get(30, TimeUnit.SECONDS);
            assertThat(renamed.status()).as("the rename that was open when gate 3 flipped finished on the name path: " + renamed).isEqualTo(200);
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, coll("p37flip", 2))))
                .as("the name path gives the collection a new id").isNotNull().isNotEqualTo(idBefore);
            assertThat(Rdr229RenameEngine.await(Duration.ofSeconds(30), () -> latchRows(engine) == 1))
                .as("the latch row lands once the rename is done").isTrue();

            var refused = engine.rename(token, next, coll("p37flip2", 2));
            assertThat(refused.refused()).as("from the latch on, a rename is refused 409 rename_refused: " + refused).isTrue();
            assertThat(refused.missing()).as("naming a condition: the move or the restart").isIn("name_key_drop_move", "restart");
            assertThat(engine.<Long>su(ctx -> Rdr229RenameChecks.registryId(ctx, tenant, next))).as("and wrote nothing").isNotNull();
        }
    }
}
