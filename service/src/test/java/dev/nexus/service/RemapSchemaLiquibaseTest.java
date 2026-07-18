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
 * RDR-186 bead nexus-146xx.3 — Liquibase chash_remap baseline integration test.
 *
 * <p>The PG twin of the client-side {@code chash_remap.db} migration artifact
 * (RDR-185 .16 lineage): the persisted old-id → new-chash map, mirrored 1:1
 * from the SQLite schema in {@code src/nexus/migration/wire_reid.py} with
 * tenant_id promoted from a DEFAULT-'' column to a first-class RLS
 * discriminator.
 *
 * <p><strong>RF-186-1 invariant (load-bearing):</strong> this table is a
 * RAW-FACT substrate ONLY. It must never grow a "converged" / "delivered" /
 * verdict column — a stored verdict consulted by rung detect() instead of
 * re-deriving collides with the Gap-4 two-mechanism pin
 * ({@code tests/upgrade/test_gap4_two_mechanisms.py}) regardless of substrate.
 * The exact-column-set assertion in test 1 is the structural tripwire: any
 * added column fails it and forces the reader back to this paragraph.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires
 * Docker. Applies the Liquibase master changelog and asserts:
 * <ol>
 *   <li>chash_remap exists with EXACTLY the mirrored + tenant columns</li>
 *   <li>RLS: relrowsecurity=t, relforcerowsecurity=t; policy USING + WITH CHECK
 *       on the nexus.tenant GUC</li>
 *   <li>PK is (tenant_id, source_collection, old_id) — the SQLite natural key
 *       plus tenant</li>
 *   <li>CHECK rejects a new_chash whose length is not 32</li>
 *   <li>reverse index (tenant_id, new_chash) exists</li>
 *   <li>tenant isolation end-to-end via TenantScope</li>
 *   <li>BYPASSRLS (superuser) sees all tenants' rows — the integrity-count
 *       read path</li>
 *   <li>RLS fail-closed: unstamped service connection sees zero rows</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT rejected</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RemapSchemaLiquibaseTest {

    // RF-186-1: raw facts only. A verdict column added here MUST fail test 1.
    private static final Set<String> EXPECTED_COLUMNS = Set.of(
        "tenant_id", "source_collection", "old_id",
        "new_chash", "target_collection", "created_at", "provenance"
    );

    private static final String SVC_ROLE = "svc_remap_schema_test";
    private static final String SVC_PASS = "svc_remap_schema_test_pass";

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
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chash_remap TO " + SVC_ROLE);
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

    // ── Test 1: exact column set (the RF-186-1 structural tripwire) ──────────

    @Test
    void remapTable_hasExactColumnSet_noVerdictColumnEver() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getColumns(null, "nexus", "chash_remap", null);
            Set<String> actual = new java.util.HashSet<>();
            while (rs.next()) actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            assertThat(actual)
                .as("nexus.chash_remap must have EXACTLY the mirrored raw-fact columns. " +
                    "RF-186-1: a 'converged'/'delivered'/verdict column is banned — a stored " +
                    "verdict consulted by rung detect() collides with the Gap-4 pin " +
                    "(test_gap4_two_mechanisms.py) regardless of substrate. Do not extend " +
                    "this set; the map holds raw facts a live computation interprets.")
                .isEqualTo(EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: RLS flags and policy ─────────────────────────────────────────

    @Test
    void remapTable_rlsEnabledForcedAndPolicyOnTenantGuc() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet cls = su.createStatement().executeQuery(
                "SELECT relrowsecurity, relforcerowsecurity " +
                "FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'chash_remap'");
            assertThat(cls.next()).as("nexus.chash_remap must exist in pg_class").isTrue();
            assertThat(cls.getBoolean("relrowsecurity"))
                .as("RLS must be ENABLED").isTrue();
            assertThat(cls.getBoolean("relforcerowsecurity"))
                .as("RLS must be FORCED (owner is subject to policy too)").isTrue();

            ResultSet pol = su.createStatement().executeQuery(
                "SELECT policyname, qual, with_check FROM pg_policies " +
                "WHERE schemaname = 'nexus' AND tablename = 'chash_remap'");
            assertThat(pol.next()).as("a policy must exist on nexus.chash_remap").isTrue();
            String qual = pol.getString("qual");
            String withCheck = pol.getString("with_check");
            assertThat(qual)
                .as("USING predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(withCheck)
                .as("WITH CHECK predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(pol.next())
                .as("exactly one policy expected on nexus.chash_remap").isFalse();
        }
    }

    // ── Test 3: PK is (tenant_id, source_collection, old_id) ─────────────────

    @Test
    void remapTable_primaryKeyIsTenantSourceOldId() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getPrimaryKeys(null, "nexus", "chash_remap");
            String[] pk = new String[3];
            int count = 0;
            while (rs.next()) {
                int seq = rs.getInt("KEY_SEQ");
                pk[seq - 1] = rs.getString("COLUMN_NAME").toLowerCase();
                count++;
            }
            assertThat(count).as("PK must have exactly 3 columns").isEqualTo(3);
            assertThat(pk)
                .as("PK must be (tenant_id, source_collection, old_id) — the SQLite " +
                    "natural key (source_collection, old_id) plus the tenant discriminator")
                .containsExactly("tenant_id", "source_collection", "old_id");
        }
    }

    // ── Test 4: CHECK rejects wrong-length new_chash ─────────────────────────

    @Test
    void remapTable_checkRejectsWrongLengthChash() throws Exception {
        assertThatThrownBy(() -> {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                stampGuc(su, "check-tenant");
                su.createStatement().execute(
                    "INSERT INTO nexus.chash_remap " +
                    "(tenant_id, source_collection, old_id, new_chash, target_collection, " +
                    " created_at, provenance) " +
                    "VALUES ('check-tenant', 'src-coll', 'legacy-1', " +
                    // 31 hex chars — one short of the required 32
                    "'0123456789abcdef0123456789abcde', 'tgt-coll', now(), 'test')");
            }
        })
        .as("new_chash with length != 32 must be rejected by the CHECK constraint " +
            "(mirrors the SQLite CHECK(length(new_chash) = 32))")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("check constraint");
    }

    // ── Test 5: reverse index (tenant_id, new_chash) ─────────────────────────

    @Test
    void remapTable_reverseIndexExists() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT indexdef FROM pg_indexes " +
                "WHERE schemaname = 'nexus' AND tablename = 'chash_remap' " +
                "AND indexname = 'idx_chash_remap_new'");
            assertThat(rs.next())
                .as("reverse index idx_chash_remap_new must exist (mirrors SQLite; " +
                    "serves new_chash → old_id reverse lookups)")
                .isTrue();
            String def = rs.getString("indexdef");
            assertThat(def).contains("tenant_id", "new_chash");
        }
    }

    // ── Test 6: tenant isolation end-to-end via TenantScope ──────────────────

    @Test
    void tenantIsolation_viaTenantScope() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "alpha", "coll-a", "old-a1", "a".repeat(32));
            insertRow(su, "alpha", "coll-a", "old-a2", "b".repeat(32));
            insertRow(su, "beta",  "coll-b", "old-b1", "c".repeat(32));
            su.commit();
        }

        List<String> alphaIds = tenantScope.withTenant("alpha", ctx ->
            ctx.fetch("SELECT old_id FROM nexus.chash_remap ORDER BY old_id")
               .getValues("old_id", String.class));
        assertThat(alphaIds)
            .as("tenant-alpha must see exactly its 2 rows")
            .containsExactly("old-a1", "old-a2");

        List<String> betaIds = tenantScope.withTenant("beta", ctx ->
            ctx.fetch("SELECT old_id FROM nexus.chash_remap ORDER BY old_id")
               .getValues("old_id", String.class));
        assertThat(betaIds)
            .as("tenant-beta must see exactly its 1 row, none of alpha's")
            .containsExactly("old-b1");
    }

    // ── Test 7: BYPASSRLS (superuser) sees all tenants ───────────────────────

    @Test
    void bypassRls_superuserSeesAllTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "gamma-su", "coll-g", "old-g1", "d".repeat(32));
            insertRow(su, "delta-su", "coll-d", "old-d1", "e".repeat(32));
            su.commit();
        }

        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT COUNT(DISTINCT tenant_id) AS tenants FROM nexus.chash_remap " +
                "WHERE tenant_id IN ('gamma-su', 'delta-su')");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("tenants"))
                .as("superuser (rolsuper → implicit RLS bypass) must see rows across " +
                    "tenants — the integrity-count read path (nexus-vounk shape)")
                .isEqualTo(2L);
        }
    }

    // ── Test 8: RLS fail-closed — unstamped connection sees zero rows ────────

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "failclosed-tenant", "coll-fc", "old-fc1", "f".repeat(32));
            su.commit();
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.chash_remap");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("cnt"))
                .as("unstamped service connection must see zero rows (RLS fail-closed)")
                .isEqualTo(0L);
        }
    }

    // ── Test 9: WITH CHECK blocks cross-tenant INSERT ────────────────────────

    @Test
    void rls_withCheck_blocksCrossTenantInsert() throws Exception {
        assertThatThrownBy(() ->
            tenantScope.withTenant("epsilon", ctx ->
                ctx.execute(
                    "INSERT INTO nexus.chash_remap " +
                    "(tenant_id, source_collection, old_id, new_chash, target_collection, " +
                    " created_at, provenance) " +
                    "VALUES (?, ?, ?, ?, ?, now(), ?)",
                    "zeta",  // tenant_id mismatch — WITH CHECK must reject
                    "coll-x", "old-x1", "0".repeat(32), "tgt-x", "test"))
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

    private void stampGuc(Connection conn, String tenant) throws Exception {
        try (var ps = conn.prepareStatement("SELECT set_config(?, ?, false)")) {
            ps.setString(1, TenantConstants.GUC_NAME);
            ps.setString(2, tenant);
            ps.execute();
        }
    }

    /** Insert a map row via superuser connection (bypasses RLS for seeding). */
    private void insertRow(Connection su, String tenant, String sourceCollection,
                           String oldId, String newChash) throws Exception {
        try (var ps = su.prepareStatement("SELECT set_config(?, ?, true)")) {
            ps.setString(1, TenantConstants.GUC_NAME);
            ps.setString(2, tenant);
            ps.execute();
        }
        try (var ps = su.prepareStatement(
                "INSERT INTO nexus.chash_remap " +
                "(tenant_id, source_collection, old_id, new_chash, target_collection, " +
                " created_at, provenance) " +
                "VALUES (?, ?, ?, ?, ?, now(), 'test-seed') " +
                "ON CONFLICT (tenant_id, source_collection, old_id) DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, sourceCollection);
            ps.setString(3, oldId);
            ps.setString(4, newChash);
            ps.setString(5, "tgt-" + sourceCollection);
            ps.executeUpdate();
        }
    }
}
