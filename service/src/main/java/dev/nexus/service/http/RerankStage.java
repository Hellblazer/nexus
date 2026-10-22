// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.Reranker;
import dev.nexus.service.vectors.RerankUpstreamException;
import dev.nexus.service.vectors.UpstreamAuthException;
import dev.nexus.service.vectors.UpstreamRateLimitedException;
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
 * rate-limited degrade additionally carries a structured
 * {@code "rerank_retry_after_seconds": &lt;long&gt;} (nexus-n75jg, critic
 * finding 2 on the nexus-1vpal review) so a well-behaved client can pace
 * itself without parsing the free-text {@code rerank_error} reason. Absent
 * (never {@code null} on the wire — the key itself is omitted) for every
 * other degrade cause.
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
        return apply(query, rows, topK, null);
    }

    /**
     * {@link #apply(String, List, Integer)} with a cap on the rows SCORED.
     *
     * <p>{@code maxCandidates} (request field {@code rerank_max_candidates}) bounds the
     * scoring cost, which is per document and the dominant cost of the local cross-encoder
     * (Sam, 2026-10-08, T2 nexus/search-latency-local-mode-2026-10-08: about 3.5 to 4.9 s of
     * a 4 to 5 s CLI search scored all 135 candidates). Only the first {@code maxCandidates}
     * rows with text, in the order the caller handed them (distance order), reach the
     * scorer. Every other row stays in the result, after the scored rows and in its
     * original relative order, with no {@code rerank_score}, so the result count and the
     * response shape are unchanged. {@code null} scores every row (today's behaviour); a
     * value at or above the number of scorable rows is the same thing. Textless rows do
     * not use up a slot. Distinct from {@code topK}, which bounds what comes BACK, not
     * what is scored.
     *
     * @throws IllegalArgumentException {@code maxCandidates} is not positive
     */
    Map<String, Object> apply(String query, List<Map<String, Object>> rows, Integer topK,
                              Integer maxCandidates) {
        if (maxCandidates != null && maxCandidates < 1) {
            throw new IllegalArgumentException(
                    "rerank_max_candidates must be at least 1, got " + maxCandidates);
        }
        if (rows.isEmpty()) {
            return envelope(rows, false, null, null);
        }
        if (reranker == null) {
            return degrade(rows, topK, "no reranker configured");
        }

        // Exclude textless rows (reference-only chunks): docIndexToRowIndex maps
        // the reranker's filtered-list indices back to the original rows.
        List<String> documents = new ArrayList<>(rows.size());
        List<Integer> docIndexToRowIndex = new ArrayList<>(rows.size());
        for (int i = 0; i < rows.size(); i++) {
            if (maxCandidates != null && documents.size() >= maxCandidates) break;
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
        } catch (UpstreamRateLimitedException e) {
            // nexus-1vpal: Voyage is rate limiting and the reranker's bounded
            // budget could not absorb it. Rate-limited reranking is a DEGRADED
            // search, not a broken one — results still serve in distance
            // order, and the loud degrade reason carries the retry-after so
            // the client can pace itself. Deliberately NOT propagated to the
            // handler's whole-request 429 arm: a degrade path exists here.
            log.warn("event=rerank_degraded reason=upstream_rate_limited retry_after_s={} error=\"{}\"",
                     e.retryAfterSeconds(), e.getMessage());
            return degrade(rows, topK, "Voyage AI is rate limiting the reranker (retry after ~"
                    + e.retryAfterSeconds() + "s): " + e.getMessage(), e.retryAfterSeconds());
        } catch (RerankUpstreamException | UpstreamAuthException e) {
            log.warn("event=rerank_degraded reason=upstream_failure error=\"{}\"", e.getMessage());
            return degrade(rows, topK, e.getMessage());
        } catch (LinkageError e) {
            // nexus-lt84r: "cannot load" covers a native-load failure. A local reranker whose ONNX Runtime
            // native library will not load throws UnsatisfiedLinkError, or NoClassDefFoundError once the
            // class's static initialiser has failed. Both are Errors, not Exceptions, so they bypassed the
            // two arms above and became a 500 instead of a loud degrade. The rows still serve in distance
            // order, and nothing is sent to another provider in its place.
            log.warn("event=rerank_degraded reason=native_load_failed error=\"{}: {}\"",
                     e.getClass().getSimpleName(), e.getMessage());
            return degrade(rows, topK, "the local reranker cannot load its native runtime ("
                    + e.getClass().getSimpleName() + ": " + e.getMessage() + "); re-provision with `"
                    + dev.nexus.service.vectors.BootPlan.PROVISION_COMMAND + "` or check the engine's native library path");
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
        // Unscored rows (textless, beyond maxCandidates, or beyond the scorer's topK) rank below every
        // scored row, keeping their original relative order.
        for (int i = 0; i < rows.size(); i++) {
            if (!taken[i]) ordered.add(rows.get(i));
        }
        return envelope(truncate(ordered, topK), false, reranker.modelToken(), null);
    }

    /**
     * The rerank of a per-collection search with the lexical leg (nexus-xd2rc): {@code primary}, the
     * vector rows, is scored as {@link #apply(String, List, Integer, Integer)} scores it ({@code
     * maxCandidates} applies); {@code extras}, the lexical rows the vector rows do not hold, are scored in
     * their own calls of at most {@code maxDocsPerCall} rows and are NOT capped by {@code maxCandidates}:
     * a lexical-only hit can sit deep in distance order, and an unscored row lands in the tail a client
     * drops. Both legs' scores come from one scorer for one query, so they are ordered against each other:
     * every scored row by {@code rerank_score} descending (ties keep the input order), then every unscored
     * row in its input order, then {@code topK}. A degrade in any call degrades the whole result, with the
     * first error and the largest retry-after.
     */
    Map<String, Object> applyUnion(String query, List<Map<String, Object>> primary,
                                   List<Map<String, Object>> extras, Integer topK, Integer maxCandidates,
                                   int maxDocsPerCall) {
        if (extras.isEmpty()) {
            return apply(query, primary, topK, maxCandidates);
        }
        List<Map<String, Object>> envelopes = new ArrayList<>();
        envelopes.add(apply(query, primary, null, maxCandidates));
        for (int from = 0; from < extras.size(); from += maxDocsPerCall) {
            envelopes.add(apply(query, extras.subList(from, Math.min(extras.size(), from + maxDocsPerCall)),
                                null, null));
        }
        List<Map<String, Object>> scored = new ArrayList<>();
        List<Map<String, Object>> unscored = new ArrayList<>();
        boolean degraded = false;
        String model = null;
        String error = null;
        Long retryAfter = null;
        for (Map<String, Object> env : envelopes) {
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> results = (List<Map<String, Object>>) env.get("results");
            for (Map<String, Object> row : results) {
                (row.get("rerank_score") instanceof Number ? scored : unscored).add(row);
            }
            degraded |= Boolean.TRUE.equals(env.get("rerank_degraded"));
            if (model == null && env.get("rerank_model") instanceof String m) model = m;
            if (error == null && env.get("rerank_error") instanceof String e) error = e;
            if (env.get("rerank_retry_after_seconds") instanceof Long r) {
                retryAfter = retryAfter == null ? r : Math.max(retryAfter, r);
            }
        }
        scored.sort(java.util.Comparator.comparingDouble(
                (Map<String, Object> row) -> ((Number) row.get("rerank_score")).doubleValue()).reversed());
        List<Map<String, Object>> ordered = new ArrayList<>(scored.size() + unscored.size());
        ordered.addAll(scored);
        ordered.addAll(unscored);
        return envelope(truncate(ordered, topK), degraded, model, error, retryAfter);
    }

    private Map<String, Object> degrade(List<Map<String, Object>> rows, Integer topK, String reason) {
        return degrade(rows, topK, reason, null);
    }

    /**
     * @param retryAfterSeconds structured rate-limit signal (nexus-n75jg);
     *                          {@code null} for every degrade cause OTHER
     *                          than {@link UpstreamRateLimitedException} —
     *                          the field is omitted from the wire envelope
     *                          entirely rather than emitted as a JSON null.
     */
    private Map<String, Object> degrade(List<Map<String, Object>> rows, Integer topK, String reason,
                                        Long retryAfterSeconds) {
        return envelope(truncate(rows, topK), true, null, reason, retryAfterSeconds);
    }

    private static Map<String, Object> envelope(List<Map<String, Object>> results,
                                                boolean degraded, String model, String error) {
        return envelope(results, degraded, model, error, null);
    }

    private static Map<String, Object> envelope(List<Map<String, Object>> results,
                                                boolean degraded, String model, String error,
                                                Long retryAfterSeconds) {
        Map<String, Object> env = new LinkedHashMap<>();
        env.put("results", results);
        env.put("rerank_degraded", degraded);
        if (model != null) env.put("rerank_model", model);
        if (error != null) env.put("rerank_error", error);
        if (retryAfterSeconds != null) env.put("rerank_retry_after_seconds", retryAfterSeconds);
        return env;
    }

    private static List<Map<String, Object>> truncate(List<Map<String, Object>> rows, Integer topK) {
        return (topK != null && rows.size() > topK) ? rows.subList(0, topK) : rows;
    }
}
