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
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-152 bead nexus-gmiaf.11 — Liquibase plans baseline integration test.
 *
 * <p>Hermetic: embedded Postgres (io.zonky), port 0, no Docker. Applies the
 * Liquibase master changelog programmatically and asserts all required structural
 * and runtime properties for the nexus.plans table (Store 2).
 *
 * <p>Required assertions (mirroring MemorySchemaLiquibaseTest for plans):
 * <ol>
 *   <li>plans table exists with exact column set (tenant_id + all 23 mirrored columns
 *       + fts_vector STORED generated column)</li>
 *   <li>RLS: relrowsecurity=t, relforcerowsecurity=t; pg_policies has USING + WITH CHECK</li>
 *   <li>fts_vector generated column (STORED) + GIN index exist; tokenisation config verified:
 *       match_text uses 'english', tags/project use 'simple'; english/simple DISCRIMINATION
 *       proven by negative simple-does-not-stem probe</li>
 *   <li>End-to-end RLS + FTS via TenantScope.withTenant: tenant isolation + FTS query</li>
 *   <li>S0.4 C4 defensive: rolsuper=false, rolbypassrls=false for service role</li>
 *   <li>RLS fail-closed: raw service-role connection without GUC stamp sees zero rows</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT rejected</li>
 *   <li>RLS WITH CHECK: cross-tenant tenant_id UPDATE (rewrite) rejected</li>
 * </ol>
 *
 * <p>Statistical FTS parity (top-K set equality + Spearman >= 0.90) is deferred
 * to the .9 MVV gate per the locked parity contract (nexus-gmiaf.2 rev 2).
 * This bead proves schema structure, tokenisation behaviour, and RLS enforcement only.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PlansSchemaLiquibaseTest {

    // Exact column set for nexus.plans (order-independent).
    // 23 mirrored plan_library columns + tenant_id + fts_vector = 25 total.
    private static final Set<String> EXPECTED_COLUMNS = Set.of(
        "id", "tenant_id", "project", "query", "plan_json", "outcome", "tags",
        "created_at", "ttl", "name", "verb", "scope", "dimensions",
        "default_bindings", "parent_dims", "use_count", "last_used",
        "match_count", "match_conf_sum", "success_count", "failure_count",
        "scope_tags", "match_text", "disabled_at",
        "fts_vector"
    );

    private static final String SVC_ROLE = "svc_plans_schema_test";
    private static final String SVC_PASS = "svc_plans_schema_test_pass";

    EmbeddedPostgres pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = EmbeddedPostgres.builder().start();

        // Bootstrap service role BEFORE Liquibase runs (so changeset grants find it).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
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

        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                db);
            liquibase.update(new Contexts());
        }

        // Grant svc role the same privileges as nexus_svc.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.plans TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.plans_id_seq TO " + SVC_ROLE);
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
    void plansTable_hasExactColumnSet() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet rs = su.getMetaData().getColumns(null, "nexus", "plans", null);
            Set<String> actual = new java.util.HashSet<>();
            while (rs.next()) {
                actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            }
            assertThat(actual)
                .as("nexus.plans must have exactly the mirrored + tenant + fts columns")
                .isEqualTo(EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: RLS flags and policy ─────────────────────────────────────────

    @Test
    void plansTable_rlsEnabledAndForced() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            ResultSet cls = su.createStatement().executeQuery(
                "SELECT relrowsecurity, relforcerowsecurity " +
                "FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'plans'");
            assertThat(cls.next()).as("nexus.plans must exist in pg_class").isTrue();
            assertThat(cls.getBoolean("relrowsecurity"))
                .as("relrowsecurity must be true (ENABLE ROW LEVEL SECURITY)").isTrue();
            assertThat(cls.getBoolean("relforcerowsecurity"))
                .as("relforcerowsecurity must be true (FORCE ROW LEVEL SECURITY)").isTrue();

            ResultSet pol = su.createStatement().executeQuery(
                "SELECT policyname, cmd, qual, with_check " +
                "FROM pg_policies " +
                "WHERE schemaname = 'nexus' AND tablename = 'plans'");
            assertThat(pol.next()).as("at least one RLS policy must exist on nexus.plans").isTrue();
            String polcmd    = pol.getString("cmd");
            String qual      = pol.getString("qual");
            String withCheck = pol.getString("with_check");
            assertThat(polcmd).as("policy must cover ALL commands").isEqualTo("ALL");
            assertThat(qual)
                .as("USING expression must reference tenant_id GUC check")
                .contains("current_setting");
            assertThat(withCheck)
                .as("WITH CHECK expression must reference tenant_id GUC check")
                .contains("current_setting");
        }
    }

    // ── Test 3: fts_vector generated column + GIN index + tokenisation config ─
    //
    // Plans FTS parity contract (Store 2, RDR-152):
    //   - match_text column: 'english' config (stemmed prose), weight A
    //   - tags column:       'simple'  config (verbatim identifier), weight B
    //   - project column:    'simple'  config (verbatim identifier), weight C
    //
    // Discrimination probe: 'searching' appears ONLY in match_text (english=stemmed).
    // 'planning' appears in tags (simple=verbatim). Querying stem 'search' must hit
    // english-indexed match_text. Querying stem 'plan' must NOT hit simple-indexed
    // tags='planning,...' (proves tags are simple, not english).

    @Test
    void plansTable_ftsColumnAndIndexExist_tokenisationCorrect() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            // fts_vector column: exists and is a STORED generated tsvector
            ResultSet gen = su.createStatement().executeQuery(
                "SELECT a.attname, a.attgenerated, " +
                "       pg_catalog.format_type(a.atttypid, a.atttypmod) AS col_type " +
                "FROM pg_attribute a " +
                "JOIN pg_class c ON c.oid = a.attrelid " +
                "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'plans' " +
                "  AND a.attname = 'fts_vector' AND a.attnum > 0 AND NOT a.attisdropped");
            assertThat(gen.next()).as("fts_vector column must exist on nexus.plans").isTrue();
            assertThat(gen.getString("col_type"))
                .as("fts_vector must be tsvector type").isEqualTo("tsvector");
            assertThat(gen.getString("attgenerated"))
                .as("fts_vector must be a STORED generated column (attgenerated='s')")
                .isEqualTo("s");

            // GIN index on fts_vector
            ResultSet idx = su.createStatement().executeQuery(
                "SELECT i.relname AS index_name, am.amname AS index_type, " +
                "       a.attname AS col_name " +
                "FROM pg_index ix " +
                "JOIN pg_class c  ON c.oid = ix.indrelid " +
                "JOIN pg_class i  ON i.oid = ix.indexrelid " +
                "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                "JOIN pg_am am ON am.oid = i.relam " +
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(ix.indkey) " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'plans' " +
                "  AND am.amname = 'gin' AND a.attname = 'fts_vector'");
            assertThat(idx.next()).as("GIN index on fts_vector must exist on nexus.plans").isTrue();
            assertThat(idx.getString("index_type"))
                .as("index type must be GIN").isEqualTo("gin");

            // Inspect generated column expression for tokenisation configs.
            ResultSet expr = su.createStatement().executeQuery(
                "SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS col_expr " +
                "FROM pg_attrdef d " +
                "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum " +
                "JOIN pg_class c ON c.oid = d.adrelid " +
                "JOIN pg_namespace n ON n.oid = c.relnamespace " +
                "WHERE n.nspname = 'nexus' AND c.relname = 'plans' " +
                "  AND a.attname = 'fts_vector'");
            assertThat(expr.next()).as("pg_attrdef must have entry for plans.fts_vector").isTrue();
            String colExpr = expr.getString("col_expr");
            assertThat(colExpr)
                .as("generated expression must use 'english' config for match_text column (prose)")
                .contains("english");
            assertThat(colExpr)
                .as("generated expression must use 'simple' config for tags/project (identifiers)")
                .contains("simple");
            assertThat(colExpr).as("must include setweight 'A' for match_text").contains("'A'");
            assertThat(colExpr).as("must include setweight 'B' for tags").contains("'B'");
            assertThat(colExpr).as("must include setweight 'C' for project").contains("'C'");
        }

        // Behaviour probe: verify tokenisation, not just DDL strings.
        //
        // 'searches' in match_text → english stems to 'search'; querying 'searching' (same stem)
        //   must match (positive english probe).
        // 'planning' in tags → simple stores verbatim; exact query 'planning' must match
        //   (positive simple exact probe).
        // 'plan' is the english stem of 'planning', but simple does NOT stem, so
        //   plainto_tsquery('simple','plan') → literal 'plan' must NOT match 'planning'
        //   in simple-indexed tags (negative discrimination probe — proves tags use simple
        //   not english; if tags were english-indexed, 'planning'→'plan' and query would match).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            try (var ps = su.prepareStatement("SELECT set_config(?, ?, true)")) {
                ps.setString(1, TenantConstants.GUC_NAME);
                ps.setString(2, "fts-probe-tenant");
                ps.execute();
            }
            su.createStatement().execute(
                "INSERT INTO nexus.plans " +
                "(tenant_id, project, query, plan_json, outcome, tags, match_text, created_at) " +
                "VALUES " +
                "('fts-probe-tenant', 'probe-proj', 'FTS discrimination probe query'," +
                " '{\"steps\":[]}', 'success', 'planning,rdr', " +
                " 'How to perform searches across knowledge repositories', now())");

            ResultSet ftsCheck = su.createStatement().executeQuery(
                // (1) Positive english: 'searching' and 'searches' share stem 'search'.
                //     match_text is indexed under english so query for stem must match.
                "SELECT fts_vector @@ plainto_tsquery('english', 'searching') AS english_stem_match, " +
                // (2) Positive simple exact: tags='planning,...'; simple stores verbatim.
                "       fts_vector @@ plainto_tsquery('simple', 'planning')   AS simple_exact_match, " +
                // (3) NEGATIVE discrimination: 'plan' is the english stem of 'planning'.
                //     Under simple, 'planning' is stored as-is (not stemmed).
                //     plainto_tsquery('simple','plan') → literal 'plan', must NOT match 'planning'.
                //     If this fails (returns true), tags are accidentally english-indexed.
                "       fts_vector @@ plainto_tsquery('simple', 'plan')       AS simple_stem_no_match " +
                "FROM nexus.plans " +
                "WHERE tenant_id = 'fts-probe-tenant' AND project = 'probe-proj'");

            assertThat(ftsCheck.next()).as("probe row must be retrievable from nexus.plans").isTrue();

            assertThat(ftsCheck.getBoolean("english_stem_match"))
                .as("english config must stem: 'searching' and 'searches' share stem 'search'; " +
                    "match_text indexed under english so query matches")
                .isTrue();

            assertThat(ftsCheck.getBoolean("simple_exact_match"))
                .as("simple config must match exact token: 'planning' stored verbatim in tags")
                .isTrue();

            // Discrimination: if tags were indexed under english, 'planning'→'plan' and
            // plainto_tsquery('simple','plan') → literal 'plan' would match the stored stem.
            // Under correct simple indexing, 'planning' is stored as 'planning', not 'plan',
            // so the query must NOT match.
            assertThat(ftsCheck.getBoolean("simple_stem_no_match"))
                .as("simple config must NOT stem: plainto_tsquery('simple','plan') " +
                    "must NOT match tags='planning,...' — proves tags use simple (verbatim), " +
                    "not english (stemming).  Failure here means tags are accidentally english-indexed.")
                .isFalse();

            su.rollback();
        }
    }

    // ── Test 4: end-to-end RLS + FTS via TenantScope ─────────────────────────

    @Test
    void tenantIsolation_and_ftsQuery_viaWithTenant() throws Exception {
        // Seed rows for two tenants via superuser (bypasses RLS for seeding).
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            insertPlan(su, "plan-alpha", "plan-proj", "How to search knowledge bases",
                "knowledge,search", "How to search knowledge bases. research scope global");
            insertPlan(su, "plan-alpha", "plan-proj", "Walk from code to docs",
                "code,navigation", "Walk from code to docs. navigate scope global");
            insertPlan(su, "plan-alpha", "plan-proj", "Research entity resolution strategies",
                "research,entity", "Research entity resolution. resolve scope global");
            insertPlan(su, "plan-beta",  "plan-proj2", "Compile and deploy Java services",
                "java,deploy", "Compile and deploy Java services. build scope ops");
            su.commit();
        }

        // tenant plan-alpha sees exactly its 3 rows.
        var alphaTitles = tenantScope.withTenant("plan-alpha", ctx ->
            ctx.fetch("SELECT query FROM nexus.plans WHERE project = 'plan-proj' ORDER BY query")
               .getValues("query", String.class));
        assertThat(alphaTitles)
            .as("tenant plan-alpha must see exactly its 3 plans")
            .containsExactlyInAnyOrder(
                "How to search knowledge bases",
                "Walk from code to docs",
                "Research entity resolution strategies");
        assertThat(alphaTitles)
            .as("tenant plan-alpha must NOT see plan-beta's plan")
            .doesNotContain("Compile and deploy Java services");

        // tenant plan-beta sees only its 1 row.
        var betaTitles = tenantScope.withTenant("plan-beta", ctx ->
            ctx.fetch("SELECT query FROM nexus.plans WHERE project = 'plan-proj2' ORDER BY query")
               .getValues("query", String.class));
        assertThat(betaTitles)
            .as("tenant plan-beta must see exactly its 1 plan")
            .containsExactly("Compile and deploy Java services");
        assertThat(betaTitles)
            .as("tenant plan-beta must NOT see any of plan-alpha's plans")
            .doesNotContain("How to search knowledge bases", "Walk from code to docs",
                            "Research entity resolution strategies");

        // FTS query scoped to plan-alpha: 'resolving' (english→stem 'resolv') must match
        // the entity resolution plan's match_text but not the search/walk plans.
        var ftsAlpha = tenantScope.withTenant("plan-alpha", ctx ->
            ctx.fetch(
                "SELECT query FROM nexus.plans " +
                "WHERE fts_vector @@ plainto_tsquery('english', 'resolving') " +
                "ORDER BY query")
               .getValues("query", String.class));
        assertThat(ftsAlpha)
            .as("FTS query for 'resolving' (english stem 'resolv') under plan-alpha " +
                "must match entity resolution plan only")
            .containsExactly("Research entity resolution strategies");

        // FTS query scoped to plan-beta: 'java' in simple (tag) config matches.
        var ftsBeta = tenantScope.withTenant("plan-beta", ctx ->
            ctx.fetch(
                "SELECT query FROM nexus.plans " +
                "WHERE fts_vector @@ plainto_tsquery('simple', 'java') " +
                "ORDER BY query")
               .getValues("query", String.class));
        assertThat(ftsBeta)
            .as("FTS query for 'java' (simple/tags) under plan-beta must match Java plan")
            .containsExactly("Compile and deploy Java services");

        // Cross-tenant FTS isolation: 'researching' under plan-beta must return nothing.
        var crossTenantFts = tenantScope.withTenant("plan-beta", ctx ->
            ctx.fetch(
                "SELECT query FROM nexus.plans " +
                "WHERE fts_vector @@ plainto_tsquery('english', 'researching')")
               .getValues("query", String.class));
        assertThat(crossTenantFts)
            .as("FTS 'researching' under plan-beta must return empty (cross-tenant isolation)")
            .isEmpty();
    }

    // ── Test 5: service role defensive — not superuser, not bypassrls ─────────

    @Test
    void serviceRole_notSuperuserNotBypassRls() {
        tenantScope.withTenant("test-tenant", ctx -> {
            var row = ctx.fetchOne(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user");
            assertThat(row).as("pg_roles row for current_user must exist").isNotNull();
            assertThat(row.get("rolsuper", Boolean.class))
                .as("service role must NOT be superuser").isFalse();
            assertThat(row.get("rolbypassrls", Boolean.class))
                .as("service role must NOT have BYPASSRLS").isFalse();
            return null;
        });
    }

    // ── Test 6: RLS fail-closed — no GUC stamp → zero rows ──────────────────

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        // Seed at least one plan row as superuser so table is non-empty.
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            insertPlan(su, "failclosed-tenant", "fc-proj",
                "Sentinel plan for fail-closed probe", "sentinel", "Sentinel plan");
            su.commit();
        }

        // Raw service-role connection WITHOUT GUC stamp.
        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            ResultSet rs = svc.createStatement().executeQuery(
                "SELECT COUNT(*) AS cnt FROM nexus.plans");
            assertThat(rs.next()).isTrue();
            long count = rs.getLong("cnt");
            assertThat(count)
                .as("unstamped service connection must see zero plans rows " +
                    "(RLS fail-closed: unset GUC → NULL → no tenant_id matches NULL)")
                .isEqualTo(0L);
        }
    }

    // ── Test 7: WITH CHECK blocks cross-tenant INSERT ────────────────────────

    @Test
    void rls_withCheck_blocksCrossTenantInsert() {
        assertThatThrownBy(() ->
            tenantScope.withTenant("gamma-plans", ctx ->
                ctx.execute(
                    "INSERT INTO nexus.plans " +
                    "(tenant_id, project, query, plan_json, outcome, match_text, created_at) " +
                    "VALUES (?, ?, ?, ?, ?, ?, now())",
                    "delta-plans",         // tenant_id mismatch — WITH CHECK must reject
                    "gamma-proj",
                    "Cross-tenant insert attempt",
                    "{}",
                    "success",
                    "this should be rejected by RLS WITH CHECK"))
        )
        .as("INSERT with tenant_id != GUC value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Test 8: WITH CHECK blocks cross-tenant tenant_id UPDATE rewrite ──────

    @Test
    void rls_withCheck_blocksCrossTenantTenantIdRewrite() throws Exception {
        try (Connection su = pg.getPostgresDatabase().getConnection()) {
            su.setAutoCommit(false);
            insertPlan(su, "alpha-plans-rw", "rw-proj", "Plan to rewrite", "rw", "Plan to rewrite");
            su.commit();
        }

        assertThatThrownBy(() ->
            tenantScope.withTenant("alpha-plans-rw", ctx ->
                ctx.execute(
                    "UPDATE nexus.plans SET tenant_id = ? " +
                    "WHERE project = 'rw-proj' AND query = 'Plan to rewrite'",
                    "beta-plans-rw")   // rewrite target — WITH CHECK must reject
            )
        )
        .as("UPDATE SET tenant_id to a different value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl("jdbc:postgresql://localhost:" + pg.getPort() + "/postgres");
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }

    /**
     * Insert a plans row via superuser connection (bypasses RLS for seeding).
     * Stamps the GUC so FORCE RLS WITH CHECK does not block the superuser insert.
     */
    private void insertPlan(Connection su, String tenant, String project,
                            String query, String tags, String matchText) throws Exception {
        try (var ps = su.prepareStatement("SELECT set_config(?, ?, true)")) {
            ps.setString(1, TenantConstants.GUC_NAME);
            ps.setString(2, tenant);
            ps.execute();
        }
        try (var ps = su.prepareStatement(
                "INSERT INTO nexus.plans " +
                "(tenant_id, project, query, plan_json, outcome, tags, match_text, created_at) " +
                "VALUES (?, ?, ?, ?, ?, ?, ?, now()) " +
                "ON CONFLICT (tenant_id, project, query) DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, project);
            ps.setString(3, query);
            ps.setString(4, "{\"steps\":[]}");
            ps.setString(5, "success");
            ps.setString(6, tags);
            ps.setString(7, matchText);
            ps.executeUpdate();
        }
    }
}
