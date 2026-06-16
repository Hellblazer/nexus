// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.vectors.Bge768Embedder;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.assertj.core.data.Offset;

import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-160 P1.1 (bead nexus-zxqs4) — bge-768 parity gate. THE go/no-go for RDR-160.
 *
 * <p>Asserts the Java {@link Bge768Embedder} reproduces the Python <b>fastembed</b>
 * {@code BAAI/bge-base-en-v1.5} reference within tolerance. The reference vectors are
 * a COMMITTED fixture ({@code resources/bge768/parity_reference.json}) captured once
 * via fastembed during the CA-1 spike (2026-06-15); see T2
 * {@code nexus_rdr/160-CA1-parity-spike-VERIFIED-2026-06-15}.
 *
 * <p>Recipe under test (CA-2, VERIFIED — see RDR-160 §Decision):
 * <ul>
 *   <li><b>CLS pooling</b> (token 0 of {@code last_hidden_state}), NOT MiniLM's mean-pool</li>
 *   <li>L2-normalize, dim 768, MAX_SEQ_LEN 512, {@code token_type_ids} all-zero, NO prefix</li>
 * </ul>
 *
 * <p><b>Model artifact (CA-1 caveat, RF-160-1):</b> loads the STANDARD un-fused bge ONNX
 * export (Xenova/bge-base-en-v1.5 {@code model.onnx}, fp32). It is NOT fastembed's qdrant
 * {@code model_optimized.onnx} — that uses the fused contrib op {@code SkipLayerNormalization}
 * which onnxruntime-java 1.20.0 cannot run. The ~416MB model is not committed; it is
 * provisioned by {@code nx init --service} (RDR-160 P3) to the canonical Java-read path, or
 * pointed at via the {@code nexus.bge.modelPath}/{@code nexus.bge.tokenizerPath} system
 * properties. When the model is absent the test is SKIPPED with a loud message (a JUnit
 * assumption, reported as skipped) — never a vacuous pass.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Bge768ParityTest {

    /** Min cosine across all fixture texts. CA-1 measured 0.99999229 headroom. */
    private static final double PARITY_MIN_COSINE = 0.9999;
    private static final int    EXPECTED_DIM       = 768;
    private static final int    EXPECTED_FIXTURE_TEXTS = 6;

    private List<String>  texts;
    private List<float[]> referenceVectors;
    private Bge768Embedder embedder;

    @BeforeAll
    void setUp() throws Exception {
        // ── Load the committed fastembed reference fixture ───────────────────
        ObjectMapper mapper = new ObjectMapper();
        JsonNode root;
        try (InputStream in = getClass().getResourceAsStream("/bge768/parity_reference.json")) {
            assertThat(in).as("committed parity fixture must be on the classpath").isNotNull();
            root = mapper.readTree(in);
        }
        assertThat(root.get("dim").asInt())
                .as("fixture dim must be 768").isEqualTo(EXPECTED_DIM);

        texts = new ArrayList<>();
        for (JsonNode t : root.get("texts")) texts.add(t.asText());
        referenceVectors = new ArrayList<>();
        for (JsonNode vec : root.get("vectors")) {
            float[] v = new float[vec.size()];
            for (int d = 0; d < vec.size(); d++) v[d] = (float) vec.get(d).asDouble();
            referenceVectors.add(v);
        }
        // Exact-count assertions (not inequalities) guard fixture integrity.
        assertThat(texts).as("fixture text count").hasSize(EXPECTED_FIXTURE_TEXTS);
        assertThat(referenceVectors).as("fixture vector count").hasSize(EXPECTED_FIXTURE_TEXTS);

        // ── Resolve the standard un-fused bge ONNX + tokenizer ───────────────
        String modelPath = System.getProperty("nexus.bge.modelPath", Bge768Embedder.DEFAULT_MODEL_PATH);
        String tokPath   = System.getProperty("nexus.bge.tokenizerPath", Bge768Embedder.DEFAULT_TOKENIZER_PATH);

        boolean present = Files.isRegularFile(Path.of(modelPath)) && Files.isRegularFile(Path.of(tokPath));
        Assumptions.assumeTrue(present, () ->
                "bge-768 parity gate SKIPPED: standard un-fused bge ONNX not found.\n" +
                "  expected model:     " + modelPath + "\n" +
                "  expected tokenizer: " + tokPath + "\n" +
                "  Provision via `nx init --service` (RDR-160 P3), or set -Dnexus.bge.modelPath / " +
                "-Dnexus.bge.tokenizerPath.\n" +
                "  MUST be the STANDARD export (Xenova model.onnx, fp32) — NOT fastembed's " +
                "model_optimized.onnx (fused SkipLayerNormalization fails on onnxruntime-java).");

        embedder = new Bge768Embedder(modelPath, tokPath);
    }

    @AfterAll
    void tearDown() {
        if (embedder != null) embedder.close();
    }

    @Test
    void modelToken_is_bge768() {
        assertThat(embedder.modelToken()).isEqualTo("bge-base-en-v15-768");
    }

    @Test
    void embedding_dimension_is_768() {
        float[] v = embedder.embedOne(texts.get(0));
        assertThat(v).hasSize(EXPECTED_DIM);
    }

    @Test
    void embedding_is_l2_normalized() {
        for (String text : texts) {
            float[] v = embedder.embedOne(text);
            double norm = 0.0;
            for (float f : v) norm += (double) f * f;
            assertThat(Math.sqrt(norm))
                    .as("bge embedding must be L2-normalized (norm ≈ 1.0) for: %s", preview(text))
                    .isCloseTo(1.0, Offset.offset(1e-5));
        }
    }

    /**
     * THE parity gate. Min cosine vs the fastembed reference across every fixture
     * text must clear 0.9999. CA-1 measured a 0.99999229 floor on this exact fixture.
     */
    @Test
    void parity_minCosine_meetsGate() {
        double minCosine = 1.0;
        for (int i = 0; i < texts.size(); i++) {
            float[] got = embedder.embedOne(texts.get(i));
            float[] ref = referenceVectors.get(i);
            assertThat(got).as("dim must match reference for text[%d]", i).hasSize(ref.length);

            double dot = 0.0, nG = 0.0, nR = 0.0;
            for (int d = 0; d < ref.length; d++) {
                dot += (double) got[d] * ref[d];
                nG  += (double) got[d] * got[d];
                nR  += (double) ref[d] * ref[d];
            }
            double cosine = dot / (Math.sqrt(nG) * Math.sqrt(nR));
            assertThat(cosine)
                    .as("bge-768 parity vs fastembed for text[%d] %s", i, preview(texts.get(i)))
                    .isGreaterThanOrEqualTo(PARITY_MIN_COSINE);
            minCosine = Math.min(minCosine, cosine);
        }
        assertThat(minCosine)
                .as("RDR-160 go/no-go: min cosine across fixture must meet the parity gate")
                .isGreaterThanOrEqualTo(PARITY_MIN_COSINE);
    }

    private static String preview(String text) {
        String oneLine = text.replace('\n', '⏎');
        return '"' + oneLine.substring(0, Math.min(30, oneLine.length())) + '"';
    }
}
