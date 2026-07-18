package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.RemapRepository;
import dev.nexus.service.db.RemapRepository.RemapEntry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-186 bead nexus-146xx.4 — chash_remap HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/remap/}):
 * <pre>
 *   POST /v1/remap/record_batch   persist a batch of old-id → new-chash facts
 *                                 {source_collection, entries:[{old_id, new_chash,
 *                                  target_collection, provenance}]} → {recorded}
 *   POST /v1/remap/clear_leg      the rollback absence-encoding (D2): clear ONE
 *                                 leg's map rows {source_collection,
 *                                  target_collection} → {deleted}. BOTH fields
 *                                 required — a leg is the (source, target) pair
 *                                 (co-residency: a wide clear would delete a
 *                                 sibling leg's claims)
 *   GET  /v1/remap/membership     live leg-convergence counts (bead .5 function):
 *                                 ?source_collection=&amp;target_collection=
 *                                 → {mapped_total, present_count}
 * </pre>
 *
 * <p>RF-186-1: raw facts and live counts only — no verdict surface exists and
 * none may be added. The membership response is a pair of counts the CLIENT
 * rung interprets (converged iff equal, including 0 == 0), computed fresh by
 * {@code nexus.remap_membership()} on every call.
 *
 * <p>Batch bound: {@link RemapRepository#MAX_BATCH} (300) entries per
 * record_batch call — the chroma_quotas MAX_RECORDS_PER_WRITE heritage cap;
 * oversized batches get 400, matching the client's existing paging contract.
 *
 * <p>new_chash normalization mirrors {@code ChashHandler}: a 64-char
 * chunk_text_hash form is normalized to its [:32] prefix (the ecosystem id
 * convention, RDR-108 D1); any other non-32 length is rejected 400.
 *
 * <p>All endpoints require {@code Authorization: Bearer} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant}.
 */
public final class RemapHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(RemapHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final RemapRepository repo;

    public RemapHandler(RemapRepository repo) {
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
        String op     = path.replaceFirst("^/v1/remap", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/record_batch" -> handleRecordBatch(exchange, tenant, method);
                case "/clear_leg"    -> handleClearLeg(exchange, tenant, method);
                case "/membership"   -> handleMembership(exchange, tenant, method);
                default              -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "remap_handler",
                    "op=" + op + " tenant=" + tenant)) {
                log.error("event=remap_handler_error op={} tenant={} error={}",
                        op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ── POST /v1/remap/record_batch ──────────────────────────────────────────

    private void handleRecordBatch(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String sourceCollection = requireString(body, "source_collection");

        Object rawEntries = body.get("entries");
        if (!(rawEntries instanceof List<?> list) || list.isEmpty()) {
            throw new IllegalArgumentException("'entries' must be a non-empty array");
        }
        if (list.size() > RemapRepository.MAX_BATCH) {
            throw new IllegalArgumentException(
                "batch too large: " + list.size() + " entries (max " + RemapRepository.MAX_BATCH
                + " — page the batch)");
        }

        List<RemapEntry> entries = new ArrayList<>(list.size());
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> m)) {
                throw new IllegalArgumentException("each entry must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> entry = (Map<String, Object>) m;
            entries.add(new RemapEntry(
                    sourceCollection,
                    requireString(entry, "old_id"),
                    normalizeChash((String) entry.get("new_chash")),
                    requireString(entry, "target_collection"),
                    requireString(entry, "provenance")));
        }

        int recorded = repo.recordBatch(tenant, entries);
        HttpUtil.send(exchange, 200, "{\"recorded\":" + recorded + "}");
    }

    // ── POST /v1/remap/clear_leg ─────────────────────────────────────────────

    private void handleClearLeg(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String sourceCollection = requireString(body, "source_collection");
        // REQUIRED: a leg is the (source, target) PAIR — a wide whole-source
        // clear would delete a co-resident sibling leg's claims (critic
        // finding; RDR-185 .13 r2/C2 is why target_collection exists).
        String targetCollection = requireString(body, "target_collection");

        int deleted = repo.clearLeg(tenant, sourceCollection, targetCollection);
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    // ── GET /v1/remap/membership ─────────────────────────────────────────────

    private void handleMembership(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String sourceCollection = queryParam(exchange, "source_collection");
        String targetCollection = queryParam(exchange, "target_collection");
        if (sourceCollection == null || sourceCollection.isBlank()
                || targetCollection == null || targetCollection.isBlank()) {
            throw new IllegalArgumentException(
                "'source_collection' and 'target_collection' query params are required");
        }

        long[] m = repo.membership(tenant, sourceCollection, targetCollection);
        HttpUtil.send(exchange, 200,
                "{\"mapped_total\":" + m[0] + ",\"present_count\":" + m[1] + "}");
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    /**
     * Normalize a chash to its 32-char form (mirrors ChashHandler): the 64-char
     * chunk_text_hash form is truncated to [:32] (RDR-108 D1 — new_chash ==
     * chunk_text_hash[:32] by construction); everything else goes through
     * {@link dev.nexus.service.db.Chash#requireLength32} — the sole enforcement
     * point for the 32-char boundary check.
     */
    private static String normalizeChash(String chash) {
        if (chash == null || chash.isBlank()) {
            throw new IllegalArgumentException("'new_chash' is required");
        }
        if (chash.length() == 64) return chash.substring(0, 32);
        return dev.nexus.service.db.Chash.requireLength32(chash, "'new_chash'");
    }

    private static String requireString(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("'" + field + "' is required");
        }
        return s;
    }

    private Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) throw new IllegalArgumentException("request body is required");
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }

    private static String queryParam(HttpExchange exchange, String key) {
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
