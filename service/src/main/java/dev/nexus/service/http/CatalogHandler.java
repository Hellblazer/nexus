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
import dev.nexus.service.db.CatalogRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.util.*;

/**
 * RDR-152 bead nexus-gmiaf.18 — Catalog HTTP endpoints.
 *
 * <p>Mirrors the full surface of the Python catalog MCP tools:
 * catalog_register, catalog_show, catalog_list, catalog_search,
 * catalog_update, catalog_link, catalog_links, catalog_link_query,
 * catalog_resolve, catalog_stats, catalog_unlink, catalog_link_bulk.
 *
 * <p>Route table (all under {@code /v1/catalog/}):
 * <pre>
 *   POST  /v1/catalog/register           upsert owner + document
 *   GET   /v1/catalog/show               get document by tumbler (or title)
 *   GET   /v1/catalog/list               list documents (paginated)
 *   GET   /v1/catalog/search             FTS search
 *   POST  /v1/catalog/update             update document fields
 *   DELETE /v1/catalog/delete            delete document by tumbler
 *   POST  /v1/catalog/link               upsert link
 *   POST  /v1/catalog/unlink             delete link
 *   GET   /v1/catalog/links              links from/to tumbler (BFS optional)
 *   GET   /v1/catalog/link_query         paginated link query with filters
 *   GET   /v1/catalog/resolve            resolve doc by file_path / source_uri / title
 *   GET   /v1/catalog/stats              per-tenant catalog statistics
 *   POST  /v1/catalog/traverse           BFS graph traversal
 *   POST  /v1/catalog/manifest/write     replace manifest
 *   POST  /v1/catalog/manifest/append    append chunks
 *   POST  /v1/catalog/manifest/write_many batch replace manifests for multiple docs (+chunk_count)
 *   GET   /v1/catalog/manifest/get       get manifest for doc_id
 *   POST  /v1/catalog/manifest/get_many  batch-fetch manifests for multiple doc_ids (nexus-7lm3q)
 *   POST  /v1/catalog/manifest/purge     purge manifest for doc_id
 *   GET   /v1/catalog/manifest/chashes   chashes for collection
 *   POST  /v1/catalog/manifest/resync    recompute chunk_count from manifest row count
 *   POST  /v1/catalog/resolve_many       batch-resolve multiple doc_ids to entries (nexus-7lm3q)
 *   POST  /v1/catalog/owners/upsert      upsert owner
 *   GET   /v1/catalog/owners/list        list all owners
 *   GET   /v1/catalog/owners/by_repo     get owner by repo_hash
 *   POST  /v1/catalog/collections/upsert upsert collection
 *   GET   /v1/catalog/collections/list   list collections
 *   GET   /v1/catalog/collections/get    get collection by name
 *   POST  /v1/catalog/collections/supersede supersede collection
 *   POST  /v1/catalog/collections/rename rename collection (cascade)
 *   POST  /v1/catalog/collections/delete delete collection + cascade all in-PG lifecycle state (RDR-164 P2)
 *   GET   /v1/catalog/coverage            link coverage by content type (nexus-3cwnx)
 *   POST  /v1/catalog/import/owner       ETL import owner
 *   POST  /v1/catalog/import/document    ETL import document
 *   POST  /v1/catalog/import/link        ETL import link
 *   POST  /v1/catalog/import/chunk       ETL import chunk
 *   POST  /v1/catalog/import/collection  ETL import collection
 * </pre>
 *
 * <p>All endpoints require {@code Authorization: Bearer <token>} (enforced by
 * {@link AuthFilter}) and {@code X-Nexus-Tenant} header.
 *
 * <p>All request/response bodies are JSON. Errors return
 * {@code {"error":"<message>"}} with appropriate HTTP status.
 */
public final class CatalogHandler implements HttpHandler {

    private static final Logger log = LoggerFactory.getLogger(CatalogHandler.class);

    static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
            .setSerializationInclusion(JsonInclude.Include.ALWAYS);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /**
     * Upper bound on doc_ids accepted by the batch endpoints
     * ({@code /manifest/get_many}, {@code /resolve_many}). Well under
     * PostgreSQL's 32767-parameter Bind-message hard limit. nexus-7lm3q review.
     */
    private static final int MAX_BATCH_DOC_IDS = 1000;

    private final CatalogRepository repo;

    public CatalogHandler(CatalogRepository repo) {
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
        String op     = path.replaceFirst("^/v1/catalog", "");
        String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);

        try {
            switch (op) {
                // ── Documents ─────────────────────────────────────────────────
                case "/register"              -> handleRegister(exchange, tenant, method);
                case "/show"                  -> handleShow(exchange, tenant, method);
                case "/list"                  -> handleList(exchange, tenant, method);
                case "/search"                -> handleSearch(exchange, tenant, method);
                case "/update"                -> handleUpdate(exchange, tenant, method);
                case "/delete"                -> handleDelete(exchange, tenant, method);
                case "/resolve"               -> handleResolve(exchange, tenant, method);
                case "/stats"                 -> handleStats(exchange, tenant, method);

                // ── Links ─────────────────────────────────────────────────────
                case "/link"                  -> handleLink(exchange, tenant, method);
                case "/unlink"                -> handleUnlink(exchange, tenant, method);
                case "/links"                 -> handleLinks(exchange, tenant, method);
                case "/link_query"            -> handleLinkQuery(exchange, tenant, method);
                case "/traverse"              -> handleTraverse(exchange, tenant, method);

                // ── Manifest ──────────────────────────────────────────────────
                case "/manifest/write"        -> handleManifestWrite(exchange, tenant, method);
                case "/manifest/append"       -> handleManifestAppend(exchange, tenant, method);
                case "/manifest/write_many"   -> handleManifestWriteMany(exchange, tenant, method);
                case "/manifest/get"          -> handleManifestGet(exchange, tenant, method);
                case "/manifest/get_many"     -> handleManifestGetMany(exchange, tenant, method);
                case "/manifest/purge"        -> handleManifestPurge(exchange, tenant, method);
                case "/manifest/chashes"      -> handleManifestChashes(exchange, tenant, method);
                case "/manifest/docs_for_chashes" -> handleDocsForChashes(exchange, tenant, method);
                case "/manifest/resync"       -> handleManifestResync(exchange, tenant, method);
                case "/manifest/backfill"     -> handleManifestBackfill(exchange, tenant, method);
                case "/manifest/orphans"      -> handleManifestOrphans(exchange, tenant, method);

                // ── Owners ────────────────────────────────────────────────────
                case "/owners/upsert"         -> handleOwnerUpsert(exchange, tenant, method);
                case "/owners/list"           -> handleOwnerList(exchange, tenant, method);
                case "/owners/by_repo"        -> handleOwnerByRepo(exchange, tenant, method);
                case "/owners/by_name"        -> handleOwnerByName(exchange, tenant, method);
                case "/owners/head_hash"      -> handleOwnerHeadHash(exchange, tenant, method);
                case "/owners/show"           -> handleOwnerShow(exchange, tenant, method);
                case "/owners/by_type"        -> handleOwnerByType(exchange, tenant, method);

                // ── Collections ───────────────────────────────────────────────
                case "/collections/upsert"    -> handleCollectionUpsert(exchange, tenant, method);
                case "/collections/list"      -> handleCollectionList(exchange, tenant, method);
                case "/collections/get"       -> handleCollectionGet(exchange, tenant, method);
                case "/collections/supersede" -> handleCollectionSupersede(exchange, tenant, method);
                case "/collections/rename"    -> handleCollectionRename(exchange, tenant, method);
                case "/collections/delete"    -> handleCollectionDelete(exchange, tenant, method);
                case "/collections/for_tuple" -> handleCollectionForTuple(exchange, tenant, method);
                case "/collections/health"    -> handleCollectionHealth(exchange, tenant, method);

                // ── ETL imports ───────────────────────────────────────────────
                case "/import/owner"          -> handleImportOwner(exchange, tenant, method);
                case "/import/document"       -> handleImportDocument(exchange, tenant, method);
                case "/import/link"           -> handleImportLink(exchange, tenant, method);
                case "/import/chunk"          -> handleImportChunk(exchange, tenant, method);
                case "/import/collection"     -> handleImportCollection(exchange, tenant, method);

                // ── Coverage analytics (nexus-3cwnx) ──────────────────────────
                case "/coverage"                  -> handleCoverage(exchange, tenant, method);

                // ── Analytics queries (nexus-xnz0o CLI port helpers) ─────────
                case "/docs/distinct-collections" -> handleDocsDistinctCollections(exchange, tenant, method);
                case "/docs/collection-counts"    -> handleDocsCollectionCounts(exchange, tenant, method);
                case "/docs/orphaned"             -> handleDocsOrphaned(exchange, tenant, method);
                case "/docs/absolute-paths"       -> handleDocsAbsolutePaths(exchange, tenant, method);
                case "/owners/all-with-roots"     -> handleOwnersWithRoots(exchange, tenant, method);
                case "/collections/owner-root"    -> handleCollectionOwnerRoot(exchange, tenant, method);

                // ── Scoring hot-path batch endpoints (nexus-qnp5s) ───────────
                case "/docs/chunk-counts"     -> handleDocChunkCounts(exchange, tenant, method);
                case "/links/from-batch"      -> handleLinksFromBatch(exchange, tenant, method);

                // ── Batch resolve endpoints (nexus-7lm3q) ────────────────────
                case "/resolve_many"          -> handleResolveMany(exchange, tenant, method);

                // ── Span / chash resolution (nexus-njrcn.4) ──────────────────
                case "/resolve_span"          -> handleResolveSpan(exchange, tenant, method);
                case "/resolve_chash"         -> handleResolveChash(exchange, tenant, method);
                case "/resolve_chunk"         -> handleResolveChunk(exchange, tenant, method);

                // ── Server-side tumbler assignment ────────────────────────────
                case "/doc/register"          -> handleDocRegister(exchange, tenant, method);
                case "/doc/register_many"     -> handleRegisterMany(exchange, tenant, method);

                // ── Migration count verification (RDR-159 P-1a) ───────────────
                case "/verify/relation-counts" -> handleRelationCounts(exchange, tenant, method);

                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found: " + op + "\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            // Shared typed-DB-error ladder: pool-exhaustion 503 + class-23 409
            // (nexus-h8rf6.2 / nexus-7e057) — see HttpUtil.sendTypedDbError.
            if (!HttpUtil.sendTypedDbError(exchange, e, log, "catalog_handler",
                    "op=" + op + " tenant=" + tenant)) {
                log.error("event=catalog_handler_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
                HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // DOCUMENTS
    // ══════════════════════════════════════════════════════════════════════════

    /** POST /v1/catalog/register — upsert owner + document row. */
    private void handleRegister(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        // body may contain both owner fields and document fields; repo handles the split
        // Owner upsert (if tumbler_prefix present)
        if (body.containsKey("tumbler_prefix")) {
            repo.upsertOwner(tenant, body);
        }
        // Document upsert (if tumbler present)
        if (body.containsKey("tumbler")) {
            repo.upsertDocument(tenant, body);
        }
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
    }

    /** GET /v1/catalog/show?tumbler=<t> — get document by tumbler. */
    private void handleShow(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String tumbler = queryParam(exchange, "tumbler");
        if (tumbler == null || tumbler.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler query param required\"}"); return;
        }
        var doc = repo.getDocument(tenant, tumbler);
        if (doc == null) {
            HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return;
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(doc));
    }

    /** GET /v1/catalog/list?limit=N&offset=N&content_type=X&collection=X&corpus=X&owner=X */
    private void handleList(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        int limit  = intParam(exchange, "limit",  200);
        int offset = intParam(exchange, "offset", 0);

        // Optional filter dispatching
        String collection  = queryParam(exchange, "collection");
        String contentType = queryParam(exchange, "content_type");
        String corpus      = queryParam(exchange, "corpus");
        String owner       = queryParam(exchange, "owner");
        String filePath    = queryParam(exchange, "file_path");
        String sourceUri   = queryParam(exchange, "source_uri");

        List<Map<String, Object>> docs;
        if (collection != null && !collection.isBlank()) {
            docs = repo.documentsByCollection(tenant, collection);
        } else if (contentType != null && !contentType.isBlank()) {
            docs = repo.documentsByContentType(tenant, contentType);
        } else if (corpus != null && !corpus.isBlank()) {
            docs = repo.documentsByCorpus(tenant, corpus);
        } else if (owner != null && !owner.isBlank()
                   && filePath != null && !filePath.isBlank()) {
            // GH #1350 Fix B: owner+file_path must filter by BOTH. The owner-only
            // branch below ignored file_path and returned the full owner list,
            // driving the client's docs[0] mis-attribution (silent corruption).
            docs = repo.documentsByOwnerAndFilePath(tenant, owner, filePath);
        } else if (owner != null && !owner.isBlank()) {
            docs = repo.documentsByOwner(tenant, owner);
        } else if (filePath != null && !filePath.isBlank()) {
            docs = repo.documentsByFilePath(tenant, filePath);
        } else if (sourceUri != null && !sourceUri.isBlank()) {
            docs = repo.documentsBySourceUri(tenant, sourceUri);
        } else {
            docs = repo.listDocuments(tenant, limit, offset);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("documents", docs, "count", docs.size())));
    }

    /** GET /v1/catalog/search?q=X&content_type=X&limit=N */
    private void handleSearch(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String q           = queryParam(exchange, "q");
        String contentType = queryParam(exchange, "content_type");
        int limit          = intParam(exchange, "limit", 50);
        if (q == null || q.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"q query param required\"}"); return;
        }
        var docs = repo.searchDocuments(tenant, q, contentType, limit);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("documents", docs, "count", docs.size())));
    }

    /** POST /v1/catalog/update — update mutable document fields. */
    private void handleUpdate(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String tumbler = (String) body.get("tumbler");
        if (tumbler == null || tumbler.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'tumbler' required\"}"); return;
        }
        Map<String, Object> fields = new LinkedHashMap<>(body);
        fields.remove("tumbler");
        int updated = repo.updateDocument(tenant, tumbler, fields);
        HttpUtil.send(exchange, 200, "{\"updated\":" + updated + "}");
    }

    /** DELETE /v1/catalog/delete?tumbler=X */
    private void handleDelete(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"DELETE".equals(method) && !"POST".equals(method)) {
            HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return;
        }
        String tumbler = queryParam(exchange, "tumbler");
        if (tumbler == null || tumbler.isBlank()) {
            // Try body
            Map<String, Object> body = readBody(exchange);
            tumbler = (String) body.get("tumbler");
        }
        if (tumbler == null || tumbler.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'tumbler' required\"}"); return;
        }
        int deleted = repo.deleteDocument(tenant, tumbler);
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    /** GET /v1/catalog/resolve?file_path=X or ?source_uri=X or ?title=X&collection=X */
    private void handleResolve(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String filePath   = queryParam(exchange, "file_path");
        String sourceUri  = queryParam(exchange, "source_uri");
        String collection = queryParam(exchange, "collection");
        String title      = queryParam(exchange, "title");

        List<Map<String, Object>> docs;
        if (filePath != null && !filePath.isBlank() && collection != null && !collection.isBlank()) {
            String tumbler = repo.lookupDocByCollectionAndPath(tenant, collection, filePath);
            if (tumbler == null) {
                HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return;
            }
            var doc = repo.getDocument(tenant, tumbler);
            docs = doc != null ? List.of(doc) : List.of();
        } else if (filePath != null && !filePath.isBlank()) {
            docs = repo.documentsByFilePath(tenant, filePath);
        } else if (sourceUri != null && !sourceUri.isBlank()) {
            docs = repo.documentsBySourceUri(tenant, sourceUri);
        } else if (title != null && !title.isBlank()) {
            docs = repo.searchDocuments(tenant, title, null, 10);
        } else {
            HttpUtil.send(exchange, 400, "{\"error\":\"file_path, source_uri, or title required\"}"); return;
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("documents", docs)));
    }

    /** GET /v1/catalog/stats */
    private void handleStats(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var stats = repo.stats(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(stats));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // LINKS
    // ══════════════════════════════════════════════════════════════════════════

    /** POST /v1/catalog/link — upsert link. */
    private void handleLink(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        boolean created = repo.upsertLink(tenant, body);
        HttpUtil.send(exchange, 200, "{\"ok\":true,\"created\":" + created + "}");
    }

    /** POST /v1/catalog/unlink — delete link(s). */
    private void handleUnlink(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String fromT    = (String) body.get("from_tumbler");
        String toT      = (String) body.get("to_tumbler");
        String linkType = (String) body.get("link_type");
        int deleted;
        if (fromT != null && toT != null && linkType != null) {
            deleted = repo.deleteLink(tenant, fromT, toT, linkType);
        } else {
            // Bulk delete
            String createdBy       = (String) body.get("created_by");
            String createdAtBefore = (String) body.get("created_at_before");
            deleted = repo.bulkDeleteLinks(tenant, fromT, toT, linkType, createdBy, createdAtBefore);
        }
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    /**
     * GET /v1/catalog/links?tumbler=X&direction=out|in|both&link_type=X
     *
     * <p>Returns direct neighbors of the tumbler (depth=1) in the given direction.
     * Used by catalog_links MCP tool.
     */
    private void handleLinks(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String tumbler   = queryParam(exchange, "tumbler");
        String direction = queryParam(exchange, "direction");
        String linkType  = queryParam(exchange, "link_type");
        // RDR-168 njrcn.5: optional comma-separated link_types for a server-side IN filter
        // (multi-type callers no longer fetch every edge and filter client-side). link_types
        // takes precedence; falls back to the single link_type; null = no type filter.
        String linkTypesRaw = queryParam(exchange, "link_types");
        List<String> linkTypes = null;
        if (linkTypesRaw != null && !linkTypesRaw.isBlank()) {
            linkTypes = java.util.Arrays.stream(linkTypesRaw.split(","))
                .map(String::trim).filter(s -> !s.isEmpty()).toList();
        } else if (linkType != null && !linkType.isBlank()) {
            linkTypes = List.of(linkType);
        }
        if (tumbler == null || tumbler.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler query param required\"}"); return;
        }
        if (direction == null) direction = "both";

        List<Map<String, Object>> linksFrom = List.of();
        List<Map<String, Object>> linksTo   = List.of();
        if ("out".equals(direction) || "both".equals(direction)) {
            linksFrom = repo.linksFrom(tenant, tumbler, linkTypes);
        }
        if ("in".equals(direction) || "both".equals(direction)) {
            linksTo = repo.linksTo(tenant, tumbler, linkTypes);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("links_from", linksFrom, "links_to", linksTo)));
    }

    /**
     * GET /v1/catalog/link_query?from_tumbler=X&to_tumbler=X&link_type=X
     *                             &created_by=X&limit=N&offset=N&created_at_before=ISO
     *                             &direction=out|in|both&tumbler=X
     */
    private void handleLinkQuery(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String fromT           = queryParam(exchange, "from_tumbler");
        String toT             = queryParam(exchange, "to_tumbler");
        String linkType        = queryParam(exchange, "link_type");
        String createdBy       = queryParam(exchange, "created_by");
        String createdAtBefore = queryParam(exchange, "created_at_before");
        String direction       = queryParam(exchange, "direction");
        String tumbler         = queryParam(exchange, "tumbler");
        int limit              = intParam(exchange, "limit",  50);
        int offset             = intParam(exchange, "offset", 0);
        var links = repo.queryLinks(tenant, fromT, toT, linkType, createdBy, createdAtBefore, limit, offset,
                                    direction, tumbler);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("links", links, "count", links.size())));
    }

    /**
     * POST /v1/catalog/traverse — BFS graph traversal.
     *
     * <p>Request: {"seeds": [...], "link_types": [...], "direction": "both", "depth": 1}
     * Response: {"nodes": [...], "edges": [...]}
     */
    @SuppressWarnings("unchecked")
    private void handleTraverse(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawSeeds = body.get("seeds");
        List<String> seeds = rawSeeds instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        Object rawTypes = body.get("link_types");
        List<String> linkTypes = rawTypes instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        String direction = (String) body.getOrDefault("direction", "both");
        int depth = body.get("depth") instanceof Number n ? n.intValue() : 1;
        var result = repo.graphBFS(tenant, seeds, linkTypes, direction, depth);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // MANIFEST
    // ══════════════════════════════════════════════════════════════════════════

    /** POST /v1/catalog/manifest/write — replace manifest (atomic delete + insert). */
    @SuppressWarnings("unchecked")
    private void handleManifestWrite(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        List<Map<String, Object>> rows = castRows(body.get("rows"));
        repo.writeManifest(tenant, docId, rows);
        HttpUtil.send(exchange, 200, "{\"ok\":true,\"count\":" + rows.size() + "}");
    }

    /** POST /v1/catalog/manifest/append */
    private void handleManifestAppend(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        List<Map<String, Object>> rows = castRows(body.get("rows"));
        repo.appendManifestChunks(tenant, docId, rows);
        HttpUtil.send(exchange, 200, "{\"ok\":true,\"count\":" + rows.size() + "}");
    }

    /**
     * POST /v1/catalog/manifest/write_many (bead nexus-u2kwq).
     *
     * <p>Body {@code {"docs": [{"doc_id": "...", "rows": [<same row shape as
     * /manifest/write>]}, ...]}}. Each doc is REPLACED in its own transaction
     * (delete all rows + insert + set documents.chunk_count = rows.size();
     * per-doc atomicity, cross-doc isolation) via
     * {@link CatalogRepository#writeManifestMany}. Cap {@value #MAX_BATCH_DOC_IDS}
     * docs. Response 200 {@code {docs: N_ok, rows: M_total, failed_doc_ids: [...]}}.
     */
    @SuppressWarnings("unchecked")
    private void handleManifestWriteMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("docs");
        // Review #2: malformed shapes must 400, not no-op as a false 200
        // (mirrors handleAssignMany; a wrong key or element type is a
        // client bug and silence would mask it).
        if (!(raw instanceof List<?> l)) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'docs' must be a list\"}"); return;
        }
        if (l.stream().anyMatch(o -> !(o instanceof Map))) {
            HttpUtil.send(exchange, 400, "{\"error\":\"every 'docs' element must be an object\"}"); return;
        }
        List<Map<String, Object>> docs =
            l.stream().map(o -> (Map<String, Object>) o).toList();
        if (docs.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many docs (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var result = repo.writeManifestMany(tenant, docs);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    /** GET /v1/catalog/manifest/get?doc_id=X */
    private void handleManifestGet(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String docId = queryParam(exchange, "doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"doc_id query param required\"}"); return;
        }
        var rows = repo.getManifest(tenant, docId);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("rows", rows, "count", rows.size())));
    }

    /** POST /v1/catalog/manifest/purge */
    private void handleManifestPurge(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        int deleted = repo.purgeManifest(tenant, docId);
        HttpUtil.send(exchange, 200, "{\"deleted\":" + deleted + "}");
    }

    /** GET /v1/catalog/manifest/chashes?collection=X */
    private void handleManifestChashes(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String collection = queryParam(exchange, "collection");
        if (collection == null || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"collection query param required\"}"); return;
        }
        var chashes = repo.chashesForCollection(tenant, collection);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("chashes", new ArrayList<>(chashes))));
    }

    /** POST /v1/catalog/manifest/docs_for_chashes */
    @SuppressWarnings("unchecked")
    private void handleDocsForChashes(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("chashes");
        List<String> chashes = raw instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        var docs = repo.docsForChashes(tenant, chashes);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("tumblers", docs)));
    }

    /**
     * POST /v1/catalog/manifest/get_many (nexus-7lm3q)
     *
     * <p>Batch-fetch manifest rows for multiple doc_ids in a single round-trip,
     * replacing the N per-doc {@code /manifest/get} loop issued by
     * {@code _attach_doc_ids_from_catalog} in {@code search_engine.py}.
     *
     * <p>Request body:  {@code {"doc_ids": ["tumbler1", "tumbler2", ...]}}
     * Response body:   {@code {"manifests": {"tumbler1": [rows...], "tumbler2": [rows...]}}}
     *
     * <p>Doc_ids with no manifest rows are absent from the response map.
     */
    @SuppressWarnings("unchecked")
    private void handleManifestGetMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("doc_ids");
        List<String> docIds = raw instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        if (docIds.isEmpty()) {
            HttpUtil.send(exchange, 200, "{\"manifests\":{}}"); return;
        }
        // nexus-7lm3q review (CR High-2 / critic Sig-1): cap the IN-list well
        // under PostgreSQL's 32767-parameter Bind limit. The sole production
        // caller (search_engine fan-out) is bounded by the 300-result cap and
        // the Python client batches at 500, but the endpoint must not trust the
        // caller — admin tooling / future consumers could submit a larger list.
        if (docIds.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many doc_ids (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var manifests = repo.getManifestMany(tenant, docIds);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("manifests", manifests)));
    }

    /**
     * GET /v1/catalog/resolve_span?span_chash=<hex32>&collection=<name>  (nexus-njrcn.4)
     *
     * <p>Resolves a 32-char chunk chash within a specific collection to its text and
     * metadata. The client parses the full span string client-side and sends only the
     * truncated chash + collection so the server does a simple keyed lookup.
     *
     * <p>Response: {@code {"chunk_text": "...", "metadata": {...}, "chunk_hash": "..."}}
     * or 404 on miss.
     */
    private void handleResolveSpan(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String spanChash = queryParam(exchange, "span_chash");
        String collection = queryParam(exchange, "collection");
        if (spanChash == null || spanChash.isBlank() || collection == null || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"span_chash and collection query params required\"}"); return;
        }
        var result = repo.resolveSpan(tenant, collection, spanChash);
        if (result == null) {
            HttpUtil.send(exchange, 404, "{\"error\":\"chunk not found\"}"); return;
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    /**
     * GET /v1/catalog/resolve_chash?chash=<hex32>[&prefer_collection=<name>]  (nexus-njrcn.4)
     *
     * <p>Globally resolves a 32-char chunk chash (across all dim tables) to its text,
     * metadata, owning collection, and doc_id. Tie-breaks by prefer_collection (if
     * provided) then newest created_at.
     *
     * <p>Response: {@code {"chash": "...", "chunk_hash": "...", "physical_collection": "...",
     * "doc_id": "...", "chunk_text": "...", "metadata": {...}}} or 404 on miss.
     */
    private void handleResolveChash(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String chash = queryParam(exchange, "chash");
        if (chash == null || chash.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"chash query param required\"}"); return;
        }
        String preferCollection = queryParam(exchange, "prefer_collection"); // may be null
        var result = repo.resolveChash(tenant, chash, preferCollection);
        if (result == null) {
            HttpUtil.send(exchange, 404, "{\"error\":\"chunk not found\"}"); return;
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    /**
     * GET /v1/catalog/resolve_chunk?tumbler=<4-segment chunk address> (nexus-gc2ze)
     *
     * <p>Mirrors the local {@code Catalog.resolve_chunk} contract
     * (catalog_docs.py): chunks are implicit addresses — the catalog stores
     * document-level rows only, and chunk sub-addresses are resolved on
     * demand from the document's {@code chunk_count}. Splits the tumbler
     * into its document prefix (first 3 segments) and chunk index (4th
     * segment), then delegates the lookup + range-check to
     * {@link CatalogRepository#resolveChunk}.
     *
     * <p>400 if {@code tumbler} has fewer than 4 segments (not a chunk
     * address) or the 4th segment is not an integer; 404 if the document is
     * missing or the chunk index is out of range.
     *
     * <p>Response: {@code {"document_tumbler", "chunk_index",
     * "physical_collection", "title", "content_type"}}.
     */
    private void handleResolveChunk(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String tumbler = queryParam(exchange, "tumbler");
        if (tumbler == null || tumbler.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler query param required\"}"); return;
        }
        String[] segments = tumbler.split("\\.");
        if (segments.length < 4) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler is not a chunk address (need >= 4 segments)\"}"); return;
        }
        int chunkIndex;
        try {
            chunkIndex = Integer.parseInt(segments[3]);
        } catch (NumberFormatException e) {
            HttpUtil.send(exchange, 400, "{\"error\":\"invalid chunk segment\"}"); return;
        }
        String docTumbler = segments[0] + "." + segments[1] + "." + segments[2];
        var result = repo.resolveChunk(tenant, docTumbler, chunkIndex);
        if (result == null) {
            HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return;
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    /**
     * POST /v1/catalog/resolve_many (nexus-7lm3q)
     *
     * <p>Batch-resolve multiple doc_ids to full document entries in a single
     * round-trip, replacing the N per-doc {@code /show?tumbler=X} calls issued
     * by {@code _attach_display_paths} in {@code search_engine.py}.
     *
     * <p>Request body:  {@code {"doc_ids": ["tumbler1", "tumbler2", ...]}}
     * Response body:   {@code {"entries": {"tumbler1": {doc...}, "tumbler2": {doc...}}}}
     *
     * <p>Doc_ids with no matching document are absent from the response map.
     */
    @SuppressWarnings("unchecked")
    private void handleResolveMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("doc_ids");
        List<String> docIds = raw instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        if (docIds.isEmpty()) {
            HttpUtil.send(exchange, 200, "{\"entries\":{}}"); return;
        }
        // nexus-7lm3q review (CR High-2 / critic Sig-1): see handleManifestGetMany —
        // cap the IN-list under PostgreSQL's 32767-parameter Bind limit.
        if (docIds.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many doc_ids (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var entries = repo.resolveMany(tenant, docIds);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("entries", entries)));
    }

    /**
     * POST /v1/catalog/manifest/resync
     *
     * <p>Recomputes {@code documents.chunk_count} for a given document by counting
     * rows in {@code catalog_document_chunks}.  Fixes the discrepancy that arises
     * when the client-pushed {@code chunk_count} in the upsert is stale or wrong.
     *
     * <p>Request body: {@code {"doc_id": "<tumbler>"}}
     * Response body:   {@code {"updated": <0|1>, "chunk_count": <N>}}
     *
     * <p>Exposes {@link CatalogRepository#resyncChunkCount} over HTTP so the Python
     * client's {@code resync_chunk_count_cache} becomes a real reconciliation call
     * instead of a no-op (bug nexus-0jq9u).
     */
    private void handleManifestResync(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        int updated = repo.resyncChunkCount(tenant, docId);
        var doc = repo.getDocument(tenant, docId);
        int chunkCount = doc != null && doc.get("chunk_count") instanceof Number n
            ? n.intValue() : 0;
        HttpUtil.send(exchange, 200,
            "{\"updated\":" + updated + ",\"chunk_count\":" + chunkCount + "}");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNERS
    // ══════════════════════════════════════════════════════════════════════════

    private void handleOwnerUpsert(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        repo.upsertOwner(tenant, body);
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
    }

    private void handleOwnerList(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var owners = repo.listOwners(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("owners", owners)));
    }

    private void handleOwnerByRepo(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String repoHash = queryParam(exchange, "repo_hash");
        if (repoHash == null || repoHash.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"repo_hash required\"}"); return;
        }
        var owner = repo.ownerByRepoHash(tenant, repoHash);
        if (owner == null) { HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return; }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(owner));
    }

    private void handleOwnerByName(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String name = queryParam(exchange, "name");
        if (name == null || name.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"name required\"}"); return;
        }
        var owners = repo.ownersByName(tenant, name);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("owners", owners)));
    }

    private void handleOwnerHeadHash(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String prefix   = (String) body.get("tumbler_prefix");
        String headHash = (String) body.get("head_hash");
        if (prefix == null || headHash == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler_prefix and head_hash required\"}"); return;
        }
        int updated = repo.setOwnerHeadHash(tenant, prefix, headHash);
        HttpUtil.send(exchange, 200, "{\"updated\":" + updated + "}");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COLLECTIONS
    // ══════════════════════════════════════════════════════════════════════════

    private void handleCollectionUpsert(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        repo.upsertCollection(tenant, body);
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
    }

    private void handleCollectionList(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var colls = repo.listCollections(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("collections", colls)));
    }

    private void handleCollectionGet(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String name = queryParam(exchange, "name");
        if (name == null || name.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"name required\"}"); return;
        }
        var coll = repo.getCollection(tenant, name);
        if (coll == null) { HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return; }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(coll));
    }

    private void handleCollectionSupersede(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String name        = (String) body.get("name");
        String supersededBy = (String) body.get("superseded_by");
        String supersededAt = (String) body.get("superseded_at");
        if (name == null || supersededBy == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"name and superseded_by required\"}"); return;
        }
        int updated = repo.supersedeCollection(tenant, name, supersededBy, supersededAt != null ? supersededAt : "");
        HttpUtil.send(exchange, 200, "{\"updated\":" + updated + "}");
    }

    private void handleCollectionRename(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        // Accept both old_name/new_name (canonical) and old/new (HttpCatalogClient compat)
        String oldName = body.get("old_name") instanceof String s ? s : (String) body.get("old");
        String newName = body.get("new_name") instanceof String s ? s : (String) body.get("new");
        if (oldName == null || newName == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"old_name/new_name (or old/new) required\"}"); return;
        }
        // nexus-hz785: a rename of an unregistered collection used to return 200 with all-zero
        // counts (insert-select copies 0 rows, every child UPDATE touches 0) — a silent no-op on
        // a typo. Fail loud with a legible 404 instead.
        if (!repo.collectionExists(tenant, oldName)) {
            HttpUtil.send(exchange, 404,
                "{\"error\":" + MAPPER.writeValueAsString("collection not found: " + oldName) + "}"); return;
        }
        // nexus-gaou3: if new_name is ALREADY a registered collection, renameCollection silently
        // takes the RDR-162 cross-model COPY branch (repoints catalog_documents ONLY; chunks/
        // taxonomy/aspects are NOT moved). That is correct ONLY for the deliberate cross-model
        // migrate. A plain rename onto an existing collection is a collision: fail loud with 409
        // unless the caller opts into the COPY branch via cross_model:true.
        boolean crossModel = Boolean.TRUE.equals(body.get("cross_model"));
        if (!crossModel && repo.collectionExists(tenant, newName)) {
            HttpUtil.send(exchange, 409,
                "{\"error\":" + MAPPER.writeValueAsString("target collection already exists: " + newName
                    + " (pass cross_model:true only for a deliberate cross-model repoint)") + "}"); return;
        }
        Map<String, Integer> counts = repo.renameCollection(tenant, oldName, newName);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("renamed", counts)));
    }

    /**
     * RDR-164 P2: atomically delete a collection and all its in-Postgres derived state.
     * Returns per-table deleted-row counts so the client can preserve its CascadeCounts
     * contract. {@code pipeline.db} and local-mode cascades remain client-side.
     */
    private void handleCollectionDelete(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        // Accept both "name" (canonical) and "collection" (client compat).
        String name = body.get("name") instanceof String s ? s : (String) body.get("collection");
        if (name == null || name.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"name (or collection) required\"}"); return;
        }
        Map<String, Integer> counts = repo.deleteCollection(tenant, name);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("deleted", counts)));
    }

    private void handleCollectionForTuple(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String contentType    = queryParam(exchange, "content_type");
        String ownerId        = queryParam(exchange, "owner_id");
        String embeddingModel = queryParam(exchange, "embedding_model");
        if (contentType == null || ownerId == null || embeddingModel == null) {
            HttpUtil.send(exchange, 400, "{\"error\":\"content_type, owner_id, embedding_model required\"}"); return;
        }
        var coll = repo.collectionForTuple(tenant, contentType, ownerId, embeddingModel);
        if (coll == null) { HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return; }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(coll));
    }

    /**
     * GET /v1/catalog/collections/health?collection=X — nexus-dsu5z.
     *
     * <p>Returns {@code {last_indexed, orphan_count}} for the given
     * physical_collection.  {@code last_indexed} is MAX(indexed_at) over
     * documents in the collection (null when no documents found).
     * {@code orphan_count} is the count of documents with no incoming link.
     *
     * <p>Both fields are tenant-scoped (RLS via TenantScope).  Unknown
     * collections return {@code {last_indexed: null, orphan_count: 0}}.
     */
    private void handleCollectionHealth(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String collection = queryParam(exchange, "collection");
        if (collection == null || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"collection query param required\"}"); return;
        }
        var result = repo.collectionHealthMeta(tenant, collection);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ETL IMPORTS
    // ══════════════════════════════════════════════════════════════════════════

    private void handleImportOwner(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        if (requireNonEmptyImportBody(exchange, body)) return;
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        repo.importOwnersBatch(tenant, rows);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportDocument(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        if (requireNonEmptyImportBody(exchange, body)) return;
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        repo.importDocumentsBatch(tenant, rows);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportLink(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        if (requireNonEmptyImportBody(exchange, body)) return;
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        repo.importLinksBatch(tenant, rows);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportChunk(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        List<Map<String, Object>> rows = castRows(body.get("rows"));
        repo.importChunksBatch(tenant, docId, rows);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        if (requireNonEmptyImportBody(exchange, body)) return;
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        repo.importCollectionsBatch(tenant, rows);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    /**
     * nexus-zbci5 (GH conexus-tsye): an empty request body previously fell
     * through to {@code rows = List.of(body)} — a ONE-element list containing
     * an EMPTY map — which then failed deep inside the repo batch-import
     * (uncaught, surfaced as a generic 500). A batch import with zero content
     * is always a client error; fail loud with a clean 400 before ever
     * reaching the repo. Returns {@code true} (caller must return) if the
     * response was already sent.
     */
    private boolean requireNonEmptyImportBody(HttpExchange exchange, Map<String, Object> body) throws IOException {
        if (body.isEmpty()) {
            HttpUtil.send(exchange, 400,
                "{\"error\":\"request body required: either {\\\"rows\\\": [...]} or a single row object\"}");
            return true;
        }
        return false;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SERVER-SIDE TUMBLER ASSIGNMENT
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * POST /v1/catalog/doc/register — assign a new tumbler and register the document.
     *
     * <p>Body: {"owner_prefix": "1.1", "title": "...", "content_type": "paper", ...}
     * Response: {"tumbler": "1.1.3"}
     *
     * <p>Uses SELECT ... FOR UPDATE on catalog_owners.next_seq to atomically claim
     * the next sequence number.  Returns the assigned tumbler string.
     */
    private void handleDocRegister(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String ownerPrefix = (String) body.get("owner_prefix");
        if (ownerPrefix == null || ownerPrefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'owner_prefix' required\"}"); return;
        }
        String tumbler = repo.registerDocument(tenant, ownerPrefix, body);
        HttpUtil.send(exchange, 200, "{\"tumbler\":" + MAPPER.writeValueAsString(tumbler) + "}");
    }

    /**
     * POST /v1/catalog/doc/register_many — batch-register N documents under one owner
     * in a single transaction, returning their tumblers in INPUT ORDER (nexus-9dvqy,
     * duoak.11 sink #2).
     *
     * <p>Body: {"owner_prefix": "1.1", "docs": [{"title": ..., "file_path": ...}, ...]}
     * Response: {"tumblers": ["1.1.3", "1.1.4", ...]}  (aligned 1:1 with docs)
     *
     * <p>Existing (idempotent) docs return their current tumbler and consume no
     * sequence number; only new docs draw from the contiguous block claimed under
     * one owner-row FOR UPDATE lock. Capped at {@value #MAX_BATCH_DOC_IDS} rows to
     * stay under PostgreSQL's 32767-parameter Bind limit (~24 cols/row).
     */
    @SuppressWarnings("unchecked")
    private void handleRegisterMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String ownerPrefix = (String) body.get("owner_prefix");
        if (ownerPrefix == null || ownerPrefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'owner_prefix' required\"}"); return;
        }
        Object raw = body.get("docs");
        List<Map<String, Object>> docs = raw instanceof List<?> l
            ? l.stream().filter(o -> o instanceof Map<?, ?>).map(o -> (Map<String, Object>) o).toList()
            : List.of();
        if (docs.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many docs (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var tumblers = repo.registerDocumentMany(tenant, ownerPrefix, docs);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("tumblers", tumblers)));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNERS — extra endpoints (nexus-qnp5s)
    // ══════════════════════════════════════════════════════════════════════════

    /** GET /v1/catalog/owners/show?tumbler_prefix=X — get owner by tumbler_prefix. */
    private void handleOwnerShow(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String prefix = queryParam(exchange, "tumbler_prefix");
        if (prefix == null || prefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler_prefix required\"}"); return;
        }
        var owner = repo.ownerByPrefix(tenant, prefix);
        if (owner == null) { HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return; }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(owner));
    }

    /** POST /v1/catalog/owners/by_type — list owners filtered by owner_type. */
    private void handleOwnerByType(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String ownerType = (String) body.get("owner_type");
        if (ownerType == null || ownerType.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"owner_type required\"}"); return;
        }
        var owners = repo.ownersByType(tenant, ownerType);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("owners", owners)));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // COVERAGE ANALYTICS (nexus-3cwnx)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * GET /v1/catalog/coverage?owner_prefix=<opt>
     *
     * <p>Returns per-content-type link coverage.  Optional {@code owner_prefix}
     * parameter scopes to documents whose tumbler LIKE 'prefix.%' OR = 'prefix'.
     *
     * <p>Response: {@code {"coverage": [{"content_type":"paper","total":10,"linked":7}, ...]}}
     */
    private void handleCoverage(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String ownerPrefix = queryParam(exchange, "owner_prefix");
        if (ownerPrefix == null) ownerPrefix = "";
        var rows = repo.coverageByContentType(tenant, ownerPrefix);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("coverage", rows)));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // ANALYTICS QUERIES (nexus-xnz0o CLI port helpers)
    // ══════════════════════════════════════════════════════════════════════════

    /** GET /v1/catalog/docs/distinct-collections — distinct non-empty physical_collection values. */
    private void handleDocsDistinctCollections(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var colls = repo.distinctDocCollections(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("collections", colls)));
    }

    /** GET /v1/catalog/docs/collection-counts — {physical_collection: doc_count} for all non-empty collections. */
    private void handleDocsCollectionCounts(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var counts = repo.collectionDocCounts(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("counts", counts)));
    }

    /** GET /v1/catalog/docs/orphaned — documents with no incoming or outgoing links. */
    private void handleDocsOrphaned(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var docs = repo.orphanedDocs(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("documents", docs)));
    }

    /** GET /v1/catalog/docs/absolute-paths — documents whose file_path starts with '/'. */
    private void handleDocsAbsolutePaths(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var docs = repo.docsWithAbsolutePaths(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("documents", docs)));
    }

    /** GET /v1/catalog/owners/all-with-roots — owners with non-empty repo_root. */
    private void handleOwnersWithRoots(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var owners = repo.ownersWithRoots(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("owners", owners)));
    }

    /**
     * GET /v1/catalog/collections/owner-root?name=X — (owner_id, repo_root) for a collection.
     *
     * <p>Returns 404 when the collection does not exist.
     * Response: {"owner_id": "1.1", "repo_root": "/path/to/repo"}
     */
    private void handleCollectionOwnerRoot(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String name = queryParam(exchange, "name");
        if (name == null || name.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"name query param required\"}"); return;
        }
        var result = repo.collectionOwnerRoot(tenant, name);
        if (result == null) { HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return; }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // SCORING HOT-PATH BATCH ENDPOINTS (nexus-qnp5s)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * POST /v1/catalog/docs/chunk-counts — batch chunk_count for a set of doc_ids.
     *
     * <p>Request: {"doc_ids": ["1.1.1", "1.1.2", ...]}
     * Response: {"1.1.1": 42, "1.1.2": 17}  (missing docs absent)
     */
    @SuppressWarnings("unchecked")
    private void handleDocChunkCounts(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawIds = body.get("doc_ids");
        List<String> docIds = rawIds instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        var counts = repo.chunkCountsForDocs(tenant, docIds);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(counts));
    }

    /**
     * POST /v1/catalog/links/from-batch — batch outbound links for a set of tumblers.
     *
     * <p>Request: {"tumblers": ["1.1.1", "1.1.2", ...]}
     * Response: {"1.1.1": [{"from_tumbler": "1.1.1", "link_type": "cites"}], ...}
     */
    @SuppressWarnings("unchecked")
    private void handleLinksFromBatch(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawT = body.get("tumblers");
        List<String> tumblers = rawT instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        var links = repo.linksFromBatch(tenant, tumblers);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(links));
    }

    /**
     * POST /v1/catalog/manifest/backfill — stamp manifest collection from the
     * owning doc's physical_collection where NULL (RDR-159 P-1b).
     *
     * <p>Response: {@code {"stamped": <n>}}. MUST run before the orphan check.
     */
    private void handleManifestBackfill(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        long stamped = repo.manifestBackfill(tenant);
        HttpUtil.send(exchange, 200, "{\"stamped\":" + stamped + "}");
    }

    /**
     * GET /v1/catalog/manifest/orphans?dim=384&limit=100 — manifest rows with no
     * chunk row in chunks_&lt;dim&gt; (RDR-159 P-1b non-vacuous validation).
     *
     * <p>Response: {@code {"dim": <d>, "count": <n>, "orphans": [...]}}, count and
     * sample computed in one transaction so they agree. {@code count} is exact;
     * {@code orphans} is a sample capped at {@code limit} (default 100, must be
     * &gt; 0 — the count is the gate, the sample is diagnostic). An unsupported
     * dim or a non-positive limit is a 400.
     */
    private void handleManifestOrphans(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String dimRaw = queryParam(exchange, "dim");
        if (dimRaw == null || dimRaw.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"dim query param required (384|768|1024)\"}"); return;
        }
        int dim;
        try {
            dim = Integer.parseInt(dimRaw);
        } catch (NumberFormatException e) {
            HttpUtil.send(exchange, 400, "{\"error\":\"dim must be an integer (384|768|1024)\"}"); return;
        }
        int limit = intParam(exchange, "limit", 100);
        if (limit <= 0) {
            HttpUtil.send(exchange, 400, "{\"error\":\"limit must be > 0 (bounded sample; the count field is the gate)\"}"); return;
        }
        // count + sample in ONE transaction so they are mutually consistent.
        var report = repo.manifestOrphanReport(tenant, dim, limit);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("dim", dim,
                   "count", report.get("count"),
                   "orphans", report.get("orphans"))));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // MIGRATION COUNT VERIFICATION (RDR-159 P-1a)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * POST /v1/catalog/verify/relation-counts — tenant-scoped row counts for
     * the migration-verify relations.
     *
     * <p>Request:  {@code {"relations": ["nexus.memory", "nexus.plans", ...]}}
     * Response:    {@code {"counts": {"nexus.memory": 123, ...}}}
     *
     * <p>The repository whitelists relation names (the fixed migration-verify
     * set); unrecognised relations are omitted. Backs the RDR-159
     * {@code nexus.migration} count verification without a direct PG
     * connection from Python (RDR-152).
     */
    private void handleRelationCounts(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("relations");
        List<String> relations = raw instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        var counts = repo.relationCounts(tenant, relations);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("counts", counts)));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // HELPERS
    // ══════════════════════════════════════════════════════════════════════════

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

    private int intParam(HttpExchange exchange, String key, int def) {
        String v = queryParam(exchange, key);
        if (v == null || v.isBlank()) return def;
        try { return Integer.parseInt(v); } catch (NumberFormatException e) { return def; }
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> castRows(Object raw) {
        if (raw instanceof List<?> l) {
            List<Map<String, Object>> result = new ArrayList<>();
            for (Object item : l) {
                if (item instanceof Map<?, ?> m) result.add((Map<String, Object>) m);
            }
            return result;
        }
        return List.of();
    }
}
