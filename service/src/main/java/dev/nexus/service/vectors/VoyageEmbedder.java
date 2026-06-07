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
 * RDR-152 bead nexus-gmiaf.20 — CLOUD Voyage AI standard embedder (voyage-code-3).
 *
 * <p>Mirrors the production Python path EXACTLY:
 * Production uses {@code chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction(
 * model_name=model, api_key=key)} with default {@code input_type=None} and
 * {@code truncation=True}.
 *
 * <p>CRITICAL: The Python voyageai SDK uses {@code encoding_format="base64"} by default
 * (see {@code voyageai.Embedding.create}).  Base64 responses contain the raw float32 binary
 * representation, decoded via {@code np.frombuffer(b64decode(embedding), np.float32)}.
 * This gives EXACT float32 bit patterns.  JSON float responses are decimal approximations
 * that differ by up to 23 ULPs from the true float32.  Java must also use base64 to get
 * bit-identical float32 values.
 *
 * <ul>
 *   <li>{@code encoding_format: "base64"} — matches Python SDK default, gives exact float32</li>
 *   <li>No {@code input_type} field — production sends None (omit from request body)</li>
 *   <li>No {@code output_dtype} field — production does not set it</li>
 *   <li>{@code truncation: true} — LOAD-BEARING: omitting gives cosine ≈ 0.99995 drift</li>
 *   <li>Sort response {@code data[]} by {@code index} (API may return out-of-order)</li>
 *   <li>Retry on 429 / 5xx with exponential backoff (max 3 attempts)</li>
 * </ul>
 *
 * <p>REST endpoint: {@code POST https://api.voyageai.com/v1/embeddings}
 * Headers: {@code Authorization: Bearer <key>}, {@code Content-Type: application/json}.
 *
 * <p>Stateless: each {@link #embed} call is independent.  Thread-safe.
 */
public final class VoyageEmbedder implements Embedder {

    private static final Logger log = LoggerFactory.getLogger(VoyageEmbedder.class);

    private static final String VOYAGE_URL = "https://api.voyageai.com/v1/embeddings";
    private static final int    MAX_RETRIES = 3;
    private static final long   RETRY_BASE_MS = 500L;

    private final String     apiKey;
    private final String     model;
    private final HttpClient http;
    private final ObjectMapper mapper;

    /**
     * @param apiKey Voyage AI API key
     * @param model  e.g. {@code "voyage-code-3"}
     * @param inputType ignored — retained for API compatibility but never sent (production
     *                  omits input_type by using {@code input_type=None})
     */
    public VoyageEmbedder(String apiKey, String model, String inputType) {
        this.apiKey  = apiKey;
        this.model   = model;
        // inputType deliberately NOT stored: production VoyageAIEmbeddingFunction
        // always passes input_type=None (field omitted from request), matching what
        // the Voyage API uses as its "unspecified" default.
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        this.mapper = new ObjectMapper();
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        String json = buildJson(texts);
        String responseBody = callApi(json);
        try {
            return parseResponseFloat(responseBody);
        } catch (Exception e) {
            throw new RuntimeException("Voyage embed parse failed", e);
        }
    }

    /**
     * Embed texts preserving full double (float64) precision for the parity gate.
     *
     * <p>The Python SDK uses base64 encoding and decodes as float32 binary.  This method
     * decodes the same base64 and promotes float32 → float64 exactly (no further precision
     * loss).  Returning float64 avoids the float32 → JSON → float64 round-trip that caused
     * cosine ≈ 0.9999669 instead of 1.0.
     */
    public List<double[]> embedDouble(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();
        String json = buildJson(texts);
        String responseBody = callApi(json);
        try {
            return parseResponseDouble(responseBody);
        } catch (Exception e) {
            throw new RuntimeException("Voyage embedDouble parse failed", e);
        }
    }

    // ── Request / response helpers ────────────────────────────────────────────

    private String buildJson(List<String> texts) {
        // Mirror chromadb VoyageAIEmbeddingFunction / voyageai.Embedding.create exactly:
        // - encoding_format="base64" (Python SDK default — gives exact float32 binary)
        // - no input_type field (production sends input_type=None → omitted)
        // - no output_dtype field (production does not set it)
        // - truncation=True (LOAD-BEARING: omit → cosine 0.99995 drift on >256-token texts)
        Map<String, Object> body = new HashMap<>();
        body.put("model",           model);
        body.put("input",           texts);
        body.put("truncation",      true);
        body.put("encoding_format", "base64");
        try {
            return mapper.writeValueAsString(body);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize Voyage request", e);
        }
    }

    private String callApi(String json) {
        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                HttpRequest req = HttpRequest.newBuilder()
                        .uri(URI.create(VOYAGE_URL))
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
                    log.warn("event=voyage_retry attempt={} status={} delay_ms={}", attempt, status, delay);
                    Thread.sleep(delay);
                    continue;
                }
                throw new RuntimeException(
                        "Voyage AI request failed: HTTP " + status + " body=" + resp.body());

            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException("Voyage embed interrupted", e);
            } catch (RuntimeException e) {
                throw e;
            } catch (Exception e) {
                if (attempt == MAX_RETRIES) {
                    throw new RuntimeException("Voyage embed failed after " + MAX_RETRIES + " attempts", e);
                }
                try { Thread.sleep(RETRY_BASE_MS * (1L << (attempt - 1))); }
                catch (InterruptedException ix) {
                    Thread.currentThread().interrupt();
                    throw new RuntimeException("interrupted", ix);
                }
            }
        }
        throw new RuntimeException("Voyage embed: exhausted retries"); // unreachable
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> sortedData(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> data = (List<Map<String, Object>>) root.get("data");
        if (data == null || data.isEmpty()) {
            throw new RuntimeException("Voyage AI returned empty data array: " + body);
        }
        data.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));
        return data;
    }

    /**
     * Parse base64-encoded response as float32 vectors.
     *
     * <p>The Python SDK decodes base64 as:
     * {@code np.frombuffer(base64.b64decode(embedding), np.float32)}
     * Little-endian IEEE 754 float32 binary.
     */
    private List<float[]> parseResponseFloat(String body) throws Exception {
        List<Map<String, Object>> data = sortedData(body);
        List<float[]> result = new ArrayList<>(data.size());
        for (Map<String, Object> item : data) {
            result.add(decodeBase64Float32(getEmbeddingField(item, body)));
        }
        return result;
    }

    /**
     * Parse base64-encoded response as float64 vectors (float32 promoted exactly).
     */
    private List<double[]> parseResponseDouble(String body) throws Exception {
        List<Map<String, Object>> data = sortedData(body);
        List<double[]> result = new ArrayList<>(data.size());
        for (Map<String, Object> item : data) {
            float[] f32 = decodeBase64Float32(getEmbeddingField(item, body));
            double[] f64 = new double[f32.length];
            for (int i = 0; i < f32.length; i++) f64[i] = f32[i];   // exact promotion
            result.add(f64);
        }
        return result;
    }

    private String getEmbeddingField(Map<String, Object> item, String body) {
        Object emb = item.get("embedding");
        if (emb == null) throw new RuntimeException("Voyage AI item missing 'embedding': " + body);
        return emb.toString();
    }

    /**
     * Decode a base64 string as an array of IEEE 754 float32 values (little-endian).
     *
     * <p>Matches Python {@code np.frombuffer(base64.b64decode(b64str), np.float32)}.
     */
    private static float[] decodeBase64Float32(String b64) {
        byte[] bytes = Base64.getDecoder().decode(b64);
        if (bytes.length % 4 != 0) {
            throw new RuntimeException("Base64 embedding byte length not multiple of 4: " + bytes.length);
        }
        int dims = bytes.length / 4;
        float[] vec = new float[dims];
        ByteBuffer buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < dims; i++) vec[i] = buf.getFloat();
        return vec;
    }
}
