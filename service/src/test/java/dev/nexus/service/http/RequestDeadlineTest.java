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

    // ── resolveBudgetMs (nexus-8hdg9 phase 5: advisory client header) ────

    @Test
    void resolveBudgetMs_absentHeaderFallsBackToEnvDefault() {
        assertThat(RequestDeadline.resolveBudgetMs(null, 300_000L)).isEqualTo(300_000L);
        assertThat(RequestDeadline.resolveBudgetMs("   ", 300_000L)).isEqualTo(300_000L);
    }

    @Test
    void resolveBudgetMs_presentPositiveHeaderBelowDefaultWins() {
        assertThat(RequestDeadline.resolveBudgetMs("45000", 300_000L)).isEqualTo(45_000L);
        assertThat(RequestDeadline.resolveBudgetMs(" 45000 ", 300_000L)).isEqualTo(45_000L);
    }

    @Test
    void resolveBudgetMs_malformedOrNonPositiveHeaderIsIgnoredNotRefused() {
        assertThat(RequestDeadline.resolveBudgetMs("soon", 300_000L)).isEqualTo(300_000L);
        assertThat(RequestDeadline.resolveBudgetMs("12.5", 300_000L)).isEqualTo(300_000L);
        assertThat(RequestDeadline.resolveBudgetMs("0", 300_000L)).isEqualTo(300_000L);
        assertThat(RequestDeadline.resolveBudgetMs("-7", 300_000L)).isEqualTo(300_000L);
    }

    @Test
    void resolveBudgetMs_oversizedHeaderWinsOverEnvDefault() {
        assertThat(RequestDeadline.resolveBudgetMs("540000", 300_000L)).isEqualTo(540_000L);
        assertThat(RequestDeadline.resolveBudgetMs("300000", 300_000L)).isEqualTo(300_000L);
    }

    // ── hard ceiling (nexus-8hdg9 phase 3 carry-in, T2 [24681]) ──────────

    @Test
    void resolveBudgetMs_headerAboveCeilingIsClampedToCeiling() {
        assertThat(RequestDeadline.resolveBudgetMs("3600000", 300_000L, 900_000L)).isEqualTo(900_000L);
        assertThat(RequestDeadline.resolveBudgetMs("900000", 300_000L, 900_000L)).isEqualTo(900_000L);
        assertThat(RequestDeadline.resolveBudgetMs("540000", 300_000L, 900_000L)).isEqualTo(540_000L);
        // 20+ digits overflow Long.parseLong: malformed, env default.
        assertThat(RequestDeadline.resolveBudgetMs("99999999999999999999", 300_000L, 900_000L))
                .isEqualTo(300_000L);
    }

    @Test
    void resolveBudgetMs_envDefaultAboveCeilingIsClampedToo() {
        assertThat(RequestDeadline.resolveBudgetMs(null, 2_000_000L, 900_000L)).isEqualTo(900_000L);
        assertThat(RequestDeadline.resolveBudgetMs("junk", 2_000_000L, 900_000L)).isEqualTo(900_000L);
    }

    @Test
    void resolveBudgetMs_twoArgOverloadUsesTheDefaultCeiling() {
        assertThat(RequestDeadline.resolveBudgetMs(
                Long.toString(RequestDeadline.DEFAULT_DEADLINE_MAX_MS + 1), 300_000L))
                .isEqualTo(RequestDeadline.DEFAULT_DEADLINE_MAX_MS);
    }

    @Test
    void resolveBudgetMs_leadingPlusAndNonAsciiDigitsAreMalformed() {
        // Long.parseLong would accept both; the accepted grammar is ASCII digits only.
        assertThat(RequestDeadline.resolveBudgetMs("+45000", 300_000L)).isEqualTo(300_000L);
        assertThat(RequestDeadline.resolveBudgetMs("٤٥٠٠٠", 300_000L))
                .as("Arabic-Indic digits are not the client's grammar")
                .isEqualTo(300_000L);
        assertThat(RequestDeadline.resolveBudgetMs("４５", 300_000L))
                .as("fullwidth digits likewise")
                .isEqualTo(300_000L);
    }

    @Test
    void deadlineMaxMsFromEnv_defaultsWhenAbsent() {
        assertThat(RequestDeadline.deadlineMaxMsFromEnv(Map.<String, String>of()::get))
                .isEqualTo(RequestDeadline.DEFAULT_DEADLINE_MAX_MS)
                .isEqualTo(900_000L);
        assertThat(RequestDeadline.DEFAULT_DEADLINE_MAX_MS)
                .as("the ceiling must sit above the operator default or it would clamp it")
                .isGreaterThan(RequestDeadline.DEFAULT_DEADLINE_MS);
    }

    @Test
    void deadlineMaxMsFromEnv_explicitOverrideWins() {
        assertThat(RequestDeadline.deadlineMaxMsFromEnv(
                Map.of(RequestDeadline.DEADLINE_MAX_MS_ENV, "1200000")::get)).isEqualTo(1_200_000L);
    }

    @Test
    void deadlineMaxMsFromEnv_refusesNonPositiveOrNonNumeric() {
        assertThatThrownBy(() -> RequestDeadline.deadlineMaxMsFromEnv(
                Map.of(RequestDeadline.DEADLINE_MAX_MS_ENV, "0")::get))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(RequestDeadline.DEADLINE_MAX_MS_ENV);
        assertThatThrownBy(() -> RequestDeadline.deadlineMaxMsFromEnv(
                Map.of(RequestDeadline.DEADLINE_MAX_MS_ENV, "forever")::get))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(RequestDeadline.DEADLINE_MAX_MS_ENV);
    }

    @Test
    void deadlineMaxMsFromEnv_realEntryPoint_returnsPositiveValue() {
        assertThat(RequestDeadline.deadlineMaxMsFromEnv()).isPositive();
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
