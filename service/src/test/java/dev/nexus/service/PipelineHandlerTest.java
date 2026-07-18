package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Base64;
import java.util.List;
import java.util.Map;

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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.pdf_pipeline, "
                + "nexus.pdf_pages, nexus.pdf_chunks TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.service_tokens, nexus.session_tokens TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'test-bound') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(OTHER_TOKEN)
                + "', '" + OTHER_TENANT + "', 'test-bound-other') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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

    // ── Test 1: create → created / skip-when-running / resume-when-failed ────

    @Test
    void create_created_thenSkipWhileRunning_thenResumeAfterFail() throws Exception {
        String hash = "h1-" + "0".repeat(28);
        var r1 = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/a.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(mapper.readValue(r1.body(), MAP_T).get("status")).isEqualTo("created");

        var r2 = post("/v1/pipeline/create", TOKEN, TENANT,
            "{\"content_hash\":\"" + hash + "\",\"pdf_path\":\"/tmp/a.pdf\",\"collection\":\"knowledge__t\"}");
        assertThat(mapper.readValue(r2.body(), MAP_T).get("status"))
            .as("fresh heartbeat + running = skip")
            .isEqualTo("skip");

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
            su.createStatement().execute(
                "UPDATE nexus.pdf_pipeline SET updated_at = now() - interval '10 minutes' "
                + "WHERE content_hash = '" + hash + "'");
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
