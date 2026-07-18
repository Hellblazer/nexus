package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.PipelineRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-186 bead nexus-146xx.16 (engine half) — streaming-PDF buffer endpoints.
 *
 * <p>Routes (all under {@code /v1/pipeline/}) mirror the client
 * {@code PipelineDB} surface 1:1 so {@code HttpPipelineDB} is a drop-in:
 * <pre>
 *   POST /v1/pipeline/create             {content_hash, pdf_path, collection} → {status: created|resuming|skip}
 *   GET  /v1/pipeline/state              ?content_hash= → {pipeline: {...}|null}
 *   GET  /v1/pipeline/list               → {pipelines: [...]} (client-side orphan scan input)
 *   POST /v1/pipeline/progress           {content_hash, fields: {...}} (allowlisted counters)
 *   POST /v1/pipeline/extraction_meta    {content_hash, metadata_json}
 *   POST /v1/pipeline/complete           {content_hash}
 *   POST /v1/pipeline/fail               {content_hash, error?}
 *   POST /v1/pipeline/pages              {content_hash, pages: [...]} → {written}   (batch = one txn)
 *   GET  /v1/pipeline/pages              ?content_hash=&start= → {pages: [...]}
 *   POST /v1/pipeline/chunks             {content_hash, chunks: [...]} → {inserted} (INSERT-OR-IGNORE)
 *   GET  /v1/pipeline/chunks             ?content_hash=&uploadable=0|1&limit= → {chunks: [...]}
 *   POST /v1/pipeline/mark_uploaded      {content_hash, chunk_indices: [...]} → {updated}
 *   GET  /v1/pipeline/counts             ?content_hash= → {embedded_chunks, pipelines}
 *   POST /v1/pipeline/clear_wal          {content_hash}   (pages+chunks only; audit row survives)
 *   POST /v1/pipeline/delete             {content_hash}
 *   POST /v1/pipeline/delete_collection  {collection} → {deleted}
 * </pre>
 *
 * <p>Embedding wire mapping (the nexus-9n1u3 sentinel, carried verbatim):
 * JSON {@code null} ↔ SQL NULL (not embedded); {@code ""} ↔ empty BYTEA
 * (service-mode sentinel: the JVM embeds at upload); base64 ↔ packed floats.
 *
 * <p>All endpoints require {@code Authorization: Bearer} (AuthFilter) and
 * {@code X-Nexus-Tenant}.
 */
public final class PipelineHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(PipelineHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final PipelineRepository repo;

    public PipelineHandler(PipelineRepository repo) {
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
        String op     = path.replaceFirst("^/v1/pipeline", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/create"            -> handleCreate(exchange, tenant, method);
                case "/state"             -> handleState(exchange, tenant, method);
                case "/list"              -> handleList(exchange, tenant, method);
                case "/progress"          -> handleProgress(exchange, tenant, method);
                case "/extraction_meta"   -> handleExtractionMeta(exchange, tenant, method);
                case "/complete"          -> handleComplete(exchange, tenant, method);
                case "/fail"              -> handleFail(exchange, tenant, method);
                case "/pages"             -> handlePages(exchange, tenant, method);
                case "/chunks"            -> handleChunks(exchange, tenant, method);
                case "/mark_uploaded"     -> handleMarkUploaded(exchange, tenant, method);
                case "/counts"            -> handleCounts(exchange, tenant, method);
                case "/clear_wal"         -> handleClearWal(exchange, tenant, method);
                case "/delete"            -> handleDelete(exchange, tenant, method);
                case "/delete_collection" -> handleDeleteCollection(exchange, tenant, method);
                default                   -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "pipeline_handler",
                    "op=" + op + " tenant=" + tenant)) {
                log.error("event=pipeline_handler_error op={} tenant={} error={}",
                        op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ── lifecycle ────────────────────────────────────────────────────────────

    private void handleCreate(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        String status = repo.create(tenant,
                requireString(body, "content_hash"),
                requireString(body, "pdf_path"),
                requireString(body, "collection"));
        HttpUtil.send(exchange, 200, "{\"status\":\"" + status + "\"}");
    }

    private void handleState(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "GET")) return;
        String contentHash = requireParam(exchange, "content_hash");
        Map<String, Object> row = repo.get(tenant, contentHash);
        Map<String, Object> out = new HashMap<>();
        out.put("pipeline", row);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(out));
    }

    private void handleList(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "GET")) return;
        HttpUtil.send(exchange, 200,
                MAPPER.writeValueAsString(Map.of("pipelines", repo.listPipelines(tenant))));
    }

    private void handleProgress(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        String contentHash = requireString(body, "content_hash");
        Map<String, Integer> fields = new HashMap<>();
        if (body.get("fields") instanceof Map<?, ?> raw) {
            for (var entry : raw.entrySet()) {
                if (!(entry.getValue() instanceof Number n)) {
                    throw new IllegalArgumentException(
                        "progress field '" + entry.getKey() + "' must be an integer");
                }
                fields.put(String.valueOf(entry.getKey()), n.intValue());
            }
        }
        repo.updateProgress(tenant, contentHash, fields);
        HttpUtil.send(exchange, 200, "{\"updated\":true}");
    }

    private void handleExtractionMeta(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        repo.storeExtractionMeta(tenant,
                requireString(body, "content_hash"),
                body.get("metadata_json") instanceof String s ? s : "");
        HttpUtil.send(exchange, 200, "{\"updated\":true}");
    }

    private void handleComplete(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        repo.markCompleted(tenant, requireString(body, "content_hash"));
        HttpUtil.send(exchange, 200, "{\"updated\":true}");
    }

    private void handleFail(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        repo.markFailed(tenant,
                requireString(body, "content_hash"),
                body.get("error") instanceof String s ? s : "");
        HttpUtil.send(exchange, 200, "{\"updated\":true}");
    }

    // ── pages ────────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handlePages(HttpExchange exchange, String tenant, String method) throws IOException {
        if ("POST".equals(method)) {
            Map<String, Object> body = readBody(exchange);
            String contentHash = requireString(body, "content_hash");
            if (!(body.get("pages") instanceof List<?> pages) || pages.isEmpty()) {
                throw new IllegalArgumentException("'pages' must be a non-empty array");
            }
            for (Object item : pages) {
                if (!(item instanceof Map<?, ?> m)
                        || !(m.get("page_index") instanceof Number)
                        || !(m.get("page_text") instanceof String)) {
                    throw new IllegalArgumentException(
                        "each page must be an object with integer 'page_index' and string 'page_text'");
                }
            }
            int written = repo.writePages(tenant, contentHash, (List<Map<String, Object>>) pages);
            HttpUtil.send(exchange, 200, "{\"written\":" + written + "}");
            return;
        }
        if (wrongMethod(exchange, method, "GET")) return;
        String contentHash = requireParam(exchange, "content_hash");
        int start = intParam(exchange, "start", 0);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
                Map.of("pages", repo.readPagesFrom(tenant, contentHash, start))));
    }

    // ── chunks ───────────────────────────────────────────────────────────────

    private void handleChunks(HttpExchange exchange, String tenant, String method) throws IOException {
        if ("POST".equals(method)) {
            Map<String, Object> body = readBody(exchange);
            String contentHash = requireString(body, "content_hash");
            if (!(body.get("chunks") instanceof List<?> raw) || raw.isEmpty()) {
                throw new IllegalArgumentException("'chunks' must be a non-empty array");
            }
            List<Map<String, Object>> chunks = new ArrayList<>(raw.size());
            for (Object item : raw) {
                if (!(item instanceof Map<?, ?> m)) {
                    throw new IllegalArgumentException("each chunk must be an object");
                }
                @SuppressWarnings("unchecked")
                Map<String, Object> chunk = new HashMap<>((Map<String, Object>) m);
                if (!(chunk.get("chunk_index") instanceof Number)
                        || !(chunk.get("chunk_text") instanceof String)
                        || !(chunk.get("chunk_id") instanceof String)) {
                    throw new IllegalArgumentException(
                        "each chunk must carry integer 'chunk_index', string 'chunk_text' and 'chunk_id'");
                }
                chunk.put("embedding", decodeEmbedding(chunk.get("embedding")));
                chunks.add(chunk);
            }
            int inserted = repo.writeChunks(tenant, contentHash, chunks);
            HttpUtil.send(exchange, 200, "{\"inserted\":" + inserted + "}");
            return;
        }
        if (wrongMethod(exchange, method, "GET")) return;
        String contentHash = requireParam(exchange, "content_hash");
        boolean uploadable = "1".equals(queryParam(exchange, "uploadable"));
        int limit = intParam(exchange, "limit", 0);
        List<Map<String, Object>> rows = uploadable
                ? repo.readUploadableChunks(tenant, contentHash, limit)
                : repo.readReadyChunks(tenant, contentHash);
        for (Map<String, Object> row : rows) {
            row.put("embedding", encodeEmbedding(row.get("embedding")));
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("chunks", rows)));
    }

    private void handleMarkUploaded(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        String contentHash = requireString(body, "content_hash");
        List<Integer> indices = new ArrayList<>();
        if (body.get("chunk_indices") instanceof List<?> raw) {
            for (Object v : raw) {
                if (!(v instanceof Number n)) {
                    throw new IllegalArgumentException("'chunk_indices' must be integers");
                }
                indices.add(n.intValue());
            }
        }
        int updated = repo.markUploaded(tenant, contentHash, indices);
        HttpUtil.send(exchange, 200, "{\"updated\":" + updated + "}");
    }

    private void handleCounts(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "GET")) return;
        String contentHash = queryParam(exchange, "content_hash");
        int embedded = contentHash == null || contentHash.isBlank()
                ? 0 : repo.countEmbeddedChunks(tenant, contentHash);
        HttpUtil.send(exchange, 200,
                "{\"embedded_chunks\":" + embedded
                + ",\"pipelines\":" + repo.countPipelines(tenant) + "}");
    }

    // ── cleanup ──────────────────────────────────────────────────────────────

    private void handleClearWal(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        repo.clearOrphanWal(tenant, requireString(body, "content_hash"));
        HttpUtil.send(exchange, 200, "{\"cleared\":true}");
    }

    private void handleDelete(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        repo.deletePipeline(tenant, requireString(body, "content_hash"));
        HttpUtil.send(exchange, 200, "{\"deleted\":true}");
    }

    private void handleDeleteCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (wrongMethod(exchange, method, "POST")) return;
        Map<String, Object> body = readBody(exchange);
        int deleted = repo.deleteForCollection(tenant, requireString(body, "collection"));
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** JSON null → SQL NULL; "" → empty bytes (service-mode sentinel);
     *  base64 string → packed floats. */
    private static byte[] decodeEmbedding(Object value) {
        if (value == null) return null;
        if (!(value instanceof String s)) {
            throw new IllegalArgumentException("'embedding' must be null or a base64 string");
        }
        if (s.isEmpty()) return new byte[0];
        try {
            return Base64.getDecoder().decode(s);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("'embedding' is not valid base64");
        }
    }

    private static String encodeEmbedding(Object value) {
        if (value == null) return null;
        byte[] bytes = (byte[]) value;
        if (bytes.length == 0) return "";
        return Base64.getEncoder().encodeToString(bytes);
    }

    /** House inline-guard style (reviewer-146xx-16e: every other handler
     *  guards-and-returns; no exception, no stack-trace capture): true when
     *  the method mismatched and the 405 was already sent. */
    private static boolean wrongMethod(HttpExchange exchange, String method, String want)
            throws IOException {
        if (!want.equals(method)) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}");
            return true;
        }
        return false;
    }

    private static String requireString(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new IllegalArgumentException("'" + field + "' is required");
        }
        return s;
    }

    private static String requireParam(HttpExchange exchange, String key) {
        String value = queryParam(exchange, key);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("'" + key + "' query param is required");
        }
        return value;
    }

    private static int intParam(HttpExchange exchange, String key, int defaultValue) {
        String raw = queryParam(exchange, key);
        if (raw == null || raw.isBlank()) return defaultValue;
        try {
            return Integer.parseInt(raw);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("'" + key + "' must be an integer, got: " + raw);
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

    private Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) throw new IllegalArgumentException("request body is required");
            return MAPPER.readValue(bytes, MAP_TYPE);
        }
    }
}
