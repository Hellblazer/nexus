// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-tu8wp.1 -- the pure decisions the per-collection fan-out makes before it touches a
 * connection: how many arms may run at once (per request and across requests), how long the
 * whole fan-out may take, and how long each statement may run.
 */
class PgVectorFanoutBoundsTest {

    private static final long MS = 1_000_000L;

    @Test
    void parallelismDefaultsToHalfThePool_neverBelowOne() {
        assertThat(PgVectorRepository.fanoutParallelism(null, 10)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutParallelism("", 10)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutParallelism("  ", 10)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutParallelism(null, 1)).isEqualTo(1);
        assertThat(PgVectorRepository.fanoutParallelism(null, 3)).isEqualTo(1);
    }

    @Test
    void parallelismTakesAValidOverride_andClampsItToThePoolSize() {
        assertThat(PgVectorRepository.fanoutParallelism("8", 10)).isEqualTo(8);
        assertThat(PgVectorRepository.fanoutParallelism(" 3 ", 10)).isEqualTo(3);
        assertThat(PgVectorRepository.fanoutParallelism("10", 10)).isEqualTo(10);
        assertThat(PgVectorRepository.fanoutParallelism("11", 10))
            .as("an override above the pool size is clamped to the pool size, not to the admission limit (2 x pool)")
            .isEqualTo(10);
        assertThat(PgVectorRepository.fanoutParallelism("500", 10)).isEqualTo(10);
    }

    @Test
    void aMalformedOrNonPositiveValueTakesTheDefault_neverACrash() {
        for (String bad : new String[] {"abc", "0", "-4", "3.5", "8x", "NaN"}) {
            assertThat(PgVectorRepository.fanoutParallelism(bad, 10)).as("raw=%s", bad).isEqualTo(5);
            assertThat(PgVectorRepository.fanoutArmPermits(bad, 10)).as("raw=%s", bad).isEqualTo(5);
        }
    }

    @Test
    void armPermitsDefaultToHalfThePool_andClampToIt() {
        assertThat(PgVectorRepository.fanoutArmPermits(null, 10)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutArmPermits("", 10)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutArmPermits(null, 1)).isEqualTo(1);
        assertThat(PgVectorRepository.fanoutArmPermits("3", 10)).isEqualTo(3);
        assertThat(PgVectorRepository.fanoutArmPermits("99", 10))
            .as("the cross-request cap leaves 2 connections of the pool to /health, writes and plain search")
            .isEqualTo(8);
        assertThat(PgVectorRepository.fanoutArmPermits("8", 10)).isEqualTo(8);
        assertThat(PgVectorRepository.fanoutArmPermits("9", 10)).isEqualTo(8);
    }

    @Test
    void armPermitsFollowThePool_withTheHeadroomClamp() {
        // nexus-wym0l: the ceiling is max(1, pool - 2); the default stays half the pool, below the ceiling
        // for every pool of 4 or more, and the ceiling never goes under 1.
        assertThat(PgVectorRepository.fanoutArmPermitCeiling(10)).isEqualTo(8);
        assertThat(PgVectorRepository.fanoutArmPermitCeiling(3)).isEqualTo(1);
        assertThat(PgVectorRepository.fanoutArmPermitCeiling(2)).isEqualTo(1);
        assertThat(PgVectorRepository.fanoutArmPermitCeiling(1)).isEqualTo(1);
        assertThat(PgVectorRepository.fanoutArmPermits(null, 20)).isEqualTo(10);
        assertThat(PgVectorRepository.fanoutArmPermits("50", 20)).isEqualTo(18);
        assertThat(PgVectorRepository.fanoutArmPermits(null, 4)).isEqualTo(2);
        assertThat(PgVectorRepository.fanoutArmPermits("4", 4)).isEqualTo(2);
        assertThat(PgVectorRepository.fanoutArmPermits("5", 3)).isEqualTo(1);
        assertThat(PgVectorRepository.fanoutArmPermits("5", 2)).isEqualTo(1);
    }

    @Test
    void fanoutBudgetDefaultsToTwentySeconds_andTakesAValidOverride() {
        assertThat(PgVectorRepository.fanoutBudgetMs(null)).isEqualTo(20_000L);
        assertThat(PgVectorRepository.fanoutBudgetMs("")).isEqualTo(20_000L);
        assertThat(PgVectorRepository.fanoutBudgetMs(" 1500 ")).isEqualTo(1_500L);
        assertThat(PgVectorRepository.fanoutBudgetMs("1")).isEqualTo(1L);
        assertThat(PgVectorRepository.fanoutBudgetMs("600000")).isEqualTo(600_000L);
    }

    @Test
    void aMalformedFanoutBudgetTakesTheDefault_neverACrash() {
        for (String bad : new String[] {"abc", "0", "-5", "600001", "1.5", "20s", "9999999999999999999"}) {
            assertThat(PgVectorRepository.fanoutBudgetMs(bad)).as("raw=%s", bad).isEqualTo(20_000L);
        }
    }

    @Test
    void statementBoundIsTheSmallestOfSearchBoundFanoutBudgetAndRequestBudget() {
        long now = 1_000_000_000_000L;
        long never = now + 3_600_000L * MS;
        // Plenty of both budgets: the search bound wins.
        var plenty = PgVectorRepository.armBound(now + 300_000L * MS, now + 20_000L * MS + 5_000L * MS, now, 10_000);
        assertThat(plenty.timeoutMs()).isEqualTo(10_000);
        assertThat(plenty.limiter()).isEqualTo(PgVectorRepository.Limiter.SEARCH);
        // 5 s left of the fan-out budget: it wins.
        var fan = PgVectorRepository.armBound(now + 300_000L * MS, now + 5_000L * MS, now, 30_000);
        assertThat(fan.timeoutMs()).isEqualTo(5_000);
        assertThat(fan.limiter()).isEqualTo(PgVectorRepository.Limiter.FANOUT);
        // 2 s left of the request budget, 5 s of the fan-out budget: the request budget wins.
        var req = PgVectorRepository.armBound(now + 2_000L * MS, now + 5_000L * MS, now, 30_000);
        assertThat(req.timeoutMs()).isEqualTo(2_000);
        assertThat(req.limiter()).isEqualTo(PgVectorRepository.Limiter.REQUEST);
        // A sliver of budget rounds UP to one millisecond: to Postgres 0 would mean DISABLED.
        assertThat(PgVectorRepository.armBound(now + 1L, never, now, 30_000).timeoutMs()).isEqualTo(1);
        assertThat(PgVectorRepository.armBound(RequestDeadlineProbe.NONE, now + 1L, now, 30_000).timeoutMs())
            .isEqualTo(1);
        // No request context (direct, in-process callers): the fan-out budget and the search bound.
        var direct = PgVectorRepository.armBound(RequestDeadlineProbe.NONE, now + 20_000L * MS, now, 30_000);
        assertThat(direct.timeoutMs()).isEqualTo(20_000);
        assertThat(direct.limiter()).isEqualTo(PgVectorRepository.Limiter.FANOUT);
        // Ties name the more external limit.
        assertThat(PgVectorRepository.armBound(now + 7_000L * MS, now + 7_000L * MS, now, 7_000).limiter())
            .isEqualTo(PgVectorRepository.Limiter.REQUEST);
        assertThat(PgVectorRepository.armBound(RequestDeadlineProbe.NONE, now + 7_000L * MS, now, 7_000).limiter())
            .isEqualTo(PgVectorRepository.Limiter.FANOUT);
    }

    @Test
    void aSpentRequestBudgetFailsTheRequest_aSpentFanoutBudgetOnlyTheCollection() {
        long now = 1_000_000_000_000L;
        long later = now + 100_000L * MS;
        assertThatThrownBy(() -> PgVectorRepository.armBound(now, later, now, 30_000))
            .isInstanceOf(RequestDeadlineExceededException.class);
        assertThatThrownBy(() -> PgVectorRepository.armBound(now - 1_000_000L, later, now, 30_000))
            .isInstanceOf(RequestDeadlineExceededException.class);
        assertThatThrownBy(() -> PgVectorRepository.armBound(later, now, now, 30_000))
            .isInstanceOf(PgVectorRepository.FanoutBudgetSpentException.class);
        assertThatThrownBy(() -> PgVectorRepository.armBound(RequestDeadlineProbe.NONE, now - 1L, now, 30_000))
            .isInstanceOf(PgVectorRepository.FanoutBudgetSpentException.class);
        // Both spent: the request failure wins (a whole-request failure outranks a per-collection one).
        assertThatThrownBy(() -> PgVectorRepository.armBound(now, now, now, 30_000))
            .isInstanceOf(RequestDeadlineExceededException.class);
    }

    @Test
    void theMergeOrderBreaksTiesOnIdThenCollection() {
        var a = new PgVectorRepository.Candidate(0.5, "c1", "colA", java.util.Map.of());
        var b = new PgVectorRepository.Candidate(0.5, "c1", "colB", java.util.Map.of());
        var otherId = new PgVectorRepository.Candidate(0.5, "c2", "colA", java.util.Map.of());
        var nearer = new PgVectorRepository.Candidate(0.4, "c9", "colZ", java.util.Map.of());
        assertThat(PgVectorRepository.MERGE_ORDER.compare(a, b)).isNegative();
        assertThat(PgVectorRepository.MERGE_ORDER.compare(b, a)).isPositive();
        assertThat(PgVectorRepository.MERGE_ORDER.compare(b, otherId)).as("id before collection").isNegative();
        assertThat(PgVectorRepository.MERGE_ORDER.compare(nearer, a)).isNegative();
    }
}
