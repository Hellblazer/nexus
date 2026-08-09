/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service.http;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.db.StagingPromoteOps;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.binding.VectorBinding;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import static org.jooq.impl.DSL.excluded;

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

    /**
     * Per-store landing spec: staged columns in wire order, the ON CONFLICT
     * target columns, and the update-set columns (empty = DO NOTHING).
     * nexus-4okz4 increment 4: replaces the former opaque raw-SQL
     * {@code conflict} string with typed metadata so {@code handleLoad}
     * composes the INSERT via jOOQ's {@code onConflict(...).doUpdate()}
     * DSL instead of string concatenation.
     */
    private record StoreSpec(String table, List<String> columns,
                              List<String> conflictColumns, List<String> updateColumns) {
        /**
         * Typed table reference for a runtime-known (but not codegen-known —
         * staging is transient landing state, outside jOOQ's nexus/t1
         * inputSchema scope) table name. Same {@code DSL.table(DSL.name(...))}
         * idiom as {@code VersionHandler.DATABASECHANGELOG} / {@code
         * ChashCensus}'s dynamic table references.
         */
        Table<?> dslTable() {
            int dot = table.indexOf('.');
            return DSL.table(DSL.name(table.substring(0, dot), table.substring(dot + 1)));
        }
    }

    private static final Map<String, StoreSpec> STORES = Map.of(
        "chunks", new StoreSpec("staging.chunks",
            List.of("collection", "dim", "legacy_ref", "chunk_text", "embedding", "model", "chunk_meta"),
            List.of("tenant_id", "collection", "legacy_ref"),
            List.of("dim", "chunk_text", "embedding", "model", "chunk_meta")),
        "document_chunks", new StoreSpec("staging.document_chunks",
            List.of("doc_id", "position", "chash", "chunk_index", "line_start", "line_end", "char_start", "char_end"),
            List.of("tenant_id", "doc_id", "position"),
            List.of("chash")),
        "topic_assignments", new StoreSpec("staging.topic_assignments",
            // topic_label + topic_collection are the CROSS-STORE topic
            // identity (critic-p1 Critical): the landing client sends the
            // SQLite topic_assignments JOIN topics projection; the legacy
            // integer id is audit-only (BIGSERIAL spaces never align).
            List.of("doc_id", "topic_id", "topic_label", "topic_collection"),
            List.of("tenant_id", "doc_id", "topic_id"),
            List.of()),
        "frecency", new StoreSpec("staging.frecency",
            List.of("chunk_id", "embedded_at", "ttl_days", "frecency_score", "miss_count", "last_hit_at"),
            List.of("tenant_id", "chunk_id"),
            List.of()),
        "relevance_log", new StoreSpec("staging.relevance_log",
            List.of("id", "query", "chunk_id", "collection", "action", "session_id", "ts"),
            List.of("tenant_id", "id"),
            List.of()),
        "document_aspects", new StoreSpec("staging.document_aspects",
            List.of("doc_id", "collection", "source_path", "problem_formulation", "proposed_method",
                    "experimental_datasets", "experimental_baselines", "experimental_results",
                    "extras", "confidence", "extracted_at", "model_version", "extractor_name", "source_uri"),
            List.of("tenant_id", "collection", "source_path"),
            List.of()),
        "aspect_extraction_queue", new StoreSpec("staging.aspect_extraction_queue",
            List.of("collection", "source_path", "doc_id", "content_hash", "content", "status",
                    "retry_count", "enqueued_at", "last_attempt_at", "last_error"),
            List.of("tenant_id", "collection", "source_path"),
            List.of()));

    /**
     * Typed ad-hoc {@code Field} for a staging column by name (nexus-4okz4
     * increment 4). {@code embedding} carries the same {@link VectorBinding}
     * generated {@code chunks_&lt;dim&gt;.embedding} columns use (renders
     * the {@code ::vector} cast automatically); {@code chunk_meta} is a
     * jOOQ {@link JSONB} field (renders {@code ::jsonb} automatically, same
     * as every generated {@code metadata} column). Every other column is a
     * plain {@code Object} field — same runtime-type-driven bind jOOQ used
     * for the former plain-SQL {@code ?} placeholders, just routed through
     * the typed DSL instead of string concatenation.
     */
    @SuppressWarnings("unchecked")
    private static Field<Object> dynField(String name) {
        if ("embedding".equals(name)) {
            return (Field<Object>) (Field<?>) DSL.field(DSL.name(name),
                SQLDataType.OTHER.asConvertedDataType(new VectorBinding()));
        }
        if ("chunk_meta".equals(name)) {
            return (Field<Object>) (Field<?>) DSL.field(DSL.name(name), JSONB.class);
        }
        return DSL.field(DSL.name(name), Object.class);
    }

    /**
     * Wire-JSON value -&gt; bind value for one staging column (nexus-4okz4
     * increment 4). {@code embedding} (a JSON number array) becomes a
     * {@link Vector}; {@code chunk_meta} (an arbitrary JSON value) becomes
     * a {@link JSONB} literal (mirrors the original {@code ?::jsonb} cast —
     * every OTHER column, {@code document_aspects.extras} included, passes
     * through verbatim, exactly as the original code's plain {@code ?}
     * branch did (relying on PostgreSQL's assignment-context implicit
     * cast for any column that happens to be jsonb without an explicit
     * cast) — this conversion does not change that behavior either way.
     */
    @SuppressWarnings("unchecked")
    private static Object columnValue(String column, Object raw) throws IOException {
        if (raw == null) {
            return null;
        }
        if ("embedding".equals(column)) {
            List<Number> nums = (List<Number>) raw;
            float[] floats = new float[nums.size()];
            for (int i = 0; i < floats.length; i++) {
                floats[i] = nums.get(i).floatValue();
            }
            return Vector.of(floats);
        }
        if ("chunk_meta".equals(column)) {
            return JSONB.valueOf(MAPPER.writeValueAsString(raw));
        }
        return raw;
    }

    // Fixed-shape typed handles for staging.chunks (nexus-4okz4 increment 4):
    // handleEmbedFill's queries target this ONE, compile-time-known table with
    // compile-time-known columns — not the per-store dynamic shape handleLoad
    // deals with — so class-level constants read better than re-deriving
    // dynField(...) calls at every use site.
    private static final Table<?> STAGING_CHUNKS = DSL.table(DSL.name("staging", "chunks"));
    private static final Field<String> SC_LEGACY_REF = DSL.field(DSL.name("legacy_ref"), String.class);
    private static final Field<String> SC_CHUNK_TEXT = DSL.field(DSL.name("chunk_text"), String.class);
    private static final Field<String> SC_COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final Field<Vector> SC_EMBEDDING = DSL.field(DSL.name("embedding"),
        SQLDataType.OTHER.asConvertedDataType(new VectorBinding()));

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

        // `ts` is the staged rename of relevance_log's reserved-ish
        // `timestamp`; everything else maps 1:1 (nexus-4okz4 increment 4:
        // typed-DSL columns/values below, no more raw INSERT string).
        List<String> allColumns = new ArrayList<>(spec.columns().size() + 1);
        allColumns.add("tenant_id");
        allColumns.addAll(spec.columns());
        Field<?>[] fieldArray = allColumns.stream()
            .map(StagingHandler::dynField).toArray(Field<?>[]::new);

        List<Object[]> rowValues = new ArrayList<>(rows.size());
        for (Object rowObj : rows) {
            Map<String, Object> row = (Map<String, Object>) rowObj;
            Object[] values = new Object[fieldArray.length];
            values[0] = tenant;
            for (int i = 0; i < spec.columns().size(); i++) {
                String c = spec.columns().get(i);
                values[i + 1] = columnValue(c, row.get(c));
            }
            rowValues.add(values);
        }

        Field<?>[] conflictFields = spec.conflictColumns().stream()
            .map(StagingHandler::dynField).toArray(Field<?>[]::new);

        int landed = tenantScope.withTenant(tenant, ctx -> {
            var insert = ctx.insertInto(spec.dslTable(), fieldArray);
            for (Object[] values : rowValues) {
                insert = insert.values(values);
            }
            if (spec.updateColumns().isEmpty()) {
                return insert.onConflict(conflictFields).doNothing().execute();
            }
            // Map-based set(...) (nexus-4okz4 increment 4): the per-field
            // set(Field<T>, T) / set(Field<T>, Field<T>) overloads are
            // genuinely ambiguous to javac once T is erased to Object (an
            // excluded(f) Field value is itself a valid Object, so both
            // overloads apply) — the Map<?, ?> overload sidesteps generic
            // dispatch entirely and reads better for a variable-length
            // column set besides.
            Map<Field<?>, Field<?>> updateSet = new LinkedHashMap<>();
            for (String c : spec.updateColumns()) {
                Field<Object> f = dynField(c);
                updateSet.put(f, excluded(f));
            }
            return insert.onConflict(conflictFields).doUpdate().set(updateSet).execute();
        });
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("landed", landed)));
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
                ctx.select(SC_LEGACY_REF, SC_CHUNK_TEXT)
                   .from(STAGING_CHUNKS)
                   .where(SC_COLLECTION.eq(collection))
                   .and(SC_CHUNK_TEXT.ne(""))
                   .and(SC_EMBEDDING.isNull())
                   .orderBy(SC_LEGACY_REF)
                   .limit(EMBED_BATCH)
                   .fetchMaps());
            if (batch.isEmpty()) break;
            List<String> texts = new ArrayList<>(batch.size());
            for (Map<String, Object> r : batch) texts.add((String) r.get("chunk_text"));
            List<float[]> vectors = docEmbedderRouter.embedForCollection(collection, texts);
            for (int i = 0; i < batch.size(); i++) {
                String ref = (String) batch.get(i).get("legacy_ref");
                Vector v = Vector.of(vectors.get(i));
                tenantScope.withTenant(tenant, ctx -> ctx.update(STAGING_CHUNKS)
                    .set(SC_EMBEDDING, v)
                    .where(SC_COLLECTION.eq(collection))
                    .and(SC_LEGACY_REF.eq(ref))
                    .execute());
                filled++;
            }
        }
        Integer remaining = tenantScope.withTenant(tenant, ctx -> ctx.selectCount()
            .from(STAGING_CHUNKS)
            .where(SC_COLLECTION.eq(collection))
            .and(SC_CHUNK_TEXT.ne(""))
            .and(SC_EMBEDDING.isNull())
            .fetchOne(0, Integer.class));
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
                ctx.deleteFrom(e.getValue().dslTable()).execute());
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
                ctx.selectCount().from(e.getValue().dslTable()).fetchOne(0, Integer.class));
            counts.put(e.getKey(), n);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(counts));
    }
}
