// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciAnchorReads.LeafStats;
import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCanary.Target;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 P1.3 (nexus-4lnn1.1.4): when the canary runs. Fake runner, injected clock and injected monotonic clock, so
 * every trigger and the pass budget are decided by the test and not by sleeping. The substrate test with a real
 * VACUUM is {@code PciReconcilerIntegrationTest}.
 */
class PciCanaryScheduleTest {

    static final Instant T0 = Instant.parse("2026-11-05T00:00:00Z");

    static final class Fake extends Clock {
        volatile Instant now = T0;

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override public Instant instant() {
            return now;
        }
    }

    /** Records each run; the state of the result for an index is scripted. */
    static final class Recorder implements PciCanarySchedule.Runner {
        final List<String> runs = new ArrayList<>();
        final List<String> triggers = new ArrayList<>();
        final Fake clock;
        final AtomicLong nanos;
        long costNanos;
        int queries = 10;
        /** When non-null, every run ends unmeasured for this reason (error, index_not_used). */
        String unmeasuredReason;

        Recorder(Fake clock, AtomicLong nanos) {
            this.clock = clock;
            this.nanos = nanos;
        }

        @Override
        public Result run(Target target, String trigger) {
            runs.add(target.index());
            triggers.add(trigger);
            nanos.addAndGet(costNanos);
            if (unmeasuredReason != null) {
                return new Result(target, trigger, State.UNMEASURED, unmeasuredReason, queries, null, null, 0,
                    clock.instant(), null, null);
            }
            if (queries < PciCanaryPolicy.MIN_QUERIES) {
                return new Result(target, trigger, State.UNMEASURED, PciCanary.NO_QUERIES, queries, null, null, 0,
                    clock.instant(), null, null);
            }
            return new Result(target, trigger, State.GREEN, null, queries, 1.0, 1.0, 0, clock.instant(), null, null);
        }
    }

    final Fake clock = new Fake();
    final AtomicLong nanos = new AtomicLong();
    final Recorder recorder = new Recorder(clock, nanos);
    final PciCanarySchedule schedule = new PciCanarySchedule(recorder, clock, nanos::get);

    static Target target(String index) {
        return new Target("nexus.leaf1", "t", "m", "coll-" + index, index);
    }

    static LeafStats stats(long deleted, long updated, long hot, Instant vacuum, Instant autovacuum) {
        return new LeafStats(deleted, updated, hot, 1000,
            vacuum == null ? null : OffsetDateTime.ofInstant(vacuum, ZoneOffset.UTC),
            autovacuum == null ? null : OffsetDateTime.ofInstant(autovacuum, ZoneOffset.UTC));
    }

    // -- daily -------------------------------------------------------------------------------------------

    @Test
    void anIndexWithNoRunOnThisEngine_runsAsDaily_thenNotAgainUntilADayHasPassed() {
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).containsExactly("a");
        assertThat(recorder.triggers).containsExactly(PciCanarySchedule.DAILY);

        clock.advance(Duration.ofHours(23));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("23 hours: not due").hasSize(1);

        clock.advance(Duration.ofHours(1).plusSeconds(1));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("older than 24 hours").hasSize(2);
    }

    // -- after_build ----------------------------------------------------------------------------------------

    @Test
    void aBuild_runsTheIndexAtOnce_afterBuild_evenWithAFreshRun() {
        schedule.runPass(List.of(target("a")));
        recorder.runs.clear();
        recorder.triggers.clear();

        schedule.built("a");
        schedule.runPass(List.of(target("a")));

        assertThat(recorder.runs).containsExactly("a");
        assertThat(recorder.triggers).containsExactly(PciCanarySchedule.AFTER_BUILD);
    }

    // -- vacuum (test 7) ------------------------------------------------------------------------------------

    @Test
    void theFirstLookAtALeaf_recordsTheBaseline_andTriggersNothing() {
        schedule.runPass(List.of(target("a")));
        recorder.runs.clear();

        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(10, 0, 0, null, T0));
        schedule.runPass(List.of(target("a")));

        assertThat(recorder.runs).isEmpty();
        assertThat(schedule.pending("a")).isNull();
    }

    @Test
    void anInsertOnlyVacuum_doesNotTrigger_aVacuumWithDeletesDoes_aVacuumWithNonHotUpdatesDoes() {
        schedule.runPass(List.of(target("a")));
        recorder.runs.clear();
        recorder.triggers.clear();
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(10, 5, 5, null, T0));

        // Autovacuum advanced; no delete, no non-HOT update (updates all HOT): an insert-only vacuum.
        clock.advance(Duration.ofMinutes(10));
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(10, 9, 9, null, T0.plusSeconds(600)));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("insert-only vacuum").isEmpty();
        assertThat(schedule.pending("a")).isNull();

        // Deletes advanced but no vacuum: nothing.
        clock.advance(Duration.ofMinutes(10));
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(50, 9, 9, null, T0.plusSeconds(600)));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("deletes with no vacuum").isEmpty();

        // Vacuum advanced and deletes advanced over the interval: triggers.
        clock.advance(Duration.ofMinutes(10));
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(80, 9, 9, null, T0.plusSeconds(1800)));
        assertThat(schedule.pending("a")).isEqualTo(PciCanarySchedule.VACUUM);
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).containsExactly("a");
        assertThat(recorder.triggers).containsExactly(PciCanarySchedule.VACUUM);
    }

    @Test
    void aVacuumWithNonHotUpdates_triggers_andLastVacuumCountsAsWellAsLastAutovacuum() {
        schedule.runPass(List.of(target("a")));
        recorder.runs.clear();
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(0, 5, 5, null, null));

        clock.advance(Duration.ofMinutes(10));
        // last_vacuum (a manual VACUUM) advanced; 20 updates, 5 of them HOT: 15 non-HOT.
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(0, 25, 10, T0.plusSeconds(300), null));

        assertThat(schedule.pending("a")).isEqualTo(PciCanarySchedule.VACUUM);
    }

    @Test
    void theVacuumTrigger_firesAtMostOncePerIndexPerSixHours() {
        schedule.runPass(List.of(target("a")));
        recorder.runs.clear();
        long deleted = 0;
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(deleted, 0, 0, null, T0));

        int fired = 0;
        for (int i = 1; i <= 12; i++) {   // a qualifying vacuum every 30 minutes for six hours
            clock.advance(Duration.ofMinutes(30));
            deleted += 10;
            schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(deleted, 0, 0, null, clock.instant()));
            schedule.runPass(List.of(target("a")));
            fired = recorder.runs.size();
        }
        assertThat(fired).as("six hours of qualifying vacuums, one run").isEqualTo(1);

        clock.advance(Duration.ofMinutes(31));
        deleted += 10;
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(deleted, 0, 0, null, clock.instant()));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("past six hours since the first trigger").hasSize(2);
    }

    @Test
    void aStatisticsReset_isNotAnAdvance() {
        schedule.runPass(List.of(target("a")));
        recorder.runs.clear();
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(500, 100, 0, null, T0));

        clock.advance(Duration.ofMinutes(10));
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(3, 1, 0, null, T0.plusSeconds(500)));

        assertThat(schedule.pending("a")).isNull();
    }

    @Test
    void aDeleteInOnePass_thenTheVacuumInTheNext_stillTriggers_andTheCarryClearsWithTheTrigger() {
        schedule.runPass(List.of(target("a")));
        recorder.runs.clear();
        recorder.triggers.clear();
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(10, 0, 0, null, T0));

        // Pass N: deletes land, the vacuum has not finished (a leaf vacuum takes minutes, a pass is ten).
        clock.advance(Duration.ofMinutes(10));
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(60, 0, 0, null, T0));
        assertThat(schedule.pending("a")).as("deletes alone trigger nothing").isNull();

        // Pass N+1: the vacuum that reaped them completes; the counters have not moved since the last look.
        clock.advance(Duration.ofMinutes(10));
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(60, 0, 0, null, T0.plusSeconds(1200)));
        assertThat(schedule.pending("a")).as("the vacuum reaped what pass N saw").isEqualTo(PciCanarySchedule.VACUUM);
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.triggers).containsExactly(PciCanarySchedule.VACUUM);

        // The carry is spent: a later insert-only vacuum, past the six-hour floor, does not trigger.
        clock.advance(Duration.ofHours(7));
        schedule.observeLeaf("nexus.leaf1", List.of("a"), stats(60, 0, 0, null, T0.plusSeconds(1200 + 7 * 3600)));
        assertThat(schedule.pending("a")).as("no delete since the vacuum that triggered").isNull();
    }

    // -- pending ----------------------------------------------------------------------------------------------

    @Test
    void aRunWithTooFewQueries_staysPending_andRunsOnTheFirstPassThatHasThem() {
        recorder.queries = 2;
        schedule.built("a");
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).hasSize(1);
        assertThat(schedule.pending("a")).as("held, with the trigger that asked").isEqualTo(PciCanarySchedule.AFTER_BUILD);
        assertThat(schedule.latest("a").reason()).isEqualTo("no_queries");
        assertThat(schedule.latest("a").queries()).isEqualTo(2);

        clock.advance(Duration.ofMinutes(10));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("tried again at the next pass").hasSize(2);
        assertThat(schedule.pending("a")).isEqualTo(PciCanarySchedule.AFTER_BUILD);

        recorder.queries = 3;
        clock.advance(Duration.ofMinutes(10));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).hasSize(3);
        assertThat(recorder.triggers.get(2)).as("still labelled by the build that asked").isEqualTo(PciCanarySchedule.AFTER_BUILD);
        assertThat(schedule.pending("a")).isNull();
        assertThat(schedule.latest("a").state()).isEqualTo(State.GREEN);

        clock.advance(Duration.ofMinutes(10));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("consumed: not run again").hasSize(3);
    }

    // -- error (nexus-4lnn1.1.7 fix round) -----------------------------------------------------------------------

    @Test
    void anErroredRun_keepsAfterBuildPending_waitsOutTheRetryFloor_thenRunsUnderTheSameLabel() {
        recorder.unmeasuredReason = PciCanary.ERROR;
        schedule.built("a");
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).hasSize(1);
        assertThat(schedule.pending("a")).as("an error does not consume the trigger").isEqualTo(PciCanarySchedule.AFTER_BUILD);

        clock.advance(PciCanaryPolicy.ERROR_RETRY_FLOOR.minusMinutes(1));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("inside the floor: not retried").hasSize(1);

        recorder.unmeasuredReason = null;
        clock.advance(Duration.ofMinutes(2));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("past the floor: retried").hasSize(2);
        assertThat(recorder.triggers.get(1)).isEqualTo(PciCanarySchedule.AFTER_BUILD);
        assertThat(schedule.pending("a")).as("a measured run consumes it").isNull();
        assertThat(schedule.latest("a").state()).isEqualTo(State.GREEN);
    }

    @Test
    void anErroredDailyRun_isRetriedAfterTheFloor_notAfterADay() {
        recorder.unmeasuredReason = PciCanary.ERROR;
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.triggers).containsExactly(PciCanarySchedule.DAILY);

        clock.advance(PciCanaryPolicy.ERROR_RETRY_FLOOR.plusMinutes(1));
        schedule.runPass(List.of(target("a")));

        assertThat(recorder.runs).as("an error leaves no last-run time to wait a day from").hasSize(2);
    }

    @Test
    void aPersistentError_runsOncePerFloor_notOncePerPass() {
        recorder.unmeasuredReason = PciCanary.ERROR;
        schedule.built("a");
        for (int i = 0; i < 6; i++) {   // six ten-minute passes: one hour
            schedule.runPass(List.of(target("a")));
            clock.advance(Duration.ofMinutes(10));
        }
        assertThat(recorder.runs.size()).as("an hour of passes, a 30-minute floor").isEqualTo(2);
    }

    @Test
    void aFreshBuild_clearsTheRetryFloorOfAnEarlierError() {
        recorder.unmeasuredReason = PciCanary.ERROR;
        schedule.built("a");
        schedule.runPass(List.of(target("a")));
        recorder.unmeasuredReason = null;

        schedule.built("a");   // the index was rebuilt a pass later
        clock.advance(Duration.ofMinutes(10));
        schedule.runPass(List.of(target("a")));

        assertThat(recorder.runs).as("the new index is measured at once").hasSize(2);
    }

    @Test
    void anIndexNotUsedResult_stillConsumesTheTrigger() {
        recorder.unmeasuredReason = PciCanary.INDEX_NOT_USED;
        schedule.built("a");
        schedule.runPass(List.of(target("a")));
        assertThat(schedule.pending("a")).isNull();

        clock.advance(Duration.ofHours(23));
        schedule.runPass(List.of(target("a")));
        assertThat(recorder.runs).as("tried again at the next trigger or in a day, not before").hasSize(1);
    }

    // -- budget (test 8) ---------------------------------------------------------------------------------------

    @Test
    void whenThePassBudgetRunsOut_theRemainingIndexesCarryToTheNextPass_theBuiltOnesFirst() {
        recorder.costNanos = Duration.ofSeconds(25).toNanos();
        List<Target> targets = List.of(target("a"), target("b"), target("c"), target("d"), target("e"));
        // d was built: it must go ahead of the stale ones.
        schedule.built("d");

        PciCanarySchedule.PassReport first = schedule.runPass(targets);

        // 25 s apiece: d at 0, a at 25 s, b at 50 s, then 75 s is past the 60 s budget.
        assertThat(recorder.runs).containsExactly("d", "a", "b");
        assertThat(first.ran()).isEqualTo(3);
        assertThat(first.carried()).isEqualTo(2);

        recorder.runs.clear();
        PciCanarySchedule.PassReport second = schedule.runPass(targets);

        assertThat(recorder.runs).as("the carried indexes, not the ones that just ran").containsExactly("c", "e");
        assertThat(second.carried()).isZero();

        recorder.runs.clear();
        assertThat(schedule.runPass(targets).ran()).as("everything has a fresh run").isZero();
    }

    @Test
    void theFirstIndexAlwaysRuns_whateverTheBudget() {
        recorder.costNanos = Duration.ofMinutes(5).toNanos();

        PciCanarySchedule.PassReport report = schedule.runPass(List.of(target("a"), target("b")));

        assertThat(report.ran()).isEqualTo(1);
        assertThat(report.carried()).isEqualTo(1);
    }

    // -- housekeeping -------------------------------------------------------------------------------------------

    @Test
    void aDroppedIndex_isForgotten_andOneAbsentFromTheTargetsToo() {
        schedule.runPass(List.of(target("a"), target("b")));
        assertThat(schedule.results()).containsOnlyKeys("a", "b");

        schedule.dropped("a");
        assertThat(schedule.results()).containsOnlyKeys("b");

        schedule.runPass(List.of());
        assertThat(schedule.results()).isEmpty();
    }

    @Test
    void theLatestResultPerIndex_isTheOnlyOneKept() {
        schedule.runPass(List.of(target("a")));
        Result first = schedule.latest("a");
        schedule.built("a");
        clock.advance(Duration.ofMinutes(1));
        schedule.runPass(List.of(target("a")));

        assertThat(schedule.latest("a")).isNotSameAs(first);
        assertThat(schedule.results()).hasSize(1);
    }
}
