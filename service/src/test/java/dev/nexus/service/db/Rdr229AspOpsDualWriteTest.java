// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHASH_REMAP;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.HOOK_FAILURES;
import static dev.nexus.service.jooq.nexus.Tables.PDF_PIPELINE;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TELEMETRY;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.1 stage 2b, the ASPECTS and OPS families (nexus-p2m1x.17): the production dual write on real Postgres with
 * the P3.1 CHECKs enforced. Each writer of {@code document_aspects}, {@code document_highlights}, {@code chash_remap},
 * {@code pdf_pipeline}, {@code reembed_jobs} and the history tables {@code relevance_log}, {@code search_telemetry} and
 * {@code hook_failures} must write the registry's id beside the name. Before the writers set it, every insert here is
 * refused with 23514 by the NAME_ID CHECK (history tables excepted: they have none, so their rows land with a NULL id
 * and the assertion on the id fails instead).
 *
 * <p>The update paths on UNSTAMPED rows are in {@link Rdr229AspOpsUnstampedUpdateTest} (it drops the CHECKs to seed a
 * row that has a name and no id).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229AspOpsDualWriteTest {

    private static final String SVC_ROLE = "svc_rdr229_aspops";
    private static final String SVC_PASS = "svc_rdr229_aspops_pass";
    private static final String T = "aspops-a";
    private static final String COLL = "docs__aspops__bge-base-en-v15-768__v1";
    private static final String COLL2 = "docs__aspops2__bge-base-en-v15-768__v1";
    private static final String UNREGISTERED = "docs__aspops-never__bge-base-en-v15-768__v1";
    private static final String CHASH = "ab".repeat(32);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    long id1;
    long id2;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String name : List.of(COLL, COLL2, "")) {
                PgContainerHelper.insertCollection(dsl, T, name);
            }
            for (String tumbler : List.of("1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5")) {
                dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, T)
                    .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                    .set(CATALOG_DOCUMENTS.TITLE, "fixture " + tumbler)
                    .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER).doNothing()
                    .execute();
            }
            id1 = registryId(dsl, COLL);
            id2 = registryId(dsl, COLL2);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static long registryId(DSLContext dsl, String name) {
        return dsl.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID);
    }

    private <R> R su(java.util.function.Function<DSLContext, R> work) throws Exception {
        try (Connection c = pg.createConnection("")) {
            return work.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    private static Map<String, Object> aspect(String collection, String path, String docId) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("collection", collection);
        m.put("source_path", path);
        m.put("problem_formulation", "p");
        m.put("confidence", 0.9);
        m.put("extracted_at", "2026-06-01T10:00:00.000000Z");
        m.put("model_version", "v1");
        m.put("extractor_name", "x");
        m.put("doc_id", docId);
        m.put("source_uri", "file:///" + path);
        return m;
    }

    private Long aspectId(String collection, String path) throws Exception {
        return su(d -> d.select(DOCUMENT_ASPECTS.COLLECTION_ID).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(T)).and(DOCUMENT_ASPECTS.COLLECTION.eq(collection))
            .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(path)).fetchOne(DOCUMENT_ASPECTS.COLLECTION_ID));
    }

    private Long highlightId(String docId) throws Exception {
        return su(d -> d.select(DOCUMENT_HIGHLIGHTS.COLLECTION_ID).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(T)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId))
            .fetchOne(DOCUMENT_HIGHLIGHTS.COLLECTION_ID));
    }

    // ── document_aspects ────────────────────────────────────────────────────────

    @Test
    void upsertAspect_setsTheRegistryId() throws Exception {
        var repo = new AspectRepository(scope);
        repo.upsertAspect(T, aspect(COLL, "up.pdf", "1.1.1"));
        assertThat(aspectId(COLL, "up.pdf")).isEqualTo(id1);
        // the conflict branch carries it too: a second upsert of the same key still has the id
        repo.upsertAspect(T, aspect(COLL, "up.pdf", "1.1.1"));
        assertThat(aspectId(COLL, "up.pdf")).isEqualTo(id1);
    }

    @Test
    void importAspect_setsTheRegistryId() throws Exception {
        new AspectRepository(scope).importAspect(T, aspect(COLL2, "imp.pdf", "1.1.2"));
        assertThat(aspectId(COLL2, "imp.pdf")).isEqualTo(id2);
    }

    @Test
    void importAspectsBatch_setsEachRowsOwnCollectionId() throws Exception {
        new AspectRepository(scope).importAspectsBatch(T, List.of(
            aspect(COLL, "b1.pdf", "1.1.3"), aspect(COLL2, "b2.pdf", "1.1.3"), aspect(COLL, "b3.pdf", "1.1.3")));
        assertThat(aspectId(COLL, "b1.pdf")).isEqualTo(id1);
        assertThat(aspectId(COLL2, "b2.pdf")).isEqualTo(id2);
        assertThat(aspectId(COLL, "b3.pdf")).isEqualTo(id1);
    }

    @Test
    void renameAspectCollection_restampsTheRewrittenRowsWithTheNewNamesId() throws Exception {
        var repo = new AspectRepository(scope);
        String src = "docs__aspops-asp-src__bge-base-en-v15-768__v1";
        su(d -> { PgContainerHelper.insertCollection(d, T, src); return null; });
        repo.upsertAspect(T, aspect(src, "mv.pdf", "1.1.4"));
        assertThat(repo.renameAspectCollection(T, src, COLL2)).isEqualTo(1);
        assertThat(aspectId(COLL2, "mv.pdf")).isEqualTo(id2);
    }

    // ── document_highlights ─────────────────────────────────────────────────────

    private static Map<String, Object> highlight(String docId, String collection) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("doc_id", docId);
        m.put("collection", collection);
        m.put("source_uri", "file:///" + docId);
        m.put("highlights_md", "h");
        m.put("mentions_md", "m");
        m.put("ingested_at", "2026-06-01T10:00:00.000000Z");
        return m;
    }

    @Test
    void upsertHighlight_setsTheRegistryId() throws Exception {
        new AspectRepository(scope).upsertHighlight(T, highlight("1.1.1", COLL));
        assertThat(highlightId("1.1.1")).isEqualTo(id1);
    }

    @Test
    void upsertHighlight_withNoCollection_isTheEmptyNameAndGivesANullId() throws Exception {
        Map<String, Object> m = highlight("1.1.5", COLL);
        m.remove("collection");
        new AspectRepository(scope).upsertHighlight(T, m);
        assertThat(highlightId("1.1.5")).as("'' means unset in the CHECK: the id is NULL").isNull();
    }

    @Test
    void importHighlight_andBatch_setTheRegistryId() throws Exception {
        var repo = new AspectRepository(scope);
        repo.importHighlight(T, highlight("1.1.2", COLL2));
        assertThat(highlightId("1.1.2")).isEqualTo(id2);
        repo.importHighlightsBatch(T, List.of(highlight("1.1.3", COLL), highlight("1.1.4", COLL2)));
        assertThat(highlightId("1.1.3")).isEqualTo(id1);
        assertThat(highlightId("1.1.4")).isEqualTo(id2);
    }

    @Test
    void renameHighlightsCollection_restampsTheRewrittenRows() throws Exception {
        var repo = new AspectRepository(scope);
        String src = "docs__aspops-hl-src__bge-base-en-v15-768__v1";
        su(d -> { PgContainerHelper.insertCollection(d, T, src); return null; });
        repo.upsertHighlight(T, highlight("1.1.1", src));
        assertThat(repo.renameHighlightsCollection(T, src, COLL2)).isEqualTo(1);
        assertThat(highlightId("1.1.1")).isEqualTo(id2);
    }

    // ── chash_remap ─────────────────────────────────────────────────────────────

    @Test
    void remapRecordBatch_setsBothIds() throws Exception {
        new RemapRepository(scope).recordBatch(T, List.of(
            new RemapRepository.RemapEntry(COLL, "old-1", CHASH, COLL2, "t")));
        var row = su(d -> d.select(CHASH_REMAP.SOURCE_COLLECTION_ID, CHASH_REMAP.TARGET_COLLECTION_ID).from(CHASH_REMAP)
            .where(CHASH_REMAP.TENANT_ID.eq(T)).and(CHASH_REMAP.OLD_ID.eq("old-1")).fetchOne());
        assertThat(row.value1()).isEqualTo(id1);
        assertThat(row.value2()).isEqualTo(id2);
        // the conflict branch: a re-record that moves the target re-stamps the target id
        new RemapRepository(scope).recordBatch(T, List.of(
            new RemapRepository.RemapEntry(COLL, "old-1", CHASH, COLL, "t")));
        var again = su(d -> d.select(CHASH_REMAP.TARGET_COLLECTION_ID).from(CHASH_REMAP)
            .where(CHASH_REMAP.TENANT_ID.eq(T)).and(CHASH_REMAP.OLD_ID.eq("old-1")).fetchOne());
        assertThat(again.value1()).isEqualTo(id1);
    }

    @Test
    void remapRecordBatch_unregisteredCollection_isRefusedTyped() {
        assertThatThrownBy(() -> new RemapRepository(scope).recordBatch(T, List.of(
                new RemapRepository.RemapEntry(UNREGISTERED, "old-x", CHASH, COLL, "t"))))
            .isInstanceOf(UnregisteredCollectionException.class);
    }

    // ── pdf_pipeline ────────────────────────────────────────────────────────────

    @Test
    void pipelineCreate_setsTheRegistryId() throws Exception {
        var repo = new PipelineRepository(scope);
        var created = repo.create(T, "hash-aspops-1", "/p/a.pdf", COLL, true);
        Long id = su(d -> d.select(PDF_PIPELINE.COLLECTION_ID).from(PDF_PIPELINE)
            .where(PDF_PIPELINE.TENANT_ID.eq(T)).and(PDF_PIPELINE.PIPELINE_ID.eq(created.pipelineId()))
            .fetchOne(PDF_PIPELINE.COLLECTION_ID));
        assertThat(id).isEqualTo(id1);
    }

    @Test
    void pipelineCreate_unregisteredCollection_isRefusedTyped() {
        assertThatThrownBy(() -> new PipelineRepository(scope).create(T, "hash-aspops-2", "/p/b.pdf", UNREGISTERED, true))
            .isInstanceOf(UnregisteredCollectionException.class);
    }

    // ── reembed_jobs ────────────────────────────────────────────────────────────

    @Test
    void reembedStart_setsBothIds() throws Exception {
        String src = "docs__aspops-rsrc__bge-base-en-v15-768__v1";
        String tgt = "docs__aspops-rtgt__minilm-l6-v2-384__v1";
        su(d -> { PgContainerHelper.insertCollection(d, T, src); PgContainerHelper.insertCollection(d, T, tgt); return null; });
        var job = new ReembedJobRepository(scope).start(T, src, tgt, null, false, false);
        var row = su(d -> d.select(REEMBED_JOBS.SOURCE_COLLECTION_ID, REEMBED_JOBS.TARGET_COLLECTION_ID).from(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(T)).and(REEMBED_JOBS.JOB_ID.eq(job.jobId())).fetchOne());
        assertThat(row.value1()).isEqualTo(su(d -> registryId(d, src)));
        assertThat(row.value2()).isEqualTo(su(d -> registryId(d, tgt)));
    }

    // ── history tables: the id where the name is known, NULL where it is not, never a failed write ───────

    private Long searchId(String hash) throws Exception {
        return su(d -> d.select(SEARCH_TELEMETRY.COLLECTION_ID).from(SEARCH_TELEMETRY)
            .where(SEARCH_TELEMETRY.TENANT_ID.eq(T)).and(SEARCH_TELEMETRY.QUERY_HASH.eq(hash))
            .fetchOne(SEARCH_TELEMETRY.COLLECTION_ID));
    }

    @Test
    void searchTelemetry_logBatchAndImport_setTheIdWhereKnown_andNullWhereNot() throws Exception {
        var repo = new TelemetryRepository(scope);
        repo.logSearchBatch(T, List.of(
            new Object[]{"2026-06-01T10:00:00Z", "h-known", COLL, 3, 2, 0.1, 0.5},
            new Object[]{"2026-06-01T10:00:01Z", "h-unknown", UNREGISTERED, 3, 2, 0.1, 0.5}));
        assertThat(searchId("h-known")).isEqualTo(id1);
        assertThat(searchId("h-unknown")).as("an unregistered name does not fail the write").isNull();
        repo.importSearchRow(T, "2026-06-01T10:00:02Z", "h-imp", COLL2, 1, 1, 0.1, 0.5);
        assertThat(searchId("h-imp")).isEqualTo(id2);
        repo.importBatch(T, "search_telemetry", List.of(Map.of("ts", "2026-06-01T10:00:03Z", "query_hash", "h-batch",
            "collection", COLL, "raw_count", 1, "kept_count", 1)));
        assertThat(searchId("h-batch")).isEqualTo(id1);
    }

    @Test
    void searchTelemetry_renameCollection_restampsTheRewrittenRows() throws Exception {
        var repo = new TelemetryRepository(scope);
        String src = "docs__aspops-tel-src__bge-base-en-v15-768__v1";
        su(d -> { PgContainerHelper.insertCollection(d, T, src); return null; });
        repo.logSearchBatch(T, List.<Object[]>of(new Object[]{"2026-06-01T11:00:00Z", "h-rename", src, 1, 1, 0.1, 0.5}));
        repo.renameCollection(T, src, COLL2);
        assertThat(searchId("h-rename")).isEqualTo(id2);
    }

    @Test
    void relevanceLog_allThreeWritePaths_setTheIdWhereKnown() throws Exception {
        var repo = new TelemetryRepository(scope);
        repo.logRelevance(T, "q-single", "aa".repeat(32), "click", "s", COLL);
        repo.logRelevanceBatch(T, List.of(List.of("q-batch", "bb".repeat(32), COLL2, "click", "s"),
            List.of("q-unknown", "cc".repeat(32), UNREGISTERED, "click", "s")));
        repo.importRelevanceRow(T, "q-imp", "dd".repeat(32), COLL, "click", "s", "2026-06-01T10:00:00Z");
        repo.importBatch(T, "relevance_log", List.of(Map.of("query", "q-ibatch", "chunk_id", "ee".repeat(32), "collection", COLL2,
            "action", "click", "timestamp", "2026-06-01T10:00:00Z")));
        Map<String, Long> ids = su(d -> d.select(RELEVANCE_LOG.QUERY, RELEVANCE_LOG.COLLECTION_ID).from(RELEVANCE_LOG)
            .where(RELEVANCE_LOG.TENANT_ID.eq(T)).fetchMap(RELEVANCE_LOG.QUERY, RELEVANCE_LOG.COLLECTION_ID));
        assertThat(ids.get("q-single")).isEqualTo(id1);
        assertThat(ids.get("q-batch")).isEqualTo(id2);
        assertThat(ids).containsKey("q-unknown");
        assertThat(ids.get("q-unknown")).isNull();
        assertThat(ids.get("q-imp")).isEqualTo(id1);
        assertThat(ids.get("q-ibatch")).isEqualTo(id2);
    }

    @Test
    void hookFailures_allThreeWritePaths_setTheIdWhereKnown() throws Exception {
        var repo = new TelemetryRepository(scope);
        repo.recordHookFailure(T, "d1", COLL, "hook-live", "e", "2026-06-01T10:00:00Z", null, false, "single");
        repo.importHookFailureRow(T, "d2", COLL2, "hook-imp", "e", "2026-06-01T10:00:00Z", null, false, "single");
        repo.importBatch(T, "hook_failures", List.of(Map.of("doc_id", "d3", "collection", COLL, "hook_name", "hook-batch",
            "error", "e", "occurred_at", "2026-06-01T10:00:00Z")));
        repo.recordHookFailure(T, "d4", UNREGISTERED, "hook-unknown", "e", "2026-06-01T10:00:00Z", null, false, "single");
        Map<String, Long> ids = su(d -> d.select(HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.COLLECTION_ID).from(HOOK_FAILURES)
            .where(HOOK_FAILURES.TENANT_ID.eq(T)).fetchMap(HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.COLLECTION_ID));
        assertThat(ids.get("hook-live")).isEqualTo(id1);
        assertThat(ids.get("hook-imp")).isEqualTo(id2);
        assertThat(ids.get("hook-batch")).isEqualTo(id1);
        assertThat(ids).containsKey("hook-unknown");
        assertThat(ids.get("hook-unknown")).isNull();
    }

    // ── an update never nulls an id that is already set (the registry row may be gone by then) ──────────────

    @Test
    void reembedJob_whoseSourceRegistryRowIsDeletedMidCutover_stillUpdates_andKeepsItsIds() throws Exception {
        String src = "docs__aspops-gone-src__bge-base-en-v15-768__v1";
        String tgt = "docs__aspops-gone-tgt__minilm-l6-v2-384__v1";
        su(d -> { PgContainerHelper.insertCollection(d, T, src); PgContainerHelper.insertCollection(d, T, tgt); return null; });
        long srcId = su(d -> registryId(d, src));
        long tgtId = su(d -> registryId(d, tgt));
        var repo = new ReembedJobRepository(scope);
        var job = repo.start(T, src, tgt, null, false, false);
        repo.applyTransition(T, job.jobId(), ReembedJobRepository.PLANNED, null, ReembedJobRepository.RUNNING,
            ReembedJobRepository.Transition.NONE);
        repo.applyTransition(T, job.jobId(), ReembedJobRepository.RUNNING, null, ReembedJobRepository.CUTOVER,
            ReembedJobRepository.Transition.NONE);
        // the cutover removed the source's registry row (the job row has no FK to it)
        su(d -> d.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(T))
            .and(CATALOG_COLLECTIONS.NAME.eq(src)).execute());

        assertThat(repo.setCutoverStep(T, job.jobId(), "superseded")).isTrue();
        assertThat(repo.setLastError(T, job.jobId(), ReembedJobRepository.CUTOVER, "x")).isTrue();
        assertThat(repo.applyTransition(T, job.jobId(), ReembedJobRepository.CUTOVER, "superseded",
            ReembedJobRepository.DONE, ReembedJobRepository.Transition.disposition("deleted"))).isPresent();

        var row = su(d -> d.select(REEMBED_JOBS.SOURCE_COLLECTION_ID, REEMBED_JOBS.TARGET_COLLECTION_ID).from(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(T)).and(REEMBED_JOBS.JOB_ID.eq(job.jobId())).fetchOne());
        assertThat(row.value1()).as("source id kept").isEqualTo(srcId);
        assertThat(row.value2()).as("target id kept").isEqualTo(tgtId);
    }

    @Test
    void pipeline_whoseRegistryRowIsDeleted_stillUpdates_andKeepsItsId() throws Exception {
        String c = "docs__aspops-gone-pipe__bge-base-en-v15-768__v1";
        su(d -> { PgContainerHelper.insertCollection(d, T, c); return null; });
        long id = su(d -> registryId(d, c));
        var repo = new PipelineRepository(scope);
        var created = repo.create(T, "hash-gone-1", "/p/g.pdf", c, true);
        su(d -> d.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(T))
            .and(CATALOG_COLLECTIONS.NAME.eq(c)).execute());
        repo.updateProgress(T, PipelineRepository.PipelineRef.byId(created.pipelineId()), Map.of("pages_extracted", 2));
        Long kept = su(d -> d.select(PDF_PIPELINE.COLLECTION_ID).from(PDF_PIPELINE)
            .where(PDF_PIPELINE.TENANT_ID.eq(T)).and(PDF_PIPELINE.PIPELINE_ID.eq(created.pipelineId()))
            .fetchOne(PDF_PIPELINE.COLLECTION_ID));
        assertThat(kept).isEqualTo(id);
    }
}
