// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-tu8wp.1 -- the two pure decisions the per-collection fan-out makes before it touches a
 * connection: how many arms may run at once, and how long each statement may run.
 */
class PgVectorFanoutBoundsTest {

    @Test
    void parallelismDefaultsToHalfThePool_neverBelowOne() {
        assertThat(PgVectorRepository.fanoutParallelism(null, 10, 20)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutParallelism("", 10, 20)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutParallelism("  ", 10, 20)).isEqualTo(5);
        assertThat(PgVectorRepository.fanoutParallelism(null, 1, 2)).isEqualTo(1);
        assertThat(PgVectorRepository.fanoutParallelism(null, 3, 6)).isEqualTo(1);
    }

    @Test
    void parallelismTakesAValidOverride_andClampsItUnderTheAdmissionLimit() {
        assertThat(PgVectorRepository.fanoutParallelism("8", 10, 20)).isEqualTo(8);
        assertThat(PgVectorRepository.fanoutParallelism(" 3 ", 10, 20)).isEqualTo(3);
        assertThat(PgVectorRepository.fanoutParallelism("20", 10, 20)).isEqualTo(20);
        assertThat(PgVectorRepository.fanoutParallelism("500", 10, 20))
            .as("one request must stay under the admission limit or it queues behind itself")
            .isEqualTo(20);
    }

    @Test
    void aMalformedOrNonPositiveValueTakesTheDefault_neverACrash() {
        for (String bad : new String[] {"abc", "0", "-4", "3.5", "8x", "NaN"}) {
            assertThat(PgVectorRepository.fanoutParallelism(bad, 10, 20))
                .as("raw=%s", bad).isEqualTo(5);
        }
    }

    @Test
    void statementBoundIsTheSmallerOfTheSearchBoundAndTheRemainingBudget() {
        long now = 1_000_000_000_000L;
        // Plenty of budget: the search bound wins.
        assertThat(PgVectorRepository.armStatementTimeoutMs(now + 300_000_000_000L, now, 30_000)).isEqualTo(30_000);
        // 5 s left of the request budget: it wins.
        assertThat(PgVectorRepository.armStatementTimeoutMs(now + 5_000_000_000L, now, 30_000)).isEqualTo(5_000);
        // A sliver of budget rounds UP to one millisecond: to Postgres 0 would mean DISABLED.
        assertThat(PgVectorRepository.armStatementTimeoutMs(now + 1L, now, 30_000)).isEqualTo(1);
        // No request context (direct, in-process callers): the search bound alone.
        assertThat(PgVectorRepository.armStatementTimeoutMs(RequestDeadlineProbe.NONE, now, 30_000)).isEqualTo(30_000);
    }

    @Test
    void aSpentBudgetRefusesTheStatement() {
        long now = 1_000_000_000_000L;
        assertThatThrownBy(() -> PgVectorRepository.armStatementTimeoutMs(now, now, 30_000))
            .isInstanceOf(RequestDeadlineExceededException.class);
        assertThatThrownBy(() -> PgVectorRepository.armStatementTimeoutMs(now - 1_000_000L, now, 30_000))
            .isInstanceOf(RequestDeadlineExceededException.class);
    }
}
