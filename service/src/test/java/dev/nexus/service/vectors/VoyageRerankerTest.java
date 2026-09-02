// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-188 bead nexus-9o6y2.1 — VoyageReranker against a fake Voyage upstream.
 *
 * <p>Hermetic: {@link HttpServer} on 127.0.0.1 port 0 scripts the
 * {@code POST /v1/rerank} responses; no network, no API key. The package-private
 * constructor injects the fake URL, a fast retry base, and an empty proxy
 * selector so an ambient {@code HTTPS_PROXY} on the host can never route the
 * localhost upstream (the public constructor resolves {@link EgressProxy} from
 * env through the same single build path).
 *
 * <p>Contract under test (RDR-188 R2/R3 + locked invariants):
 * <ul>
 *   <li>upstream failure raises TYPED exceptions ({@link UpstreamAuthException}
 *       on 401/403, {@link RerankUpstreamException} otherwise) — NEVER a silent
 *       input-order fallback (the scoring.py:419-468 client anti-pattern this
 *       RDR retires);</li>
 *   <li>retry via the consolidated {@link VoyageRetryLoop} (nexus-1vpal):
 *       429s budget-bounded with {@code Retry-After} honoured and typed
 *       {@link UpstreamRateLimitedException} on exhaustion; 5xx retried with
 *       backoff, max 3 attempts; auth and 4xx failures are terminal on the
 *       first response;</li>
 *   <li>&gt;1000 docs throws — never silently truncates (R3 API cap);</li>
 *   <li>results ordered by relevance score descending regardless of upstream
 *       order; indices validated against the input document list.</li>
 * </ul>
 */
class VoyageRerankerTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private HttpServer server;
    private String url;
    /** Scripted responses, consumed one per request: [status, body]. */
    private final ConcurrentLinkedQueue<Object[]> responses = new ConcurrentLinkedQueue<>();
    private final List<String> requestBodies = new CopyOnWriteArrayList<>();
    private final List<String> authHeaders = new CopyOnWriteArrayList<>();

    @BeforeEach
    void startFakeVoyage() throws Exception {
        responses.clear();
        requestBodies.clear();
        authHeaders.clear();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/rerank", exchange -> {
            requestBodies.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            authHeaders.add(exchange.getRequestHeaders().getFirst("Authorization"));
            Object[] scripted = responses.poll();
            int status = scripted == null ? 200 : (Integer) scripted[0];
            byte[] body = (scripted == null ? "{\"data\": []}" : (String) scripted[1])
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            if (scripted != null && scripted.length > 2 && scripted[2] != null) {
                exchange.getResponseHeaders().set("Retry-After", (String) scripted[2]);
            }
            exchange.sendResponseHeaders(status, body.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(body);
            }
        });
        server.start();
        url = "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/rerank";
    }

    @AfterEach
    void stopFakeVoyage() {
        server.stop(0);
    }

    private VoyageReranker reranker() {
        // retryBaseMs=10 keeps the retry/exhaustion tests fast; production uses 500.
        return new VoyageReranker("test-key", "rerank-2.5", url, 10L, Optional.empty());
    }

    private void respond(int status, String body) {
        responses.add(new Object[] {status, body});
    }

    private void respondWithRetryAfter(int status, String body, String retryAfter) {
        responses.add(new Object[] {status, body, retryAfter});
    }

    /** nexus-1vpal: injectable 429 budget so fail-fast tests need no wall clock. */
    private VoyageReranker rerankerWithBudget(long budgetMs) {
        return new VoyageReranker("test-key", "rerank-2.5", url, 10L, Optional.empty(),
                                  new java.util.Random(), budgetMs);
    }

    private static String dataBody(String entries) {
        return "{\"object\": \"list\", \"data\": [" + entries + "], "
                + "\"model\": \"rerank-2.5\", \"usage\": {\"total_tokens\": 42}}";
    }

    // ── Happy path ────────────────────────────────────────────────────────────

    @Test
    void ranksByRelevanceScoreDescendingWithInputIndices() throws Exception {
        respond(200, dataBody(
                "{\"index\": 2, \"relevance_score\": 0.97}, "
                + "{\"index\": 0, \"relevance_score\": 0.61}, "
                + "{\"index\": 1, \"relevance_score\": 0.15}"));

        List<Reranker.Scored> out = reranker()
                .rerank("what is a tumbler", List.of("doc a", "doc b", "doc c"), null);

        assertThat(out).containsExactly(
                new Reranker.Scored(2, 0.97),
                new Reranker.Scored(0, 0.61),
                new Reranker.Scored(1, 0.15));

        JsonNode body = MAPPER.readTree(requestBodies.get(0));
        assertThat(body.get("query").asText()).isEqualTo("what is a tumbler");
        assertThat(body.get("model").asText()).isEqualTo("rerank-2.5");
        assertThat(body.get("documents")).hasSize(3);
        assertThat(body.get("documents").get(1).asText()).isEqualTo("doc b");
        assertThat(body.get("return_documents").asBoolean()).isFalse();
        assertThat(body.has("top_k")).isFalse();
        assertThat(authHeaders.get(0)).isEqualTo("Bearer test-key");
    }

    @Test
    void sortsDefensivelyWhenUpstreamReturnsUnsorted() throws Exception {
        respond(200, dataBody(
                "{\"index\": 0, \"relevance_score\": 0.10}, "
                + "{\"index\": 1, \"relevance_score\": 0.90}"));

        List<Reranker.Scored> out = reranker().rerank("q", List.of("a", "b"), null);

        assertThat(out).containsExactly(
                new Reranker.Scored(1, 0.90),
                new Reranker.Scored(0, 0.10));
    }

    @Test
    void topKIsForwardedAndTruncatedResponseAccepted() throws Exception {
        respond(200, dataBody("{\"index\": 3, \"relevance_score\": 0.88}"));

        List<Reranker.Scored> out = reranker()
                .rerank("q", List.of("a", "b", "c", "d"), 1);

        assertThat(out).containsExactly(new Reranker.Scored(3, 0.88));
        JsonNode body = MAPPER.readTree(requestBodies.get(0));
        assertThat(body.get("top_k").asInt()).isEqualTo(1);
    }

    @Test
    void emptyDocumentsReturnsEmptyWithoutCallingUpstream() throws Exception {
        assertThat(reranker().rerank("q", List.of(), null)).isEmpty();
        assertThat(reranker().rerank("q", null, null)).isEmpty();
        assertThat(requestBodies).isEmpty();
    }

    // ── Input validation ─────────────────────────────────────────────────────

    @Test
    void blankQueryThrowsWithoutCallingUpstream() {
        assertThatThrownBy(() -> reranker().rerank("  ", List.of("a"), null))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> reranker().rerank(null, List.of("a"), null))
                .isInstanceOf(IllegalArgumentException.class);
        assertThat(requestBodies).isEmpty();
    }

    @Test
    void moreThanMaxDocsThrowsNeverTruncates() {
        List<String> docs = new ArrayList<>(Collections.nCopies(1001, "d"));
        assertThatThrownBy(() -> reranker().rerank("q", docs, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("1000");
        assertThat(requestBodies).isEmpty();
    }

    @Test
    void nonPositiveTopKThrows() {
        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), 0))
                .isInstanceOf(IllegalArgumentException.class);
        assertThat(requestBodies).isEmpty();
    }

    // ── Retry shape (VoyageEmbedder.callApi clone) ───────────────────────────

    @Test
    void retriesOn429ThenSucceeds() {
        respond(429, "{\"detail\": \"rate limited\"}");
        respond(200, dataBody("{\"index\": 0, \"relevance_score\": 0.5}"));

        List<Reranker.Scored> out = reranker().rerank("q", List.of("a"), null);

        assertThat(out).containsExactly(new Reranker.Scored(0, 0.5));
        assertThat(requestBodies).hasSize(2);
    }

    @Test
    void retriesOn5xxThenSucceeds() {
        respond(503, "{\"detail\": \"upstream sad\"}");
        respond(200, dataBody("{\"index\": 0, \"relevance_score\": 0.5}"));

        assertThat(reranker().rerank("q", List.of("a"), null)).hasSize(1);
        assertThat(requestBodies).hasSize(2);
    }

    @Test
    void exhaustedRetriesRaiseTypedRerankUpstreamException() {
        respond(500, "{}");
        respond(500, "{}");
        respond(500, "{}");

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class)
                .hasMessageContaining("500");
        assertThat(requestBodies).hasSize(3);
    }

    // ── nexus-1vpal: budget-bounded 429s + Retry-After (99r7y semantics) ─────

    @Test
    void sustained429IsBudgetBoundedNotAttemptBoundedAndTyped() {
        // Rerank storms share the account-wide Voyage RPM ceiling with the
        // embed paths (the 2026-08-15 incident class). Old behavior burned 3
        // attempts and threw the generic RerankUpstreamException; new
        // behavior keeps absorbing 429s past 3 attempts while the budget
        // lasts, then fails TYPED so RerankStage degrades with the
        // retry-after in the reason.
        for (int i = 0; i < 200; i++) respond(429, "{\"detail\": \"rate limited\"}");

        assertThatThrownBy(() -> rerankerWithBudget(2_500L).rerank("q", List.of("a"), null))
                .isInstanceOf(UpstreamRateLimitedException.class);
        assertThat(requestBodies.size())
                .as("429s must be budget-bounded, not capped at 3 attempts")
                .isGreaterThan(3);
    }

    @Test
    void retryAfterExceedingBudgetFailsFastTypedAfterOneRequest() {
        respondWithRetryAfter(429, "{\"detail\": \"rate limited\"}", "60");

        long start = System.nanoTime();
        assertThatThrownBy(() -> rerankerWithBudget(500L).rerank("q", List.of("a"), null))
                .isInstanceOf(UpstreamRateLimitedException.class)
                .satisfies(e -> assertThat(
                        ((UpstreamRateLimitedException) e).retryAfterSeconds())
                        .isEqualTo(60L));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000L;
        assertThat(elapsedMs)
                .as("fail-fast must not sleep out the advertised Retry-After")
                .isLessThan(5_000L);
        assertThat(requestBodies).hasSize(1);
    }

    @Test
    void retryAfterWithinBudgetIsHonouredThenSucceeds() {
        // Retry-After: 1 fits the production 2.5s rerank budget — the sleep
        // must respect the header (>= ~1s, not the ~10ms jitter base), then
        // the retry succeeds. This is the brief-blip case the small budget
        // is sized to absorb rather than degrade.
        respondWithRetryAfter(429, "{\"detail\": \"rate limited\"}", "1");
        respond(200, dataBody("{\"index\": 0, \"relevance_score\": 0.5}"));

        long start = System.nanoTime();
        List<Reranker.Scored> out = reranker().rerank("q", List.of("a"), null);
        long elapsedMs = (System.nanoTime() - start) / 1_000_000L;

        assertThat(out).containsExactly(new Reranker.Scored(0, 0.5));
        assertThat(elapsedMs).isGreaterThanOrEqualTo(900L);
        assertThat(requestBodies).hasSize(2);
    }

    @Test
    void retryAfterTwoSecondsAtProductionBudgetDegradesImmediatelyByDesign() {
        // Critic finding 1 (2026-08-31), pinned DELIBERATELY: at the rerank
        // path's 2.5s budget, MIN_CALL_ALLOWANCE (1s) means Retry-After: 2
        // (2000 + 1000 > 2500) fails typed on the FIRST 429 — zero retries,
        // fewer than the old 3-attempt loop made. That is the intended
        // boundary, not an accident: Voyage explicitly asking for a 2s wait
        // inside an interactive fused search stage IS the degrade-now case,
        // and retrying before an explicit Retry-After elapses is the herd
        // behavior the header exists to stop. Retry-After: 1 remains the
        // absorbed-blip case (retryAfterWithinBudgetIsHonouredThenSucceeds).
        respondWithRetryAfter(429, "{\"detail\": \"rate limited\"}", "2");

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(UpstreamRateLimitedException.class)
                .satisfies(e -> assertThat(
                        ((UpstreamRateLimitedException) e).retryAfterSeconds())
                        .isEqualTo(2L));
        assertThat(requestBodies).hasSize(1);
    }

    @Test
    void authFailureRaisesUpstreamAuthExceptionWithoutRetry() {
        respond(401, "{\"detail\": \"bad key\"}");

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(UpstreamAuthException.class);
        assertThat(requestBodies).hasSize(1);
    }

    @Test
    void nonRetryable4xxRaisesTypedExceptionWithoutRetry() {
        respond(400, "{\"detail\": \"bad request\"}");

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class)
                .hasMessageContaining("400");
        assertThat(requestBodies).hasSize(1);
    }

    @Test
    void connectionFailureRaisesTypedExceptionAfterRetries() {
        server.stop(0);  // upstream gone: every attempt fails at connect

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class);
    }

    // ── Response validation (garbage upstream degrades LOUD, typed) ──────────

    @Test
    void malformedResponseBodyRaisesTypedException() {
        respond(200, "not json at all");

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class);
    }

    @Test
    void emptyDataForNonEmptyDocsRaisesTypedException() {
        respond(200, "{\"data\": []}");

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class);
    }

    @Test
    void outOfBoundsIndexRaisesTypedException() {
        respond(200, dataBody("{\"index\": 5, \"relevance_score\": 0.9}"));

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a", "b"), null))
                .isInstanceOf(RerankUpstreamException.class)
                .hasMessageContaining("index");
    }

    @Test
    void duplicateIndexRaisesTypedException() {
        respond(200, dataBody(
                "{\"index\": 0, \"relevance_score\": 0.9}, "
                + "{\"index\": 0, \"relevance_score\": 0.8}"));

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a", "b"), null))
                .isInstanceOf(RerankUpstreamException.class)
                .hasMessageContaining("index");
    }

    @Test
    void missingRelevanceScoreRaisesTypedException() {
        respond(200, dataBody("{\"index\": 0}"));

        assertThatThrownBy(() -> reranker().rerank("q", List.of("a"), null))
                .isInstanceOf(RerankUpstreamException.class);
    }
}
