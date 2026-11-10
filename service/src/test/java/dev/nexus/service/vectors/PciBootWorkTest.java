// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.BootWorkRegistry.Entry;
import dev.nexus.service.BootWorkRegistry.Step;
import dev.nexus.service.vectors.PciBuilderSession.BuildProgress;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd.3: {@link PciBootWork}'s own rules, hermetic: a scripted sampler stands in for
 * {@code pg_stat_progress_create_index} (the real one is {@code PciReconcilerBootWorkIntegrationTest}'s).
 */
class PciBootWorkTest {

    private static final Duration FAST = Duration.ofMillis(20);

    private final BootWorkRegistry registry = new BootWorkRegistry(Clock.systemUTC());
    private final AtomicReference<Object> script = new AtomicReference<>(Optional.<BuildProgress>empty());
    private ListAppender<ILoggingEvent> logs;
    private Level levelBefore;
    private static Level levelAtStart;

    @BeforeAll
    static void rememberTheLoggerLevel() {
        levelAtStart = ((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PciBootWork.class)).getLevel();
    }

    /** This class raises the producer's logger to DEBUG; whatever it was (often unset) must be back for the next class. */
    @AfterAll
    static void theLoggerLevelIsBackAsItWas() {
        assertThat(((ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PciBootWork.class)).getLevel())
            .as("a test class leaves no logger level behind").isEqualTo(levelAtStart);
    }

    @BeforeEach
    void captureLogs() {
        logs = new ListAppender<>();
        logs.start();
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PciBootWork.class);
        levelBefore = logger.getLevel();   // null when it inherits: setLevel(null) puts that back
        logger.addAppender(logs);
        logger.setLevel(Level.DEBUG);
    }

    @AfterEach
    void releaseLogs() {
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PciBootWork.class);
        logger.detachAppender(logs);
        logger.setLevel(levelBefore);
    }

    @SuppressWarnings("unchecked")
    private Optional<BuildProgress> scripted() throws SQLException {
        Object next = script.get();
        if (next instanceof SQLException e) {
            throw e;
        }
        return (Optional<BuildProgress>) next;
    }

    private PciBootWork work() {
        return new PciBootWork(registry, this::scripted, FAST);
    }

    private static void await(BooleanSupplier condition, String what) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(30).toNanos();
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out waiting for " + what);
            }
            Thread.sleep(10);
        }
    }

    private Step step() {
        return registry.snapshot().isEmpty() ? null : registry.snapshot().get(0).step();
    }

    private static final String ON_STOP_TODAY =
        "the build keeps running in the database and is adopted after a restart, unless that restart applies a schema change; "
        + "stopping the database, or a restart that applies a schema change, ends it and it is rebuilt from the start";

    /** Wait on {@code latch} however often the thread is interrupted, then put the interrupt back. */
    private static void awaitUninterruptibly(CountDownLatch latch) {
        boolean interrupted = false;
        try {
            while (true) {
                try {
                    latch.await();
                    return;
                } catch (InterruptedException e) {
                    interrupted = true;
                }
            }
        } finally {
            if (interrupted) {
                Thread.currentThread().interrupt();
            }
        }
    }

    @Test
    void aSingleLeafPlan_registersNothingForNoBuild_andGivesTheRealTotalAtOnceForSome() {
        PciBootWork work = work();

        work.planned(0, false);
        assertThat(registry.snapshot()).isEmpty();

        work.planned(2, false);
        assertThat(registry.snapshot()).containsExactly(new Entry(PciBootWork.ID, "building search indexes",
            registry.snapshot().get(0).startedAt(), 0L, 2L, "indexes", true, ON_STOP_TODAY, null));
    }

    @Test
    void onStopSaysAnEngineStopKeepsTheBuild_unlessTheRestartAppliesASchemaChange_andADatabaseStopLosesIt() {
        // Pinned beside PciEngineRestartKeepsBuildIntegrationTest (an engine restart with no schema change leaves the
        // build running in Postgres and the next engine adopts it) and PciBuilderTerminationIntegrationTest (a restart
        // with a pending real changeset terminates it): when either behaviour changes, that test and this text change
        // together.
        assertThat(PciBootWork.ON_STOP).isEqualTo(ON_STOP_TODAY);
        assertThat(registry.snapshot()).isEmpty();
        PciBootWork work = work();
        work.planned(1, false);
        assertThat(registry.snapshot().get(0).safeToStop()).as("stopping the engine loses nothing").isTrue();
    }

    @Test
    void theTotalIsNullUntilTheLastPlannableLeafHasPlanned_soTheBarNeverGoesBackwardOrReadsFullEarly() {
        PciBootWork work = work();
        List<Long> totals = new ArrayList<>();

        work.planned(1, true);   // the first leaf plans one build; more leaves follow
        assertThat(registry.snapshot().get(0).total()).as("not known yet").isNull();
        try (var w = work.watchBuild()) {
            assertThat(registry.snapshot().get(0).done()).isZero();
        }
        // The one planned build has ended: done == planned, which must not read as 100%.
        assertThat(registry.snapshot().get(0).done()).isEqualTo(1L);
        assertThat(registry.snapshot().get(0).total()).as("still not known after the first build ended").isNull();

        work.planned(2, true);
        assertThat(registry.snapshot().get(0).total()).isNull();
        work.planned(0, false);   // the last plannable leaf has nothing to build: the total is fixed now
        totals.add(registry.snapshot().get(0).total());
        assertThat(registry.snapshot().get(0).done()).isEqualTo(1L);
        try (var w = work.watchBuild()) {
            totals.add(registry.snapshot().get(0).total());
        }
        try (var w = work.watchBuild()) {
            totals.add(registry.snapshot().get(0).total());
        }
        assertThat(totals).as("the total is the whole pass's, from the last leaf on").containsOnly(3L);
        assertThat(registry.snapshot().get(0).done()).isEqualTo(3L);

        work.endPass();
        assertThat(registry.snapshot()).isEmpty();

        work.planned(1, true);   // the next pass starts again: unknown, from zero
        assertThat(registry.snapshot().get(0).done()).isZero();
        assertThat(registry.snapshot().get(0).total()).isNull();
    }

    @Test
    void theLastLeafPlanningBuildsFixesTheTotalInTheSameCall_andDoneCountsFailedBuildsToo() {
        PciBootWork work = work();
        work.planned(1, true);
        try (var w = work.watchBuild()) {
            // a build that fails still ends: close() is what the reconciler's finally runs
        }
        work.planned(2, false);

        assertThat(registry.snapshot().get(0).total()).isEqualTo(3L);
        assertThat(registry.snapshot().get(0).done()).as("the ended build, however it ended").isEqualTo(1L);
    }

    @Test
    void aPlannedBuildTheExecutorSkips_countsAsEnded_soDoneStillReachesTheTotalAndTheTotalDoesNotMove() {
        // The planner rebuilds a failed index in the plan that drops it; the executor skips that build when the drop
        // did not happen. noteLeaf counted it, so it must leave the pending count or the bar stops short for the pass.
        PciBootWork work = work();
        work.planned(2, false);   // one ordinary build and one rebuild after a failed index's drop
        try (var w = work.watchBuild()) {
            assertThat(registry.snapshot().get(0).done()).isZero();
        }
        assertThat(registry.snapshot().get(0).done()).isEqualTo(1L);

        work.buildSkipped();

        assertThat(registry.snapshot().get(0).done()).isEqualTo(2L);
        assertThat(registry.snapshot().get(0).total()).as("the plan's count stands").isEqualTo(2L);
    }

    @Test
    void aSkippedBuildBeforeTheTotalIsKnown_leavesTheTotalUnknown() {
        PciBootWork work = work();
        work.planned(1, true);
        work.buildSkipped();
        assertThat(registry.snapshot().get(0).done()).isEqualTo(1L);
        assertThat(registry.snapshot().get(0).total()).as("a later leaf may still plan builds").isNull();
    }

    @Test
    void theStepIsBlocks_whenThereAreBlocks_elseTuples_elseNullCounts() throws Exception {
        PciBootWork work = work();
        work.planned(1, false);
        try (var w = work.watchBuild()) {
            script.set(Optional.of(new BuildProgress("building index: loading tuples in tree", 120, 4000, 7, 9)));
            await(() -> step() != null && step().total() != null && step().total() == 4000L, "blocks");
            assertThat(step()).isEqualTo(new Step("building index: loading tuples in tree", 120L, 4000L, "blocks"));

            script.set(Optional.of(new BuildProgress("building index: loading tuples in tree", 0, 0, 55, 900)));
            await(() -> step() != null && "tuples".equals(step().unit()), "tuples fallback");
            assertThat(step()).isEqualTo(new Step("building index: loading tuples in tree", 55L, 900L, "tuples"));

            script.set(Optional.of(new BuildProgress("waiting for writers before build", 0, 0, 0, 0)));
            await(() -> step() != null && step().what().startsWith("waiting"), "phase only");
            assertThat(step().what()).isEqualTo("waiting for writers before build");
            assertThat(step().done()).isNull();
            assertThat(step().total()).isNull();
        }
        assertThat(registry.snapshot().get(0).step()).as("cleared when the build ends").isNull();
    }

    @Test
    void theStepCarriesTheBuildPhaseAndNothingElse() throws Exception {
        script.set(Optional.of(new BuildProgress("building index: scanning table", 1, 2, 0, 0)));
        PciBootWork work = work();
        work.planned(1, false);
        try (var w = work.watchBuild()) {
            await(() -> step() != null, "a step");
            assertThat(step().what()).isEqualTo("building index: scanning table");
        }
    }

    /**
     * GET /v1/status is unauthenticated and names no collection, tenant or index (Sam, 2026-10-19 and 2026-11-06). The
     * switch that once could make the step read "collection: phase" is gone; a boolean back on the producer's surface
     * would be one again.
     */
    @Test
    void noSwitchOnTheProducerCanMakeTheStepNameACollection() {
        for (var ctor : PciBootWork.class.getDeclaredConstructors()) {
            assertThat(ctor.getParameterTypes()).as(ctor.toString()).doesNotContain(boolean.class, Boolean.class);
        }
        long overloads = 0;
        for (var m : PciReconciler.class.getDeclaredMethods()) {
            if (m.getName().equals("bootWork")) {
                overloads++;
                assertThat(m.getParameterTypes()).as(m.toString()).doesNotContain(boolean.class, Boolean.class);
            }
        }
        assertThat(overloads).as("the reconciler's bootWork overloads were examined").isEqualTo(2);
        assertThat(PciBootWork.Watch.class.getDeclaredFields()).extracting(java.lang.reflect.Field::getType)
            .as("a watch holds no collection name to put in a step").doesNotContain(String.class);
    }

    @Test
    void aFailedSample_keepsTheLastValue_isLoggedOnceAtWarn_andSamplingResumes() throws Exception {
        PciBootWork work = work();
        work.planned(1, false);
        try (var w = work.watchBuild()) {
            script.set(Optional.of(new BuildProgress("p1", 10, 100, 0, 0)));
            await(() -> step() != null && step().done() != null && step().done() == 10L, "first value");

            script.set(new SQLException("connection refused", "08001"));
            await(() -> logs.list.stream().anyMatch(e -> e.getFormattedMessage().contains("boot_work_sample_failed")),
                "the failure line");
            Thread.sleep(200);   // several failing ticks
            assertThat(step()).as("last value kept").isEqualTo(new Step("p1", 10L, 100L, "blocks"));
            assertThat(logs.list.stream().filter(e -> e.getLevel() == Level.WARN
                && e.getFormattedMessage().contains("boot_work_sample_failed"))).hasSize(1);

            script.set(Optional.of(new BuildProgress("p2", 20, 100, 0, 0)));
            await(() -> step().done() == 20L, "sampling resumes");
        }
    }

    @Test
    void aNoRowSample_leavesTheStepAsItWas() throws Exception {
        PciBootWork work = work();
        work.planned(1, false);
        try (var w = work.watchBuild()) {
            script.set(Optional.of(new BuildProgress("p1", 5, 50, 0, 0)));
            await(() -> step() != null, "a value");
            script.set(Optional.<BuildProgress>empty());
            Thread.sleep(150);
            assertThat(step()).isEqualTo(new Step("p1", 5L, 50L, "blocks"));
        }
    }

    /**
     * A sample is IN FLIGHT (blocked in the sampler) when the build ends. Releasing it afterwards must not write the
     * step back, and the sampler must not be called again: close() stops the thread, and the in-flight write is
     * refused by the closed flag.
     */
    @Test
    void aWatchClosedWhileASampleIsInFlight_neverWritesTheStepAfterwards_andIsNeverSampledAgain() throws Exception {
        CountDownLatch entered = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        AtomicInteger calls = new AtomicInteger();
        PciBootWork work = new PciBootWork(registry, () -> {
            calls.incrementAndGet();
            entered.countDown();
            awaitUninterruptibly(release);
            return Optional.of(new BuildProgress("late", 1, 2, 0, 0));
        }, FAST);
        work.planned(1, false);
        var watch = work.watchBuild();
        assertThat(entered.await(30, TimeUnit.SECONDS)).as("a sample is in flight").isTrue();

        watch.close();
        int callsAtClose = calls.get();
        release.countDown();
        Thread.sleep(400);   // the in-flight read returns and, if it may, writes; a live scheduler fires ~20 more

        assertThat(step()).as("the in-flight sample's late value was refused").isNull();
        assertThat(calls.get()).as("no sampler call started after close").isEqualTo(callsAtClose);
        assertThat(registry.snapshot().get(0).done()).as("and the build counted once").isEqualTo(1L);
    }

    @Test
    void aReadThatFailsBecauseTheWatchClosedUnderItIsNotLoggedAsAFailure() throws Exception {
        CountDownLatch entered = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        PciBootWork work = new PciBootWork(registry, () -> {
            entered.countDown();
            awaitUninterruptibly(release);
            throw new SQLException("interrupted by close", "57014");
        }, FAST);
        work.planned(1, false);
        var watch = work.watchBuild();
        assertThat(entered.await(30, TimeUnit.SECONDS)).isTrue();

        watch.close();
        release.countDown();
        Thread.sleep(300);

        assertThat(logs.list.stream().filter(e -> e.getFormattedMessage().contains("boot_work_sample_failed")))
            .as("a build that ended is not a failed sample, at any level").isEmpty();
    }

    @Test
    void theFirstReadIsOnePeriodIn_notAtOnce() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        PciBootWork work = new PciBootWork(registry, () -> {
            calls.incrementAndGet();
            return Optional.<BuildProgress>empty();
        }, Duration.ofMillis(1500));
        work.planned(1, false);
        try (var w = work.watchBuild()) {
            Thread.sleep(400);
            assertThat(calls.get()).as("a build that ends in under a period is never sampled").isZero();
            await(() -> calls.get() >= 1, "the first read after a period");
        }
    }
}
