// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23), slice A1: the four taxonomy writes the consumption census found answering 200 by id without
 * reading the id in their own transaction: {@code purge_collection}, {@code topics/persist_discovered}, {@code centroids/delete}
 * and {@code centroids/purge}. Each reads the id first, in its own transaction ({@code FOR KEY SHARE}), so an id freed between the
 * route boundary and the write answers 404 and the new holder of its label keeps every row. The gap is made with the
 * {@code beforeTransactionRead} hook of {@link CollectionRetiredIdRoutesTest}: it runs just before the write's own by-id read,
 * after every outside-a-transaction lookup has passed, so the order is fixed and no timing is involved.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionTaxonomyByIdGapTest {

    private static final String DELETE = "/v1/catalog/collections/delete";
    private static final String EMBEDDING_384 = "[" + String.join(",", java.util.Collections.nCopies(384, "0.1")) + "]";

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
        String l = label("taxgap-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private void hooks(Runnable beforeTransactionRead) {
        fx.service.collectionSelectorFilter().testHooks(null, beforeTransactionRead, null, null);
    }

    private int topicsOf(String collection) {
        Integer c = fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(collection)));
        return c;
    }

    private int centroidsOf(String collection) {
        Integer c = fx.asSuperuser(ctx -> ctx.fetchCount(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.COLLECTION.eq(collection)));
        return c;
    }

    /** One topic and one centroid (topic id 1) under {@code collection}, written by label. */
    private void seedTaxonomy(String collection) {
        Resp t = fx.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(collection) + ",\"label\":\"held\",\"doc_count\":1}");
        assertThat(t.status()).as("seed topic: %s", t.body()).isEqualTo(200);
        seedCentroid(collection);
    }

    private void seedCentroid(String collection) {
        Resp c = fx.post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection\":" + json(collection)
            + ",\"topic_id\":1,\"label\":\"held\",\"doc_count\":1,\"embedding\":" + EMBEDDING_384 + "}]}");
        assertThat(c.status()).as("seed centroid: %s", c.body()).isEqualTo(200);
    }

    /**
     * Frees {@code a}'s id and gives its label to a new row in the gap before the write's own read, filing {@code seed} under the
     * new holder, and returns what the request answered.
     */
    private Resp withTheLabelTakenBeforeTheWritesRead(String a, Runnable seed, Supplier<Resp> request) {
        AtomicInteger deleteStatus = new AtomicInteger();
        // every setup action runs on ANOTHER thread: the hook runs on the request's own thread, where the carried id is set,
        // so a seed that read a registry id there would read the freed id and fail, and the test would pass by accident
        hooks(() -> CompletableFuture.runAsync(() -> {
            deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(a) + "}").status());
            fx.register(a);
            seed.run();
        }).join());
        try {
            Resp r = request.get();
            assertThat(deleteStatus.get()).as("setup: the hook ran, freeing the id").isEqualTo(200);
            return r;
        } finally {
            hooks(null);
        }
    }

    private void assertUnknownId(Resp r, long x) {
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
    }

    // ---- purge_collection -------------------------------------------------------------------------------------------------------

    @Test
    void purgeCollection_byALiveIdPurgesThatRow() {
        String a = registered("purge-live");
        seedTaxonomy(a);
        long x = fx.idOf(a);
        Resp r = fx.post("/v1/taxonomy/purge_collection", "{\"collection_id\":" + x + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("topics").asInt(-1)).isEqualTo(1);
        assertThat(topicsOf(a)).isZero();
    }

    @Test
    void purgeCollection_byAnIdFreedAfterTheLookupsPurgesNothingOfTheNewHolder() {
        String a = registered("purge-gap");
        long x = fx.idOf(a);
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, () -> seedTaxonomy(a),
            () -> fx.post("/v1/taxonomy/purge_collection", "{\"collection_id\":" + x + "}"));
        assertUnknownId(r, x);
        assertThat(topicsOf(a)).as("the new holder's topic survives").isEqualTo(1);
    }

    @Test
    void purgeCollection_byALabelWithNoRegistryRowStillAnswers200() {
        // topics_collection_fk keeps a topic from outliving its registry row, so "leftovers with no row" cannot be staged;
        // what the helper's catch keeps is the label path's answer for a label the registry does not hold (the client's
        // purge of a collection already gone, corpus.py): a 200 with nothing to purge, never the 422 a by-id read would give.
        String a = label("purge-orphan-" + (n++));
        Resp r = fx.post("/v1/taxonomy/purge_collection", "{\"collection\":" + json(a) + "}");
        assertThat(r.status()).as("by label, a purge never needed a registry row: %s", r.body()).isEqualTo(200);
        assertThat(topicsOf(a)).isZero();
    }

    // ---- topics/persist_discovered ----------------------------------------------------------------------------------------------

    private static String specsBody(long x) {
        return "{\"collection_id\":" + x + ",\"specs\":[{\"label\":\"found\",\"doc_ids\":[]}]}";
    }

    @Test
    void persistDiscovered_byALiveIdFilesTheTopicUnderThatRow() {
        String a = registered("disc-live");
        long x = fx.idOf(a);
        Resp r = fx.post("/v1/taxonomy/topics/persist_discovered", specsBody(x));
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("topic_ids").size()).isEqualTo(1);
        Integer filed = fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.COLLECTION.eq(a).and(TOPICS.COLLECTION_ID.eq(x))));
        assertThat(filed).as("filed under the label and the id of the row asked for").isEqualTo(1);
    }

    @Test
    void persistDiscovered_byAnIdFreedAfterTheLookupsFilesNothingInTheNewHolder() {
        String a = registered("disc-gap");
        long x = fx.idOf(a);
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, () -> { },
            () -> fx.post("/v1/taxonomy/topics/persist_discovered", specsBody(x)));
        assertUnknownId(r, x);
        assertThat(topicsOf(a)).as("the new holder, which had no topic, got none").isZero();
    }

    // ---- centroids/delete -------------------------------------------------------------------------------------------------------

    @Test
    void centroidDelete_byALiveIdDeletesThatRowsCentroid() {
        String a = registered("cdel-live");
        seedCentroid(a);
        long x = fx.idOf(a);
        Resp r = fx.post("/v1/taxonomy/centroids/delete", "{\"collection_id\":" + x + ",\"topic_ids\":[1]}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("deleted").asInt(-1)).isEqualTo(1);
        assertThat(centroidsOf(a)).isZero();
    }

    @Test
    void centroidDelete_byAnIdFreedAfterTheLookupsDeletesNothingOfTheNewHolder() {
        String a = registered("cdel-gap");
        long x = fx.idOf(a);
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, () -> seedCentroid(a),
            () -> fx.post("/v1/taxonomy/centroids/delete", "{\"collection_id\":" + x + ",\"topic_ids\":[1]}"));
        assertUnknownId(r, x);
        assertThat(centroidsOf(a)).as("the new holder's centroid survives").isEqualTo(1);
    }

    // ---- centroids/purge --------------------------------------------------------------------------------------------------------

    @Test
    void centroidPurge_byALiveIdPurgesThatRowsCentroids() {
        String a = registered("cpurge-live");
        seedCentroid(a);
        long x = fx.idOf(a);
        Resp r = fx.post("/v1/taxonomy/centroids/purge", "{\"collection_id\":" + x + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("deleted").asInt(-1)).isEqualTo(1);
        assertThat(centroidsOf(a)).isZero();
    }

    @Test
    void centroidPurge_byAnIdFreedAfterTheLookupsPurgesNothingOfTheNewHolder() {
        String a = registered("cpurge-gap");
        long x = fx.idOf(a);
        Resp r = withTheLabelTakenBeforeTheWritesRead(a, () -> seedCentroid(a),
            () -> fx.post("/v1/taxonomy/centroids/purge", "{\"collection_id\":" + x + "}"));
        assertUnknownId(r, x);
        assertThat(centroidsOf(a)).as("the new holder's centroid survives").isEqualTo(1);
    }
}
