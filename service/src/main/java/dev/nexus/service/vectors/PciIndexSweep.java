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
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.SortedSet;
import java.util.TreeSet;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.locks.ReentrantLock;
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
 * <p><b>Reads are serialised, and the wait for the turn is bounded.</b> {@link #refresh()} is public for the builder
 * and may run while the scheduled read is mid-statement. Without a lock, a statement that began before the builder's
 * index committed could finish after the builder's own read and put its older view back. Each read, and the install
 * of its result, holds one lock, so the last read to START is the last to land. The wait for that lock is bounded by
 * the read bound: a caller that cannot get its turn in that time returns false and logs
 * {@code event=pci_sweep_refresh_skipped}, so a stuck read never stalls the builder holding its advisory lock.
 *
 * <p><b>The set expires.</b> A failed or hung read keeps the previous set, which is right for a brief outage (the
 * indexes are usually still there) and wrong forever: a frozen set can claim a dropped index valid, and a collection
 * routed to {@code ef_search} on a leaf with no graph of its own. Once {@code now - lastReadAt} exceeds
 * {@code 3 * period + readBound} the set answers as empty ({@link PciIndexSet#NONE}, every statement walks at
 * {@code ef_search} 1000, the safe direction), {@code event=pci_sweep_set_expired} is logged once, and the next
 * successful read restores it and logs {@code event=pci_sweep_set_recovered}. Time comes from the injected clock.
 *
 * <p><b>The schedule is the read half's alone.</b> {@link #start()} runs one {@code scheduleWithFixedDelay} task on
 * a thread of its own, whatever {@code NX_SEARCH_PCI} says, so a long DDL pass in the other half never delays a
 * read or its read time. {@link #stop()} ends it without waiting; {@code Main} calls it in the shutdown hook after the
 * backend reaper and before the pool closes (closing the pool aborts a read the interrupt did not reach).
 *
 * <p>The status ({@link #status()}) is what the status object reads: counts and times from the last successful read,
 * the failures since, and whether the set has expired.
 */
public final class PciIndexSweep implements PciIndexSet {

    private static final Logger log = LoggerFactory.getLogger(PciIndexSweep.class);

    /** Name of the scheduler thread; the stop test and a thread dump both look for it. */
    static final String THREAD_NAME = "pci-sweep-read";

    /** How many sweep periods without a successful read the set survives; see the class comment. */
    static final int EXPIRY_PERIODS = 3;

    /**
     * What the last successful read saw, and when. Zero counts and {@code null} times until a read succeeds.
     *
     * @param everRead            whether any read has succeeded since boot
     * @param valid               parsed indexes with {@code indisvalid} true
     * @param invalid             parsed indexes with {@code indisvalid} false
     * @param unparsed            {@code pci_} indexes that failed any part of the parsed rule
     * @param lastReadAt          when the last successful read finished, or {@code null}
     * @param lastFailureAt       when the last failed read finished, or {@code null}
     * @param consecutiveFailures failed reads since the last successful one; 0 after a success
     * @param expired             true when the set is older than {@code 3 * period + readBound} and so is answering
     *                            as empty; the counts above are still the last read's
     */
    public record Status(boolean everRead, int valid, int invalid, int unparsed, Instant lastReadAt,
                         Instant lastFailureAt, int consecutiveFailures, boolean expired) {
        static final Status NEVER = new Status(false, 0, 0, 0, null, null, 0, false);

        Status failedAt(Instant when) {
            return new Status(everRead, valid, invalid, unparsed, lastReadAt, when, consecutiveFailures + 1, expired);
        }

        Status withExpired(boolean now) {
            return now == expired ? this
                : new Status(everRead, valid, invalid, unparsed, lastReadAt, lastFailureAt, consecutiveFailures, now);
        }
    }

    /**
     * The set and its counts, replaced together so a reader never sees one from one read and one from another.
     * {@code expiryLogged} belongs to the state, not the sweep: a reader that raced a newer read and flags the older
     * state expired marks only that state, and a failed read carries the flag forward so the expiry logs once.
     */
    private record State(PciIndexSet set, Status status, AtomicBoolean expiryLogged,
                         Map<PciCatalog.Key, Instant> since, SortedSet<String> entries) {
        State(PciIndexSet set, Status status) {
            this(set, status, new AtomicBoolean(), Map.of(), new TreeSet<>());
        }
    }

    /** The longest list of entries a {@code pci_sweep_set_changed} line names; the counts say how many there were. */
    static final int MAX_LOGGED_ENTRIES = 20;

    private final Supplier<PciCatalog.Snapshot> reader;
    private final Duration period;
    private final Duration readBound;
    private final Duration expiryAfter;
    private final Clock clock;
    private final AtomicReference<State> state = new AtomicReference<>(new State(PciIndexSet.NONE, Status.NEVER));
    private final ReentrantLock readLock = new ReentrantLock();
    private final Object lifecycle = new Object();
    private ScheduledExecutorService scheduler;   // guarded by lifecycle
    private boolean stopped;                      // guarded by lifecycle

    /**
     * A sweep with the catalog's default read bound. The bound sizes the wait for the read lock and the expiry.
     *
     * @param reader reads the catalog; throws or returns an empty snapshot on a failed read
     */
    public PciIndexSweep(Supplier<PciCatalog.Snapshot> reader, PciSettings settings) {
        this(reader, settings, Clock.systemUTC());
    }

    /** As the public constructor with an injected clock. */
    PciIndexSweep(Supplier<PciCatalog.Snapshot> reader, PciSettings settings, Clock clock) {
        this(reader, settings, clock, PciCatalog.DEFAULT_READ_BOUND);
    }

    PciIndexSweep(Supplier<PciCatalog.Snapshot> reader, PciSettings settings, Clock clock, Duration readBound) {
        this(reader, Duration.ofSeconds(Objects.requireNonNull(settings, "settings").sweepSeconds()), clock,
            readBound);
    }

    private PciIndexSweep(Supplier<PciCatalog.Snapshot> reader, Duration period, Clock clock, Duration readBound) {
        this.reader = Objects.requireNonNull(reader, "reader");
        this.period = Objects.requireNonNull(period, "period");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.readBound = Objects.requireNonNull(readBound, "readBound");
        if (period.isZero() || period.isNegative()) {
            throw new IllegalArgumentException("period must be positive, got " + period);
        }
        if (readBound.isZero() || readBound.isNegative()) {
            throw new IllegalArgumentException("readBound must be positive, got " + readBound);
        }
        this.expiryAfter = period.multipliedBy(EXPIRY_PERIODS).plus(readBound);
    }

    /**
     * The production sweep: reads the catalog through {@code ds}, borrowing one pooled connection per read, at the
     * period {@code settings} names, with the catalog's own read bound.
     */
    public static PciIndexSweep create(DataSource ds, PciSettings settings) {
        PciCatalog catalog = new PciCatalog(ds);
        return new PciIndexSweep(() -> catalog.read(), settings, Clock.systemUTC(), catalog.readBound());
    }

    /**
     * Same as the constructor but at {@code period} instead of {@code settings.sweepSeconds()}. The setting's floor is
     * 60 seconds, which no test can wait out; production code does not call this. Public only because
     * {@code PgVectorCardinalityRouterIntegrationTest}, in another package, uses it.
     *
     * @throws NullPointerException     when an argument is null
     * @throws IllegalArgumentException when {@code period} is not positive
     */
    public static PciIndexSweep withPeriod(Supplier<PciCatalog.Snapshot> reader, PciSettings settings, Duration period) {
        Objects.requireNonNull(reader, "reader");
        Objects.requireNonNull(settings, "settings");
        Objects.requireNonNull(period, "period");
        if (period.isZero() || period.isNegative()) {
            throw new IllegalArgumentException("period must be positive, got " + period);
        }
        return new PciIndexSweep(reader, period, Clock.systemUTC(), PciCatalog.DEFAULT_READ_BOUND);
    }

    /** The delay between the end of one scheduled read and the start of the next. */
    Duration period() {
        return period;
    }

    /**
     * From memory, with no JDBC: true when the last successful read saw a valid parsed index for exactly this key, and
     * that read is not older than {@code 3 * period + readBound}. An expired set answers false for every key.
     */
    @Override
    public boolean hasValidIndex(String model, String tenant, String collection) {
        State s = state.get();
        if (expired(s)) {
            noteExpired(s);
            return false;
        }
        return s.set().hasValidIndex(model, tenant, collection);
    }

    /**
     * As {@link PciIndexSet#validIndex}, from memory, with the name the last read saw and since when this key has been
     * listed without a break (a set that expired and recovered counts as a new run). An expired set answers empty.
     */
    @Override
    public Optional<ValidIndex> validIndex(String model, String tenant, String collection) {
        State s = state.get();
        if (expired(s)) {
            noteExpired(s);
            return Optional.empty();
        }
        Optional<ValidIndex> held = s.set().validIndex(model, tenant, collection);
        if (held.isEmpty()) {
            return held;
        }
        Instant since = s.since().get(new PciCatalog.Key(model, tenant, collection));
        return Optional.of(new ValidIndex(held.get().name(), since == null ? Instant.EPOCH : since));
    }

    /** The counts and times of the last successful read, the failures since, and whether it has expired. */
    public Status status() {
        State s = state.get();
        return s.status().withExpired(expired(s));
    }

    private boolean expired(State s) {
        Instant readAt = s.status().lastReadAt();
        return readAt != null && Duration.between(readAt, clock.instant()).compareTo(expiryAfter) > 0;
    }

    private void noteExpired(State s) {
        if (s.expiryLogged().compareAndSet(false, true)) {
            log.warn("event=pci_sweep_set_expired last_read_at={} expire_after_seconds={} consecutive_failures={} "
                + "detail=\"no successful catalog read within 3 periods plus the read bound; routing as if no "
                + "per-collection index exists until a read succeeds\"",
                s.status().lastReadAt(), expiryAfter.toSeconds(), s.status().consecutiveFailures());
        }
    }

    /**
     * Read the catalog now and, if the read succeeds, replace the set. Safe to call from any thread at any time,
     * including while the scheduled read runs; the two are serialised. The builder calls it after every successful
     * build and every successful drop: a set that still lists a dropped index routes its collection to the serving
     * {@code ef_search} with no per-collection graph behind it.
     *
     * <p>The wait for the read lock is bounded by the read bound: a caller that does not get its turn in that time
     * (a read ahead of it is stuck) returns false, logs {@code event=pci_sweep_refresh_skipped}, and leaves the set
     * and the counts alone.
     *
     * @return true when the set was replaced; false when the read failed or was skipped and the previous set stands
     */
    public boolean refresh() {
        try {
            if (!readLock.tryLock(readBound.toMillis(), TimeUnit.MILLISECONDS)) {
                log.warn("event=pci_sweep_refresh_skipped reason=read_lock_wait_timeout waited_ms={}",
                    readBound.toMillis());
                return false;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            if (isStopping()) {
                log.debug("event=pci_sweep_refresh_skipped reason=interrupted during=shutdown");
            } else {
                log.warn("event=pci_sweep_refresh_skipped reason=interrupted");
            }
            return false;
        }
        try {
            PciCatalog.Snapshot snapshot;
            try {
                snapshot = reader.get();
            } catch (RuntimeException e) {
                return failed("read_error", e);
            }
            if (snapshot == null || snapshot.leaves().isEmpty()) {
                return failed("no_partition_leaves", null);
            }
            State previous = state.get();
            Instant now = clock.instant();
            Status status = new Status(true, snapshot.validCount(), snapshot.invalidCount(),
                snapshot.unparsedCount(), now, previous.status().lastFailureAt(), 0, false);
            // A key listed by the read before this one, which had not expired, keeps its date; any other is new now.
            boolean continuous = !expired(previous);
            Map<PciCatalog.Key, Instant> since = new HashMap<>();
            for (PciCatalog.Key key : snapshot.validKeys()) {
                Instant earlier = continuous ? previous.since().get(key) : null;
                since.put(key, earlier == null ? now : earlier);
            }
            SortedSet<String> entries = snapshot.validEntries();
            state.set(new State(snapshot, status, new AtomicBoolean(), Map.copyOf(since), entries));
            logSetChange(previous.entries(), entries);
            if (status.unparsed() > 0 && status.valid() == 0) {
                // Indexes named pci_ exist and none parsed: a deparse change after a PostgreSQL upgrade looks
                // exactly like this, and routes every collection as if it had no index.
                log.warn("event=pci_sweep valid={} invalid={} unparsed={} warning=unparsed_indexes_and_no_valid_one",
                    status.valid(), status.invalid(), status.unparsed());
            } else {
                log.info("event=pci_sweep valid={} invalid={} unparsed={}",
                    status.valid(), status.invalid(), status.unparsed());
            }
            if (previous.expiryLogged().get()) {
                log.info("event=pci_sweep_set_recovered valid={} invalid={} unparsed={}",
                    status.valid(), status.invalid(), status.unparsed());
            }
            return true;
        } finally {
            readLock.unlock();
        }
    }

    /**
     * One line when the router's set changed between two reads, naming what was added and removed as
     * {@code leaf:collection:index}; silent when nothing changed. The lists are cut at {@value #MAX_LOGGED_ENTRIES}
     * entries (the first read after boot lists every index) and the counts are always exact.
     */
    private static void logSetChange(SortedSet<String> before, SortedSet<String> after) {
        SortedSet<String> added = new TreeSet<>(after);
        added.removeAll(before);
        SortedSet<String> removed = new TreeSet<>(before);
        removed.removeAll(after);
        if (added.isEmpty() && removed.isEmpty()) {
            return;
        }
        log.info("event=pci_sweep_set_changed added_count={} removed_count={} added={} removed={}", added.size(),
            removed.size(), bounded(added), bounded(removed));
    }

    private static String bounded(SortedSet<String> entries) {
        if (entries.isEmpty()) {
            return "-";
        }
        StringBuilder out = new StringBuilder();
        int n = 0;
        for (String entry : entries) {
            if (n == MAX_LOGGED_ENTRIES) {
                out.append(",...+").append(entries.size() - n);
                break;
            }
            if (n++ > 0) {
                out.append(',');
            }
            out.append(entry);
        }
        return out.toString();
    }

    /** Caller holds the read lock, so the state it replaces is the one it read. */
    private boolean failed(String reason, RuntimeException cause) {
        State previous = state.get();
        state.set(new State(previous.set(), previous.status().failedAt(clock.instant()), previous.expiryLogged(),
            previous.since(), previous.entries()));
        String detail = cause == null ? "the read saw no partition leaves of nexus.chunks"
            : String.valueOf(cause.getMessage() != null ? cause.getMessage() : cause.toString());
        if (isStopping()) {
            // The shutdown hook interrupts a read in flight; that is not an incident.
            log.debug("event=pci_sweep_read_failed reason={} during=shutdown", reason);
        } else {
            log.warn("event=pci_sweep_read_failed reason={} kept_valid={} consecutive_failures={} error=\"{}\"",
                reason, previous.status().valid(), previous.status().consecutiveFailures() + 1, detail, cause);
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
     * End the read task. A read in flight is interrupted, and this does not wait for it: a JDBC read on a silent
     * socket ignores the interrupt, and the caller (the shutdown hook) has a grace period to keep. Closing the pool
     * aborts that read. The last set stays readable. Harmless when never started or already stopped.
     */
    public void stop() {
        ScheduledExecutorService toStop;
        synchronized (lifecycle) {
            stopped = true;
            toStop = scheduler;
        }
        if (toStop != null) {
            toStop.shutdownNow();
        }
    }
}
