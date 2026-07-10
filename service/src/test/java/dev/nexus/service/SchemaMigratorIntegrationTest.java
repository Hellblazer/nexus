package dev.nexus.service;

import dev.nexus.service.db.SchemaMigrator;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.*;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

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
        "catalog_document_chunks", "catalog_collections", "catalog_meta",
        "service_tokens", "session_tokens",
        "chunks_384", "chunks_768", "chunks_1024"
    );

    private static final Set<String> EXPECTED_T1_TABLES = Set.of("scratch");

    // ── Fixtures ─────────────────────────────────────────────────────────────

    PostgreSQLContainer<?> pg;

    /** Migration pool — uses nexus_admin_test (non-superuser owner). */
    com.zaxxer.hikari.HikariDataSource adminDs;

    /** Service pool — uses nexus_svc (NOSUPERUSER NOBYPASSRLS). */
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void bootstrap() throws Exception {
        // Start a completely schema-less embedded Postgres.
        pg = PgContainerHelper.start();

        // ── Phase A: provisioning (done by DBA / Phase-5 nx step, NOT by Liquibase) ──
        // Using the embedded postgres superuser to simulate the DBA bootstrap:
        //   1. Create nexus_admin_test: NON-superuser, will own nexus + t1 schemas.
        //   2. Create nexus_svc: NOSUPERUSER NOBYPASSRLS LOGIN.
        //   3. Create the schemas and transfer ownership to nexus_admin_test.
        //      (In real provisioning: CREATE DATABASE nexus; CREATE SCHEMA nexus
        //       AUTHORIZATION nexus_admin; Liquibase then runs as nexus_admin.)
        try (Connection su = pg.createConnection("")) {
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

            // Pre-create pgvector and pg_trgm extensions as superuser (DBA step).
            // CREATE EXTENSION requires superuser in PostgreSQL; in production the DBA
            // installs extensions before nexus_admin runs the Liquibase changelog.
            // The vectors-001-baseline.xml changeset uses CREATE EXTENSION IF NOT EXISTS,
            // so it is idempotent: if already installed here it becomes a no-op when
            // Liquibase runs as nexus_admin_test.
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
        }

        // ── Phase B: build connection pools ─────────────────────────────────────

        // Migration pool: nexus_admin_test (non-superuser owner).
        var adminCfg = new com.zaxxer.hikari.HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        adminCfg.setPoolName("nexus-admin-test");
        adminDs = new com.zaxxer.hikari.HikariDataSource(adminCfg);

        // Service pool: nexus_svc (NOSUPERUSER NOBYPASSRLS) with search_path via initSql.
        var svcCfg = new com.zaxxer.hikari.HikariConfig();
        svcCfg.setJdbcUrl(pg.getJdbcUrl());
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
            if (pg != null) pg.stop();
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

    // ── Test 5: aged/divergent box — missing chash-length CHECK must not crash-loop ──

    /**
     * RDR nexus-4m6i0.1 (ms57z / GH#1390, engine-service v0.1.36 production incident).
     *
     * <p>Reproduces the real-world "aged box" scenario: a chash-length CHECK constraint
     * ({@code chunks_384_chash_len_check}) is missing when the migration reaches
     * {@code catalog-013-2}'s VALIDATE step. Before the fix, {@code catalog-013-2}'s bare
     * {@code ALTER TABLE ... VALIDATE CONSTRAINT ...} raises a hard Postgres ERROR that
     * {@link SchemaMigrator#migrate} rethrows as a fatal {@link SchemaMigrator.MigrationException}
     * — since the changeset never commits, EVERY subsequent boot retries the identical
     * failing statement (the crash loop). After the fix ({@code catalog-013-2} guarded by a
     * whole-changeset {@code <preConditions onFail="MARK_RAN">} counting all five
     * constraints, plus the new per-table-guarded {@code catalog-013-3}), migration must
     * complete cleanly: the precondition sees only 4 of 5 constraints, marks {@code
     * catalog-013-2} ran (once, no retry), and {@code catalog-013-3} independently
     * validates the four constraints that DO exist while leaving the missing one alone.
     *
     * <p>Uses a dedicated container (not the shared {@link #pg}/{@link #adminDs} from
     * {@code bootstrap()}) because the divergence must be injected BEFORE {@code
     * catalog-013-2} first executes; the shared fixture has already migrated cleanly by
     * {@code @Order(1)}, and Liquibase never re-runs an already-succeeded changeset.
     */
    @Test
    @Order(5)
    void agedBoxWithMissingChashConstraint_migrationDoesNotCrashLoop() throws Exception {
        PostgreSQLContainer<?> agedPg = PgContainerHelper.start();
        try {
            final String role = "nexus_admin_aged_test";
            final String pass = "nexus_admin_aged_test_pass";

            // Phase A: minimal DBA-equivalent bootstrap (mirrors bootstrap() above),
            // scoped to a throwaway admin role for this dedicated container. Also
            // pre-creates nexus_svc as superuser (same as bootstrap()'s SVC_ROLE) so
            // role-001-1's "IF NOT EXISTS" CREATE ROLE branch is skipped — otherwise
            // the migration role would need CREATEROLE just to no-op past it.
            try (Connection su = agedPg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
                su.createStatement().execute(
                    "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
            }

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(agedPg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-aged-test");

            try (var agedDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {

                // Phase B: migrate only up through catalog-002-2-chash-checks (the
                // last changeset that ADDS the five chash-length CHECK constraints),
                // via Liquibase's changeSetCount-limited update — so the divergence
                // can be injected BEFORE catalog-013-2 gets a chance to run.
                int changesetsThroughCatalog002;
                try (Connection conn = agedDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(
                            // SchemaMigrator.MASTER_CHANGELOG is package-private to
                            // dev.nexus.service.db; this test lives in dev.nexus.service,
                            // so the classpath-relative path is duplicated here verbatim.
                            "db/changelog/db.changelog-master.xml",
                            new ClassLoaderResourceAccessor(),
                            database)) {
                        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(
                            new Contexts(), new LabelExpression());
                        int idx = -1;
                        for (int i = 0; i < unrun.size(); i++) {
                            if ("catalog-002-2-chash-checks".equals(unrun.get(i).getId())) {
                                idx = i;
                                break;
                            }
                        }
                        assertThat(idx)
                            .as("catalog-002-2-chash-checks must be present in the master changelog")
                            .isGreaterThanOrEqualTo(0);
                        changesetsThroughCatalog002 = idx + 1;

                        liquibase.update(changesetsThroughCatalog002, new Contexts(), new LabelExpression());
                    }
                }

                // Phase C: simulate the real-world divergence — drop chunks_384's
                // chash-length CHECK. (Root cause of the real divergence is out of
                // scope here — investigated and closed as a dead end; see
                // catalog-013-3's inline comment. The fix must be defensive
                // regardless of how the divergence arose.)
                try (Connection conn = agedDs.getConnection()) {
                    conn.createStatement().execute(
                        "ALTER TABLE nexus.chunks_384 DROP CONSTRAINT chunks_384_chash_len_check");
                }

                // Phase D: resume the rest of the migration chain (catalog-003
                // onward, including catalog-013-2's guarded precondition and the
                // catalog-013-3 defensive re-validate). This is the RED/GREEN
                // hinge: before the fix, this throws MigrationException wrapping
                // the Postgres "constraint ... does not exist" error; after the
                // fix, it completes cleanly.
                assertThatCode(() -> SchemaMigrator.migrate(agedDs))
                    .as("migration must not crash-loop when a chash-length CHECK is missing on an aged box")
                    .doesNotThrowAnyException();

                // Phase E: the four constraints that DO exist must end up
                // VALIDATED; the missing one must simply stay absent (never
                // silently re-added, never fatal).
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(constraintValidated(conn, "chunks_768_chash_len_check"))
                        .as("chunks_768_chash_len_check must be validated despite chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chunks_1024_chash_len_check"))
                        .as("chunks_1024_chash_len_check must be validated despite chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "catalog_document_chunks_chash_len_check"))
                        .as("catalog_document_chunks_chash_len_check must be validated despite chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chash_index_chash_len_check"))
                        .as("chash_index_chash_len_check must be validated despite chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintExists(conn, "chunks_384_chash_len_check"))
                        .as("the dropped chunks_384_chash_len_check must remain absent, not silently re-added")
                        .isFalse();

                    // Phase F (nexus-boz39 round-2 gap): prove catalog-013-2 was
                    // MARK_RAN, not soft-failed-and-still-pending -- the property
                    // that actually distinguishes this fix from the superseded
                    // failOnError="false" approach, which "doesNotThrowAnyException"
                    // alone cannot tell apart.
                    assertThat(changesetExecType(conn, "catalog-013-2", "nexus-e0hd2",
                            "db/changelog/catalog-013-chash-checks-validate.xml"))
                        .as("catalog-013-2 must be recorded as MARK_RAN (skipped-and-marked, never retried) "
                            + "-- not FAILED (which Liquibase never marks, causing an every-boot re-attempt)")
                        .isEqualTo("MARK_RAN");
                }
            }
        } finally {
            agedPg.stop();
        }
    }

    // ── Test 6: aged/divergent box — missing chash_index constraint must not crash-loop ──

    /**
     * nexus-boz39 (substantive-critic follow-up to nexus-4m6i0.1). Test 5 above only
     * exercises the {@code chunks_384_chash_len_check} case — the real ms57z incident,
     * and one of the four constraints added in {@code catalog-002-hygiene.xml}.
     * {@code chash_index_chash_len_check} is structurally different: it is added later,
     * in {@code catalog-013-1} (this same changelog file), not in
     * {@code catalog-002-hygiene.xml} — a genuinely distinct migration code path, not
     * just a copy-paste of the same scenario. This test drops {@code
     * chash_index_chash_len_check} instead and asserts the migration still completes
     * cleanly, with the other four constraints validated and the dropped one left absent.
     *
     * <p>Uses a dedicated container for the same reason as test 5: the divergence must be
     * injected BEFORE {@code catalog-013-2} first executes, and the shared {@link #pg}/
     * {@link #adminDs} fixture has already migrated cleanly by {@code @Order(1)}.
     */
    @Test
    @Order(6)
    void agedBoxWithMissingChashIndexConstraint_migrationDoesNotCrashLoop() throws Exception {
        PostgreSQLContainer<?> agedPg = PgContainerHelper.start();
        try {
            final String role = "nexus_admin_aged_ci_test";
            final String pass = "nexus_admin_aged_ci_test_pass";

            // Phase A: same minimal DBA-equivalent bootstrap as test 5.
            try (Connection su = agedPg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
                su.createStatement().execute(
                    "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
            }

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(agedPg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-aged-ci-test");

            try (var agedDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {

                // Phase B: migrate only up through catalog-013-1 — the changeset that
                // ADDS chash_index_chash_len_check (unlike the other four, added in
                // catalog-002-hygiene.xml) — so the divergence can be injected BEFORE
                // catalog-013-2 gets a chance to run.
                int changesetsThroughCatalog0131;
                try (Connection conn = agedDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(
                            "db/changelog/db.changelog-master.xml",
                            new ClassLoaderResourceAccessor(),
                            database)) {
                        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(
                            new Contexts(), new LabelExpression());
                        int idx = -1;
                        for (int i = 0; i < unrun.size(); i++) {
                            if ("catalog-013-1".equals(unrun.get(i).getId())) {
                                idx = i;
                                break;
                            }
                        }
                        assertThat(idx)
                            .as("catalog-013-1 must be present in the master changelog")
                            .isGreaterThanOrEqualTo(0);
                        changesetsThroughCatalog0131 = idx + 1;

                        liquibase.update(changesetsThroughCatalog0131, new Contexts(), new LabelExpression());
                    }
                }

                // Phase C: simulate the divergence — drop chash_index's chash-length
                // CHECK right after it was added.
                try (Connection conn = agedDs.getConnection()) {
                    conn.createStatement().execute(
                        "ALTER TABLE nexus.chash_index DROP CONSTRAINT chash_index_chash_len_check");
                }

                // Phase D: resume the rest of the migration chain (catalog-013-1b
                // onward, including catalog-013-2's guarded precondition and the
                // catalog-013-3 defensive re-validate). Must not throw.
                assertThatCode(() -> SchemaMigrator.migrate(agedDs))
                    .as("migration must not crash-loop when chash_index_chash_len_check is missing on an aged box")
                    .doesNotThrowAnyException();

                // Phase E: the four constraints that DO exist must end up VALIDATED;
                // the missing one must simply stay absent (never silently re-added).
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(constraintValidated(conn, "chunks_384_chash_len_check"))
                        .as("chunks_384_chash_len_check must be validated despite chash_index's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chunks_768_chash_len_check"))
                        .as("chunks_768_chash_len_check must be validated despite chash_index's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chunks_1024_chash_len_check"))
                        .as("chunks_1024_chash_len_check must be validated despite chash_index's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "catalog_document_chunks_chash_len_check"))
                        .as("catalog_document_chunks_chash_len_check must be validated despite chash_index's divergence")
                        .isTrue();
                    assertThat(constraintExists(conn, "chash_index_chash_len_check"))
                        .as("the dropped chash_index_chash_len_check must remain absent, not silently re-added")
                        .isFalse();

                    // Phase F (nexus-boz39 round-2 gap): same MARK_RAN proof as test 5.
                    assertThat(changesetExecType(conn, "catalog-013-2", "nexus-e0hd2",
                            "db/changelog/catalog-013-chash-checks-validate.xml"))
                        .as("catalog-013-2 must be recorded as MARK_RAN (skipped-and-marked, never retried) "
                            + "-- not FAILED (which Liquibase never marks, causing an every-boot re-attempt)")
                        .isEqualTo("MARK_RAN");
                }
            }
        } finally {
            agedPg.stop();
        }
    }

    // ── Test 7: happy path — fresh box validates all five chash constraints ──

    /**
     * Verification gate 3 (nexus-4m6i0.1): the defensive re-validate in
     * {@code catalog-013-3} must not change happy-path behavior. On a fresh box
     * where all five constraints exist (the {@link #adminDs} fixture, already
     * migrated end-to-end by {@code @Order(1)}), every constraint must end up
     * {@code convalidated = true}.
     */
    @Test
    @Order(7)
    void freshBox_allFiveChashConstraints_endUpValidated() throws Exception {
        SchemaMigrator.migrate(adminDs); // defensive re-migrate; idempotent

        try (Connection conn = adminDs.getConnection()) {
            assertThat(constraintValidated(conn, "chunks_384_chash_len_check")).isTrue();
            assertThat(constraintValidated(conn, "chunks_768_chash_len_check")).isTrue();
            assertThat(constraintValidated(conn, "chunks_1024_chash_len_check")).isTrue();
            assertThat(constraintValidated(conn, "catalog_document_chunks_chash_len_check")).isTrue();
            assertThat(constraintValidated(conn, "chash_index_chash_len_check")).isTrue();
        }
    }

    // ── Test 8: aged/divergent box — missing fk-002 collection FK must not crash-loop ──

    /**
     * nexus-4m6i0.13 (follow-up to nexus-4m6i0.1 / nexus-4m6i0.2): {@code
     * fk-002-validate.xml} runs five bare {@code ALTER TABLE ... VALIDATE CONSTRAINT ...}
     * statements (changesets {@code fk-002-7}..{@code fk-002-11}), the identical crash-loop
     * risk class as {@code catalog-013-2} (ms57z / GH#1390) — before this fix, a missing
     * constraint on an aged/divergent box would raise a hard Postgres ERROR and, because a
     * failed changeset never commits a DATABASECHANGELOG row, crash-loop on every subsequent
     * boot. The fix retrofits each of the five changesets with a whole-changeset {@code
     * <preConditions onFail="MARK_RAN">} (single-name form, since each changeset validates
     * exactly one constraint — unlike {@code catalog-013-2}'s five-constraint IN-list form).
     * No {@code catalog-013-3}-style defensive re-validate changeset exists here, on
     * purpose: that changeset rescues collateral damage from catalog-013-2's MONOLITHIC
     * precondition (one missing constraint MARK_RANs all five VALIDATEs), a coupling the
     * independent fk-002-7..11 changesets never had — each skips only its own VALIDATE.
     *
     * <p>Reproduces the divergence on {@code chunks_384_collection_fk} — added {@code NOT
     * VALID} by changeset {@code fk-002-1} in {@code fk-002-collection-registry.xml}, then
     * (normally) VALIDATEd by {@code fk-002-7}. This test migrates only through {@code
     * fk-002-1}, drops the freshly-added constraint (modeling a box where it went missing
     * before {@code fk-002-7} could VALIDATE it), then resumes migration and asserts: (a) no
     * exception, (b) {@code fk-002-7} is recorded {@code MARK_RAN} (not silently re-attempted
     * forever), (c) the other four fk-002 collection FKs still end up validated — each via
     * ITS OWN independently-guarded changeset ({@code fk-002-8}..{@code fk-002-11}), and
     * (d) the dropped constraint stays absent (never silently re-added).
     *
     * <p>Uses a dedicated container for the same reason as tests 5/6: the divergence must be
     * injected BEFORE {@code fk-002-7} first executes, and the shared {@link #pg}/{@link
     * #adminDs} fixture has already migrated cleanly by {@code @Order(1)}.
     */
    @Test
    @Order(8)
    void agedBoxWithMissingFk002CollectionFk_migrationDoesNotCrashLoop() throws Exception {
        PostgreSQLContainer<?> agedPg = PgContainerHelper.start();
        try {
            final String role = "nexus_admin_aged_fk002_test";
            final String pass = "nexus_admin_aged_fk002_test_pass";

            // Phase A: same minimal DBA-equivalent bootstrap as tests 5/6.
            try (Connection su = agedPg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
                su.createStatement().execute(
                    "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
            }

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(agedPg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-aged-fk002-test");

            try (var agedDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {

                // Phase B: migrate only up through fk-002-1 — the changeset that ADDS
                // chunks_384_collection_fk NOT VALID — so the divergence can be injected
                // BEFORE fk-002-7 gets a chance to run.
                int changesetsThroughFk0021;
                try (Connection conn = agedDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(
                            "db/changelog/db.changelog-master.xml",
                            new ClassLoaderResourceAccessor(),
                            database)) {
                        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(
                            new Contexts(), new LabelExpression());
                        int idx = -1;
                        for (int i = 0; i < unrun.size(); i++) {
                            if ("fk-002-1".equals(unrun.get(i).getId())) {
                                idx = i;
                                break;
                            }
                        }
                        assertThat(idx)
                            .as("fk-002-1 must be present in the master changelog")
                            .isGreaterThanOrEqualTo(0);
                        changesetsThroughFk0021 = idx + 1;

                        liquibase.update(changesetsThroughFk0021, new Contexts(), new LabelExpression());
                    }
                }

                // Phase C: simulate the divergence — drop chunks_384_collection_fk right
                // after fk-002-1 added it NOT VALID.
                try (Connection conn = agedDs.getConnection()) {
                    conn.createStatement().execute(
                        "ALTER TABLE nexus.chunks_384 DROP CONSTRAINT chunks_384_collection_fk");
                }

                // Phase D: resume the rest of the migration chain (fk-002-2 onward,
                // including fk-002-7's guarded precondition). This is the RED/GREEN
                // hinge: before the fix, this throws MigrationException wrapping the
                // Postgres "constraint ... does not exist" error; after the fix, it
                // completes cleanly.
                assertThatCode(() -> SchemaMigrator.migrate(agedDs))
                    .as("migration must not crash-loop when chunks_384_collection_fk is "
                        + "missing on an aged box")
                    .doesNotThrowAnyException();

                // Phase E: the other four fk-002 collection FKs must end up VALIDATED
                // (each via its OWN independently-guarded changeset, fk-002-8..11);
                // the dropped one must simply stay absent (never silently re-added,
                // never fatal).
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(constraintValidated(conn, "chunks_768_collection_fk"))
                        .as("chunks_768_collection_fk must be validated despite "
                            + "chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chunks_1024_collection_fk"))
                        .as("chunks_1024_collection_fk must be validated despite "
                            + "chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chash_index_collection_fk"))
                        .as("chash_index_collection_fk must be validated despite "
                            + "chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintValidated(conn, "topic_assignments_collection_fk"))
                        .as("topic_assignments_collection_fk must be validated despite "
                            + "chunks_384's divergence")
                        .isTrue();
                    assertThat(constraintExists(conn, "chunks_384_collection_fk"))
                        .as("the dropped chunks_384_collection_fk must remain absent, "
                            + "not silently re-added")
                        .isFalse();

                    // Phase F: prove fk-002-7 was MARK_RAN, not soft-failed-and-still-pending
                    // -- the property that actually distinguishes a genuine fix from a
                    // regression back to bare/unguarded VALIDATE.
                    assertThat(changesetExecType(conn, "fk-002-7", "nexus-70r3c.3",
                            "db/changelog/fk-002-validate.xml"))
                        .as("fk-002-7 must be recorded as MARK_RAN (skipped-and-marked, "
                            + "never retried) -- not FAILED (which Liquibase never marks, "
                            + "causing an every-boot re-attempt)")
                        .isEqualTo("MARK_RAN");
                }
            }
        } finally {
            agedPg.stop();
        }
    }

    // ── Test 9: happy path — fresh box validates all ten fk-002/fk-003 collection FKs ──

    /**
     * nexus-4m6i0.13 verification gate: the fk-002-7..11/fk-003-7..11 preConditions
     * retrofit must not change happy-path behavior. On a fresh box where all ten
     * collection FK constraints exist (the {@link #adminDs} fixture, already migrated
     * end-to-end by {@code @Order(1)}), every one must end up {@code
     * convalidated = true}.
     */
    @Test
    @Order(9)
    void freshBox_allTenFkCollectionConstraints_endUpValidated() throws Exception {
        SchemaMigrator.migrate(adminDs); // defensive re-migrate; idempotent

        try (Connection conn = adminDs.getConnection()) {
            assertThat(constraintValidated(conn, "chunks_384_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "chunks_768_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "chunks_1024_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "chash_index_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "topic_assignments_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "document_aspects_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "aspect_extraction_queue_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "topics_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "taxonomy_meta_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "document_highlights_collection_fk")).isTrue();
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

    /** True iff a constraint with this name exists anywhere in the database. */
    private boolean constraintExists(Connection conn, String conname) throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT 1 FROM pg_constraint WHERE conname = ?")) {
            ps.setString(1, conname);
            ResultSet rs = ps.executeQuery();
            return rs.next();
        }
    }

    /** True iff a constraint with this name exists AND is validated (convalidated). */
    private boolean constraintValidated(Connection conn, String conname) throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT convalidated FROM pg_constraint WHERE conname = ?")) {
            ps.setString(1, conname);
            ResultSet rs = ps.executeQuery();
            return rs.next() && rs.getBoolean("convalidated");
        }
    }

    /**
     * DATABASECHANGELOG's EXECTYPE for a changeset (or {@code null} if it has no row
     * yet). nexus-boz39 round-2 review: {@code assertThatCode(...).doesNotThrowAnyException()}
     * alone does not distinguish the current {@code <preConditions onFail="MARK_RAN">}
     * fix from the superseded {@code failOnError="false"} approach — both leave a single
     * {@code migrate()} call non-throwing. Only a direct EXECTYPE='MARK_RAN' check proves
     * the changeset was skipped-and-marked (never retried) rather than soft-failed
     * (silently re-attempted, and SEVERE-logged, on every future boot).
     */
    private String changesetExecType(Connection conn, String id, String author, String filename)
            throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT exectype FROM databasechangelog WHERE id = ? AND author = ? AND filename = ?")) {
            ps.setString(1, id);
            ps.setString(2, author);
            ps.setString(3, filename);
            ResultSet rs = ps.executeQuery();
            return rs.next() ? rs.getString("exectype") : null;
        }
    }
}
