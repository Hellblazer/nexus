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
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.20 — CLOUD Voyage AI embedder.
 *
 * <p>Mirrors the voyageai Python SDK envelope exactly (S0.2 proof):
 * <ul>
 *   <li>{@code truncation: true} — LOAD-BEARING: omitting gives cosine ≈ 0.99995 silent drift</li>
 *   <li>{@code output_dtype: "float"}</li>
 *   <li>Sort response {@code data[]} by {@code index} (API may return out-of-order)</li>
 *   <li>Retry on 429 / 5xx with exponential backoff (max 3 attempts)</li>
 * </ul>
 *
 * <p>REST endpoint: {@code POST https://api.voyageai.com/v1/embeddings}
 * Headers: {@code Authorization: Bearer <key>}, {@code Content-Type: application/json}.
 *
 * <p><strong>CCE LIMITATION (nexus-gmiaf.21 gate):</strong>
 * This class targets the standard {@code /v1/embeddings} endpoint (correct for
 * {@code voyage-code-3}).  The Python cloud path uses Contextualized Cross-Encoder
 * (CCE) via {@code /v1/contextualized-embeddings} for {@code knowledge__},
 * {@code docs__}, and {@code rdr__} collections (model {@code voyage-context-3}).
 * CCE and standard embeddings live in a different embedding space (cross-collection
 * cosine sim ≈ 0.05); mixing them returns semantically garbage results.
 * Cloud-mode Seam B is DISABLED in .20 precisely to avoid this.
 * Do NOT enable cloud mode for voyage-context-3 collections until the CCE path
 * is implemented in {@code .21}.
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
    private final String     inputType;  // "document" or "query"
    private final HttpClient http;
    private final ObjectMapper mapper;

    /**
     * @param apiKey    Voyage AI API key
     * @param model     e.g. {@code "voyage-code-3"} or {@code "voyage-context-3"}
     * @param inputType {@code "document"} for indexing, {@code "query"} for search
     */
    public VoyageEmbedder(String apiKey, String model, String inputType) {
        this.apiKey    = apiKey;
        this.model     = model;
        this.inputType = inputType;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        this.mapper = new ObjectMapper();
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();

        // Build request body mirroring voyageai SDK defaults exactly
        Map<String, Object> body = new HashMap<>();
        body.put("model",        model);
        body.put("input",        texts);
        body.put("input_type",   inputType);
        body.put("truncation",   true);   // LOAD-BEARING — omit → 0.99995 cosine drift
        body.put("output_dtype", "float");

        String json;
        try {
            json = mapper.writeValueAsString(body);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize Voyage request", e);
        }

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

                if (status == 200) {
                    return parseResponse(resp.body());
                }

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
                try { Thread.sleep(RETRY_BASE_MS * (1L << (attempt - 1))); } catch (InterruptedException ix) {
                    Thread.currentThread().interrupt(); throw new RuntimeException("interrupted", ix);
                }
            }
        }
        throw new RuntimeException("Voyage embed: exhausted retries"); // unreachable
    }

    /**
     * Embed texts preserving full double (float64) precision from the JSON response.
     *
     * <p>Unlike {@link #embed} which converts to float32, this method returns the raw
     * double values parsed from the Voyage API JSON response.  Used by the parity gate
     * ({@code /v1/vectors/embed}) to avoid the float32 round-trip precision loss that
     * causes cosine ≈ 0.9999669 instead of 1.0 exactly.
     *
     * <p>Root cause: Voyage API returns float32-precision values serialized as JSON doubles
     * (e.g., {@code 0.12345679}). Java parses to double exactly, then float32 conversion
     * produces the same float32, but serializing back via {@code Float.toString()} gives a
     * different shortest-form string, which Python parses to a slightly different float64.
     * Keeping the original double avoids this round-trip.
     */
    public List<double[]> embedDouble(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();

        Map<String, Object> body = new HashMap<>();
        body.put("model",        model);
        body.put("input",        texts);
        body.put("input_type",   inputType);
        body.put("truncation",   true);
        body.put("output_dtype", "float");

        String json;
        try {
            json = mapper.writeValueAsString(body);
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize Voyage request", e);
        }

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

                if (status == 200) {
                    return parseResponseDouble(resp.body());
                }

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
                    throw new RuntimeException("Voyage embedDouble failed after " + MAX_RETRIES + " attempts", e);
                }
                try { Thread.sleep(RETRY_BASE_MS * (1L << (attempt - 1))); } catch (InterruptedException ix) {
                    Thread.currentThread().interrupt(); throw new RuntimeException("interrupted", ix);
                }
            }
        }
        throw new RuntimeException("Voyage embedDouble: exhausted retries");
    }

    @SuppressWarnings("unchecked")
    private List<float[]> parseResponse(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> data = (List<Map<String, Object>>) root.get("data");
        if (data == null || data.isEmpty()) {
            throw new RuntimeException("Voyage AI returned empty data array: " + body);
        }

        // Sort by index (API may return out-of-order)
        data.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));

        List<float[]> result = new ArrayList<>(data.size());
        for (Map<String, Object> item : data) {
            List<Number> rawEmb = (List<Number>) item.get("embedding");
            if (rawEmb == null) {
                throw new RuntimeException("Voyage AI item missing 'embedding': " + item);
            }
            float[] vec = new float[rawEmb.size()];
            for (int i = 0; i < rawEmb.size(); i++) {
                vec[i] = rawEmb.get(i).floatValue();
            }
            result.add(vec);
        }
        return result;
    }

    @SuppressWarnings("unchecked")
    private List<double[]> parseResponseDouble(String body) throws Exception {
        Map<String, Object> root = mapper.readValue(body, Map.class);
        List<Map<String, Object>> data = (List<Map<String, Object>>) root.get("data");
        if (data == null || data.isEmpty()) {
            throw new RuntimeException("Voyage AI returned empty data array: " + body);
        }

        data.sort(Comparator.comparingInt(m -> ((Number) m.get("index")).intValue()));

        List<double[]> result = new ArrayList<>(data.size());
        for (Map<String, Object> item : data) {
            List<Number> rawEmb = (List<Number>) item.get("embedding");
            if (rawEmb == null) {
                throw new RuntimeException("Voyage AI item missing 'embedding': " + item);
            }
            double[] vec = new double[rawEmb.size()];
            for (int i = 0; i < rawEmb.size(); i++) {
                vec[i] = rawEmb.get(i).doubleValue();  // no float32 truncation
            }
            result.add(vec);
        }
        return result;
    }
}
