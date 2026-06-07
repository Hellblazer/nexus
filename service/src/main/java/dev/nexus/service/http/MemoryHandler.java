package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.MemoryRepository;
import dev.nexus.service.jooq.tables.records.MemoryRecord;
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
 * RDR-152 bead nexus-gmiaf.7 — Memory HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/memory/}):
 * <pre>
 *   POST   /v1/memory/put               upsert entry
 *   POST   /v1/memory/put_or_merge      server-side Jaccard scan + conditional merge or insert
 *   GET    /v1/memory/get               fetch by (project+title) or id=
 *   GET    /v1/memory/resolve           exact-then-prefix title resolution
 *   POST   /v1/memory/search            FTS search (access=track|silent)
 *   GET    /v1/memory/list              list entries (summary, optional project/agent filter)
 *   GET    /v1/memory/projects          distinct projects with prefix
 *   POST   /v1/memory/search_glob       FTS scoped to project glob
 *   POST   /v1/memory/search_by_tag     FTS scoped to tag
 *   GET    /v1/memory/all               all entries for project
 *   DELETE /v1/memory/delete            delete by (project+title) or id=
 *   POST   /v1/memory/expire            delete TTL-expired entries (returns deleted ids)
 *   POST   /v1/memory/merge             atomic merge: update keep_id + delete delete_ids
 *   GET    /v1/memory/flag_stale        entries not accessed within idle_days
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant} header (extracted by AuthFilter
 * into exchange attribute {@link AuthFilter#ATTR_TENANT}).
 *
 * <p>All request/response bodies are JSON. Errors return
 * {@code {"error":"<message>"}} with appropriate HTTP status.
 */
public final class MemoryHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(MemoryHandler.class);

    // Jackson configured to handle OffsetDateTime and skip null fields in output.
    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final MemoryRepository repo;

    public MemoryHandler(MemoryRepository repo) {
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
        // Strip prefix /v1/memory  → /put, /get, etc.
        String op = path.replaceFirst("^/v1/memory", "");

        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/put"            -> handlePut(exchange, tenant, method);
                case "/put_or_merge"   -> handlePutOrMerge(exchange, tenant, method);
                case "/get"            -> handleGet(exchange, tenant, method);
                case "/resolve"        -> handleResolve(exchange, tenant, method);
                case "/search"         -> handleSearch(exchange, tenant, method);
                case "/list"           -> handleList(exchange, tenant, method);
                case "/projects"       -> handleProjects(exchange, tenant, method);
                case "/search_glob"    -> handleSearchGlob(exchange, tenant, method);
                case "/search_by_tag"  -> handleSearchByTag(exchange, tenant, method);
                case "/all"            -> handleAll(exchange, tenant, method);
                case "/delete"         -> handleDelete(exchange, tenant, method);
                case "/expire"         -> handleExpire(exchange, tenant, method);
                case "/merge"          -> handleMerge(exchange, tenant, method);
                case "/flag_stale"     -> handleFlagStale(exchange, tenant, method);
                case "/import"         -> handleImport(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            log.debug("event=memory_bad_request tenant={} op={} error={}", tenant, op, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (IllegalStateException e) {
            // e.g. keepId not found in merge
            log.debug("event=memory_conflict tenant={} op={} error={}", tenant, op, e.getMessage());
            HttpUtil.send(exchange, 409, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            log.error("event=memory_handler_error tenant={} op={}", tenant, op, e);
            HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
        }
    }

    // ── Handlers ──────────────────────────────────────────────────────────────

    /**
     * POST /v1/memory/put
     * Request: {"project","title","content","tags","ttl","agent","session"}
     * Response 200: {"id": <long>}
     */
    private void handlePut(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String project = requireString(body, "project");
        String title   = requireString(body, "title");
        String content = requireString(body, "content");
        // tags must be "" when absent/null — never null in DB (Critical #2)
        String tags    = optStringOrEmpty(body, "tags");
        String session = optStringOrNull(body, "session");
        String agent   = optStringOrNull(body, "agent");
        Integer ttl    = optInt(body, "ttl");

        long id = repo.upsert(tenant, project, title, content, tags, session, agent, ttl);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    /**
     * POST /v1/memory/put_or_merge
     * Request: {"project","title","content","tags","ttl","agent","session","min_similarity"}
     * Response 200: {"id":<long>,"action":"inserted"|"merged"}
     *
     * <p>Server-side Jaccard scan + conditional merge or upsert in a single transaction.
     * Eliminates the TOCTOU window of the client-composed path
     * (get_all → merge_memories). See {@link MemoryRepository#putOrMerge}.
     */
    private void handlePutOrMerge(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String project = requireString(body, "project");
        String title   = requireString(body, "title");
        // content may be empty string (no word-set → plain insert, no Jaccard scan,
        // no division-by-zero).  Use optStringOrEmpty so "" is accepted.
        String content = optStringOrEmpty(body, "content");
        String tags    = optStringOrEmpty(body, "tags");
        String session = optStringOrNull(body, "session");
        String agent   = optStringOrNull(body, "agent");
        Integer ttl    = optInt(body, "ttl");
        double minSim  = optDouble(body, "min_similarity", 0.5);

        long[] result = repo.putOrMerge(tenant, project, title, content, tags, session, agent, ttl, minSim);
        String action = result[1] == 1L ? "merged" : "inserted";
        HttpUtil.send(ex, 200, json(Map.of("id", result[0], "action", action)));
    }

    /**
     * GET /v1/memory/get?project=&title= or ?id=
     * Response 200: {entry} or 404 {"error":"not found"}
     */
    private void handleGet(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());

        Optional<MemoryRecord> row;
        if (params.containsKey("id")) {
            long id = parseLong(params.get("id"), "id");
            row = repo.findById(tenant, id);
        } else {
            String project = requireParam(params, "project");
            String title   = requireParam(params, "title");
            row = repo.findByTitle(tenant, project, title);
        }

        if (row.isEmpty()) {
            HttpUtil.send(ex, 404, "{\"error\":\"not found\"}");
        } else {
            HttpUtil.send(ex, 200, json(recordToMap(row.get())));
        }
    }

    /**
     * GET /v1/memory/resolve?project=&title=
     * Response 200: {"entry": {...} | null, "candidates": [...]}
     */
    private void handleResolve(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String project = requireParam(params, "project");
        String title   = requireParam(params, "title");

        var result = repo.resolveTitle(tenant, project, title);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("entry", result.entry() != null ? recordToMap(result.entry()) : null);
        resp.put("candidates", result.candidates().stream().map(this::recordToMap).toList());
        HttpUtil.send(ex, 200, json(resp));
    }

    /**
     * POST /v1/memory/search
     * Request: {"query","project"(opt),"access"(opt,"track"|"silent")}
     * Response 200: [entries]
     *
     * <p>The {@code access} field mirrors Python's {@code search(access="track"|"silent")}:
     * {@code "track"} (default) increments access_count on returned rows;
     * {@code "silent"} skips it (for internal consolidation scans).
     */
    private void handleSearch(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String query   = requireString(body, "query");
        String project = optStringOrNull(body, "project");
        String access  = optStringOrNull(body, "access");
        boolean trackAccess = !"silent".equals(access);

        var rows = repo.search(tenant, query, project, trackAccess);
        HttpUtil.send(ex, 200, json(rows.stream().map(this::recordToMap).toList()));
    }

    /**
     * GET /v1/memory/list?project=&agent=  (both optional)
     * Response 200: [summary entries]
     */
    private void handleList(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String project = params.get("project");
        String agent   = params.get("agent");

        var rows = repo.listEntries(tenant, project, agent);
        // Mirror Python list_entries: summary view (id, project, title, agent, timestamp)
        var summaries = rows.stream().map(r -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", r.getId());
            m.put("project", r.getProject());
            m.put("title", r.getTitle());
            m.put("agent", r.getAgent());
            // Use same UTC second-precision format as recordToMap
            m.put("timestamp", r.getTimestamp() != null
                ? MemoryRepository.UTC_SECOND.format(r.getTimestamp().withOffsetSameInstant(ZoneOffset.UTC))
                : null);
            return m;
        }).toList();
        HttpUtil.send(ex, 200, json(summaries));
    }

    /**
     * GET /v1/memory/projects?prefix=
     * Response 200: [{"project":"...","last_updated":"..."}]
     */
    private void handleProjects(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String prefix = params.getOrDefault("prefix", "");

        var rows = repo.getProjectsWithPrefix(tenant, prefix);
        var result = rows.stream().map(pair -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("project", pair[0] != null ? pair[0] : "");
            // pair[1] is already formatted by getProjectsWithPrefix via UTC_SECOND
            m.put("last_updated", pair[1] != null ? pair[1] : "");
            return m;
        }).toList();
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/memory/search_glob
     * Request: {"query","project_glob"}
     * Response 200: [entries]
     */
    private void handleSearchGlob(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String query       = requireString(body, "query");
        String projectGlob = requireString(body, "project_glob");

        var rows = repo.searchGlob(tenant, query, projectGlob);
        HttpUtil.send(ex, 200, json(rows.stream().map(this::recordToMap).toList()));
    }

    /**
     * POST /v1/memory/search_by_tag
     * Request: {"query","tag"}
     * Response 200: [entries]
     */
    private void handleSearchByTag(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String query = requireString(body, "query");
        String tag   = requireString(body, "tag");

        var rows = repo.searchByTag(tenant, query, tag);
        HttpUtil.send(ex, 200, json(rows.stream().map(this::recordToMap).toList()));
    }

    /**
     * GET /v1/memory/all?project=
     * Response 200: [full entries]
     */
    private void handleAll(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String project = requireParam(params, "project");

        var rows = repo.getAll(tenant, project);
        HttpUtil.send(ex, 200, json(rows.stream().map(this::recordToMap).toList()));
    }

    /**
     * DELETE /v1/memory/delete?project=&title= or ?id=
     * Response 200: {"deleted": true|false}
     */
    private void handleDelete(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "DELETE");
        Map<String, String> params = queryParams(ex.getRequestURI());

        boolean deleted;
        if (params.containsKey("id")) {
            long id = parseLong(params.get("id"), "id");
            deleted = repo.deleteById(tenant, id);
        } else {
            String project = requireParam(params, "project");
            String title   = requireParam(params, "title");
            deleted = repo.delete(tenant, project, title);
        }
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    /**
     * POST /v1/memory/expire  (body can be empty {})
     * Response 200: {"deleted_ids": [<long>, ...]}
     */
    private void handleExpire(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var deletedIds = repo.expire(tenant);
        HttpUtil.send(ex, 200, json(Map.of("deleted_ids", deletedIds)));
    }

    /**
     * POST /v1/memory/merge
     * Request: {"keep_id": <long>, "delete_ids": [<long>,...], "merged_content": "..."}
     * Response 204 (no content) on success; 409 if keepId not found; 400 if keepId in deleteIds.
     */
    private void handleMerge(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        long keepId = requireLong(body, "keep_id");
        List<Long> deleteIds = requireLongList(body, "delete_ids");
        String mergedContent = requireString(body, "merged_content");

        repo.mergeMemories(tenant, keepId, deleteIds, mergedContent);
        // 204 No Content
        ex.sendResponseHeaders(204, -1);
        ex.getResponseBody().close();
    }

    /**
     * GET /v1/memory/flag_stale?project=&idle_days=
     * Response 200: [entries]
     */
    private void handleFlagStale(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "GET");
        Map<String, String> params = queryParams(ex.getRequestURI());
        String project  = requireParam(params, "project");
        int idleDays = 30;
        if (params.containsKey("idle_days")) {
            idleDays = parseInt(params.get("idle_days"), "idle_days");
        }

        var rows = repo.flagStaleMemories(tenant, project, idleDays);
        HttpUtil.send(ex, 200, json(rows.stream().map(this::recordToMap).toList()));
    }

    /**
     * POST /v1/memory/import
     *
     * <p>Fidelity-preserving ETL import endpoint (bead nexus-gmiaf.8, RDR-152 P1.8).
     * Accepts the source row's {@code timestamp}, {@code access_count}, and
     * {@code last_accessed} and writes them verbatim via
     * {@link MemoryRepository#importRow}, so migration does NOT reset these to
     * {@code now()} / {@code 0} / {@code null} as the normal {@code /put} path does.
     *
     * <p>ON CONFLICT (tenant_id, project, title) DO UPDATE propagates EXCLUDED.*
     * for ALL fields, so re-running the ETL is idempotent and content changes in
     * the source are applied on the next run.
     *
     * <p>Fields:
     * <pre>
     *   project      String (required)
     *   title        String (required)
     *   content      String (required)
     *   timestamp    String ISO-8601 UTC (required, e.g. "2026-05-15T08:30:00Z")
     *   tags         String (optional, default "")
     *   session      String (optional)
     *   agent        String (optional)
     *   ttl          Integer (optional)
     *   access_count Integer (optional, default 0)
     *   last_accessed String ISO-8601 UTC or null/absent (optional, null means never accessed)
     * </pre>
     *
     * <p>Response 200: {"id": <long>}
     */
    private void handleImport(HttpExchange ex, String tenant, String method) throws IOException {
        requireMethod(ex, method, "POST");
        Map<String, Object> body = readBody(ex);
        String project  = requireString(body, "project");
        String title    = requireString(body, "title");
        String content  = requireString(body, "content");
        String tags     = optStringOrEmpty(body, "tags");
        String session  = optStringOrNull(body, "session");
        String agent    = optStringOrNull(body, "agent");
        Integer ttl     = optInt(body, "ttl");

        // Required fidelity fields
        String tsRaw = requireString(body, "timestamp");
        OffsetDateTime timestamp;
        try {
            timestamp = OffsetDateTime.parse(tsRaw).withOffsetSameInstant(ZoneOffset.UTC);
        } catch (Exception e) {
            throw new IllegalArgumentException("field 'timestamp' must be ISO-8601 UTC, got: " + tsRaw);
        }

        int accessCount = 0;
        Object acVal = body.get("access_count");
        if (acVal instanceof Number n) {
            accessCount = n.intValue();
        } else if (acVal != null) {
            try { accessCount = Integer.parseInt(acVal.toString()); }
            catch (NumberFormatException e) {
                throw new IllegalArgumentException("field 'access_count' must be an integer");
            }
        }

        // last_accessed: null/absent -> NULL (was '' in SQLite); non-null -> parse as ISO-8601
        OffsetDateTime lastAccessed = null;
        String laRaw = optStringOrNull(body, "last_accessed");
        if (laRaw != null && !laRaw.isBlank()) {
            try {
                lastAccessed = OffsetDateTime.parse(laRaw).withOffsetSameInstant(ZoneOffset.UTC);
            } catch (Exception e) {
                throw new IllegalArgumentException("field 'last_accessed' must be ISO-8601 UTC or null, got: " + laRaw);
            }
        }

        long id = repo.importRow(tenant, project, title, content, tags, session, agent, ttl,
                                 timestamp, accessCount, lastAccessed);
        HttpUtil.send(ex, 200, json(Map.of("id", id)));
    }

    // ── Serialization helpers ─────────────────────────────────────────────────

    /**
     * Convert a MemoryRecord to a Map for JSON serialization.
     *
     * <p>Invariants matching Python MemoryStore (dict(zip(_COLUMNS, row))):
     * <ul>
     *   <li>{@code tags} is ALWAYS a non-null string (empty string when the DB column
     *       is NULL). Python callers do {@code entry["tags"]} without a default —
     *       a missing key would cause KeyError.</li>
     *   <li>{@code timestamp} and {@code last_accessed} are emitted in
     *       UTC second-precision format {@code yyyy-MM-dd'T'HH:mm:ss'Z'} to match
     *       Python's {@code datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}.
     *       String compares and the .9 parity harness rely on this format being
     *       identical on both sides of the seam.</li>
     *   <li>{@code last_accessed} is serialized as an empty string (not null/omitted)
     *       when the column is NULL, matching SQLite MemoryStore's
     *       {@code last_accessed TEXT DEFAULT ''} column default.</li>
     * </ul>
     */
    private Map<String, Object> recordToMap(MemoryRecord r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", r.getId());
        m.put("project", r.getProject());
        m.put("title", r.getTitle());
        m.put("session", r.getSession());
        m.put("agent", r.getAgent());
        m.put("content", r.getContent());
        // tags MUST be present as "" when the DB column is NULL (Critical #2)
        m.put("tags", r.getTags() != null ? r.getTags() : "");
        // Timestamp: UTC second-precision format matching Python strftime("%Y-%m-%dT%H:%M:%SZ")
        m.put("timestamp", r.getTimestamp() != null
            ? MemoryRepository.UTC_SECOND.format(r.getTimestamp().withOffsetSameInstant(ZoneOffset.UTC))
            : null);
        m.put("ttl", r.getTtl());
        m.put("access_count", r.getAccessCount());
        // last_accessed: empty string when null (SQLite default is '' not NULL)
        m.put("last_accessed", r.getLastAccessed() != null
            ? MemoryRepository.UTC_SECOND.format(r.getLastAccessed().withOffsetSameInstant(ZoneOffset.UTC))
            : "");
        return m;
    }

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
            if (bytes.length == 0) {
                return Map.of();
            }
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

    /**
     * Returns the string value for {@code key}, or {@code null} if absent/blank.
     * Used for optional nullable fields (agent, session, project filter).
     */
    private String optStringOrNull(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        String s = val.toString();
        return s.isBlank() ? null : s;
    }

    /**
     * Returns the string value for {@code key}, or {@code ""} if absent/null/blank.
     * Used for {@code tags}: Python MemoryStore always stores tags as "" (never null)
     * and callers do {@code entry["tags"]} without a default.
     */
    private String optStringOrEmpty(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return "";
        String s = val.toString();
        return s;  // preserve empty string as-is; caller decides if blank is meaningful
    }

    /** @deprecated use {@link #optStringOrNull} or {@link #optStringOrEmpty} */
    @Deprecated
    private String optString(Map<String, Object> body, String key) {
        return optStringOrNull(body, key);
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

    private double optDouble(Map<String, Object> body, String key, double defaultValue) {
        Object val = body.get(key);
        if (val == null) return defaultValue;
        if (val instanceof Number n) return n.doubleValue();
        try { return Double.parseDouble(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be a number");
        }
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

    @SuppressWarnings("unchecked")
    private List<Long> requireLongList(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) throw new IllegalArgumentException("missing required field: " + key);
        if (val instanceof List<?> list) {
            List<Long> result = new ArrayList<>();
            for (Object item : list) {
                if (item instanceof Number n) result.add(n.longValue());
                else {
                    try { result.add(Long.parseLong(item.toString())); }
                    catch (NumberFormatException e) {
                        throw new IllegalArgumentException("field '" + key + "' must be a list of longs");
                    }
                }
            }
            return result;
        }
        throw new IllegalArgumentException("field '" + key + "' must be an array");
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

    private static int parseInt(String value, String name) {
        try { return Integer.parseInt(value); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("query param '" + name + "' must be an integer");
        }
    }

    /**
     * Sentinel exception: method check already sent the 405 response; caller must return.
     */
    private static final class SkipHandlerException extends RuntimeException {
        SkipHandlerException() { super(null, null, true, false); }
    }
}
