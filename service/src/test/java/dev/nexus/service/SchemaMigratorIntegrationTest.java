package dev.nexus.service;

import dev.nexus.service.db.SchemaMigrator;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import org.junit.jupiter.api.*;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.HashSet;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-net63 — SchemaMigrator end-to-end integration test.
 *
 * <p><strong>Load-bearing proof (Critical-2 fix):</strong> this test migrates as
 * {@code nexus_admin}, a NON-SUPERUSER schema-owner role, proving that the full
 * changelog runs without any superuser-only DDL.  The previous version migrated as
 * the embedded {@code postgres} superuser, which validated nothing about the production
 * split.
 *
 * <p>Five assertions:
 * <ol>
 *   <li><strong>Non-superuser owner can migrate</strong> — {@code nexus_admin} (owns nexus+t1
 *       schemas, is NOT superuser) runs {@code SchemaMigrator.migrate()} to completion.</li>
 *   <li><strong>All tables exist</strong> — every table in the master changelog is present
 *       in the correct schema after migration.</li>
 *   <li><strong>RLS enabled + forced</strong> on {@code nexus.memory}: {@code
 *       relrowsecurity=t}, {@code relforcerowsecurity=t}, policy USING contains
 *       {@code current_setting}.</li>
 *   <li><strong>nexus_svc DML under RLS</strong> — connects as {@code nexus_svc}, sets
 *       {@code nexus.tenant} GUC, INSERTs and SELECTs on {@code nexus.memory}: proves
 *       the {@code runAlways} grants wired nexus_svc correctly.</li>
 *   <li><strong>RLS fail-closed</strong> — {@code nexus_svc} connection with NO GUC stamp
 *       returns zero rows (fail-closed: unset GUC → NULL → no tenant_id matches NULL).</li>
 * </ol>
 *
 * <p>Idempotency is covered inside test 1: a second {@code migrate()} call is a no-op.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class SchemaMigratorIntegrationTest {

    // ── Role names ────────────────────────────────────────────────────────────

    /** Non-superuser schema owner — the Phase-5 nexus_admin equivalent. */
    private static final String ADMIN_ROLE = "nexus_admin_test";
    private static final String ADMIN_PASS = "nexus_admin_test_pass";

    /** Application role — NOSUPERUSER NOBYPASSRLS. */
    private static final String SVC_ROLE = "nexus_svc";
    private static final String SVC_PASS = "nexus_svc_pass";

    // ── Expected tables ───────────────────────────────────────────────────────

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

    private static final Set<String> EXPECTED_T1_TABLES = Set.of("scratch");

    // ── Fixtures ─────────────────────────────────────────────────────────────

    EmbeddedPostgres pg;

    /** Migration pool — uses nexus_admin_test (non-superuser owner). */
    com.zaxxer.hikari.HikariDataSource adminDs;

    /** Service pool — uses nexus_svc (NOSUPERUSER NOBYPASSRLS). */
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void bootstrap() throws Exception {
        // Start a completely schema-less embedded Postgres.
        pg = EmbeddedPostgres.builder().start();

        // ── Phase A: provisioning (done by DBA / Phase-5 nx step, NOT by Liquibase) ──
        // Using the embedded postgres superuser to simulate the DBA bootstrap:
        //   1. Create nexus_admin_test: NON-superuser, will own nexus + t1 schemas.
        //   2. Create nexus_svc: NOSUPERUSER NOBYPASSRLS LOGIN.
        //   3. Create the schemas and transfer ownership to nexus_admin_test.
        //      (In real provisioning: CREATE DATABASE nexus; CREATE SCHEMA nexus
        //       AUTHORIZATION nexus_admin; Liquibase then runs as nexus_admin.)
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);

            // nexus_admin_test: NOT superuser, NOT createrole — plain schema owner.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + ADMIN_ROLE + "') THEN " +
                "    CREATE ROLE " + ADMIN_ROLE + " LOGIN PASSWORD '" + ADMIN_PASS + "' NOSUPERUSER NOCREATEDB NOCREATEROLE; " +
                "  END IF; " +
                "END $$");

            // nexus_svc: NOSUPERUSER NOBYPASSRLS — the production application role.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");

            // Grant nexus_admin_test CREATE privilege on the database so it can CREATE SCHEMA
            // (models production: nexus_admin holds CONNECT + CREATE on the nexus database,
            // not superuser).  CREATE ON DATABASE is NOT superuser — it is a normal privilege
            // that schema-owner roles must hold.
            su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + ADMIN_ROLE);

            // Allow nexus_admin_test to write Liquibase's DATABASECHANGELOG to public.
            su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + ADMIN_ROLE);
        }

        // ── Phase B: build connection pools ─────────────────────────────────────

        // Migration pool: nexus_admin_test (non-superuser owner).
        var adminCfg = new com.zaxxer.hikari.HikariConfig();
        adminCfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        adminCfg.setPoolName("nexus-admin-test");
        adminDs = new com.zaxxer.hikari.HikariDataSource(adminCfg);

        // Service pool: nexus_svc (NOSUPERUSER NOBYPASSRLS) with search_path via initSql.
        var svcCfg = new com.zaxxer.hikari.HikariConfig();
        svcCfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        svcCfg.setUsername(SVC_ROLE);
        svcCfg.setPassword(SVC_PASS);
        svcCfg.setMaximumPoolSize(3);
        svcCfg.setConnectionInitSql("SET search_path TO nexus, t1, public");
        svcCfg.setPoolName("nexus-svc-test");
        svcDs = new com.zaxxer.hikari.HikariDataSource(svcCfg);
    }

    @AfterAll
    void stopAll() {
        if (adminDs != null) adminDs.close();
        if (svcDs   != null) svcDs.close();
        try {
            if (pg != null) pg.close();
        } catch (Exception ignored) { }
    }

    // ── Test 1: non-superuser owner migrates + all tables present + idempotent ─

    /**
     * Runs {@link SchemaMigrator#migrate} as {@code nexus_admin_test} (NOT superuser)
     * and asserts:
     * (a) migration completes without error — proves no superuser-only DDL in the changelog,
     * (b) all expected tables exist in nexus and t1 schemas,
     * (c) a second {@code migrate()} call is a clean no-op (idempotent).
     *
     * <p>This is the Critical-2 fix: prior version migrated as postgres superuser,
     * validating nothing about the non-superuser-owner production path.
     */
    @Test
    @Order(1)
    void nonSuperuserOwner_migrate_allTablesPresent_andIdempotent() throws Exception {
        // ── Act: non-superuser owner runs migration ───────────────────────────
        SchemaMigrator.migrate(adminDs);

        // ── Assert: all expected tables in nexus schema ───────────────────────
        try (Connection conn = adminDs.getConnection()) {
            Set<String> nexusTables = tablesInSchema(conn, "nexus");
            assertThat(nexusTables)
                .as("nexus schema must contain all expected tables after non-superuser migration")
                .containsAll(EXPECTED_NEXUS_TABLES);

            Set<String> t1Tables = tablesInSchema(conn, "t1");
            assertThat(t1Tables)
                .as("t1 schema must contain the scratch table after non-superuser migration")
                .containsAll(EXPECTED_T1_TABLES);
        }

        // ── Assert: idempotency — second migrate() is a no-op ────────────────
        int beforeCount;
        try (Connection conn = adminDs.getConnection()) {
            beforeCount = tablesInSchema(conn, "nexus").size()
                        + tablesInSchema(conn, "t1").size();
        }

        SchemaMigrator.migrate(adminDs);  // second call — must not throw

        int afterCount;
        try (Connection conn = adminDs.getConnection()) {
            afterCount = tablesInSchema(conn, "nexus").size()
                       + tablesInSchema(conn, "t1").size();
        }
        assertThat(afterCount)
            .as("second migrate() must not create new tables (idempotent)")
            .isEqualTo(beforeCount);

        // DATABASECHANGELOG must have records (not wiped).
        try (Connection conn = adminDs.getConnection()) {
            ResultSet rs = conn.createStatement().executeQuery(
                "SELECT COUNT(*) FROM public.\"databasechangelog\"");
            rs.next();
            assertThat(rs.getLong(1))
                .as("DATABASECHANGELOG must be non-empty after migration")
                .isGreaterThan(0);
        }
    }

    // ── Test 2: RLS enabled + forced + policy references tenant GUC ──────────

    /**
     * After migration, {@code nexus.memory} must have:
     * {@code relrowsecurity=t}, {@code relforcerowsecurity=t}, and a policy whose
     * USING expression is not null and contains {@code current_setting}.
     */
    @Test
    @Order(2)
    void memory_rlsEnabledForcedWithPolicy() throws Exception {
        // Defensive re-migrate (idempotent — DATABASECHANGELOG guards it).
        SchemaMigrator.migrate(adminDs);

        try (Connection conn = adminDs.getConnection()) {
            ResultSet cls = conn.createStatement().executeQuery(
                "SELECT relrowsecurity, relforcerowsecurity " +
                "FROM pg_class c " +
                "JOIN pg_namespace n ON c.relnamespace = n.oid " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'memory'");
            assertThat(cls.next())
                .as("nexus.memory must exist in pg_class after migration").isTrue();
            assertThat(cls.getBoolean("relrowsecurity"))
                .as("ENABLE ROW LEVEL SECURITY must be set on nexus.memory").isTrue();
            assertThat(cls.getBoolean("relforcerowsecurity"))
                .as("FORCE ROW LEVEL SECURITY must be set on nexus.memory").isTrue();

            ResultSet pol = conn.createStatement().executeQuery(
                "SELECT qual FROM pg_policies " +
                "WHERE schemaname = 'nexus' AND tablename = 'memory'");
            assertThat(pol.next())
                .as("nexus.memory must have at least one RLS policy after migration").isTrue();
            String using = pol.getString("qual");
            // Fix code-review M3: assert non-null BEFORE calling contains() to avoid NPE.
            assertThat(using)
                .as("RLS USING expression must not be null")
                .isNotNull();
            assertThat(using)
                .as("RLS USING expression must reference current_setting (tenant GUC)")
                .contains("current_setting");
        }
    }

    // ── Test 3: nexus_svc DML under RLS (runAlways grants wired) ─────────────

    /**
     * Connects as {@code nexus_svc} (NOSUPERUSER NOBYPASSRLS), stamps the
     * {@code nexus.tenant} GUC, then asserts that INSERT + SELECT on
     * {@code nexus.memory} succeed.
     *
     * <p>This proves the {@code runAlways} consolidated grant changeset
     * ({@code grants-nexus-svc.xml}) correctly wired DML rights for nexus_svc.
     * If grants are missing, the INSERT will raise "permission denied for table memory"
     * and this test fails immediately — not at service runtime under load.
     */
    @Test
    @Order(3)
    void nexusSvc_dmlUnderRls_succeeds() throws Exception {
        // Defensive: ensure migration has run.
        SchemaMigrator.migrate(adminDs);

        final String tenant = "net63-svc-test-tenant";
        final String project = "net63-proj";
        final String title = "SchemaMigrator DML proof";

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(false);

            // Stamp the tenant GUC (same pattern as TenantScope.withTenant).
            try (var ps = svc.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
                ps.setString(1, tenant);
                ps.execute();
            }

            // INSERT: proves nexus_svc has INSERT privilege on nexus.memory.
            try (var ps = svc.prepareStatement(
                    "INSERT INTO nexus.memory " +
                    "(tenant_id, project, title, content, tags, timestamp, access_count) " +
                    "VALUES (?, ?, ?, ?, ?, now(), 0) " +
                    "ON CONFLICT (tenant_id, project, title) DO NOTHING")) {
                ps.setString(1, tenant);
                ps.setString(2, project);
                ps.setString(3, title);
                ps.setString(4, "content body for DML proof");
                ps.setString(5, "test,migration");
                ps.executeUpdate();
            }

            // SELECT: proves nexus_svc has SELECT privilege AND RLS lets the row through.
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT title FROM nexus.memory WHERE project = '" + project + "'");
            assertThat(rs.next())
                .as("nexus_svc must be able to SELECT its own row via RLS (GUC stamped)")
                .isTrue();
            assertThat(rs.getString("title"))
                .as("selected row title must match inserted row")
                .isEqualTo(title);

            svc.rollback();  // cleanup
        }
    }

    // ── Test 4: RLS fail-closed — nexus_svc with no GUC sees zero rows ────────

    /**
     * Connects as {@code nexus_svc} WITHOUT stamping the {@code nexus.tenant} GUC.
     * {@code current_setting('nexus.tenant', true)} returns NULL; NULL != any
     * tenant_id so the USING predicate filters all rows → SELECT returns zero.
     *
     * <p>Seeds at least one row via the admin connection (bypasses RLS as owner).
     */
    @Test
    @Order(4)
    void nexusSvc_noGucStamp_rlsFailClosed_returnsZeroRows() throws Exception {
        // Seed a row via admin connection (bypasses RLS as schema owner).
        try (Connection admin = adminDs.getConnection()) {
            admin.setAutoCommit(false);
            // Owner must stamp GUC even for themselves when FORCE RLS is set.
            try (var ps = admin.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
                ps.setString(1, "failclosed-tenant");
                ps.execute();
            }
            try (var ps = admin.prepareStatement(
                    "INSERT INTO nexus.memory " +
                    "(tenant_id, project, title, content, tags, timestamp, access_count) " +
                    "VALUES (?, ?, ?, ?, ?, now(), 0) " +
                    "ON CONFLICT (tenant_id, project, title) DO NOTHING")) {
                ps.setString(1, "failclosed-tenant");
                ps.setString(2, "fc-proj");
                ps.setString(3, "Fail-closed sentinel row");
                ps.setString(4, "sentinel content");
                ps.setString(5, "sentinel");
                ps.executeUpdate();
            }
            admin.commit();
        }

        // Connect as nexus_svc WITHOUT stamping GUC → RLS must block all rows.
        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            // Deliberately do NOT stamp nexus.tenant GUC.
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.memory");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getLong("cnt"))
                .as("nexus_svc with no GUC stamp must see zero rows (RLS fail-closed)")
                .isEqualTo(0L);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

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
