// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.http.CollectionSelectorFilter;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

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
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private String registered(String stem) {
        String l = label("retired-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void seedChunk(String collection) {
        String hex = Chash.ofText(collection + "/chunk").toHex();
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
        CollectionSelectorFilter.afterResolveForTests = () -> {
            if (fired.getAndIncrement() == 0) {   // the delete below passes through the boundary too: only the first call acts
                deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status());
                fx.register(a);
            }
        };
        Resp r;
        try {
            r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"gap-topic\",\"doc_count\":1}");
        } finally {
            CollectionSelectorFilter.afterResolveForTests = null;
        }

        assertThat(deleteStatus.get()).as("setup: the hook freed the id").isEqualTo(200);
        long z = fx.idOf(a);
        assertThat(z).as("setup: the label is now another row's").isNotEqualTo(x);
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a)))).as("nothing landed in the new holder of the label")
            .isZero();
    }

    /** The same gap, with the id retired (not freed) in it: the handler's by-id read answers the retired row's 409. */
    @Test
    void aWriteByAnIdRetiredBetweenTheBoundaryAndTheHandlerAnswers409NamingTheSuccessor() {
        String a = registered("gap-retire-a");
        String b = registered("gap-retire-b");
        long x = fx.idOf(a);
        AtomicInteger fired = new AtomicInteger();
        AtomicInteger supersedeStatus = new AtomicInteger();
        CollectionSelectorFilter.afterResolveForTests = () -> {
            if (fired.getAndIncrement() == 0) {
                supersedeStatus.set(fx.post("/v1/catalog/collections/supersede",
                    "{\"name\":" + json(a) + ",\"superseded_by\":" + json(b) + "}").status());
            }
        };
        Resp r;
        try {
            r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + x + ",\"label\":\"gap-topic\",\"doc_count\":1}");
        } finally {
            CollectionSelectorFilter.afterResolveForTests = null;
        }
        assertThat(supersedeStatus.get()).as("setup").isEqualTo(200);
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).isEqualTo("collection_superseded");
        assertThat(r.json().path("superseded_by").asText()).isEqualTo(b);
        assertThat(r.json().path("successor_collection_id").asLong(-1)).isEqualTo(fx.idOf(b));
        assertThat((int) (Integer) fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a)))).isZero();
    }

    @Test
    void d3_anIdWithNoRowIsStillThe404() {
        Resp r = fx.get("/v1/catalog/collections/get?collection_id=" + CollectionRouteCensus.UNKNOWN_ID);
        assertThat(r.status()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
    }

    // ---- F6: a3ebfe6605, the boundary reads the process-wide cache ----------------------------------------------------------------

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
}
