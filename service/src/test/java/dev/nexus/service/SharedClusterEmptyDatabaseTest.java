// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-7xfh7: {@link PgContainerHelper#startEmptyDatabase} hands a migration-process class an EMPTY,
 * unmigrated database on the per-fork shared cluster instead of a fresh container boot. This class pins
 * what "empty" means and what the handle owes the cluster it borrowed:
 * <ul>
 *   <li>the database is virgin: no {@code nexus} schema, no user relations, only the {@code plpgsql}
 *       extension, and the acquire creates no role;</li>
 *   <li>it is not a clone of the migrated template, and a full migration of it does not touch the next
 *       empty database (the database boundary isolates the schema);</li>
 *   <li>{@code stop()} drops it;</li>
 *   <li>cluster-wide role settings are reset on this path too, as they are on {@link SharedCluster#acquireDatabase()};</li>
 *   <li>a role a class must create bare under a FIXED name (it cannot be made unique) is declared
 *       exclusive: absent at acquire (else the acquire fails loud and names the role), dropped at stop.</li>
 * </ul>
 */
class SharedClusterEmptyDatabaseTest {

    private static final String EXCLUSIVE_ROLE = "empty_db_test_exclusive_role";

    @Test
    void theEmptyDatabaseIsVirginAndTheAcquireCreatesNoRole() throws Exception {
        // Make sure the cluster is bootstrapped (template migrated, nexus_svc present) first, so the
        // role snapshot below is the steady state a real class would see.
        PgContainerHelper.start().stop();
        List<String> rolesBefore;
        try (Connection admin = SharedCluster.controlConnection(clusterOf())) {
            rolesBefore = roleNames(DSL.using(admin, SQLDialect.POSTGRES));
        }
        assertThat(rolesBefore).as("non-vacuity: the cluster has its bootstrap roles").contains("postgres", "nexus_svc");

        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.tablesInSchema(ctx, "nexus")).as("no nexus relations").isEmpty();
            assertThat(PgCatalogProbes.tablesInSchema(ctx, "public")).as("no public relations").isEmpty();
            assertThat(schemaExists(ctx, "nexus")).as("no nexus schema: it is not a template clone").isFalse();
            assertThat(PgCatalogProbes.extensionNames(ctx)).as("only the default extension").containsExactly("plpgsql");
            assertThat(ctx.select(DSL.function("current_database", String.class)).fetchOne(0, String.class))
                .isEqualTo(pg.getDatabaseName()).isNotEqualTo(PgContainerHelper.DATABASE);
            assertThat(roleNames(ctx)).as("acquiring an empty database creates no role").isEqualTo(rolesBefore);
        } finally {
            pg.stop();
        }
    }

    @Test
    void aFullMigrationOfOneEmptyDatabaseDoesNotShowInTheNext() throws Exception {
        PostgreSQLContainer<?> first = PgContainerHelper.startEmptyDatabase();
        try (Connection su = first.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
            assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), "nexus", "chunks"))
                .as("the empty database takes the full product changelog, as a virgin container does").isTrue();
        } finally {
            first.stop();
        }
        PostgreSQLContainer<?> second = PgContainerHelper.startEmptyDatabase();
        try (Connection su = second.createConnection("")) {
            assertThat(schemaExists(DSL.using(su, SQLDialect.POSTGRES), "nexus"))
                .as("the next empty database starts virgin again").isFalse();
        } finally {
            second.stop();
        }
    }

    @Test
    void stopDropsTheDatabase() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        String name = pg.getDatabaseName();
        try (Connection admin = SharedCluster.controlConnection(clusterOf())) {
            assertThat(databaseExists(admin, name)).as("non-vacuity: it exists while held").isTrue();
        }
        pg.stop();
        try (Connection admin = SharedCluster.controlConnection(clusterOf())) {
            assertThat(databaseExists(admin, name)).as("stop() drops the empty database").isFalse();
        }
    }

    @Test
    void clusterWideRoleSettingsAreResetOnTheEmptyPathToo() throws Exception {
        PostgreSQLContainer<?> poisoner = PgContainerHelper.start();
        try (Connection su = poisoner.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, "ALTER ROLE nexus_svc SET application_name TO 'empty_db_poison'");
            assertThat(roleSettingsHolding(su, "empty_db_poison"))
                .as("non-vacuity: the poison landed as a cluster-wide setting").isEqualTo(1);
        } finally {
            poisoner.stop();
        }
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            assertThat(roleSettingsHolding(su, "empty_db_poison"))
                .as("the empty-database acquire clears cluster-wide role settings like the template path")
                .isZero();
        } finally {
            pg.stop();
        }
    }

    @Test
    void anExclusiveRoleIsAbsentAtAcquireAndDroppedAtStop() throws Exception {
        for (int round = 0; round < 2; round++) {
            PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(EXCLUSIVE_ROLE);
            try (Connection su = pg.createConnection("")) {
                // The bare statement a fixed-name-role class issues; it would collide on round 2 if stop() kept the role.
                PgContainerHelper.runSuperuserDdl(su, "CREATE ROLE " + EXCLUSIVE_ROLE + " NOLOGIN");
                assertThat(roleNames(DSL.using(su, SQLDialect.POSTGRES))).contains(EXCLUSIVE_ROLE);
            } finally {
                pg.stop();
            }
            try (Connection admin = SharedCluster.controlConnection(clusterOf())) {
                assertThat(roleNames(DSL.using(admin, SQLDialect.POSTGRES)))
                    .as("round %d: stop() drops the exclusive role", round).doesNotContain(EXCLUSIVE_ROLE);
            }
        }
    }

    /**
     * The migrating role of every moved class is the GRANTOR of grants-004's {@code GRANT pg_monitor TO nexus_svc}
     * (the template migration already made that membership, so the walk adds a second row with a different
     * grantor). PostgreSQL 16+ refuses to drop a role that is a grantor, so stop() must revoke first.
     */
    @Test
    void anExclusiveRoleThatGrantedAMembershipIsStillDropped() throws Exception {
        String grantor = EXCLUSIVE_ROLE + "_grantor";
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(grantor);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, "CREATE ROLE " + grantor + " NOLOGIN");
            PgContainerHelper.runSuperuserDdl(su, "GRANT pg_monitor TO " + grantor + " WITH ADMIN OPTION");
            PgContainerHelper.runSuperuserDdl(su, "GRANT pg_monitor TO nexus_svc GRANTED BY " + grantor);
            assertThat(PgCatalogProbes.hasRole(DSL.using(su, SQLDialect.POSTGRES), "nexus_svc", "pg_monitor"))
                .as("non-vacuity: the membership exists").isTrue();
        } finally {
            pg.stop();
        }
        try (Connection admin = SharedCluster.controlConnection(clusterOf())) {
            assertThat(roleNames(DSL.using(admin, SQLDialect.POSTGRES)))
                .as("a grantor role is dropped at stop").doesNotContain(grantor);
            assertThat(PgCatalogProbes.hasRole(DSL.using(admin, SQLDialect.POSTGRES), "nexus_svc", "pg_monitor"))
                .as("the membership the template migration made stays").isTrue();
        }
    }

    @Test
    void anExclusiveRoleThatAlreadyExistsFailsTheAcquireLoudAndNamesTheRole() throws Exception {
        String leaked = EXCLUSIVE_ROLE + "_leaked";
        // The holder that "leaks": it creates the role and never declares it exclusive, so stop() leaves it.
        PostgreSQLContainer<?> holder = PgContainerHelper.startEmptyDatabase();
        try (Connection su = holder.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, "CREATE ROLE " + leaked + " NOLOGIN");
        } finally {
            holder.stop();
        }
        PostgreSQLContainer<?> cleaner = PgContainerHelper.startEmptyDatabase();
        try {
            assertThatThrownBy(() -> PgContainerHelper.startEmptyDatabase(leaked)).hasMessageContaining(leaked);
        } finally {
            try (Connection su = cleaner.createConnection("")) {
                PgContainerHelper.runSuperuserDdl(su, "DROP ROLE IF EXISTS " + leaked);
            }
            cleaner.stop();
        }
    }

    /**
     * The cleanup that cannot run must not be silent. The exclusive role owns a schema in ANOTHER database, so
     * DROP ROLE fails at stop() (PostgreSQL records cross-database ownership as a dependency). The holder's
     * stop() then has to fail, naming the class and the roles, and the next acquire that declares the role
     * has to name the holder rather than leave the victim to guess.
     */
    @Test
    void aFailedExclusiveRoleCleanupFailsTheHoldersStopAndTheNextAcquireNamesTheHolder() throws Exception {
        String stuck = EXCLUSIVE_ROLE + "_stuck";
        PostgreSQLContainer<?> holder = PgContainerHelper.startEmptyDatabase(stuck);
        PostgreSQLContainer<?> anchor = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = holder.createConnection("")) {
                PgContainerHelper.runSuperuserDdl(su, "CREATE ROLE " + stuck + " NOLOGIN");
            }
            try (Connection su = anchor.createConnection("")) {
                PgContainerHelper.runSuperuserDdl(su, "CREATE SCHEMA stuck_schema AUTHORIZATION " + stuck);
            }
            assertThatThrownBy(holder::stop)
                .as("the holder's own teardown goes red, not a later class's acquire")
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining(stuck)
                .hasMessageContaining("SharedClusterEmptyDatabaseTest");
            assertThatThrownBy(() -> PgContainerHelper.startEmptyDatabase(stuck))
                .as("the next declarer is told who leaked it")
                .hasMessageContaining(stuck)
                .hasMessageContaining("SharedClusterEmptyDatabaseTest");
            holder.stop(); // a second stop is a no-op: it neither rethrows nor retries
        } finally {
            try (Connection su = anchor.createConnection("")) {
                PgContainerHelper.runSuperuserDdl(su, "DROP SCHEMA IF EXISTS stuck_schema");
                PgContainerHelper.runSuperuserDdl(su, "DROP ROLE IF EXISTS " + stuck);
            }
            anchor.stop();
        }
    }

    /** A second stop() that runs after a later holder acquired the same exclusive role must not drop that holder's role. */
    @Test
    void aSecondStopCannotDropALaterHoldersRole() throws Exception {
        String role = EXCLUSIVE_ROLE + "_twice";
        PostgreSQLContainer<?> first = PgContainerHelper.startEmptyDatabase(role);
        try (Connection su = first.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, "CREATE ROLE " + role + " NOLOGIN");
        }
        first.stop();
        PostgreSQLContainer<?> second = PgContainerHelper.startEmptyDatabase(role);
        try {
            try (Connection su = second.createConnection("")) {
                PgContainerHelper.runSuperuserDdl(su, "CREATE ROLE " + role + " NOLOGIN");
            }
            first.stop();
            try (Connection admin = SharedCluster.controlConnection(clusterOf())) {
                assertThat(roleNames(DSL.using(admin, SQLDialect.POSTGRES)))
                    .as("the second holder's role survives the first holder's repeated stop()").contains(role);
            }
        } finally {
            second.stop();
        }
        try (Connection admin = SharedCluster.controlConnection(clusterOf())) {
            assertThat(roleNames(DSL.using(admin, SQLDialect.POSTGRES))).doesNotContain(role);
        }
    }

    /**
     * The five-argument bootstrap overload cannot change a role that already exists. On the shared cluster
     * {@code nexus_svc} exists (NOINHERIT, already a member of pg_monitor from the template migration), so a flag
     * that asks for the opposite would be silently ignored and a falsification built on it would be vacuous.
     */
    @Test
    void theFiveArgumentBootstrapRefusesFlagsThatThePreexistingServiceRoleDefeats() throws Exception {
        String admin = EXCLUSIVE_ROLE + "_boot";
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(admin);
        try (Connection su = pg.createConnection("")) {
            assertThatThrownBy(() -> PgContainerHelper.bootstrapNonSuperuserOwner(su, admin, "pw", true, true))
                .as("INHERIT requested, nexus_svc is NOINHERIT")
                .hasMessageContaining("nexus_svc").hasMessageContaining("INHERIT");
            assertThatThrownBy(() -> PgContainerHelper.bootstrapNonSuperuserOwner(su, admin, "pw", false, false))
                .as("no pg_monitor ADMIN OPTION requested, nexus_svc already holds pg_monitor")
                .hasMessageContaining("pg_monitor");
            assertThat(roleNames(DSL.using(su, SQLDialect.POSTGRES)))
                .as("a refused call changed nothing").doesNotContain(admin);
            // Flags the cluster already satisfies are accepted (pg_monitor ADMIN OPTION, NOINHERIT).
            PgContainerHelper.bootstrapNonSuperuserOwner(su, admin, "pw", true, false);
            assertThat(roleNames(DSL.using(su, SQLDialect.POSTGRES))).contains(admin);
        } finally {
            pg.stop();
        }
    }

    /**
     * nexus-sz651: a database made from {@code template0} has no event trigger of its own, so the DDL a
     * function runs through {@code EXECUTE} would go uncounted there. {@code acquireEmptyDatabase} installs the
     * probe, and the install itself must add nothing to the count.
     */
    @Test
    void theEmptyDatabaseCountsNestedDdlAndTheProbeInstallCountsNothing() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        String name = pg.getDatabaseName();
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("pg_event_trigger")),
                DSL.field(DSL.name("evtname"), String.class).eq("nx_ddl_probe")))
                .as("the probe's event trigger is installed in the empty database").isEqualTo(1);
            assertThat(ddlEntriesOf(name, false)).as("installing the probe is neither a logged statement nor an event")
                .isEmpty();

            PgContainerHelper.runSuperuserDdl(su,
                "DO $$ BEGIN EXECUTE 'CREATE TABLE nx_sz651_nested_probe (i int)'; END $$");
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("pg_tables")),
                DSL.field(DSL.name("tablename"), String.class).eq("nx_sz651_nested_probe")))
                .as("non-vacuity: the nested DDL ran").isEqualTo(1);
            assertThat(ddlEntriesOf(name, true))
                .as("the CREATE TABLE a DO block ran through EXECUTE is counted, as a nested command")
                .anyMatch(e -> e.nested() && e.tag().equals("CREATE TABLE"));
        } finally {
            pg.stop();
        }
    }

    /**
     * nexus-sz651: bytes written. The shared cluster logs a {@code start} mark (the container's init script)
     * and a {@code ready} mark (the migrated template) with the WAL position, and {@code DdlCounts.markEnd}
     * adds the {@code end} mark, so a migration's bytes and the tests' bytes are differences of positions.
     * Non-vacuity: the work between {@code ready} and {@code end} here is a real table created and written
     * in an empty database, and both phases must come out positive.
     */
    @Test
    void theSharedClusterLogsWalPositionsAtItsStartReadyAndEndMarks() throws Exception {
        PgContainerHelper.start().stop();
        FailFastPostgreSQLContainer cluster = (FailFastPostgreSQLContainer) clusterOf();
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.createTable("nx_sz651_bytes_probe").column("i", SQLDataType.INTEGER).execute();
            ctx.insertInto(DSL.table(DSL.name("nx_sz651_bytes_probe"))).columns(DSL.field(DSL.name("i"), Integer.class))
                .select(DSL.select(DSL.field(DSL.name("g", "i"), Integer.class))
                    .from(DSL.generateSeries(1, 5000).as("g", "i"))).execute();
            assertThat(DdlCounts.walBytes(su)).as("the position is readable").isPositive();
        } finally {
            pg.stop();
        }
        assertThat(DdlCounts.markEnd(cluster)).as("the end mark can be logged on a live cluster").isTrue();
        DdlLogParser.Parsed parsed = null;
        for (int attempt = 0; attempt < 30; attempt++) {
            parsed = DdlLogParser.parse(PgContainerHelper.serverLog(cluster));
            if (parsed.marks().contains(DdlLogParser.END_LABEL)) {
                break;
            }
            Thread.sleep(100);
        }
        assertThat(parsed.marks()).startsWith(DdlLogParser.START_LABEL, DdlLogParser.READY_LABEL)
            .contains(DdlLogParser.END_LABEL);
        java.util.Map<String, Long> wal = DdlLogParser.walSplit(parsed.stats());
        assertThat(wal.get("migration_bytes")).as("the Liquibase migration wrote WAL").isPositive();
        assertThat(wal.get("tests_bytes")).as("the table and its rows wrote WAL").isPositive();
        assertThat(wal.get("total_bytes")).isEqualTo(wal.get("migration_bytes") + wal.get("tests_bytes"));
        assertThat(wal.get("dbsize_growth_bytes")).as("database sizes are readable").isNotNull();
        assertThat(parsed.entries().stream().filter(DdlLogParser.Entry::boot).count())
            .as("the probe's own install and the container bootstrap are boot, not counted").isPositive();
    }

    /** The commands the shared cluster's log attributes to {@code db}; with {@code expectSome}, read until the server flushed them. */
    private static List<DdlLogParser.Entry> ddlEntriesOf(String db, boolean expectSome) throws Exception {
        List<DdlLogParser.Entry> mine = List.of();
        for (int attempt = 0; attempt < 20; attempt++) {
            mine = DdlLogParser.parse(PgContainerHelper.serverLog(clusterOf())).entries().stream()
                .filter(e -> e.db().equals(db)).toList();
            if (!expectSome || mine.stream().anyMatch(DdlLogParser.Entry::nested)) {
                return mine;
            }
            Thread.sleep(100);
        }
        return mine;
    }

    /** The shared cluster's container, reached through a throwaway handle (only its host and port are used). */
    private static PostgreSQLContainer<?> clusterOf() throws Exception {
        return SharedCluster.container();
    }

    private static List<String> roleNames(DSLContext ctx) {
        return ctx.select(DSL.field(DSL.name("rolname"), String.class))
            .from(DSL.table(DSL.name("pg_roles")))
            .orderBy(DSL.field(DSL.name("rolname"), String.class))
            .fetch(0, String.class);
    }

    private static boolean schemaExists(DSLContext ctx, String schema) {
        return ctx.fetchCount(DSL.table(DSL.name("pg_namespace")),
            DSL.field(DSL.name("nspname"), String.class).eq(schema)) > 0;
    }

    private static boolean databaseExists(Connection admin, String name) {
        return DSL.using(admin, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("pg_database")),
            DSL.field(DSL.name("datname"), String.class).eq(name)) > 0;
    }

    /** Cluster-wide ({@code setdatabase = 0}) role settings whose value mentions {@code needle}. */
    private static int roleSettingsHolding(Connection c, String needle) {
        return DSL.using(c, SQLDialect.POSTGRES).fetchCount(DSL.table(DSL.name("pg_db_role_setting")),
            DSL.field(DSL.name("setdatabase"), Integer.class).eq(0)
                .and(DSL.function("array_to_string", String.class,
                    DSL.field(DSL.name("setconfig"), String[].class), DSL.val(",")).like("%" + needle + "%")));
    }
}
