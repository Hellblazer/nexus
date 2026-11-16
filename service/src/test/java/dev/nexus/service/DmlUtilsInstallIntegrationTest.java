// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.DmlUtilsInstaller;
import dev.nexus.service.db.DmlUtilsInstaller.State;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.TimeUnit;

/**
 * nexus-f3yqd.2.11 (RDR-246 Step 4, Install sequence steps 2 and 3): the engine applies the vendored ddl_utils and
 * dml_utils changelogs at boot, as {@code dml_utils_owner}, before nexus's own, and serves whatever happens to them.
 *
 * <p>One container of the pg_background image, migrated once by a non-superuser admin role exactly as production's
 * {@code nexus_admin} is (the first boot in {@link #start()} IS the production order: libraries, then the whole
 * nexus walk from scratch). Every test then calls {@link #reset()}, which puts the database back to "ready to install,
 * nothing installed" with the library schemas dropped, and sets up its own scenario. No SQL text lives here: reads are
 * typed jOOQ, the superuser-side states are the test changelogs {@link DmlUtilsTestFixtures} runs.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Timeout(value = 20, unit = TimeUnit.MINUTES)
class DmlUtilsInstallIntegrationTest {

    /** The engine's migration role (production's nexus_admin); a name no other test class uses. */
    static final String ADMIN = DmlUtilsTestFixtures.ADMIN;
    static final String ADMIN_PASSWORD = DmlUtilsTestFixtures.ADMIN_PASSWORD;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource adminPool;
    private Connection su;
    /** The install status of the first boot, which walked nexus from scratch after the libraries. */
    private DmlUtilsInstaller.Status firstBoot;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.startDataMove();
        su = pg.createConnection("");
        su.setAutoCommit(true);
        DmlUtilsTestFixtures.createAdmin(su);
        PgContainerHelper.createDataMoveExtensions(su);
        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASSWORD);
        cfg.setMaximumPoolSize(1); // as Main's migration pool: the session a boot used is the one read back
        adminPool = new HikariDataSource(cfg);
        var installer = new DmlUtilsInstaller(true);
        SchemaMigrator.migrate(adminPool, installer);
        firstBoot = installer.status();
    }

    @AfterAll
    void stop() throws Exception {
        if (adminPool != null) adminPool.close();
        if (su != null) su.close();
        if (pg != null) pg.stop();
    }

    /** Extension and roles in place, library schemas and tracking tables gone, the {@code liquibase} schema re-made empty. */
    @BeforeEach
    void reset() throws Exception {
        DmlUtilsTestFixtures.uninstallDmlUtils(su);
        PgContainerHelper.createDataMoveExtensions(su);
        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private DmlUtilsInstaller.Status boot() {
        return boot(new DmlUtilsInstaller(true)).status();
    }

    private DmlUtilsInstaller boot(DmlUtilsInstaller installer) {
        SchemaMigrator.migrate(adminPool, installer);
        return installer;
    }

    private SchemaMigrator.MigrationOutcome bootOutcome(DmlUtilsInstaller installer) {
        return SchemaMigrator.migrate(adminPool, installer);
    }

    /** {@code current_user} of the pool's one connection, which the last boot's migration used. */
    private String adminCurrentUser() throws Exception {
        try (Connection c = adminPool.getConnection()) {
            return DSL.using(c, SQLDialect.POSTGRES).select(DSL.currentUser()).fetchOne(0, String.class);
        }
    }

    private String adminSetting(String name) throws Exception {
        try (Connection c = adminPool.getConnection()) {
            return DSL.using(c, SQLDialect.POSTGRES)
                .select(DSL.function("current_setting", String.class, DSL.val(name))).fetchOne(0, String.class);
        }
    }

    /** The {@code locked} column of every row of both libraries' changelog lock tables. */
    private List<Boolean> libraryLockRows() {
        List<Boolean> rows = new java.util.ArrayList<>();
        for (String library : List.of("ddl_utils", "dml_utils")) {
            rows.addAll(ctx().select(DSL.field(DSL.name("locked"), Boolean.class))
                .from(table("liquibase", library + "_databasechangeloglock")).fetch(0, Boolean.class));
        }
        return rows;
    }

    private DSLContext ctx() {
        return DSL.using(su, SQLDialect.POSTGRES);
    }

    private static Table<?> table(String schema, String name) {
        return DSL.table(DSL.name(schema, name));
    }

    private boolean schemaExists(String schema) {
        return ctx().fetchExists(DSL.selectOne().from(table("pg_catalog", "pg_namespace"))
            .where(DSL.field(DSL.name("nspname"), String.class).eq(schema)));
    }

    private boolean tableExists(String schema, String name) {
        return ctx().fetchExists(DSL.selectOne().from(table("pg_catalog", "pg_tables"))
            .where(DSL.field(DSL.name("schemaname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("tablename"), String.class).eq(name)));
    }

    private int rows(String schema, String name) {
        return ctx().fetchCount(table(schema, name));
    }

    private static final Field<String> FILENAME = DSL.field(DSL.name("filename"), String.class);
    private static final Field<String> CHANGESET_ID = DSL.field(DSL.name("id"), String.class);
    private static final Field<Integer> ORDER_EXECUTED = DSL.field(DSL.name("orderexecuted"), Integer.class);

    /** A tracking table's rows as {@code id|filename|orderexecuted|exectype}, in execution order. */
    private List<String> trackingRows(String library) {
        return ctx().select(CHANGESET_ID, FILENAME, ORDER_EXECUTED, DSL.field(DSL.name("exectype"), String.class))
            .from(table("liquibase", library + "_databasechangelog"))
            .orderBy(ORDER_EXECUTED)
            .fetch(r -> r.value1() + "|" + r.value2() + "|" + r.value3() + "|" + r.value4());
    }

    private List<String> ownersOfFunctionsIn(String schema) {
        var p = table("pg_catalog", "pg_proc").as("p");
        var n = table("pg_catalog", "pg_namespace").as("n");
        return ctx().selectDistinct(DSL.function("pg_get_userbyid", String.class, DSL.field(DSL.name("p", "proowner"))))
            .from(p).join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .fetch(0, String.class);
    }

    private String schemaOwner(String schema) {
        return ctx().select(DSL.function("pg_get_userbyid", String.class, DSL.field(DSL.name("nspowner"))))
            .from(table("pg_catalog", "pg_namespace"))
            .where(DSL.field(DSL.name("nspname"), String.class).eq(schema))
            .fetchOne(0, String.class);
    }

    private String tableOwner(String schema, String name) {
        var c = table("pg_catalog", "pg_class").as("c");
        var n = table("pg_catalog", "pg_namespace").as("n");
        return ctx().select(DSL.function("pg_get_userbyid", String.class, DSL.field(DSL.name("c", "relowner"))))
            .from(c).join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(schema))
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
            .fetchOne(0, String.class);
    }

    /** The roles a policy names (PUBLIC, oid 0, is not a row of pg_roles and so does not appear). */
    private List<String> policyRoles(String policy) {
        var r = table("pg_catalog", "pg_roles").as("r");
        var p = table("pg_catalog", "pg_policy").as("p");
        Field<Long> roleOid = DSL.field(DSL.name("r", "oid"), Long.class);
        Field<Long[]> polRoles = DSL.field(DSL.name("p", "polroles"), Long[].class);
        return ctx().select(DSL.field(DSL.name("r", "rolname"), String.class)).from(r).join(p)
            .on(roleOid.eq(DSL.any(polRoles)))
            .where(DSL.field(DSL.name("p", "polname"), String.class).eq(policy))
            .fetch(0, String.class);
    }

    private boolean libraryObjectsPresent() {
        return schemaExists("dml_utils") || schemaExists("ddl_utils");
    }

    // ── the production order ────────────────────────────────────────────────────────────────────────────────

    /**
     * The first boot (extension, roles and grants in place, nothing walked yet) applied the libraries and then walked
     * nexus from scratch as the admin login: the session role was the login again before nexus's first changeset, the
     * library objects belong to dml_utils_owner, and vectors-029's policy binds the login, not dml_utils_owner.
     */
    @Test
    void firstBoot_appliesTheLibraries_thenWalksNexusAsTheAdminLogin() {
        assertThat(firstBoot.state()).as("first boot status %s", firstBoot).isEqualTo(State.APPLIED);
        assertThat(firstBoot.blockedReason()).isEmpty();
        assertThat(firstBoot.sessionRoleAfter()).as("current_user right after SET ROLE NONE").isEqualTo(ADMIN);

        // reset() has dropped the library schemas since; install again to read the ownership the install gives.
        assertThat(boot().state()).isEqualTo(State.APPLIED);
        assertThat(ownersOfFunctionsIn("dml_utils")).as("owners of dml_utils' functions").containsExactly("dml_utils_owner");
        assertThat(ownersOfFunctionsIn("ddl_utils")).as("owners of ddl_utils' functions").containsExactly("dml_utils_owner");
        assertThat(schemaOwner("dml_utils")).isEqualTo("dml_utils_owner");
        assertThat(schemaOwner("liquibase")).isEqualTo("dml_utils_owner");
        assertThat(tableOwner("liquibase", "ddl_utils_databasechangelog")).isEqualTo("dml_utils_owner");
        assertThat(tableOwner("liquibase", "dml_utils_databasechangelog")).isEqualTo("dml_utils_owner");

        // Nexus's own objects were created by the first boot, after the reset: the admin login owns them, and vectors-029 EXECUTED
        // (a MARK_RAN would leave no policy) with CURRENT_USER = the login.
        assertThat(tableOwner("nexus", "chunks")).isEqualTo(ADMIN);
        assertThat(tableOwner("public", "databasechangelog")).isEqualTo(ADMIN);
        // The same policy name sits on several tables, so the list has one row per binding; every one names the login.
        List<String> policyRoles = policyRoles("chunks_gate_probe_owner_read");
        assertThat(policyRoles).as("vectors-029's owner-read policy bindings").isNotEmpty().containsOnly(ADMIN);
        assertThat(policyRoles).as("dml_utils_owner binds no owner-read policy").doesNotContain("dml_utils_owner");
    }

    // ── skip-and-serve ──────────────────────────────────────────────────────────────────────────────────────

    @Test
    void pgBackgroundAbsent_skipsAndServes_changesNoRow_thenTheNextBootAfterCreateExtensionApplies() throws Exception {
        DmlUtilsTestFixtures.dropPgBackground(su);
        int nexusRowsBefore = rows("public", "databasechangelog");

        DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
        SchemaMigrator.MigrationOutcome outcome = bootOutcome(installer);

        assertThat(outcome).as("the nexus walk completed").isNotNull();
        assertThat(installer.status().state()).isEqualTo(State.SKIPPED);
        assertThat(installer.status().blockedReason()).hasValueSatisfying(r -> assertThat(r).contains("pg_background is not installed"));
        assertThat(libraryObjectsPresent()).as("no library schema").isFalse();
        assertThat(schemaExists("liquibase")).as("the liquibase schema holds no tracking table").isTrue();
        assertThat(tableExists("liquibase", "ddl_utils_databasechangelog")).isFalse();
        assertThat(tableExists("liquibase", "dml_utils_databasechangelog")).isFalse();
        assertThat(rows("public", "databasechangelog")).as("nexus's tracking table gained no row").isEqualTo(nexusRowsBefore);

        PgContainerHelper.createDataMoveExtensions(su);
        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        assertThat(boot()).as("next boot, extension created").isEqualTo(new DmlUtilsInstaller.Status(State.APPLIED, null, ADMIN));
        assertThat(rows("liquibase", "ddl_utils_databasechangelog")).isGreaterThan(0);
        assertThat(rows("liquibase", "dml_utils_databasechangelog")).isGreaterThan(0);
        assertThat(schemaExists("dml_utils")).isTrue();
    }

    @Test
    void aLibraryRoleMissing_skipsAndServes_thenTheNextBootAfterTheRoleReturnsApplies() throws Exception {
        PgContainerHelper.dropRoleRevokingGrants(su, "ddl_utils_caller");

        DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
        assertThat(bootOutcome(installer)).as("the nexus walk completed").isNotNull();

        assertThat(installer.status().state()).isEqualTo(State.SKIPPED);
        assertThat(installer.status().blockedReason()).hasValueSatisfying(r -> assertThat(r).contains("ddl_utils_caller is missing"));
        assertThat(libraryObjectsPresent()).isFalse();
        assertThat(tableExists("liquibase", "ddl_utils_databasechangelog")).isFalse();

        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        assertThat(boot().state()).isEqualTo(State.APPLIED);
        assertThat(schemaExists("ddl_utils")).isTrue();
    }

    @Test
    void aLibraryChangelogFailing_skipsTheRest_serves_resetsTheRole_andTheNextBootAfterTheFixApplies() throws Exception {
        DmlUtilsTestFixtures.revokeDmlUtilsOwnerDatabaseCreate(su);

        DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
        assertThat(bootOutcome(installer)).as("the nexus walk completed").isNotNull();

        DmlUtilsInstaller.Status status = installer.status();
        assertThat(status.state()).isEqualTo(State.FAILED);
        assertThat(status.blockedReason()).hasValueSatisfying(r -> assertThat(r)
            .startsWith("a library changelog failed")
            .as("the failure is the first ddl_utils changeset, not the liquibase-schema step before it")
            .contains("db/vendor/ddl_utils/changes/changes.xml::001-create-schema"));
        assertThat(status.sessionRoleAfter()).as("the role was reset after the failure").isEqualTo(ADMIN);
        assertThat(schemaExists("ddl_utils")).as("the failing changeset left nothing").isFalse();
        assertThat(schemaExists("dml_utils")).as("dml_utils was not attempted after ddl_utils failed").isFalse();

        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        assertThat(boot().state()).as("the retry after the grant returns").isEqualTo(State.APPLIED);
        assertThat(schemaExists("dml_utils")).isTrue();
    }

    @Test
    void theMigrationRoleWithoutSetRole_skips() throws Exception {
        DmlUtilsTestFixtures.revokeSetRole(su);

        DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
        assertThat(bootOutcome(installer)).isNotNull();

        assertThat(installer.status().state()).isEqualTo(State.SKIPPED);
        assertThat(installer.status().blockedReason()).hasValueSatisfying(r -> assertThat(r).contains("cannot SET ROLE dml_utils_owner"));
        assertThat(libraryObjectsPresent()).isFalse();
    }

    @Test
    void aLiquibaseSchemaOwnedByAnotherRole_skips() throws Exception {
        DmlUtilsTestFixtures.uninstallDmlUtils(su);
        PgContainerHelper.createSchemaAuthorization(su, "liquibase", ADMIN);

        DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
        assertThat(bootOutcome(installer)).isNotNull();

        assertThat(installer.status().state()).isEqualTo(State.SKIPPED);
        assertThat(installer.status().blockedReason()).hasValueSatisfying(r -> assertThat(r).isEqualTo("liquibase schema owned by " + ADMIN));
        assertThat(libraryObjectsPresent()).isFalse();
    }

    @Test
    void theSingleRolePosture_skipsTheLibraries_evenWithEveryPrerequisitePresent() {
        DmlUtilsInstaller installer = new DmlUtilsInstaller(false);
        assertThat(bootOutcome(installer)).isNotNull();

        assertThat(installer.status().state()).isEqualTo(State.SKIPPED);
        assertThat(installer.status().blockedReason()).hasValueSatisfying(r -> assertThat(r).contains("no separate admin role"));
        assertThat(libraryObjectsPresent()).isFalse();
    }

    @Test
    void anAbsentLiquibaseSchema_isCreatedAsTheOwner() throws Exception {
        DmlUtilsTestFixtures.uninstallDmlUtils(su);
        assertThat(schemaExists("liquibase")).isFalse();

        assertThat(boot().state()).isEqualTo(State.APPLIED);
        assertThat(schemaOwner("liquibase")).isEqualTo("dml_utils_owner");
    }

    // ── a settled install ───────────────────────────────────────────────────────────────────────────────────

    @Test
    void aSecondBoot_isANoOp_andTheTrackedFilenamesCarryTheFrozenVendorPath() {
        assertThat(boot().state()).isEqualTo(State.APPLIED);
        List<String> ddlBefore = trackingRows("ddl_utils");
        List<String> dmlBefore = trackingRows("dml_utils");
        assertThat(ddlBefore).as("ddl_utils changesets applied").hasSizeGreaterThan(50);
        assertThat(dmlBefore).as("dml_utils changesets applied").hasSizeGreaterThan(50);
        assertThat(ddlBefore).allSatisfy(row -> assertThat(row).contains("|db/vendor/ddl_utils/changes/"));
        assertThat(dmlBefore).allSatisfy(row -> assertThat(row).contains("|db/vendor/dml_utils/changes/"));

        assertThat(boot().state()).isEqualTo(State.APPLIED);

        assertThat(trackingRows("ddl_utils")).as("ddl_utils tracking rows after the second boot").isEqualTo(ddlBefore);
        assertThat(trackingRows("dml_utils")).as("dml_utils tracking rows after the second boot").isEqualTo(dmlBefore);
        assertThat(libraryLockRows()).as("both libraries' changelog lock rows after clean boots (one row each)")
            .containsExactly(false, false);
    }

    /** Under the migration lock a locked library tracking row is stale, as nexus's own is. */
    @Test
    void aStaleLibraryChangelogLock_isReleased_andTheBootApplies() {
        assertThat(boot().state()).isEqualTo(State.APPLIED);
        Table<?> lock = table("liquibase", "dml_utils_databasechangeloglock");
        Field<Boolean> locked = DSL.field(DSL.name("locked"), Boolean.class);
        // The row as Liquibase writes it: locked, granted at a time, by a holder; the walker that wrote it is gone.
        ctx().update(lock).set(locked, true)
            .set(DSL.field(DSL.name("lockgranted"), java.sql.Timestamp.class), DSL.currentTimestamp())
            .set(DSL.field(DSL.name("lockedby"), String.class), "a-dead-walker")
            .where(DSL.field(DSL.name("id"), Integer.class).eq(1)).execute();
        assertThat(ctx().select(locked).from(lock).fetchOne(0, Boolean.class)).as("the stale row is planted").isTrue();

        assertThat(boot().state()).isEqualTo(State.APPLIED);

        assertThat(ctx().select(locked).from(lock).fetchOne(0, Boolean.class)).as("the row after the boot").isFalse();
    }

    // ── nexus-f3yqd.2.5: library changesets are not nexus schema work ───────────────────────────────────────

    /**
     * A boot whose only pending work is the library install keeps a running index build: the library changesets
     * are not counted into the pending schema work the builder termination keys on (SchemaMigrator walks them with
     * Liquibase objects of their own, after that decision), and they take no lock on any nexus relation. Nexus's own
     * pending count is the same with the libraries pending and with them applied.
     */
    @Test
    void aLibraryOnlyBoot_keepsAnIndexBuilder_andDoesNotCountLibraryChangesets() throws Exception {
        Properties props = new Properties();
        props.setProperty("user", ADMIN);
        props.setProperty("password", ADMIN_PASSWORD);
        props.setProperty("ApplicationName", "nexus-pci-builder-dmlutils-keep");
        try (Connection builder = DriverManager.getConnection(pg.getJdbcUrl(), props)) {
            DSLContext b = DSL.using(builder, SQLDialect.POSTGRES);
            assertThat(b.select(DSL.inline(1)).fetchOne(0, Integer.class)).isEqualTo(1);

            DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
            SchemaMigrator.MigrationOutcome withLibrariesPending = bootOutcome(installer);
            assertThat(installer.status().state()).as("non-vacuity: this boot installed the libraries").isEqualTo(State.APPLIED);
            assertThat(rows("liquibase", "dml_utils_databasechangelog")).isGreaterThan(0);

            assertThat(b.select(DSL.inline(1)).fetchOne(0, Integer.class))
                .as("the builder's session survived a boot that only installed the libraries").isEqualTo(1);

            SchemaMigrator.MigrationOutcome librariesApplied = bootOutcome(new DmlUtilsInstaller(true));
            assertThat(withLibrariesPending.pendingAtStart())
                .as("pending changesets with the libraries pending (nexus's runAlways re-run plan only)")
                .isEqualTo(librariesApplied.pendingAtStart());
        }
    }

    // ── a failure of any kind is skip-and-serve, and the connection leaves the role window as it entered ─────────

    /**
     * An Error from a library walk (a class that cannot load, a class initializer that fails, a stack that overflows,
     * the native image's missing-reflection error) is a FAILED install, not a failed boot: the role and lock_timeout
     * are back, nexus's walk completed, and the libraries that already applied stay applied. An OutOfMemoryError is
     * not a library failure and still propagates, with the role window closed behind it.
     */
    @Test
    void anErrorInALibraryWalk_isAFailedInstall_theWindowCloses_andTheBootServes() throws Exception {
        String lockTimeoutBefore = adminSetting("lock_timeout");
        List<Error> injected = List.of(new NoClassDefFoundError("injected-linkage"),
            new ExceptionInInitializerError("injected-clinit"), new StackOverflowError("injected-stack"),
            new Error("injected-native-missing-registration"));
        for (Error error : injected) {
            reset();
            var installer = DmlUtilsInstaller.withHookForTest(true, (conn, library) -> {
                if (library.equals("dml_utils")) {
                    throw error;
                }
            });
            assertThat(bootOutcome(installer)).as("%s: the nexus walk completed", error).isNotNull();
            DmlUtilsInstaller.Status status = installer.status();
            assertThat(status.state()).as("%s", error).isEqualTo(State.FAILED);
            assertThat(status.blockedReason()).hasValueSatisfying(r -> assertThat(r)
                .startsWith("a library changelog failed").contains(error.getMessage()));
            assertThat(status.sessionRoleAfter()).as("%s: role after", error).isEqualTo(ADMIN);
            assertThat(adminCurrentUser()).as("%s: the migration session's role", error).isEqualTo(ADMIN);
            assertThat(adminSetting("lock_timeout")).as("%s: lock_timeout restored", error).isEqualTo(lockTimeoutBefore);
            assertThat(schemaExists("ddl_utils")).as("ddl_utils, walked before the error, stays applied").isTrue();
            assertThat(schemaExists("dml_utils")).as("dml_utils was cut off by the error").isFalse();
        }

        reset();
        var oom = DmlUtilsInstaller.withHookForTest(true, (conn, library) -> {
            throw new OutOfMemoryError("injected-oom");
        });
        assertThatThrownBy(() -> boot(oom)).isInstanceOf(OutOfMemoryError.class);
        assertThat(adminCurrentUser()).as("the window was closed behind the OutOfMemoryError").isEqualTo(ADMIN);
        assertThat(adminSetting("lock_timeout")).isEqualTo(lockTimeoutBefore);
        assertThat(boot().state()).as("the next boot installs the rest").isEqualTo(State.APPLIED);
    }

    /**
     * A library statement queued behind another session's lock ends at the window's lock_timeout (55P03): FAILED and
     * serving, not a boot that waits on it. An ACCESS EXCLUSIVE lock on dml_utils' history table stands in for the
     * leftover pg_background worker or long transaction of the field; the walk's read of that table queues behind it.
     */
    @Test
    @Timeout(value = 3, unit = TimeUnit.MINUTES)
    void aLibraryStatementBlockedBehindALock_endsInLockTimeout_failedAndServing() throws Exception {
        assertThat(boot().state()).isEqualTo(State.APPLIED);
        String lockTimeoutBefore = adminSetting("lock_timeout");
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            // An open transaction's ALTER TABLE holds ACCESS EXCLUSIVE on dml_utils' history table, which every walk
            // reads (changesets pending or not); the holder's close rolls the column back.
            DSL.using(holder, SQLDialect.POSTGRES).alterTable(table("liquibase", "dml_utils_databasechangelog"))
                .addColumn(DSL.field(DSL.name("held_by_the_test"), org.jooq.impl.SQLDataType.INTEGER)).execute();

            DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
            long started = System.nanoTime();
            assertThat(bootOutcome(installer)).as("the nexus walk completed").isNotNull();
            long seconds = TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - started);

            assertThat(installer.status().state()).isEqualTo(State.FAILED);
            assertThat(installer.status().blockedReason())
                .hasValueSatisfying(r -> assertThat(r).containsIgnoringCase("lock timeout"));
            assertThat(seconds).as("the boot waited the window's lock_timeout, not for the lock holder").isLessThan(60);
            assertThat(installer.status().sessionRoleAfter()).isEqualTo(ADMIN);
            assertThat(adminCurrentUser()).isEqualTo(ADMIN);
            assertThat(adminSetting("lock_timeout")).as("lock_timeout restored").isEqualTo(lockTimeoutBefore);
        }
        assertThat(boot().state()).as("the next boot, the lock gone").isEqualTo(State.APPLIED);
        assertThat(libraryLockRows()).containsExactly(false, false);
    }

    /** The fallback of the lock_timeout restore rests on this: set_config(name, NULL, false) is RESET. */
    @Test
    void setConfigWithANullValue_resetsTheSessionSettingToItsDefault() throws Exception {
        try (Connection c = pg.createConnection("")) {
            DSLContext s = DSL.using(c, SQLDialect.POSTGRES);
            String before = s.select(DSL.function("current_setting", String.class, DSL.val("lock_timeout"))).fetchOne(0, String.class);
            s.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val("7s"), DSL.val(false))).fetch();
            assertThat(s.select(DSL.function("current_setting", String.class, DSL.val("lock_timeout"))).fetchOne(0, String.class))
                .as("non-vacuity: the setting really moved").isEqualTo("7s");
            s.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val((String) null), DSL.val(false))).fetch();
            assertThat(s.select(DSL.function("current_setting", String.class, DSL.val("lock_timeout"))).fetchOne(0, String.class))
                .isEqualTo(before);
        }
    }

    /**
     * A re-pin that edits a changeset an install already applied fails Liquibase's checksum validation: FAILED with a
     * reason that names the checksum, and the boot serves. (The vendoring script refuses such a re-pin; this is what
     * happens to an install when one gets through.) Recovery messaging is nexus-f3yqd.2.17's.
     */
    @Test
    void aChangedChecksumInALibraryTrackingTable_isAFailedInstallNamingTheChecksum_andTheBootServes() throws Exception {
        assertThat(boot().state()).isEqualTo(State.APPLIED);
        int tampered = ctx().update(table("liquibase", "ddl_utils_databasechangelog"))
            .set(DSL.field(DSL.name("md5sum"), String.class), "9:00000000000000000000000000000dead")
            .where(CHANGESET_ID.eq("001-create-schema")).execute();
        assertThat(tampered).as("non-vacuity: one tracking row was changed").isEqualTo(1);

        DmlUtilsInstaller installer = new DmlUtilsInstaller(true);
        assertThat(bootOutcome(installer)).as("the nexus walk completed").isNotNull();

        assertThat(installer.status().state()).isEqualTo(State.FAILED);
        assertThat(installer.status().blockedReason()).hasValueSatisfying(r -> assertThat(r)
            .containsIgnoringCase("check sum").contains("001-create-schema"));
        assertThat(installer.status().sessionRoleAfter()).isEqualTo(ADMIN);
        assertThat(adminCurrentUser()).isEqualTo(ADMIN);
    }
}
