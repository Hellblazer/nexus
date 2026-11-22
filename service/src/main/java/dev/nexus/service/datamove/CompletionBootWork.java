// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.Executor;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): runs {@link CompletionCheck} after the schema walk, on its own admin session and its own
 * thread, keeps the latest {@link CompletionCheck.Result} for the engine's Java to read ({@link #latest}), and reports it in its
 * own {@code /v1/status} field, {@code rdr229_completion} ({@link CompletionStatusJson}), where {@code nx doctor} reads it
 * ({@code nexus/rdr229_completion_health.py}).
 *
 * <p><b>Not in {@code boot_work}</b> (fix round A, critique B1). {@code boot_work} is work in progress, and {@code nx upgrade}'s
 * bundle swap and the 3h boot-write harness count every entry in it that is not a parked data move as work. The three gates wait
 * by design (gate 3 cannot hold until P3.4 builds its id indexes), so an entry for them stays on every install, and on a bundle
 * that lacks pg_background it deferred the very swap that lets the stamp moves run. An older nx reads {@code boot_work} the same
 * way, so the state is never published there; nothing this class reports is work. (The moves themselves, which are work, are
 * {@code boot_work} entries of {@link MoveBootWork}.) One exception, RDR-229 P3.7: while every stamp move is complete and a P3.5b changeset has
 * not applied, the engine needs one restart, and the entry {@value #RESTART_ENTRY_ID} says so ({@code nx doctor}), until that restart applies them.
 * It is visible and safe to stop, but it is NOT work (linda 2026-11-22): both idle rules that count {@code boot_work} entries,
 * {@code nx upgrade}'s bundle-swap deferral ({@code engine_busy_reason}) and the boot-write harness ({@code boot_work_settled}), skip
 * it by id, because an upgrade is the restart that clears it.
 *
 * <p><b>It reads and reports; it writes nothing.</b> The session is opened {@code default_transaction_read_only = on}, so a
 * write would raise at the server, and the check itself holds no write. Sam's rule is no boot-time data migrations.
 *
 * <p>The state is re-read when the facts it reads can have changed, not on a short timer (RDR-229 item 6, "Readers during the
 * stamp"): when the move worker records a move's completion ({@link #onMoveCompleted}, called by
 * {@code DataMoveWorker}) and when a PCI reconciler pass ends ({@link #onReconcilerPassEnded}, called by {@code PciReconciler}),
 * so the engine's id readers, P3.3b's pass, P3.4's builds and the one-row rename see a gate hold with no restart. A
 * {@link #SAFETY_POLL} of 60 s reads again if no event came in that time, in case one was missed. A consumer that must read
 * at once calls {@link #refresh}. The rows that keep a CHECK NOT VALID are counted by a bounded scan, so that count is
 * cached for {@link #VIOLATOR_TTL} per table.
 *
 * <p>The field names no collection or tenant ({@code /v1/status} is unauthenticated); {@link #statusJson} renders it from the
 * last sample, in memory.
 */
public final class CompletionBootWork implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(CompletionBootWork.class);

    /** The slow poll that backs the two events: a read this long after the last one, whatever caused it. */
    public static final Duration SAFETY_POLL = Duration.ofSeconds(60);
    /** How often the schedule asks whether the safety poll is due; the read itself is at most once per {@link #SAFETY_POLL}. */
    private static final Duration TICK = Duration.ofSeconds(1);
    /** How long gate 3's flip to true waits for the renames pinned in the rename window ({@link RenameReadiness#pinned}). */
    public static final Duration RENAME_WINDOW_WAIT = Duration.ofSeconds(30);
    /** How long after a deferred latch the next read is scheduled. */
    public static final Duration RENAME_WINDOW_RETRY = Duration.ofSeconds(1);
    /** How long a table's violating-row count is kept: the scan is bounded, not free. */
    public static final Duration VIOLATOR_TTL = Duration.ofMinutes(10);

    /** Where one evaluation comes from. {@link #abort} ends a read in flight; {@link #close} releases the session. */
    interface Reader extends AutoCloseable {
        CompletionCheck.Result read();

        void abort();

        @Override
        void close();
    }

    /** The {@code boot_work} id of the restart that applies P3.5b's changesets (RDR-229 P3.7). */
    public static final String RESTART_ENTRY_ID = "rdr229-restart";

    private final BootWorkRegistry registry;
    private final Reader reader;
    /** Read side: a rename in flight ({@link RenameReadiness#pinned}). Write side: gate 3 flipping to true, in {@link #publish}. Fair, so a flip is not starved. */
    private final java.util.concurrent.locks.ReentrantReadWriteLock renameWindow = new java.util.concurrent.locks.ReentrantReadWriteLock(true);
    private final Object sampleLock = new Object();
    /** The process-local gate 3 latch: once true it stays true (see {@link #gate3Latched()} for what replaces it). Written under {@code sampleLock}. */
    private boolean gate3Latch;
    /** How long gate 3's flip waits for the pinned renames before it defers to the next sample. */
    private volatile Duration renameWindowWait = RENAME_WINDOW_WAIT;
    private volatile Duration renameWindowRetry = RENAME_WINDOW_RETRY;
    private final Clock clock;
    /** Where an event's read runs; null before {@link #start}, when the event only marks that a read is owed. */
    private volatile Executor eventExecutor;
    private final AtomicBoolean readOwed = new AtomicBoolean();
    private volatile Instant lastSampleAt;
    private volatile Duration safetyPoll = SAFETY_POLL;
    private volatile CompletionCheck.Result latest;
    private volatile boolean closed;
    private String lastError;
    private volatile ScheduledExecutorService scheduler;

    /**
     * @param gate3 P3.4's id-index coverage ({@link CompletionCheck.Gate3Source#NOT_INSTALLED} until it ships)
     */
    public CompletionBootWork(BootWorkRegistry registry, AdminConnection admin, CompletionCheck.ReadSet readSet,
                              CompletionCheck.Gate3Source gate3) {
        this(registry, admin, readSet, gate3, P35bChangesets.resource());
    }

    /**
     * As above, with the P3.5b changeset ids the one-row rename waits for supplied by {@code p35b} ({@link P35bChangesets#resource()} in
     * production; a test passes its own list). An empty set is refused: a rename waits.
     */
    public CompletionBootWork(BootWorkRegistry registry, AdminConnection admin, CompletionCheck.ReadSet readSet,
                              CompletionCheck.Gate3Source gate3, java.util.function.Supplier<java.util.Set<String>> p35b) {
        this(registry, new DbReader(admin, readSet, gate3, Clock.systemUTC(), p35b));
    }

    /**
     * The reporter a running engine starts: the boot read set ({@link CompletionCheck.ReadSet#boot()}) and no gate 3 until P3.4 ships one.
     * {@code Main} builds it only through here, so a test of this factory (and of {@code Main}'s one call to it) pins what boot reads.
     */
    public static CompletionBootWork atBoot(BootWorkRegistry registry, AdminConnection admin) {
        return new CompletionBootWork(registry, admin, CompletionCheck.ReadSet.boot(), CompletionCheck.Gate3Source.NOT_INSTALLED);
    }

    /** The read set a database-backed reporter reads, or null for a test's injected reader. */
    CompletionCheck.ReadSet readSet() {
        return reader instanceof DbReader d ? d.readSet : null;
    }

    CompletionBootWork(BootWorkRegistry registry, Reader reader) {
        this(registry, reader, Clock.systemUTC(), null);
    }

    /** As above with the clock the safety poll reads and the executor an event's read runs on injected (tests: a fixed clock, a direct executor). */
    CompletionBootWork(BootWorkRegistry registry, Reader reader, Clock clock, Executor eventExecutor) {
        this.registry = Objects.requireNonNull(registry, "registry");
        this.reader = Objects.requireNonNull(reader, "reader");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.eventExecutor = eventExecutor;
        registry.rdr229Completion(this::statusJson);
    }

    /**
     * The {@code rdr229_completion} field of {@code /v1/status} as a JSON object, rendered from the last sample (memory only, no
     * query), or null before the first read: a client reads an absent field as "not known yet".
     */
    public String statusJson() {
        CompletionCheck.Result r = latest;
        return r == null ? null : CompletionStatusJson.render(r);
    }

    /** The result of the last sample, or null before the first one. */
    public CompletionCheck.Result latest() {
        return latest;
    }

    /**
     * RDR-229 item 6, "Readers during the stamp": whether gate 3 of the latest sample holds, read per call, never cached. False before
     * the first sample. {@code Main} hands it to each reader that switches to the id ({@link Gate3}).
     */
    // TODO(P3.4 rebase): the readers' Gate3 (held by CatalogRepository, PgVectorRepository and TaxonomyCentroidRepository) stays LIVE in this
    // tree and moves to the latch ({@link #gate3Latched()}) when P3.4 stacks.
    public Gate3 gate3() {
        return () -> {
            var r = latest();
            return r != null && r.gate3();
        };
    }

    /**
     * Whether gate 3 has been latched on this install: it held once and stays remembered although the live {@link #gate3()} can drop back
     * (an id index going INVALID). The rename path keys on this ({@link CompletionCheck.Result#gate3Latched()}). Read per call.
     * TODO(P3.4, vectors-080): the table {@code nexus.rdr229_gate_latch} replaces the process-local latch kept here ({@code gate3Latch}) at
     * the rebase; the accessor names stay.
     */
    public boolean gate3Latched() {
        var r = latest();
        return r != null && r.gate3Latched();
    }

    /**
     * What a rename from gate 3 still waits for, read per call ({@link RenameReadiness}), from the latest sample, the one that
     * {@link #gate3()} reads: {@link CompletionCheck.Result#renameMissing()}. Before the first sample it answers that the name-key drop move is
     * missing (not known yet is not ready). Its {@link RenameReadiness#pinned} is the rename window ({@code renameWindow}): gate 3's flip
     * to true waits for every pinned rename to return.
     */
    public RenameReadiness renameReadiness() {
        return new RenameReadiness() {
            @Override
            public Missing missing() {
                var r = latest();
                return r == null ? Missing.NAME_KEY_DROP_MOVE : r.renameMissing();
            }

            @Override
            public boolean gate3Latched() {
                return CompletionBootWork.this.gate3Latched();
            }

            @Override
            public boolean dropMoveComplete() {
                var r = latest();
                return r != null && r.rename().dropMoveComplete();
            }

            @Override
            public <T> T pinned(java.util.function.Supplier<T> rename) {
                renameWindow.readLock().lock();
                try {
                    return rename.get();
                } finally {
                    renameWindow.readLock().unlock();
                }
            }
        };
    }

    /** Test seam: how long after a deferred latch the retry is scheduled. */
    void renameWindowRetry(Duration retry) {
        this.renameWindowRetry = Objects.requireNonNull(retry, "retry");
    }

    /** Test seam: how long the flip waits for a pinned rename. */
    void renameWindowWait(Duration wait) {
        this.renameWindowWait = Objects.requireNonNull(wait, "wait");
    }

    /** Reads now, outside the schedule, for a consumer that must not wait for the next sample. */
    public CompletionCheck.Result refresh() {
        publish();
        return latest;
    }

    /**
     * The worker finished a move ({@code completed_at} is recorded): read again, off the worker's thread. A burst of events is
     * one read. Never throws and never blocks the caller.
     */
    public void onMoveCompleted() {
        requestRead();
    }

    /** A PCI reconciler pass ended: read again, as {@link #onMoveCompleted}. */
    public void onReconcilerPassEnded() {
        requestRead();
    }

    private void requestRead() {
        readOwed.set(true);
        Executor e = eventExecutor;
        if (e == null || closed) {
            return;   // before start: the first sample reads everything
        }
        try {
            e.execute(this::readIfOwed);
        } catch (RuntimeException rejected) {
            // the schedule was shut down by close(): nothing to read for
        }
    }

    private void readIfOwed() {
        if (!readOwed.getAndSet(false)) {
            return;   // an earlier event's read already covered this one
        }
        sampleSafely();
    }

    /** Reads when {@link #SAFETY_POLL} has passed since the last sample (an event's or the poll's own). Public to the schedule and the tests. */
    void pollIfDue() {
        Instant last = lastSampleAt;
        if (last == null || !clock.instant().isBefore(last.plus(safetyPoll))) {
            sampleSafely();
        }
    }

    private void sampleSafely() {
        try {
            publish();
        } catch (VirtualMachineError e) {
            throw e;
        } catch (Throwable t) {   // a surprise costs one sample, never the thread
            log.warn("event=rdr229_completion_failed cause=\"{}\"", FailureHandler.oneLine(t.toString()));
        }
    }

    /** Samples once now, then on the two events, with a read every {@link #SAFETY_POLL} behind them, on a daemon thread. A second call does nothing. */
    public synchronized void start() {
        start(SAFETY_POLL);
    }

    synchronized void start(Duration safetyPoll) {
        if (scheduler != null) {
            return;
        }
        this.safetyPoll = safetyPoll;
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "rdr229-completion-check");
            t.setDaemon(true);
            return t;
        });
        if (eventExecutor == null) {
            eventExecutor = scheduler;   // events read on the schedule's own thread, so reads never overlap
        }
        long tickMs = Math.min(TICK.toMillis(), safetyPoll.toMillis());
        scheduler.execute(this::sampleSafely);
        scheduler.scheduleWithFixedDelay(this::pollIfDueSafely, tickMs, tickMs, TimeUnit.MILLISECONDS);
    }

    private void pollIfDueSafely() {
        try {
            pollIfDue();
        } catch (VirtualMachineError e) {
            throw e;
        } catch (Throwable t) {
            log.warn("event=rdr229_completion_failed cause=\"{}\"", FailureHandler.oneLine(t.toString()));
        }
    }

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
        closed = true;
        registry.rdr229Completion(null);
        registry.finish(RESTART_ENTRY_ID);
        try {
            reader.abort();
        } finally {
            reader.close();
        }
    }

    /**
     * One sample: evaluates the check and keeps the result for {@link #statusJson}.
     *
     * @return how many things the field lists as waiting (the summary while a gate waits, plus a row per missing CHECK and per
     *         table whose CHECK is NOT VALID), or -1 when the check could not be read (the field stays as it was)
     */
    int publish() {
        synchronized (sampleLock) {
            if (closed) {
                return -1;
            }
            lastSampleAt = clock.instant();
            CompletionCheck.Result result;
            try {
                result = reader.read();
                lastError = null;
            } catch (RuntimeException e) {
                String cause = FailureHandler.oneLine(e.toString());
                if (!cause.equals(lastError)) {
                    log.warn("event=rdr229_completion_unreadable cause=\"{}\"", cause);
                    lastError = cause;
                }
                return -1;
            }
            if (result.gate3() && !gate3Latch) {
                // gate 3 is about to be latched, a one-time event: wait for every pinned rename to return, so none commits a name-path
                // rewrite after the readers switch (O-5). The wait is bounded: a rename that holds the window longer than renameWindowWait
                // defers the latch to the next sample (a read is owed at once) instead of stalling the check, and its readers keep reading
                // gate 3 as false. After the latch no pin is needed: every rename from then on is one-row or refused.
                boolean locked;
                try {
                    locked = renameWindow.writeLock().tryLock(renameWindowWait.toMillis(), TimeUnit.MILLISECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    locked = false;
                }
                if (!locked) {
                    // A rename still holds the window: publish this sample (the moves, the CHECKs, the counts are current) but with gate 3
                    // read as not holding and the latch unset, so the readers keep the name key while that rename can still commit a
                    // name-path rewrite. Only the latch is deferred: the retry is scheduled (RENAME_WINDOW_RETRY), not left to the next event
                    // or the 60 s poll.
                    log.warn("event=rdr229_rename_window_busy waited_ms={} cause=\"a pinned rename is still open; the gate 3 latch is deferred to the next sample\"",
                        renameWindowWait.toMillis());
                    scheduleRetry();
                    latest = result.withoutGate3("a collection rename is still open: gate 3 is deferred to the next sample");
                    result = latest;
                    publishRestartEntry(result);
                    return countWaiting(result);
                }
                try {
                    // the sample is as old as the wait: gate 3 may have dropped again while the rename ran, so read again under the
                    // lock and latch only if it still holds (the fresh sample is what is published either way)
                    try {
                        result = reader.read();
                        lastError = null;
                    } catch (RuntimeException e) {
                        log.warn("event=rdr229_completion_unreadable cause=\"{}\" during=rename_window", FailureHandler.oneLine(e.toString()));
                        return -1;
                    }
                    if (result.gate3()) {
                        gate3Latch = true;
                    }
                    latest = result.withGate3Latched(gate3Latch);
                } finally {
                    renameWindow.writeLock().unlock();
                }
            } else {
                latest = result.withGate3Latched(gate3Latch);
            }
            result = latest;
            publishRestartEntry(result);
            return countWaiting(result);
        }
    }

    /** Asks for another read after {@link #RENAME_WINDOW_RETRY} on the schedule's thread (nothing before {@link #start}: the first sample reads everything). */
    private void scheduleRetry() {
        readOwed.set(true);
        ScheduledExecutorService s = scheduler;
        if (s == null || closed) {
            return;
        }
        try {
            s.schedule(this::requestRead, renameWindowRetry.toMillis(), TimeUnit.MILLISECONDS);
        } catch (RuntimeException rejected) {
            // the schedule was shut down by close(): nothing to read for
        }
    }

    /** The things the field lists as waiting: the summary while a gate waits, a row per missing CHECK and per table whose CHECK is NOT VALID. */
    private static int countWaiting(CompletionCheck.Result result) {
        long holding = (result.gate1() ? 1 : 0) + (result.gate2() ? 1 : 0) + (result.gate3() ? 1 : 0);
        return (holding < 3 ? 1 : 0) + (int) result.checks().stream().filter(c -> !c.present()).count()
            + byTable(result.violators()).size();
    }

    /**
     * RDR-229 P3.7: while every stamp move is complete and a P3.5b changeset has not applied, the engine needs one restart, and says so in
     * {@code boot_work}, where {@code nx doctor} reads it; it leaves the list when the changesets have applied (the restart ran). Not the
     * three gates, which are not work ({@link CompletionBootWork}): a restart is the one step an operator takes, and it is what the entry names.
     */
    private void publishRestartEntry(CompletionCheck.Result result) {
        if (result.restartNeeded()) {
            String what = "restart the engine once: it applies the P3.5b changesets that a collection rename waits for ("
                + result.rename().missing().size() + " of " + result.rename().listed() + " not applied yet)";
            if (registry.snapshot().stream().anyMatch(e -> RESTART_ENTRY_ID.equals(e.id()))) {
                registry.update(RESTART_ENTRY_ID, what, null, null, null);   // keeps startedAt: a sample is not a new run
            } else {
                registry.register(RESTART_ENTRY_ID, what, null, null, null, true, "nothing is lost: the restart is the step that applies them");
            }
        } else {
            registry.finish(RESTART_ENTRY_ID);
        }
    }

    /** One table whose stamp is done and whose CHECK(s) are still NOT VALID: every such constraint of it, with the summed counts. */
    record TableWait(String table, java.util.List<String> constraints, long orphans, long unstamped, boolean counted) { }

    /**
     * One row per table: a table with two name columns (chash_remap, reembed_jobs) has a CHECK per column, and the operator sees one
     * VALIDATE waiting that names every one of them, with the sum.
     */
    static java.util.List<TableWait> byTable(java.util.List<CompletionCheck.Violators> all) {
        Map<String, TableWait> merged = new LinkedHashMap<>();
        for (CompletionCheck.Violators v : all) {
            merged.merge(v.table(), new TableWait(v.table(), java.util.List.of(v.constraint()), v.orphans(), v.unstamped(), v.counted()),
                (a, b) -> {
                    var constraints = new java.util.ArrayList<>(a.constraints());
                    constraints.addAll(b.constraints());
                    return new TableWait(a.table(), java.util.List.copyOf(constraints), a.orphans() + b.orphans(),
                        a.unstamped() + b.unstamped(), a.counted() && b.counted());
                });
        }
        return java.util.List.copyOf(merged.values());
    }

    static String summary(CompletionCheck.Result r) {
        long total = r.labels().size();
        long unsatisfied = total - r.labelsSatisfied();
        String g1 = r.gate1() ? "holds"
            : "waits (" + unsatisfied + " of " + total + " stamp moves incomplete"
                + (r.supersededByCheckPresent() ? "" : "; the superseded_by CHECK is missing") + ")";
        String g2 = r.gate2() ? "holds" : (r.gate1() ? "waits (a swap or validation move is incomplete)" : "waits on gate 1");
        String g3 = r.gate3() ? "holds" : "waits: " + r.gate3Why();
        return "gate 1 " + g1 + "; gate 2 " + g2 + "; gate 3 " + g3;
    }

    static String missing(CompletionCheck.CheckState c, CompletionCheck.Result r) {
        if (CompletionCheck.SUPERSEDED_BY_CHECK.equals(c.constraint())) {
            // no count: the text goes out on the unauthenticated /v1/status (the caller's own count is on the counts route)
            return "waits: " + c.constraint() + " is not on " + c.table() + " yet; registry rows with a blank model_version and no "
                + "superseded_by_id hold its changeset back "
                + "(D2's repair move or the superseded_by stamp clears them, then the next boot adds it); the switch is refused until then";
        }
        return "missing: " + c.constraint() + " is not on " + c.table() + "; the switch is refused until it is restored "
            + "(its changeset text is in vectors-068-collection-id-expand.xml)";
    }

    /**
     * The operator text of a table whose stamp move is done and whose CHECK stays NOT VALID. It carries no count and no name: it goes out
     * on the unauthenticated {@code /v1/status}, so a number of orphan or unstamped rows (which describes tenants) is not in it; a caller
     * reads its own counts from {@code GET /v1/catalog/rdr229_completion_counts} ({@link CompletionTenantCounts}) and says which of the
     * two shapes applies. The move does not run again on its own (it is complete), so registering the missing collection does not
     * stamp the rows: the step is {@code nexus.rdr229_restamp} (vectors-071), which registers the table's stamp move once more,
     * guarded to run only when the table's stamp move is complete, a CHECK of the table is NOT VALID and no orphan row remains. The
     * new move stamps the rows and validates the CHECK(s). An orphan row (name set, id NULL, no registry row) is write-frozen: the
     * CHECK refuses any UPDATE of it, whatever the update sets, until its collection is registered or the row is deleted.
     */
    static String validateWaits(TableWait w) {
        return "waits: " + String.join(", ", w.constraints()) + " on " + w.table() + " is NOT VALID while rows remain that it refuses: "
            + "orphan rows (a collection name no registry row of their tenant carries) and rows the stamp has not given an id yet. "
            + "An orphan row is write-frozen: any UPDATE of it fails with SQLSTATE 23514 until its collection is registered or the row is deleted. "
            + "To finish: register each missing collection or delete the orphan rows, if there are any, then run  SELECT nexus.rdr229_restamp('"
            + w.table() + "');  as the migrating role. It re-registers this table's stamp move once no orphan remains; the move stamps "
            + "the rows and validates the CHECK. With no orphan row, that restamp step alone finishes it.";
    }

    /** The reader of a running engine: its own read-only admin session, kept between samples and replaced after a failure. */
    private static final class DbReader implements Reader {
        private final AdminConnection admin;
        private final CompletionCheck.ReadSet readSet;
        private final CompletionCheck.Gate3Source gate3;
        private final CompletionCheck.ViolatorCounter counter;
        private final java.util.function.Supplier<java.util.Set<String>> p35b;
        private volatile AdminDdlSession session;

        DbReader(AdminConnection admin, CompletionCheck.ReadSet readSet, CompletionCheck.Gate3Source gate3, Clock clock,
                 java.util.function.Supplier<java.util.Set<String>> p35b) {
            this.p35b = Objects.requireNonNull(p35b, "p35b");
            this.admin = Objects.requireNonNull(admin, "admin");
            this.readSet = Objects.requireNonNull(readSet, "readSet");
            this.gate3 = Objects.requireNonNull(gate3, "gate3");
            this.counter = new CachedCounter(CompletionCheck.DIRECT, clock, VIOLATOR_TTL);
        }

        @Override
        public CompletionCheck.Result read() {
            try {
                AdminDdlSession s = session;
                if (s == null || s.isCancelled()) {
                    s = AdminDdlSession.open(admin, new AdminDdlSession.Settings("nexus-rdr229-completion", 0L,
                        "rdr229 completion check", "nexus", "data_moves", "none"), false);
                    // The check reads and reports. Held at the server: a write on this session raises 25006.
                    s.setSession("default_transaction_read_only", "on");
                    session = s;
                }
                return CompletionCheck.evaluate(s.ctx(), readSet, gate3, counter, p35b);
            } catch (RuntimeException e) {
                close();   // a failed session is replaced on the next sample
                throw e;
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
                s.close();
            }
        }
    }

    /** A violating-row counter that keeps each table's answer for {@code ttl}; a count that failed is not kept. */
    static final class CachedCounter implements CompletionCheck.ViolatorCounter {
        private final CompletionCheck.ViolatorCounter delegate;
        private final Clock clock;
        private final Duration ttl;
        private final Map<String, Kept> kept = new HashMap<>();

        private record Kept(CompletionCheck.Violators value, Instant at) { }

        CachedCounter(CompletionCheck.ViolatorCounter delegate, Clock clock, Duration ttl) {
            this.delegate = delegate;
            this.clock = clock;
            this.ttl = ttl;
        }

        @Override
        public synchronized CompletionCheck.Violators count(DSLContext ctx, P31Checks.Row row) {
            Kept k = kept.get(row.constraint());
            Instant now = clock.instant();
            if (k != null && Duration.between(k.at(), now).compareTo(ttl) < 0) {
                return k.value();
            }
            CompletionCheck.Violators fresh = delegate.count(ctx, row);
            if (fresh.counted()) {
                kept.put(row.constraint(), new Kept(fresh, now));
            } else {
                kept.remove(row.constraint());
            }
            return fresh;
        }
    }
}
