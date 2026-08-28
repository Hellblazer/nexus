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
 *   POST /v1/vectors/search-metadata-scoped  combined catalog-metadata-scoped query — RDR-156 P4
 *   POST /v1/vectors/search-topic-scoped     combined topic-scoped query (chunk-level) — RDR-156 P4
 *   POST /v1/vectors/search-graph-hop        combined graph-hop query (catalog BFS + rank) — RDR-156 P4
 *   POST /v1/vectors/search-aspect-scoped    combined aspect-filtered query (vector rank + document_aspects predicate) — RDR-156 D5
 *   POST /v1/vectors/store-put       single-chunk put (MCP store_put path)
 *   POST /v1/vectors/get             get chunks by metadata where-filter (incremental-sync staleness check)
 *   POST /v1/vectors/get-all-metadata  ids+metadata for an ENTIRE collection in one round trip (nexus-duoak)
 *   POST /v1/vectors/store-get       fetch chunks by IDs (MCP store_get/store_get_many)
 *   POST /v1/vectors/get-embeddings  fetch stored vectors by IDs (migration/audit)
 *   POST /v1/vectors/store-list      list collection (MCP store_list)
 *   POST /v1/vectors/store-delete    delete by IDs (MCP store_delete)
 *   POST /v1/vectors/update-metadata metadata-only update (frecency reindex)
 *   GET  /v1/vectors/collections     list the tenant's collections
 *   GET  /v1/vectors/count           count chunks in a collection
 *   GET  /v1/vectors/stats           per-collection live stats (count/dim/last_write) — RDR-156 P3
 *   POST /v1/vectors/embed           embed-only (parity gate); 503 without a router
 * </pre>
 *
 * <p><strong>Fused rerank stage (RDR-188, bead nexus-9o6y2.2).</strong> The five
 * search routes (search/query, hybrid-search, search-metadata-scoped,
 * search-topic-scoped, search-graph-hop) accept optional {@code "rerank": true}
 * + {@code "rerank_top_k": N} request fields. With {@code rerank=true} the
 * response becomes the {@link RerankStage} object envelope
 * ({@code {"results": [...], "rerank_degraded": ...}}) — rows are reranked
 * server-side on content already fetched under RLS, and any scoring failure
 * degrades LOUD via {@code rerank_degraded=true} + {@code rerank_error}, never
 * a silent fallback to distance order. Without the field the bare-array
 * envelope is byte-shape unchanged.
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
     * Upper bound on {@code ids} accepted by {@code /v1/vectors/store-get}
     * (nexus-hdx2u E2). Same value and rationale as {@code CatalogHandler
     * .MAX_BATCH_DOC_IDS} — well under PostgreSQL's 32767-parameter
     * Bind-message hard limit.
     */
    private static final int MAX_BATCH_IDS = 1000;

    /**
     * Engine↔conexus protocol header name (bead nexus-ehc4q).
     * The conexus edge proxy reads this value and injects it into the usage counter.
     * Absent (not "0") when {@code tokens == 0} — zero means "no billable usage" (e.g.
     * ONNX local-mode), which is meaningless to the proxy and must not be ingested.
     */
    public static final String USAGE_TOKENS_HEADER = "X-Nexus-Usage-Tokens";

    private final EmbedderRouter      embedderRouter;
    private final PgVectorRepository  pgRepo;
    private final RerankStage         rerankStage;

    /**
     * @param embedderRouter collection-aware embedder router for /embed (may be null —
     *                       /embed answers 503, the pinned absent-router behaviour)
     * @param pgRepo         pgvector repository serving every storage/query route
     *                       (may be null — all serving routes answer 503)
     */
    public VectorHandler(EmbedderRouter embedderRouter, PgVectorRepository pgRepo) {
        this(embedderRouter, pgRepo, null);
    }

    /**
     * Full wiring (RDR-188 bead nexus-9o6y2.2).
     *
     * @param embedderRouter collection-aware embedder router for /embed (may be null —
     *                       /embed answers 503, the pinned absent-router behaviour)
     * @param pgRepo         pgvector repository serving every storage/query route
     *                       (may be null — all serving routes answer 503)
     * @param reranker       reranker for the fused rerank stage on the search routes
     *                       (may be null — {@code rerank=true} requests degrade LOUD
     *                       with {@code rerank_error="no reranker configured..."})
     */
    public VectorHandler(EmbedderRouter embedderRouter, PgVectorRepository pgRepo,
                         dev.nexus.service.vectors.Reranker reranker) {
        this.embedderRouter = embedderRouter;
        this.pgRepo         = pgRepo;
        this.rerankStage    = new RerankStage(reranker);
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
                case "/search-aspect-scoped"   -> handleSearchAspectScoped(exchange, method);    // RDR-156 D5 (ubnwk)
                case "/store-put"     -> handleStorePut(exchange, method);
                case "/get"           -> handleGet(exchange, method);
                case "/get-all-metadata" -> handleGetAllMetadata(exchange, method);
                case "/store-get"     -> handleStoreGet(exchange, method);
                case "/get-embeddings" -> handleGetEmbeddings(exchange, method);
                case "/store-list"    -> handleStoreList(exchange, method);
                case "/store-delete"  -> handleStoreDelete(exchange, method);
                case "/update-metadata" -> handleUpdateMetadata(exchange, method);
                case "/collections"   -> handleCollections(exchange, method);
                case "/count"         -> handleCount(exchange, method);
                case "/stats"         -> handleStats(exchange, method);   // RDR-156 P3
                case "/embed"         -> handleEmbed(exchange, method);    // parity gate
                case "/gc/quarantine-orphans"  -> handleGcQuarantineOrphans(exchange, method);   // RDR-191 P1
                case "/gc/restore-rereferenced" -> handleGcRestoreRereferenced(exchange, method); // RDR-191 P1
                case "/gc/expire-quarantine"   -> handleGcExpireQuarantine(exchange, method);     // RDR-191 P1
                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}");
            }
        } catch (SkipHandlerException e) {
            // Response already sent (405 / 503 / 401 guard) — nothing further.
        } catch (dev.nexus.service.vectors.UpstreamAuthException e) {
            // nexus-pmhpc: upstream (Voyage) rejected OUR credentials — an
            // operator/config problem on every embedding-bearing route
            // (store-put, search, query, hybrid). 502 Bad Gateway with the
            // actionable detail, never an opaque 500.
            log.error("event=vector_upstream_auth_failed op={} error={}", op, e.getMessage());
            HttpUtil.send(exchange, 502, json(Map.of("error", e.getMessage())));
        } catch (EmbeddingModelUnavailableException e) {
            // nexus-pebfx.2: well-formed request, unservable in this embedding
            // mode (e.g. voyage-* collection while the service has no Voyage
            // credentials) → 422, distinguishable from a malformed request (400).
            log.warn("event=vector_model_unavailable op={} error={}", op, e.getMessage());
            HttpUtil.send(exchange, 422, json(Map.of("error", e.getMessage())));
        } catch (dev.nexus.service.vectors.VoyageTooManyTokensException e) {
            // RDR-195 gate remediation (nexus-kmtlp.11 fix 2, 2026-08-19): a precisely
            // typed, actionable oversize-batch condition must not launder into the
            // generic 500 arm below — that laundering is exactly the opaque-500 symptom
            // this exception exists to replace, one layer up (substantive-critic finding
            // 2, T2 substantive-critique-rdr195-phase2-da9c61781-2026-08-19). 422, not
            // 500 (the opaque outcome being fixed) and not one of the client's
            // _GATEWAY_RETRY_CODES {502,503,504} — resending the identical oversize body
            // is guaranteed to fail again and re-bills Voyage tokenization.
            log.warn("event=vector_too_many_tokens_in_batch op={} model={} batch_size={} "
                    + "sub_requests={} error={}",
                    op, e.model(), e.batchSize(), e.subRequests(), e.getMessage());
            HttpUtil.send(exchange, 422, json(Map.of(
                    "error", e.errorCode(),
                    "detail", e.getMessage(),
                    "sub_requests", e.subRequests(),
                    "batch_size", e.batchSize(),
                    "model", e.model())));
        } catch (IllegalArgumentException e) {
            log.debug("event=vector_bad_request op={} error={}", op, e.getMessage());
            HttpUtil.send(exchange, 400, json(Map.of("error", e.getMessage())));
        } catch (IllegalStateException e) {
            // get-all-metadata's row-count cap (well-formed request, just too
            // big for the single-round-trip fast path) — 422 distinguishes
            // this from a malformed request (400) or a real server error
            // (500); the Python client falls back to paginated /get on any
            // non-2xx, so the exact code just needs to be non-2xx and logged.
            log.debug("event=vector_get_all_metadata_row_cap_exceeded op={} error={}", op, e.getMessage());
            HttpUtil.send(exchange, 422, json(Map.of("error", e.getMessage())));
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
     *   "metadatas":  [{...}, ...]    // optional; length must match ids if provided
     *   "force_re_embed": false       // optional, default false — see below
     * }
     * </pre>
     *
     * <p>{@code force_re_embed} (RDR-181, bead nexus-f0r8p.3): bypasses the
     * server-side existence-partition entirely so every chunk in the batch is
     * re-embedded, even if its chash already has a stored vector. Wired from the
     * client {@code --force} path and the deprecated
     * {@code NX_UPSERT_SKIP_EXISTING=0} escape — the rare model-drift-within-a-
     * collection recompute, and the escape for the (0%-hit) first-index path so
     * it never pays for the existence SELECT with no offsetting benefit. Ignored
     * on the vector-passthrough branch below ({@code embeddings} supplied) — that
     * path already skips the existence check unconditionally.
     *
     * <p>Server-side (Postgres/service-mode) only: this endpoint and
     * {@link dev.nexus.service.vectors.PgVectorRepository} are the sole owners of
     * the embed-skip optimization {@code force_re_embed} bypasses. The Python
     * client's local/Chroma-mode path ({@code T3Database.upsert_chunks} /
     * {@code upsert_chunks_with_embeddings}) accepts {@code force_re_embed} for
     * signature parity with {@code HttpVectorClient} (callers duck-type against
     * {@code IndexContext.db} regardless of mode) but treats it as a documented
     * no-op — local mode has no server-side existence-partition to bypass.
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
        // RDR-180 (nexus-jxizy.7/.8): ONE strict tier — chunk ids ARE
        // chashes, the full-digest 64-hex form, parsed through the type so a
        // wrong-width id 400s with its index BEFORE the embed + transaction.
        // The byte[16]-era length-only tolerance (Chroma-era arbitrary
        // 32-char external ids) is retired: post-rekey every surviving row
        // id is a real 32-byte digest, and PgVectorServingContractTest now
        // proves the REJECTION of the old tolerated shape.
        for (int i = 0; i < ids.size(); i++) {
            dev.nexus.service.db.Chash.requireCanonical(ids.get(i), "ids[" + i + "]");
        }
        List<String> documents      = requireStringList(body, "documents");
        List<Map<String, Object>> metadatas = optMetadataList(body, "metadatas", ids.size());
        List<float[]> embeddings = optEmbeddingsList(body, "embeddings");
        boolean forceReEmbed = Boolean.TRUE.equals(body.get("force_re_embed"));

        if (ids.size() != documents.size()) {
            throw new IllegalArgumentException(
                    "ids length " + ids.size() + " != documents length " + documents.size());
        }

        if (embeddings != null) {
            // Same-model vector PASSTHROUGH (nexus-hxry2): store the supplied vectors
            // verbatim, no embedder call (token usage 0). Dimension is validated
            // against the dispatched table inside the repository (fail loud).
            // force_re_embed is irrelevant here (see javadoc above) — never threaded
            // into upsertChunksWithVectors, which has no such parameter.
            if (embeddings.size() != ids.size()) {
                throw new IllegalArgumentException(
                        "embeddings length " + embeddings.size() + " != ids length " + ids.size());
            }
            repo.upsertChunksWithVectors(tenant, collection, ids, documents, embeddings, metadatas);
            emitTokenUsage(ex, 0L);
            HttpUtil.send(ex, 200, json(Map.of("upserted", ids.size(), "tokens", 0)));
            return;
        }

        var upsertResult = repo.upsertChunksWithTokens(
                tenant, collection, ids, documents, metadatas, forceReEmbed);
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
        sendSearchResult(ex, body, queryText, searchResult);
    }

    /**
     * Shared tail of the five search handlers: emit the query-embedding token
     * count (bead nexus-ehc4q), then either the bare-array envelope (unchanged
     * legacy shape) or — when the caller opted in with {@code rerank=true} —
     * the {@link RerankStage} object envelope (RDR-188 bead nexus-9o6y2.2).
     * A {@code rerank_top_k} without {@code rerank=true} is a caller error
     * (400), not a silently ignored field.
     */
    private void sendSearchResult(HttpExchange ex, Map<String, Object> body, String queryText,
                                  PgVectorRepository.Tokened<List<Map<String, Object>>> result)
            throws IOException {
        emitTokenUsage(ex, result.tokens());
        boolean rerank     = optBool(body, "rerank", false);
        Integer rerankTopK = optInteger(body, "rerank_top_k");
        if (!rerank && rerankTopK != null) {
            throw new IllegalArgumentException(
                    "rerank_top_k requires \"rerank\": true — set both or neither");
        }
        Object payload = rerank ? rerankStage.apply(queryText, result.value(), rerankTopK)
                                : result.value();
        HttpUtil.send(ex, 200, json(payload));
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
        sendSearchResult(ex, body, queryText, hybridResult);
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
        sendSearchResult(ex, body, queryText, metaResult);
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
        sendSearchResult(ex, body, queryText, topicResult);
    }

    /**
     * POST /v1/vectors/search-graph-hop (RDR-156 P4 follow-on, Decision 5, bead nexus-houg9).
     *
     * <p>The combined graph-hop query that retires the {@code query} tool's
     * {@code follow_links} app-side BFS dance. Request:
     * {@code {"query": "...", "seeds": [...], "collections": [...], "link_type": "cites",
     * "depth": 1, "direction": "both", "where": {...}, "n_results": 10}} — link_type may
     * be omitted (follow all edge types); direction defaults to "both"; depth defaults
     * to 1; where (bead nexus-7ndh3) is an optional chunk-metadata equality map applied
     * as JSONB containment, same shape as {@code /search-metadata-scoped}'s.
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
        Map<String, Object> where = optMap(body, "where");
        int nResults             = optInt(body, "n_results", 10);

        var graphResult = repo.searchGraphHopWithTokens(
            tenant, queryText, seeds, collections, linkType, depth, direction, where, nResults);
        sendSearchResult(ex, body, queryText, graphResult);
    }

    /**
     * POST /v1/vectors/search-aspect-scoped (RDR-156 Decision 5, bead nexus-ubnwk).
     *
     * <p>The combined aspect-filtered query that retires the {@code search} +
     * {@code operator_filter(source="aspects")} app-side two-step for the case where
     * the aspect predicate is selective. Request:
     * {@code {"query": "...", "collections": [...], "field": "proposed_method",
     * "pattern": "gradient descent", "min_confidence": 0.5, "where": {...},
     * "n_results": 10}} — field/pattern/min_confidence/where may all be omitted (no
     * filter on that dimension). {@code field}, when present, must be one of
     * {@link PgVectorRepository#ASPECT_SCOPED_FIELD_ALLOWLIST} — an unrecognized value
     * 400s here, before the SQL function is ever called (the function's own CASE
     * fallthrough is a second, independent line of defense, not the primary contract).
     * Returns document-level rows, same envelope as {@code /search-metadata-scoped}.
     */
    private void handleSearchAspectScoped(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String queryText         = requireString(body, "query");
        List<String> collections = requireStringList(body, "collections");
        String field             = optString(body, "field");
        String pattern           = optString(body, "pattern");
        Double minConfidence     = optDoubleOrNull(body, "min_confidence");
        Map<String, Object> where = optMap(body, "where");
        int nResults              = optInt(body, "n_results", 10);
        if (field != null && !PgVectorRepository.ASPECT_SCOPED_FIELD_ALLOWLIST.contains(field)) {
            throw new IllegalArgumentException(
                "unknown aspect field '" + field + "' - must be one of "
                + PgVectorRepository.ASPECT_SCOPED_FIELD_ALLOWLIST);
        }

        var aspectResult = repo.searchAspectScopedWithTokens(
            tenant, queryText, collections, field, pattern, minConfidence, where, nResults);
        sendSearchResult(ex, body, queryText, aspectResult);
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
        // Deliberate: 10 is a genuine page-size default for this where-filtered
        // scan (not a truncation bug — see handleStoreGet's ids-branch default
        // below, nexus-hdx2u E1).
        int limit                      = optInt(body, "limit", 10);
        int offset                     = optInt(body, "offset", 0);
        boolean includeSourceUri       = optBool(body, "include_source_uri", false);

        var result = repo.getWhere(tenant, collection, where, limit, offset, includeSourceUri);
        HttpUtil.send(ex, 200, json(result));
    }

    /**
     * POST /v1/vectors/get-all-metadata (nexus-duoak follow-up).
     *
     * <p>ids + metadata for EVERY chunk in a collection in ONE round trip —
     * collapses the ``ceil(chunk_count / 300)`` client round trips the
     * indexer's staleness-cache-build phase otherwise pays through paginated
     * {@code /v1/vectors/get} calls. No {@code documents} field (staleness
     * only needs metadata) and no pagination — see
     * {@link PgVectorRepository#getAllMetadata}.
     *
     * <p>Request: {"collection": "...", "where": {...}}  (where optional)
     * <p>Response 200: {"ids": [...], "metadatas": [...]}
     * <p>Response 422: row count exceeds {@link PgVectorRepository#GET_ALL_METADATA_MAX_ROWS}
     *   — caller falls back to paginated {@code /get}.
     */
    private void handleGetAllMetadata(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection         = requireString(body, "collection");
        Map<String, Object> where = optMap(body, "where");

        var result = repo.getAllMetadata(tenant, collection, where);
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
     *   "limit":      20,              // optional; default ids.size() when ids present (nexus-hdx2u E1), else 20
     *   "offset":     0               // optional, default 0
     * }
     * </pre>
     *
     * <p>Response 200: {"ids":[...], "documents":[...], "metadatas":[...]}
     *
     * <p><strong>ids-branch cap (nexus-hdx2u E2):</strong> {@code ids} is capped at
     * {@value #MAX_BATCH_IDS} — 400 on oversize — same rationale and value as
     * {@code CatalogHandler.MAX_BATCH_DOC_IDS} (well under PostgreSQL's 32767-parameter
     * Bind-message hard limit).
     */
    private void handleStoreGet(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection  = requireString(body, "collection");
        List<String> ids   = optStringList(body, "ids");
        // nexus-hdx2u E1: when ids is present, an ABSENT limit defaults to
        // ids.size() — a get-these-specific-rows fetch means "give me all N I
        // asked for", not an implicit page size. The historical fixed default
        // (20) silently truncated any larger batch with no signal (the client
        // half of this bead, C1, made the identical fix on the Python stub's
        // own separate default). The no-ids (paginated where-scan) branch
        // keeps a real page-size default — same as handleStoreList below.
        int defaultLimit   = (ids != null) ? ids.size() : 20;
        int limit          = optInt(body, "limit", defaultLimit);
        int offset         = optInt(body, "offset", 0);
        boolean includeSourceUri = optBool(body, "include_source_uri", false);

        if (ids != null && ids.size() > MAX_BATCH_IDS) {
            HttpUtil.send(ex, 400, "{\"error\":\"too many ids (max " + MAX_BATCH_IDS + ")\"}");
            return;
        }

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
     * <p>ANTI-JOIN SCOPED (RDR-191 F10c fix, bead nexus-o8dil.5): {@link
     * PgVectorRepository#delete} now skips any id still referenced by a live
     * {@code catalog_document_chunks} row in this collection — a chunk another
     * document's manifest still points at survives, silently, rather than being
     * destroyed and leaving that document's manifest dangling. Callers remain
     * responsible for removing their OWN document's manifest rows; this only
     * protects chunks OTHER documents still reference. See {@link
     * PgVectorRepository#delete}'s javadoc for the full rationale.
     *
     * <p>Request: {"collection": "...", "ids": ["...", ...]}
     * <p>Response 200: {"deleted": N} — rows ACTUALLY deleted (RLS makes foreign
     * tenants' rows invisible, and a still-referenced id is silently skipped rather
     * than counted, so this can be less than {@code ids.length} even with no
     * cross-tenant ids present)
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
     * <p>Response 200: {"updated": N, "missing": [ids...]}. {@code missing} names the
     * ids that had no matching row (nexus-5xn3k.2, memo §3.6, AC6 engine half): a stale
     * {@code existing_ids} probe on the client's {@code _upsert_skip_reembed} path routes
     * an id here believing it already has a stored vector — when that belief is wrong the
     * client must be able to re-route the id through a full upsert instead of silently
     * losing content. Detection lives here; the client-side reroute is a separate bead.
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
        var outcome = repo.updateMetadataWithMissing(tenant, collection, ids, metadatas);
        HttpUtil.send(ex, 200, json(Map.of("updated", outcome.updated(), "missing", outcome.missing())));
    }

    /**
     * POST /v1/vectors/gc/quarantine-orphans (RDR-191 Phase 1)
     *
     * <p>Server-side anti-join move: every chunk in {@code collection} whose
     * chash has no {@code catalog_document_chunks} row moves to
     * {@code quarantine_collection}. Zero chunk rows / embeddings cross the
     * wire — the response carries only the moved count and a capped sample.
     *
     * <p>Request:
     * <pre>
     * {
     *   "collection": "code__owner__voyage-code-3__v1",
     *   "quarantine_collection": "quarantine-code__owner__voyage-code-3__v1",
     *   "quarantined_at": "2026-08-10T12:00:00Z",
     *   "sample_limit": 20
     * }
     * </pre>
     * <p>Response 200: {"moved": N, "sample": [{"chash": "...", "title": "..."}, ...]}
     */
    private void handleGcQuarantineOrphans(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String collection           = requireString(body, "collection");
        String quarantineCollection = requireString(body, "quarantine_collection");
        String quarantinedAt        = requireString(body, "quarantined_at");
        // nexus-0uuit/sybbh crit-fix critique 2026-08-19 (code-review-expert):
        // sample_limit was caller-supplied with no upper bound, unlike every
        // other gc_audit producer -- clamp server-side to the same forensic
        // UPPER BOUND gc_audit's writers already honor (defense in depth:
        // the SQL side, nexus.gc_quarantine_orphans / catalog-033-2, also
        // enforces this ceiling independently, AND floors a negative value
        // to 0 -- something this clamp alone does not do, see
        // #clampSampleLimit's javadoc).
        int sampleLimit = clampSampleLimit(optInt(body, "sample_limit", 20));

        var outcome = repo.quarantineOrphans(tenant, collection, quarantineCollection, quarantinedAt, sampleLimit);
        HttpUtil.send(ex, 200, json(Map.of("moved", outcome.moved(), "sample", outcome.sample())));
    }

    /**
     * Upper-bound clamp for {@code handleGcQuarantineOrphans}'s caller-supplied
     * {@code sample_limit} (nexus-0uuit/sybbh crit-fix critique 2026-08-19, round 2:
     * code-review-expert found the inline {@code Math.min} had zero direct coverage —
     * the one existing test, {@code CatalogGcAuditProducersTest
     * #quarantineOrphans_oversizedSampleLimitRequest_clampsAndFlagsTruncation}, calls
     * {@link PgVectorRepository#quarantineOrphans} directly and only exercises the
     * SQL-side clamp, redundantly re-capping at the identical ceiling — a revert of
     * THIS method would go undetected by any prior test). Extracted to a
     * package-private static method so it is directly unit-testable without an HTTP
     * round trip or a database: see {@code VectorHandlerSampleLimitClampTest}.
     *
     * <p>UPPER-BOUND ONLY, by design, not full parity with the SQL clamp: unlike
     * {@code nexus.gc_quarantine_orphans}'s own {@code LEAST(GREATEST(...,0),5000)}
     * (catalog-033-2), this does not floor a negative {@code requested} value to 0 —
     * {@code Math.min(-1, GC_AUDIT_MAX_CHASHES)} returns {@code -1} unchanged. That is
     * safe end-to-end ONLY because the SQL function's own independent floor is what
     * actually neutralizes a negative value before it reaches {@code LIMIT}; a future
     * caller of this method for some OTHER purpose must not assume it floors.
     */
    static int clampSampleLimit(int requested) {
        return Math.min(requested, dev.nexus.service.db.CatalogRepository.GC_AUDIT_MAX_CHASHES);
    }

    /**
     * POST /v1/vectors/gc/restore-rereferenced (RDR-191 Phase 1)
     *
     * <p>Request: {"quarantine_collection": "...", "origin_collection": "..."}
     * <p>Response 200: {"restored": N}
     */
    private void handleGcRestoreRereferenced(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String quarantineCollection = requireString(body, "quarantine_collection");
        String originCollection     = requireString(body, "origin_collection");

        long restored = repo.restoreRereferenced(tenant, quarantineCollection, originCollection);
        HttpUtil.send(ex, 200, json(Map.of("restored", restored)));
    }

    /**
     * POST /v1/vectors/gc/expire-quarantine (RDR-191 Phase 1)
     *
     * <p>Request:
     * <pre>
     * {
     *   "quarantine_collection": "...",
     *   "origin_collection": "...",
     *   "cutoff": "2026-07-27T12:00:00Z",
     *   "floor_fraction": 0.5,
     *   "floor_min_chunks": 50,
     *   "force": false
     * }
     * </pre>
     * <p>Response 200: {"expired": N, "refused": M} — the nexus-mr89x safety
     * floor (see catalog-023 changelog): {@code refused &gt; 0} means the
     * floor fired and nothing was deleted this call.
     */
    private void handleGcExpireQuarantine(HttpExchange ex, String method) throws IOException {
        requireMethod(ex, method, "POST");
        var repo   = requirePgRepo(ex);
        var tenant = requireTenant(ex);
        Map<String, Object> body = readBody(ex);
        String quarantineCollection = requireString(body, "quarantine_collection");
        String originCollection     = requireString(body, "origin_collection");
        String cutoff                = requireString(body, "cutoff");
        double floorFraction        = optDouble(body, "floor_fraction", 0.5);
        int floorMinChunks          = optInt(body, "floor_min_chunks", 50);
        boolean force                = optBool(body, "force", false);

        var outcome = repo.expireQuarantine(tenant, quarantineCollection, originCollection,
                cutoff, floorFraction, floorMinChunks, force);
        HttpUtil.send(ex, 200, json(Map.of("expired", outcome.expired(), "refused", outcome.refused())));
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

    /** Optional double field; absent or null → {@code defaultValue}. */
    private double optDouble(Map<String, Object> body, String key, double defaultValue) {
        Object val = body.get(key);
        if (val == null) return defaultValue;
        if (val instanceof Number n) return n.doubleValue();
        try { return Double.parseDouble(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be a number");
        }
    }

    /** Optional double field; null → null (no-filter semantics, e.g. min_confidence). */
    private Double optDoubleOrNull(Map<String, Object> body, String key) {
        Object val = body.get(key);
        if (val == null) return null;
        if (val instanceof Number n) return n.doubleValue();
        try { return Double.parseDouble(val.toString()); }
        catch (NumberFormatException e) {
            throw new IllegalArgumentException("field '" + key + "' must be a number");
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
