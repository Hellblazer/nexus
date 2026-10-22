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
    }

    private EngineSingletonLock.ConnectionOpener opener(String applicationName) {
        return () -> {
            Properties props = new Properties();
            props.setProperty("user", pg.getUsername());
            props.setProperty("password", pg.getPassword());
            props.setProperty("ApplicationName", applicationName);
            Connection c = DriverManager.getConnection(pg.getJdbcUrl(), props);
            toClose.add(c);
            return c;
        };
    }

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

    private static CollectionRow row() {
        return new CollectionRow("docs", "owner", "voyage-context-3", 1024, "live");
    }
}
