// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.db.BackendReaper;
import dev.nexus.service.db.DmlUtilsInstaller;
import dev.nexus.service.log.SecretMask;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
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
 * table with a tenant column and row-level security on has a run per tenant ({@code dml_utils_owner}'s boundary scan is
 * bound by the policy even where the admin session, the table's owner, is not). A table with no tenant column or with row
 * security off has one run, labelled by the move: a tenant setting would change nothing, and a run per tenant would
 * process every row once per tenant. A
 * driving table list-partitioned by model and then by tenant ({@code nexus.chunks}) is never run partition-wise: it is run
 * once per leaf that holds the tenant's rows, with the leaf as a plain driving table and the run named
 * {@code <label>/<tenant>/<leaf>} ({@link LeafCatalog}); no tenant's session ever names another tenant's leaf. A run left
 * incomplete (an engine stop, a lost connection) resumes at the next pass: the same label is CALLed again and dml_utils
 * continues at the first unprocessed chunk.
 *
 * <p>The move's row is read again at the start of its turn and before each CALL, so a pause, a superseding row, a failure
 * or a completion set meanwhile takes effect there; a CALL already in flight runs to its end.
 *
 * <p>Every way a pass can stop goes through {@link FailureHandler} and what follows the runs through {@link MoveFinisher};
 * those are the failure-class and verification beads' to extend. A move that cannot proceed is reported {@code blocked:}
 * with the reason and the worker goes on to the next open move, coming back after {@link Settings#blockedRetry()}.
 *
 * <p><b>Never takes the engine down.</b> {@link #start} runs a daemon thread whose loop catches everything but a
 * {@link VirtualMachineError}, and a surprise in one move's pass is that move's failure and leaves the others to run.
 * {@code Main} starts the worker inside its own guard. The registry is read on the admin connection only:
 * {@code nexus_svc} holds no grant on {@code nexus.data_moves}, and nothing here borrows a serving connection.
 *
 * <p><b>One worker per database.</b> {@code EngineSingletonLock} keeps a second engine out, so when this worker starts, a
 * backend named {@code nexus-data-move-<other nonce>} is an orphan of a hard-killed engine (a coordinator mid-CALL does
 * not notice its client died) and is ended.
 */
public final class DataMoveWorker implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(DataMoveWorker.class);

    /** The bounds the registry's CHECK puts on {@code sleep_ms}; {@code NX_DATA_MOVE_SLEEP_MS} is clamped to them. */
    public static final int MIN_SLEEP_MS = 0;
    public static final int MAX_SLEEP_MS = 60_000;

    /** The engine setting that overrides every move's {@code sleep_ms} for this environment. */
    public static final String SLEEP_ENV = "NX_DATA_MOVE_SLEEP_MS";

    /** Every backend this worker opens is named with this prefix, then the engine's boot nonce. */
    static final String APPLICATION_NAME_PREFIX = "nexus-data-move-";

    /**
     * @param pollInterval how long the worker idles between passes over the open moves
     * @param blockedRetry the least a blocked move waits before its next pass; the row's backoff ({@link MoveRetryPolicy}) is
     *                     applied as well, and is longer from the first retry
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
    /** Whether this engine holds the one-engine-per-database lock; orphans are ended only when it does. */
    private final boolean singletonLockHeld;
    private volatile boolean stopped;
    /** Test seam: runs between opening the call connection and publishing it, where a stop can slip through. */
    Runnable afterConnectionOpenedHook;
    /** Test seam: runs before a move's admin session is opened; it may throw what the open throws (a {@code Refused}). */
    Runnable beforeSessionOpenHook;
    /** Test seam: runs after a CALL has returned and the call connection is clean, before the run's row is read again. */
    Runnable afterCallHook;
    /**
     * Runs when a move is recorded complete ({@code completed_at}), on the worker's thread: the completion check re-reads then
     * (RDR-229 P3.1, {@link CompletionBootWork#onMoveCompleted}). It must not block; a throw is logged and goes no further.
     */
    private volatile Runnable onMoveCompleted = () -> { };
    private volatile Thread loop;
    private volatile CallConnection callConnection;
    /** The one admin session the registry is read on, kept between cycles; guarded by {@code registryLock}. */
    private AdminDdlSession registrySession;
    private final Object registryLock = new Object();
    /** The last registry read failure that was logged, so one that repeats every cycle is logged once. */
    private String lastRegistryError;
    /** Set once the skipped orphan reaping has been logged. */
    private boolean orphansSkipLogged;

    DataMoveWorker(AdminConnection admin, DmlUtilsInstaller installer, Settings settings, String applicationName,
                   Clock clock, MoveStates states, FailureHandler failures, MoveFinisher finisher,
                   boolean singletonLockHeld) {
        this.singletonLockHeld = singletonLockHeld;
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
     * pass-and-stop failure handling the failure-class bead replaces, and the verifying finisher.
     *
     * @param engineApplicationName the engine pool's per-boot application name ({@code BackendReaper.newApplicationName});
     *                              its nonce names this worker's connections
     * @param singletonLockHeld     whether the engine holds {@code EngineSingletonLock} (it does not when the operator opted
     *                              out or a pooler is configured); without it a backend of another nonce may be a LIVE
     *                              engine's, so none is ended
     */
    public static DataMoveWorker create(AdminConnection admin, DmlUtilsInstaller installer, Settings settings,
                                        String engineApplicationName, boolean singletonLockHeld) {
        String nonce = BackendReaper.bootNonce(engineApplicationName);
        String name = APPLICATION_NAME_PREFIX + nonce;
        SecretMask.add(admin.password());   // the credential this worker connects with stays out of what it reports (Main has added it)
        return new DataMoveWorker(admin, installer, settings, name, Clock.systemUTC(),
            new MoveStates(Clock.systemUTC()), new DataMoveFailureHandler(admin, name, Clock.systemUTC()),
            new VerifyingFinisher(), singletonLockHeld);
    }

    /** Sets what runs when a move is recorded complete; see the field. A null clears it. */
    public void onMoveCompleted(Runnable listener) {
        this.onMoveCompleted = listener == null ? () -> { } : listener;
    }

    /** The worker's in-memory view of its moves. */
    public MoveStates states() {
        return states;
    }

    /** The name of this worker's connections: the prefix a sibling that reads for it ({@link MoveBootWork}) names its own with. */
    public String applicationName() {
        return applicationName;
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
     * Stops the worker: no further pass starts and no CALL starts after the flag is set, and a CALL in flight is cancelled at
     * the server (the run resumes at the next boot, from the first unprocessed chunk). The cancel is bounded by 2 s
     * ({@code cancelSignalTimeout}); the thread ends once the CALL has raised.
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
        finisher.cancelInFlight();   // a completion statement (a validation with no statement bound) ends too; the next boot redoes it
    }

    /** Waits for the worker thread to end after {@link #stop}; true when it has (or was never started). */
    public boolean awaitStopped(Duration timeout) throws InterruptedException {
        Thread t = loop;
        if (t == null) {
            return true;
        }
        t.join(timeout.toMillis());
        return !t.isAlive();
    }

    @Override
    public void close() {
        stop();
        closeConnection();
        closeRegistry();
    }

    private void run() {
        try {
            try {
                terminateOrphans();
            } catch (RuntimeException e) {
                log.warn("event=data_move_orphans_not_ended cause=\"{}\"", FailureHandler.oneLine(e.toString()));
            }
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
            closeRegistry();
            log.info("event=data_move_worker_stopped");
        }
    }

    /**
     * Ends every backend a data-move worker of another boot left: its application name begins with
     * {@value #APPLICATION_NAME_PREFIX} and does not begin with this worker's own. Safe because the engine singleton lock
     * allows one engine per database, so such a backend belongs to an engine that is gone; a coordinator mid-CALL does not
     * notice that its client died, and would run beside the CALL the next boot starts for the same label.
     *
     * @return how many backends were signalled
     */
    int terminateOrphans() {
        if (!singletonLockHeld) {
            if (!orphansSkipLogged) {
                orphansSkipLogged = true;
                log.warn("event=data_move_orphans_skipped reason=\"the engine holds no singleton lock, so a backend of "
                    + "another boot may be a live engine's\"");
            }
            return 0;
        }
        try (AdminDdlSession session = openSession("nexus", "data_moves")) {
            var pid = DSL.field(DSL.name("pid"), Integer.class);
            var name = DSL.field(DSL.name("application_name"), String.class);
            var rows = session.ctx().select(pid, name,
                    DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, pid))
                .from(DSL.table(DSL.name("pg_catalog", "pg_stat_activity")))
                .where(DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, name,
                    DSL.val(APPLICATION_NAME_PREFIX))))
                .and(DataMoveOrphans.ours())
                .and(DSL.not(DSL.condition(DSL.function("starts_with", SQLDataType.BOOLEAN, name,
                    DSL.val(applicationName)))))
                .and(pid.ne(DSL.function("pg_backend_pid", SQLDataType.INTEGER)))
                .fetch();
            for (var r : rows) {
                log.warn("event=data_move_orphan_ended pid={} application_name=\"{}\"", r.value1(), r.value2());
            }
            return rows.size();
        }
    }

    /**
     * One pass over the open moves, in order. A paused move is reported and skipped; a blocked move waits out its retry; a
     * move whose runs are all complete, or that failed, is left alone. Package-visible for tests, which drive passes
     * themselves.
     *
     * @return how many moves were worked
     */
    int runCycle() {
        List<DataMove> open;
        List<String> superseded;
        try {
            open = onRegistry(DataMoveRegistry::openMoves);
            superseded = onRegistry(DataMoveRegistry::supersededLabels);
        } catch (RuntimeException e) {
            String cause = FailureHandler.oneLine(e.toString());
            if (!cause.equals(lastRegistryError)) {
                log.warn("event=data_move_registry_unreadable cause=\"{}\"", cause);
                lastRegistryError = cause;
            }
            return 0;
        }
        lastRegistryError = null;
        Set<String> labels = new HashSet<>();
        for (DataMove move : open) {
            labels.add(move.label());
        }
        states.retainOnly(labels, superseded);
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

    /** Reads the registry on the kept session; a session that has gone bad is replaced once before the read is given up. */
    private <T> T onRegistry(Function<DSLContext, T> read) {
        RuntimeException first = null;
        for (int attempt = 0; attempt < 2; attempt++) {
            try {
                return read.apply(registry().ctx());
            } catch (RuntimeException e) {
                if (first == null) {
                    first = e;
                }
                closeRegistry();
            }
        }
        throw first;
    }

    private AdminDdlSession registry() {
        synchronized (registryLock) {
            if (registrySession == null) {
                registrySession = openSession("nexus", "data_moves");
            }
            return registrySession;
        }
    }

    private void closeRegistry() {
        AdminDdlSession s;
        synchronized (registryLock) {
            s = registrySession;
            registrySession = null;
        }
        if (s != null) {
            s.close();
        }
    }

    private boolean due(DataMove move) {
        Optional<MoveStates.State> state = states.state(move.label());
        if (state.isEmpty()) {
            return backedOff(move);   // after a restart the row's own stamp is all there is
        }
        return switch (state.get().kind()) {
            case RUNS_COMPLETE, FAILED -> false;
            case BLOCKED -> backedOff(move) && !clock.instant().isBefore(state.get().updatedAt().plus(settings.blockedRetry()));
            case WORKING, PAUSED -> backedOff(move);
        };
    }

    /** The row's backoff (nexus-f3yqd.2.15) has passed; a move with no retry on its row has none to wait out. */
    private boolean backedOff(DataMove move) {
        return MoveRetryPolicy.due(move.lastTransientAt() == null ? null : move.lastTransientAt().toInstant(),
            move.transientRetries(), clock.instant());
    }

    private void workMove(DataMove listed) {
        DataMove move = listed;
        String schema = listed.drivingSchema();
        String table = listed.drivingTable();
        AdminDdlSession opened;
        try {
            Runnable before = beforeSessionOpenHook;
            if (before != null) {
                before.run();
            }
            opened = openSession(schema, table);
        } catch (RuntimeException e) {
            // a session that cannot be opened is the connection's fault, never the move's: transient, and the moves behind this
            // one still run. (A Refused has dropped the driver's SQLException, so the pass-level catch below could not place it.)
            fail(move, null, FailureHandler.Stage.CONNECTION, null, schema, table, null, 0,
                DataMoveFailureClassifier.sqlExceptionOf(e),
                "the admin connection could not be opened: " + FailureHandler.oneLine(e.getMessage()));
            return;
        }
        AdminDdlSession session = opened;
        try {
            DSLContext ctx = session.ctx();
            // the row as it is now: another move may have run for hours since the list was read
            Optional<DataMove> current = DataMoveRegistry.reload(ctx, listed.label());
            if (current.isEmpty()) {
                states.forget(listed.label());
                return;
            }
            move = current.get();
            if (move.paused()) {
                states.paused(move);
                return;
            }
            boolean alreadyWorking = states.state(move.label()).filter(s -> s.kind() == MoveStates.Kind.WORKING).isPresent();
            if (!alreadyWorking) {
                MoveEvents.started(move.label());   // a pass that begins on a move that was not working: one transition
            }
            states.working(move);   // a retry that runs for hours is working; the state keeps the time it was first blocked
            Optional<MovePreflight.Miss> blocked = MovePreflight.check(session, move, installer);
            if (blocked.isPresent()) {
                fail(move, ctx, FailureHandler.Stage.PREFLIGHT, null, schema, table, null, 0, blocked.get().raised(),
                    blocked.get().reason());
                return;
            }
            passOver(move, session);
        } catch (RuntimeException e) {
            // one move's surprise is that move's failure; the moves behind it still run (no admin context: the session's state is
            // unknown after a surprise, so the handler writes on a short connection of its own)
            fail(move, null, FailureHandler.Stage.PASS, null, schema, table, null, 0,
                DataMoveFailureClassifier.sqlExceptionOf(e),
                "the pass could not run: " + FailureHandler.oneLine(e.getMessage() == null ? e.toString() : e.getMessage()));
        } finally {
            closeQuietly(session);   // a close that fails after the pass reported is not a second failure of the pass
        }
    }

    private void closeQuietly(AdminDdlSession session) {
        try {
            session.close();
        } catch (RuntimeException e) {
            log.debug("event=data_move_session_close_failed cause=\"{}\"", FailureHandler.oneLine(e.toString()));
        }
    }

    /**
     * Hands one failure to the handler and applies what it decides.
     *
     * @return false, always: the pass ends
     */
    private boolean fail(DataMove move, DSLContext ctx, FailureHandler.Stage stage, String runLabel, String schema,
                         String table, String tenant, long errorMark, SQLException raised, String detail) {
        return report(new FailureHandler.Failure(move, stage, runLabel, schema, table, tenant, errorMark, raised,
            detail, ctx));
    }

    /** {@link #fail} for a failure that is already built (the finisher's). */
    private boolean report(FailureHandler.Failure reported) {
        FailureHandler.Failure failure = reported.masked();   // no credential reaches the handler, a log line or a registry column
        DataMove move = failure.move();
        FailureHandler.Stage stage = failure.stage();
        String detail = failure.detail();
        FailureHandler.Disposition d;
        try {
            d = failures.onFailure(failure);
        } catch (RuntimeException e) {
            // Disposition masks its reason, and oneLine masks the exception text before it cuts it
            d = FailureHandler.Disposition.blocked(detail + " (the failure handler failed: "
                + FailureHandler.oneLine(e.toString()) + ")");
        }
        String sqlstate = d.sqlstate() != null ? d.sqlstate() : failure.raised() == null ? null : failure.raised().getSQLState();
        if (d.kind() == FailureHandler.Kind.FAILED) {
            MoveEvents.failed(move.label(), stage.name(), failure.tenant(), sqlstate, d.reason());
            states.failed(move, d.reason());
        } else {
            // a handler that counted no retry leaves the row's count as it was
            int retries = d.retries() >= 0 ? d.retries() : move.transientRetries();
            MoveEvents.blocked(move.label(), stage.name(), failure.tenant(), retries, sqlstate, d.reason());
            states.blocked(move, d.reason());
        }
        return false;
    }

    /** Step 2: the runs, then what follows them. */
    private void passOver(DataMove move, AdminDdlSession session) {
        DSLContext ctx = session.ctx();
        String schema = move.drivingSchema();
        String table = move.drivingTable();
        boolean tenantScoped;
        boolean leafMode = false;
        List<String> tenants = List.of();
        try {
            // a tenant setting changes what a run reads wherever row-level security is on, even for an owner it does not bind:
            // dml_utils_owner's boundary scan is bound by it. Where it is off, one run reads every row, and a run per tenant
            // would read them all N times
            tenantScoped = MoveTenants.hasTenantColumn(ctx, schema, table) && MovePreflight.rowSecurity(ctx, move).enabled();
            if (tenantScoped) {
                leafMode = LeafCatalog.hasTenantLeaves(ctx, schema, table);
                if (leafMode) {
                    archiveVanished(ctx, move);
                    tenants = MoveTenants.byLeafBounds(ctx, LeafCatalog.tenantLeaves(ctx, schema, table, null).stream()
                        .map(LeafCatalog.TenantLeaf::tenant).toList());
                } else {
                    tenants = MoveTenants.byProbe(session, schema, table);
                }
            }
        } catch (DataAccessException e) {
            fail(move, ctx, FailureHandler.Stage.PROBE, null, schema, table, null, 0,
                DataMoveFailureClassifier.sqlExceptionOf(e),
                "the tenants could not be listed: sqlstate=" + e.sqlState() + " " + FailureHandler.oneLine(e.getMessage()));
            return;
        }
        if (!tenantScoped) {
            if (runOne(move, ctx, move.label(), schema, table, null)) {
                finish(move, tenants, ctx);
            }
            return;
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
        if (stopped) {   // the runs are complete and the next boot finds them so: nothing starts after a stop
            return;
        }
        MoveFinisher.Result result = finisher.finish(new MoveFinisher.Context(move, tenants, ctx, admin, applicationName,
            () -> stopped, states));
        switch (result.outcome()) {
            case COMPLETED -> {
                states.forget(move.label());
                notifyCompleted(move);
            }
            case FAILED -> {
                MoveEvents.failed(move.label(), "FINISH", null, null, result.reason());
                states.failed(move, result.reason());
            }
            case BLOCKED -> {
                MoveEvents.blocked(move.label(), "FINISH", null, move.transientRetries(), null, result.reason());
                states.blocked(move, result.reason());
            }
            case LEFT_OPEN -> states.runsComplete(move);
            case REPORT -> report(result.failure());
            case PAUSED -> states.paused(result.move());
            case GONE -> states.forget(move.label());
            // on_complete re-opened the move (completed_at back to NULL): no completion event, no listener, no retry reset; the next pass takes it
            case REOPENED -> states.forget(move.label());
        }
        if (result.outcome() == MoveFinisher.Outcome.COMPLETED || result.outcome() == MoveFinisher.Outcome.LEFT_OPEN) {
            clearRetries(move, ctx);   // a clean pass resets the count; no failure reached the handler to do it
        }
    }

    private void notifyCompleted(DataMove move) {
        try {
            onMoveCompleted.run();
        } catch (RuntimeException e) {   // a listener is the engine's, never the worker's failure
            log.warn("event=data_move_completed_listener_failed label={} cause=\"{}\"", move.label(),
                FailureHandler.oneLine(e.toString()));
        }
    }

    private void clearRetries(DataMove move, DSLContext ctx) {
        if (move.transientRetries() == 0) {
            return;
        }
        try {
            MoveFailureRecords.clearTransient(ctx, move.label());
        } catch (RuntimeException e) {
            log.warn("event=data_move_retries_not_cleared label={} cause=\"{}\"", move.label(),
                FailureHandler.oneLine(e.toString()));
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
     * One run: the move's row read again, held error mark, session tenant, CALL, RESET, then the active row read again.
     * Skips a run dml_utils already completed.
     *
     * @return true when the run is complete and the pass goes on; false when the pass ends (the state is set)
     */
    private boolean runOne(DataMove listed, DSLContext ctx, String runLabel, String drivingSchema, String drivingTable,
                           String tenant) {
        DataMoveRuns.ActiveRun existing = DataMoveRuns.activeRun(ctx, runLabel);
        if (existing != null && existing.completedAt() != null) {
            return true;
        }
        // a pause, a superseding row, a failure or a completion set since the pass began: no further CALL starts
        Optional<DataMove> current = DataMoveRegistry.reload(ctx, listed.label());
        if (current.isEmpty()) {
            states.forget(listed.label());
            return false;
        }
        DataMove move = current.get();
        if (move.paused()) {
            states.paused(move);
            return false;
        }
        if (existing != null && existing.cancelledAt() != null) {
            return fail(move, ctx, FailureHandler.Stage.CANCELLED, runLabel, drivingSchema, drivingTable, tenant, 0, null,
                "run " + runLabel + " cancelled at " + existing.cancelledAt());
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
            if (stopped) {
                return stoppedPass(move, "the worker is stopped");
            }
            return fail(move, ctx, FailureHandler.Stage.CONNECTION, runLabel, drivingSchema, drivingTable, tenant,
                errorMark, e, "the call connection could not be opened: sqlstate=" + e.getSQLState() + " "
                    + FailureHandler.oneLine(e.getMessage()));
        }
        if (stopped) {   // immediately before the CALL: none starts after a stop
            conn.cancel();
            return stoppedPass(move, "the worker is stopped");
        }
        try {
            if (tenant != null) {
                conn.useTenant(tenant);
            }
            log.info("event=data_move_call label={} run={} pid={}", move.label(), runLabel, conn.backendPid());
            states.callStarted(move, runLabel, conn.backendPid());
            conn.call(move.template(), drivingSchema, drivingTable, runLabel, move.chunkSize(), sleepMs);
        } catch (SQLException e) {
            if (stopped) {   // stop() cancelled the CALL at the server (57014): the engine's stop, not a failure to count
                return stoppedPass(move, "run " + runLabel + " was cancelled by the worker's stop");
            }
            return fail(move, ctx, FailureHandler.Stage.CALL, runLabel, drivingSchema, drivingTable, tenant, errorMark, e,
                "run " + runLabel + " raised sqlstate=" + e.getSQLState() + ": " + FailureHandler.oneLine(e.getMessage()));
        } finally {
            states.callEnded(move);
            endSession(conn);
        }
        Runnable afterCall = afterCallHook;
        if (afterCall != null) {
            afterCall.run();
        }
        DataMoveRuns.ActiveRun after = DataMoveRuns.activeRun(ctx, runLabel);
        if (after != null && after.cancelledAt() != null) {
            return fail(move, ctx, FailureHandler.Stage.CANCELLED, runLabel, drivingSchema, drivingTable, tenant, errorMark,
                null, "run " + runLabel + " cancelled at " + after.cancelledAt());
        }
        if (after == null || after.completedAt() == null) {
            return fail(move, ctx, FailureHandler.Stage.INCOMPLETE, runLabel, drivingSchema, drivingTable, tenant,
                errorMark, null, "run " + runLabel + " returned without completing");
        }
        MoveEvents.runComplete(move.label(), runLabel, tenant);
        return true;
    }

    /**
     * The worker was stopped: the pass ends and the move is reported with the reason, but no failure reaches the handler, so
     * the stop counts no retry and sets no backoff for the next boot.
     *
     * @return false, always: the pass ends
     */
    private boolean stoppedPass(DataMove move, String reason) {
        log.info("event=data_move_pass_stopped label={} reason=\"{}\"", move.label(), reason);
        states.blocked(move, reason);
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
        Runnable hook = afterConnectionOpenedHook;
        if (hook != null) {
            hook.run();
        }
        callConnection = c;
        // a stop() between the check above and the line above saw no connection to cancel: look again now it is published
        if (stopped) {
            c.cancel();
            closeConnection();
            throw new SQLException("the worker is stopped", "57014");
        }
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
