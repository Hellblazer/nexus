// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.SchemaMigrator;
import dev.nexus.service.vectors.PciBuilderSession.DdlOutcome;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
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
 * <p><b>Shutdown</b> (nexus-f3yqd). {@code BackendReaper.terminateAtShutdown} ends the pool's backends and leaves the
 * builder's, a long statement on a builder-named admin session, running with its advisory lock: the build finishes in
 * Postgres and the next engine adopts it.
 *
 * <p><b>Migration.</b> {@code SchemaMigrator.migrate} ends every builder backend of its database once it holds
 * its lock, but only when the walk has a schema changeset to run (or a partition repair to make): with only runAlways
 * changesets pending it leaves them be, because their bodies take no lock that conflicts with a build. When it does end
 * a build it terminates, never cancels: the session ends and takes the builder's lock with it, and the pass reports
 * closed and starts no later build.
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

    /**
     * An open transaction holding ROW EXCLUSIVE on the leaf. A {@code CREATE INDEX CONCURRENTLY} on that leaf has by
     * then committed its invalid catalog entry and waits for this transaction to end: a build held open mid-flight.
     */
    final class LeafWriter implements AutoCloseable {
        private final Connection conn;
        private boolean closed;

        LeafWriter(PciCatalog.Leaf leaf) throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(false);
            PgContainerHelper.lockTable(conn, DSL.table(DSL.name(leaf.schema(), leaf.name())),
                PgContainerHelper.LockMode.ROW_EXCLUSIVE);
        }

        @Override
        public void close() throws Exception {
            if (!closed) {
                closed = true;
                conn.rollback();
                conn.close();
            }
        }
    }

    /**
     * An open REPEATABLE READ transaction that has taken a snapshot and holds no lock on the leaf: a {@code CREATE INDEX
     * CONCURRENTLY} finishes its builds and then waits for it (older snapshots), still holding its
     * ShareUpdateExclusiveLock on the leaf.
     */
    final class SnapshotHolder implements AutoCloseable {
        private final Connection conn;
        private boolean closed;

        SnapshotHolder() throws Exception {
            conn = pg.createConnection("");
            conn.setAutoCommit(false);
            conn.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
            DSL.using(conn, SQLDialect.POSTGRES).selectCount()
                .from(DSL.table(DSL.name("pg_catalog", "pg_class"))).fetchOne();
        }

        @Override
        public void close() throws Exception {
            if (!closed) {
                closed = true;
                conn.rollback();
                conn.close();
            }
        }
    }

    /** A build of {@code collection} on its own thread, returned once its invalid index is in the catalog. */
    CompletableFuture<DdlOutcome> startHeldBuild(PciBuilderSession.Pass pass, PciCatalog.Leaf leaf, String collection)
            throws Exception {
        String index = PciCatalog.indexName(M1024, T1, collection);
        var build = CompletableFuture.supplyAsync(() -> pass.build(leaf, 1024, collection));
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(30);
        while (System.nanoTime() < deadline && !indexExists(index)) {
            Thread.sleep(50);
        }
        assertThat(indexExists(index)).as("the build's invalid index is in the catalog").isTrue();
        assertThat(build.isDone()).as("the build is waiting, not finished").isFalse();
        return build;
    }

    boolean indexValid(String name) {
        return leaf().indexes().stream().anyMatch(i -> name.equals(i.name()) && i.valid());
    }

    /**
     * Forget one applied, idempotent, non-runAlways changeset (vectors-058-1 is a CREATE OR REPLACE of functions), so
     * that the next walk has a real schema changeset pending.
     */
    void forgetAChangeset() throws Exception {
        int deleted = asSuperuser(pg, ctx -> ctx.deleteFrom(DSL.table(DSL.name("public", "databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq("vectors-058-1")).execute());
        assertThat(deleted).as("vectors-058-1 was applied").isEqualTo(1);
    }

    /**
     * {@code SchemaMigrator.migrate}, bounded: a walk that queues behind a build the test holds open is a failing test
     * with its cause named, not a hung fork (a JDBC read blocked in the test thread ignores the timeout's interrupt).
     * The caller releases what holds the build in a {@code finally}, which lets a stuck walk end.
     */
    void migrateWithin(HikariDataSource ds, long seconds) {
        var walk = CompletableFuture.runAsync(() -> SchemaMigrator.migrate(ds));
        try {
            walk.get(seconds, TimeUnit.SECONDS);
        } catch (java.util.concurrent.TimeoutException e) {
            throw new AssertionError("the walk did not end within " + seconds + " s: it is queued behind a build", e);
        } catch (Exception e) {
            throw new AssertionError("the walk failed: " + e, e);
        }
    }

    HikariDataSource migrationPool() {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(1);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    // -- shutdown ----------------------------------------------------------------------------------------

    /**
     * nexus-f3yqd: the shutdown reap ends the pool's backends and NOT the builder's. A build in flight keeps running in
     * Postgres, with the builder's session advisory lock, so the next engine neither rebuilds it nor starts a parallel
     * one (its pass reports standby until the build is over).
     */
    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS)
    void shutdown_reapsThePoolsBackend_butLeavesTheBuilderBackendAndItsLockRunning() throws Exception {
        String poolName = BackendReaper.newApplicationName("test");
        String builderName = PciBuilderSession.builderApplicationName(BackendReaper.bootNonce(poolName));

        try (Sleeper pool = new Sleeper(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName);
             Sleeper builder = new Sleeper(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword(), builderName);
             PciBuilderSession.Pass idlePass = builderSession(BackendReaper.bootNonce(poolName)).open()) {
            assertThat(backendsNamed(builderName)).as("the builder statement and the pass").isEqualTo(2);
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY)).isEqualTo(1);

            int reaped = BackendReaper.terminateAtShutdown(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName, 1);

            assertThat(reaped).as("the pool's backend is reaped as before").isEqualTo(1);
            assertThat(pool.sqlState.get(10, TimeUnit.SECONDS)).isEqualTo(ADMIN_SHUTDOWN);
            awaitGone(pool.pid);
            assertThat(backendExists(pg, builder.pid)).as("the builder's statement is left running").isTrue();
            assertThat(builder.sqlState.isDone()).as("it was not signalled").isFalse();
            assertThat(backendsNamed(builderName)).as("both builder backends remain").isEqualTo(2);
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY))
                .as("and the builder's lock with them: a new engine's builder cannot start a parallel build").isEqualTo(1);
        }
    }

    /** The other backends of the engine are still reaped as before, with a builder backend alongside. */
    @Test
    @Timeout(value = 60, unit = TimeUnit.SECONDS)
    void shutdown_withNoBuilderBackend_stillReapsThePool() throws Exception {
        String poolName = BackendReaper.newApplicationName("test");

        try (Sleeper pool = new Sleeper(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName)) {
            int reaped = BackendReaper.terminateAtShutdown(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS, poolName, 1);

            assertThat(reaped).isEqualTo(1);
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

            forgetAChangeset();
            List<String> logs = captureLogs(SchemaMigrator.class, () -> migrateWithin(migrationDs, 120));

            assertThat(logs).as("the migrator ended exactly the two builder backends of this database")
                .anyMatch(l -> l.contains("event=pci_builders_terminated") && l.contains("count=2")
                    && l.contains("reason=schema_changesets_pending") && l.contains("n=1"));
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
        try (HikariDataSource migrationDs = migrationPool()) {
            List<String> logs = captureLogs(SchemaMigrator.class, () -> migrateWithin(migrationDs, 120));

            assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_kept") && l.contains("count=0"));
        }
    }

    /**
     * nexus-f3yqd: a boot with nothing but {@code runAlways} changesets pending (every boot of an unchanged release)
     * leaves a build in flight alone. The runAlways bodies take no lock that conflicts with the build's (grants take no
     * relation lock; vectors-034-2 bounds its wait at 5 s and skips a busy leaf), so there is nothing to settle.
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void migrate_withOnlyRunAlwaysPending_keepsAnInFlightBuilder_andTheBuildCompletesValid() throws Exception {
        String nonce = "c0ffee01";
        String builderName = PciBuilderSession.builderApplicationName(nonce);
        String coll = "code__pcitrm-keep__voyage-code-3__v1";
        String index = PciCatalog.indexName(M1024, T1, coll);
        PciCatalog.Leaf leaf = leaf();

        try (HikariDataSource migrationDs = migrationPool();
             PciBuilderSession.Pass pass = builderSession(nonce).open();
             LeafWriter writer = new LeafWriter(leaf)) {
            var build = startHeldBuild(pass, leaf, coll);

            List<String> logs = captureLogs(SchemaMigrator.class, () -> migrateWithin(migrationDs, 120));

            assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_kept") && l.contains("count=1")
                && l.contains("reason=only_run_always_pending"));
            assertThat(logs).noneMatch(l -> l.contains("event=pci_builders_terminated"));
            assertThat(backendsNamed(builderName)).as("the builder's backend is still there").isEqualTo(1);
            assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY)).isEqualTo(1);
            assertThat(build.isDone()).as("and still building").isFalse();

            writer.close();
            assertThat(build.get(60, TimeUnit.SECONDS)).isEqualTo(DdlOutcome.DONE);
            assertThat(indexValid(index)).as("the build ran to its end").isTrue();
        }
    }

    /** The other direction, and the control for the one above: a real changeset pending ends the build as before. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void migrate_withARealChangesetPending_endsAnInFlightBuilder() throws Exception {
        String nonce = "c0ffee02";
        String builderName = PciBuilderSession.builderApplicationName(nonce);
        String coll = "code__pcitrm-end__voyage-code-3__v1";
        PciCatalog.Leaf leaf = leaf();

        try (HikariDataSource migrationDs = migrationPool();
             PciBuilderSession.Pass pass = builderSession(nonce).open();
             LeafWriter writer = new LeafWriter(leaf)) {
            var build = startHeldBuild(pass, leaf, coll);
            forgetAChangeset();

            List<String> logs = captureLogs(SchemaMigrator.class, () -> migrateWithin(migrationDs, 120));

            assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_terminated") && l.contains("count=1")
                && l.contains("reason=schema_changesets_pending") && l.contains("n=1"));
            assertThat(logs).noneMatch(l -> l.contains("event=pci_builders_kept"));
            assertThat(build.get(30, TimeUnit.SECONDS)).isEqualTo(DdlOutcome.CONNECTION_LOST);
            awaitBuilderGone(builderName);
            writer.close();
        }
    }

    // -- a self-retrying changeset is not schema work (nexus-f3yqd.3) -------------------------------------

    static final String RETIRE_CHANGESET = "aspects-005-2";
    static final String QUEUE_COLLECTION = "pcitrm-queue-coll";
    static final String QUEUE_DOC = "pcitrm.1";

    static final String RETIRE_CHANGELOG = "db/changelog/aspects-005-retire-aspect-queue-table.xml";
    static final Table<?> QUEUE_TABLE = DSL.table(DSL.name("nexus", "aspect_extraction_queue"));

    /**
     * aspects-005-2 as a store with leftover aspect-queue rows has it: the table is back with one row and the counting
     * helper exists (both from the changelog's own text: the rollback of aspects-005-2 recreates the empty table and
     * the helper, aspects-005-1 swaps the foreign key), and the changeset is not recorded, so it is pending on every
     * boot (its precondition fails, onFail CONTINUE, and a body that ran would raise with failOnError false; neither
     * records it).
     */
    void stageRetireChangesetPending() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.replayChangesetRollback(su, RETIRE_CHANGELOG, RETIRE_CHANGESET);
            PgContainerHelper.replayChangeset(su, RETIRE_CHANGELOG, "aspects-005-1");
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, T1, QUEUE_COLLECTION);
            PgContainerHelper.insertCatalogDocument(ctx, T1, QUEUE_DOC);
            ctx.insertInto(QUEUE_TABLE, DSL.field(DSL.name("tenant_id"), String.class),
                    DSL.field(DSL.name("collection"), String.class), DSL.field(DSL.name("source_path"), String.class),
                    DSL.field(DSL.name("doc_id"), String.class),
                    DSL.field(DSL.name("enqueued_at"), java.time.OffsetDateTime.class))
                .values(T1, QUEUE_COLLECTION, "/legacy/one.pdf", QUEUE_DOC, java.time.OffsetDateTime.now())
                .execute();
            ctx.deleteFrom(DSL.table(DSL.name("public", "databasechangelog")))
                .where(DSL.field(DSL.name("id"), String.class).eq(RETIRE_CHANGESET)).execute();
        }
        assertThat(changesetRecorded(RETIRE_CHANGESET)).as("%s is pending", RETIRE_CHANGESET).isFalse();
    }

    boolean changesetRecorded(String id) throws Exception {
        return asSuperuser(pg, ctx -> ctx.fetchExists(DSL.selectOne()
            .from(DSL.table(DSL.name("public", "databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq(id))));
    }

    /**
     * The drain has emptied the queue: the next walk drops the table and records the changeset, the state every
     * other test in this class assumes. Also the cleanup when a test fails after staging.
     */
    void drainAndRetireTheStagedQueue(HikariDataSource ds) throws Exception {
        asSuperuser(pg, ctx -> {
            if (PgCatalogProbes.tableExists(ctx, "nexus", "aspect_extraction_queue")) {
                ctx.deleteFrom(QUEUE_TABLE).execute();
            }
            return null;
        });
        migrateWithin(ds, 120);
        assertThat(changesetRecorded(RETIRE_CHANGESET)).as("the retry recorded %s", RETIRE_CHANGESET).isTrue();
        boolean tableLeft = asSuperuser(pg, ctx -> PgCatalogProbes.tableExists(ctx, "nexus", "aspect_extraction_queue"));
        assertThat(tableLeft).as("the retired table is gone").isFalse();
    }

    /**
     * nexus-f3yqd.3: aspects-005-2 holds the unrun set non-empty on every boot of a store whose aspect queue still has
     * rows, and its body touches no chunk leaf, so it cannot conflict with a build. A boot with only it pending keeps
     * the in-flight build and says why.
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void migrate_withOnlyASelfRetryingChangesetPending_keepsAnInFlightBuilder() throws Exception {
        String nonce = "c0ffee04";
        String builderName = PciBuilderSession.builderApplicationName(nonce);
        String coll = "code__pcitrm-retry__voyage-code-3__v1";
        String index = PciCatalog.indexName(M1024, T1, coll);
        PciCatalog.Leaf leaf = leaf();

        try (HikariDataSource migrationDs = migrationPool()) {
            try {
                stageRetireChangesetPending();
                try (PciBuilderSession.Pass pass = builderSession(nonce).open();
                     LeafWriter writer = new LeafWriter(leaf)) {
                    var build = startHeldBuild(pass, leaf, coll);

                    List<String> logs = captureLogs(SchemaMigrator.class, () -> migrateWithin(migrationDs, 120));

                    assertThat(logs).noneMatch(l -> l.contains("event=pci_builders_terminated"));
                    assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_kept") && l.contains("count=1")
                        && l.contains("retrying=1"));
                    assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_kept_despite")
                        && l.contains("aspects-005-retire-aspect-queue-table.xml::aspects-005-2")
                        && l.contains("reason=self_retrying_precondition"));
                    assertThat(changesetRecorded(RETIRE_CHANGESET)).as("still unrecorded: it retries").isFalse();
                    assertThat(backendsNamed(builderName)).as("the builder's backend is still there").isEqualTo(1);
                    assertThat(holdersOf(PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY)).isEqualTo(1);
                    assertThat(build.isDone()).as("and still building").isFalse();

                    writer.close();
                    assertThat(build.get(60, TimeUnit.SECONDS)).isEqualTo(DdlOutcome.DONE);
                    assertThat(indexValid(index)).as("the build ran to its end").isTrue();
                }
            } finally {
                drainAndRetireTheStagedQueue(migrationDs);
            }
        }
    }

    /** The control: the same staged changeset plus a real pending one still ends the build, and counts only the real one. */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void migrate_withASelfRetryingAndARealChangesetPending_endsTheBuilder_countingOnlyTheRealOne() throws Exception {
        String nonce = "c0ffee05";
        String builderName = PciBuilderSession.builderApplicationName(nonce);
        String coll = "code__pcitrm-retryend__voyage-code-3__v1";
        PciCatalog.Leaf leaf = leaf();

        try (HikariDataSource migrationDs = migrationPool()) {
            try {
                stageRetireChangesetPending();
                try (PciBuilderSession.Pass pass = builderSession(nonce).open();
                     LeafWriter writer = new LeafWriter(leaf)) {
                    var build = startHeldBuild(pass, leaf, coll);
                    forgetAChangeset();

                    List<String> logs = captureLogs(SchemaMigrator.class, () -> migrateWithin(migrationDs, 120));

                    assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_terminated") && l.contains("count=1")
                        && l.contains("reason=schema_changesets_pending") && l.contains("n=1"));
                    assertThat(logs).noneMatch(l -> l.contains("event=pci_builders_kept"));
                    assertThat(build.get(30, TimeUnit.SECONDS)).isEqualTo(DdlOutcome.CONNECTION_LOST);
                    awaitBuilderGone(builderName);
                    writer.close();
                }
            } finally {
                drainAndRetireTheStagedQueue(migrationDs);
            }
        }
    }

    /**
     * The one non-runAlways step that follows the walk: the partition access repair runs {@code ALTER TABLE ... ENABLE
     * ROW LEVEL SECURITY} on a leaf (ACCESS EXCLUSIVE), which waits behind a build's ShareUpdateExclusiveLock while the
     * build waits for the repair's snapshot. A boot that keeps the builder must still end it before such a repair.
     */
    @Test
    @Timeout(value = 300, unit = TimeUnit.SECONDS)
    void migrate_withOnlyRunAlwaysPending_butAccessDriftToRepair_endsTheBuilderBeforeTheRepair() throws Exception {
        String nonce = "c0ffee03";
        String builderName = PciBuilderSession.builderApplicationName(nonce);
        String coll = "code__pcitrm-drift__voyage-code-3__v1";
        PciCatalog.Leaf leaf = leaf();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.setRowSecurity(su, DSL.table(DSL.name(leaf.schema(), leaf.name())),
                PgContainerHelper.RowSecurityMode.DISABLE);
        }

        try (HikariDataSource migrationDs = migrationPool();
             PciBuilderSession.Pass pass = builderSession(nonce).open();
             SnapshotHolder writer = new SnapshotHolder()) {
            var build = startHeldBuild(pass, leaf, coll);

            List<String> logs = captureLogs(SchemaMigrator.class, () -> migrateWithin(migrationDs, 120));

            assertThat(logs).anyMatch(l -> l.contains("event=partition_access_drift_repaired"));
            assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_terminated") && l.contains("count=1")
                && l.contains("reason=partition_access_drift"));
            assertThat(build.get(30, TimeUnit.SECONDS)).isEqualTo(DdlOutcome.CONNECTION_LOST);
            awaitBuilderGone(builderName);
            Boolean rls = asSuperuser(pg, ctx -> ctx.select(DSL.field(DSL.name("c", "relrowsecurity"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n"))
                .on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq(leaf.schema()))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(leaf.name()))
                .fetchOne(0, Boolean.class));
            assertThat(rls).as("the repair ran").isTrue();
            writer.close();
        }
    }
}
