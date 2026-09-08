package dev.nexus.service;

import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.jooq.impl.DSL.condition;
import static org.jooq.impl.DSL.val;

/**
 * RDR-152 bead nexus-gmiaf.5 — Liquibase memory baseline integration test.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires Docker. Applies the
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
        "content", "tags", "timestamp", "ttl_days", "access_count", "last_accessed",
        "fts_vector"
    );

    // Service role created by @BeforeAll — plain LOGIN, no superuser, no bypassrls.
    private static final String SVC_ROLE = "svc_memory_test";
    private static final String SVC_PASS = "svc_memory_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // Bootstrap service role BEFORE Liquibase runs (so changeset 5 finds it).
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
    void memoryTable_hasExactColumnSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
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
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // pg_class flags
            PgCatalogProbes.RowSecurity cls = PgCatalogProbes.rowSecurity(ctx, "nexus", "memory");
            assertThat(cls).as("nexus.memory must exist in pg_class").isNotNull();
            assertThat(cls.enabled())
                .as("relrowsecurity must be true (ENABLE ROW LEVEL SECURITY)").isTrue();
            assertThat(cls.forced())
                .as("relforcerowsecurity must be true (FORCE ROW LEVEL SECURITY)").isTrue();

            // pg_policies: expect exactly one policy covering both USING and WITH CHECK
            List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "memory");
            assertThat(policies).as("at least one RLS policy must exist on nexus.memory").isNotEmpty();
            PgCatalogProbes.Policy pol = policies.get(0);
            String polcmd    = pol.cmd();
            String qual      = pol.qual();
            String withCheck = pol.withCheck();
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
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // fts_vector column exists and is a generated stored column
            PgCatalogProbes.GeneratedColumn gen =
                PgCatalogProbes.generatedColumn(ctx, "nexus", "memory", "fts_vector");
            assertThat(gen).as("fts_vector column must exist").isNotNull();
            assertThat(gen.colType())
                .as("fts_vector must be tsvector type").isEqualTo("tsvector");
            // attgenerated='s' means STORED generated column (PostgreSQL 12+)
            assertThat(gen.attgenerated())
                .as("fts_vector must be a STORED generated column (attgenerated='s')")
                .isEqualTo("s");

            // GIN index exists on fts_vector
            assertThat(PgCatalogProbes.indexCountOnColumn(ctx, "nexus", "memory", "gin", "fts_vector"))
                .as("GIN index on fts_vector must exist").isPositive();

            // Inspect generated column expression to verify tokenisation configs.
            String colExpr = PgCatalogProbes.columnExpression(ctx, "nexus", "memory", "fts_vector");
            assertThat(colExpr).as("pg_attrdef must have entry for fts_vector").isNotNull();
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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(TenantConstants.GUC_NAME), DSL.val("probe-tenant"), DSL.inline(true))).fetch();

            ctx.insertInto(MEMORY, MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT,
                    MEMORY.TAGS, MEMORY.TIMESTAMP, MEMORY.ACCESS_COUNT)
               .values("probe-tenant", "probe-proj", "Quantum mechanics overview",
                   "wave functions superposition entanglement", "running,distributed",
                   OffsetDateTime.now(), 0)
               .execute();

            // @@ / plainto_tsquery have no typed jOOQ operator/function form (same house
            // idiom as PlanRepository/MemoryRepository's own FTS predicates) -- a bare,
            // statically-imported condition() template, never DSL.condition(...) qualified
            // (which the scanDslTemplates gate flags as assembled SQL text).
            var ftsCheck = ctx.select(
                    // (1) Positive english: 'mechanics' stems to 'mechan'; 'mechanic' also
                    //     stems to 'mechan' under english.  Title is indexed under english,
                    //     so the stem query must match.
                    DSL.field(condition("fts_vector @@ plainto_tsquery('english', {0})", val("mechanic"))),
                    // (2) Positive simple exact: tags='running,...'; simple stores verbatim.
                    DSL.field(condition("fts_vector @@ plainto_tsquery('simple', {0})", val("running"))),
                    // (3) NEGATIVE discrimination: 'run' is the english stem of 'running'.
                    //     Under simple, 'running' is stored as-is (no stemming), so querying
                    //     the stem 'run' must NOT match.  Proves tags≠english.
                    DSL.field(condition("fts_vector @@ plainto_tsquery('simple', {0})", val("run"))))
                .from(MEMORY)
                .where(MEMORY.TENANT_ID.eq("probe-tenant")
                    .and(MEMORY.TITLE.eq("Quantum mechanics overview")))
                .fetchOne();

            assertThat(ftsCheck).as("probe row must be retrievable").isNotNull();

            assertThat(ftsCheck.value1())
                .as("english config must stem: 'mechanic' and 'mechanics' share stem 'mechan'; " +
                    "title is indexed under english so query matches")
                .isTrue();

            assertThat(ftsCheck.value2())
                .as("simple config must match exact token: 'running' stored verbatim in tags")
                .isTrue();

            // This is the discrimination assertion: if tags were indexed under english
            // instead of simple, 'running' would be stored as 'run' (stem) and
            // plainto_tsquery('simple','run') → literal 'run' would match.
            // Under correct simple indexing, 'running' ≠ 'run', so it must NOT match.
            assertThat(ftsCheck.value3())
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
        try (Connection su = pg.createConnection("")) {
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
            ctx.select(MEMORY.TITLE).from(MEMORY).where(MEMORY.PROJECT.eq("alpha-proj"))
               .orderBy(MEMORY.TITLE).fetch(MEMORY.TITLE));
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
            ctx.select(MEMORY.TITLE).from(MEMORY).where(MEMORY.PROJECT.eq("beta-proj"))
               .orderBy(MEMORY.TITLE).fetch(MEMORY.TITLE));
        assertThat(betaTitles)
            .as("tenant-beta must see exactly its 1 row")
            .containsExactly("Rust ownership model");
        assertThat(betaTitles)
            .as("tenant-beta must NOT see any of alpha's rows")
            .doesNotContain("Machine learning basics", "Python type hints", "Database indexing strategies");

        // FTS query scoped to tenant-alpha: search for 'neural' (english→'neural' retained)
        List<String> ftsAlpha = tenantScope.withTenant("alpha", ctx ->
            ctx.select(MEMORY.TITLE).from(MEMORY)
               .where(condition("fts_vector @@ plainto_tsquery('english', {0})", val("neural")))
               .orderBy(MEMORY.TITLE).fetch(MEMORY.TITLE));
        assertThat(ftsAlpha)
            .as("FTS query for 'neural' under tenant-alpha must match ML row only")
            .containsExactly("Machine learning basics");

        // FTS query scoped to tenant-beta: 'rust' in simple (tag) config
        List<String> ftsBeta = tenantScope.withTenant("beta", ctx ->
            ctx.select(MEMORY.TITLE).from(MEMORY)
               .where(condition("fts_vector @@ plainto_tsquery('simple', {0})", val("rust")))
               .orderBy(MEMORY.TITLE).fetch(MEMORY.TITLE));
        assertThat(ftsBeta)
            .as("FTS query for 'rust' (simple/tags) under tenant-beta must match Rust row")
            .containsExactly("Rust ownership model");

        // Cross-tenant FTS isolation: 'neural' under beta must return nothing
        List<String> ftsAlphaUnderBeta = tenantScope.withTenant("beta", ctx ->
            ctx.select(MEMORY.TITLE).from(MEMORY)
               .where(condition("fts_vector @@ plainto_tsquery('english', {0})", val("neural")))
               .orderBy(MEMORY.TITLE).fetch(MEMORY.TITLE));
        assertThat(ftsAlphaUnderBeta)
            .as("FTS query for 'neural' under tenant-beta must return empty (cross-tenant isolation)")
            .isEmpty();
    }

    // ── Test 5: S0.4 C4 defensive — rolsuper=false, rolbypassrls=false ───────

    @Test
    void serviceRole_notSuperuserNotBypassRls() throws Exception {
        tenantScope.withTenant("test-tenant", ctx -> {
            PgCatalogProbes.RoleFlags row = PgCatalogProbes.currentRoleFlags(ctx);
            assertThat(row).as("pg_roles row for current_user must exist").isNotNull();
            assertThat(row.superuser())
                .as("service role must NOT be superuser (would bypass RLS entirely)")
                .isFalse();
            assertThat(row.bypassRls())
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
        try (Connection su = pg.createConnection("")) {
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
            Long count = DSL.using(svc, SQLDialect.POSTGRES)
                .selectCount().from(MEMORY).fetchOne(0, Long.class);
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
                ctx.insertInto(MEMORY, MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT,
                        MEMORY.TIMESTAMP, MEMORY.ACCESS_COUNT)
                   .values("delta",        // tenant_id mismatch — WITH CHECK must reject
                       "gamma-proj",
                       "Cross-tenant insert attempt",
                       "this should be rejected by RLS WITH CHECK",
                       OffsetDateTime.now(), 0)
                   .execute())
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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "alpha-rw", "rw-proj", "Row to rewrite",
                "content", "tag");
            su.commit();
        }

        // Connecting as 'alpha-rw': the row is visible via USING (tenant_id='alpha-rw').
        // Attempt to UPDATE tenant_id to 'beta-rw' — WITH CHECK must block it.
        assertThatThrownBy(() ->
            tenantScope.withTenant("alpha-rw", ctx ->
                ctx.update(MEMORY)
                   .set(MEMORY.TENANT_ID, "beta-rw")   // rewrite target — WITH CHECK must reject
                   .where(MEMORY.PROJECT.eq("rw-proj").and(MEMORY.TITLE.eq("Row to rewrite")))
                   .execute()
            )
        )
        .as("UPDATE SET tenant_id to a different value must be rejected by RLS WITH CHECK")
        .isInstanceOf(Exception.class)
        .hasMessageContaining("violates row-level security policy");
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private com.zaxxer.hikari.HikariDataSource buildSvcDataSource() {
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
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
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
            DSL.val(TenantConstants.GUC_NAME), DSL.val(tenant), DSL.inline(true))).fetch();
        ctx.insertInto(MEMORY, MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE, MEMORY.CONTENT,
                MEMORY.TAGS, MEMORY.TIMESTAMP, MEMORY.ACCESS_COUNT)
           .values(tenant, project, title, content, tags, OffsetDateTime.now(), 0)
           .onConflict(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE)
           .doNothing()
           .execute();
    }
}
