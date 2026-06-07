package dev.nexus.service;

import dev.nexus.service.db.TenantConstants;
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
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
 *   <li>fts_vector generated column + GIN index exist; tokenisation config verified;
 *       english/simple DISCRIMINATION proven by negative simple-does-not-stem probe</li>
 *   <li>End-to-end RLS + FTS via TenantScope.withTenant: tenant isolation + FTS query</li>
 *   <li>S0.4 C4 defensive: rolsuper=false, rolbypassrls=false for service role</li>
 *   <li>RLS fail-closed: raw service-role connection without GUC stamp sees zero rows</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT rejected</li>
 *   <li>RLS WITH CHECK: cross-tenant tenant_id UPDATE (rewrite) rejected</li>
 * </ol>
 *
 * <p>Statistical FTS parity (top-K set equality + Spearman ≥ 0.90) is deferred
 * to the .9 MVV gate per the locked parity contract (nexus-gmiaf.2 rev 2); it
 * requires post-ETL production data.  This bead proves schema structure,
 * tokenisation behavior, and RLS enforcement only.
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
            // Create nexus_svc so changeset 5's grant DO block finds it.
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

        // Grant svc_memory_test the same privileges as nexus_svc (for RLS tests).
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
            String polcmd    = pol.getString("cmd");
            String qual      = pol.getString("qual");
            String withCheck = pol.getString("with_check");
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
    //
    // Proves both the structural DDL (STORED generated column, GIN index) and
    // the tokenisation behaviour required by the parity contract:
    //   - english config stems 'programming' → 'program', so a query for the
    //     stem matches the full form in the title (positive english probe)
    //   - simple config does NOT stem, so plainto_tsquery('simple','program')
    //     does NOT match tags='programming,systems' (negative discrimination probe)
    //   - simple config does match the exact token 'programming' in tags (positive
    //     simple probe confirms the column is indexed, just unstemmed)
    //
    // Statistical parity harness (top-K set equality + Spearman ≥ 0.90) is
    // deferred to the .9 MVV gate per the locked parity contract (nexus-gmiaf.2
    // rev 2); it requires post-ETL production data volumes.

    @Test
    void memoryTable_ftsColumnAndIndexExist_tokenisationCorrect() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            // fts_vector column exists and is a generated stored column
            ResultSet gen = su.createStatement().executeQuery(
                "SELECT a.attname, a.attgenerated, " +
                "       pg_catalog.format_type(a.atttypid, a.atttypmod) AS col_type " +
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

            // Inspect generated column expression to verify tokenisation configs.
            ResultSet expr = su.createStatement().executeQuery(
                "SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS col_expr " +
                "FROM pg_attrdef d " +
                "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum " +
                "JOIN pg_class c ON c.oid = d.adrelid " +
                "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'memory' " +
                "  AND a.attname = 'fts_vector'");
            assertThat(expr.next()).as("pg_attrdef must have entry for fts_vector").isTrue();
            String colExpr = expr.getString("col_expr");
            assertThat(colExpr)
                .as("generated expression must use 'english' config for prose columns")
                .contains("english");
            assertThat(colExpr)
                .as("generated expression must use 'simple' config for tags column")
                .contains("simple");
            assertThat(colExpr).as("must include setweight 'A' for title").contains("'A'");
            assertThat(colExpr).as("must include setweight 'B' for content").contains("'B'");
            assertThat(colExpr).as("must include setweight 'C' for tags").contains("'C'");
        }

        // Probe row: verify tokenisation behaviour, not just DDL strings.
        //
        // Design: the discriminating word ('running') appears ONLY in tags, not in
        // title or content, so the fts_vector's 'running' lexeme comes exclusively
        // from the simple-indexed tags column (weight C).  Title/content are indexed
        // under english and contain no word that stems to 'run', so there is no
        // cross-column contamination that could mask the negative assertion.
        //
        // The three probes together prove that title/content use english (stemming)
        // and tags uses simple (verbatim, no stemming) as separate configs:
        //   (1) english stems: 'mechanics' → 'mechan'; querying the stem 'mechanic'
        //       hits the title lexeme (english config produces same stem for both forms)
        //   (2) simple exact: 'running' stored verbatim in tags; exact query matches
        //   (3) KEY NEGATIVE: 'run' is the english stem of 'running', but simple does
        //       NOT stem, so plainto_tsquery('simple','run') → literal token 'run',
        //       which does NOT match 'running' in the simple-indexed tags column.
        //       If tags were accidentally indexed under english instead of simple,
        //       'running' would be stored as 'run' and the query WOULD match.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            try (var ps = su.prepareStatement("SELECT set_config(?, ?, true)")) {
                ps.setString(1, TenantConstants.GUC_NAME);
                ps.setString(2, "probe-tenant");
                ps.execute();
            }
            su.createStatement().execute(
                "INSERT INTO nexus.memory " +
                "(tenant_id, project, title, content, tags, timestamp, access_count) " +
                "VALUES " +
                "('probe-tenant', 'probe-proj', 'Quantum mechanics overview', " +
                " 'wave functions superposition entanglement', 'running,distributed', now(), 0)");

            ResultSet ftsCheck = su.createStatement().executeQuery(
                // (1) Positive english: 'mechanics' stems to 'mechan'; 'mechanic' also
                //     stems to 'mechan' under english.  Title is indexed under english,
                //     so the stem query must match.
                "SELECT fts_vector @@ plainto_tsquery('english', 'mechanic')  AS english_stem_match, " +
                // (2) Positive simple exact: tags='running,...'; simple stores verbatim.
                "       fts_vector @@ plainto_tsquery('simple',  'running')   AS simple_exact_match, " +
                // (3) NEGATIVE discrimination: 'run' is the english stem of 'running'.
                //     Under simple, 'running' is stored as-is (no stemming), so querying
                //     the stem 'run' must NOT match.  Proves tags≠english.
                "       fts_vector @@ plainto_tsquery('simple',  'run')       AS simple_stem_no_match " +
                "FROM nexus.memory " +
                "WHERE tenant_id = 'probe-tenant' AND title = 'Quantum mechanics overview'");

            assertThat(ftsCheck.next()).as("probe row must be retrievable").isTrue();

            assertThat(ftsCheck.getBoolean("english_stem_match"))
                .as("english config must stem: 'mechanic' and 'mechanics' share stem 'mechan'; " +
                    "title is indexed under english so query matches")
                .isTrue();

            assertThat(ftsCheck.getBoolean("simple_exact_match"))
                .as("simple config must match exact token: 'running' stored verbatim in tags")
                .isTrue();

            // This is the discrimination assertion: if tags were indexed under english
            // instead of simple, 'running' would be stored as 'run' (stem) and
            // plainto_tsquery('simple','run') → literal 'run' would match.
            // Under correct simple indexing, 'running' ≠ 'run', so it must NOT match.
            assertThat(ftsCheck.getBoolean("simple_stem_no_match"))
                .as("simple config must NOT stem: plainto_tsquery('simple','run') " +
                    "must NOT match tags='running,...' — proves tags use simple (verbatim), " +
                    "not english (stemming).  If this fails, tags are accidentally english-indexed.")
                .isFalse();

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

    // ── Test 6: RLS fail-closed — no GUC stamp → zero rows ──────────────────
    //
    // Proves that current_setting('nexus.tenant', true) returns NULL when unset,
    // and NULL ≠ any tenant_id causes the USING predicate to filter all rows.
    // The table is pre-seeded (tests 4 and 7 both insert rows before this runs
    // in JUnit's natural ordering, but test ordering is non-guaranteed; we seed
    // explicitly here so the assertion is never vacuously true against an empty table).

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        // Seed at least one row as superuser so the table is non-empty.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            insertRow(su, "failclosed-tenant", "fc-proj", "Sentinel row",
                "content for fail-closed probe", "probe");
            su.commit();
        }

        // Borrow a raw connection from the service-role datasource WITHOUT
        // calling set_config — GUC is unset, so current_setting returns NULL.
        // The USING predicate (tenant_id = NULL) is false for every row,
        // so SELECT must return zero rows even though a row exists.
        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            // Do NOT stamp the GUC — this is the unstamped-connection scenario.
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.memory");
            assertThat(rs.next()).isTrue();
            long count = rs.getLong("cnt");
            assertThat(count)
                .as("unstamped service connection must see zero rows (RLS fail-closed: " +
                    "unset GUC → NULL → no tenant_id matches NULL)")
                .isEqualTo(0L);
        }
    }

    // ── Test 7: WITH CHECK blocks cross-tenant INSERT ────────────────────────
    //
    // Proves that the WITH CHECK predicate rejects an INSERT where tenant_id
    // does not match the stamped GUC value.  This is the primary protection
    // against a buggy service layer writing rows into the wrong tenant's space.

    @Test
    void rls_withCheck_blocksCrossTenantInsert() throws Exception {
        assertThatThrownBy(() ->
            tenantScope.withTenant("gamma", ctx ->
                // tenant is stamped as 'gamma' but we try to INSERT with tenant_id='delta'
                ctx.execute(
                    "INSERT INTO nexus.memory " +
                    "(tenant_id, project, title, content, timestamp, access_count) " +
                    "VALUES (?, ?, ?, ?, now(), 0)",
                    "delta",        // tenant_id mismatch — WITH CHECK must reject
                    "gamma-proj",
                    "Cross-tenant insert attempt",
                    "this should be rejected by RLS WITH CHECK"))
        )
        .as("INSERT with tenant_id != GUC value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Test 8: WITH CHECK blocks cross-tenant tenant_id rewrite via UPDATE ──
    //
    // Proves the subtle UPDATE case: the USING predicate makes the alpha row
    // visible to the alpha session, but the WITH CHECK predicate must block the
    // attempt to rewrite tenant_id to 'beta'.  Without WITH CHECK on UPDATE,
    // a row could be silently moved into another tenant's visibility space.

    @Test
    void rls_withCheck_blocksCrossTenantTenantIdRewrite() throws Exception {
        // Seed a row for 'alpha-rw' via superuser.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            insertRow(su, "alpha-rw", "rw-proj", "Row to rewrite",
                "content", "tag");
            su.commit();
        }

        // Connecting as 'alpha-rw': the row is visible via USING (tenant_id='alpha-rw').
        // Attempt to UPDATE tenant_id to 'beta-rw' — WITH CHECK must block it.
        assertThatThrownBy(() ->
            tenantScope.withTenant("alpha-rw", ctx ->
                ctx.execute(
                    "UPDATE nexus.memory SET tenant_id = ? " +
                    "WHERE project = 'rw-proj' AND title = 'Row to rewrite'",
                    "beta-rw")   // rewrite target — WITH CHECK must reject
            )
        )
        .as("UPDATE SET tenant_id to a different value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
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
     * Uses ON CONFLICT (tenant_id, project, title) — the required three-column
     * key per the upsert contract documented in memory-001-baseline.xml.
     */
    private void insertRow(Connection su, String tenant, String project,
                           String title, String content, String tags) throws Exception {
        try (var ps = su.prepareStatement("SELECT set_config(?, ?, true)")) {
            ps.setString(1, TenantConstants.GUC_NAME);
            ps.setString(2, tenant);
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
