// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;

/**
 * RDR-188: scores {@code (query, document)} pairs for the fused rerank stage on
 * the {@code /v1/vectors/*} search handlers.
 *
 * <p>Implementations: {@link VoyageReranker} (cloud, rerank-2.5); the P1.3
 * server-side DJL/ONNX cross-encoder covers the no-Voyage local posture
 * (RDR-109/160 lineage — server-side rerank must never resurrect a client key
 * requirement).
 *
 * <p>Failure contract: upstream/scoring failure raises a typed exception
 * ({@link RerankUpstreamException}, or {@link UpstreamAuthException} for
 * credential rejection) — the handler converts it into a LOUD structured
 * degraded-rerank field on the response. Implementations never fall back to
 * input order silently.
 */
public interface Reranker {

    /** One reranked document: {@code index} into the input list, higher score = more relevant. */
    record Scored(int index, double relevanceScore) {
    }

    /** The model identifier reported in the response envelope (e.g. {@code "rerank-2.5"}). */
    String modelToken();

    /**
     * Rerank {@code documents} against {@code query}, returning
     * {@code (input index, relevance score)} pairs ordered by relevance
     * descending. With {@code topK} set, at most {@code topK} entries return.
     *
     * @throws IllegalArgumentException blank query, non-positive topK, or an
     *         implementation-specific document cap exceeded (never truncated)
     * @throws UpstreamAuthException    the scoring provider rejected the service's key
     * @throws RerankUpstreamException  any other scoring failure
     */
    List<Scored> rerank(String query, List<String> documents, Integer topK);
}
