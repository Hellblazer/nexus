// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/** {@link Ts2Rule} on recorded figures (ms). */
class Ts2RuleTest {

    /** The rule on recorded figures: the row that failed on noise passes, and the pruning control still fails. */
    @Test
    void violates_passesSubMillisecondNoise_andFailsThePruningControlAndTheCap() {
        assertThat(Ts2Rule.violates(0.600, 1.232)).as("assign_from_chashes 1024 on develop aa226556f, 2.05x").isFalse();
        assertThat(Ts2Rule.violates(0.9, 15.4)).as("plain_search_1024 with its tenant predicate removed").isTrue();
        assertThat(Ts2Rule.violates(0.9, 3.0)).as("over 2x and 2.1 ms apart").isTrue();
        assertThat(Ts2Rule.violates(0.9, 2.8)).as("over 2x but 1.9 ms apart").isFalse();
        assertThat(Ts2Rule.violates(5.0, 9.0)).as("4 ms apart but under 2x").isFalse();
        assertThat(Ts2Rule.violates(24.0, 25.5)).as("over the 25 ms cap, which has no allowance").isTrue();
    }

    /** The factor is exactly 2 and strict: a change to 1.5x or 3x turns a case red. */
    @Test
    void violates_pinsTheTwoTimesFactor() {
        assertThat(Ts2Rule.violates(4.0, 8.0)).as("exactly 2x, 4 ms apart: not over").isFalse();
        assertThat(Ts2Rule.violates(4.0, 8.5)).as("just over 2x, 4.5 ms apart").isTrue();
        assertThat(Ts2Rule.violates(3.0, 7.0)).as("2.33x, 4 ms apart: over at 2x, not at 3x").isTrue();
        assertThat(Ts2Rule.violates(3.0, 5.9)).as("1.97x, 2.9 ms apart: not over at 2x, over at 1.5x").isFalse();
    }

    /** The 2 ms allowance is exactly 2.0 and strict, and the cap is exactly 25.0 and strict. */
    @Test
    void violates_pinsTheAllowanceAndTheCapAtTheirBoundaries() {
        assertThat(Ts2Rule.RATIO_MIN_DIFF_MS).isEqualTo(2.0);
        assertThat(Ts2Rule.CAP_MS).isEqualTo(25.0);
        assertThat(Ts2Rule.violates(1.5, 3.5)).as("over 2x, exactly 2.0 ms apart: not over").isFalse();
        assertThat(Ts2Rule.violates(1.5, 3.75)).as("over 2x, 2.25 ms apart").isTrue();
        assertThat(Ts2Rule.violates(20.0, 25.0)).as("exactly the cap, 1.25x: not over").isFalse();
        assertThat(Ts2Rule.violates(20.0, 25.5)).as("just over the cap, 1.275x, 5.5 ms apart: the cap alone").isTrue();
        assertThat(Ts2Rule.violates(10.0, 25.0)).as("over 2x and 15 ms apart, exactly the cap: the ratio rule")
            .isTrue();
    }

    /** The per-set verdict: load that reaches one set passes, a growth in every set fails (nexus-7fku2.1.1). */
    @Test
    void judge_failsOnlyWhenEverySetFails_andReportsTheBestSetsOwnPair() {
        Ts2Rule.Figures burst = Ts2Rule.judge(List.of(new double[] {2.27, 4.12}, new double[] {4.11, 4.08},
            new double[] {4.37, 4.33}, new double[] {4.69, 4.52}, new double[] {4.74, 4.49}));
        assertThat(burst.violates()).as("search_topic_set 384, one loaded run on chas").isFalse();
        assertThat(burst.few()).as("the best set's own pair (lowest ratio), not the column minimum 2.27")
            .isEqualTo(4.74);
        assertThat(burst.many()).isEqualTo(4.49);

        Ts2Rule.Figures oneSetOver = Ts2Rule.judge(List.of(new double[] {1.9, 5.0}, new double[] {5.0, 5.1}));
        assertThat(oneSetOver.violates()).as("one set over the rule, one not").isFalse();
        assertThat(oneSetOver.many()).isEqualTo(5.1);

        Ts2Rule.Figures planted = Ts2Rule.judge(List.of(new double[] {2.81, 59.91}, new double[] {4.53, 64.59},
            new double[] {2.67, 59.21}, new double[] {2.33, 62.24}, new double[] {2.56, 58.55}));
        assertThat(planted.violates())
            .as("search_topic_set 1024 with its tenant predicate removed, under load on chas").isTrue();
        assertThat(planted.few()).as("lowest ratio of five failing sets, 14.3x").isEqualTo(4.53);
        assertThat(planted.many()).isEqualTo(64.59);

        Ts2Rule.Figures capOnly = Ts2Rule.judge(List.of(new double[] {24.0, 25.5}, new double[] {24.5, 26.0}));
        assertThat(capOnly.violates()).as("over the 25 ms cap in every set").isTrue();

        // an intermediate growth, +2.6 ms on a 1 ms plan (3.6x): well under the planted 10-17x, over the rule
        Ts2Rule.Figures intermediate = Ts2Rule.judge(List.of(new double[] {1.0, 3.6}, new double[] {1.1, 3.7},
            new double[] {0.9, 3.5}, new double[] {1.0, 3.6}, new double[] {1.0, 3.6}));
        assertThat(intermediate.violates()).as("1.0 against 3.6 in every set").isTrue();
        Ts2Rule.Figures intermediateOnePass = Ts2Rule.judge(List.of(new double[] {1.0, 3.6}, new double[] {1.1, 3.7},
            new double[] {3.0, 3.6}, new double[] {1.0, 3.6}, new double[] {1.0, 3.6}));
        assertThat(intermediateOnePass.violates()).as("the same, with one set that passes (3.0 against 3.6)")
            .isFalse();
        assertThat(intermediateOnePass.few()).as("the passing set's own pair").isEqualTo(3.0);
        assertThat(intermediateOnePass.many()).isEqualTo(3.6);
    }
}
