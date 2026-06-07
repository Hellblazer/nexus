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
 *   GET   /v1/catalog/manifest/get       get manifest for doc_id
 *   POST  /v1/catalog/manifest/purge     purge manifest for doc_id
 *   GET   /v1/catalog/manifest/chashes   chashes for collection
 *   POST  /v1/catalog/owners/upsert      upsert owner
 *   GET   /v1/catalog/owners/list        list all owners
 *   GET   /v1/catalog/owners/by_repo     get owner by repo_hash
 *   POST  /v1/catalog/collections/upsert upsert collection
 *   GET   /v1/catalog/collections/list   list collections
 *   GET   /v1/catalog/collections/get    get collection by name
 *   POST  /v1/catalog/collections/supersede supersede collection
 *   POST  /v1/catalog/collections/rename rename collection (cascade)
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
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private final CatalogRepository repo;

    public CatalogHandler(CatalogRepository repo) {
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
                case "/manifest/get"          -> handleManifestGet(exchange, tenant, method);
                case "/manifest/purge"        -> handleManifestPurge(exchange, tenant, method);
                case "/manifest/chashes"      -> handleManifestChashes(exchange, tenant, method);
                case "/manifest/docs_for_chashes" -> handleDocsForChashes(exchange, tenant, method);

                // ── Owners ────────────────────────────────────────────────────
                case "/owners/upsert"         -> handleOwnerUpsert(exchange, tenant, method);
                case "/owners/list"           -> handleOwnerList(exchange, tenant, method);
                case "/owners/by_repo"        -> handleOwnerByRepo(exchange, tenant, method);
                case "/owners/by_name"        -> handleOwnerByName(exchange, tenant, method);
                case "/owners/head_hash"      -> handleOwnerHeadHash(exchange, tenant, method);

                // ── Collections ───────────────────────────────────────────────
                case "/collections/upsert"    -> handleCollectionUpsert(exchange, tenant, method);
                case "/collections/list"      -> handleCollectionList(exchange, tenant, method);
                case "/collections/get"       -> handleCollectionGet(exchange, tenant, method);
                case "/collections/supersede" -> handleCollectionSupersede(exchange, tenant, method);
                case "/collections/rename"    -> handleCollectionRename(exchange, tenant, method);
                case "/collections/for_tuple" -> handleCollectionForTuple(exchange, tenant, method);

                // ── ETL imports ───────────────────────────────────────────────
                case "/import/owner"          -> handleImportOwner(exchange, tenant, method);
                case "/import/document"       -> handleImportDocument(exchange, tenant, method);
                case "/import/link"           -> handleImportLink(exchange, tenant, method);
                case "/import/chunk"          -> handleImportChunk(exchange, tenant, method);
                case "/import/collection"     -> handleImportCollection(exchange, tenant, method);

                // ── Server-side tumbler assignment ────────────────────────────
                case "/doc/register"          -> handleDocRegister(exchange, tenant, method);

                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found: " + op + "\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (Exception e) {
            log.error("event=catalog_handler_error op={} tenant={} error={}", op, tenant, e.getMessage(), e);
            HttpUtil.send(exchange, 500, "{\"error\":\"internal server error\"}");
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
        repo.upsertLink(tenant, body);
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
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
            String createdBy = (String) body.get("created_by");
            deleted = repo.bulkDeleteLinks(tenant, fromT, toT, linkType, createdBy);
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
        if (tumbler == null || tumbler.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"tumbler query param required\"}"); return;
        }
        if (direction == null) direction = "both";

        List<Map<String, Object>> linksFrom = List.of();
        List<Map<String, Object>> linksTo   = List.of();
        if ("out".equals(direction) || "both".equals(direction)) {
            linksFrom = repo.linksFrom(tenant, tumbler, linkType);
        }
        if ("in".equals(direction) || "both".equals(direction)) {
            linksTo = repo.linksTo(tenant, tumbler, linkType);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("links_from", linksFrom, "links_to", linksTo)));
    }

    /**
     * GET /v1/catalog/link_query?from_tumbler=X&to_tumbler=X&link_type=X
     *                             &created_by=X&limit=N&offset=N&created_at_before=ISO
     */
    private void handleLinkQuery(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String fromT           = queryParam(exchange, "from_tumbler");
        String toT             = queryParam(exchange, "to_tumbler");
        String linkType        = queryParam(exchange, "link_type");
        String createdBy       = queryParam(exchange, "created_by");
        String createdAtBefore = queryParam(exchange, "created_at_before");
        int limit              = intParam(exchange, "limit",  50);
        int offset             = intParam(exchange, "offset", 0);
        var links = repo.queryLinks(tenant, fromT, toT, linkType, createdBy, createdAtBefore, limit, offset);
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
        int updated = repo.renameCollection(tenant, oldName, newName);
        HttpUtil.send(exchange, 200, "{\"updated\":" + updated + "}");
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

    // ══════════════════════════════════════════════════════════════════════════
    // ETL IMPORTS
    // ══════════════════════════════════════════════════════════════════════════

    private void handleImportOwner(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        for (var row : rows) repo.importOwner(tenant, row);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportDocument(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        for (var row : rows) repo.importDocument(tenant, row);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportLink(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        for (var row : rows) repo.importLink(tenant, row);
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
        for (var row : rows) repo.importChunk(tenant, docId, row);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        List<Map<String, Object>> rows = body.containsKey("rows")
            ? castRows(body.get("rows"))
            : List.of(body);
        for (var row : rows) repo.importCollection(tenant, row);
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
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
