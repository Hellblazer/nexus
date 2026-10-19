package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
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
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * RDR-152 bead nexus-gmiaf.3 — skeleton integration tests.
 *
 * 5 required tests (all hermetic, port 0, embedded PG, requires Docker):
 *   1. /health returns 200 and DB is up
 *   2. auth 401 matrix (no token, bad token, good token)
 *   3. missing X-Nexus-Tenant → 400
 *   4. GUC tenant isolation with RLS+FORCE + defensive rolsuper/rolbypassrls assertion
 *   5. rollback on exception
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ServiceIntegrationTest {

    static final String TOKEN = "test-token-secret-0123456789abcdef";

    // nexus_test.rls_probe is a harness-only table (db.changelog-test-rls-probe-harness.xml)
    // with no generated jOOQ class, so it is referenced by qualified name.
    private static final Table<?> RLS_PROBE = DSL.table(DSL.name("nexus_test", "rls_probe"));
    private static final Field<String> PROBE_TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> PROBE_KEY = DSL.field(DSL.name("key"), String.class);
    private static final Field<String> PROBE_PAYLOAD = DSL.field(DSL.name("payload"), String.class);

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        // Start embedded postgres on a free port.
        // nexus-yhmav opt-out: this "skeleton" harness deliberately does NOT run the
        // master changelog (see bootstrapTestSchema below) -- a shared, already-fully-
        // migrated-to-HEAD cluster would silently contradict that hermeticity premise.
        pg = PgContainerHelper.startDedicated();

        // Bootstrap: create service role and RLS schema for test 4. The superuser pool is
        // transient (setup only); close it so it does not leak (review nexus-22man).
        try (com.zaxxer.hikari.HikariDataSource ds = PgContainerHelper.superuserDataSource(pg)) {
            bootstrapTestSchema(ds);
        }

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
            pg.stop();
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
    void version_reportsAppVersion_andExplicitSchemaError_withoutPublicJournal() throws Exception {
        // nexus-pebfx.4: this harness never runs the product master changelog and keeps its
        // own Liquibase journal in nexus_test, so there is no public.databasechangelog and
        // /version must report app_version AND an explicit schema_error — never silently
        // omit the schema fields.
        var resp = get("/version", null);
        org.assertj.core.api.Assertions.assertThat(resp.statusCode()).isEqualTo(200);
        org.assertj.core.api.Assertions.assertThat(resp.body())
            .contains("\"app_version\"")
            .contains("\"schema_error\"")
            // The journal-less premise itself: the error is the missing public.databasechangelog,
            // not a permission error on a journal that exists (installRlsProbeHarness keeps its own
            // Liquibase journal in nexus_test for exactly this).
            .contains("databasechangelog")
            .contains("does not exist");
    }

    @Test
    void auth_matrix() throws Exception {
        // No Authorization header → 401
        var noToken = get("/v1/_whoami", null);
        assertThat(noToken.statusCode()).isEqualTo(401);

        // Wrong token → 401
        var badToken = get("/v1/_whoami", "Bearer wrong-token");
        assertThat(badToken.statusCode()).isEqualTo(401);

        // Good bound token → 200; the token's tenant is authoritative (tested in test 3
        // that the X-Nexus-Tenant header is not required and a mismatch is ignored).
        var goodResp = getWithTenant("/v1/_whoami", TOKEN, "test-tenant");
        assertThat(goodResp.statusCode()).isEqualTo(200);
        assertThat(goodResp.body()).contains("test-tenant");
    }

    // ─── Test 3: bound token resolves tenant server-side (header not required) ──

    @Test
    void boundToken_resolvesTenant_withoutTenantHeader() throws Exception {
        // Phase E (nexus-gmiaf.32.5): the wildcard any-tenant grant is retired. A bound
        // token resolves its tenant SERVER-SIDE, so no X-Nexus-Tenant header is required
        // (and a mismatched one is ignored, not honored).
        var noHeader = get("/v1/_whoami", "Bearer " + TOKEN);
        assertThat(noHeader.statusCode()).isEqualTo(200);
        assertThat(noHeader.body()).contains("test-tenant");

        var mismatch = getWithTenant("/v1/_whoami", TOKEN, "some-other-tenant");
        assertThat(mismatch.statusCode()).isEqualTo(200);
        assertThat(mismatch.body()).contains("test-tenant");  // header ignored
    }

    // ─── Test 4: GUC tenant isolation with RLS + defensive role assertion ────

    @Test
    void gucTenantIsolation_rlsEnforced() throws Exception {
        // C4 defensive assertion (S0.4 review requirement):
        // service user must NOT be superuser or BYPASSRLS
        tenantScope.withTenant("tenant-A", ctx -> {
            PgCatalogProbes.RoleFlags row = PgCatalogProbes.currentRoleFlags(ctx);
            assertThat(row).isNotNull();
            assertThat(row.superuser())
                .as("service user must not be superuser")
                .isFalse();
            assertThat(row.bypassRls())
                .as("service user must not bypass RLS")
                .isFalse();
            return null;
        });

        // Seed data via superuser (bypasses RLS) — 3 rows for A, 1 for B
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            stampAndInsert(su, "tenant-A", "key-a1", "alpha one");
            stampAndInsert(su, "tenant-A", "key-a2", "alpha two");
            stampAndInsert(su, "tenant-A", "key-a3", "alpha three");
            stampAndInsert(su, "tenant-B", "key-b1", "beta one");
            su.commit();
        }

        // tenant-A sees exactly its 3 rows; must NOT see B's row
        List<String> aKeys = tenantScope.withTenant("tenant-A", ctx ->
            ctx.select(PROBE_KEY).from(RLS_PROBE).orderBy(PROBE_KEY).fetch(PROBE_KEY));
        assertThat(aKeys).containsExactlyInAnyOrder("key-a1", "key-a2", "key-a3");
        assertThat(aKeys).doesNotContain("key-b1");  // cross-tenant negative

        // tenant-B sees exactly its 1 row; must NOT see any of A's rows
        List<String> bKeys = tenantScope.withTenant("tenant-B", ctx ->
            ctx.select(PROBE_KEY).from(RLS_PROBE).orderBy(PROBE_KEY).fetch(PROBE_KEY));
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
                ctx.insertInto(RLS_PROBE, PROBE_TENANT_ID, PROBE_KEY, PROBE_PAYLOAD)
                    .values("tenant-X", "key-x1", "should be rolled back")
                    .execute();
                throw new RuntimeException("deliberate failure");
            })
        ).hasMessageContaining("deliberate failure");

        // Verify via SUPERUSER (bypasses RLS) — an RLS-subject connection would
        // see empty due to RLS even if the row was committed; superuser proves rollback.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThat(DSL.using(su, SQLDialect.POSTGRES)
                    .fetchExists(RLS_PROBE, PROBE_TENANT_ID.eq("tenant-X")))
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
     * Create the RLS-enforced probe table, the plain service role and the minimal token table
     * (db.changelog-test-rls-probe-harness.xml). Pattern mirrors the S0.1 spike proof.
     */
    private void bootstrapTestSchema(DataSource superDs) throws Exception {
        try (Connection conn = superDs.getConnection()) {
            PgContainerHelper.installRlsProbeHarness(
                conn, dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN));
        }
    }

    /**
     * Build a DataSource connecting as svc_test (the RLS-subject role).
     * autoCommit=true matches the production pool default in Main.java;
     * TenantScope toggles to false per borrow.
     */
    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource(PostgreSQLContainer<?> epg) {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(epg.getJdbcUrl());
        config.setUsername("svc_test");
        config.setPassword("svc_test_pass");
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);  // pool default; TenantScope toggles per borrow
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    private void stampAndInsert(Connection conn, String tenant, String key, String payload) {
        PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, true);
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        ctx.insertInto(RLS_PROBE, PROBE_TENANT_ID, PROBE_KEY, PROBE_PAYLOAD)
            .values(tenant, key, payload)
            .execute();
    }
}
