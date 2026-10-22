// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.PgContainerHelper;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.slf4j.LoggerFactory;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.Duration;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;
import java.util.function.UnaryOperator;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-5tykh: {@link EngineSingletonLock} against a real Postgres. Every connection here is opened outside any
 * pool, the way {@code Main} opens the engine's. The monitor tests drive {@code checkOnce()} directly with an
 * hour-long interval so no timing decides the outcome; one test lets the scheduler fire it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EngineSingletonLockIntegrationTest {

    private PostgreSQLContainer<?> pg;
    private final List<AutoCloseable> toClose = new CopyOnWriteArrayList<>();

    @BeforeAll
    void startPg() {
        pg = PgContainerHelper.start();
    }

    @AfterAll
    void stopPg() {
        if (pg != null) {
            pg.stop();
        }
    }

    @AfterEach
    void closeEverything() throws Exception {
        for (AutoCloseable c : toClose) {
            try {
                c.close();
            } catch (Exception ignored) {
                // best effort
            }
        }
        toClose.clear();
        opened.clear();
    }

    private EngineSingletonLock.ConnectionOpener opener(String applicationName) {
        return () -> {
            Properties props = new Properties();
            props.setProperty("user", pg.getUsername());
            props.setProperty("password", pg.getPassword());
            props.setProperty("ApplicationName", applicationName);
            Connection c = DriverManager.getConnection(pg.getJdbcUrl(), props);
            toClose.add(c);
            opened.add(c);
            return c;
        };
    }

    /** Every connection {@link #opener} made, in order: the first of an engine's is its lock connection. */
    private final List<Connection> opened = new CopyOnWriteArrayList<>();

    private EngineSingletonLock acquire(String applicationName, Duration wait) throws Exception {
        EngineSingletonLock lock = EngineSingletonLock.acquire(opener(applicationName), applicationName, wait,
            Duration.ofMillis(50));
        toClose.add(lock);
        return lock;
    }

    // The test's own view of Postgres, written independently of the class under test and typed (no SQL text).
    private static final Table<?> PG_LOCKS = DSL.table(DSL.name("pg_catalog", "pg_locks"));
    private static final Table<?> PG_ACTIVITY = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));
    private static final Field<Integer> PID = DSL.field(DSL.name("pid"), Integer.class);

    private static Condition theLocksRow() {
        return DSL.field(DSL.name("locktype"), String.class).eq("advisory")
            .and(DSL.field(DSL.name("granted"), Boolean.class).isTrue())
            .and(DSL.field(DSL.name("objsubid"), Integer.class).cast(SQLDataType.INTEGER).eq(1))
            .and(DSL.field(DSL.name("classid"), Long.class).cast(SQLDataType.BIGINT)
                .eq(EngineSingletonLock.LOCK_KEY >>> 32))
            .and(DSL.field(DSL.name("objid"), Long.class).cast(SQLDataType.BIGINT)
                .eq(EngineSingletonLock.LOCK_KEY & 0xFFFFFFFFL));
    }

    /** A connection for the test's own probes, not registered for teardown: the caller closes it. */
    private Connection probeConnection() throws SQLException {
        Properties props = new Properties();
        props.setProperty("user", pg.getUsername());
        props.setProperty("password", pg.getPassword());
        props.setProperty("ApplicationName", "test-probe");
        return DriverManager.getConnection(pg.getJdbcUrl(), props);
    }

    private <T> T probe(java.util.function.Function<DSLContext, T> query) throws SQLException {
        try (Connection c = probeConnection()) {
            return query.apply(DSL.using(c, SQLDialect.POSTGRES));
        }
    }

    /** How many sessions hold the engine lock's row, cluster-wide. */
    private int lockRows() throws SQLException {
        return probe(ctx -> ctx.selectCount().from(PG_LOCKS).where(theLocksRow()).fetchOne(0, Integer.class));
    }

    /** The pid of the backend holding the engine lock, or null. */
    private Integer holderPid() throws SQLException {
        return probe(ctx -> ctx.select(PID).from(PG_LOCKS).where(theLocksRow()).fetchOne(PID));
    }

    private String applicationNameOf(int pid) throws SQLException {
        return probe(ctx -> ctx.select(DSL.field(DSL.name("application_name"), String.class)).from(PG_ACTIVITY)
            .where(PID.eq(pid)).fetchOne(0, String.class));
    }

    private String clientHostOf(int pid) throws SQLException {
        return probe(ctx -> ctx.select(DSL.function("host", SQLDataType.VARCHAR,
            DSL.field(DSL.name("client_addr")))).from(PG_ACTIVITY).where(PID.eq(pid)).fetchOne(0, String.class));
    }

    private int backendCount(int pid) throws SQLException {
        return probe(ctx -> ctx.selectCount().from(PG_ACTIVITY).where(PID.eq(pid)).fetchOne(0, Integer.class));
    }

    private static boolean tryLockOn(Connection c) {
        return Boolean.TRUE.equals(DSL.using(c, SQLDialect.POSTGRES)
            .select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN, DSL.val(EngineSingletonLock.LOCK_KEY)))
            .fetchOne(0, Boolean.class));
    }

    // ── boot: acquire, refuse, release ───────────────────────────────────────────────────────────────────────

    @Test
    void aFirstEngineAcquiresTheLockOnItsOwnBackend() throws Exception {
        acquire("engine-a/singleton-lock", Duration.ZERO);

        assertThat(lockRows()).isEqualTo(1);
        assertThat(applicationNameOf(holderPid()))
            .as("the holder is identifiable by the name it connected with").isEqualTo("engine-a/singleton-lock");
    }

    @Test
    void aSecondEngineRefusesAfterTheBoundedWait_andTheMessageNamesTheHolderByPidAndClientAddr() throws Exception {
        acquire("engine-a/singleton-lock", Duration.ZERO);
        int holderPid = holderPid();
        String holderAddr = clientHostOf(holderPid);
        assertThat(holderAddr).as("the test's own connection came in over TCP").isNotBlank();

        long start = System.nanoTime();
        assertThatThrownBy(() -> acquire("engine-b/singleton-lock", Duration.ofMillis(700)))
            .isInstanceOfSatisfying(EngineSingletonLock.HeldElsewhereException.class, e -> {
                // Pinned for the operator who must pg_terminate_backend a half-open connection (conexus, 2026-10-22).
                assertThat(e.getMessage()).contains("holder_pid=" + holderPid)
                    .contains("holder_client_addr=" + holderAddr)
                    .contains("holder_application_name=engine-a/singleton-lock")
                    .contains("holder_backend_start=")
                    .contains("event=engine_singleton_lock_refused");
                assertThat(e.holder().pid()).isEqualTo(holderPid);
                assertThat(e.holder().clientAddr()).isEqualTo(holderAddr);
            });
        long waitedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);
        assertThat(waitedMs).as("it waited the bound before refusing").isGreaterThanOrEqualTo(650);
        assertThat(waitedMs).as("and did not wait much longer").isLessThan(10_000);
        assertThat(lockRows()).as("the first engine still holds it").isEqualTo(1);
        assertThat(holderPid()).isEqualTo(holderPid);
    }

    @Test
    void theRefusalMessageCarriesNoCredentials() throws Exception {
        acquire("engine-a/singleton-lock", Duration.ZERO);
        assertThatThrownBy(() -> acquire("engine-b/singleton-lock", Duration.ZERO))
            .isInstanceOfSatisfying(EngineSingletonLock.HeldElsewhereException.class, e ->
                assertThat(e.getMessage()).doesNotContain(pg.getPassword()).doesNotContain("jdbc:"));
    }

    @Test
    void closingTheFirstEngineReleasesTheLock_andTheSecondThenAcquires() throws Exception {
        EngineSingletonLock first = acquire("engine-a/singleton-lock", Duration.ZERO);
        assertThatThrownBy(() -> acquire("engine-b/singleton-lock", Duration.ZERO))
            .isInstanceOf(EngineSingletonLock.HeldElsewhereException.class);

        first.close();
        assertThat(lockRows()).isZero();

        acquire("engine-b/singleton-lock", Duration.ZERO);
        assertThat(lockRows()).isEqualTo(1);
    }

    @Test
    void aSecondEngineThatArrivesWhileTheFirstIsShuttingDownWaitsForItAndThenBoots() throws Exception {
        EngineSingletonLock first = acquire("engine-a/singleton-lock", Duration.ZERO);
        var releaser = Executors.newSingleThreadScheduledExecutor();
        toClose.add(releaser::shutdownNow);
        releaser.schedule(() -> {
            try {
                first.close();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }, 400, TimeUnit.MILLISECONDS);

        EngineSingletonLock second = acquire("engine-b/singleton-lock", Duration.ofSeconds(20));

        assertThat(second).isNotNull();
        assertThat(applicationNameOf(holderPid()))
            .isEqualTo("engine-b/singleton-lock");
    }

    @Test
    void aLockOnAnotherDatabaseOfTheClusterDoesNotCollide() throws Exception {
        // Advisory locks are per database: engines on different databases of one cluster (the test harness's
        // shape) do not see each other.
        probe(ctx -> ctx.createDatabase("singleton_other").execute());
        try {
            acquire("engine-a/singleton-lock", Duration.ZERO);
            EngineSingletonLock.ConnectionOpener other = () -> {
                Properties props = new Properties();
                props.setProperty("user", pg.getUsername());
                props.setProperty("password", pg.getPassword());
                props.setProperty("ApplicationName", "engine-other/singleton-lock");
                Connection c = DriverManager.getConnection(
                    pg.getJdbcUrl().replace("/" + pg.getDatabaseName(), "/singleton_other"), props);
                toClose.add(c);
                return c;
            };
            toClose.add(EngineSingletonLock.acquire(other, "engine-other/singleton-lock", Duration.ZERO,
                Duration.ofMillis(50)));
            assertThat(lockRows()).as("both held at once, one per database").isEqualTo(2);
        } finally {
            closeEverything();
            probe(ctx -> ctx.dropDatabaseIfExists("singleton_other").execute());
        }
    }

    // ── mid-life loss ─────────────────────────────────────────────────────────────────────────────────────────

    /** The recorded outcomes of one lock's monitor. */
    private static final class Outcomes {
        final AtomicInteger regained = new AtomicInteger();
        final List<String> fatal = new CopyOnWriteArrayList<>();
        final CountDownLatch regainedLatch = new CountDownLatch(1);
    }

    private Outcomes monitor(EngineSingletonLock lock, Duration interval, Runnable onRegained) {
        Outcomes o = new Outcomes();
        lock.startMonitor(interval, () -> {
            o.regained.incrementAndGet();
            onRegained.run();
            o.regainedLatch.countDown();
        }, o.fatal::add);
        return o;
    }

    private void terminateBackend(int pid) throws SQLException {
        Boolean signalled = probe(ctx -> ctx.select(
            DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.val(pid))).fetchOne(0, Boolean.class));
        assertThat(signalled).isTrue();
        // pg_terminate_backend signals; wait until the backend is gone so the lock is really free.
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (backendCount(pid) != 0) {
            assertThat(System.nanoTime()).as("backend " + pid + " did not exit").isLessThan(deadline);
            Thread.onSpinWait();
        }
    }

    @Test
    void aHealthyLockIsLeftAlone() throws Exception {
        EngineSingletonLock lock = acquire("engine-a/singleton-lock", Duration.ZERO);
        Outcomes o = monitor(lock, Duration.ofHours(1), () -> { });
        int pid = holderPid();

        lock.checkOnce();
        lock.checkOnce();

        assertThat(o.regained.get()).isZero();
        assertThat(o.fatal).isEmpty();
        assertThat(holderPid()).as("the same backend still holds it").isEqualTo(pid);
    }

    @Test
    void whenTheLockBackendDies_theCheckReacquiresOnAFreshBackend_andClearsTheRegistryCache() throws Exception {
        CollectionRegistry.clearForTests();
        CollectionRegistry.markKnown("t1", "docs__a__voyage-context-3__v1", row());
        assertThat(CollectionRegistry.isKnown("t1", "docs__a__voyage-context-3__v1")).isTrue();

        EngineSingletonLock lock = acquire("engine-a/singleton-lock", Duration.ZERO);
        Outcomes o = monitor(lock, Duration.ofHours(1), CollectionRegistry::evictAll);
        int before = holderPid();

        terminateBackend(before);
        assertThat(lockRows()).as("the termination released the lock").isZero();
        lock.checkOnce();

        assertThat(o.regained.get()).isEqualTo(1);
        assertThat(o.fatal).isEmpty();
        assertThat(holderPid()).as("a new backend holds the lock").isNotNull().isNotEqualTo(before);
        assertThat(CollectionRegistry.isKnown("t1", "docs__a__voyage-context-3__v1"))
            .as("another engine may have run in the gap, so the cache is gone").isFalse();
    }

    @Test
    void whenAnotherEngineTookTheLockInTheGap_theCheckReportsItAndDoesNotServeOn() throws Exception {
        EngineSingletonLock lock = acquire("engine-a/singleton-lock", Duration.ZERO);
        Outcomes o = monitor(lock, Duration.ofHours(1), () -> { });
        terminateBackend(holderPid());

        Connection rival = opener("engine-rival/singleton-lock").open();
        assertThat(tryLockOn(rival)).as("the rival took it").isTrue();
        int rivalPid = holderPid();

        lock.checkOnce();

        assertThat(o.regained.get()).isZero();
        assertThat(o.fatal).hasSize(1);
        assertThat(o.fatal.get(0)).contains("event=engine_singleton_lock_lost_to_another_engine")
            .contains("holder_pid=" + rivalPid).contains("holder_client_addr=")
            .contains("holder_application_name=engine-rival/singleton-lock");
        assertThat(holderPid()).as("the rival is untouched").isEqualTo(rivalPid);

        lock.checkOnce();
        assertThat(o.fatal).as("the verdict fires once").hasSize(1);
    }

    @Test
    void aStaleBackendOfThisSameEngineIsTerminated_notMistakenForAnotherEngine() throws Exception {
        // A network drop leaves the engine's old lock backend alive in Postgres until it notices the dead socket.
        // Its application_name is this boot's own, so the re-acquire ends it rather than exiting the engine.
        EngineSingletonLock lock = acquire("engine-a/singleton-lock", Duration.ZERO);
        Outcomes o = monitor(lock, Duration.ofHours(1), () -> { });
        int original = holderPid();

        // The engine believes it lost the lock (its connection was cut) while the old backend lives on holding it.
        lock.severConnectionForTests();
        assertThat(lockRows()).as("the old backend still holds it").isEqualTo(1);
        assertThat(holderPid()).isEqualTo(original);

        lock.checkOnce();

        assertThat(o.fatal).isEmpty();
        assertThat(o.regained.get()).isEqualTo(1);
        assertThat(holderPid()).as("the stale backend was replaced").isNotEqualTo(original);
    }

    @Test
    void anUnreachableDatabaseIsNotAVerdict_theNextCheckTriesAgain() throws Exception {
        var failNext = new AtomicBoolean(false);
        EngineSingletonLock.ConnectionOpener flaky = () -> {
            if (failNext.compareAndSet(true, false)) {
                throw new SQLException("simulated: connection refused");
            }
            return opener("engine-a/singleton-lock").open();
        };
        EngineSingletonLock lock = EngineSingletonLock.acquire(flaky, "engine-a/singleton-lock", Duration.ZERO,
            Duration.ofMillis(50));
        toClose.add(lock);
        Outcomes o = monitor(lock, Duration.ofHours(1), () -> { });
        lock.severConnectionForTests();
        failNext.set(true);

        lock.checkOnce();   // the opener throws

        assertThat(o.fatal).isEmpty();
        assertThat(o.regained.get()).isZero();

        lock.checkOnce();   // the database is back; the stale backend of ours still holds the lock

        assertThat(o.fatal).isEmpty();
        assertThat(o.regained.get()).isEqualTo(1);
        assertThat(lockRows()).isEqualTo(1);
    }

    @Test
    void theScheduledMonitorRunsTheCheckOnItsOwn() throws Exception {
        EngineSingletonLock lock = acquire("engine-a/singleton-lock", Duration.ZERO);
        Outcomes o = monitor(lock, Duration.ofSeconds(1), () -> { });
        terminateBackend(holderPid());

        assertThat(o.regainedLatch.await(30, TimeUnit.SECONDS)).as("the scheduler fired the check").isTrue();
        assertThat(o.fatal).isEmpty();
        assertThat(lockRows()).isEqualTo(1);
    }

    @Test
    void aClosedLockIsNotChecked() throws Exception {
        EngineSingletonLock lock = acquire("engine-a/singleton-lock", Duration.ZERO);
        Outcomes o = monitor(lock, Duration.ofHours(1), () -> { });
        lock.close();

        lock.checkOnce();

        assertThat(o.regained.get()).isZero();
        assertThat(o.fatal).isEmpty();
        assertThat(lockRows()).as("a closed lock is not re-taken").isZero();
    }

    // ── fix round: nexus-5tykh review ────────────────────────────────────────────────────────────────────────

    /**
     * A Connection whose prepared statements go to the connection {@code route} picks, with their SQL as
     * {@code rewrite} returns it: stands in for a pooler (statements land on different backends) or for a server
     * that fails or ignores one statement. Closing it closes every connection it may route to.
     */
    private static Connection routed(Connection primary, Function<String, Connection> route,
                                     UnaryOperator<String> rewrite, Connection... others) {
        InvocationHandler h = (proxy, method, args) -> {
            try {
                if (method.getName().equals("close")) {
                    for (Connection o : others) {
                        o.close();
                    }
                    primary.close();
                    return null;
                }
                if (method.getName().equals("prepareStatement") && args != null && args[0] instanceof String sql) {
                    Object[] a = args.clone();
                    a[0] = rewrite.apply(sql);
                    return method.invoke(route.apply(sql), a);
                }
                return method.invoke(primary, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        };
        return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(),
            new Class<?>[] {Connection.class}, h);
    }

    private ListAppender<ILoggingEvent> captureLogs() {
        Logger logger = (Logger) LoggerFactory.getLogger(EngineSingletonLock.class);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        toClose.add(() -> logger.detachAppender(appender));
        return appender;
    }

    private static long count(ListAppender<ILoggingEvent> logs, String needle) {
        return logs.list.stream().filter(e -> e.getFormattedMessage().contains(needle)).count();
    }

    private static final String SELECT_FALSE_FOR_TWO_BINDS =
        "select false from (select 1) x where ?::text is not null and ?::text is not null";

    @Test
    void aVerdictOfAnotherEngineNeedsAHolderThatIsIdentifiedAsForeign_aFailedLookupIsNotOne() throws Exception {
        // nexus-5tykh review: describeHolder answers UNKNOWN when its query throws, and an unknown holder is not
        // evidence of another engine. The old stale backend of ours still holds the lock here.
        AtomicBoolean lookupBroken = new AtomicBoolean(true);
        EngineSingletonLock.ConnectionOpener o = () -> {
            Connection c = opener("engine-a/singleton-lock").open();
            return routed(c, sql -> c, sql -> lookupBroken.get() && sql.contains("pg_locks")
                && sql.contains("pg_stat_activity") ? "select 1/0" : sql);
        };
        EngineSingletonLock lock = EngineSingletonLock.acquire(o, "engine-a/singleton-lock", Duration.ZERO,
            Duration.ofMillis(50));
        toClose.add(lock);
        Outcomes out = monitor(lock, Duration.ofHours(1), () -> { });
        int original = holderPid();
        lock.severConnectionForTests();

        lock.checkOnce();

        assertThat(out.fatal).as("an unidentified holder is no verdict").isEmpty();
        assertThat(out.regained.get()).isZero();
        assertThat(holderPid()).as("nothing was terminated on a guess").isEqualTo(original);

        lookupBroken.set(false);
        lock.checkOnce();   // the lookup works: the holder is ours, so it is ended and the lock taken back

        assertThat(out.fatal).isEmpty();
        assertThat(out.regained.get()).isEqualTo(1);
        assertThat(holderPid()).isNotEqualTo(original);
    }

    @Test
    void aHolderCarryingOurOwnNameThatWillNotGoIsNotAVerdictEither_theNextCheckTriesAgain() throws Exception {
        AtomicBoolean terminationIgnored = new AtomicBoolean(true);
        EngineSingletonLock.ConnectionOpener o = () -> {
            Connection c = opener("engine-a/singleton-lock").open();
            return routed(c, sql -> c, sql -> terminationIgnored.get() && sql.contains("pg_terminate_backend")
                ? SELECT_FALSE_FOR_TWO_BINDS : sql);
        };
        EngineSingletonLock lock = EngineSingletonLock.acquire(o, "engine-a/singleton-lock", Duration.ZERO,
            Duration.ofMillis(50)).withStaleTerminationWait(Duration.ofMillis(300));
        toClose.add(lock);
        Outcomes out = monitor(lock, Duration.ofHours(1), () -> { });
        int original = holderPid();
        lock.severConnectionForTests();

        lock.checkOnce();

        assertThat(out.fatal).as("our own name is never another engine").isEmpty();
        assertThat(out.regained.get()).isZero();
        assertThat(holderPid()).isEqualTo(original);

        terminationIgnored.set(false);
        lock.checkOnce();

        assertThat(out.fatal).isEmpty();
        assertThat(out.regained.get()).isEqualTo(1);
        assertThat(holderPid()).isNotEqualTo(original);
    }

    @Test
    void theStaleTerminationEndsABackendOnlyWhileItStillCarriesTheNameItWasChosenFor() throws Exception {
        // pid reuse: between the lookup and the terminate the pid can belong to another process. The name is
        // tested in the terminating statement itself.
        Connection victim = opener("engine-b/singleton-lock").open();
        int victimPid = DSL.using(victim, SQLDialect.POSTGRES)
            .select(DSL.function("pg_backend_pid", SQLDataType.INTEGER)).fetchOne(0, Integer.class);
        Connection actor = opener("engine-a/singleton-lock").open();

        assertThat(EngineSingletonLock.terminateBackendNamed(actor, victimPid, "engine-a/singleton-lock"))
            .as("the pid now carries another name: not ended").isFalse();
        assertThat(backendCount(victimPid)).isEqualTo(1);

        assertThat(EngineSingletonLock.terminateBackendNamed(actor, victimPid, "engine-b/singleton-lock")).isTrue();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (backendCount(victimPid) != 0) {
            assertThat(System.nanoTime()).as("backend did not exit").isLessThan(deadline);
            Thread.onSpinWait();
        }
    }

    @Test
    void aCloseThatLandsDuringACheckEndsItQuietly_neverAVerdictAgainstTheSuccessorEngine() throws Exception {
        // A clean shutdown: close() releases the lock and the successor engine takes it while this engine's
        // check is mid-flight. That check must not log lost_to_another_engine or call onFatal.
        AtomicBoolean blockNext = new AtomicBoolean(false);
        CountDownLatch opening = new CountDownLatch(1);
        CountDownLatch proceed = new CountDownLatch(1);
        EngineSingletonLock.ConnectionOpener o = () -> {
            if (blockNext.compareAndSet(true, false)) {
                opening.countDown();
                try {
                    proceed.await(30, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            return opener("engine-a/singleton-lock").open();
        };
        var logs = captureLogs();
        EngineSingletonLock lock = EngineSingletonLock.acquire(o, "engine-a/singleton-lock", Duration.ZERO,
            Duration.ofMillis(50));
        toClose.add(lock);
        Outcomes out = monitor(lock, Duration.ofHours(1), () -> { });
        terminateBackend(holderPid());
        blockNext.set(true);
        var check = Executors.newSingleThreadExecutor();
        toClose.add(check::shutdownNow);
        var running = check.submit(lock::checkOnce);
        assertThat(opening.await(30, TimeUnit.SECONDS)).as("the check reached its re-acquire").isTrue();

        lock.close();
        Connection successor = opener("engine-successor/singleton-lock").open();
        assertThat(tryLockOn(successor)).isTrue();
        int successorPid = holderPid();
        proceed.countDown();
        running.get(30, TimeUnit.SECONDS);

        assertThat(out.fatal).isEmpty();
        assertThat(out.regained.get()).isZero();
        assertThat(count(logs, "lost_to_another_engine")).isZero();
        assertThat(holderPid()).as("the successor is untouched").isEqualTo(successorPid);
    }

    @Test
    void aLockLostWhileTheConnectionLivesIsFoundByTheHolderRowNotByTheConnectionAlone() throws Exception {
        // The engine's connection is alive and no longer holds the lock (here: released on it, then a rival took
        // it). A check that only asked "is the connection up" or matched the lock row without this backend's
        // pid would call it held.
        EngineSingletonLock lock = acquire("engine-a/singleton-lock", Duration.ZERO);
        Outcomes out = monitor(lock, Duration.ofHours(1), () -> { });
        Connection engineConn = opened.get(0);
        Boolean released = DSL.using(engineConn, SQLDialect.POSTGRES)
            .select(DSL.function("pg_advisory_unlock", SQLDataType.BOOLEAN, DSL.val(EngineSingletonLock.LOCK_KEY)))
            .fetchOne(0, Boolean.class);
        assertThat(released).isTrue();
        Connection rival = opener("engine-rival/singleton-lock").open();
        assertThat(tryLockOn(rival)).isTrue();
        int rivalPid = holderPid();

        lock.checkOnce();

        assertThat(out.fatal).hasSize(1);
        assertThat(out.fatal.get(0)).contains("holder_pid=" + rivalPid);
    }

    @Test
    void theHolderLookupIgnoresAHolderOnAnotherDatabaseOfTheCluster() throws Exception {
        probe(ctx -> ctx.createDatabase("singleton_other_lookup").execute());
        try {
            EngineSingletonLock.ConnectionOpener other = () -> {
                Properties props = new Properties();
                props.setProperty("user", pg.getUsername());
                props.setProperty("password", pg.getPassword());
                props.setProperty("ApplicationName", "engine-other/singleton-lock");
                Connection c = DriverManager.getConnection(
                    pg.getJdbcUrl().replace("/" + pg.getDatabaseName(), "/singleton_other_lookup"), props);
                toClose.add(c);
                return c;
            };
            toClose.add(EngineSingletonLock.acquire(other, "engine-other/singleton-lock", Duration.ZERO,
                Duration.ofMillis(50)));
            assertThat(lockRows()).as("the other database's engine holds the key").isEqualTo(1);

            Connection here = opener("engine-here/singleton-lock").open();

            assertThat(EngineSingletonLock.describeHolder(here).pid())
                .as("nobody holds it on this database").isNull();
            assertThat(EngineSingletonLock.stillHeld(here)).isFalse();
        } finally {
            closeEverything();
            probe(ctx -> ctx.dropDatabaseIfExists("singleton_other_lookup").execute());
        }
    }

    @Test
    void theProductionOpenerCarriesItsApplicationNameAndABoundedSocket() throws Exception {
        String name = "engine-j/singleton-lock";
        EngineSingletonLock.ConnectionOpener jdbc = EngineSingletonLock.jdbcOpener(pg.getJdbcUrl(), pg.getUsername(),
            pg.getPassword(), name);

        EngineSingletonLock lock = EngineSingletonLock.acquire(jdbc, name, Duration.ZERO, Duration.ofMillis(50));
        toClose.add(lock);

        assertThat(applicationNameOf(holderPid())).isEqualTo(name);
        Connection c = jdbc.open();
        toClose.add(c);
        assertThat(c.getNetworkTimeout()).as("socketTimeout=30 s reached the connection").isEqualTo(30_000);
    }

    @Test
    void aDatabaseErrorAtBootIsAnSqlException_notABareJooqRuntimeException() throws Exception {
        Connection dead = opener("engine-a/singleton-lock").open();
        dead.close();
        AtomicInteger opens = new AtomicInteger();

        assertThatThrownBy(() -> EngineSingletonLock.acquire(() -> {
            opens.incrementAndGet();
            return dead;
        }, "engine-a/singleton-lock", Duration.ZERO, Duration.ofMillis(50)))
            .as("Main catches SQLException and exits 1 with a structured event")
            .isInstanceOf(SQLException.class);
        assertThat(opens.get()).isEqualTo(1);
    }

    @Test
    void aLockTakenThroughAPoolerThatCannotShowItAsHeldByTheConnectionIsRefusedAtBoot() throws Exception {
        // Statements land on different backends: the lock is taken on one, the holder row is read on another.
        Connection holder = opener("engine-a/singleton-lock").open();
        Connection other = opener("engine-a/singleton-lock").open();
        EngineSingletonLock.ConnectionOpener pooled = () ->
            routed(holder, sql -> sql.contains("pg_locks") ? other : holder, sql -> sql, other);

        assertThatThrownBy(() -> EngineSingletonLock.acquire(pooled, "engine-a/singleton-lock", Duration.ZERO,
            Duration.ofMillis(50)))
            .isInstanceOfSatisfying(EngineSingletonLock.PoolerSuspectedException.class, e ->
                assertThat(e.getMessage()).contains("event=engine_singleton_lock_pooler_suspected")
                    .contains("pooler").contains("NX_ENGINE_SINGLETON_LOCK=off")
                    .contains("NX_PGBOUNCER_ADMIN_URL"));
        assertThat(lockRows()).as("the refused boot holds nothing").isZero();
    }

    @Test
    void aBackendThatChangesBetweenTwoStatementsOfTheSameConnectionIsRefusedAtBoot() throws Exception {
        Connection holder = opener("engine-a/singleton-lock").open();
        Connection other = opener("engine-a/singleton-lock").open();
        AtomicInteger pidAsks = new AtomicInteger();
        EngineSingletonLock.ConnectionOpener pooled = () -> routed(holder, sql -> {
            if (sql.contains("pg_backend_pid") && !sql.contains("pg_locks")) {
                return pidAsks.incrementAndGet() == 1 ? holder : other;
            }
            return holder;
        }, sql -> sql, other);

        assertThatThrownBy(() -> EngineSingletonLock.acquire(pooled, "engine-a/singleton-lock", Duration.ZERO,
            Duration.ofMillis(50)))
            .isInstanceOf(EngineSingletonLock.PoolerSuspectedException.class);
        assertThat(pidAsks.get()).isEqualTo(2);
    }

    @Test
    void aWaitingBootLogsAProgressLineEveryIntervalWithTheHolder_soASupervisorTailingTheLogSeesLife() throws Exception {
        acquire("engine-a/singleton-lock", Duration.ZERO);
        var logs = captureLogs();

        assertThatThrownBy(() -> EngineSingletonLock.acquire(opener("engine-b/singleton-lock"),
            "engine-b/singleton-lock", Duration.ofMillis(1300), Duration.ofMillis(50), Duration.ofMillis(300)))
            .isInstanceOf(EngineSingletonLock.HeldElsewhereException.class);

        assertThat(count(logs, "event=engine_singleton_lock_waiting")).as("first line plus one per interval")
            .isGreaterThanOrEqualTo(4);
        assertThat(logs.list.stream().filter(e -> e.getFormattedMessage().contains("event=engine_singleton_lock_waiting"))
            .allMatch(e -> e.getFormattedMessage().contains("holder_pid=")
                && e.getFormattedMessage().contains("elapsed_s=") && e.getFormattedMessage().contains("wait_s=")))
            .isTrue();
    }

    @Test
    void theRefusalNamesTheLocalStopCommand_andStaysInsideTheSupervisorsReasonCap() throws Exception {
        // The supervisor's last_engine_error_line keeps 600 characters of the engine's ERROR line, logback prefix
        // included (about 90 here). A longer line loses its tail, which is the remedy.
        acquire("engine-a/" + "x".repeat(60), Duration.ZERO);
        assertThatThrownBy(() -> acquire("engine-b/singleton-lock", Duration.ZERO))
            .isInstanceOfSatisfying(EngineSingletonLock.HeldElsewhereException.class, e -> {
                assertThat(e.getMessage()).contains("nx daemon service stop")
                    .contains("pg_terminate_backend").contains("NX_ENGINE_SINGLETON_LOCK=off");
                assertThat(e.getMessage().length()).isLessThanOrEqualTo(480);
            });
    }

    private static CollectionRow row() {
        return new CollectionRow("docs", "owner", "voyage-context-3", 1024, "live");
    }
}
