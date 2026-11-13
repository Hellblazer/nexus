// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

/**
 * The engine's tenant export sessions (RDR-248 "Export session", bead nexus-csayy.2): one per tenant, a
 * small number per engine, each holding one dedicated connection in a {@code REPEATABLE READ READ ONLY}
 * transaction that has called {@code pg_export_snapshot()} and NOTHING else.
 *
 * <p><b>The held transaction reads no table.</b> A read there holds an AccessShareLock on every table and
 * partition leaf it touched until the session ends, and tenant create needs an AccessExclusiveLock on a
 * model-level {@code chunks} partition to add the new tenant's leaf, so it would fail for every tenant for
 * the session's life (spike round 2: 503 {@code tenant_creation_busy} after 3.45 s with 467 locks held,
 * 0.04 s with the snapshot alone). The header and every page are read in their own short transactions that
 * import the snapshot ({@link TenantExportReads}).
 *
 * <p><b>Bounds.</b> The held transaction pins the vacuum horizon of the whole database while it lives, so the
 * engine closes a session itself: idle for more than {@link Config#idleBound()} (last request start or end),
 * or older than {@link Config#totalBound()}. Defaults 10 minutes and 60 minutes (Sam, 2026-11-11), both
 * configurable ({@code NX_EXPORT_IDLE_SECONDS}, {@code NX_EXPORT_TOTAL_SECONDS}). A background sweep applies
 * them every {@link Config#sweepInterval()}, and every lookup applies them first, so a session past a bound
 * is never served whatever the sweep's timing. The bounds must sit inside any
 * {@code idle_in_transaction_session_timeout} the cloud sets for the engine role (RDR-248 A7): a shorter one
 * ends the held transaction, and the next page answers "session closed".
 *
 * <p><b>Binding.</b> A session belongs to a tenant, not to a token: the lookup key is the tenant the auth
 * filter resolved, so another token of the same tenant continues the session, and no other tenant can see it
 * ({@link NoSuchSessionException}, the same answer as for an id that never existed).
 *
 * <p><b>Capacity.</b> At most {@link Config#maxSessions()} sessions per engine (default 2: each holds one
 * pool connection for up to an hour, and its page reads take one more while they run); one open or opening
 * per tenant. A refused open carries the reason, and for a full engine a retry-after.
 */
public final class TenantExportSessions implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(TenantExportSessions.class);

    /** Environment names, read once by {@link Config#fromEnv}. */
    public static final String ENV_IDLE_SECONDS = "NX_EXPORT_IDLE_SECONDS";
    public static final String ENV_TOTAL_SECONDS = "NX_EXPORT_TOTAL_SECONDS";
    public static final String ENV_MAX_SESSIONS = "NX_EXPORT_MAX_SESSIONS";
    public static final String ENV_PAGE_BYTES = "NX_EXPORT_PAGE_BYTES";
    public static final String ENV_PAGE_BUDGET_SECONDS = "NX_EXPORT_PAGE_BUDGET_SECONDS";

    /** Longest a session bound may be set: a week. Keeps every bound sum far inside {@code Instant}'s range. */
    static final long MAX_BOUND_SECONDS = 7L * 24 * 3600;
    /** The most sessions an engine may be set to hold; each pins one pooled connection for up to the total bound. */
    static final int MAX_SESSIONS_LIMIT = 64;
    /** Smallest page byte target: a page always holds at least one whole row, so this only sets the cut's grain. */
    static final int MIN_PAGE_BYTES = 1024;
    /**
     * Largest page byte target. The cloud edge caps a response body at 20 MB (RDR-248 A1); a page overshoots its
     * target by at most one row plus one network read, so the target stays well under that.
     */
    static final int MAX_PAGE_BYTES = 19_000_000;
    /** Longest a page's database time may be budgeted, seconds. */
    static final long MAX_PAGE_BUDGET_SECONDS = 300;

    /**
     * Session and page settings. {@code pageBytes} is the page's byte target (about 16 MB: under the cloud
     * edge's 20 MB body cap); {@code pageMaxRows} bounds a page of small rows; {@code lockTimeoutMs} bounds a
     * page's wait for a table lock behind a tenant create; {@code pageBudgetMs} bounds the DATABASE time of one
     * page, all its statements together (default 20 s). The budget covers database statements only: the pool
     * borrow before the first one is bounded by the pool's own connection timeout (HikariCP's 30 s default, which
     * {@code Main} does not change), outside the budget, so the budget does not by itself keep a page inside the
     * bead's 30 s response-start bound on a contended pool. The header read gives each of its statements the full
     * budget (they are catalog reads, not table scans), not a shared one. {@code maxSessions} 0 disables export.
     * {@code retryAfterSeconds} is what a full engine asks a client to wait.
     */
    public record Config(Duration totalBound, Duration idleBound, int maxSessions, int pageBytes, int pageMaxRows,
                         int lockTimeoutMs, int retryAfterSeconds, Duration sweepInterval, int pageBudgetMs) {
        public Config {
            if (totalBound.isNegative() || totalBound.isZero() || idleBound.isNegative() || idleBound.isZero()) {
                throw new IllegalArgumentException("export session bounds must be positive");
            }
            if (totalBound.toSeconds() > MAX_BOUND_SECONDS || idleBound.toSeconds() > MAX_BOUND_SECONDS) {
                throw new IllegalArgumentException("export session bounds may not exceed " + MAX_BOUND_SECONDS
                    + " seconds");
            }
            if (maxSessions < 0 || maxSessions > MAX_SESSIONS_LIMIT) {   // 0: export disabled (a pool too small to carry one)
                throw new IllegalArgumentException("export sessions per engine must be between 0 and "
                    + MAX_SESSIONS_LIMIT + ": " + maxSessions);
            }
            if (pageBytes < MIN_PAGE_BYTES || pageBytes > MAX_PAGE_BYTES) {
                throw new IllegalArgumentException("export page bytes must be between " + MIN_PAGE_BYTES + " and "
                    + MAX_PAGE_BYTES + " (the cloud edge caps a body at 20 MB): " + pageBytes);
            }
            if (pageMaxRows < 1 || lockTimeoutMs < 1 || retryAfterSeconds < 1 || pageBudgetMs < 1
                    || sweepInterval.isNegative() || sweepInterval.isZero()) {
                throw new IllegalArgumentException("export page limits must be at least 1");
            }
        }

        public static Config defaults() {
            return new Config(Duration.ofMinutes(60), Duration.ofMinutes(10), 2, 16 * 1024 * 1024, 100_000,
                5_000, 30, Duration.ofSeconds(5), 20_000);
        }

        public Config withBounds(Duration total, Duration idle) {
            return new Config(total, idle, maxSessions, pageBytes, pageMaxRows, lockTimeoutMs, retryAfterSeconds,
                sweepInterval, pageBudgetMs);
        }

        public Config withMaxSessions(int n) {
            return new Config(totalBound, idleBound, n, pageBytes, pageMaxRows, lockTimeoutMs, retryAfterSeconds,
                sweepInterval, pageBudgetMs);
        }

        public Config withPageBytes(int n) {
            return new Config(totalBound, idleBound, maxSessions, n, pageMaxRows, lockTimeoutMs, retryAfterSeconds,
                sweepInterval, pageBudgetMs);
        }

        public Config withLockTimeoutMs(int n) {
            return new Config(totalBound, idleBound, maxSessions, pageBytes, pageMaxRows, n, retryAfterSeconds,
                sweepInterval, pageBudgetMs);
        }

        public Config withPageBudgetMs(int n) {
            return new Config(totalBound, idleBound, maxSessions, pageBytes, pageMaxRows, lockTimeoutMs,
                retryAfterSeconds, sweepInterval, n);
        }

        public Config withSweepInterval(Duration d) {
            return new Config(totalBound, idleBound, maxSessions, pageBytes, pageMaxRows, lockTimeoutMs,
                retryAfterSeconds, d, pageBudgetMs);
        }

        public Config withRetryAfterSeconds(int n) {
            return new Config(totalBound, idleBound, maxSessions, pageBytes, pageMaxRows, lockTimeoutMs, n,
                sweepInterval, pageBudgetMs);
        }

        /**
         * The defaults, with each {@code NX_EXPORT_*} value that {@code env} answers. A value that is not a whole
         * number, or is outside its range, is an {@link IllegalArgumentException} whose message names the variable
         * and the range, so a boot can print it as it prints any other bad environment value.
         */
        public static Config fromEnv(Function<String, String> env) {
            Config d = defaults();
            return new Config(
                Duration.ofSeconds(ranged(env, ENV_TOTAL_SECONDS, d.totalBound().toSeconds(), MAX_BOUND_SECONDS)),
                Duration.ofSeconds(ranged(env, ENV_IDLE_SECONDS, d.idleBound().toSeconds(), MAX_BOUND_SECONDS)),
                (int) ranged(env, ENV_MAX_SESSIONS, d.maxSessions(), MAX_SESSIONS_LIMIT),
                (int) ranged(env, ENV_PAGE_BYTES, d.pageBytes(), MAX_PAGE_BYTES, MIN_PAGE_BYTES),
                d.pageMaxRows(), d.lockTimeoutMs(), d.retryAfterSeconds(), d.sweepInterval(),
                Math.toIntExact(ranged(env, ENV_PAGE_BUDGET_SECONDS, d.pageBudgetMs() / 1000, MAX_PAGE_BUDGET_SECONDS)
                    * 1000));
        }

        /**
         * The most sessions a pool of {@code poolSize} connections carries: each holds one connection and opening
         * one needs a second, and the rest of the engine needs the others (see {@link #requirePoolHeadroom}).
         * 3 connections carry 1, 4 carry 2, and below 3 none.
         */
        public static int carriedSessions(int poolSize) {
            return poolSize >= 4 ? poolSize / 2 : poolSize == 3 ? 1 : 0;
        }

        /**
         * {@link #fromEnv} fitted to the pool the engine runs with. A cap left UNSET is clamped to what the pool
         * carries (possibly 0: export is then disabled and every open answers 503 {@code export_capacity_full}),
         * so a small pool boots as it always did. A cap the operator SET that the pool cannot carry is refused
         * with {@link IllegalArgumentException} ({@link #requirePoolHeadroom}).
         */
        public static Config forPool(Function<String, String> env, int poolSize) {
            Config c = fromEnv(env);
            String set = env.apply(ENV_MAX_SESSIONS);
            if (set != null && !set.isBlank()) {
                c.requirePoolHeadroom(poolSize);
                return c;
            }
            return c.withMaxSessions(Math.min(c.maxSessions(), carriedSessions(poolSize)));
        }

        /** {@link #forPool} for a pool whose size is known (HikariCP); any other pool is not clamped. */
        public static Config forDataSource(Function<String, String> env, DataSource dataSource) {
            return dataSource instanceof com.zaxxer.hikari.HikariDataSource hikari && hikari.getMaximumPoolSize() > 0
                ? forPool(env, hikari.getMaximumPoolSize()) : fromEnv(env);
        }

        /**
         * Refuses a session cap the pool cannot carry. Each session holds one pooled connection for up to the
         * total bound, and opening one needs a second for the header read; the rest of the engine needs the others.
         * So the cap may not exceed half the pool, and the pool needs two connections beyond it.
         *
         * @throws IllegalArgumentException naming {@code NX_EXPORT_MAX_SESSIONS}, {@code NX_POOL_SIZE} and the least
         *                                  pool size that carries the cap
         */
        public void requirePoolHeadroom(int poolSize) {
            if (maxSessions == 0) return;
            int needed = Math.max(2 * maxSessions, maxSessions + 2);
            if (poolSize < needed) {
                throw new IllegalArgumentException(ENV_MAX_SESSIONS + "=" + maxSessions + " needs NX_POOL_SIZE of at least "
                    + needed + " (it is " + poolSize + "): each export session holds one connection for up to "
                    + totalBound.toMinutes() + " minutes and opening one needs a second. Lower " + ENV_MAX_SESSIONS
                    + " or raise NX_POOL_SIZE.");
            }
        }

        private static long ranged(Function<String, String> env, String name, long fallback, long max) {
            return ranged(env, name, fallback, max, 1);
        }

        private static long ranged(Function<String, String> env, String name, long fallback, long max, long min) {
            String v = env.apply(name);
            if (v == null || v.isBlank()) return fallback;
            try {
                long n = Long.parseLong(v.trim());
                if (n < min || n > max) {
                    throw new IllegalArgumentException(name + " must be between " + min + " and " + max + ": " + v);
                }
                return n;
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(name + " is not a whole number: " + v);
            }
        }
    }

    /** The caller's tenant has no such session: never opened, closed, expired, or another tenant's. */
    public static final class NoSuchSessionException extends RuntimeException {
        public NoSuchSessionException() {
            super("no such export session");
        }
    }

    /** The tenant already has an export session ({@code sessionId} is its id, which only that tenant can see). */
    public static final class SessionExistsException extends RuntimeException {
        private final String sessionId;
        private final String kind;

        SessionExistsException(String sessionId, String kind) {
            super("this tenant already has an export or import session");
            this.sessionId = sessionId;
            this.kind = kind;
        }

        public String sessionId() {
            return sessionId;
        }

        /** {@code "export"} or {@code "import"}: which routes close {@link #sessionId()} ({@code DELETE /v1/tenant/<kind>/{id}}); null while one is still opening. */
        public String kind() {
            return kind;
        }
    }

    /** The engine holds its maximum number of sessions. */
    public static final class CapacityFullException extends RuntimeException {
        private final int retryAfterSeconds;

        CapacityFullException(int retryAfterSeconds) {
            super("this engine already holds its maximum number of export and import sessions");
            this.retryAfterSeconds = retryAfterSeconds;
        }

        public int retryAfterSeconds() {
            return retryAfterSeconds;
        }
    }

    /** A page of this session is already being read; one at a time per session. */
    public static final class PageInFlightException extends RuntimeException {
        public PageInFlightException() {
            super("a page of this export session is already being read");
        }
    }

    /** An open session: the snapshot, the connection that holds it, and its clocks. Guarded by the sessions' lock. */
    public static final class Session {
        private final String id;
        private final String owner;
        private final String snapshotId;
        private final Connection held;
        private final Instant openedAt;
        private Instant lastActive;
        private final Semaphore pageGate = new Semaphore(1);

        private Session(String id, String owner, String snapshotId, Connection held, Instant openedAt) {
            this.id = id;
            this.owner = owner;
            this.snapshotId = snapshotId;
            this.held = held;
            this.openedAt = openedAt;
            this.lastActive = openedAt;
        }

        public String id() {
            return id;
        }

        public String snapshotId() {
            return snapshotId;
        }

        public Instant openedAt() {
            return openedAt;
        }

        /** The tenant the session belongs to; every read of it is stamped with this, never with a request's value. */
        public String owner() {
            return owner;
        }

        /** The held connection, for a test that checks what the held transaction is and holds. */
        Connection heldConnectionForTests() {
            return held;
        }
    }

    private final DataSource dataSource;
    private final Config config;
    private final Clock clock;
    private final TenantExportReads reads;

    private final Object lock = new Object();
    private final Map<String, Session> byOwner = new HashMap<>();
    private final Set<String> opening = new HashSet<>();
    /** Slots held by tenant import sessions (RDR-248 Step 5): they share the per-engine cap and the per-tenant rule. */
    private final Map<String, Slot> importSlots = new HashMap<>();
    private ScheduledExecutorService sweeper;
    private boolean closed;

    public TenantExportSessions(DataSource dataSource, Config config, Clock clock) {
        this.dataSource = dataSource;
        this.config = config;
        this.clock = clock;
        this.reads = new TenantExportReads(dataSource, config.lockTimeoutMs(), config.pageBudgetMs());
    }

    public Config config() {
        return config;
    }

    /** The clock the session bounds read; the import sessions that share this cap read the same one. */
    public Clock clock() {
        return clock;
    }

    /** The pool the sessions hold their connections from. */
    DataSource dataSource() {
        return dataSource;
    }

    public TenantExportReads reads() {
        return reads;
    }

    /** An opened session and the header read from its snapshot. */
    public record Opened(Session session, TenantExportReads.Header header) {
    }

    /**
     * Opens {@code tenant}'s session: reserves its slot, holds a connection in a snapshot-exporting
     * transaction, reads the header in a separate short transaction, and publishes the session.
     *
     * @throws SessionExistsException if the tenant already has a session
     * @throws CapacityFullException  if the engine holds its maximum
     */
    public Opened open(String tenant) {
        List<Session> expired;
        synchronized (lock) {
            if (closed) throw new IllegalStateException("export sessions are closed");
            expired = expireLocked(clock.instant());
        }
        handOff(expired);
        synchronized (lock) {
            Session existing = byOwner.get(tenant);
            if (existing != null) throw new SessionExistsException(existing.id, "export");
            Slot importing = importSlots.get(tenant);
            if (importing != null) throw new SessionExistsException(importing.id(), "import");
            if (opening.contains(tenant)) throw new SessionExistsException(null, null);
            if (byOwner.size() + opening.size() + importSlots.size() >= config.maxSessions()) {
                throw new CapacityFullException(config.retryAfterSeconds());
            }
            opening.add(tenant);
            startSweeperLocked();
        }
        Connection held = null;
        boolean published = false;
        try {
            held = dataSource.getConnection();
            String snapshotId = exportSnapshot(held);
            TenantExportReads.Header header = reads.header(snapshotId, tenant);
            Session session = new Session(UUID.randomUUID().toString(), tenant, snapshotId, held, clock.instant());
            synchronized (lock) {
                opening.remove(tenant);
                if (closed) throw new IllegalStateException("export sessions are closed");
                byOwner.put(tenant, session);
                published = true;
            }
            log.info("event=export_session_opened session={} tenant={} snapshot={}", session.id, tenant, snapshotId);
            return new Opened(session, header);
        } catch (SQLException e) {
            throw new IllegalStateException("could not open an export session", e);
        } finally {
            if (!published) {
                synchronized (lock) {
                    opening.remove(tenant);
                }
                endHeld(held, "open_failed", tenant);
            }
        }
    }

    /** A tenant's place among the engine's sessions held by something other than an export (an import session). */
    public record Slot(String tenant, String id) {
    }

    /**
     * Reserves {@code tenant}'s one session place and one of the engine's session places for an import session
     * ({@link TenantImportSessions}): the same cap and the same one-per-tenant rule as an export, because both hold a
     * pooled connection for the length of the session. {@code id} is the id the import session will answer to.
     *
     * @throws SessionExistsException if the tenant already has an export or import session (or one is opening)
     * @throws CapacityFullException  if the engine holds its maximum
     */
    public Slot reserveSlot(String tenant, String id) {
        List<Session> expired;
        synchronized (lock) {
            if (closed) throw new IllegalStateException("export sessions are closed");
            expired = expireLocked(clock.instant());
        }
        handOff(expired);
        synchronized (lock) {
            Session existing = byOwner.get(tenant);
            if (existing != null) throw new SessionExistsException(existing.id, "export");
            Slot held = importSlots.get(tenant);
            if (held != null) throw new SessionExistsException(held.id(), "import");
            if (opening.contains(tenant)) throw new SessionExistsException(null, null);
            if (byOwner.size() + opening.size() + importSlots.size() >= config.maxSessions()) {
                throw new CapacityFullException(config.retryAfterSeconds());
            }
            Slot slot = new Slot(tenant, id);
            importSlots.put(tenant, slot);
            return slot;
        }
    }

    /** Frees a place taken by {@link #reserveSlot}; a place already freed is left alone. */
    public void releaseSlot(Slot slot) {
        synchronized (lock) {
            importSlots.remove(slot.tenant(), slot);
        }
    }

    /**
     * The session of {@code tenant} named {@code id}, with its idle clock restarted.
     *
     * @throws NoSuchSessionException if there is none, it is another tenant's, or it is past a bound
     */
    public Session lookup(String tenant, String id) {
        List<Session> expired;
        Session s;
        synchronized (lock) {
            expired = expireLocked(clock.instant());
            s = byOwner.get(tenant);
            if (s != null && s.id.equals(id)) s.lastActive = clock.instant();
        }
        handOff(expired);
        if (s == null || !s.id.equals(id)) throw new NoSuchSessionException();
        return s;
    }

    /**
     * Runs {@code read} as the one page read of {@code session}; a second concurrent page is refused. The idle
     * clock restarts when the read ends, so a long page is not idle time.
     */
    public <T> T withPage(Session session, Function<Session, T> read) {
        if (!session.pageGate.tryAcquire()) throw new PageInFlightException();
        try {
            return read.apply(session);
        } finally {
            synchronized (lock) {
                session.lastActive = clock.instant();
            }
            session.pageGate.release();
        }
    }

    /**
     * Closes {@code tenant}'s session {@code id}: ends the held transaction and frees the slot.
     *
     * @throws NoSuchSessionException as {@link #lookup}
     */
    public void close(String tenant, String id) {
        List<Session> expired;
        Session s;
        synchronized (lock) {
            expired = expireLocked(clock.instant());
            s = byOwner.get(tenant);
            if (s != null && s.id.equals(id)) byOwner.remove(tenant);
        }
        handOff(expired);
        if (s == null || !s.id.equals(id)) throw new NoSuchSessionException();
        endHeld(s.held, "closed", tenant);
        log.info("event=export_session_closed session={} tenant={} reason=delete", s.id, tenant);
    }

    /**
     * Drops {@code session} after a read found its snapshot gone (the held transaction ended some way the
     * engine did not see, such as the database's own idle timeout): frees the slot. A no-op if it is already gone.
     */
    public void forget(Session session) {
        boolean removed;
        synchronized (lock) {
            removed = byOwner.remove(session.owner, session);
        }
        if (removed) {
            endHeld(session.held, "snapshot_gone", session.owner);
            log.warn("event=export_session_closed session={} tenant={} reason=snapshot_gone", session.id, session.owner);
        }
    }

    /** Closes the tenant's session if it has one (the tenant reset route's evictor); returns how many. */
    public int evictTenant(String tenant) {
        Session s;
        synchronized (lock) {
            s = byOwner.remove(tenant);
        }
        if (s == null) return 0;
        endHeld(s.held, "evicted", tenant);
        return 1;
    }

    /** Applies both bounds now; returns how many sessions it closed. The background sweep calls this. */
    public int sweep() {
        List<Session> expired;
        synchronized (lock) {
            expired = expireLocked(clock.instant());
        }
        endAll(expired, "expired");
        return expired.size();
    }

    /** Sessions open now (not counting one still opening). */
    public int openCount() {
        synchronized (lock) {
            return byOwner.size();
        }
    }

    /**
     * Ends every session and stops the sweep; the engine's stop calls it, so no held connection outlives the pool.
     * Expiries a request handed to the sweeper are finished first (the sweeper is shut down, not cut off, and
     * given a few seconds), because those sessions are no longer in the map and nothing else would end them.
     */
    @Override
    public void close() {
        List<Session> all;
        ScheduledExecutorService toStop;
        synchronized (lock) {
            closed = true;
            all = new ArrayList<>(byOwner.values());
            byOwner.clear();
            toStop = sweeper;
            sweeper = null;
        }
        if (toStop != null) {
            toStop.shutdown();   // queued hand-offs still run; the periodic sweep does not
            try {
                if (!toStop.awaitTermination(5, TimeUnit.SECONDS)) {
                    log.warn("event=export_sweeper_close_timeout; ending what is queued by interrupting it");
                    toStop.shutdownNow();
                }
            } catch (InterruptedException e) {
                toStop.shutdownNow();
                Thread.currentThread().interrupt();
            }
        }
        for (Session s : all) {
            endHeld(s.held, "engine_stop", s.owner);
        }
    }

    /** The sweep's executor, or null before the first open; a test occupies it to queue a hand-off behind it. */
    ScheduledExecutorService sweeperForTests() {
        synchronized (lock) {
            return sweeper;
        }
    }

    // ── internals ────────────────────────────────────────────────────────────────────────────────

    /**
     * Removes every session past a bound from the map and returns them; the caller holds the lock and, once it
     * has released it, ends their connections ({@link #endAll}), so a rollback never runs under the lock.
     */
    private List<Session> expireLocked(Instant now) {
        List<Session> expired = new ArrayList<>();
        for (Session s : byOwner.values()) {
            boolean total = !now.isBefore(s.openedAt.plus(config.totalBound()));
            boolean idle = !now.isBefore(s.lastActive.plus(config.idleBound())) && s.pageGate.availablePermits() > 0;
            if (total || idle) {
                expired.add(s);
                log.info("event=export_session_closed session={} tenant={} reason={}", s.id, s.owner,
                    total ? "total_bound" : "idle_bound");
            }
        }
        for (Session s : expired) {
            byOwner.remove(s.owner);
        }
        return expired;
    }

    /**
     * Ends the connections of sessions a REQUEST found expired, on the sweep thread, so a rollback that stalls (a
     * wedged held connection) never stalls an unrelated tenant's request. With no sweeper (every session was
     * closed already) there is nothing to hand to, and the work runs here.
     */
    private void handOff(List<Session> expired) {
        if (expired.isEmpty()) return;
        ScheduledExecutorService executor;
        synchronized (lock) {
            executor = sweeper;
        }
        if (executor != null && !executor.isShutdown()) {
            try {
                executor.execute(() -> endAll(expired, "expired"));
                return;
            } catch (java.util.concurrent.RejectedExecutionException e) {
                // shut down between the check and the call: fall through
            }
        }
        endAll(expired, "expired");
    }

    private static void endAll(List<Session> sessions, String why) {
        for (Session s : sessions) {
            endHeld(s.held, why, s.owner);
        }
    }

    private void startSweeperLocked() {
        if (sweeper != null || closed) return;
        long ms = Math.max(50, config.sweepInterval().toMillis());
        sweeper = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "nx-export-expiry");
            t.setDaemon(true);
            return t;
        });
        sweeper.scheduleWithFixedDelay(() -> {
            try {
                sweep();
            } catch (RuntimeException e) {
                log.error("event=export_session_sweep_failed", e);
            }
        }, ms, ms, TimeUnit.MILLISECONDS);
    }

    /**
     * Starts the held transaction and takes the snapshot. The first and only statement is
     * {@code pg_export_snapshot()}: the transaction reads no table and holds no lock but its own virtual
     * transaction id. The connection is autocommit-off, read-only and {@code REPEATABLE READ}, so the driver's
     * implicit BEGIN carries those characteristics.
     */
    private static String exportSnapshot(Connection conn) throws SQLException {
        conn.setAutoCommit(false);
        conn.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
        conn.setReadOnly(true);
        return DSL.using(conn, SQLDialect.POSTGRES)
            .select(DSL.function("pg_export_snapshot", SQLDataType.VARCHAR))
            .fetchOne(0, String.class);
    }

    /** Ends a held transaction and returns its connection; never throws. */
    private static void endHeld(Connection conn, String why, String tenant) {
        if (conn == null) return;
        try {
            conn.rollback();
        } catch (SQLException e) {
            log.warn("event=export_held_rollback_failed why={} tenant={} error={}", why, tenant, e.getMessage());
        }
        try {
            conn.setReadOnly(false);
            conn.setAutoCommit(true);
        } catch (SQLException e) {
            log.warn("event=export_held_restore_failed why={} tenant={} error={}", why, tenant, e.getMessage());
        }
        try {
            conn.close();
        } catch (SQLException e) {
            log.warn("event=export_held_close_failed why={} tenant={} error={}", why, tenant, e.getMessage());
        }
    }
}
