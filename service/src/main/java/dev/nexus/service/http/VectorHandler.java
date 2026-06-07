// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.ChromaQuotaValidator;
import dev.nexus.service.vectors.VectorRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.20 — Vector HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/vectors/}):
 * <pre>
 *   POST /v1/vectors/upsert-chunks   server-side embed + quota + Chroma write
 *   POST /v1/vectors/search          embed query server-side + Chroma query (multi-collection)
 *   POST /v1/vectors/query           alias for search (mirrors MCP query tool)
 *   POST /v1/vectors/store-put       single-chunk put (MCP store_put path)
 *   POST /v1/vectors/store-get       fetch chunks by IDs (MCP store_get/store_get_many)
 *   POST /v1/vectors/store-list      list collection (MCP store_list)
 *   POST /v1/vectors/store-delete    delete by IDs (MCP store_delete)
 *   GET  /v1/vectors/collections     list all Chroma collections
 *   GET  /v1/vectors/count           count chunks in a collection
 * </pre>
 *
 * <p>Auth: all routes require Bearer token (enforced by {@link AuthFilter}).
 * Tenant header ({@code X-Nexus-Tenant}) is accepted but used only for Postgres RLS;
 * Chroma collection names encode scope via the four-segment convention.
 *
 * <p>Quota violations are caught and returned as HTTP 413 with a JSON error body.
 */
public final class VectorHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(VectorHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final VectorRepository repo;

    public VectorHandler(VectorRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        // Strip prefix /v1/vectors → /upsert-chunks, /search, etc.
        String op = path.replaceFirst("^/v1/vectors", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/upsert-chunks" -> handleUpsertChunks(exchange, method);
                case "/search"        -> handleSearch(exchange, method);
                case "/query"         -> handleSearch(exchange, method);   // alias
                case "/store-put"     -> handleStorePut(exchange, method);
                case "/store-get"     -> handleStoreGet(exchange, method);
                case "/store-list"    -> handleStoreList(exchange, method);
                case "/store-delete"  -> handleStoreDelete(exchange, method);
                case "/collections"   -> handleCollections(exchange, method);
                case "/count"         -> handleCount(exchange, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (ChromaQuotaValidator.QuotaViolation e) {
            log.debug("event=vector_quota_violation op={} field={} actual={} limit={}",
                    op, e.field, e.actual, e.limit);
            HttpUtil.send(exchange, 413, json(Map.of(
                    "error", "quota_violation",
                    "field", e.field,
                    "actual", e.actual,
                    "limit", e.limit)));
        } catch (IllegalArgumentException e) {
            log.debug("event=vector_bad_request op={} error={}", op, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=vector_handler_error op={}", op, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    // ── Handlers ──────────────────────────────────────────────────────────────

    /**
     * POST /v1/vectors/upsert-chunks
     *
     * <p>Primary Seam B write path.  Python sends chunk text (not vectors);
     * this service embeds + validates + writes to Chroma.
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "knowledge__owner__model__v1",
     *   "ids":        ["sha256hex...", ...],
     *   "documents":  ["chunk text", ...],
     *   "metadatas":  [{...}, ...]   // optional; length must match ids if provided
     * }
     * </pre>
     *
     * <p>Response 200: {"upserted": N}
     */
    private void handleUpsertChunks(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection           = requireString(body, "collection");
        List<String> ids            = requireStringList(body, "ids");
        List<String> documents      = requireStringList(body, "documents");
        List<Map<String, Object>> metadatas = optMetadataList(body, "metadatas", ids.size());

        if (ids.size() != documents.size()) {
            throw new IllegalArgumentException(
                    "ids length " + ids.size() + " != documents length " + documents.size());
        }

        repo.upsertChunks(collection, ids, documents, metadatas);
        HttpUtil.send(ex, 200, json(Map.of("upserted", ids.size())));
    }

    /**
     * POST /v1/vectors/search  (also: /query — same logic)
     *
     * <p>Request:
     * <pre>
     * {
     *   "query":       "search text",
     *   "collections": ["name1", "name2", ...],
     *   "n_results":   10,                    // optional, default 10
     *   "where":       {"key": "val"}         // optional metadata filter
     * }
     * </pre>
     *
     * <p>Response 200: [{"id","content","distance","collection", ...metadata}]
     */
    @SuppressWarnings("unchecked")
    private void handleSearch(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String queryText              = requireString(body, "query");
        List<String> collections      = requireStringList(body, "collections");
        int nResults                  = optInt(body, "n_results", 10);
        Map<String, Object> where     = optMap(body, "where");

        var results = repo.search(queryText, collections, nResults, where);
        HttpUtil.send(ex, 200, json(results));
    }

    /**
     * POST /v1/vectors/store-put
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "knowledge__...",
     *   "doc_id":     "sha256hex...",   // chunk ID
     *   "content":    "chunk text",
     *   "metadata":   {...}              // optional
     * }
     * </pre>
     *
     * <p>Response 200: {"id": "..."}
     */
    private void handleStorePut(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection  = requireString(body, "collection");
        String docId       = requireString(body, "doc_id");
        String content     = requireString(body, "content");
        Map<String, Object> metadata = optMap(body, "metadata");
        if (metadata == null) metadata = Map.of();

        String returnedId = repo.put(collection, docId, content, metadata);
        HttpUtil.send(ex, 200, json(Map.of("id", returnedId)));
    }

    /**
     * POST /v1/vectors/store-get
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "...",
     *   "ids":        ["...", ...],    // optional; if absent returns paginated
     *   "limit":      20,              // optional, default 20
     *   "offset":     0               // optional, default 0
     * }
     * </pre>
     *
     * <p>Response 200: {"ids":[...], "documents":[...], "metadatas":[...]}
     */
    @SuppressWarnings("unchecked")
    private void handleStoreGet(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection  = requireString(body, "collection");
        List<String> ids   = optStringList(body, "ids");
        int limit          = optInt(body, "limit", 20);
        int offset         = optInt(body, "offset", 0);

        var result = repo.get(collection, ids, limit, offset);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/vectors/store-list
     *
     * <p>Request: {"collection": "...", "limit": 20, "offset": 0}
     * <p>Response 200: {"ids":[...], "metadatas":[...]}
     */
    private void handleStoreList(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection = requireString(body, "collection");
        int limit         = optInt(body, "limit", 20);
        int offset        = optInt(body, "offset", 0);

        var result = repo.list(collection, limit, offset);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/vectors/store-delete
     *
     * <p>Request: {"collection": "...", "ids": ["...", ...]}
     * <p>Response 200: {"deleted": N}
     */
    private void handleStoreDelete(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String collection  = requireString(body, "collection");
        List<String> ids   = requireStringList(body, "ids");

        int deleted = repo.delete(collection, ids);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    /**
     * GET /v1/vectors/collections
     * Response 200: [{"name":"...", ...}, ...]
     */
    private void handleCollections(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var cols = repo.listCollections();
        HttpUtil.send(ex, 200, json(cols));
    }

    /**
     * GET /v1/vectors/count?collection=...
     * Response 200: {"count": N}
     */
    private void handleCount(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "GET");
        String collection = requireQueryParam(ex, "collection");
        int count = repo.count(collection);
        HttpUtil.send(ex, 200, json(Map.of("count", count)));
    }

    // ── Request parsing helpers ───────────────────────────────────────────────

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody()) {
            byte[] bytes = is.readAllBytes();
            if (bytes.length == 0) return Map.of();
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    private String requireString(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null || val.toString().isBlank()) {
            throw new IllegalArgumentException("missing required field: " + key);
        }
        return val.toString();
    }

    @SuppressWarnings("unchecked")
    private List<String> requireStringList(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (!(val instanceof List<?> list)) {
            throw new IllegalArgumentException("field '" + key + "' must be an array");
        }
        List<String> result = new ArrayList<>(list.size());
        for (Object item : list) result.add(item == null ? "" : item.toString());
        return result;
    }

    @SuppressWarnings("unchecked")
    private List<String> optStringList(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (!(val instanceof List<?> list)) return null;
        List<String> result = new ArrayList<>(list.size());
        for (Object item : list) result.add(item == null ? "" : item.toString());
        return result;
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> optMetadataList(Map<String, Object> body, String key, int expectedSize) {
        Object val = body.get(key);
        if (val == null) {
            // Return list of empty maps as default
            List<Map<String, Object>> defaults = new ArrayList<>(expectedSize);
            for (int i = 0; i < expectedSize; i++) defaults.add(Map.of());
            return defaults;
        }
        if (!(val instanceof List<?> list)) {
            throw new IllegalArgumentException("field '" + key + "' must be an array");
        }
        List<Map<String, Object>> result = new ArrayList<>(list.size());
        for (Object item : list) {
            if (item instanceof Map<?, ?> m) result.add((Map<String, Object>) m);
            else result.add(Map.of());
        }
        return result;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> optMap(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Map<?, ?> m) return (Map<String, Object>) m;
        return null;
    }

    private int optInt(Map<String, Object> body, String key, int defaultValue) {
        Object val = body.get(key);
        if (val == null) return defaultValue;
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be an integer");
        }
    }

    private String requireQueryParam(HttpExchange ex, String key) {
        String raw = ex.getRequestURI().getRawQuery();
        if (raw != null) {
            for (String pair : raw.split("&")) {
                int eq = pair.indexOf('=');
                if (eq > 0) {
                    String k = java.net.URLDecoder.decode(pair.substring(0, eq), java.nio.charset.StandardCharsets.UTF_8);
                    if (k.equals(key)) {
                        String v = java.net.URLDecoder.decode(pair.substring(eq + 1), java.nio.charset.StandardCharsets.UTF_8);
                        if (!v.isBlank()) return v;
                    }
                }
            }
        }
        throw new IllegalArgumentException("missing required query param: " + key);
    }

    private void requireMethod(HttpExchange ex, String actual, String expected) throws IOException {
        if (!expected.equalsIgnoreCase(actual)) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            throw new SkipHandlerException();
        }
    }

    private String json(Object obj) {
        try { return MAPPER.writeValueAsString(obj); }
        catch (Exception e) {
            log.error("event=json_serialize_error", e);
            return "{\"error\":\"serialization failed\"}";
        }
    }

    private static final class SkipHandlerException extends RuntimeException {
        SkipHandlerException() { super(null, null, true, false); }
    }
}
