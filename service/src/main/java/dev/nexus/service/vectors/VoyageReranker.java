// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;

/**
 * RDR-188 bead nexus-9o6y2.1 — CLOUD Voyage AI reranker (rerank-2.5).
 *
 * <p>Sibling of {@link VoyageEmbedder}: same 3-attempt reactive backoff on
 * 429/5xx, same typed {@link UpstreamAuthException} on 401/403, same explicit
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
 *
 * <p>Stateless: each {@link #rerank} call is independent. Thread-safe.
 */
public final class VoyageReranker {

    private static final Logger log = LoggerFactory.getLogger(VoyageReranker.class);

    /** Voyage rerank API hard cap (R3); requests above it are rejected, not truncated. */
    public static final int MAX_DOCS_PER_REQUEST = 1000;
    public static final String DEFAULT_MODEL = "rerank-2.5";

    private static final String VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank";
    private static final int    MAX_RETRIES = 3;
    private static final long   RETRY_BASE_MS = 500L;
    // Fused-stage bound: worst case with retries ≈ 3×30s + backoff, still under
    // typical client HTTP timeouts; the embed path's 120s would not be.
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(30);

    /** One reranked document: {@code index} into the input list, higher score = more relevant. */
    public record Scored(int index, double relevanceScore) {
    }

    private final String       apiKey;
    private final String       model;
    private final String       url;
    private final long         retryBaseMs;
    private final HttpClient   http;
    private final ObjectMapper mapper;

    /**
     * @param apiKey Voyage AI API key (the engine's {@code NX_VOYAGE_API_KEY})
     * @param model  e.g. {@link #DEFAULT_MODEL}
     */
    public VoyageReranker(String apiKey, String model) {
        this(apiKey, model, VOYAGE_RERANK_URL, RETRY_BASE_MS, EgressProxy.selector());
    }

    /**
     * Package-private full wiring, the single build path: tests inject a fake
     * upstream URL, a fast retry base, and {@code Optional.empty()} so an
     * ambient {@code HTTPS_PROXY} can never route the localhost upstream.
     */
    VoyageReranker(String apiKey, String model, String url, long retryBaseMs,
                   Optional<ProxySelector> proxy) {
        this.apiKey = apiKey;
        this.model = model;
        this.url = url;
        this.retryBaseMs = retryBaseMs;
        var builder = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10));
        proxy.ifPresent(builder::proxy);
        this.http = builder.build();
        this.mapper = new ObjectMapper();
    }

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
        String responseBody = callApi(body);
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

    private String callApi(String json) {
        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                HttpRequest req = HttpRequest.newBuilder()
                        .uri(URI.create(url))
                        .header("Authorization", "Bearer " + apiKey)
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .timeout(REQUEST_TIMEOUT)
                        .build();

                HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
                int status = resp.statusCode();

                if (status == 200) return resp.body();

                boolean retryable = (status == 429 || status >= 500);
                if (retryable && attempt < MAX_RETRIES) {
                    long delay = retryBaseMs * (1L << (attempt - 1));
                    log.warn("event=voyage_rerank_retry attempt={} status={} delay_ms={}",
                             attempt, status, delay);
                    Thread.sleep(delay);
                    continue;
                }
                if (status == 401 || status == 403) {
                    // Same posture as the embed path (nexus-pmhpc): a credentials
                    // problem, not an engine defect — handler maps to 502-with-detail.
                    throw new UpstreamAuthException(
                            "Voyage AI rejected the service's API key (HTTP " + status
                            + ") on rerank: the key is invalid, expired, or lacks scope. Rotate"
                            + " the service's Voyage key and restart. body=" + resp.body());
                }
                throw new RerankUpstreamException(
                        "Voyage AI rerank failed: HTTP " + status + " body=" + resp.body());

            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RerankUpstreamException("Voyage rerank interrupted", e);
            } catch (RuntimeException e) {
                throw e;
            } catch (Exception e) {
                if (attempt == MAX_RETRIES) {
                    throw new RerankUpstreamException(
                            "Voyage rerank failed after " + MAX_RETRIES + " attempts", e);
                }
                try { Thread.sleep(retryBaseMs * (1L << (attempt - 1))); }
                catch (InterruptedException ix) {
                    Thread.currentThread().interrupt();
                    throw new RerankUpstreamException("Voyage rerank interrupted", ix);
                }
            }
        }
        throw new RerankUpstreamException("Voyage rerank: exhausted retries"); // unreachable
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
