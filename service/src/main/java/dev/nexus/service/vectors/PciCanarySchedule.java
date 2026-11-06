// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciAnchorReads.LeafStats;
import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCanary.Target;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.LongSupplier;

/**
 * When the recall canary runs (RDR-230 Technical Design 5, bead nexus-4lnn1.1.4), and what the engine remembers of it.
 * One schedule belongs to one reconciler; the reconciler calls it on the builder-lock holder's pass only, so every
 * method but {@link #results()} and {@link #latest} is called from the pass thread.
 *
 * <p><b>Triggers</b> (the strongest pending one labels the run):
 * <ol>
 *   <li>{@code after_build}: {@link #built} after a successful build of the index.</li>
 *   <li>{@code vacuum}: {@link #observeLeaf} sees the leaf's {@code last_vacuum} or {@code last_autovacuum} advance
 *       since this engine last looked AND its deletes or non-HOT updates advance over the same interval, at most once
 *       per index per {@link PciCanaryPolicy#VACUUM_MIN_INTERVAL}. An insert-only vacuum reaps nothing and repairs no
 *       graph (nexus-s9wav makes autovacuum run on inserts), so the timestamp alone would run the canary on ingest. The
 *       first look at a leaf after boot records the baseline and triggers nothing.</li>
 *   <li>{@code daily}: the index's last run on this engine is older than {@link PciCanaryPolicy#STALE_AFTER}, or it has
 *       none (a boot, a lock handed over, an index adopted rather than built).</li>
 * </ol>
 * A trigger is held until a run that is neither {@code no_queries} nor {@code error} consumes it. A trigger that finds
 * too few ring queries stays <i>pending</i>, in memory, and the index runs on the first pass that has them. An
 * {@code error} (a permit wait that timed out under load, a ring read that blipped, a statement timeout) also leaves
 * the trigger pending and stamps no last-run time, but the index is not tried again for
 * {@link PciCanaryPolicy#ERROR_RETRY_FLOOR}, doubled for each further consecutive error and capped at
 * {@link PciCanaryPolicy#ERROR_RETRY_CAP} (reset by any other result and by a build), so a failing statement costs one
 * run per wait and not one per pass, a deterministic error backs off to one run a day, and a transient error does not
 * cost the after-build reading a day. {@code index_not_used} is a run: the planner will not
 * change its mind by the next pass, so it consumes the trigger and stamps the last-run time, and the index is tried
 * again at the next trigger or in a day.
 *
 * <p><b>Budget.</b> {@link #runPass} stops starting runs once the pass has spent {@link PciCanaryPolicy#PASS_BUDGET}
 * on them (the run in flight finishes) and carries the rest to the next pass: their triggers are kept, and the order is
 * after_build, then vacuum, then daily, oldest last run first, so a carried index is ahead of a fresh one next time.
 *
 * <p><b>Memory.</b> Per index: the last run's time, the pending trigger, the vacuum trigger's time, the earliest time
 * an errored run may be retried, and the latest {@link Result} (a red result keeps its query sample). Per leaf: the
 * vacuum baseline and whether deletes or non-HOT updates have been seen since the last vacuum looked at. All lost on
 * restart.
 */
final class PciCanarySchedule {

    private static final Logger log = LoggerFactory.getLogger(PciCanarySchedule.class);

    static final String AFTER_BUILD = "after_build";
    static final String VACUUM = "vacuum";
    static final String DAILY = "daily";

    /** Runs the canary on one index; {@link PciCanary} is the production one. */
    @FunctionalInterface
    interface Runner {
        Result run(Target target, String trigger);
    }

    /** What one {@link #runPass} did. */
    record PassReport(int ran, int carried, int pendingForQueries) {
        static final PassReport NONE = new PassReport(0, 0, 0);
    }

    private static final class IndexState {
        volatile Instant lastRun;
        volatile Instant lastVacuumTrigger;
        /** After an errored run: no new attempt before this. Cleared by any other result and by a build. */
        volatile Instant retryNotBefore;
        /** Consecutive errored runs; sets the length of the wait. Cleared by any other result and by a build. */
        volatile int errorStreak;
        volatile String pending;
        volatile Result latest;
    }

    /**
     * What the engine remembers of one leaf. The counters are those of the last look. {@code reapable} is true when a
     * look saw deletes or non-HOT updates advance and no vacuum has been seen since, so a vacuum that finishes a pass
     * after the deletes still counts as having reaped them (a leaf vacuum takes minutes, a pass is ten).
     */
    private record Baseline(Instant vacuumedAt, long deleted, long nonHotUpdated, boolean reapable) { }

    private final Runner runner;
    private final Clock clock;
    private final LongSupplier nanos;
    private final Map<String, IndexState> states = new ConcurrentHashMap<>();
    private final Map<String, Baseline> baselines = new ConcurrentHashMap<>();

    PciCanarySchedule(Runner runner) {
        this(runner, Clock.systemUTC(), System::nanoTime);
    }

    PciCanarySchedule(Runner runner, Clock clock, LongSupplier nanos) {
        this.runner = Objects.requireNonNull(runner, "runner");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.nanos = Objects.requireNonNull(nanos, "nanos");
    }

    private IndexState state(String index) {
        return states.computeIfAbsent(index, k -> new IndexState());
    }

    /** The index was just built: run it at the next opportunity, whatever else is pending. */
    void built(String index) {
        IndexState s = state(index);
        s.pending = AFTER_BUILD;
        s.retryNotBefore = null;   // a new index owes nothing to the old one's error
        s.errorStreak = 0;
    }

    /** The index was dropped: forget it. */
    void dropped(String index) {
        states.remove(index);
    }

    /**
     * The leaf's statistics, read in a committed counting transaction of the pass. Arms the {@code vacuum} trigger of
     * each of the leaf's valid indexes when the leaf was vacuumed and had deletes or non-HOT updates since the last look.
     *
     * @param indexes the names of the leaf's valid indexes the pass is not dropping
     */
    void observeLeaf(String leaf, Collection<String> indexes, LeafStats stats) {
        Instant vacuumedAt = latest(stats.lastVacuum() == null ? null : stats.lastVacuum().toInstant(),
            stats.lastAutovacuum() == null ? null : stats.lastAutovacuum().toInstant());
        Baseline before = baselines.get(leaf);
        if (before == null) {
            // the first look after a boot records and does not trigger
            baselines.put(leaf, new Baseline(vacuumedAt, stats.deleted(), stats.nonHotUpdated(), false));
            return;
        }
        boolean vacuumed = vacuumedAt.isAfter(before.vacuumedAt());
        boolean advanced = stats.deleted() > before.deleted() || stats.nonHotUpdated() > before.nonHotUpdated();
        boolean reaped = before.reapable() || advanced;
        // The counters move to this look's; the carry survives until a vacuum is seen, then it is spent.
        baselines.put(leaf, new Baseline(vacuumedAt, stats.deleted(), stats.nonHotUpdated(), reaped && !vacuumed));
        if (!vacuumed || !reaped) {
            return;
        }
        Instant now = clock.instant();
        for (String index : indexes) {
            IndexState s = state(index);
            Instant last = s.lastVacuumTrigger;
            if (last != null && now.isBefore(last.plus(PciCanaryPolicy.VACUUM_MIN_INTERVAL))) {
                continue;
            }
            s.lastVacuumTrigger = now;
            if (s.pending == null || DAILY.equals(s.pending)) {
                s.pending = VACUUM;
            }
        }
    }

    private static Instant latest(Instant a, Instant b) {
        if (a == null && b == null) {
            return Instant.EPOCH;
        }
        if (a == null) {
            return b;
        }
        return b == null || a.isAfter(b) ? a : b;
    }

    /**
     * Run what is due, within the pass budget. {@code targets} is every valid index the pass knows, including those it
     * built; an index not in it is forgotten.
     */
    PassReport runPass(List<Target> targets) {
        Set<String> present = new HashSet<>();
        for (Target t : targets) {
            present.add(t.index());
        }
        states.keySet().removeIf(name -> !present.contains(name));
        Instant now = clock.instant();
        List<Due> due = new ArrayList<>();
        for (Target t : targets) {
            IndexState s = state(t.index());
            if (s.retryNotBefore != null && now.isBefore(s.retryNotBefore)) {
                continue;   // an errored run: not again before the floor
            }
            String trigger = s.pending;
            if (trigger == null && (s.lastRun == null
                || now.isAfter(s.lastRun.plus(PciCanaryPolicy.STALE_AFTER)))) {
                trigger = DAILY;
            }
            if (trigger != null) {
                due.add(new Due(t, trigger, s));
            }
        }
        if (due.isEmpty()) {
            return PassReport.NONE;
        }
        due.sort(Comparator.<Due>comparingInt(d -> rank(d.trigger))
            .thenComparing(d -> d.state.lastRun == null ? Instant.EPOCH : d.state.lastRun));
        long startedNanos = nanos.getAsLong();
        int ran = 0;
        int waiting = 0;
        int i = 0;
        for (; i < due.size(); i++) {
            if (i > 0 && nanos.getAsLong() - startedNanos >= PciCanaryPolicy.PASS_BUDGET.toNanos()) {
                break;
            }
            Due d = due.get(i);
            Result r = runner.run(d.target, d.trigger);
            ran++;
            if (record(d, r)) {
                waiting++;
            }
        }
        int carried = due.size() - i;
        if (carried > 0) {
            log.info("event=pci_canary_carried carried={} ran={} budget_ms={}", carried, ran,
                PciCanaryPolicy.PASS_BUDGET.toMillis());
        }
        return new PassReport(ran, carried, waiting);
    }

    private record Due(Target target, String trigger, IndexState state) { }

    private static int rank(String trigger) {
        return switch (trigger) {
            case AFTER_BUILD -> 0;
            case VACUUM -> 1;
            default -> 2;
        };
    }

    /** @return true when the result left the trigger pending for want of queries */
    private boolean record(Due d, Result r) {
        IndexState s = d.state;
        Result previous = s.latest;
        s.latest = r;
        boolean noQueries = r.state() == State.UNMEASURED && PciCanary.NO_QUERIES.equals(r.reason());
        if (noQueries) {
            s.errorStreak = 0;
            s.retryNotBefore = null;
            s.pending = d.trigger;
            boolean repeat = previous != null && previous.state() == State.UNMEASURED
                && PciCanary.NO_QUERIES.equals(previous.reason());
            if (repeat) {
                log.debug("{}", PciCanary.line(r));
            } else {
                log.info("{}", PciCanary.line(r));
            }
            return true;
        }
        if (r.state() == State.UNMEASURED && PciCanary.ERROR.equals(r.reason())) {
            // Not a run: the trigger stays, no last-run time is stamped, and the wait (doubling per consecutive error,
            // capped) keeps a failing index from running on every pass.
            s.pending = d.trigger;
            int streak = s.errorStreak + 1;
            s.errorStreak = streak;
            s.retryNotBefore = clock.instant().plus(PciCanaryPolicy.errorRetryAfter(streak));
            log.warn("{}", PciCanary.line(r));
            return false;
        }
        s.pending = null;
        s.retryNotBefore = null;
        s.errorStreak = 0;
        s.lastRun = clock.instant();
        if (r.state() == State.RED) {
            log.warn("{}", PciCanary.line(r));
        } else {
            log.info("{}", PciCanary.line(r));
        }
        return false;
    }

    /** The latest result of the index on this engine, or null. */
    Result latest(String index) {
        IndexState s = states.get(index);
        return s == null ? null : s.latest;
    }

    /** The latest result of every index that has one, by index name. */
    Map<String, Result> results() {
        Map<String, Result> out = new java.util.TreeMap<>();
        states.forEach((name, s) -> {
            Result r = s.latest;
            if (r != null) {
                out.put(name, r);
            }
        });
        return out;
    }

    /** The trigger the index holds pending, or null; for tests. */
    String pending(String index) {
        IndexState s = states.get(index);
        return s == null ? null : s.pending;
    }
}
