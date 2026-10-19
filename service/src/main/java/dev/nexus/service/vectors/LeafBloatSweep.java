// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

/**
 * nexus-dxsjc (nexus-4lnn1 R4): fills and serves the cache behind {@code GET /v1/status}'s {@code chunks_leaf_bloat}.
 * RDR-227 forbids a catalog query on the status route, so the route reads {@link #status()} and a task of its own
 * does the reading: one {@link LeafBloatCatalog#read()} at start and one a period after each read ends, the period
 * being {@code NX_SEARCH_PCI_SWEEP_SECONDS} (the same cadence as the per-collection index reads; no new setting).
 *
 * <p><b>Isolated from the router.</b> This is a second sweep beside {@link PciIndexSweep}, not a field of its
 * snapshot: a statistics read that fails or hangs must not be able to make the router's index set look failed or
 * expired, and the router's set must not wait on a statistics read.
 *
 * <p><b>The overdue guard is a duration and a vacuum count, not a read count.</b> {@code n_dead_tup} is an estimate and
 * autovacuum is cost-throttled, so a leaf in the middle of a vacuum can be overdue
 * ({@link LeafBloatCatalog.Leaf#overdue()}) for a while. For each leaf this sweep remembers when it first read overdue
 * ({@link LeafStatus#overdueSince()}) and the leaf's vacuum count at that moment. On a later read where the leaf is
 * still overdue:
 * <ul>
 * <li>{@link OverdueState#PINNED}: a vacuum has finished since the leaf first read overdue, so the estimate is fresh
 *     and the dead rows are still there (a long transaction, an idle-in-transaction session or a replication slot is
 *     holding back the oldest xmin). Warned at once.</li>
 * <li>{@link OverdueState#STARVED}: no vacuum has finished and the leaf has been overdue for at least
 *     {@link #OVERDUE_WARN_AFTER}: autovacuum is not reaching it (disabled, queued behind other work, or throttled).</li>
 * <li>{@link OverdueState#WAITING}: overdue for less than that with no vacuum finished: a vacuum may be running.</li>
 * </ul>
 * The duration is measured on the clock from the first overdue read, so it does not move when
 * {@code NX_SEARCH_PCI_SWEEP_SECONDS} is retuned. A read where the leaf is not overdue clears the record; so does a gap
 * longer than the view survives (the old reading says nothing about the new one).
 *
 * <p><b>A failed read keeps the previous view</b> and counts toward {@link Status#consecutiveFailures()}. The view
 * expires, as {@link PciIndexSweep}'s does, once {@code now - lastReadAt} exceeds {@code 3 * period + readBound}; the
 * counts are still the last read's, and {@link Status#expired()} tells the reader not to trust them.
 */
public final class LeafBloatSweep {

    private static final Logger log = LoggerFactory.getLogger(LeafBloatSweep.class);

    /** Name of the scheduler thread; the stop test and a thread dump both look for it. */
    static final String THREAD_NAME = "leaf-bloat-read";

    /** Sweep periods without a successful read the view survives. */
    static final int EXPIRY_PERIODS = 3;

    /** How long an overdue leaf with no vacuum finished is waited for before the row warns. */
    public static final Duration OVERDUE_WARN_AFTER = Duration.ofMinutes(30);

    /** Where an overdue leaf stands; {@link #wire()} is the spelling on {@code /v1/status}. */
    public enum OverdueState {
        /** Not overdue. */
        NONE,
        /** Overdue, no vacuum finished since, for less than {@link #OVERDUE_WARN_AFTER}. */
        WAITING,
        /** Overdue, and a vacuum finished since it became so: something holds the dead rows. */
        PINNED,
        /** Overdue for {@link #OVERDUE_WARN_AFTER} or more with no vacuum finished since. */
        STARVED;

        public String wire() {
            return name().toLowerCase(java.util.Locale.ROOT);
        }
    }

    /**
     * A leaf and its overdue record.
     *
     * @param leaf            what the last read saw
     * @param overdueSince    when the leaf first read overdue in the current run of overdue reads; {@code null} when
     *                        the last read found it not overdue
     * @param vacuumBaseline  the leaf's {@code vacuumCount} on that first overdue read (the current count when the
     *                        leaf is not overdue)
     * @param overdueForSeconds seconds from {@code overdueSince} to the read that produced this status; 0 when not
     *                        overdue
     */
    public record LeafStatus(LeafBloatCatalog.Leaf leaf, Instant overdueSince, long vacuumBaseline,
                             long overdueForSeconds) {

        /** A leaf that is not overdue. */
        public static LeafStatus clean(LeafBloatCatalog.Leaf leaf) {
            return new LeafStatus(leaf, null, leaf.vacuumCount(), 0);
        }

        public boolean overdue() {
            return overdueSince != null;
        }

        /** Vacuums finished since the leaf first read overdue; 0 after a reset of the statistics. */
        public long vacuumsSinceOverdue() {
            return overdue() ? Math.max(0, leaf.vacuumCount() - vacuumBaseline) : 0;
        }

        public OverdueState state() {
            if (!overdue()) {
                return OverdueState.NONE;
            }
            if (vacuumsSinceOverdue() > 0) {
                return OverdueState.PINNED;
            }
            return overdueForSeconds >= OVERDUE_WARN_AFTER.toSeconds() ? OverdueState.STARVED : OverdueState.WAITING;
        }

        /** The row warns on this leaf for bloat: pinned or starved. */
        public boolean persistent() {
            OverdueState state = state();
            return state == OverdueState.PINNED || state == OverdueState.STARVED;
        }
    }

    /**
     * @param everRead            whether any read has succeeded since boot
     * @param lastReadAt          when the last successful read finished, or {@code null}
     * @param lastFailureAt       when the last failed read finished, or {@code null}
     * @param consecutiveFailures failed reads since the last successful one
     * @param expired             the view is older than {@code 3 * period + readBound}
     * @param leaves              every leaf the last read saw, in name order; empty before the first read
     */
    public record Status(boolean everRead, Instant lastReadAt, Instant lastFailureAt, int consecutiveFailures,
                         boolean expired, List<LeafStatus> leaves) {
        static final Status NEVER = new Status(false, null, null, 0, false, List.of());

        public Status {
            leaves = List.copyOf(leaves);
        }
    }

    private final Supplier<LeafBloatCatalog.Snapshot> reader;
    private final Duration period;
    private final Duration expiryAfter;
    private final Clock clock;
    private final Object lifecycle = new Object();
    private volatile Status current = Status.NEVER;    // replaced whole; written under refresh()'s monitor
    private Map<String, String> loggedProblems = Map.of();   // leaf -> the problem last logged; refresh()'s monitor
    private ScheduledExecutorService scheduler;         // guarded by lifecycle
    private boolean stopped;                            // guarded by lifecycle

    /** The production sweep: reads through {@code ds} every {@code settings.sweepSeconds()}. */
    public static LeafBloatSweep create(javax.sql.DataSource ds, PciSettings settings) {
        LeafBloatCatalog catalog = new LeafBloatCatalog(ds);
        return new LeafBloatSweep(catalog::read,
            Duration.ofSeconds(Objects.requireNonNull(settings, "settings").sweepSeconds()), Clock.systemUTC(),
            catalog.readBound());
    }

    /** As {@link #create} with the reader, period and clock named, for tests. */
    LeafBloatSweep(Supplier<LeafBloatCatalog.Snapshot> reader, Duration period, Clock clock, Duration readBound) {
        this.reader = Objects.requireNonNull(reader, "reader");
        this.period = Objects.requireNonNull(period, "period");
        this.clock = Objects.requireNonNull(clock, "clock");
        Objects.requireNonNull(readBound, "readBound");
        if (period.isZero() || period.isNegative()) {
            throw new IllegalArgumentException("period must be positive, got " + period);
        }
        this.expiryAfter = period.multipliedBy(EXPIRY_PERIODS).plus(readBound);
    }

    /** The sweep period in whole seconds, for the status object. */
    public long periodSeconds() {
        return period.toSeconds();
    }

    /** The last successful read, with its expiry judged now. From memory: no JDBC. */
    public Status status() {
        Status s = current;
        boolean expired = s.lastReadAt() != null
            && Duration.between(s.lastReadAt(), clock.instant()).compareTo(expiryAfter) > 0;
        return expired == s.expired() ? s
            : new Status(s.everRead(), s.lastReadAt(), s.lastFailureAt(), s.consecutiveFailures(), expired, s.leaves());
    }

    /**
     * Read now and, if the read succeeds, replace the view. Reads are serialised. A snapshot with no leaf is a
     * failed read (no installed schema has none), never evidence that nothing is bloated.
     *
     * @return true when the view was replaced
     */
    public synchronized boolean refresh() {
        LeafBloatCatalog.Snapshot snapshot;
        try {
            snapshot = reader.get();
        } catch (RuntimeException e) {
            return failed("read_error", e);
        }
        if (snapshot == null || snapshot.leaves().isEmpty()) {
            return failed("no_partition_leaves", null);
        }
        Status previous = current;
        Instant now = clock.instant();
        // A reading older than the view survives says nothing about this one: start every run of overdue reads over.
        boolean usable = previous.lastReadAt() != null
            && Duration.between(previous.lastReadAt(), now).compareTo(expiryAfter) <= 0;
        Map<String, LeafStatus> before = new HashMap<>();
        if (usable) {
            for (LeafStatus l : previous.leaves()) {
                before.put(l.leaf().name(), l);
            }
        }
        List<LeafStatus> leaves = new ArrayList<>(snapshot.leaves().size());
        Map<String, String> problems = new HashMap<>();
        int overdue = 0;
        int pinned = 0;
        int starved = 0;
        int unset = 0;
        int disabled = 0;
        for (LeafBloatCatalog.Leaf leaf : snapshot.leaves()) {
            LeafStatus prior = before.get(leaf.name());
            LeafStatus status;
            if (!leaf.overdue()) {
                status = LeafStatus.clean(leaf);
            } else {
                boolean continuing = prior != null && prior.overdue();
                Instant since = continuing ? prior.overdueSince() : now;
                // A smaller count than the baseline means the statistics were reset: the baseline follows it down.
                long baseline = continuing ? Math.min(prior.vacuumBaseline(), leaf.vacuumCount()) : leaf.vacuumCount();
                status = new LeafStatus(leaf, since, baseline, Duration.between(since, now).toSeconds());
            }
            leaves.add(status);
            overdue += status.overdue() ? 1 : 0;
            pinned += status.state() == OverdueState.PINNED ? 1 : 0;
            starved += status.state() == OverdueState.STARVED ? 1 : 0;
            unset += leaf.policySet() ? 0 : 1;
            disabled += leaf.autovacuumEnabled() ? 0 : 1;
            String problem = problemKey(status);
            if (!problem.isEmpty()) {
                problems.put(leaf.name(), problem);
            }
        }
        current = new Status(true, now, previous.lastFailureAt(), 0, false, leaves);
        log.info("event=chunks_leaf_bloat leaves={} overdue={} pinned={} starved={} policy_unset={} "
            + "autovacuum_disabled={}", leaves.size(), overdue, pinned, starved, unset, disabled);
        logTransitions(problems, leaves);
        return true;
    }

    /** The reasons a leaf warrants a warning, comma-joined; empty for a leaf that does not. */
    private static String problemKey(LeafStatus status) {
        var out = new ArrayList<String>(3);
        if (status.persistent()) {
            out.add(status.state().wire());
        }
        if (!status.leaf().policySet()) {
            out.add("policy_unset");
        }
        if (!status.leaf().autovacuumEnabled()) {
            out.add("autovacuum_disabled");
        }
        return String.join(",", out);
    }

    /** One WARN per leaf when its problem appears or changes, one INFO when it clears; silence in between. */
    private void logTransitions(Map<String, String> problems, List<LeafStatus> leaves) {
        for (LeafStatus l : leaves) {
            String name = l.leaf().name();
            String problem = problems.get(name);
            if (problem != null && !problem.equals(loggedProblems.get(name))) {
                log.warn("event=chunks_leaf_bloat_leaf leaf={} problem={} dead_rows={} live_rows={} planner_rows={} dead_fraction={} "
                        + "vacuum_count={} overdue_for_seconds={} vacuums_since_overdue={}", name, problem,
                    l.leaf().deadRows(), l.leaf().liveRows(), l.leaf().plannerRows(), l.leaf().deadFraction(),
                    l.leaf().vacuumCount(),
                    l.overdueForSeconds(), l.vacuumsSinceOverdue());
            }
        }
        for (String name : loggedProblems.keySet()) {
            if (!problems.containsKey(name)) {
                log.info("event=chunks_leaf_bloat_leaf_cleared leaf={}", name);
            }
        }
        loggedProblems = problems;
    }

    private boolean failed(String reason, RuntimeException cause) {
        Status previous = current;
        current = new Status(previous.everRead(), previous.lastReadAt(), clock.instant(),
            previous.consecutiveFailures() + 1, previous.expired(), previous.leaves());
        boolean stopping;
        synchronized (lifecycle) {
            stopping = stopped;
        }
        if (stopping) {
            log.debug("event=chunks_leaf_bloat_read_failed reason={} during=shutdown", reason);
        } else {
            log.warn("event=chunks_leaf_bloat_read_failed reason={} consecutive_failures={} error=\"{}\"", reason,
                previous.consecutiveFailures() + 1,
                cause == null ? "the read saw no leaf of nexus.chunks" : String.valueOf(cause.getMessage()), cause);
        }
        return false;
    }

    /** Start the read task: one read at once, then one {@code period} after each read ends. Idempotent. */
    public void start() {
        synchronized (lifecycle) {
            if (stopped) {
                throw new IllegalStateException("the leaf bloat read task was stopped and does not restart");
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
        log.info("event=chunks_leaf_bloat_read_started period_seconds={}", period.toSeconds());
    }

    /** A thrown task is never rescheduled by the executor, so nothing may escape this method. */
    private void scheduledRead() {
        try {
            refresh();
        } catch (Throwable t) {
            log.error("event=chunks_leaf_bloat_read_task_error error=\"{}\"", t.toString(), t);
        }
    }

    /** True between {@link #start()} and {@link #stop()}. */
    public boolean isRunning() {
        synchronized (lifecycle) {
            return scheduler != null && !stopped;
        }
    }

    /** End the read task without waiting for a read in flight (closing the pool aborts it). Harmless twice. */
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
