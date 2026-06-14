// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;

/**
 * Bead nexus-ehc4q — embedding result carrying vectors PLUS the token count
 * consumed by the embedding API call.
 *
 * <p>Returned by {@link Embedder#embedWithUsage} and the {@code *WithUsage}
 * helpers on {@link EmbedderRouter}. The token count is surfaced to callers
 * so the serving layer can emit it as the {@code X-Nexus-Usage-Tokens}
 * response header for the edge proxy to ingest.
 *
 * <p>Token semantics by implementation:
 * <ul>
 *   <li>{@link VoyageEmbedder}: {@code usage.total_tokens} from the Voyage
 *       {@code /v1/embeddings} response root — total tokens for the batch.</li>
 *   <li>{@link CceEmbedder}: sum of {@code usage.total_tokens} across the
 *       per-text CCE calls (one API call per text).</li>
 *   <li>{@link OnnxEmbedder}: sum of tokenizer token IDs across the batch
 *       (the ONNX tokenizer's {@code getIds().length} per text, before
 *       MAX_SEQ_LEN clamping).</li>
 *   <li>Default / unknown embedders: 0 (token tracking not available).</li>
 * </ul>
 *
 * @param embeddings  embedding vectors, one per input text, in order
 * @param tokens      total token count consumed; 0 when unavailable
 */
public record EmbedResult(List<float[]> embeddings, long tokens) {
}
