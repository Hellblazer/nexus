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

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.ResultSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.32.3 — token lifecycle admin endpoints, end-to-end through the
 * real {@link NexusService} (so the admin handler and AuthFilter share the live TokenCache).
 *
 * <p>Hermetic: embedded Postgres, port 0, no Docker. A wildcard bootstrap token authenticates
 * the admin calls (mirrors provisioning riding the legacy credential).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TokenAdminHandlerTest {

    private static final String BOOT = "boot-admin-token";
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
        // Seed the wildcard bootstrap token used to authenticate the admin calls.
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

    // ── issue ────────────────────────────────────────────────────────────────

    @Test
    void issue_mintsBoundToken_storedAsHash() throws Exception {
        JsonNode r = postJson("/v1/service-tokens/issue", "{\"tenant\":\"tenant-a\",\"label\":\"ci\"}");
        assertThat(r.path("token").asText()).isNotBlank();
        String raw = r.get("token").asText();
        String hash = r.get("token_hash").asText();
        assertThat(hash).isEqualTo(TokenHashing.sha256Hex(raw));
        // The hash is stored bound to tenant-a.
        assertThat(tenantOf(hash)).isEqualTo("tenant-a");
        // The freshly issued token actually authenticates as tenant-a (bound; no tenant header).
        assertThat(whoami(raw, null)).isEqualTo(200);
    }

    @Test
    void issue_rejectsWildcardTenant() throws Exception {
        assertThat(status("/v1/service-tokens/issue", "{\"tenant\":\"*\"}")).isEqualTo(400);
    }

    // ── tenant create ──────────────────────────────────────────────────────────

    @Test
    void tenantCreate_mintsInitialToken_rejectsWildcardName() throws Exception {
        JsonNode r = postJson("/v1/tenants/create", "{\"name\":\"tenant-new\"}");
        assertThat(r.get("tenant").asText()).isEqualTo("tenant-new");
        assertThat(tenantOf(r.get("token_hash").asText())).isEqualTo("tenant-new");
        assertThat(status("/v1/tenants/create", "{\"name\":\"*\"}")).isEqualTo(400);
    }

    // ── rotate ───────────────────────────────────────────────────────────────

    @Test
    void rotate_overlapsOldAndNew() throws Exception {
        postJson("/v1/service-tokens/issue", "{\"tenant\":\"tenant-rot\"}");
        postJson("/v1/service-tokens/issue", "{\"tenant\":\"tenant-rot\"}");
        JsonNode r = postJson("/v1/service-tokens/rotate", "{\"tenant\":\"tenant-rot\",\"grace_seconds\":300}");
        assertThat(r.get("token").asText()).isNotBlank();

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            // All three rows are still live (revoked_at IS NULL) during the grace window.
            assertThat(countLive("tenant-rot", su)).isEqualTo(3L);
            // The two pre-existing rows now have a future expires_at; the new one has none.
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(*) c FROM nexus.service_tokens WHERE tenant_id='tenant-rot' "
                + "AND revoked_at IS NULL AND expires_at IS NOT NULL");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("c")).as("the two old tokens are now grace-expiring").isEqualTo(2L);
            ResultSet rs2 = su.createStatement().executeQuery(
                "SELECT COUNT(*) c FROM nexus.service_tokens WHERE tenant_id='tenant-rot' "
                + "AND revoked_at IS NULL AND expires_at IS NULL");
            assertThat(rs2.next()).isTrue();
            assertThat(rs2.getLong("c")).as("exactly one fresh non-expiring token").isEqualTo(1L);
        }
    }

    // ── revoke ───────────────────────────────────────────────────────────────

    @Test
    void revoke_setsRevoked_invalidatesCache_andUnknownIsFalse() throws Exception {
        JsonNode issued = postJson("/v1/service-tokens/issue", "{\"tenant\":\"tenant-rev\"}");
        String raw = issued.get("token").asText();
        String hash = issued.get("token_hash").asText();
        // It authenticates before revocation (warm the cache).
        assertThat(whoami(raw, null)).isEqualTo(200);

        JsonNode r = postJson("/v1/service-tokens/revoke", "{\"selector\":\"" + hash + "\"}");
        assertThat(r.get("revoked").asBoolean()).isTrue();
        // Immediately rejected (revoked_at set + cache invalidated) — no TTL wait.
        assertThat(whoami(raw, null)).isEqualTo(401);

        JsonNode unknown = postJson("/v1/service-tokens/revoke", "{\"selector\":\"deadbeef-nope\"}");
        assertThat(unknown.get("revoked").asBoolean()).isFalse();
    }

    // ── list ───────────────────────────────────────────────────────────────────

    @Test
    void list_returnsRows_neverPlaintext() throws Exception {
        postJson("/v1/service-tokens/issue", "{\"tenant\":\"tenant-list\",\"label\":\"L1\"}");
        JsonNode r = postJson("/v1/service-tokens/list", "{\"tenant\":\"tenant-list\"}");
        JsonNode tokens = r.get("tokens");
        assertThat(tokens.isArray()).isTrue();
        assertThat(tokens).isNotEmpty();
        for (JsonNode row : tokens) {
            assertThat(row.has("token")).as("list must NEVER leak the raw token").isFalse();
            assertThat(row.get("token_hash").asText()).isNotBlank();
            assertThat(row.get("status").asText()).isEqualTo("active");
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private JsonNode postJson(String path, String body) throws Exception {
        var resp = http.send(req(path, body), HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as("POST %s -> %s", path, resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }

    private int status(String path, String body) throws Exception {
        return http.send(req(path, body), HttpResponse.BodyHandlers.ofString()).statusCode();
    }

    private HttpRequest req(String path, String body) {
        return HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
            .header("Authorization", "Bearer " + BOOT)
            .header("X-Nexus-Tenant", "default")  // wildcard token requires a tenant header
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
    }

    private int whoami(String bearer, String tenantHeader) throws Exception {
        var b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/v1/_whoami"))
            .header("Authorization", "Bearer " + bearer).GET();
        if (tenantHeader != null) b.header("X-Nexus-Tenant", tenantHeader);
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString()).statusCode();
    }

    private String tenantOf(String hash) throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT tenant_id FROM nexus.service_tokens WHERE token_hash = '" + hash + "'");
            assertThat(rs.next()).isTrue();
            return rs.getString("tenant_id");
        }
    }

    private long countLive(String tenant, Connection su) throws Exception {
        ResultSet rs = su.createStatement().executeQuery(
            "SELECT COUNT(*) c FROM nexus.service_tokens WHERE tenant_id='" + tenant
            + "' AND revoked_at IS NULL");
        assertThat(rs.next()).isTrue();
        return rs.getLong("c");
    }
}
