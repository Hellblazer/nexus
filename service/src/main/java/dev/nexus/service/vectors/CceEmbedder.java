// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.21 — Voyage AI Contextualized Chunk Embedding (CCE) embedder.
 *
 * <p>Mirrors the voyageai Python SDK's {@code contextualized_embed} call EXACTLY, including
 * the Python t3.py per-text calling convention:
 * <ul>
 *   <li>REST endpoint: {@code POST https://api.voyageai.com/v1/contextualizedembeddings}</li>
 *   <li>Each text is embedded in a SEPARATE API call: {@code inputs=[[text]]} per text.</li>
 *   <li>{@code encoding_format: "base64"} — Python SDK default; gives exact float32 binary</li>
 *   <li>No {@code output_dtype} field — production _cce_embed does not set it</li>
 *   <li>Response: {@code data[0].data[0].embedding}</li>
 *   <li>No {@code truncation} field — CCE API does not accept it (unlike /v1/embeddings)</li>
 * </ul>
 *
 * <p><strong>CRITICAL: per-text not batch.</strong> The CCE model embeds each document
 * in the context of ALL other documents in the same API call.  Sending multiple texts as
 * {@code inputs=[[t0],[t1],...]} produces DIFFERENT embeddings than sending
 * {@code inputs=[[t0]]} and {@code inputs=[[t1]]} separately (measured cosine ≈ 0.999,
 * not 1.0).  Python's t3.py always calls one text at a time:
 * <pre>
 *   result = _voyage_with_retry(
 *       self._voyage_client.contextualized_embed,
 *       inputs=[[text]],
 *       model="voyage-context-3",
 *       input_type=input_type,
 *   )
 *   return result.results[0].embeddings[0]
 * </pre>
 * Java must match this: one API call per text, even for batch inputs.
 *
 * <p><strong>CRITICAL: base64 encoding.</strong>  The Python voyageai SDK uses
 * {@code encoding_format="base64"} by default (see {@code ContextualizedEmbedding.create}).
 * Base64 contains the raw float32 binary (little-endian IEEE 754), decoded via
 * {@code np.frombuffer(b64decode(s), np.float32)}.  JSON float decimals differ by up to
 * 115 ULPs from the true float32 binary.  Java must also use base64 for bit-identical parity.
 *
 * <p>Collection routing: collections starting with {@code knowledge__}, {@code docs__},
 * or {@code rdr__} use this embedder (model {@code voyage-context-3}).
 * Routing enforced by {@link EmbedderRouter}.
 *
 * <p>Thread-safe: stateless per call.
 */
public final class CceEmbedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(CceEmbedder.class);

    /** CCE endpoint — note: no hyphen, different from /v1/embeddings. */
    private static final String CCE_URL = "https://api.voyageai.com/v1/contextualizedembeddings";
    private static final int    MAX_RETRIES   = 3;
    private static final long   RETRY_BASE_MS = 500L;

    private final String     apiKey;
    private final String     inputType;  // "document" or "query"
    private final HttpClient http;
    private final ObjectMapper mapper;

    /**
     * @param apiKey    Voyage AI API key
     * @param inputType {@code "document"} for indexing, {@code "query"} for search
     */
    public CceEmbedder(String apiKey, String inputType) {
        this.apiKey    = apiKey;
        this.inputType = inputType;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        this.mapper = new ObjectMapper();
    }

    /**
     * Embed a batch of texts via CCE, one text per API call.
     *
     * <p>Each text is sent as a separate API call ({@code inputs=[[text]]}) to match
     * Python's t3.py per-text behavior.  Batching multiple texts in one call produces
     * different embeddings due to cross-document context propagation in the CCE model.
     */
    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        List<float[]> result = new ArrayList<>(texts.size());
        for (String text : texts) {
            result.add(embedOneFloat(text));
        }
        return result;
    }

    /**
     * Embed a batch of texts, preserving full double (float64) precision.
     *
     * <p>Decodes base64 as float32, then promotes float32 → float64 exactly.
     * Used by the parity gate ({@code /v1/vectors/embed}) so the returned JSON doubles
     * can be compared against the Python float32 values without serialization loss.
     */
    public List<double[]> embedDouble(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        List<double[]> result = new ArrayList<>(texts.size());
        for (String text : texts) {
            result.add(embedOneDouble(text));
        }
        return result;
    }

    // ── Per-text API call helpers ─────────────────────────────────────────────

    private float[] embedOneFloat(String text) {
        String json = buildJson(text);
        String body = callApi(json);
        try {
            return parseOneFloat(body);
        } catch (Exception e) {
            throw new RuntimeException("CCE float parse failed for text: " + text.substring(0, Math.min(40, text.length())), e);
        }
    }

    private double[] embedOneDouble(String text) {
        String json = buildJson(text);
        String body = callApi(json);
        try {
            float[] f32 = parseOneFloat(body);
            double[] f64 = new double[f32.length];
            for (int i = 0; i < f32.length; i++) f64[i] = f32[i];  // exact float32 → float64
            return f64;
        } catch (Exception e) {
            throw new RuntimeException("CCE double parse failed for text: " + text.substring(0, Math.min(40, text.length())), e);
        }
    }

    private String buildJson(String text) {
        // Mirror production _cce_embed / ContextualizedEmbedding.create exactly:
        //   inputs=[[text]]: one doc, one chunk — per-text independent embedding
        //   input_type: "document" for indexing, "query" for search (passed through)
        //   encoding_format="base64": Python SDK default — gives exact float32 binary
        //   No output_dtype field — production does not set it
        //   No truncation field — CCE API does not accept it
        Map<String, Object> body = new HashMap<>();
        body.put("model",           "voyage-context-3");
        body.put("inputs",          List.of(List.of(text)));
        body.put("input_type",      inputType);
        body.put("encoding_format", "base64");
        try {
            return mapper.writeValueAsString(body);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize CCE request", e);
        }
    }

    private String callApi(String json) {
        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                HttpRequest req = HttpRequest.newBuilder()
                        .uri(URI.create(CCE_URL))
                        .header("Authorization", "Bearer " + apiKey)
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .timeout(Duration.ofSeconds(120))
                        .build();

                HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
                int status = resp.statusCode();

                if (status == 200) return resp.body();

                boolean retryable = (status == 429 || status >= 500);
                if (retryable && attempt < MAX_RETRIES) {
                    long delay = RETRY_BASE_MS * (1L << (attempt - 1));
                    log.warn("event=cce_retry attempt={} status={} delay_ms={}", attempt, status, delay);
                    Thread.sleep(delay);
                    continue;
                }
                throw new RuntimeException("Voyage AI CCE request failed: HTTP " + status + " body=" + resp.body());

            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException("CCE embed interrupted", e);
            } catch (RuntimeException e) {
                throw e;
            } catch (Exception e) {
                if (attempt == MAX_RETRIES) {
                    throw new RuntimeException("CCE embed failed after " + MAX_RETRIES + " attempts", e);
                }
                try { Thread.sleep(RETRY_BASE_MS * (1L << (attempt - 1))); }
                catch (InterruptedException ix) { Thread.currentThread().interrupt(); throw new RuntimeException("interrupted", ix); }
            }
        }
        throw new RuntimeException("CCE embed: exhausted retries"); // unreachable
    }

    // ── Response parsers ──────────────────────────────────────────────────────

    /**
     * Parse CCE base64 response body for a single-text call (inputs=[[text]]).
     *
     * <p>Response structure with encoding_format="base64":
     * <pre>
     * {
     *   "object": "list",
     *   "data": [
     *     {
     *       "object": "list",
     *       "index": 0,              // outer doc index (always 0 for single-text call)
     *       "data": [
     *         {
     *           "object": "embedding",
     *           "index": 0,          // chunk index (always 0 for single chunk)
     *           "embedding": "base64string..."
     *         }
     *       ]
     *     }
     *   ]
     * }
     * </pre>
     */
    @SuppressWarnings("unchecked")
    private float[] parseOneFloat(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> outerData = (List<Map<String, Object>>) root.get("data");
        if (outerData == null || outerData.isEmpty()) {
            throw new RuntimeException("CCE response missing data array: " + body);
        }
        outerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));

        List<Map<String, Object>> innerData = (List<Map<String, Object>>) outerData.get(0).get("data");
        if (innerData == null || innerData.isEmpty()) {
            throw new RuntimeException("CCE response: doc group has empty data array");
        }
        innerData.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));

        Object emb = innerData.get(0).get("embedding");
        if (emb == null) throw new RuntimeException("CCE response: chunk missing 'embedding'");

        // Decode base64 as float32 binary (same as Python np.frombuffer(b64decode(s), np.float32))
        if (emb instanceof String b64) {
            return decodeBase64Float32(b64);
        }
        // Fallback: if API returns JSON array (non-base64 path)
        List<Number> rawEmb = (List<Number>) emb;
        float[] vec = new float[rawEmb.size()];
        for (int i = 0; i < rawEmb.size(); i++) vec[i] = rawEmb.get(i).floatValue();
        return vec;
    }

    /**
     * Decode a base64 string as an array of IEEE 754 float32 values (little-endian).
     *
     * <p>Matches Python {@code np.frombuffer(base64.b64decode(b64str), np.float32)}.
     */
    private static float[] decodeBase64Float32(String b64) {
        byte[] bytes = Base64.getDecoder().decode(b64);
        if (bytes.length % 4 != 0) {
            throw new RuntimeException("CCE base64 byte length not multiple of 4: " + bytes.length);
        }
        int dims = bytes.length / 4;
        float[] vec = new float[dims];
        ByteBuffer buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < dims; i++) vec[i] = buf.getFloat();
        return vec;
    }
}
