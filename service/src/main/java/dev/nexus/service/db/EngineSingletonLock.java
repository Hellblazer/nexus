// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.Duration;
import java.util.Objects;
import java.util.Optional;
import java.util.Properties;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.function.Function;

/**
 * nexus-5tykh: one engine JVM per database, enforced. A session-level PostgreSQL advisory lock, taken at boot on a
 * connection of its own and held for the JVM's life. A second engine serving the same database does not get it and
 * refuses to start.
 *
 * <p><b>Why.</b> {@link CollectionRegistry} caches collection rows with no TTL, and its correctness rests on one
 * engine per database (its class doc). A second engine, as in a blue/green overlap, would keep serving a live row
 * for a name the first had superseded and would accept writes to the tombstone, silently. Production redeploys
 * stop-then-start; this makes a violation loud instead of silent, and it also enforces stop-then-start itself.
 *
 * <p><b>The connection.</b> Opened outside the HikariCP pool and kept for the life of the process: a pooled
 * connection is recycled (max lifetime, idle eviction) and a recycled connection drops a session lock without a
 * word. A session advisory lock lives and dies with its backend, so a crashed or killed engine frees it as soon as
 * Postgres sees the socket close. A half-open connection (the engine's host vanished) keeps the lock until Postgres
 * notices; the refusal names the holder's backend pid and address so an operator can
 * {@code pg_terminate_backend} it.
 *
 * <p><b>The key.</b> {@link #LOCK_KEY}: the first eight bytes of SHA-256 of {@link #KEY_NAME}, as a signed
 * big-endian long. It is the one-bigint form, {@code pg_try_advisory_lock(bigint)}. The only other advisory locks
 * the engine takes are {@code pg_advisory_xact_lock(hashtext(...))}, an int4 widened to bigint, whose high word is 0
 * or -1; this key's is neither, which {@code EngineSingletonLockSettingsTest} pins. The bigint session keys the
 * engine holds are {@link SchemaMigrator#MIGRATION_ADVISORY_LOCK_KEY} (the migration walk's, held only while it
 * runs) and {@code PciBuilderSession.PCI_BUILDER_ADVISORY_LOCK_KEY} (the index builder's, held for a pass); all
 * three differ, and the test finds every {@code *_ADVISORY_LOCK_KEY} constant in the main sources, so a fourth
 * cannot be added unchecked. Advisory locks are per database, so engines on different databases of one cluster
 * (the test harness's shape) never meet.
 *
 * <p><b>Boot.</b> {@link #acquire} polls {@code pg_try_advisory_lock} for up to {@link Settings#bootWait()} (default 30
 * s), so a stop-then-start whose old process is still shutting down does not fail the deploy. The wait logs a line
 * at the start and one every {@link #WAIT_PROGRESS_INTERVAL}, each naming the holder; the local supervisor counts
 * any new engine log line as progress ({@code readiness.py}), so those lines are also what keeps its 60 s
 * pre-migration deadline from firing during a long wait. On timeout it throws {@link HeldElsewhereException};
 * {@code Main} logs its message at ERROR and exits 1. The message names the holder from {@code pg_locks} joined to
 * {@code pg_stat_activity}: pid, client address, application name, backend start. Never a credential. Once the lock
 * is taken, {@link #acquire} checks that it shows as held by this connection's own backend and that the backend
 * does not change between statements; a connection that fails that is going through a pooler, and the boot is
 * refused with {@link PoolerSuspectedException} rather than exiting about one check interval later as a loss.
 *
 * <p><b>Mid-life loss.</b> A PG restart or a network drop ends the lock backend and releases the lock. A check on
 * its own thread ({@link #startMonitor}, every {@link Settings#checkInterval()}) asks whether this connection still
 * holds the lock. If not, it tries to take it back once per tick. Outcomes. Taken: {@code onRegained} runs (the
 * registry cache is cleared, because another engine may have run in the gap). Held by a backend that is positively
 * another engine's (its pid is known and its application name is not this boot's own) and still held on a second
 * try: the check reports it and {@code onFatal} runs, which exits non-zero rather than serve on. Held by a backend
 * whose application name is this boot's own, a stale backend of ours that Postgres has not yet noticed is dead:
 * that one is terminated, the name tested in the terminating statement itself, and the lock taken; if it does not
 * go within {@link #STALE_TERMINATION_WAIT} the check tries again next tick. Database unreachable, holder lookup
 * failed, or no holder found: no verdict, the next tick tries again. A check that meets {@link #close} ends without
 * a verdict, so a clean shutdown never reports a loss against the engine that follows it.
 *
 * <p><b>Detection window.</b> Loss is detected, not prevented. An engine that lost the lock to a Postgres restart
 * or a network drop learns of it at its next check, so two engines can serve for up to the check interval (default
 * 10 s, at most {@value #MAX_CHECK_SECONDS} s) plus the connection timeouts ({@value #CONNECT_TIMEOUT_SECONDS} s to
 * connect, {@value #SOCKET_TIMEOUT_SECONDS} s per statement). The lock refuses a second engine at boot and ends a
 * split that outlasts that window; it does not close the window.
 *
 * <p><b>Cost of a check.</b> {@code pg_locks} is a snapshot of the whole lock table, so a check costs more the more
 * locks the cluster holds (the RDR-225 walk holds one per leaf until it commits). Measured on pgvector/pg17, the
 * lock table sized for it (nexus-5tykh review): 0.6 ms with the table idle, 2 ms with 6,000 locks held (the default
 * table, 64 x 100 slots, is full at 6,400), 33 ms with 100,000. A re-entrant {@code pg_try_advisory_lock} plus unlock
 * would be cheaper but cannot tell "still held" from "just taken", so it cannot find a lock this connection no
 * longer holds; the exact check stays.
 *
 * <p><b>Thread.</b> The check does not ride {@code NexusService}'s {@code sweepScheduler}: that is one thread shared
 * with sweeps that run for minutes, and a loss that waits behind them is a longer window for two engines. It has a
 * daemon thread of its own.
 *
 * <p><b>Behind a transaction-mode pooler</b> a session lock is not held by any one backend and the check would see
 * it lost on every tick. {@link Mode#POOLED_SKIP} (an {@code NX_PGBOUNCER_ADMIN_URL} is set) skips the lock with a
 * WARN, and {@code NX_ENGINE_SINGLETON_LOCK=off} is the operator's explicit opt-out. The managed cloud connects
 * straight to Postgres (conexus, 2026-10-22).
 */
public final class EngineSingletonLock implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(EngineSingletonLock.class);

    /** {@code on} (default, unset or blank) or {@code off}. Anything else refuses to boot. */
    public static final String MODE_ENV = "NX_ENGINE_SINGLETON_LOCK";
    /** Whole seconds, 0..600, default 30: how long boot waits for a holder to release before refusing. */
    public static final String WAIT_ENV = "NX_ENGINE_SINGLETON_LOCK_WAIT_SECONDS";
    /** Whole seconds, 1..{@link #MAX_CHECK_SECONDS}, default 10: how often the held lock is checked. */
    public static final String CHECK_ENV = "NX_ENGINE_SINGLETON_LOCK_CHECK_SECONDS";

    /** The longest check interval: it bounds how long two engines can serve before one notices. */
    public static final int MAX_CHECK_SECONDS = 60;

    /** Seconds to connect (the lock connection's {@code connectTimeout}). */
    static final int CONNECT_TIMEOUT_SECONDS = 10;
    /** Seconds one statement may wait for the server (the lock connection's {@code socketTimeout}). */
    static final int SOCKET_TIMEOUT_SECONDS = 30;

    /** What the lock connection's name adds to the pool's: {@code <pool application_name>/singleton-lock}. */
    public static final String NAME_SUFFIX = "/singleton-lock";

    /** The string the key derives from. Never change it: two engine versions must agree on the key. */
    public static final String KEY_NAME = "nexus.engine.singleton-lock.v1";

    /** First eight bytes of SHA-256({@link #KEY_NAME}) as a big-endian long; a test recomputes it. */
    public static final long LOCK_KEY = 0x5AEEFE01512F7ACCL;

    static final Duration DEFAULT_WAIT = Duration.ofSeconds(30);
    static final Duration DEFAULT_CHECK_INTERVAL = Duration.ofSeconds(10);
    static final Duration DEFAULT_RETRY_INTERVAL = Duration.ofMillis(250);
    /** How often a waiting boot logs where it is and who holds the lock. Well under the supervisor's 60 s deadline. */
    static final Duration WAIT_PROGRESS_INTERVAL = Duration.ofSeconds(10);

    /** How long a re-acquire waits for the termination of its own stale backend to take effect. */
    private static final Duration STALE_TERMINATION_WAIT = Duration.ofSeconds(5);
    private static final Duration STALE_TERMINATION_POLL = Duration.ofMillis(100);

    /** The exit status of an engine that lost the lock to another. */
    public static final int EXIT_STATUS = 1;

    /** What {@code Main} does about the lock. */
    public enum Mode {
        /** Take the lock; refuse to boot when another engine holds it. */
        ENFORCE,
        /** {@code NX_ENGINE_SINGLETON_LOCK=off}: the operator's explicit opt-out. */
        OFF,
        /** A PgBouncer admin URL is set: a session lock is not reliable through it, so none is taken. */
        POOLED_SKIP
    }

    /** The three settings, validated. */
    public record Settings(Mode mode, Duration bootWait, Duration checkInterval) {

        /**
         * Reads {@link #MODE_ENV}, {@link #WAIT_ENV} and {@link #CHECK_ENV}; a blank value is the default, as for
         * the engine's other integer settings.
         *
         * @throws IllegalArgumentException naming the variable, for a value outside its range or vocabulary
         */
        public static Settings fromEnv(Function<String, String> env) {
            String modeRaw = blankToNull(env.apply(MODE_ENV));
            Mode mode;
            if (modeRaw == null || modeRaw.equals("on")) {
                mode = Mode.ENFORCE;
            } else if (modeRaw.equals("off")) {
                mode = Mode.OFF;
            } else {
                throw new IllegalArgumentException(MODE_ENV + " must be 'on' or 'off', got '" + modeRaw + "'");
            }
            if (mode == Mode.ENFORCE && blankToNull(env.apply(PoolerModeCheck.ADMIN_URL_ENV)) != null) {
                mode = Mode.POOLED_SKIP;
            }
            Duration wait = seconds(env, WAIT_ENV, 0, 600, DEFAULT_WAIT);
            Duration check = seconds(env, CHECK_ENV, 1, MAX_CHECK_SECONDS, DEFAULT_CHECK_INTERVAL);
            return new Settings(mode, wait, check);
        }

        private static Duration seconds(Function<String, String> env, String name, int min, int max, Duration dflt) {
            String raw = blankToNull(env.apply(name));
            if (raw == null) {
                return dflt;
            }
            int n;
            try {
                n = Integer.parseInt(raw);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(name + " must be a whole number of seconds, " + min + ".." + max
                    + ", got '" + raw + "'");
            }
            if (n < min || n > max) {
                throw new IllegalArgumentException(name + " must be " + min + ".." + max + " seconds, got " + n);
            }
            return Duration.ofSeconds(n);
        }

        private static String blankToNull(String v) {
            return v == null || v.isBlank() ? null : v.trim();
        }
    }

    /** Opens a new connection to the engine's database, outside any pool. */
    @FunctionalInterface
    public interface ConnectionOpener {
        Connection open() throws SQLException;
    }

    /**
     * Connections for the engine's own database, outside any pool: the user and password of the engine's pool, a
     * bounded connect, a socket bound so a dead network fails a check instead of hanging it, and TCP keepalive.
     * The URL and the password stay in the driver; no log line here carries either.
     */
    public static ConnectionOpener jdbcOpener(String jdbcUrl, String user, String password, String applicationName) {
        return () -> {
            Properties props = new Properties();
            props.setProperty("user", user);
            props.setProperty("password", password);
            props.setProperty("ApplicationName", applicationName);
            props.setProperty("connectTimeout", Integer.toString(CONNECT_TIMEOUT_SECONDS));
            props.setProperty("socketTimeout", Integer.toString(SOCKET_TIMEOUT_SECONDS));
            props.setProperty("tcpKeepAlive", "true");
            return DriverManager.getConnection(jdbcUrl, props);
        };
    }

    /** The backend holding the lock, as {@code pg_locks} and {@code pg_stat_activity} report it. */
    public record Holder(Integer pid, String clientAddr, String applicationName, String backendStart) {

        static final Holder UNKNOWN = new Holder(null, null, null, null);

        /** {@code key=value} fields for a log line: no credentials, no URL. */
        String fields() {
            return "holder_pid=" + (pid == null ? "unknown" : pid)
                + " holder_client_addr=" + (clientAddr == null ? "unknown" : clientAddr)
                + " holder_application_name=" + (applicationName == null ? "unknown" : token(applicationName))
                + " holder_backend_start=\"" + (backendStart == null ? "unknown" : backendStart) + "\"";
        }

        private static String token(String s) {
            return s.replaceAll("[^A-Za-z0-9._/:@+=-]", "_");
        }
    }

    /** Another engine holds the lock. {@link #getMessage()} is the structured log line. */
    public static final class HeldElsewhereException extends Exception {
        private final transient Holder holder;

        HeldElsewhereException(String message, Holder holder) {
            super(message);
            this.holder = holder;
        }

        public Holder holder() {
            return holder;
        }
    }

    /**
     * The lock was taken but does not behave as a session lock of this connection: the connection is going through
     * a pooler. {@link #getMessage()} is the structured log line.
     */
    public static final class PoolerSuspectedException extends Exception {
        PoolerSuspectedException(String message) {
            super(message);
        }
    }

    // ── jOOQ references: pg_catalog is not in codegen scope ──────────────────────────────────────────────────

    private static final Table<?> PG_LOCKS = DSL.table(DSL.name("pg_catalog", "pg_locks")).as("l");
    private static final Table<?> PG_ACTIVITY = DSL.table(DSL.name("pg_catalog", "pg_stat_activity")).as("a");
    private static final Field<String> L_LOCKTYPE = DSL.field(DSL.name("l", "locktype"), String.class);
    private static final Field<Boolean> L_GRANTED = DSL.field(DSL.name("l", "granted"), Boolean.class);
    private static final Field<Integer> L_PID = DSL.field(DSL.name("l", "pid"), Integer.class);
    private static final Field<Integer> L_OBJSUBID =
        DSL.field(DSL.name("l", "objsubid"), Integer.class).cast(SQLDataType.INTEGER);
    private static final Field<Long> L_CLASSID =
        DSL.field(DSL.name("l", "classid"), Long.class).cast(SQLDataType.BIGINT);
    private static final Field<Long> L_OBJID =
        DSL.field(DSL.name("l", "objid"), Long.class).cast(SQLDataType.BIGINT);
    private static final Field<Long> L_DATABASE = DSL.field(DSL.name("l", "database"), Long.class);
    private static final Field<Integer> A_PID = DSL.field(DSL.name("a", "pid"), Integer.class);
    private static final Field<String> A_APPLICATION_NAME = DSL.field(DSL.name("a", "application_name"), String.class);
    private static final Field<String> A_BACKEND_START =
        DSL.field(DSL.name("a", "backend_start")).cast(SQLDataType.VARCHAR);
    private static final Field<String> A_CLIENT_HOST =
        DSL.function("host", SQLDataType.VARCHAR, DSL.field(DSL.name("a", "client_addr")));

    /** The classid half of {@link #LOCK_KEY} as {@code pg_locks} reports it (an unsigned 32-bit oid). */
    private static final long KEY_CLASSID = LOCK_KEY >>> 32;
    /** The objid half. */
    private static final long KEY_OBJID = LOCK_KEY & 0xFFFFFFFFL;

    /** A granted lock row for this key, in this database. */
    private static org.jooq.Condition lockRow() {
        return L_LOCKTYPE.eq("advisory")
            .and(L_GRANTED.isTrue())
            .and(L_OBJSUBID.eq(1))
            .and(L_CLASSID.eq(KEY_CLASSID))
            .and(L_OBJID.eq(KEY_OBJID))
            .and(L_DATABASE.eq(DSL.select(DSL.field(DSL.name("oid"), Long.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_database")))
                .where(DSL.field(DSL.name("datname"), String.class)
                    .eq(DSL.function("current_database", SQLDataType.VARCHAR)))));
    }

    // ── state ────────────────────────────────────────────────────────────────────────────────────────────────

    private final ConnectionOpener opener;
    private final String applicationName;
    private volatile Connection connection;
    private volatile boolean closed;
    private volatile boolean lost;
    private final Object checkMutex = new Object();
    private volatile Runnable onRegained = () -> { };
    private volatile Consumer<String> onFatal = message -> { };
    private volatile Duration staleTerminationWait = STALE_TERMINATION_WAIT;
    private ScheduledExecutorService monitor;

    private EngineSingletonLock(ConnectionOpener opener, String applicationName, Connection connection) {
        this.opener = opener;
        this.applicationName = applicationName;
        this.connection = connection;
    }

    /**
     * The boot decision: the lock per {@code settings}, or none. {@link Mode#OFF} and {@link Mode#POOLED_SKIP}
     * return empty after a WARN that says why; {@link Mode#ENFORCE} acquires or throws.
     *
     * @param applicationName what the lock connection names itself in {@code pg_stat_activity}
     */
    public static Optional<EngineSingletonLock> acquireAtBoot(Settings settings, ConnectionOpener opener,
                                                              String applicationName)
            throws SQLException, HeldElsewhereException, PoolerSuspectedException, InterruptedException {
        switch (settings.mode()) {
            case OFF -> {
                log.warn("event=engine_singleton_lock_disabled reason=operator_opt_out setting={}=off "
                    + "note=\"a second engine on this database will NOT be refused; the CollectionRegistry "
                    + "cache assumes one engine per database\"", MODE_ENV);
                return Optional.empty();
            }
            case POOLED_SKIP -> {
                log.warn("event=engine_singleton_lock_skipped reason=pooler_configured setting={}=<unset> "
                    + "note=\"{} is set, and a session advisory lock does not hold through a transaction-mode "
                    + "pooler; one engine per database is NOT enforced here\"",
                    MODE_ENV, PoolerModeCheck.ADMIN_URL_ENV);
                return Optional.empty();
            }
            default -> {
                return Optional.of(acquire(opener, applicationName, settings.bootWait(), DEFAULT_RETRY_INTERVAL));
            }
        }
    }

    /**
     * Take the lock on a new connection from {@code opener}, polling every {@code retryInterval} for up to
     * {@code wait}.
     *
     * @throws HeldElsewhereException when another backend still holds it after {@code wait}
     * @throws PoolerSuspectedException when the lock was taken but this connection does not show it as its own
     * @throws SQLException when the database cannot be reached or a statement fails
     */
    public static EngineSingletonLock acquire(ConnectionOpener opener, String applicationName, Duration wait,
                                              Duration retryInterval)
            throws SQLException, HeldElsewhereException, PoolerSuspectedException, InterruptedException {
        return acquire(opener, applicationName, wait, retryInterval, WAIT_PROGRESS_INTERVAL);
    }

    /** As above, with the interval of the progress lines a waiting boot logs. */
    static EngineSingletonLock acquire(ConnectionOpener opener, String applicationName, Duration wait,
                                       Duration retryInterval, Duration progressInterval)
            throws SQLException, HeldElsewhereException, PoolerSuspectedException, InterruptedException {
        long start = System.nanoTime();
        long deadline = start + wait.toNanos();
        Connection c = opener.open();
        boolean ok = false;
        try {
            long nextProgress = start;   // the first failed try logs at once
            while (true) {
                if (tryLock(c)) {
                    EngineSingletonLock lock = taken(c, opener, applicationName, start);
                    ok = true;
                    return lock;
                }
                long now = System.nanoTime();
                long remaining = deadline - now;
                if (remaining <= 0) {
                    break;
                }
                if (now >= nextProgress) {
                    nextProgress = now + progressInterval.toNanos();
                    log.warn("event=engine_singleton_lock_waiting elapsed_s={} wait_s={} {}",
                        TimeUnit.NANOSECONDS.toSeconds(now - start), wait.toSeconds(), describeHolder(c).fields());
                }
                TimeUnit.NANOSECONDS.sleep(Math.min(remaining, retryInterval.toNanos()));
            }
            Holder holder = describeHolder(c);
            if (holder.pid() == null && tryLock(c)) {
                // Released between our last try and the lookup.
                EngineSingletonLock lock = taken(c, opener, applicationName, start);
                ok = true;
                return lock;
            }
            throw new HeldElsewhereException("event=engine_singleton_lock_refused key=" + LOCK_KEY
                + " waited_s=" + wait.toSeconds() + " " + holder.fields()
                + " remedy=\"stop the other engine (local install: nx daemon service stop). A half-open holder "
                + "left by a dead host: pg_terminate_backend(<holder_pid>). "
                + MODE_ENV + "=off disables this check\"", holder);
        } catch (DataAccessException e) {
            // jOOQ wraps the driver's SQLException; boot handles one exception type for an unreachable database.
            throw new SQLException("engine singleton lock statement failed: " + e.getMessage(), e);
        } finally {
            if (!ok) {
                closeQuietly(c);
            }
        }
    }

    private static EngineSingletonLock taken(Connection c, ConnectionOpener opener, String applicationName,
                                             long startNanos) throws PoolerSuspectedException, SQLException {
        verifyHeldByThisBackend(c);
        log.info("event=engine_singleton_lock_acquired key={} waited_ms={} application_name={}",
            LOCK_KEY, TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startNanos), applicationName);
        return new EngineSingletonLock(opener, applicationName, c);
    }

    /**
     * Right after taking the lock: it must show as held by the backend this connection talks to, and that backend
     * must not change between two statements. A transaction-mode pooler fails one or the other, and the engine
     * would otherwise exit about one check interval later as "lost to another engine".
     */
    private static void verifyHeldByThisBackend(Connection c) throws PoolerSuspectedException, SQLException {
        Integer first = backendPid(c);
        boolean held = stillHeld(c);
        Integer second = backendPid(c);
        if (held && Objects.equals(first, second)) {
            return;
        }
        throw new PoolerSuspectedException("event=engine_singleton_lock_pooler_suspected key=" + LOCK_KEY
            + " held_by_this_connection=" + held + " backend_pid_first=" + first + " backend_pid_second=" + second
            + " note=\"the lock was taken, but it does not show as held by this connection's backend, or the "
            + "backend changed between two statements: the engine's database URL goes through a connection pooler, "
            + "and a session advisory lock does not hold through a transaction-mode pooler. Connect to Postgres "
            + "directly, set " + PoolerModeCheck.ADMIN_URL_ENV + " to say a pooler is in use (the lock is then "
            + "skipped), or set " + MODE_ENV + "=off\"");
    }

    /**
     * Start the periodic check on a daemon thread of its own.
     *
     * @param onRegained runs after the lock was lost and taken back; clear in-process state another engine may
     *                   have outdated
     * @param onFatal    runs once with the log line when another engine holds the lock; it must end the process
     */
    public void startMonitor(Duration interval, Runnable onRegained, Consumer<String> onFatal) {
        this.onRegained = onRegained;
        this.onFatal = onFatal;
        synchronized (this) {
            if (monitor != null || closed) {
                return;
            }
            monitor = Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "engine-singleton-lock-check");
                t.setDaemon(true);
                return t;
            });
            monitor.scheduleWithFixedDelay(this::checkOnceQuietly, interval.toNanos(), interval.toNanos(),
                TimeUnit.NANOSECONDS);
        }
    }

    private void checkOnceQuietly() {
        try {
            checkOnce();
        } catch (Throwable t) {
            // A throw would cancel every later run of the schedule: log it and keep checking.
            log.error("event=engine_singleton_lock_check_failed error=\"{}\"", t.toString(), t);
        }
    }

    /** One pass of the check; the scheduler calls it, and tests call it directly. */
    void checkOnce() {
        synchronized (checkMutex) {
            if (closed || lost) {
                return;
            }
            Connection held = connection;
            if (held != null) {
                String reason;
                try {
                    if (stillHeld(held)) {
                        return;
                    }
                    reason = "lock_not_held_by_this_connection";
                } catch (SQLException | DataAccessException e) {
                    reason = "connection_failed: " + e.getMessage();
                }
                if (closed) {
                    return;   // close() ended the connection under this check: a shutdown, not a loss
                }
                log.warn("event=engine_singleton_lock_lost key={} application_name={} reason=\"{}\" "
                    + "action=reacquire", LOCK_KEY, applicationName, reason);
                connection = null;
                closeQuietly(held);
            }
            reacquire();
        }
    }

    /** A holder is positively ours when its pid is known and it carries this boot's own application name. */
    private boolean isOurs(Holder holder) {
        return holder.pid() != null && pgApplicationName(applicationName).equals(holder.applicationName());
    }

    private void reacquire() {
        Connection c = null;
        boolean ok = false;
        try {
            if (closed) {
                return;
            }
            c = opener.open();
            if (closed) {
                return;   // the finally closes c
            }
            boolean got = tryLock(c);
            Holder holder = Holder.UNKNOWN;
            if (!got) {
                holder = describeHolder(c);
                if (closed) {
                    return;
                }
                if (isOurs(holder)) {
                    got = terminateStaleBackendOfOurs(c, holder);
                    if (!got) {
                        deferred("stale_backend_of_ours_still_holds_the_lock", holder);
                        return;
                    }
                } else {
                    // The holder may have gone between the two statements; ask once more before judging.
                    got = tryLock(c);
                    if (!got) {
                        if (closed) {
                            return;
                        }
                        if (holder.pid() == null) {
                            deferred("holder_not_identified", holder);
                            return;
                        }
                    }
                }
            }
            if (got) {
                connection = c;
                if (closed) {
                    // close() ran between the check above and the assignment, and saw no connection to close.
                    connection = null;
                    return;   // the finally closes c
                }
                ok = true;
                log.warn("event=engine_singleton_lock_reacquired key={} application_name={} "
                    + "action=clear_collection_registry", LOCK_KEY, applicationName);
                try {
                    onRegained.run();
                } catch (RuntimeException e) {
                    log.error("event=engine_singleton_lock_regain_hook_failed error=\"{}\"", e.toString(), e);
                }
                return;
            }
            if (closed) {
                return;
            }
            lost = true;
            String message = "event=engine_singleton_lock_lost_to_another_engine key=" + LOCK_KEY + " "
                + holder.fields() + " action=exit status=" + EXIT_STATUS
                + " note=\"the lock was released and another engine now holds it; serving on would let the "
                + "CollectionRegistry cache serve stale rows\"";
            log.error(message);
            onFatal.accept(message);
        } catch (SQLException | DataAccessException e) {
            log.warn("event=engine_singleton_lock_reacquire_deferred error=\"{}\" "
                + "note=\"database unreachable; no verdict, trying again next check\"", e.getMessage());
        } finally {
            if (!ok) {
                closeQuietly(c);
            }
        }
    }

    private void deferred(String reason, Holder holder) {
        log.warn("event=engine_singleton_lock_reacquire_deferred reason={} {} "
            + "note=\"no verdict without a positively identified holder; trying again next check\"",
            reason, holder.fields());
    }

    /** The holder is a backend of this same boot (its name is per-boot unique): end it and take the lock. */
    private boolean terminateStaleBackendOfOurs(Connection c, Holder holder) throws SQLException {
        log.warn("event=engine_singleton_lock_stale_backend_terminated {} note=\"a backend of this boot "
            + "still holds the lock after its connection was lost\"", holder.fields());
        terminateBackendNamed(c, holder.pid(), pgApplicationName(applicationName));
        long deadline = System.nanoTime() + staleTerminationWait.toNanos();
        while (true) {
            if (tryLock(c)) {
                return true;
            }
            if (System.nanoTime() >= deadline) {
                return false;
            }
            try {
                TimeUnit.NANOSECONDS.sleep(STALE_TERMINATION_POLL.toNanos());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }

    /**
     * End backend {@code pid} only while it still carries {@code name}: the name is tested in the terminating
     * statement, so a pid that was reused between the holder lookup and this call is left alone.
     *
     * @return whether a backend was signalled
     */
    static boolean terminateBackendNamed(Connection c, int pid, String name) {
        Boolean signalled = DSL.using(c, org.jooq.SQLDialect.POSTGRES)
            .select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, A_PID))
            .from(PG_ACTIVITY)
            .where(A_PID.eq(pid))
            .and(A_APPLICATION_NAME.eq(name))
            .fetchOne(0, Boolean.class);
        return Boolean.TRUE.equals(signalled);
    }

    /** Stop the check and close the connection, which releases the lock. Idempotent. */
    @Override
    public void close() {
        closed = true;
        ScheduledExecutorService m;
        synchronized (this) {
            m = monitor;
            monitor = null;
        }
        if (m != null) {
            m.shutdownNow();
        }
        Connection c = connection;
        connection = null;
        closeQuietly(c);
    }

    /** Test seam: how long a re-acquire waits for the termination of its own stale backend. */
    EngineSingletonLock withStaleTerminationWait(Duration wait) {
        this.staleTerminationWait = wait;
        return this;
    }

    /** Test seam: forget the connection WITHOUT closing it, as if the engine had lost it while the backend lives. */
    void severConnectionForTests() {
        connection = null;
    }

    // ── SQL ──────────────────────────────────────────────────────────────────────────────────────────────────

    static boolean tryLock(Connection c) {
        return Boolean.TRUE.equals(DSL.using(c, org.jooq.SQLDialect.POSTGRES)
            .select(DSL.function("pg_try_advisory_lock", SQLDataType.BOOLEAN, DSL.val(LOCK_KEY)))
            .fetchOne(0, Boolean.class));
    }

    /** The pid of the backend this connection talks to. */
    static Integer backendPid(Connection c) {
        return DSL.using(c, org.jooq.SQLDialect.POSTGRES)
            .select(DSL.function("pg_backend_pid", SQLDataType.INTEGER))
            .fetchOne(0, Integer.class);
    }

    /** Does this very connection's backend hold the lock row? */
    static boolean stillHeld(Connection c) throws SQLException {
        Integer n = DSL.using(c, org.jooq.SQLDialect.POSTGRES)
            .selectCount().from(PG_LOCKS)
            .where(lockRow())
            .and(L_PID.eq(DSL.function("pg_backend_pid", SQLDataType.INTEGER)))
            .fetchOne(0, Integer.class);
        return n != null && n > 0;
    }

    /** The backend holding the lock; {@link Holder#UNKNOWN} when nothing does. */
    static Holder describeHolder(Connection c) {
        try {
            DSLContext ctx = DSL.using(c, org.jooq.SQLDialect.POSTGRES);
            var rec = ctx.select(L_PID, A_CLIENT_HOST, A_APPLICATION_NAME, A_BACKEND_START)
                .from(PG_LOCKS)
                .leftJoin(PG_ACTIVITY).on(A_PID.eq(L_PID))
                .where(lockRow())
                .limit(1)
                .fetchOne();
            if (rec == null) {
                return Holder.UNKNOWN;
            }
            return new Holder(rec.get(L_PID), rec.get(A_CLIENT_HOST), rec.get(A_APPLICATION_NAME),
                rec.get(A_BACKEND_START));
        } catch (DataAccessException e) {
            // The refusal must still go out; a failed lookup is reported as an unknown holder.
            log.warn("event=engine_singleton_lock_holder_lookup_failed error=\"{}\"", e.getMessage());
            return Holder.UNKNOWN;
        }
    }

    // ── names ────────────────────────────────────────────────────────────────────────────────────────────────

    /** {@code application_name} as Postgres stores it: cut to 63 bytes. */
    static String pgApplicationName(String name) {
        byte[] b = name.getBytes(StandardCharsets.UTF_8);
        if (b.length <= 63) {
            return name;
        }
        return new String(b, 0, 63, StandardCharsets.UTF_8);
    }

    private static void closeQuietly(Connection c) {
        if (c == null) {
            return;
        }
        try {
            c.close();
        } catch (SQLException | RuntimeException e) {
            log.debug("event=engine_singleton_lock_close_failed error=\"{}\"", e.toString());
        }
    }
}
