package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.ScratchRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.*;

/**
 * RDR-152 bead nexus-gmiaf.13 — T1 scratch HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/t1/}):
 * <pre>
 *   POST   /v1/t1/put            put a scratch entry
 *   POST   /v1/t1/get            get by id (body: {id, session_id})
 *   POST   /v1/t1/search         FTS search (body: {query, session_id, limit})
 *   POST   /v1/t1/list           list session entries (body: {session_id})
 *   POST   /v1/t1/flagged        list flagged entries (body: {session_id})
 *   POST   /v1/t1/flag           flag entry for T2 flush (body: {id, session_id, project, title})
 *   POST   /v1/t1/unflag         unflag entry (body: {id, session_id})
 *   DELETE /v1/t1/delete         delete by id (body: {id, session_id})
 *   POST   /v1/t1/resolve_prefix resolve id prefix to full ids (body: {prefix, session_id})
 *   POST   /v1/t1/session/close  delete all entries for session (body: {session_id})
 *   POST   /v1/t1/sweep          TTL sweep (superuser path — NOT exposed to nexus_svc)
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant} header.
 *
 * <p>Session-close (POST /v1/t1/session/close): deletes all scratch rows for
 * (tenant, session_id). Called from the Python MCP lifespan on exit for promptness.
 * The service also runs a periodic TTL sweep (POST /v1/t1/sweep) as a crash-safety
 * backstop, reaping idle sessions that never called session-close.
 *
 * <p>SEARCH BEHAVIOR CHANGE: T1 scratch search was vector/cosine (ChromaDB ONNX).
 * This PG backend uses FTS (tsvector). See {@link ScratchRepository} for details.
 *
 * <p>NX_T1_SESSION env var: Python client passes session_id as a request body field
 * ({@code session_id}) and as the {@code X-Nexus-T1-Session} header (for observability).
 * Sub-agents share scratch by inheriting the same NX_T1_SESSION token.
 */
public final class ScratchHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ScratchHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** HTTP header carrying session_id (observability; repository reads body field). */
    public static final String HEADER_T1_SESSION = "X-Nexus-T1-Session";

    /**
     * TTL sweep cutoff: rows older than 24 hours are reaped.
     * Conservative default; the real expiry is session-close which is immediate.
     */
    static final long SWEEP_HOURS = 24L;

    private final ScratchRepository repo;

    public ScratchHandler(ScratchRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = (String) exchange.getAttribute(AuthFilter.ATTR_TENANT);
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path = exchange.getRequestURI().getPath();
        // Strip prefix /v1/t1  → /put, /get, etc.
        String op     = path.replaceFirst("^/v1/t1", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/put"            -> handlePut(exchange, tenant, method);
                case "/get"            -> handleGet(exchange, tenant, method);
                case "/search"         -> handleSearch(exchange, tenant, method);
                case "/list"           -> handleList(exchange, tenant, method);
                case "/flagged"        -> handleFlagged(exchange, tenant, method);
                case "/flag"           -> handleFlag(exchange, tenant, method);
                case "/unflag"         -> handleUnflag(exchange, tenant, method);
                case "/delete"         -> handleDelete(exchange, tenant, method);
                case "/resolve_prefix" -> handleResolvePrefix(exchange, tenant, method);
                case "/session/close"  -> handleSessionClose(exchange, tenant, method);
                case "/sweep"          -> handleSweep(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            log.debug("event=t1_bad_request tenant={} op={} error={}", tenant, op, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=t1_handler_error tenant={} op={}", tenant, op, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    // ── Handlers ─────────────────────────────────────────────────────────────

    /**
     * POST /v1/t1/put
     * Request: {id, session_id, content, tags?, agent?, flagged?, flush_project?, flush_title?}
     * Response 200: {"id": "<uuid>"}
     */
    private void handlePut(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String id         = requireString(body, "id");
        String sessionId  = requireString(body, "session_id");
        String content    = requireString(body, "content");
        String tags       = optStringOrEmpty(body, "tags");
        String agent      = optStringOrNull(body, "agent");
        boolean flagged   = optBool(body, "flagged", false);
        String flushProj  = optStringOrNull(body, "flush_project");
        String flushTitle = optStringOrNull(body, "flush_title");

        String result = repo.put(tenant, sessionId, id, content, tags, agent,
                                  flagged, flushProj, flushTitle);
        HttpUtil.send(ex, 200, json(Map.of("id", result)));
    }

    /**
     * POST /v1/t1/get
     * Request: {id, session_id}
     * Response 200: {entry} or {"found": false}
     */
    private void handleGet(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String id        = requireString(body, "id");
        String sessionId = requireString(body, "session_id");

        Map<String, Object> row = repo.get(tenant, sessionId, id);
        if (row.isEmpty()) {
            HttpUtil.send(ex, 200, json(Map.of("found", false)));
        } else {
            HttpUtil.send(ex, 200, json(row));
        }
    }

    /**
     * POST /v1/t1/search
     * Request: {query, session_id, limit?}
     * Response 200: {"results": [...]}
     *
     * BEHAVIOR CHANGE: was vector/cosine, now FTS. See ScratchRepository.
     */
    private void handleSearch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String query     = requireString(body, "query");
        String sessionId = requireString(body, "session_id");
        int limit        = optIntOrDefault(body, "limit", 10);

        List<Map<String, Object>> results = repo.search(tenant, sessionId, query, limit);
        HttpUtil.send(ex, 200, json(Map.of("results", results)));
    }

    /**
     * POST /v1/t1/list
     * Request: {session_id}
     * Response 200: {"entries": [...]}
     */
    private void handleList(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String sessionId = requireString(body, "session_id");

        List<Map<String, Object>> entries = repo.listEntries(tenant, sessionId);
        HttpUtil.send(ex, 200, json(Map.of("entries", entries)));
    }

    /**
     * POST /v1/t1/flagged
     * Request: {session_id}
     * Response 200: {"entries": [...]}
     */
    private void handleFlagged(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String sessionId = requireString(body, "session_id");

        List<Map<String, Object>> entries = repo.flaggedEntries(tenant, sessionId);
        HttpUtil.send(ex, 200, json(Map.of("entries", entries)));
    }

    /**
     * POST /v1/t1/flag
     * Request: {id, session_id, flush_project?, flush_title?}
     * Response 200: {"ok": true} or {"ok": false, "error": "not found"}
     */
    private void handleFlag(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String id         = requireString(body, "id");
        String sessionId  = requireString(body, "session_id");
        String flushProj  = optStringOrEmpty(body, "flush_project");
        String flushTitle = optStringOrEmpty(body, "flush_title");

        boolean ok = repo.flag(tenant, sessionId, id, flushProj, flushTitle);
        HttpUtil.send(ex, 200, json(Map.of("ok", ok)));
    }

    /**
     * POST /v1/t1/unflag
     * Request: {id, session_id}
     * Response 200: {"ok": true} or {"ok": false}
     */
    private void handleUnflag(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String id        = requireString(body, "id");
        String sessionId = requireString(body, "session_id");

        boolean ok = repo.unflag(tenant, sessionId, id);
        HttpUtil.send(ex, 200, json(Map.of("ok", ok)));
    }

    /**
     * DELETE /v1/t1/delete (also accepts POST for Python httpx compatibility)
     * Request: {id, session_id}
     * Response 200: {"deleted": true} or {"deleted": false}
     */
    private void handleDelete(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST", "DELETE");
        Map<String, Object> body = readBody(ex);
        String id        = requireString(body, "id");
        String sessionId = requireString(body, "session_id");

        boolean deleted = repo.delete(tenant, sessionId, id);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    /**
     * POST /v1/t1/resolve_prefix
     * Request: {prefix, session_id}
     * Response 200: {"ids": [...]}  — empty list if no match; multiple if ambiguous
     */
    private void handleResolvePrefix(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String prefix    = requireString(body, "prefix");
        String sessionId = requireString(body, "session_id");

        List<String> ids = repo.resolvePrefix(tenant, sessionId, prefix);
        HttpUtil.send(ex, 200, json(Map.of("ids", ids)));
    }

    /**
     * POST /v1/t1/session/close
     * Request: {session_id}
     * Response 200: {"deleted": <count>}
     *
     * Called from MCP lifespan on exit. Deletes all scratch rows for
     * (tenant, session_id). Idempotent (double-close returns 0 deleted, not an error).
     */
    private void handleSessionClose(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String sessionId = requireString(body, "session_id");

        int deleted = repo.closeSession(tenant, sessionId);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    /**
     * POST /v1/t1/sweep
     * Request: {} (optional: {hours: <int>})
     * Response 200: {"swept": <count>}
     *
     * TTL sweep for idle sessions within the requesting tenant. Deletes scratch
     * rows older than {@code hours} (default 24h) for the caller's tenant only
     * (RLS enforced — the nexus_svc role has FORCE RLS, so rows for other tenants
     * are invisible even without explicit WHERE).
     *
     * A true cross-tenant sweep (for the superuser "garbage collection" path)
     * requires a BYPASSRLS connection. That path is deferred to bead .30
     * (Phase 5 operational). The per-tenant sweep is sufficient for correctness:
     * each tenant's sweep call cleans its own stale sessions; the session-close
     * endpoint handles normal (non-crash) cleanup promptly.
     */
    private void handleSweep(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long hours = optLongOrDefault(body, "hours", SWEEP_HOURS);

        java.time.OffsetDateTime cutoff = java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC)
            .minusHours(hours);
        int swept = repo.sweepTenant(tenant, cutoff);
        HttpUtil.send(ex, 200, json(Map.of("swept", swept)));
    }

    // ── JSON helpers ─────────────────────────────────────────────────────────

    private String json(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        } catch (Exception e) {
            log.error("event=t1_json_serialize_error", e);
            return "{\"error\":\"serialization failed\"}";
        }
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

    private String optStringOrNull(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        String s = val.toString();
        return s.isBlank() ? null : s;
    }

    private String optStringOrEmpty(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return "";
        return val.toString();
    }

    private boolean optBool(Map<String, Object> body, String key, boolean defaultVal) {
        Object val = body.get(key);
        if (val == null) return defaultVal;
        if (val instanceof Boolean b) return b;
        return Boolean.parseBoolean(val.toString());
    }

    private int optIntOrDefault(Map<String, Object> body, String key, int defaultVal) {
        Object val = body.get(key);
        if (val == null) return defaultVal;
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be an integer");
        }
    }

    private long optLongOrDefault(Map<String, Object> body, String key, long defaultVal) {
        Object val = body.get(key);
        if (val == null) return defaultVal;
        if (val instanceof Number n) return n.longValue();
        try { return Long.parseLong(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be a number");
        }
    }

    private void requireMethod(HttpExchange ex, String actual, String... allowed) throws IOException {
        for (String m : allowed) {
            if (m.equals(actual)) return;
        }
        HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
        throw new IllegalArgumentException("method not allowed: " + actual);
    }
}
