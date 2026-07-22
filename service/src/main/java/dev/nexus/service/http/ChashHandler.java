package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.Chash;
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
 * RDR-187 (bead nexus-piwya.3) — Chash HTTP endpoints, served from the chunks
 * tables. The HTTP SHAPE is unchanged from the router era (the RDR-187
 * compatibility contract); the {@code chash_index} router behind it is
 * retired.
 *
 * <p>Routes (all under {@code /v1/chash/}):
 * <pre>
 *   POST   /v1/chash/upsert               DEPRECATED no-op (chunks ingest is the write path)
 *   POST   /v1/chash/upsert_many          DEPRECATED no-op
 *   GET    /v1/chash/lookup               lookup(chash=) -> [{collection, created_at}] over chunks
 *   POST   /v1/chash/delete_collection    DEPRECATED no-op (vector/catalog delete owns content)
 *   GET    /v1/chash/distinct_collections   distinct chunk-bearing collection names
 *   POST   /v1/chash/rename_collection    REAL: re-homes chunks_<dim>.collection (Q3; idempotent
 *                                         when the RDR-164 catalog cascade already re-homed)
 *   POST   /v1/chash/delete_stale         DEPRECATED no-op (no derived copy, nothing to heal)
 *   GET    /v1/chash/is_empty             true when no chunk rows exist
 *   GET    /v1/chash/count_for_collection  chunk-row count for collection=
 *   POST   /v1/chash/import               DEPRECATED no-op (returns imported:0 — honest)
 *   GET    /v1/chash/registered_chashes   set of hex chashes for collection= (audit, over chunks)
 * </pre>
 *
 * <p>Deprecated write endpoints VALIDATE their inputs exactly as before (a
 * malformed request still 400s — client bugs stay visible), perform no
 * database work, and add {@code "deprecated":true} to the old response shape.
 * The no-op window spans one release for the mixed-version guard (RDR-187
 * finding 3); the release after nexus-piwya.9 flips them to 410
 * (nexus-piwya.11, deferred).
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
            .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final ChashRepository repo;

    public ChashHandler(ChashRepository repo) {
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
            // Shared typed-DB-error ladder: pool-exhaustion 503 + class-23 409
            // (nexus-h8rf6.2 / nexus-7e057) — see HttpUtil.sendTypedDbError.
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "chash_handler",
                    "op=" + op + " tenant=" + tenant)) {
                log.error("event=chash_handler_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ── POST /v1/chash/upsert (DEPRECATED no-op) ──────────────────────────────

    private void handleUpsert(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        // RDR-180 (nexus-jxizy.7/.8): ONE strict tier — the boundary parses
        // through the Chash type (64 lowercase hex), yielding a uniform 400
        // with the offending length BEFORE any transaction. Validation stays
        // through the deprecation window so client bugs remain visible.
        parseChash((String) body.get("chash"), "'chash'");
        requireCollection((String) body.get("collection"), "'collection'");
        // RDR-187: the router is retired; chunk ingest IS the registration.
        logDeprecatedWrite("upsert", tenant);
        HttpUtil.send(exchange, 200, "{\"ok\":true,\"deprecated\":true}");
    }

    // ── POST /v1/chash/upsert_many (DEPRECATED no-op) ─────────────────────────

    private void handleUpsertMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawChashes = body.get("chashes");
        requireCollection((String) body.get("collection"), "'collection'");
        if (!(rawChashes instanceof List)) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'chashes' must be a JSON array\"}");
            return;
        }
        // nexus-e0hd2: strict, not silent-drop — validation stays through the
        // deprecation window so a malformed batch still fails loud at the
        // boundary instead of silently "succeeding".
        List<?> rawList = (List<?>) rawChashes;
        int count = 0;
        for (int i = 0; i < rawList.size(); i++) {
            Object item = rawList.get(i);
            if (!(item instanceof String s)) {
                throw new IllegalArgumentException(
                    "chashes[" + i + "]: must be a string, got "
                    + (item == null ? "null" : item.getClass().getSimpleName()));
            }
            parseChash(s, "chashes[" + i + "]");
            count++;
        }
        // RDR-187: the router is retired; chunk ingest IS the registration.
        // The count field keeps its historical meaning: chashes ACCEPTED
        // (parsed), which was never a rows-persisted count.
        logDeprecatedWrite("upsert_many", tenant);
        HttpUtil.send(exchange, 200, "{\"ok\":true,\"count\":" + count + ",\"deprecated\":true}");
    }

    /**
     * Parse an incoming chash through the type — the sole enforcement point
     * (RDR-180 one-strict-tier). 64 lowercase hex or a labeled 400; a 32-hex
     * legacy reference gets the self-diagnosing alias-resolution hint from
     * {@link Chash#fromHex}.
     */
    private static Chash parseChash(String value, String label) {
        try {
            return Chash.fromHex(value);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(label + ": " + e.getMessage());
        }
    }

    // ── GET /v1/chash/lookup?chash=<hex> ─────────────────────────────────────

    private void handleLookup(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String raw = queryParam(exchange, "chash");
        if (raw == null || raw.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"chash query param required\"}");
            return;
        }
        // RDR-180 Item3 read seam: the canonical 64-hex parses through the
        // type; anything else is treated as a LEGACY REFERENCE (pre-flip
        // 32-hex chunk id, ETL-era external id) and resolved through the
        // permanent chash_alias map. An unmapped legacy ref answers empty
        // rows — same contract as an unknown canonical chash (the alias map
        // is the collision-free resolver; a miss is dangling, not an error).
        Chash chash;
        if (raw.length() == Chash.HEX_LENGTH) {
            chash = parseChash(raw, "'chash'");
        } else {
            chash = repo.resolveLegacyRef(tenant, raw);
            if (chash == null) {
                HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
                    Map.of("rows", List.of(), "legacy_ref_unresolved", true)));
                return;
            }
        }
        var rows = repo.lookup(tenant, chash);
        // The canonical 64-hex is echoed so a LEGACY-ref caller learns the
        // resolved identity (the client citation resolver then fetches the
        // chunk by ITS canonical hash — RDR-180 Failure Modes: resolvers
        // accept 32-hex via alias lookup and 64-hex directly).
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("rows", rows, "chash", chash.toHex())));
    }

    // ── POST /v1/chash/delete_collection (DEPRECATED no-op) ───────────────────

    private void handleDeleteCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String collection = (String) body.get("collection");
        if (collection == null || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'collection' required\"}");
            return;
        }
        // RDR-187: content deletion is the vector/catalog API's job; there is
        // no router copy left to drop. Rerouting this to DELETE chunk rows
        // would silently escalate "drop routing rows" into "drop content" —
        // deliberately not done. deleted:0 matches what callers already see
        // today after the RDR-164 cascade has run.
        logDeprecatedWrite("delete_collection", tenant);
        HttpUtil.send(exchange, 200, "{\"deleted\":0,\"deprecated\":true}");
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

    // ── POST /v1/chash/delete_stale (DEPRECATED no-op) ────────────────────────

    private void handleDeleteStale(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String rawChash   = (String) body.get("chash");
        String collection = (String) body.get("collection");
        if (rawChash == null || collection == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'chash' and 'collection' required\"}");
            return;
        }
        parseChash(rawChash, "'chash'");
        // RDR-187: delete_stale was the client-side self-heal for router rows
        // that had drifted from the chunk store. With no derived copy there
        // is nothing to heal; the lookup is chunk-backed truth already.
        logDeprecatedWrite("delete_stale", tenant);
        HttpUtil.send(exchange, 200, "{\"deleted\":0,\"deprecated\":true}");
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

    // ── POST /v1/chash/import (DEPRECATED no-op) ──────────────────────────────

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
        // Validation stays (same rules as the router era, incl. the RDR-180
        // 64-hex requirement and input-position error indexing) so malformed
        // batches still 400 during the window.
        List<?> rawList = (List<?>) rawRows;
        for (int rowIdx = 0; rowIdx < rawList.size(); rowIdx++) {
            Object item = rawList.get(rowIdx);
            if (!(item instanceof Map)) continue;
            Map<String, Object> row = (Map<String, Object>) item;
            String rawChash   = (String) row.get("chash");
            String collection = (String) row.get("collection");
            if (rawChash == null || rawChash.isBlank() || collection == null || collection.isBlank()) continue;
            parseChash(rawChash, "rows[" + rowIdx + "].chash");
        }
        // RDR-187: the router is retired, so the legacy --cold ETL leg has no
        // destination. imported:0 is HONEST — nothing was persisted; an old
        // client's verify-fill records filled=0 and reports visible
        // divergence rather than a fabricated success (the paired client
        // release skips this leg entirely, nexus-piwya.10).
        logDeprecatedWrite("import", tenant);
        HttpUtil.send(exchange, 200, "{\"imported\":0,\"deprecated\":true}");
    }

    // ── GET /v1/chash/registered_chashes?collection=<name> ───────────────────

    /**
     * Return the set of chashes present in {@code collection}, hex-encoded.
     *
     * <p>RDR-187: values come straight from {@code chunks_<dim>.chash} —
     * uniformly full 64-hex digests (RDR-180 natural chunk IDs; the
     * production rekey is complete, and the router-era "shorter legacy hex"
     * caveat died with the router).
     *
     * <p>Response: {@code {"chashes": ["<hex>", ...]}}
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

    /** Shared required-collection validation for the deprecated write shapes. */
    private static void requireCollection(String collection, String label) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException(label + " must not be empty");
        }
    }

    /**
     * One structured line per deprecated-write call, debug level: old clients
     * fire upsert_many on every index batch during the mixed-version window,
     * so info would be log spam; debug keeps the forensic trail available.
     */
    private static void logDeprecatedWrite(String op, String tenant) {
        log.debug("event=chash_write_deprecated op={} tenant={} rdr=187", op, tenant);
    }

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
