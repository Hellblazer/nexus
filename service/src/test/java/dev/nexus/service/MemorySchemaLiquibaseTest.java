package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
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

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.5 — Liquibase memory baseline integration test.
 *
 * <p>Hermetic: embedded Postgres (io.zonky), port 0, no Docker. Applies the
 * Liquibase master changelog programmatically (no Maven plugin binding yet —
 * that is bead .6) and asserts all required structural and runtime properties.
 *
 * <p>Required assertions (per bead spec):
 * <ol>
 *   <li>memory table exists with exact column set (tenant_id + all mirrored columns)</li>
 *   <li>RLS: relrowsecurity=t, relforcerowsecurity=t; pg_policies has USING + WITH CHECK</li>
 *   <li>fts_vector generated column + GIN index exist; tokenisation config verified</li>
 *   <li>End-to-end RLS + FTS via TenantScope.withTenant: tenant isolation + FTS query</li>
 *   <li>S0.4 C4 defensive: rolsuper=false, rolbypassrls=false for service role</li>
 * </ol>
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemorySchemaLiquibaseTest {

    // Expected exact column set in nexus.memory (order-independent).
    private static final Set<String> EXPECTED_COLUMNS = Set.of(
        "id", "tenant_id", "project", "title", "session", "agent",
        "content", "tags", "timestamp", "ttl", "access_count", "last_accessed",
        "fts_vector"
    );

    // Service role created by @BeforeAll — plain LOGIN, no superuser, no bypassrls.
    private static final String SVC_ROLE = "svc_memory_test";
    private static final String SVC_PASS = "svc_memory_test_pass";

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        // Bootstrap service role BEFORE Liquibase runs (so changeset 5 finds it).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            // Rename the role to nexus_svc so changeset 5's DO block finds it,
            // then the svcDs connects using the original svc_memory_test credentials.
            // Simpler: create nexus_svc alias as well.
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        // Apply Liquibase changelog via superuser connection (schema DDL requires superuser
        // or schema owner; service role is granted privileges after table creation).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                db);
            liquibase.update(new Contexts());
        }

        // Grant svc_memory_test the same privileges as nexus_svc (for RLS test).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.close();
    }

    // ── Test 1: exact column set ─────────────────────────────────────────────

    @Test
    void memoryTable_hasExactColumnSet() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.getMetaData().getColumns(null, "nexus", "memory", null);
            Set<String> actual = new java.util.HashSet<>();
            while (rs.next()) {
                actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            }
            assertThat(actual)
                .as("nexus.memory must have exactly the mirrored + tenant columns")
                .isEqualTo(EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: RLS flags and policy ─────────────────────────────────────────

    @Test
    void memoryTable_rlsEnabledAndForced() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            // pg_class flags
            ResultSet cls = su.createStatement().executeQuery(
                "SELECT relrowsecurity, relforcerowsecurity " +
                "FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'memory'");
            assertThat(cls.next()).as("nexus.memory must exist in pg_class").isTrue();
            assertThat(cls.getBoolean("relrowsecurity"))
                .as("relrowsecurity must be true (ENABLE ROW LEVEL SECURITY)").isTrue();
            assertThat(cls.getBoolean("relforcerowsecurity"))
                .as("relforcerowsecurity must be true (FORCE ROW LEVEL SECURITY)").isTrue();

            // pg_policies: expect exactly one policy covering both USING and WITH CHECK
            ResultSet pol = su.createStatement().executeQuery(
                "SELECT policyname, cmd, qual, with_check " +
                "FROM pg_policies " +
                "WHERE schemaname = 'nexus' AND tablename = 'memory'");
            assertThat(pol.next()).as("at least one RLS policy must exist on nexus.memory").isTrue();
            String polcmd     = pol.getString("cmd");
            String qual       = pol.getString("qual");
            String withCheck  = pol.getString("with_check");
            // pg_policies.cmd is 'ALL', 'SELECT', 'INSERT', 'UPDATE', or 'DELETE'
            assertThat(polcmd).as("policy must cover ALL commands").isEqualTo("ALL");
            assertThat(qual)
                .as("USING expression must reference tenant_id GUC check")
                .contains("current_setting");
            assertThat(withCheck)
                .as("WITH CHECK expression must reference tenant_id GUC check")
                .contains("current_setting");
        }
    }

    // ── Test 3: tsvector generated column + GIN index + tokenisation config ──

    @Test
    void memoryTable_ftsColumnAndIndexExist_tokenisationCorrect() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            // fts_vector column exists and is a generated stored column
            ResultSet gen = su.createStatement().executeQuery(
                "SELECT a.attname, a.attgenerated, pg_catalog.format_type(a.atttypid, a.atttypmod) AS col_type " +
                "FROM pg_attribute a " +
                "JOIN pg_class c ON c.oid = a.attrelid " +
                "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'memory' " +
                "  AND a.attname = 'fts_vector' AND a.attnum > 0 AND NOT a.attisdropped");
            assertThat(gen.next()).as("fts_vector column must exist").isTrue();
            assertThat(gen.getString("col_type"))
                .as("fts_vector must be tsvector type").isEqualTo("tsvector");
            // attgenerated='s' means STORED generated column (PostgreSQL 12+)
            assertThat(gen.getString("attgenerated"))
                .as("fts_vector must be a STORED generated column (attgenerated='s')")
                .isEqualTo("s");

            // GIN index exists on fts_vector
            ResultSet idx = su.createStatement().executeQuery(
                "SELECT i.relname AS index_name, am.amname AS index_type, " +
                "       a.attname AS col_name " +
                "FROM pg_index ix " +
                "JOIN pg_class c  ON c.oid = ix.indrelid " +
                "JOIN pg_class i  ON i.oid = ix.indexrelid " +
                "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                "JOIN pg_am am ON am.oid = i.relam " +
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(ix.indkey) " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'memory' " +
                "  AND am.amname = 'gin' AND a.attname = 'fts_vector'");
            assertThat(idx.next())
                .as("GIN index on fts_vector must exist").isTrue();
            assertThat(idx.getString("index_type"))
                .as("index type must be GIN").isEqualTo("gin");

            // Inspect generated column definition to verify tokenisation configs.
            // pg_get_expr returns the expression from pg_attrdef for generated columns.
            ResultSet expr = su.createStatement().executeQuery(
                "SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS col_expr " +
                "FROM pg_attrdef d " +
                "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum " +
                "JOIN pg_class c ON c.oid = d.adrelid " +
                "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'memory' AND a.attname = 'fts_vector'");
            assertThat(expr.next()).as("pg_attrdef must have entry for fts_vector").isTrue();
            String colExpr = expr.getString("col_expr");
            // The expression must reference 'english' config for title/content
            // and 'simple' config for tags, per parity contract rev 2.
            assertThat(colExpr)
                .as("generated expression must use 'english' config for prose columns")
                .contains("english");
            assertThat(colExpr)
                .as("generated expression must use 'simple' config for tags column")
                .contains("simple");
            // Verify setweight calls with the expected weight letters
            assertThat(colExpr)
                .as("generated expression must include setweight 'A' for title")
                .contains("'A'");
            assertThat(colExpr)
                .as("generated expression must include setweight 'B' for content")
                .contains("'B'");
            assertThat(colExpr)
                .as("generated expression must include setweight 'C' for tags")
                .contains("'C'");
        }

        // Probe row: insert via superuser, verify fts_vector populated correctly.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            // Stamp a probe tenant to satisfy RLS (FORCE applies to owner too).
            try (var ps = su.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
                ps.setString(1, "probe-tenant");
                ps.execute();
            }
            su.createStatement().execute(
                "INSERT INTO nexus.memory " +
                "(tenant_id, project, title, content, tags, timestamp, access_count) " +
                "VALUES " +
                "('probe-tenant', 'probe-proj', 'Rust async programming', " +
                " 'async await futures tokio', 'rust,async,systems', now(), 0)");
            // Verify english tokenisation: 'programming' should be stemmed to 'program'
            ResultSet ftsCheck = su.createStatement().executeQuery(
                "SELECT fts_vector @@ plainto_tsquery('english', 'programming') AS title_match, " +
                "       fts_vector @@ plainto_tsquery('simple',  'rust')         AS tag_match " +
                "FROM nexus.memory " +
                "WHERE tenant_id = 'probe-tenant' AND title = 'Rust async programming'");
            assertThat(ftsCheck.next()).as("probe row must be retrievable").isTrue();
            assertThat(ftsCheck.getBoolean("title_match"))
                .as("english-stemmed title query 'programming' must match title 'Rust async programming'")
                .isTrue();
            assertThat(ftsCheck.getBoolean("tag_match"))
                .as("simple-config tag query 'rust' must match tags 'rust,async,systems'")
                .isTrue();
            su.rollback();  // cleanup probe row
        }
    }

    // ── Test 4: end-to-end RLS + FTS via TenantScope ─────────────────────────

    @Test
    void tenantIsolation_and_ftsQuery_viaWithTenant() throws Exception {
        // Seed rows for two tenants via superuser (bypasses RLS for seeding).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            insertRow(su, "alpha", "alpha-proj", "Machine learning basics",
                "neural networks deep learning", "ml,ai,research");
            insertRow(su, "alpha", "alpha-proj", "Python type hints",
                "mypy type annotations generics", "python,types");
            insertRow(su, "alpha", "alpha-proj", "Database indexing strategies",
                "btree gin gist hash indexes performance", "database,indexing");
            insertRow(su, "beta",  "beta-proj",  "Rust ownership model",
                "borrow checker lifetimes ownership", "rust,systems");
            su.commit();
        }

        // tenant-alpha sees exactly its 3 rows via TenantScope.withTenant
        List<String> alphaTitles = tenantScope.withTenant("alpha", ctx ->
            ctx.fetch("SELECT title FROM nexus.memory WHERE project = 'alpha-proj' ORDER BY title")
               .getValues("title", String.class));
        assertThat(alphaTitles)
            .as("tenant-alpha must see exactly its 3 rows")
            .containsExactlyInAnyOrder(
                "Machine learning basics",
                "Python type hints",
                "Database indexing strategies");
        assertThat(alphaTitles)
            .as("tenant-alpha must NOT see beta's row")
            .doesNotContain("Rust ownership model");

        // tenant-beta sees only its 1 row
        List<String> betaTitles = tenantScope.withTenant("beta", ctx ->
            ctx.fetch("SELECT title FROM nexus.memory WHERE project = 'beta-proj' ORDER BY title")
               .getValues("title", String.class));
        assertThat(betaTitles)
            .as("tenant-beta must see exactly its 1 row")
            .containsExactly("Rust ownership model");
        assertThat(betaTitles)
            .as("tenant-beta must NOT see any of alpha's rows")
            .doesNotContain("Machine learning basics", "Python type hints", "Database indexing strategies");

        // FTS query scoped to tenant-alpha: search for 'neural' (english→'neural' retained)
        List<String> ftsAlpha = tenantScope.withTenant("alpha", ctx ->
            ctx.fetch(
                "SELECT title FROM nexus.memory " +
                "WHERE fts_vector @@ plainto_tsquery('english', 'neural') " +
                "ORDER BY title")
               .getValues("title", String.class));
        assertThat(ftsAlpha)
            .as("FTS query for 'neural' under tenant-alpha must match ML row only")
            .containsExactly("Machine learning basics");

        // FTS query scoped to tenant-beta: 'rust' in simple (tag) config
        List<String> ftsBeta = tenantScope.withTenant("beta", ctx ->
            ctx.fetch(
                "SELECT title FROM nexus.memory " +
                "WHERE fts_vector @@ plainto_tsquery('simple', 'rust') " +
                "ORDER BY title")
               .getValues("title", String.class));
        assertThat(ftsBeta)
            .as("FTS query for 'rust' (simple/tags) under tenant-beta must match Rust row")
            .containsExactly("Rust ownership model");

        // Cross-tenant FTS isolation: 'neural' under beta must return nothing
        List<String> ftsAlphaUnderBeta = tenantScope.withTenant("beta", ctx ->
            ctx.fetch(
                "SELECT title FROM nexus.memory " +
                "WHERE fts_vector @@ plainto_tsquery('english', 'neural') " +
                "ORDER BY title")
               .getValues("title", String.class));
        assertThat(ftsAlphaUnderBeta)
            .as("FTS query for 'neural' under tenant-beta must return empty (cross-tenant isolation)")
            .isEmpty();
    }

    // ── Test 5: S0.4 C4 defensive — rolsuper=false, rolbypassrls=false ───────

    @Test
    void serviceRole_notSuperuserNotBypassRls() throws Exception {
        tenantScope.withTenant("test-tenant", ctx -> {
            var row = ctx.fetchOne(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user");
            assertThat(row).as("pg_roles row for current_user must exist").isNotNull();
            assertThat(row.get("rolsuper", Boolean.class))
                .as("service role must NOT be superuser (would bypass RLS entirely)")
                .isFalse();
            assertThat(row.get("rolbypassrls", Boolean.class))
                .as("service role must NOT have BYPASSRLS (would bypass RLS on RLS-enabled tables)")
                .isFalse();
            return null;
        });
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(5);
        config.setAutoCommit(true);  // pool default; TenantScope toggles per borrow
        return new com.zaxxer.hikari.HikariDataSource(config);
    }

    /**
     * Insert a memory row via superuser connection (bypasses RLS for seeding).
     * Stamps the GUC so FORCE RLS WITH CHECK does not block the owner insert.
     */
    private void insertRow(Connection su, String tenant, String project,
                           String title, String content, String tags) throws Exception {
        try (var ps = su.prepareStatement("SELECT set_config('nexus.tenant', ?, true)")) {
            ps.setString(1, tenant);
            ps.execute();
        }
        try (var ps = su.prepareStatement(
                "INSERT INTO nexus.memory " +
                "(tenant_id, project, title, content, tags, timestamp, access_count) " +
                "VALUES (?, ?, ?, ?, ?, now(), 0) " +
                "ON CONFLICT (tenant_id, project, title) DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, project);
            ps.setString(3, title);
            ps.setString(4, content);
            ps.setString(5, tags);
            ps.executeUpdate();
        }
    }
}
