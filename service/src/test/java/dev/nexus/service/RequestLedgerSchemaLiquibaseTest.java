package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.1): the shape of {@code nexus.request_ledger}
 * ({@code vectors-033-request-ledger.xml}) on real Postgres, its CHECK constraints
 * enforced, and tenant isolation through a non-superuser service role.
 *
 * <p>Tests 1-3 read the shared, already-migrated cluster ({@link PgContainerHelper#start()}).
 * Test 4 gets its own database (a second {@link PgContainerHelper#start()}) so the schema it
 * hands a service role stays its own, and uses a non-superuser role because a superuser
 * connection would bypass RLS and prove nothing (the
 * {@link TuplesBaselineSchemaLiquibaseTest} Test 12 idiom). Grants are pinned in
 * {@link GrantsNexusDiagViewAccessIntegrationTest}, whose walk runs as the production-shaped
 * non-superuser admin role.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RequestLedgerSchemaLiquibaseTest {

    /** column -> information_schema udt_name + is_nullable. No doc_id (nexus-icdmw item 2). */
    private static final Map<String, String> EXPECTED_COLUMNS = new TreeMap<>(Map.ofEntries(
        Map.entry("tenant_id", "text NO"),
        Map.entry("request_id", "uuid NO"),
        Map.entry("route", "text NO"),
        Map.entry("body_sha256", "bytea NO"),
        Map.entry("state", "text NO"),
        Map.entry("holder", "uuid YES"),
        Map.entry("lease_until", "timestamptz YES"),
        Map.entry("started_at", "timestamptz NO"),
        Map.entry("finished_at", "timestamptz YES"),
        Map.entry("response_status", "int4 YES"),
        Map.entry("response", "jsonb YES")));

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    // ── Test 1: columns, types, nullability, PK, CHECKs, indexes ─────────────

    @Test
    void requestLedger_hasExactColumnsKeysChecksAndIndexes() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            Map<String, String> actual = new TreeMap<>();
            for (String col : PgCatalogProbes.columnNames(ctx, "nexus", "request_ledger")) {
                PgCatalogProbes.ColumnInfo info = PgCatalogProbes.columnInfo(ctx, "nexus", "request_ledger", col);
                actual.put(col, info.udtName() + " " + info.isNullable());
            }
            assertThat(actual).as("nexus.request_ledger exact column set and types").isEqualTo(EXPECTED_COLUMNS);

            Map<String, String> constraints = PgCatalogProbes.constraintDefinitionsIn(ctx, List.of("nexus")).stream()
                .filter(c -> c.table().equals("request_ledger"))
                .collect(Collectors.toMap(
                    PgCatalogProbes.ConstraintDefinition::conname,
                    PgCatalogProbes.ConstraintDefinition::definition));
            assertThat(constraints).containsOnlyKeys(
                "request_ledger_pk", "request_ledger_body_sha256_len", "request_ledger_state_check");
            assertThat(constraints.get("request_ledger_pk")).isEqualTo("PRIMARY KEY (tenant_id, request_id)");
            assertThat(constraints.get("request_ledger_body_sha256_len")).contains("octet_length(body_sha256) = 32");
            assertThat(constraints.get("request_ledger_state_check"))
                .contains("'in_flight'").contains("'committed'").contains("'failed'");

            assertThat(PgCatalogProbes.indexDef(ctx, "nexus", "idx_request_ledger_lease"))
                .as("live-lease reads and the expired in_flight retention delete")
                .endsWith("(tenant_id, state, lease_until)");
            assertThat(PgCatalogProbes.indexDef(ctx, "nexus", "idx_request_ledger_finished"))
                .as("the terminal-row retention delete (nexus-icdmw item 3)")
                .endsWith("(tenant_id, finished_at)");
        }
    }

    // ── Test 2: RLS ENABLE + FORCE with the tenant_isolation policy ──────────

    @Test
    void requestLedger_rlsEnabledForcedWithTenantIsolation() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "request_ledger");
            assertThat(rls).as("nexus.request_ledger must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("RLS ENABLE").isTrue();
            assertThat(rls.forced()).as("RLS FORCE").isTrue();
            List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "request_ledger");
            assertThat(policies).hasSize(1);
            assertThat(policies.get(0).policyname()).isEqualTo("tenant_isolation");
        }
    }

    // ── Test 3: the CHECK constraints reject what they name ─────────────────

    @Test
    void requestLedger_checksRejectShortFingerprintAndUnknownState() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            OffsetDateTime now = OffsetDateTime.now();

            ctx.insertInto(REQUEST_LEDGER)
                .set(REQUEST_LEDGER.TENANT_ID, "ledger-check")
                .set(REQUEST_LEDGER.REQUEST_ID, UUID.randomUUID())
                .set(REQUEST_LEDGER.ROUTE, "/v1/vectors/gc/quarantine-orphans")
                .set(REQUEST_LEDGER.BODY_SHA256, new byte[32])
                .set(REQUEST_LEDGER.STATE, "committed")
                .set(REQUEST_LEDGER.STARTED_AT, now)
                .set(REQUEST_LEDGER.FINISHED_AT, now)
                .set(REQUEST_LEDGER.RESPONSE_STATUS, 200)
                .set(REQUEST_LEDGER.RESPONSE, JSONB.valueOf("{\"moved\": 0}"))
                .execute();

            assertThatThrownBy(() -> ctx.insertInto(REQUEST_LEDGER)
                    .set(REQUEST_LEDGER.TENANT_ID, "ledger-check")
                    .set(REQUEST_LEDGER.REQUEST_ID, UUID.randomUUID())
                    .set(REQUEST_LEDGER.ROUTE, "/v1/vectors/gc/quarantine-orphans")
                    .set(REQUEST_LEDGER.BODY_SHA256, new byte[31])
                    .set(REQUEST_LEDGER.STATE, "in_flight")
                    .set(REQUEST_LEDGER.STARTED_AT, now)
                    .execute())
                .as("a 31-byte body fingerprint is refused")
                .hasMessageContaining("request_ledger_body_sha256_len");

            assertThatThrownBy(() -> ctx.insertInto(REQUEST_LEDGER)
                    .set(REQUEST_LEDGER.TENANT_ID, "ledger-check")
                    .set(REQUEST_LEDGER.REQUEST_ID, UUID.randomUUID())
                    .set(REQUEST_LEDGER.ROUTE, "/v1/vectors/gc/quarantine-orphans")
                    .set(REQUEST_LEDGER.BODY_SHA256, new byte[32])
                    .set(REQUEST_LEDGER.STATE, "done")
                    .set(REQUEST_LEDGER.STARTED_AT, now)
                    .execute())
                .as("a state outside in_flight/committed/failed is refused")
                .hasMessageContaining("request_ledger_state_check");
        }
    }

    // ── Test 4: tenant isolation through a non-superuser service role ───────

    @Test
    void tenantIsolation_secondTenantSeesNoneOfFirstTenantsRows_andCannotWriteThem() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.start();
        com.zaxxer.hikari.HikariDataSource svcDs = null;
        try {
            try (Connection su = dedicated.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }
            String svcRole = "svc_request_ledger_test";
            String svcPass = "svc_request_ledger_test_pass";
            try (Connection su = dedicated.createConnection("")) {
                PgContainerHelper.bootstrapServiceRole(su, svcRole, svcPass);
            }

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(dedicated.getJdbcUrl());
            cfg.setUsername(svcRole);
            cfg.setPassword(svcPass);
            cfg.setMaximumPoolSize(5);
            cfg.setAutoCommit(true);
            svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
            TenantScope tenantScope = new TenantScope(svcDs);

            UUID keyA = UUID.randomUUID();
            UUID keyB = UUID.randomUUID();
            OffsetDateTime now = OffsetDateTime.now();
            tenantScope.withTenant("ledger-tenant-a", ctx -> insertInFlight(ctx, "ledger-tenant-a", keyA, now));
            tenantScope.withTenant("ledger-tenant-b", ctx -> insertInFlight(ctx, "ledger-tenant-b", keyB, now));

            List<UUID> seenByA = tenantScope.withTenant("ledger-tenant-a", ctx ->
                ctx.select(REQUEST_LEDGER.REQUEST_ID).from(REQUEST_LEDGER).fetch(REQUEST_LEDGER.REQUEST_ID));
            assertThat(seenByA).as("tenant-a sees exactly its own ledger row").containsExactly(keyA);
            List<UUID> seenByB = tenantScope.withTenant("ledger-tenant-b", ctx ->
                ctx.select(REQUEST_LEDGER.REQUEST_ID).from(REQUEST_LEDGER).fetch(REQUEST_LEDGER.REQUEST_ID));
            assertThat(seenByB).as("tenant-b sees exactly its own ledger row, none of tenant-a's").containsExactly(keyB);

            assertThatThrownBy(() -> tenantScope.withTenant("ledger-tenant-b", ctx ->
                    insertInFlight(ctx, "ledger-tenant-a", UUID.randomUUID(), now)))
                .as("WITH CHECK: tenant-b cannot write a row stamped for tenant-a")
                .hasMessageContaining("row-level security");
        } finally {
            if (svcDs != null) svcDs.close();
            dedicated.stop();
        }
    }

    private static int insertInFlight(DSLContext ctx, String tenant, UUID key, OffsetDateTime now) {
        return ctx.insertInto(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.TENANT_ID, tenant)
            .set(REQUEST_LEDGER.REQUEST_ID, key)
            .set(REQUEST_LEDGER.ROUTE, "/v1/vectors/gc/quarantine-orphans")
            .set(REQUEST_LEDGER.BODY_SHA256, new byte[32])
            .set(REQUEST_LEDGER.STATE, "in_flight")
            .set(REQUEST_LEDGER.HOLDER, UUID.randomUUID())
            .set(REQUEST_LEDGER.LEASE_UNTIL, now.plusSeconds(95))
            .set(REQUEST_LEDGER.STARTED_AT, now)
            .execute();
    }
}
