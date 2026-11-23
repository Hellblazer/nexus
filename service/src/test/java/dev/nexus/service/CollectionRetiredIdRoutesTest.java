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

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
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
