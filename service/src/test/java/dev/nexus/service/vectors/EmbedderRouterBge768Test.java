// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-160 P2 (bead nexus-jl9z5) — production-shaped local-mode routing with the
 * bge-768 embedder wired (the model Main wires in local mode).
 *
 * <p>Model-free: uses a {@link FakeBge} whose {@code modelToken()} is
 * {@code "bge-base-en-v15-768"}, so this verifies the ROUTING contract without
 * the 416MB ONNX (the cosine parity itself is {@code Bge768ParityTest}).
 *
 * <p>The load-bearing P2 assertion is {@link #minilmCollection_refused_noSilentFallback}:
 * on a bge-only local service a {@code minilm-l6-v2-384} collection must be
 * REFUSED, never silently embedded at 384-dim into a 768 table.
 */
class EmbedderRouterBge768Test {

    /** A stand-in for {@link Bge768Embedder} that needs no model file. */
    private static final class FakeBge implements Embedder {
        @Override public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> new float[768]).toList();
        }
        @Override public String modelToken() {
            return "bge-base-en-v15-768";
        }
    }

    private final EmbedderRouter router = new EmbedderRouter(new FakeBge(), "document");

    @Test
    void localMode_routesBgeCollection_toBgeEmbedder() {
        Embedder e = router.resolveEmbedderStrict("knowledge__nexus__bge-base-en-v15-768__v1");
        assertThat(e.modelToken()).isEqualTo("bge-base-en-v15-768");
        // every conformant prefix routes to bge in local mode
        for (String col : List.of(
                "docs__nexus__bge-base-en-v15-768__v1",
                "rdr__nexus__bge-base-en-v15-768__v1",
                "code__nexus__bge-base-en-v15-768__v1")) {
            assertThat(router.resolveEmbedderStrict(col).modelToken())
                    .as("local-mode bge routing for %s", col)
                    .isEqualTo("bge-base-en-v15-768");
        }
    }

    @Test
    void minilmCollection_refused_noSilentFallback() {
        // The whole point of RDR-160 P2: a MiniLM collection on the bge-only
        // local service must REFUSE, not degrade to a 384-dim embed.
        assertThatThrownBy(() -> router.resolveEmbedderStrict(
                "knowledge__nexus__minilm-l6-v2-384__v1"))
                .isInstanceOf(EmbeddingModelUnavailableException.class)
                .hasMessageContaining("minilm-l6-v2-384");
    }

    @Test
    void voyageCollection_refused_inLocalMode() {
        assertThatThrownBy(() -> router.resolveEmbedderStrict(
                "knowledge__nexus__voyage-context-3__v1"))
                .isInstanceOf(EmbeddingModelUnavailableException.class);
    }

    @Test
    void availableModels_and_modeName_reflectBge() {
        // modeName stays "onnx-local" — the RUNTIME is local ONNX; only the MODEL
        // changed (RDR-160). The model identity is surfaced via availableModels.
        assertThat(router.modeName()).isEqualTo("onnx-local");
        assertThat(router.availableModels()).containsExactly("bge-base-en-v15-768");
    }

    @Test
    void nonConformantName_fallsBackToLocalBge() {
        // Legacy non-conformant names use prefix routing → the local embedder.
        Embedder e = router.resolveEmbedderStrict("knowledge__test");
        assertThat(e.modelToken()).isEqualTo("bge-base-en-v15-768");
    }

    /**
     * RDR-160 P4.3 (bead nexus-x9cjh) — the production embed-dispatch composition
     * with the REAL Bge768Embedder (not FakeBge): provisioned model → router
     * resolves a bge collection to it → embedDoubleForCollection yields a 768-dim
     * vector. Closes the fetch→load→embed→768 chain that the fake cannot.
     * Skipped (loud) when the 416MB model is absent; the live HTTP layer over this
     * is model-agnostic and covered by VectorHandlerEmbeddingModeTest.
     */
    @Test
    void realBge_embedForCollection_yields768Dim() {
        String modelPath = System.getProperty("nexus.bge.modelPath", Bge768Embedder.DEFAULT_MODEL_PATH);
        String tokPath = System.getProperty("nexus.bge.tokenizerPath", Bge768Embedder.DEFAULT_TOKENIZER_PATH);
        Assumptions.assumeTrue(
                Files.isRegularFile(Path.of(modelPath)) && Files.isRegularFile(Path.of(tokPath)),
                "bge-768 model absent — provision via `nx init --service` (RDR-160 P3)");

        try (Bge768Embedder bge = new Bge768Embedder(modelPath, tokPath)) {
            EmbedderRouter real = new EmbedderRouter(bge, "document");
            // a conformant bge collection (→ chunks_768) routes to the real embedder
            List<double[]> vecs = real.embedDoubleForCollection(
                    "knowledge__nexus__bge-base-en-v15-768__v1", List.of("fresh --service boot smoke"));
            assertThat(vecs).hasSize(1);
            assertThat(vecs.get(0)).hasSize(768);
        }
    }
}
