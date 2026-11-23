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

    @Test
    void d3_anIdWithNoRowIsStillThe404() {
        Resp r = fx.get("/v1/catalog/collections/get?collection_id=" + CollectionRouteCensus.UNKNOWN_ID);
        assertThat(r.status()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
    }

    // ---- F6: a3ebfe6605, the boundary reads the process-wide cache (restored: P3.5a review F6 found these gone) -------------------

    /**
     * a3ebfe6605 made the route boundary read the registry cache every evict reaches, not a cache of its own. Revert it and a
     * request by id keeps a row the delete evicted: this fails on the last GET, which answers 200 with the re-registered row's
     * label for an id that row never had. The sequence: GET by id fills the cache; delete X by label; register X again (a new
     * row, a new id); GET by the old id must not return the new row.
     */
    @Test
    void f6_afterADeleteAndAReRegistration_theOldIdDoesNotReturnTheNewRow() {
        String x = registered("f6-del");
        long oldId = fx.idOf(x);
        Resp first = fx.get("/v1/catalog/collections/get?collection_id=" + oldId);
        assertThat(first.status()).as("setup: the GET by id fills the cache: %s", first.body()).isEqualTo(200);

        assertThat(fx.post(DELETE, "{\"name\":" + json(x) + "}").status()).isEqualTo(200);
        fx.register(x);
        long newId = fx.idOf(x);
        assertThat(newId).as("setup: the re-registration is a new row").isNotEqualTo(oldId);

        Resp stale = fx.get("/v1/catalog/collections/get?collection_id=" + oldId);
        assertThat(stale.status()).as("the old id has no row: %s", stale.body()).isEqualTo(404);
        assertThat(stale.reason()).isEqualTo("unknown_collection_id");
        Resp fresh = fx.get("/v1/catalog/collections/get?collection_id=" + newId);
        assertThat(fresh.status()).as(fresh.body()).isEqualTo(200);
        assertThat(fresh.json().path("name").asText()).isEqualTo(x);
    }

    /** The same revert, by a rename: the cached row of the old id must not outlive the rename that retired it. */
    @Test
    void f6_afterARename_theOldIdIsTheRetiredRowAndNotTheCachedLiveOne() {
        String from = registered("f6-ren");
        String to = label("retired-f6-ren-to-" + (n++));
        long oldId = fx.idOf(from);
        assertThat(fx.get("/v1/catalog/collections/get?collection_id=" + oldId).status()).as("setup: fills the cache").isEqualTo(200);

        Resp ren = fx.post("/v1/catalog/collections/rename", "{\"old_name\":" + json(from) + ",\"new_name\":" + json(to) + "}");
        assertThat(ren.status()).as("setup: " + ren.body()).isEqualTo(200);
        assertThat(fx.idOf(to)).as("setup: the name path gives the collection a new id").isNotEqualTo(oldId);

        Resp r = fx.get("/v1/catalog/collections/get?collection_id=" + oldId);
        assertThat(r.status()).as("a cached live row would answer 200: %s", r.body()).isEqualTo(409);
        assertThat(r.json().path("superseded_by").asText()).isEqualTo(to);
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

    // ---- the RENAME variant of the gap: the id is the identity (linda's ruling) -----------------------------------------------------

    /**
     * The request names B by id X, and the id alone: the route boundary resolves it to the label B. Just before the write transaction's
     * registry read (the hook, on another thread) a one-row rename gives X's row the label C (the registry row keeps its id, which is
     * what P3.7's rename does and what the scoped-delete test simulates in place), and a NEW row takes the label B. The registry cache
     * holds B as X's row (warmed before the request, so a known cache hit exists and a writer that went back to the label would write
     * under the new B). The write reads the row of X by id FOR KEY SHARE and the row is live, so the write LANDS in the renamed
     * collection under its CURRENT label C, with X's id; nothing lands under the new B's id. No refusal: a rename in the gap changes
     * nothing about where an id-only write goes.
     */
    private Resp withTheRowRenamedAndTheLabelTaken(CollectionRouteFixture f, String b, long x, String c, Supplier<Resp> request) {
        return withTheRowRenamedAndTheLabelTaken(f, b, x, c, null, request);
    }

    /**
     * As above, and {@code afterRename} (when not null) runs in the hook's thread once the row is renamed and the old label taken: the
     * place to store something under the row's NEW label. A chunk stored before the rename would block it (the chunk's foreign key to the
     * registry is ON UPDATE NO ACTION), so a test that needs a stored chunk files it here, under C, where the write will look for it.
     */
    private Resp withTheRowRenamedAndTheLabelTaken(CollectionRouteFixture f, String b, long x, String c, Runnable afterRename,
                                                   Supplier<Resp> request) {
        // warm the process-wide cache for the label: a by-label lookup fills B -> X's row, and the rename below does not evict it
        dev.nexus.service.db.CollectionRegistry.lookup(new dev.nexus.service.db.TenantScope(f.svcDs), f.tenant, b);
        assertThat(dev.nexus.service.db.CollectionRegistry.cached(f.tenant, b)).as("setup: the cache is warm for " + b).isPresent();
        AtomicInteger ran = new AtomicInteger();
        f.service.collectionSelectorFilter().testHooks(null, () -> elsewhere(() -> {
            f.asSuperuser(ctx -> ctx.update(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
                .set(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME, c)
                .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(f.tenant))
                .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.COLLECTION_ID.eq(x)).execute());
            f.register(b);
            if (afterRename != null) {
                afterRename.run();
            }
            ran.incrementAndGet();
        }), null, null);
        try {
            Resp r = request.get();
            assertThat(ran.get()).as("setup: the hook renamed the row and registered a new one under " + b).isEqualTo(1);
            Long newId = f.idOf(b);
            assertThat(newId).as("setup: the old label now names another row").isNotNull().isNotEqualTo(x);
            assertThat(f.idOf(c)).as("setup: the renamed row bears " + c).isEqualTo(x);
            return r;
        } finally {
            f.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
    }

    /** The chunk rows with {@code collection_id} X: each is stored under label {@code c}, X's current one. */
    private void assertChunkLandedUnderTheCurrentLabel(CollectionRouteFixture f, long x, String c, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        java.util.List<String> labels = f.asSuperuser(ctx -> ctx.select(CHUNKS.COLLECTION).from(CHUNKS)
            .where(CHUNKS.COLLECTION_ID.eq(x)).and(CHUNKS.CHASH.eq(chash)).fetch(CHUNKS.COLLECTION));
        assertThat(labels).as("the chunk landed in the renamed collection, under its new label").containsExactly(c);
    }

    private int chunkRowsOfTheNewHolder(CollectionRouteFixture f, String b) {
        Long newId = f.idOf(b);
        return f.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION_ID.eq(newId)));
    }

    @Test
    void upsertChunks_byAnIdWhoseRowWasRenamedAndItsLabelTaken_landsInTheRenamedCollection_withTheCacheWarm() {
        String b = registered("rn-gap-b");
        String c = label("retired-rn-gap-c-" + (n++));
        long x = fx.idOf(b);
        String hex = Chash.ofText("rn-gap-upsert-" + b).toHex();
        Resp r = withTheRowRenamedAndTheLabelTaken(fx, b, x, c, () -> fx.post("/v1/vectors/upsert-chunks",
            "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"],\"documents\":[\"rename gap text\"]}"));
        assertThat(r.status()).as("an id-only write follows the rename: %s", r.body()).isEqualTo(200);
        assertChunkLandedUnderTheCurrentLabel(fx, x, c, hex);
        assertThat(chunkRowsOfTheNewHolder(fx, b)).as("nothing under the new holder of the old label").isZero();
    }

    @Test
    void writeMany_byAnIdWhoseRowWasRenamedAndItsLabelTaken_landsInTheRenamedCollection() {
        String b = label("retired-rn-wm-b-" + (n++));
        String c = label("retired-rn-wm-c-" + (n++));
        String host = label("retired-rn-wm-host-" + (n++));
        fxLocal.register(b, host);
        long x = fxLocal.idOf(b);
        String hex = Chash.ofText("rn-gap-write-many-" + b).toHex();
        String docJson = docReferencing(fxLocal, host, b, hex);
        Resp r = withTheRowRenamedAndTheLabelTaken(fxLocal, b, x, c, () -> fxLocal.post("/v1/catalog/manifest/write_many",
            "{\"collection_id\":" + x + ",\"chunks\":[{\"chash\":\"" + hex + "\",\"text\":\"rename gap text\",\"metadata\":{}}],"
                + "\"docs\":[" + docJson + "]}"));
        assertThat(r.status()).as("an id-only write follows the rename: %s", r.body()).isEqualTo(200);
        assertThat(r.json().path("failed_doc_ids").size()).as("no document failed: %s", r.body()).isZero();
        assertChunkLandedUnderTheCurrentLabel(fxLocal, x, c, hex);
        assertThat(chunkRowsOfTheNewHolder(fxLocal, b)).as("nothing under the new holder of the old label").isZero();
    }

    /**
     * The manifest-only {@code write_many} (no {@code chunks} key) has no metadata refresh transaction: its first write transaction is
     * the per-document one, which takes the gate of the label it is given, reads the registry row, sees the row renamed and runs again with
     * the new label's gate first. The chunk is STORED in the collection (seeded before the rename), so the document's manifest row has a
     * chunk to reference and the write must land: the manifest row is filed under the row's CURRENT label with X's id, nothing under the new
     * holder of the old label, and no document fails.
     */
    @Test
    void writeManyWithDocsOnly_byAnIdWhoseRowWasRenamedAndItsLabelTaken_landsTheManifestRowUnderTheNewLabel() {
        String b = label("retired-rn-wmd-b-" + (n++));
        String c = label("retired-rn-wmd-c-" + (n++));
        String host = label("retired-rn-wmd-host-" + (n++));
        fxLocal.register(b, host);
        long x = fxLocal.idOf(b);
        String doc = "rn-gap-doc-" + b;
        String hex = Chash.ofText("rn-gap-wmd-" + b).toHex();
        fxLocal.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fxLocal.tenant, doc, host);
            return null;
        });
        // the chunk is stored under the NEW label, filed in the hook once the row is renamed (a chunk stored under B first would block the
        // in-place rename: its foreign key to the registry is ON UPDATE NO ACTION)
        Resp r = withTheRowRenamedAndTheLabelTaken(fxLocal, b, x, c, () -> seedChunkIn(fxLocal, c, hex), () -> fxLocal.post("/v1/catalog/manifest/write_many",
            "{\"collection_id\":" + x + ",\"docs\":[{\"doc_id\":" + json(doc) + ",\"rows\":[{\"position\":0,\"chash\":\"" + hex
                + "\",\"chunk_index\":0}]}]}"));
        assertThat(r.status()).as("not refused for the rename: %s", r.body()).isEqualTo(200);
        assertThat(r.json().path("failed_doc_ids").size()).as("no document failed: %s", r.body()).isZero();
        var chunks = dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
        java.util.List<String> labels = fxLocal.asSuperuser(ctx -> ctx.select(chunks.COLLECTION).from(chunks)
            .where(chunks.DOC_ID.eq(doc)).and(chunks.COLLECTION_ID.eq(x)).fetch(chunks.COLLECTION));
        assertThat(labels).as("the manifest row landed under the renamed row's current label, with its id").containsExactly(c);
        Long newId = fxLocal.idOf(b);
        int underTheNewHolder = fxLocal.asSuperuser(ctx -> ctx.fetchCount(chunks, chunks.COLLECTION_ID.eq(newId)));
        assertThat(underTheNewHolder).as("no manifest row under the new holder").isZero();
    }

    /** The topic rows of X are filed under C, X's CURRENT label, and the new holder of B has none. */
    @Test
    void topicInsert_byAnIdWhoseRowWasRenamedAndItsLabelTaken_landsInTheRenamedCollection() {
        String b = registered("rn-gap-topic-b");
        String c = label("retired-rn-gap-topic-c-" + (n++));
        long x = fx.idOf(b);
        Resp topic = withTheRowRenamedAndTheLabelTaken(fx, b, x, c, () -> fx.post("/v1/taxonomy/topics/insert",
            "{\"collection_id\":" + x + ",\"label\":\"rn-gap-topic\",\"doc_count\":1}"));
        assertThat(topic.status()).as("an id-only write follows the rename: %s", topic.body()).isEqualTo(200);
        java.util.List<String> labels = fx.asSuperuser(ctx -> ctx.select(TOPICS.COLLECTION).from(TOPICS).where(TOPICS.COLLECTION_ID.eq(x))
            .fetch(TOPICS.COLLECTION));
        assertThat(labels).as("the topic is the renamed collection's, under its new label").containsExactly(c);
        Long newId = fx.idOf(b);
        int underTheNewHolder = fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION_ID.eq(newId)));
        assertThat(underTheNewHolder).as("nothing under the new holder of the old label").isZero();
    }

    @Test
    void aspectUpsert_byAnIdWhoseRowWasRenamedAndItsLabelTaken_landsInTheRenamedCollection() {
        String b = registered("rn-gap-aspect-b");
        String c = label("retired-rn-gap-aspect-c-" + (n++));
        String host = registered("rn-gap-aspect-host");
        long x = fx.idOf(b);
        String doc = "rn-gap-aspect-doc-" + b;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, host);
            return null;
        });
        Resp r = withTheRowRenamedAndTheLabelTaken(fx, b, x, c, () -> fx.post("/v1/aspects/upsert", "{\"collection_id\":" + x
            + ",\"source_path\":\"rn-gap.md\",\"doc_id\":" + json(doc) + ",\"confidence\":0.9,"
            + "\"extracted_at\":\"2026-01-01T00:00:00Z\",\"model_version\":\"v1\",\"extractor_name\":\"ex\"}"));
        assertThat(r.status()).as("an id-only write follows the rename: %s", r.body()).isEqualTo(200);
        java.util.List<String> labels = fx.asSuperuser(ctx -> ctx.select(dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS.COLLECTION)
            .from(dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS)
            .where(dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS.COLLECTION_ID.eq(x))
            .fetch(dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS.COLLECTION));
        assertThat(labels).as("the aspect row is the renamed collection's, under its new label").containsExactly(c);
        Long newId = fx.idOf(b);
        int underTheNewHolder = fx.asSuperuser(ctx -> ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS,
            dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS.COLLECTION_ID.eq(newId)));
        assertThat(underTheNewHolder).as("nothing under the new holder of the old label").isZero();
    }

    // ---- W1 (review F1, C1, C2, F5): the writes that used to go on by label before any registry read -----------------------------

    /** What the hook does in the gap, as a retirement: supersedes {@code a} by {@code b}, on another thread. */
    private Runnable retireInTheGap(CollectionRouteFixture f, String a, String b, AtomicInteger status) {
        return () -> elsewhere(() -> status.set(f.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(a) + ",\"superseded_by\":" + json(b) + "}").status()));
    }

    private String chunkMetadata(CollectionRouteFixture f, String collection, String hex) {
        byte[] chash = java.util.HexFormat.of().parseHex(hex);
        return f.asSuperuser(ctx -> ctx.select(CHUNKS.METADATA).from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(collection).and(CHUNKS.CHASH.eq(chash))).fetchOne(CHUNKS.METADATA).data());
    }

    /**
     * Review F1(a) and C1: every chash the request names is already stored with the same text (a re-index), so the batch
     * transaction is skipped and the only write is the metadata-only UPDATE of {@code resolveNeedEmbedIdx}'s own transaction,
     * by label. X is freed and its label taken by Z, which holds the same chash and text, just before that transaction's registry
     * read (the hook fires at the head of the transaction, after its sweep gate). The write answers 404 and the request's metadata
     * merges into nothing of Z's. With the registry read missing from that transaction the UPDATE merges into Z and the answer is 200.
     */
    @Test
    void upsertChunks_everyChashAlreadyStored_byAnIdFreedBeforeTheMetadataTransaction_mergesNothingIntoTheNewHolder() {
        String a = registered("w1-present");
        long x = fx.idOf(a);
        String hex = Chash.ofText("w1-present-" + a).toHex();
        seedChunk(a, hex);   // X holds it ("retired text"); the hook files the same chash and text in the row that takes the label
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, hex, () -> fx.post("/v1/vectors/upsert-chunks",
            "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"],\"documents\":[\"retired text\"],\"metadatas\":[{\"w1_key\":\"merged\"}]}"));
        assertUnknownId(r, x);
        assertThat(chunkMetadata(fx, a, hex)).as("the new holder's chunk took none of the request's metadata").doesNotContain("w1_key");
    }

    /**
     * Review C2: an empty manifest is not a no-op. {@code manifest/write} with no rows deletes the document's stored manifest rows
     * and clears their spans, by label. The id is retired just before the write transaction's registry read: the write answers 409
     * and the stored manifest row is still there. (Before, it answered 200 and cleared it.)
     */
    @Test
    void aManifestWriteWithNoRows_byAnIdRetiredBeforeTheWriteTransaction_is409_andClearsNothing() {
        String a = registered("w1-empty-a");
        String b = registered("w1-empty-b");
        long x = fx.idOf(a);
        String hex = Chash.ofText("w1-empty-" + a).toHex();
        String doc = "w1-empty-doc-" + a;
        seedChunk(a, hex);
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, doc, a);
            CollectionIdSeeds.manifestRows(ctx, fx.tenant, doc, a, 0, hex);
            return null;
        });
        AtomicInteger status = new AtomicInteger();
        hooks(null, retireInTheGap(fx, a, b, status), null);
        Resp r;
        try {
            r = fx.post("/v1/catalog/manifest/write", "{\"collection_id\":" + x + ",\"doc_id\":" + json(doc) + ",\"rows\":[]}");
        } finally {
            noHooks();
        }
        assertThat(status.get()).as("setup: the hook retired the row").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        int rows = fx.asSuperuser(ctx -> ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS,
            dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)));
        assertThat(rows).as("the document's stored manifest row was not deleted").isEqualTo(1);
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

    /**
     * A document of a HOST collection (a collection the test never frees or renames) whose one row references {@code hex}. A
     * {@code write_many} resolves only the chunks that the rows of a REGISTERED document reference, so a request with chunks and no such
     * document runs no refresh transaction at all and the hook never fires.
     */
    private String docReferencing(CollectionRouteFixture f, String host, String stem, String hex) {
        String doc = "w1-ref-doc-" + stem;
        f.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, f.tenant, doc, host);
            return null;
        });
        return "{\"doc_id\":" + json(doc) + ",\"rows\":[{\"position\":0,\"chash\":\"" + hex + "\",\"chunk_index\":0}]}";
    }

    private void seedChunkIn(CollectionRouteFixture f, String collection, String hex) {
        f.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, f.tenant, collection, List.of(hex), List.of("retired text"),
                List.of(new float[384]), List.of(Map.<String, Object>of()));
            return null;
        });
    }

    /**
     * Review F1(b): {@code write_many} with a chunk already stored with the same text runs the combined writer's metadata refresh
     * transaction, by label, before any per-document transaction. X is freed and Z takes the label with the same chash and text
     * just before that transaction's registry read: the write answers 404 and Z's chunk takes none of the metadata. (Before, the refresh
     * merged it into Z, committed, and the per-document transaction answered 404 after it.)
     */
    @Test
    void writeMany_withAChashAlreadyStored_byAnIdFreedBeforeTheRefreshTransaction_mergesNothingIntoTheNewHolder() {
        String a = label("retired-wm-refresh-" + (n++));
        fxLocal.register(a);
        long x = fxLocal.idOf(a);
        String hex = Chash.ofText("w1-wm-refresh-" + a).toHex();
        String host = label("retired-wm-refresh-host-" + (n++));
        fxLocal.register(host);
        String docJson = docReferencing(fxLocal, host, a, hex);
        seedChunkIn(fxLocal, a, hex);
        AtomicInteger deleteStatus = new AtomicInteger();
        fxLocal.service.collectionSelectorFilter().testHooks(null, () -> elsewhere(() -> {
            deleteStatus.set(fxLocal.post(DELETE, "{\"name\":" + json(a) + "}").status());
            fxLocal.register(a);
            seedChunkIn(fxLocal, a, hex);
        }), null, null);
        Resp r;
        try {
            r = fxLocal.post("/v1/catalog/manifest/write_many", "{\"collection_id\":" + x + ",\"chunks\":[{\"chash\":\"" + hex
                + "\",\"text\":\"retired text\",\"metadata\":{\"w1_key\":\"merged\"}}],\"docs\":[" + docJson + "]}");
        } finally {
            fxLocal.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        assertUnknownId(r, x);
        assertThat(chunkMetadata(fxLocal, a, hex)).as("the new holder's chunk took none of the request's metadata").doesNotContain("w1_key");
    }

    /**
     * Review F5: the window after the pre-check. The pre-check ({@code refuseRowsUnderSupersededCollection}) has passed; the id is
     * retired just before the first per-document transaction's registry read (a manifest-only request has no refresh transaction, so
     * that is the first write transaction the hook sees). The per-document catch used to record the typed refusal as the document's
     * failure and answer 200; it answers 409 now, and nothing is written.
     */
    @Test
    void writeMany_byAnIdRetiredAfterThePreCheck_answers409NotA200WithTheDocumentFailed() {
        String a = label("retired-wm-doc-a-" + (n++));
        String b = label("retired-wm-doc-b-" + (n++));
        fxLocal.register(a, b);
        long x = fxLocal.idOf(a);
        String hex = Chash.ofText("w1-wm-doc-" + a).toHex();
        String doc = "w1-wm-doc-" + a;
        seedChunkIn(fxLocal, a, hex);
        fxLocal.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fxLocal.tenant, doc, a);
            return null;
        });
        AtomicInteger status = new AtomicInteger();
        fxLocal.service.collectionSelectorFilter().testHooks(null, retireInTheGap(fxLocal, a, b, status), null, null);
        Resp r;
        try {
            r = fxLocal.post("/v1/catalog/manifest/write_many", "{\"collection_id\":" + x + ",\"docs\":[{\"doc_id\":" + json(doc)
                + ",\"rows\":[{\"position\":0,\"chash\":\"" + hex + "\",\"chunk_index\":0}]}]}");
        } finally {
            fxLocal.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
        assertThat(status.get()).as("setup: the hook retired the row").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        int rows = fxLocal.asSuperuser(ctx -> ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS,
            dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc)));
        assertThat(rows).as("no manifest row was written").isZero();
    }

    /**
     * Sensitivity of the gap tests (review: removing a read left them green, because a later read in the same transaction still saw the
     * freed id). The write transaction reads the registry row ONCE (memoised), so the count of reads a request makes, which the
     * registry reports through its test hook, is what goes to zero when the read is taken out; the gap tests above go red then too, but
     * this names the cause. Every route here answers 200 by a live id, and each makes at least one read, no more than one per write
     * transaction (the upsert has two: the metadata transaction and the batch insert).
     */
    @Test
    void aWriteByAnIdReadsTheRegistryRowInItsWriteTransactions_oncePerTransaction() {
        String a = registered("w1-reads");
        long x = fx.idOf(a);
        String hex = Chash.ofText("w1-reads-" + a).toHex();
        seedChunk(a, hex);
        java.util.concurrent.ConcurrentLinkedQueue<String> reads = new java.util.concurrent.ConcurrentLinkedQueue<>();
        dev.nexus.service.db.CollectionRegistry.setWriteReadHookForTests(reads::add);
        try {
            Resp topic = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"w1-reads\",\"doc_count\":1}");
            assertThat(topic.status()).as(topic.body()).isEqualTo(200);
            assertThat(reads.size()).as("topics/insert: one write transaction, one read").isEqualTo(1);
            reads.clear();

            Resp meta = fx.post("/v1/vectors/update-metadata", "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\"],\"metadatas\":[{\"k\":\"v\"}]}");
            assertThat(meta.status()).as(meta.body()).isEqualTo(200);
            assertThat(reads.size()).as("update-metadata: one write transaction, one read").isEqualTo(1);
            reads.clear();

            Resp del = fx.post("/v1/vectors/store-delete", "{\"collection_id\":" + x + ",\"ids\":[\"" + Chash.ofText("w1-none-" + a).toHex() + "\"]}");
            assertThat(del.status()).as(del.body()).isEqualTo(200);
            assertThat(reads.size()).as("store-delete: one write transaction, one read").isEqualTo(1);
            reads.clear();

            String fresh = Chash.ofText("w1-reads-fresh-" + a).toHex();
            Resp up = fx.post("/v1/vectors/upsert-chunks", "{\"collection_id\":" + x + ",\"ids\":[\"" + hex + "\",\"" + fresh
                + "\"],\"documents\":[\"retired text\",\"new text\"]}");
            assertThat(up.status()).as(up.body()).isEqualTo(200);
            assertThat(reads.size()).as("upsert-chunks: the metadata transaction and the batch insert read once each").isEqualTo(2);
        } finally {
            dev.nexus.service.db.CollectionRegistry.setWriteReadHookForTests(null);
        }
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
                // the specific wait: a backend whose statement is the DELETE of a catalog_collections row and that waits on a lock
                int ungranted = fx.asSuperuser(ctx -> ctx.selectCount()
                    .from(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("pg_catalog", "pg_stat_activity")))
                    .where(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("wait_event_type"), String.class).eq("Lock"))
                    .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("query"), String.class).likeIgnoreCase("delete%catalog_collections%"))
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
        assertThat(waited.get()).as("the DELETE of the registry row was waiting on a lock while the write's transaction was open (pg_stat_activity)").isTrue();
        assertThat(write.status()).as(write.body()).isEqualTo(200);
        Resp deleted = delete[0].get(30, TimeUnit.SECONDS);
        assertThat(deleted.status()).as("the delete went through once the write had committed: %s", deleted.body()).isEqualTo(200);
        assertThat(fx.exists(a)).isFalse();
    }
}
