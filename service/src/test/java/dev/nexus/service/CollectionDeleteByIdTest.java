// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.Map;

import static dev.nexus.service.CollectionRouteCensus.UNKNOWN_ID;
import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a write half (nexus-p2m1x.23): {@code POST /v1/catalog/collections/delete} by {@code collection_id} or a label,
 * the delete contract agreed with bob for P3.12 (bead nexus-p2m1x.23 comment 2026-11-22 08:20 and nexus-p2m1x.32's notes;
 * tamsin agreed to all seven points). One test per point, named after it, quoting the contract line. Point 5 (a label held by
 * several live rows) is {@link CollectionDeleteLabelClashTest}, which needs its own database.
 *
 * <p>The RDR line these rest on is P3.5a's (line 2662): "every collection-taking route accepts {@code collection_id} and a
 * label; responses carry both", applied to the route that purges a collection; the delete-specific refusals are the contract.
 * The error bodies' wire shape is not in the contract, so each test fixes the part it needs in a constant at the top, once.
 * Existing name-path behaviour is {@code CatalogHandlerDeleteTest} and {@code QuarantineCollectionRouteTest}; nothing here
 * changes what those assert except point 4, which changes a name-path outcome and says so.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionDeleteByIdTest {

    private static final String DELETE = "/v1/catalog/collections/delete";

    /** Point 3: the typed 404 for an id no row has (the same body every by-id route answers, CollectionRoutesByIdTest). */
    private static final String UNKNOWN_REASON = "unknown_collection_id";

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
        String l = label("del-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    // ---- point 1 -------------------------------------------------------------------------------------------------------

    /**
     * Contract point 1: "POST /v1/catalog/collections/delete takes collection_id (JSON integer) in place of name|collection;
     * both, or a non-integer id, -> 400."
     */
    @Test
    void point1_deleteTakesCollectionIdInPlaceOfNameOrCollection_bothOrANonIntegerIs400() {
        String byId = registered("p1-byid");
        long id = fx.idOf(byId);
        String keep = registered("p1-keep");
        long keepId = fx.idOf(keep);

        // both a name and an id, and both a collection and an id
        for (String both : List.of("{\"name\":" + json(keep) + ",\"collection_id\":" + keepId + "}",
                "{\"collection\":" + json(keep) + ",\"collection_id\":" + keepId + "}")) {
            Resp r = fx.post(DELETE, both);
            assertThat(r.status()).as(both + " -> " + r.body()).isEqualTo(400);
            assertThat(fx.exists(keep)).as("a refused request deletes nothing").isTrue();
        }
        // an id that is not a JSON integer
        for (String bad : List.of("\"" + keepId + "\"", "1.5", "true", "[" + keepId + "]", "{\"id\":1}")) {
            Resp r = fx.post(DELETE, "{\"collection_id\":" + bad + "}");
            assertThat(r.status()).as("collection_id=" + bad + " -> " + r.body()).isEqualTo(400);
            assertThat(fx.exists(keep)).as("a refused request deletes nothing").isTrue();
        }
        // neither (today's 400, unchanged)
        assertThat(fx.post(DELETE, "{}").status()).isEqualTo(400);

        // an integer id deletes that collection and no other
        Resp ok = fx.post(DELETE, "{\"collection_id\":" + id + "}");
        assertThat(ok.status()).as(ok.body()).isEqualTo(200);
        assertThat(fx.exists(byId)).isFalse();
        assertThat(fx.exists(keep)).as("the other collection is untouched").isTrue();
        assertThat(((Number) fx.idOf(keep)).longValue()).isEqualTo(keepId);
    }

    // ---- point 2 -------------------------------------------------------------------------------------------------------

    /**
     * Contract point 2: "200 carries collection_id and name (label); the client's pipeline purge keys on that label." On the
     * id path and on the label path (RDR line 2662: "responses carry both"), as JSON number and string, beside the existing
     * {@code deleted} counts.
     */
    @Test
    void point2_the200CarriesCollectionIdAndName_onBothPaths() {
        String viaId = registered("p2-id");
        long idA = fx.idOf(viaId);
        Resp byId = fx.post(DELETE, "{\"collection_id\":" + idA + "}");
        assertThat(byId.status()).as(byId.body()).isEqualTo(200);
        assertThat(byId.json().path("collection_id").isNumber()).as("a JSON number: %s", byId.body()).isTrue();
        assertThat(byId.json().path("collection_id").asLong()).isEqualTo(idA);
        assertThat(byId.json().path("name").asText()).as("the label the client's pipeline purge keys on").isEqualTo(viaId);
        assertThat(byId.json().has("deleted")).as("the existing counts stay (additive)").isTrue();

        String viaLabel = registered("p2-label");
        long idB = fx.idOf(viaLabel);
        Resp byLabel = fx.post(DELETE, "{\"name\":" + json(viaLabel) + "}");
        assertThat(byLabel.status()).as(byLabel.body()).isEqualTo(200);
        assertThat(byLabel.json().path("collection_id").asLong(-1)).isEqualTo(idB);
        assertThat(byLabel.json().path("name").asText()).isEqualTo(viaLabel);
    }

    // ---- point 3 -------------------------------------------------------------------------------------------------------

    /**
     * Contract point 3: "unknown id -> typed 404 naming the id." Never a silent no-op: the same id twice is a 404 the second
     * time, and a refused delete leaves every other collection alone.
     */
    @Test
    void point3_anUnknownIdIsATyped404NamingTheId() {
        String bystander = registered("p3-bystander");
        Resp r = fx.post(DELETE, "{\"collection_id\":" + UNKNOWN_ID + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).as("typed: %s", r.body()).isEqualTo(UNKNOWN_REASON);
        assertThat(r.json().path("collection_id").asLong(-1)).as("names the id: %s", r.body()).isEqualTo(UNKNOWN_ID);
        assertThat(r.json().path("error").asText()).contains(String.valueOf(UNKNOWN_ID));
        assertThat(fx.exists(bystander)).isTrue();

        String gone = registered("p3-gone");
        long goneId = fx.idOf(gone);
        assertThat(fx.post(DELETE, "{\"collection_id\":" + goneId + "}").status()).isEqualTo(200);
        Resp again = fx.post(DELETE, "{\"collection_id\":" + goneId + "}");
        assertThat(again.status()).as("the id of a deleted collection: %s", again.body()).isEqualTo(404);
        assertThat(again.reason()).isEqualTo(UNKNOWN_REASON);
    }

    // ---- point 4 -------------------------------------------------------------------------------------------------------

    /**
     * Contract point 4 as linda's rulings of 2026-11-23 changed it. A superseded LABEL is deleted with the 200 it always got
     * (option (b); the refusal naming the successor moved to the paired client release, P3.12): the tombstone goes and the live
     * successor is untouched. A superseded row's ID is refused (D3, which replaces this test's earlier "a retired id is deleted"):
     * 409 {@code collection_superseded} naming the successor's label ({@code superseded_by}, the key the client reads) and id
     * ({@code successor_collection_id}), and nothing is deleted. (From gate 3 the label's purge is scoped by the tombstone's own
     * id: {@link CollectionDeleteScopeByIdTest}.) Both ways a tombstone arises: a supersede (the tombstone keeps its data) and a
     * name-path rename (the tombstone is empty).
     */
    @Test
    void point4_aSupersededLabelIsDeleted_aSupersededIdIsRefused409NamingTheSuccessor() {
        // a supersede, deleted by its label
        String oldOne = registered("p4-old");
        String successor = registered("p4-succ");
        long successorId = fx.idOf(successor);
        Resp sup = fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(oldOne) + ",\"superseded_by\":" + json(successor) + "}");
        assertThat(sup.status()).as("setup: " + sup.body()).isEqualTo(200);
        Resp byLabel = fx.post(DELETE, "{\"name\":" + json(oldOne) + "}");
        assertThat(byLabel.status()).as("the 200 stays: %s", byLabel.body()).isEqualTo(200);
        assertThat(fx.exists(oldOne)).as("the tombstone goes").isFalse();
        assertThat(fx.supersededBy(successor)).as("and the live successor is untouched").isEqualTo("");
        assertThat(fx.idOf(successor)).isEqualTo(successorId);

        // a supersede, asked for by the tombstone's id: refused, nothing deleted
        String oldTwo = registered("p4-old-id");
        long oldTwoId = fx.idOf(oldTwo);
        assertThat(fx.post("/v1/catalog/collections/supersede",
            "{\"name\":" + json(oldTwo) + ",\"superseded_by\":" + json(successor) + "}").status()).isEqualTo(200);
        Resp byId = fx.post(DELETE, "{\"collection_id\":" + oldTwoId + "}");
        assertThat(byId.status()).as(byId.body()).isEqualTo(409);
        assertThat(byId.reason()).isEqualTo("collection_superseded");
        assertThat(byId.json().path("collection_id").asLong(-1)).isEqualTo(oldTwoId);
        assertThat(byId.json().path("superseded_by").asText()).isEqualTo(successor);
        assertThat(byId.json().path("successor_collection_id").asLong(-1)).isEqualTo(successorId);
        assertThat(fx.exists(oldTwo)).as("a refused delete removes nothing").isTrue();
        assertThat(fx.exists(successor)).isTrue();

        // a name-path rename: the old label is an empty tombstone of the new one; its label deletes it, its id is refused
        String from = registered("p4-ren-from");
        long fromId = fx.idOf(from);
        String to = label("del-p4-ren-to-" + (n++));
        Resp ren = fx.post("/v1/catalog/collections/rename", "{\"old_name\":" + json(from) + ",\"new_name\":" + json(to) + "}");
        assertThat(ren.status()).as("setup: " + ren.body()).isEqualTo(200);
        Resp tombstoneById = fx.post(DELETE, "{\"collection_id\":" + fromId + "}");
        assertThat(tombstoneById.status()).as(tombstoneById.body()).isEqualTo(409);
        assertThat(tombstoneById.json().path("superseded_by").asText()).isEqualTo(to);
        assertThat(tombstoneById.json().path("successor_collection_id").asLong(-1)).isEqualTo(fx.idOf(to));
        assertThat(fx.exists(from)).isTrue();
        Resp byTombstone = fx.post(DELETE, "{\"name\":" + json(from) + "}");
        assertThat(byTombstone.status()).as(byTombstone.body()).isEqualTo(200);
        assertThat(fx.exists(from)).as("the rename's empty tombstone goes").isFalse();
        assertThat(fx.exists(to)).as("the renamed collection stays").isTrue();
    }

    /**
     * Contract point 4, last clause: "the --all empty-tombstone sweep unchanged." Deleting the live successor still removes
     * the empty retired rows that name it (nexus-s80u9; {@code CatalogDeleteRetiredPredecessorTest}), by id as by label: the
     * delete of the tombstone itself is a plain delete (point 4 above).
     */
    @Test
    void point4_theEmptyTombstoneSweepIsUnchanged_deletingTheSuccessorByIdStillRemovesIt() {
        String from = registered("p4s-from");
        String to = label("del-p4s-to-" + (n++));
        assertThat(fx.post("/v1/catalog/collections/rename",
            "{\"old_name\":" + json(from) + ",\"new_name\":" + json(to) + "}").status()).isEqualTo(200);
        assertThat(fx.supersededBy(from)).as("setup: an empty tombstone").isEqualTo(to);

        Resp r = fx.post(DELETE, "{\"collection_id\":" + fx.idOf(to) + "}");

        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(fx.exists(to)).isFalse();
        assertThat(fx.exists(from)).as("the empty tombstone that named it goes with it").isFalse();
    }

    // ---- point 6 -------------------------------------------------------------------------------------------------------

    /**
     * Contract point 6: "/version flag catalog_collection_delete_by_id_supported advertises (1); the client refuses --id
     * without it." A boolean, true on an engine that has point 1, on {@code GET /version} (no auth, as the other flags).
     */
    @Test
    void point6_versionAdvertisesCatalogCollectionDeleteByIdSupported() throws Exception {
        var r = fx.http.send(TestHttp.request("http://127.0.0.1:" + fx.service.getPort() + "/version").GET().build(),
            java.net.http.HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).isEqualTo(200);
        JsonNode flag = CollectionRouteFixture.MAPPER.readTree(r.body()).get("catalog_collection_delete_by_id_supported");
        assertThat(flag).as("the flag is present").isNotNull();
        assertThat(flag.isBoolean()).isTrue();
        assertThat(flag.asBoolean()).isTrue();
    }

    // ---- point 7 -------------------------------------------------------------------------------------------------------

    /**
     * Contract point 7: "keep_quarantine keeps its meaning on the id path." As on the name path
     * ({@code QuarantineCollectionRouteTest#collectionDeleteWithKeepQuarantine_leavesTheRows_andWithoutItTakesThem}):
     * {@code true} leaves the origin's rows in its quarantine sibling, omitted or {@code false} takes them, a present value
     * that is not a JSON boolean is a 400 that deletes nothing.
     */
    @Test
    void point7_keepQuarantineKeepsItsMeaningOnTheIdPath() {
        String origin = label("del-p7-origin-" + (n++));
        String sibling = "quarantine-" + origin;
        String hex = Chash.ofText(sibling + "/p7").toHex();
        seedOriginWithQuarantineRow(origin, sibling, hex);
        long originId = fx.idOf(origin);

        // a present non-boolean is refused, nothing deleted
        Resp bad = fx.post(DELETE, "{\"collection_id\":" + originId + ",\"keep_quarantine\":\"true\"}");
        assertThat(bad.status()).as(bad.body()).isEqualTo(400);
        assertThat(bad.body()).contains("keep_quarantine");
        assertThat(fx.exists(origin)).as("a refused request deletes nothing").isTrue();
        assertThat(chunksIn(sibling)).isEqualTo(1);

        // true leaves the sibling's rows
        Resp kept = fx.post(DELETE, "{\"collection_id\":" + originId + ",\"keep_quarantine\":true}");
        assertThat(kept.status()).as(kept.body()).isEqualTo(200);
        assertThat(kept.json().path("deleted").path("quarantine_chunks").asInt(-1)).isZero();
        assertThat(chunksIn(sibling)).as("keep_quarantine:true leaves the row").isEqualTo(1);

        // the same name registered again is a new row with a new id; omitted takes the sibling's rows
        fx.register(origin);
        long again = fx.idOf(origin);
        assertThat(again).as("setup: the re-registered name has its own id").isNotEqualTo(originId);
        Resp omitted = fx.post(DELETE, "{\"collection_id\":" + again + "}");
        assertThat(omitted.status()).as(omitted.body()).isEqualTo(200);
        assertThat(omitted.json().path("deleted").path("quarantine_chunks").asInt(-1)).isEqualTo(1);
        assertThat(chunksIn(sibling)).isZero();
    }

    private void seedOriginWithQuarantineRow(String origin, String sibling, String hex) {
        fx.register(origin, sibling);
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertChunks(ctx, CollectionRouteFixture.TENANT, sibling, List.of(hex), List.of("p7 text"),
                List.of(new float[384]),
                List.of(Map.<String, Object>of("origin_collection", origin, "quarantined_at", "2026-09-01T00:00:00Z")));
            return null;
        });
    }

    private int chunksIn(String collection) {
        return fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.COLLECTION.eq(collection)));
    }
}
