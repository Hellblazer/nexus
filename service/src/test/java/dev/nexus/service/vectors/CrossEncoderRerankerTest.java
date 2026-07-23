// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-188 bead nexus-9o6y2.3 — model-free unit seams of the local cross-encoder
 * reranker (the ~91MB ONNX is absent on CI; the real-inference path is covered
 * by {@link CrossEncoderRerankerInferenceTest}, gated on artifact presence —
 * same split as Bge768EmbedderMathTest / Bge768ParityTest, RDR-160 P1 S1).
 */
class CrossEncoderRerankerTest {

    // ── Scoring/order seam (static, model-free) ──────────────────────────────

    @Test
    void toScoredSortsByLogitDescendingKeepingInputIndices() {
        var out = CrossEncoderReranker.toScored(new float[] {-2.5f, 7.25f, 0.5f}, null);

        assertThat(out).containsExactly(
                new Reranker.Scored(1, 7.25),
                new Reranker.Scored(2, 0.5),
                new Reranker.Scored(0, -2.5));
    }

    @Test
    void toScoredTruncatesToTopK() {
        var out = CrossEncoderReranker.toScored(new float[] {1f, 3f, 2f}, 2);

        assertThat(out).containsExactly(
                new Reranker.Scored(1, 3.0),
                new Reranker.Scored(2, 2.0));
    }

    @Test
    void flattenLogitsHandlesBothExportShapes() {
        // MS-MARCO cross-encoder exports declare logits as (batch, 1) or (batch,).
        assertThat(CrossEncoderReranker.flattenLogits(
                new float[][] {{0.5f}, {1.5f}}, 2)).containsExactly(0.5f, 1.5f);
        assertThat(CrossEncoderReranker.flattenLogits(
                new float[] {0.5f, 1.5f}, 2)).containsExactly(0.5f, 1.5f);
    }

    @Test
    void flattenLogitsRejectsShapeMismatch() {
        // A (batch, 2) classifier head or a wrong-cardinality output must degrade
        // loud (typed), never be silently reinterpreted as scores.
        assertThatThrownBy(() -> CrossEncoderReranker.flattenLogits(
                new float[][] {{0.5f, 0.7f}, {1.5f, 0.1f}}, 2))
                .isInstanceOf(RerankUpstreamException.class);
        assertThatThrownBy(() -> CrossEncoderReranker.flattenLogits(new float[] {0.5f}, 2))
                .isInstanceOf(RerankUpstreamException.class);
        assertThatThrownBy(() -> CrossEncoderReranker.flattenLogits("nonsense", 2))
                .isInstanceOf(RerankUpstreamException.class);
    }

    // ── Input validation (mirrors VoyageReranker semantics) ──────────────────

    @Test
    void blankQueryAndBadTopKThrowWithoutTouchingModel(@TempDir Path tmp) {
        var rr = rerankerAt(tmp);
        assertThatThrownBy(() -> rr.rerank(" ", List.of("a"), null))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> rr.rerank(null, List.of("a"), null))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> rr.rerank("q", List.of("a"), 0))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void emptyDocumentsReturnEmptyWithoutTouchingModel(@TempDir Path tmp) {
        var rr = rerankerAt(tmp);
        assertThat(rr.rerank("q", List.of(), null)).isEmpty();
        assertThat(rr.rerank("q", null, null)).isEmpty();
    }

    @Test
    void moreThanMaxDocsThrowsNeverTruncates(@TempDir Path tmp) {
        List<String> docs = new ArrayList<>(
                Collections.nCopies(CrossEncoderReranker.MAX_DOCS_PER_REQUEST + 1, "d"));
        assertThatThrownBy(() -> rerankerAt(tmp).rerank("q", docs, null))
                .isInstanceOf(IllegalArgumentException.class);
    }

    // ── Missing-model posture: typed degrade, never a boot failure ───────────

    @Test
    void missingModelRaisesTypedRerankUpstreamExceptionWithRemedy(@TempDir Path tmp) {
        // Construction is cheap and touches no I/O (the engine boots before the
        // client may have provisioned the ~91MB artifact); the first rerank call
        // must fail TYPED with the provisioning remedy so the fused stage
        // degrades LOUD instead of 500ing the search.
        var rr = rerankerAt(tmp);

        assertThatThrownBy(() -> rr.rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class)
                .hasMessageContaining("not found")
                .hasMessageContaining("nx");
    }

    private static CrossEncoderReranker rerankerAt(Path dir) {
        return new CrossEncoderReranker(
                dir.resolve("model.onnx").toString(),
                dir.resolve("tokenizer.json").toString());
    }
}
