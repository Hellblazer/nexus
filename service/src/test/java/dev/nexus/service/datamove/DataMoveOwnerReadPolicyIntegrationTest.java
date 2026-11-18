// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.DirectoryResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;

/**
 * nexus-f3yqd.2.16 (RDR-246, Row-level security, second paragraph): the owner-read policy pattern for a driving table
 * other than {@code nexus.chunks}, and the boot check under it, on the pg_background image. The pattern is two changelogs
 * under {@code datamove/owner-read-policy/}: {@code register.xml} (the move's one INSERT, runs in every posture) and
 * {@code policy.xml} (the guarded owner-read SELECT policy, its own changeset), run here as the migrating role
 * {@code nexus_admin_dmlutils} through Liquibase exactly as a boot would run them. The fixture tables
 * {@code dm_ownerread.rows} and {@code dm_ownerread.other} are FORCE row-level security with no policy for the migrating
 * role. No real move ships.
 *
 * <p>The single-role posture (the migrating role is {@code nexus_svc}) needs a database migrated by that role and is
 * {@link DataMoveOwnerReadSingleRoleIntegrationTest}.
 *
 * <p>Falsifiers, by hand: drop the {@code CREATE POLICY} from policy.xml and the first test stays blocked; drop the
 * precondition and the inheriting-role tests execute the changeset or (post-condition intact) abort the walk; drop the
 * post-condition and the stripped-precondition test creates the policy for nexus_svc; stop reading
 * {@code nexus.data_moves} in {@code DrivingTableIsolationCheck} and the late-membership tests see no violation.
 */
class DataMoveOwnerReadPolicyIntegrationTest extends DataMoveIntegrationBase {

    private static final String FIXTURES = "db/changelog-test/db.changelog-test-datamove-ownerread-fixtures.xml";
    private static final String REGISTER = "datamove/owner-read-policy/register.xml";
    private static final String POLICY = "datamove/owner-read-policy/policy.xml";
    private static final String MEMBERSHIP = "datamove/owner-read-policy/membership.xml";
    private static final String LOGIN_PASSWORD = "dm_ownerread_login_pw";
    private static final String SVC = PgContainerHelper.SVC_USERNAME;
    private static final String LOGIN = "dm_ownerread_login";
    private static final String POLICY_ID = "owner-read-policy-1";
    private static final String REGISTER_ID = "owner-read-register-1";
    private static final int ROWS = 300;
    private static final int PER_TENANT = 100;

    private int runs;

    @BeforeEach
    void ownerReadFixtures() throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, FIXTURES, "databasechangelog_test_datamove_ownerread", Map.of());
        // a test that granted the migrating role to another role left it a non-inheriting member
        membership(SVC, false);
        membership(LOGIN, false);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    /** {@code GRANT ADMIN TO member WITH INHERIT inherit}: put the role where the guard looks, or take it back out. */
    private void membership(String member, boolean inherit) throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, MEMBERSHIP, "databasechangelog_test_ownerread_membership",
            Map.of("granted", ADMIN, "member", member, "inherit", inherit ? "TRUE" : "FALSE"));
    }

    private static Map<String, String> params(String label, String table) {
        return Map.of("label", label, "driving_schema", "dm_ownerread", "driving_table", table);
    }

    /** Runs {@code changelog} through Liquibase as the migrating role, with a bookkeeping table of its own; returns its name. */
    private String migrateAsAdmin(String changelog, String label, String table) throws Exception {
        String bookkeeping = "databasechangelog_test_ownerread_" + (++runs);
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            PgContainerHelper.runSuperuserTestChangelog(c, changelog, bookkeeping, params(label, table));
        }
        return bookkeeping;
    }

    private String execType(String bookkeeping, String changesetId) {
        return ctx().select(DSL.field(DSL.name("exectype"), String.class)).from(DSL.table(DSL.name(bookkeeping)))
            .where(DSL.field(DSL.name("id"), String.class).eq(changesetId)).fetchOne(0, String.class);
    }

    private boolean policyExists(String table) {
        return PgCatalogProbes.policyExists(ctx(), "dm_ownerread", table, table + "_owner_read");
    }

    private int movesRegistered(String label) {
        return ctx().fetchCount(table("nexus", "data_moves"), DSL.field(DSL.name("label"), String.class).eq(label));
    }

    /** What {@code role} reads from dm_ownerread.rows, with {@code tenant} set on its session (or none). */
    private int rowsSeenBy(String role, String password, String tenant) throws Exception {
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), role, password)) {
            DSLContext ctx = DSL.using(c, SQLDialect.POSTGRES);
            if (tenant != null) {
                ctx.select(DSL.function("set_config", SQLDataType.VARCHAR, DSL.val("nexus.tenant"), DSL.val(tenant),
                    DSL.inline(false))).fetch();
            }
            return ctx.fetchCount(table("dm_ownerread", "rows"));
        }
    }

    /** A registered move over {@code schema.table}, as one INSERT writes it; the texts are never run. */
    private void insertMove(String label, String schema, String tbl) {
        ctx().insertInto(table("nexus", "data_moves"))
            .columns(DSL.field(DSL.name("label"), String.class), DSL.field(DSL.name("template"), String.class),
                DSL.field(DSL.name("driving_schema"), String.class), DSL.field(DSL.name("driving_table"), String.class),
                DSL.field(DSL.name("chunk_size"), Integer.class), DSL.field(DSL.name("sleep_ms"), Integer.class),
                DSL.field(DSL.name("statement_timeout_ms"), Integer.class),
                DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("changeset_id"), String.class))
            .values(label, "template", schema, tbl, 50, 0, 1000, "verification", "test-" + label).execute();
    }

    private HikariDataSource pool(String role, String password) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(role);
        cfg.setPassword(password);
        cfg.setMaximumPoolSize(2);   // the check holds one connection while the test holds another
        return new HikariDataSource(cfg);
    }

    private void blockedWithoutMoving(String label, dev.nexus.service.db.DmlUtilsInstaller installer) throws Exception {
        var w = worker(installer, null);
        assertThat(w.runCycle()).isEqualTo(1);
        var state = w.states().state(label).orElseThrow();
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(state.reason()).contains("does not read every tenant's rows");
        assertThat(ctx().fetchCount(table("dm_ownerread", "rows"), N.ne(0))).as("nothing moved").isZero();
    }

    // ── the two-role posture ────────────────────────────────────────────────────────────────────────────────

    @Test
    void twoRoles_theMoveIsBlockedUntilThePolicyChangesetRuns_thenRewritesEveryTenantsRows() throws Exception {
        String reg = migrateAsAdmin(REGISTER, "or-move", "rows");
        assertThat(execType(reg, REGISTER_ID)).isEqualTo("EXECUTED");
        assertThat(movesRegistered("or-move")).isEqualTo(1);
        assertThat(rowsSeenBy(ADMIN, ADMIN_PASSWORD, null))
            .as("non-vacuity: FORCE RLS and no policy for the migrating role, so it reads nothing").isZero();
        blockedWithoutMoving("or-move", installed());

        String pol = migrateAsAdmin(POLICY, "or-move", "rows");

        assertThat(execType(pol, POLICY_ID)).as("the guard did not over-skip").isEqualTo("EXECUTED");
        assertThat(policyExists("rows")).isTrue();
        assertThat(PgCatalogProbes.policyRoles(ctx(), "dm_ownerread", "rows").stream()
                .map(PgCatalogProbes.PolicyRoles::toString).toList())
            .containsExactlyInAnyOrder("tenant_isolation roles={public} cmd=ALL",
                "rows_owner_read roles={" + ADMIN + "} cmd=SELECT");
        assertThat(rowsSeenBy(ADMIN, ADMIN_PASSWORD, null)).as("the migrating role reads every tenant").isEqualTo(ROWS);
        assertThat(rowsSeenBy(SVC, PgContainerHelper.SVC_PASSWORD, "acme"))
            .as("nexus_svc stamped acme still reads only acme").isEqualTo(PER_TENANT);
        assertThat(rowsSeenBy(SVC, PgContainerHelper.SVC_PASSWORD, null)).as("nexus_svc unstamped reads nothing").isZero();

        var w = worker(installed(), null);
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(w.states().state("or-move").orElseThrow().kind()).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertThat(ctx().fetchCount(table("dm_ownerread", "rows"), N.eq(1))).as("every tenant's rows moved once")
            .isEqualTo(ROWS);
        assertThat(ctx().fetchCount(table("dm_ownerread", "rows"), N.ne(1))).isZero();
        assertThat(ctx().fetchCount(table("dm_ownerread", "rows"), TOUCHED_BY.isNull().or(TOUCHED_BY.ne(TENANT_ID))))
            .as("each row stamped by a session carrying its own tenant").isZero();
        assertThat(rowsSeenBy(SVC, PgContainerHelper.SVC_PASSWORD, "acme")).as("still no widening after the move")
            .isEqualTo(PER_TENANT);
    }

    // ── the guard ───────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void nexusSvcInheritingTheMigratingRole_thePolicyIsSkipped_theMoveStaysBlocked_andNexusSvcReadsOnlyItsTenant()
            throws Exception {
        var installer = installed();   // before the grant: a boot with nexus_svc inheriting would walk the changelog differently
        membership(SVC, true);
        try {
            migrateAsAdmin(REGISTER, "or-svc", "rows");
            String pol = migrateAsAdmin(POLICY, "or-svc", "rows");

            assertThat(execType(pol, POLICY_ID)).as("skipped, not run and failed").isEqualTo("MARK_RAN");
            assertThat(policyExists("rows")).as("no owner-read policy exists to bind nexus_svc").isFalse();
            assertThat(movesRegistered("or-svc")).as("the move is registered, which a skipped policy changeset cannot undo")
                .isEqualTo(1);
            assertThat(rowsSeenBy(SVC, PgContainerHelper.SVC_PASSWORD, "acme")).isEqualTo(PER_TENANT);
            assertThat(rowsSeenBy(SVC, PgContainerHelper.SVC_PASSWORD, null)).isZero();
            blockedWithoutMoving("or-svc", installer);
        } finally {
            membership(SVC, false);
        }
    }

    @Test
    void anotherLoginRoleInheritingTheMigratingRole_thePolicyIsSkipped_andANonInheritingMemberDoesNotSkipIt()
            throws Exception {
        membership(LOGIN, true);
        try {
            String skipped = migrateAsAdmin(POLICY, "or-login", "rows");
            assertThat(execType(skipped, POLICY_ID)).isEqualTo("MARK_RAN");
            assertThat(policyExists("rows")).isFalse();
        } finally {
            membership(LOGIN, false);
        }
        String ran = migrateAsAdmin(POLICY, "or-login", "other");
        assertThat(execType(ran, POLICY_ID)).as("INHERIT FALSE: the role does not have the privileges").isEqualTo("EXECUTED");
        assertThat(policyExists("other")).isTrue();
    }

    /**
     * The post-condition. The precondition is stripped from a copy of policy.xml and the copy is run as the migrating role
     * with nexus_svc inheriting it: the walk must abort with the post-condition's own message and nothing may persist (the
     * policy, a bookkeeping row).
     */
    @Test
    void withThePreconditionRemoved_thePostConditionAbortsTheChangeset_andNoPolicyPersists(@TempDir Path tmp)
            throws Exception {
        String xml;
        try (InputStream in = getClass().getClassLoader().getResourceAsStream(POLICY)) {
            assertThat(in).as(POLICY + " on the classpath").isNotNull();
            xml = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        String stripped = xml.replaceAll("(?s)<preConditions.*?</preConditions>", "");
        assertThat(stripped).as("non-vacuity: the precondition was there and is gone").isNotEqualTo(xml)
            .doesNotContain("<preConditions");
        Files.writeString(tmp.resolve("policy-no-guard.xml"), stripped);

        membership(SVC, true);
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(c));
            database.setDatabaseChangeLogTableName("databasechangelog_test_ownerread_stripped");
            database.setDatabaseChangeLogLockTableName("databasechangelog_test_ownerread_stripped_lock");
            try (Liquibase lb = new Liquibase("policy-no-guard.xml", new DirectoryResourceAccessor(tmp), database)) {
                params("or-stripped", "rows").forEach(lb::setChangeLogParameter);
                assertThatThrownBy(() -> lb.update(new Contexts()))
                    .hasStackTraceContaining("have the privileges of the migrating role " + ADMIN)
                    .hasStackTraceContaining(SVC);
            }
        } finally {
            membership(SVC, false);
        }
        assertThat(policyExists("rows")).as("the changeset rolled back with its policy").isFalse();
        assertThat(ctx().fetchCount(DSL.table(DSL.name("databasechangelog_test_ownerread_stripped")),
            DSL.field(DSL.name("id"), String.class).eq(POLICY_ID))).as("and left no bookkeeping row").isZero();
    }

    // ── the boot check ──────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aMembershipGrantedAfterThePolicy_isCaughtByTheBootCheck_onTheServicePool() throws Exception {
        migrateAsAdmin(REGISTER, "or-late", "rows");
        assertThat(execType(migrateAsAdmin(POLICY, "or-late", "rows"), POLICY_ID)).isEqualTo("EXECUTED");

        try (var admin = dev.nexus.service.db.AdminDdlSession.open(adminConnection,
                new dev.nexus.service.db.AdminDdlSession.Settings("or-late-read", 0L, "test", "nexus", "data_moves", "none"),
                false)) {
            assertThat(DrivingTableIsolationCheck.registeredDrivingTables(admin.ctx()))
                .as("non-vacuity: the check has a table to ask about")
                .containsExactly(new DrivingTableIsolationCheck.Table("dm_ownerread", "rows"));
        }
        try (var svc = pool(SVC, PgContainerHelper.SVC_PASSWORD)) {
            DrivingTableIsolationCheck.verifyAtStartup(svc, adminConnection);   // the clean two-role state boots
        }

        membership(SVC, true);
        membership(LOGIN, true);
        try {
            for (String[] who : new String[][] {{SVC, PgContainerHelper.SVC_PASSWORD},
                                                 {LOGIN, LOGIN_PASSWORD}}) {
                try (var svc = pool(who[0], who[1]); Connection c = svc.getConnection()) {
                    var seen = DrivingTableIsolationCheck.violations(DSL.using(c, SQLDialect.POSTGRES),
                        List.of(new DrivingTableIsolationCheck.Table("dm_ownerread", "rows")));
                    assertThat(seen).as("%s inherits the migrating role, so the owner-read policy applies to it", who[0])
                        .containsExactly(new DrivingTableIsolationCheck.Violation(
                            new DrivingTableIsolationCheck.Table("dm_ownerread", "rows"), "rows_owner_read", ADMIN));
                    assertThatThrownBy(() -> DrivingTableIsolationCheck.verifyAtStartup(svc, adminConnection))
                        .isInstanceOf(DrivingTableIsolationCheck.IsolationException.class)
                        .hasMessageContaining("rows_owner_read").hasMessageContaining("dm_ownerread.rows")
                        .hasMessageContaining(who[0]).hasMessageContaining(ADMIN);
                }
            }
        } finally {
            membership(SVC, false);
            membership(LOGIN, false);
        }
        try (var svc = pool(SVC, PgContainerHelper.SVC_PASSWORD)) {
            DrivingTableIsolationCheck.verifyAtStartup(svc, adminConnection);   // and the revoked state boots again
        }
    }

    @Test
    void everyDrivingTableIsAsked_aCompletedMovesTableStaysInScope_aVanishedTablePasses_andChunksIsLeftToItsOwnCheck()
            throws Exception {
        migrateAsAdmin(REGISTER, "or-a", "rows");
        migrateAsAdmin(POLICY, "or-a", "rows");
        migrateAsAdmin(REGISTER, "or-b", "other");
        migrateAsAdmin(POLICY, "or-b", "other");
        insertMove("or-chunks", "nexus", "chunks");
        insertMove("or-centroids", "nexus", "taxonomy_centroids");   // a centroid driving table is asked, unlike chunks
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("completed_at"), OffsetDateTime.class), OffsetDateTime.now())
            .where(DSL.field(DSL.name("label"), String.class).eq("or-a")).execute();

        var rows = new DrivingTableIsolationCheck.Table("dm_ownerread", "rows");
        var other = new DrivingTableIsolationCheck.Table("dm_ownerread", "other");
        try (var admin = dev.nexus.service.db.AdminDdlSession.open(adminConnection,
                new dev.nexus.service.db.AdminDdlSession.Settings("or-scope-read", 0L, "test", "nexus", "data_moves", "none"),
                false)) {
            assertThat(DrivingTableIsolationCheck.registeredDrivingTables(admin.ctx()))
                .as("both tables, the completed move's included, and nexus.taxonomy_centroids; nexus.chunks is ChunksIsolationCheck's")
                .containsExactly(other, rows, new DrivingTableIsolationCheck.Table("nexus", "taxonomy_centroids"));
        }

        membership(SVC, true);
        try (var svc = pool(SVC, PgContainerHelper.SVC_PASSWORD); Connection c = svc.getConnection()) {
            DSLContext service = DSL.using(c, SQLDialect.POSTGRES);
            assertThat(DrivingTableIsolationCheck.violations(service, List.of(other, rows)))
                .extracting(v -> v.table().name() + ":" + v.policy())
                .as("the second table, and the completed move's table, both still carry the policy")
                .containsExactly("other:other_owner_read", "rows:rows_owner_read");

            // a policy that reaches the serving role on the centroid tree is the parent's question, asked here
            var centroids = new DrivingTableIsolationCheck.Table("nexus", "taxonomy_centroids");
            PgContainerHelper.createPolicyUsingTrue(su, dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS,
                "or_centroid_probe", PgContainerHelper.PolicyKind.PERMISSIVE, "SELECT", "PUBLIC");
            try {
                assertThat(DrivingTableIsolationCheck.violations(service, List.of(centroids)))
                    .containsExactly(new DrivingTableIsolationCheck.Violation(centroids, "or_centroid_probe", "public"));
            } finally {
                PgContainerHelper.dropPolicy(su, dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS, "or_centroid_probe");
            }

            ctx().dropTable(DSL.name("dm_ownerread", "rows")).execute();
            assertThat(DrivingTableIsolationCheck.violations(service, List.of(other, rows)))
                .as("a table that no longer exists has no policy").extracting(v -> v.table().name()).containsExactly("other");
        } finally {
            membership(SVC, false);
        }
    }

    /**
     * The wiring: a real {@code dev.nexus.service.Main}, connecting as a login role that inherits the migrating role, exits
     * 1 through the driving-table check and not through the chunks check. The chunks owner policy (vectors-029, which the
     * same inheritance reaches) is dropped first, with its copies on the partition trees, so the one thing that can refuse
     * this boot is the policy on dm_ownerread.rows.
     */
    @Test
    void mainRefusesToBoot_whenTheServingRoleInheritsTheMigratingRoleOnADrivingTable() throws Exception {
        PgContainerHelper.dropPolicy(su, dev.nexus.service.jooq.nexus.Tables.CHUNKS, "chunks_gate_probe_owner_read");
        @SuppressWarnings("deprecation")
        int synced = dev.nexus.service.jooq.nexus.Routines.partitionSyncAccess(ctx().configuration(), "nexus.chunks", true);
        assertThat(synced).as("non-vacuity: the partition trees were re-synced").isNotNegative();
        migrateAsAdmin(REGISTER, "or-main", "rows");
        assertThat(execType(migrateAsAdmin(POLICY, "or-main", "rows"), POLICY_ID)).isEqualTo("EXECUTED");
        membership(LOGIN, true);
        try {
            var pb = new ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", System.getProperty("java.class.path"), "dev.nexus.service.Main").redirectErrorStream(true);
            var env = pb.environment();
            env.put("NX_DB_URL", pg.getJdbcUrl());
            env.put("NX_DB_USER", LOGIN);
            env.put("NX_DB_PASS", LOGIN_PASSWORD);
            env.put("NX_DB_ADMIN_URL", pg.getJdbcUrl());
            env.put("NX_DB_ADMIN_USER", ADMIN);
            env.put("NX_DB_ADMIN_PASS", ADMIN_PASSWORD);
            env.put("NX_SERVICE_PORT", "0");
            env.remove("NX_PGBOUNCER_ADMIN_URL");
            Process p = pb.start();
            try {
                var out = new java.util.concurrent.CopyOnWriteArrayList<String>();
                Thread reader = new Thread(() -> {
                    try (var r = new java.io.BufferedReader(
                            new java.io.InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                        String line;
                        while ((line = r.readLine()) != null) out.add(line);
                    } catch (java.io.IOException ignored) {
                        // torn down mid-read
                    }
                }, "main-boot-reader");
                reader.setDaemon(true);
                reader.start();
                boolean exited = p.waitFor(180, java.util.concurrent.TimeUnit.SECONDS);
                reader.join(5_000);
                String log = String.join("\n", out);
                assertThat(exited).as("Main exits by itself; output:%n%s", log).isTrue();
                assertThat(p.exitValue()).as("Main refuses to boot (exit 1); output:%n%s", log).isEqualTo(1);
                assertThat(log).contains("event=driving_table_isolation_check_failed")
                    .contains("rows_owner_read").contains("dm_ownerread.rows").contains(LOGIN);
                assertThat(log).as("it was this check that refused, and it did not serve")
                    .doesNotContain("event=chunks_isolation_check_failed").doesNotContain("event=service_ready");
            } finally {
                p.destroyForcibly();
            }
        } finally {
            membership(LOGIN, false);
        }
    }
}
