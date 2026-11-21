// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.DmlUtilsInstaller;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.time.Clock;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * nexus-f3yqd.2.16, the single-role posture of the owner-read pattern ({@link DataMoveOwnerReadPolicyIntegrationTest}
 * is the two-role one): the engine's database is migrated BY {@code nexus_svc}, which then owns every table and is the
 * migrating role (Main.buildMigrationDataSource with {@code NX_DB_ADMIN_*} unset). {@code register.xml} and
 * {@code policy.xml} run as it. Asserts RDR-246's scenario: the policy changeset is MARK_RAN, the move is registered and
 * blocked, the service role reads only its tenant's rows, and the boot check passes.
 *
 * <p>The worker is blocked here by the install gate first (one role holds no {@code dml_utils_owner} grant, so the
 * libraries are skipped), which would block the move whatever the policy said. So the row-security refusal, the one this
 * bead is about, is shown on its own through the two calls the preflight makes
 * ({@link MovePreflight#rowSecurity} and {@code AdminDdlSession.requireUnrestrictedView}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 20, unit = TimeUnit.MINUTES)
class DataMoveOwnerReadSingleRoleIntegrationTest {

    private static final String SVC = PgContainerHelper.SVC_USERNAME;
    private static final String SVC_PASSWORD = PgContainerHelper.SVC_PASSWORD;
    private static final String FIXTURES = "db/changelog-test/db.changelog-test-datamove-ownerread-fixtures.xml";
    private static final String REGISTER = "datamove/owner-read-policy/register.xml";
    private static final String POLICY = "datamove/owner-read-policy/policy.xml";
    private static final int PER_TENANT = 100;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcPool;
    Connection su;
    private java.util.TreeSet<String> shippedOverCollections;
    private String registerBook;
    private String policyBook;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.startDataMove();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        // nexus_svc IS the owner role: the single-role posture
        PgContainerHelper.bootstrapNonSuperuserOwner(su, SVC, SVC_PASSWORD);
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC);
        cfg.setPassword(SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        svcPool = new HikariDataSource(cfg);
        SchemaMigrator.migrate(svcPool);
        // The moves the walk registers are read from the master's registering changesets, not listed here. The residue moves
        // of RDR-229 P2.10 drive other tables (their driving tables are not under this test's single-role check) and are not
        // this test's, so they are deleted; every shipped move over catalog_collections stays (D2's repair and the re-own move
        // of owner-is-source), since the test asserts they are blocked the same way as sr-move (nexus-f3yqd.2.23).
        var driving = DSL.field(DSL.name("driving_table"), String.class);
        var label = DSL.field(DSL.name("label"), String.class);
        var shipped = ShippedMoveRegistrations.labels();
        var registered = new java.util.TreeSet<>(ctx().select(label).from(DSL.table(DSL.name("nexus", "data_moves")))
            .fetch(label));
        assertThat(registered).as("non-vacuity: the walk registered exactly the shipped moves").isEqualTo(shipped);
        var other = new java.util.TreeSet<>(ctx().select(label).from(DSL.table(DSL.name("nexus", "data_moves")))
            .where(driving.ne("catalog_collections")).fetch(label));
        ctx().deleteFrom(DSL.table(DSL.name("nexus", "data_moves"))).where(label.in(other)).execute();
        shippedOverCollections = new java.util.TreeSet<>(shipped);
        shippedOverCollections.removeAll(other);
        assertThat(shippedOverCollections).as("non-vacuity: D2's repair and the re-own move are the moves kept")
            .hasSizeGreaterThanOrEqualTo(2);
        PgContainerHelper.runSuperuserTestChangelog(su, FIXTURES, "databasechangelog_test_datamove_ownerread", Map.of());
        registerBook = migrate(REGISTER, "databasechangelog_test_ownerread_register");
        policyBook = migrate(POLICY, "databasechangelog_test_ownerread_policy");
    }

    @AfterAll
    void stop() throws Exception {
        if (svcPool != null) svcPool.close();
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    private String migrate(String changelog, String bookkeeping) throws Exception {
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), SVC, SVC_PASSWORD)) {
            PgContainerHelper.runSuperuserTestChangelog(c, changelog, bookkeeping,
                Map.of("label", "sr-move", "driving_schema", "dm_ownerread", "driving_table", "rows"));
        }
        return bookkeeping;
    }

    private DSLContext ctx() {
        return DSL.using(su, SQLDialect.POSTGRES);
    }

    private String execType(String bookkeeping, String id) {
        return ctx().select(DSL.field(DSL.name("exectype"), String.class)).from(DSL.table(DSL.name(bookkeeping)))
            .where(DSL.field(DSL.name("id"), String.class).eq(id)).fetchOne(0, String.class);
    }

    private int rowsSeenBySvc(String tenant) throws Exception {
        try (Connection c = svcPool.getConnection()) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("nexus.tenant"),
                DSL.val(tenant == null ? "" : tenant), DSL.inline(false))).fetch();
            try {
                return ctx.fetchCount(DSL.table(DSL.name("dm_ownerread", "rows")));
            } finally {
                ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("nexus.tenant"), DSL.val(""),
                    DSL.inline(false))).fetch();
            }
        }
    }

    @Test
    void migratedAsTheServiceRole_theMoveIsRegistered_andThePolicyChangesetIsMarkRan() {
        assertThat(execType(registerBook, "owner-read-register-1"))
            .as("the registering changeset has no guard, so it runs in every posture").isEqualTo("EXECUTED");
        assertThat(ctx().fetchCount(DSL.table(DSL.name("nexus", "data_moves")),
            DSL.field(DSL.name("label"), String.class).eq("sr-move"))).as("and registered the move").isEqualTo(1);
        assertThat(execType(policyBook, "owner-read-policy-1"))
            .as("the guard fired: skipped, not run and failed").isEqualTo("MARK_RAN");
        assertThat(PgCatalogProbes.policyRoles(ctx(), "dm_ownerread", "rows").stream()
                .map(PgCatalogProbes.PolicyRoles::toString).toList())
            .as("the only policy is the tenant policy, bound to PUBLIC; none names nexus_svc")
            .containsExactly("tenant_isolation roles={public} cmd=ALL");
    }

    @Test
    void migratedAsTheServiceRole_nexusSvcGainsNoCrossTenantRead() throws Exception {
        assertThat(ctx().fetchCount(DSL.table(DSL.name("dm_ownerread", "rows"))))
            .as("non-vacuity: the superuser sees all three tenants").isEqualTo(3 * PER_TENANT);
        assertThat(rowsSeenBySvc("acme")).isEqualTo(PER_TENANT);
        assertThat(rowsSeenBySvc("globex")).isEqualTo(PER_TENANT);
        assertThat(rowsSeenBySvc(null)).as("the table owner, FORCE RLS, no tenant set: no row").isZero();
    }

    @Test
    void migratedAsTheServiceRole_theMoveIsBlocked_byRowSecurity_andByTheInstallGate() throws Exception {
        var admin = new AdminConnection(pg.getJdbcUrl(), SVC, SVC_PASSWORD);
        var settings = new AdminDdlSession.Settings("sr-preflight", 0L, "test", "dm_ownerread", "rows", "none");
        try (AdminDdlSession session = AdminDdlSession.open(admin, settings, false)) {
            DataMove move = DataMoveRegistry.reload(session.ctx(), "sr-move").orElseThrow();
            assertThat(MovePreflight.rowSecurity(session.ctx(), move).narrowsAdmin())
                .as("FORCE RLS on a table the session owns: the admin session is bound by it").isTrue();
            assertThatThrownBy(() -> session.requireUnrestrictedView("the owner"))
                .isInstanceOf(AdminDdlSession.Refused.class)
                .hasMessageContaining("does not read every tenant's rows");
        }
        var installer = new DmlUtilsInstaller(false);
        var worker = new DataMoveWorker(admin, installer,
            new DataMoveWorker.Settings(Duration.ofMillis(50), Duration.ofMillis(10), null), "nexus-data-move-sr",
            Clock.systemUTC(), new MoveStates(Clock.systemUTC()), FailureHandler.BLOCK_ALWAYS, MoveFinisher.RUNS_ONLY, true);
        try {
            assertThat(worker.runCycle()).as("this move and every shipped move over catalog_collections, which the walk registered")
                .isEqualTo(1 + shippedOverCollections.size());
            // nexus-f3yqd.2.23: the real move over catalog_collections is registered here too, its policy changeset
            // (vectors-065) MARK_RAN by the same guard, and it is blocked the same way and writes nothing
            var blockedLabels = new java.util.ArrayList<String>(List.of("sr-move"));
            blockedLabels.addAll(shippedOverCollections);
            for (String label : blockedLabels) {
                var blocked = worker.states().state(label).orElseThrow();
                assertThat(blocked.kind()).as(label).isEqualTo(MoveStates.Kind.BLOCKED);
                assertThat(blocked.reason()).as("%s: the install gate, which comes first in a single-role boot, and no other cause", label)
                    .contains("the library install has not run");
            }
            assertThat(execType("databasechangelog", "vectors-064-1")).as("the real registering changeset has no guard")
                .isEqualTo("EXECUTED");
            assertThat(execType("databasechangelog", "vectors-065-1")).as("the real policy changeset: the guard fired")
                .isEqualTo("MARK_RAN");
            assertThat(PgCatalogProbes.policyRoles(ctx(), "nexus", "catalog_collections").stream()
                    .map(PgCatalogProbes.PolicyRoles::toString).toList())
                .as("catalog_collections keeps only its tenant policy: nexus_svc gains no cross-tenant read")
                .containsExactly("tenant_isolation roles={public} cmd=ALL");
            assertThat(ctx().fetchCount(DSL.table(DSL.name("dm_ownerread", "rows")),
                DSL.field(DSL.name("n"), Integer.class).ne(0))).as("nothing moved").isZero();
        } finally {
            worker.close();
        }
    }

    @Test
    void migratedAsTheServiceRole_theBootCheckPasses() {
        var admin = new AdminConnection(pg.getJdbcUrl(), SVC, SVC_PASSWORD);
        DrivingTableIsolationCheck.verifyAtStartup(svcPool, admin);
        try (var session = AdminDdlSession.open(admin,
                new AdminDdlSession.Settings("sr-scope", 0L, "test", "nexus", "data_moves", "none"), false)) {
            assertThat(DrivingTableIsolationCheck.registeredDrivingTables(session.ctx()))
                .as("non-vacuity: the check asked about the move's table")
                .containsExactlyInAnyOrder(new DrivingTableIsolationCheck.Table("dm_ownerread", "rows"),
                    new DrivingTableIsolationCheck.Table("nexus", "catalog_collections"),   // and the first real move's
                    // the re-own move's verification reads these two (SUPPORT_TABLES), registered in every posture
                    new DrivingTableIsolationCheck.Table("nexus", "catalog_owners"),
                    new DrivingTableIsolationCheck.Table("nexus", "gc_audit"));
        }
    }
}
