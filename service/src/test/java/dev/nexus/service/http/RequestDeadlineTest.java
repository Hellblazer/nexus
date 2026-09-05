// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-8hdg9 phase 2 (write-path cancellation on client disconnect) --
 * {@link RequestDeadline}'s env resolution and {@code newDeadlineNanos}
 * arithmetic. Mirrors {@code LocalOnnxAdmissionTest}'s resolver-test shape
 * (env-injectable resolver, explicit-override-wins, refuse zero/negative,
 * refuse non-numeric, a real-entry-point smoke test).
 */
class RequestDeadlineTest {

    // ── deadlineMsFromEnv ────────────────────────────────────────────────

    @Test
    void deadlineMsFromEnv_defaultsWhenAbsent() {
        assertThat(RequestDeadline.deadlineMsFromEnv(name -> null))
                .isEqualTo(RequestDeadline.DEFAULT_DEADLINE_MS);
    }

    @Test
    void deadlineMsFromEnv_defaultsWhenBlank() {
        Map<String, String> env = Map.of(RequestDeadline.DEADLINE_MS_ENV, "   ");
        assertThat(RequestDeadline.deadlineMsFromEnv(env::get))
                .isEqualTo(RequestDeadline.DEFAULT_DEADLINE_MS);
    }

    @Test
    void deadlineMsFromEnv_explicitOverrideWins() {
        Map<String, String> env = Map.of(RequestDeadline.DEADLINE_MS_ENV, "45000");
        assertThat(RequestDeadline.deadlineMsFromEnv(env::get)).isEqualTo(45_000L);
    }

    @Test
    void deadlineMsFromEnv_refusesZeroOrNegativeOverride() {
        Map<String, String> zero = Map.of(RequestDeadline.DEADLINE_MS_ENV, "0");
        assertThatThrownBy(() -> RequestDeadline.deadlineMsFromEnv(zero::get))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(RequestDeadline.DEADLINE_MS_ENV);

        Map<String, String> negative = Map.of(RequestDeadline.DEADLINE_MS_ENV, "-1");
        assertThatThrownBy(() -> RequestDeadline.deadlineMsFromEnv(negative::get))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void deadlineMsFromEnv_refusesNonNumericOverride() {
        Map<String, String> junk = Map.of(RequestDeadline.DEADLINE_MS_ENV, "soon");
        assertThatThrownBy(() -> RequestDeadline.deadlineMsFromEnv(junk::get))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void deadlineMsFromEnv_realEntryPoint_returnsPositiveValue() {
        // No override in the test process env: exercises the real default.
        assertThat(RequestDeadline.deadlineMsFromEnv()).isPositive();
    }

    // ── newDeadlineNanos ─────────────────────────────────────────────────

    @Test
    void newDeadlineNanos_isApproximatelyNowPlusBudget() {
        long budgetMs = 1_000L;
        long before = System.nanoTime();
        long deadline = RequestDeadline.newDeadlineNanos(budgetMs);
        long after = System.nanoTime();

        long budgetNanos = TimeUnit.MILLISECONDS.toNanos(budgetMs);
        assertThat(deadline).isGreaterThanOrEqualTo(before + budgetNanos);
        assertThat(deadline).isLessThanOrEqualTo(after + budgetNanos);
    }

    @Test
    void newDeadlineNanos_largerBudgetYieldsLaterDeadline() {
        long deadline1 = RequestDeadline.newDeadlineNanos(1_000L);
        long deadline2 = RequestDeadline.newDeadlineNanos(2_000L);
        assertThat(deadline2).isGreaterThan(deadline1);
    }
}
