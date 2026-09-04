// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-00wsf residual — the shared intra-op thread bound for the three bare
 * {@code new OrtSession.SessionOptions()} sites (Bge768Embedder, OnnxEmbedder,
 * CrossEncoderReranker). Diagnosed T2 [24231]: one request measured taking 7.8
 * of 16 cores with no bound at all. {@code OrtSession.SessionOptions} has no
 * getter for the configured thread count (write-only native API), so this
 * tests the resolver's arithmetic and override discipline directly — the
 * wiring at each call site is covered by {@link OnnxIntraOpWiringTest}.
 */
class OnnxThreadPolicyTest {

    @Test
    void defaultsToHalfAvailableCores_minimumOne() {
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 16)).isEqualTo(8);
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 1)).isEqualTo(1);
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null, 3)).isEqualTo(1);
    }

    @Test
    void explicitOverrideWins() {
        Map<String, String> env = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "4");
        assertThat(OnnxThreadPolicy.intraOpThreads(env::get, 16)).isEqualTo(4);
    }

    @Test
    void refusesZeroOrNegativeOverride() {
        Map<String, String> zero = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "0");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(zero::get, 16))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(OnnxThreadPolicy.INTRA_OP_THREADS_ENV);

        Map<String, String> negative = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "-1");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(negative::get, 16))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void refusesNonNumericOverride() {
        Map<String, String> junk = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "lots");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(junk::get, 16))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void blankOverrideFallsBackToDefault() {
        Map<String, String> blank = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "   ");
        assertThat(OnnxThreadPolicy.intraOpThreads(blank::get, 16)).isEqualTo(8);
    }

    @Test
    void realEnvEntryPoint_returnsPositiveValue() {
        // Production entry point (System::getenv + real core count) — no
        // override in the test process env, so this exercises the real
        // Runtime.getRuntime().availableProcessors() rung end to end.
        assertThat(OnnxThreadPolicy.intraOpThreads()).isPositive();
    }
}
