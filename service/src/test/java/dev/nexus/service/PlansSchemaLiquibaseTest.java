package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.SQLDialect;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.PLANS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.jooq.impl.DSL.condition;
import static org.jooq.impl.DSL.val;

/**
 * RDR-152 bead nexus-gmiaf.11 — Liquibase plans baseline integration test.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires Docker. Applies the
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
        "created_at", "ttl_days", "name", "verb", "scope", "dimensions",
        "default_bindings", "parent_dims", "use_count", "last_used",
        "match_count", "match_conf_sum", "success_count", "failure_count",
        "scope_tags", "match_text", "disabled_at",
        "fts_vector"
    );

    private static final String SVC_ROLE = "svc_plans_schema_test";
    private static final String SVC_PASS = "svc_plans_schema_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Bootstrap service role BEFORE Liquibase runs (so changeset grants find it).
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        svcDs = buildSvcDataSource();
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ── Test 1: exact column set ─────────────────────────────────────────────

    @Test
    void plansTable_hasExactColumnSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
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
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity cls = PgCatalogProbes.rowSecurity(ctx, "nexus", "plans");
            assertThat(cls).as("nexus.plans must exist in pg_class").isNotNull();
            assertThat(cls.enabled())
                .as("relrowsecurity must be true (ENABLE ROW LEVEL SECURITY)").isTrue();
            assertThat(cls.forced())
                .as("relforcerowsecurity must be true (FORCE ROW LEVEL SECURITY)").isTrue();

            java.util.List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "plans");
            assertThat(policies).as("at least one RLS policy must exist on nexus.plans").isNotEmpty();
            PgCatalogProbes.Policy pol = policies.get(0);
            String polcmd    = pol.cmd();
            String qual      = pol.qual();
            String withCheck = pol.withCheck();
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
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // fts_vector column: exists and is a STORED generated tsvector
            PgCatalogProbes.GeneratedColumn gen =
                PgCatalogProbes.generatedColumn(ctx, "nexus", "plans", "fts_vector");
            assertThat(gen).as("fts_vector column must exist on nexus.plans").isNotNull();
            assertThat(gen.colType())
                .as("fts_vector must be tsvector type").isEqualTo("tsvector");
            assertThat(gen.attgenerated())
                .as("fts_vector must be a STORED generated column (attgenerated='s')")
                .isEqualTo("s");

            // GIN index on fts_vector
            assertThat(PgCatalogProbes.indexCountOnColumn(ctx, "nexus", "plans", "gin", "fts_vector"))
                .as("GIN index on fts_vector must exist on nexus.plans").isPositive();

            // Inspect generated column expression for tokenisation configs.
            String colExpr = PgCatalogProbes.columnExpression(ctx, "nexus", "plans", "fts_vector");
            assertThat(colExpr).as("pg_attrdef must have entry for plans.fts_vector").isNotNull();
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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(TenantConstants.GUC_NAME), DSL.val("fts-probe-tenant"), DSL.inline(true))).fetch();

            // verb: plans.verb is NOT NULL (hygiene-001-11).
            ctx.insertInto(PLANS, PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY, PLANS.PLAN_JSON,
                    PLANS.OUTCOME, PLANS.TAGS, PLANS.MATCH_TEXT, PLANS.VERB, PLANS.CREATED_AT)
               .values("fts-probe-tenant", "probe-proj", "FTS discrimination probe query",
                   JSONB.valueOf("{\"steps\":[]}"), "success", "planning,rdr",
                   "How to perform searches across knowledge repositories", "research",
                   OffsetDateTime.now())
               .execute();

            // @@ / plainto_tsquery have no typed jOOQ operator/function form (same house
            // idiom as PlanRepository/MemoryRepository's own FTS predicates) -- a bare,
            // statically-imported condition() template, never DSL.condition(...) qualified
            // (which the scanDslTemplates gate flags as assembled SQL text).
            var ftsCheck = ctx.select(
                    // (1) Positive english: 'searching' and 'searches' share stem 'search'.
                    //     match_text is indexed under english so query for stem must match.
                    DSL.field(condition("fts_vector @@ plainto_tsquery('english', {0})", val("searching"))),
                    // (2) Positive simple exact: tags='planning,...'; simple stores verbatim.
                    DSL.field(condition("fts_vector @@ plainto_tsquery('simple', {0})", val("planning"))),
                    // (3) NEGATIVE discrimination: 'plan' is the english stem of 'planning'.
                    //     Under simple, 'planning' is stored as-is (not stemmed).
                    //     plainto_tsquery('simple','plan') → literal 'plan', must NOT match 'planning'.
                    //     If this fails (returns true), tags are accidentally english-indexed.
                    DSL.field(condition("fts_vector @@ plainto_tsquery('simple', {0})", val("plan"))))
                .from(PLANS)
                .where(PLANS.TENANT_ID.eq("fts-probe-tenant").and(PLANS.PROJECT.eq("probe-proj")))
                .fetchOne();

            assertThat(ftsCheck).as("probe row must be retrievable from nexus.plans").isNotNull();

            assertThat(ftsCheck.value1())
                .as("english config must stem: 'searching' and 'searches' share stem 'search'; " +
                    "match_text indexed under english so query matches")
                .isTrue();

            assertThat(ftsCheck.value2())
                .as("simple config must match exact token: 'planning' stored verbatim in tags")
                .isTrue();

            // Discrimination: if tags were indexed under english, 'planning'→'plan' and
            // plainto_tsquery('simple','plan') → literal 'plan' would match the stored stem.
            // Under correct simple indexing, 'planning' is stored as 'planning', not 'plan',
            // so the query must NOT match.
            assertThat(ftsCheck.value3())
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
        try (Connection su = pg.createConnection("")) {
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
            ctx.select(PLANS.QUERY).from(PLANS).where(PLANS.PROJECT.eq("plan-proj"))
               .orderBy(PLANS.QUERY).fetch(PLANS.QUERY));
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
            ctx.select(PLANS.QUERY).from(PLANS).where(PLANS.PROJECT.eq("plan-proj2"))
               .orderBy(PLANS.QUERY).fetch(PLANS.QUERY));
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
            ctx.select(PLANS.QUERY).from(PLANS)
               .where(condition("fts_vector @@ plainto_tsquery('english', {0})", val("resolving")))
               .orderBy(PLANS.QUERY).fetch(PLANS.QUERY));
        assertThat(ftsAlpha)
            .as("FTS query for 'resolving' (english stem 'resolv') under plan-alpha " +
                "must match entity resolution plan only")
            .containsExactly("Research entity resolution strategies");

        // FTS query scoped to plan-beta: 'java' in simple (tag) config matches.
        var ftsBeta = tenantScope.withTenant("plan-beta", ctx ->
            ctx.select(PLANS.QUERY).from(PLANS)
               .where(condition("fts_vector @@ plainto_tsquery('simple', {0})", val("java")))
               .orderBy(PLANS.QUERY).fetch(PLANS.QUERY));
        assertThat(ftsBeta)
            .as("FTS query for 'java' (simple/tags) under plan-beta must match Java plan")
            .containsExactly("Compile and deploy Java services");

        // Cross-tenant FTS isolation: 'researching' under plan-beta must return nothing.
        var crossTenantFts = tenantScope.withTenant("plan-beta", ctx ->
            ctx.select(PLANS.QUERY).from(PLANS)
               .where(condition("fts_vector @@ plainto_tsquery('english', {0})", val("researching")))
               .fetch(PLANS.QUERY));
        assertThat(crossTenantFts)
            .as("FTS 'researching' under plan-beta must return empty (cross-tenant isolation)")
            .isEmpty();
    }

    // ── Test 5: service role defensive — not superuser, not bypassrls ─────────

    @Test
    void serviceRole_notSuperuserNotBypassRls() {
        tenantScope.withTenant("test-tenant", ctx -> {
            PgCatalogProbes.RoleFlags row = PgCatalogProbes.currentRoleFlags(ctx);
            assertThat(row).as("pg_roles row for current_user must exist").isNotNull();
            assertThat(row.superuser())
                .as("service role must NOT be superuser").isFalse();
            assertThat(row.bypassRls())
                .as("service role must NOT have BYPASSRLS").isFalse();
            return null;
        });
    }

    // ── Test 6: RLS fail-closed — no GUC stamp → zero rows ──────────────────

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        // Seed at least one plan row as superuser so table is non-empty.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertPlan(su, "failclosed-tenant", "fc-proj",
                "Sentinel plan for fail-closed probe", "sentinel", "Sentinel plan");
            su.commit();
        }

        // Raw service-role connection WITHOUT GUC stamp.
        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            Long count = DSL.using(svc, SQLDialect.POSTGRES)
                .selectCount().from(PLANS).fetchOne(0, Long.class);
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
                // verb supplied (plans.verb NOT NULL, hygiene-001-11) so the
                // RLS WITH CHECK violation this test is proving is what
                // actually fires, not an unrelated NOT NULL violation.
                ctx.insertInto(PLANS, PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY, PLANS.PLAN_JSON,
                        PLANS.OUTCOME, PLANS.MATCH_TEXT, PLANS.VERB, PLANS.CREATED_AT)
                   .values("delta-plans",         // tenant_id mismatch — WITH CHECK must reject
                       "gamma-proj",
                       "Cross-tenant insert attempt",
                       JSONB.valueOf("{}"),
                       "success",
                       "this should be rejected by RLS WITH CHECK",
                       "research",
                       OffsetDateTime.now())
                   .execute())
        )
        .as("INSERT with tenant_id != GUC value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Test 8: WITH CHECK blocks cross-tenant tenant_id UPDATE rewrite ──────

    @Test
    void rls_withCheck_blocksCrossTenantTenantIdRewrite() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertPlan(su, "alpha-plans-rw", "rw-proj", "Plan to rewrite", "rw", "Plan to rewrite");
            su.commit();
        }

        assertThatThrownBy(() ->
            tenantScope.withTenant("alpha-plans-rw", ctx ->
                ctx.update(PLANS)
                   .set(PLANS.TENANT_ID, "beta-plans-rw")   // rewrite target — WITH CHECK must reject
                   .where(PLANS.PROJECT.eq("rw-proj").and(PLANS.QUERY.eq("Plan to rewrite")))
                   .execute()
            )
        )
        .as("UPDATE SET tenant_id to a different value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
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
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
            DSL.val(TenantConstants.GUC_NAME), DSL.val(tenant), DSL.inline(true))).fetch();
        // verb: plans.verb is NOT NULL (hygiene-001-11, nexus-tk070.p6a follow-on) --
        // every insertPlan call now supplies one. plan_json is a typed JSONB column
        // (plans-002-jsonb.xml) -- JSONB.valueOf renders the correct cast, no raw
        // ?::jsonb placeholder needed (nexus-cbo4a).
        ctx.insertInto(PLANS, PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY, PLANS.PLAN_JSON,
                PLANS.OUTCOME, PLANS.TAGS, PLANS.MATCH_TEXT, PLANS.VERB, PLANS.CREATED_AT)
           .values(tenant, project, query, JSONB.valueOf("{\"steps\":[]}"), "success", tags,
               matchText, "research", OffsetDateTime.now())
           .onConflict(PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY)
           .doNothing()
           .execute();
    }
}
