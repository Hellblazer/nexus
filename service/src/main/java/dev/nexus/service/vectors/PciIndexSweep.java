// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Objects;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

/**
 * RDR-227 Step 2 (nexus-43ulx.12): the sweep's read half. It owns the router's real {@link PciIndexSet}: an
 * immutable {@link PciCatalog.Snapshot} behind an {@link AtomicReference}, replaced whole by each successful read of
 * the catalog. The set is empty until the first read succeeds.
 *
 * <p><b>The search path never reads the catalog.</b> {@link #hasValidIndex} answers from the snapshot in memory;
 * only {@link #refresh()} (the scheduled task, and the builder after each local build or drop) touches the reader.
 *
 * <p><b>A failed read keeps the previous set.</b> A reader that throws, or a snapshot with no leaves (which no
 * installed schema produces, so it is a failed read, never evidence that no index exists), logs
 * {@code event=pci_sweep_read_failed} and leaves both the set and the counts as they were.
 *
 * <p><b>Reads are serialised.</b> {@link #refresh()} is public for the builder and may run while the scheduled read
 * is mid-statement. Without a lock, a statement that began before the builder's index committed could finish after
 * the builder's own read and put its older view back. Each read, and the install of its result, holds one lock, so
 * the last read to START is the last to land.
 *
 * <p><b>The schedule is the read half's alone.</b> {@link #start()} runs one {@code scheduleWithFixedDelay} task on
 * a thread of its own, whatever {@code NX_SEARCH_PCI} says, so a long DDL pass in the other half never delays a
 * read or its read time. {@link #stop()} ends it; {@code Main} calls it in the shutdown hook before the pool closes.
 *
 * <p>The status ({@link #status()}) is what the status object reads: counts and times from the last successful read.
 */
public final class PciIndexSweep implements PciIndexSet {

    private static final Logger log = LoggerFactory.getLogger(PciIndexSweep.class);

    /** Name of the scheduler thread; the stop test and a thread dump both look for it. */
    static final String THREAD_NAME = "pci-sweep-read";

    private static final Duration STOP_WAIT = Duration.ofSeconds(5);

    /**
     * What the last successful read saw, and when. Zero counts and {@code null} times until a read succeeds.
     *
     * @param everRead      whether any read has succeeded since boot
     * @param valid         parsed indexes with {@code indisvalid} true
     * @param invalid       parsed indexes with {@code indisvalid} false
     * @param unparsed      {@code pci_} indexes that failed any part of the parsed rule
     * @param lastReadAt    when the last successful read finished, or {@code null}
     * @param lastFailureAt when the last failed read finished, or {@code null}
     */
    public record Status(boolean everRead, int valid, int invalid, int unparsed, Instant lastReadAt,
                         Instant lastFailureAt) {
        static final Status NEVER = new Status(false, 0, 0, 0, null, null);

        Status failedAt(Instant when) {
            return new Status(everRead, valid, invalid, unparsed, lastReadAt, when);
        }
    }

    /** The set and its counts, replaced together so a reader never sees one from one read and one from another. */
    private record State(PciIndexSet set, Status status) { }

    private final Supplier<PciCatalog.Snapshot> reader;
    private final Duration period;
    private final Clock clock;
    private final AtomicReference<State> state = new AtomicReference<>(new State(PciIndexSet.NONE, Status.NEVER));
    private final Object readLock = new Object();
    private final Object lifecycle = new Object();
    private ScheduledExecutorService scheduler;   // guarded by lifecycle
    private boolean stopped;                      // guarded by lifecycle

    /** @param reader reads the catalog; throws or returns an empty snapshot on a failed read */
    public PciIndexSweep(Supplier<PciCatalog.Snapshot> reader, PciSettings settings) {
        this(reader, settings, Clock.systemUTC());
    }

    PciIndexSweep(Supplier<PciCatalog.Snapshot> reader, PciSettings settings, Clock clock) {
        this(reader, Duration.ofSeconds(settings.sweepSeconds()), clock);
    }

    private PciIndexSweep(Supplier<PciCatalog.Snapshot> reader, Duration period, Clock clock) {
        this.reader = Objects.requireNonNull(reader, "reader");
        this.period = Objects.requireNonNull(period, "period");
        this.clock = Objects.requireNonNull(clock, "clock");
        if (period.isZero() || period.isNegative()) {
            throw new IllegalArgumentException("period must be positive, got " + period);
        }
    }

    /**
     * The production sweep: reads the catalog through {@code ds}, borrowing one pooled connection per read, at the
     * period {@code settings} names.
     */
    public static PciIndexSweep create(DataSource ds, PciSettings settings) {
        PciCatalog catalog = new PciCatalog(ds);
        return new PciIndexSweep(() -> catalog.read(), settings);
    }

    /**
     * Same as the constructor but at {@code period} instead of {@code settings.sweepSeconds()}. The setting's floor is
     * 60 seconds, which no test can wait out; production code does not call this.
     */
    public static PciIndexSweep withPeriod(Supplier<PciCatalog.Snapshot> reader, PciSettings settings,
                                           Duration period) {
        Objects.requireNonNull(settings, "settings");
        return new PciIndexSweep(reader, period, Clock.systemUTC());
    }

    /** The delay between the end of one scheduled read and the start of the next. */
    Duration period() {
        return period;
    }

    /** From memory, with no JDBC: true when the last successful read saw a valid parsed index for exactly this key. */
    @Override
    public boolean hasValidIndex(String model, String tenant, String collection) {
        return state.get().set().hasValidIndex(model, tenant, collection);
    }

    /** The counts and times of the last successful read; see {@link Status}. */
    public Status status() {
        return state.get().status();
    }

    /**
     * Read the catalog now and, if the read succeeds, replace the set. Safe to call from any thread at any time,
     * including while the scheduled read runs; the two are serialised. The builder calls it after every successful
     * build and every successful drop: a set that still lists a dropped index routes its collection to the serving
     * {@code ef_search} with no per-collection graph behind it.
     *
     * @return true when the set was replaced; false when the read failed and the previous set stands
     */
    public boolean refresh() {
        synchronized (readLock) {
            PciCatalog.Snapshot snapshot;
            try {
                snapshot = reader.get();
            } catch (RuntimeException e) {
                return failed("read_error", e);
            }
            if (snapshot == null || snapshot.leaves().isEmpty()) {
                return failed("no_partition_leaves", null);
            }
            Status status = new Status(true, snapshot.validCount(), snapshot.invalidCount(),
                snapshot.unparsedCount(), clock.instant(), state.get().status().lastFailureAt());
            state.set(new State(snapshot, status));
            log.info("event=pci_sweep valid={} invalid={} unparsed={}",
                status.valid(), status.invalid(), status.unparsed());
            return true;
        }
    }

    private boolean failed(String reason, RuntimeException cause) {
        State previous = state.get();
        state.set(new State(previous.set(), previous.status().failedAt(clock.instant())));
        String detail = cause == null ? "the read saw no partition leaves of nexus.chunks"
            : String.valueOf(cause.getMessage() != null ? cause.getMessage() : cause.toString());
        if (isStopping()) {
            // The shutdown hook interrupts a read in flight; that is not an incident.
            log.debug("event=pci_sweep_read_failed reason={} during=shutdown", reason);
        } else {
            log.warn("event=pci_sweep_read_failed reason={} kept_valid={} error=\"{}\"",
                reason, previous.status().valid(), detail, cause);
        }
        return false;
    }

    private boolean isStopping() {
        synchronized (lifecycle) {
            return stopped;
        }
    }

    /**
     * Start the read task: one read at once, then one {@code period} after each read ends. Idempotent while running.
     * Call after the service has started. A sweep that has been stopped does not restart.
     */
    public void start() {
        synchronized (lifecycle) {
            if (stopped) {
                throw new IllegalStateException("the PCI sweep read task was stopped and does not restart");
            }
            if (scheduler != null) {
                return;
            }
            scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, THREAD_NAME);
                t.setDaemon(true);
                return t;
            });
            scheduler.scheduleWithFixedDelay(this::scheduledRead, 0, period.toMillis(), TimeUnit.MILLISECONDS);
        }
        log.info("event=pci_sweep_read_started period_seconds={}", period.toSeconds());
    }

    /** A thrown task is never rescheduled by the executor, so nothing may escape this method. */
    private void scheduledRead() {
        try {
            refresh();
        } catch (Throwable t) {
            log.error("event=pci_sweep_read_task_error error=\"{}\"", t.toString(), t);
        }
    }

    /** True between {@link #start()} and {@link #stop()}. */
    public boolean isRunning() {
        synchronized (lifecycle) {
            return scheduler != null && !stopped;
        }
    }

    /**
     * End the read task and wait briefly for it. A read in flight is interrupted. The last set stays readable.
     * Harmless when never started or already stopped.
     */
    public void stop() {
        ScheduledExecutorService toStop;
        synchronized (lifecycle) {
            stopped = true;
            toStop = scheduler;
        }
        if (toStop == null) {
            return;
        }
        toStop.shutdownNow();
        try {
            if (!toStop.awaitTermination(STOP_WAIT.toMillis(), TimeUnit.MILLISECONDS)) {
                log.warn("event=pci_sweep_read_stop_timeout waited_seconds={}", STOP_WAIT.toSeconds());
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
