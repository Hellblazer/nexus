// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): a re-home is a WRITER. It retires nothing (it moves rows between two live collections), so it takes
 * both registry rows {@code FOR KEY SHARE} after the sweep gates and builds its scope from those locked rows, never from a name lookup.
 * Before this it resolved the labels unlocked, live-first, and took only the sweep gate (which a supersede or rename shares), so an id
 * freed with its label taken again in the gap moved the new holder's rows out, or filed rows into it, and a concurrent retire of either
 * end was not excluded.
 *
 * <ul>
 * <li>a freed id on the source, and on the target: the typed 404, nothing moved, the new holder's rows untouched;</li>
 * <li>both orders against a supersede parked on the row, zero {@code DeadlockRetry} retries: the verb first (the re-home waits, then reads
 *     the retired row and is refused 409, nothing moved) and the re-home first (the verb waits for the re-home's key share, then retires
 *     the source once the rows have moved).</li>
 * </ul>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRehomeLiveAtCommitTest {

    private static final String REHOME = "/v1/catalog/collections/rehome";
    private static final String SUPERSEDE = "/v1/catalog/collections/supersede";
    private static final String DELETE = "/v1/catalog/collections/delete";

    CollectionRouteFixture fx;
    ExecutorService pool;
    int n;
    private final ConcurrentLinkedQueue<String> retries = new ConcurrentLinkedQueue<>();

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        pool = Executors.newFixedThreadPool(4);
    }

    @AfterAll
    void stopAll() {
        if (pool != null) pool.shutdownNow();
        if (fx != null) fx.close();
    }

    @BeforeEach
    void listenForRetries() {
        retries.clear();
        dev.nexus.service.db.DeadlockRetry.setRetryListenerForTests(retries::add);
    }

    @AfterEach
    void noDeadlockWasAbsorbedAndNoHooksRemain() {
        dev.nexus.service.db.DeadlockRetry.setRetryListenerForTests(null);
        fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        assertThat(retries).as("the lock order prevents a deadlock: DeadlockRetry absorbed none").isEmpty();
    }

    private String registered(String stem) {
        String l = label("rh-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("rehome text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
    }

    private int chunksOf(String collection, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        return fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection).and(CHUNKS.CHASH.eq(chash))));
    }

    private static void elsewhere(Runnable work) {
        CompletableFuture.runAsync(work).join();
    }

    private int waitingRows() {
        return fx.asSuperuser(ctx -> ctx.fetchCount(DSL.table(DSL.name("pg_catalog", "pg_locks")),
            DSL.field(DSL.name("granted"), Boolean.class).isFalse()
                .and(DSL.field(DSL.name("locktype"), String.class).in("tuple", "transactionid"))));
    }

    private void awaitWaiting(int count, Future<?> racer, String what) {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        while (waitingRows() < count) {
            if (racer.isDone() || System.nanoTime() > deadline) {
                throw new AssertionError(what + ": " + count + " waiting lock request(s) never showed in pg_locks (racer done: "
                    + racer.isDone() + ")");
            }
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        }
    }

    private void assertUnknownId(Resp r, long id) {
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(id);
    }

    /** What the hook does in the gap: frees the id by deleting {@code a}, and takes the label again as a new row, filing {@code hex} in it. */
    private void freeTheIdAndTakeTheLabel(String a, String hex, AtomicInteger deleteStatus, AtomicInteger fired) {
        fx.service.collectionSelectorFilter().testHooks(() -> {
            if (fired.getAndIncrement() == 0) {   // the delete passes through the boundary too: only the first call acts
                elsewhere(() -> {
                    deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status());
                    fx.register(a);
                    if (hex != null) {
                        seedChunk(a, hex);
                    }
                });
            }
        }, null, null, null);
    }

    @Timeout(180)
    @Test
    void aRehomeByASourceIdFreedInTheGap_is404_movesNothing_andLeavesTheNewHolderOfTheLabelAlone() {
        String s = registered("src-freed");
        String t = registered("src-freed-t");
        long sid = fx.idOf(s);
        String zHex = Chash.ofText("rh-z-" + s).toHex();
        AtomicInteger deleteStatus = new AtomicInteger();
        freeTheIdAndTakeTheLabel(s, zHex, deleteStatus, new AtomicInteger());
        Resp r = fx.post(REHOME, "{\"source_id\":" + sid + ",\"target\":" + json(t) + "}");
        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        assertThat(fx.idOf(s)).as("setup: the label is another row's now").isNotEqualTo(sid);
        assertUnknownId(r, sid);
        assertThat(chunksOf(s, zHex)).as("the new holder of the source label kept its chunk").isEqualTo(1);
        assertThat(chunksOf(t, zHex)).as("nothing was moved into the target").isZero();
    }

    @Timeout(180)
    @Test
    void aRehomeByATargetIdFreedInTheGap_is404_movesNothing_andFilesNothingInTheNewHolder() {
        String s = registered("tgt-freed-s");
        String t = registered("tgt-freed");
        long tid = fx.idOf(t);
        String sHex = Chash.ofText("rh-s-" + s).toHex();
        seedChunk(s, sHex);
        AtomicInteger deleteStatus = new AtomicInteger();
        freeTheIdAndTakeTheLabel(t, null, deleteStatus, new AtomicInteger());
        Resp r = fx.post(REHOME, "{\"source\":" + json(s) + ",\"target_id\":" + tid + "}");
        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        assertThat(fx.idOf(t)).as("setup: the label is another row's now").isNotEqualTo(tid);
        assertUnknownId(r, tid);
        assertThat(chunksOf(s, sHex)).as("the source kept its chunk").isEqualTo(1);
        assertThat(chunksOf(t, sHex)).as("nothing was filed into the new holder of the target label").isZero();
    }

    /** The real supersede of {@code x} by {@code y}, parked on Y's row (held by {@code park}) while it holds X. */
    private Future<Resp> parkedSupersede(Connection park, String x, String y) throws Exception {
        park.setAutoCommit(false);
        DSL.using(park, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(y)).forUpdate().fetch();
        Future<Resp> supersede = pool.submit(() -> fx.post(SUPERSEDE, "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}"));
        awaitWaiting(1, supersede, "the supersede parks on the successor's row, holding X");
        return supersede;
    }

    @Timeout(180)
    @Test
    void aRehomeArrivingWhileASupersedeHoldsTheSourceRow_waits_thenIsRefused409_andMovesNothing() throws Exception {
        String x = registered("verb-first-x");   // registered first: the verb locks it first
        String y = registered("verb-first-y");
        String t = registered("verb-first-t");
        long xid = fx.idOf(x);
        String hex = Chash.ofText("rh-vf-" + x).toHex();
        seedChunk(x, hex);
        Future<Resp> supersede;
        Future<Resp> rehome;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            rehome = pool.submit(() -> fx.post(REHOME, "{\"source_id\":" + xid + ",\"target\":" + json(t) + "}"));
            awaitWaiting(2, rehome, "the re-home waits for the source row the verb holds");
            assertThat(rehome.isDone()).as("the re-home waits for the verb").isFalse();
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        Resp r = rehome.get(60, TimeUnit.SECONDS);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(chunksOf(x, hex)).as("nothing was moved out of the retired source").isEqualTo(1);
        assertThat(chunksOf(t, hex)).as("nothing was filed into the target").isZero();
    }

    @Timeout(180)
    @Test
    void aSupersedeArrivingWhileARehomeHoldsTheSourceRow_waitsForIt_thenRetiresTheEmptiedSource() throws Exception {
        String x = registered("rehome-first-x");
        String y = registered("rehome-first-y");
        String t = registered("rehome-first-t");
        long xid = fx.idOf(x);
        String hex = Chash.ofText("rh-rf-" + x).toHex();
        seedChunk(x, hex);
        CountDownLatch parked = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        fx.service.collectionSelectorFilter().testHooks(null, null, () -> {
            parked.countDown();
            try {
                release.await(60, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }, null);
        Future<Resp> rehome = pool.submit(() -> fx.post(REHOME, "{\"source_id\":" + xid + ",\"target\":" + json(t) + "}"));
        Future<Resp> supersede = null;
        try {
            assertThat(parked.await(60, TimeUnit.SECONDS)).as("the re-home reached its registry read and parked with the rows held").isTrue();
            supersede = pool.submit(() -> fx.post(SUPERSEDE, "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}"));
            awaitWaiting(1, supersede, "the supersede waits for the re-home's key share on " + x);
            assertThat(supersede.isDone()).as("the verb waits for the re-home in flight").isFalse();
        } finally {
            release.countDown();
            fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
        Resp r = rehome.get(60, TimeUnit.SECONDS);
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).as("the verb completes once the re-home is done").isEqualTo(200);
        assertThat(chunksOf(t, hex)).as("the chunk moved to the target").isEqualTo(1);
        assertThat(chunksOf(x, hex)).as("and left the source").isZero();
    }
}
