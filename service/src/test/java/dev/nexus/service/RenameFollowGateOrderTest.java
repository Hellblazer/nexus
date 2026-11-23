// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.DeadlockRetry;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, review finding F2): a write by id that finds its row renamed does not take the new label's sweep gate
 * while it holds the registry row {@code FOR KEY SHARE} (a gate-then-row inversion against a verb that holds that gate and wants the row).
 * It rolls back and runs again from the top with the new label's gate FIRST ({@code RenameFollow}).
 *
 * <p>The test holds the NEW label's gate from a hand session, renames the row in place in the window before the writer's registry read
 * (the hook), and sends the real {@code upsert-chunks} by id. The writer's first attempt takes the old label's gate, reads the row, sees the
 * new label and rolls back; its second attempt waits for the new label's gate. While it waits (pg_locks shows the advisory request), a
 * {@code FOR UPDATE NOWAIT} on the registry row must succeed: the writer holds no key share. Under the old order (the second gate after the
 * read) the writer would still hold the share and the probe would fail 55P03. Released, the write lands under the new label.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RenameFollowGateOrderTest {

    CollectionRouteFixture fx;
    ExecutorService pool;
    int n;
    private final ConcurrentLinkedQueue<String> retries = new ConcurrentLinkedQueue<>();

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        pool = Executors.newFixedThreadPool(2);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
    }

    @BeforeEach
    void listenForRetries() {
        retries.clear();
        DeadlockRetry.setRetryListenerForTests(retries::add);
    }

    @AfterEach
    void cleanUp() {
        DeadlockRetry.setRetryListenerForTests(null);
        fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        assertThat(retries).as("no deadlock was absorbed").isEmpty();
    }

    private int waitingAdvisory() {
        return fx.asSuperuser(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse().and(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))));
    }

    @Timeout(180)
    @Test
    void aWriteByIdThatFindsItsRowRenamed_runsAgainWithTheNewLabelsGateFirst_holdingNoRowLockWhileItWaits() throws Exception {
        String b = label("rf-gate-b-" + (n++));
        fx.register(b);
        long x = fx.idOf(b);
        String c = label("rf-gate-c-" + (n++));
        String hex = Chash.ofText("rf-gate-" + b).toHex();
        AtomicBoolean renamed = new AtomicBoolean();
        Future<Resp> writer;
        try (Connection gateHolder = fx.pg.createConnection("")) {
            gateHolder.setAutoCommit(false);
            DSL.using(gateHolder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + fx.tenant + "/" + c)))).fetch();   // the NEW label's gate, held
            fx.service.collectionSelectorFilter().testHooks(null, () -> {
                if (renamed.compareAndSet(false, true)) {   // once, in the window before the writer's registry read
                    CompletableFuture.runAsync(() -> fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS)
                        .set(CATALOG_COLLECTIONS.NAME, c)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x)).execute())).join();
                }
            }, null, null);
            writer = pool.submit(() -> fx.post("/v1/vectors/upsert-chunks",
                "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"],\"documents\":[\"rename follow text\"]}"));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
            while (waitingAdvisory() < 1) {
                if (writer.isDone() || System.nanoTime() > deadline) {
                    throw new AssertionError("the writer never waited for the new label's gate (done: " + writer.isDone() + ")");
                }
                LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
            }
            assertThat(renamed.get()).as("setup: the row was renamed in the window").isTrue();
            // the probe: a verb's lock on the row goes through at once, because the waiting writer holds no key share
            try (Connection probe = fx.pg.createConnection("")) {
                probe.setAutoCommit(false);
                try {
                    DSL.using(probe, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(x))
                        .forUpdate().noWait().fetch();
                } catch (DataAccessException e) {
                    throw new AssertionError("the writer waits for the new label's gate while holding the registry row (" + e.sqlState()
                        + "): the second gate was taken after the key share", e);
                } finally {
                    probe.rollback();
                }
            }
            gateHolder.rollback();   // release the new label's gate
        }
        Resp r = writer.get(120, TimeUnit.SECONDS);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        java.util.List<String> labels = fx.asSuperuser(ctx -> ctx.select(CHUNKS.COLLECTION).from(CHUNKS)
            .where(CHUNKS.COLLECTION_ID.eq(x)).and(CHUNKS.CHASH.eq(chash)).fetch(CHUNKS.COLLECTION));
        assertThat(labels).as("the chunk landed under the row's current label").containsExactly(c);
    }
}
