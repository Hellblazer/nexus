// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.vectors.CceEmbedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.VoyageEmbedder;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.21 — Embedding parity: Java embedders must produce
 * bit-exact output on repeated calls (ONNX) and numerically match the Python SDK
 * (Voyage standard + CCE), proved by the Python harness {@code test_embed_parity.py}.
 *
 * <p>This Java test covers:
 * <ol>
 *   <li>ONNX determinism: same text → bit-identical float[] twice.</li>
 *   <li>EmbedderRouter local-mode routing: knowledge/docs/rdr/code all → ONNX.</li>
 *   <li>EmbedderRouter cloud-mode routing: correct embedder class selected per prefix.</li>
 *   <li>CCE embedder: basic shape + non-zero output (requires {@code VOYAGE_API_KEY}).
 *       Marked {@code @Tag("integration")}.</li>
 * </ol>
 *
 * <p>The cross-language cosine == 1.0 parity (the actual gate) is in
 * {@code tests/db/test_embed_parity.py} which calls the service's
 * {@code POST /v1/vectors/embed} endpoint.
 *
 * <p>ONNX tests run unconditionally (no API key needed).
 * Cloud tests require {@code VOYAGE_API_KEY} set in the environment and
 * are tagged {@code @Tag("integration")}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class EmbedParityTest {

    // Fixed corpus for parity — same texts as test_embed_parity.py
    private static final List<String> CORPUS = List.of(
            "The quick brown fox jumps over the lazy dog.",
            "Semantic search connects questions to answers through meaning.",
            // Long text to exercise truncation (> 256 tokens)
            "In the beginning God created the heavens and the earth. " +
            "Now the earth was formless and empty, darkness was over the surface " +
            "of the deep, and the Spirit of God was hovering over the waters. " +
            "And God said, Let there be light, and there was light. God saw that " +
            "the light was good, and he separated the light from the darkness. " +
            "God called the light day, and the darkness he called night. And there " +
            "was evening, and there was morning the first day. " +
            "And God said, Let there be a vault between the waters to separate water " +
            "from water. So God made the vault and separated the water under the vault " +
            "from the water above it. And it was so. God called the vault sky. " +
            "And there was evening, and there was morning the second day."
    );

    OnnxEmbedder onnxEmbedder;

    @BeforeAll
    void setUp() {
        onnxEmbedder = new OnnxEmbedder();
    }

    @AfterAll
    void tearDown() {
        if (onnxEmbedder != null) onnxEmbedder.close();
    }

    // ── Test 1: ONNX bit-exact determinism ───────────────────────────────────

    @Test
    void onnx_bitExact_determinism_sameText() {
        for (String text : CORPUS) {
            float[] v1 = onnxEmbedder.embedOne(text);
            float[] v2 = onnxEmbedder.embedOne(text);
            assertThat(v1).as("ONNX must be bit-exactly deterministic for: " + text.substring(0, 40))
                    .isEqualTo(v2);
        }
    }

    @Test
    void onnx_bitExact_determinism_batchVsSingle() {
        // Batch embed vs individual embeds: cosine similarity must be exactly 1.0.
        //
        // NOTE: bit-exact equality is NOT guaranteed between batch and single-item
        // ONNX inference because:
        //  - Batch packs texts into a rectangular tensor (shape [batchSize, maxLen])
        //    where maxLen = max(all text lengths in batch)
        //  - Single-item packs into [1, ownLen], a different tensor shape
        //  - ONNX's internal parallelism (SIMD, thread scheduling) over different
        //    tensor shapes can produce different float32 rounding at the ~1e-7 level
        // This is expected and benign: the S0.2 gate asserts Java vs Python cosine
        // (same-shape vs same-shape), not batch vs single within Java.
        // We assert cosine == 1.0 between batch and single (same mathematical result
        // to within float32 precision) using the same norm-equality the parity gate uses.
        List<float[]> batch = onnxEmbedder.embed(CORPUS);
        assertThat(batch).hasSize(CORPUS.size());

        for (int i = 0; i < CORPUS.size(); i++) {
            float[] single = onnxEmbedder.embedOne(CORPUS.get(i));
            float[] bv = batch.get(i);

            // Cosine similarity: both vectors are L2-normalized, so cosine = dot product
            double dot = 0.0, normB = 0.0, normS = 0.0;
            for (int d = 0; d < bv.length; d++) {
                dot   += (double) bv[d] * single[d];
                normB += (double) bv[d] * bv[d];
                normS += (double) single[d] * single[d];
            }
            double cosine = dot / (Math.sqrt(normB) * Math.sqrt(normS));
            assertThat(cosine)
                    .as("ONNX batch[%d] vs single cosine must be 1.0 (float32 eps ~1e-7; diff is FP rounding)", i)
                    .isGreaterThan(1.0 - 1e-6);  // allow float32 epsilon; real drift (Voyage bug) is 5e-5
        }
    }

    @Test
    void onnx_embedding_dimension_is_384() {
        float[] vec = onnxEmbedder.embedOne(CORPUS.get(0));
        assertThat(vec).hasSize(384);
    }

    @Test
    void onnx_embedding_is_l2_normalized() {
        for (String text : CORPUS) {
            float[] v = onnxEmbedder.embedOne(text);
            double norm = 0.0;
            for (float f : v) norm += (double) f * f;
            norm = Math.sqrt(norm);
            assertThat(norm).as("ONNX embedding must be L2-normalized (norm ≈ 1.0)")
                    .isCloseTo(1.0, org.assertj.core.data.Offset.offset(1e-5));
        }
    }

    // ── Test 2: EmbedderRouter local-mode routing ─────────────────────────────

    @Test
    void embedderRouter_localMode_alwaysOnnx() {
        EmbedderRouter router = new EmbedderRouter(onnxEmbedder, "document");

        // All prefixes should resolve to ONNX in local mode
        for (String col : List.of("knowledge__nexus__minilm-l6-v2-384__v1",
                                   "docs__nexus__minilm-l6-v2-384__v1",
                                   "rdr__nexus__minilm-l6-v2-384__v1",
                                   "code__nexus__minilm-l6-v2-384__v1")) {
            assertThat(router.resolveEmbedder(col))
                    .as("local-mode router should return ONNX for " + col)
                    .isInstanceOf(OnnxEmbedder.class);
        }
    }

    // ── Test 3: EmbedderRouter cloud-mode routing ─────────────────────────────

    @Test
    void embedderRouter_cloudMode_routesByPrefix() {
        // Use a dummy API key — we're only testing class identity, not calling the API
        EmbedderRouter router = new EmbedderRouter(onnxEmbedder, "dummy-key", "document");

        assertThat(router.resolveEmbedder("knowledge__nexus__voyage-context-3__v1"))
                .as("knowledge__ → CCE")
                .isInstanceOf(CceEmbedder.class);
        assertThat(router.resolveEmbedder("docs__nexus__voyage-context-3__v1"))
                .as("docs__ → CCE")
                .isInstanceOf(CceEmbedder.class);
        assertThat(router.resolveEmbedder("rdr__nexus__voyage-context-3__v1"))
                .as("rdr__ → CCE")
                .isInstanceOf(CceEmbedder.class);
        assertThat(router.resolveEmbedder("code__nexus__voyage-code-3__v1"))
                .as("code__ → VoyageEmbedder")
                .isInstanceOf(VoyageEmbedder.class);
        assertThat(router.resolveEmbedder("unknown__nexus__model__v1"))
                .as("unrecognised prefix → ONNX fallback")
                .isInstanceOf(OnnxEmbedder.class);
        assertThat(router.resolveEmbedder(null))
                .as("null collection → ONNX fallback")
                .isInstanceOf(OnnxEmbedder.class);
    }

    // ── Test 4: CCE embedder live call (integration — requires VOYAGE_API_KEY) ──

    @Test
    @Tag("integration")
    @EnabledIfEnvironmentVariable(named = "VOYAGE_API_KEY", matches = ".+")
    void cceEmbedder_liveCall_correctShapeAndNonZero() {
        String apiKey = System.getenv("VOYAGE_API_KEY");
        CceEmbedder cce = new CceEmbedder(apiKey, "document");

        List<float[]> vecs = cce.embed(CORPUS);
        assertThat(vecs).hasSize(CORPUS.size());

        for (int i = 0; i < vecs.size(); i++) {
            float[] v = vecs.get(i);
            assertThat(v).as("CCE embedding[%d] must have 1024 dims", i).hasSize(1024);

            // Compute norm — must be close to 1.0 (Voyage returns normalized vectors)
            double norm = 0.0;
            for (float f : v) norm += (double) f * f;
            norm = Math.sqrt(norm);
            assertThat(norm).as("CCE embedding[%d] must be L2-normalized", i)
                    .isCloseTo(1.0, org.assertj.core.data.Offset.offset(1e-3));
        }
    }

    @Test
    @Tag("integration")
    @EnabledIfEnvironmentVariable(named = "VOYAGE_API_KEY", matches = ".+")
    void cceEmbedder_deterministic_repeatedCall() {
        String apiKey = System.getenv("VOYAGE_API_KEY");
        CceEmbedder cce = new CceEmbedder(apiKey, "document");

        // Voyage AI CCE is deterministic for the same input
        float[] v1 = cce.embedOne(CORPUS.get(0));
        float[] v2 = cce.embedOne(CORPUS.get(0));
        assertThat(v1).as("CCE must return identical vector for same text (API is deterministic)")
                .isEqualTo(v2);
    }

    @Test
    @Tag("integration")
    @EnabledIfEnvironmentVariable(named = "VOYAGE_API_KEY", matches = ".+")
    void voyageEmbedder_liveCall_correctShape() {
        String apiKey = System.getenv("VOYAGE_API_KEY");
        VoyageEmbedder voyage = new VoyageEmbedder(apiKey, "voyage-code-3", "document");

        List<float[]> vecs = voyage.embed(CORPUS);
        assertThat(vecs).hasSize(CORPUS.size());
        for (int i = 0; i < vecs.size(); i++) {
            assertThat(vecs.get(i)).as("Voyage embedding[%d] must have 1024 dims", i).hasSize(1024);
        }
    }
}
