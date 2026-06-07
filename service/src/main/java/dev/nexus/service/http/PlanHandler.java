package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.PlanRepository;
import dev.nexus.service.jooq.tables.records.PlansRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.*;

/**
 * RDR-152 bead nexus-gmiaf.11 — Plans HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/plans/}):
 * <pre>
 *   POST   /v1/plans/save               insert/upsert a plan (by tenant_id, project, query)
 *   GET    /v1/plans/get                fetch by id= or (project=&amp;dimensions=)
 *   DELETE /v1/plans/delete             delete by id=
 *   POST   /v1/plans/disable            soft-disable by id= in body
 *   POST   /v1/plans/enable             re-enable by id= in body
 *   POST   /v1/plans/set_scope_tags     update scope_tags
 *   GET    /v1/plans/list_active        list active plans (outcome=, project= optional)
 *   POST   /v1/plans/search             FTS search (query, project optional, limit optional)
 *   GET    /v1/plans/list               list non-expired plans (project optional, limit optional)
 *   GET    /v1/plans/exists             plan_exists check (query=&tag=)
 *   POST   /v1/plans/metrics/match      increment match metrics
 *   POST   /v1/plans/metrics/run_start  increment run_started
 *   POST   /v1/plans/metrics/run_outcome increment run outcome
 *   POST   /v1/plans/import             fidelity-preserving ETL import
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer &lt;token&gt;} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant} header.
 */
public final class PlanHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(PlanHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final PlanRepository repo;

    public PlanHandler(PlanRepository repo) {
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
        String op   = path.replaceFirst("^/v1/plans", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/save"                -> handleSave(exchange, tenant, method);
                case "/get"                 -> handleGet(exchange, tenant, method);
                case "/delete"              -> handleDelete(exchange, tenant, method);
                case "/disable"             -> handleDisable(exchange, tenant, method);
                case "/enable"              -> handleEnable(exchange, tenant, method);
                case "/set_scope_tags"      -> handleSetScopeTags(exchange, tenant, method);
                case "/list_active"         -> handleListActive(exchange, tenant, method);
                case "/search"              -> handleSearch(exchange, tenant, method);
                case "/list"                -> handleList(exchange, tenant, method);
                case "/exists"              -> handleExists(exchange, tenant, method);
                case "/metrics/match"       -> handleMetricsMatch(exchange, tenant, method);
                case "/metrics/run_start"   -> handleMetricsRunStart(exchange, tenant, method);
                case "/metrics/run_outcome" -> handleMetricsRunOutcome(exchange, tenant, method);
                case "/import"              -> handleImport(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            log.debug("event=plans_bad_request tenant={} op={} error={}", tenant, op, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=plans_handler_error tenant={} op={}", tenant, op, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    // ── Handlers ──────────────────────────────────────────────────────────────

    /**
     * POST /v1/plans/save
     * Request: {project, query, plan_json, outcome, tags, ttl, name, verb, scope,
     *           dimensions, default_bindings, parent_dims, scope_tags, match_text}
     * Response 200: {"id": <long>}
     */
    private void handleSave(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String project         = optStringOrEmpty(body, "project");
        String query           = requireString(body, "query");
        String planJson        = requireString(body, "plan_json");
        String outcome         = optStringOrEmpty(body, "outcome");
        String tags            = optStringOrEmpty(body, "tags");
        Integer ttl            = optInt(body, "ttl");
        String name            = optStringOrNull(body, "name");
        String verb            = optStringOrNull(body, "verb");
        String scope           = optStringOrNull(body, "scope");
        String dimensions      = optStringOrNull(body, "dimensions");
        String defaultBindings = optStringOrNull(body, "default_bindings");
        String parentDims      = optStringOrNull(body, "parent_dims");
        String scopeTags       = optStringOrEmpty(body, "scope_tags");
        String matchText       = optStringOrEmpty(body, "match_text");

        long id = repo.savePlan(tenant, project, query, planJson,
                                outcome.isBlank() ? "success" : outcome,
                                tags, ttl, name, verb, scope, dimensions,
                                defaultBindings, parentDims, scopeTags, matchText);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    /**
     * GET /v1/plans/get?id= or ?project=&dimensions=
     * Response 200: {plan} or 404 {"error":"not found"}
     */
    private void handleGet(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());

        Optional<PlansRecord> row;
        if (params.containsKey("id")) {
            long id = parseLong(params.get("id"), "id");
            row = repo.getById(tenant, id);
        } else {
            String project    = requireParam(params, "project");
            String dimensions = requireParam(params, "dimensions");
            row = repo.getByDimensions(tenant, project, dimensions);
        }

        if (row.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
        } else {
            HttpUtil.send(ex, 200, json(PlanRepository.recordToMap(row.get())));
        }
    }

    /**
     * DELETE /v1/plans/delete?id=
     * Response 200: {"deleted": true|false}
     */
    private void handleDelete(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "DELETE");
        Map<String, String> params = queryParams(ex.getRequestURI());
        long id = parseLong(requireParam(params, "id"), "id");
        boolean deleted = repo.delete(tenant, id);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    /**
     * POST /v1/plans/disable
     * Request: {"id": <long>}
     * Response 200: {"updated": true|false}
     */
    private void handleDisable(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long id = requireLong(body, "id");
        String reason = body.containsKey("reason") ? String.valueOf(body.get("reason")) : "";
        boolean updated = repo.disable(tenant, id, reason);
        HttpUtil.send(ex, 200, json(Map.of("updated", updated)));
    }

    /**
     * POST /v1/plans/enable
     * Request: {"id": <long>}
     * Response 200: {"updated": true|false}
     */
    private void handleEnable(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long id = requireLong(body, "id");
        boolean updated = repo.enable(tenant, id);
        HttpUtil.send(ex, 200, json(Map.of("updated", updated)));
    }

    /**
     * POST /v1/plans/set_scope_tags
     * Request: {"id": <long>, "scope_tags": "..."}
     * Response 200: {"updated": true|false}
     */
    private void handleSetScopeTags(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long id         = requireLong(body, "id");
        String scopeTags = optStringOrEmpty(body, "scope_tags");
        boolean updated = repo.setScopeTags(tenant, id, scopeTags);
        HttpUtil.send(ex, 200, json(Map.of("updated", updated)));
    }

    /**
     * GET /v1/plans/list_active?outcome=success&amp;project=
     * Response 200: [plan objects]
     */
    private void handleListActive(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String outcome = params.getOrDefault("outcome", "success");
        String project = params.get("project");
        var rows = repo.listActivePlans(tenant, outcome, project);
        HttpUtil.send(ex, 200, json(rows.stream().map(PlanRepository::recordToMap).toList()));
    }

    /**
     * POST /v1/plans/search
     * Request: {"query": "...", "project": "...", "limit": 5}
     * Response 200: [plan objects]
     */
    private void handleSearch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String query   = requireString(body, "query");
        String project = optStringOrNull(body, "project");
        int limit      = optIntDefault(body, "limit", 5);
        var rows = repo.searchPlans(tenant, query, project, limit);
        HttpUtil.send(ex, 200, json(rows.stream().map(PlanRepository::recordToMap).toList()));
    }

    /**
     * GET /v1/plans/list?project=&amp;limit=&amp;include_disabled=
     * Response 200: [plan objects]
     */
    private void handleList(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String project        = params.get("project");
        int limit             = parseIntDefault(params.getOrDefault("limit", "20"), 20);
        boolean inclDisabled  = "true".equalsIgnoreCase(params.get("include_disabled"));
        var rows = repo.listPlans(tenant, project, limit, inclDisabled);
        HttpUtil.send(ex, 200, json(rows.stream().map(PlanRepository::recordToMap).toList()));
    }

    /**
     * GET /v1/plans/exists?query=&amp;tag=
     * Response 200: {"exists": true|false}
     */
    private void handleExists(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String query = requireParam(params, "query");
        String tag   = requireParam(params, "tag");
        boolean exists = repo.planExists(tenant, query, tag);
        HttpUtil.send(ex, 200, json(Map.of("exists", exists)));
    }

    /**
     * POST /v1/plans/metrics/match
     * Request: {"id": <long>, "confidence": <double|null>}
     * Response 200: {}
     */
    private void handleMetricsMatch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long id = requireLong(body, "id");
        Double confidence = optDoubleOrNull(body, "confidence");
        repo.incrementMatchMetrics(tenant, id, confidence);
        HttpUtil.send(ex, 200, "{}");
    }

    /**
     * POST /v1/plans/metrics/run_start
     * Request: {"id": <long>}
     * Response 200: {}
     */
    private void handleMetricsRunStart(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long id = requireLong(body, "id");
        repo.incrementRunStarted(tenant, id);
        HttpUtil.send(ex, 200, "{}");
    }

    /**
     * POST /v1/plans/metrics/run_outcome
     * Request: {"id": <long>, "success": true|false}
     * Response 200: {}
     */
    private void handleMetricsRunOutcome(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long id      = requireLong(body, "id");
        boolean succ = optBoolean(body, "success", true);
        repo.incrementRunOutcome(tenant, id, succ);
        HttpUtil.send(ex, 200, "{}");
    }

    /**
     * POST /v1/plans/import
     *
     * <p>Fidelity-preserving ETL import (bead nexus-gmiaf.11, RDR-152 P2.1).
     * Preserves source {@code created_at}, all counter columns, and
     * {@code disabled_at} verbatim via EXCLUDED.* on conflict.
     *
     * <p>Fields:
     * <pre>
     *   project, query, plan_json   (required strings)
     *   outcome, tags               (optional strings; default "success"/"")
     *   created_at                  (required ISO-8601 UTC string — fidelity field)
     *   ttl                         (optional integer)
     *   name, verb, scope, dimensions, default_bindings, parent_dims (optional strings)
     *   use_count, match_count, success_count, failure_count (optional ints; default 0)
     *   match_conf_sum              (optional double; default 0.0)
     *   last_used, disabled_at      (optional ISO-8601 UTC strings; null means absent)
     *   scope_tags, match_text      (optional strings; default "")
     * </pre>
     *
     * <p>Response 200: {"id": <long>}
     */
    private void handleImport(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String project         = optStringOrEmpty(body, "project");
        String query           = requireString(body, "query");
        String planJson        = requireString(body, "plan_json");
        String outcome         = optStringOrEmpty(body, "outcome");
        String tags            = optStringOrEmpty(body, "tags");
        Integer ttl            = optInt(body, "ttl");
        String name            = optStringOrNull(body, "name");
        String verb            = optStringOrNull(body, "verb");
        String scope           = optStringOrNull(body, "scope");
        String dimensions      = optStringOrNull(body, "dimensions");
        String defaultBindings = optStringOrNull(body, "default_bindings");
        String parentDims      = optStringOrNull(body, "parent_dims");
        String scopeTags       = optStringOrEmpty(body, "scope_tags");
        String matchText       = optStringOrEmpty(body, "match_text");

        // Fidelity fields
        String caRaw = requireString(body, "created_at");
        OffsetDateTime createdAt;
        try {
            createdAt = OffsetDateTime.parse(caRaw).withOffsetSameInstant(ZoneOffset.UTC);
        } catch (Exception e) {
            throw new IllegalArgumentException("field 'created_at' must be ISO-8601 UTC, got: " + caRaw);
        }

        int useCount      = optIntDefault(body, "use_count",      0);
        int matchCount    = optIntDefault(body, "match_count",     0);
        double matchConf  = optDoubleDefault(body, "match_conf_sum", 0.0);
        int successCount  = optIntDefault(body, "success_count",   0);
        int failureCount  = optIntDefault(body, "failure_count",   0);

        OffsetDateTime lastUsed    = parseOptTimestamp(body, "last_used");
        OffsetDateTime disabledAt  = parseOptTimestamp(body, "disabled_at");

        long id = repo.importRow(tenant, project, query, planJson,
                                 outcome.isBlank() ? "success" : outcome,
                                 tags, createdAt, ttl, name, verb, scope, dimensions,
                                 defaultBindings, parentDims, useCount, lastUsed,
                                 matchCount, matchConf, successCount, failureCount,
                                 scopeTags, matchText, disabledAt);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    // ── Serialization helpers ─────────────────────────────────────────────────

    private String json(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        } catch (Exception e) {
            log.error("event=json_serialize_error", e);
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

    private Integer optInt(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be an integer");
        }
    }

    private int optIntDefault(Map<String, Object> body, String key, int def) {
        Object val = body.get(key);
        if (val == null) return def;
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) { return def; }
    }

    private double optDoubleDefault(Map<String, Object> body, String key, double def) {
        Object val = body.get(key);
        if (val == null) return def;
        if (val instanceof Number n) return n.doubleValue();
        try { return Double.parseDouble(val.toString()); }
        catch (NumberFormatException e) { return def; }
    }

    private Double optDoubleOrNull(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Number n) return n.doubleValue();
        String s = val.toString();
        if (s.equalsIgnoreCase("null") || s.isBlank()) return null;
        try { return Double.parseDouble(s); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be a number or null");
        }
    }

    private boolean optBoolean(Map<String, Object> body, String key, boolean def) {
        Object val = body.get(key);
        if (val == null) return def;
        if (val instanceof Boolean b) return b;
        return Boolean.parseBoolean(val.toString());
    }

    private long requireLong(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) throw new IllegalArgumentException("missing required field: " + key);
        if (val instanceof Number n) return n.longValue();
        try { return Long.parseLong(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be a long integer");
        }
    }

    private OffsetDateTime parseOptTimestamp(Map<String, Object> body, String key) {
        String raw = optStringOrNull(body, key);
        if (raw == null) return null;
        try {
            return OffsetDateTime.parse(raw).withOffsetSameInstant(ZoneOffset.UTC);
        } catch (Exception e) {
            throw new IllegalArgumentException(
                "field '" + key + "' must be ISO-8601 UTC or null, got: " + raw);
        }
    }

    private void requireMethod(HttpExchange ex, String actual, String expected) throws IOException {
        if (!expected.equalsIgnoreCase(actual)) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            throw new SkipHandlerException();
        }
    }

    private static Map<String, String> queryParams(URI uri) {
        Map<String, String> params = new LinkedHashMap<>();
        String query = uri.getRawQuery();
        if (query == null || query.isBlank()) return params;
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            if (eq < 0) {
                params.put(decode(pair), "");
            } else {
                params.put(decode(pair.substring(0, eq)), decode(pair.substring(eq + 1)));
            }
        }
        return params;
    }

    private static String decode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static String requireParam(Map<String, String> params, String key) {
        String v = params.get(key);
        if (v == null || v.isBlank()) {
            throw new IllegalArgumentException("missing required query param: " + key);
        }
        return v;
    }

    private static long parseLong(String value, String name) {
        try { return Long.parseLong(value); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("query param '" + name + "' must be a long integer");
        }
    }

    private static int parseIntDefault(String value, int def) {
        try { return Integer.parseInt(value); }
        catch (NumberFormatException e) { return def; }
    }

    private static final class SkipHandlerException extends RuntimeException {
        SkipHandlerException() { super(null, null, true, false); }
    }
}
