package dev.nexus.service;

import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
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

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-186 bead nexus-146xx.10 — Liquibase ladder_completions baseline test.
 *
 * <p>The PG home for upgrade-ladder rung completion bookkeeping (RDR-186 D3:
 * derive-first, record-late). Mirrors the client-side {@code ladder.db}
 * {@code rung_completions} schema ({@code src/nexus/upgrade_ladder/completion.py})
 * 1:1 with tenant_id added: one durable "verified" fact per rung. First-class
 * relation per the RDR-154 bias (Hal's Q5 relaxation permits a transitional KV
 * facility, but the relation is the same effort here so the bias stands).
 *
 * <p>Completion records are position BOOKKEEPING, not truth (RDR-142 / RF-186-2):
 * ladder position is DERIVED at read time from these rows; there is no stored
 * position and no setter, and audit metadata (verified_at / package_version)
 * is observability-only, accepted lossy across the transition.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires
 * Docker. Applies the Liquibase master changelog and asserts:
 * <ol>
 *   <li>ladder_completions exists with EXACTLY the mirrored + tenant columns</li>
 *   <li>RLS: relrowsecurity=t, relforcerowsecurity=t; policy USING + WITH CHECK
 *       on the nexus.tenant GUC</li>
 *   <li>PK is (tenant_id, rung_name) — the SQLite PK plus tenant</li>
 *   <li>detail defaults to '' (mirrors SQLite DEFAULT '')</li>
 *   <li>tenant isolation end-to-end via TenantScope</li>
 *   <li>BYPASSRLS (superuser) sees all tenants' rows</li>
 *   <li>RLS fail-closed: unstamped service connection sees zero rows</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT rejected</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LadderSchemaLiquibaseTest {

    private static final Set<String> EXPECTED_COLUMNS = Set.of(
        "tenant_id", "rung_name", "verified_at", "package_version", "detail"
    );

    private static final String SVC_ROLE = "svc_ladder_schema_test";
    private static final String SVC_PASS = "svc_ladder_schema_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
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
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.ladder_completions TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    // ── Test 1: exact column set ─────────────────────────────────────────────

    @Test
    void ladderTable_hasExactColumnSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getColumns(null, "nexus", "ladder_completions", null);
            Set<String> actual = new java.util.HashSet<>();
            while (rs.next()) actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            assertThat(actual)
                .as("nexus.ladder_completions must have exactly the mirrored + tenant " +
                    "columns. NO position column ever: ladder position is DERIVED " +
                    "(derive_ladder_position, completion.py) — a stored position is the " +
                    "RDR-142 bug class the Gap-4 pin makes unrepresentable.")
                .isEqualTo(EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: RLS flags and policy ─────────────────────────────────────────

    @Test
    void ladderTable_rlsEnabledForcedAndPolicyOnTenantGuc() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet cls = su.createStatement().executeQuery(
                "SELECT relrowsecurity, relforcerowsecurity " +
                "FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'ladder_completions'");
            assertThat(cls.next()).as("nexus.ladder_completions must exist in pg_class").isTrue();
            assertThat(cls.getBoolean("relrowsecurity"))
                .as("RLS must be ENABLED").isTrue();
            assertThat(cls.getBoolean("relforcerowsecurity"))
                .as("RLS must be FORCED (owner is subject to policy too)").isTrue();

            ResultSet pol = su.createStatement().executeQuery(
                "SELECT policyname, qual, with_check FROM pg_policies " +
                "WHERE schemaname = 'nexus' AND tablename = 'ladder_completions'");
            assertThat(pol.next()).as("a policy must exist on nexus.ladder_completions").isTrue();
            assertThat(pol.getString("qual"))
                .as("USING predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(pol.getString("with_check"))
                .as("WITH CHECK predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(pol.next())
                .as("exactly one policy expected on nexus.ladder_completions").isFalse();
        }
    }

    // ── Test 3: PK is (tenant_id, rung_name) ─────────────────────────────────

    @Test
    void ladderTable_primaryKeyIsTenantRungName() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getPrimaryKeys(null, "nexus", "ladder_completions");
            String[] pk = new String[2];
            int count = 0;
            while (rs.next()) {
                int seq = rs.getInt("KEY_SEQ");
                pk[seq - 1] = rs.getString("COLUMN_NAME").toLowerCase();
                count++;
            }
            assertThat(count).as("PK must have exactly 2 columns").isEqualTo(2);
            assertThat(pk)
                .as("PK must be (tenant_id, rung_name) — the SQLite PK (rung_name) " +
                    "plus the tenant discriminator; one verified fact per rung per tenant")
                .containsExactly("tenant_id", "rung_name");
        }
    }

    // ── Test 4: detail defaults to '' ────────────────────────────────────────

    @Test
    void ladderTable_detailDefaultsToEmptyString() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            try (var ps = su.prepareStatement("SELECT set_config(?, ?, true)")) {
                ps.setString(1, TenantConstants.GUC_NAME);
                ps.setString(2, "default-probe");
                ps.execute();
            }
            su.createStatement().execute(
                "INSERT INTO nexus.ladder_completions " +
                "(tenant_id, rung_name, verified_at, package_version) " +
                "VALUES ('default-probe', 'probe-rung', now(), '6.11.0')");
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT detail FROM nexus.ladder_completions " +
                "WHERE tenant_id = 'default-probe' AND rung_name = 'probe-rung'");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getString("detail"))
                .as("detail must default to '' (mirrors SQLite DEFAULT '')")
                .isEmpty();
            su.rollback();
        }
    }

    // ── Test 5: tenant isolation end-to-end via TenantScope ──────────────────

    @Test
    void tenantIsolation_viaTenantScope() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "alpha", "engine-install");
            insertRow(su, "alpha", "t2-schema");
            insertRow(su, "beta",  "engine-install");
            su.commit();
        }

        List<String> alphaRungs = tenantScope.withTenant("alpha", ctx ->
            ctx.fetch("SELECT rung_name FROM nexus.ladder_completions ORDER BY rung_name")
               .getValues("rung_name", String.class));
        assertThat(alphaRungs)
            .as("tenant-alpha must see exactly its 2 rung records")
            .containsExactly("engine-install", "t2-schema");

        List<String> betaRungs = tenantScope.withTenant("beta", ctx ->
            ctx.fetch("SELECT rung_name FROM nexus.ladder_completions ORDER BY rung_name")
               .getValues("rung_name", String.class));
        assertThat(betaRungs)
            .as("tenant-beta must see exactly its 1 rung record")
            .containsExactly("engine-install");
    }

    // ── Test 6: BYPASSRLS (superuser) sees all tenants ───────────────────────

    @Test
    void bypassRls_superuserSeesAllTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "gamma-su", "rung-g");
            insertRow(su, "delta-su", "rung-d");
            su.commit();
        }

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(DISTINCT tenant_id) AS tenants FROM nexus.ladder_completions " +
                "WHERE tenant_id IN ('gamma-su', 'delta-su')");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("tenants"))
                .as("superuser (rolsuper → implicit RLS bypass) must see rows across tenants")
                .isEqualTo(2L);
        }
    }

    // ── Test 7: RLS fail-closed — unstamped connection sees zero rows ────────

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "failclosed-tenant", "rung-fc");
            su.commit();
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.ladder_completions");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("cnt"))
                .as("unstamped service connection must see zero rows (RLS fail-closed)")
                .isEqualTo(0L);
        }
    }

    // ── Test 8: WITH CHECK blocks cross-tenant INSERT ────────────────────────

    @Test
    void rls_withCheck_blocksCrossTenantInsert() throws Exception {
        assertThatThrownBy(() ->
            tenantScope.withTenant("epsilon", ctx ->
                ctx.execute(
                    "INSERT INTO nexus.ladder_completions " +
                    "(tenant_id, rung_name, verified_at, package_version) " +
                    "VALUES (?, ?, now(), ?)",
                    "zeta",  // tenant_id mismatch — WITH CHECK must reject
                    "rung-x", "6.11.0"))
        )
        .as("INSERT with tenant_id != GUC value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    /** Insert a completion row via superuser connection (bypasses RLS for seeding). */
    private void insertRow(Connection su, String tenant, String rungName) throws Exception {
        try (var ps = su.prepareStatement("SELECT set_config(?, ?, true)")) {
            ps.setString(1, TenantConstants.GUC_NAME);
            ps.setString(2, tenant);
            ps.execute();
        }
        try (var ps = su.prepareStatement(
                "INSERT INTO nexus.ladder_completions " +
                "(tenant_id, rung_name, verified_at, package_version) " +
                "VALUES (?, ?, now(), 'test-seed') " +
                "ON CONFLICT (tenant_id, rung_name) DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, rungName);
            ps.executeUpdate();
        }
    }
}
