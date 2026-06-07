// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.20 — Chroma REST API client.
 *
 * <p>Covers both local ({@code chroma run} on loopback) and cloud
 * ({@code api.trychroma.com}) backends.  All operations use the Chroma v2 API:
 * {@code /api/v2/tenants/{tenant}/databases/{database}/collections/...}
 *
 * <p>Tenant scoping for Chroma: Chroma collections are identified by their
 * content-type/owner-scoped name (e.g. {@code knowledge__nexus__voyage-context-3__v1}).
 * The service does NOT apply per-request-tenant RLS on Chroma — Chroma collections
 * are content-addressed, not row-scoped.  The {@code X-Nexus-Tenant} header gates
 * Postgres operations only.  This is the correct behaviour: Chroma collection names
 * already encode scope (owner_id segment in the four-segment naming convention).
 *
 * <p>Retry: transient failures (429, ≥ 500) are retried up to 3 times with
 * 500 ms exponential back-off (mirrors nexus.retry._voyage_with_retry semantics).
 */
public final class ChromaRestClient {

    private static final Logger log = LoggerFactory.getLogger(ChromaRestClient.class);

    /** Chroma Cloud endpoint. */
    public static final String CLOUD_HOST   = "api.trychroma.com";
    public static final int    CLOUD_PORT   = 443;

    /** Default tenant/database for local Chroma instance. */
    public static final String LOCAL_TENANT   = "default_tenant";
    public static final String LOCAL_DATABASE = "default_database";

    private static final int  MAX_RETRIES    = 3;
    private static final long RETRY_BASE_MS  = 500L;

    private static final TypeReference<Map<String, Object>>       MAP_T   = new TypeReference<>() {};
    private static final TypeReference<List<Map<String, Object>>> LIST_T  = new TypeReference<>() {};

    private final String     baseUrl;
    private final String     tenant;
    private final String     database;
    private final String     apiKey;       // null for unauthenticated local
    private final boolean    isCloud;

    private final HttpClient   http;
    private final ObjectMapper mapper;

    // ── Collection ID cache: name → UUID ─────────────────────────────────────
    // Avoids a round-trip per operation to look up the UUID.
    private final java.util.concurrent.ConcurrentHashMap<String, String> collectionIdCache =
            new java.util.concurrent.ConcurrentHashMap<>();

    /**
     * Local Chroma instance (no auth — {@code chroma run} listens on loopback).
     *
     * @param host loopback host (usually {@code "127.0.0.1"})
     * @param port port where {@code chroma run} is listening
     */
    public static ChromaRestClient local(String host, int port) {
        return new ChromaRestClient(
                "http://" + host + ":" + port + "/api/v2",
                LOCAL_TENANT, LOCAL_DATABASE, null, false);
    }

    /**
     * Chroma Cloud instance (X-Chroma-Token auth over HTTPS).
     *
     * @param tenant   Chroma Cloud tenant
     * @param database Chroma Cloud database
     * @param apiKey   Chroma Cloud API key
     */
    public static ChromaRestClient cloud(String tenant, String database, String apiKey) {
        return new ChromaRestClient(
                "https://" + CLOUD_HOST + "/api/v2",
                tenant, database, apiKey, true);
    }

    private ChromaRestClient(String baseUrl, String tenant, String database,
                              String apiKey, boolean isCloud) {
        this.baseUrl  = baseUrl;
        this.tenant   = tenant;
        this.database = database;
        this.apiKey   = apiKey;
        this.isCloud  = isCloud;
        this.http = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        this.mapper = new ObjectMapper();
    }

    // ── Internal helpers ──────────────────────────────────────────────────────

    private String collectionsBase() {
        return baseUrl + "/tenants/" + tenant + "/databases/" + database + "/collections";
    }

    private String collectionBase(String collectionId) {
        return collectionsBase() + "/" + collectionId;
    }

    private HttpRequest.Builder authBuilder(URI uri) {
        var b = HttpRequest.newBuilder(uri)
                .header("Content-Type", "application/json")
                .timeout(Duration.ofSeconds(120));
        if (apiKey != null) {
            // Chroma Cloud uses X-Chroma-Token header (same as Python CloudClient)
            b.header("X-Chroma-Token", apiKey);
        }
        return b;
    }

    private Map<String, Object> doPost(String url, Map<String, Object> body) {
        return doRequestMap("POST", url, body);
    }

    private List<Map<String, Object>> doPostList(String url, Map<String, Object> body) {
        return doRequestList("POST", url, body);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> doGet(String url) {
        return doRequestMap("GET", url, null);
    }

    private List<Map<String, Object>> doGetList(String url) {
        return doRequestList("GET", url, null);
    }

    private Map<String, Object> doRequestMap(String method, String url, Map<String, Object> body) {
        String respBody = doRequest(method, url, body);
        try {
            return mapper.readValue(respBody, MAP_T);
        } catch (Exception e) {
            throw new RuntimeException("Failed to parse Chroma response as map: " + respBody, e);
        }
    }

    private List<Map<String, Object>> doRequestList(String method, String url, Map<String, Object> body) {
        String respBody = doRequest(method, url, body);
        try {
            return mapper.readValue(respBody, LIST_T);
        } catch (Exception e) {
            throw new RuntimeException("Failed to parse Chroma response as list: " + respBody, e);
        }
    }

    private String doRequest(String method, String url, Map<String, Object> body) {
        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                var reqBuilder = authBuilder(URI.create(url));
                if ("POST".equals(method)) {
                    String json = body != null ? mapper.writeValueAsString(body) : "{}";
                    reqBuilder.POST(HttpRequest.BodyPublishers.ofString(json));
                } else {
                    reqBuilder.GET();
                }
                HttpResponse<String> resp = http.send(reqBuilder.build(),
                        HttpResponse.BodyHandlers.ofString());
                int status = resp.statusCode();
                if (status >= 200 && status < 300) {
                    return resp.body();
                }
                boolean retryable = (status == 429 || status >= 500);
                if (retryable && attempt < MAX_RETRIES) {
                    long delay = RETRY_BASE_MS * (1L << (attempt - 1));
                    log.warn("event=chroma_retry attempt={} status={} url={} delay_ms={}",
                            attempt, status, url, delay);
                    Thread.sleep(delay);
                    continue;
                }
                throw new RuntimeException(
                        "Chroma " + method + " " + url + " failed: HTTP " + status +
                        " body=" + resp.body());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException("Chroma request interrupted", e);
            } catch (RuntimeException e) {
                if (attempt == MAX_RETRIES) throw e;
                try { Thread.sleep(RETRY_BASE_MS * (1L << (attempt - 1))); }
                catch (InterruptedException ix) { Thread.currentThread().interrupt(); throw new RuntimeException("interrupted", ix); }
            } catch (Exception e) {
                if (attempt == MAX_RETRIES) throw new RuntimeException("Chroma request failed", e);
                try { Thread.sleep(RETRY_BASE_MS * (1L << (attempt - 1))); }
                catch (InterruptedException ix) { Thread.currentThread().interrupt(); throw new RuntimeException("interrupted", ix); }
            }
        }
        throw new RuntimeException("Chroma request: exhausted retries");
    }

    // ── Collection management ─────────────────────────────────────────────────

    /**
     * Get or create a collection by name.  Returns the collection UUID.
     * Caches the UUID per name to avoid repeated round-trips.
     */
    public String getOrCreateCollection(String name) {
        return collectionIdCache.computeIfAbsent(name, this::fetchOrCreate);
    }

    private String fetchOrCreate(String name) {
        Map<String, Object> body = new HashMap<>();
        body.put("name", name);
        body.put("get_or_create", true);
        // cosine space for all nexus collections
        Map<String, Object> config = new HashMap<>();
        config.put("hnsw", Map.of("space", "cosine"));
        body.put("configuration", config);

        Map<String, Object> resp = doPost(collectionsBase(), body);
        Object id = resp.get("id");
        if (id == null) {
            throw new RuntimeException("Chroma getOrCreate returned no id for " + name + ": " + resp);
        }
        return id.toString();
    }

    /**
     * List all collections.  Returns list of maps with "name" and "id" keys.
     */
    public List<Map<String, Object>> listCollections() {
        return doGetList(collectionsBase());
    }

    /**
     * Count documents in a collection.  Returns 0 if collection doesn't exist.
     */
    public int count(String collectionName) {
        try {
            String colId = getOrCreateCollection(collectionName);
            String body = doRequest("GET", collectionBase(colId) + "/count", null);
            // Response is a plain integer
            return Integer.parseInt(body.trim());
        } catch (Exception e) {
            log.warn("event=chroma_count_failed collection={} error={}", collectionName, e.getMessage());
            return 0;
        }
    }

    // ── Write ─────────────────────────────────────────────────────────────────

    /**
     * Upsert a batch of chunks with pre-computed embeddings.
     *
     * @param collectionName Chroma collection name
     * @param ids            chunk IDs
     * @param documents      chunk texts
     * @param embeddings     pre-computed float arrays (must be aligned with ids)
     * @param metadatas      per-chunk metadata maps
     */
    public void upsert(String collectionName,
                       List<String> ids,
                       List<String> documents,
                       List<float[]> embeddings,
                       List<Map<String, Object>> metadatas) {
        String colId = getOrCreateCollection(collectionName);

        // Paginate: max 300 per Chroma batch
        int page = ChromaQuotaValidator.MAX_RECORDS_PER_WRITE;
        for (int start = 0; start < ids.size(); start += page) {
            int end = Math.min(start + page, ids.size());
            Map<String, Object> body = new HashMap<>();
            body.put("ids",      ids.subList(start, end));
            body.put("documents", documents.subList(start, end));
            body.put("metadatas", metadatas.subList(start, end));

            if (embeddings != null) {
                List<List<Float>> embList = new ArrayList<>(end - start);
                for (int i = start; i < end; i++) {
                    float[] v = embeddings.get(i);
                    List<Float> row = new ArrayList<>(v.length);
                    for (float f : v) row.add(f);
                    embList.add(row);
                }
                body.put("embeddings", embList);
            }

            doPost(collectionBase(colId) + "/upsert", body);
        }
    }

    /**
     * Delete chunks by ID from a collection.
     */
    public int delete(String collectionName, List<String> ids) {
        if (ids.isEmpty()) return 0;
        String colId = getOrCreateCollection(collectionName);
        Map<String, Object> body = Map.of("ids", ids);
        Map<String, Object> resp = doPost(collectionBase(colId) + "/delete", body);
        Object deleted = resp.get("deleted");
        return deleted instanceof Number n ? n.intValue() : 0;
    }

    // ── Read ──────────────────────────────────────────────────────────────────

    /**
     * Semantic search via pre-computed query embedding.
     *
     * @param collectionName target collection
     * @param queryEmbedding query vector (float[])
     * @param nResults       number of results to return
     * @param where          optional metadata filter (may be null)
     * @return raw Chroma query response map
     */
    @SuppressWarnings("unchecked")
    public Map<String, Object> query(String collectionName,
                                     float[] queryEmbedding,
                                     int nResults,
                                     Map<String, Object> where) {
        String colId = getOrCreateCollection(collectionName);

        // Wrap single query embedding in outer list
        List<List<Float>> queryEmbs = new ArrayList<>(1);
        List<Float> qVec = new ArrayList<>(queryEmbedding.length);
        for (float f : queryEmbedding) qVec.add(f);
        queryEmbs.add(qVec);

        Map<String, Object> body = new HashMap<>();
        body.put("query_embeddings", queryEmbs);
        body.put("n_results", nResults);
        body.put("include", List.of("documents", "metadatas", "distances"));
        if (where != null && !where.isEmpty()) {
            body.put("where", where);
        }

        return doPost(collectionBase(colId) + "/query", body);
    }

    /**
     * Get chunks by ID, or get all (paginated).
     *
     * @param collectionName target collection
     * @param ids            specific IDs to fetch (may be null or empty for "get all")
     * @param limit          max records to return
     * @param offset         skip N records
     * @param include        fields to include in response
     * @return raw Chroma get response map
     */
    public Map<String, Object> get(String collectionName,
                                   List<String> ids,
                                   int limit, int offset,
                                   List<String> include,
                                   Map<String, Object> where) {
        String colId = getOrCreateCollection(collectionName);

        Map<String, Object> body = new HashMap<>();
        if (ids != null && !ids.isEmpty()) {
            body.put("ids", ids);
        }
        if (limit > 0)  body.put("limit",  limit);
        if (offset > 0) body.put("offset", offset);
        body.put("include", include != null ? include : List.of("documents", "metadatas"));
        if (where != null && !where.isEmpty()) {
            body.put("where", where);
        }

        return doPost(collectionBase(colId) + "/get", body);
    }

    /**
     * Heartbeat check — returns true if Chroma server is responsive.
     */
    public boolean heartbeat() {
        try {
            String resp = doRequest("GET", baseUrl + "/heartbeat", null);
            return resp != null && !resp.isEmpty();
        } catch (Exception e) {
            return false;
        }
    }
}
