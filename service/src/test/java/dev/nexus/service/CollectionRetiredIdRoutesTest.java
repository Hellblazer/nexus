// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), the route boundary on the real engine: linda's D3 ruling (a retired id answers 409
 * {@code collection_superseded} naming the successor's label and id on every route; 404 {@code unknown_collection_id} only for an id
 * with no row; a retired row whose successor has no live row answers 409 with no successor fields), and the cache regression F6.
 * The stub-backed twin, which also pins F5 and the F13 edges, is {@code CollectionSelectorFilterRowsTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionRetiredIdRoutesTest {

    private static final String DELETE = "/v1/catalog/collections/delete";

    CollectionRouteFixture fx;
    /** The LOCAL_EMBED engine: the one with an embedder router, so the combined write service (write_many) is wired. */
    CollectionRouteFixture fxLocal;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        fxLocal = new CollectionRouteFixture(true);
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
        if (fxLocal != null) fxLocal.close();
    }

    private String registered(String stem) {
        String l = label("retired-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection) {
        seedChunk(collection, Chash.ofText(collection + "/chunk").toHex());
    }

    private void seedChunk(String collection, String hex) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, fx.tenant, collection, List.of(hex), List.of("retired text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
    }

    // ---- D3 ---------------------------------------------------------------------------------------------------------------------

    /** A supersede leaves a tombstone that keeps its data; its id is refused on a read, a write, a list and the delete. */
    @Test
    void d3_aSupersededRowsIdIs409OnEveryShapeOfRoute() {
        String x = registered("d3-x");
        String y = registered("d3-y");
        seedChunk(x);
        long xId = fx.idOf(x);
        long yId = fx.idOf(y);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}").status()).isEqualTo(200);

        List<Resp> answers = List.of(
            fx.get("/v1/catalog/collections/get?collection_id=" + xId),
            fx.get("/v1/taxonomy/topics?collection_id=" + xId),
            fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + xId + ",\"label\":\"t\",\"doc_count\":1}"),
            fx.post("/v1/vectors/search", "{\"collection_ids\":[" + yId + "," + xId + "],\"query\":\"q\",\"limit\":1}"),
            fx.post(DELETE, "{\"collection_id\":" + xId + "}"));
        for (Resp r : answers) {
            assertThat(r.status()).as(r.body()).isEqualTo(409);
            assertThat(r.reason()).isEqualTo("collection_superseded");
            assertThat(r.json().path("collection").asText()).isEqualTo(x);
            assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(xId);
            assertThat(r.json().path("superseded_by").asText()).as(r.body()).isEqualTo(y);
            assertThat(r.json().path("successor_collection_id").asLong(-1)).isEqualTo(yId);
        }
        assertThat(fx.exists(x)).as("the refused delete removed nothing").isTrue();
    }

    /** Deleting the successor of a tombstone that holds data leaves the tombstone naming a label nothing bears. */
    @Test
    void d3_aRetiredRowWhoseSuccessorWasDeletedIs409WithNoSuccessorFields() {
        String x = registered("d3-nosucc-x");
        String y = registered("d3-nosucc-y");
        seedChunk(x);
        long xId = fx.idOf(x);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}").status()).isEqualTo(200);
        assertThat(fx.post(DELETE, "{\"name\":" + json(y) + "}").status()).isEqualTo(200);
        assertThat(fx.exists(x)).as("setup: the tombstone holds data, so it outlives its successor").isTrue();

        Resp r = fx.get("/v1/catalog/collections/get?collection_id=" + xId);

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(xId);
        assertThat(r.json().has("superseded_by")).as("no successor fields: %s", r.body()).isFalse();
        assertThat(r.json().has("successor_collection_id")).isFalse();

        // the successor's label is registered again, as a new row: the tombstone's text now names a live row that is not its
        // successor, and the answer stays bare (the successor is found by superseded_by_id, never by the label)
        fx.register(y);
        assertThat(fx.idOf(y)).as("setup: a new row bears the label").isNotNull();
        Resp again = fx.get("/v1/catalog/collections/get?collection_id=" + xId);
        assertThat(again.status()).as(again.body()).isEqualTo(409);
        assertThat(again.json().has("superseded_by")).as("a re-registered label is not the successor: %s", again.body()).isFalse();
        assertThat(again.json().has("successor_collection_id")).isFalse();
    }

    /** A rename of a rename: the first tombstone is not re-pointed, and its id still names the live end, by its current label. */
    @Test
    void d3_aChainOfRenamesNamesTheLiveEnd() {
        String a = registered("d3-chain-a");
        String b = label("retired-d3-chain-b-" + (n++));
        String c = label("retired-d3-chain-c-" + (n++));
        long aId = fx.idOf(a);
        assertThat(fx.post("/v1/catalog/collections/rename", "{\"old_name\":" + json(a) + ",\"new_name\":" + json(b) + "}").status())
            .isEqualTo(200);
        assertThat(fx.post("/v1/catalog/collections/rename", "{\"old_name\":" + json(b) + ",\"new_name\":" + json(c) + "}").status())
            .isEqualTo(200);
        assertThat(fx.exists(a)).as("setup: the first tombstone is still a row").isTrue();

        Resp r = fx.get("/v1/catalog/collections/get?collection_id=" + aId);

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("superseded_by").asText()).as("the live end, not the middle tombstone: %s", r.body()).isEqualTo(c);
        assertThat(r.json().path("successor_collection_id").asLong(-1)).isEqualTo(fx.idOf(c));
    }

    // ---- a retired id on the chunk and manifest writes writes nothing --------------------------------------------------------------

    @Test
    void d3_aRetiredIdOnAChunkWriteAndAManifestWriteIsRefused_andNothingIsWritten() {
        String x = registered("d3-write-x");
        String y = registered("d3-write-y");
        seedChunk(x);
        long xId = fx.idOf(x);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(x) + ",\"superseded_by\":" + json(y) + "}").status()).isEqualTo(200);
        String sentinel = Chash.ofText("never-written-" + x).toHex();
        int before = chunkCount(x);

        Resp upsert = fx.post("/v1/vectors/upsert-chunks",
            "{\"collection_id\":" + xId + ",\"ids\":[\"" + sentinel + "\"],\"documents\":[\"p35a\"]}");
        Resp manifest = fx.post("/v1/catalog/manifest/write", "{\"collection_id\":" + xId + ",\"doc_id\":\"1.1.1\",\"rows\":[]}");

        for (Resp r : List.of(upsert, manifest)) {
            assertThat(r.status()).as(r.body()).isEqualTo(409);
            assertThat(r.reason()).isEqualTo("collection_superseded");
            assertThat(r.json().path("superseded_by").asText()).isEqualTo(y);
        }
        assertThat(chunkCount(x)).as("nothing was written to the tombstone").isEqualTo(before);
        assertThat(chunkCount(y)).as("and nothing to the successor").isZero();
    }

    private int chunkCount(String collection) {
        return fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection)));
    }

    // ---- the write by id survives the gap between the boundary and the handler (linda's ruling on the route boundary) -------------------

    private void hooks(Runnable afterResolve, Runnable beforeTransactionRead, Runnable afterTransactionRead) {
        fx.service.collectionSelectorFilter().testHooks(afterResolve, beforeTransactionRead, afterTransactionRead, null);
    }

    private void noHooks() {
        hooks(null, null, null);
    }

    /**
     * Runs {@code work} on another thread and waits. A hook runs on the REQUEST's own thread, where the route boundary's named ids
     * are set, and a seed that goes through {@code CollectionIds.require} (the chunk seeds do) would read the named id there and
     * answer its 404 out of the hook, which looks like the engine refusing the write and files nothing. The seeds belong to the
     * test's own thread of work.
     */
    private static void elsewhere(Runnable work) {
        CompletableFuture.runAsync(work).join();
    }

    /** What the hook does in the gap: frees the id by deleting {@code a}, takes the label again as a new row, and files {@code hex} in it. */
    private Runnable freeTheIdAndTakeTheLabel(String a, String hex, AtomicInteger deleteStatus) {
        return () -> elsewhere(() -> {
            deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status());
            fx.register(a);
            seedChunk(a, hex);
        });
    }

    private int chunksOf(String collection, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        return fx.asSuperuser(ctx -> (int) ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection).and(CHUNKS.CHASH.eq(chash))));
    }

    /** A write by id with the id live all the way through: the handler's by-id read (FOR KEY SHARE) answers, and the row lands. */
    @Test
    void aWriteByIdWhoseRowStaysLiveLandsUnderItsLabel() {
        String a = registered("gap-control");
        long x = fx.idOf(a);
        Resp r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"gap-topic\",\"doc_count\":1}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a).and(TOPICS.COLLECTION_ID.eq(x)))))
            .as("the topic is filed under the label and the id of the row asked for").isEqualTo(1);
    }

    /**
     * The gap: the boundary resolves id X to label A; before the handler runs, X is deleted and A is registered again as a new row
     * Z. A handler that went back to the label would write under Z. It reads X itself, so the write answers 404
     * {@code unknown_collection_id} and nothing lands in Z. The hook runs between the boundary and the handler, so the order is
     * fixed and no timing is involved.
     */
    @Test
    void aWriteByAnIdFreedBetweenTheBoundaryAndTheHandlerIsRefused_andNothingLandsInTheNewHolderOfItsLabel() {
        String a = registered("gap-window");
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        hooks(() -> {
            if (fired.getAndIncrement() == 0) {   // the delete below passes through the boundary too: only the first call acts
                elsewhere(() -> {
                    deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status());
                    fx.register(a);
                });
            }
        }, null, null);
        Resp r;
        try {
            r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"gap-topic\",\"doc_count\":1}");
        } finally {
            noHooks();
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        long z = fx.idOf(a);
        assertThat(z).as("setup: the label is now another row's").isNotEqualTo(x);
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a))))
            .as("nothing landed in the new holder of the label").isZero();
    }

    /** The same gap, with the id retired (not freed) in it: the handler's by-id read answers the retired row's 409. */
    @Test
    void aWriteByAnIdRetiredBetweenTheBoundaryAndTheHandlerAnswers409NamingTheSuccessor() {
        String a = registered("gap-retire-a");
        String b = registered("gap-retire-b");
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger supersedeStatus = new AtomicInteger();
        hooks(() -> {
            if (fired.getAndIncrement() == 0) {
                supersedeStatus.set(fx.post("/v1/catalog/collections/supersede",
                    "{\"name\":" + json(a) + ",\"superseded_by\":" + json(b) + "}").status());
            }
        }, null, null);
        Resp r;
        try {
            r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"gap-topic\",\"doc_count\":1}");
        } finally {
            noHooks();
        }
        assertThat(supersedeStatus.get()).as("setup").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("superseded_by").asText()).isEqualTo(b);
        assertThat(r.json().path("successor_collection_id").asLong(-1)).isEqualTo(fx.idOf(b));
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a)))).isZero();
    }

    // ---- the four routes whose write used to go on by label, and a lookup(scope) route -----------------------------------------------

    /**
     * The window the first hook cannot see: every outside-a-transaction lookup of the handler has PASSED (the id was live), and
     * the id is freed and its label taken again just before the write's own transaction reads the id. The second hook runs there.
     * A write that went on by label from the lookups would land in the new holder; each of these must answer 404 and leave the new
     * holder's rows as they were.
     */
    private Resp withTheLabelTakenBeforeTheWritesRead(String a, String hex, Supplier<Resp> request) {
        AtomicInteger deleteStatus = new AtomicInteger();
        hooks(null, freeTheIdAndTakeTheLabel(a, hex, deleteStatus), null);
        try {
            Resp r = request.get();
            assertThat(deleteStatus.get()).as("setup: the hook ran, freeing the id").isEqualTo(200);
            return r;
        } finally {
            noHooks();
        }
    }

    private void assertUnknownId(Resp r, long x) {
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
    }

    @Test
    void upsertChunks_byAnIdFreedAfterTheLookupsLandsNothingInTheNewHolder() {
        String a = registered("gap-upsert");
        long x = fx.idOf(a);
        String sentinel = Chash.ofText("gap-upsert-sentinel-" + a).toHex();
        String fresh = Chash.ofText("gap-upsert-fresh-" + a).toHex();
        // control: the same request by id, with nothing in the way, lands
        Resp control = fx.post("/v1/vectors/upsert-chunks",
            "{\"collection_id\":" + x + ",\"ids\":[\"" + fresh + "\"],\"documents\":[\"control text\"]}");
        assertThat(control.status()).as("control: %s", control.body()).isEqualTo(200);
        assertThat(chunksOf(a, fresh)).as("control: the chunk landed").isEqualTo(1);

        String hex = Chash.ofText("gap-upsert-gap-" + a).toHex();
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, sentinel, () -> fx.post("/v1/vectors/upsert-chunks",
            "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"],\"documents\":[\"gap text\"]}"));

        assertUnknownId(r, x);
        assertThat(chunksOf(a, hex)).as("the new holder of the label got no chunk").isZero();
        assertThat(chunksOf(a, sentinel)).as("and kept the one the hook filed").isEqualTo(1);
    }

    @Test
    void storeDelete_byAnIdFreedAfterTheLookupsDeletesNothingOfTheNewHolder() {
        String a = registered("gap-sdel");
        long x = fx.idOf(a);
        String hex = Chash.ofText("gap-sdel-" + a).toHex();
        seedChunk(a, hex);
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, hex, () -> fx.post("/v1/vectors/store-delete",
            "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"]}"));
        assertUnknownId(r, x);
        assertThat(chunksOf(a, hex)).as("the chunk the new holder of the label holds is still there").isEqualTo(1);
    }

    @Test
    void updateMetadata_byAnIdFreedAfterTheLookupsChangesNothingOfTheNewHolder() {
        String a = registered("gap-meta");
        long x = fx.idOf(a);
        String hex = Chash.ofText("gap-meta-" + a).toHex();
        seedChunk(a, hex);
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, hex, () -> fx.post("/v1/vectors/update-metadata",
            "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"],\"metadatas\":[{\"gap_key\":\"gap\"}]}"));
        assertUnknownId(r, x);
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        String metadata = fx.asSuperuser(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(a).and(CHUNKS.CHASH.eq(chash))).fetchOne(CHUNKS.METADATA).data());
        assertThat(metadata).as("the new holder's chunk was not touched").doesNotContain("gap_key");
    }

    @Test
    void assignFromChashes_byAnIdFreedAfterTheLookupsAnswers404() {
        String a = registered("gap-assign");
        long x = fx.idOf(a);
        String hex = Chash.ofText("gap-assign-" + a).toHex();
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, hex, () -> fx.post("/v1/taxonomy/assignments/assign_from_chashes",
            "{\"collection_id\":" + x + ",\"chashes\":[\"" + hex + "\"],\"cross_collection\":false}"));
        assertUnknownId(r, x);
    }

    /** A route that reads the collection through the outside-a-transaction lookup first: the id freed before the handler runs. */
    @Test
    void aRouteWhoseLookupIsOutsideATransaction_answers404ForAnIdFreedBeforeTheHandler() {
        String a = registered("gap-lookup");
        long x = fx.idOf(a);
        String hex = Chash.ofText("gap-lookup-" + a).toHex();
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger deleteStatus = new AtomicInteger();
        hooks(() -> {
            if (fired.getAndIncrement() == 0) {
                elsewhere(() -> {
                    deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status());
                    fx.register(a);
                    seedChunk(a, hex);
                });
            }
        }, null, null);
        Resp r;
        try {
            r = fx.post("/v1/vectors/store-delete", "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"]}");
        } finally {
            noHooks();
        }
        assertThat(deleteStatus.get()).as("setup").isEqualTo(200);
        assertUnknownId(r, x);
        assertThat(chunksOf(a, hex)).as("the new holder's chunk is still there").isEqualTo(1);
    }

    // ---- write_many and append_many answer the typed 404 and 409, not a 200 with every document failed ---------------------------

    private String writeManyBody(long id) {
        String hex = Chash.ofText("gap-write-many").toHex();
        return "{\"collection_id\":" + id + ",\"chunks\":[{\"chash\":\"" + hex + "\",\"text\":\"t\",\"metadata\":{}}],"
            + "\"docs\":[{\"doc_id\":\"1.1.1\",\"rows\":[{\"position\":0,\"chash\":\"" + hex + "\",\"chunk_index\":0}]}]}";
    }

    /**
     * write_many needs the combined write service, which the engine builds only with an embedder router: the LOCAL_EMBED fixture
     * (an {@code EmbedderRouter} over a minilm embedder), as the census builds it for the embed routes. The refusal sits in
     * {@code refuseRowsUnderSupersededCollection}, which used to swallow every refusal but the by-label one into a per-document
     * failure and a 200.
     */
    @Test
    void writeMany_byAnIdFreedAfterTheBoundaryAnswers404_andByAnIdRetiredThere409() {
        String a = label("retired-wm-free-" + (n++));
        fxLocal.register(a);
        long x = fxLocal.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        fxLocal.service.collectionSelectorFilter().testHooks(() -> {
            if (fired.getAndIncrement() == 0) {
                elsewhere(() -> {
                    fxLocal.post(DELETE, "{\"name\":" + json(a) + "}");
                    fxLocal.register(a);
                });
            }
        }, null, null, null);
        Resp freed;
        try {
            freed = fxLocal.post("/v1/catalog/manifest/write_many", writeManyBody(x));
        } finally {
            fxLocal.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
        assertUnknownId(freed, x);

        String b = label("retired-wm-ret-a-" + (n++));
        String c = label("retired-wm-ret-b-" + (n++));
        fxLocal.register(b, c);
        long y = fxLocal.idOf(b);
        AtomicInteger fired2 = new AtomicInteger();
        fxLocal.service.collectionSelectorFilter().testHooks(() -> {
            if (fired2.getAndIncrement() == 0) {
                elsewhere(() -> fxLocal.post("/v1/catalog/collections/supersede",
                    "{\"name\":" + json(b) + ",\"superseded_by\":" + json(c) + "}"));
            }
        }, null, null, null);
        Resp retired;
        try {
            retired = fxLocal.post("/v1/catalog/manifest/write_many", writeManyBody(y));
        } finally {
            fxLocal.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
        assertThat(retired.status()).as(retired.body()).isEqualTo(409);
        assertThat(retired.reason()).isEqualTo("collection_superseded");
        assertThat(retired.json().path("superseded_by").asText()).isEqualTo(c);
    }

    // ---- FOR KEY SHARE ----------------------------------------------------------------------------------------------------------------

    /**
     * The lock itself: the write's by-id read holds FOR KEY SHARE on the row until the write commits, so a delete of the row waits.
     * The hook runs with the read done and the transaction open; it starts a delete of the collection from another thread and waits
     * until PostgreSQL shows an ungranted lock (pg_locks), then lets the write finish. The write answers 200 and the delete, which
     * could not take the row while the write held it, answers 200 after it. The route is update-metadata on an empty collection, so
     * the write touches no row the delete also needs.
     */
    @Test
    void aWritesByIdReadHoldsTheRowAgainstADeleteUntilTheWriteCommits() throws Exception {
        String a = registered("keyshare");
        long x = fx.idOf(a);
        AtomicBoolean waited = new AtomicBoolean();
        CompletableFuture<Resp>[] delete = new CompletableFuture[1];
        hooks(null, null, () -> {
            delete[0] = CompletableFuture.supplyAsync(() -> fx.post(DELETE, "{\"name\":" + json(a) + "}"));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
            while (System.nanoTime() < deadline) {
                int ungranted = fx.asSuperuser(ctx -> ctx.selectCount()
                    .from(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("pg_catalog", "pg_locks")))
                    .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("granted"), Boolean.class).isFalse())
                    .fetchOne(0, int.class));
                if (ungranted > 0) {
                    waited.set(true);
                    return;
                }
                Thread.onSpinWait();
            }
        });
        Resp write;
        try {
            write = fx.post("/v1/vectors/update-metadata", "{\"collection_id\":" + x + ",\"ids\":[\""
                + Chash.ofText("keyshare-" + a).toHex() + "\"],\"metadatas\":[{}]}");
        } finally {
            noHooks();
        }
        assertThat(waited.get()).as("the delete of the row was waiting while the write's transaction was open (pg_locks)").isTrue();
        assertThat(write.status()).as(write.body()).isEqualTo(200);
        Resp deleted = delete[0].get(30, TimeUnit.SECONDS);
        assertThat(deleted.status()).as("the delete went through once the write had committed: %s", deleted.body()).isEqualTo(200);
        assertThat(fx.exists(a)).isFalse();
    }
}
