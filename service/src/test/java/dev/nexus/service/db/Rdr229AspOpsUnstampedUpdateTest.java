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
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.PDF_PIPELINE;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.1 stage 2b, the ASPECTS and OPS families (nexus-p2m1x.17): every UPDATE that names no collection column
 * still sets the id. The NOT VALID CHECK looks at every updated row, and a row the stamp has not reached has a name and
 * no id, so an update that leaves the id alone is refused 23514 from the first release that has the CHECK until the
 * stamp finishes.
 *
 * <p>The seed is a row with a name and a NULL id: write it through the repository, then drop the CHECK and null the id
 * (a CHECK that is in place refuses to hold such a row, and it cannot be seeded another way). Each test checks that
 * the repository's update puts the registry's id back. The container is this class's own, so dropping the CHECKs
 * touches no other test. {@link Rdr229AspOpsDualWriteTest} holds the writers' inserts with the CHECKs in force.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229AspOpsUnstampedUpdateTest {

    private static final String SVC_ROLE = "svc_rdr229_aspops_u";
    private static final String SVC_PASS = "svc_rdr229_aspops_u_pass";
    private static final String T = "aspops-unstamped";
    private static final String COLL = "docs__aspopsu__bge-base-en-v15-768__v1";
    private static final String TGT = "docs__aspopsu-tgt__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    long collId;
    long tgtId;

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
            PgContainerHelper.insertCollection(dsl, T, COLL);
            PgContainerHelper.insertCollection(dsl, T, TGT);
            for (String tumbler : List.of("1.1.1", "1.1.2", "1.1.3", "1.1.4")) {
                dsl.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, T).set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
                    .set(CATALOG_DOCUMENTS.TITLE, "fixture " + tumbler)
                    .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER).doNothing().execute();
            }
            collId = registryId(dsl, COLL);
            tgtId = registryId(dsl, TGT);
            PgContainerHelper.dropConstraint(su, DOCUMENT_ASPECTS, "document_aspects_collection_id_check");
            PgContainerHelper.dropConstraint(su, DOCUMENT_HIGHLIGHTS, "document_highlights_collection_id_check");
            PgContainerHelper.dropConstraint(su, PDF_PIPELINE, "pdf_pipeline_collection_id_check");
            PgContainerHelper.dropConstraint(su, REEMBED_JOBS, "reembed_jobs_source_collection_id_check");
            PgContainerHelper.dropConstraint(su, REEMBED_JOBS, "reembed_jobs_target_collection_id_check");
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

    // ── document_aspects ────────────────────────────────────────────────────────

    private void seedUnstampedAspect(String path, String docId) throws Exception {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("collection", COLL);
        m.put("source_path", path);
        m.put("confidence", 0.9);
        m.put("extracted_at", "2026-06-01T10:00:00.000000Z");
        m.put("model_version", "v1");
        m.put("extractor_name", "x");
        m.put("doc_id", docId);
        m.put("source_uri", "file:///" + path);
        new AspectRepository(scope).upsertAspect(T, m);
        su(d -> d.update(DOCUMENT_ASPECTS).setNull(DOCUMENT_ASPECTS.COLLECTION_ID)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(T)).and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(path)).execute());
        assertThat(aspectId(path)).as("the seed is unstamped").isNull();
    }

    private Long aspectId(String path) throws Exception {
        return su(d -> d.select(DOCUMENT_ASPECTS.COLLECTION_ID).from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(T)).and(DOCUMENT_ASPECTS.SOURCE_PATH.eq(path))
            .fetchOne(DOCUMENT_ASPECTS.COLLECTION_ID));
    }

    @Test
    void setSalientSentences_stampsAnUnstampedRow() throws Exception {
        seedUnstampedAspect("ss.pdf", "1.1.4");
        assertThat(new AspectRepository(scope).setSalientSentences(T, "1.1.4", "[\"s\"]")).isEqualTo(1);
        assertThat(aspectId("ss.pdf")).isEqualTo(collId);
    }

    @Test
    void setSalientSentencesByKey_stampsAnUnstampedRow() throws Exception {
        seedUnstampedAspect("ssk.pdf", "1.1.2");
        assertThat(new AspectRepository(scope).setSalientSentencesByKey(T, COLL, "ssk.pdf", "[\"s\"]")).isEqualTo(1);
        assertThat(aspectId("ssk.pdf")).isEqualTo(collId);
    }

    @Test
    void upsertAspect_conflictBranch_stampsAnUnstampedRow() throws Exception {
        seedUnstampedAspect("cf.pdf", "1.1.3");
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("collection", COLL);
        m.put("source_path", "cf.pdf");
        m.put("confidence", 0.9);
        m.put("extracted_at", "2026-06-01T10:00:00.000000Z");
        m.put("model_version", "v2");
        m.put("extractor_name", "x");
        m.put("doc_id", "1.1.3");
        m.put("source_uri", "file:///cf.pdf");
        new AspectRepository(scope).upsertAspect(T, m);
        assertThat(aspectId("cf.pdf")).isEqualTo(collId);
    }

    @Test
    void importAspectsBatch_conflictBranch_stampsAnUnstampedRow() throws Exception {
        seedUnstampedAspect("cfb.pdf", "1.1.1");
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("collection", COLL);
        m.put("source_path", "cfb.pdf");
        m.put("confidence", 0.9);
        m.put("extracted_at", "2026-06-01T10:00:00.000000Z");
        m.put("model_version", "v2");
        m.put("extractor_name", "x");
        m.put("doc_id", "1.1.1");
        m.put("source_uri", "file:///cfb.pdf");
        new AspectRepository(scope).importAspectsBatch(T, List.of(m));
        assertThat(aspectId("cfb.pdf")).isEqualTo(collId);
    }

    // ── document_highlights ─────────────────────────────────────────────────────

    @Test
    void upsertHighlight_conflictBranch_stampsAnUnstampedRow() throws Exception {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("doc_id", "1.1.1");
        m.put("collection", COLL);
        m.put("source_uri", "file:///hl");
        m.put("highlights_md", "h");
        m.put("mentions_md", "m");
        m.put("ingested_at", "2026-06-01T10:00:00.000000Z");
        var repo = new AspectRepository(scope);
        repo.upsertHighlight(T, m);
        su(d -> d.update(DOCUMENT_HIGHLIGHTS).setNull(DOCUMENT_HIGHLIGHTS.COLLECTION_ID)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(T)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("1.1.1")).execute());
        repo.upsertHighlight(T, m);
        Long stamped = su(d -> d.select(DOCUMENT_HIGHLIGHTS.COLLECTION_ID).from(DOCUMENT_HIGHLIGHTS)
            .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(T)).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("1.1.1"))
            .fetchOne(DOCUMENT_HIGHLIGHTS.COLLECTION_ID));
        assertThat(stamped).isEqualTo(collId);
    }

    // ── pdf_pipeline ────────────────────────────────────────────────────────────

    private long unstampedPipeline(String hash) throws Exception {
        long id = new PipelineRepository(scope).create(T, hash, "/p/" + hash + ".pdf", COLL, true).pipelineId();
        su(d -> d.update(PDF_PIPELINE).setNull(PDF_PIPELINE.COLLECTION_ID)
            .where(PDF_PIPELINE.TENANT_ID.eq(T)).and(PDF_PIPELINE.PIPELINE_ID.eq(id)).execute());
        assertThat(pipelineIdOf(id)).as("the seed is unstamped").isNull();
        return id;
    }

    private Long pipelineIdOf(long pipelineId) throws Exception {
        return su(d -> d.select(PDF_PIPELINE.COLLECTION_ID).from(PDF_PIPELINE)
            .where(PDF_PIPELINE.TENANT_ID.eq(T)).and(PDF_PIPELINE.PIPELINE_ID.eq(pipelineId))
            .fetchOne(PDF_PIPELINE.COLLECTION_ID));
    }

    @Test
    void everyPipelineUpdate_stampsAnUnstampedRow() throws Exception {
        var repo = new PipelineRepository(scope);
        var ref = PipelineRepository.PipelineRef.byDocument("h-upd", COLL, "/p/h-upd.pdf");

        long id = unstampedPipeline("h-upd");
        repo.updateProgress(T, ref, Map.of("pages_extracted", 3));
        assertThat(pipelineIdOf(id)).as("updateProgress").isEqualTo(collId);

        for (var step : List.<java.util.function.Consumer<PipelineRepository>>of(
                r -> r.markCompleted(T, ref),   // first: it only completes a live run
                r -> r.storeExtractionMeta(T, ref, "{}"),
                r -> r.markFailed(T, ref, "boom"),
                r -> r.resetUploaded(T, ref),
                r -> r.clearOrphanWal(T, ref))) {
            su(d -> d.update(PDF_PIPELINE).setNull(PDF_PIPELINE.COLLECTION_ID)
                .where(PDF_PIPELINE.TENANT_ID.eq(T)).and(PDF_PIPELINE.PIPELINE_ID.eq(id)).execute());
            step.accept(repo);
            assertThat(pipelineIdOf(id)).isEqualTo(collId);
        }
    }

    @Test
    void pipelineCreate_takeoverAndLeftoverReset_stampAnUnstampedRow() throws Exception {
        var repo = new PipelineRepository(scope);
        long id = unstampedPipeline("h-take");
        repo.markFailed(T, PipelineRepository.PipelineRef.byDocument("h-take", COLL, "/p/h-take.pdf"), "x");
        su(d -> d.update(PDF_PIPELINE).setNull(PDF_PIPELINE.COLLECTION_ID)
            .where(PDF_PIPELINE.TENANT_ID.eq(T)).and(PDF_PIPELINE.PIPELINE_ID.eq(id)).execute());
        assertThat(repo.create(T, "h-take", "/p/h-take.pdf", COLL, true).status()).isEqualTo("resuming");
        assertThat(pipelineIdOf(id)).as("takeover").isEqualTo(collId);

        repo.markCompleted(T, PipelineRepository.PipelineRef.byDocument("h-take", COLL, "/p/h-take.pdf"));
        su(d -> d.update(PDF_PIPELINE).setNull(PDF_PIPELINE.COLLECTION_ID)
            .where(PDF_PIPELINE.TENANT_ID.eq(T)).and(PDF_PIPELINE.PIPELINE_ID.eq(id)).execute());
        assertThat(repo.create(T, "h-take", "/p/h-take.pdf", COLL, true).status()).isEqualTo("created");
        assertThat(pipelineIdOf(id)).as("leftover reset").isEqualTo(collId);
    }

    // ── reembed_jobs ────────────────────────────────────────────────────────────

    private ReembedJobRepository.Job unstampedJob(String src, String tgt) throws Exception {
        su(d -> { PgContainerHelper.insertCollection(d, T, src); PgContainerHelper.insertCollection(d, T, tgt); return null; });
        var job = new ReembedJobRepository(scope).start(T, src, tgt, null, false, false);
        unstamp(job.jobId());
        return job;
    }

    private void unstamp(UUID jobId) throws Exception {
        su(d -> d.update(REEMBED_JOBS).setNull(REEMBED_JOBS.SOURCE_COLLECTION_ID).setNull(REEMBED_JOBS.TARGET_COLLECTION_ID)
            .where(REEMBED_JOBS.TENANT_ID.eq(T)).and(REEMBED_JOBS.JOB_ID.eq(jobId)).execute());
    }

    private void assertStamped(UUID jobId, String what, String src, String tgt) throws Exception {
        var row = su(d -> d.select(REEMBED_JOBS.SOURCE_COLLECTION_ID, REEMBED_JOBS.TARGET_COLLECTION_ID).from(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(T)).and(REEMBED_JOBS.JOB_ID.eq(jobId)).fetchOne());
        assertThat(row.value1()).as(what + " source id").isEqualTo(su(d -> registryId(d, src)));
        assertThat(row.value2()).as(what + " target id").isEqualTo(su(d -> registryId(d, tgt)));
    }

    @Test
    void everyReembedJobUpdate_stampsAnUnstampedRow() throws Exception {
        String src = "docs__aspopsu-rsrc__bge-base-en-v15-768__v1";
        String tgt = "docs__aspopsu-rtgt__minilm-l6-v2-384__v1";
        var repo = new ReembedJobRepository(scope);
        var job = unstampedJob(src, tgt);
        UUID id = job.jobId();

        repo.applyTransition(T, id, ReembedJobRepository.PLANNED, null, ReembedJobRepository.RUNNING,
            ReembedJobRepository.Transition.NONE);
        assertStamped(id, "applyTransition", src, tgt);

        unstamp(id);
        repo.advanceAfterBatch(T, id, new ReembedJobRepository.BatchResult(List.of(), 1, 0, 1, "1.1.1"));
        assertStamped(id, "advanceAfterBatch", src, tgt);

        unstamp(id);
        repo.startNextPass(T, id, 1);
        assertStamped(id, "startNextPass", src, tgt);

        unstamp(id);
        repo.setLastError(T, id, ReembedJobRepository.RUNNING, "blocked");
        assertStamped(id, "setLastError", src, tgt);

        repo.applyTransition(T, id, ReembedJobRepository.RUNNING, null, ReembedJobRepository.CUTOVER,
            ReembedJobRepository.Transition.NONE);
        unstamp(id);
        repo.setCutoverStep(T, id, "superseded");
        assertStamped(id, "setCutoverStep", src, tgt);

        unstamp(id);
        repo.rescan(T, id, "superseded");
        assertStamped(id, "rescan", src, tgt);

        unstamp(id);
        scope.withTenant(T, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, T, id, "1.1.1"); return 0; });
        assertStamped(id, "recordMoveInTxn", src, tgt);
    }

    @Test
    void reembedReenterAndMarkRevived_stampAnUnstampedRow() throws Exception {
        String src = "docs__aspopsu-esrc__bge-base-en-v15-768__v1";
        String tgt = "docs__aspopsu-etgt__minilm-l6-v2-384__v1";
        var repo = new ReembedJobRepository(scope);
        var job = unstampedJob(src, tgt);
        UUID id = job.jobId();
        repo.applyTransition(T, id, ReembedJobRepository.PLANNED, null, ReembedJobRepository.DONE,
            ReembedJobRepository.Transition.disposition("kept_tombstoned"));

        unstamp(id);
        assertThat(repo.markRevived(T, src, tgt)).isPresent();
        assertStamped(id, "markRevived", src, tgt);

        // markRevived took the disposition; put kept_tombstoned back (the repository's own transition, from done)
        // to reach reenter from a done job.
        var again = unstampedJob("docs__aspopsu-e2src__bge-base-en-v15-768__v1", "docs__aspopsu-e2tgt__minilm-l6-v2-384__v1");
        repo.applyTransition(T, again.jobId(), ReembedJobRepository.PLANNED, null, ReembedJobRepository.DONE,
            ReembedJobRepository.Transition.disposition("kept_tombstoned"));
        unstamp(again.jobId());
        assertThat(repo.reenter(T, again.jobId(), ReembedJobRepository.RUNNING)).isPresent();
        assertStamped(again.jobId(), "reenter", "docs__aspopsu-e2src__bge-base-en-v15-768__v1",
            "docs__aspopsu-e2tgt__minilm-l6-v2-384__v1");
    }
}
