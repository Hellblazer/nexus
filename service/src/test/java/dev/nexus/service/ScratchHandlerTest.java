package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.13 — ScratchHandler endpoint tests.
 *
 * <p>Proves every T1 scratch HTTP endpoint behaves correctly through the full stack:
 * HTTP → ScratchHandler → ScratchRepository → Postgres (t1.scratch).
 *
 * <p>Coverage:
 * <ol>
 *   <li>PUT: inserts entry, returns id</li>
 *   <li>GET: found and not-found paths</li>
 *   <li>SEARCH: FTS returns matching entry; session-scoped</li>
 *   <li>LIST: all entries for session</li>
 *   <li>FLAGGED: only flagged entries returned</li>
 *   <li>FLAG + UNFLAG: flag cycle via HTTP</li>
 *   <li>DELETE: removes entry, second delete returns false</li>
 *   <li>RESOLVE_PREFIX: resolves UUID prefix</li>
 *   <li>SESSION/CLOSE: deletes all session entries, idempotent</li>
 *   <li>SWEEP: TTL sweep endpoint returns swept count</li>
 *   <li>RLS isolation through HTTP: cross-tenant GET returns not-found</li>
 *   <li>Session isolation through HTTP: cross-session GET returns not-found</li>
 *   <li>Auth: 401 on missing/bad Bearer token</li>
 * </ol>
 *
 * <p>Hermetic: embedded Postgres (io.zonky), port 0, no Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ScratchHandlerTest {

    private static final String TOKEN = "scratch-handler-test-token-abc789";
    private static final String SVC_ROLE = "svc_scratch_handler_test";
    private static final String SVC_PASS = "svc_scratch_handler_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String OTHER_TENANT = "other-scratch-tenant";
    private static final String SESSION = "test-session-abc";
    private static final String OTHER_SESSION = "test-session-xyz";

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};
    private static final TypeReference<List<Map<String, Object>>> LIST_T = new TypeReference<>() {};

    EmbeddedPostgres pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = EmbeddedPostgres.builder().start();

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SCHEMA t1 TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON t1.scratch TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, t1, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
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
        if (svcDs  != null) svcDs.close();
        if (pg     != null) pg.close();
    }

    // ── Test 1: PUT ───────────────────────────────────────────────────────────

    @Test
    void put_insertsEntry_returnsId() throws Exception {
        String id = uuid();
        var resp = post("/v1/t1/put", TENANT,
            json("id", id, "session_id", SESSION, "content", "hello scratch world",
                 "tags", "a,b"));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("id")).isEqualTo(id);
    }

    // ── Test 2: GET — found and not-found ────────────────────────────────────

    @Test
    void get_found_returnsEntry() throws Exception {
        String id = uuid();
        post("/v1/t1/put", TENANT,
            json("id", id, "session_id", SESSION, "content", "get test content"));

        var resp = post("/v1/t1/get", TENANT,
            json("id", id, "session_id", SESSION));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("id")).isEqualTo(id);
        assertThat(body.get("content")).isEqualTo("get test content");
    }

    @Test
    void get_notFound_returnsFoundFalse() throws Exception {
        var resp = post("/v1/t1/get", TENANT,
            json("id", "nonexistent-id-9999", "session_id", SESSION));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("found")).isEqualTo(false);
    }

    // ── Test 3: SEARCH — FTS returns matching entry ──────────────────────────

    @Test
    void search_fts_returnsMatchingEntry() throws Exception {
        String id  = uuid();
        String searchSession = "search-session-" + System.nanoTime();
        String term = "uniquexyzqrst" + System.nanoTime();
        post("/v1/t1/put", TENANT,
            json("id", id, "session_id", searchSession, "content", term + " embedded in content"));

        var resp = post("/v1/t1/search", TENANT,
            json("query", term, "session_id", searchSession, "limit", 10));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) body.get("results");
        assertThat(results).as("FTS search must find the entry").isNotEmpty();
        assertThat(results.get(0).get("id")).isEqualTo(id);
    }

    @Test
    void search_sessionScoped_doesNotReturnOtherSession() throws Exception {
        String term = "crosssessionftstest" + System.nanoTime();
        String idA = uuid();
        String idB = uuid();
        post("/v1/t1/put", TENANT, json("id", idA, "session_id", SESSION, "content", term + " session A"));
        post("/v1/t1/put", TENANT, json("id", idB, "session_id", OTHER_SESSION, "content", term + " session B"));

        var resp = post("/v1/t1/search", TENANT, json("query", term, "session_id", SESSION, "limit", 10));
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("results");
        List<String> ids = results.stream().map(e -> (String) e.get("id")).toList();
        assertThat(ids).contains(idA);
        assertThat(ids).doesNotContain(idB);
    }

    // ── Test 4: LIST ──────────────────────────────────────────────────────────

    @Test
    void list_returnsSessionEntries() throws Exception {
        String listSession = "list-session-" + System.nanoTime();
        String id1 = uuid();
        String id2 = uuid();
        post("/v1/t1/put", TENANT, json("id", id1, "session_id", listSession, "content", "entry one"));
        post("/v1/t1/put", TENANT, json("id", id2, "session_id", listSession, "content", "entry two"));
        // Other session — must NOT appear
        post("/v1/t1/put", TENANT, json("id", uuid(), "session_id", OTHER_SESSION, "content", "other session"));

        var resp = post("/v1/t1/list", TENANT, json("session_id", listSession));
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> entries = (List<Map<String, Object>>)
            mapper.readValue(resp.body(), MAP_T).get("entries");
        List<String> ids = entries.stream().map(e -> (String) e.get("id")).toList();
        assertThat(ids).contains(id1, id2);
    }

    // ── Test 5: FLAGGED — only flagged entries ────────────────────────────────

    @Test
    void flagged_returnsOnlyFlaggedEntries() throws Exception {
        String flagSession = "flag-session-" + System.nanoTime();
        String flagId   = uuid();
        String unflagId = uuid();
        post("/v1/t1/put", TENANT, json("id", flagId,   "session_id", flagSession, "content", "will be flagged"));
        post("/v1/t1/put", TENANT, json("id", unflagId, "session_id", flagSession, "content", "not flagged"));

        // Flag only flagId
        post("/v1/t1/flag", TENANT, json("id", flagId, "session_id", flagSession,
            "flush_project", "target-proj", "flush_title", "target-title"));

        var resp = post("/v1/t1/flagged", TENANT, json("session_id", flagSession));
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> entries = (List<Map<String, Object>>)
            mapper.readValue(resp.body(), MAP_T).get("entries");
        List<String> flaggedIds = entries.stream().map(e -> (String) e.get("id")).toList();
        assertThat(flaggedIds).as("flagged list must include flagged entry").contains(flagId);
        assertThat(flaggedIds).as("flagged list must not include unflagged entry").doesNotContain(unflagId);
    }

    // ── Test 6: FLAG + UNFLAG via HTTP ────────────────────────────────────────

    @Test
    void flag_unflag_httpCycle() throws Exception {
        String id = uuid();
        post("/v1/t1/put", TENANT, json("id", id, "session_id", SESSION, "content", "flag cycle test"));

        var flagResp = post("/v1/t1/flag", TENANT, json("id", id, "session_id", SESSION,
            "flush_project", "p", "flush_title", "t"));
        assertThat(flagResp.statusCode()).isEqualTo(200);
        var flagBody = mapper.readValue(flagResp.body(), MAP_T);
        assertThat(flagBody.get("ok")).isEqualTo(true);

        var unflagResp = post("/v1/t1/unflag", TENANT, json("id", id, "session_id", SESSION));
        assertThat(unflagResp.statusCode()).isEqualTo(200);
        var unflagBody = mapper.readValue(unflagResp.body(), MAP_T);
        assertThat(unflagBody.get("ok")).isEqualTo(true);
    }

    @Test
    void flag_absentEntry_returnsOkFalse() throws Exception {
        var resp = post("/v1/t1/flag", TENANT, json("id", "no-such-id", "session_id", SESSION,
            "flush_project", "p", "flush_title", "t"));
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("ok")).isEqualTo(false);
    }

    // ── Test 7: DELETE ────────────────────────────────────────────────────────

    @Test
    void delete_removesEntry_secondReturnsFalse() throws Exception {
        String id = uuid();
        post("/v1/t1/put", TENANT, json("id", id, "session_id", SESSION, "content", "delete this"));

        var del1 = post("/v1/t1/delete", TENANT, json("id", id, "session_id", SESSION));
        assertThat(del1.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(del1.body(), MAP_T).get("deleted")).isEqualTo(true);

        var del2 = post("/v1/t1/delete", TENANT, json("id", id, "session_id", SESSION));
        assertThat(mapper.readValue(del2.body(), MAP_T).get("deleted")).isEqualTo(false);
    }

    @Test
    void delete_wrongSession_returnsFalse() throws Exception {
        String id = uuid();
        post("/v1/t1/put", TENANT, json("id", id, "session_id", SESSION, "content", "protected by session"));

        var resp = post("/v1/t1/delete", TENANT, json("id", id, "session_id", OTHER_SESSION));
        assertThat(mapper.readValue(resp.body(), MAP_T).get("deleted"))
            .as("cross-session delete must return false").isEqualTo(false);
    }

    // ── Test 8: RESOLVE_PREFIX ────────────────────────────────────────────────

    @Test
    void resolvePrefix_findsFullId() throws Exception {
        String id     = uuid();
        String prefix = id.substring(0, 8);
        post("/v1/t1/put", TENANT, json("id", id, "session_id", SESSION, "content", "prefix resolution test"));

        var resp = post("/v1/t1/resolve_prefix", TENANT, json("prefix", prefix, "session_id", SESSION));
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) mapper.readValue(resp.body(), MAP_T).get("ids");
        assertThat(ids).contains(id);
    }

    @Test
    void resolvePrefix_absent_returnsEmptyList() throws Exception {
        var resp = post("/v1/t1/resolve_prefix", TENANT,
            json("prefix", "00000000-ffff", "session_id", SESSION));
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) mapper.readValue(resp.body(), MAP_T).get("ids");
        assertThat(ids).isEmpty();
    }

    // ── Test 9: SESSION/CLOSE ─────────────────────────────────────────────────

    @Test
    void sessionClose_deletesAllEntries_returnsCount() throws Exception {
        String closeSession = "close-handler-" + System.nanoTime();
        post("/v1/t1/put", TENANT, json("id", uuid(), "session_id", closeSession, "content", "entry one"));
        post("/v1/t1/put", TENANT, json("id", uuid(), "session_id", closeSession, "content", "entry two"));

        var resp = post("/v1/t1/session/close", TENANT, json("session_id", closeSession));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        int deleted = ((Number) body.get("deleted")).intValue();
        assertThat(deleted).as("session/close must return count of deleted rows").isEqualTo(2);

        // Verify session is empty
        var listResp = post("/v1/t1/list", TENANT, json("session_id", closeSession));
        @SuppressWarnings("unchecked")
        List<?> entries = (List<?>) mapper.readValue(listResp.body(), MAP_T).get("entries");
        assertThat(entries).as("entries must be empty after session close").isEmpty();
    }

    @Test
    void sessionClose_idempotent_secondCallReturnsZero() throws Exception {
        String closeSession = "close-idem-" + System.nanoTime();
        post("/v1/t1/put", TENANT, json("id", uuid(), "session_id", closeSession, "content", "ephemeral"));

        post("/v1/t1/session/close", TENANT, json("session_id", closeSession));
        var resp = post("/v1/t1/session/close", TENANT, json("session_id", closeSession));
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(((Number) body.get("deleted")).intValue())
            .as("second close must return 0 (idempotent)").isEqualTo(0);
    }

    // ── Test 10: SWEEP endpoint ───────────────────────────────────────────────

    @Test
    void sweep_returnsSweptCount() throws Exception {
        var resp = post("/v1/t1/sweep", TENANT, "{}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKey("swept");
        assertThat(((Number) body.get("swept")).intValue()).isGreaterThanOrEqualTo(0);
    }

    // ── Test 11: RLS isolation through HTTP layer ─────────────────────────────

    @Test
    void rlsIsolation_crossTenantGetReturnsNotFound() throws Exception {
        String id = uuid();
        post("/v1/t1/put", TENANT, json("id", id, "session_id", SESSION, "content", "rls tenant secret"));

        // OTHER_TENANT tries to get the same id + session
        var resp = post("/v1/t1/get", OTHER_TENANT, json("id", id, "session_id", SESSION));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("found")).as("cross-tenant GET must return found=false (RLS)").isEqualTo(false);
    }

    // ── Test 12: session isolation through HTTP layer ─────────────────────────

    @Test
    void sessionIsolation_crossSessionGetReturnsNotFound() throws Exception {
        String id = uuid();
        post("/v1/t1/put", TENANT, json("id", id, "session_id", SESSION, "content", "session isolated"));

        // Same tenant but wrong session
        var resp = post("/v1/t1/get", TENANT, json("id", id, "session_id", OTHER_SESSION));
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("found"))
            .as("cross-session GET must return found=false (session_id filter)").isEqualTo(false);
    }

    // ── Test 13: auth — 401 on missing/bad token ──────────────────────────────

    @Test
    void auth_401OnMissingToken() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/t1/list"))
            .POST(HttpRequest.BodyPublishers.ofString("{\"session_id\":\"s\"}"))
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
    }

    @Test
    void auth_401OnBadToken() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/t1/list"))
            .POST(HttpRequest.BodyPublishers.ofString("{\"session_id\":\"s\"}"))
            .header("Authorization", "Bearer wrong-token-999")
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String path, String tenant, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static String uuid() {
        return UUID.randomUUID().toString();
    }

    /** Minimal flat JSON builder for 2-field and 4-field maps. */
    private static String json(Object... kvPairs) {
        if (kvPairs.length % 2 != 0) throw new IllegalArgumentException("must be k/v pairs");
        var sb = new StringBuilder("{");
        for (int i = 0; i < kvPairs.length; i += 2) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(kvPairs[i]).append("\":");
            Object v = kvPairs[i + 1];
            if (v instanceof String s)  sb.append("\"").append(s).append("\"");
            else if (v instanceof Number) sb.append(v);
            else if (v instanceof Boolean) sb.append(v);
            else sb.append("\"").append(v).append("\"");
        }
        sb.append("}");
        return sb.toString();
    }
}
