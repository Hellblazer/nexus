// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.Timeout;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.17): both ways the engine ends the per-collection index builder's backend.
 *
 * <p><b>Shutdown.</b> The builder is a separate admin session named {@code nexus-pci-builder-<nonce>}.
 * {@code BackendReaper.terminateOwnBackends} matches by equality and runs as the pool's role, which cannot signal
 * the admin role's backend, so the hook needs a second call with the admin values. The shutdown test runs a
 * long statement on a builder-named admin session and on a pool-named service session and checks that one call to
 * {@link BackendReaper#terminateAtShutdown} ends both.
 *
 * <p><b>Migration.</b> {@code SchemaMigrator.migrate} ends every builder backend of its database once it holds
 * its lock, so a {@code CREATE INDEX CONCURRENTLY} cannot deadlock a changeset on the same leaf. Terminated, not
 * cancelled: the session ends and takes the builder's lock with it, and the pass reports closed and starts no
 * later build.
 *
 * <p>The builder's admin credentials are the container superuser (it owns the leaves, as {@code nexus_admin} does
 * in production). Long statements are {@code pg_sleep}: they hold a backend the way a build does, and the
 * termination is the same signal.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciBuilderTerminationIntegrationTest {

    static final String SVC_ROLE = "svc_pcitrm";
    static final String SVC_PASS = "svc_pcitrm_pass";
    static final String T1 = "pcitrm-tenant-1";
    static final String M1024 = "voyage-code-3";
    static final String ADMIN_SHUTDOWN = "57P01";
    static final PciSettings ON = new PciSettings(true, 20_000, 600, 16);

    PostgreSQLContainer<?> pg;
    PostgreSQLContainer<?> otherDatabase;
    HikariDataSource svcDs;
    PciCatalog catalog;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES),
                "tok-pcitrm-1-0123456789abcdef000000", T1, "pcitrm1");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        catalog = new PciCatalog(svcDs);
        // A second database on the same cluster: a builder-named backend there is not this database's to end.
        otherDatabase = PgContainerHelper.start();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (otherDatabase != null) otherDatabase.stop();
        if (pg != null) pg.stop();
    }

    // -- helpers -----------------------------------------------------------------------------------------

    /** A session that runs {@code pg_sleep(60)} on its own thread, so a backend is busy until it is ended. */
    final class Sleeper implements AutoCloseable {
        private final Connection conn;
        final int pid;
        final CompletableFuture<String> sqlState;

        Sleeper(String url, String user, String password, String applicationName) throws Exception {
            Properties props = new Properties();
            props.setProperty("user", user);
            props.setProperty("password", password);
            props.setProperty("ApplicationName", applicationName);
            conn = DriverManager.getConnection(url, props);
            conn.setAutoCommit(true);
            DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
            pid = ctx.select(DSL.function("pg_backend_pid", SQLDataType.INTEGER)).fetchOne(0, Integer.class);
            sqlState = CompletableFuture.supplyAsync(() -> {
                try {
                    ctx.select(DSL.function("pg_sleep", SQLDataType.OTHER, DSL.val(60))).fetch();
                    return "completed";
                } catch (DataAccessException e) {
                    return e.sqlState();
                }
            });
            awaitActive(pid);
        }

        @Override
        public void close() throws Exception {
            // End a sleep the test did not end, so no 60-second backend outlives the class and blocks the database drop.
            asSuperuser(pg, ctx -> ctx.select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN,
                DSL.val(pid))).fetchOne(0, Boolean.class));
            conn.close();
        }
    }

    <T> T asSuperuser(PostgreSQLContainer<?> container, java.util.function.Function<DSLContext, T> work)
            throws Exception {
        try (Connection su = container.createConnection("")) {
            su.setAutoCommit(true);
            return work.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    boolean backendExists(PostgreSQLContainer<?> container, int pid) throws Exception {
        return asSuperuser(container, ctx -> ctx.fetchExists(
            DSL.selectOne().from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.field(DSL.name("pid"), Integer.class).eq(pid))));
    }

    /** Backends of this database carrying exactly {@code applicationName}. */
    long backendsNamed(String applicationName) throws Exception {
        return asSuperuser(pg, ctx -> ctx.selectCount()
            .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
            .where(DSL.field(DSL.name("application_name"), String.class).eq(applicationName))
            .and(DSL.field(DSL.name("datname"), String.class)
                .eq(DSL.function("current_database", SQLDataType.VARCHAR)))
            .fetchOne(0, Long.class));
    }

    /** Sessions holding the advisory lock {@code key} (granted), in this database. */
    long holdersOf(long key) throws Exception {
        return asSuperuser(pg, ctx -> ctx.selectCount()
            .from(DSL.table(DSL.name("pg_catalog", "pg_locks")))
            .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
            .and(DSL.field(DSL.name("granted"), Boolean.class).isTrue())
            .and(DSL.field(DSL.name("database"), Long.class).cast(SQLDataType.BIGINT)
                .eq(DSL.field(ctx.select(DSL.field(DSL.name("oid"), Long.class).cast(SQLDataType.BIGINT))
                    .from(DSL.table(DSL.name("pg_catalog", "pg_database")))
                    .where(DSL.field(DSL.name("datname"), String.class)
                        .eq(DSL.function("current_database", SQLDataType.VARCHAR))))))
            .and(DSL.field(DSL.name("classid"), Long.class).cast(SQLDataType.BIGINT).eq(key >>> 32))
            .and(DSL.field(DSL.name("objid"), Long.class).cast(SQLDataType.BIGINT).eq(key & 0xffffffffL))
            .and(DSL.field(DSL.name("objsubid"), Integer.class).eq(1))
            .fetchOne(0, Long.class));
    }

    void awaitActive(int pid) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline) {
            boolean active = asSuperuser(pg, ctx -> ctx.fetchExists(
                DSL.selectOne().from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                    .where(DSL.field(DSL.name("pid"), Integer.class).eq(pid))
                    .and(DSL.field(DSL.name("state"), String.class).eq("active"))
                    .and(DSL.field(DSL.name("query"), String.class).likeIgnoreCase("%pg_sleep%"))));
            if (active) {
                return;
            }
            Thread.sleep(50);
        }
        throw new AssertionError("backend " + pid + " never became active");
    }

    void awaitGone(int pid) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline && backendExists(pg, pid)) {
            Thread.sleep(50);
        }
        assertThat(backendExists(pg, pid)).as("backend %d is gone", pid).isFalse();
    }

    /**
     * No backend carries {@code builderName} and nobody holds the builder's lock. A signalled backend leaves
     * {@code pg_stat_activity} and {@code pg_locks} a moment after the signal, so this polls.
     */
    void awaitBuilderGone(String builderName) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline
                && (backendsNamed(builderName) != 0
                    || holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY) != 0)) {
            Thread.sleep(50);
        }
        assertThat(backendsNamed(builderName)).as("builder backends").isZero();
        assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY))
            .as("the builder's lock went with its session").isZero();
    }

    PciBuilderSession builderSession(String nonce) {
        return new PciBuilderSession(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), nonce, ON);
    }

    PciCatalog.Leaf leaf() {
        List<PciCatalog.Leaf> hits = catalog.read().leaves().stream()
            .filter(l -> M1024.equals(l.model()) && T1.equals(l.tenant())).toList();
        assertThat(hits).as("exactly one leaf for (%s, %s)", M1024, T1).hasSize(1);
        return hits.get(0);
    }

    boolean indexExists(String name) {
        return leaf().indexes().stream().anyMatch(i -> name.equals(i.name()));
    }

    List<String> captureLogs(Class<?> source, Runnable body) {
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(source);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        Level before = logger.getLevel();
        logger.setLevel(Level.INFO);
        logger.addAppender(appender);
        try {
            body.run();
        } finally {
            logger.detachAppender(appender);
            logger.setLevel(before);
        }
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    // -- shutdown ----------------------------------------------------------------------------------------

    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS)
    void shutdown_endsABuilderStatementTheAppRoleCannotReach_andThePoolsOwnBackendToo() throws Exception {
        String poolName = BackendReaper.newApplicationName("test");
        String builderName = PciBuilderSession.builderApplicationName(BackendReaper.bootNonce(poolName));
        assertThat(builderName).startsWith("nexus-pci-builder-");

        try (Sleeper pool = new Sleeper(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName);
             Sleeper builder = new Sleeper(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), builderName);
             PciBuilderSession.Pass idlePass = builderSession(BackendReaper.bootNonce(poolName)).open()) {
            assertThat(backendsNamed(builderName)).as("the builder statement and the pass").isEqualTo(2);
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY)).isEqualTo(1);

            // Control: today's call, with the application's credentials and the builder's name, cannot end it.
            int appRoleResult = BackendReaper.terminateOwnBackends(
                pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, builderName);
            assertThat(appRoleResult).as("the app role may not signal the admin role's backend").isLessThanOrEqualTo(0);
            assertThat(backendExists(pg, builder.pid)).as("still running after the app-role call").isTrue();

            var admin = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
            var reaped = BackendReaper.terminateAtShutdown(
                pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName, 1, admin, builderName);

            assertThat(reaped.pool()).isEqualTo(1);
            assertThat(reaped.builder()).as("the statement and the pass").isEqualTo(2);
            assertThat(builder.sqlState.get(10, TimeUnit.SECONDS)).isEqualTo(ADMIN_SHUTDOWN);
            assertThat(pool.sqlState.get(10, TimeUnit.SECONDS)).isEqualTo(ADMIN_SHUTDOWN);
            awaitGone(builder.pid);
            awaitGone(pool.pid);
            awaitBuilderGone(builderName);
        }
    }

    @Test
    @Timeout(value = 60, unit = TimeUnit.SECONDS)
    void shutdown_withNoBuilderBackend_reportsZero_andStillReapsThePool() throws Exception {
        String poolName = BackendReaper.newApplicationName("test");
        String builderName = PciBuilderSession.builderApplicationName(BackendReaper.bootNonce(poolName));

        try (Sleeper pool = new Sleeper(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName)) {
            var admin = new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
            var reaped = BackendReaper.terminateAtShutdown(
                pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName, 1, admin, builderName);

            assertThat(reaped.builder()).isZero();
            assertThat(reaped.pool()).isEqualTo(1);
            assertThat(pool.sqlState.get(10, TimeUnit.SECONDS)).isEqualTo(ADMIN_SHUTDOWN);
        }
    }

    @Test
    @Timeout(value = 60, unit = TimeUnit.SECONDS)
    void shutdown_withAnUnreachableAdminUrl_stillReapsThePool_andReportsTheBuilderCallFailed() throws Exception {
        String poolName = BackendReaper.newApplicationName("test");
        String builderName = PciBuilderSession.builderApplicationName(BackendReaper.bootNonce(poolName));

        try (Sleeper pool = new Sleeper(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName)) {
            var admin = new AdminConnection("jdbc:postgresql://127.0.0.1:1/nowhere", "x", "x");
            var reaped = BackendReaper.terminateAtShutdown(
                pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName, 1, admin, builderName);

            assertThat(reaped.builder()).isEqualTo(BackendReaper.FAILED);
            assertThat(reaped.pool()).isEqualTo(1);
            assertThat(pool.sqlState.get(10, TimeUnit.SECONDS)).isEqualTo(ADMIN_SHUTDOWN);
        }
    }

    // -- migration ---------------------------------------------------------------------------------------

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void migrate_terminatesAnInFlightBuilder_freesItsLock_andThePassStartsNoLaterBuild() throws Exception {
        String nonce = "feedc0de";
        String builderName = PciBuilderSession.builderApplicationName(nonce);
        String first = "code__pcitrm-first__voyage-code-3__v1";
        String second = "code__pcitrm-second__voyage-code-3__v1";
        PciCatalog.Leaf leaf = leaf();
        String secondIndex = PciCatalog.indexName(M1024, T1, second);

        Properties otherProps = new Properties();
        otherProps.setProperty("ApplicationName", builderName);

        // The migrating session is itself named like a builder: it must not end itself.
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(1);
        cfg.setAutoCommit(true);
        cfg.addDataSourceProperty("ApplicationName", PciBuilderSession.APPLICATION_NAME_PREFIX + "the-migrator");

        try (HikariDataSource migrationDs = new HikariDataSource(cfg);
             PciBuilderSession.Pass pass = builderSession(nonce).open()) {
            // Built before the sleepers start: a concurrent build waits for every older snapshot.
            assertThat(pass.build(leaf, 1024, first)).as("the pass is live and builds").isEqualTo(DdlOutcome.DONE);
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY)).isEqualTo(1);
            try (Sleeper building = new Sleeper(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), builderName);
                 Sleeper bystander = new Sleeper(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS,
                     BackendReaper.newApplicationName("test"));
                 Sleeper lookalike = new Sleeper(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(),
                     "nexus-pci-build-" + nonce);
                 Connection elsewhere = otherDatabase.createConnection("", otherProps)) {
            int elsewherePid = DSL.using(elsewhere, SQLDialect.POSTGRES)
                .select(DSL.function("pg_backend_pid", SQLDataType.INTEGER)).fetchOne(0, Integer.class);

            List<String> logs = captureLogs(SchemaMigrator.class, () -> SchemaMigrator.migrate(migrationDs));

            assertThat(logs).as("the migrator ended exactly the two builder backends of this database")
                .anyMatch(l -> l.contains("event=pci_builders_terminated") && l.contains("count=2"));
            assertThat(building.sqlState.get(10, TimeUnit.SECONDS)).as("terminated, not cancelled")
                .isEqualTo(ADMIN_SHUTDOWN);
            awaitGone(building.pid);
            awaitBuilderGone(builderName);
            assertThat(holdersOf(SchemaMigrator.MIGRATION_ADVISORY_LOCK_KEY))
                .as("and the migrator released its own").isZero();

            // The pass reports closed and starts nothing: a terminated connection ends it.
            assertThat(pass.build(leaf, 1024, second)).as("a terminated connection is not the statement's failure")
                .isEqualTo(DdlOutcome.CONNECTION_LOST);
            assertThat(pass.build(leaf, 1024, second)).as("the pass stays ended")
                .isEqualTo(DdlOutcome.CONNECTION_LOST);
            assertThat(indexExists(secondIndex)).as("no later build started").isFalse();
            assertThat(indexExists(PciCatalog.indexName(M1024, T1, first))).as("the finished build stays").isTrue();

            // Not the migrator's: a pool backend, a name that only looks like the prefix, another database.
            assertThat(bystander.sqlState.isDone()).isFalse();
            assertThat(lookalike.sqlState.isDone()).isFalse();
            assertThat(backendExists(pg, bystander.pid)).isTrue();
            assertThat(backendExists(pg, lookalike.pid)).isTrue();
            assertThat(backendExists(otherDatabase, elsewherePid)).as("builder-named, in another database").isTrue();
            }
        }

        // The next pass builds: the lock is free and no migration holds its own.
        try (PciBuilderSession.Pass next = builderSession(nonce).open()) {
            assertThat(next.build(leaf, 1024, second)).isEqualTo(DdlOutcome.DONE);
        }
        assertThat(indexExists(secondIndex)).isTrue();
    }

    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void migrate_withNoBuilderBackend_logsZero() throws Exception {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(1);
        cfg.setAutoCommit(true);
        try (HikariDataSource migrationDs = new HikariDataSource(cfg)) {
            List<String> logs = captureLogs(SchemaMigrator.class, () -> SchemaMigrator.migrate(migrationDs));

            assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_terminated") && l.contains("count=0"));
        }
    }
}
