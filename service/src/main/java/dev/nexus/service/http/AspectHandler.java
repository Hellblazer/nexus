package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.AspectRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * RDR-152 bead nexus-gmiaf.15 — Aspects / highlights / queue HTTP endpoints.
 *
 * <p>Routes (all under {@code /v1/aspects/}):
 * <pre>
 *   POST  /v1/aspects/upsert                      upsert aspect record
 *   GET   /v1/aspects/get                         get by collection= &amp; source_path=
 *   GET   /v1/aspects/get_by_doc_id               get by doc_id=
 *   GET   /v1/aspects/list_by_collection          list by collection= (limit=, offset=)
 *   GET   /v1/aspects/list_by_extractor_version   list for re-extraction (extractor=, max_version=)
 *   POST  /v1/aspects/delete                      delete by collection + source_path
 *   POST  /v1/aspects/rename_collection           rename collection denorm
 *   POST  /v1/aspects/salient_sentences/set       set salient_sentences by doc_id
 *   POST  /v1/aspects/salient_sentences/set_by_key set by (collection, source_path)
 *   GET   /v1/aspects/salient_sentences/get       get salient_sentences for doc_id=
 *   POST  /v1/aspects/import                      ETL fidelity import
 *
 *   POST  /v1/aspects/highlights/upsert              upsert highlight record
 *   GET   /v1/aspects/highlights/get               get by doc_id=
 *   GET   /v1/aspects/highlights/get_by_source_uri  get by source_uri=
 *   GET   /v1/aspects/highlights/list              list (limit=, offset=)
 *   POST  /v1/aspects/highlights/delete            delete by doc_id
 *   POST  /v1/aspects/highlights/import            ETL import
 *   POST  /v1/aspects/highlights/rename_collection rename collection denorm

 *   POST  /v1/aspects/queue/enqueue               enqueue document
 *   POST  /v1/aspects/queue/claim_next            atomically claim one pending row
 *   POST  /v1/aspects/queue/claim_batch           claim up to limit= pending rows
 *   POST  /v1/aspects/queue/mark_done             delete row on success
 *   POST  /v1/aspects/queue/mark_failed           mark as failed
 *   POST  /v1/aspects/queue/mark_retry            reset to pending
 *   POST  /v1/aspects/queue/reclaim_stale         reclaim stale in_progress rows
 *   GET   /v1/aspects/queue/pending_count         count pending rows
 *   GET   /v1/aspects/queue/is_drained            check drained
 *   GET   /v1/aspects/queue/list_pending          list pending (limit= optional)
 *   POST  /v1/aspects/queue/rename_collection     rename collection in queue
 *   POST  /v1/aspects/queue/import                ETL import of queue row
 *
 *   POST  /v1/aspects/promotion/record            record promotion event
 *   GET   /v1/aspects/promotion/list              list promotion history
 *   POST  /v1/aspects/promotion/import            ETL import of promotion row
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} and {@code X-Nexus-Tenant}.
 */
public final class AspectHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(AspectHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final AspectRepository repo;

    public AspectHandler(AspectRepository repo) {
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
        String op     = path.replaceFirst("^/v1/aspects", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                // ── document_aspects ──────────────────────────────────────────
                case "/upsert"                          -> handleUpsert(exchange, tenant, method);
                case "/get"                             -> handleGet(exchange, tenant, method);
                case "/get_by_doc_id"                   -> handleGetByDocId(exchange, tenant, method);
                case "/list_by_collection"              -> handleListByCollection(exchange, tenant, method);
                case "/list_by_extractor_version"       -> handleListByExtractorVersion(exchange, tenant, method);
                case "/delete"                          -> handleDeleteAspect(exchange, tenant, method);
                case "/rename_collection"               -> handleRenameCollection(exchange, tenant, method);
                case "/salient_sentences/set"           -> handleSetSalient(exchange, tenant, method);
                case "/salient_sentences/set_by_key"    -> handleSetSalientByKey(exchange, tenant, method);
                case "/salient_sentences/get"           -> handleGetSalient(exchange, tenant, method);
                case "/import"                          -> handleImportAspect(exchange, tenant, method);
                // ── document_highlights ───────────────────────────────────────
                case "/highlights/upsert"               -> handleHighlightUpsert(exchange, tenant, method);
                case "/highlights/get"                  -> handleHighlightGet(exchange, tenant, method);
                case "/highlights/get_by_source_uri"    -> handleHighlightGetByUri(exchange, tenant, method);
                case "/highlights/list"                 -> handleHighlightList(exchange, tenant, method);
                case "/highlights/delete"               -> handleHighlightDelete(exchange, tenant, method);
                case "/highlights/import"               -> handleHighlightImport(exchange, tenant, method);
                case "/highlights/rename_collection"    -> handleHighlightRenameCollection(exchange, tenant, method);
                // ── aspect_extraction_queue ───────────────────────────────────
                case "/queue/enqueue"                   -> handleQueueEnqueue(exchange, tenant, method);
                case "/queue/claim_next"                -> handleQueueClaimNext(exchange, tenant, method);
                case "/queue/claim_batch"               -> handleQueueClaimBatch(exchange, tenant, method);
                case "/queue/mark_done"                 -> handleQueueMarkDone(exchange, tenant, method);
                case "/queue/mark_failed"               -> handleQueueMarkFailed(exchange, tenant, method);
                case "/queue/mark_retry"                -> handleQueueMarkRetry(exchange, tenant, method);
                case "/queue/reclaim_stale"             -> handleQueueReclaimStale(exchange, tenant, method);
                case "/queue/pending_count"             -> handleQueuePendingCount(exchange, tenant, method);
                case "/queue/is_drained"                -> handleQueueIsDrained(exchange, tenant, method);
                case "/queue/list_pending"              -> handleQueueListPending(exchange, tenant, method);
                case "/queue/rename_collection"         -> handleQueueRenameCollection(exchange, tenant, method);
                case "/queue/import"                    -> handleQueueImport(exchange, tenant, method);
                // ── aspect_promotion_log ──────────────────────────────────────
                case "/promotion/record"                -> handlePromotionRecord(exchange, tenant, method);
                case "/promotion/list"                  -> handlePromotionList(exchange, tenant, method);
                case "/promotion/import"                -> handlePromotionImport(exchange, tenant, method);
                default -> HttpUtil.send(exchange, 404,
                    "{\"error\":\"unknown aspects op: " + op + "\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + HttpUtil.jsonString(e.getMessage()) + "}");
        } catch (Exception e) {
            log.error("event=aspects_handler_error op={} error={}", op, e.getMessage(), e);
            HttpUtil.send(exchange, 500, "{\"error\":" + HttpUtil.jsonString(e.getMessage()) + "}");
        }
    }

    // ── document_aspects handlers ──────────────────────────────────────────────

    /**
     * Normalize list and map fields in an aspect body to JSON strings before
     * passing to the repository.
     *
     * <p>Python clients send {@code experimental_datasets}, {@code experimental_baselines},
     * {@code extras}, and {@code salient_sentences} as JSON arrays/objects. Jackson
     * deserializes them as {@code ArrayList}/{@code LinkedHashMap}. The repository
     * stores them as PostgreSQL TEXT columns, so they must be JSON-serialized strings.
     *
     * <p>String values are passed through unchanged (allows both pre-serialized and
     * raw-list callers). Null values are left null.
     */
    private Map<String, Object> serializeAspectBody(Map<String, Object> body) throws IOException {
        java.util.LinkedHashMap<String, Object> out = new java.util.LinkedHashMap<>(body);
        for (String field : List.of("experimental_datasets", "experimental_baselines",
                                     "extras", "salient_sentences")) {
            Object v = out.get(field);
            if (v instanceof List || v instanceof Map) {
                out.put(field, MAPPER.writeValueAsString(v));
            }
        }
        return out;
    }

    private void handleUpsert(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = serializeAspectBody(readBody(ex));
        long id = repo.upsertAspect(tenant, body);
        if (id < 0) {
            HttpUtil.send(ex, 200, "{\"written\":false,\"reason\":\"confidence_below_threshold\"}");
        } else {
            HttpUtil.send(ex, 200, "{\"written\":true,\"id\":" + id + "}");
        }
    }

    private void handleGet(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        Map<String, String> q = parseQuery(ex.getRequestURI());
        String collection = q.get("collection");
        String sourcePath = q.get("source_path");
        if (collection == null || sourcePath == null) {
            HttpUtil.send(ex, 400, "{\"error\":\"collection and source_path required\"}"); return;
        }
        Optional<Map<String, Object>> rec = repo.getAspect(tenant, collection, sourcePath);
        if (rec.isEmpty()) { HttpUtil.send(ex, 404, "{\"found\":false}"); return; }
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(rec.get()));
    }

    private void handleGetByDocId(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        String docId = parseQuery(ex.getRequestURI()).get("doc_id");
        Optional<Map<String, Object>> rec = repo.getAspectByDocId(tenant, docId);
        if (rec.isEmpty()) { HttpUtil.send(ex, 404, "{\"found\":false}"); return; }
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(rec.get()));
    }

    private void handleListByCollection(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        Map<String, String> q = parseQuery(ex.getRequestURI());
        String collection = q.get("collection");
        if (collection == null) { HttpUtil.send(ex, 400, "{\"error\":\"collection required\"}"); return; }
        int limit  = parseIntOrDefault(q.get("limit"),  0);
        int offset = parseIntOrDefault(q.get("offset"), 0);
        List<Map<String, Object>> rows = repo.listByCollection(tenant, collection, limit, offset);
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(rows));
    }

    private void handleListByExtractorVersion(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        Map<String, String> q = parseQuery(ex.getRequestURI());
        String extractor   = q.get("extractor_name");
        String maxVersion  = q.get("max_version");
        if (extractor == null || maxVersion == null) {
            HttpUtil.send(ex, 400, "{\"error\":\"extractor_name and max_version required\"}"); return;
        }
        List<Map<String, Object>> rows = repo.listByExtractorVersion(tenant, extractor, maxVersion);
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(rows));
    }

    private void handleDeleteAspect(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        int n = repo.deleteAspect(tenant, (String) body.get("collection"), (String) body.get("source_path"));
        HttpUtil.send(ex, 200, "{\"deleted\":" + n + "}");
    }

    private void handleRenameCollection(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        int n = repo.renameAspectCollection(tenant, (String) body.get("old"), (String) body.get("new"));
        HttpUtil.send(ex, 200, "{\"updated\":" + n + "}");
    }

    private void handleSetSalient(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        // Accept either "sentences" (list from Python) or "sentences_json" (pre-serialized string)
        String sentencesJson = extractSentencesJson(body);
        int n = repo.setSalientSentences(tenant, (String) body.get("doc_id"), sentencesJson);
        HttpUtil.send(ex, 200, "{\"updated\":" + n + "}");
    }

    private void handleSetSalientByKey(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        String sentencesJson = extractSentencesJson(body);
        int n = repo.setSalientSentencesByKey(tenant,
            (String) body.get("collection"), (String) body.get("source_path"), sentencesJson);
        HttpUtil.send(ex, 200, "{\"updated\":" + n + "}");
    }

    private String extractSentencesJson(Map<String, Object> body) throws IOException {
        Object sentences = body.get("sentences");
        if (sentences instanceof List) {
            return MAPPER.writeValueAsString(sentences);
        }
        Object sentencesJson = body.get("sentences_json");
        if (sentencesJson != null) {
            return sentencesJson.toString();
        }
        return "[]";
    }

    private void handleGetSalient(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        String docId = parseQuery(ex.getRequestURI()).get("doc_id");
        String val = repo.getSalientSentences(tenant, docId);
        if (val == null) {
            HttpUtil.send(ex, 404, "{\"sentences\":[]}");
            return;
        }
        // Parse the stored JSON string into a list and return as {"sentences":[...]}
        // so the Python client's r.get("sentences", []) works correctly.
        Object parsed;
        try {
            parsed = MAPPER.readValue(val, List.class);
        } catch (Exception e) {
            parsed = List.of();
        }
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(Map.of("sentences", parsed)));
    }

    private void handleImportAspect(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = serializeAspectBody(readBody(ex));
        int n = repo.importAspect(tenant, body);
        HttpUtil.send(ex, 200, "{\"imported\":" + n + "}");
    }

    // ── document_highlights handlers ───────────────────────────────────────────

    private void handleHighlightUpsert(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        boolean written = repo.upsertHighlight(tenant, readBody(ex));
        HttpUtil.send(ex, 200, "{\"written\":" + written + "}");
    }

    private void handleHighlightGet(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        String docId = parseQuery(ex.getRequestURI()).get("doc_id");
        Optional<Map<String, Object>> rec = repo.getHighlight(tenant, docId);
        if (rec.isEmpty()) { HttpUtil.send(ex, 404, "{\"found\":false}"); return; }
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(rec.get()));
    }

    private void handleHighlightGetByUri(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        String uri = parseQuery(ex.getRequestURI()).get("source_uri");
        Optional<Map<String, Object>> rec = repo.getHighlightBySourceUri(tenant, uri);
        if (rec.isEmpty()) { HttpUtil.send(ex, 404, "{\"found\":false}"); return; }
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(rec.get()));
    }

    private void handleHighlightList(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        Map<String, String> q = parseQuery(ex.getRequestURI());
        int limit  = parseIntOrDefault(q.get("limit"),  50);
        int offset = parseIntOrDefault(q.get("offset"), 0);
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(repo.listHighlights(tenant, limit, offset)));
    }

    private void handleHighlightDelete(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        boolean deleted = repo.deleteHighlight(tenant, (String) body.get("doc_id"));
        HttpUtil.send(ex, 200, "{\"deleted\":" + deleted + "}");
    }

    private void handleHighlightImport(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        int n = repo.importHighlight(tenant, readBody(ex));
        HttpUtil.send(ex, 200, "{\"imported\":" + n + "}");
    }

    private void handleHighlightRenameCollection(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        String oldColl = (String) body.get("old");
        String newColl = (String) body.get("new");
        if (oldColl == null || newColl == null) {
            HttpUtil.send(ex, 400, "{\"error\":\"old and new required\"}"); return;
        }
        int n = repo.renameHighlightsCollection(tenant, oldColl, newColl);
        HttpUtil.send(ex, 200, "{\"updated\":" + n + "}");
    }

    // ── aspect_extraction_queue handlers ──────────────────────────────────────

    private void handleQueueEnqueue(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        repo.enqueue(tenant, readBody(ex));
        HttpUtil.send(ex, 200, "{\"enqueued\":true}");
    }

    private void handleQueueClaimNext(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Optional<Map<String, Object>> row = repo.claimNext(tenant);
        if (row.isEmpty()) { HttpUtil.send(ex, 200, "{\"claimed\":false}"); return; }
        // Wrap in {"claimed":true,"row":{...}} to match Python protocol in http_aspect_queue.py
        Map<String, Object> envelope = new java.util.LinkedHashMap<>();
        envelope.put("claimed", true);
        envelope.put("row", row.get());
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(envelope));
    }

    private void handleQueueClaimBatch(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        int limit = body.containsKey("limit") ? ((Number) body.get("limit")).intValue() : 1;
        List<Map<String, Object>> rows = repo.claimBatch(tenant, limit);
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(rows));
    }

    private void handleQueueMarkDone(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        int n = repo.markDone(tenant,
            (String) body.get("doc_id"),
            (String) body.get("collection"),
            (String) body.get("source_path"));
        HttpUtil.send(ex, 200, "{\"deleted\":" + n + "}");
    }

    private void handleQueueMarkFailed(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        repo.markFailed(tenant, (String) body.get("collection"), (String) body.get("source_path"),
            (String) body.getOrDefault("error", ""));
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleQueueMarkRetry(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        repo.markRetry(tenant, (String) body.get("collection"), (String) body.get("source_path"));
        HttpUtil.send(ex, 200, "{\"ok\":true}");
    }

    private void handleQueueReclaimStale(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        int timeoutSec = body.containsKey("timeout_seconds")
            ? ((Number) body.get("timeout_seconds")).intValue() : 300;
        int n = repo.reclaimStale(tenant, timeoutSec);
        HttpUtil.send(ex, 200, "{\"reclaimed\":" + n + "}");
    }

    private void handleQueuePendingCount(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        HttpUtil.send(ex, 200, "{\"count\":" + repo.pendingCount(tenant) + "}");
    }

    private void handleQueueIsDrained(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        HttpUtil.send(ex, 200, "{\"drained\":" + repo.isDrained(tenant) + "}");
    }

    private void handleQueueListPending(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        String limitStr = parseQuery(ex.getRequestURI()).get("limit");
        int limit = limitStr != null ? Integer.parseInt(limitStr) : 0;
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(repo.listPending(tenant, limit)));
    }

    private void handleQueueRenameCollection(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        Map<String, Object> body = readBody(ex);
        int n = repo.renameQueueCollection(tenant, (String) body.get("old"), (String) body.get("new"));
        HttpUtil.send(ex, 200, "{\"updated\":" + n + "}");
    }

    private void handleQueueImport(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        int n = repo.importQueueRow(tenant, readBody(ex));
        HttpUtil.send(ex, 200, "{\"imported\":" + n + "}");
    }

    // ── aspect_promotion_log handlers ─────────────────────────────────────────

    private void handlePromotionRecord(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        repo.recordPromotion(tenant, readBody(ex));
        HttpUtil.send(ex, 200, "{\"recorded\":true}");
    }

    private void handlePromotionList(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"GET required\"}"); return; }
        HttpUtil.send(ex, 200, MAPPER.writeValueAsString(repo.listPromotions(tenant)));
    }

    private void handlePromotionImport(HttpExchange ex, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(ex, 405, "{\"error\":\"POST required\"}"); return; }
        int n = repo.importPromotionRow(tenant, readBody(ex));
        HttpUtil.send(ex, 200, "{\"imported\":" + n + "}");
    }

    // ── Shared helpers ─────────────────────────────────────────────────────────

    private Map<String, Object> readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody()) {
            return MAPPER.readValue(is, MAP_TYPE);
        }
    }

    private static Map<String, String> parseQuery(java.net.URI uri) {
        Map<String, String> out = new java.util.LinkedHashMap<>();
        String raw = uri.getRawQuery();
        if (raw == null || raw.isBlank()) return out;
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            if (eq < 0) continue;
            out.put(java.net.URLDecoder.decode(pair.substring(0, eq), java.nio.charset.StandardCharsets.UTF_8),
                    java.net.URLDecoder.decode(pair.substring(eq + 1), java.nio.charset.StandardCharsets.UTF_8));
        }
        return out;
    }

    private static int parseIntOrDefault(String s, int def) {
        if (s == null || s.isBlank()) return def;
        try { return Integer.parseInt(s); } catch (NumberFormatException e) { return def; }
    }
}
