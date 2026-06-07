package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import javax.sql.DataSource;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * RDR-152 bead nexus-gmiaf.3 — skeleton integration tests.
 *
 * 5 required tests (all hermetic, port 0, embedded PG, no Docker):
 *   1. /health returns 200 and DB is up
 *   2. auth 401 matrix (no token, bad token, good token)
 *   3. missing X-Nexus-Tenant → 400
 *   4. GUC tenant isolation with RLS+FORCE + defensive rolsuper/rolbypassrls assertion
 *   5. rollback on exception
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ServiceIntegrationTest {

    static final String TOKEN = "test-token-secret-0123456789abcdef";

    EmbeddedPostgres pg;
    NexusService service;
    HttpClient http;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        // Start embedded postgres on a free port
        pg = EmbeddedPostgres.builder().start();

        DataSource ds = pg.getPostgresDatabase();

        // Bootstrap: create service role and RLS schema for test 4
        bootstrapTestSchema(ds);

        // Build the DataSource for service using the svc_user role
        // (service scope, subject to RLS)
        svcDs = buildSvcDataSource(pg);

        tenantScope = new TenantScope(svcDs);

        // Start service on port 0 (assigned at bind)
        service = new NexusService(0, TOKEN, svcDs);
        service.start();

        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) {
            service.stop();
        }
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.close();
        }
    }

    // ─── Test 1: /health → 200 + DB up ──────────────────────────────────────

    @Test
    void health_returns200() throws Exception {
        var resp = get("/health", null);
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(resp.body()).contains("\"status\":\"ok\"");
    }

    // ─── Test 2: auth 401 matrix ──────────────────────────────────────────────

    @Test
    void auth_matrix() throws Exception {
        // No Authorization header → 401
        var noToken = get("/v1/_whoami", null);
        assertThat(noToken.statusCode()).isEqualTo(401);

        // Wrong token → 401
        var badToken = get("/v1/_whoami", "Bearer wrong-token");
        assertThat(badToken.statusCode()).isEqualTo(401);

        // Good token but no tenant header → 400 (tested in test 3)
        // Good token + tenant → 200 (tested transitively via test 4, also here)
        var goodResp = getWithTenant("/v1/_whoami", TOKEN, "test-tenant");
        assertThat(goodResp.statusCode()).isEqualTo(200);
        assertThat(goodResp.body()).contains("test-tenant");
    }

    // ─── Test 3: missing X-Nexus-Tenant → 400 ───────────────────────────────

    @Test
    void missingTenant_returns400() throws Exception {
        var resp = get("/v1/_whoami", "Bearer " + TOKEN);
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    // ─── Test 4: GUC tenant isolation with RLS + defensive role assertion ────

    @Test
    void gucTenantIsolation_rlsEnforced() throws Exception {
        // C4 defensive assertion (S0.4 review requirement):
        // service user must NOT be superuser or BYPASSRLS
        tenantScope.withTenant("tenant-A", ctx -> {
            var row = ctx.fetchOne(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user");
            assertThat(row).isNotNull();
            assertThat(row.get("rolsuper", Boolean.class))
                .as("service user must not be superuser")
                .isFalse();
            assertThat(row.get("rolbypassrls", Boolean.class))
                .as("service user must not bypass RLS")
                .isFalse();
            return null;
        });

        // Seed data via superuser (bypasses RLS) — 3 rows for A, 1 for B
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            stampAndInsert(su, "tenant-A", "key-a1", "alpha one");
            stampAndInsert(su, "tenant-A", "key-a2", "alpha two");
            stampAndInsert(su, "tenant-A", "key-a3", "alpha three");
            stampAndInsert(su, "tenant-B", "key-b1", "beta one");
            su.commit();
        }

        // tenant-A sees exactly its 3 rows; must NOT see B's row
        List<String> aKeys = tenantScope.withTenant("tenant-A", ctx ->
            ctx.fetch("SELECT key FROM nexus_test.rls_probe ORDER BY key")
               .getValues("key", String.class));
        assertThat(aKeys).containsExactlyInAnyOrder("key-a1", "key-a2", "key-a3");
        assertThat(aKeys).doesNotContain("key-b1");  // cross-tenant negative

        // tenant-B sees exactly its 1 row; must NOT see any of A's rows
        List<String> bKeys = tenantScope.withTenant("tenant-B", ctx ->
            ctx.fetch("SELECT key FROM nexus_test.rls_probe ORDER BY key")
               .getValues("key", String.class));
        assertThat(bKeys).containsExactly("key-b1");
        assertThat(bKeys).doesNotContain("key-a1", "key-a2", "key-a3");  // cross-tenant negatives

        // Fail-closed: API design prevents unstamped DSLContext (compile-time)
        // Runtime proof: no-tenant path is impossible via public API
        // (TenantScope.withTenant is the ONLY factory — no getDSLContext() method)
    }

    // ─── Test 5: rollback on exception ───────────────────────────────────────

    @Test
    void rollbackOnException() throws Exception {
        // withTenant work that throws → txn rolled back, no row persisted
        assertThatCode(() ->
            tenantScope.withTenant("tenant-X", ctx -> {
                ctx.execute("INSERT INTO nexus_test.rls_probe (tenant_id, key, payload) VALUES (?, ?, ?)",
                    "tenant-X", "key-x1", "should be rolled back");
                throw new RuntimeException("deliberate failure");
            })
        ).hasMessageContaining("deliberate failure");

        // Verify via SUPERUSER (bypasses RLS) — an RLS-subject connection would
        // see empty due to RLS even if the row was committed; superuser proves rollback.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            var rs = su.createStatement().executeQuery(
                "SELECT key FROM nexus_test.rls_probe WHERE tenant_id = 'tenant-X'");
            assertThat(rs.next())
                .as("no tenant-X row should exist after rollback (verified via superuser, bypassing RLS)")
                .isFalse();
        }
    }

    // ─── Helpers ─────────────────────────────────────────────────────────────

    private HttpResponse<String> get(String path, String authHeader) throws Exception {
        var builder = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .GET();
        if (authHeader != null) {
            builder.header("Authorization", authHeader);
        }
        return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> getWithTenant(String path, String token, String tenant)
            throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    /**
     * Create schema + RLS-enforced table + plain service role (LOGIN, no superuser, no bypassrls).
     * Pattern mirrors S0.1 spike proof verbatim.
     */
    private void bootstrapTestSchema(DataSource superDs) throws SQLException {
        try (Connection conn = superDs.getConnection()) {
            conn.setAutoCommit(true);

            // Service role: plain LOGIN, subject to RLS
            conn.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='svc_test') THEN " +
                "    CREATE ROLE svc_test LOGIN PASSWORD 'svc_test_pass'; " +
                "  END IF; " +
                "END $$");

            // Schema + table
            conn.createStatement().execute("CREATE SCHEMA IF NOT EXISTS nexus_test");
            conn.createStatement().execute(
                "CREATE TABLE IF NOT EXISTS nexus_test.rls_probe (" +
                "  id BIGSERIAL PRIMARY KEY," +
                "  tenant_id TEXT NOT NULL," +
                "  key TEXT NOT NULL," +
                "  payload TEXT" +
                ")");

            // Enable RLS with FORCE (applies even to table owner)
            conn.createStatement().execute(
                "ALTER TABLE nexus_test.rls_probe ENABLE ROW LEVEL SECURITY");
            conn.createStatement().execute(
                "ALTER TABLE nexus_test.rls_probe FORCE ROW LEVEL SECURITY");

            // Policy: tenant sees only its own rows via GUC nexus.tenant
            // Use DROP+CREATE for idempotent re-runs within a test session
            conn.createStatement().execute(
                "DROP POLICY IF EXISTS tenant_isolation ON nexus_test.rls_probe");
            conn.createStatement().execute(
                "CREATE POLICY tenant_isolation ON nexus_test.rls_probe " +
                "  USING (tenant_id = current_setting('nexus.tenant', true)) " +
                "  WITH CHECK (tenant_id = current_setting('nexus.tenant', true))");

            // Grant to service role
            conn.createStatement().execute(
                "GRANT USAGE ON SCHEMA nexus_test TO svc_test");
            conn.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus_test.rls_probe TO svc_test");
            conn.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus_test.rls_probe_id_seq TO svc_test");
        }
    }

    /**
     * Build a DataSource connecting as svc_test (the RLS-subject role).
     * autoCommit=true matches the production pool default in Main.java;
     * TenantScope toggles to false per borrow.
     */
    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource(EmbeddedPostgres epg) {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + epg.getPort() + "/postgres");
        config.setUsername("svc_test");
        config.setPassword("svc_test_pass");
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);  // pool default; TenantScope toggles per borrow
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    private void stampAndInsert(Connection conn, String tenant, String key, String payload)
            throws SQLException {
        try (var ps = conn.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
            ps.setString(1, tenant);
            ps.execute();
        }
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        ctx.execute("INSERT INTO nexus_test.rls_probe (tenant_id, key, payload) VALUES (?, ?, ?)",
            tenant, key, payload);
    }
}
