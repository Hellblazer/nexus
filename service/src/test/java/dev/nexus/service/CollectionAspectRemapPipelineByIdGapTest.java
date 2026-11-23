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
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.PDF_PIPELINE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, slice A2): the aspect delete, the salient-sentences write by key, the remap leg clear (both
 * legs) and the pipeline collection delete read the id the request named in their own write transaction. The consumption census
 * found them answering 200 by id without reading it, so between the route boundary and the write the id could be freed and its
 * label taken by another row, and the write would act on that row.
 *
 * <p>Each gap test frees the id just before the write's by-id read (the {@code beforeTransactionRead} hook: delete X, register
 * its label again as a new row Z, file rows in Z), and the request by X must answer 404 {@code unknown_collection_id} and leave
 * Z's rows as they were. The setup runs on ANOTHER thread ({@link #elsewhere}): the hook runs on the request's own thread, where
 * the carried ids are set, and a setup that read the registry there would read the freed id and fail the test by accident. Each
 * has a control: the same request by the id of a live collection does its work.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionAspectRemapPipelineByIdGapTest {

    private static final String DELETE = "/v1/catalog/collections/delete";
    private static final String PATH = "p35a-gap.md";

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
        String l = label("a2-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    private static void elsewhere(Runnable setup) {
        CompletableFuture.runAsync(setup).join();
    }

    private void beforeRead(Runnable hook) {
        fx.service.collectionSelectorFilter().testHooks(null, hook, null, null);
    }

    /**
     * Runs {@code request} with the id of {@code freed} freed just before the write's by-id read: the label's row is deleted,
     * the label registered again as a new row, and {@code seedNewHolder} files rows in it.
     */
    private Resp withTheIdFreed(String freed, Runnable seedNewHolder, Supplier<Resp> request) {
        AtomicInteger deleteStatus = new AtomicInteger();
        beforeRead(() -> elsewhere(() -> {
            deleteStatus.set(fx.post(DELETE, "{\"name\":" + json(freed) + "}").status());
            fx.register(freed);
            seedNewHolder.run();
        }));
        try {
            Resp r = request.get();
            assertThat(deleteStatus.get()).as("setup: the hook ran and freed the id").isEqualTo(200);
            return r;
        } finally {
            beforeRead(null);
        }
    }

    private void assertUnknownId(Resp r, long x) {
        assertThat(r.status()).as(r.body()).isEqualTo(404);
        assertThat(r.reason()).isEqualTo("unknown_collection_id");
        assertThat(r.json().path("collection_id").asLong(-1)).isEqualTo(x);
    }

    // ---- aspects -------------------------------------------------------------------------------------------------------------

    /** An aspect row of {@code collection}, filed by label through the route (the owner document it needs is seeded first). */
    private void seedAspect(String collection, String sentences) {
        String docId = "own-" + collection;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, docId, collection);
            return null;
        });
        Resp r = fx.post("/v1/aspects/upsert", "{\"collection\":" + json(collection) + ",\"source_path\":" + json(PATH)
            + ",\"doc_id\":" + json(docId) + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"salient_sentences\":" + json(sentences) + "}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
        assertThat(r.json().path("written").asBoolean()).as("setup: " + r.body()).isTrue();
    }

    /** The salient_sentences of each aspect row filed under {@code collection}, as text. */
    private List<String> aspectRows(String collection) {
        List<String> rows = fx.asSuperuser(ctx -> ctx.select(DOCUMENT_ASPECTS.SALIENT_SENTENCES).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_ASPECTS.COLLECTION.eq(collection)))
            .fetch(r -> String.valueOf(r.value1())));
        return rows;
    }

    @Test
    void aspectsDelete_byALiveId_deletes() {
        String a = registered("adel-live");
        seedAspect(a, "[\"orig\"]");
        Resp r = fx.post("/v1/aspects/delete", "{\"collection_id\":" + fx.idOf(a) + ",\"source_path\":" + json(PATH) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("deleted").asInt(-1)).isEqualTo(1);
        assertThat(aspectRows(a)).isEmpty();
    }

    @Test
    void aspectsDelete_byAnIdFreedBeforeTheWritesRead_deletesNothingOfTheNewHolder() {
        String a = registered("adel-gap");
        long x = fx.idOf(a);
        Resp r = withTheIdFreed(a, () -> seedAspect(a, "[\"holder\"]"),
            () -> fx.post("/v1/aspects/delete", "{\"collection_id\":" + x + ",\"source_path\":" + json(PATH) + "}"));
        assertUnknownId(r, x);
        assertThat(aspectRows(a)).as("the new holder's aspect row is still there").hasSize(1);
    }

    @Test
    void aspectsSetSalientByKey_byALiveId_updates() {
        String a = registered("aset-live");
        seedAspect(a, "[\"orig\"]");
        Resp r = fx.post("/v1/aspects/salient_sentences/set_by_key",
            "{\"collection_id\":" + fx.idOf(a) + ",\"source_path\":" + json(PATH) + ",\"sentences\":[\"changed\"]}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("updated").asInt(-1)).isEqualTo(1);
        assertThat(aspectRows(a)).singleElement().asString().contains("changed");
    }

    @Test
    void aspectsSetSalientByKey_byAnIdFreedBeforeTheWritesRead_changesNothingOfTheNewHolder() {
        String a = registered("aset-gap");
        long x = fx.idOf(a);
        Resp r = withTheIdFreed(a, () -> seedAspect(a, "[\"holder\"]"), () -> fx.post("/v1/aspects/salient_sentences/set_by_key",
            "{\"collection_id\":" + x + ",\"source_path\":" + json(PATH) + ",\"sentences\":[\"changed\"]}"));
        assertUnknownId(r, x);
        assertThat(aspectRows(a)).singleElement().asString().contains("holder").doesNotContain("changed");
    }

    // ---- remap ---------------------------------------------------------------------------------------------------------------

    private void seedLeg(String source, String target) {
        Resp r = fx.post("/v1/remap/record_batch", "{\"source_collection\":" + json(source) + ",\"entries\":[{\"old_id\":\"old-1\","
            + "\"new_chash\":\"" + Chash.ofText("a2-remap-" + source + target).toHex() + "\",\"target_collection\":" + json(target)
            + ",\"provenance\":\"p35a\"}]}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private int legRows(String source, String target) {
        Integer rows = fx.asSuperuser(ctx -> ctx.fetchCount(CHASH_REMAP,
            CHASH_REMAP.TENANT_ID.eq(fx.tenant).and(CHASH_REMAP.SOURCE_COLLECTION.eq(source))
                .and(CHASH_REMAP.TARGET_COLLECTION.eq(target))));
        return rows;
    }

    @Test
    void remapClearLeg_byLiveIdsOnBothLegs_clears() {
        String s = registered("leg-live-s");
        String t = registered("leg-live-t");
        seedLeg(s, t);
        Resp r = fx.post("/v1/remap/clear_leg",
            "{\"source_collection_id\":" + fx.idOf(s) + ",\"target_collection_id\":" + fx.idOf(t) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("deleted").asInt(-1)).isEqualTo(1);
        assertThat(legRows(s, t)).isZero();
    }

    @Test
    void remapClearLeg_bySourceIdFreedBeforeTheWritesRead_clearsNothingOfTheNewHolder() {
        String s = registered("leg-src-s");
        String t = registered("leg-src-t");
        long x = fx.idOf(s);
        Resp r = withTheIdFreed(s, () -> seedLeg(s, t), () -> fx.post("/v1/remap/clear_leg",
            "{\"source_collection_id\":" + x + ",\"target_collection\":" + json(t) + "}"));
        assertUnknownId(r, x);
        assertThat(legRows(s, t)).as("the new holder's leg is still there").isEqualTo(1);
    }

    @Test
    void remapClearLeg_byTargetIdFreedBeforeTheWritesRead_clearsNothingOfTheNewHolder() {
        String s = registered("leg-tgt-s");
        String t = registered("leg-tgt-t");
        long x = fx.idOf(t);
        Resp r = withTheIdFreed(t, () -> seedLeg(s, t), () -> fx.post("/v1/remap/clear_leg",
            "{\"source_collection\":" + json(s) + ",\"target_collection_id\":" + x + "}"));
        assertUnknownId(r, x);
        assertThat(legRows(s, t)).as("the leg onto the new holder is still there").isEqualTo(1);
    }

    // ---- pipeline ------------------------------------------------------------------------------------------------------------

    private void seedPipeline(String collection) {
        Resp r = fx.post("/v1/pipeline/create", "{\"collection\":" + json(collection) + ",\"content_hash\":"
            + json("a2-" + collection) + ",\"pdf_path\":\"/a2.pdf\"}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private int pipelines(String collection) {
        Integer rows = fx.asSuperuser(ctx -> ctx.fetchCount(PDF_PIPELINE,
            PDF_PIPELINE.TENANT_ID.eq(fx.tenant).and(PDF_PIPELINE.COLLECTION.eq(collection))));
        return rows;
    }

    @Test
    void pipelineDeleteCollection_byALiveId_deletes() {
        String a = registered("pdel-live");
        seedPipeline(a);
        Resp r = fx.post("/v1/pipeline/delete_collection", "{\"collection_id\":" + fx.idOf(a) + "}");
        assertThat(r.status()).as(r.body()).isEqualTo(200);
        assertThat(r.json().path("deleted").asInt(-1)).isEqualTo(1);
        assertThat(pipelines(a)).isZero();
    }

    @Test
    void pipelineDeleteCollection_byAnIdFreedBeforeTheWritesRead_deletesNothingOfTheNewHolder() {
        String a = registered("pdel-gap");
        long x = fx.idOf(a);
        Resp r = withTheIdFreed(a, () -> seedPipeline(a),
            () -> fx.post("/v1/pipeline/delete_collection", "{\"collection_id\":" + x + "}"));
        assertUnknownId(r, x);
        assertThat(pipelines(a)).as("the new holder's pipeline row is still there").isEqualTo(1);
    }
}
