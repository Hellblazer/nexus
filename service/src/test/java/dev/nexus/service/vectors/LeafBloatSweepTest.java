// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.vectors.LeafBloatSweep.OverdueState;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-dxsjc: the overdue rule, and {@link LeafBloatSweep}'s duration, vacuum-count, failure and expiry rules, with a
 * scripted reader and a fixed clock. No database; {@link LeafBloatCatalogIntegrationTest} covers what the reader reads.
 */
class LeafBloatSweepTest {

    static final class MutableClock extends Clock {
        private volatile Instant now = Instant.parse("2026-10-19T00:00:00Z");

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override public java.time.ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(java.time.ZoneId zone) { return this; }
        @Override public Instant instant() { return now; }
    }

    static final Duration PERIOD = Duration.ofMinutes(10);
    static final Duration BOUND = Duration.ofSeconds(30);

    static LeafBloatCatalog.Leaf leaf(String name, long live, long dead) {
        return leaf(name, live, dead, 0);
    }

    static LeafBloatCatalog.Leaf leaf(String name, long live, long dead, long vacuums) {
        return new LeafBloatCatalog.Leaf(name, live, dead, null, vacuums, true, true, List.of());
    }

    /** A reader that returns the scripted answers in order: a Snapshot, or a RuntimeException to throw. */
    static Supplier<LeafBloatCatalog.Snapshot> script(Object... answers) {
        Deque<Object> queue = new ArrayDeque<>(List.of(answers));
        return () -> {
            Object next = queue.removeFirst();
            if (next instanceof RuntimeException e) throw e;
            return (LeafBloatCatalog.Snapshot) next;
        };
    }

    static LeafBloatCatalog.Snapshot snap(LeafBloatCatalog.Leaf... leaves) {
        return new LeafBloatCatalog.Snapshot(List.of(leaves));
    }

    static LeafBloatSweep.LeafStatus status(LeafBloatSweep sweep, String leaf) {
        return sweep.status().leaves().stream().filter(s -> s.leaf().name().equals(leaf)).findFirst().orElseThrow();
    }

    static OverdueState state(LeafBloatSweep sweep, String leaf) {
        return status(sweep, leaf).state();
    }

    // -- the rule: overdue, not a flat fraction ---------------------------------------------------------

    @Test
    void aSmallLeafAtThirtyPercentDeadIsNotOverdue_autovacuumHasNoReasonToRunOnIt() {
        var small = leaf("small", 70, 30);
        assertThat(small.deadFraction()).isEqualTo(0.3);
        assertThat(small.overdue()).as("30 dead of 100 rows is far under the 1000-row base").isFalse();
        assertThat(leaf("deleted-everything", 0, 50).overdue()).as("100 percent dead, 50 rows").isFalse();
        assertThat(leaf("mid", 5_000, 1_400).overdue()).as("22 percent of 6,400 rows: 1400 < 1000 + 640").isFalse();
    }

    @Test
    void aHundredThousandRowLeafAtTwelvePercentDeadIsOverdue() {
        assertThat(leaf("big", 88_000, 12_000).overdue()).as("12,000 > 1000 + 0.10 x 100,000").isTrue();
        assertThat(leaf("big", 90_000, 10_000).overdue()).as("10,000 < 11,000").isFalse();
    }

    @Test
    void theOverdueLineIsStrict_andMatchesTheDocumentedFormula() {
        // dead > 1000 + 0.10 x (live + dead); with 8000 live rows the line is exactly 2000 dead.
        assertThat(leaf("edge", 8_000, 2_000).overdue()).as("equal to the line is not over it").isFalse();
        assertThat(leaf("edge", 8_000, 2_001).overdue()).isTrue();
        assertThat(LeafBloatCatalog.OVERDUE_BASE_ROWS).isEqualTo(1000);
        assertThat(LeafBloatCatalog.OVERDUE_SCALE_FACTOR).isEqualTo(0.10);
    }

    // -- the sweep's per-leaf record --------------------------------------------------------------------

    @Test
    void anOverdueLeafWaitsUntilAVacuumFinishesOrTheDurationPasses_andAReadNotOverdueClearsIt() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(
            snap(leaf("a", 80_000, 20_000, 5), leaf("b", 100_000, 0, 5)),    // a overdue (first read)
            snap(leaf("a", 80_000, 21_000, 5), leaf("b", 100_000, 0, 5)),    // 10 min on, no vacuum: waiting
            snap(leaf("a", 80_000, 22_000, 5), leaf("b", 100_000, 0, 5)),    // 20 min: waiting
            snap(leaf("a", 80_000, 23_000, 5), leaf("b", 100_000, 0, 5)),    // 30 min: starved
            snap(leaf("a", 99_000, 1_000, 6), leaf("b", 100_000, 0, 5))),    // a vacuumed and clean: cleared
            PERIOD, clock, BOUND);

        assertThat(sweep.status().everRead()).isFalse();
        assertThat(sweep.status().leaves()).isEmpty();

        sweep.refresh();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.WAITING);
        assertThat(status(sweep, "a").overdueForSeconds()).isZero();
        assertThat(state(sweep, "b")).isEqualTo(OverdueState.NONE);
        clock.advance(PERIOD);
        sweep.refresh();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.WAITING);
        assertThat(status(sweep, "a").overdueForSeconds()).isEqualTo(600);
        clock.advance(PERIOD);
        sweep.refresh();
        assertThat(state(sweep, "a")).as("20 minutes, under the 30 minute wait").isEqualTo(OverdueState.WAITING);
        clock.advance(PERIOD);
        sweep.refresh();
        assertThat(LeafBloatSweep.OVERDUE_WARN_AFTER).isEqualTo(Duration.ofMinutes(30));
        assertThat(state(sweep, "a")).as("overdue for the full wait, no vacuum finished").isEqualTo(OverdueState.STARVED);
        assertThat(status(sweep, "a").persistent()).isTrue();
        assertThat(status(sweep, "a").vacuumsSinceOverdue()).isZero();
        clock.advance(PERIOD);
        sweep.refresh();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.NONE);
        assertThat(status(sweep, "a").overdueSince()).isNull();
        assertThat(status(sweep, "a").persistent()).isFalse();
    }

    @Test
    void aVacuumFinishedWhileTheLeafStaysOverdueIsPinned_atOnce_notAfterTheWait() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(
            snap(leaf("a", 80_000, 20_000, 5)),
            snap(leaf("a", 80_000, 20_500, 6))),     // one vacuum finished since, still overdue
            PERIOD, clock, BOUND);
        sweep.refresh();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.WAITING);
        clock.advance(PERIOD);
        sweep.refresh();
        assertThat(status(sweep, "a").overdueForSeconds()).as("far under the wait").isEqualTo(600);
        assertThat(status(sweep, "a").vacuumsSinceOverdue()).isEqualTo(1);
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.PINNED);
        assertThat(status(sweep, "a").persistent()).isTrue();
    }

    @Test
    void theDurationDoesNotDependOnThePeriod_aHourlySweepStarvesOnItsSecondRead() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(snap(leaf("a", 80_000, 20_000)), snap(leaf("a", 80_000, 20_000))),
            Duration.ofHours(1), clock, BOUND);
        sweep.refresh();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.WAITING);
        clock.advance(Duration.ofHours(1));
        sweep.refresh();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.STARVED);

        var fast = new LeafBloatSweep(script(snap(leaf("a", 80_000, 20_000)), snap(leaf("a", 80_000, 20_000))),
            Duration.ofMinutes(1), clock, BOUND);
        fast.refresh();
        clock.advance(Duration.ofMinutes(1));
        fast.refresh();
        assertThat(state(fast, "a")).as("two reads a minute apart prove nothing about 30 minutes")
            .isEqualTo(OverdueState.WAITING);
    }

    @Test
    void aStatisticsResetBelowTheBaselineIsNotAVacuum() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(
            snap(leaf("a", 80_000, 20_000, 40)),
            snap(leaf("a", 80_000, 20_000, 0)),      // the count went DOWN: statistics reset, no vacuum
            snap(leaf("a", 80_000, 20_000, 1))),     // then a real vacuum
            PERIOD, clock, BOUND);
        sweep.refresh();
        clock.advance(PERIOD);
        sweep.refresh();
        assertThat(status(sweep, "a").vacuumsSinceOverdue()).isZero();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.WAITING);
        clock.advance(PERIOD);
        sweep.refresh();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.PINNED);
    }

    @Test
    void aGapLongerThanTheViewSurvives_startsTheRunOver() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(snap(leaf("a", 80_000, 20_000)), snap(leaf("a", 80_000, 20_000))),
            PERIOD, clock, BOUND);
        sweep.refresh();
        clock.advance(PERIOD.multipliedBy(3).plus(BOUND).plusSeconds(1));
        assertThat(sweep.status().expired()).isTrue();
        sweep.refresh();
        assertThat(status(sweep, "a").overdueForSeconds()).as("the old reading is not carried across the outage")
            .isZero();
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.WAITING);
    }

    @Test
    void aFailedReadKeepsTheViewAndTheRecord_andCountsTheFailure() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(
            snap(leaf("a", 80_000, 20_000)),
            new IllegalStateException("boom"),
            snap(leaf("a", 80_000, 20_000))),
            PERIOD, clock, BOUND);

        assertThat(sweep.refresh()).isTrue();
        Instant firstRead = sweep.status().lastReadAt();
        clock.advance(PERIOD);
        assertThat(sweep.refresh()).isFalse();

        var s = sweep.status();
        assertThat(s.consecutiveFailures()).isEqualTo(1);
        assertThat(s.everRead()).isTrue();
        assertThat(s.lastFailureAt()).isNotNull();
        assertThat(s.lastReadAt()).as("the failed read moved nothing").isEqualTo(firstRead);
        assertThat(state(sweep, "a")).isEqualTo(OverdueState.WAITING);

        clock.advance(PERIOD);
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.status().consecutiveFailures()).isZero();
        assertThat(status(sweep, "a").overdueForSeconds()).as("a failed read between two overdue reads does not"
            + " break the run").isEqualTo(1200);
    }

    @Test
    void readsThatFailedFromBoot_leaveEverReadFalseAndCountTheFailures() {
        var sweep = new LeafBloatSweep(script(new IllegalStateException("x"), new IllegalStateException("y")),
            PERIOD, new MutableClock(), BOUND);
        sweep.refresh();
        sweep.refresh();
        var s = sweep.status();
        assertThat(s.everRead()).isFalse();
        assertThat(s.consecutiveFailures()).isEqualTo(2);
        assertThat(s.lastReadAt()).isNull();
        assertThat(s.leaves()).isEmpty();
    }

    @Test
    void aSnapshotWithNoLeavesIsAFailedRead_neverEvidenceOfNoBloat() {
        var sweep = new LeafBloatSweep(script(snap(leaf("a", 80_000, 20_000)), snap()),
            PERIOD, new MutableClock(), BOUND);
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.refresh()).isFalse();
        assertThat(sweep.status().leaves()).as("the previous view stands").hasSize(1);
        assertThat(sweep.status().consecutiveFailures()).isEqualTo(1);
    }

    @Test
    void theViewExpiresAfterThreePeriodsPlusTheReadBound_andRecoversOnTheNextRead() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(snap(leaf("a", 80_000, 20_000)), snap(leaf("a", 100, 0))),
            PERIOD, clock, BOUND);
        sweep.refresh();

        clock.advance(Duration.ofMinutes(30).plusSeconds(30));
        assertThat(sweep.status().expired()).as("exactly at the limit").isFalse();
        clock.advance(Duration.ofSeconds(1));
        assertThat(sweep.status().expired()).as("one second past").isTrue();
        assertThat(sweep.status().leaves()).as("the last read's figures are still there").hasSize(1);

        sweep.refresh();
        assertThat(sweep.status().expired()).isFalse();
    }

    // -- logging ----------------------------------------------------------------------------------------

    @Test
    void aPersistentLeafIsWarnedOnceOnTransition_notEveryPeriod_andClearedOnce() {
        var clock = new MutableClock();
        var sweep = new LeafBloatSweep(script(
            snap(leaf("a", 80_000, 20_000)),
            snap(leaf("a", 80_000, 20_000)),
            snap(leaf("a", 80_000, 20_000)),
            snap(leaf("a", 80_000, 20_000)),     // starved from here
            snap(leaf("a", 80_000, 20_000)),     // still starved: no second warning
            snap(leaf("a", 80_000, 20_000)),
            snap(leaf("a", 100_000, 0, 1))),     // clean
            PERIOD, clock, BOUND);
        var logs = new ListAppender<ILoggingEvent>();
        logs.start();
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(LeafBloatSweep.class);
        logger.addAppender(logs);
        try {
            for (int i = 0; i < 7; i++) {
                sweep.refresh();
                clock.advance(PERIOD);
            }
        } finally {
            logger.detachAppender(logs);
            logs.stop();
        }
        var warns = logs.list.stream().filter(e -> e.getLevel() == ch.qos.logback.classic.Level.WARN)
            .map(ILoggingEvent::getFormattedMessage).toList();
        assertThat(warns).as("one WARN for the transition into starved, none while it stays so").hasSize(1);
        assertThat(warns.get(0)).contains("leaf=a").contains("problem=starved");
        assertThat(logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.contains("chunks_leaf_bloat_leaf_cleared")).count()).isEqualTo(1);
    }

    // -- lifecycle --------------------------------------------------------------------------------------

    @Test
    void startReadsAtOnce_andStopEndsTheTaskAndItsThreadForGood() throws Exception {
        var sweep = new LeafBloatSweep(script(snap(leaf("a", 100, 0))), PERIOD, Clock.systemUTC(), BOUND);
        assertThat(sweep.isRunning()).isFalse();
        assertThat(sweepThreads()).as("no read thread before start").isZero();
        sweep.start();
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (!sweep.status().everRead() && System.nanoTime() < deadline) {
            Thread.sleep(20);
        }
        assertThat(sweep.status().everRead()).as("the first read ran at start").isTrue();
        assertThat(sweep.isRunning()).isTrue();
        assertThat(sweepThreads()).as("the scheduler thread exists while running (non-vacuity)").isEqualTo(1);
        sweep.start();   // idempotent
        assertThat(sweepThreads()).as("a second start made no second thread").isEqualTo(1);
        sweep.stop();
        sweep.stop();    // harmless twice
        assertThat(sweep.isRunning()).isFalse();
        deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (sweepThreads() > 0 && System.nanoTime() < deadline) {
            Thread.sleep(20);
        }
        assertThat(sweepThreads()).as("stop() ended the scheduler thread (shutdownNow interrupts the idle worker)")
            .isZero();
        assertThatThrownBy(sweep::start).isInstanceOf(IllegalStateException.class);
    }

    private static long sweepThreads() {
        return Thread.getAllStackTraces().keySet().stream()
            .filter(t -> LeafBloatSweep.THREAD_NAME.equals(t.getName()) && t.isAlive()).count();
    }

    @Test
    void aNonPositivePeriodIsRefused() {
        assertThatThrownBy(() -> new LeafBloatSweep(script(), Duration.ZERO, Clock.systemUTC(), Duration.ofSeconds(1)))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
