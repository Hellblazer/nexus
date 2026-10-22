// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
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

    // ── Live availability (nexus-yuh4a) ─────────────────────────────────────

    @Test
    void availableTracksTheFilesOnDiskLive_andReportsPresenceNotLoadability(@TempDir Path dir) throws Exception {
        Path model = dir.resolve("onnx/model.onnx");
        Path tokenizer = dir.resolve("onnx/tokenizer.json");
        var reranker = new CrossEncoderReranker(model.toString(), tokenizer.toString());

        assertThat(reranker.available()).as("neither file").isFalse();

        Files.createDirectories(model.getParent());
        Files.writeString(model, "m");
        assertThat(reranker.available()).as("model only: a half install is not available").isFalse();

        Files.writeString(tokenizer, "t");
        assertThat(reranker.available()).as("both files appeared after construction: read live from the files")
                .isTrue();

        Files.delete(model);
        assertThat(reranker.available()).as("a file removed again: flips back").isFalse();
    }

    @Test
    void filesThatFailToLoadReadUnavailableUntilTheyChange(@TempDir Path dir) throws Exception {
        // nexus-vk1l1: present files that cannot load read true until a rerank tries them; after the
        // failed load they read false for as long as the files are the ones that failed, and true again
        // once either file is replaced, so a re-provision is picked up without a restart.
        Path model = dir.resolve("model.onnx");
        Path tokenizer = dir.resolve("tokenizer.json");
        Files.writeString(model, "not an onnx model");
        Files.writeString(tokenizer, "not a tokenizer");
        var reranker = new CrossEncoderReranker(model.toString(), tokenizer.toString());
        assertThat(reranker.available()).as("never tried: presence is all it knows").isTrue();

        assertThatThrownBy(() -> reranker.rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class)
                .hasMessageContaining("failed to initialise");
        assertThat(reranker.available()).as("the files that failed to load are still there").isFalse();
        assertThatThrownBy(() -> reranker.rerank("q", List.of("a"), null))
                .as("every rerank retries the load, whatever available() says")
                .isInstanceOf(RerankUpstreamException.class);
        assertThat(reranker.available()).as("the same files failed again").isFalse();

        Files.writeString(model, "a re-provisioned model of another size");
        assertThat(reranker.available()).as("the model was replaced: worth another try").isTrue();

        assertThatThrownBy(() -> reranker.rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class);
        assertThat(reranker.available()).as("the replacement failed too").isFalse();

        Files.writeString(tokenizer, "a re-provisioned tokenizer, longer than before");
        assertThat(reranker.available()).as("the tokenizer was replaced").isTrue();

        assertThatThrownBy(() -> reranker.rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class);
        assertThat(reranker.available()).isFalse();
        Files.setLastModifiedTime(model, FileTime.fromMillis(Files.getLastModifiedTime(model).toMillis() + 60_000));
        assertThat(reranker.available()).as("same size, new mtime: a same-size replacement still counts").isTrue();
    }

    @Test
    void availableIsTrueWhenNoRerankerDeclaresOtherwise_theInterfaceDefault() {
        Reranker voyageLike = new Reranker() {
            @Override public String modelToken() { return "rerank-2.5"; }
            @Override public List<Scored> rerank(String q, List<String> d, Integer k) { return List.of(); }
        };
        assertThat(voyageLike.available()).isTrue();
    }

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
