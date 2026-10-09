// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-227 Step 2 (nexus-43ulx.17): the shutdown hook runs the pool reaper and the builder reaper together, inside
 * one budget. No database: the calls are latches, so the assertions are about scheduling, not about timing luck.
 */
class BackendReaperShutdownBudgetTest {

    @Test
    @Timeout(value = 30, unit = TimeUnit.SECONDS)
    void twoCalls_runAtTheSameTime_notOneAfterTheOther() {
        // Each call waits for the other to start. Run one after the other, the first would wait out its latch
        // and report 0; run together, both see the other and report 1.
        CountDownLatch bothStarted = new CountDownLatch(2);
        Callable<Integer> call = () -> {
            bothStarted.countDown();
            return bothStarted.await(5, TimeUnit.SECONDS) ? 1 : 0;
        };

        List<Integer> results = BackendReaper.runConcurrently(List.of(call, call), 20_000);

        assertThat(results).containsExactly(1, 1);
    }

    @Test
    @Timeout(value = 30, unit = TimeUnit.SECONDS)
    void aCallThatNeverReturns_isAbandonedAtTheBudget_andTheOtherResultIsKept() {
        CountDownLatch never = new CountDownLatch(1);
        Callable<Integer> stuck = () -> {
            never.await();
            return 7;
        };
        Callable<Integer> quick = () -> 3;

        long start = System.nanoTime();
        List<Integer> results = BackendReaper.runConcurrently(List.of(stuck, quick), 400);
        Duration took = Duration.ofNanos(System.nanoTime() - start);

        assertThat(results).containsExactly(BackendReaper.TIMED_OUT, 3);
        assertThat(took).as("the join gave up at the budget, not when the stuck call ended")
            .isLessThan(Duration.ofSeconds(5));
        never.countDown();
    }

    @Test
    @Timeout(value = 30, unit = TimeUnit.SECONDS)
    void aCallThatThrows_isReportedAsFailed_andDoesNotCostTheOtherItsResult() {
        Callable<Integer> boom = () -> {
            throw new IllegalStateException("no connection string");
        };
        Callable<Integer> quick = () -> 2;

        assertThat(BackendReaper.runConcurrently(List.of(boom, quick), 20_000))
            .containsExactly(BackendReaper.FAILED, 2);
    }

    @Test
    void theBudget_isTheOldSingleCallWorstCase_soTheSecondCallAddsNothing() {
        // One reaper call: connect (3 s) plus one socket read (6 s) = 9 s, the bound the hook always had.
        long singleCallWorstMillis =
            (BackendReaper.CONNECT_TIMEOUT_SECONDS + BackendReaper.CONNECT_TIMEOUT_SECONDS * 2L) * 1000;
        long containerStopGraceMillis = 10_000;

        assertThat(2 * singleCallWorstMillis)
            .as("two calls in sequence would not fit the container's stop grace").isGreaterThan(containerStopGraceMillis);
        assertThat(BackendReaper.SHUTDOWN_BUDGET_MILLIS)
            .as("run together under one budget, the pair costs no more than one call did")
            .isEqualTo(singleCallWorstMillis)
            .isLessThan(containerStopGraceMillis);
    }
}
