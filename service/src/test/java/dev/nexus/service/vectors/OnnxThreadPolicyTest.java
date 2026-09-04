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
    void unsetLeavesOrtDefault_neverDerivedFromCoresOrPermits() {
        // The ONNX session is one shared object per model, so its intra-op
        // pool is the whole engine's embedding width. v0.1.99 derived a count
        // from cores/permits and capped a 16-core box at 2 threads; the 7.29.0
        // shakedown measured nx index rdr stalling on it. Unset now means
        // "do not call setIntraOpNumThreads at all": ORT's own default.
        assertThat(OnnxThreadPolicy.intraOpThreads(name -> null)).isEmpty();
        Map<String, String> permitsOnly = Map.of(LocalOnnxAdmission.PERMITS_ENV, "8");
        assertThat(OnnxThreadPolicy.intraOpThreads(permitsOnly::get)).isEmpty();
    }

    @Test
    void explicitOverrideWins() {
        Map<String, String> env = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "4");
        assertThat(OnnxThreadPolicy.intraOpThreads(env::get)).hasValue(4);
    }

    @Test
    void refusesZeroOrNegativeOverride() {
        Map<String, String> zero = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "0");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(zero::get))
                .isInstanceOf(IllegalArgumentException.class);
        Map<String, String> negative = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "-3");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(negative::get))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void refusesNonNumericOverride() {
        Map<String, String> junk = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "many");
        assertThatThrownBy(() -> OnnxThreadPolicy.intraOpThreads(junk::get))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void blankOverrideLeavesOrtDefault() {
        Map<String, String> blank = Map.of(OnnxThreadPolicy.INTRA_OP_THREADS_ENV, "   ");
        assertThat(OnnxThreadPolicy.intraOpThreads(blank::get)).isEmpty();
    }

    @Test
    void realEnvEntryPoint_isEmptyOrPositive() {
        // Production entry point on the real env: either the operator set an
        // explicit positive count or the session keeps ORT's default.
        var threads = OnnxThreadPolicy.intraOpThreads();
        assertThat(threads.isEmpty() || threads.getAsInt() > 0).isTrue();
    }
}
