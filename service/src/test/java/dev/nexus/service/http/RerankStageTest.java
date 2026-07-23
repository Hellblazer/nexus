// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import dev.nexus.service.vectors.Reranker;
import dev.nexus.service.vectors.RerankUpstreamException;
import dev.nexus.service.vectors.UpstreamAuthException;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-188 bead nexus-9o6y2.2 — the fused rerank stage's envelope contract.
 *
 * <p>The LOUD-degrade invariant under test: every response the stage builds
 * carries an explicit {@code rerank_degraded} boolean. Upstream failure returns
 * distance-order rows WITH {@code rerank_degraded=true} + {@code rerank_error}
 * — never a silent fallback to input order (the retired client anti-pattern),
 * and never a failed search (the rows were already fetched under RLS).
 *
 * <p>Also load-bearing: index re-mapping when textless rows (reference-only
 * chunks, {@code content=null}) are excluded from the scoring call — the
 * reranker's indices refer to the FILTERED document list, and the stage must
 * map them back to the original rows or reordering silently corrupts results.
 */
class RerankStageTest {

    /** Scripted reranker: returns a fixed result or throws. */
    private static final class FakeReranker implements Reranker {
        List<Scored> result;
        RuntimeException failure;
        String lastQuery;
        List<String> lastDocuments;
        Integer lastTopK;
        int calls;

        @Override
        public String modelToken() {
            return "fake-rerank";
        }

        @Override
        public List<Scored> rerank(String query, List<String> documents, Integer topK) {
            calls++;
            lastQuery = query;
            lastDocuments = documents;
            lastTopK = topK;
            if (failure != null) throw failure;
            return result;
        }
    }

    private static Map<String, Object> row(String id, String content, double distance) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("id", id);
        row.put("content", content);
        row.put("distance", distance);
        row.put("collection", "knowledge__test");
        return row;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> results(Map<String, Object> envelope) {
        return (List<Map<String, Object>>) envelope.get("results");
    }

    // ── Success path ─────────────────────────────────────────────────────────

    @Test
    void reordersRowsByRerankAndAttachesScores() {
        var fake = new FakeReranker();
        fake.result = List.of(new Reranker.Scored(2, 0.9), new Reranker.Scored(0, 0.5),
                              new Reranker.Scored(1, 0.1));
        var rows = List.of(row("a", "text a", 0.10), row("b", "text b", 0.20),
                           row("c", "text c", 0.30));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), null);

        assertThat(env.get("rerank_degraded")).isEqualTo(false);
        assertThat(env.get("rerank_model")).isEqualTo("fake-rerank");
        assertThat(env).doesNotContainKey("rerank_error");
        List<Map<String, Object>> out = results(env);
        assertThat(out).extracting(r -> r.get("id")).containsExactly("c", "a", "b");
        assertThat(out).extracting(r -> r.get("rerank_score")).containsExactly(0.9, 0.5, 0.1);
        assertThat(fake.lastQuery).isEqualTo("q");
        assertThat(fake.lastDocuments).containsExactly("text a", "text b", "text c");
        assertThat(fake.lastTopK).isNull();
    }

    @Test
    void topKForwardedAndFinalListTruncated() {
        var fake = new FakeReranker();
        fake.result = List.of(new Reranker.Scored(1, 0.8));
        var rows = List.of(row("a", "ta", 0.1), row("b", "tb", 0.2), row("c", "tc", 0.3));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), 1);

        assertThat(fake.lastTopK).isEqualTo(1);
        assertThat(results(env)).extracting(r -> r.get("id")).containsExactly("b");
    }

    @Test
    void emptyRowsShortCircuitWithoutCallingReranker() {
        var fake = new FakeReranker();

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(), null);

        assertThat(env.get("rerank_degraded")).isEqualTo(false);
        assertThat(results(env)).isEmpty();
        assertThat(fake.calls).isZero();
    }

    // ── Textless-row mapping (reference-only chunks) ─────────────────────────

    @Test
    void textlessRowsAreExcludedFromScoringAndRankBelowScoredRows() {
        var fake = new FakeReranker();
        // Docs passed to the reranker are only the two WITH text: [text a, text c].
        // Scored index 1 = "text c" = original row c — the mapping under test.
        fake.result = List.of(new Reranker.Scored(1, 0.9), new Reranker.Scored(0, 0.4));
        var rows = List.of(row("a", "text a", 0.1), row("b", null, 0.2),
                           row("c", "text c", 0.3), row("d", "  ", 0.4));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), null);

        assertThat(fake.lastDocuments).containsExactly("text a", "text c");
        List<Map<String, Object>> out = results(env);
        assertThat(out).extracting(r -> r.get("id")).containsExactly("c", "a", "b", "d");
        assertThat(out.get(0).get("rerank_score")).isEqualTo(0.9);
        assertThat(out.get(2)).doesNotContainKey("rerank_score");
        assertThat(env.get("rerank_degraded")).isEqualTo(false);
    }

    @Test
    void allTextlessRowsDegradeLoud() {
        var fake = new FakeReranker();
        var rows = List.of(row("a", null, 0.1), row("b", "", 0.2));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), null);

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("no rerankable content");
        assertThat(results(env)).extracting(r -> r.get("id")).containsExactly("a", "b");
        assertThat(fake.calls).isZero();
    }

    // ── Degrade paths (LOUD, never silent) ───────────────────────────────────

    @Test
    void upstreamFailureDegradesLoudWithDistanceOrderRows() {
        var fake = new FakeReranker();
        fake.failure = new RerankUpstreamException("Voyage AI rerank failed: HTTP 500");
        var rows = List.of(row("a", "ta", 0.1), row("b", "tb", 0.2));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), null);

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("HTTP 500");
        assertThat(env).doesNotContainKey("rerank_model");
        List<Map<String, Object>> out = results(env);
        assertThat(out).extracting(r -> r.get("id")).containsExactly("a", "b");
        assertThat(out.get(0)).doesNotContainKey("rerank_score");
    }

    @Test
    void authFailureDegradesLoudWithReason() {
        var fake = new FakeReranker();
        fake.failure = new UpstreamAuthException("Voyage AI rejected the service's API key (HTTP 401)");
        var rows = List.of(row("a", "ta", 0.1));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), null);

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("HTTP 401");
    }

    @Test
    void degradedResponseStillHonoursTopK() {
        var fake = new FakeReranker();
        fake.failure = new RerankUpstreamException("boom");
        var rows = List.of(row("a", "ta", 0.1), row("b", "tb", 0.2), row("c", "tc", 0.3));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), 2);

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat(results(env)).extracting(r -> r.get("id")).containsExactly("a", "b");
    }

    @Test
    void noRerankerConfiguredDegradesLoud() {
        Map<String, Object> env = new RerankStage(null)
                .apply("q", new ArrayList<>(List.of(row("a", "ta", 0.1))), null);

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("no reranker configured");
        assertThat(results(env)).hasSize(1);
    }

    // ── Caller errors propagate (never converted to degrade) ─────────────────

    @Test
    void illegalArgumentFromRerankerPropagates() {
        var fake = new FakeReranker();
        fake.failure = new IllegalArgumentException("rerank request has 1001 documents");
        var rows = List.of(row("a", "ta", 0.1));

        assertThatThrownBy(() -> new RerankStage(fake).apply("q", new ArrayList<>(rows), null))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void invalidIndexFromRerankerDegradesNotCorrupts() {
        // A reranker returning an index outside the scored-docs list must degrade
        // loud, never map onto the wrong row.
        var fake = new FakeReranker();
        fake.result = List.of(new Reranker.Scored(5, 0.9));
        var rows = List.of(row("a", "ta", 0.1), row("b", "tb", 0.2));

        Map<String, Object> env = new RerankStage(fake).apply("q", new ArrayList<>(rows), null);

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat(results(env)).extracting(r -> r.get("id")).containsExactly("a", "b");
    }
}
