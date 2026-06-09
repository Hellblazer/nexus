package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenHashing;
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
import java.sql.ResultSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.32.4 — session-token mint/close end-to-end through the real
 * {@link NexusService}. The load-bearing security assertion: a minted session token
 * authorizes T1 ops for ITS session and is DENIED (403) for a sibling session within the
 * same tenant (Decision 2 cross-session denial, server-enforced).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class SessionTokenHandlerTest {

    private static final String BOOT = "boot-session-admin";
    private static final String TENANT = "tenant-sess";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    EmbeddedPostgres pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = HttpClient.newHttpClient();

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES (?, ?, 'boot') "
                + "ON CONFLICT (token_hash) DO NOTHING")) {
                ps.setString(1, TokenHashing.sha256Hex(BOOT));
                ps.setString(2, TenantConstants.BOOTSTRAP_ANY_TENANT);
                ps.executeUpdate();
            }
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername("postgres");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        cfg.setConnectionInitSql("SET search_path TO nexus, t1, public");
        ds = new HikariDataSource(cfg);
        service = new NexusService(0, BOOT, ds);
        service.start();
        port = service.getPort();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.close();
    }

    @Test
    void start_mintsSessionToken_storedForTenantAndSession() throws Exception {
        JsonNode r = post("/v1/sessions/start", "{\"session_id\":\"sess-store\"}", null);
        assertThat(r.path("session_token").asText()).isNotBlank();
        assertThat(r.get("session_id").asText()).isEqualTo("sess-store");
        String hash = TokenHashing.sha256Hex(r.get("session_token").asText());
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT tenant_id, session_id FROM nexus.session_tokens WHERE session_token_hash = '" + hash + "'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("tenant_id")).isEqualTo(TENANT);
            assertThat(rs.getString("session_id")).isEqualTo("sess-store");
        }
    }

    @Test
    void mintedToken_authorizesOwnSession_deniesSiblingSession() throws Exception {
        String token = post("/v1/sessions/start", "{\"session_id\":\"sess-1\"}", null)
            .get("session_token").asText();
        // Authorizes its own session (minted token + matching body session_id).
        assertThat(scratchPut(token, "sess-1")).isEqualTo(200);
        assertThat(scratchGet(token, "sess-1")).as("own session allowed").isEqualTo(200);
        // DENIED for a sibling session within the same tenant: the minted token resolves
        // sess-1, the body claims sess-2 → 403 (server-enforced cross-session denial).
        assertThat(scratchGet(token, "sess-2")).as("sibling session denied").isEqualTo(403);
    }

    @Test
    void reMint_replacesPriorToken() throws Exception {
        String first = post("/v1/sessions/start", "{\"session_id\":\"sess-remint\"}", null)
            .get("session_token").asText();
        String second = post("/v1/sessions/start", "{\"session_id\":\"sess-remint\"}", null)
            .get("session_token").asText();
        assertThat(second).isNotEqualTo(first);
        // Exactly one live row for the session; the old token's hash is gone.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) c FROM nexus.session_tokens WHERE tenant_id='" + TENANT
                + "' AND session_id='sess-remint'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("c")).isEqualTo(1L);
            ResultSet old = su.createStatement().executeQuery(
                "SELECT 1 FROM nexus.session_tokens WHERE session_token_hash = '"
                + TokenHashing.sha256Hex(first) + "'");
            assertThat(old.next())
                .as("the prior session token row is gone (DB-level invalidation)").isFalse();
        }
        // The new token works (minted-enforced).
        assertThat(scratchGet(second, "sess-remint")).isEqualTo(200);
        // The old token is no longer minted-enforced: it degrades to the transitional
        // bootstrap path (no matching row → body session_id trusted), so it still returns
        // 200 rather than 401. Full revocation of the old token awaits Phase E's
        // require-minted flag (nexus-gmiaf.32.5); asserting 401 here would be a false test.
        assertThat(scratchGet(first, "sess-remint")).isEqualTo(200);
    }

    @Test
    void close_deletesSessionToken_idempotent() throws Exception {
        String token = post("/v1/sessions/start", "{\"session_id\":\"sess-close\"}", null)
            .get("session_token").asText();
        assertThat(post("/v1/sessions/close", "{\"session_id\":\"sess-close\"}", null)
            .get("closed").asInt()).isEqualTo(1);
        // Double close is a no-op (0), not an error.
        assertThat(post("/v1/sessions/close", "{\"session_id\":\"sess-close\"}", null)
            .get("closed").asInt()).isEqualTo(0);
        // The row is gone.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) c FROM nexus.session_tokens WHERE session_token_hash = '"
                + TokenHashing.sha256Hex(token) + "'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("c")).isEqualTo(0L);
        }
    }

    @Test
    void start_rejectsNonPositiveTtl() throws Exception {
        var r0 = http.send(req("/v1/sessions/start", "{\"session_id\":\"s\",\"ttl_seconds\":0}"),
            HttpResponse.BodyHandlers.ofString());
        assertThat(r0.statusCode()).isEqualTo(400);
        var rneg = http.send(req("/v1/sessions/start", "{\"session_id\":\"s\",\"ttl_seconds\":-1}"),
            HttpResponse.BodyHandlers.ofString());
        assertThat(rneg.statusCode()).isEqualTo(400);
    }

    @Test
    void start_requiresSessionId_andRejectsNonPost() throws Exception {
        var resp = http.send(req("/v1/sessions/start", "{}"), HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(400);
        var get = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/v1/sessions/start"))
            .header("Authorization", "Bearer " + BOOT).header("X-Nexus-Tenant", TENANT).GET().build();
        assertThat(http.send(get, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(405);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private JsonNode post(String path, String body, String ignored) throws Exception {
        var resp = http.send(req(path, body), HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as("POST %s -> %s", path, resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    private HttpRequest req(String path, String body) {
        return HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
            .header("Authorization", "Bearer " + BOOT)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
    }

    private int scratchPut(String sessionToken, String bodySession) throws Exception {
        String body = "{\"id\":\"" + java.util.UUID.randomUUID()
            + "\",\"session_id\":\"" + bodySession + "\",\"content\":\"x\"}";
        return scratchOp("/v1/t1/put", sessionToken, body);
    }

    private int scratchGet(String sessionToken, String bodySession) throws Exception {
        return scratchOp("/v1/t1/get", sessionToken,
            "{\"id\":\"any\",\"session_id\":\"" + bodySession + "\"}");
    }

    private int scratchOp(String path, String sessionToken, String body) throws Exception {
        var req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
            .header("Authorization", "Bearer " + BOOT)
            .header("X-Nexus-Tenant", TENANT)
            .header("X-Nexus-T1-Session", sessionToken)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode();
    }
}
