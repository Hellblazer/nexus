// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-188 bead nexus-9o6y2.3 — REAL-inference gate for the local cross-encoder:
 * loads the ms-marco-MiniLM-L-6-v2 ONNX (~91MB, fp32) through onnxruntime-java +
 * the DJL tokenizer's PAIR encoding and asserts relevance ordering on obvious
 * query/document pairs.
 *
 * <p>This is the R4 verification that the official cross-encoder export runs on
 * onnxruntime-java (the bge lesson: fastembed's fused {@code SkipLayerNormalization}
 * export did NOT — RDR-160 CA-1). Skipped when the artifact is absent (CI has no
 * model; same gating idiom as {@code Bge768ParityTest}); provisioned installs and
 * dev boxes run it for real.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CrossEncoderRerankerInferenceTest {

    private CrossEncoderReranker reranker;

    @BeforeAll
    void setUp() {
        String modelPath = System.getProperty("nexus.crossencoder.modelPath",
                CrossEncoderReranker.DEFAULT_MODEL_PATH);
        String tokPath   = System.getProperty("nexus.crossencoder.tokenizerPath",
                CrossEncoderReranker.DEFAULT_TOKENIZER_PATH);

        boolean present = Files.isRegularFile(Path.of(modelPath)) && Files.isRegularFile(Path.of(tokPath));
        Assumptions.assumeTrue(present, () ->
                "cross-encoder inference gate SKIPPED: ms-marco-MiniLM ONNX not found.\n"
                + "  expected model:     " + modelPath + "\n"
                + "  expected tokenizer: " + tokPath + "\n"
                + "  Provision via `nx init` (RDR-188 P1.3), or set "
                + "-Dnexus.crossencoder.modelPath / -Dnexus.crossencoder.tokenizerPath.");

        reranker = new CrossEncoderReranker(modelPath, tokPath);
    }

    @AfterAll
    void tearDown() {
        if (reranker != null) reranker.close();
    }

    @Test
    void modelTokenIsMsMarcoMiniLm() {
        assertThat(reranker.modelToken()).isEqualTo("ms-marco-minilm-l6-v2");
    }

    @Test
    void ranksObviouslyRelevantDocumentFirst() {
        List<Reranker.Scored> out = reranker.rerank(
                "how do I bake sourdough bread",
                List.of(
                        "The Treaty of Westphalia in 1648 ended the Thirty Years' War in Europe.",
                        "Mix flour, water, and sourdough starter, let the dough ferment overnight, "
                                + "then shape and bake it in a hot dutch oven.",
                        "Quantum chromodynamics describes the strong interaction between quarks."),
                null);

        assertThat(out).hasSize(3);
        assertThat(out.get(0).index()).isEqualTo(1);
        // Ordered by score descending, all three input indices present exactly once.
        assertThat(out).extracting(Reranker.Scored::relevanceScore)
                .isSortedAccordingTo((a, b) -> Double.compare(b, a));
        assertThat(out).extracting(Reranker.Scored::index).containsExactlyInAnyOrder(0, 1, 2);
    }

    @Test
    void topKReturnsOnlyTheBestDocument() {
        List<Reranker.Scored> out = reranker.rerank(
                "what is the capital of France",
                List.of("Paris is the capital and largest city of France.",
                        "A capital gains tax is levied on profit from the sale of an asset.",
                        "The recipe calls for two cups of rice."),
                1);

        assertThat(out).hasSize(1);
        assertThat(out.get(0).index()).isEqualTo(0);
    }

    @Test
    void longDocumentIsTruncatedNotFatal() {
        // > 512 tokens: the tokenizer truncates the PAIR; scoring must succeed.
        String longDoc = "bread baking oven flour ".repeat(400);
        List<Reranker.Scored> out = reranker.rerank(
                "how do I bake bread", List.of(longDoc, "unrelated tax law text"), null);

        assertThat(out).hasSize(2);
    }
}
