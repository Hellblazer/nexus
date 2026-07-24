/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.StagingPromoteOps;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * RDR-180 LAND-THEN-TRANSFORM HTTP surface (nexus-jxizy.10.4).
 *
 * <pre>
 *   POST /v1/staging/load/{store}   land verbatim rows (&le;300/batch, upsert)
 *   POST /v1/staging/embed_fill     embed staged NULL-vector content rows
 *                                   (the reuse-vs-reembed seam: the landing
 *                                   client stages a vector only when reuse is
 *                                   legal; everything else fills here)
 *   POST /v1/staging/promote        {collection, orphan_policy?} — one
 *                                   per-(tenant,collection) promote txn
 *   POST /v1/staging/finalize       {orphan_policy?} — the IDEMPOTENT
 *                                   re-runnable tenant finalize
 *   POST /v1/staging/clear          per-tenant DELETE across all 8 tables
 *   GET  /v1/staging/counts         per-store staged counts (parity checks)
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer} (AuthFilter) +
 * {@code X-Nexus-Tenant}; every statement runs under
 * {@link TenantScope#withTenant} (RLS-scoped by construction). Typed-DB
 * errors ride the shared {@link HttpUtil} ladder (503 pool / 409 class-23).
 */
public final class StagingHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(StagingHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** The staging quota mirror of the store-write cap. */
    static final int MAX_ROWS_PER_LOAD = 300;

    /** Embed-fill batch size (matches the serving upsert's embed batching). */
    private static final int EMBED_BATCH = 64;

    /** Per-store landing spec: staged columns in wire order + conflict clause. */
    private record StoreSpec(String table, List<String> columns, String conflict) {
    }

    private static final Map<String, StoreSpec> STORES = Map.of(
        "chunks", new StoreSpec("staging.chunks",
            List.of("collection", "dim", "legacy_ref", "chunk_text", "embedding", "model", "chunk_meta"),
            "ON CONFLICT (tenant_id, collection, legacy_ref) DO UPDATE SET "
            + "dim = excluded.dim, chunk_text = excluded.chunk_text, "
            + "embedding = excluded.embedding, model = excluded.model, "
            + "chunk_meta = excluded.chunk_meta"),
        "document_chunks", new StoreSpec("staging.document_chunks",
            List.of("doc_id", "position", "chash", "chunk_index", "line_start", "line_end", "char_start", "char_end"),
            "ON CONFLICT (tenant_id, doc_id, position) DO UPDATE SET chash = excluded.chash"),
        "topic_assignments", new StoreSpec("staging.topic_assignments",
            // topic_label + topic_collection are the CROSS-STORE topic
            // identity (critic-p1 Critical): the landing client sends the
            // SQLite topic_assignments JOIN topics projection; the legacy
            // integer id is audit-only (BIGSERIAL spaces never align).
            List.of("doc_id", "topic_id", "topic_label", "topic_collection"),
            "ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING"),
        "frecency", new StoreSpec("staging.frecency",
            List.of("chunk_id", "embedded_at", "ttl_days", "frecency_score", "miss_count", "last_hit_at"),
            "ON CONFLICT (tenant_id, chunk_id) DO NOTHING"),
        "relevance_log", new StoreSpec("staging.relevance_log",
            List.of("id", "query", "chunk_id", "collection", "action", "session_id", "ts"),
            "ON CONFLICT (tenant_id, id) DO NOTHING"),
        "document_aspects", new StoreSpec("staging.document_aspects",
            List.of("doc_id", "collection", "source_path", "problem_formulation", "proposed_method",
                    "experimental_datasets", "experimental_baselines", "experimental_results",
                    "extras", "confidence", "extracted_at", "model_version", "extractor_name", "source_uri"),
            "ON CONFLICT (tenant_id, collection, source_path) DO NOTHING"),
        "aspect_extraction_queue", new StoreSpec("staging.aspect_extraction_queue",
            List.of("collection", "source_path", "doc_id", "content_hash", "content", "status",
                    "retry_count", "enqueued_at", "last_attempt_at", "last_error"),
            "ON CONFLICT (tenant_id, collection, source_path) DO NOTHING"));

    private final TenantScope tenantScope;
    private final StagingPromoteOps promoteOps;
    private final EmbedderRouter docEmbedderRouter;

    public StagingHandler(TenantScope tenantScope, StagingPromoteOps promoteOps,
                          EmbedderRouter docEmbedderRouter) {
        this.tenantScope = tenantScope;
        this.promoteOps = promoteOps;
        this.docEmbedderRouter = docEmbedderRouter;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null) {
            HttpUtil.send(exchange, 500, "{\"error\":\"internal: tenant not set\"}");
            return;
        }
        String path   = exchange.getRequestURI().getPath();
        String op     = path.replaceFirst("^/v1/staging", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
        try {
            if (op.startsWith("/load/")) {
                handleLoad(exchange, tenant, method, op.substring("/load/".length()));
            } else {
                switch (op) {
                    case "/embed_fill" -> handleEmbedFill(exchange, tenant, method);
                    case "/promote"    -> handlePromote(exchange, tenant, method);
                    case "/finalize"   -> handleFinalize(exchange, tenant, method);
                    case "/clear"      -> handleClear(exchange, tenant, method);
                    case "/counts"     -> handleCounts(exchange, tenant, method);
                    default            -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
                }
            }
        } catch (StagingPromoteOps.PromoteConflictException e) {
            HttpUtil.send(exchange, 409, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (StagingPromoteOps.PromotePreconditionException | IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "staging_handler",
                    "op=" + op + " tenant=" + tenant)) {
                log.error("event=staging_handler_error op={} tenant={} error={}",
                        op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ── POST /v1/staging/load/{store} ────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private void handleLoad(HttpExchange exchange, String tenant, String method, String store)
            throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        StoreSpec spec = STORES.get(store);
        if (spec == null) {
            throw new IllegalArgumentException(
                "unknown staging store '" + store + "' — one of " + STORES.keySet());
        }
        Map<String, Object> body = MAPPER.readValue(exchange.getRequestBody(), MAP_TYPE);
        Object rowsRaw = body.get("rows");
        if (!(rowsRaw instanceof List<?> rows) || rows.isEmpty()) {
            throw new IllegalArgumentException("rows must be a non-empty list");
        }
        if (rows.size() > MAX_ROWS_PER_LOAD) {
            throw new IllegalArgumentException(
                "rows exceeds the per-load cap (" + rows.size() + " > " + MAX_ROWS_PER_LOAD + ")");
        }

        StringBuilder sql = new StringBuilder("INSERT INTO ").append(spec.table())
            .append(" (tenant_id");
        for (String c : spec.columns()) {
            // `ts` is the staged rename of relevance_log's reserved-ish
            // `timestamp`; everything else maps 1:1.
            sql.append(", ").append(c);
        }
        sql.append(") VALUES ");
        List<Object> binds = new ArrayList<>();
        for (int i = 0; i < rows.size(); i++) {
            Map<String, Object> row = (Map<String, Object>) rows.get(i);
            sql.append(i > 0 ? ", (" : "(").append("?");
            binds.add(tenant);
            for (String c : spec.columns()) {
                Object v = row.get(c);
                if ("embedding".equals(c)) {
                    sql.append(", ?::vector");
                    binds.add(v == null ? null : vectorLiteral((List<Number>) v));
                } else if ("chunk_meta".equals(c)) {
                    sql.append(", ?::jsonb");
                    binds.add(v == null ? null : MAPPER.writeValueAsString(v));
                } else {
                    sql.append(", ?");
                    binds.add(v);
                }
            }
            sql.append(")");
        }
        sql.append(" ").append(spec.conflict());

        int landed = tenantScope.withTenant(tenant, ctx ->
            ctx.execute(sql.toString(), binds.toArray()));
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("landed", landed)));
    }

    private static String vectorLiteral(List<Number> values) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) sb.append(',');
            sb.append(values.get(i).floatValue());
        }
        return sb.append(']').toString();
    }

    // ── POST /v1/staging/embed_fill ──────────────────────────────────────────

    /**
     * Embed staged content rows whose vectors are NULL (reuse was not legal
     * for them), batched, model routed by the staged HONEST collection name.
     * Idempotent: filled rows leave the predicate. Returns
     * {@code {"filled": n, "remaining": m}}.
     */
    private void handleEmbedFill(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        if (docEmbedderRouter == null) {
            HttpUtil.send(exchange, 503, "{\"error\":\"no embedder wired — embed_fill unavailable\"}");
            return;
        }
        Map<String, Object> body = MAPPER.readValue(exchange.getRequestBody(), MAP_TYPE);
        String collection = (String) body.get("collection");
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("collection is required");
        }
        int filled = 0;
        while (true) {
            List<Map<String, Object>> batch = tenantScope.withTenant(tenant, ctx ->
                ctx.resultQuery(
                    "SELECT legacy_ref, chunk_text FROM staging.chunks "
                    + "WHERE collection = ? AND chunk_text <> '' AND embedding IS NULL "
                    + "ORDER BY legacy_ref LIMIT " + EMBED_BATCH, collection)
                   .fetchMaps());
            if (batch.isEmpty()) break;
            List<String> texts = new ArrayList<>(batch.size());
            for (Map<String, Object> r : batch) texts.add((String) r.get("chunk_text"));
            List<float[]> vectors = docEmbedderRouter.embedForCollection(collection, texts);
            for (int i = 0; i < batch.size(); i++) {
                String ref = (String) batch.get(i).get("legacy_ref");
                StringBuilder lit = new StringBuilder("[");
                float[] v = vectors.get(i);
                for (int j = 0; j < v.length; j++) {
                    if (j > 0) lit.append(',');
                    lit.append(v[j]);
                }
                lit.append(']');
                tenantScope.withTenant(tenant, ctx -> ctx.execute(
                    "UPDATE staging.chunks SET embedding = ?::vector "
                    + "WHERE collection = ? AND legacy_ref = ?",
                    lit.toString(), collection, ref));
                filled++;
            }
        }
        Integer remaining = tenantScope.withTenant(tenant, ctx -> ctx.fetchOne(
            "SELECT count(*) FROM staging.chunks "
            + "WHERE collection = ? AND chunk_text <> '' AND embedding IS NULL", collection)
            .get(0, Integer.class));
        HttpUtil.send(exchange, 200,
            MAPPER.writeValueAsString(Map.of("filled", filled, "remaining", remaining)));
    }

    // ── POST /v1/staging/promote ─────────────────────────────────────────────

    private void handlePromote(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = MAPPER.readValue(exchange.getRequestBody(), MAP_TYPE);
        String collection = (String) body.get("collection");
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("collection is required");
        }
        // H1: the name-implied dim from the SAME dispatch serving uses.
        int impliedDim = PgVectorRepository.dimForCollection(collection);
        Map<String, Object> counts = promoteOps.promoteCollection(tenant, collection, impliedDim);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(counts));
    }

    // ── POST /v1/staging/finalize ────────────────────────────────────────────

    private void handleFinalize(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = exchange.getRequestBody().available() > 0
            ? MAPPER.readValue(exchange.getRequestBody(), MAP_TYPE) : Map.of();
        String policy = (String) body.getOrDefault("orphan_policy", "drop");
        if (!"drop".equals(policy) && !"synthesize".equals(policy)) {
            throw new IllegalArgumentException(
                "orphan_policy must be 'drop' or 'synthesize', got '" + policy + "'");
        }
        Map<String, Object> counts = promoteOps.finalizeTenant(tenant, "synthesize".equals(policy));
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(counts));
    }

    // ── POST /v1/staging/clear ───────────────────────────────────────────────

    /** Per-tenant DELETE (RLS-scoped — TRUNCATE would cross tenants). */
    private void handleClear(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> deleted = new LinkedHashMap<>();
        for (Map.Entry<String, StoreSpec> e : STORES.entrySet()) {
            int n = tenantScope.withTenant(tenant, ctx ->
                ctx.execute("DELETE FROM " + e.getValue().table()));
            deleted.put(e.getKey(), n);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("cleared", deleted)));
    }

    // ── GET /v1/staging/counts ───────────────────────────────────────────────

    private void handleCounts(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> counts = new LinkedHashMap<>();
        for (Map.Entry<String, StoreSpec> e : STORES.entrySet()) {
            Integer n = tenantScope.withTenant(tenant, ctx ->
                ctx.fetchOne("SELECT count(*) FROM " + e.getValue().table())
                   .get(0, Integer.class));
            counts.put(e.getKey(), n);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(counts));
    }
}
