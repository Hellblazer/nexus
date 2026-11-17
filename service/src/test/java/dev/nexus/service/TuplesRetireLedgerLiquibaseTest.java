// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.tuples.TemplateRegistry;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_DELIVERIES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-onjvw: {@code tuples-012-1} retires the RDR-205 v1 session ledger. It deletes every
 * {@code nexus.tuples} row whose subspace starts with {@code ledger/}, across all tenants, with the
 * {@code nexus.tuple_deliveries} and {@code nexus.tuple_claim_log} rows for those subspaces, and
 * leaves everything else alone.
 *
 * <p>Runs on an empty database owned by a NOBYPASSRLS, non-superuser role: the production
 * migration role's shape, under which an un-toggled DELETE on a FORCE-RLS table matches no row on
 * any tenant (the nexus-1wjmq class). A superuser connection would bypass RLS and prove nothing
 * about the toggles, so the rows are seeded as superuser and the migration runs as the owner; the
 * test first asserts that the owner cannot see the seeded rows, so a green run cannot come from
 * RLS never having been in play.
 *
 * <p>The seed and the assertions are package-visible statics so
 * {@code SchemaUpgradeRehearsalIntegrationTest}'s data leg (the SEED-COVERAGE contract) runs the
 * same population through the old-tag to HEAD hop.
 */
class TuplesRetireLedgerLiquibaseTest {

    private static final String ADMIN_ROLE = "nexus_admin_ledger_retire";
    private static final String ADMIN_PASS = "nexus_admin_ledger_retire_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    static final String CHANGESET_ID = "tuples-012-1";

    static final String TENANT_A = "ledger-retire-a";
    static final String TENANT_B = "ledger-retire-b";
    static final String TENANT_C = "ledger-retire-c";

    private static final List<String> TENANTS = List.of(TENANT_A, TENANT_B, TENANT_C);

    /** The seeded population: which ids must go and which must stay. */
    record Seeded(Set<String> ledgerIds, Set<String> keptIds) {
    }

    // ── the tests ───────────────────────────────────────────────────────────

    @Test
    void ledgerRowsAndTheirSideRowsAreDeletedAcrossTenants_everythingElseSurvives() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, CHANGESET_ID);

                Seeded seeded;
                try (Connection su = pg.createConnection("")) {
                    seeded = seed(su);
                    assertSeededPopulation(su, seeded);
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThat(DSL.using(admin, SQLDialect.POSTGRES).fetchCount(TUPLES))
                        .as("FORCE RLS must hide every seeded row from the NOBYPASSRLS owner: this is "
                            + "what makes the toggles in tuples-012-1 load-bearing")
                        .isEqualTo(0);
                }

                SchemaMigrator.migrate(adminDs);

                try (Connection su = pg.createConnection("")) {
                    assertRetired(su, seeded);
                    assertForceRlsRestored(su);
                }
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aSecondRunIsANoOp_andARunAfterANewLedgerRowDeletesOnlyThatRow() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, CHANGESET_ID);
                Seeded seeded;
                try (Connection su = pg.createConnection("")) {
                    seeded = seed(su);
                }
                SchemaMigrator.migrate(adminDs);

                // Second run, nothing left to delete: Liquibase records the changeset as run, so the
                // statements are re-executed by forgetting that record.
                forgetChangeset(adminDs, CHANGESET_ID);
                SchemaMigrator.migrate(adminDs);
                try (Connection su = pg.createConnection("")) {
                    assertThat(changesetRecorded(su, CHANGESET_ID))
                        .as("the changeset ran a second time (its record is back)").isTrue();
                    assertRetired(su, seeded);
                    assertForceRlsRestored(su);
                }

                // Third run after one more ledger row arrives (a tenant that had none): that row, and
                // only that row, goes; the earlier survivors are untouched.
                String lateId;
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    lateId = insertTuple(ctx, "late-ledger", TENANT_C, "ledger/late-session");
                    insertDelivery(ctx, TENANT_C, "ledger/late-session", lateId);
                    assertThat(ctx.fetchCount(TUPLES, TUPLES.TENANT_ID.in(TENANTS)))
                        .isEqualTo(seeded.keptIds().size() + 1);
                }
                forgetChangeset(adminDs, CHANGESET_ID);
                SchemaMigrator.migrate(adminDs);
                try (Connection su = pg.createConnection("")) {
                    assertRetired(su, seeded);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(ctx.fetchCount(TUPLE_DELIVERIES,
                            TUPLE_DELIVERIES.SUBSPACE.eq("ledger/late-session")))
                        .as("the late ledger row's delivery stamp went with it").isEqualTo(0);
                }
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void theRegistryNoLongerListsTheLedger_andAnOutToItIsRefusedAsAnUnknownSubspace() throws Exception {
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        assertThat(registry.templates().stream().map(t -> t.name()).toList())
            .as("the bundled registry")
            .doesNotContain("ledger/<session_id>")
            .hasSize(7);
        assertThat(registry.resolve("ledger/x")).as("ledger/x resolves to no template").isNull();

        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.applyProductSchema(su);
                PgContainerHelper.bootstrapServiceRole(su, "svc_ledger_retire", "svc_ledger_retire_pass");
            }
            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername("svc_ledger_retire");
            cfg.setPassword("svc_ledger_retire_pass");
            cfg.setMaximumPoolSize(2);
            cfg.setAutoCommit(true);
            try (HikariDataSource svcDs = new HikariDataSource(cfg)) {
                var repo = new dev.nexus.service.db.TupleRepository(new dev.nexus.service.db.TenantScope(svcDs), registry);
                assertThatThrownBy(() -> repo.out(TENANT_A, "ledger/x",
                        java.util.Map.of("agent_id", "a1", "kind", "start"), java.util.Map.of(), null, null, null))
                    .isInstanceOf(dev.nexus.service.db.UnknownSubspaceException.class);
                assertThatThrownBy(() -> repo.rdp(TENANT_A, "ledger/x", null, 10, null))
                    .isInstanceOf(dev.nexus.service.db.UnknownSubspaceException.class);
            }
        } finally {
            pg.stop();
        }
    }

    // ── seed and assertions (shared with SchemaUpgradeRehearsalIntegrationTest) ─────

    /**
     * Tenants A and B each hold ledger rows; tenant C holds none. Beside them, rows that must
     * survive: ordinary subspaces, and look-alikes whose subspace contains {@code ledger} without
     * starting with {@code ledger/}. Side rows for both kinds: delivery stamps, claim-log rows
     * (one with its tuple, one orphaned with a null tuple_id), and the tenant-cursor rows.
     * Superuser connection (implicit BYPASSRLS), as every seed helper in this module.
     */
    static Seeded seed(Connection su) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        var ledger = new TreeSet<String>();
        var kept = new TreeSet<String>();

        String l1 = insertTuple(ctx, "L1", TENANT_A, "ledger/session-1");
        String l2 = insertTuple(ctx, "L2", TENANT_A, "ledger/session-1");
        String l3 = insertTuple(ctx, "L3", TENANT_A, "ledger/session-2");
        String l4 = insertTuple(ctx, "L4", TENANT_B, "ledger/session-3");
        ledger.addAll(List.of(l1, l2, l3, l4));

        kept.add(insertTuple(ctx, "K1", TENANT_A, "mailbox/m1"));
        String k2 = insertTuple(ctx, "K2", TENANT_B, "mailbox/m2");
        kept.add(k2);
        kept.add(insertTuple(ctx, "K3", TENANT_A, "mailbox/ledger/x"));
        kept.add(insertTuple(ctx, "K4", TENANT_B, "ledger-x/y"));
        kept.add(insertTuple(ctx, "K5", TENANT_A, "directory/ledger/z"));
        kept.add(insertTuple(ctx, "K6", TENANT_C, "board/ledgerish"));
        kept.add(insertTuple(ctx, "K7", TENANT_C, "ledger"));

        insertDelivery(ctx, TENANT_A, "ledger/session-1", l1);
        insertDelivery(ctx, TENANT_A, "mailbox/m1", sha("K1"));
        // A ledger-subspace delivery whose tuple is a SURVIVING non-ledger row (k2): the FK cascade
        // cannot remove it, so only the tuple_deliveries DELETE in tuples-012-1 does.
        insertDelivery(ctx, TENANT_B, "ledger/dangling", k2);
        insertClaimLog(ctx, TENANT_A, "ledger/session-2", l3, "ledger-claim-with-tuple");
        insertClaimLog(ctx, TENANT_B, "ledger/gone", null, "ledger-claim-orphan");
        insertClaimLog(ctx, TENANT_B, "mailbox/m2", k2, "mailbox-claim");

        for (String tenant : List.of(TENANT_A, TENANT_B)) {
            ctx.insertInto(TUPLE_TENANTS)
                .set(TUPLE_TENANTS.TENANT_ID, tenant)
                .set(TUPLE_TENANTS.FIRST_SEEN, now())
                .set(TUPLE_TENANTS.LAST_SEEN, now())
                .execute();
        }
        return new Seeded(ledger, kept);
    }

    static void assertSeededPopulation(Connection su, Seeded seeded) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        assertThat(ctx.fetchCount(TUPLES, TUPLES.TENANT_ID.in(TENANTS))).as("seeded tuples")
            .isEqualTo(seeded.ledgerIds().size() + seeded.keptIds().size());
        assertThat(ctx.fetchCount(TUPLE_DELIVERIES, TUPLE_DELIVERIES.TENANT_ID.in(TENANTS)))
            .as("seeded deliveries").isEqualTo(3);
        assertThat(ctx.fetchCount(TUPLE_CLAIM_LOG, TUPLE_CLAIM_LOG.TENANT_ID.in(TENANTS)))
            .as("seeded claim-log rows").isEqualTo(3);
    }

    /**
     * Every ledger row and its side rows are gone, in every tenant; every other row, its side rows
     * and the tenant cursor rows are exactly as seeded.
     */
    static void assertRetired(Connection su, Seeded seeded) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

        assertThat(ctx.fetchCount(TUPLES, TUPLES.SUBSPACE.like("ledger/%")))
            .as("no tuple row whose subspace starts with ledger/ remains, in any tenant").isEqualTo(0);
        var remaining = new TreeSet<String>();
        for (byte[] id : ctx.select(TUPLES.ID).from(TUPLES).where(TUPLES.TENANT_ID.in(TENANTS)).fetch(TUPLES.ID)) {
            remaining.add(java.util.HexFormat.of().formatHex(id));
        }
        assertThat(remaining).as("exactly the non-ledger rows survive").isEqualTo(seeded.keptIds());
        assertThat(ctx.fetchCount(TUPLES, TUPLES.SUBSPACE.in(
                "mailbox/ledger/x", "ledger-x/y", "directory/ledger/z", "board/ledgerish", "ledger")))
            .as("look-alike subspaces that merely contain 'ledger' are untouched").isEqualTo(5);

        assertThat(ctx.fetchCount(TUPLE_DELIVERIES, TUPLE_DELIVERIES.SUBSPACE.like("ledger/%")))
            .as("no ledger delivery stamp remains, including the one pointing at a surviving non-ledger "
                + "tuple (the FK cascade cannot remove that one; only the DELETE does)").isEqualTo(0);
        assertThat(ctx.fetchCount(TUPLE_DELIVERIES, TUPLE_DELIVERIES.TENANT_ID.in(TENANTS)))
            .as("the mailbox delivery stamp survives").isEqualTo(1);

        assertThat(ctx.fetchCount(TUPLE_CLAIM_LOG, TUPLE_CLAIM_LOG.SUBSPACE.like("ledger/%")))
            .as("no ledger claim-log row remains: not the one with its tuple, not the orphan with a null tuple_id")
            .isEqualTo(0);
        assertThat(ctx.fetchCount(TUPLE_CLAIM_LOG, TUPLE_CLAIM_LOG.CLAIM_ID.eq("mailbox-claim")))
            .as("the mailbox claim-log row survives").isEqualTo(1);
        assertThat(ctx.fetchCount(TUPLE_CLAIM_LOG, TUPLE_CLAIM_LOG.TENANT_ID.in(TENANTS))).isEqualTo(1);

        assertThat(ctx.fetchCount(TUPLE_TENANTS, TUPLE_TENANTS.TENANT_ID.in(TENANTS)))
            .as("the tenant cursor rows are untouched").isEqualTo(2);
    }

    static void assertForceRlsRestored(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        for (String table : List.of("tuples", "tuple_claim_log", "tuple_deliveries")) {
            var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", table);
            assertThat(rls).as(table).isNotNull();
            assertThat(rls.enabled()).as(table + " RLS enabled").isTrue();
            assertThat(rls.forced())
                .as("tuples-012-1 must restore FORCE ROW LEVEL SECURITY on " + table)
                .isTrue();
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    private static OffsetDateTime now() {
        return OffsetDateTime.now(ZoneOffset.UTC);
    }

    private static byte[] shaBytes(String label) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(label.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static byte[] sha(String label) {
        return shaBytes(label);
    }

    private static String hex(byte[] b) {
        return java.util.HexFormat.of().formatHex(b);
    }

    /** One tuple row; returns its id as hex. The template column carries the template name a registry would resolve. */
    private static String insertTuple(DSLContext ctx, String label, String tenant, String subspace) {
        byte[] id = shaBytes(label);
        String template = subspace.startsWith("ledger/") ? "ledger/<session_id>" : "other/<x>";
        ctx.insertInto(TUPLES)
            .set(TUPLES.ID, id)
            .set(TUPLES.TENANT_ID, tenant)
            .set(TUPLES.SUBSPACE, subspace)
            .set(TUPLES.TEMPLATE, template)
            .set(TUPLES.KEYS, JSONB.jsonb("{}"))
            .set(TUPLES.EXPIRES_AT, now().plusDays(7))
            .set(TUPLES.CREATED_AT, now())
            .execute();
        return hex(id);
    }

    private static void insertDelivery(DSLContext ctx, String tenant, String subspace, Object tupleId) {
        byte[] id = tupleId instanceof String s ? java.util.HexFormat.of().parseHex(s) : (byte[]) tupleId;
        ctx.insertInto(TUPLE_DELIVERIES)
            .set(TUPLE_DELIVERIES.TENANT_ID, tenant)
            .set(TUPLE_DELIVERIES.SUBSPACE, subspace)
            .set(TUPLE_DELIVERIES.SUBSCRIBER, "subscriber-1")
            .set(TUPLE_DELIVERIES.TUPLE_ID, id)
            .set(TUPLE_DELIVERIES.ANNOUNCED_AT, now())
            .execute();
    }

    private static void insertClaimLog(DSLContext ctx, String tenant, String subspace, String tupleIdHexOrNull,
                                        String claimId) {
        ctx.insertInto(TUPLE_CLAIM_LOG)
            .set(TUPLE_CLAIM_LOG.TENANT_ID, tenant)
            .set(TUPLE_CLAIM_LOG.SUBSPACE, subspace)
            .set(TUPLE_CLAIM_LOG.TEMPLATE, subspace.startsWith("ledger/") ? "ledger/<session_id>" : "other/<x>")
            .set(TUPLE_CLAIM_LOG.TUPLE_ID, tupleIdHexOrNull == null ? null
                : java.util.HexFormat.of().parseHex(tupleIdHexOrNull))
            .set(TUPLE_CLAIM_LOG.CLAIM_ID, claimId)
            .set(TUPLE_CLAIM_LOG.CLAIMANT, "claimant-1")
            .set(TUPLE_CLAIM_LOG.TRANSITION, "claim")
            .set(TUPLE_CLAIM_LOG.AT, now())
            .set(TUPLE_CLAIM_LOG.EXPIRES_AT, now().plusDays(30))
            .execute();
    }

    private static HikariDataSource adminPool(PostgreSQLContainer<?> pg) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-ledger-retire");
        return new HikariDataSource(cfg);
    }

    /** Applies the HEAD changelog up to, but not including, {@code changesetId}. */
    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun =
                    liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId)
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    private static org.jooq.Table<?> databaseChangeLog() {
        return DSL.table(DSL.name("databasechangelog"));
    }

    private static void forgetChangeset(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            int deleted = DSL.using(conn, SQLDialect.POSTGRES)
                .deleteFrom(databaseChangeLog())
                .where(DSL.field(DSL.name("id"), String.class).eq(changesetId))
                .execute();
            assertThat(deleted).as("databasechangelog row for %s", changesetId).isEqualTo(1);
        }
    }

    private static boolean changesetRecorded(Connection conn, String changesetId) {
        return DSL.using(conn, SQLDialect.POSTGRES).fetchExists(
            DSL.selectOne().from(databaseChangeLog())
                .where(DSL.field(DSL.name("id"), String.class).eq(changesetId)));
    }
}
