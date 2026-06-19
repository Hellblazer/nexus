// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;

/**
 * RDR-152 bead nexus-gmiaf.20 — common embedding interface for Seam B.
 *
 * <p>Implementations:
 * <ul>
 *   <li>{@link OnnxEmbedder} — LOCAL: onnxruntime-java + DJL HF tokenizer,
 *       exact S0.2 pipeline (cosine 1.0 vs Python chromadb ONNXMiniLM_L6_V2)</li>
 *   <li>{@link VoyageEmbedder} — CLOUD: Voyage AI REST with truncation=true envelope</li>
 * </ul>
 */
public interface Embedder extends AutoCloseable {

    /**
     * Embed a batch of texts.  Returns one float[] per input text (in order).
     *
     * @param texts list of text strings to embed
     * @return list of embedding vectors, aligned with input
     */
    List<float[]> embed(List<String> texts);

    /**
     * Embed a single text — convenience wrapper over {@link #embed(List)}.
     */
    default float[] embedOne(String text) {
        return embed(List.of(text)).get(0);
    }

    /**
     * Embed a batch of texts and return both the vectors and the token count
     * consumed by the embedding call (bead nexus-ehc4q).
     *
     * <p>Default implementation returns 0 for {@code tokens} so that
     * {@link FakeEmbedder} and any custom implementations compile without
     * change. Production embedders ({@link VoyageEmbedder}, {@link CceEmbedder},
     * {@link OnnxEmbedder}) override this to return the real count.
     *
     * @param texts list of text strings to embed
     * @return {@link EmbedResult} carrying vectors (aligned with input) and
     *         the total token count ({@code 0} when unknown)
     */
    default EmbedResult embedWithUsage(List<String> texts) {
        return new EmbedResult(embed(texts), 0L);
    }

    /**
     * RDR-103 embedding-model token this embedder produces (the collection-name
     * model segment, e.g. {@code "voyage-code-3"}, {@code "minilm-l6-v2-384"}).
     *
     * <p>Default {@code "unknown"} keeps test fakes compiling (the locked
     * {@code PgVectorRepositoryContractTest.FakeEmbedder} is additive-only);
     * every production embedder overrides (bead nexus-pebfx.2 model-identity
     * validation).
     */
    default String modelToken() {
        return "unknown";
    }

    @Override
    default void close() {}
}
