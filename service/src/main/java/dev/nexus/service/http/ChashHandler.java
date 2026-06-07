package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.ChashRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-152 bead nexus-gmiaf.16 — Chash-index HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/chash/}):
 * <pre>
 *   POST   /v1/chash/upsert               register (chash, collection) row
 *   POST   /v1/chash/upsert_many          batch register (chashes[], collection)
 *   GET    /v1/chash/lookup               lookup(chash=) -> [{collection, created_at}]
 *   POST   /v1/chash/delete_collection    delete all rows for collection
 *   GET    /v1/chash/distinct_collections   all distinct collection names
 *   POST   /v1/chash/rename_collection    rename old -> new
 *   POST   /v1/chash/delete_stale         delete (chash, collection) PK row
 *   GET    /v1/chash/is_empty             true when no rows exist
 *   GET    /v1/chash/count_for_collection  count rows for collection=
 *   POST   /v1/chash/import               fidelity-preserving ETL import
 *   GET    /v1/chash/registered_chashes   set of chash[:32] for collection= (audit)
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant} header.
 *
 * <p>All request/response bodies are JSON. Errors return
 * {@code {"error":"<message>"}} with appropriate HTTP status.
 */
public final class ChashHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(ChashHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final ChashRepository repo;

    public ChashHandler(ChashRepository repo) {
        this.repo = repo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = (String) exchange.getAttribute(AuthFilter.ATTR_TENANT);
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }

        String path   = exchange.getRequestURI().getPath();
        String op     = path.replaceFirst("^/v1/chash", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/upsert"               -> handleUpsert(exchange, tenant, method);
                case "/upsert_many"          -> handleUpsertMany(exchange, tenant, method);
                case "/lookup"               -> handleLookup(exchange, tenant, method);
                case "/delete_collection"    -> handleDeleteCollection(exchange, tenant, method);
                case "/distinct_collections" -> handleDistinctCollections(exchange, tenant, method);
                case "/rename_collection"    -> handleRenameCollection(exchange, tenant, method);
                case "/delete_stale"         -> handleDeleteStale(exchange, tenant, method);
                case "/is_empty"             -> handleIsEmpty(exchange, tenant, method);
                case "/count_for_collection"   -> handleCountForCollection(exchange, tenant, method);
                case "/import"                 -> handleImport(exchange, tenant, method);
                case "/registered_chashes"     -> handleRegisteredChashes(exchange, tenant, method);
                default                        -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            log.error("event=chash_handler_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
            HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
        }
    }

    // ── POST /v1/chash/upsert ─────────────────────────────────────────────────

    private void handleUpsert(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String chash      = (String) body.get("chash");
        String collection = (String) body.get("collection");
        repo.upsert(tenant, chash, collection);
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
    }

    // ── POST /v1/chash/upsert_many ────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleUpsertMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawChashes = body.get("chashes");
        String collection = (String) body.get("collection");
        if (!(rawChashes instanceof List)) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'chashes' must be a JSON array\"}");
            return;
        }
        List<String> chashes = new ArrayList<>();
        for (Object item : (List<?>) rawChashes) {
            if (item instanceof String s) chashes.add(s);
        }
        repo.upsertMany(tenant, chashes, collection);
        HttpUtil.send(exchange, 200, "{\"ok\":true,\"count\":" + chashes.size() + "}");
    }

    // ── GET /v1/chash/lookup?chash=<hex> ─────────────────────────────────────

    private void handleLookup(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String chash = queryParam(exchange, "chash");
        if (chash == null || chash.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"chash query param required\"}");
            return;
        }
        var rows = repo.lookup(tenant, chash);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("rows", rows)));
    }

    // ── POST /v1/chash/delete_collection ─────────────────────────────────────

    private void handleDeleteCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String collection = (String) body.get("collection");
        if (collection == null || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'collection' required\"}");
            return;
        }
        int deleted = repo.deleteCollection(tenant, collection);
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    // ── GET /v1/chash/distinct_collections ───────────────────────────────────

    private void handleDistinctCollections(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var collections = repo.distinctCollections(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("collections", new ArrayList<>(collections))));
    }

    // ── POST /v1/chash/rename_collection ─────────────────────────────────────

    private void handleRenameCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String oldColl = (String) body.get("old");
        String newColl = (String) body.get("new");
        if (oldColl == null || oldColl.isBlank() || newColl == null || newColl.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'old' and 'new' fields required\"}");
            return;
        }
        int updated = repo.renameCollection(tenant, oldColl, newColl);
        HttpUtil.send(exchange, 200, "{\"updated\":" + updated + "}");
    }

    // ── POST /v1/chash/delete_stale ───────────────────────────────────────────

    private void handleDeleteStale(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String chash      = (String) body.get("chash");
        String collection = (String) body.get("collection");
        if (chash == null || collection == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'chash' and 'collection' required\"}");
            return;
        }
        int deleted = repo.deleteStale(tenant, chash, collection);
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    // ── GET /v1/chash/is_empty ────────────────────────────────────────────────

    private void handleIsEmpty(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        boolean empty = repo.isEmpty(tenant);
        HttpUtil.send(exchange, 200, "{\"empty\":" + empty + "}");
    }

    // ── GET /v1/chash/count_for_collection?collection=<name> ─────────────────

    private void handleCountForCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String collection = queryParam(exchange, "collection");
        if (collection == null || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"collection query param required\"}");
            return;
        }
        int count = repo.countForCollection(tenant, collection);
        HttpUtil.send(exchange, 200, "{\"count\":" + count + "}");
    }

    // ── POST /v1/chash/import ─────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleImport(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);

        // Batch import: expects {"rows": [{"chash":..., "collection":..., "created_at":...}, ...]}
        Object rawRows = body.get("rows");
        if (!(rawRows instanceof List)) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'rows' must be a JSON array\"}");
            return;
        }
        int imported = 0;
        for (Object item : (List<?>) rawRows) {
            if (!(item instanceof Map)) continue;
            Map<String, Object> row = (Map<String, Object>) item;
            String chash      = (String) row.get("chash");
            String collection = (String) row.get("collection");
            String createdAt  = (String) row.get("created_at");
            if (chash == null || chash.isBlank() || collection == null || collection.isBlank()) continue;
            if (createdAt == null || createdAt.isBlank()) createdAt = "1970-01-01T00:00:00Z";
            repo.doImport(tenant, chash, collection, createdAt);
            imported++;
        }
        HttpUtil.send(exchange, 200, "{\"imported\":" + imported + "}");
    }

    // ── GET /v1/chash/registered_chashes?collection=<name> ───────────────────

    /**
     * Return the set of {@code chash[:32]} values registered for {@code collection}.
     *
     * <p>Mirrors {@code ChashIndex.registered_chashes_for_collection}: the 32-char
     * prefix matches Chroma natural-ID shape (RDR-108 §D1) and is used by the
     * collection-audit coverage probe to produce the set-difference against T3
     * chunk IDs.
     *
     * <p>Response: {@code {"chashes": ["<hex32>", ...]}}
     */
    private void handleRegisteredChashes(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String collection = queryParam(exchange, "collection");
        if (collection == null || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"collection query param required\"}");
            return;
        }
        var chashes = repo.registeredChashesForCollection(tenant, collection);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("chashes", new ArrayList<>(chashes))));
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) return Map.of();
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    private String queryParam(HttpExchange exchange, String key) {
        String query = exchange.getRequestURI().getRawQuery();
        if (query == null) return null;
        for (String part : query.split("&")) {
            String[] kv = part.split("=", 2);
            if (kv.length == 2 && kv[0].equals(key)) {
                return java.net.URLDecoder.decode(kv[1], java.nio.charset.StandardCharsets.UTF_8);
            }
        }
        return null;
    }
}
