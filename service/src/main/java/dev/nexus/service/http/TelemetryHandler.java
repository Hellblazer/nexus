package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.TelemetryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.12 — Telemetry HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/telemetry/}):
 * <pre>
 *   POST /v1/telemetry/relevance/log           single relevance event
 *   POST /v1/telemetry/relevance/batch         batch relevance events
 *   GET  /v1/telemetry/relevance/query         query by filters
 *   POST /v1/telemetry/relevance/expire        expire old entries
 *   POST /v1/telemetry/search/batch            batch search telemetry
 *   GET  /v1/telemetry/search/stats            collection health stats
 *   POST /v1/telemetry/search/trim             trim old entries
 *   POST /v1/telemetry/rename_collection       rename collection in all tables
 *   POST /v1/telemetry/tier_writes/record      record a tier-write event
 *   POST /v1/telemetry/nx_answer_runs/record   record an nx_answer run
 *   POST /v1/telemetry/hook_failures/record    record a hook failure
 *   POST /v1/telemetry/hook_failures/trim      trim old hook-failure entries
 *   POST /v1/telemetry/frecency/upsert         upsert frecency record
 *   GET  /v1/telemetry/frecency/get            get frecency by chunk_id
 *   POST /v1/telemetry/import                  fidelity ETL for all 6 tables
 *   POST /v1/telemetry/import_batch             bulk fidelity ETL for one table
 *   POST /v1/telemetry/ids/probe                membership probe (verify-fill inner loop)
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} (via {@link AuthFilter})
 * and {@code X-Nexus-Tenant} header.
 */
public final class TelemetryHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(TelemetryHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** RDR-178 wave-2 (nexus-s3dd4.3): max candidate keys per /ids/probe request. */
    private static final int MAX_PROBE_KEYS = 300;

    private final TelemetryRepository repo;

    public TelemetryHandler(TelemetryRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path   = exchange.getRequestURI().getPath();
        String op     = path.replaceFirst("^/v1/telemetry", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/relevance/log"          -> handleRelevanceLog(exchange, tenant, method);
                case "/relevance/batch"        -> handleRelevanceBatch(exchange, tenant, method);
                case "/relevance/query"        -> handleRelevanceQuery(exchange, tenant, method);
                case "/relevance/expire"       -> handleRelevanceExpire(exchange, tenant, method);
                case "/search/batch"           -> handleSearchBatch(exchange, tenant, method);
                case "/search/stats"           -> handleSearchStats(exchange, tenant, method);
                case "/search/trim"            -> handleSearchTrim(exchange, tenant, method);
                case "/rename_collection"      -> handleRenameCollection(exchange, tenant, method);
                case "/tier_writes/record"     -> handleTierWriteRecord(exchange, tenant, method);
                case "/nx_answer_runs/record"  -> handleNxAnswerRunRecord(exchange, tenant, method);
                case "/hook_failures/record"   -> handleHookFailureRecord(exchange, tenant, method);
                case "/hook_failures/trim"     -> handleHookFailureTrim(exchange, tenant, method);
                case "/frecency/upsert"        -> handleFrecencyUpsert(exchange, tenant, method);
                case "/frecency/get"           -> handleFrecencyGet(exchange, tenant, method);
                case "/import"                 -> handleImport(exchange, tenant, method);
                case "/import_batch"           -> handleImportBatch(exchange, tenant, method);
                case "/ids/probe"               -> handleIdsProbe(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=telemetry_handler_error op={}", op, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    // ── relevance_log ──────────────────────────────────────────────────────────

    private void handleRelevanceLog(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String query      = requireString(body, "query");
        String chunkId    = requireString(body, "chunk_id");
        String action     = requireString(body, "action");
        String sessionId  = optStr(body, "session_id");
        String collection = optStr(body, "collection");
        long id = repo.logRelevance(tenant, query, chunkId, action, sessionId, collection);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    @SuppressWarnings("unchecked")
    private void handleRelevanceBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        var rows = (List<List<String>>) body.getOrDefault("rows", List.of());
        int count = repo.logRelevanceBatch(tenant, rows);
        HttpUtil.send(ex, 200, json(Map.of("inserted", count)));
    }

    private void handleRelevanceQuery(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params  = queryParams(ex);
        String query     = params.getOrDefault("query", "");
        String chunkId   = params.getOrDefault("chunk_id", "");
        String action    = params.getOrDefault("action", "");
        String sessionId = params.getOrDefault("session_id", "");
        int limit = parseIntParam(params, "limit", 100);
        var rows = repo.getRelevanceLog(tenant, query, chunkId, action, sessionId, limit);
        HttpUtil.send(ex, 200, json(rows));
    }

    private void handleRelevanceExpire(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        int days = optInt(body, "days", 90);
        int deleted = repo.expireRelevanceLog(tenant, days);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    // ── search_telemetry ───────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleSearchBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        var rows = (List<List<Object>>) body.getOrDefault("rows", List.of());
        List<Object[]> tuples = rows.stream()
            .map(r -> r.toArray(Object[]::new))
            .toList();
        int count = repo.logSearchBatch(tenant, tuples);
        HttpUtil.send(ex, 200, json(Map.of("inserted", count)));
    }

    private void handleSearchStats(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params = queryParams(ex);
        String collection = params.getOrDefault("collection", "");
        int days = parseIntParam(params, "days", 30);
        var stats = repo.queryCollectionStats(tenant, collection, days);
        HttpUtil.send(ex, 200, json(stats));
    }

    private void handleSearchTrim(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        int days = optInt(body, "days", 30);
        int deleted = repo.trimSearchTelemetry(tenant, days);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    // ── rename_collection ──────────────────────────────────────────────────────

    private void handleRenameCollection(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String oldName = requireString(body, "old");
        String newName = requireString(body, "new");
        var counts = repo.renameCollection(tenant, oldName, newName);
        HttpUtil.send(ex, 200, json(counts));
    }

    // ── tier_writes ────────────────────────────────────────────────────────────

    private void handleTierWriteRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String sessionId   = requireString(body, "session_id");
        String tsIso       = optStr(body, "ts");
        String tool        = requireString(body, "tool");
        String tier        = requireString(body, "tier");
        String agent       = optStrNull(body, "agent");
        String project     = optStrNull(body, "project");
        String targetTitle = optStrNull(body, "target_title");
        repo.recordTierWrite(tenant, sessionId, tsIso, tool, tier, agent, project, targetTitle);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    // ── nx_answer_runs ─────────────────────────────────────────────────────────

    private void handleNxAnswerRunRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String question = requireString(body, "question");
        // Same number-or-string coercion as the /import path (nexus-5gaj7): never a
        // bare ((Number)) cast that 500s on a stringified numeric field.
        Long planId      = optLongNull(body, "plan_id");
        Double conf      = optDoubleNull(body, "matched_confidence");
        int stepCount    = optInt(body, "step_count", 0);
        String finalText = optStr(body, "final_text");
        Double cost      = optDoubleNull(body, "cost_usd");
        Long durationMs  = optLongNull(body, "duration_ms");
        double costUsd   = cost != null ? cost : 0.0;
        long durationMsV = durationMs != null ? durationMs : 0L;
        String createdAt = optStr(body, "created_at");
        repo.recordNxAnswerRun(tenant, question, planId, conf, stepCount, finalText, costUsd, durationMsV, createdAt);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    // ── hook_failures ──────────────────────────────────────────────────────────

    private void handleHookFailureRecord(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String docId       = optStr(body, "doc_id");
        String collection  = optStr(body, "collection");
        String hookName    = requireString(body, "hook_name");
        String error       = optStr(body, "error");
        String occurredAt  = optStr(body, "occurred_at");
        String batchDocIds = optStrNull(body, "batch_doc_ids");
        boolean isBatch    = Boolean.TRUE.equals(body.get("is_batch"));
        String chain       = optStr(body, "chain");
        repo.recordHookFailure(tenant, docId, collection, hookName, error, occurredAt,
            batchDocIds, isBatch, chain);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    private void handleHookFailureTrim(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        int days = optInt(body, "days", 30);
        int deleted = repo.trimHookFailures(tenant, days);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    // ── frecency ───────────────────────────────────────────────────────────────

    private void handleFrecencyUpsert(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String chunkId       = requireString(body, "chunk_id");
        String embeddedAt    = optStr(body, "embedded_at");
        int ttlDays          = optInt(body, "ttl_days", 0);
        double frecencyScore = body.get("frecency_score") != null
            ? ((Number) body.get("frecency_score")).doubleValue() : 0.0;
        int missCount        = optInt(body, "miss_count", 0);
        String lastHitAt     = optStr(body, "last_hit_at");
        repo.upsertFrecency(tenant, chunkId, embeddedAt, ttlDays, frecencyScore, missCount, lastHitAt);
        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    private void handleFrecencyGet(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var params  = queryParams(ex);
        String chunkId = params.getOrDefault("chunk_id", "");
        if (chunkId.isBlank()) {
            HttpUtil.send(ex, 400, json(Map.of("error", "chunk_id required")));
            return;
        }
        var result = repo.getFrecency(tenant, chunkId);
        if (result.isEmpty()) {
            HttpUtil.send(ex, 404, json(Map.of("error", "not found")));
        } else {
            HttpUtil.send(ex, 200, json(result.get()));
        }
    }

    // ── import (ETL fidelity-preserving, all tables) ───────────────────────────

    /**
     * Fidelity-preserving bulk import endpoint (ETL path).
     *
     * <p>Request body is a JSON object with optional arrays for each table:
     * <pre>
     * {
     *   "table": "relevance_log" | "search_telemetry" | "tier_writes" |
     *            "nx_answer_runs" | "hook_failures" | "frecency",
     *   ... table-specific fields ...
     * }
     * </pre>
     *
     * <p>One row per request for simplicity (matching the .8/.11 per-row HTTP pattern).
     * Each row is dispatched to the correct import method based on the {@code table} field.
     */
    private void handleImport(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body  = readBody(ex);
        String table = requireString(body, "table");

        switch (table) {
            case "relevance_log" -> {
                repo.importRelevanceRow(tenant,
                    requireString(body, "query"),
                    requireString(body, "chunk_id"),
                    optStr(body, "collection"),
                    requireString(body, "action"),
                    optStr(body, "session_id"),
                    requireString(body, "timestamp"));
            }
            case "search_telemetry" -> {
                Double topDist = optDoubleNull(body, "top_distance");
                Double threshold = optDoubleNull(body, "threshold");
                Long rawCount = optLongNull(body, "raw_count");
                Long keptCount = optLongNull(body, "kept_count");
                if (rawCount == null) throw new IllegalArgumentException("Missing required field: raw_count");
                if (keptCount == null) throw new IllegalArgumentException("Missing required field: kept_count");
                repo.importSearchRow(tenant,
                    requireString(body, "ts"),
                    requireString(body, "query_hash"),
                    requireString(body, "collection"),
                    rawCount.intValue(),
                    keptCount.intValue(),
                    topDist, threshold);
            }
            case "tier_writes" -> {
                repo.importTierWriteRow(tenant,
                    requireString(body, "session_id"),
                    requireString(body, "ts"),
                    requireString(body, "tool"),
                    requireString(body, "tier"),
                    optStrNull(body, "agent"),
                    optStrNull(body, "project"),
                    optStrNull(body, "target_title"));
            }
            case "nx_answer_runs" -> {
                // plan_id is a BIGINT; the SQLite ETL historically serialized it as a
                // string, causing String->Number ClassCastException (nexus-5gaj7).
                // optLongNull / optDoubleNull accept either a number or a numeric string.
                Long planId = optLongNull(body, "plan_id");
                Double conf = optDoubleNull(body, "matched_confidence");
                Double cost = optDoubleNull(body, "cost_usd");
                Long durationMs = optLongNull(body, "duration_ms");
                repo.importNxAnswerRunRow(tenant,
                    requireString(body, "question"),
                    planId, conf,
                    optInt(body, "step_count", 0),
                    optStr(body, "final_text"),
                    cost != null ? cost : 0.0,
                    durationMs != null ? durationMs : 0L,
                    requireString(body, "created_at"));
            }
            case "hook_failures" -> {
                repo.importHookFailureRow(tenant,
                    optStr(body, "doc_id"),
                    optStr(body, "collection"),
                    requireString(body, "hook_name"),
                    optStr(body, "error"),
                    requireString(body, "occurred_at"),
                    optStrNull(body, "batch_doc_ids"),
                    Boolean.TRUE.equals(body.get("is_batch")),
                    optStr(body, "chain"));
            }
            case "frecency" -> {
                Double frecScore = optDoubleNull(body, "frecency_score");
                repo.upsertFrecency(tenant,
                    requireString(body, "chunk_id"),
                    optStr(body, "embedded_at"),
                    optInt(body, "ttl_days", 0),
                    frecScore != null ? frecScore : 0.0,
                    optInt(body, "miss_count", 0),
                    optStr(body, "last_hit_at"));
            }
            default -> throw new IllegalArgumentException("Unknown table: " + table);
        }

        HttpUtil.send(ex, 200, json(Map.of("ok", true)));
    }

    /**
     * POST /v1/telemetry/import_batch — RDR-176 P3 (Gap 1, bead nexus-t9rmg.18).
     *
     * <p>Body {@code {"table": "<one of the six>", "rows": [ {<table fields>}, … ]}}.
     * The whole batch for one table lands under ONE tenant transaction (GUC set
     * once) via {@link dev.nexus.service.db.TelemetryRepository#importBatch}.
     * Response 200 {@code {"imported": <int>}}.
     */
    private void handleImportBatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String table = requireString(body, "table");
        Object rowsObj = body.get("rows");
        if (!(rowsObj instanceof List<?> rawRows)) {
            throw new IllegalArgumentException("field 'rows' must be a JSON array");
        }
        List<Map<String, Object>> rows = new ArrayList<>(rawRows.size());
        for (Object o : rawRows) {
            if (!(o instanceof Map<?, ?> rm)) {
                throw new IllegalArgumentException("each element of 'rows' must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> row = (Map<String, Object>) rm;
            rows.add(row);
        }
        int imported = repo.importBatch(tenant, table, rows);
        HttpUtil.send(ex, 200, json(Map.of("imported", imported)));
    }

    /**
     * POST /v1/telemetry/ids/probe — RDR-178 wave-2 P1 (bead nexus-s3dd4.3).
     *
     * <p>Membership-probe identity endpoint for the verify-fill inner loop: the
     * caller sends up to {@link #MAX_PROBE_KEYS} candidate conflict-key tuples
     * for one of the six telemetry tables; the response is the subset already
     * present, echoed back verbatim (never reconstructed from the stored
     * values — see {@link TelemetryRepository#probeIds}).
     *
     * <p>Body {@code {"table": "<one of the six>", "keys": [[...], ...]}}.
     * Response 200 {@code {"present": [[...], ...]}}. A batch larger than
     * {@value #MAX_PROBE_KEYS} is rejected with HTTP 400 rather than silently
     * truncated (chroma_quotas-style batch discipline).
     */
    private void handleIdsProbe(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var body = readBody(ex);
        String table = requireString(body, "table");
        Object keysObj = body.get("keys");
        if (!(keysObj instanceof List<?> rawKeys)) {
            throw new IllegalArgumentException("field 'keys' must be a JSON array");
        }
        if (rawKeys.size() > MAX_PROBE_KEYS) {
            throw new IllegalArgumentException(
                "field 'keys' exceeds max batch size of " + MAX_PROBE_KEYS + " (got " + rawKeys.size() + ")");
        }
        List<List<Object>> keys = new ArrayList<>(rawKeys.size());
        for (Object o : rawKeys) {
            if (!(o instanceof List<?> keyList)) {
                throw new IllegalArgumentException("each element of 'keys' must be a JSON array");
            }
            @SuppressWarnings("unchecked")
            List<Object> key = (List<Object>) keyList;
            keys.add(key);
        }
        List<List<Object>> present = repo.probeIds(tenant, table, keys);
        HttpUtil.send(ex, 200, json(Map.of("present", present)));
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private String json(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        } catch (Exception e) {
            log.error("event=telemetry_json_serialize_error", e);
            return "{\"error\":\"serialization failed\"}";
        }
    }

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody()) {
            String text = new String(is.readAllBytes(), StandardCharsets.UTF_8);
            if (text.isBlank()) return Map.of();
            return MAPPER.readValue(text, MAP_TYPE);
        }
    }

    private String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null || v.toString().isBlank()) {
            throw new IllegalArgumentException("Missing required field: " + key);
        }
        return v.toString();
    }

    private Object requireObj(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) throw new IllegalArgumentException("Missing required field: " + key);
        return v;
    }

    private String optStr(Map<String, Object> body, String key) {
        Object v = body.get(key);
        return v != null ? v.toString() : "";
    }

    private String optStrNull(Map<String, Object> body, String key) {
        Object v = body.get(key);
        return v != null ? v.toString() : null;
    }

    private int optInt(Map<String, Object> body, String key, int defaultVal) {
        Object v = body.get(key);
        if (v == null) return defaultVal;
        if (v instanceof Number n) return n.intValue();
        // A numeric string is coerced (same ETL-stringification class as optLongNull);
        // a non-numeric string throws (HTTP 400) rather than silently returning the
        // default, which would corrupt step_count / ttl_days / miss_count telemetry.
        if (v instanceof String s) {
            String t = s.trim();
            if (t.isEmpty()) return defaultVal;
            try {
                return Integer.parseInt(t);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "Field '" + key + "' is not a valid integer: " + s);
            }
        }
        throw new IllegalArgumentException(
            "Field '" + key + "' has unexpected type: " + v.getClass().getSimpleName());
    }

    /**
     * Coerce an optional numeric field to Long, accepting either a JSON number or a
     * numeric string. Defensive against ETL payloads that serialize an INTEGER column
     * as a string (the nx_answer_runs.plan_id ClassCastException class). Returns null
     * for absent/blank values; throws IllegalArgumentException (HTTP 400, not a 500
     * crash) for a non-numeric string.
     */
    private Long optLongNull(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) return null;
        if (v instanceof Number n) return n.longValue();
        if (v instanceof String s) {
            String t = s.trim();
            if (t.isEmpty()) return null;
            try {
                return Long.parseLong(t);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "Field '" + key + "' is not a valid integer: " + s);
            }
        }
        throw new IllegalArgumentException(
            "Field '" + key + "' has unexpected type: " + v.getClass().getSimpleName());
    }

    /**
     * Coerce an optional numeric field to Double, accepting a JSON number or numeric
     * string. Companion to {@link #optLongNull} for the same ETL-stringification class.
     */
    private Double optDoubleNull(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) return null;
        if (v instanceof Number n) return n.doubleValue();
        if (v instanceof String s) {
            String t = s.trim();
            if (t.isEmpty()) return null;
            try {
                return Double.parseDouble(t);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                    "Field '" + key + "' is not a valid number: " + s);
            }
        }
        throw new IllegalArgumentException(
            "Field '" + key + "' has unexpected type: " + v.getClass().getSimpleName());
    }

    private Map<String, String> queryParams(HttpExchange ex) {
        String query = ex.getRequestURI().getQuery();
        if (query == null || query.isBlank()) return Map.of();
        var map = new java.util.HashMap<String, String>();
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0) {
                map.put(java.net.URLDecoder.decode(pair.substring(0, eq), StandardCharsets.UTF_8),
                        java.net.URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8));
            }
        }
        return map;
    }

    private int parseIntParam(Map<String, String> params, String key, int def) {
        String v = params.get(key);
        if (v == null || v.isBlank()) return def;
        try { return Integer.parseInt(v); } catch (NumberFormatException e) { return def; }
    }

    private void requireMethod(HttpExchange ex, String actual, String expected) throws IOException {
        if (!actual.equals(expected)) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            throw new IllegalArgumentException("method not allowed");
        }
    }
}
