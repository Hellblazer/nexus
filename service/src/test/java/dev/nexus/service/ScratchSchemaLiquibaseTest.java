package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.jooq.DSLContext;
import dev.nexus.service.db.ScratchRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-152 bead nexus-gmiaf.13 — Liquibase t1 scratch schema integration test.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires Docker. Applies the
 * Liquibase master changelog and asserts all structural and runtime properties
 * of the T1 scratch tier.
 *
 * <p>Coverage:
 * <ol>
 *   <li>t1.scratch UNLOGGED table exists with exact column set</li>
 *   <li>RLS: relrowsecurity=t, relforcerowsecurity=t; policy uses nexus.t1_tenant GUC</li>
 *   <li>fts_vector generated column + GIN index; tokenisation: content='english', tags='simple'</li>
 *   <li>End-to-end via TenantScope: tenant isolation + FTS search</li>
 *   <li>Service role defensive: rolsuper=false, rolbypassrls=false</li>
 *   <li>RLS fail-closed: unstamped connection sees zero rows</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT rejected</li>
 *   <li>UNLOGGED table: verify relpersistence='u' in pg_class</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ScratchSchemaLiquibaseTest {

    private static final Set<String> EXPECTED_COLUMNS = Set.of(
        "id", "tenant_id", "session_id", "content", "tags",
        "flagged", "flush_project", "flush_title", "agent",
        "access_count", "last_accessed", "ts", "fts_vector"
    );

    private static final String SVC_ROLE = "svc_scratch_schema_test";
    private static final String SVC_PASS = "svc_scratch_schema_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // t1 is a separate schema bootstrapServiceRole never touches (it covers
            // nexus/staging only) -- kept as explicit grants (nexus-cbo4a batch 1b).
            // No session search_path is set (Sam's directive, nexus-zrcj7): every
            // raw statement this class issues already qualifies t1.scratch by hand.
            su.createStatement().execute("GRANT USAGE ON SCHEMA t1 TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON t1.scratch TO " + SVC_ROLE);
        }

        svcDs = buildSvcDs();
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    // ── Test 1: exact column set ─────────────────────────────────────────────

    @Test
    void scratchTable_hasExactColumnSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getColumns(null, "t1", "scratch", null);
            Set<String> actual = new java.util.HashSet<>();
            while (rs.next()) actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            assertThat(actual)
                .as("t1.scratch must have exactly the expected columns")
                .isEqualTo(EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: UNLOGGED table ───────────────────────────────────────────────

    @Test
    void scratchTable_isUnlogged() throws Exception {
        try (Connection su = pg.createConnection("")) {
            String relpersistence = PgCatalogProbes.relPersistence(
                DSL.using(su, SQLDialect.POSTGRES), "t1", "scratch");
            assertThat(relpersistence).as("t1.scratch must exist in pg_class").isNotNull();
            assertThat(relpersistence)
                .as("t1.scratch must be UNLOGGED (relpersistence='u')")
                .isEqualTo("u");
        }
    }

    // ── Test 3: RLS enabled + forced + nexus.t1_tenant GUC ──────────────────

    @Test
    void scratchTable_rlsEnabledAndForced_usingT1TenantGuc() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity cls = PgCatalogProbes.rowSecurity(ctx, "t1", "scratch");
            assertThat(cls).isNotNull();
            assertThat(cls.enabled()).as("RLS must be enabled").isTrue();
            assertThat(cls.forced()).as("RLS must be forced").isTrue();

            java.util.List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "t1", "scratch");
            assertThat(policies).as("at least one RLS policy must exist on t1.scratch").isNotEmpty();
            PgCatalogProbes.Policy pol = policies.get(0);
            assertThat(pol.cmd()).as("policy must cover ALL commands").isEqualTo("ALL");
            assertThat(pol.qual())
                .as("USING must reference nexus.t1_tenant GUC (NOT nexus.tenant)")
                .contains("t1_tenant");
            assertThat(pol.withCheck())
                .as("WITH CHECK must reference nexus.t1_tenant GUC")
                .contains("t1_tenant");
        }
    }

    // ── Test 4: fts_vector generated column + GIN index + tokenisation ───────

    @Test
    void scratchTable_ftsColumnAndIndex_tokenisationCorrect() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Verify STORED generated column
            PgCatalogProbes.GeneratedColumn gen =
                PgCatalogProbes.generatedColumn(ctx, "t1", "scratch", "fts_vector");
            assertThat(gen).as("fts_vector must exist").isNotNull();
            assertThat(gen.colType()).isEqualTo("tsvector");
            assertThat(gen.attgenerated()).as("must be STORED").isEqualTo("s");

            // Verify GIN index
            assertThat(PgCatalogProbes.indexCountOnColumn(ctx, "t1", "scratch", "gin", "fts_vector"))
                .as("GIN index on fts_vector must exist").isPositive();

            // Verify generated expression uses 'english' and 'simple' configs
            String colExpr = PgCatalogProbes.columnExpression(ctx, "t1", "scratch", "fts_vector");
            assertThat(colExpr).isNotNull();
            assertThat(colExpr).as("must use english config").contains("english");
            assertThat(colExpr).as("must use simple config for tags").contains("simple");
        }

        // Tokenisation probe: insert via superuser, verify FTS behavior
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            PgContainerHelper.setTenant(su, TenantScope.T1_TENANT_GUC, "probe-tenant-scratch", true);
            su.createStatement().execute(
                "INSERT INTO t1.scratch " +
                "(id, tenant_id, session_id, content, tags, flagged, access_count, ts) " +
                "VALUES " +
                "('probe-id-1', 'probe-tenant-scratch', 'probe-session', " +
                " 'training neural networks gradient descent', 'running,ml', false, 0, now())");

            ResultSet probe = su.createStatement().executeQuery(
                "SELECT fts_vector @@ plainto_tsquery('english', 'network') AS en_match, " +
                "       fts_vector @@ plainto_tsquery('simple',  'running') AS si_exact, " +
                "       fts_vector @@ plainto_tsquery('simple',  'run')     AS si_nostem " +
                "FROM t1.scratch WHERE id = 'probe-id-1'");
            assertThat(probe.next()).isTrue();
            assertThat(probe.getBoolean("en_match"))
                .as("english must stem: 'network' matches 'networks' in content").isTrue();
            assertThat(probe.getBoolean("si_exact"))
                .as("simple must match exact tag 'running'").isTrue();
            assertThat(probe.getBoolean("si_nostem"))
                .as("simple must NOT match 'run' against 'running' (no stemming)").isFalse();
            su.rollback();
        }
    }

    // ── Test 5: RLS isolation via TenantScope ────────────────────────────────

    @Test
    void rls_tenantIsolation_viaWithTenant() throws Exception {
        // Seed rows for two tenants via superuser
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "t1-alice", "session-alice", "alice-entry-1", "content for alice", "alice");
            insertRow(su, "t1-bob",   "session-bob",   "bob-entry-1",   "content for bob",   "bob");
            su.commit();
        }

        // alice sees only her entry
        long aliceCount = tenantScope.withTenant("t1-alice", ScratchRepository.T1_TENANT_GUC, ctx ->
            (Long) ctx.fetchOne("SELECT COUNT(*) FROM t1.scratch WHERE session_id = 'session-alice'")
                      .getValue(0));
        assertThat(aliceCount).as("alice must see exactly 1 row").isEqualTo(1L);

        // alice cannot see bob's entry
        long crossCount = tenantScope.withTenant("t1-alice", ScratchRepository.T1_TENANT_GUC, ctx ->
            (Long) ctx.fetchOne("SELECT COUNT(*) FROM t1.scratch WHERE session_id = 'session-bob'")
                      .getValue(0));
        assertThat(crossCount).as("alice must not see bob's session entries").isEqualTo(0L);

        // bob sees only his entry
        long bobCount = tenantScope.withTenant("t1-bob", ScratchRepository.T1_TENANT_GUC, ctx ->
            (Long) ctx.fetchOne("SELECT COUNT(*) FROM t1.scratch WHERE session_id = 'session-bob'")
                      .getValue(0));
        assertThat(bobCount).as("bob must see exactly 1 row").isEqualTo(1L);
    }

    // ── Test 6: service role defensive ──────────────────────────────────────

    @Test
    void serviceRole_notSuperuserNotBypassRls() {
        tenantScope.withTenant("test-tenant", ScratchRepository.T1_TENANT_GUC, ctx -> {
            PgCatalogProbes.RoleFlags row = PgCatalogProbes.currentRoleFlags(ctx);
            assertThat(row).isNotNull();
            assertThat(row.superuser())
                .as("service role must NOT be superuser").isFalse();
            assertThat(row.bypassRls())
                .as("service role must NOT have BYPASSRLS").isFalse();
            return null;
        });
    }

    // ── Test 7: RLS fail-closed — no GUC stamp → zero rows ──────────────────

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "fc-tenant", "fc-session", "fc-id", "fail-closed probe", "probe");
            su.commit();
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            // No GUC stamp — current_setting('nexus.t1_tenant', true) returns NULL
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM t1.scratch");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("cnt"))
                .as("unstamped connection must see zero rows (RLS fail-closed)")
                .isEqualTo(0L);
        }
    }

    // ── Test 8: RLS WITH CHECK blocks cross-tenant INSERT ───────────────────

    @Test
    void rls_withCheck_blocksCrossTenantInsert() {
        assertThatThrownBy(() ->
            tenantScope.withTenant("tenant-gamma", ScratchRepository.T1_TENANT_GUC, ctx ->
                ctx.execute(
                    "INSERT INTO t1.scratch " +
                    "(id, tenant_id, session_id, content, flagged, access_count, ts) " +
                    "VALUES (?, ?, ?, ?, false, 0, now())",
                    "cross-id", "tenant-delta",  // tenant_id mismatch!
                    "session-gamma", "should be blocked")
            )
        )
        .as("INSERT with mismatched tenant_id must be rejected by WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDs() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    private void insertRow(Connection su, String tenant, String sessionId,
                           String id, String content, String tags) throws Exception {
        PgContainerHelper.setTenant(su, TenantScope.T1_TENANT_GUC, tenant, true);
        try (var ps = su.prepareStatement(
                "INSERT INTO t1.scratch (id, tenant_id, session_id, content, tags, flagged, access_count, ts) " +
                "VALUES (?, ?, ?, ?, ?, false, 0, now()) ON CONFLICT (id) DO NOTHING")) {
            ps.setString(1, id);
            ps.setString(2, tenant);
            ps.setString(3, sessionId);
            ps.setString(4, content);
            ps.setString(5, tags);
            ps.executeUpdate();
        }
    }
}
