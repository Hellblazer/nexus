package dev.nexus.service;

import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.db.SchemaMigrator.MigrationException;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.postgresql.util.PSQLException;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.*;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
        // ("chash_index" left the end-state — RDR-187/nexus-piwya.9 DROP)
        "catalog_owners", "catalog_documents", "catalog_links",
        "catalog_document_chunks", "catalog_collections", "catalog_meta",
        "service_tokens", "session_tokens",
        // RDR-191 Phase 4 unify (vectors-004-1): chunks_384/768/1024 collapsed
        // into ONE nexus.chunks table with three nullable typed embedding
        // columns (embedding_384/768/1024) -- the three per-dim tables no
        // longer exist post-migration (DROP TABLE ... CASCADE).
        "chunks"
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
        pg = PgContainerHelper.startDedicated();

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

            // nexus-hzhgl: mirrors pg_provision.py's bootstrap-only GRANT pg_monitor TO
            // nexus_admin WITH ADMIN OPTION -- required since grants-004-monitor-wal-
            // visibility (grants-nexus-svc.xml) grants pg_monitor onward to nexus_svc, and
            // PostgreSQL refuses that GRANT unless the migration role already holds
            // pg_monitor WITH ADMIN OPTION (or is superuser). See GrantsPgMonitorTest for
            // the falsification proof of this exact prerequisite.
            su.createStatement().execute("GRANT pg_monitor TO " + ADMIN_ROLE + " WITH ADMIN OPTION");

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
        PostgreSQLContainer<?> agedPg = PgContainerHelper.startDedicated();
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
                // nexus-hzhgl: mirrors pg_provision.py's bootstrap-only GRANT pg_monitor TO
                // nexus_admin WITH ADMIN OPTION -- required since grants-004-monitor-wal-
                // visibility (grants-nexus-svc.xml) grants pg_monitor onward to nexus_svc,
                // and PostgreSQL refuses that GRANT unless the migration role already holds
                // pg_monitor WITH ADMIN OPTION (or is superuser). See GrantsPgMonitorTest for
                // the falsification proof of this exact prerequisite.
                su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
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

                // Phase E (RDR-180 era, RE-DERIVED for RDR-191 unify): the
                // migration chain now ALSO carries rdr180-2 (drops every
                // len_check, the divergence included — DROP IF EXISTS
                // tolerates the aged box), rdr180-11 (the octet successors,
                // NOT VALID at boot by design), AND vectors-004-1 (Phase 4:
                // chunks_384/768/1024 collapsed into ONE nexus.chunks table
                // via DROP TABLE ... CASCADE, which takes their per-table
                // octet CHECKs down with them — a constraint cannot outlive
                // its table). End-state: the three per-dim tables and every
                // constraint that named them are GONE; the unified
                // nexus.chunks carries exactly ONE octet CHECK in their
                // place, still NOT VALID (client rung validates post-rekey,
                // unchanged by the unify). catalog_document_chunks is
                // untouched by vectors-004-1 and keeps its own len/octet
                // pair exactly as before.
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(constraintExists(conn, "chunks_chash_octet_check"))
                        .as("unified nexus.chunks carries the octet CHECK post-RDR-191-unify "
                            + "(the aged chunks_384 divergence injected above is upstream of the "
                            + "copy and does not survive the DROP)")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chunks_chash_octet_check"))
                        .as("chunks_chash_octet_check stays NOT VALID at boot (client rung validates)")
                        .isFalse();
                    for (String t : new String[] {"chunks_384", "chunks_768", "chunks_1024"}) {
                        assertThat(constraintExists(conn, t + "_chash_len_check"))
                            .as("%s no longer exists post-vectors-004-1 unify -- no len_check to find", t)
                            .isFalse();
                        assertThat(constraintExists(conn, t + "_chash_octet_check"))
                            .as("%s's own octet CHECK died with its table at the unify DROP", t)
                            .isFalse();
                    }
                    assertThat(constraintExists(conn, "catalog_document_chunks_chash_len_check"))
                        .as("catalog_document_chunks_chash_len_check gone post-rdr180-2 "
                            + "(unaffected by the chunks unify)")
                        .isFalse();
                    assertThat(constraintExists(conn, "catalog_document_chunks_chash_octet_check"))
                        .as("catalog_document_chunks_chash_octet_check present post-rdr180-11 "
                            + "(unaffected by the chunks unify)")
                        .isTrue();
                    assertThat(constraintValidated(conn, "catalog_document_chunks_chash_octet_check"))
                        .as("catalog_document_chunks octet CHECK stays NOT VALID at boot")
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
        PostgreSQLContainer<?> agedPg = PgContainerHelper.startDedicated();
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
                // nexus-hzhgl: mirrors pg_provision.py's bootstrap-only GRANT pg_monitor TO
                // nexus_admin WITH ADMIN OPTION -- required since grants-004-monitor-wal-
                // visibility (grants-nexus-svc.xml) grants pg_monitor onward to nexus_svc,
                // and PostgreSQL refuses that GRANT unless the migration role already holds
                // pg_monitor WITH ADMIN OPTION (or is superuser). See GrantsPgMonitorTest for
                // the falsification proof of this exact prerequisite.
                su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
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

                // Phase E (RDR-180 era, RE-DERIVED for RDR-191 unify): the
                // chain now also carries rdr180-2 (drops every len_check —
                // the divergence included, via DROP IF EXISTS), rdr180-11
                // (octet successors, NOT VALID at boot), AND vectors-004-1
                // (chunks_384/768/1024 collapsed into ONE nexus.chunks via
                // DROP TABLE ... CASCADE — their per-table octet CHECKs die
                // with them). See test 5's Phase E for the full derivation;
                // the chash_index divergence injected above is unrelated to
                // this table set and does not change the end-state here.
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(constraintExists(conn, "chunks_chash_octet_check"))
                        .as("unified nexus.chunks carries the octet CHECK post-RDR-191-unify")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chunks_chash_octet_check"))
                        .as("chunks_chash_octet_check stays NOT VALID at boot (client rung validates)")
                        .isFalse();
                    for (String t : new String[] {"chunks_384", "chunks_768", "chunks_1024"}) {
                        assertThat(constraintExists(conn, t + "_chash_len_check"))
                            .as("%s no longer exists post-vectors-004-1 unify -- no len_check to find", t)
                            .isFalse();
                        assertThat(constraintExists(conn, t + "_chash_octet_check"))
                            .as("%s's own octet CHECK died with its table at the unify DROP", t)
                            .isFalse();
                    }
                    assertThat(constraintExists(conn, "catalog_document_chunks_chash_len_check"))
                        .as("catalog_document_chunks_chash_len_check gone post-rdr180-2 "
                            + "(unaffected by the chunks unify)")
                        .isFalse();
                    assertThat(constraintExists(conn, "catalog_document_chunks_chash_octet_check"))
                        .as("catalog_document_chunks_chash_octet_check present post-rdr180-11 "
                            + "(unaffected by the chunks unify)")
                        .isTrue();
                    assertThat(constraintValidated(conn, "catalog_document_chunks_chash_octet_check"))
                        .as("catalog_document_chunks octet CHECK stays NOT VALID at boot")
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
     * Verification gate 3 (nexus-4m6i0.1 lineage, RDR-180 era, RE-DERIVED for
     * RDR-191 Phase 4 unify): the happy path on a fresh box. The TEXT-era
     * len_check lifecycle (added, then VALIDATEd by catalog-013) is retired
     * by rdr180-2; the octet successors exist NOT VALID (validated only by
     * the client rung's admin connection, post-rekey — never at boot). On
     * top of that, vectors-004-1 collapses chunks_384/768/1024 into ONE
     * nexus.chunks table (DROP TABLE ... CASCADE), so the three per-table
     * octet CHECKs are also gone, replaced by a single unified
     * chunks_chash_octet_check. A defensive re-migrate stays idempotent.
     */
    @Test
    @Order(7)
    void freshBox_allFiveChashConstraints_octetEra_endState() throws Exception {
        SchemaMigrator.migrate(adminDs); // defensive re-migrate; idempotent

        try (Connection conn = adminDs.getConnection()) {
            // Unified nexus.chunks (RDR-191 Phase 4): ONE octet CHECK, still
            // NOT VALID at boot, in place of the three per-dim CHECKs.
            assertThat(constraintExists(conn, "chunks_chash_octet_check")).isTrue();
            assertThat(constraintValidated(conn, "chunks_chash_octet_check")).isFalse();
            // The three per-dim tables no longer exist -- no constraints of
            // any era survive them.
            for (String t : new String[] {"chunks_384", "chunks_768", "chunks_1024"}) {
                assertThat(constraintExists(conn, t + "_chash_len_check")).isFalse();
                assertThat(constraintExists(conn, t + "_chash_octet_check")).isFalse();
            }
            // catalog_document_chunks is untouched by the unify.
            assertThat(constraintExists(conn, "catalog_document_chunks_chash_len_check")).isFalse();
            assertThat(constraintExists(conn, "catalog_document_chunks_chash_octet_check")).isTrue();
            assertThat(constraintValidated(conn, "catalog_document_chunks_chash_octet_check")).isFalse();
            // RDR-187 (nexus-piwya.9): chash_index died at the DROP —
            // no constraints of any era.
            assertThat(constraintExists(conn, "chash_index_chash_octet_check")).isFalse();
            assertThat(constraintExists(conn, "chash_index_chash_len_check")).isFalse();
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
        PostgreSQLContainer<?> agedPg = PgContainerHelper.startDedicated();
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
                // nexus-hzhgl: mirrors pg_provision.py's bootstrap-only GRANT pg_monitor TO
                // nexus_admin WITH ADMIN OPTION -- required since grants-004-monitor-wal-
                // visibility (grants-nexus-svc.xml) grants pg_monitor onward to nexus_svc,
                // and PostgreSQL refuses that GRANT unless the migration role already holds
                // pg_monitor WITH ADMIN OPTION (or is superuser). See GrantsPgMonitorTest for
                // the falsification proof of this exact prerequisite.
                su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
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

                // Phase E (RE-DERIVED for RDR-191 unify): fk-002-8..11 still
                // independently VALIDATE chunks_768/1024_collection_fk and the
                // other collection FKs at the point they run -- unaffected by
                // chunks_384's divergence, exactly as before. But vectors-004-1
                // runs LATER in the same chain and DROPs chunks_384/768/1024 via
                // CASCADE, and a constraint cannot outlive its table: EVERY
                // fk-002 collection FK on the three per-dim tables is gone by
                // the time this assertion runs, the validated ones included.
                // NO COLLECTION FK IS ADDED ON nexus.chunks IN THIS BATCH (S3,
                // T2 [22436] -- deliberately deferred to Phase 5, beads
                // nexus-o8dil.29/.31), so the unified table carries ZERO
                // collection-FK enforcement at this point in the rollout.
                // topic_assignments is untouched by the unify and keeps its FK.
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(constraintExists(conn, "chunks_768_collection_fk"))
                        .as("chunks_768_collection_fk died with its table at the "
                            + "vectors-004-1 unify DROP, despite having been "
                            + "independently validated by fk-002-8 earlier in the chain")
                        .isFalse();
                    assertThat(constraintExists(conn, "chunks_1024_collection_fk"))
                        .as("chunks_1024_collection_fk died with its table at the "
                            + "vectors-004-1 unify DROP, despite having been "
                            + "independently validated by fk-002-9 earlier in the chain")
                        .isFalse();
                    // (chash_index_collection_fk died with its table even
                    // earlier — RDR-187/nexus-piwya.9; asserted absent below.)
                    assertThat(constraintValidated(conn, "topic_assignments_collection_fk"))
                        .as("topic_assignments_collection_fk must be validated despite "
                            + "chunks_384's divergence -- topic_assignments is untouched "
                            + "by the chunks unify")
                        .isTrue();
                    assertThat(constraintExists(conn, "chunks_384_collection_fk"))
                        .as("the dropped chunks_384_collection_fk must remain absent, "
                            + "not silently re-added -- doubly so once its table is gone")
                        .isFalse();
                    assertThat(constraintExists(conn, "chash_index_collection_fk"))
                        .as("chash_index_collection_fk absent -- RDR-187/nexus-piwya.9")
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
     * nexus-4m6i0.13 verification gate (RE-DERIVED for RDR-191 Phase 4 unify): the
     * fk-002-7..11/fk-003-7..11 preConditions retrofit must not change happy-path
     * behavior for the constraints that still exist. On a fresh box (the
     * {@link #adminDs} fixture, already migrated end-to-end by {@code @Order(1)}),
     * fk-002-8..11 DO independently validate chunks_768/1024_collection_fk
     * mid-chain, exactly as before -- but vectors-004-1 runs later in the SAME
     * chain and DROPs chunks_384/768/1024 via CASCADE, taking every fk-002
     * collection FK on those three tables down with them (a constraint cannot
     * outlive its table). No collection FK is added on nexus.chunks in this
     * batch (deliberately deferred to Phase 5, beads nexus-o8dil.29/.31), so by
     * the time this test's end-state is observed, ZERO of the three per-dim
     * collection FKs survive -- only the six FKs on tables the unify never
     * touched remain to validate.
     */
    @Test
    @Order(9)
    void freshBox_allTenFkCollectionConstraints_endUpValidated() throws Exception {
        SchemaMigrator.migrate(adminDs); // defensive re-migrate; idempotent

        try (Connection conn = adminDs.getConnection()) {
            // The three per-dim collection FKs died with their tables at the
            // vectors-004-1 unify DROP, despite having been independently
            // validated by fk-002-7..9 earlier in the same chain.
            assertThat(constraintExists(conn, "chunks_384_collection_fk")).isFalse();
            assertThat(constraintExists(conn, "chunks_768_collection_fk")).isFalse();
            assertThat(constraintExists(conn, "chunks_1024_collection_fk")).isFalse();
            // RDR-187 (nexus-piwya.9): chash_index_collection_fk died with its
            // table even earlier.
            assertThat(constraintExists(conn, "chash_index_collection_fk")).isFalse();
            // The six collection FKs on tables the unify never touched.
            assertThat(constraintValidated(conn, "topic_assignments_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "document_aspects_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "aspect_extraction_queue_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "topics_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "taxonomy_meta_collection_fk")).isTrue();
            assertThat(constraintValidated(conn, "document_highlights_collection_fk")).isTrue();
        }
    }

    // ── Test 10: present-but-VIOLATING chash constraint must FAIL CLEAN, not crash-loop ──

    /**
     * nexus-c4143 (follow-up to nexus-4m6i0.1 / ms57z / GH#1390). Tests 5/6/8 above cover
     * a constraint that is MISSING when catalog-013-2/fk-002-7 first runs — the defensive
     * {@code IF EXISTS} guards in {@code catalog-013-3} tolerate that case cleanly. This
     * test covers the DIFFERENT, opposite condition: the constraint is PRESENT (added
     * {@code NOT VALID}) but at least one row genuinely VIOLATES it (a chash whose length
     * is neither 32 nor the legacy 64 that {@code catalog-013-0}/{@code -1b} normalize).
     * {@code catalog-013-3}'s {@code IF EXISTS} guard does not help here — the constraint
     * DOES exist, so its bare {@code VALIDATE CONSTRAINT} still runs and still raises a
     * hard Postgres ERROR on the violating row, which (same crash-loop mechanism as ms57z)
     * would repeat on every subsequent boot.
     *
     * <p>Fix under test: {@link SchemaMigrator#migrate} now runs a preflight BEFORE
     * invoking Liquibase at all — for each of the five chash-length constraints that
     * EXISTS but is not yet {@code convalidated}, it counts violating rows on a
     * temporarily-{@code NO FORCE ROW LEVEL SECURITY} connection (the same RLS-bypass
     * pattern {@code catalog-013-1b} uses, closing the exact visibility gap that caused
     * the 2026-07-08 v0.1.33 production incident: a NOBYPASSRLS owner's plain SELECT
     * silently sees zero rows under FORCE RLS while VALIDATE — a physical scan, RLS-exempt
     * — still finds and crashes on the true violating rows). If any violations are found,
     * {@code migrate()} throws a single, clean, informative {@link SchemaMigrator.MigrationException}
     * — with the violating table/constraint/count named directly, so an operator does not
     * need to reproduce the RLS-blind diagnostic dead-end the incident hit — WITHOUT ever
     * invoking Liquibase, so no changeset is left FAILED-and-retried and the exact
     * remaining-good rows / recorded changesets are untouched, safe to retry after the
     * violating row is remediated.
     *
     * <p>Uses a dedicated container for the same reason as tests 5/6/8: the violation must
     * be injected BEFORE {@code catalog-013-2} first executes, and the shared {@link #pg}/
     * {@link #adminDs} fixture has already migrated cleanly by {@code @Order(1)}.
     */
    @Test
    @Order(10)
    void presentButViolatingChashIndexConstraint_migrationFailsCleanNotCrashLoop() throws Exception {
        PostgreSQLContainer<?> agedPg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_aged_viol_test";
            final String pass = "nexus_admin_aged_viol_test_pass";

            // Phase A: same minimal DBA-equivalent bootstrap as tests 5/6/8.
            try (Connection su = agedPg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
                // nexus-hzhgl: mirrors pg_provision.py's bootstrap-only GRANT pg_monitor TO
                // nexus_admin WITH ADMIN OPTION -- required since grants-004-monitor-wal-
                // visibility (grants-nexus-svc.xml) grants pg_monitor onward to nexus_svc,
                // and PostgreSQL refuses that GRANT unless the migration role already holds
                // pg_monitor WITH ADMIN OPTION (or is superuser). See GrantsPgMonitorTest for
                // the falsification proof of this exact prerequisite.
                su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
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
            cfg.setPoolName("nexus-admin-aged-viol-test");

            try (var agedDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {

                // Phase B: migrate only up through catalog-013-0 -- the LAST changeset that
                // runs BEFORE chash_index_chash_len_check is added. The chash_index TABLE
                // (and its RLS policy) already exist from chash-001-baseline.xml, long
                // before catalog-013, but no length-CHECK constraint exists yet at this
                // point, so a plain (RLS-toggled) INSERT of a malformed row still succeeds --
                // matching the real incident's timeline: the constraint is added NOT VALID in
                // one release (which does NOT check pre-existing rows), and VALIDATE is only
                // attempted much later, in a subsequent release.
                int changesetsThroughCatalog0130;
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
                            if ("catalog-013-0".equals(unrun.get(i).getId())) {
                                idx = i;
                                break;
                            }
                        }
                        assertThat(idx)
                            .as("catalog-013-0 must be present in the master changelog")
                            .isGreaterThanOrEqualTo(0);
                        changesetsThroughCatalog0130 = idx + 1;

                        liquibase.update(changesetsThroughCatalog0130, new Contexts(), new LabelExpression());
                    }
                }

                // Phase C: inject a GENUINELY violating row -- length 11, neither 32 (the
                // enforced width) nor 64 (the legacy width catalog-013-0/-1b normalize) --
                // via the SAME NO FORCE / FORCE toggle catalog-013-1b uses, since the admin
                // role is NOT the bypass-RLS superuser and FORCE RLS blocks even the owner's
                // own DML without a GUC stamp. No length-CHECK constraint exists on
                // chash_index yet at this point, so the INSERT itself succeeds cleanly.
                // chash_index_collection_fk (added earlier in the changelog than catalog-013)
                // requires a matching (tenant_id, name) row in catalog_collections first.
                try (Connection conn = agedDs.getConnection()) {
                    conn.setAutoCommit(true);
                    conn.createStatement().execute(
                        "ALTER TABLE nexus.catalog_collections NO FORCE ROW LEVEL SECURITY");
                    try (var ps = conn.prepareStatement(
                            "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?)")) {
                        ps.setString(1, "c4143-viol-tenant");
                        ps.setString(2, "c4143-viol-collection");
                        ps.executeUpdate();
                    }
                    conn.createStatement().execute(
                        "ALTER TABLE nexus.catalog_collections FORCE ROW LEVEL SECURITY");

                    conn.createStatement().execute(
                        "ALTER TABLE nexus.chash_index NO FORCE ROW LEVEL SECURITY");
                    try (var ps = conn.prepareStatement(
                            "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
                            + "VALUES (?, ?, ?, now())")) {
                        ps.setString(1, "c4143-viol-tenant");
                        ps.setString(2, "shortchash1"); // length 11 -- genuinely malformed
                        ps.setString(3, "c4143-viol-collection");
                        ps.executeUpdate();
                    }
                    conn.createStatement().execute(
                        "ALTER TABLE nexus.chash_index FORCE ROW LEVEL SECURITY");
                }

                // Phase C2: run catalog-013-1 (ADD CONSTRAINT ... NOT VALID) via Liquibase's
                // own update -- NOT VALID does not check pre-existing rows at ADD time, so
                // this succeeds despite the violating row just inserted, leaving the
                // constraint present-but-unvalidated exactly as it would be on a real box
                // between the release that adds it and the later release that first attempts
                // to VALIDATE it.
                try (Connection conn = agedDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(
                            "db/changelog/db.changelog-master.xml",
                            new ClassLoaderResourceAccessor(),
                            database)) {
                        liquibase.update(1, new Contexts(), new LabelExpression()); // catalog-013-1 only
                    }
                }
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(constraintExists(conn, "chash_index_chash_len_check"))
                        .as("catalog-013-1 must have added the constraint (NOT VALID) despite the "
                            + "pre-existing violating row")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chash_index_chash_len_check"))
                        .as("the constraint must NOT be validated yet -- only ADDED NOT VALID")
                        .isFalse();
                }

                // Phase D: resume the FULL migration. This is the RED/GREEN hinge: before the
                // Liquibase's bare catalog-013-2 VALIDATE CONSTRAINT crashes raw on the
                // violating row (a MigrationException wrapping the opaque Postgres error,
                // with NO row-count/visibility for the operator -- reproducing the incident's
                // finding-2 RLS-blind dead end); after the fix, the NEW preflight catches it
                // BEFORE Liquibase runs at all, with a clean, informative message.
                MigrationException thrown = null;
                try {
                    SchemaMigrator.migrate(agedDs);
                } catch (MigrationException e) {
                    thrown = e;
                }
                assertThat(thrown)
                    .as("migrate() must throw a clean MigrationException for a present-but-violating "
                        + "chash constraint, not let Liquibase's bare VALIDATE crash raw")
                    .isNotNull();
                assertThat(thrown.getMessage())
                    .as("the exception must name the violating table/constraint so an operator has "
                        + "direct visibility without an RLS-blind manual diagnostic query")
                    .contains("chash_index")
                    .contains("1"); // the violating row count

                // Phase E: catalog-013-2 must NOT have been reached/recorded at all -- the
                // preflight runs BEFORE Liquibase, so no changeset past catalog-013-1 (and
                // its immediate neighbor catalog-013-1b) should show any exectype, FAILED or
                // otherwise. A clean, un-attempted state is what makes a retry-after-remediate
                // safe.
                try (Connection conn = agedDs.getConnection()) {
                    assertThat(changesetExecType(conn, "catalog-013-2", "nexus-e0hd2",
                            "db/changelog/catalog-013-chash-checks-validate.xml"))
                        .as("catalog-013-2 must never be attempted -- the preflight blocks "
                            + "Liquibase from running at all while a violation is present")
                        .isNull();
                }
            }
        } finally {
            agedPg.stop();
        }
    }

    // ── Test 11: RDR-180 rewrite leaves planner statistics FRESH (BUG-0148) ──

    /**
     * BUG-0148 (conexus-xpg7, 2026-07-19): the rdr180-3/-7 {@code ALTER TABLE ...
     * ALTER COLUMN chash TYPE bytea} conversions REWRITE their tables, which resets
     * planner statistics — and a rewritten table looks "fresh" to autovacuum, so
     * autoanalyze may never re-trigger on a read-mostly store. The cloud boot
     * applied the rewrite and never ANALYZEd: the stale-stats planner flipped
     * sparse-text-gate hybrid queries off the GIN-bitmap plan onto the
     * budget-bounded HNSW plan, which shed rows to ZERO while every health check,
     * /version probe, and the aggregate cloud gate stayed green. Remediation was a
     * manual {@code ANALYZE} (conexus, 2026-07-19T18:25Z).
     *
     * <p>This pins the product fix (rdr180-16): a boot that applies the RDR-180
     * rewrite changesets must leave the rewritten tables ANALYZEd, so upgrading
     * local installs — where nobody is standing by to run ANALYZE — never re-live
     * the incident.
     *
     * <p>Cumulative stats reporting is asynchronous (shared memory since PG 15),
     * so poll briefly instead of asserting a single read.
     *
     * <p><strong>RE-DERIVED for RDR-191 Phase 4 unify — KNOWN GAP, nexus-97gii.</strong>
     * chunks_384/768/1024 no longer exist (vectors-004-1 collapses them into
     * nexus.chunks via CREATE + bulk INSERT...SELECT + DROP ... CASCADE), so
     * this test's own explicit-ANALYZE invariant now targets nexus.chunks in
     * their place. Unlike rdr180-001-bytea-chash.xml's {@code ALTER COLUMN
     * TYPE} rewrite of the (by-then populated) per-dim tables — which pairs
     * an explicit {@code ANALYZE nexus.chunks_384/768/1024;} in the SAME
     * changeset — vectors-004-1 creates nexus.chunks fresh and bulk-copies
     * into it but runs no explicit ANALYZE at all (confirmed: no
     * {@code ANALYZE nexus.chunks;} appears anywhere under
     * {@code db/changelog/}). That is the identical BUG-0148 risk shape this
     * whole test exists to catch, just via "never populated" rather than
     * "reset to stale": with default {@code autovacuum_naptime} (60s) autoanalyze
     * is not guaranteed within this test's 10s poll window, and in production
     * the first post-migration queries against a 384k+-row table can run
     * planned against NULL statistics. Filed as nexus-97gii (fix: add an
     * explicit {@code ANALYZE nexus.chunks;} to vectors-004-1, e.g. after step
     * 5's index builds) — out of this lane's edit surface (vectors-004-1.xml
     * belongs to Step A, already landed). This assertion pins the TRUE target
     * contract rather than being narrowed to the tables that still exist, so
     * it stays RED until nexus-97gii lands.
     */
    @Test
    @Order(11)
    void rdr180Rewrite_leavesPlannerStatsFresh() throws Exception {
        SchemaMigrator.migrate(adminDs);  // defensive; idempotent

        // (chash_index left the set — RDR-187/nexus-piwya.9: dropped at HEAD,
        // so it can carry no statistics at all. chunks_384/768/1024 left the
        // set — RDR-191 Phase 4: collapsed into "chunks", see nexus-97gii.)
        Set<String> expected = Set.of(
            "chunks", "catalog_document_chunks");

        Set<String> analyzed = new HashSet<>();
        long deadline = System.nanoTime() + java.time.Duration.ofSeconds(10).toNanos();
        while (System.nanoTime() < deadline) {
            analyzed.clear();
            try (Connection conn = adminDs.getConnection()) {
                ResultSet rs = conn.createStatement().executeQuery(
                    "SELECT relname FROM pg_stat_user_tables "
                    + "WHERE schemaname = 'nexus' AND last_analyze IS NOT NULL");
                while (rs.next()) {
                    analyzed.add(rs.getString("relname"));
                }
            }
            if (analyzed.containsAll(expected)) {
                break;
            }
            Thread.sleep(200);
        }

        assertThat(analyzed)
            .as("the RDR-180-rewritten tables must have fresh planner statistics after "
                + "migration (last_analyze stamped) — a rewrite silently resets stats and "
                + "degrades sparse-gate hybrid queries to zero rows (BUG-0148)")
            .containsAll(expected);
    }

    // ── Test 12: nexus_svc can actually ANALYZE chash_alias (rdr180-17) ──────

    /**
     * F2 (production 2026-07-20): the rekey and staging-promote paths ANALYZE
     * {@code nexus.chash_alias} inside their own transaction so the planner can
     * see the alias rows they just wrote — without it, a multi-tenant store's
     * second tenant is planned against statistics frozen at "100% tenant 1"
     * and the cascade degrades from 461 seconds to 101 minutes.
     *
     * <p>This asserts the PRIVILEGE half of that fix, which is the half that
     * fails silently: {@code nexus_svc} holds DML grants only and does not own
     * the table, and Postgres does not ERROR when a non-owner analyzes — it
     * WARNs and SKIPS. So an engine shipped without this grant would run the
     * ANALYZE, log nothing the caller sees, and leave the planner blind.
     * {@code RekeyOpsIntegrationTest} proves the rekey produces statistics
     * given the privilege; this proves the migration actually grants it to the
     * role production uses.
     */
    @Test
    @Order(12)
    void chashAlias_isAnalyzableByNexusSvc() throws Exception {
        SchemaMigrator.migrate(adminDs);  // defensive; idempotent

        try (Connection conn = adminDs.getConnection()) {
            // No version assumption: nexus always installs its own PostgreSQL
            // bundle (17.x) and never adopts a host server, so MAINTAIN is
            // always available. A server that cannot satisfy this is an
            // unsupported substrate and SHOULD fail here rather than be
            // skipped past.
            ResultSet rs = conn.createStatement().executeQuery(
                "SELECT pg_catalog.has_table_privilege('nexus_svc', "
                + "'nexus.chash_alias', 'MAINTAIN')");
            assertThat(rs.next()).isTrue();
            assertThat(rs.getBoolean(1))
                .as("grants-nexus-svc must grant MAINTAIN on nexus.chash_alias to "
                    + "nexus_svc — without it the rekey's in-transaction ANALYZE is a "
                    + "SILENT no-op (Postgres warns and skips for a non-owner) and the "
                    + "F2 planner blindness returns unnoticed")
                .isTrue();
        }
    }

    // ── Test 11: late-upgrading deployment with a pre-existing dangling manifest
    //    population must boot successfully through the full RDR-191 Phase 5
    //    ADD-NOT-VALID -> remediate -> VALIDATE walk (nexus-o8dil.29) ──────────

    /**
     * The MANDATORY acceptance test for the manifest FK's three-step Liquibase
     * shape (T2 {@code nexus/rdr-191-validate-placement-decision} [22557]; RDR-191
     * amendment (xi)). A late-upgrading deployment's OWN accumulated
     * dangling-manifest population — a {@code catalog_document_chunks} row whose
     * chash has no matching {@code nexus.chunks} row, created while the FK did not
     * yet exist — must not brick the box: {@code catalog-029-1}'s anti-join
     * remediation must clean it up immediately before {@code catalog-029-2}'s
     * VALIDATE scans, in the SAME migration walk, for every future upgrader.
     *
     * <p>Uses a dedicated container for the same reason as tests 5/6/8/10: the
     * dangling population must be injected on a schema that has {@code
     * nexus.chunks} / {@code catalog_document_chunks} but NOT YET the FK — i.e.
     * BEFORE {@code catalog-029-0} first runs — and the shared {@link #pg}/{@link
     * #adminDs} fixture has already migrated cleanly (FK included) by {@code
     * @Order(1)}.
     *
     * <p>The RED/GREEN hinge, and this test's own falsification proof, is the
     * sibling test {@link #bareValidateWithoutRemediation_againstDanglingPopulation_throws}:
     * against the naive two-step shape (ADD NOT VALID -&gt; VALIDATE, skipping
     * remediation), {@code VALIDATE CONSTRAINT} raises a hard Postgres ERROR on
     * this exact population — proving the remediation changeset is load-bearing,
     * not incidental, for this test's own success below.
     */
    @Test
    @Order(13)
    void lateUpgradingDeployment_withPreExistingDanglingManifestPopulation_bootsSuccessfully_andRemediates()
            throws Exception {
        PostgreSQLContainer<?> agedPg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_o8dil29_test";
            final String pass = "nexus_admin_o8dil29_test_pass";

            // Phase A: same minimal DBA-equivalent bootstrap as tests 5/6/8/10.
            try (Connection su = agedPg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
                su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
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
            cfg.setPoolName("nexus-admin-o8dil29-test");

            try (var agedDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {

                // Phase B: migrate only up through the changeset immediately BEFORE
                // catalog-029-0 (the FK's ADD ... NOT VALID) -- so the dangling
                // population can be injected on a schema that has nexus.chunks /
                // catalog_document_chunks but NOT YET the FK, exactly the
                // late-upgrading-deployment shape amendment (x)/(xi) targets.
                int changesetsBeforeFk;
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
                            if ("catalog-029-0".equals(unrun.get(i).getId())) {
                                idx = i;
                                break;
                            }
                        }
                        assertThat(idx)
                            .as("catalog-029-0 must be present in the master changelog")
                            .isGreaterThanOrEqualTo(0);
                        changesetsBeforeFk = idx;

                        liquibase.update(changesetsBeforeFk, new Contexts(), new LabelExpression());
                    }
                }

                // Phase C: seed the pre-existing dangling population directly, on
                // the pre-FK schema -- exactly what a late upgrader's own
                // accumulated drift looks like. One GOOD (chunk-backed) row and one
                // DANGLING (no matching chunk) row, same tenant/collection/doc.
                final String tenant = "o8dil29-late-upgrade-tenant";
                final String collection = "knowledge__o8dil29-late-upgrade__voyage-context-3__v1";
                final String goodChash = "a".repeat(64);
                final String danglingChash = "b".repeat(64);
                try (Connection su = agedPg.createConnection("")) {
                    su.setAutoCommit(true);
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                        + "VALUES ('" + tenant + "', 'o8dil29-doc', 'late upgrade doc', '" + collection + "')");
                    su.createStatement().execute(
                        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) VALUES "
                        + "('" + tenant + "', '" + collection + "', decode('" + goodChash + "', 'hex'), 'good text', "
                        + "('[" + "0.1,".repeat(383) + "0.1]')::vector)");
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                        + "VALUES ('" + tenant + "', 'o8dil29-doc', 0, decode('" + goodChash + "', 'hex'), '"
                        + collection + "')");
                    // The dangling row: no matching nexus.chunks row exists for
                    // danglingChash. Legal to insert here ONLY because the FK does
                    // not exist yet on this pre-catalog-029-0 schema -- exactly the
                    // pre-existing drift this test's remediation step must clean up.
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                        + "VALUES ('" + tenant + "', 'o8dil29-doc', 1, decode('" + danglingChash + "', 'hex'), '"
                        + collection + "')");
                }

                // Phase D: resume the rest of the migration chain -- catalog-029-0
                // (ADD NOT VALID), catalog-029-1 (remediate), catalog-029-2
                // (VALIDATE), catalog-029-3 (purge_trash fix), plus everything
                // after. This is the RED/GREEN hinge: against the naive two-step
                // shape (ADD NOT VALID -> VALIDATE, no remediation),
                // catalog-029-2's VALIDATE would raise a hard Postgres ERROR on the
                // danglingChash row and MigrationException would propagate here
                // (see the sibling falsification test).
                assertThatCode(() -> SchemaMigrator.migrate(agedDs))
                    .as("a late-upgrading deployment with a pre-existing dangling manifest "
                        + "population must boot successfully through the full "
                        + "ADD-NOT-VALID -> remediate -> VALIDATE walk (RDR-191 Phase 5, "
                        + "nexus-o8dil.29) -- the naive two-step shape would throw "
                        + "MigrationException here")
                    .doesNotThrowAnyException();

                // Phase E: the FK exists and is VALIDATED, the dangling row is
                // gone, and the good (chunk-backed) row survived untouched.
                //
                // catalog-029-1's own toggle-wrap correctly restores FORCE ROW LEVEL
                // SECURITY on both tables before it commits (production-correct — we
                // WANT it back on). That means these Phase E SELECTs run under FORCE
                // RLS same as any other post-migration caller: the table OWNER
                // (nexus_admin_o8dil29_test, NOT BYPASSRLS) must stamp the
                // nexus.tenant GUC even for itself, or every row is invisible and a
                // plain COUNT(*) reads 0 regardless of what's actually in the table
                // (the exact php10/nexus-1wjmq trap this file's own Test 4 —
                // nexusSvc_noGucStamp_rlsFailClosed_returnsZeroRows — documents).
                try (Connection conn = agedDs.getConnection()) {
                    conn.setAutoCommit(false);
                    try (var ps = conn.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
                        ps.setString(1, tenant);
                        ps.execute();
                    }

                    assertThat(constraintValidated(conn, "fk_catalog_chunks_chunk"))
                        .as("fk_catalog_chunks_chunk must exist and be VALIDATED "
                            + "(pg_constraint.convalidated=true) after the full walk")
                        .isTrue();

                    assertThat(rows(conn, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                        + "WHERE tenant_id = '" + tenant + "' AND chash = decode('" + danglingChash + "', 'hex')"))
                        .as("the dangling row must have been remediated (deleted) by catalog-029-1 "
                            + "before catalog-029-2's VALIDATE ran")
                        .isEqualTo(0);

                    assertThat(rows(conn, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                        + "WHERE tenant_id = '" + tenant + "' AND chash = decode('" + goodChash + "', 'hex')"))
                        .as("the non-dangling (chunk-backed) manifest row must be untouched by "
                            + "remediation -- the anti-join must not over-delete")
                        .isEqualTo(1);
                }
            }
        } finally {
            agedPg.stop();
        }
    }

    // ── Test 12: falsification proof — VALIDATE alone, without catalog-029-1's
    //    remediation, must fail loud against the SAME dangling population ──────

    /**
     * The MUST-FAIL demonstration the test above depends on: reproduces the
     * naive two-step shape (ADD CONSTRAINT ... NOT VALID, then a bare VALIDATE
     * CONSTRAINT, skipping the anti-join remediation) against the identical
     * dangling-manifest population, and asserts it throws. This is the evidence
     * that {@code catalog-029-1} is load-bearing, not incidental, for {@link
     * #lateUpgradingDeployment_withPreExistingDanglingManifestPopulation_bootsSuccessfully_andRemediates}'s
     * success — the two tests share the exact same fixture shape, differing only
     * in whether the remediation changeset runs before VALIDATE.
     */
    @Test
    @Order(14)
    void bareValidateWithoutRemediation_againstDanglingPopulation_throws() throws Exception {
        PostgreSQLContainer<?> agedPg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_o8dil29_neg_test";
            final String pass = "nexus_admin_o8dil29_neg_test_pass";

            try (Connection su = agedPg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
                su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
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
            cfg.setPoolName("nexus-admin-o8dil29-neg-test");

            try (var agedDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {

                // Migrate through catalog-029-0 INCLUSIVE (idx+1) -- the FK exists,
                // NOT VALID, but catalog-029-1's remediation has NOT run.
                int changesetsThroughFk;
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
                            if ("catalog-029-0".equals(unrun.get(i).getId())) {
                                idx = i;
                                break;
                            }
                        }
                        assertThat(idx)
                            .as("catalog-029-0 must be present in the master changelog")
                            .isGreaterThanOrEqualTo(0);
                        changesetsThroughFk = idx + 1;

                        liquibase.update(changesetsThroughFk, new Contexts(), new LabelExpression());
                    }
                }

                final String tenant = "o8dil29-neg-tenant";
                final String collection = "knowledge__o8dil29-neg__voyage-context-3__v1";
                final String danglingChash = "c".repeat(64);
                try (Connection su = agedPg.createConnection("")) {
                    su.setAutoCommit(true);
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                        + "VALUES ('" + tenant + "', 'o8dil29-neg-doc', 'neg doc', '" + collection + "')");
                    // Legal here: the FK is NOT VALID (added by catalog-029-0
                    // above) but has not yet had its pre-existing rows checked --
                    // NOT VALID only enforces NEW writes going forward, and this
                    // INSERT is itself checked (would fail if the FK enforced
                    // new writes against a missing chunk) ... except NOT VALID
                    // enforces new INSERTs too. So seed the dangling row via the
                    // SAME bypass idiom used elsewhere in this suite: drop, insert,
                    // re-add NOT VALID -- reproducing exactly what a genuinely aged
                    // box looks like immediately before its own first VALIDATE.
                    su.createStatement().execute(
                        "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                        + "VALUES ('" + tenant + "', 'o8dil29-neg-doc', 0, decode('" + danglingChash + "', 'hex'), '"
                        + collection + "')");
                    su.createStatement().execute(
                        "ALTER TABLE nexus.catalog_document_chunks "
                        + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                        + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                        + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
                }

                // The naive two-step shape: VALIDATE alone, skipping remediation.
                // Must throw against the dangling row seeded above.
                try (Connection su = agedPg.createConnection("")) {
                    su.setAutoCommit(true);
                    assertThatThrownBy(() -> su.createStatement().execute(
                            "ALTER TABLE nexus.catalog_document_chunks VALIDATE CONSTRAINT fk_catalog_chunks_chunk"))
                        .as("VALIDATE CONSTRAINT alone, without catalog-029-1's anti-join remediation, "
                            + "must fail loud against a genuinely dangling row -- this is the naive "
                            + "two-step shape the three-step decision (T2 "
                            + "nexus/rdr-191-validate-placement-decision [22557]) exists to avoid, and "
                            + "the falsification proof that the sibling test's success is not vacuous")
                        .isInstanceOf(PSQLException.class)
                        .hasMessageContaining("fk_catalog_chunks_chunk");
                }
            }
        } finally {
            agedPg.stop();
        }
    }

    // ── Test 15: nexus-o8dil.49 (RDR-191 Phase 5, Deliverable 1) — unified
    //    chunks->catalog_collections FK: happy path boots and VALIDATEs ──────────

    /**
     * The happy-path half of nexus-o8dil.49's acceptance criterion: on the
     * shared {@link #adminDs} fixture (already migrated end-to-end by
     * {@code @Order(1)}), {@code chunks_collection_fk} exists and is VALIDATED —
     * proving fk-004-0 (ADD ... NOT VALID), fk-004-1-reconcile (additive
     * stub-register), and fk-004-2 (VALIDATE) all ran successfully in sequence
     * against the fresh, empty-population schema every other test in this file
     * already relies on.
     */
    @Test
    @Order(15)
    void chunksCollectionFk_existsAndValidated() throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            assertThat(constraintValidated(conn, "chunks_collection_fk"))
                .as("chunks_collection_fk must exist and be VALIDATED (pg_constraint.convalidated=true) "
                    + "after the full migration walk (nexus-o8dil.49, RDR-191 Phase 5)")
                .isTrue();
        }
    }

    // ── Test 16: nexus-o8dil.49 — MUST-FAIL demonstration: VALIDATE without
    //    fk-004-1-reconcile's additive stub-register fails loud on a genuinely
    //    unregistered collection ───────────────────────────────────────────────

    /**
     * The MUST-FAIL half of nexus-o8dil.49's acceptance criterion ("A test
     * proves the new VALIDATE changeset FAILS on a deliberately violating
     * row"). Because fk-004-1-reconcile derives every stub-registered
     * collection FROM nexus.chunks' own distinct values, the forward
     * three-step changelog can never itself produce a row VALIDATE would
     * reject — so this test constructs the violating scenario directly,
     * mirroring {@link #bareValidateWithoutRemediation_againstDanglingPopulation_throws}'s
     * shape: drop the FK, seed a chunks row under a collection with NO
     * catalog_collections registration, re-add the FK NOT VALID (which does
     * NOT retroactively check this pre-existing row), then VALIDATE ALONE
     * (skipping the reconcile) and assert it fails loud, naming the
     * constraint.
     */
    @Test
    @Order(16)
    void chunksCollectionFk_bareValidateWithoutReconcile_againstUnregisteredCollection_throws()
            throws Exception {
        PostgreSQLContainer<?> agedPg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_o8dil49_neg_test";
            final String pass = "nexus_admin_o8dil49_neg_test_pass";

            try (Connection su = agedPg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
                su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
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
            cfg.setPoolName("nexus-admin-o8dil49-neg-test");

            try (var agedDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                // Full migration first (simplest reliable way to reach a schema with
                // nexus.chunks + chunks_collection_fk already VALIDATED) -- then
                // reproduce the violating scenario directly against it, the same
                // "self-contained per test: drop + re-create the FK" idiom
                // CollectionRegistryFkTest#assertReconcileLoadBearing uses.
                SchemaMigrator.migrate(agedDs);

                final String tenant = "o8dil49-neg-tenant";
                final String unregCollection = "knowledge__o8dil49-neg__voyage-context-3__v1";
                final String chash = "d".repeat(64);
                try (Connection su = agedPg.createConnection("")) {
                    su.setAutoCommit(true);
                    su.createStatement().execute(
                        "ALTER TABLE nexus.chunks DROP CONSTRAINT IF EXISTS chunks_collection_fk");
                    // Legal here: the FK is absent. A genuinely unregistered
                    // collection -- no catalog_collections row for (tenant, name).
                    su.createStatement().execute(
                        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024) "
                        + "VALUES ('" + tenant + "', '" + unregCollection + "', decode('" + chash + "', 'hex'), "
                        + "'neg text', ('[" + "0.1,".repeat(1023) + "0.1]')::vector)");
                    su.createStatement().execute(
                        "ALTER TABLE nexus.chunks "
                        + "ADD CONSTRAINT chunks_collection_fk "
                        + "FOREIGN KEY (tenant_id, collection) REFERENCES nexus.catalog_collections (tenant_id, name) "
                        + "ON DELETE RESTRICT NOT VALID");

                    // The naive shape: VALIDATE alone, skipping fk-004-1-reconcile's
                    // additive stub-register. Must throw against the unregistered row.
                    assertThatThrownBy(() -> su.createStatement().execute(
                            "ALTER TABLE nexus.chunks VALIDATE CONSTRAINT chunks_collection_fk"))
                        .as("VALIDATE CONSTRAINT alone, without fk-004-1-reconcile's additive "
                            + "stub-register, must fail loud against a genuinely unregistered "
                            + "collection -- proves the VALIDATE changeset actually enforces the "
                            + "invariant rather than trivially passing (nexus-o8dil.49)")
                        .isInstanceOf(PSQLException.class)
                        .hasMessageContaining("chunks_collection_fk");
                }
            }
        } finally {
            agedPg.stop();
        }
    }

    // ── Test 17: nexus-71gw2 (RDR-191 Decision item 6) — catalog_document_chunks
    //    .collection is NOT NULL at HEAD ─────────────────────────────────────────

    /**
     * A cheap, standing pin that {@code catalog_document_chunks.collection}
     * carries {@code pg_attribute.attnotnull = true} on the shared,
     * fully-migrated {@link #adminDs} fixture — the invariant
     * catalog-025-collection-not-null.xml (bead nexus-71gw2) establishes.
     * RDR-191 Phase 7 (bead nexus-o8dil.37, Decision item 6) is CLOSED on this
     * evidence: a dedicated catalog-030 changeset was drafted, found
     * structurally vestigial (catalog-025-0 always closes the NULL-collection
     * population first, on every real deployment — see catalog-025-collection-
     * not-null.xml's own header), and DROPPED under the standing convergence
     * directive (2026-08-15) rather than shipped as permanent maintenance
     * surface for an invariant already proven elsewhere. This test remains as
     * the standing pin.
     */
    @Test
    @Order(17)
    void catalogDocumentChunksCollection_isNotNull_atHead() throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            ResultSet rs = conn.createStatement().executeQuery(
                "SELECT a.attnotnull FROM pg_attribute a "
                + "JOIN pg_class c ON c.oid = a.attrelid "
                + "JOIN pg_namespace n ON n.oid = c.relnamespace "
                + "WHERE n.nspname = 'nexus' AND c.relname = 'catalog_document_chunks' "
                + "  AND a.attname = 'collection'");
            assertThat(rs.next())
                .as("catalog_document_chunks.collection column must exist")
                .isTrue();
            assertThat(rs.getBoolean("attnotnull"))
                .as("catalog_document_chunks.collection must be NOT NULL at HEAD "
                    + "(RDR-191 Decision item 6, Phase 7 fold, nexus-o8dil.37)")
                .isTrue();
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private int rows(Connection conn, String sql) throws Exception {
        ResultSet rs = conn.createStatement().executeQuery(sql);
        rs.next();
        return rs.getInt(1);
    }

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
