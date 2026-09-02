// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.Random;

/**
 * RDR-188 bead nexus-9o6y2.1 — CLOUD Voyage AI reranker (rerank-2.5).
 *
 * <p>Sibling of {@link VoyageEmbedder}: same consolidated {@link VoyageRetryLoop}
 * choreography (nexus-1vpal — budget-bounded 429s with {@code Retry-After}
 * honoured, attempt-bounded equal-jittered 5xx/network), same typed
 * {@link UpstreamAuthException} on 401/403, same explicit
 * {@link EgressProxy} wiring (the cloud private subnet has no NAT — a bare
 * client works locally and dies in cloud). Differences, per the RDR:
 * <ul>
 *   <li>No byte-faithful body contract — that discipline is
 *       embeddings-parity-specific (nexus-f4wcg); plain Jackson serialization.</li>
 *   <li>No {@link EmbedderRouter}: rerank scores {@code (query, chunk_text)}
 *       pairs regardless of which model embedded the chunks.</li>
 *   <li>Bounded request timeout well under the embed path's 120s: the rerank
 *       call runs synchronously inside a {@code /v1/vectors/*} search request
 *       (R2 Option A fused stage), so a hung upstream must fail the stage while
 *       the client is still listening.</li>
 *   <li>All non-auth failures raise the typed {@link RerankUpstreamException}
 *       so the handler emits a LOUD structured degrade field — never the
 *       silent input-order fallback this RDR retires.</li>
 * </ul>
 *
 * <p>REST endpoint: {@code POST https://api.voyageai.com/v1/rerank}
 * (docs.voyageai.com, R3): query ≤8k tokens, ≤1,000 documents/request,
 * query+doc ≤32k, total ≤600k tokens. The 1,000-doc cap is asserted here —
 * never silently truncated.
 *
 * <p>Governor: reactive retry only, matching VoyageEmbedder. The proactive
 * rate limiter is accepted engine-wide debt (nexus-rb67a) — do not add here.
 * On a budget-exhausted 429 the typed {@link UpstreamRateLimitedException}
 * surfaces to {@code RerankStage}, which degrades the search LOUDLY (results
 * still served in distance order) rather than failing the whole request —
 * rate-limited reranking is a degraded search, not a broken one.
 *
 * <p>Stateless: each {@link #rerank} call is independent. Thread-safe.
 */
public final class VoyageReranker implements Reranker {

    /** Voyage rerank API hard cap (R3); requests above it are rejected, not truncated. */
    public static final int MAX_DOCS_PER_REQUEST = 1000;
    public static final String DEFAULT_MODEL = "rerank-2.5";

    private static final String VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank";
    private static final long   RETRY_BASE_MS = 500L;
    // Fused-stage bound: worst case with retries ≈ 3×30s + backoff, still under
    // typical client HTTP timeouts; the embed path's 120s would not be.
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(30);

    /**
     * Total 429-absorption budget for ONE {@link #rerank} call (nexus-1vpal —
     * the nexus-99r7y semantics, sized for THIS path, not copied from the
     * embedders' 20s): rerank runs synchronously inside an interactive fused
     * search stage, where {@link dev.nexus.service.http.RerankStage} can
     * DEGRADE to distance order the moment the reranker gives up — so
     * sustained limiting should degrade the search in a couple of seconds,
     * not hold it hostage for 20. 2.5s absorbs a {@code Retry-After: 1} blip
     * with margin (the old un-jittered 3-attempt loop already slept ~1.5s at
     * the production base) and fails typed on anything sustained, well
     * inside the 30s edge bound.
     *
     * <p>Interplay with {@link VoyageRetryLoop#MIN_CALL_ALLOWANCE_MS} (1s),
     * stated because at this scale it is 40% of the budget, not the
     * embedders' 5%: the largest honourable {@code Retry-After} is ~1.5s,
     * so {@code Retry-After: 2} degrades IMMEDIATELY on the first 429 with
     * zero retries — by design. Voyage explicitly asking for a 2s wait
     * inside an interactive stage is the degrade-now case; retrying before
     * an advertised Retry-After elapses is the herd behavior the header
     * exists to stop. Pinned by the reranker test
     * {@code retryAfterTwoSecondsAtProductionBudgetDegradesImmediatelyByDesign}.
     */
    private static final long RATE_LIMIT_BUDGET_MS = 2_500L;

    /** Terminal-failure vocabulary handed to {@link #retryLoop}: everything
     *  non-auth, non-rate-limit stays {@link RerankUpstreamException} so the
     *  RDR-188 loud-degrade contract is unchanged. */
    private static final VoyageRetryLoop.Failures RERANK_FAILURES = new VoyageRetryLoop.Failures() {
        @Override
        public RuntimeException status(int status, String body) {
            return new RerankUpstreamException(
                    "Voyage AI rerank failed: HTTP " + status + " body=" + body);
        }

        @Override
        public RuntimeException wrap(String message, Throwable cause) {
            return new RerankUpstreamException(message, cause);
        }
    };

    private final String       apiKey;
    private final String       model;
    private final String       url;
    private final HttpClient   http;
    private final ObjectMapper mapper;

    /** The consolidated retry choreography (nexus-1vpal) — owns backoff,
     *  Retry-After, the 429 budget arithmetic, and the shared auth arm. */
    private final VoyageRetryLoop retryLoop;

    /**
     * @param apiKey Voyage AI API key (the engine's {@code NX_VOYAGE_API_KEY})
     * @param model  e.g. {@link #DEFAULT_MODEL}
     */
    public VoyageReranker(String apiKey, String model) {
        this(apiKey, model, VOYAGE_RERANK_URL, RETRY_BASE_MS, EgressProxy.selector());
    }

    /**
     * Full wiring, the single build path: tests (unit + Testcontainers stage
     * suite) inject a fake upstream URL, a fast retry base, and
     * {@code Optional.empty()} so an ambient {@code HTTPS_PROXY} can never
     * route the localhost upstream. Production uses the 2-arg constructor.
     */
    public VoyageReranker(String apiKey, String model, String url, long retryBaseMs,
                          Optional<ProxySelector> proxy) {
        this(apiKey, model, url, retryBaseMs, proxy, new Random(), RATE_LIMIT_BUDGET_MS);
    }

    /**
     * Full wiring — the single build path (nexus-1vpal, mirrors the embedders'
     * widest constructors): adds the injectable jitter source and 429 budget so
     * tests can assert the budget-bounded fail-fast without wall clock.
     * Production always takes {@link #RATE_LIMIT_BUDGET_MS} and an unseeded
     * {@link Random} via the shorter constructors.
     */
    VoyageReranker(String apiKey, String model, String url, long retryBaseMs,
                   Optional<ProxySelector> proxy, Random jitterRandom, long rateLimitBudgetMs) {
        this.apiKey = apiKey;
        this.model = model;
        this.url = url;
        this.retryLoop = new VoyageRetryLoop("voyage_rerank", "Voyage rerank", retryBaseMs,
                                             rateLimitBudgetMs, jitterRandom, () -> { });
        var builder = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10));
        proxy.ifPresent(builder::proxy);
        this.http = builder.build();
        this.mapper = new ObjectMapper();
    }

    @Override
    public String modelToken() {
        return model;
    }

    /**
     * Rerank {@code documents} against {@code query}, returning
     * {@code (input index, relevance score)} pairs ordered by relevance
     * descending. With {@code topK} set, at most {@code topK} entries return
     * (forwarded to the API — Voyage bills by input tokens either way).
     *
     * @throws IllegalArgumentException blank query, non-positive topK, or more
     *         than {@link #MAX_DOCS_PER_REQUEST} documents (never truncated)
     * @throws UpstreamAuthException    Voyage rejected the service's key (401/403)
     * @throws RerankUpstreamException  any other upstream failure — retries
     *         exhausted, non-retryable status, network error, invalid response
     */
    @Override
    public List<Scored> rerank(String query, List<String> documents, Integer topK) {
        if (query == null || query.isBlank()) {
            throw new IllegalArgumentException("rerank query must be non-blank");
        }
        if (topK != null && topK <= 0) {
            throw new IllegalArgumentException("rerank top_k must be positive, got " + topK);
        }
        if (documents == null || documents.isEmpty()) {
            return List.of();
        }
        if (documents.size() > MAX_DOCS_PER_REQUEST) {
            throw new IllegalArgumentException(
                    "rerank request has " + documents.size() + " documents; the Voyage API cap is "
                    + MAX_DOCS_PER_REQUEST + " — refusing to silently truncate. Trim the candidate"
                    + " set before reranking.");
        }
        String body = buildJson(query, documents, topK);
        String responseBody = callApi(body, retryLoop.newDeadlineNanos());
        return parseResponse(responseBody, documents.size());
    }

    // ── Request / response helpers ────────────────────────────────────────────

    private String buildJson(String query, List<String> documents, Integer topK) {
        // Plain Jackson — no byte contract here (embeddings-parity-specific).
        ObjectNode root = mapper.createObjectNode();
        root.put("query", query);
        ArrayNode docs = root.putArray("documents");
        documents.forEach(docs::add);
        root.put("model", model);
        if (topK != null) {
            root.put("top_k", topK);
        }
        root.put("return_documents", false);
        try {
            return mapper.writeValueAsString(root);
        } catch (Exception e) {
            throw new RerankUpstreamException("rerank request serialization failed", e);
        }
    }

    private String callApi(String json, long deadlineNanos) {
        // nexus-1vpal: retry choreography consolidated into VoyageRetryLoop
        // (budget-bounded 429s with Retry-After, attempt-bounded jittered
        // 5xx/network, shared auth arm). On budget exhaustion the loop throws
        // the typed UpstreamRateLimitedException, which RerankStage catches
        // and DEGRADES on — search keeps serving, per the RDR-188 contract.
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Authorization", "Bearer " + apiKey)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .timeout(REQUEST_TIMEOUT)
                .build();
        return retryLoop.send(http, req, deadlineNanos, RERANK_FAILURES);
    }

    /**
     * Parse the rerank response: {@code data[]} of {@code {index, relevance_score}}.
     * Indices are validated against the input size (garbage upstream degrades
     * LOUD and typed, never propagates a wrong-row mapping) and the result is
     * defensively sorted by score descending — the API documents sorted output
     * but the row mapping is too load-bearing to trust unverified.
     */
    private List<Scored> parseResponse(String body, int docCount) {
        JsonNode root;
        try {
            root = mapper.readTree(body);
        } catch (Exception e) {
            throw new RerankUpstreamException("Voyage rerank response is not valid JSON", e);
        }
        JsonNode data = root.get("data");
        if (data == null || !data.isArray() || data.isEmpty()) {
            throw new RerankUpstreamException(
                    "Voyage rerank returned no data for " + docCount + " documents: " + body);
        }
        boolean[] seen = new boolean[docCount];
        List<Scored> out = new ArrayList<>(data.size());
        for (JsonNode item : data) {
            JsonNode idx = item.get("index");
            JsonNode score = item.get("relevance_score");
            if (idx == null || !idx.isIntegralNumber() || score == null || !score.isNumber()) {
                throw new RerankUpstreamException(
                        "Voyage rerank item missing index/relevance_score: " + item);
            }
            int i = idx.intValue();
            if (i < 0 || i >= docCount) {
                throw new RerankUpstreamException(
                        "Voyage rerank returned out-of-bounds index " + i + " for "
                        + docCount + " documents");
            }
            if (seen[i]) {
                throw new RerankUpstreamException(
                        "Voyage rerank returned duplicate index " + i);
            }
            seen[i] = true;
            out.add(new Scored(i, score.doubleValue()));
        }
        out.sort(Comparator.comparingDouble(Scored::relevanceScore).reversed());
        return out;
    }
}
