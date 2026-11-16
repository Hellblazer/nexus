// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** nexus-bbtc2: the shared probe's arithmetic (cap, worker setting, statement bound, log words), with no database. */
class LexicalGateProbeTest {

    private static final long MS = 1_000_000L;

    @Test
    void theCapIsOneMoreThanTheSelectiveCutoff_forTheSharedAndThePerArmProbeAlike() {
        assertThat(LexicalGateProbe.probeCap(5000)).isEqualTo(5001);
        assertThat(LexicalGateProbe.probeCap(1)).isEqualTo(2);
        assertThat(LexicalGateProbe.probeCap(PgVectorRepository.SELECTIVE_GATE_MAX))
            .as("the production cap is the 5,001 the per-arm probe has always used").isEqualTo(5001);
    }

    @Test
    void theWorkerSettingReadsZeroAsSerial_clampsHigh_andTakesTheDefaultOtherwise() {
        assertThat(LexicalGateProbe.probeWorkers(null)).isEqualTo(2);
        assertThat(LexicalGateProbe.probeWorkers("  ")).isEqualTo(2);
        assertThat(LexicalGateProbe.probeWorkers("0")).as("0 is serial, not the default").isZero();
        assertThat(LexicalGateProbe.probeWorkers(" 4 ")).isEqualTo(4);
        assertThat(LexicalGateProbe.probeWorkers("9999")).isEqualTo(LexicalGateProbe.MAX_PROBE_WORKERS);
        assertThat(LexicalGateProbe.probeWorkers("-1")).isEqualTo(2);
        assertThat(LexicalGateProbe.probeWorkers("many")).isEqualTo(2);
    }

    @Test
    void theProbeBoundIsAtMostHalfOfWhatTheBudgetsLeave_andAtMostTheSearchBound() {
        long now = 1_000_000_000L;
        // 20 s of fan-out budget left, no request deadline: half of it, though the search bound allows 30 s
        assertThat(LexicalGateProbe.probeBoundMs(RequestDeadlineProbe.NONE, now + 20_000 * MS, now, 30_000))
            .isEqualTo(10_000);
        // a smaller search bound wins
        assertThat(LexicalGateProbe.probeBoundMs(RequestDeadlineProbe.NONE, now + 20_000 * MS, now, 3_000))
            .isEqualTo(3_000);
        // the nearer of the two budgets is the one halved
        assertThat(LexicalGateProbe.probeBoundMs(now + 8_000 * MS, now + 20_000 * MS, now, 30_000)).isEqualTo(4_000);
        // never below 1 ms (to Postgres 0 means disabled)
        assertThat(LexicalGateProbe.probeBoundMs(RequestDeadlineProbe.NONE, now + 1 * MS, now, 30_000)).isEqualTo(1);
    }

    @Test
    void aSpentBudgetIsStillAnErrorNotABound() {
        long now = 1_000_000_000L;
        assertThatThrownBy(() -> LexicalGateProbe.probeBoundMs(RequestDeadlineProbe.NONE, now - 1, now, 30_000))
            .isInstanceOf(PgVectorRepository.FanoutBudgetSpentException.class);
    }

    @Test
    void theLogWordSaysNotStartedForAProbeThatNeverRan_notPending() {
        var never = new SharedLexicalProbe();
        assertThat(never.label()).isEqualTo("not_started");
        var done = new SharedLexicalProbe();
        done.completePerCollection(Map.of(), Set.of("d1", "d2"), 1L);
        assertThat(done.label()).isEqualTo("per_collection");
        assertThat(done.denseCount()).isEqualTo(2);
        var failed = new SharedLexicalProbe();
        failed.completeFallback(1L);
        assertThat(failed.label()).isEqualTo("fallback");
        assertThat(failed.denseCount()).isZero();
    }
}
