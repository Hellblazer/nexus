package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.testcontainers.containers.PostgreSQLContainer;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Base64;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.PDF_PIPELINE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-186 bead nexus-146xx.16 (engine half) — PipelineHandler endpoint tests.
 *
 * <p>The engine-hosted streaming-PDF buffer (pipeline.db's PG twin). Pins the
 * RDR-048 semantics the client's resume contract depends on:
 * created/resuming/skip with the stale heartbeat, INSERT-OR-REPLACE pages,
 * INSERT-OR-IGNORE chunks (an existing row's embedding is never
 * overwritten), the embedding sentinel tri-state over the wire (null / "" /
 * base64 — nexus-9n1u3), clear_wal preserving the audit row, and RLS.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PipelineHandlerTest {

    private static final String TOKEN = "pipeline-handler-test-token-abc123";
    private static final String OTHER_TOKEN = "pipeline-handler-test-token-def456";
    private static final String SVC_ROLE = "svc_pipeline_handler_test";
    private static final String SVC_PASS = "svc_pipeline_handler_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String OTHER_TENANT = "pipeline-other-tenant";

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), OTHER_TOKEN, OTHER_TENANT, "test-bound-other");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null)   svcDs.close();
        if (pg != null)      pg.stop();
    }

    // ── Test 1: create → created / 409 conflict-when-running / resume-when-failed ─

    @Test
    void create_created_thenConflictWhileRunning_thenResumeAfterFail() throws Exception {
        String hash = "h1-" + "0".repeat(28);
        var r1 = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/a.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(mapper.readValue(r1.body(), MAP_T).get("status")).isEqualTo("created");

        // nexus-lcmbp: a retry against a 'running' row with a FRESH heartbeat must be a
        // loud 409, never a 200 "skip" the caller could mistake for success.
        var r2 = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/a.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(r2.statusCode())
            .as("fresh heartbeat + running must be a loud conflict, never a silent-success 200")
            .isEqualTo(409);
        var body2 = mapper.readValue(r2.body(), MAP_T);
        assertThat(body2.get("status")).isEqualTo("conflict_running");
        assertThat(body2.get("content_hash")).isEqualTo(hash);
        assertThat(body2.get("started_at")).isNotNull();
        assertThat(((Number) body2.get("heartbeat_age_seconds")).longValue()).isGreaterThanOrEqualTo(0);
        assertThat(((Number) body2.get("stale_threshold_seconds")).longValue()).isEqualTo(300);
        assertThat((String) body2.get("remedy")).contains("resume window");
        assertThat((String) body2.get("error")).contains(hash);

        // The row is untouched by the refused attempt — still 'running'.
        var state1 = pipelineState(hash);
        assertThat(state1.get("status")).isEqualTo("running");

        post("/v1/pipeline/fail", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"error\":\"extractor died\"}");
        var r3 = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/a.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(mapper.readValue(r3.body(), MAP_T).get("status"))
            .as("failed → resuming (the RDR-048 crash-resume contract)")
            .isEqualTo("resuming");

        var state = pipelineState(hash);
        assertThat(state.get("status")).isEqualTo("resuming");
        assertThat(state.get("error")).isEqualTo("extractor died");
    }

    // ── Test 2: stale running pipeline resumes ───────────────────────────────

    @Test
    void create_staleHeartbeat_resumes() throws Exception {
        String hash = "h2-" + "0".repeat(28);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/b.pdf\",\"collection\":\"knowledge__t\"}");
        // Age the heartbeat past STALE_THRESHOLD via superuser.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES)
                .update(PDF_PIPELINE)
                .set(PDF_PIPELINE.UPDATED_AT, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(10))
                .where(PDF_PIPELINE.CONTENT_HASH.eq(hash))
                .execute();
        }
        var r = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/b.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(mapper.readValue(r.body(), MAP_T).get("status"))
            .as("stale running pipeline (crashed) → resuming")
            .isEqualTo("resuming");
    }

    // ── Test 3: completed short-circuits ─────────────────────────────────────

    @Test
    void create_completed_skips() throws Exception {
        String hash = "h3-" + "0".repeat(28);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/c.pdf\",\"collection\":\"knowledge__t\"}");
        post("/v1/pipeline/complete", TOKEN, TENANT, "{\"content_hash\":\"" + hash + "\"}");
        var r = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/c.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(mapper.readValue(r.body(), MAP_T).get("status")).isEqualTo("skip");
    }

    // ── Test 4: pages — batch write, replace semantics, read-from ────────────

    @Test
    void pages_batchWrite_replace_readFrom() throws Exception {
        String hash = "h4-" + "0".repeat(28);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/d.pdf\",\"collection\":\"knowledge__t\"}");
        var w = post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"content_hash":"%s","pages":[
              {"page_index":0,"page_text":"page zero","metadata_json":"{}"},
              {"page_index":1,"page_text":"page one","metadata_json":"{}"},
              {"page_index":2,"page_text":"page two","metadata_json":"{}"}
            ]}""".formatted(hash));
        assertThat(mapper.readValue(w.body(), MAP_T).get("written")).isEqualTo(3);

        // REPLACE parity: rewriting page 1 overwrites its text.
        post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"content_hash":"%s","pages":[
              {"page_index":1,"page_text":"page one v2","metadata_json":"{}"}
            ]}""".formatted(hash));

        var resp = get("/v1/pipeline/pages?content_hash=" + hash + "&start=1", TOKEN, TENANT);
        @SuppressWarnings("unchecked")
        var pages = (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("pages");
        assertThat(pages).extracting(pg2 -> pg2.get("page_index")).containsExactly(1, 2);
        assertThat(pages.get(0).get("page_text")).isEqualTo("page one v2");
    }

    // ── Test 5: chunks — INSERT-OR-IGNORE + embedding sentinel tri-state ─────

    @Test
    void chunks_insertOrIgnore_andEmbeddingSentinelRoundTrip() throws Exception {
        String hash = "h5-" + "0".repeat(28);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/e.pdf\",\"collection\":\"knowledge__t\"}");
        String b64 = Base64.getEncoder().encodeToString(new byte[]{1, 2, 3, 4});
        var w = post("/v1/pipeline/chunks", TOKEN, TENANT, """
            {"content_hash":"%s","chunks":[
              {"chunk_index":0,"chunk_text":"c0","chunk_id":"id0","metadata_json":"{}","embedding":null},
              {"chunk_index":1,"chunk_text":"c1","chunk_id":"id1","metadata_json":"{}","embedding":""},
              {"chunk_index":2,"chunk_text":"c2","chunk_id":"id2","metadata_json":"{}","embedding":"%s"}
            ]}""".formatted(hash, b64));
        assertThat(mapper.readValue(w.body(), MAP_T).get("inserted")).isEqualTo(3);

        // IGNORE parity: re-writing chunk 2 with a DIFFERENT embedding must
        // NOT overwrite the existing row (idempotent resume keeps embeddings).
        var w2 = post("/v1/pipeline/chunks", TOKEN, TENANT, """
            {"content_hash":"%s","chunks":[
              {"chunk_index":2,"chunk_text":"c2-changed","chunk_id":"id2","metadata_json":"{}","embedding":null}
            ]}""".formatted(hash));
        assertThat(mapper.readValue(w2.body(), MAP_T).get("inserted")).isEqualTo(0);

        var resp = get("/v1/pipeline/chunks?content_hash=" + hash, TOKEN, TENANT);
        @SuppressWarnings("unchecked")
        var chunks = (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("chunks");
        assertThat(chunks).hasSize(3);
        assertThat(chunks.get(0).get("embedding")).as("NULL survives").isNull();
        assertThat(chunks.get(1).get("embedding")).as("service-mode sentinel survives").isEqualTo("");
        assertThat(chunks.get(2).get("embedding")).as("packed floats survive").isEqualTo(b64);
        assertThat(chunks.get(2).get("chunk_text")).as("IGNORE kept the original").isEqualTo("c2");

        // uploadable = embedding IS NOT NULL (the "" sentinel counts: the JVM
        // embeds at upload) and not yet uploaded.
        var up = get("/v1/pipeline/chunks?content_hash=" + hash + "&uploadable=1", TOKEN, TENANT);
        @SuppressWarnings("unchecked")
        var uploadable = (List<Map<String, Object>>) mapper.readValue(up.body(), MAP_T).get("chunks");
        assertThat(uploadable).extracting(c -> c.get("chunk_index")).containsExactly(1, 2);

        post("/v1/pipeline/mark_uploaded", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"chunk_indices\":[1,2]}");
        var up2 = get("/v1/pipeline/chunks?content_hash=" + hash + "&uploadable=1", TOKEN, TENANT);
        @SuppressWarnings("unchecked")
        var uploadable2 = (List<Map<String, Object>>) mapper.readValue(up2.body(), MAP_T).get("chunks");
        assertThat(uploadable2).isEmpty();

        var counts = get("/v1/pipeline/counts?content_hash=" + hash, TOKEN, TENANT);
        assertThat(mapper.readValue(counts.body(), MAP_T).get("embedded_chunks")).isEqualTo(2);
    }

    // ── Test 6: progress counters + allowlist ────────────────────────────────

    @Test
    void progress_updatesAllowlistedCounters_rejectsUnknown() throws Exception {
        String hash = "h6-" + "0".repeat(28);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/f.pdf\",\"collection\":\"knowledge__t\"}");
        var ok = post("/v1/pipeline/progress", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"fields\":{\"pages_extracted\":7,\"total_pages\":9}}");
        assertThat(ok.statusCode()).isEqualTo(200);
        var state = pipelineState(hash);
        assertThat(state.get("pages_extracted")).isEqualTo(7);
        assertThat(state.get("total_pages")).isEqualTo(9);

        var bad = post("/v1/pipeline/progress", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"fields\":{\"status\":1}}");
        assertThat(bad.statusCode())
            .as("non-allowlisted field rejected (the injection-shaped surface)")
            .isEqualTo(400);
    }

    // ── Test 7: clear_wal preserves the audit row ────────────────────────────

    @Test
    void clearWal_removesPagesAndChunks_keepsPipelineRow() throws Exception {
        String hash = "h7-" + "0".repeat(28);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/g.pdf\",\"collection\":\"knowledge__t\"}");
        post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"content_hash":"%s","pages":[{"page_index":0,"page_text":"p","metadata_json":"{}"}]}"""
            .formatted(hash));
        // nexus-33q80: give both counters clear_orphan_wal must reset a
        // real, non-zero value BEFORE the wipe -- proves the reset, not
        // just the pre-existing default.
        post("/v1/pipeline/progress", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"fields\":{\"pages_extracted\":7,\"chunks_uploaded\":20}}");
        post("/v1/pipeline/fail", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"error\":\"math pdf without MinerU\"}");

        post("/v1/pipeline/clear_wal", TOKEN, TENANT, "{\"content_hash\":\"" + hash + "\"}");

        var pages = get("/v1/pipeline/pages?content_hash=" + hash, TOKEN, TENANT);
        assertThat(((List<?>) mapper.readValue(pages.body(), MAP_T).get("pages"))).isEmpty();
        var state = pipelineState(hash);
        assertThat(state)
            .as("the audit row survives clear_wal (nexus-2fyb orphan-replay fix)")
            .isNotNull();
        assertThat(state.get("error")).isEqualTo("math pdf without MinerU");
        // nexus-33q80: chunks_uploaded/pages_extracted reset in the SAME
        // call (server-side transaction) as the WAL wipe -- no window
        // where the wipe has landed and the counters have not, which is
        // exactly the window a SEPARATE client-side reset call could not
        // close (nexus-6m9zy.1's non-atomicity).
        assertThat(state.get("chunks_uploaded"))
            .as("chunks_uploaded must be zeroed in the same transaction as the WAL wipe")
            .isEqualTo(0);
        assertThat(state.get("pages_extracted"))
            .as("pages_extracted must be zeroed in the same transaction as the WAL wipe")
            .isEqualTo(0);
    }

    // ── Test 8: delete_collection sweeps all three tables ────────────────────

    @Test
    void deleteCollection_removesPipelinePagesChunks() throws Exception {
        String hash = "h8-" + "0".repeat(28);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/h.pdf\",\"collection\":\"knowledge__h8\"}");
        post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"content_hash":"%s","pages":[{"page_index":0,"page_text":"p","metadata_json":"{}"}]}"""
            .formatted(hash));

        var r = post("/v1/pipeline/delete_collection", TOKEN, TENANT,
            "{\"collection\":\"knowledge__h8\"}");
        assertThat(mapper.readValue(r.body(), MAP_T).get("deleted")).isEqualTo(1);
        assertThat(pipelineState(hash)).isNull();
    }

    // ── nexus-edjmu: one row per document (pipeline-002-per-row-identity) ────

    private static String docCreate(String hash, String path, String collection) {
        return "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"" + path
            + "\",\"collection\":\"" + collection + "\",\"identity\":\"document\"}";
    }

    private static String legacyCreate(String hash, String path, String collection) {
        return "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"" + path
            + "\",\"collection\":\"" + collection + "\"}";
    }

    private Map<String, Object> create(String body) throws Exception {
        var r = post("/v1/pipeline/create", TOKEN, TENANT, body);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return mapper.readValue(r.body(), MAP_T);
    }

    private long pipelineId(Map<String, Object> created) {
        return ((Number) created.get("pipeline_id")).longValue();
    }

    private Map<String, Object> stateById(long pipelineId) throws Exception {
        var resp = get("/v1/pipeline/state?pipeline_id=" + pipelineId, TOKEN, TENANT);
        assertThat(resp.statusCode()).isEqualTo(200);
        return (Map<String, Object>) mapper.readValue(resp.body(), MAP_T).get("pipeline");
    }

    private List<?> pagesById(long pipelineId) throws Exception {
        var r = get("/v1/pipeline/pages?pipeline_id=" + pipelineId, TOKEN, TENANT);
        return (List<?>) mapper.readValue(r.body(), MAP_T).get("pages");
    }

    private List<?> uploadableById(long pipelineId) throws Exception {
        var r = get("/v1/pipeline/chunks?uploadable=1&pipeline_id=" + pipelineId, TOKEN, TENANT);
        return (List<?>) mapper.readValue(r.body(), MAP_T).get("chunks");
    }

    private int embeddedCountById(long pipelineId) throws Exception {
        var r = get("/v1/pipeline/counts?pipeline_id=" + pipelineId, TOKEN, TENANT);
        return ((Number) mapper.readValue(r.body(), MAP_T).get("embedded_chunks")).intValue();
    }

    @Test
    void documentIdentity_twoPathsSharingAHash_ownRowsOwnWal() throws Exception {
        String hash = "e1-" + "0".repeat(28);
        var a = create(docCreate(hash, "/tmp/e1-a.pdf", "knowledge__t"));
        var b = create(docCreate(hash, "/tmp/e1-b.pdf", "knowledge__t"));
        assertThat(a.get("status")).isEqualTo("created");
        assertThat(b.get("status")).as("a second path is its OWN run, never a skip or a 409").isEqualTo("created");
        long idA = pipelineId(a), idB = pipelineId(b);
        assertThat(idA).isNotEqualTo(idB);

        post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"pipeline_id":%d,"pages":[{"page_index":0,"page_text":"a","metadata_json":"{}"}]}""".formatted(idA));
        post("/v1/pipeline/chunks", TOKEN, TENANT, """
            {"pipeline_id":%d,"chunks":[{"chunk_index":0,"chunk_text":"a0","chunk_id":"cid-a0","embedding":""}]}""".formatted(idA));
        post("/v1/pipeline/mark_uploaded", TOKEN, TENANT,
            "{\"pipeline_id\":" + idA + ",\"chunk_indices\":[0]}");
        post("/v1/pipeline/chunks", TOKEN, TENANT, """
            {"pipeline_id":%d,"chunks":[{"chunk_index":0,"chunk_text":"b0","chunk_id":"cid-b0","embedding":""}]}""".formatted(idB));

        // The deadlock of the rejected key-widen attempt: B's counters and
        // uploadable set were seeded from A's already-uploaded WAL. Per-row
        // keying makes each run's WAL its own.
        assertThat(pagesById(idB)).as("B has no pages of its own yet").isEmpty();
        assertThat(embeddedCountById(idA)).isEqualTo(1);
        assertThat(embeddedCountById(idB)).isEqualTo(1);
        assertThat(uploadableById(idA)).as("A's chunk is uploaded").isEmpty();
        assertThat(uploadableById(idB)).as("B's chunk is still to upload").hasSize(1);
    }

    @Test
    void documentIdentity_oneRowsCleanup_leavesTheSiblingsWal() throws Exception {
        String hash = "e2-" + "0".repeat(28);
        long idA = pipelineId(create(docCreate(hash, "/tmp/e2-a.pdf", "knowledge__t")));
        long idB = pipelineId(create(docCreate(hash, "/tmp/e2-b.pdf", "knowledge__t")));
        for (long id : new long[] {idA, idB}) {
            post("/v1/pipeline/pages", TOKEN, TENANT, """
                {"pipeline_id":%d,"pages":[{"page_index":0,"page_text":"p","metadata_json":"{}"}]}""".formatted(id));
        }
        post("/v1/pipeline/clear_wal", TOKEN, TENANT, "{\"pipeline_id\":" + idA + "}");
        assertThat(pagesById(idA)).isEmpty();
        assertThat(pagesById(idB)).as("A's clear_wal must not touch B's WAL").hasSize(1);

        var del = post("/v1/pipeline/delete", TOKEN, TENANT, "{\"pipeline_id\":" + idA + "}");
        assertThat(mapper.readValue(del.body(), MAP_T).get("deleted")).isEqualTo(true);
        assertThat(stateById(idA)).isNull();
        assertThat(stateById(idB)).isNotNull();
        assertThat(pagesById(idB)).as("A's delete must not cascade into B").hasSize(1);
    }

    @Test
    void documentIdentity_completedLeftover_isResetAndCreated() throws Exception {
        String hash = "e3-" + "0".repeat(28);
        long id = pipelineId(create(docCreate(hash, "/tmp/e3.pdf", "knowledge__t")));
        post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"pipeline_id":%d,"pages":[{"page_index":0,"page_text":"p","metadata_json":"{}"}]}""".formatted(id));
        post("/v1/pipeline/progress", TOKEN, TENANT,
            "{\"pipeline_id\":" + id + ",\"fields\":{\"pages_extracted\":1,\"chunks_created\":3,\"chunks_uploaded\":3}}");
        post("/v1/pipeline/complete", TOKEN, TENANT, "{\"pipeline_id\":" + id + "}");

        var again = create(docCreate(hash, "/tmp/e3.pdf", "knowledge__t"));
        assertThat(again.get("status"))
            .as("a completed row that outlived its run is crash residue: reset, never skipped")
            .isEqualTo("created");
        assertThat(pipelineId(again)).isEqualTo(id);
        var state = stateById(id);
        assertThat(state.get("status")).isEqualTo("running");
        assertThat(state.get("pages_extracted")).isEqualTo(0);
        assertThat(state.get("chunks_uploaded")).isEqualTo(0);
        assertThat(state.get("chunks_created")).isNull();
        assertThat(pagesById(id)).as("the leftover WAL is wiped").isEmpty();
    }

    @Test
    void legacyCreate_secondPathIsTheSameRow_conflictThenSkip() throws Exception {
        // A client older than pipeline-002 sends no identity: one row per
        // hash, exactly the pipeline-001 contract (create_completed_skips
        // above pins the completed -> skip half unchanged).
        String hash = "e4-" + "0".repeat(28);
        var first = create(legacyCreate(hash, "/tmp/e4-a.pdf", "knowledge__t"));
        assertThat(first.get("status")).isEqualTo("created");
        var second = post("/v1/pipeline/create", TOKEN, TENANT, legacyCreate(hash, "/tmp/e4-b.pdf", "knowledge__t"));
        assertThat(second.statusCode()).as("legacy: a second path while running is the 409 it always was").isEqualTo(409);
        post("/v1/pipeline/complete", TOKEN, TENANT, "{\"content_hash\":\"" + hash + "\"}");
        var third = create(legacyCreate(hash, "/tmp/e4-b.pdf", "knowledge__t"));
        assertThat(third.get("status")).isEqualTo("skip");
        assertThat(pipelineId(third)).isEqualTo(pipelineId(first));
    }

    @Test
    void legacyHashOnlyCalls_landOnTheLegacyRow_notASiblingDocumentRow() throws Exception {
        // An old client mid-run must never be hijacked by a document row a
        // newer client inserts for the same bytes.
        String hash = "e5-" + "0".repeat(28);
        long legacyId = pipelineId(create(legacyCreate(hash, "/tmp/e5-old.pdf", "knowledge__t")));
        long docId = pipelineId(create(docCreate(hash, "/tmp/e5-new.pdf", "knowledge__t")));
        assertThat(docId).isNotEqualTo(legacyId);

        post("/v1/pipeline/progress", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"fields\":{\"pages_extracted\":7}}");
        post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"content_hash":"%s","pages":[{"page_index":0,"page_text":"old","metadata_json":"{}"}]}""".formatted(hash));
        post("/v1/pipeline/complete", TOKEN, TENANT, "{\"content_hash\":\"" + hash + "\"}");

        var legacy = stateById(legacyId);
        assertThat(legacy.get("pages_extracted")).isEqualTo(7);
        assertThat(legacy.get("status")).isEqualTo("completed");
        assertThat(pagesById(legacyId)).hasSize(1);
        var doc = stateById(docId);
        assertThat(doc.get("pages_extracted")).isEqualTo(0);
        assertThat(doc.get("status")).isEqualTo("running");
        assertThat(pagesById(docId)).isEmpty();
        // The hash-only state read resolves the same way.
        assertThat(((Number) pipelineState(hash).get("pipeline_id")).longValue()).isEqualTo(legacyId);
    }

    @Test
    void legacyCreate_besideAStrangersDocumentRow_getsItsOwnRow() throws Exception {
        // The substantive-critic's implementation finding: with NO legacy row
        // for the hash, an old client's create used to adopt a document row
        // of ANOTHER document (completed -> the bead's silent skip; failed ->
        // resuming INTO the stranger's row). A bare hash now names legacy
        // rows only, so the old client inserts beside the document row.
        String hash = "f1-" + "0".repeat(28);
        long docId = pipelineId(create(docCreate(hash, "/tmp/f1-b.pdf", "knowledge__t")));
        post("/v1/pipeline/fail", TOKEN, TENANT, "{\"pipeline_id\":" + docId + ",\"error\":\"crash\"}");

        var legacy = create(legacyCreate(hash, "/tmp/f1-a.pdf", "knowledge__t"));
        assertThat(legacy.get("status")).isEqualTo("created");
        long legacyId = pipelineId(legacy);
        assertThat(legacyId).isNotEqualTo(docId);

        post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"content_hash":"%s","pages":[{"page_index":0,"page_text":"a","metadata_json":"{}"}]}""".formatted(hash));
        post("/v1/pipeline/complete", TOKEN, TENANT, "{\"content_hash\":\"" + hash + "\"}");
        assertThat(stateById(legacyId).get("status")).isEqualTo("completed");
        assertThat(pagesById(legacyId)).hasSize(1);
        var stranger = stateById(docId);
        assertThat(stranger.get("status")).as("the stranger's row is untouched").isEqualTo("failed");
        assertThat(pagesById(docId)).isEmpty();
        // ...and an old client's hash-only delete (its orphan scan) can never
        // reach a document row: with the legacy row gone, it is a miss.
        post("/v1/pipeline/delete", TOKEN, TENANT, "{\"content_hash\":\"" + hash + "\"}");
        var again = post("/v1/pipeline/delete", TOKEN, TENANT, "{\"content_hash\":\"" + hash + "\"}");
        assertThat(mapper.readValue(again.body(), MAP_T).get("deleted")).isEqualTo(false);
        assertThat(stateById(docId)).isNotNull();
    }

    @Test
    void legacyCreate_adoptsItsOwnDocumentsRow_andOwnsItFromThen() throws Exception {
        // The one document row an old client may adopt: the SAME document's
        // (same collection and path). On resume it becomes a legacy row so
        // the client's later hash-only calls find it.
        String hash = "f2-" + "0".repeat(28);
        long docId = pipelineId(create(docCreate(hash, "/tmp/f2.pdf", "knowledge__t")));
        post("/v1/pipeline/fail", TOKEN, TENANT, "{\"pipeline_id\":" + docId + ",\"error\":\"crash\"}");

        var legacy = create(legacyCreate(hash, "/tmp/f2.pdf", "knowledge__t"));
        assertThat(legacy.get("status")).isEqualTo("resuming");
        assertThat(pipelineId(legacy)).isEqualTo(docId);
        var row = stateById(docId);
        assertThat(row.get("keyed_by")).isEqualTo("content_hash");
        post("/v1/pipeline/progress", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"fields\":{\"pages_extracted\":3}}");
        assertThat(stateById(docId).get("pages_extracted")).isEqualTo(3);
    }

    // ── nexus-8vu8p: run_epoch fences a taken-over run's writes ───────────

    private int runEpoch(Map<String, Object> created) {
        return ((Number) created.get("run_epoch")).intValue();
    }

    private HttpResponse<String> writeFenced(String route, long id, int epoch, String extra) throws Exception {
        return post("/v1/pipeline/" + route, TOKEN, TENANT,
            "{\"pipeline_id\":" + id + ",\"run_epoch\":" + epoch + extra + "}");
    }

    private void assertStaleRun(HttpResponse<String> r, long id, int carried, int current) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(409);
        var body = mapper.readValue(r.body(), MAP_T);
        assertThat(body.get("status")).isEqualTo("stale_run");
        assertThat(((Number) body.get("pipeline_id")).longValue()).isEqualTo(id);
        assertThat(body.get("run_epoch")).isEqualTo(carried);
        assertThat(body.get("current_epoch")).isEqualTo(current);
        assertThat((String) body.get("remedy")).contains("taken over");
        assertThat((String) body.get("error")).contains("taken over");
    }

    @Test
    void runEpoch_startsAtZero_bumpsOnEveryTakeover_neverResets() throws Exception {
        String hash = "g1-" + "0".repeat(28);
        var first = create(docCreate(hash, "/tmp/g1.pdf", "knowledge__t"));
        long id = pipelineId(first);
        assertThat(runEpoch(first)).isEqualTo(0);
        // A stale-heartbeat takeover: 0 -> 1.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).update(PDF_PIPELINE)
               .set(PDF_PIPELINE.UPDATED_AT, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(10))
               .where(PDF_PIPELINE.PIPELINE_ID.eq(id)).execute();
        }
        var second = create(docCreate(hash, "/tmp/g1.pdf", "knowledge__t"));
        assertThat(second.get("status")).isEqualTo("resuming");
        assertThat(runEpoch(second)).isEqualTo(1);
        // A failed-row takeover: 1 -> 2 (monotonic across cycles).
        post("/v1/pipeline/fail", TOKEN, TENANT, "{\"pipeline_id\":" + id + ",\"run_epoch\":1,\"error\":\"x\"}");
        var third = create(docCreate(hash, "/tmp/g1.pdf", "knowledge__t"));
        assertThat(runEpoch(third)).isEqualTo(2);
        // A completed-leftover reset: 2 -> 3, never back to 0.
        post("/v1/pipeline/complete", TOKEN, TENANT, "{\"pipeline_id\":" + id + ",\"run_epoch\":2}");
        var fourth = create(docCreate(hash, "/tmp/g1.pdf", "knowledge__t"));
        assertThat(fourth.get("status")).isEqualTo("created");
        assertThat(runEpoch(fourth)).isEqualTo(3);
        assertThat(stateById(id).get("run_epoch")).isEqualTo(3);
        // The first run's delayed write, still at 0, is fenced by the reset row.
        assertStaleRun(writeFenced("pages", id, 0,
            ",\"pages\":[{\"page_index\":0,\"page_text\":\"stale\",\"metadata_json\":\"{}\"}]"), id, 0, 3);
        assertThat(pagesById(id)).isEmpty();
    }

    @Test
    void runEpoch_everyWriteRouteRefusesAStaleEpoch_andWritesNothing() throws Exception {
        String hash = "g2-" + "0".repeat(28);
        long id = pipelineId(create(docCreate(hash, "/tmp/g2.pdf", "knowledge__t")));
        // The new owner's WAL and counters, written at the current epoch (1).
        post("/v1/pipeline/fail", TOKEN, TENANT, "{\"pipeline_id\":" + id + ",\"run_epoch\":0,\"error\":\"x\"}");
        var owner = create(docCreate(hash, "/tmp/g2.pdf", "knowledge__t"));
        assertThat(runEpoch(owner)).isEqualTo(1);
        assertThat(writeFenced("pages", id, 1,
            ",\"pages\":[{\"page_index\":0,\"page_text\":\"owner\",\"metadata_json\":\"{}\"}]").statusCode()).isEqualTo(200);
        assertThat(writeFenced("chunks", id, 1,
            ",\"chunks\":[{\"chunk_index\":0,\"chunk_text\":\"o0\",\"chunk_id\":\"cid-o0\",\"embedding\":\"\"}]").statusCode()).isEqualTo(200);
        assertThat(writeFenced("progress", id, 1, ",\"fields\":{\"pages_extracted\":1}").statusCode()).isEqualTo(200);

        // The stale run, still holding 0: every write route refuses, nothing changes.
        assertStaleRun(writeFenced("pages", id, 0,
            ",\"pages\":[{\"page_index\":1,\"page_text\":\"stale\",\"metadata_json\":\"{}\"}]"), id, 0, 1);
        assertStaleRun(writeFenced("chunks", id, 0,
            ",\"chunks\":[{\"chunk_index\":1,\"chunk_text\":\"s1\",\"chunk_id\":\"cid-s1\"}]"), id, 0, 1);
        assertStaleRun(writeFenced("progress", id, 0, ",\"fields\":{\"pages_extracted\":9}"), id, 0, 1);
        assertStaleRun(writeFenced("extraction_meta", id, 0, ",\"metadata_json\":\"{}\""), id, 0, 1);
        assertStaleRun(writeFenced("mark_uploaded", id, 0, ",\"chunk_indices\":[0]"), id, 0, 1);
        assertStaleRun(writeFenced("complete", id, 0, ""), id, 0, 1);
        assertStaleRun(writeFenced("fail", id, 0, ",\"error\":\"stale\""), id, 0, 1);
        assertStaleRun(writeFenced("clear_wal", id, 0, ""), id, 0, 1);
        assertStaleRun(writeFenced("delete", id, 0, ""), id, 0, 1);

        var state = stateById(id);
        assertThat(state).as("the stale delete removed nothing").isNotNull();
        assertThat(state.get("status")).isEqualTo("resuming");
        assertThat(state.get("pages_extracted")).isEqualTo(1);
        assertThat(state.get("error")).isEqualTo("x");
        assertThat(pagesById(id)).as("the stale clear_wal wiped nothing, the stale page never landed").hasSize(1);
        assertThat(uploadableById(id)).as("the stale mark_uploaded flipped nothing").hasSize(1);
        assertThat(embeddedCountById(id)).isEqualTo(1);
        // Reads are never fenced: the stale run can still see the row.
        var read = get("/v1/pipeline/pages?pipeline_id=" + id + "&run_epoch=0", TOKEN, TENANT);
        assertThat(read.statusCode()).isEqualTo(200);
        // A write carrying no epoch (a client older than pipeline-003) is unfenced.
        var legacy = post("/v1/pipeline/progress", TOKEN, TENANT,
            "{\"pipeline_id\":" + id + ",\"fields\":{\"pages_extracted\":2}}");
        assertThat(legacy.statusCode()).isEqualTo(200);
        assertThat(stateById(id).get("pages_extracted")).isEqualTo(2);
    }

    @Test
    void hashNarrowedByDocument_missesRatherThanWidens() throws Exception {
        String hash = "e6-" + "0".repeat(28);
        long idA = pipelineId(create(docCreate(hash, "/tmp/e6-a.pdf", "knowledge__t")));
        var miss = post("/v1/pipeline/delete", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"collection\":\"knowledge__t\",\"pdf_path\":\"/tmp/e6-zzz.pdf\"}");
        assertThat(mapper.readValue(miss.body(), MAP_T).get("deleted")).isEqualTo(false);
        assertThat(stateById(idA)).as("a narrowing that matches nothing never falls back to the bare hash").isNotNull();
        var hit = post("/v1/pipeline/delete", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"collection\":\"knowledge__t\",\"pdf_path\":\"/tmp/e6-a.pdf\"}");
        assertThat(mapper.readValue(hit.body(), MAP_T).get("deleted")).isEqualTo(true);
        assertThat(stateById(idA)).isNull();
    }

    @Test
    void deleteCollection_keepsTheSiblingRunInAnotherCollection() throws Exception {
        String hash = "e7-" + "0".repeat(28);
        long gone = pipelineId(create(docCreate(hash, "/tmp/e7.pdf", "knowledge__e7gone")));
        long keep = pipelineId(create(docCreate(hash, "/tmp/e7.pdf", "knowledge__e7keep")));
        for (long id : new long[] {gone, keep}) {
            post("/v1/pipeline/pages", TOKEN, TENANT, """
                {"pipeline_id":%d,"pages":[{"page_index":0,"page_text":"p","metadata_json":"{}"}]}""".formatted(id));
        }
        var r = post("/v1/pipeline/delete_collection", TOKEN, TENANT, "{\"collection\":\"knowledge__e7gone\"}");
        assertThat(mapper.readValue(r.body(), MAP_T).get("deleted")).isEqualTo(1);
        assertThat(stateById(gone)).isNull();
        assertThat(pagesById(gone)).isEmpty();
        assertThat(stateById(keep)).isNotNull();
        assertThat(pagesById(keep)).as("the FK cascade is per row, not per hash").hasSize(1);
    }

    @Test
    void walWriteWithoutARow_is400_neverASilentInsert() throws Exception {
        String hash = "e8-" + "0".repeat(28);
        var r = post("/v1/pipeline/pages", TOKEN, TENANT, """
            {"content_hash":"%s","pages":[{"page_index":0,"page_text":"p","metadata_json":"{}"}]}""".formatted(hash));
        assertThat(r.statusCode()).isEqualTo(400);
        assertThat(r.body()).contains("no pipeline row");
        var byId = post("/v1/pipeline/chunks", TOKEN, TENANT, """
            {"pipeline_id":999999999,"chunks":[{"chunk_index":0,"chunk_text":"t","chunk_id":"c"}]}""");
        assertThat(byId.statusCode()).isEqualTo(400);
    }

    @Test
    void counts_withNoRef_isZeroNotAnError() throws Exception {
        var r = get("/v1/pipeline/counts", TOKEN, TENANT);
        assertThat(r.statusCode()).isEqualTo(200);
        var body = mapper.readValue(r.body(), MAP_T);
        assertThat(body.get("embedded_chunks")).isEqualTo(0);
        assertThat(((Number) body.get("pipelines")).intValue()).isGreaterThanOrEqualTo(0);
    }

    @Test
    void create_unknownIdentity_is400() throws Exception {
        var r = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"e9\",\"pdf_path\":\"/tmp/e9.pdf\",\"collection\":\"knowledge__t\",\"identity\":\"tumbler\"}");
        assertThat(r.statusCode()).isEqualTo(400);
    }

    // ── Test 9: RLS isolation through HTTP ───────────────────────────────────

    @Test
    void rls_otherTenantsPipelinesInvisible() throws Exception {
        String hash = "h9-" + "0".repeat(28);
        post("/v1/pipeline/create", OTHER_TOKEN, OTHER_TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/i.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(pipelineState(hash))
            .as("RLS: the default tenant must not see the other tenant's pipeline")
            .isNull();
        // ...and creating under the default tenant is a fresh 'created', not a skip.
        var r = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/i.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(mapper.readValue(r.body(), MAP_T).get("status")).isEqualTo("created");
    }

    // ── Test 9b: malformed batch elements → 400, never 500 ───────────────────

    @Test
    void malformedBatchElements_rejected400() throws Exception {
        String hash = "h9b-" + "0".repeat(27);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/j.pdf\",\"collection\":\"knowledge__t\"}");

        var badPage = post("/v1/pipeline/pages", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pages\":[{\"page_text\":\"no index\"}]}");
        assertThat(badPage.statusCode())
            .as("a page element missing page_index is a 400, not a repository NPE→500")
            .isEqualTo(400);

        var badChunk = post("/v1/pipeline/chunks", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"chunks\":[{\"chunk_index\":\"NaN\",\"chunk_text\":\"t\",\"chunk_id\":\"i\"}]}");
        assertThat(badChunk.statusCode()).isEqualTo(400);
    }

    // -- Test 9c: nexus-yvzhz -- NUL bytes in page_text are sanitized, not 500 --

    @Test
    void pages_nulBytesInPageText_sanitizedNotRejected() throws Exception {
        String hash = "h9c-" + "0".repeat(26);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/k.pdf\",\"collection\":\"knowledge__t\"}");

        // A broken PDF ToUnicode CMap can carry raw NUL bytes in the PyMuPDF
        // text layer (nexus-yvzhz); Postgres text cannot store 0x00 (SQLSTATE
        // 22021). page_text is display/storage text, not an identity source
        // (no chash derives from it), so sanitizing it is safe.
        var w = post("/v1/pipeline/pages", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pages\":["
            + "{\"page_index\":0,\"page_text\":\"before\\u0000after\",\"metadata_json\":\"{}\"}"
            + "]}");
        assertThat(w.statusCode())
            .as("a NUL byte in page_text must be sanitized, not a 500")
            .isEqualTo(200);
        assertThat(mapper.readValue(w.body(), MAP_T).get("written")).isEqualTo(1);

        var resp = get("/v1/pipeline/pages?content_hash=" + hash + "&start=0", TOKEN, TENANT);
        @SuppressWarnings("unchecked")
        var pages = (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("pages");
        assertThat(pages).hasSize(1);
        String storedText = (String) pages.get(0).get("page_text");
        assertThat(storedText)
            .as("the stored text must be NUL-free")
            .isEqualTo("beforeafter");
    }

    // -- Test 9d: nexus-yvzhz/nexus-dmrkm -- chunk_text is NOT silently
    // stripped (chash is caller identity over the exact bytes); a NUL byte
    // there is a typed 422 rejection (nexus-dmrkm's class-22 mapping), never
    // a silent mutation and never a bare 500. --------------------------------

    @Test
    void chunks_nulBytesInChunkText_typedRejection_notSilentlyStripped() throws Exception {
        String hash = "h9d-" + "0".repeat(26);
        post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/l.pdf\",\"collection\":\"knowledge__t\"}");

        var w = post("/v1/pipeline/chunks", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"chunks\":["
            + "{\"chunk_index\":0,\"chunk_text\":\"before\\u0000after\",\"chunk_id\":\"id0\","
            + "\"metadata_json\":\"{}\",\"embedding\":null}"
            + "]}");
        assertThat(w.statusCode())
            .as("chunk_text is caller-computed chash identity -- a NUL byte must be a typed "
                + "rejection (nexus-dmrkm), never a silent strip and never a bare 500")
            .isEqualTo(422);
        assertThat(w.body()).contains("\"sqlstate\":\"22021\"");

        // The row must not exist half-written or silently mutated.
        var resp = get("/v1/pipeline/chunks?content_hash=" + hash, TOKEN, TENANT);
        @SuppressWarnings("unchecked")
        var chunks = (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("chunks");
        assertThat(chunks).as("the rejected chunk must not have landed a row").isEmpty();
    }

    // ── Test 10: auth — 401 without bearer ───────────────────────────────────

    @Test
    void noAuth_rejected401() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/pipeline/list"))
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private Map<String, Object> pipelineState(String hash) throws Exception {
        var resp = get("/v1/pipeline/state?content_hash=" + hash, TOKEN, TENANT);
        assertThat(resp.statusCode()).isEqualTo(200);
        return (Map<String, Object>) mapper.readValue(resp.body(), MAP_T).get("pipeline");
    }

    private HttpResponse<String> post(String path, String token, String tenant, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path, String token, String tenant) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
