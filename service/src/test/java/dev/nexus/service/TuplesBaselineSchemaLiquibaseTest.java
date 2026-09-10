// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * RDR-205 Phase 1 Step 2 (bead nexus-em75s.2) — Liquibase schema-apply test
 * for {@code tuples-001-baseline.xml}: {@code nexus.tuples} (the Linda tuple
 * space, tenant-scoped RLS), {@code nexus.tuple_claim_log} (append-only
 * claim-lifecycle audit, tenant-scoped RLS, FK to {@code nexus.tuples}), and
 * {@code nexus.tuple_tenants} (sweep-cursor bookkeeping, no RLS).
 *
 * <p>Tests 1-9 run against {@link PgContainerHelper#start()}'s shared,
 * already-fully-migrated cluster (the {@link Catalog036EmbeddingProfileSchemaLiquibaseTest}
 * idiom) — that cluster is bootstrapped by one real full Liquibase walk from
 * an empty database, so shape/RLS/index/FK assertions against it are also a
 * live proof that this bead's three changesets walk clean from empty.
 *
 * <p>{@link #changesetsRollBackAndReapply_restoreTheWholeChainFaithfully()}
 * and {@link #tenantIsolation_secondTenantSeesNoneOfFirstTenantsRows()} use a
 * DEDICATED container ({@link PgContainerHelper#startDedicated()}) — the
 * former to roll back exactly this bead's three changesets and prove the
 * re-apply restores tables, RLS, indexes, and the FK; the latter to exercise
 * RLS enforcement through a real NOBYPASSRLS service role (the shared
 * cluster's raw superuser connection would silently bypass RLS and prove
 * nothing about isolation).
 *
 * <p>Every data-manipulation call site here goes through typed jOOQ DSL
 * (generated {@code Tables.TUPLES}/{@code TUPLE_CLAIM_LOG}/{@code
 * TUPLE_TENANTS}, or {@link PgContainerHelper#setTenant}/{@link
 * PgCatalogProbes} for the GUC stamp and catalog reads) — no raw string SQL
 * (the {@code RawSqlGateTest} house rule, nexus-zrcj7).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TuplesBaselineSchemaLiquibaseTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String LAST_CHANGESET_ID = "tuples-001-3";

    private static final Set<String> TUPLES_EXPECTED_COLUMNS = Set.of(
        "id", "tenant_id", "subspace", "template", "keys", "dims", "body",
        "claim_state", "claimant", "claim_id", "lease_until", "attempts",
        "consumed_at", "consumed_by", "expires_at", "created_at");

    private static final Set<String> TUPLE_CLAIM_LOG_EXPECTED_COLUMNS = Set.of(
        "log_id", "tenant_id", "subspace", "template", "tuple_id",
        "claim_id", "claimant", "transition", "at", "expires_at");

    private static final Set<String> TUPLE_TENANTS_EXPECTED_COLUMNS = Set.of(
        "tenant_id", "first_seen", "last_seen", "last_swept_at");

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    // ── Test 1: nexus.tuples exact column set ────────────────────────────────

    @Test
    void tuples_hasExactColumnSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(columnNames(su, "tuples")).isEqualTo(TUPLES_EXPECTED_COLUMNS);
        }
    }

    // ── Test 2: nexus.tuples RLS ENABLE+FORCE with tenant_isolation policy ──

    @Test
    void tuples_rlsEnabledAndForced_withTenantIsolationPolicy() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "tuples");
            assertThat(rls).as("nexus.tuples must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("nexus.tuples RLS ENABLE").isTrue();
            assertThat(rls.forced()).as("nexus.tuples RLS FORCE").isTrue();

            List<PgCatalogProbes.Policy> policies = PgCatalogProbes.policies(ctx, "nexus", "tuples");
            assertThat(policies).as("nexus.tuples must have a tenant_isolation policy").hasSize(1);
            PgCatalogProbes.Policy pol = policies.get(0);
            assertThat(pol.policyname()).isEqualTo("tenant_isolation");
            assertThat(pol.qual()).contains("current_setting");
            assertThat(pol.withCheck()).contains("current_setting");
        }
    }

    // ── Test 3: nexus.tuples claim-scan partial index ────────────────────────

    @Test
    void tuples_claimScanPartialIndexExists_withWherePredicate() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.indexExists(ctx, "nexus", "idx_tuples_claim_scan")).isTrue();
            String def = PgCatalogProbes.indexDef(ctx, "nexus", "idx_tuples_claim_scan");
            assertThat(def).as("index must cover the claim-scan columns")
                .contains("tenant_id").contains("subspace").contains("created_at");
            assertThat(def.toUpperCase(Locale.ROOT))
                .as("index must be PARTIAL on the unconsumed+unclaimed predicate")
                .contains("WHERE").contains("CONSUMED_AT IS NULL").contains("CLAIM_STATE IS NULL");
        }
    }

    // ── Test 4: nexus.tuples autovacuum_vacuum_scale_factor storage param ───

    @Test
    void tuples_autovacuumScaleFactorSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String[] opts = PgCatalogProbes.relOptions(ctx, "nexus", "tuples");
            assertThat(opts).as("nexus.tuples reloptions must be set").isNotNull();
            assertThat(List.of(opts)).contains("autovacuum_vacuum_scale_factor=0.01");
        }
    }

    // ── Test 5: nexus.tuple_claim_log exact column set ───────────────────────

    @Test
    void tupleClaimLog_hasExactColumnSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(columnNames(su, "tuple_claim_log")).isEqualTo(TUPLE_CLAIM_LOG_EXPECTED_COLUMNS);
        }
    }

    // ── Test 6: nexus.tuple_claim_log RLS ENABLE+FORCE with its own policy ──

    @Test
    void tupleClaimLog_rlsEnabledAndForced_withTenantIsolationPolicy() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "tuple_claim_log");
            assertThat(rls).as("nexus.tuple_claim_log must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("nexus.tuple_claim_log RLS ENABLE").isTrue();
            assertThat(rls.forced()).as("nexus.tuple_claim_log RLS FORCE").isTrue();

            List<PgCatalogProbes.Policy> policies =
                PgCatalogProbes.policies(ctx, "nexus", "tuple_claim_log");
            assertThat(policies).as("nexus.tuple_claim_log must have a tenant_isolation policy").hasSize(1);
            assertThat(policies.get(0).policyname()).isEqualTo("tenant_isolation");
        }
    }

    // ── Test 7: tuple_claim_log.tuple_id FK ON DELETE SET NULL, indexed ─────

    @Test
    void tupleClaimLog_tupleIdForeignKey_onDeleteSetNull_andIndexed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            PgCatalogProbes.Constraint fk =
                PgCatalogProbes.foreignKey(ctx, "nexus", "tuple_claim_log_tuple_fk");
            assertThat(fk).as("tuple_claim_log_tuple_fk must exist").isNotNull();
            assertThat(fk.convalidated()).isTrue();
            assertThat(fk.confdeltype())
                .as("ON DELETE must be SET NULL ('n')").isEqualTo("n");

            assertThat(PgCatalogProbes.indexExists(ctx, "nexus", "idx_tuple_claim_log_tuple_id"))
                .as("tuple_id must be indexed so the FK ON DELETE SET NULL scan on purge "
                    + "is not a sequential scan of the log")
                .isTrue();
            String def = PgCatalogProbes.indexDef(ctx, "nexus", "idx_tuple_claim_log_tuple_id");
            assertThat(def).contains("tuple_id");
        }
    }

    // ── Test 8: nexus.tuple_claim_log autovacuum_vacuum_scale_factor ────────

    @Test
    void tupleClaimLog_autovacuumScaleFactorSet() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            String[] opts = PgCatalogProbes.relOptions(ctx, "nexus", "tuple_claim_log");
            assertThat(opts).as("nexus.tuple_claim_log reloptions must be set").isNotNull();
            assertThat(List.of(opts)).contains("autovacuum_vacuum_scale_factor=0.01");
        }
    }

    // ── Test 9: nexus.tuple_tenants — exact columns, NO RLS ──────────────────

    @Test
    void tupleTenants_hasExactColumnSet_andNoRls() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(columnNames(su, "tuple_tenants")).isEqualTo(TUPLE_TENANTS_EXPECTED_COLUMNS);

            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "tuple_tenants");
            assertThat(rls).as("nexus.tuple_tenants must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("nexus.tuple_tenants must NOT have RLS enabled").isFalse();
            assertThat(rls.forced()).as("nexus.tuple_tenants must NOT have RLS forced").isFalse();
            assertThat(PgCatalogProbes.policies(ctx, "nexus", "tuple_tenants"))
                .as("nexus.tuple_tenants must have no RLS policy").isEmpty();
        }
    }

    // ── Test 10: rollback round trip — this bead's 3 changesets only ────────

    /**
     * Migrates up to (and including) {@code tuples-001-3}, seeds one row per
     * table (a tuple, a claim-log entry referencing it, a tuple_tenants row),
     * rolls back exactly 3 changesets — which land at the execution tail on a
     * single walk from empty, so {@code rollback(3, ...)} removes precisely
     * this bead's own changesets (the {@link VectorsUnifyChunksIntegrationTest}
     * / {@link SchemaRollbackRoundTripIntegrationTest} idiom) — asserts all
     * three tables are gone, then re-applies the rest of the changelog and
     * asserts the tables, RLS, the claim-scan index, and the FK are all
     * restored (empty, since rollback drops the data with the tables).
     */
    @Test
    void changesetsRollBackAndReapply_restoreTheWholeChainFaithfully() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.startDedicated();
        try {
            try (Connection su = dedicated.createConnection("")) {
                migrateUpTo(su, LAST_CHANGESET_ID, true);
            }

            byte[] tupleId = HexFormat.of().parseHex("deadbeef");
            try (Connection su = dedicated.createConnection("")) {
                su.setAutoCommit(false);
                PgContainerHelper.setTenant(su, TenantScope.DEFAULT_TENANT_GUC, "rollback-tenant", true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                OffsetDateTime now = OffsetDateTime.now();
                ctx.insertInto(TUPLES)
                    .set(TUPLES.ID, tupleId)
                    .set(TUPLES.TENANT_ID, "rollback-tenant")
                    .set(TUPLES.SUBSPACE, "mailbox/a1")
                    .set(TUPLES.TEMPLATE, "mailbox/<agent_id>")
                    .set(TUPLES.KEYS, JSONB.valueOf("{}"))
                    .set(TUPLES.EXPIRES_AT, now.plusHours(1))
                    .set(TUPLES.CREATED_AT, now)
                    .execute();
                ctx.insertInto(TUPLE_CLAIM_LOG)
                    .set(TUPLE_CLAIM_LOG.TENANT_ID, "rollback-tenant")
                    .set(TUPLE_CLAIM_LOG.SUBSPACE, "mailbox/a1")
                    .set(TUPLE_CLAIM_LOG.TEMPLATE, "mailbox/<agent_id>")
                    .set(TUPLE_CLAIM_LOG.TUPLE_ID, tupleId)
                    .set(TUPLE_CLAIM_LOG.TRANSITION, "claim")
                    .set(TUPLE_CLAIM_LOG.AT, now)
                    .set(TUPLE_CLAIM_LOG.EXPIRES_AT, now.plusDays(180))
                    .execute();
                ctx.insertInto(TUPLE_TENANTS)
                    .set(TUPLE_TENANTS.TENANT_ID, "rollback-tenant")
                    .set(TUPLE_TENANTS.FIRST_SEEN, now)
                    .set(TUPLE_TENANTS.LAST_SEEN, now)
                    .execute();
                su.commit();
            }

            try (Connection su = dedicated.createConnection("")) {
                Database database = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                try (Liquibase liquibase = new Liquibase(
                        MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                    liquibase.rollback(3, new Contexts(), new LabelExpression());
                }
            }

            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "tuples"))
                    .as("nexus.tuples must be gone after rolling back its 3 changesets").isFalse();
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "tuple_claim_log"))
                    .as("nexus.tuple_claim_log must be gone after rollback").isFalse();
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "tuple_tenants"))
                    .as("nexus.tuple_tenants must be gone after rollback").isFalse();
            }

            try (Connection su = dedicated.createConnection("")) {
                applyFullChangelog(su);
            }

            try (Connection su = dedicated.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "tuples"))
                    .as("nexus.tuples must be recreated by the re-apply").isTrue();
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "tuple_claim_log"))
                    .as("nexus.tuple_claim_log must be recreated by the re-apply").isTrue();
                assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "tuple_tenants"))
                    .as("nexus.tuple_tenants must be recreated by the re-apply").isTrue();

                PgCatalogProbes.RowSecurity tuplesRls = PgCatalogProbes.rowSecurity(ctx, "nexus", "tuples");
                assertThat(tuplesRls.enabled()).as("RLS must be restored on nexus.tuples").isTrue();
                assertThat(tuplesRls.forced()).as("FORCE RLS must be restored on nexus.tuples").isTrue();

                assertThat(PgCatalogProbes.indexExists(ctx, "nexus", "idx_tuples_claim_scan"))
                    .as("the claim-scan partial index must be restored").isTrue();

                PgCatalogProbes.Constraint fk =
                    PgCatalogProbes.foreignKey(ctx, "nexus", "tuple_claim_log_tuple_fk");
                assertThat(fk).as("the tuple_id FK must be restored").isNotNull();
                assertThat(fk.confdeltype()).isEqualTo("n");

                assertThat(ctx.fetchCount(TUPLES))
                    .as("re-created tables start empty — rollback dropped the seeded row with the table")
                    .isEqualTo(0);
            }
        } finally {
            dedicated.stop();
        }
    }

    // ── Test 11: walk from empty + full-changelog re-apply is a no-op ───────

    @Test
    void changesetsWalkFromEmpty_andReapplyIsNoOp() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.startDedicated();
        try {
            try (Connection su = dedicated.createConnection("")) {
                applyFullChangelog(su);
            }
            try (Connection su = dedicated.createConnection("")) {
                // Re-applying the full changelog against this SAME already-migrated
                // database must be a no-op — Liquibase's own checksum verification
                // throws on any drift in a non-runAlways changeset.
                assertThatCode(() -> applyFullChangelog(su)).doesNotThrowAnyException();
            }
        } finally {
            dedicated.stop();
        }
    }

    // ── Test 12: RLS enforcement — second tenant sees none of the first's rows ─

    /**
     * Runs through a real NOBYPASSRLS service role via {@link TenantScope}
     * (the {@link PlansSchemaLiquibaseTest} idiom) — a raw superuser
     * connection would bypass RLS and prove nothing about isolation.
     */
    @Test
    void tenantIsolation_secondTenantSeesNoneOfFirstTenantsRows() throws Exception {
        PostgreSQLContainer<?> dedicated = PgContainerHelper.startDedicated();
        com.zaxxer.hikari.HikariDataSource svcDs = null;
        try {
            try (Connection su = dedicated.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
            }
            String svcRole = "svc_tuples_schema_test";
            String svcPass = "svc_tuples_schema_test_pass";
            try (Connection su = dedicated.createConnection("")) {
                PgContainerHelper.bootstrapServiceRole(su, svcRole, svcPass);
            }

            OffsetDateTime now = OffsetDateTime.now();
            try (Connection su = dedicated.createConnection("")) {
                su.setAutoCommit(false);
                PgContainerHelper.setTenant(su, TenantScope.DEFAULT_TENANT_GUC, "tuples-tenant-a", true);
                DSL.using(su, SQLDialect.POSTGRES).insertInto(TUPLES)
                    .set(TUPLES.ID, HexFormat.of().parseHex("a1a1a1a1"))
                    .set(TUPLES.TENANT_ID, "tuples-tenant-a")
                    .set(TUPLES.SUBSPACE, "mailbox/a1")
                    .set(TUPLES.TEMPLATE, "mailbox/<agent_id>")
                    .set(TUPLES.KEYS, JSONB.valueOf("{}"))
                    .set(TUPLES.EXPIRES_AT, now.plusHours(1))
                    .set(TUPLES.CREATED_AT, now)
                    .execute();
                su.commit();
            }
            try (Connection su = dedicated.createConnection("")) {
                su.setAutoCommit(false);
                PgContainerHelper.setTenant(su, TenantScope.DEFAULT_TENANT_GUC, "tuples-tenant-b", true);
                DSL.using(su, SQLDialect.POSTGRES).insertInto(TUPLES)
                    .set(TUPLES.ID, HexFormat.of().parseHex("b2b2b2b2"))
                    .set(TUPLES.TENANT_ID, "tuples-tenant-b")
                    .set(TUPLES.SUBSPACE, "mailbox/b1")
                    .set(TUPLES.TEMPLATE, "mailbox/<agent_id>")
                    .set(TUPLES.KEYS, JSONB.valueOf("{}"))
                    .set(TUPLES.EXPIRES_AT, now.plusHours(1))
                    .set(TUPLES.CREATED_AT, now)
                    .execute();
                su.commit();
            }

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(dedicated.getJdbcUrl());
            cfg.setUsername(svcRole);
            cfg.setPassword(svcPass);
            cfg.setMaximumPoolSize(5);
            cfg.setAutoCommit(true);
            svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
            TenantScope tenantScope = new TenantScope(svcDs);

            var tenantASubspaces = tenantScope.withTenant("tuples-tenant-a", ctx ->
                ctx.select(TUPLES.SUBSPACE).from(TUPLES).fetch(TUPLES.SUBSPACE));
            assertThat(tenantASubspaces)
                .as("tenant-a must see exactly its own tuple").containsExactly("mailbox/a1");

            var tenantBSubspaces = tenantScope.withTenant("tuples-tenant-b", ctx ->
                ctx.select(TUPLES.SUBSPACE).from(TUPLES).fetch(TUPLES.SUBSPACE));
            assertThat(tenantBSubspaces)
                .as("tenant-b must see exactly its own tuple, none of tenant-a's")
                .containsExactly("mailbox/b1");
            assertThat(tenantBSubspaces)
                .as("tenant-b's SELECT must not contain tenant-a's row")
                .doesNotContain("mailbox/a1");
        } finally {
            if (svcDs != null) svcDs.close();
            dedicated.stop();
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    private static Set<String> columnNames(Connection su, String table) throws Exception {
        ResultSet rs = su.getMetaData().getColumns(null, "nexus", table, null);
        Set<String> actual = new java.util.HashSet<>();
        while (rs.next()) {
            actual.add(rs.getString("COLUMN_NAME").toLowerCase(Locale.ROOT));
        }
        return actual;
    }

    private static void applyFullChangelog(Connection conn) throws Exception {
        // Deliberately NOT try-with-resources on the Liquibase object (matches
        // PgContainerHelper#applyProductSchema's own idiom): Liquibase#close()
        // closes the underlying JdbcConnection it wraps, which would leave the
        // setAutoCommit(true) restoration below hitting an already-closed
        // connection.
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(
            MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        liquibase.update(new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }

    /**
     * Apply the master changelog's changesets UP TO {@code targetChangesetId}
     * — {@code Catalog036EmbeddingProfileSchemaLiquibaseTest}'s identical
     * index-based idiom (robust against other changesets landing earlier in
     * the chain). {@code inclusive=true} applies {@code targetChangesetId}
     * itself too (Liquibase's {@code update(int)} is exclusive of the index
     * passed, so this passes {@code idx + 1} for the inclusive case).
     */
    private static void migrateUpTo(Connection conn, String targetChangesetId, boolean inclusive)
            throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(
            MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
        int idx = -1;
        for (int i = 0; i < unrun.size(); i++) {
            if (targetChangesetId.equals(unrun.get(i).getId())) {
                idx = i;
                break;
            }
        }
        assertThat(idx)
            .as(targetChangesetId + " must be present in the master changelog")
            .isGreaterThanOrEqualTo(0);
        liquibase.update(inclusive ? idx + 1 : idx, new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }
}
