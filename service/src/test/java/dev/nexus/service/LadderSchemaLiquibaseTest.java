package dev.nexus.service;

import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record2;
import org.jooq.Record3;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.Table;
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
import java.time.ZoneOffset;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.LADDER_COMPLETIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-186 bead nexus-146xx.10 — Liquibase ladder_completions baseline test.
 *
 * <p>The PG home for upgrade-ladder rung completion bookkeeping (RDR-186 D3:
 * derive-first, record-late). Mirrors the client-side {@code ladder.db}
 * {@code rung_completions} schema ({@code src/nexus/upgrade_ladder/completion.py})
 * 1:1 with tenant_id added: one durable "verified" fact per rung. First-class
 * relation per the RDR-154 bias (Hal's Q5 relaxation permits a transitional KV
 * facility, but the relation is the same effort here so the bias stands).
 *
 * <p>Completion records are position BOOKKEEPING, not truth (RDR-142 / RF-186-2):
 * ladder position is DERIVED at read time from these rows; there is no stored
 * position and no setter, and audit metadata (verified_at / package_version)
 * is observability-only, accepted lossy across the transition.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires
 * Docker. Applies the Liquibase master changelog and asserts:
 * <ol>
 *   <li>ladder_completions exists with EXACTLY the mirrored + tenant columns</li>
 *   <li>RLS: relrowsecurity=t, relforcerowsecurity=t; policy USING + WITH CHECK
 *       on the nexus.tenant GUC</li>
 *   <li>PK is (tenant_id, rung_name) — the SQLite PK plus tenant</li>
 *   <li>detail defaults to '' (mirrors SQLite DEFAULT '')</li>
 *   <li>tenant isolation end-to-end via TenantScope</li>
 *   <li>BYPASSRLS (superuser) sees all tenants' rows</li>
 *   <li>RLS fail-closed: unstamped service connection sees zero rows</li>
 *   <li>RLS WITH CHECK: cross-tenant INSERT rejected</li>
 * </ol>
 *
 * <p>nexus-cbo4a batch 6 (Sam's no-raw-SQL-in-Java directive, nexus-zrcj7): every
 * remaining raw {@code execute}/{@code fetch}/{@code prepareStatement} call is
 * retired onto the generated {@code LADDER_COMPLETIONS} jOOQ table (a real
 * product table has codegen — no {@code DSL.table(DSL.name(...))} fallback
 * needed here) or typed {@code DSL.table(DSL.name(...))}/{@code
 * DSL.field(DSL.name(...), Class)} composition over the pg_catalog views
 * ({@code pg_class}/{@code pg_namespace}/{@code pg_policies}) that have no
 * jOOQ codegen. {@code su.getMetaData().getColumns}/{@code getPrimaryKeys}
 * (tests 1 and 3) are plain JDBC {@code DatabaseMetaData} calls, not raw SQL
 * text, and were never flagged — left untouched.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LadderSchemaLiquibaseTest {

    private static final Set<String> EXPECTED_COLUMNS = Set.of(
        "tenant_id", "rung_name", "verified_at", "package_version", "detail"
    );

    private static final String SVC_ROLE = "svc_ladder_schema_test";
    private static final String SVC_PASS = "svc_ladder_schema_test_pass";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

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
        if (pg    != null) pg.stop();
    }

    // ── Test 1: exact column set ─────────────────────────────────────────────

    @Test
    void ladderTable_hasExactColumnSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getColumns(null, "nexus", "ladder_completions", null);
            Set<String> actual = new java.util.HashSet<>();
            while (rs.next()) actual.add(rs.getString("COLUMN_NAME").toLowerCase());
            assertThat(actual)
                .as("nexus.ladder_completions must have exactly the mirrored + tenant " +
                    "columns. NO position column ever: ladder position is DERIVED " +
                    "(derive_ladder_position, completion.py) — a stored position is the " +
                    "RDR-142 bug class the Gap-4 pin makes unrepresentable.")
                .isEqualTo(EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: RLS flags and policy ─────────────────────────────────────────

    @Test
    void ladderTable_rlsEnabledForcedAndPolicyOnTenantGuc() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            Table<?> pgClass = DSL.table(DSL.name("pg_class")).as("c");
            Table<?> pgNamespace = DSL.table(DSL.name("pg_namespace")).as("n");
            Field<Boolean> relrowsecurity = DSL.field(DSL.name("c", "relrowsecurity"), Boolean.class);
            Field<Boolean> relforcerowsecurity =
                DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class);
            Field<Object> relnamespace = DSL.field(DSL.name("c", "relnamespace"));
            Field<Object> nsOid = DSL.field(DSL.name("n", "oid"));
            Field<String> nspname = DSL.field(DSL.name("n", "nspname"), String.class);
            Field<String> relname = DSL.field(DSL.name("c", "relname"), String.class);

            Record2<Boolean, Boolean> cls = ctx.select(relrowsecurity, relforcerowsecurity)
                .from(pgClass)
                .join(pgNamespace).on(relnamespace.eq(nsOid))
                .where(nspname.eq("nexus")).and(relname.eq("ladder_completions"))
                .fetchOne();
            assertThat(cls).as("nexus.ladder_completions must exist in pg_class").isNotNull();
            assertThat(cls.value1()).as("RLS must be ENABLED").isTrue();
            assertThat(cls.value2()).as("RLS must be FORCED (owner is subject to policy too)").isTrue();

            Table<?> pgPolicies = DSL.table(DSL.name("pg_policies"));
            Field<String> policyname = DSL.field(DSL.name("policyname"), String.class);
            Field<String> qual = DSL.field(DSL.name("qual"), String.class);
            Field<String> withCheck = DSL.field(DSL.name("with_check"), String.class);
            Field<String> schemaname = DSL.field(DSL.name("schemaname"), String.class);
            Field<String> tablename = DSL.field(DSL.name("tablename"), String.class);

            Result<Record3<String, String, String>> pol = ctx.select(policyname, qual, withCheck)
                .from(pgPolicies)
                .where(schemaname.eq("nexus")).and(tablename.eq("ladder_completions"))
                .fetch();
            assertThat(pol).as("a policy must exist on nexus.ladder_completions").isNotEmpty();
            Record3<String, String, String> row = pol.get(0);
            assertThat(row.value2())
                .as("USING predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(row.value3())
                .as("WITH CHECK predicate must read the nexus.tenant GUC")
                .contains("current_setting('" + TenantConstants.GUC_NAME + "'");
            assertThat(pol).as("exactly one policy expected on nexus.ladder_completions").hasSize(1);
        }
    }

    // ── Test 3: PK is (tenant_id, rung_name) ─────────────────────────────────

    @Test
    void ladderTable_primaryKeyIsTenantRungName() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.getMetaData().getPrimaryKeys(null, "nexus", "ladder_completions");
            String[] pk = new String[2];
            int count = 0;
            while (rs.next()) {
                int seq = rs.getInt("KEY_SEQ");
                pk[seq - 1] = rs.getString("COLUMN_NAME").toLowerCase();
                count++;
            }
            assertThat(count).as("PK must have exactly 2 columns").isEqualTo(2);
            assertThat(pk)
                .as("PK must be (tenant_id, rung_name) — the SQLite PK (rung_name) " +
                    "plus the tenant discriminator; one verified fact per rung per tenant")
                .containsExactly("tenant_id", "rung_name");
        }
    }

    // ── Test 4: detail defaults to '' ────────────────────────────────────────

    @Test
    void ladderTable_detailDefaultsToEmptyString() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                    DSL.val(TenantConstants.GUC_NAME), DSL.val("default-probe"), DSL.inline(true)))
                .fetch();
            ctx.insertInto(LADDER_COMPLETIONS,
                    LADDER_COMPLETIONS.TENANT_ID, LADDER_COMPLETIONS.RUNG_NAME,
                    LADDER_COMPLETIONS.VERIFIED_AT, LADDER_COMPLETIONS.PACKAGE_VERSION)
                .values("default-probe", "probe-rung", OffsetDateTime.now(ZoneOffset.UTC), "6.11.0")
                .execute();
            var row = ctx.select(LADDER_COMPLETIONS.DETAIL)
                .from(LADDER_COMPLETIONS)
                .where(LADDER_COMPLETIONS.TENANT_ID.eq("default-probe"))
                .and(LADDER_COMPLETIONS.RUNG_NAME.eq("probe-rung"))
                .fetchOne();
            assertThat(row).isNotNull();
            assertThat(row.value1())
                .as("detail must default to '' (mirrors SQLite DEFAULT '')")
                .isEmpty();
            su.rollback();
        }
    }

    // ── Test 5: tenant isolation end-to-end via TenantScope ──────────────────

    @Test
    void tenantIsolation_viaTenantScope() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "alpha", "engine-install");
            insertRow(su, "alpha", "t2-schema");
            insertRow(su, "beta",  "engine-install");
            su.commit();
        }

        List<String> alphaRungs = tenantScope.withTenant("alpha", ctx ->
            ctx.select(LADDER_COMPLETIONS.RUNG_NAME)
               .from(LADDER_COMPLETIONS)
               .orderBy(LADDER_COMPLETIONS.RUNG_NAME)
               .fetch(LADDER_COMPLETIONS.RUNG_NAME));
        assertThat(alphaRungs)
            .as("tenant-alpha must see exactly its 2 rung records")
            .containsExactly("engine-install", "t2-schema");

        List<String> betaRungs = tenantScope.withTenant("beta", ctx ->
            ctx.select(LADDER_COMPLETIONS.RUNG_NAME)
               .from(LADDER_COMPLETIONS)
               .orderBy(LADDER_COMPLETIONS.RUNG_NAME)
               .fetch(LADDER_COMPLETIONS.RUNG_NAME));
        assertThat(betaRungs)
            .as("tenant-beta must see exactly its 1 rung record")
            .containsExactly("engine-install");
    }

    // ── Test 6: BYPASSRLS (superuser) sees all tenants ───────────────────────

    @Test
    void bypassRls_superuserSeesAllTenants() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "gamma-su", "rung-g");
            insertRow(su, "delta-su", "rung-d");
            su.commit();
        }

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Field<Long> cnt = DSL.countDistinct(LADDER_COMPLETIONS.TENANT_ID).cast(SQLDataType.BIGINT);
            Long tenants = ctx.select(cnt)
                .from(LADDER_COMPLETIONS)
                .where(LADDER_COMPLETIONS.TENANT_ID.in("gamma-su", "delta-su"))
                .fetchOne(cnt);
            assertThat(tenants)
                .as("superuser (rolsuper → implicit RLS bypass) must see rows across tenants")
                .isEqualTo(2L);
        }
    }

    // ── Test 7: RLS fail-closed — unstamped connection sees zero rows ────────

    @Test
    void rls_failClosed_noGucStamp_returnsZeroRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            insertRow(su, "failclosed-tenant", "rung-fc");
            su.commit();
        }

        try (Connection svc = svcDs.getConnection()) {
            svc.setAutoCommit(true);
            DSLContext ctx = DSL.using(svc, SQLDialect.POSTGRES);
            int count = ctx.fetchCount(LADDER_COMPLETIONS);
            assertThat(count)
                .as("unstamped service connection must see zero rows (RLS fail-closed)")
                .isZero();
        }
    }

    // ── Test 8: WITH CHECK blocks cross-tenant INSERT ────────────────────────

    @Test
    void rls_withCheck_blocksCrossTenantInsert() throws Exception {
        assertThatThrownBy(() ->
            tenantScope.withTenant("epsilon", ctx ->
                ctx.insertInto(LADDER_COMPLETIONS,
                        LADDER_COMPLETIONS.TENANT_ID, LADDER_COMPLETIONS.RUNG_NAME,
                        LADDER_COMPLETIONS.VERIFIED_AT, LADDER_COMPLETIONS.PACKAGE_VERSION)
                    .values(
                        "zeta",  // tenant_id mismatch — WITH CHECK must reject
                        "rung-x", OffsetDateTime.now(ZoneOffset.UTC), "6.11.0")
                    .execute())
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

    /** Insert a completion row via superuser connection (bypasses RLS for seeding). */
    private void insertRow(Connection su, String tenant, String rungName) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(TenantConstants.GUC_NAME), DSL.val(tenant), DSL.inline(true)))
            .fetch();
        ctx.insertInto(LADDER_COMPLETIONS,
                LADDER_COMPLETIONS.TENANT_ID, LADDER_COMPLETIONS.RUNG_NAME,
                LADDER_COMPLETIONS.VERIFIED_AT, LADDER_COMPLETIONS.PACKAGE_VERSION)
            .values(tenant, rungName, OffsetDateTime.now(ZoneOffset.UTC), "test-seed")
            .onConflict(LADDER_COMPLETIONS.TENANT_ID, LADDER_COMPLETIONS.RUNG_NAME)
            .doNothing()
            .execute();
    }
}
