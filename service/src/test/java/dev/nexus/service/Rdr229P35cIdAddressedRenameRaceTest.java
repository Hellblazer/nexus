// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.PDF_PIPELINE;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_META;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.datamove.IdUniqueIndexPass;
import org.jooq.Field;
import org.jooq.impl.DSL;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.function.LongFunction;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), census invariant I7 (linda): a write transaction under an id-addressed request reaches that id
 * through {@code CollectionIds} and never resolves the label on its own. Each test makes the gap P3.5a's gap tests make, with a
 * one-row rename in it: the route boundary has resolved the id N to the label B, and just before the write's own by-id read B is
 * renamed to C (the registry row keeps N, nothing else is written: P3.7's rename) and a NEW row takes the label B with rows of its
 * own. The request by N must be refused (409 {@code collection_renamed}: N now carries another label) and the new holder
 * of B must keep every row it had, in each path P3.5c switched: the manifest write and append, the taxonomy topic insert, assign and
 * meta record, purge_collection with its meta, and the aspect and highlight writes.
 *
 * <p>The setup runs on ANOTHER thread ({@link #elsewhere}): the hook runs on the request's own thread, where the carried ids are set,
 * and a setup that read the registry there would read the requested id and pass by accident.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35cIdAddressedRenameRaceTest {

    private static final String PATH = "p35c-i7.md";
    private static final String DOC = "1.1.7";
    private static final String EMBEDDING_384 = "[" + String.join(",", java.util.Collections.nCopies(384, "0.1")) + "]";

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
        // P3.4's arbiter indexes exist before any latched upsert: the id arbiter needs them
        for (String t : List.of("taxonomy_meta", "document_aspects", "topics", "chash_remap")) {
            ensureArbiterIndex(t);
        }
        fx.gate3.set(true);
    }

    /** The arbiter index P3.4's pass builds for {@code table}, created from its spec if not there yet. */
    private void ensureArbiterIndex(String table) {
        var spec = IdUniqueIndexPass.SPECS.stream().filter(x -> x.table().equals(table) && x.unique()).findFirst().orElseThrow();
        List<Field<?>> cols = java.util.Arrays.stream(spec.columns().split(",\\s*")).<Field<?>>map(c -> DSL.field(DSL.name(c))).toList();
        fx.asSuperuser(ctx -> {
            var step = ctx.createUniqueIndexIfNotExists(spec.index()).on(DSL.table(DSL.name("nexus", table)), cols);
            if (spec.predicate() != null) {
                assertThat(table).as("the only partial arbiter this test builds is topics'").isEqualTo("topics");
                step.where(TOPICS.PARENT_ID.isNull()).execute();
            } else {
                step.execute();
            }
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private static void elsewhere(Runnable setup) {
        CompletableFuture.runAsync(setup).join();
    }

    private String registered(String stem) {
        String l = label("i7-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** P3.7's rename, simulated: the registry row of {@code from} takes the name {@code to}, keeps its id, and nothing else is written. */
    private void oneRowRename(String from, String to) {
        fx.asSuperuser(ctx -> {
            ctx.set(DSL.name("session_replication_role"), DSL.inline("replica")).execute();
            try {
                int renamed = ctx.update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(from)).execute();
                assertThat(renamed).as("setup: the registry row was renamed in place").isEqualTo(1);
            } finally {
                ctx.set(DSL.name("session_replication_role"), DSL.inline("origin")).execute();
            }
            return null;
        });
    }

    /**
     * Runs {@code request} (given the id N of {@code b}) with, just before the write's own by-id read: {@code b} renamed to
     * {@code c}, a new row registered under {@code b}, and {@code seedNewHolder} run for it. Asserts the setup ran and that N is
     * still the renamed row's id.
     */
    private Resp withTheLabelRenamedAwayAndTaken(String b, java.util.function.LongConsumer seedNewHolder, LongFunction<Resp> request) {
        long id = fx.idOf(b);
        String c = b + "-moved";
        // warm the process-wide cache for the label (a by-label lookup fills B -> N's row, and the rename does not evict it), so a
        // write that went back to the label would hit the cache and the new row alike: only a read of the id refuses it
        CollectionRegistry.lookup(new dev.nexus.service.db.TenantScope(fx.svcDs), fx.tenant, b);
        java.util.concurrent.atomic.AtomicBoolean ran = new java.util.concurrent.atomic.AtomicBoolean();
        java.util.concurrent.atomic.AtomicBoolean started = new java.util.concurrent.atomic.AtomicBoolean();
        fx.service.collectionSelectorFilter().testHooks(null, () -> {
            if (!started.compareAndSet(false, true)) {
                return;   // the seed requests below pass this hook too: once
            }
            elsewhere(() -> {
                oneRowRename(b, c);
                fx.register(b);
                // the new holder is seeded by ITS id: a by-label request would hit the warm cache, which still maps B to the renamed row
                seedNewHolder.accept(fx.idOf(b));
                ran.set(true);
            });
        }, null, null);
        try {
            Resp r = request.apply(id);
            assertThat(ran.get()).as("setup: the hook ran before the write's own read").isTrue();
            assertThat(fx.labelOf(id)).as("N is the renamed row's id").isEqualTo(c);
            assertThat(fx.idOf(b)).as("and the label B is a new row's").isNotEqualTo(id);
            return r;
        } finally {
            fx.service.collectionSelectorFilter().testHooks(null, null, null, null);
        }
    }

    private void assertRefused(Resp r) {
        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.reason()).as("the typed refusal: the id now carries another label: %s", r.body()).isEqualTo("collection_renamed");
    }

    // ---- counts of what the new holder of B keeps ------------------------------------------------------------------------------

    private int topics(String c) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(TOPICS, TOPICS.TENANT_ID.eq(fx.tenant).and(TOPICS.COLLECTION.eq(c))));
        return v;
    }

    private int metas(String c) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(TAXONOMY_META, TAXONOMY_META.TENANT_ID.eq(fx.tenant).and(TAXONOMY_META.COLLECTION.eq(c))));
        return v;
    }

    private int aspects(String c) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_ASPECTS.COLLECTION.eq(c))));
        return v;
    }

    private int highlights(String c) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(c))));
        return v;
    }

    private int manifestRows(String c) {
        Integer v = fx.asSuperuser(ctx -> ctx.fetchCount(CATALOG_DOCUMENT_CHUNKS,
            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(fx.tenant).and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(c))));
        return v;
    }

    private void seedTopic(long id) {
        Resp r = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + id + ",\"label\":\"held\",\"doc_count\":1}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private void seedMeta(long id) {
        Resp r = fx.post("/v1/taxonomy/meta/record", "{\"collection_id\":" + id + ",\"doc_count\":3}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private void seedAspect(long id) {
        String c = fx.labelOf(id);
        String docId = "own-" + c;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, docId, c);
            return null;
        });
        Resp r = fx.post("/v1/aspects/upsert", "{\"collection_id\":" + id + ",\"source_path\":" + json(PATH)
            + ",\"doc_id\":" + json(docId) + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"salient_sentences\":\"[\\\"held\\\"]\"}");
        assertThat(r.status()).as("setup: " + r.body()).isEqualTo(200);
    }

    private void seedDocument(String c) {
        fx.asSuperuser(ctx -> {
            PgContainerHelper.insertCatalogDocument(ctx, fx.tenant, DOC);
            return null;
        });
    }

    // ---- the manifest write and append ------------------------------------------------------------------------------------------

    @Test
    void manifestWrite_underAnIdRenamedAwayBeforeItsRead_isRefused_andWritesNothingToTheNewHolder() {
        String b = registered("mw");
        seedDocument(b);
        Resp r = withTheLabelRenamedAwayAndTaken(b, id -> { },
            id -> fx.post("/v1/catalog/manifest/write", "{\"collection_id\":" + id + ",\"doc_id\":\"" + DOC + "\",\"rows\":[]}"));
        assertRefused(r);
        assertThat(manifestRows(b)).as("the new holder of B got no manifest row").isZero();
    }

    @Test
    void manifestAppend_underAnIdRenamedAwayBeforeItsRead_isRefused_andWritesNothingToTheNewHolder() {
        String b = registered("ma");
        seedDocument(b);
        Resp r = withTheLabelRenamedAwayAndTaken(b, id -> { },
            id -> fx.post("/v1/catalog/manifest/append", "{\"collection_id\":" + id + ",\"doc_id\":\"" + DOC + "\",\"rows\":[]}"));
        assertRefused(r);
        assertThat(manifestRows(b)).as("the new holder of B got no manifest row").isZero();
    }

    // ---- the taxonomy writes -------------------------------------------------------------------------------------------------------

    @Test
    void topicInsert_underAnIdRenamedAwayBeforeItsRead_isRefused_andFilesNothingInTheNewHolder() {
        String b = registered("ti");
        Resp r = withTheLabelRenamedAwayAndTaken(b, id -> { },
            id -> fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + id + ",\"label\":\"p35c-i7\",\"doc_count\":1}"));
        assertRefused(r);
        assertThat(topics(b)).as("the new holder of B got no topic").isZero();
    }

    @Test
    void assignFromChashes_underAnIdRenamedAwayBeforeItsRead_isRefused() {
        String b = registered("af");
        Resp r = withTheLabelRenamedAwayAndTaken(b, id -> { },
            id -> fx.post("/v1/taxonomy/assignments/assign_from_chashes", "{\"collection_id\":" + id + ",\"chashes\":[\""
                + Chash.ofText("p35c-i7-af").toHex() + "\"],\"cross_collection\":false}"));
        assertRefused(r);
    }

    @Test
    void metaRecord_underAnIdRenamedAwayBeforeItsRead_isRefused_andRecordsNothingForTheNewHolder() {
        String b = registered("mr");
        Resp r = withTheLabelRenamedAwayAndTaken(b, id -> { },
            id -> fx.post("/v1/taxonomy/meta/record", "{\"collection_id\":" + id + ",\"doc_count\":9}"));
        assertRefused(r);
        assertThat(metas(b)).as("the new holder of B got no meta row").isZero();
    }

    @Test
    void purgeCollection_underAnIdRenamedAwayBeforeItsRead_isRefused_andPurgesNothingOfTheNewHolder() {
        String b = registered("pc");
        Resp r = withTheLabelRenamedAwayAndTaken(b, newId -> {
            seedTopic(newId);
            seedMeta(newId);
        }, id -> fx.post("/v1/taxonomy/purge_collection", "{\"collection_id\":" + id + "}"));
        assertRefused(r);
        assertThat(topics(b)).as("the new holder's topic survives the purge").isEqualTo(1);
        assertThat(metas(b)).as("and its meta row (the purge meta)").isEqualTo(1);
    }

    // ---- the aspect writes -----------------------------------------------------------------------------------------------------------

    @Test
    void aspectsUpsert_underAnIdRenamedAwayBeforeItsRead_isRefused_andWritesNothingToTheNewHolder() {
        String b = registered("au");
        String docId = "own-" + b;
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, docId, b);
            return null;
        });
        Resp r = withTheLabelRenamedAwayAndTaken(b, id -> { },
            id -> fx.post("/v1/aspects/upsert", "{\"collection_id\":" + id + ",\"source_path\":" + json(PATH) + ",\"doc_id\":" + json(docId)
                + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\",\"model_version\":\"v1\",\"extractor_name\":\"ex\","
                + "\"salient_sentences\":\"[\\\"x\\\"]\"}"));
        assertRefused(r);
        assertThat(aspects(b)).as("the new holder of B got no aspect row").isZero();
    }

    @Test
    void aspectsDelete_underAnIdRenamedAwayBeforeItsRead_isRefused_andDeletesNothingOfTheNewHolder() {
        String b = registered("ad");
        Resp r = withTheLabelRenamedAwayAndTaken(b, newId -> seedAspect(newId),
            id -> fx.post("/v1/aspects/delete", "{\"collection_id\":" + id + ",\"source_path\":" + json(PATH) + "}"));
        assertRefused(r);
        assertThat(aspects(b)).as("the new holder's aspect row survives").isEqualTo(1);
    }

    @Test
    void aspectsSetSalientByKey_underAnIdRenamedAwayBeforeItsRead_isRefused_andChangesNothingOfTheNewHolder() {
        String b = registered("as");
        Resp r = withTheLabelRenamedAwayAndTaken(b, newId -> seedAspect(newId),
            id -> fx.post("/v1/aspects/salient_sentences/set_by_key",
                "{\"collection_id\":" + id + ",\"source_path\":" + json(PATH) + ",\"sentences\":[\"changed\"]}"));
        assertRefused(r);
        String held = fx.asSuperuser(ctx -> ctx.select(DOCUMENT_ASPECTS.SALIENT_SENTENCES).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_ASPECTS.COLLECTION.eq(b))).fetchOne(x -> String.valueOf(x.value1())));
        assertThat(held).as("the new holder's sentences are untouched").contains("held").doesNotContain("changed");
    }

    @Test
    void highlightsUpsert_underAnIdRenamedAwayBeforeItsRead_isRefused_andWritesNothingToTheNewHolder() {
        String b = registered("hu");
        Resp r = withTheLabelRenamedAwayAndTaken(b, id -> { },
            id -> fx.post("/v1/aspects/highlights/upsert", "{\"collection_id\":" + id + ",\"doc_id\":\"p35c-i7-doc\","
                + "\"ingested_at\":\"2026-01-01T00:00:00Z\",\"highlights_md\":\"p35c\"}"));
        assertRefused(r);
        assertThat(highlights(b)).as("the new holder of B got no highlight row").isZero();
    }

    // ---- I8: a write stores the locked registry row's CURRENT label, never the request's ---------------------------------------------

    /**
     * RDR-229 P3.5c, census invariant I8 (linda via folio): after a one-row rename X: B to C and a NEW row under B, a write by X
     * stores the row (label C, id X), never (B, X), and the new holder of B keeps nothing of it. The rename is the DB seam of the
     * rename-gap tests (P3.7's rename has no route yet); the registry cache is evicted so the route boundary resolves X to C. The
     * stale-cache variant (the boundary still says B) is the I7 tests above: it is refused 409 before any row is stored.
     */
    private long renamedAndLabelTaken(String b) {
        long x = fx.idOf(b);
        oneRowRename(b, b + "-moved");
        fx.register(b);
        CollectionRegistry.shared().evictTenant(fx.tenant);
        assertThat(fx.idOf(b)).as("setup: the old label is a new row's").isNotEqualTo(x);
        return x;
    }

    private static final String EMBED = EMBEDDING_384;

    /** The label C the stored rows must carry, and that none of them carries B. */
    private record Renamed(long x, String b, String c) { }

    private Renamed renamed(String stem) {
        String b = registered("i8-" + stem);
        long x = renamedAndLabelTaken(b);
        return new Renamed(x, b, b + "-moved");
    }

    private void assertLabels(Renamed r, java.util.List<String> storedNames, int newHolderRows) {
        assertThat(storedNames).as("rows stored under id %d", r.x()).isNotEmpty().allMatch(r.c()::equals);
        assertThat(newHolderRows).as("the new holder of %s got none", r.b()).isZero();
    }

    @Test
    void i8_topicInsert_storesTheRowsCurrentLabel() {
        Renamed r = renamed("ti");
        Resp resp = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + r.x() + ",\"label\":\"i8\",\"doc_count\":1}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(TOPICS.COLLECTION).from(TOPICS).where(TOPICS.TENANT_ID.eq(fx.tenant)
            .and(TOPICS.COLLECTION_ID.eq(r.x()))).fetch(TOPICS.COLLECTION)), topics(r.b()));
    }

    @Test
    void i8_metaRecord_storesTheRowsCurrentLabel() {
        Renamed r = renamed("mr");
        Resp resp = fx.post("/v1/taxonomy/meta/record", "{\"collection_id\":" + r.x() + ",\"doc_count\":4}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(TAXONOMY_META.COLLECTION).from(TAXONOMY_META).where(TAXONOMY_META.TENANT_ID.eq(fx.tenant)
            .and(TAXONOMY_META.COLLECTION_ID.eq(r.x()))).fetch(TAXONOMY_META.COLLECTION)), metas(r.b()));
    }

    @Test
    void i8_centroidUpsert_storesTheRowsCurrentLabel() {
        Renamed r = renamed("cu");
        Resp topic = fx.post("/v1/taxonomy/topics/insert", "{\"collection_id\":" + r.x() + ",\"label\":\"i8c\",\"doc_count\":1}");
        assertThat(topic.status()).as(topic.body()).isEqualTo(200);
        long topicId = topic.json().path("id").asLong();
        Resp resp = fx.post("/v1/taxonomy/centroids/upsert", "{\"records\":[{\"collection_id\":" + r.x() + ",\"topic_id\":" + topicId
            + ",\"label\":\"i8\",\"doc_count\":1,\"embedding\":" + EMBED + "}]}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(TAXONOMY_CENTROIDS.COLLECTION).from(TAXONOMY_CENTROIDS)
            .where(TAXONOMY_CENTROIDS.TENANT_ID.eq(fx.tenant).and(TAXONOMY_CENTROIDS.COLLECTION_ID.eq(r.x()))).fetch(TAXONOMY_CENTROIDS.COLLECTION)),
            fx.asSuperuser(ctx -> ctx.fetchCount(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID.eq(fx.tenant).and(TAXONOMY_CENTROIDS.COLLECTION.eq(r.b())))));
    }

    @Test
    void i8_persistDiscovered_storesTheRowsCurrentLabel() {
        Renamed r = renamed("pd");
        Resp resp = fx.post("/v1/taxonomy/topics/persist_discovered",
            "{\"collection_id\":" + r.x() + ",\"specs\":[{\"label\":\"found\",\"doc_ids\":[]}]}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(TOPICS.COLLECTION).from(TOPICS).where(TOPICS.TENANT_ID.eq(fx.tenant)
            .and(TOPICS.COLLECTION_ID.eq(r.x()))).fetch(TOPICS.COLLECTION)), topics(r.b()));
    }

    @Test
    void i8_aspectsUpsert_storesTheRowsCurrentLabel() {
        Renamed r = renamed("au");
        String docId = "own-" + r.c();
        fx.asSuperuser(ctx -> {
            CollectionIdSeeds.ownerDocument(ctx, fx.tenant, docId, r.c());
            return null;
        });
        Resp resp = fx.post("/v1/aspects/upsert", "{\"collection_id\":" + r.x() + ",\"source_path\":" + json(PATH) + ",\"doc_id\":" + json(docId)
            + ",\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\",\"model_version\":\"v1\",\"extractor_name\":\"ex\","
            + "\"salient_sentences\":\"[\\\"x\\\"]\"}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(DOCUMENT_ASPECTS.COLLECTION).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_ASPECTS.COLLECTION_ID.eq(r.x()))).fetch(DOCUMENT_ASPECTS.COLLECTION)), aspects(r.b()));
    }

    @Test
    void i8_highlightsUpsert_storesTheRowsCurrentLabel() {
        Renamed r = renamed("hu");
        Resp resp = fx.post("/v1/aspects/highlights/upsert", "{\"collection_id\":" + r.x() + ",\"doc_id\":\"p35c-i8-doc\","
            + "\"ingested_at\":\"2026-01-01T00:00:00Z\",\"highlights_md\":\"p35c\"}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(DOCUMENT_HIGHLIGHTS.COLLECTION).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(fx.tenant).and(DOCUMENT_HIGHLIGHTS.COLLECTION_ID.eq(r.x()))).fetch(DOCUMENT_HIGHLIGHTS.COLLECTION)),
            highlights(r.b()));
    }

    @Test
    void i8_upsertChunks_storesTheRowsCurrentLabel() {
        Renamed r = renamed("uc");
        Resp resp = fx.post("/v1/vectors/upsert-chunks", "{\"collection_id\":" + r.x() + ",\"ids\":[\"" + Chash.ofText("p35c-i8-chunk").toHex()
            + "\"],\"documents\":[\"i8 text\"]}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(CHUNKS.COLLECTION).from(CHUNKS).where(CHUNKS.TENANT_ID.eq(fx.tenant)
            .and(CHUNKS.COLLECTION_ID.eq(r.x()))).fetch(CHUNKS.COLLECTION)),
            fx.asSuperuser(ctx -> ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(fx.tenant).and(CHUNKS.COLLECTION.eq(r.b())))));
    }

    @Test
    void i8_pipelineCreate_storesTheRowsCurrentLabel() {
        Renamed r = renamed("pc");
        Resp resp = fx.post("/v1/pipeline/create", "{\"collection_id\":" + r.x() + ",\"content_hash\":\"p35c-i8-" + r.x() + "\",\"pdf_path\":\"/i8.pdf\"}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(PDF_PIPELINE.COLLECTION).from(PDF_PIPELINE).where(PDF_PIPELINE.TENANT_ID.eq(fx.tenant)
            .and(PDF_PIPELINE.COLLECTION_ID.eq(r.x()))).fetch(PDF_PIPELINE.COLLECTION)),
            fx.asSuperuser(ctx -> ctx.fetchCount(PDF_PIPELINE, PDF_PIPELINE.TENANT_ID.eq(fx.tenant).and(PDF_PIPELINE.COLLECTION.eq(r.b())))));
    }

    @Test
    void i8_remapRecordBatch_storesTheRowsCurrentLabel() {
        Renamed r = renamed("rb");
        String target = registered("i8-rb-target");
        Resp resp = fx.post("/v1/remap/record_batch", "{\"source_collection_id\":" + r.x() + ",\"entries\":[{\"old_id\":\"i8-old\","
            + "\"new_chash\":\"" + Chash.ofText("p35c-i8-remap").toHex() + "\",\"target_collection\":" + json(target) + ",\"provenance\":\"i8\"}]}");
        assertThat(resp.status()).as(resp.body()).isEqualTo(200);
        assertLabels(r, fx.asSuperuser(ctx -> ctx.select(CHASH_REMAP.SOURCE_COLLECTION).from(CHASH_REMAP).where(CHASH_REMAP.TENANT_ID.eq(fx.tenant)
            .and(CHASH_REMAP.SOURCE_COLLECTION_ID.eq(r.x()))).fetch(CHASH_REMAP.SOURCE_COLLECTION)),
            fx.asSuperuser(ctx -> ctx.fetchCount(CHASH_REMAP, CHASH_REMAP.TENANT_ID.eq(fx.tenant).and(CHASH_REMAP.SOURCE_COLLECTION.eq(r.b())))));
    }
}
