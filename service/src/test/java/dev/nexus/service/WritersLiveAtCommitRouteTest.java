// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), linda's option (b), on the production writers through their routes: the registry check every
 * WRITE transaction runs takes the collection's row {@code FOR KEY SHARE} first (after its advisory locks), for a request by label
 * as for a request by id, so a retiring verb's {@code FOR UPDATE} waits for every writer in flight and a writer that arrives after the
 * verb waits for it and reads the retired row. {@code RetiringVerbsLiveAtCommitTest} proves the verbs with hand-rolled writer
 * sessions; these are the real writers.
 *
 * <p>The verb is the real {@code POST /v1/catalog/collections/supersede}, parked by a row the test holds: it locks X and Y
 * {@code FOR UPDATE} in {@code collection_id} order, so with Y held by the test the verb holds X and waits. The writer then
 * arrives, and the test goes on only when {@code pg_locks} shows each side waiting (no sleeps). History writes (a relevance log row, a
 * hook failure) are exempt and must not wait for the parked verb.
 *
 * <p>Not covered here, named: the fenced aspect upsert (a claim in the tuple space is needed to reach its tuples-row lock), and a
 * label-keyed writer parked in flight (no seam inside its transaction).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class WritersLiveAtCommitRouteTest {

    private static final String SUPERSEDE = "/v1/catalog/collections/supersede";

    CollectionRouteFixture fx;
    ExecutorService pool;
    int n;

    /** Every retry {@code DeadlockRetry} made while a test ran: the belt turns a 40P01 into a clean answer, so each test asserts none. */
    private final java.util.concurrent.ConcurrentLinkedQueue<String> retries = new java.util.concurrent.ConcurrentLinkedQueue<>();

    @org.junit.jupiter.api.BeforeEach
    void listenForRetries() {
        retries.clear();
        dev.nexus.service.db.DeadlockRetry.setRetryListenerForTests(retries::add);
    }

    @org.junit.jupiter.api.AfterEach
    void noDeadlockWasAbsorbed() {
        dev.nexus.service.db.DeadlockRetry.setRetryListenerForTests(null);
        assertThat(retries).as("the lock order prevents a deadlock: DeadlockRetry absorbed none (a retried 40P01 ends in a clean answer)")
            .isEmpty();
    }

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

    private String registered(String stem) {
        String l = label("w1-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    // ---- parking and waiting (pg_locks, no sleeps) ----------------------------------------------------------------------------

    /** How many lock requests wait on a row or a transaction now. */
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

    /**
     * The real supersede of {@code x} by {@code y}, parked: {@code park} holds Y's row {@code FOR UPDATE}, so the verb (which locks
     * both rows in id order, X first because it was registered first) holds X and waits for Y. Release by rolling {@code park} back.
     */
    private Future<Resp> parkedSupersede(Connection park, String x, String y) throws Exception {
        park.setAutoCommit(false);
        DSL.using(park, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.NAME).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(y)).forUpdate().fetch();
        Future<Resp> supersede = pool.submit(() -> fx.post(SUPERSEDE, "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}"));
        awaitWaiting(1, supersede, "the supersede parks on the successor's row, holding X");
        return supersede;
    }

    private int chunksOf(String collection, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        return fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection).and(CHUNKS.CHASH.eq(chash))));
    }

    private void seedChunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("w1 text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
    }

    private void assertRefusedAsRetired(Resp w, String successor) {
        assertThat(w.status()).as(w.body()).isEqualTo(409);
        assertThat(w.reason()).isEqualTo("collection_superseded");
        assertThat(w.json().path("superseded_by").asText()).isEqualTo(successor);
    }

    // ---- a writer arriving while the verb holds the row: it waits, then reads the retired row ---------------------------------

    @Timeout(180)
    @Test
    void aChunkWriteByLabelArrivingWhileASupersedeHoldsTheRow_waits_thenIsRefused409_andWritesNothing() throws Exception {
        String x = registered("chunk-x");
        String y = registered("chunk-y");
        String hex = Chash.ofText("w1-chunk-" + x).toHex();
        Future<Resp> supersede;
        Future<Resp> writer;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            writer = pool.submit(() -> fx.post("/v1/vectors/upsert-chunks",
                "{\"collection\":" + json(x) + ",\"ids\":[\"" + hex + "\"],\"documents\":[\"w1 text\"]}"));
            awaitWaiting(2, writer, "the chunk writer waits for the registry row of " + x + " (a label request takes the key share too)");
            assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        assertRefusedAsRetired(writer.get(60, TimeUnit.SECONDS), y);
        assertThat(chunksOf(x, hex)).as("nothing was written into the retired row").isZero();
        assertThat(chunksOf(y, hex)).as("nor into the successor").isZero();
    }

    @Timeout(180)
    @Test
    void aManifestWriteByLabelArrivingWhileASupersedeHoldsTheRow_waits_thenIsRefused409_andWritesNothing() throws Exception {
        String x = registered("manifest-x");
        String y = registered("manifest-y");
        String hex = Chash.ofText("w1-manifest-" + x).toHex();
        String doc = "w1-doc-" + x;
        seedChunk(x, hex);
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, x);
            return null;
        });
        Future<Resp> supersede;
        Future<Resp> writer;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            writer = pool.submit(() -> fx.post("/v1/catalog/manifest/write", "{\"collection\":" + json(x) + ",\"doc_id\":" + json(doc)
                + ",\"rows\":[{\"position\":0,\"chash\":\"" + hex + "\",\"chunk_index\":0}]}"));
            awaitWaiting(2, writer, "the manifest writer waits for the registry row of " + x);
            assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        assertRefusedAsRetired(writer.get(60, TimeUnit.SECONDS), y);
        int manifestRows = fx.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)));
        assertThat(manifestRows).as("no manifest row was written for the document").isZero();
    }

    @Timeout(180)
    @Test
    void anAspectUpsertByLabelArrivingWhileASupersedeHoldsTheRow_waits_thenIsRefused409_andWritesNothing() throws Exception {
        String x = registered("aspect-x");
        String y = registered("aspect-y");
        String doc = "w1-aspect-doc-" + x;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, x);
            return null;
        });
        Future<Resp> supersede;
        Future<Resp> writer;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            writer = pool.submit(() -> fx.post("/v1/aspects/upsert", "{\"collection\":" + json(x) + ",\"source_path\":\"w1.md\",\"doc_id\":"
                + json(doc) + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\",\"model_version\":\"v1\","
                + "\"extractor_name\":\"ex\"}"));
            awaitWaiting(2, writer, "the aspect writer waits for the registry row of " + x);
            assertThat(writer.isDone()).as("the writer waits for the verb").isFalse();
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        assertRefusedAsRetired(writer.get(60, TimeUnit.SECONDS), y);
        int aspectRows = fx.asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.COLLECTION.eq(x)));
        assertThat(aspectRows).as("no aspect row was written").isZero();
    }

    /**
     * {@code mergeTopics} moves assignment rows keyed by topic ids, so it names no collection on the wire, but the topics belong to
     * one: it reads that collection's registry row FOR KEY SHARE before any assignment row (review of 449d5d8b03), so it waits for a
     * supersede that holds the row. It is not refused for the retirement (a merge of a tombstone's own topics is a clean-up the
     * registry has no objection to): it waits, then completes.
     */
    @Timeout(180)
    @Test
    void aTopicMergeArrivingWhileASupersedeHoldsTheCollectionsRow_waits_thenCompletes() throws Exception {
        String x = registered("merge-x");
        String y = registered("merge-y");
        Resp a = fx.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(x) + ",\"label\":\"merge-a\",\"doc_count\":1}");
        Resp b = fx.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(x) + ",\"label\":\"merge-b\",\"doc_count\":1}");
        long sourceId = a.json().path("id").asLong(-1);
        long targetId = b.json().path("id").asLong(-1);
        assertThat(sourceId).as("setup: " + a.body()).isPositive();
        assertThat(targetId).as("setup: " + b.body()).isPositive();
        Future<Resp> supersede;
        Future<Resp> merge;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            merge = pool.submit(() -> fx.post("/v1/taxonomy/topics/merge", "{\"source_id\":" + sourceId + ",\"target_id\":" + targetId + "}"));
            awaitWaiting(2, merge, "the merge waits for the registry row of " + x);
            assertThat(merge.isDone()).as("the merge waits for the verb").isFalse();
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        Resp m = merge.get(60, TimeUnit.SECONDS);
        assertThat(m.status()).as(m.body()).isEqualTo(200);
    }

    // ---- history writes are exempt: they stamp the id without a lock and do not wait for the verb -----------------------------

    @Timeout(180)
    @Test
    void aHistoryWriteDuringAParkedSupersedeDoesNotWait_andStampsTheLiveRowsId() throws Exception {
        String x = registered("history-x");
        String y = registered("history-y");
        long xid = fx.idOf(x);
        String hex = Chash.ofText("w1-history-" + x).toHex();
        Future<Resp> supersede;
        try (Connection park = fx.pg.createConnection("")) {
            supersede = parkedSupersede(park, x, y);
            Future<Resp> relevance = pool.submit(() -> fx.post("/v1/telemetry/relevance/log", "{\"query\":\"q\",\"chunk_id\":\"" + hex
                + "\",\"action\":\"click\",\"session_id\":\"s\",\"collection\":" + json(x) + "}"));
            Future<Resp> failure = pool.submit(() -> fx.post("/v1/telemetry/hook_failures/record",
                "{\"hook_name\":\"w1-hook\",\"doc_id\":\"d\",\"collection\":" + json(x) + ",\"error\":\"e\"}"));
            Resp r = relevance.get(30, TimeUnit.SECONDS);
            Resp f = failure.get(30, TimeUnit.SECONDS);
            assertThat(supersede.isDone()).as("guard: the verb is still parked while the history writes answered").isFalse();
            assertThat(r.status()).as(r.body()).isEqualTo(200);
            assertThat(f.status()).as(f.body()).isEqualTo(200);
            park.rollback();
        }
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).isEqualTo(200);
        List<Long> relevanceIds = fx.asSuperuser(ctx -> ctx.select(RELEVANCE_LOG.COLLECTION_ID).from(RELEVANCE_LOG)
            .where(RELEVANCE_LOG.COLLECTION.eq(x)).fetch(RELEVANCE_LOG.COLLECTION_ID));
        List<Long> failureIds = fx.asSuperuser(ctx -> ctx.select(HOOK_FAILURES.COLLECTION_ID).from(HOOK_FAILURES)
            .where(HOOK_FAILURES.COLLECTION.eq(x)).fetch(HOOK_FAILURES.COLLECTION_ID));
        assertThat(relevanceIds).as("the relevance row carries the id of the row the label named when it was written").containsExactly(xid);
        assertThat(failureIds).as("and so does the hook failure").containsExactly(xid);
    }

    // ---- the reverse order: a writer by id in flight holds the verb back ---------------------------------------------------------

    /**
     * The id request's write transaction holds the key share from the registry check (the by-id read, now the first statement of
     * the upsert's metadata transaction) until it commits, so a supersede issued while it is parked waits. The request is parked with
     * the lock held ({@code afterTransactionRead}); the verb shows as waiting in {@code pg_locks}; the writer is released. Two outcomes
     * are right: the writer's next transaction (the batch insert) reads the row before the verb takes it and the chunk lands inside the
     * row that is then retired, or it reads the retired row and is refused 409 with nothing written. The verb answers 200 either way.
     */
    @Timeout(180)
    @Test
    void aSupersedeWaitsForAChunkWriteByIdInFlight_thenTheChunkIsInsideOrTheWriteIsRefused() throws Exception {
        String x = registered("byid-x");
        String y = registered("byid-y");
        long xid = fx.idOf(x);
        String hex = Chash.ofText("w1-byid-" + x).toHex();
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
        Future<Resp> writer = pool.submit(() -> fx.post("/v1/vectors/upsert-chunks",
            "{\"collection_id\":" + xid + ",\"ids\":[\"" + hex + "\"],\"documents\":[\"w1 text\"]}"));
        Future<Resp> supersede = null;
        try {
            assertThat(parked.await(60, TimeUnit.SECONDS)).as("the writer reached its registry check and parked with the lock held").isTrue();
            supersede = pool.submit(() -> fx.post(SUPERSEDE, "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}"));
            awaitWaiting(1, supersede, "the supersede waits for the writer's key share on " + x);
            assertThat(supersede.isDone()).as("the verb waits for the writer in flight").isFalse();
        } finally {
            release.countDown();
            fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
        Resp w = writer.get(60, TimeUnit.SECONDS);
        assertThat(supersede.get(60, TimeUnit.SECONDS).status()).as("the verb completes once the writer is done").isEqualTo(200);
        if (w.status() == 200) {
            assertThat(chunksOf(x, hex)).as("the write landed inside the row that was then retired").isEqualTo(1);
        } else {
            assertRefusedAsRetired(w, y);
            assertThat(chunksOf(x, hex)).as("a refused write wrote nothing").isZero();
        }
    }
}
