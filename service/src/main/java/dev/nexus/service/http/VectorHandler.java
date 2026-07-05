// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.EmbeddingModelUnavailableException;
import dev.nexus.service.vectors.PgVectorRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Vector HTTP endpoints — pgvector serving surface (RDR-155 P4a.2, bead nexus-1k8s1).
 *
 * <p>Routes (all under {@code /v1/vectors/}):
 * <pre>
 *   POST /v1/vectors/upsert-chunks   server-side embed + pgvector write
 *   POST /v1/vectors/search          embed query server-side + cosine rank (multi-collection)
 *   POST /v1/vectors/query           alias for search (mirrors MCP query tool)
 *   POST /v1/vectors/hybrid-search   pgvector hybrid fusion (tsvector+pg_trgm gate, vector rank) — RDR-155 P3
 *   POST /v1/vectors/store-put       single-chunk put (MCP store_put path)
 *   POST /v1/vectors/get             get chunks by metadata where-filter (incremental-sync staleness check)
 *   POST /v1/vectors/store-get       fetch chunks by IDs (MCP store_get/store_get_many)
 *   POST /v1/vectors/store-list      list collection (MCP store_list)
 *   POST /v1/vectors/store-delete    delete by IDs (MCP store_delete)
 *   POST /v1/vectors/update-metadata metadata-only update (frecency reindex)
 *   GET  /v1/vectors/collections     list the tenant's collections
 *   GET  /v1/vectors/count           count chunks in a collection
 *   GET  /v1/vectors/stats           per-collection live stats (count/dim/last_write) — RDR-156 P3
 *   POST /v1/vectors/embed           embed-only (parity gate); 503 without a router
 * </pre>
 *
 * <p><strong>Tenant contract (skp06 supersession).</strong> Every serving op is
 * scoped by the SERVER-RESOLVED tenant from {@link RequestContext} under FORCE RLS —
 * a bearer bound to another tenant sees and affects exactly 0 rows. The Chroma-era
 * collection-name boundary (and the never-built skp06 app-layer guard) is replaced
 * by native RLS.
 *
 * <p><strong>Envelope parity.</strong> Response envelopes are byte-shape-identical
 * to the retired Chroma path (locked by {@code PgVectorServingContractTest}), so the
 * Python {@code _ServiceCollectionStub} / {@code HttpVectorClient} port unchanged.
 *
 * <p><strong>/get {@code include} parameter (P4a.2 decision, recorded on
 * nexus-1k8s1):</strong> the {@code include} field the Python stub sends is accepted
 * and IGNORED — /get always returns the full {@code {ids, documents, metadatas}}
 * envelope. Honouring {@code include} would make the envelope shape request-dependent
 * for no consumer benefit (the stub normalises all three keys unconditionally).
 *
 * <p><strong>Error mapping (P4a.2 decision, recorded on nexus-1k8s1):</strong>
 * {@link IllegalArgumentException} messages (including
 * {@code dimForCollection}'s, which echo the collection name) pass verbatim into
 * 400 bodies — the collection name is the caller's own request data and the bearer
 * is already tenant-bound, so nothing crosses a trust boundary. The Chroma quota
 * 413 mapping is retired with the Chroma serving path: pgvector imposes no
 * record-count / document-size quotas (RDR-155 §Retire).
 *
 * <p>503 when no {@link PgVectorRepository} is wired (matches the /embed
 * absent-router pin): a service constructed without a vector backend refuses
 * loudly instead of NPEing.
 */
public final class VectorHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(VectorHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /**
     * Engine↔conexus protocol header name (bead nexus-ehc4q).
     * The conexus edge proxy reads this value and injects it into the usage counter.
     * Absent (not "0") when {@code tokens == 0} — zero means "no billable usage" (e.g.
     * ONNX local-mode), which is meaningless to the proxy and must not be ingested.
     */
    public static final String USAGE_TOKENS_HEADER = "X-Nexus-Usage-Tokens";

    private final EmbedderRouter      embedderRouter;
    private final PgVectorRepository  pgRepo;

    /**
     * @param embedderRouter collection-aware embedder router for /embed (may be null —
     *                       /embed answers 503, the pinned absent-router behaviour)
     * @param pgRepo         pgvector repository serving every storage/query route
     *                       (may be null — all serving routes answer 503)
     */
    public VectorHandler(EmbedderRouter embedderRouter, PgVectorRepository pgRepo) {
        this.embedderRouter = embedderRouter;
        this.pgRepo         = pgRepo;
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        // Strip prefix /v1/vectors → /upsert-chunks, /search, etc.
        String op = path.replaceFirst("^/v1/vectors", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                case "/upsert-chunks" -> handleUpsertChunks(exchange, method);
                case "/search"        -> handleSearch(exchange, method);
                case "/query"         -> handleSearch(exchange, method);   // alias
                case "/hybrid-search" -> handleHybridSearch(exchange, method);  // RDR-155 P3
                case "/search-metadata-scoped" -> handleSearchMetadataScoped(exchange, method);  // RDR-156 P4
                case "/search-topic-scoped"    -> handleSearchTopicScoped(exchange, method);     // RDR-156 P4
                case "/search-graph-hop"       -> handleSearchGraphHop(exchange, method);        // RDR-156 P4 (houg9)
                case "/store-put"     -> handleStorePut(exchange, method);
                case "/get"           -> handleGet(exchange, method);
                case "/store-get"     -> handleStoreGet(exchange, method);
                case "/get-embeddings" -> handleGetEmbeddings(exchange, method);
                case "/store-list"    -> handleStoreList(exchange, method);
                case "/store-delete"  -> handleStoreDelete(exchange, method);
                case "/update-metadata" -> handleUpdateMetadata(exchange, method);
                case "/collections"   -> handleCollections(exchange, method);
                case "/count"         -> handleCount(exchange, method);
                case "/stats"         -> handleStats(exchange, method);   // RDR-156 P3
                case "/embed"         -> handleEmbed(exchange, method);    // parity gate
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (SkipHandlerException e) {
            // Response already sent (405 / 503 / 401 guard) — nothing further.
        } catch (EmbeddingModelUnavailableException e) {
            // nexus-pebfx.2: well-formed request, unservable in this embedding
            // mode (e.g. voyage-* collection while the service has no Voyage
            // credentials) → 422, distinguishable from a malformed request (400).
            log.warn("event=vector_model_unavailable op={} error={}", op, e.getMessage());
            HttpUtil.send(exchange, 422, json(Map.of("error", e.getMessage())));
        } catch (IllegalArgumentException e) {
            log.debug("event=vector_bad_request op={} error={}", op, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            // Shared typed-DB-error ladder: pool-exhaustion 503 + class-23 409
            // (nexus-h8rf6.2 / nexus-7e057) — see HttpUtil.sendTypedDbError.
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "vector_handler",
                    "op=" + op)) {
                log.error("event=vector_handler_error op={}", op, e);
                HttpUtil.send(exchange, 500, json(Map.of("error", "internal server error")));
            }
        }
    }

    // ── Per-request guards ────────────────────────────────────────────────────

    /**
     * 503 + skip when no pgvector repository is wired (matches the /embed
     * absent-router pattern: refuse explicitly, never NPE).
     */
    private PgVectorRepository requirePgRepo(HttpExchange ex) throws IOException {
        if (pgRepo == null) {
            HttpUtil.send(ex, 503, json(Map.of(
                    "error", "vector serving not configured (no pgvector repository)")));
            throw new SkipHandlerException();
        }
        return pgRepo;
    }

    /**
     * The SERVER-RESOLVED tenant for this request. Defense-in-depth, deliberately
     * redundant: AuthFilter rejects unauthenticated requests before this handler
     * runs, and TenantScope.withTenant fails loud on a blank tenant. This guard
     * exists because RLS is the tenant boundary on the pgvector path — if this
     * handler is ever instantiated without the filter, it must refuse, not widen.
     */
    private String requireTenant(HttpExchange ex) throws IOException {
        String tenant = RequestContext.tenant();
        if (tenant == null || tenant.isBlank()) {
            HttpUtil.send(ex, 401, json(Map.of("error", "no resolved tenant for request")));
            throw new SkipHandlerException();
        }
        return tenant;
    }

    // ── Handlers ──────────────────────────────────────────────────────────────

    /**
     * POST /v1/vectors/upsert-chunks
     *
     * <p>Primary Seam B write path.  Python sends chunk text (not vectors);
     * this service embeds + writes to the dispatched {@code chunks_<dim>} table.
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "knowledge__owner__model__v1",
     *   "ids":        ["sha256hex...", ...],
     *   "documents":  ["chunk text", ...],
     *   "metadatas":  [{...}, ...]   // optional; length must match ids if provided
     * }
     * </pre>
     *
     * <p>Response 200: {"upserted": N}
     */
    private void handleUpsertChunks(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection           = requireString(body, "collection");
        List<String> ids            = requireStringList(body, "ids");
        List<String> documents      = requireStringList(body, "documents");
        List<Map<String, Object>> metadatas = optMetadataList(body, "metadatas", ids.size());
        List<float[]> embeddings = optEmbeddingsList(body, "embeddings");

        if (ids.size() != documents.size()) {
            throw new IllegalArgumentException(
                    "ids length " + ids.size() + " != documents length " + documents.size());
        }

        if (embeddings != null) {
            // Same-model vector PASSTHROUGH (nexus-hxry2): store the supplied vectors
            // verbatim, no embedder call (token usage 0). Dimension is validated
            // against the dispatched table inside the repository (fail loud).
            if (embeddings.size() != ids.size()) {
                throw new IllegalArgumentException(
                        "embeddings length " + embeddings.size() + " != ids length " + ids.size());
            }
            repo.upsertChunksWithVectors(tenant, collection, ids, documents, embeddings, metadatas);
            emitTokenUsage(ex, 0L);
            HttpUtil.send(ex, 200, json(Map.of("upserted", ids.size(), "tokens", 0)));
            return;
        }

        var upsertResult = repo.upsertChunksWithTokens(tenant, collection, ids, documents, metadatas);
        // Emit token count from the doc-embedding call (bead nexus-ehc4q).
        emitTokenUsage(ex, upsertResult.tokens());
        HttpUtil.send(ex, 200, json(Map.of("upserted", ids.size())));
    }

    /**
     * POST /v1/vectors/search  (also: /query — same logic)
     *
     * <p>Request:
     * <pre>
     * {
     *   "query":       "search text",
     *   "collections": ["name1", "name2", ...],
     *   "n_results":   10,                    // optional, default 10
     *   "where":       {"key": "val"}         // optional metadata filter
     * }
     * </pre>
     *
     * <p>Response 200: [{"id","content","distance","collection", ...metadata}]
     *
     * <p>Optional: {@code "include_source_uri": true} gates a catalog JOIN to populate
     * {@code source_uri} on each row. Default false — omits the field entirely so default
     * callers pay zero JOIN cost (RDR-169 G5, bead nexus-jkv85).
     */
    private void handleSearch(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String queryText              = requireString(body, "query");
        List<String> collections      = requireStringList(body, "collections");
        int nResults                  = optInt(body, "n_results", 10);
        Map<String, Object> where     = optMap(body, "where");
        boolean includeSourceUri      = optBool(body, "include_source_uri", false);

        var searchResult = repo.searchWithTokens(tenant, queryText, collections, nResults, where,
                                                 includeSourceUri);
        // Emit token count from the query-embedding call (bead nexus-ehc4q).
        emitTokenUsage(ex, searchResult.tokens());
        HttpUtil.send(ex, 200, json(searchResult.value()));
    }

    /**
     * POST /v1/vectors/hybrid-search — RDR-155 Phase 3 (bead nexus-eap5l).
     *
     * <p>The pgvector hybrid fusion query (tsvector + pg_trgm text gate, vector rank).
     * Request body matches /search:
     * {@code {"query": "...", "collections": [...], "n_results": 10, "where": {...}}}.
     * Optional {@code "include_source_uri": true} gates the catalog JOIN (RDR-169 G5).
     */
    private void handleHybridSearch(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String queryText          = requireString(body, "query");
        List<String> collections  = requireStringList(body, "collections");
        int nResults              = optInt(body, "n_results", 10);
        Map<String, Object> where = optMap(body, "where");
        boolean includeSourceUri  = optBool(body, "include_source_uri", false);

        var hybridResult = repo.hybridSearchWithTokens(tenant, queryText, collections, nResults, where,
                                                       includeSourceUri);
        // Emit token count from the query-embedding call (bead nexus-ehc4q).
        emitTokenUsage(ex, hybridResult.tokens());
        HttpUtil.send(ex, 200, json(hybridResult.value()));
    }

    /**
     * POST /v1/vectors/search-metadata-scoped (RDR-156 P4, Decision 5).
     *
     * <p>The combined metadata-scoped query that retires the {@code query} MCP tool's
     * app-side catalog-routing dance. Request:
     * {@code {"query": "...", "collections": [...], "content_type": "...", "author": "...",
     * "year": 2024, "corpus": "...", "n_results": 10}} — any of content_type/author/year/
     * corpus may be omitted (no filter on that dimension). Returns document-level rows.
     */
    private void handleSearchMetadataScoped(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String queryText         = requireString(body, "query");
        List<String> collections = requireStringList(body, "collections");
        String contentType       = optString(body, "content_type");
        String author            = optString(body, "author");
        Integer year             = optInteger(body, "year");
        String corpus            = optString(body, "corpus");
        String subtree           = optString(body, "subtree");
        Map<String, Object> where = optMap(body, "where");
        int nResults             = optInt(body, "n_results", 10);

        var metaResult = repo.searchMetadataScopedWithTokens(
            tenant, queryText, collections, contentType, author, year, corpus,
            subtree, where, nResults);
        // Emit token count from the query-embedding call (bead nexus-ehc4q).
        emitTokenUsage(ex, metaResult.tokens());
        HttpUtil.send(ex, 200, json(metaResult.value()));
    }

    /**
     * POST /v1/vectors/search-topic-scoped (RDR-156 P4, Decision 5).
     *
     * <p>The combined topic-scoped query. Request:
     * {@code {"query": "...", "topic": "...", "collection": "...", "n_results": 10}}.
     * Chunk-level results (topic membership is chunk-keyed, nexus-sa14p).
     */
    private void handleSearchTopicScoped(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String queryText  = requireString(body, "query");
        String topicLabel = requireString(body, "topic");
        String collection = requireString(body, "collection");
        int nResults      = optInt(body, "n_results", 10);

        var topicResult = repo.searchTopicScopedWithTokens(tenant, queryText, topicLabel, collection, nResults);
        // Emit token count from the query-embedding call (bead nexus-ehc4q).
        emitTokenUsage(ex, topicResult.tokens());
        HttpUtil.send(ex, 200, json(topicResult.value()));
    }

    /**
     * POST /v1/vectors/search-graph-hop (RDR-156 P4 follow-on, Decision 5, bead nexus-houg9).
     *
     * <p>The combined graph-hop query that retires the {@code query} tool's
     * {@code follow_links} app-side BFS dance. Request:
     * {@code {"query": "...", "seeds": [...], "collections": [...], "link_type": "cites",
     * "depth": 1, "direction": "both", "n_results": 10}} — link_type may be omitted
     * (follow all edge types); direction defaults to "both"; depth defaults to 1.
     * Document-level rows, each carrying the matched chunk's {@code chash}.
     */
    private void handleSearchGraphHop(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String queryText         = requireString(body, "query");
        List<String> seeds       = requireStringList(body, "seeds");
        List<String> collections = requireStringList(body, "collections");
        String linkType          = optString(body, "link_type");
        int depth                = optInt(body, "depth", 1);
        String direction         = optString(body, "direction");
        if (direction == null) direction = "both";
        int nResults             = optInt(body, "n_results", 10);

        var graphResult = repo.searchGraphHopWithTokens(
            tenant, queryText, seeds, collections, linkType, depth, direction, nResults);
        // Emit token count from the query-embedding call (bead nexus-ehc4q).
        emitTokenUsage(ex, graphResult.tokens());
        HttpUtil.send(ex, 200, json(graphResult.value()));
    }

    /**
     * POST /v1/vectors/store-put
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "knowledge__...",
     *   "doc_id":     "sha256hex...",   // chunk ID
     *   "content":    "chunk text",
     *   "metadata":   {...}              // optional
     * }
     * </pre>
     *
     * <p>Response 200: {"id": "..."}
     */
    private void handleStorePut(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection  = requireString(body, "collection");
        String docId       = requireString(body, "doc_id");
        String content     = requireString(body, "content");
        Map<String, Object> metadata = optMap(body, "metadata");
        if (metadata == null) metadata = Map.of();

        var putResult = repo.putWithTokens(tenant, collection, docId, content, metadata);
        // Emit token count from the doc-embedding call (bead nexus-ehc4q).
        emitTokenUsage(ex, putResult.tokens());
        HttpUtil.send(ex, 200, json(Map.of("id", putResult.value())));
    }

    /**
     * POST /v1/vectors/get
     *
     * <p>Incremental-sync staleness check for the Python {@code _ServiceCollectionStub}
     * (RDR-152 Seam B nexus-gmiaf.22): doc_indexer queries existing chunks by
     * {@code source_key} / {@code content_hash} without fetching the full collection.
     * Plain-equality predicates only (the staleness check's shape).
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "...",
     *   "where":      {"source_key": "..."},  // optional plain-equality metadata filter
     *   "include":    ["metadatas"],    // optional, ignored — always returns ids+docs+metadatas
     *                                   // (P4a.2 decision, recorded on nexus-1k8s1)
     *   "limit":      10,              // optional, default 10
     *   "offset":     0               // optional, default 0
     * }
     * </pre>
     *
     * <p>Response 200: {"ids":[...], "documents":[...], "metadatas":[...]}
     */
    private void handleGet(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection              = requireString(body, "collection");
        Map<String, Object> where      = optMap(body, "where");
        int limit                      = optInt(body, "limit", 10);
        int offset                     = optInt(body, "offset", 0);
        boolean includeSourceUri       = optBool(body, "include_source_uri", false);

        var result = repo.getWhere(tenant, collection, where, limit, offset, includeSourceUri);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/vectors/store-get
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "...",
     *   "ids":        ["...", ...],    // optional; if absent returns paginated
     *   "limit":      20,              // optional, default 20
     *   "offset":     0               // optional, default 0
     * }
     * </pre>
     *
     * <p>Response 200: {"ids":[...], "documents":[...], "metadatas":[...]}
     */
    private void handleStoreGet(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection  = requireString(body, "collection");
        List<String> ids   = optStringList(body, "ids");
        int limit          = optInt(body, "limit", 20);
        int offset         = optInt(body, "offset", 0);
        boolean includeSourceUri = optBool(body, "include_source_uri", false);

        // No ids → paginated full fetch (same envelope); getWhere with no
        // predicates is exactly that shape.
        var result = (ids == null)
                ? repo.getWhere(tenant, collection, null, limit, offset, includeSourceUri)
                : repo.get(tenant, collection, ids, limit, offset, includeSourceUri);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/vectors/get-embeddings (bead nexus-pebfx.7)
     *
     * <p>Request: {"collection": "...", "ids": ["...", ...]}
     * <p>Response 200: {"ids":[...], "embeddings":[[...], ...]} in request
     * order; missing ids omitted (Chroma parity — the Python caller detects
     * the count mismatch).
     */
    private void handleGetEmbeddings(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection = requireString(body, "collection");
        List<String> ids  = optStringList(body, "ids");
        var result = repo.getEmbeddings(tenant, collection,
                                        ids == null ? List.of() : ids);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/vectors/store-list
     *
     * <p>Request: {"collection": "...", "limit": 20, "offset": 0}
     * <p>Response 200: {"ids":[...], "metadatas":[...]}
     */
    private void handleStoreList(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection = requireString(body, "collection");
        int limit         = optInt(body, "limit", 20);
        int offset        = optInt(body, "offset", 0);

        var result = repo.list(tenant, collection, limit, offset);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/vectors/store-delete
     *
     * <p>Manifest obligation (P4a.2 decision, recorded on nexus-1k8s1): callers are
     * responsible for removing {@code catalog_document_chunks} rows referencing the
     * deleted chunks — the serving path does NOT pre-check the manifest (documented
     * caller obligation per {@link PgVectorRepository#delete}; dangling references
     * fail loud at {@code fetchDocumentChunks}, never silently).
     *
     * <p>Request: {"collection": "...", "ids": ["...", ...]}
     * <p>Response 200: {"deleted": N} — rows ACTUALLY deleted (RLS makes foreign
     * tenants' rows invisible, so cross-tenant attempts delete exactly 0)
     */
    private void handleStoreDelete(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection  = requireString(body, "collection");
        List<String> ids   = requireStringList(body, "ids");

        int deleted = repo.delete(tenant, collection, ids);
        HttpUtil.send(ex, 200, json(Map.of("deleted", deleted)));
    }

    /**
     * POST /v1/vectors/update-metadata  (RDR-152 bead nexus-enehl)
     *
     * <p>Metadata-only update on existing chunks — no re-embedding.
     * Used by the Python {@code _ServiceCollectionStub.update()} call from
     * {@code _run_index_frecency_only}: updates {@code frecency_score} on
     * already-stored chunks without touching document text or vectors.
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "code__owner__voyage-code-3__v1",
     *   "ids":        ["sha256hex...", ...],
     *   "metadatas":  [{"frecency_score": 0.75, ...}, ...]
     * }
     * </pre>
     *
     * <p>Response 200: {"updated": N}
     */
    private void handleUpdateMetadata(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection                     = requireString(body, "collection");
        List<String> ids                      = requireStringList(body, "ids");
        List<Map<String, Object>> metadatas   = optMetadataList(body, "metadatas", ids.size());

        if (metadatas.size() != ids.size()) {
            throw new IllegalArgumentException(
                    "metadatas length " + metadatas.size() + " != ids length " + ids.size());
        }

        // RDR-181 (bead nexus-f0r8p.2): updateMetadata now returns the actual
        // affected-row count rather than void — report it verbatim instead of
        // assuming every id existed (a stale/deleted id previously reported as
        // "updated" with no row actually touched).
        int updated = repo.updateMetadata(tenant, collection, ids, metadatas);
        HttpUtil.send(ex, 200, json(Map.of("updated", updated)));
    }

    /**
     * GET /v1/vectors/collections
     * Response 200: [{"name":"..."}, ...] — the tenant's collections only (RLS)
     */
    private void handleCollections(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        var cols = repo.listCollections(tenant);
        HttpUtil.send(ex, 200, json(cols));
    }

    /**
     * GET /v1/vectors/stats
     * Response 200: [{"name":"...","dim":384,"count":N,"last_write":"2026-..."}, ...]
     *
     * <p>Per-collection vector statistics from {@code nexus.collection_vector_stats}
     * (RDR-156 P3, Decision 4) — tombstone-filtered live counts, one round-trip for
     * all of the tenant's collections. Replaces doctor/status N+1 count() loops.
     */
    private void handleStats(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        var stats = repo.collectionStats(tenant);
        HttpUtil.send(ex, 200, json(stats));
    }

    /**
     * GET /v1/vectors/count?collection=...
     * Response 200: {"count": N}
     */
    private void handleCount(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "GET");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        String collection = requireQueryParam(ex, "collection");
        int count = repo.count(tenant, collection);
        HttpUtil.send(ex, 200, json(Map.of("count", count)));
    }

    /**
     * POST /v1/vectors/embed
     *
     * <p>Embed-only endpoint — returns raw vectors WITHOUT storing.
     * Used by the parity gate (bead nexus-gmiaf.21) to compare Java vs Python
     * embedding output directly (cosine == 1.0 exactly).
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "knowledge__owner__voyage-context-3__v1",  // drives embedder routing
     *   "texts":      ["text0", "text1", ...]
     * }
     * </pre>
     *
     * <p>Response 200:
     * <pre>
     * {
     *   "embeddings": [[f0, f1, ...], [f0, f1, ...], ...]
     * }
     * </pre>
     *
     * <p>Returns 503 if no EmbedderRouter was configured — a pinned invariant
     * ({@code PgVectorServingContractTest} Order 13): absent backend is an explicit
     * refusal, never a fallback.
     */
    private void handleEmbed(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        if (embedderRouter == null) {
            HttpUtil.send(ex, 503, json(Map.of("error", "embed endpoint not configured")));
            return;
        }
        Map<String, Object> body = readBody(ex);
        String collection     = requireString(body, "collection");
        List<String> texts    = requireStringList(body, "texts");

        // Use embedForCollectionWithUsage to get both embeddings and token count in one
        // API call (bead nexus-ehc4q). The float32 vectors are promoted to double exactly
        // (same float32 binary as embedDoubleForCollection — both decode the same base64
        // blob; the only difference was that embedDouble skipped the Java float intermediate,
        // but the source bits are identical). This avoids a double-embed while capturing tokens.
        EmbedResult embedResult = embedderRouter.embedForCollectionWithUsage(collection, texts);
        List<float[]> float32Vecs = embedResult.embeddings();

        // Convert to List<List<Double>> for JSON serialization, promoting float32 → double.
        List<List<Double>> embeddings = new ArrayList<>(float32Vecs.size());
        for (float[] fv : float32Vecs) {
            List<Double> row = new ArrayList<>(fv.length);
            for (float f : fv) row.add((double) f);
            embeddings.add(row);
        }
        // Emit token count before sending (bead nexus-ehc4q).
        emitTokenUsage(ex, embedResult.tokens());
        HttpUtil.send(ex, 200, json(Map.of("embeddings", embeddings)));
    }

    // ── Token-usage header helper (bead nexus-ehc4q) ─────────────────────────

    /**
     * Emit the {@code X-Nexus-Usage-Tokens} response header when the embedding
     * call consumed a non-zero token count (bead nexus-ehc4q).
     *
     * <p>Must be called BEFORE {@link HttpUtil#send} — once headers are written
     * the exchange is committed and further header mutations are silently ignored.
     *
     * <p>Only sets the header when {@code tokens > 0}: a zero value means the
     * embedder did not report usage (test fakes, ONNX default path, non-embedding
     * endpoints).
     *
     * @param ex     the in-flight HTTP exchange
     * @param tokens token count from the embedding call (0 = not available)
     */
    private static void emitTokenUsage(HttpExchange ex, long tokens) {
        if (tokens > 0) {
            ex.getResponseHeaders().set(USAGE_TOKENS_HEADER, Long.toString(tokens));
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

    private List<String> requireStringList(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (!(val instanceof List<?> list)) {
            throw new IllegalArgumentException("field '" + key + "' must be an array");
        }
        List<String> result = new ArrayList<>(list.size());
        for (Object item : list) result.add(item == null ? "" : item.toString());
        return result;
    }

    private List<String> optStringList(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (!(val instanceof List<?> list)) return null;
        List<String> result = new ArrayList<>(list.size());
        for (Object item : list) result.add(item == null ? "" : item.toString());
        return result;
    }

    /**
     * Optional {@code embeddings} field: a list of numeric vectors (one per id),
     * for the same-model vector-passthrough path (nexus-hxry2). Returns null when
     * absent (the default server-side-embed path). Malformed shapes fail loud —
     * a claimed passthrough that cannot be parsed must NOT silently fall back to
     * re-embed (that would mask a client bug and re-bill).
     */
    private List<float[]> optEmbeddingsList(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (!(val instanceof List<?> rows)) {
            throw new IllegalArgumentException("field '" + key + "' must be an array of vectors");
        }
        List<float[]> result = new ArrayList<>(rows.size());
        for (Object row : rows) {
            if (!(row instanceof List<?> nums)) {
                throw new IllegalArgumentException("field '" + key + "' must be an array of numeric vectors");
            }
            float[] vec = new float[nums.size()];
            for (int i = 0; i < nums.size(); i++) {
                Object n = nums.get(i);
                if (!(n instanceof Number num)) {
                    throw new IllegalArgumentException("field '" + key + "' contains a non-numeric component");
                }
                vec[i] = num.floatValue();
            }
            result.add(vec);
        }
        return result;
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> optMetadataList(Map<String, Object> body, String key, int expectedSize) {
        Object val = body.get(key);
        if (val == null) {
            // Return list of empty maps as default
            List<Map<String, Object>> defaults = new ArrayList<>(expectedSize);
            for (int i = 0; i < expectedSize; i++) defaults.add(Map.of());
            return defaults;
        }
        if (!(val instanceof List<?> list)) {
            throw new IllegalArgumentException("field '" + key + "' must be an array");
        }
        List<Map<String, Object>> result = new ArrayList<>(list.size());
        for (Object item : list) {
            if (item instanceof Map<?, ?> m) result.add((Map<String, Object>) m);
            else result.add(Map.of());
        }
        return result;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> optMap(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Map<?, ?> m) return (Map<String, Object>) m;
        return null;
    }

    private int optInt(Map<String, Object> body, String key, int defaultValue) {
        Object val = body.get(key);
        if (val == null) return defaultValue;
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be an integer");
        }
    }

    /** Optional boolean field; absent or null → {@code defaultValue}. */
    private boolean optBool(Map<String, Object> body, String key, boolean defaultValue) {
        Object val = body.get(key);
        if (val == null) return defaultValue;
        if (val instanceof Boolean b) return b;
        return Boolean.parseBoolean(val.toString());
    }

    /** Optional string field; null/blank → null (no-filter semantics for combined queries). */
    private String optString(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        String s = val.toString();
        return s.isBlank() ? null : s;
    }

    /** Optional integer field; null → null (no-filter on that dimension). */
    private Integer optInteger(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Number n) return n.intValue();
        try { return Integer.parseInt(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be an integer");
        }
    }

    private String requireQueryParam(HttpExchange ex, String key) {
        String raw = ex.getRequestURI().getRawQuery();
        if (raw != null) {
            for (String pair : raw.split("&")) {
                int eq = pair.indexOf('=');
                if (eq > 0) {
                    String k = java.net.URLDecoder.decode(pair.substring(0, eq), java.nio.charset.StandardCharsets.UTF_8);
                    if (k.equals(key)) {
                        String v = java.net.URLDecoder.decode(pair.substring(eq + 1), java.nio.charset.StandardCharsets.UTF_8);
                        if (!v.isBlank()) return v;
                    }
                }
            }
        }
        throw new IllegalArgumentException("missing required query param: " + key);
    }

    private void requireMethod(HttpExchange ex, String actual, String expected) throws IOException {
        if (!expected.equalsIgnoreCase(actual)) {
            HttpUtil.send(ex, 405, "{\"error\":\"method not allowed\"}");
            throw new SkipHandlerException();
        }
    }

    private String json(Object obj) {
        try { return MAPPER.writeValueAsString(obj); }
        catch (Exception e) {
            log.error("event=json_serialize_error", e);
            return "{\"error\":\"serialization failed\"}";
        }
    }

    private static final class SkipHandlerException extends RuntimeException {
        SkipHandlerException() { super(null, null, true, false); }
    }
}
