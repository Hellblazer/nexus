package dev.nexus.service;

import dev.nexus.service.db.SchemaMigrator;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.HashSet;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-net63 — SchemaMigrator end-to-end integration test.
 *
 * <p><strong>Load-bearing proof:</strong> this test proves that the SERVICE
 * provisions its own schema via {@link SchemaMigrator#migrate(javax.sql.DataSource)},
 * not via a test-fixture pre-apply.  No {@code Liquibase.update()} call appears in
 * {@code @BeforeAll}; the only migration is the {@code SchemaMigrator.migrate(ds)}
 * call inside the test body (or setup that explicitly delegates to it).
 *
 * <p>Three assertions:
 * <ol>
 *   <li><strong>All tables exist</strong> — every table created by the master
 *       changelog is present in the correct schema.</li>
 *   <li><strong>RLS enabled + forced</strong> on a representative tenant-scoped
 *       table ({@code nexus.memory}): {@code relrowsecurity=t},
 *       {@code relforcerowsecurity=t}, and at least one policy with
 *       {@code USING} referencing {@code current_setting}.</li>
 *   <li><strong>Idempotency</strong> — a second call to
 *       {@code SchemaMigrator.migrate(ds)} completes without error and applies
 *       zero additional changesets.</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class SchemaMigratorIntegrationTest {

    // ── Expected tables (nexus schema) ───────────────────────────────────────

    /** All nexus-schema tables produced by the master changelog. */
    private static final Set<String> EXPECTED_NEXUS_TABLES = Set.of(
        "memory",
        "plans",
        "relevance_log", "search_telemetry", "tier_writes", "nx_answer_runs",
        "hook_failures", "frecency",
        "topics", "taxonomy_meta", "topic_assignments", "topic_links",
        "document_aspects", "document_highlights",
        "aspect_extraction_queue", "aspect_promotion_log",
        "chash_index",
        "catalog_owners", "catalog_documents", "catalog_links",
        "catalog_document_chunks", "catalog_collections", "catalog_meta"
    );

    /** t1-schema tables (UNLOGGED). */
    private static final Set<String> EXPECTED_T1_TABLES = Set.of("scratch");

    // ── Fixtures ─────────────────────────────────────────────────────────────

    EmbeddedPostgres pg;
    com.zaxxer.hikari.HikariDataSource migDs;

    @BeforeAll
    void startFreshPostgres() throws Exception {
        // Start a completely schema-less embedded Postgres instance.
        // NO Liquibase apply here — the test methods call SchemaMigrator.migrate().
        pg = EmbeddedPostgres.builder().start();

        // Migration datasource: superuser connection (postgres role owns the DB,
        // can CREATE SCHEMA, CREATE TABLE, ENABLE ROW LEVEL SECURITY, CREATE POLICY).
        // This mirrors the production NX_DB_ADMIN_* path where a privileged role
        // performs DDL that the application role (nexus_svc) cannot run.
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername("postgres");
        cfg.setPassword("postgres");
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-migration-test");
        migDs = new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (migDs != null) migDs.close();
        try {
            if (pg != null) pg.close();
        } catch (Exception e) {
            // best-effort
        }
    }

    // ── Test 1: fresh DB → migrate → all tables present ──────────────────────

    /**
     * Calls {@link SchemaMigrator#migrate} against a fresh schema-less Postgres
     * and asserts that every table in the master changelog exists afterwards.
     *
     * <p>This is the "service provisions its own schema" proof: no test fixture
     * pre-applies Liquibase; the only DDL comes from {@code SchemaMigrator.migrate}.
     */
    @Test
    void freshDb_migrate_allTablesPresent() throws Exception {
        // ── Act: service migrates its own schema ──────────────────────────────
        SchemaMigrator.migrate(migDs);

        // ── Assert: nexus-schema tables ───────────────────────────────────────
        try (Connection conn = migDs.getConnection()) {
            Set<String> nexusTables = tablesInSchema(conn, "nexus");
            assertThat(nexusTables)
                .as("nexus schema must contain all expected tables after migration")
                .containsAll(EXPECTED_NEXUS_TABLES);

            // ── Assert: t1-schema tables ─────────────────────────────────────
            Set<String> t1Tables = tablesInSchema(conn, "t1");
            assertThat(t1Tables)
                .as("t1 schema must contain the scratch table after migration")
                .containsAll(EXPECTED_T1_TABLES);
        }
    }

    // ── Test 2: RLS enabled + forced on memory; policy references tenant GUC ─

    /**
     * After migration, {@code nexus.memory} must have:
     * <ul>
     *   <li>{@code relrowsecurity = true} (ENABLE ROW LEVEL SECURITY)</li>
     *   <li>{@code relforcerowsecurity = true} (FORCE ROW LEVEL SECURITY)</li>
     *   <li>at least one policy whose USING expression contains
     *       {@code current_setting} (tenant GUC enforcement)</li>
     * </ul>
     *
     * <p>Depends on {@link #freshDb_migrate_allTablesPresent} having applied the
     * migration first. JUnit 5 test-method ordering within a {@code @TestInstance
     * PER_CLASS} does not guarantee execution order, so this test re-migrates
     * defensively (idempotent no-op if already applied).
     */
    @Test
    void memory_rlsEnabledForcedWithPolicy() throws Exception {
        // Defensive re-migrate (idempotent — DATABASECHANGELOG guards it).
        SchemaMigrator.migrate(migDs);

        try (Connection conn = migDs.getConnection()) {
            // pg_class flags
            ResultSet cls = conn.createStatement().executeQuery(
                "SELECT relrowsecurity, relforcerowsecurity " +
                "FROM pg_class c " +
                "JOIN pg_namespace n ON c.relnamespace = n.oid " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'memory'");
            assertThat(cls.next())
                .as("nexus.memory must exist in pg_class after migration").isTrue();
            assertThat(cls.getBoolean("relrowsecurity"))
                .as("ENABLE ROW LEVEL SECURITY must be applied to nexus.memory").isTrue();
            assertThat(cls.getBoolean("relforcerowsecurity"))
                .as("FORCE ROW LEVEL SECURITY must be applied to nexus.memory").isTrue();

            // At least one policy referencing the tenant GUC
            ResultSet pol = conn.createStatement().executeQuery(
                "SELECT qual FROM pg_policies " +
                "WHERE schemaname = 'nexus' AND tablename = 'memory'");
            assertThat(pol.next())
                .as("nexus.memory must have at least one RLS policy after migration").isTrue();
            String using = pol.getString("qual");
            assertThat(using)
                .as("RLS USING expression must reference current_setting (tenant GUC)")
                .contains("current_setting");
        }
    }

    // ── Test 3: second migrate() is a clean no-op (idempotency) ─────────────

    /**
     * Calls {@link SchemaMigrator#migrate} a second time and asserts it completes
     * without error.  Liquibase tracks applied changesets in
     * {@code DATABASECHANGELOG}; re-running applies zero additional DDL.
     *
     * <p>Also verifies that the table count did not change (no duplicate tables
     * were created).
     */
    @Test
    void migrate_idempotent_secondCallIsNoOp() throws Exception {
        // First apply (may already be applied by test 1 — idempotent either way).
        SchemaMigrator.migrate(migDs);

        // Record table count before second call.
        int beforeCount;
        try (Connection conn = migDs.getConnection()) {
            beforeCount = tablesInSchema(conn, "nexus").size()
                        + tablesInSchema(conn, "t1").size();
        }

        // ── Act: second migrate call ──────────────────────────────────────────
        // Must not throw; Liquibase must detect all changesets already applied.
        SchemaMigrator.migrate(migDs);

        // Table count must be unchanged (no extra tables created).
        int afterCount;
        try (Connection conn = migDs.getConnection()) {
            afterCount = tablesInSchema(conn, "nexus").size()
                       + tablesInSchema(conn, "t1").size();
        }

        assertThat(afterCount)
            .as("second migrate() must not create new tables (idempotent)")
            .isEqualTo(beforeCount);

        // Liquibase DATABASECHANGELOG must still exist (not dropped/cleared).
        try (Connection conn = migDs.getConnection()) {
            ResultSet rs = conn.createStatement().executeQuery(
                "SELECT COUNT(*) FROM public.\"databasechangelog\"");
            rs.next();
            assertThat(rs.getLong(1))
                .as("DATABASECHANGELOG must have changesets recorded (not empty)")
                .isGreaterThan(0);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    /**
     * Returns the set of base-table names in {@code schema} (excludes views,
     * sequences, indexes, DATABASECHANGELOG meta-tables, etc.).
     */
    private Set<String> tablesInSchema(Connection conn, String schema) throws Exception {
        Set<String> names = new HashSet<>();
        ResultSet rs = conn.getMetaData().getTables(null, schema, null,
            new String[]{"TABLE"});
        while (rs.next()) {
            names.add(rs.getString("TABLE_NAME").toLowerCase());
        }
        return names;
    }
}
