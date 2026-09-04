// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-00wsf residual (T2 [24231]; coordinated with admission in review
 * round 2, T2 [24238]) — the shared intra-op thread bound for the three bare
 * {@code new OrtSession.SessionOptions()} sites (Bge768Embedder, OnnxEmbedder,
 * CrossEncoderReranker). Diagnosed T2 [24231]: one request measured taking
 * 7.8 of 16 cores with no bound at all. {@code OrtSession.SessionOptions} has
 * no getter for the configured thread count (write-only native API), so this
 * tests the resolver's arithmetic and override discipline directly — the
 * wiring at each call site is covered by {@link OnnxIntraOpWiringTest}.
 */
class OnnxThreadPolicyTest {

    @Test
    void defaultsToCoresDividedByAdmissionPermits_minimumOne() {
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 16, 2)).isEqualTo(8);
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 16, 8)).isEqualTo(2);
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 1, 4)).isEqualTo(1);
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 16, 100)).isEqualTo(1);
    }

    @Test
    void explicitOverrideWins() {
        Map<String, String> env = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "4");
        assertThat(OnnxThreadPolicy.intraOpThreads(env::get, 16, 2)).isEqualTo(4);
    }

    @Test
    void refusesZeroOrNegativeOverride() {
        Map<String, String> zero = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "0");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(zero::get, 16, 2))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(OnnxThreadPolicy.INTRA_OP_THREADS_ENV);

        Map<String, String> negative = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "-1");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(negative::get, 16, 2))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void refusesNonNumericOverride() {
        Map<String, String> junk = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "lots");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(junk::get, 16, 2))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void blankOverrideFallsBackToDefault() {
        Map<String, String> blank = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "   ");
        assertThat(OnnxThreadPolicy.intraOpThreads(blank::get, 16, 2)).isEqualTo(8);
    }

    @Test
    void permitsTimesThreads_neverExceedsCores_wherePermitsFitsWithinCores() {
        // The review-2 coordination invariant: floor(cores/permits) * permits <= cores,
        // for every permits value that itself fits within cores (the only regime
        // where "coordinated" is a meaningful claim — see the next test for the
        // permits > cores degenerate case).
        for (int cores : new int[] {1, 2, 3, 4, 7, 16, 32, 64}) {
            for (int permits = 1; permits <= cores; permits++) {
                int threads = OnnxThreadPolicy.intraOpThreads(name -> null, cores, permits);
                assertThat((long) threads * permits)
                        .as("cores=%d permits=%d threads=%d", cores, permits, threads)
                        .isLessThanOrEqualTo(cores);
            }
        }
    }

    @Test
    void threadsFloorsToOne_whenPermitsExceedCores() {
        // permits > cores is a degenerate config (more concurrent-embed permits
        // than the box has cores) — the minimum-1 floor applies and the product
        // can legitimately exceed cores; this is the boundary the previous test
        // deliberately excludes.
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 4, 100)).isEqualTo(1);
    }

    @Test
    void realEnvEntryPoint_returnsPositiveValue() {
        // Production entry point (System::getenv + real core count, admission
        // permits from LocalOnnxAdmission.permitsFromEnv()) — no override in
        // the test process env, so this exercises the real coordination path
        // end to end.
        assertThat(OnnxThreadPolicy.intraOpThreads()).isPositive();
    }
}
