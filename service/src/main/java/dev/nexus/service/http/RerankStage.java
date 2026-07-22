// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.Reranker;
import dev.nexus.service.vectors.RerankUpstreamException;
import dev.nexus.service.vectors.UpstreamAuthException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-188 bead nexus-9o6y2.2 — the fused rerank stage (R2 Option A) shared by
 * the five {@code /v1/vectors/*} search handlers.
 *
 * <p>Reranks rows ALREADY FETCHED under RLS inside the same request — no new
 * tenancy surface, one round trip preserved. The response is an object
 * envelope (only built when the caller opted in with {@code rerank=true}; the
 * bare-array shape of non-rerank responses is untouched):
 *
 * <pre>
 * success:  {"results": [rows reordered, each + "rerank_score"],
 *            "rerank_degraded": false, "rerank_model": "rerank-2.5"}
 * degraded: {"results": [rows in distance order],
 *            "rerank_degraded": true, "rerank_error": "&lt;reason&gt;"}
 * </pre>
 *
 * <p><strong>The degrade is LOUD and structured, never silent</strong> (the
 * locked RDR-188 invariant): any scoring failure — upstream outage, credential
 * rejection, invalid upstream payload, no reranker configured — returns the
 * distance-order rows WITH {@code rerank_degraded=true} and a reason. The
 * client surfaces that field (Gap 2's WARN-only invisibility must not reappear
 * one layer up). Caller errors ({@link IllegalArgumentException}, e.g. the
 * 1000-doc cap) propagate to the handler's 400 mapping — a bad request is not
 * a degrade.
 *
 * <p>Rows whose {@code content} is null/blank (reference-only chunks, RDR-169
 * G4) cannot be scored: they are excluded from the scoring call and rank below
 * every scored row, in their original relative order. The reranker's indices
 * refer to the filtered document list and are re-mapped to the original rows
 * here; an out-of-range/duplicate index from the scorer degrades loud rather
 * than corrupting the row mapping.
 */
final class RerankStage {

    private static final Logger log = LoggerFactory.getLogger(RerankStage.class);

    private final Reranker reranker;   // null = no reranker configured (degrade loud)

    RerankStage(Reranker reranker) {
        this.reranker = reranker;
    }

    /**
     * Build the rerank response envelope for {@code rows}.
     *
     * @param query the search query text (the rerank query)
     * @param rows  result rows from the repository, in distance order
     * @param topK  optional cap; applied to the final list on both the success
     *              and the degraded path (the caller sized its consumer for topK)
     */
    Map<String, Object> apply(String query, List<Map<String, Object>> rows, Integer topK) {
        if (rows.isEmpty()) {
            return envelope(rows, false, null, null);
        }
        if (reranker == null) {
            return degrade(rows, topK, "no reranker configured: the engine has neither Voyage"
                    + " credentials nor a local cross-encoder for this deployment");
        }

        // Exclude textless rows (reference-only chunks): docIndexToRowIndex maps
        // the reranker's filtered-list indices back to the original rows.
        List<String> documents = new ArrayList<>(rows.size());
        List<Integer> docIndexToRowIndex = new ArrayList<>(rows.size());
        for (int i = 0; i < rows.size(); i++) {
            Object content = rows.get(i).get("content");
            if (content instanceof String s && !s.isBlank()) {
                documents.add(s);
                docIndexToRowIndex.add(i);
            }
        }
        if (documents.isEmpty()) {
            return degrade(rows, topK, "no rerankable content in result rows (all "
                    + rows.size() + " rows are reference-only/textless)");
        }

        List<Reranker.Scored> scored;
        try {
            scored = reranker.rerank(query, documents, topK);
        } catch (RerankUpstreamException | UpstreamAuthException e) {
            log.warn("event=rerank_degraded reason=upstream_failure error=\"{}\"", e.getMessage());
            return degrade(rows, topK, e.getMessage());
        }

        boolean[] taken = new boolean[rows.size()];
        List<Map<String, Object>> ordered = new ArrayList<>(rows.size());
        for (Reranker.Scored s : scored) {
            if (s.index() < 0 || s.index() >= documents.size()) {
                log.warn("event=rerank_degraded reason=invalid_scorer_index index={} docs={}",
                         s.index(), documents.size());
                return degrade(rows, topK, "reranker returned invalid document index "
                        + s.index() + " for " + documents.size() + " scored documents");
            }
            int rowIndex = docIndexToRowIndex.get(s.index());
            if (taken[rowIndex]) {
                log.warn("event=rerank_degraded reason=duplicate_scorer_index index={}", s.index());
                return degrade(rows, topK, "reranker returned duplicate document index " + s.index());
            }
            taken[rowIndex] = true;
            Map<String, Object> row = new LinkedHashMap<>(rows.get(rowIndex));
            row.put("rerank_score", s.relevanceScore());
            ordered.add(row);
        }
        // Unscored rows (textless, or beyond the scorer's topK) rank below every
        // scored row, keeping their original relative order.
        for (int i = 0; i < rows.size(); i++) {
            if (!taken[i]) ordered.add(rows.get(i));
        }
        return envelope(truncate(ordered, topK), false, reranker.modelToken(), null);
    }

    private Map<String, Object> degrade(List<Map<String, Object>> rows, Integer topK, String reason) {
        return envelope(truncate(rows, topK), true, null, reason);
    }

    private static Map<String, Object> envelope(List<Map<String, Object>> results,
                                                boolean degraded, String model, String error) {
        Map<String, Object> env = new LinkedHashMap<>();
        env.put("results", results);
        env.put("rerank_degraded", degraded);
        if (model != null) env.put("rerank_model", model);
        if (error != null) env.put("rerank_error", error);
        return env;
    }

    private static List<Map<String, Object>> truncate(List<Map<String, Object>> rows, Integer topK) {
        return (topK != null && rows.size() > topK) ? rows.subList(0, topK) : rows;
    }
}
