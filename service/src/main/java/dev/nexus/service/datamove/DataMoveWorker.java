// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.DmlUtilsInstaller;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
import java.util.HashSet;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.function.Function;

/**
 * nexus-f3yqd.2.13 (RDR-246 Step 4, The move worker steps 1 and 2): the engine thread that works the open moves of
 * {@code nexus.data_moves}, oldest {@code registered_at} first, one move at a time.
 *
 * <p>Each pass over a move: grants and checks ({@link MovePreflight}), then, for each tenant on a dedicated autocommit
 * connection ({@link CallConnection}), a session-level {@code SET nexus.tenant}, a CALL of dml_utils'
 * {@code run_migration_chunks_committing} (threads 1, the move's chunk size and {@code sleep_ms}) and a {@code RESET}. A
 * table with no tenant column has one run, labelled by the move. A driving table list-partitioned by model and then by
 * tenant ({@code nexus.chunks}) is never run partition-wise: it is run once per leaf that holds the tenant's rows, with
 * the leaf as a plain driving table and the run named {@code <label>/<tenant>/<leaf>} ({@link LeafCatalog}); no tenant's
 * session ever names another tenant's leaf. A run left incomplete (an engine stop, a lost connection) resumes at the
 * next pass: the same label is CALLed again and dml_utils continues at the first unprocessed chunk.
 *
 * <p>A pass that cannot proceed reports the move {@code blocked:} with the reason ({@link MoveReporter}) and the worker
 * goes on to the next open move, coming back after {@link Settings#blockedRetry()}. Failures of a CALL go through
 * {@link FailureHandler} and what follows the runs through {@link MoveFinisher}; those are the failure-class and
 * verification beads' to extend.
 *
 * <p><b>Never takes the engine down.</b> {@link #start} runs a daemon thread whose loop catches everything but a
 * {@link VirtualMachineError}, and {@code Main} starts it inside its own guard. The registry is read on the admin
 * connection only: {@code nexus_svc} holds no grant on {@code nexus.data_moves}, and nothing here borrows a serving
 * connection.
 */
public final class DataMoveWorker implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(DataMoveWorker.class);

    /** The bounds the registry's CHECK puts on {@code sleep_ms}; {@code NX_DATA_MOVE_SLEEP_MS} is clamped to them. */
    public static final int MIN_SLEEP_MS = 0;
    public static final int MAX_SLEEP_MS = 60_000;

    /** The engine setting that overrides every move's {@code sleep_ms} for this environment. */
    public static final String SLEEP_ENV = "NX_DATA_MOVE_SLEEP_MS";

    /**
     * @param pollInterval how long the worker idles between passes over the open moves
     * @param blockedRetry how long a blocked move waits before its next pass (the failure-class bead replaces this with the
     *                     row's backoff)
     * @param sleepOverrideMs {@code NX_DATA_MOVE_SLEEP_MS} clamped to the registry's bounds, or null when unset
     */
    public record Settings(Duration pollInterval, Duration blockedRetry, Integer sleepOverrideMs) {

        public Settings {
            Objects.requireNonNull(pollInterval, "pollInterval");
            Objects.requireNonNull(blockedRetry, "blockedRetry");
            if (sleepOverrideMs != null) {
                sleepOverrideMs = clampSleep(sleepOverrideMs);
            }
        }

        public static Settings of(Integer sleepOverrideMs) {
            return new Settings(Duration.ofSeconds(10), Duration.ofSeconds(30), sleepOverrideMs);
        }

        /** The settings an engine boots with: the override from {@link #SLEEP_ENV}, clamped; a value that is not a number is ignored. */
        public static Settings fromEnv(Function<String, String> env) {
            String raw = env.apply(SLEEP_ENV);
            if (raw == null || raw.isBlank()) {
                return of(null);
            }
            try {
                long parsed = Long.parseLong(raw.strip());
                int clamped = clampSleep(parsed);
                if (clamped != parsed) {
                    log.warn("event=data_move_sleep_override_clamped requested={} applied={}", parsed, clamped);
                }
                return of(clamped);
            } catch (NumberFormatException e) {
                log.warn("event=data_move_sleep_override_ignored value=\"{}\" reason=\"not an integer\"", raw);
                return of(null);
            }
        }

        static int clampSleep(long ms) {
            return (int) Math.max(MIN_SLEEP_MS, Math.min(MAX_SLEEP_MS, ms));
        }
    }

    private final AdminConnection admin;
    private final DmlUtilsInstaller installer;
    private final Settings settings;
    private final String applicationName;
    private final Clock clock;
    private final MoveStates states;
    private final FailureHandler failures;
    private final MoveFinisher finisher;
    private volatile boolean stopped;
    private volatile Thread loop;
    private volatile CallConnection callConnection;

    DataMoveWorker(AdminConnection admin, DmlUtilsInstaller installer, Settings settings, String applicationName,
                   Clock clock, MoveStates states, FailureHandler failures, MoveFinisher finisher) {
        this.admin = Objects.requireNonNull(admin, "admin");
        this.installer = Objects.requireNonNull(installer, "installer");
        this.settings = Objects.requireNonNull(settings, "settings");
        this.applicationName = Objects.requireNonNull(applicationName, "applicationName");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.states = Objects.requireNonNull(states, "states");
        this.failures = Objects.requireNonNull(failures, "failures");
        this.finisher = Objects.requireNonNull(finisher, "finisher");
    }

    /**
     * The worker an engine boots with: system clock, the in-memory {@link MoveStates} the doctor line reads, and the
     * pass-and-stop failure handling and verification the later beads replace.
     *
     * @param engineApplicationName the engine pool's per-boot application name ({@code BackendReaper.newApplicationName});
     *                              its nonce names this worker's connections
     */
    public static DataMoveWorker create(AdminConnection admin, DmlUtilsInstaller installer, Settings settings,
                                        String engineApplicationName) {
        String nonce = BackendReaper.bootNonce(engineApplicationName);
        return new DataMoveWorker(admin, installer, settings, "nexus-data-move-" + nonce, Clock.systemUTC(),
            new MoveStates(Clock.systemUTC()), FailureHandler.BLOCK_ALWAYS, MoveFinisher.RUNS_ONLY);
    }

    /** The worker's in-memory view of its open moves. */
    public MoveStates states() {
        return states;
    }

    /** Starts the worker thread. A second call does nothing. */
    public synchronized void start() {
        if (loop != null || stopped) {
            return;
        }
        Thread t = new Thread(this::run, "data-move-worker");
        t.setDaemon(true);
        loop = t;
        t.start();
        log.info("event=data_move_worker_started");
    }

    /**
     * Stops the worker: no further pass starts, and a CALL in flight is cancelled at the server (the run resumes at the next
     * boot, from the first unprocessed chunk). Returns at once.
     */
    public void stop() {
        stopped = true;
        Thread t = loop;
        if (t != null) {
            t.interrupt();
        }
        CallConnection c = callConnection;
        if (c != null) {
            c.cancel();
        }
    }

    @Override
    public void close() {
        stop();
        closeConnection();
    }

    private void run() {
        try {
            while (!stopped) {
                try {
                    runCycle();
                } catch (VirtualMachineError e) {
                    throw e;
                } catch (Throwable t) {
                    log.error("event=data_move_cycle_failed cause=\"{}\"", FailureHandler.oneLine(t.toString()), t);
                }
                try {
                    Thread.sleep(settings.pollInterval().toMillis());
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        } finally {
            closeConnection();
            log.info("event=data_move_worker_stopped");
        }
    }

    /**
     * One pass over the open moves, in order. A paused move is reported and skipped; a blocked move waits out its retry; a
     * move whose runs are all complete is left alone. Package-visible for tests, which drive passes themselves.
     *
     * @return how many moves were worked
     */
    int runCycle() {
        List<DataMove> open;
        try (AdminDdlSession registry = openSession("nexus", "data_moves")) {
            open = DataMoveRegistry.openMoves(registry.ctx());
        } catch (RuntimeException e) {
            log.warn("event=data_move_registry_unreadable cause=\"{}\"", FailureHandler.oneLine(e.toString()));
            return 0;
        }
        Set<String> labels = new HashSet<>();
        for (DataMove move : open) {
            labels.add(move.label());
        }
        states.retainOnly(labels);
        int worked = 0;
        for (DataMove move : open) {
            if (stopped) {
                break;
            }
            if (move.paused()) {
                states.paused(move);
                continue;
            }
            if (!due(move)) {
                continue;
            }
            workMove(move);
            worked++;
        }
        return worked;
    }

    private boolean due(DataMove move) {
        Optional<MoveStates.State> state = states.state(move.label());
        if (state.isEmpty()) {
            return true;
        }
        return switch (state.get().kind()) {
            case RUNS_COMPLETE -> false;
            case BLOCKED -> !clock.instant().isBefore(state.get().updatedAt().plus(settings.blockedRetry()));
            case WORKING, PAUSED -> true;
        };
    }

    private void workMove(DataMove move) {
        states.working(move);
        try (AdminDdlSession session = openSession(move.drivingSchema(), move.drivingTable())) {
            Optional<String> blocked = MovePreflight.blockedReason(session, move, installer);
            if (blocked.isPresent()) {
                blocked(move, blocked.get());
                return;
            }
            passOver(move, session.ctx());
        } catch (AdminDdlSession.Refused | org.jooq.exception.DataAccessException | IllegalStateException e) {
            blocked(move, "the pass could not run: " + FailureHandler.oneLine(e.getMessage()));
        }
    }

    private void blocked(DataMove move, String reason) {
        log.warn("event=data_move_blocked label={} reason=\"{}\"", move.label(), reason);
        states.blocked(move, reason);
    }

    /** Step 2: the runs, then what follows them. */
    private void passOver(DataMove move, DSLContext ctx) {
        String schema = move.drivingSchema();
        String table = move.drivingTable();
        boolean tenantScoped = MoveTenants.hasTenantColumn(ctx, schema, table);
        if (!tenantScoped) {
            if (runOne(move, ctx, move.label(), schema, table, null)) {
                finish(move, List.of(), ctx);
            }
            return;
        }
        List<String> tenants = MoveTenants.tenants(ctx, schema, table);
        boolean leafMode = LeafCatalog.hasTenantLeaves(ctx, schema, table);
        if (leafMode) {
            archiveVanished(ctx, move);
        }
        for (String tenant : tenants) {
            if (stopped) {
                return;
            }
            if (leafMode) {
                // read once per tenant, at the start of that tenant's runs: from the catalog, the way drop_tenant_partitions does
                for (LeafCatalog.TenantLeaf tl : LeafCatalog.tenantLeaves(ctx, schema, table, tenant)) {
                    if (stopped || !runOne(move, ctx, leafRunLabel(move, tenant, tl.leaf()), tl.leaf().schema(),
                            tl.leaf().name(), tenant)) {
                        return;
                    }
                }
            } else if (!runOne(move, ctx, move.label() + "/" + tenant, schema, table, tenant)) {
                return;
            }
        }
        finish(move, tenants, ctx);
    }

    private void finish(DataMove move, List<String> tenants, DSLContext ctx) {
        MoveFinisher.Result result = finisher.finish(move, tenants, ctx);
        if (result.blockedReason() != null) {
            blocked(move, result.blockedReason());
        } else if (!result.closed()) {
            states.runsComplete(move);
        }
    }

    /** {@code <label>/<tenant>/<leaf>}; the leaf is {@code schema.name} only when it is outside the driving table's schema. */
    private static String leafRunLabel(DataMove move, String tenant, LeafCatalog.Leaf leaf) {
        String leafName = leaf.schema().equals(move.drivingSchema()) ? leaf.name() : leaf.schema() + "." + leaf.name();
        return move.label() + "/" + tenant + "/" + leafName;
    }

    /**
     * A leaf dropped mid-move (a tenant removal): its active run would stay in the progress sum with a total that never
     * completes. Archive the active run of every leaf the catalog no longer has, so it leaves the sum and does not fail
     * the move. The comparison is by whole run name, so no tenant or leaf name is parsed out of a label.
     */
    private void archiveVanished(DSLContext ctx, DataMove move) {
        Set<String> existing = new HashSet<>();
        for (LeafCatalog.TenantLeaf tl : LeafCatalog.tenantLeaves(ctx, move.drivingSchema(), move.drivingTable(), null)) {
            existing.add(leafRunLabel(move, tl.tenant(), tl.leaf()));
        }
        String prefix = move.label() + "/";
        for (String run : DataMoveRuns.activeRunLabels(ctx, move.label())) {
            if (run.startsWith(prefix) && !existing.contains(run)) {
                DataMoveRuns.archive(ctx, run);
                log.info("event=data_move_run_archived label={} run={} reason=\"leaf no longer exists\"", move.label(), run);
            }
        }
    }

    /**
     * One run: held error mark, session tenant, CALL, RESET, then the active row read again. Skips a run dml_utils already
     * completed.
     *
     * @return true when the run is complete and the pass goes on; false when the move was reported and the pass ends
     */
    private boolean runOne(DataMove move, DSLContext ctx, String runLabel, String drivingSchema, String drivingTable,
                           String tenant) {
        DataMoveRuns.ActiveRun existing = DataMoveRuns.activeRun(ctx, runLabel);
        if (existing != null && existing.completedAt() != null) {
            return true;
        }
        if (existing != null && existing.cancelledAt() != null) {
            return report(move, failures.onRunCancelled(move, runLabel, existing.cancelledAt()));
        }
        int sleepMs = settings.sleepOverrideMs() != null ? settings.sleepOverrideMs() : move.sleepMs();
        if (existing != null && existing.sleepMs() != sleepMs) {
            DataMoveRuns.setSleepMs(ctx, runLabel, sleepMs);
        }
        long errorMark = DataMoveRuns.errorMark(ctx, runLabel);

        CallConnection conn;
        try {
            conn = connection();
        } catch (SQLException e) {
            blocked(move, "the call connection could not be opened: sqlstate=" + e.getSQLState() + " "
                + FailureHandler.oneLine(e.getMessage()));
            return false;
        }
        try {
            if (tenant != null) {
                conn.useTenant(tenant);
            }
            log.info("event=data_move_call label={} run={} pid={}", move.label(), runLabel, conn.backendPid());
            conn.call(move.template(), drivingSchema, drivingTable, runLabel, move.chunkSize(), sleepMs);
        } catch (SQLException e) {
            return report(move, failures.onCallRaised(move, runLabel, errorMark, e, ctx));
        } finally {
            endSession(conn);
        }
        DataMoveRuns.ActiveRun after = DataMoveRuns.activeRun(ctx, runLabel);
        if (after != null && after.cancelledAt() != null) {
            return report(move, failures.onRunCancelled(move, runLabel, after.cancelledAt()));
        }
        if (after == null || after.completedAt() == null) {
            blocked(move, "run " + runLabel + " returned without completing");
            return false;
        }
        return true;
    }

    private boolean report(DataMove move, FailureHandler.Disposition disposition) {
        blocked(move, disposition.reason());
        return false;
    }

    /** {@code RESET nexus.tenant}; a connection that cannot be shown clean is closed and never reused. */
    private void endSession(CallConnection conn) {
        try {
            conn.clearTenant();
        } catch (RuntimeException e) {
            log.warn("event=data_move_call_connection_discarded cause=\"{}\"", FailureHandler.oneLine(e.toString()));
            closeConnection();
        }
    }

    private CallConnection connection() throws SQLException {
        CallConnection c = callConnection;
        if (c != null && c.usable()) {
            return c;
        }
        closeConnection();
        if (stopped) {
            throw new SQLException("the worker is stopped", "57014");
        }
        c = CallConnection.open(admin, applicationName + "-call");
        callConnection = c;
        return c;
    }

    private void closeConnection() {
        CallConnection c = callConnection;
        callConnection = null;
        if (c != null) {
            c.close();
        }
    }

    private AdminDdlSession openSession(String schema, String table) {
        return AdminDdlSession.open(admin,
            new AdminDdlSession.Settings(applicationName, 0L, "data move", schema, table, "none"), false);
    }
}
