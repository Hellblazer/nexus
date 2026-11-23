// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.jooq.DSLContext;
import org.jooq.exception.DataAccessException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;

/**
 * nexus-f3yqd.2.17 (RDR-246 Step 4, Progress and Day 2 Operations): says what each data move is doing, in the engine's
 * {@code /v1/status} {@code boot_work}. One entry per open move and per failed move no row supersedes, id
 * {@code data-move:<label>}; {@code nx doctor} reads them from the status and turns each {@code blocked:} and
 * {@code failed:} into a red row.
 *
 * <p>A worker thread sits in a CALL for hours, so the worker cannot be the one that reports. This class samples on its
 * own thread and its own admin session: the move rows and the active dml_utils runs ({@code done} and {@code total} summed
 * over the move's runs, unit {@code chunks}), and the worker's in-memory {@link MoveStates}. Never the serving pool:
 * {@code nexus_svc} holds no grant on {@code nexus.data_moves}.
 *
 * <p><b>What the entry says.</b> {@code /v1/status} is unauthenticated, and {@code boot_work} names no collection, tenant
 * or index. So {@code what} carries the move's label, a fixed class and, where there is one, an SQLSTATE, and nothing a
 * run, a tenant or the server wrote: no run label (they are {@code <label>/<tenant>[/<leaf>]}), leaf name, backend pid or
 * PostgreSQL message. {@link MoveReasons#publicForm} does the reduction; the full text stays in the engine log
 * ({@code event=data_move_blocked}, {@code data_move_failed}, {@code data_move_verification_failed}, {@code data_move_call}; {@link MoveEvents}) and, for a failed move, in the
 * registry's {@code failure} column.
 *
 * <p>The classes, in {@code what}: {@code working:}, {@code blocked:}, {@code failed:}, {@code paused:}, {@code stalled:} (below),
 * {@code runs complete:} and {@code queued:} (an open move the worker has not reached: another move's CALL holds it). A move
 * the worker has not reported, whose row carries a transient failure from an earlier boot, is {@code blocked:}.
 *
 * <p><b>A stalled CALL.</b> The CALL connection has no socket timeout (it is one statement that may run for hours, and a
 * keepalive and a cancel are all it has), so a silent network partition parks the serial worker until the keepalive gives up,
 * about two hours, with no data lost. Nothing in the engine can end it sooner without risking a healthy long chunk, so this
 * makes it visible: while a CALL is in flight and the move's {@code done} has not advanced for the stall bound
 * ({@value #STALL_ENV}, default {@value #DEFAULT_STALL_MINUTES} minutes), the entry says {@code stalled:} with how long, and
 * {@code nx doctor} shows a warning, not red (the CALL may be a slow chunk). The backend's pid is in the engine log
 * ({@code event=data_move_call}); an operator ends a truly dead CALL with {@code pg_cancel_backend(pid)} and the worker
 * retries it as transient. Any advance of {@code done} restarts the timer.
 *
 * <p>{@code done} never falls within a process and never exceeds {@code total} ({@link #shown}): a leaf dropped mid-move
 * leaves both sums.
 *
 * <p>Thread-safety: {@link #publish} calls are serialized; the registry state has its own short lock; {@link #close} takes
 * neither the read nor waits on it (it cancels a read in flight), so a sample hung on a silent connection cannot hold the
 * engine's shutdown.
 */
public final class MoveBootWork implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(MoveBootWork.class);

    /** The prefix of every move's entry id, so a client picks the data moves out of {@code boot_work}. */
    public static final String ID_PREFIX = "data-move:";
    /** The id of the entry that says the move worker is not running. */
    public static final String WORKER_ID = "data-move-worker";
    /** The id of the entry that says the worker is running but its boot_work reporter is not. */
    public static final String REPORTER_ID = "data-move-boot-work";
    /** What stopping the engine does to a move. */
    public static final String ON_STOP = "resumes from the last committed batch";
    public static final String UNIT = "chunks";
    /** The environment setting of the stall bound, in minutes. */
    public static final String STALL_ENV = "NX_DATA_MOVE_STALL_MINUTES";
    /** A CALL with no chunk done for this long is reported {@code stalled:} unless {@link #STALL_ENV} says otherwise. */
    public static final long DEFAULT_STALL_MINUTES = 15;
    public static final long MIN_STALL_MINUTES = 1;
    public static final long MAX_STALL_MINUTES = 1_440;
    public static final Duration DEFAULT_INTERVAL = Duration.ofSeconds(5);

    /** What one sample read. */
    record Snapshot(List<DataMove> open, List<DataMoveRegistry.FailedMove> failed,
                    Map<String, DataMoveRuns.Progress> progress) { }

    /** Where a sample comes from: the registry and the runs. {@link #abort} ends a read in flight; {@link #close} releases it. */
    interface Reader extends AutoCloseable {
        Snapshot read();

        void abort();

        @Override
        void close();
    }

    private final BootWorkRegistry registry;
    private final MoveStates states;
    private final Reader reader;
    private final Clock clock;
    private final Duration stallAfter;

    /** Serializes samples, the read included; {@link #close} never takes it. */
    private final Object sampleLock = new Object();
    /** Guards the state below, held only for map operations, never across a read. */
    private final Object stateLock = new Object();
    private final Set<String> published = new HashSet<>();
    private final Map<String, Long> highestDone = new HashMap<>();
    private final Map<String, Advance> lastAdvance = new HashMap<>();
    private boolean closed;
    private String lastError;
    private ScheduledExecutorService scheduler;

    /** The last value of {@code done} seen for a move and when it first had it. */
    private record Advance(long done, Instant at) { }

    /**
     * @param worker the move worker; its {@link DataMoveWorker#states() states} are read, and this class's admin connection
     *               is named after the worker's so the worker's orphan sweep, which ends every
     *               {@code nexus-data-move-} backend of another boot, ends this one's too
     */
    public MoveBootWork(BootWorkRegistry registry, DataMoveWorker worker, AdminConnection admin) {
        this(registry, worker, admin, System::getenv);
    }

    MoveBootWork(BootWorkRegistry registry, DataMoveWorker worker, AdminConnection admin, Function<String, String> env) {
        this(registry, worker.states(), new DbReader(admin, worker.applicationName() + "-bootwork"), Clock.systemUTC(),
            stallFromEnv(env));
    }

    MoveBootWork(BootWorkRegistry registry, MoveStates states, AdminConnection admin, String applicationName, Clock clock,
                 Duration stallAfter) {
        this(registry, states, new DbReader(admin, applicationName), clock, stallAfter);
    }

    MoveBootWork(BootWorkRegistry registry, MoveStates states, Reader reader, Clock clock, Duration stallAfter) {
        this.registry = Objects.requireNonNull(registry, "registry");
        this.states = Objects.requireNonNull(states, "states");
        this.reader = Objects.requireNonNull(reader, "reader");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.stallAfter = Objects.requireNonNull(stallAfter, "stallAfter");
    }

    /**
     * The stall bound: {@value #STALL_ENV} in minutes, clamped to {@value #MIN_STALL_MINUTES} to {@value #MAX_STALL_MINUTES};
     * {@value #DEFAULT_STALL_MINUTES} when unset or not a whole number.
     */
    static Duration stallFromEnv(Function<String, String> env) {
        String raw = env.apply(STALL_ENV);
        if (raw == null || raw.isBlank()) {
            return Duration.ofMinutes(DEFAULT_STALL_MINUTES);
        }
        try {
            long parsed = Long.parseLong(raw.strip());
            long clamped = Math.max(MIN_STALL_MINUTES, Math.min(MAX_STALL_MINUTES, parsed));
            if (clamped != parsed) {
                log.warn("event=data_move_stall_clamped requested={} applied={}", parsed, clamped);
            }
            return Duration.ofMinutes(clamped);
        } catch (NumberFormatException e) {
            log.warn("event=data_move_stall_ignored value=\"{}\" reason=\"not a whole number of minutes\"", raw);
            return Duration.ofMinutes(DEFAULT_STALL_MINUTES);
        }
    }

    /**
     * Says the move worker is not running, as an entry {@code nx doctor} turns red: without it a doctor run reads "no data
     * move is open" from an engine whose worker never started, and every registered move waits forever.
     */
    public static void reportWorkerNotRunning(BootWorkRegistry registry) {
        registry.register(WORKER_ID, "blocked: the data-move worker is not running, so no data move is worked "
            + "(the engine log has event=data_move_worker_not_started with the cause)", null, null, UNIT, true, ON_STOP);
    }

    /**
     * Says the worker runs but nothing reports its moves (this class could not be built or started): a doctor run would
     * otherwise read "no data move is open or failed" while moves are being worked, or blocked, unseen.
     */
    public static void reportReporterNotRunning(BootWorkRegistry registry) {
        registry.register(REPORTER_ID, "blocked: the data-move boot_work reporter is not running, so no data move is "
            + "reported (the engine log has event=data_move_boot_work_not_started with the cause)", null, null, UNIT, true,
            ON_STOP);
    }

    public Duration stallAfter() {
        return stallAfter;
    }

    /** Samples every {@link #DEFAULT_INTERVAL}, on a daemon thread. A second call does nothing. */
    public synchronized void start() {
        start(DEFAULT_INTERVAL);
    }

    synchronized void start(Duration interval) {
        if (scheduler != null) {
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "data-move-boot-work");
            t.setDaemon(true);
            return t;
        });
        scheduler.scheduleWithFixedDelay(() -> {
            try {
                publish();
            } catch (VirtualMachineError e) {
                throw e;
            } catch (Throwable t) {   // a surprise costs one sample, never the thread
                log.warn("event=data_move_boot_work_failed cause=\"{}\"", FailureHandler.oneLine(t.toString()));
            }
        }, 0, interval.toMillis(), TimeUnit.MILLISECONDS);
    }

    /** Stops sampling, ends a read in flight, and takes the entries off. Does not wait for a sample. */
    @Override
    public void close() {
        ScheduledExecutorService s;
        synchronized (this) {
            s = scheduler;
            scheduler = null;
        }
        if (s != null) {
            s.shutdownNow();
        }
        synchronized (stateLock) {
            closed = true;
            for (String id : published) {
                registry.finish(id);
            }
            published.clear();
            registry.dataMovesNeedOperator(List.of());
        }
        try {
            reader.abort();   // a read hung on a silent connection must not outlive the engine's shutdown
        } finally {
            reader.close();
        }
    }

    /**
     * One sample: reads the registry and the runs, and brings the {@code boot_work} entries to match.
     *
     * @return how many entries there are now, or -1 when the registry could not be read (the entries are left as they were)
     */
    int publish() {
        synchronized (sampleLock) {
            synchronized (stateLock) {
                if (closed) {
                    return -1;   // before the read: a sample racing close() must not open a session close has already run past
                }
            }
            Snapshot snapshot;
            try {
                snapshot = reader.read();
                lastError = null;
            } catch (RuntimeException e) {
                String cause = FailureHandler.oneLine(e.toString());
                if (!cause.equals(lastError)) {
                    log.warn("event=data_move_boot_work_unreadable cause=\"{}\"", cause);
                    lastError = cause;
                }
                return -1;
            }
            synchronized (stateLock) {
                if (closed) {
                    return -1;
                }
                return apply(snapshot);
            }
        }
    }

    private int apply(Snapshot snapshot) {
        Instant now = clock.instant();
        Map<String, Desired> desired = new LinkedHashMap<>();
        for (DataMove move : snapshot.open()) {
            desired.put(move.label(), describeOpen(move, snapshot.progress().get(move.label()), now));
        }
        for (DataMoveRegistry.FailedMove f : snapshot.failed()) {
            desired.putIfAbsent(f.label(), failedEntry(f.label(), f.failure(), snapshot.progress().get(f.label())));
        }
        Set<String> gone = new HashSet<>(published);
        for (var e : desired.entrySet()) {
            String id = ID_PREFIX + e.getKey();
            Desired d = e.getValue();
            if (published.add(id)) {
                registry.register(id, d.what(), d.done(), d.total(), UNIT, true, ON_STOP);
            }
            registry.update(id, d.what(), d.done(), d.total(), d.step(), d.reasonClass());
            gone.remove(id);
        }
        for (String id : gone) {
            registry.finish(id);
            published.remove(id);
        }
        highestDone.keySet().retainAll(desired.keySet());
        lastAdvance.keySet().retainAll(desired.keySet());
        registry.dataMovesNeedOperator(needOperator(snapshot.open()));
        return published.size();
    }

    /**
     * The open moves in the needs-an-operator state, for {@code /v1/status}: the row says it ({@link MoveRetryPolicy#needsOperator(DataMove)},
     * 20 consecutive transients, so a restart brings it back) or the worker's state does (the early rule, latched in the handler
     * and carried in {@link MoveStates.State#needsOperator()}). The list is empty again the sample after the move completes or
     * its retry count is reset. Also announces ({@link #announce}) each move this process has not yet paged.
     */
    private List<BootWorkRegistry.NeedsOperator> needOperator(List<DataMove> open) {
        List<BootWorkRegistry.NeedsOperator> out = new java.util.ArrayList<>();
        for (DataMove m : open) {
            var state = states.state(m.label());
            boolean held = !m.paused() && m.transientRetries() > 0 && m.lastTransientAt() != null
                && state.map(MoveStates.State::needsOperator).orElse(false);
            if (!MoveRetryPolicy.needsOperator(m) && !held) {
                continue;
            }
            Instant last = m.lastTransientAt().toInstant();
            // the last failure this process saw says the step, the bound and whether it ended builds; with none (an engine that
            // has just started) the move's definition says whether an attempt would
            boolean seen = state.map(s -> s.step() != null).orElse(false);
            boolean builds = seen ? state.get().terminatesIndexBuilds() : m.onComplete() != null && ChunkFamily.touches(m);
            out.add(new BootWorkRegistry.NeedsOperator(m.label(), m.transientRetries(), last,
                last.plus(MoveRetryPolicy.backoff(m.transientRetries())), MoveReasons.sqlstateOf(m.lastTransient()),
                m.statementTimeoutMs(), builds, seen ? state.get().step() : null,
                seen ? state.get().boundMs() : FailureHandler.PageInfo.NO_BOUND,
                state.map(s -> s.kind() == MoveStates.Kind.WORKING).orElse(false)));
        }
        announce(out);
        return List.copyOf(out);
    }

    /**
     * Logs {@code event=data_move_needs_operator} for each listed move this process has not announced: the ones already in the
     * state when it started, which the worker's transition never sees. {@link MoveStates#announce} is shared with the worker, so
     * the two never both log one move. A move whose last failure this process saw but whose state does not yet say
     * "needs an operator" is skipped: the worker is about to log it with its step and bound. A move that has left the list
     * is forgotten, so entering the state again pages again.
     */
    private void announce(List<BootWorkRegistry.NeedsOperator> listed) {
        for (var e : listed) {
            var state = states.state(e.label());
            boolean workerAboutToLog = state.map(s -> s.step() != null && !s.needsOperator()).orElse(false);
            if (!workerAboutToLog && states.announce(e.label())) {
                MoveEvents.needsOperator(e.label(), e.step(), e.sqlstate(), e.boundMs(), e.retries(),
                    e.terminatesIndexBuilds(), MoveRetryPolicy.pageReason(e.retries(), e.terminatesIndexBuilds()));
            }
        }
        states.retainAnnounced(listed.stream().map(BootWorkRegistry.NeedsOperator::label).toList());
    }

    private record Desired(String what, Long done, Long total, BootWorkRegistry.Step step, String reasonClass) {
        Desired(String what, Long done, Long total, BootWorkRegistry.Step step) {
            this(what, done, total, step, null);
        }
    }

    /** The reader of a running engine: its own admin session, kept between samples and replaced after a failure. */
    private static final class DbReader implements Reader {
        private final AdminConnection admin;
        private final String applicationName;
        private volatile AdminDdlSession session;

        DbReader(AdminConnection admin, String applicationName) {
            this.admin = Objects.requireNonNull(admin, "admin");
            this.applicationName = Objects.requireNonNull(applicationName, "applicationName");
        }

        @Override
        public Snapshot read() {
            try {
                AdminDdlSession s = session;
                if (s == null || s.isCancelled()) {
                    s = AdminDdlSession.open(admin, new AdminDdlSession.Settings(applicationName, 0L,
                        "data move boot work", "nexus", "data_moves", "none"), false);
                    session = s;
                }
                DSLContext ctx = s.ctx();
                List<DataMove> open = DataMoveRegistry.openMoves(ctx);
                List<DataMoveRegistry.FailedMove> failed = DataMoveRegistry.failedMoves(ctx);
                Map<String, DataMoveRuns.Progress> progress = new HashMap<>();
                for (DataMove m : open) {
                    progressOf(ctx, m.label(), progress);
                }
                for (var f : failed) {
                    progressOf(ctx, f.label(), progress);
                }
                return new Snapshot(open, failed, progress);
            } catch (RuntimeException e) {
                close();   // the next sample opens a new one
                throw e;
            }
        }

        /**
         * The move's runs summed, left out when they cannot be read: where the library install was skipped
         * ({@code dml_utils_data} does not exist) every open move is blocked for that reason, and the reason is what the
         * entry must say, so a missing run table costs the counts and nothing else.
         */
        private static void progressOf(DSLContext ctx, String label, Map<String, DataMoveRuns.Progress> into) {
            try {
                into.put(label, DataMoveRuns.progress(ctx, label));
            } catch (DataAccessException e) {
                log.debug("event=data_move_progress_unreadable label={} cause=\"{}\"", label,
                    FailureHandler.oneLine(e.getMessage()));
            }
        }

        @Override
        public void abort() {
            AdminDdlSession s = session;
            if (s != null) {
                s.cancel();
            }
        }

        @Override
        public void close() {
            AdminDdlSession s = session;
            session = null;
            if (s != null) {
                try {
                    s.close();
                } catch (RuntimeException e) {
                    log.debug("event=data_move_boot_work_session_close_failed cause=\"{}\"", FailureHandler.oneLine(e.toString()));
                }
            }
        }
    }

    private Desired failedEntry(String label, String failure, DataMoveRuns.Progress progress) {
        Long total = total(progress);
        return new Desired("failed: " + label + ": " + MoveReasons.publicForm(failure) + failedPointer(label),
            shownDone(label, progress, total), total, null);
    }

    /** Where the full failure is: the registry's column, which only the admin role reads, and the engine log. */
    static String failedPointer(String label) {
        return " (the failure text: SELECT failure FROM nexus.data_moves WHERE label = '" + label
            + "' as nexus_admin, or the engine log, event=data_move_failed)";
    }

    private Desired describeOpen(DataMove move, DataMoveRuns.Progress progress, Instant now) {
        String label = move.label();
        Long total = total(progress);
        Long done = shownDone(label, progress, total);
        var state = states.state(label);
        Instant firstBlocked = state.map(MoveStates.State::firstBlockedAt).orElse(null);
        String blockedSince = firstBlocked == null ? "" : " (blocked since " + stamp(firstBlocked) + ")";
        var call = states.call(label);
        var step = call.map(c -> new BootWorkRegistry.Step("CALL in flight", null, null, null)).orElse(null);

        if (move.paused()) {
            return new Desired("paused: " + label + (move.pausedBy() == null ? "" : " by " + move.pausedBy())
                + " (an operator holds it; nothing is lost)", done, total, null);
        }
        if (state.isEmpty()) {
            if (move.transientRetries() > 0 && move.lastTransient() != null) {
                return new Desired("blocked: " + label + ": " + MoveReasons.publicForm(move.lastTransient())
                    + " (from before this engine started; details in the engine log)", done, total, null,
                    MoveReasons.classOf(move.lastTransient()));
            }
            return new Desired("queued: " + label + " (the worker has not reached it yet)", done, total, null);
        }
        var s = state.get();
        return switch (s.kind()) {
            case BLOCKED -> new Desired("blocked: " + label + ": " + MoveReasons.publicForm(s.reason()) + blockedSince, done,
                total, null, MoveReasons.classOf(s.reason()));
            case FAILED -> new Desired("failed: " + label + ": " + MoveReasons.publicForm(s.reason()) + failedPointer(label),
                done, total, null);
            case PAUSED -> new Desired("paused: " + label + (s.pausedBy() == null ? "" : " by " + s.pausedBy()), done, total,
                null);
            case RUNS_COMPLETE -> new Desired("runs complete: " + label + " (verification pending)", done, total, null);
            case WORKING -> {
                String stalled = done == null ? null
                    : call.map(c -> stalledFor(label, done, c.startedAt(), now)).orElse(null);
                if (stalled != null) {
                    yield new Desired("stalled: " + label + ": no chunk has completed for " + stalled + "; its CALL is still "
                        + "open (the engine log names its backend, event=data_move_call; pg_cancel_backend on it makes the "
                        + "worker retry)" + blockedSince, done, total, step);
                }
                yield new Desired("working: " + label + (firstBlocked == null ? "" : ", retrying after being blocked since "
                    + stamp(firstBlocked)), done, total, step);
            }
        };
    }

    /**
     * {@code done} as an entry shows it: the sum of the move's active runs, never lower than it has been and never above
     * {@code total}. A leaf dropped mid-move leaves both sums (its run is archived), so the raw sum can fall, or fall
     * behind a high-water that was taken before it.
     *
     * @param highest the value shown before, or null
     * @param raw     the sum now, or null when the runs cannot be read
     * @param total   the planned total now, or null when not known
     */
    static Long shown(Long highest, Long raw, Long total) {
        Long value = raw == null ? highest : highest == null ? raw : Long.valueOf(Math.max(highest, raw));
        return value == null || total == null ? value : Long.valueOf(Math.min(value, total));
    }

    private Long shownDone(String label, DataMoveRuns.Progress progress, Long total) {
        Long value = shown(highestDone.get(label), progress == null ? null : progress.done(), total);
        if (value != null) {
            highestDone.put(label, value);
        }
        return value;
    }

    private static Long total(DataMoveRuns.Progress progress) {
        return progress == null ? null : progress.total();
    }

    /**
     * How long the CALL has gone without a chunk, or null when it has not gone {@link #stallAfter} yet: from the later of the
     * call's start and the last time {@code done} moved.
     */
    private String stalledFor(String label, long done, Instant callStart, Instant now) {
        Advance last = lastAdvance.get(label);
        if (last == null || last.done() != done) {
            last = new Advance(done, now);
            lastAdvance.put(label, last);
        }
        Instant since = last.at().isAfter(callStart) ? last.at() : callStart;
        Duration idle = Duration.between(since, now);
        return idle.compareTo(stallAfter) >= 0 ? words(idle) : null;
    }

    private static String stamp(Instant t) {
        return t.truncatedTo(ChronoUnit.SECONDS).toString();
    }

    private static String words(Duration d) {
        long minutes = d.toMinutes();
        return minutes >= 60 ? (minutes / 60) + " h " + (minutes % 60) + " m" : minutes + " m";
    }
}
