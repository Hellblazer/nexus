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

    /**
     * Session and page settings. {@code pageBytes} is the page's byte target (about 16 MB: under the cloud
     * edge's 20 MB body cap); {@code pageMaxRows} bounds a page of small rows; {@code lockTimeoutMs} bounds a
     * page's wait for a table lock behind a tenant create; {@code retryAfterSeconds} is what a full engine
     * asks a client to wait.
     */
    public record Config(Duration totalBound, Duration idleBound, int maxSessions, int pageBytes, int pageMaxRows,
                         int lockTimeoutMs, int retryAfterSeconds, Duration sweepInterval) {
        public Config {
            if (totalBound.isNegative() || totalBound.isZero() || idleBound.isNegative() || idleBound.isZero()) {
                throw new IllegalArgumentException("export session bounds must be positive");
            }
            if (maxSessions < 1 || pageBytes < 1 || pageMaxRows < 1) {
                throw new IllegalArgumentException("export session capacity and page limits must be at least 1");
            }
        }

        public static Config defaults() {
            return new Config(Duration.ofMinutes(60), Duration.ofMinutes(10), 2, 16 * 1024 * 1024, 100_000,
                5_000, 30, Duration.ofSeconds(5));
        }

        /** The defaults, with each {@code NX_EXPORT_*} value that {@code env} answers; a malformed one is an error. */
        public static Config fromEnv(Function<String, String> env) {
            Config d = defaults();
            return new Config(
                Duration.ofSeconds(positive(env, ENV_TOTAL_SECONDS, d.totalBound().toSeconds())),
                Duration.ofSeconds(positive(env, ENV_IDLE_SECONDS, d.idleBound().toSeconds())),
                (int) positive(env, ENV_MAX_SESSIONS, d.maxSessions()),
                (int) positive(env, ENV_PAGE_BYTES, d.pageBytes()),
                d.pageMaxRows(), d.lockTimeoutMs(), d.retryAfterSeconds(), d.sweepInterval());
        }

        private static long positive(Function<String, String> env, String name, long fallback) {
            String v = env.apply(name);
            if (v == null || v.isBlank()) return fallback;
            try {
                long n = Long.parseLong(v.trim());
                if (n < 1) throw new IllegalArgumentException(name + " must be at least 1: " + v);
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

        SessionExistsException(String sessionId) {
            super("this tenant already has an export session");
            this.sessionId = sessionId;
        }

        public String sessionId() {
            return sessionId;
        }
    }

    /** The engine holds its maximum number of sessions. */
    public static final class CapacityFullException extends RuntimeException {
        private final int retryAfterSeconds;

        CapacityFullException(int retryAfterSeconds) {
            super("this engine already holds its maximum number of export sessions");
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
    private ScheduledExecutorService sweeper;
    private boolean closed;

    public TenantExportSessions(DataSource dataSource, Config config, Clock clock) {
        this.dataSource = dataSource;
        this.config = config;
        this.clock = clock;
        this.reads = new TenantExportReads(dataSource, config.lockTimeoutMs());
    }

    public Config config() {
        return config;
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
        endAll(expired, "expired");
        synchronized (lock) {
            Session existing = byOwner.get(tenant);
            if (existing != null) throw new SessionExistsException(existing.id);
            if (opening.contains(tenant)) throw new SessionExistsException(null);
            if (byOwner.size() + opening.size() >= config.maxSessions()) {
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
        endAll(expired, "expired");
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
        endAll(expired, "expired");
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

    /** Ends every session and stops the sweep; the engine's stop calls it, so no held connection outlives the pool. */
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
        if (toStop != null) toStop.shutdownNow();
        for (Session s : all) {
            endHeld(s.held, "engine_stop", s.owner);
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
