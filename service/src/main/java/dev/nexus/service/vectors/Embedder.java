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

    @Override
    default void close() {}
}
