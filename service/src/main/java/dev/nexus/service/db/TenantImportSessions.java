// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.db.TenantImportPlan.FileTable;
import dev.nexus.service.db.TenantImportPlan.Plan;
import dev.nexus.service.db.TenantImportPlan.TablePlan;
import dev.nexus.service.db.TenantImportStaging.Stage;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.postgresql.PGConnection;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.Supplier;

/**
 * The engine's tenant import sessions (RDR-248 "Import session", bead nexus-csayy.6). The cloud edge ends every
 * exchange at 60 seconds and caps a body at 20 MB, so an import is a session of short requests: open (the file's
 * header and its refusal checks), parts (each a complete binary COPY stream, at most 8 MiB, staged into a temp
 * table with the FILE's columns), a close per table, and a commit that runs the load ({@link TenantImportLoad}) on
 * the session's held connection after answering 202.
 *
 * <p><b>One session slot.</b> An import shares the per-engine cap and the one-per-tenant rule of the export
 * sessions ({@link TenantExportSessions#reserveSlot}), because each holds one dedicated pooled connection for the
 * whole session. The sessions live in this engine's memory, which is sound because one engine runs per database;
 * {@code open} therefore refuses unless the engine says it holds the singleton lock.
 *
 * <p><b>The held connection</b> is READ COMMITTED with the tenant stamped, in one open transaction from the first
 * stage table to the load's commit. A session that ends closes that connection and evicts it from the pool, never
 * returning it: temp tables and session settings would outlive it. The refusal checks of {@code open} run on pooled
 * connections in their own short transactions, so no check holds a lock through the upload.
 *
 * <p><b>Bounds.</b> Before the first commit attempt: idle 10 minutes without a part, 60 minutes in all (the export
 * sessions' bounds). A commit refused {@code restore_busy} counts as activity; the 60-minute total stops at the
 * first attempt and the restore-wait bound, {@link Config#loadMax()} from that attempt, applies instead. After the
 * commit the load has {@link Config#loadMax()} in all, enforced by cancelling the load's backend, and each
 * statement runs under a statement timeout of the same value. A status poll is not activity.
 *
 * <p><b>Outcomes.</b> A session's outcome ({@code committed}, {@code failed: <reason>}) is kept in memory for
 * {@link Config#outcomeRetention()} (24 hours). A dropped client neither stops nor rolls back a load; after an
 * engine restart the in-memory outcome is gone and the client reads the restore marker instead
 * ({@link #readMarker}).
 */
public final class TenantImportSessions implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(TenantImportSessions.class);

    public static final String ENV_MAX_BYTES = "NX_IMPORT_MAX_BYTES";
    /** Whole seconds, the load's total bound and the restore-wait bound. */
    public static final String ENV_LOAD_MAX = "NX_IMPORT_LOAD_MAX";

    /** The largest part the engine takes, and what an open answers as the client's part size. */
    public static final int PART_BYTES = 8 * 1024 * 1024;

    /** Each statement of the staging transaction (a part's COPY, a stage's creation) runs under this bound. */
    private static final long STAGING_STATEMENT_TIMEOUT_MS = 120_000L;

    /** Import settings; the idle and total bounds of an upload are the export sessions'. */
    public record Config(long maxBytes, Duration loadMax, int restoreRetryAfterSeconds, Duration restoreLockWait,
                         Duration outcomeRetention, Duration sweepInterval) {
        public Config {
            if (maxBytes < 1) throw new IllegalArgumentException(ENV_MAX_BYTES + " must be at least 1");
            if (loadMax.isNegative() || loadMax.isZero() || loadMax.toSeconds() > 7L * 24 * 3600) {
                throw new IllegalArgumentException(ENV_LOAD_MAX + " must be between 1 second and a week");
            }
            if (restoreRetryAfterSeconds < 1 || restoreLockWait.isNegative() || outcomeRetention.isNegative()
                    || sweepInterval.isNegative() || sweepInterval.isZero()) {
                throw new IllegalArgumentException("import limits must be positive");
            }
        }

        public static Config defaults() {
            return new Config(8L << 30, Duration.ofMinutes(60), 60, Duration.ofSeconds(5), Duration.ofHours(24),
                Duration.ofSeconds(5));
        }

        public static Config fromEnv(Function<String, String> env) {
            Config d = defaults();
            return new Config(longEnv(env, ENV_MAX_BYTES, d.maxBytes(), 1L << 40),
                Duration.ofSeconds(longEnv(env, ENV_LOAD_MAX, d.loadMax().toSeconds(), 7L * 24 * 3600)),
                d.restoreRetryAfterSeconds(), d.restoreLockWait(), d.outcomeRetention(), d.sweepInterval());
        }

        public Config withMaxBytes(long n) {
            return new Config(n, loadMax, restoreRetryAfterSeconds, restoreLockWait, outcomeRetention, sweepInterval);
        }

        public Config withLoadMax(Duration d) {
            return new Config(maxBytes, d, restoreRetryAfterSeconds, restoreLockWait, outcomeRetention, sweepInterval);
        }

        public Config withRestoreLockWait(Duration d) {
            return new Config(maxBytes, loadMax, restoreRetryAfterSeconds, d, outcomeRetention, sweepInterval);
        }

        public Config withSweepInterval(Duration d) {
            return new Config(maxBytes, loadMax, restoreRetryAfterSeconds, restoreLockWait, outcomeRetention, d);
        }

        private static long longEnv(Function<String, String> env, String name, long fallback, long max) {
            String v = env.apply(name);
            if (v == null || v.isBlank()) return fallback;
            try {
                long n = Long.parseLong(v.trim());
                if (n < 1 || n > max) throw new IllegalArgumentException(name + " must be between 1 and " + max + ": " + v);
                return n;
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(name + " is not a whole number: " + v);
            }
        }
    }

    /**
     * An answer the handler sends as it is: {@code status} and {@code reason} for the body, an optional
     * {@code Retry-After}, and extra body fields (the open session's id on a 409).
     */
    public static final class RefusedException extends RuntimeException {
        private final int status;
        private final String reason;
        private final Integer retryAfterSeconds;
        private final Map<String, Object> extra;

        public RefusedException(int status, String reason, String message) {
            this(status, reason, message, null, Map.of());
        }

        public RefusedException(int status, String reason, String message, Integer retryAfterSeconds,
                                Map<String, Object> extra) {
            super(message);
            this.status = status;
            this.reason = reason;
            this.retryAfterSeconds = retryAfterSeconds;
            this.extra = extra;
        }

        public int status() { return status; }
        public String reason() { return reason; }
        public Integer retryAfterSeconds() { return retryAfterSeconds; }
        public Map<String, Object> extra() { return extra; }
    }

    /** The file's header and what the client declared about the upload. */
    public record OpenRequest(int formatVersion, String engineVersion, Long schemaChangesetCount, String sourceTenant,
                              String headerSha256, long rowBytes, List<FileTable> tables, List<String> embeddingModels) {
        public OpenRequest {
            tables = List.copyOf(tables);
            embeddingModels = List.copyOf(embeddingModels);
        }
    }

    private enum State { STAGING, LOADING, COMMITTED, FAILED }

    private static final class TableState {
        final String name;
        boolean closed;
        long rows;
        int lastPart = -1;
        String lastSha;

        TableState(String name) {
            this.name = name;
        }
    }

    /** An open (or just ended) session. Fields are guarded by the sessions' lock; the connection by {@code gate}. */
    public static final class Session {
        private final String id;
        private final String tenant;
        private final TenantExportSessions.Slot slot;
        private final Connection held;
        private final DSLContext ctx;
        private final OpenRequest request;
        private final Plan plan;
        private final Map<String, Stage> stages;
        private final Map<String, TableState> tables = new LinkedHashMap<>();
        private final Set<String> ignored;
        private final Instant openedAt;
        private final Semaphore gate = new Semaphore(1);
        private Instant lastActive;
        private Instant firstCommitAt;
        private Instant loadStartedAt;
        private State state = State.STAGING;
        private String status = "staging";
        private long stagedBytes;
        private volatile String cancelReason;
        private boolean ended;
        /** True once the load's COMMIT has returned: from then on the outcome is committed, whatever else races it. */
        private boolean committed;
        private TenantImportReport report;
        private final List<String> notes = new ArrayList<>();

        private Session(String id, String tenant, TenantExportSessions.Slot slot, Connection held, DSLContext ctx,
                        OpenRequest request, Plan plan, Map<String, Stage> stages, Set<String> ignored, Instant now) {
            this.id = id;
            this.tenant = tenant;
            this.slot = slot;
            this.held = held;
            this.ctx = ctx;
            this.request = request;
            this.plan = plan;
            this.stages = stages;
            this.ignored = ignored;
            this.openedAt = now;
            this.lastActive = now;
        }

        public String id() { return id; }
        public String tenant() { return tenant; }
        public Instant openedAt() { return openedAt; }

        /** The held connection, for a test that checks what the session holds. */
        Connection heldConnectionForTests() { return held; }
    }

    /** What an open answers. */
    public record Opened(Session session, Map<String, String> skippedTables, List<String> notes, List<String> loadOrder) {
    }

    /** What the status route reports. {@code state} is staging, loading, verifying, committed or failed. */
    public record StatusView(String id, String state, String status, TenantImportReport report, List<String> notes,
                             long stagedBytes) {
    }

    private record Outcome(String tenant, String status, TenantImportReport report, List<String> notes, Instant endedAt) {
    }

    /** What a part upload did. */
    public enum PartResult { STAGED, REPEAT, SKIPPED }

    /** What a commit did: started the load (202), or answers the status of a load already started or ended (200). */
    public enum CommitResult { STARTED, STATUS }

    private final Supplier<TenantExportSessions> exports;
    private final DataSource dataSource;
    private final Config config;
    private final TenantImportStaging staging;
    private final Consumer<String> onCommitted;
    private final Supplier<Boolean> singletonLockHeld;
    private final Supplier<Long> engineChangesets;
    private final List<TenantExport.Retired> retired;

    private final Object lock = new Object();
    private final Map<String, Session> byTenant = new HashMap<>();
    private final Map<String, Outcome> outcomes = new HashMap<>();
    private ScheduledExecutorService sweeper;
    private final ExecutorService loader = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "nx-import-load");
        t.setDaemon(true);
        return t;
    });
    private boolean closed;

    /** Test seam: called with the table name inside a part's turn at the connection, before its COPY; null in production. */
    private volatile Consumer<String> partHookForTests;
    /** Test seam: called with each status text the load reports, on the load's thread; null in production. */
    private volatile Consumer<String> loadHookForTests;

    /** Test seam: a hook run inside a part's turn at the session's connection, before its COPY (a test holds it to keep a part in flight). */
    public void partHookForTests(Consumer<String> hook) {
        this.partHookForTests = hook;
    }

    /** Test seam: a hook run on the load's thread at each status text (a test holds the load open between two tables). */
    public void loadHookForTests(Consumer<String> hook) {
        this.loadHookForTests = hook;
    }

    /**
     * @param exports           the export sessions this one shares its cap with; a supplier so a test can swap them
     * @param onCommitted       runs after a load commits, with the tenant (evicts the tenant's cached state)
     * @param singletonLockHeld whether this engine holds the one-engine-per-database lock; {@code open} refuses when not
     * @param engineChangesets  the engine's schema changeset count, for the open's note; null when unread
     */
    public TenantImportSessions(Supplier<TenantExportSessions> exports, DataSource dataSource, Config config,
                                Consumer<String> onCommitted, Supplier<Boolean> singletonLockHeld,
                                Supplier<Long> engineChangesets) {
        this(exports, dataSource, config, onCommitted, singletonLockHeld, engineChangesets, TenantExport.RETIRED);
    }

    /** As above, with the registry's retired list given: a test retires a column the registry has no entry for. */
    public TenantImportSessions(Supplier<TenantExportSessions> exports, DataSource dataSource, Config config,
                                Consumer<String> onCommitted, Supplier<Boolean> singletonLockHeld,
                                Supplier<Long> engineChangesets, List<TenantExport.Retired> retired) {
        this.retired = List.copyOf(retired);
        this.exports = exports;
        this.dataSource = dataSource;
        this.config = config;
        this.staging = new TenantImportStaging(dataSource);
        this.onCommitted = onCommitted;
        this.singletonLockHeld = singletonLockHeld;
        this.engineChangesets = engineChangesets;
    }

    public Config config() {
        return config;
    }

    private Instant now() {
        return exports.get().clock().instant();
    }

    // ── open ────────────────────────────────────────────────────────────────────────────────────

    /**
     * Opens {@code tenant}'s import session after every refusal check (RDR-248 "Open").
     *
     * @throws RefusedException the refusal, with the status and reason the route answers
     */
    public Opened open(String tenant, OpenRequest req) {
        if (!Boolean.TRUE.equals(singletonLockHeld.get())) {
            throw new RefusedException(503, "singleton_lock_not_held", "this engine does not hold the engine singleton lock "
                + "(EngineSingletonLock): import sessions live in one engine's memory, so a second engine, or an engine "
                + "run with NX_ENGINE_SINGLETON_LOCK=off or behind a pooler, refuses to open one");
        }
        if (closed) throw new RefusedException(503, "shutting_down", "the engine is stopping");
        if (req.formatVersion() != 1) {
            throw new RefusedException(422, "unsupported_format_version", "this engine reads backup format version 1; the file is "
                + req.formatVersion());
        }
        if (req.headerSha256() == null || !req.headerSha256().matches("[0-9a-f]{64}")) {
            throw new RefusedException(400, "bad_header_sha256", "header_sha256 must be 64 lower-case hex digits");
        }
        if (req.rowBytes() < 0) {
            throw new RefusedException(400, "bad_row_bytes", "row_bytes must not be negative");
        }
        if (req.rowBytes() > config.maxBytes()) {
            throw new RefusedException(413, "import_too_large", "the file declares " + req.rowBytes() + " row bytes; this engine"
                + " takes at most " + config.maxBytes() + " (" + ENV_MAX_BYTES + ")");
        }
        TenantExportSessions shared = exports.get();
        String id = UUID.randomUUID().toString();
        TenantExportSessions.Slot slot;
        try {
            slot = shared.reserveSlot(tenant, id);
        } catch (TenantExportSessions.SessionExistsException e) {
            Map<String, Object> extra = e.sessionId() == null ? Map.of() : Map.of("session_id", e.sessionId(), "session_kind", e.kind());
            throw new RefusedException(409, "session_exists", e.getMessage() + " (DELETE the session id given to close it)",
                null, extra);
        } catch (TenantExportSessions.CapacityFullException e) {
            throw new RefusedException(503, "import_capacity_full", e.getMessage(), e.retryAfterSeconds(), Map.of());
        }
        Connection held = null;
        boolean published = false;
        try {
            Map<String, List<TenantImportPlan.TargetColumn>> target = staging.targetColumns();
            Plan plan = TenantImportPlan.analyze(req.tables(), target, retired);
            if (plan.refused()) {
                throw new RefusedException(422, "file_not_loadable", plan.refusals().get(0),
                    null, Map.of("problems", plan.refusals()));
            }
            List<String> missing = staging.missingModels(req.embeddingModels());
            if (!missing.isEmpty()) {
                throw new RefusedException(422, "missing_embedding_model", "the target has no embedding_models row for "
                    + String.join(", ", missing), null, Map.of("models", missing));
            }
            Map<String, String> skippedNow = new LinkedHashMap<>();
            String nonEmpty = staging.inTenantTransaction(tenant, ctx -> {
                for (TablePlan tp : plan.tables().values()) {
                    if (!tp.loads()) skippedNow.put(tp.name(), tp.skipReason());
                    else if (tp.entry().exportClass() == TenantExport.ExportClass.DERIVED
                            && TenantImportStaging.derivedHasRows(ctx, tp.entry())) {
                        skippedNow.put(tp.name(), "the target already holds rows for it");
                    }
                }
                return TenantImportStaging.firstNonEmptyDataTable(ctx, tenant).orElse(null);
            });
            if (nonEmpty != null) {
                throw new RefusedException(409, "tenant_not_empty", "tenant " + tenant + " already holds rows in " + nonEmpty
                    + "; a restore loads into an empty tenant only (nothing was written)", null, Map.of("table", nonEmpty));
            }
            held = dataSource.getConnection();
            held.setAutoCommit(false);
            held.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);
            DSLContext ctx = DSL.using(held, SQLDialect.POSTGRES);
            PgSession.GucBatch stamp = PgSession.gucBatch(ctx).stamp(TenantScope.DEFAULT_TENANT_GUC, tenant);
            stamp.set("statement_timeout", Long.toString(STAGING_STATEMENT_TIMEOUT_MS));
            stamp.apply(held);
            Map<String, Stage> stages;
            try {
                stages = staging.createStages(ctx, plan.tables());
            } catch (org.jooq.exception.DataAccessException e) {
                throw new RefusedException(422, "file_not_loadable", "the file's columns could not be staged: " + e.getMessage());
            }
            Set<String> ignored = new LinkedHashSet<>();
            for (FileTable t : req.tables()) {
                TablePlan tp = plan.tables().get(t.name());
                if (tp == null || !tp.loads()) ignored.add(t.name());
            }
            Session s = new Session(id, tenant, slot, held, ctx, req, plan, stages, ignored, shared.clock().instant());
            for (TablePlan tp : plan.tables().values()) {
                if (tp.loads()) s.tables.put(tp.name(), new TableState(tp.name()));
            }
            s.notes.addAll(plan.notes());
            Long engineCount = engineChangesets.get();
            if (req.schemaChangesetCount() != null && engineCount != null && !req.schemaChangesetCount().equals(engineCount)) {
                String note = "the file's schema_changeset_count is " + req.schemaChangesetCount() + " and this engine's is "
                    + engineCount + "; the columns decide, the difference is shown";
                s.notes.add(note);
                log.info("event=import_changeset_count_differs tenant={} file={} engine={}", tenant, req.schemaChangesetCount(), engineCount);
            }
            synchronized (lock) {
                if (closed) throw new RefusedException(503, "shutting_down", "the engine is stopping");
                byTenant.put(tenant, s);
                startSweeperLocked();
                published = true;
            }
            log.info("event=import_session_opened session={} tenant={} tables={} skipped={}", id, tenant,
                s.tables.size(), skippedNow.keySet());
            return new Opened(s, skippedNow, List.copyOf(s.notes), List.copyOf(s.tables.keySet()));
        } catch (SQLException e) {
            throw new IllegalStateException("could not open an import session", e);
        } finally {
            if (!published) {
                shared.releaseSlot(slot);
                endHeld(held, tenant);
            }
        }
    }

    // ── lookup ──────────────────────────────────────────────────────────────────────────────────

    private RefusedException noSuchSession() {
        return new RefusedException(404, "no_such_session", "no such import session");
    }

    /** The tenant's active session {@code id}; {@code touch} restarts its idle clock. */
    private Session active(String tenant, String id, boolean touch) {
        Session s;
        synchronized (lock) {
            s = byTenant.get(tenant);
            if (s != null && !s.id.equals(id)) s = null;
            if (s != null && touch) s.lastActive = now();
        }
        boolean mineEnded = false;
        for (Due d : expireDue()) {
            if (endExpired(d) && d.session() == s) mineEnded = true;
        }
        if (s == null || mineEnded) throw noSuchSession();
        return s;
    }

    // ── parts and tables ────────────────────────────────────────────────────────────────────────

    /**
     * Stages one part of {@code table}. A repeat of the table's last staged part with the same SHA-256 stages nothing;
     * any other repeat, and a gap, is refused. A part whose hash passed but whose COPY fails (a malformed stream, a
     * full disk, a timeout) fails the session.
     */
    public PartResult stagePart(String tenant, String id, String table, int part, String sha256, byte[] body) {
        Session s = active(tenant, id, true);
        if (!s.gate.tryAcquire()) {
            throw new RefusedException(409, "part_in_flight", "another request of this session is running", 1, Map.of());
        }
        try {
            TableState ts;
            synchronized (lock) {
                if (s.ended || s.state != State.STAGING) {
                    throw new RefusedException(409, "not_staging", "this session is no longer taking parts: " + s.status);
                }
                ts = s.tables.get(table);
                if (ts == null) {
                    if (s.ignored.contains(table)) return PartResult.SKIPPED;
                    throw new RefusedException(400, "unknown_table", "the file's header has no table " + table);
                }
                requireOrder(s, ts);
                if (ts.closed) {
                    throw new RefusedException(409, "table_closed", "table " + table + " is closed; its parts are all in");
                }
                if (part < 0) throw new RefusedException(400, "bad_part", "part must be 0 or more");
                if (part == ts.lastPart) {
                    if (sha256.equals(ts.lastSha)) return PartResult.REPEAT;
                    throw new RefusedException(409, "part_sha_conflict", "part " + part + " of " + table
                        + " is staged with another SHA-256");
                }
                if (part < ts.lastPart) {
                    throw new RefusedException(409, "part_already_staged", "part " + part + " of " + table + " is already staged");
                }
                if (part > ts.lastPart + 1) {
                    throw new RefusedException(409, "part_gap", "table " + table + " expects part " + (ts.lastPart + 1)
                        + " next, not " + part);
                }
                if (s.stagedBytes + body.length > config.maxBytes()) {
                    throw new RefusedException(413, "import_too_large", "this part would take the staged bytes past "
                        + config.maxBytes() + " (" + ENV_MAX_BYTES + ")");
                }
            }
            long rows;
            try {
                Consumer<String> hook = partHookForTests;
                if (hook != null) hook.accept(table);
                rows = staging.copyPart(s.held, s.plan.tables().get(table), s.stages.get(table), body);
            } catch (SQLException | java.io.IOException | RuntimeException e) {
                String why = s.cancelReason != null ? s.cancelReason : e.getMessage();
                failAndEnd(s, "part " + table + "/" + part + ": " + why);
                throw new RefusedException(422, "part_failed", "part " + table + "/" + part + " failed and the session"
                    + " is closed: " + why + "; start over");
            }
            synchronized (lock) {
                ts.rows += rows;
                ts.lastPart = part;
                ts.lastSha = sha256;
                s.stagedBytes += body.length;
                s.lastActive = now();
            }
            return PartResult.STAGED;
        } finally {
            s.gate.release();
        }
    }

    private static void requireOrder(Session s, TableState ts) {
        for (TableState earlier : s.tables.values()) {
            if (earlier == ts) return;
            if (!earlier.closed) {
                throw new RefusedException(409, "table_out_of_order", "tables arrive in load order: close " + earlier.name
                    + " before " + ts.name);
            }
        }
    }

    /**
     * Closes {@code table} at {@code rows}, the row count of the file's trailer: the staged count must equal it.
     *
     * @return true when the table is one the load skips (nothing was checked)
     */
    public boolean closeTable(String tenant, String id, String table, long rows) {
        Session s = active(tenant, id, true);
        if (!s.gate.tryAcquire()) {   // a close must not race a part of the same table that is still being staged
            throw new RefusedException(409, "part_in_flight", "another request of this session is running", 1, Map.of());
        }
        try {
            return closeTableLocked(s, table, rows);
        } finally {
            s.gate.release();
        }
    }

    private boolean closeTableLocked(Session s, String table, long rows) {
        synchronized (lock) {
            if (s.ended || s.state != State.STAGING) {
                throw new RefusedException(409, "not_staging", "this session is no longer taking tables: " + s.status);
            }
            TableState ts = s.tables.get(table);
            if (ts == null) {
                if (s.ignored.contains(table)) return true;
                throw new RefusedException(400, "unknown_table", "the file's header has no table " + table);
            }
            if (ts.closed) {
                if (ts.rows == rows) return false;
                throw new RefusedException(409, "row_count_mismatch", "table " + table + " is closed at " + ts.rows
                    + " rows, not " + rows, null, Map.of("staged", ts.rows, "expected", rows));
            }
            requireOrder(s, ts);
            if (ts.rows != rows) {
                throw new RefusedException(409, "row_count_mismatch", "table " + table + " has " + ts.rows
                    + " staged rows and the file's trailer says " + rows, null, Map.of("staged", ts.rows, "expected", rows));
            }
            ts.closed = true;
            s.lastActive = now();
            return false;
        }
    }

    // ── commit and the load ─────────────────────────────────────────────────────────────────────

    /**
     * Starts the load: the restore-load lock is taken on the held connection first, so {@code restore_busy} is this
     * call's own answer; the load then runs on a thread of its own and this returns {@link CommitResult#STARTED} (202).
     * A repeat while the load runs, or after it ended, returns {@link CommitResult#STATUS} and starts nothing.
     */
    public CommitResult commit(String tenant, String id) {
        synchronized (lock) {
            Outcome ended = outcomes.get(id);
            if (ended != null && ended.tenant().equals(tenant)) return CommitResult.STATUS;   // the load is over: say how it went
        }
        Session s = active(tenant, id, true);
        synchronized (lock) {
            if (s.state != State.STAGING) return CommitResult.STATUS;
            List<String> open = new ArrayList<>();
            s.tables.values().stream().filter(t -> !t.closed).forEach(t -> open.add(t.name));
            if (!open.isEmpty()) {
                throw new RefusedException(409, "tables_open", "these tables are not closed: " + String.join(", ", open),
                    null, Map.of("tables", open));
            }
            if (s.firstCommitAt == null) s.firstCommitAt = now();
        }
        if (!s.gate.tryAcquire()) {
            throw new RefusedException(409, "part_in_flight", "another request of this session is running", 1, Map.of());
        }
        boolean started = false;
        try {
            synchronized (lock) {
                if (s.state != State.STAGING) return CommitResult.STATUS;
            }
            boolean locked = takeRestoreLock(s);
            synchronized (lock) {
                s.lastActive = now();   // a refused commit is activity for the idle bound
            }
            if (!locked) {
                throw new RefusedException(503, "restore_busy", "another tenant restore is loading; the upload stays staged,"
                    + " commit again", config.restoreRetryAfterSeconds(), Map.of());
            }
            synchronized (lock) {
                s.state = State.LOADING;
                s.status = "loading";
                s.loadStartedAt = now();
            }
            started = true;
            loader.execute(() -> runLoad(s));
            return CommitResult.STARTED;
        } catch (SQLException e) {
            failAndEnd(s, "could not take the restore lock: " + e.getMessage());
            throw new RefusedException(500, "internal", "could not take the restore lock: " + e.getMessage());
        } finally {
            if (!started) s.gate.release();
        }
    }

    /**
     * The restore-load lock, exclusive, for the held transaction. A tenant create holds it shared for a moment; that is
     * waited out up to {@link Config#restoreLockWait()}. A load holding it exclusive is not waited for.
     */
    private boolean takeRestoreLock(Session s) throws SQLException {
        long deadline = System.nanoTime() + config.restoreLockWait().toNanos();
        while (true) {
            try {
                if (RestoreLoadLock.tryExclusive(s.ctx)) return true;
                if (RestoreLoadLock.heldByLoad(s.ctx)) return false;
            } catch (org.jooq.exception.DataAccessException e) {
                throw new SQLException(e.getMessage(), e);
            }
            if (System.nanoTime() >= deadline) return false;
            try {
                Thread.sleep(100);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }

    private void runLoad(Session s) {
        String failure = null;
        TenantImportReport report = null;
        Error fatal = null;
        try {
            PgSession.setLocal(s.ctx, "statement_timeout", Long.toString(config.loadMax().toMillis()));
            Map<String, Long> staged = new HashMap<>();
            synchronized (lock) {
                s.tables.values().forEach(t -> staged.put(t.name, t.rows));
            }
            report = TenantImportLoad.run(s.ctx, s.tenant, s.plan.tables(), s.stages, staged, s.request.headerSha256(),
                s.id, now(), text -> {
                    if (s.cancelReason != null) throw new TenantImportLoad.LoadFailedException(s.cancelReason);
                    synchronized (lock) {
                        s.status = text;
                    }
                    Consumer<String> hook = loadHookForTests;
                    if (hook != null) hook.accept(text);
                    if (s.cancelReason != null) throw new TenantImportLoad.LoadFailedException(s.cancelReason);
                });
            if (s.cancelReason != null) throw new TenantImportLoad.LoadFailedException(s.cancelReason);
            try {
                s.held.commit();
            } catch (SQLException e) {
                throw new TenantImportLoad.LoadFailedException("the commit was not confirmed (" + e.getMessage()
                    + "); read /v1/tenant/import-marker to learn whether it landed");
            }
            synchronized (lock) {   // the one place a load becomes committed: after the COMMIT returned
                s.report = report;
                s.committed = true;
            }
        } catch (TenantImportLoad.LoadFailedException e) {
            failure = e.getMessage();
        } catch (Throwable t) {   // an Error too: the outcome is recorded as failed, never left to read committed
            failure = s.cancelReason != null ? s.cancelReason : describe(t);
            if (t instanceof Error err) fatal = err;
            log.warn("event=import_load_failed session={} tenant={} error={}", s.id, s.tenant, t.toString());
        }
        boolean committed;
        try {
            synchronized (lock) {
                committed = s.committed;
            }
            // finish() itself turns any end of a committed session into committed, so an abort, an eviction or an expiry that
            // raced the COMMIT cannot overwrite it
            // a failed outcome never carries a report: the wire entry defines report as the commit's, so one on a failure
            // (run() returned, then the COMMIT was not confirmed) would read as success to a client (confirm round, 2026-11-13)
            finish(s, committed ? State.COMMITTED : State.FAILED, committed ? "committed" : "failed: " + failure,
                committed ? report : null);
        } finally {
            s.gate.release();
        }
        if (committed) {
            try {
                onCommitted.accept(s.tenant);
            } catch (RuntimeException e) {
                log.warn("event=import_post_commit_hook_failed session={} tenant={} error={}", s.id, s.tenant, e.getMessage());
            }
            log.info("event=import_committed session={} tenant={} rows={} dropped={}", s.id, s.tenant,
                s.report.insertedRows(), s.report.droppedRows());
        } else {
            log.warn("event=import_failed session={} tenant={} reason={}", s.id, s.tenant, failure);
        }
        if (fatal != null) throw fatal;
    }

    private static String describe(Throwable e) {
        for (Throwable c = e; c != null; c = c.getCause()) {
            if (c instanceof SQLException se) {
                if ("57014".equals(se.getSQLState())) return "cancelled (the load's time bound or an abort): " + se.getMessage();
                return se.getMessage();
            }
        }
        return String.valueOf(e.getMessage());
    }

    // ── status, marker, abort ───────────────────────────────────────────────────────────────────

    /** The session's status; for an ended session, its recorded outcome. A poll is not activity. */
    public StatusView status(String tenant, String id) {
        synchronized (lock) {
            Session s = byTenant.get(tenant);
            if (s != null && s.id.equals(id)) {
                return view(s);
            }
            Outcome o = outcomes.get(id);
            if (o != null && o.tenant().equals(tenant)) {
                String state = o.status().startsWith("failed") ? "failed" : "committed";
                return new StatusView(id, state, o.status(), o.report(), o.notes(), 0);
            }
        }
        throw noSuchSession();
    }

    private StatusView view(Session s) {
        String state = switch (s.state) {
            case STAGING -> "staging";
            case COMMITTED -> "committed";
            case FAILED -> "failed";
            case LOADING -> "verifying".equals(s.status) ? "verifying" : "loading";
        };
        return new StatusView(s.id, state, s.status, s.report, List.copyOf(s.notes), s.stagedBytes);
    }

    /** The tenant's restore marker JSON ({@code catalog_meta} key {@code rdr248_restore}), or null when none. */
    public String readMarker(String tenant) {
        return staging.inTenantTransaction(tenant, ctx -> ctx.select(DSL.field(DSL.name("value"), String.class))
            .from(DSL.table(DSL.name("nexus", "catalog_meta")))
            .where(DSL.field(DSL.name("key"), String.class).eq(TenantImportLoad.MARKER_KEY))
            .fetchOne(0, String.class));
    }

    /** Aborts: before the commit it rolls the staging back; during the load it cancels the backend (status {@code failed: aborted}). */
    public void abort(String tenant, String id) {
        Session s = active(tenant, id, false);
        s.cancelReason = "aborted";
        if (s.gate.tryAcquire()) {
            try {
                synchronized (lock) {
                    if (s.state != State.STAGING) return;   // the load's thread took the gate and will see cancelReason
                }
                finish(s, State.FAILED, "failed: aborted", null);
            } finally {
                s.gate.release();
            }
            return;
        }
        cancelBackend(s);   // a part or the load is running: it fails on the cancel and ends the session
        if (s.state == State.STAGING) {
            try {
                if (s.gate.tryAcquire(30, TimeUnit.SECONDS)) {
                    try {
                        finish(s, State.FAILED, "failed: aborted", null);
                    } finally {
                        s.gate.release();
                    }
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    private static void cancelBackend(Session s) {
        try {
            s.held.unwrap(PGConnection.class).cancelQuery();
        } catch (SQLException | RuntimeException e) {
            log.warn("event=import_cancel_failed session={} error={}", s.id, e.getMessage());
        }
    }

    /** Ends the tenant's session if it has one (the tenant reset route's evictor); returns how many. */
    public int evictTenant(String tenant) {
        Session s;
        synchronized (lock) {
            s = byTenant.get(tenant);
            outcomes.values().removeIf(o -> o.tenant().equals(tenant));
        }
        if (s == null) return 0;
        s.cancelReason = "evicted";
        cancelBackend(s);
        finish(s, State.FAILED, "failed: evicted", null);
        synchronized (lock) {
            outcomes.values().removeIf(o -> o.tenant().equals(tenant));
        }
        return 1;
    }

    /** Sessions open now. */
    public int openCount() {
        synchronized (lock) {
            return byTenant.size();
        }
    }

    // ── ending a session ────────────────────────────────────────────────────────────────────────

    /** Fails the session and ends it; the caller holds the gate. */
    private void failAndEnd(Session s, String reason) {
        finish(s, State.FAILED, "failed: " + reason, null);
    }

    /**
     * Ends {@code s} with {@code status}: records the outcome, frees the slot, and closes its held connection (rolled
     * back unless it committed) and evicts it from the pool. Idempotent.
     */
    private void finish(Session s, State end, String status, TenantImportReport report) {
        synchronized (lock) {
            if (s.ended) return;
            s.ended = true;
            if (s.committed) {   // the COMMIT returned: nothing that raced it (abort, eviction, expiry) can make it a failure
                end = State.COMMITTED;
                status = "committed";
                report = s.report;
            }
            s.state = end;
            s.status = status;
            s.report = report;
            byTenant.remove(s.tenant, s);
            outcomes.put(s.id, new Outcome(s.tenant, status, report, List.copyOf(s.notes), now()));
        }
        try {
            exports.get().releaseSlot(s.slot);
        } catch (RuntimeException e) {
            log.warn("event=import_slot_release_failed session={} error={}", s.id, e.getMessage());
        }
        endHeld(s.held, s.tenant);
    }

    private void endHeld(Connection conn, String tenant) {
        if (conn == null) return;
        try {
            conn.rollback();   // a no-op after a commit
        } catch (SQLException | RuntimeException e) {
            log.debug("event=import_held_rollback_failed tenant={} error={}", tenant, e.getMessage());
        }
        if (dataSource instanceof com.zaxxer.hikari.HikariDataSource pool) {
            try {
                pool.evictConnection(conn);   // never returned: temp tables and session settings would outlive the session
            } catch (RuntimeException e) {
                log.debug("event=import_held_evict_failed tenant={} error={}", tenant, e.getMessage());
            }
        }
        try {
            conn.close();   // after an evict Hikari's proxy has no delegate left and may throw: the connection is gone either way
        } catch (SQLException | RuntimeException e) {
            log.debug("event=import_held_close_failed tenant={} error={}", tenant, e.getMessage());
        }
    }

    // ── bounds ──────────────────────────────────────────────────────────────────────────────────

    /** A session past a bound, with the reason it is closed for. */
    private record Due(Session session, String reason) {
    }

    /** Sessions past a bound that can be ended now; a load past its bound is cancelled instead. */
    private List<Due> expireDue() {
        Instant now = now();
        Duration idle = exports.get().config().idleBound();
        Duration total = exports.get().config().totalBound();
        List<Due> due = new ArrayList<>();
        List<Session> overLoad = new ArrayList<>();
        synchronized (lock) {
            for (Session s : byTenant.values()) {
                if (s.state == State.LOADING) {
                    if (s.cancelReason == null && !now.isBefore(s.loadStartedAt.plus(config.loadMax()))) overLoad.add(s);
                    continue;
                }
                if (s.state != State.STAGING) continue;
                boolean idleOver = !now.isBefore(s.lastActive.plus(idle)) && s.gate.availablePermits() > 0;
                boolean totalOver = s.firstCommitAt == null && !now.isBefore(s.openedAt.plus(total));
                boolean waitOver = s.firstCommitAt != null && !now.isBefore(s.firstCommitAt.plus(config.loadMax()));
                if (totalOver) {
                    due.add(new Due(s, "total bound: the upload passed " + total.toMinutes() + " minutes"));
                } else if (waitOver) {
                    due.add(new Due(s, "restore-wait bound: another restore held the load for " + config.loadMax().toMinutes() + " minutes"));
                } else if (idleOver) {
                    due.add(new Due(s, "idle bound: no part for " + idle.toMinutes() + " minutes"));
                }
            }
            outcomes.values().removeIf(o -> now.isAfter(o.endedAt().plus(config.outcomeRetention())));
        }
        for (Session s : overLoad) {
            s.cancelReason = "the load passed " + ENV_LOAD_MAX + " (" + config.loadMax().toMinutes() + " minutes)";
            cancelBackend(s);
        }
        return due;
    }

    /** Ends a session past a bound for the reason {@code expireDue} found; false when a request holds it (the next sweep tries again). */
    private boolean endExpired(Due d) {
        Session s = d.session();
        if (!s.gate.tryAcquire()) return false;
        try {
            log.info("event=import_session_closed session={} tenant={} reason={}", s.id, s.tenant, d.reason());
            finish(s, State.FAILED, "failed: " + d.reason(), null);
            return true;
        } finally {
            s.gate.release();
        }
    }

    /** Applies every bound now; returns how many sessions it ended. The background sweep calls this. */
    public int sweep() {
        int ended = 0;
        for (Due d : expireDue()) {
            if (endExpired(d)) ended++;
        }
        return ended;
    }

    private void startSweeperLocked() {
        if (sweeper != null || closed) return;
        long ms = Math.max(50, config.sweepInterval().toMillis());
        sweeper = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "nx-import-expiry");
            t.setDaemon(true);
            return t;
        });
        sweeper.scheduleWithFixedDelay(() -> {
            try {
                sweep();
            } catch (RuntimeException e) {
                log.error("event=import_session_sweep_failed", e);
            }
        }, ms, ms, TimeUnit.MILLISECONDS);
    }

    /** Ends every session and stops the sweep and the loads; the engine's stop calls it. */
    @Override
    public void close() {
        List<Session> all;
        ScheduledExecutorService toStop;
        synchronized (lock) {
            closed = true;
            all = new ArrayList<>(byTenant.values());
            toStop = sweeper;
            sweeper = null;
        }
        if (toStop != null) toStop.shutdownNow();
        for (Session s : all) {
            s.cancelReason = "engine stop";
            cancelBackend(s);
            finish(s, State.FAILED, "failed: engine stop", null);
        }
        loader.shutdownNow();
    }
}
