// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.assertj.core.data.Offset;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-160 P1 review (S1) — model-free guard on bge CLS-pooling math.
 *
 * <p>The integration parity gate ({@link Bge768ParityTest}) is SKIPPED on CI when
 * the 416MB standard ONNX is absent, so the pooling/normalization implementation
 * would otherwise have no CI coverage. This unit test exercises
 * {@link Bge768Embedder#clsPoolNormalize(float[][])} directly with synthetic
 * hidden states — no model required — so a regression to "take token 0 then
 * L2-normalize" is caught on every CI run.
 */
class Bge768EmbedderMathTest {

    @Test
    void clsPool_takesRowZero_not_meanPool() {
        // Row 0 ([CLS]) is [3,4,0]; other rows are decoys a mean-pool would mix in.
        float[][] hidden = {
                {3f, 4f, 0f},
                {100f, 0f, 0f},
                {0f, 0f, 99f},
        };
        float[] out = Bge768Embedder.clsPoolNormalize(hidden);

        // CLS = [3,4,0], norm 5 -> [0.6, 0.8, 0]. A mean-pool would be nowhere near this.
        assertThat(out).hasSize(3);
        assertThat((double) out[0]).isCloseTo(0.6, Offset.offset(1e-6));
        assertThat((double) out[1]).isCloseTo(0.8, Offset.offset(1e-6));
        assertThat((double) out[2]).isCloseTo(0.0, Offset.offset(1e-6));
    }

    @Test
    void clsPool_output_is_l2_normalized() {
        float[][] hidden = {{1f, 2f, 3f, 4f}, {9f, 9f, 9f, 9f}};
        float[] out = Bge768Embedder.clsPoolNormalize(hidden);
        double norm = 0.0;
        for (float f : out) norm += (double) f * f;
        assertThat(Math.sqrt(norm)).isCloseTo(1.0, Offset.offset(1e-6));
    }

    @Test
    void clsPool_zeroVector_doesNotDivideByZero() {
        // Degenerate [CLS] row of all zeros: guard (norm > 1e-12) leaves it zero, no NaN.
        float[][] hidden = {{0f, 0f, 0f}, {1f, 1f, 1f}};
        float[] out = Bge768Embedder.clsPoolNormalize(hidden);
        assertThat(out).containsExactly(0f, 0f, 0f);
    }

    @Test
    void clsPool_doesNotMutateInput() {
        float[][] hidden = {{3f, 4f}, {1f, 1f}};
        Bge768Embedder.clsPoolNormalize(hidden);
        // Original [CLS] row must be untouched (clone() inside).
        assertThat(hidden[0]).containsExactly(3f, 4f);
    }
}
