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
 *   GET   /v1/catalog/descendants        all documents under a tumbler prefix (unbounded, T2 nexus/chroma-residue-plan-2026-08-10 §C1)
 *   GET   /v1/catalog/search             FTS search
 *   POST  /v1/catalog/update             update document fields
 *   POST  /v1/catalog/update_many        batch-update fields for N documents (nexus-xedhp)
 *   POST  /v1/catalog/delete_many        batch-tombstone N documents (nexus-xedhp)
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
 *   POST  /v1/catalog/manifest/chashes_many batch-fetch just the chash list for multiple doc_ids (nexus-eslkl)
 *   POST  /v1/catalog/manifest/purge     purge manifest for doc_id
 *   GET   /v1/catalog/manifest/chashes   chashes for collection
 *   POST  /v1/catalog/manifest/resync    recompute chunk_count from manifest row count
 *   GET   /v1/catalog/manifest/null_collection  read-only count of pre-backfill (collection IS NULL) manifest rows (T2 nexus/chroma-residue-plan-2026-08-10 §C2)
 *   GET   /v1/catalog/chash/conformance   per-table chash width-conformance report (tenant-scoped, RDR-180 nexus-du2dw)
 *   GET   /v1/catalog/manifest/verify    per-doc referenced/present/missing (RUNFENCE, nexus-5xn3k.2)
 *   GET   /v1/catalog/manifest/verify_all per-collection referenced/present/missing, every live doc
 *   POST  /v1/catalog/index-run/begin    stamp index_state='indexing' (idempotent, NOT a lock)
 *   POST  /v1/catalog/index-run/begin-many batch index_state='indexing' for N docs, ONE round trip (nexus-vw594 F1)
 *   POST  /v1/catalog/index-run/complete FAIL-CLOSED verify-then-stamp index_state='complete'
 *   POST  /v1/catalog/index-run/fail     stamp index_state='failed'
 *   POST  /v1/catalog/resolve_many       batch-resolve multiple doc_ids to entries (nexus-7lm3q)
 *   POST  /v1/catalog/owners/upsert      upsert owner
 *   GET   /v1/catalog/owners/list        list all owners
 *   POST  /v1/catalog/owners/sweep_next_seq_drift  floor every drifted owner's next_seq (nexus-0ehwe item 5)
 *   POST  /v1/catalog/owners/deactivate  soft-delete an owner (nexus-cw262 -- excluded from list/by_type by default)
 *   POST  /v1/catalog/owners/reactivate  clear an owner's deactivated_at (nexus-cw262)
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
    private final dev.nexus.service.db.CombinedWriteService combinedWriteService;

    public CatalogHandler(CatalogRepository repo) {
        this(repo, null);
    }

    /**
     * nexus-kl2z6 increment 1: {@code combinedWriteService} may be null — a
     * request carrying {@code chunks} then answers 503 (matches {@link
     * VectorHandler}'s absent-backend pattern: refuse explicitly, never
     * NPE). Every EXISTING call site keeps the 1-arg constructor and gets
     * byte-for-byte the pre-kl2z6 behaviour.
     */
    public CatalogHandler(CatalogRepository repo,
                           dev.nexus.service.db.CombinedWriteService combinedWriteService) {
        this.repo = repo;
        this.combinedWriteService = combinedWriteService;
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
                case "/descendants"           -> handleDescendants(exchange, tenant, method);
                case "/search"                -> handleSearch(exchange, tenant, method);
                case "/update"                -> handleUpdate(exchange, tenant, method);
                case "/update_many"           -> handleUpdateMany(exchange, tenant, method);
                case "/delete"                -> handleDelete(exchange, tenant, method);
                case "/delete_many"           -> handleDeleteMany(exchange, tenant, method);
                case "/purge-trash"           -> handlePurgeTrash(exchange, tenant, method);
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
                case "/manifest/chashes_many" -> handleManifestChashesMany(exchange, tenant, method);
                case "/manifest/purge"        -> handleManifestPurge(exchange, tenant, method);
                case "/manifest/chashes"      -> handleManifestChashes(exchange, tenant, method);
                case "/manifest/docs_for_chashes" -> handleDocsForChashes(exchange, tenant, method);
                case "/manifest/resync"       -> handleManifestResync(exchange, tenant, method);
                case "/manifest/null_collection" -> handleManifestNullCollection(exchange, tenant, method);
                case "/chash/conformance"     -> handleChashConformance(exchange, tenant, method);
                // nexus-ysrwi: the third sibling. /docs/orphaned already
                // existed; links had no equivalent, which is why the client
                // could not build a doctor check. (/manifest/orphans, its
                // other named sibling, was retired RDR-191 Phase 6,
                // nexus-o8dil.33 — the manifest-chunk FK makes the dangling
                // state it detected unreachable.)
                case "/links/orphaned"        -> handleLinksOrphaned(exchange, tenant, method);

                // ── Index run fence (RUNFENCE, nexus-5xn3k.2) ─────────────────
                // RDR-191 Phase 6 (nexus-o8dil.33): the READ-ONLY diagnostic
                // routes /manifest/verify and /manifest/verify_all are
                // retired — the manifest-chunk FK (catalog-029) makes the
                // dangling state they detected unreachable. The underlying
                // nexus.manifest_verify(text) SQL function and its
                // manifestVerifyCtx call site are NOT removed:
                // completeIndexRun below still depends on them for the
                // write-path fail-closed verify-then-stamp (a DIFFERENT
                // completeness question the FK does not answer).
                case "/index-run/begin"       -> handleIndexRunBegin(exchange, tenant, method);
                case "/index-run/begin-many"  -> handleIndexRunBeginMany(exchange, tenant, method);
                case "/index-run/complete"    -> handleIndexRunComplete(exchange, tenant, method);
                case "/index-run/fail"        -> handleIndexRunFail(exchange, tenant, method);

                // ── Owners ────────────────────────────────────────────────────
                case "/owners/upsert"         -> handleOwnerUpsert(exchange, tenant, method);
                case "/owners/list"           -> handleOwnerList(exchange, tenant, method);
                case "/owners/sweep_next_seq_drift" -> handleOwnersSweepNextSeqDrift(exchange, tenant, method);
                case "/owners/by_repo"        -> handleOwnerByRepo(exchange, tenant, method);
                case "/owners/by_name"        -> handleOwnerByName(exchange, tenant, method);
                case "/owners/head_hash"      -> handleOwnerHeadHash(exchange, tenant, method);
                case "/owners/show"           -> handleOwnerShow(exchange, tenant, method);
                case "/owners/by_type"        -> handleOwnerByType(exchange, tenant, method);
                case "/owners/deactivate"     -> handleOwnerDeactivate(exchange, tenant, method);
                case "/owners/reactivate"     -> handleOwnerReactivate(exchange, tenant, method);

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
                case "/docs/collection-counts-all" -> handleDocsCollectionCountsAll(exchange, tenant, method);
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
                // ── GC audit (nexus-jqvzk) ────────────────────────────────────
                case "/gc_audit/record"       -> handleGcAuditRecord(exchange, tenant, method);
                case "/gc_audit/list"         -> handleGcAuditList(exchange, tenant, method);

                case "/verify/relation-counts" -> handleRelationCounts(exchange, tenant, method);

                default -> HttpUtil.send(exchange, 404, "{\"error\":\"not found: " + op + "\"}");
            }
        } catch (IllegalArgumentException e) {
            HttpUtil.send(exchange, 400, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (CatalogRepository.CollectionMergeRefused e) {
            // nexus-v6za0: the rename txn's own emptiness assertion. Reachable only when a
            // concurrent write populates the target between the handler's pre-check and the
            // transaction — the TOCTOU case that assertion exists for. It is a REFUSAL, so it
            // gets the same 409 the pre-check gives, with the message that names the remedy.
            // It fell into the generic catch below for one commit and surfaced as an opaque 500.
            HttpUtil.send(exchange, 409, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (CatalogRepository.TombstonedDocumentException e) {
            // nexus-eldyi: a manifest write (write/append/purge) refused a
            // tombstoned target — the non-resurrection rule extended beyond
            // /update. Same 409 shape as CollectionMergeRefused above: a
            // refusal, not a server error.
            HttpUtil.send(exchange, 409, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (CatalogRepository.DocumentNotFoundException e) {
            // nexus-s13u0/RDR-191 GATE-2 review High: a manifest write
            // (write/append/import_chunks_batch) against a doc_id with no
            // catalog_documents row at all — the explicit-throw replacement
            // for the fk-001 FK violation this same shape used to surface as
            // before catalog_document_chunks.collection went NOT NULL (see
            // CatalogRepository.DocumentNotFoundException's own javadoc for
            // the caller-supplied collection contract). DocumentNotFoundException
            // extends IllegalStateException, NOT IllegalArgumentException, so
            // without this arm it fell into the generic catch below and
            // surfaced as an opaque 500 with the message dropped — the exact
            // mistake the CollectionMergeRefused arm above was already fixed
            // for once. 409, not 404: pre-fix this same case was an
            // SQLException class-23 FK violation, which the typed ladder
            // already maps to 409 via HttpUtil.sendTypedDbError — this keeps
            // the wire contract byte-identical rather than introducing an
            // unrequested 404.
            HttpUtil.send(exchange, 409, "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}");
        } catch (CatalogRepository.IndexRunVerifyRefused e) {
            // nexus-5xn3k.2: /complete's fail-closed gate (memo §3.3, HARD spec
            // amendment T2 21350) — missing>0 OR referenced!=claimed chunk_count.
            // 409 carrying the counts so the client can log/retry rather than a
            // bare message string.
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", e.getMessage());
            body.put("doc_id", e.docId);
            body.put("referenced", e.referenced);
            body.put("present", e.present);
            body.put("missing", e.missing);
            body.put("chunk_count", e.chunkCount);
            HttpUtil.send(exchange, 409, MAPPER.writeValueAsString(body));
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

    /**
     * nexus-v3w9n Amendment 2: tumbler grammar is enforced HERE, at the HTTP
     * boundary, not by a schema CHECK (deferred to nexus-ia69x — see {@link
     * TumblerGrammar}'s own javadoc for the full rationale). Every route that
     * accepts an explicit {@code tumbler_prefix} or {@code tumbler} from
     * outside validates it BEFORE any repository call; repository methods
     * themselves are untouched. On violation, sends a 400 naming the rule,
     * the offending field, and the value, and returns true so the caller can
     * return immediately without ever reaching the DB.
     */
    private boolean rejectIfBadOwnerPrefix(HttpExchange exchange, String value) throws IOException {
        // Blank/absent is the auto-mint path (server allocates a conforming
        // "1.N" prefix itself) — only a genuinely present value is checked.
        if (value == null || value.isBlank() || TumblerGrammar.isOwnerPrefix(value)) {
            return false;
        }
        sendTumblerGrammarViolation(exchange, "tumbler_prefix", value);
        return true;
    }

    private boolean rejectIfBadDocumentTumbler(HttpExchange exchange, String value) throws IOException {
        if (value == null || value.isBlank() || TumblerGrammar.isDocumentTumbler(value)) {
            return false;
        }
        sendTumblerGrammarViolation(exchange, "tumbler", value);
        return true;
    }

    private void sendTumblerGrammarViolation(HttpExchange exchange, String field, String value) throws IOException {
        log.warn("event=catalog_handler_tumbler_grammar_violation field={} value={}", field, value);
        HttpUtil.send(exchange, 400,
            "{\"error\":\"tumbler grammar violation\",\"rule\":\"tumbler-grammar\",\"field\":"
            + MAPPER.writeValueAsString(field) + ",\"value\":" + MAPPER.writeValueAsString(value) + "}");
    }

    /** POST /v1/catalog/register — upsert owner + document row. */
    private void handleRegister(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        // body may contain both owner fields and document fields; repo handles the split
        if (rejectIfBadOwnerPrefix(exchange, (String) body.get("tumbler_prefix"))) return;
        if (rejectIfBadDocumentTumbler(exchange, (String) body.get("tumbler"))) return;
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
        // nexus-ekaxn: follow_alias defaults FALSE — byte-identical to the
        // pre-fix response for any client that does not send the param.
        boolean followAlias = boolParam(exchange, "follow_alias", false);
        var doc = repo.getDocument(tenant, tumbler, followAlias);
        if (doc == null) {
            HttpUtil.send(exchange, 404, "{\"error\":\"not found\"}"); return;
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(doc));
    }

    /**
     * GET /v1/catalog/list?limit=N&offset=N&content_type=X&collection=X&corpus=X&owner=X
     *
     * <p>nexus-xoimv: {@code limit}/{@code offset} are now threaded through
     * every filter branch below, not just the terminal (unfiltered) {@code
     * listDocuments} call. THE SEMANTICS ARE DELIBERATELY ASYMMETRIC between
     * "absent from the query string" and "explicit":
     * <ul>
     *   <li>{@code limit} ABSENT — the seven filter branches stay UNBOUNDED
     *       (their pre-xoimv behaviour: {@code documentsByCollection} et al.
     *       never took a limit at all). Naively defaulting the missing param
     *       to 200 here — as the terminal branch always has — would silently
     *       truncate every existing unbounded caller (the client's
     *       {@code all_documents} treats {@code limit==0} as unbounded and
     *       {@code list_by_collection} omits the param entirely when
     *       {@code None}) from "return everything" to "return first page and
     *       drop the rest", with no error to signal the shrinkage.</li>
     *   <li>{@code limit} EXPLICIT — honored verbatim on every branch,
     *       filtered or not.</li>
     *   <li>The terminal (unfiltered) branch is UNCHANGED: it always applied
     *       {@code limit=200} whether or not the caller sent one, and keeps
     *       doing so here — only the seven filter branches gained the
     *       absent/explicit distinction.</li>
     *   <li>{@code offset} has no such asymmetry: absent defaults to 0 on
     *       every branch, which is a no-op regardless of whether the branch
     *       is bounded or unbounded, so it is passed straight through.</li>
     * </ul>
     */
    private void handleList(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String rawLimit       = queryParam(exchange, "limit");
        boolean limitExplicit = rawLimit != null && !rawLimit.isBlank();
        int limit             = intParam(exchange, "limit",  200);
        int offset            = intParam(exchange, "offset", 0);
        // 0 signals "unbounded" to every documentsBy* filter method below —
        // used ONLY when the caller did not send `limit` at all.
        int filterLimit = limitExplicit ? limit : 0;

        // Optional filter dispatching
        String collection  = queryParam(exchange, "collection");
        String contentType = queryParam(exchange, "content_type");
        String corpus       = queryParam(exchange, "corpus");
        String owner        = queryParam(exchange, "owner");
        String filePath      = queryParam(exchange, "file_path");
        String sourceUri     = queryParam(exchange, "source_uri");

        List<Map<String, Object>> docs;
        if (collection != null && !collection.isBlank()) {
            docs = repo.documentsByCollection(tenant, collection, filterLimit, offset);
        } else if (contentType != null && !contentType.isBlank()) {
            docs = repo.documentsByContentType(tenant, contentType, filterLimit, offset);
        } else if (corpus != null && !corpus.isBlank()) {
            docs = repo.documentsByCorpus(tenant, corpus, filterLimit, offset);
        } else if (owner != null && !owner.isBlank()
                   && filePath != null && !filePath.isBlank()) {
            // GH #1350 Fix B: owner+file_path must filter by BOTH. The owner-only
            // branch below ignored file_path and returned the full owner list,
            // driving the client's docs[0] mis-attribution (silent corruption).
            docs = repo.documentsByOwnerAndFilePath(tenant, owner, filePath, filterLimit, offset);
        } else if (owner != null && !owner.isBlank()) {
            docs = repo.documentsByOwner(tenant, owner, filterLimit, offset);
        } else if (filePath != null && !filePath.isBlank()) {
            docs = repo.documentsByFilePath(tenant, filePath, filterLimit, offset);
        } else if (sourceUri != null && !sourceUri.isBlank()) {
            docs = repo.documentsBySourceUri(tenant, sourceUri, filterLimit, offset);
        } else {
            docs = repo.listDocuments(tenant, limit, offset);
        }
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("documents", docs, "count", docs.size())));
    }

    /**
     * GET /v1/catalog/descendants?prefix=&lt;p&gt; — all documents whose
     * tumbler starts with {@code prefix + "."} (T2 nexus/chroma-residue-plan-2026-08-10 §C1).
     *
     * <p>Backs {@code HttpCatalogClient.descendants()}, which previously had
     * no dedicated route and pulled a single unbounded {@code /list} page
     * (500 rows) and filtered client-side — silently truncating any subtree
     * with more than one page of documents (verified 0% coverage on 11 of
     * the 12 largest cloud-catalog subtrees). {@link CatalogRepository
     * #descendants} runs ONE unbounded SQL query ({@code LIKE 'prefix.%'}),
     * so there is no pagination to thread here — the whole point of this
     * route is that the answer is complete by construction, not that it is
     * capped and then paged. Response shape mirrors {@link #handleList}:
     * {@code {"documents": [...], "count": N}}.
     *
     * <p><b>Completeness vs. index-acceleration (substantive critique
     * 2026-08-10, T2 nexus/chroma-residue-C1-T0.1-critique-2026-08-10,
     * finding 2):</b> completeness is guaranteed by construction above —
     * that claim does not depend on any index existing. Whether the {@code
     * LIKE 'prefix.%'} predicate itself is index-accelerated is a SEPARATE,
     * narrower claim: a prefix {@code LIKE} can use the {@code
     * catalog_documents_pk} B-tree on {@code (tenant_id, tumbler)} only
     * under a "C" (or otherwise pattern-indexable, e.g. {@code
     * text_pattern_ops}) collation — {@code scripts/build_pg_bundle.sh}
     * provisions the LOCAL bundle with {@code initdb --no-locale} (C
     * collation), so index-acceleration is confirmed there, but the CLOUD
     * managed PostgreSQL's locale/collation is not provisioned or verified
     * anywhere in this repository, and no {@code text_pattern_ops} index
     * exists on this column. Do not describe this route as "one indexed
     * SQL query" without that caveat — say "complete by construction,
     * index-accelerated only under a pattern-indexable collation, unverified
     * on the managed deployment."
     */
    private void handleDescendants(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        String prefix = queryParam(exchange, "prefix");
        if (prefix == null || prefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"prefix query param required\"}"); return;
        }
        var docs = repo.descendants(tenant, prefix);
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

    /**
     * POST /v1/catalog/update_many — batch-update N documents' mutable fields
     * in ONE round trip (nexus-xedhp, duoak.11 follow-up).
     *
     * <p>Body: {"updates": [{"tumbler": "1.1.3", "head_hash": "...", ...}, ...]}
     * Response: {"updated": [1, 1, 0, ...]}  (per-entry update count, aligned
     * 1:1 with the input — 0 means not found / tombstoned / no-op; a
     * non-updatable-column entry is -1 (marked, not dropped) — the entry's
     * OWN column-whitelist violation does NOT abort the rest of the batch,
     * mirroring register_many's per-doc failure isolation. A malformed
     * *shape* (non-list body, non-object element) is a client bug, not a
     * per-doc data problem, and 400s rather than silently shrinking the
     * response array — mirrors handleManifestWriteMany's review #2 fix.
     *
     * <p>Capped at {@value #MAX_BATCH_DOC_IDS} rows, same bind-limit rationale
     * as register_many.
     */
    @SuppressWarnings("unchecked")
    private void handleUpdateMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("updates");
        if (!(raw instanceof List<?> l)) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'updates' must be a list\"}"); return;
        }
        if (l.stream().anyMatch(o -> !(o instanceof Map))) {
            HttpUtil.send(exchange, 400, "{\"error\":\"every 'updates' element must be an object\"}"); return;
        }
        List<Map<String, Object>> updates = l.stream().map(o -> (Map<String, Object>) o).toList();
        if (updates.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many updates (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var counts = repo.updateDocumentsMany(tenant, updates);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("updated", counts)));
    }

    /**
     * POST /v1/catalog/delete_many — batch-tombstone N documents in ONE
     * round trip (nexus-xedhp: completes the update_many/register_many/
     * delete_many batch trio).
     *
     * <p>Body: {"tumblers": ["1.1.3", "1.1.4", ...]}
     * Response: {"deleted": ["1.1.3", ...]}  (the subset of input tumblers
     * that were actually tombstoned — already-deleted or non-existent
     * tumblers are silently excluded, same idempotent semantics as the
     * single-doc DELETE). NOT positionally aligned with the input (unlike
     * update_many) — a duplicate or already-deleted tumbler collapses to
     * one membership check; callers needing per-position outcomes should
     * not assume this response mirrors input order/length.
     *
     * <p>A malformed *shape* (non-list body, non-string element) 400s
     * rather than silently shrinking the input — mirrors
     * handleManifestWriteMany's review #2 fix and handleUpdateMany above.
     *
     * <p>Capped at {@value #MAX_BATCH_DOC_IDS} rows, same bind-limit
     * rationale as register_many / update_many.
     */
    private void handleDeleteMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("tumblers");
        if (!(raw instanceof List<?> l)) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'tumblers' must be a list\"}"); return;
        }
        if (l.stream().anyMatch(o -> !(o instanceof String))) {
            HttpUtil.send(exchange, 400, "{\"error\":\"every 'tumblers' element must be a string\"}"); return;
        }
        List<String> tumblers = l.stream().map(o -> (String) o).toList();
        if (tumblers.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many tumblers (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var deleted = repo.deleteDocumentsMany(tenant, tumblers);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("deleted", deleted)));
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

    /**
     * POST /v1/catalog/purge-trash (nexus-3ck2g E3) — the caller {@code
     * nexus.purge_trash} never had (catalog-003-soft-delete.xml:200-296 defined the
     * SECURITY INVOKER function; nothing in the service invoked it, so the stranded-
     * chunk sweep and the physical tombstone GC it implements were both dead code).
     *
     * <p>Body: {@code {"older_than_days": int >= 1 (default 30), "dry_run": bool
     * (default true)}}. {@code dry_run=true}: count-only preview via {@link
     * CatalogRepository#purgeTrashPreview}, no mutation. {@code dry_run=false}:
     * actually purges via {@link CatalogRepository#purgeTrash} — the per-tenant GUC
     * guard (catalog-003-soft-delete.xml:209-216, "cross-tenant purge is not
     * permitted") is enforced INSIDE the SQL function itself, and {@code
     * TenantScope.withTenant} always stamps {@code nexus.tenant} before the function
     * runs, so there is no unscoped call path. Response carries {@code documents_purged}
     * plus per-dim {@code chunks_<dim>_stranded} counts in BOTH modes (preview vs.
     * actual, per the field's own semantics) and {@code dry_run} echoing the mode
     * actually taken. A live (non-dry-run) invocation confirm-gate is the CLIENT's
     * job (mirrors the {@code nx catalog reconcile-stale} pattern) — this route
     * itself does not add its own confirmation step beyond {@code dry_run}'s default.
     */
    private void handlePurgeTrash(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);

        // nexus-3ck2g code-review Minor: a present-but-wrong-typed field (e.g.
        // "older_than_days": "abc") must 400, not silently fall back to the default
        // the way an ABSENT field correctly does.
        int olderThanDays = 30;
        Object olderThanDaysRaw = body.get("older_than_days");
        if (olderThanDaysRaw != null) {
            if (!(olderThanDaysRaw instanceof Number n)) {
                HttpUtil.send(exchange, 400, "{\"error\":\"'older_than_days' must be an integer\"}"); return;
            }
            olderThanDays = n.intValue();
        }
        if (olderThanDays < 1) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'older_than_days' must be >= 1\"}"); return;
        }

        boolean dryRun = true;
        Object dryRunRaw = body.get("dry_run");
        if (dryRunRaw != null) {
            if (!(dryRunRaw instanceof Boolean b)) {
                HttpUtil.send(exchange, 400, "{\"error\":\"'dry_run' must be a boolean\"}"); return;
            }
            dryRun = b;
        }

        Map<String, Object> result = dryRun
            ? repo.purgeTrashPreview(tenant, olderThanDays)
            : repo.purgeTrash(tenant, olderThanDays);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
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
            docs = repo.documentsByFilePath(tenant, filePath, 0, 0);
        } else if (sourceUri != null && !sourceUri.isBlank()) {
            docs = repo.documentsBySourceUri(tenant, sourceUri, 0, 0);
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
        boolean created;
        try {
            created = repo.upsertLink(tenant, body);
        } catch (CatalogRepository.DanglingEndpointException e) {
            // nexus-9ssih: a MACHINE-READABLE 400 — the auto-linker counts
            // skipped_missing_endpoint off `code`, and must not confuse this
            // with the generic malformed-body 400 the outer ladder produces.
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("error", e.getMessage());
            payload.put("code", "dangling_endpoint");
            payload.put("missing", e.missing());
            HttpUtil.send(exchange, 400, MAPPER.writeValueAsString(payload));
            return;
        }
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
     *
     * <p>{@code seeds} capped at {@value #MAX_BATCH_DOC_IDS} (found during
     * CatalogHandlerEnvelopeConformanceGateTest authorship): the BFS's first
     * round uses the raw seeds list directly in a jOOQ {@code .in(...)} — the
     * 500-node MAX_GRAPH_NODES cap in {@link CatalogRepository#graphBFS}
     * bounds the REACHABLE set, not this initial IN-list, so an oversized
     * seeds array reached the bind-parameter risk uncapped.
     */
    @SuppressWarnings("unchecked")
    private void handleTraverse(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawSeeds = body.get("seeds");
        List<String> seeds = rawSeeds instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        if (seeds.size() > MAX_BATCH_DOC_IDS) {
            // NOTE for cap-sizing: the scoring callers of the batch reads
            // (src/nexus/scoring.py chunk_counts_for_docs / links_from_batch)
            // wrap these calls in a broad except and DEGRADE — they lose the
            // scoring signal on a 400, they do not crash. That degrade-not-
            // crash contract is load-bearing if a corpus="all" aggregation
            // ever exceeds the cap; do not narrow their except arms without
            // adding client-side paging first.
            HttpUtil.send(exchange, 400, "{\"error\":\"too many seeds (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        Object rawTypes = body.get("link_types");
        List<String> linkTypes = rawTypes instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        String direction = (String) body.getOrDefault("direction", "both");
        int depth = body.get("depth") instanceof Number n ? n.intValue() : 1;
        // nexus-ybj1b: the client has always sent this; the server used to drop
        // it on the floor, which broke the contract in BOTH directions — the
        // default stopped excluding implements-heuristic (reinstating the 2:1
        // flood for every service-mode user) and the opt-in became a no-op.
        // ABSENT means false, which is the correct default: exclude.
        boolean includeHeuristic = Boolean.TRUE.equals(body.get("include_heuristic"));
        var result = repo.graphBFS(tenant, seeds, linkTypes, direction, depth, includeHeuristic);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // MANIFEST
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Extracts and validates the required top-level {@code collection} field
     * (RDR-191, Hal ruling 2026-08-12): writers SEND the collection, the
     * engine STAMPS it verbatim — no default, no inference, no silent
     * omission. Sends 400 and returns {@code null} on a missing/blank value;
     * callers must check for {@code null} and return immediately.
     */
    private static String requireCollection(HttpExchange exchange, Map<String, Object> body) throws IOException {
        Object raw = body.get("collection");
        if (!(raw instanceof String collection) || collection.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'collection' required\"}");
            return null;
        }
        return collection;
    }

    /** POST /v1/catalog/manifest/write — replace manifest (atomic delete + insert). */
    @SuppressWarnings("unchecked")
    private void handleManifestWrite(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        String collection = requireCollection(exchange, body);
        if (collection == null) return;
        List<Map<String, Object>> rows = strictRows(body.get("rows"));
        requireCanonicalChashes(rows);
        repo.writeManifest(tenant, docId, collection, rows);
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
        String collection = requireCollection(exchange, body);
        if (collection == null) return;
        List<Map<String, Object>> rows = strictRows(body.get("rows"));
        requireCanonicalChashes(rows);
        repo.appendManifestChunks(tenant, docId, collection, rows);
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
     *
     * <p>Optional {@code "sweep": true} (nexus-eslkl / T2
     * nexus/design-eslkl-hook-lock-narrowing §8.1, default {@code false} —
     * BACKWARD COMPATIBLE, an omitted field is byte-for-byte the pre-eslkl
     * behaviour): folds nexus-39upx's superseded-vector sweep into each
     * doc's own transaction server-side. See
     * {@link CatalogRepository#writeManifestMany(String, List, String, Map, boolean)}
     * for the exact guard. Adds {@code swept}/{@code sweep_skipped}/
     * {@code sweep_detail} to the response, zero/empty when
     * {@code sweep=false}.
     *
     * <p><b>RDR-191 (Hal ruling 2026-08-12).</b> Top-level {@code
     * "collection"} is REQUIRED ALWAYS — not merely when {@code chunks} is
     * present — and applies to every doc in this call (the caller already
     * batches per collection). Missing or blank -> 400. The engine stamps it
     * verbatim on every manifest row; it never infers one.
     *
     * <p><b>nexus-kl2z6 increment 1 — the COMBINED WRITE.</b> Optional
     * {@code "chunks": [{"chash", "text", "metadata"}, ...]}.
     * When {@code chunks} is present, each doc's manifest write AND the
     * chunk VECTOR rows its own manifest references land in the SAME
     * per-doc transaction (T2 {@code design-kl2z6-combined-write} §0) via
     * {@link dev.nexus.service.db.CombinedWriteService}. Absent {@code
     * chunks} is BACKWARD COMPATIBLE, byte-for-byte the pre-kl2z6 path
     * (design memo §5.1) — including the response shape: {@code
     * chunks_written} appears ONLY when this call carried {@code chunks}
     * (design memo §5.2 — the ONLY runtime signal a client has that this
     * engine understands the combined write; an old engine silently drops
     * the unknown {@code chunks} field with no error). Optional {@code
     * "force_re_embed"} mirrors {@code /v1/vectors/upsert-chunks}' escape
     * from the RDR-181 existence-partition. 503 when no {@link
     * dev.nexus.service.db.CombinedWriteService} is wired (matches {@link
     * VectorHandler}'s absent-backend pattern).
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
        // RDR-191 (Hal ruling 2026-08-12): top-level 'collection' is REQUIRED
        // ALWAYS, not only when 'chunks' is present — it applies to every doc
        // in this call (the caller already batches per collection). Validated
        // up front, same discipline as the per-doc rows validation below.
        String collection = requireCollection(exchange, body);
        if (collection == null) return;
        // Validate every doc's rows up front — the whole batch 400s before
        // ANY per-doc transaction runs (nexus-z4skl: no more reason-less
        // failed_doc_ids for a malformed chash).
        for (int d = 0; d < docs.size(); d++) {
            try {
                // strictRows (not castRows): the repo re-extracts the ORIGINAL
                // rows list, so a silently-filtered junk element here would
                // reappear mid-transaction — reject the shape up front.
                requireCanonicalChashes(strictRows(docs.get(d).get("rows")));
            } catch (IllegalArgumentException e) {
                throw new IllegalArgumentException("docs[" + d + "]." + e.getMessage());
            }
        }
        // nexus-5xn3k.2 (memo §3.3): optional {"complete": {doc_id: content_hash}}
        // stamps completion inside the SAME per-doc transaction — no extra
        // round trip on the hot flush-grain repo path.
        Map<String, String> complete = null;
        Object rawComplete = body.get("complete");
        if (rawComplete instanceof Map<?, ?> m) {
            complete = new LinkedHashMap<>();
            for (var e : m.entrySet()) {
                // stacked-review item 4: a JSON null value (e.g. {"doc_id": null})
                // must NOT become the 4-character string "null" via
                // String.valueOf(e.getValue()) — that would silently stamp a
                // bogus literal content_hash. Treat a null value as ABSENT: the
                // doc_id is simply not in the completion set, same as if the
                // caller had omitted the key entirely.
                if (e.getValue() == null) continue;
                complete.put(String.valueOf(e.getKey()), String.valueOf(e.getValue()));
            }
        }
        // ABSENT/null/non-Boolean means false — same "explicit true only" idiom
        // as handleAssignMany's cross_collection default (only inverted: sweep
        // is opt-IN, cross_collection is opt-OUT), so an in-flight client
        // built against the pre-eslkl 3-arg shape gets identical behaviour.
        boolean sweep = Boolean.TRUE.equals(body.get("sweep"));

        // nexus-kl2z6 increment 1: optional combined-write payload. `chunks`
        // ABSENT (the common case, and every pre-kl2z6 caller) takes the
        // untouched byte-for-byte-compatible path below.
        Object rawChunks = body.get("chunks");
        if (rawChunks != null) {
            if (combinedWriteService == null) {
                HttpUtil.send(exchange, 503, "{\"error\":\"combined write not configured"
                    + " (no CombinedWriteService)\"}");
                return;
            }
            if (!(rawChunks instanceof List<?> cl)) {
                HttpUtil.send(exchange, 400, "{\"error\":\"'chunks' must be a list\"}"); return;
            }
            // 'collection' already validated above (required unconditionally).
            List<Map<String, Object>> chunks = new ArrayList<>(cl.size());
            for (int i = 0; i < cl.size(); i++) {
                if (!(cl.get(i) instanceof Map)) {
                    HttpUtil.send(exchange, 400,
                        "{\"error\":\"chunks[" + i + "]: every element must be an object\"}"); return;
                }
                Map<String, Object> chunk = new LinkedHashMap<>((Map<String, Object>) cl.get(i));
                Object chashObj = chunk.get("chash");
                if (!(chashObj instanceof String chashStr)) {
                    HttpUtil.send(exchange, 400,
                        "{\"error\":\"chunks[" + i + "]: 'chash' required (string)\"}"); return;
                }
                try {
                    chunk.put("chash", dev.nexus.service.db.Chash.requireCanonical(chashStr, "chunks[" + i + "]"));
                } catch (IllegalArgumentException e) {
                    HttpUtil.send(exchange, 400,
                        "{\"error\":" + MAPPER.writeValueAsString(e.getMessage()) + "}"); return;
                }
                if (!(chunk.get("text") instanceof String)) {
                    HttpUtil.send(exchange, 400,
                        "{\"error\":\"chunks[" + i + "]: 'text' required (string)\"}"); return;
                }
                chunks.add(chunk);
            }
            boolean forceReEmbed = Boolean.TRUE.equals(body.get("force_re_embed"));
            var combined = combinedWriteService.writeManyCombined(
                tenant, collection, chunks, docs, complete, sweep, forceReEmbed);
            if (combined.tokens() > 0) {
                exchange.getResponseHeaders().set(
                    VectorHandler.USAGE_TOKENS_HEADER, Long.toString(combined.tokens()));
            }
            HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(combined.response()));
            return;
        }

        var result = repo.writeManifestMany(tenant, docs, collection, complete, sweep);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    /**
     * GET /v1/catalog/manifest/get?doc_id=X
     *
     * <p>nexus-kzso5 (RDR-191 follow-up, ADDITIVE wire change): each row in
     * {@code rows} now also carries the row's own {@code collection} —
     * {@link dev.nexus.service.db.CatalogRepository#getManifest}'s
     * caller-supplied, NOT NULL RDR-191 truth, independent of the owning
     * document's {@code physical_collection}. Old clients that don't know
     * the key simply ignore it.
     */
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
        // nexus-ir6eh: the count is a TRUNCATION DEFENCE, not a convenience.
        // The client classifies chunks absent from this list as orphans and the
        // indexer GC deletes them, so a PARTIALLY-delivered list silently
        // destroys live data. A fully-missing list is already guarded client-side
        // (indexer manifest_empty_skipping_gc); a short one was not detectable at
        // all. The client reconciles len(chashes) == count before any orphan
        // classification and aborts on mismatch.
        var list = new ArrayList<>(chashes);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("chashes", list, "count", list.size())));
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
        // nexus-uu4b9: cap the IN-list well under PostgreSQL's 32767-parameter
        // Bind limit (mirrors handleManifestGetMany's identical guard below) —
        // this endpoint had NO cap, so an unbounded chash list went straight
        // into a jOOQ IN toward that hard limit.
        if (chashes.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many chashes (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var docs = repo.docsForChashes(tenant, chashes);
        // nexus-ocf52: this list is the union guard for the superseded-vector
        // sweep — a chash is hard-deleted from T3 iff no tumbler here
        // references it, so a partially-delivered list silently destroys a
        // live shared row. The client reconciles len(tumblers) == count before
        // any delete decision (same contract as nexus-ir6eh's chashes/count
        // in handleManifestChashes above).
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("tumblers", docs, "count", docs.size())));
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
     *
     * <p>nexus-kzso5 (RDR-191 follow-up, ADDITIVE wire change): each row now
     * also carries the row's own {@code collection} — same field, same
     * rationale as {@link #handleManifestGet}'s javadoc above. Old clients
     * that don't know the key simply ignore it.
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
            HttpUtil.send(exchange, 200, "{\"manifests\":{},\"count\":0}"); return;
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
        // nexus-b9puj: same union-guard chain as nexus-ocf52 (handleDocsForChashes),
        // one hop deeper — get_manifests fails loud on a page FAILURE (500) but
        // cannot detect a silently truncated page. The count lets the client
        // reconcile manifests.size() == count before trusting a page as complete.
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("manifests", manifests, "count", manifests.size())));
    }

    /**
     * POST /v1/catalog/manifest/chashes_many (nexus-eslkl / T2
     * nexus/design-eslkl-hook-lock-narrowing §8.1)
     *
     * <p>Batch-fetch just the chash list for multiple doc_ids in ONE round
     * trip — the batched twin of the superseded-vector sweep's per-doc
     * "before" read ({@code reader.get_chunk_chashes(doc_id)},
     * {@code mcp_infra.py:1465}). Same shape as {@link #handleManifestGetMany}
     * but chash-only (no position/line/char payload) — the sweep needs
     * nothing else, and the lighter response collapses the client's N
     * before-reads (one per flushed doc) to 1 call per flush.
     *
     * <p>Request body:  {@code {"doc_ids": ["tumbler1", "tumbler2", ...]}}
     * Response body:   {@code {"chashes": {"tumbler1": [chash...], "tumbler2": [chash...]}, "count": N}}
     *
     * <p>Doc_ids with no manifest rows are absent from the response map.
     * {@code count} is the number of doc_ids present in the map (same
     * truncation-defense convention as {@code handleManifestGetMany}'s
     * {@code manifests.size()}), not the total chash count.
     */
    @SuppressWarnings("unchecked")
    private void handleManifestChashesMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("doc_ids");
        List<String> docIds = raw instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        if (docIds.isEmpty()) {
            HttpUtil.send(exchange, 200, "{\"chashes\":{},\"count\":0}"); return;
        }
        // Same cap + rationale as handleManifestGetMany/handleResolveMany: well under
        // PostgreSQL's 32767-parameter Bind limit.
        if (docIds.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many doc_ids (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var chashesByDoc = repo.getChunkChashesMany(tenant, docIds);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("chashes", chashesByDoc, "count", chashesByDoc.size())));
    }

    /**
     * GET /v1/catalog/resolve_span?span_chash=<hex32>&collection=<name>  (nexus-njrcn.4)
     *
     * <p>Resolves a chunk chash (64-hex canonical only — the legacy 32-hex
     * chash_alias resolution route was retired at nexus-lgdel.l1) within a
     * specific collection to its text and metadata. The client parses the
     * full span string client-side and sends only the truncated chash +
     * collection so the server does a simple keyed lookup.
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
     * <p>Globally resolves a chunk chash (64-hex canonical, RDR-180; across all dim tables) to its text,
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
        if (rejectIfBadOwnerPrefix(exchange, (String) body.get("tumbler_prefix"))) return;
        repo.upsertOwner(tenant, body);
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
    }

    /**
     * GET /v1/catalog/owners/list[?include_deactivated=true] — nexus-cw262: the
     * audit escape hatch. Default (param absent or any value other than
     * {@code "true"}) excludes deactivated owners.
     */
    private void handleOwnerList(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        boolean includeDeactivated = "true".equals(queryParam(exchange, "include_deactivated"));
        var owners = repo.listOwners(tenant, includeDeactivated);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("owners", owners)));
    }

    /**
     * POST /v1/catalog/owners/deactivate {"tumbler_prefix": X} — nexus-cw262: the
     * 7kl32 dead-owner GC mutation arm's engine half. Soft-delete, mirrors {@code
     * /delete}'s shape (idempotent 0-return; reversible). Response: {@code
     * {"deactivated": 0|1}}.
     */
    private void handleOwnerDeactivate(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String prefix = (String) body.get("tumbler_prefix");
        if (prefix == null || prefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'tumbler_prefix' required\"}"); return;
        }
        int deactivated = repo.deactivateOwner(tenant, prefix);
        HttpUtil.send(exchange, 200, "{\"deactivated\":" + deactivated + "}");
    }

    /**
     * POST /v1/catalog/owners/reactivate {"tumbler_prefix": X} — nexus-cw262: the
     * explicit manual-correction path (clears deactivated_at without a full
     * re-registration). Response: {@code {"reactivated": 0|1}}.
     */
    private void handleOwnerReactivate(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String prefix = (String) body.get("tumbler_prefix");
        if (prefix == null || prefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'tumbler_prefix' required\"}"); return;
        }
        int reactivated = repo.reactivateOwner(tenant, prefix);
        HttpUtil.send(exchange, 200, "{\"reactivated\":" + reactivated + "}");
    }

    /**
     * POST /v1/catalog/owners/sweep_next_seq_drift — the nexus-0ehwe item 5 converge verb.
     *
     * <p>Floors every drifted owner's {@code next_seq} in the tenant to its own high-water
     * mark in one pass and reports which owners were actually below it, so a drift
     * incident's blast radius is KNOWN rather than guessed (nexus-pbawi's owner 1.12 was
     * found only because an operator happened to suspect it). No request body is read; the
     * sweep always covers the whole tenant.
     */
    private void handleOwnersSweepNextSeqDrift(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var report = repo.sweepNextSeqDrift(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(report));
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
        // nexus-g8z8n: the three preconditions the retired local implementation enforced
        // and this route had none of. A bare UPDATE replies 200 {"updated":0} for every
        // one of them, and the client discarded the count — so all three were SILENT.
        // Guarded here rather than in the repo to match the sibling verb
        // handleCollectionRename, which already carries its 404 (nexus-hz785) and 409
        // (nexus-gaou3) the same way.
        //
        // Guard 0 — a collection cannot supersede itself. The result would be a row
        // pointing at its own name: permanently excluded from collectionForTuple (which
        // skips anything with superseded_by set) with nothing to redirect to. Its sibling
        // handleCollectionRename refuses the identical old==new case for the same reason.
        if (name.equals(supersededBy)) {
            HttpUtil.send(exchange, 400,
                "{\"error\":" + MAPPER.writeValueAsString(
                    "a collection cannot supersede itself: " + name) + "}"); return;
        }
        // Guard 1 — old_name must be registered. Superseding a name that does not exist
        // is a typo on an explicit destructive-ish action; it must fail loud, not no-op.
        Map<String, Object> oldRow = repo.getCollection(tenant, name);
        if (oldRow == null) {
            HttpUtil.send(exchange, 404,
                "{\"error\":" + MAPPER.writeValueAsString("collection not found: " + name) + "}"); return;
        }
        // Guard 3 — superseded_by must name a registered, LIVE collection. This used to
        // be repo.collectionExists(tenant, supersededBy) — "a row exists, tombstone
        // included" — but a RETIRED target is exactly a pointer nothing can usefully
        // follow: supersede(X -> Y) where Y already has superseded_by = Z builds the
        // two-hop unaudited chain guard 2 (below) refuses from the SOURCE side. Reading
        // the row directly gives the same 404 for "unregistered" plus a distinct 409 for
        // "registered but itself retired", naming the target's own superseded_by so the
        // caller can follow the real chain instead (nexus-laa8j).
        //
        // nexus-v6za0's lesson applies here too: superseded_by alone cannot distinguish a
        // RENAME tombstone (left empty, its children already re-homed by
        // renameCollectionTxn step 3) from a SUPERSEDE tombstone (left fully populated —
        // supersedeCollection is a pure UPDATE that never touches chunks). That
        // distinction does not change the answer HERE: either shape is a target nothing
        // should chain a fresh supersede onto, so both refuse identically with this same
        // 409. (It matters only for handleCollectionRename's REVIVE decision above, which
        // asks a different question — may an EMPTY tombstone be resurrected — not "may a
        // new supersede point at this name".)
        //
        // Disposition of the handler's other collectionExists-family reads, so the next
        // reader does not have to re-derive it (1232585d shipped this one silently, which
        // is what produced this bead): the rename SOURCE guard a few lines below (nexus-
        // c29vr) and the rename TARGET guard (nexus-cecqy/nexus-v6za0) already read
        // getCollection()+superseded_by directly, not collectionExists; the cross_model
        // converse guard (nexus-tnx48, keyed on !tgtLive) is the opposite polarity on
        // purpose — it demands the target be LIVE — and is correct as written. This was
        // the last remaining direct repo.collectionExists call in this handler.
        Map<String, Object> targetRow = repo.getCollection(tenant, supersededBy);
        if (targetRow == null) {
            HttpUtil.send(exchange, 404,
                "{\"error\":" + MAPPER.writeValueAsString(
                    "superseded_by names an unregistered collection: " + supersededBy) + "}"); return;
        }
        Object targetSupersededObj = targetRow.get("superseded_by");
        String targetRetiredBy = targetSupersededObj instanceof String s ? s : "";
        if (!targetRetiredBy.isEmpty()) {
            HttpUtil.send(exchange, 409,
                "{\"error\":" + MAPPER.writeValueAsString(
                    "superseded_by names a retired collection: " + supersededBy
                    + " is itself superseded by " + targetRetiredBy
                    + "; refusing to build an unaudited two-hop chain") + "}"); return;
        }
        // Guard 2 — refuse to CHAIN a second supersession. Re-asserting the SAME target
        // is idempotent, not a chain: the canonical rename now tombstones X -> Y itself
        // (nexus-cecqy) and its caller then issues supersede(X, Y), which must succeed.
        // A DIFFERENT target would rewrite the supersession chain unaudited.
        Object current = oldRow.get("superseded_by");
        String currentBy = current instanceof String s ? s : "";
        if (!currentBy.isEmpty()) {
            if (!currentBy.equals(supersededBy)) {
                HttpUtil.send(exchange, 409,
                    "{\"error\":" + MAPPER.writeValueAsString(
                        "collection " + name + " is already superseded by " + currentBy
                        + "; refusing to chain a second supersede to " + supersededBy) + "}"); return;
            }
            // Same target: the desired state already holds. Reply as though one row was
            // marked (the caller's contract is "is it superseded to Y", and its CLI gates
            // its CollectionSuperseded message on a non-zero count) but do NOT re-run the
            // UPDATE — re-stamping superseded_at would move the supersession's recorded
            // instant every time the operation is retried.
            HttpUtil.send(exchange, 200, "{\"updated\":1}"); return;
        }
        int updated = repo.supersedeCollection(tenant, name, supersededBy, supersededAt != null ? supersededAt : "");
        if (updated == 0) {
            // The UPDATE carries guard 2's precondition in its WHERE, so zero rows here
            // means the row we just read has already changed. That precondition rules out
            // exactly ONE cause — a concurrent supersede to a DIFFERENT target — but it is
            // not the only way to get zero. POST /collections/delete hard-deletes the
            // registry row, and a concurrent delete between guard 1's read and this UPDATE
            // also matches nothing, where the correct answer is 404 (the row is gone), not
            // a 409 naming a supersession that never happened (nexus-0svvu). Asserting a
            // specific cause the code did not observe is the same honesty failure these
            // comments criticise elsewhere — so ask, rather than assert.
            Map<String, Object> recheck = repo.getCollection(tenant, name);
            if (recheck == null) {
                HttpUtil.send(exchange, 404,
                    "{\"error\":" + MAPPER.writeValueAsString(
                        "collection not found: " + name) + "}"); return;
            }
            Object recheckSuperseded = recheck.get("superseded_by");
            String actualSupersededBy = recheckSuperseded instanceof String s ? s : "";
            HttpUtil.send(exchange, 409,
                "{\"error\":" + MAPPER.writeValueAsString(
                    "collection " + name + " was superseded to " + actualSupersededBy
                    + " concurrently; refusing to chain a second supersede to "
                    + supersededBy) + "}"); return;
        }
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
        // Guard 0 (nexus-mxzxs) — old == new. Mirrors handleCollectionSupersede's guard 0,
        // whose comment already claimed this verb refused the case. That was only ever
        // INCIDENTALLY true: collectionExists(newName) was true when newName == oldName, so
        // the collision guard caught it. Widening that guard to liveCollectionExists
        // (nexus-u4e20) removed the cover for tombstones, and X->X onto a tombstoned X would
        // revive it in step 1 and then have step 3 stamp superseded_by = X ON X — the
        // self-superseded row guard 0 calls fatal.
        if (oldName.equals(newName)) {
            HttpUtil.send(exchange, 400,
                "{\"error\":" + MAPPER.writeValueAsString(
                    "a collection cannot be renamed to itself: " + oldName) + "}"); return;
        }
        // nexus-hz785: a rename of an unregistered collection used to return 200 with all-zero
        // counts (insert-select copies 0 rows, every child UPDATE touches 0) — a silent no-op on
        // a typo. Fail loud with a legible 404 instead.
        //
        // ONE tri-state read per name (nexus-u4e20 review): absent / retired / live. The
        // separate collectionExists + liveCollectionExists calls this replaces were two
        // connections and two snapshots that could disagree under concurrency, and reading
        // superseded_by directly is what makes the identity check below possible at all.
        Map<String, Object> srcRow = repo.getCollection(tenant, oldName);
        if (srcRow == null) {
            HttpUtil.send(exchange, 404,
                "{\"error\":" + MAPPER.writeValueAsString("collection not found: " + oldName) + "}"); return;
        }
        // nexus-c29vr — the SOURCE must be live. renameCollectionTxn step 1's INSERT copies
        // superseded_by/at from the source row, so renaming a RETIRED X onto a free name Y
        // gives Y superseded_by = X's target: born dead, permanently invisible to
        // collectionForTuple, with all of X's data re-homed onto it. The repo-side select-list
        // now clears those columns explicitly, but the request is still meaningless — refuse
        // it here and name the remedy rather than silently reviving a retired collection under
        // a new name. (The engine must not depend on which CLI verb the operator typed:
        // rename_collection_cmd checks superseded_by, rename_collection_data_plane — the
        // unattended indexer path — checks only T3 chunk presence.)
        //
        // Scoped to !crossModel (review of 351874c5): the rationale above is a CANONICAL-branch
        // fact. The cross_model COPY branch returns after two UPDATEs — it never runs step 1,
        // never inserts a registry row, never touches superseded_by — so a retired source is
        // harmless there, and repointing a dead collection's documents onto its live successor
        // is exactly the repair the RDR-162 flow exists for. Blocking it would degrade
        // remap_collection_references to a warn-and-continue that leaves catalog_documents
        // pointing at the dead source.
        boolean crossModel = Boolean.TRUE.equals(body.get("cross_model"));
        String srcSuperseded = (String) srcRow.get("superseded_by");
        if (!crossModel && srcSuperseded != null && !srcSuperseded.isEmpty()) {
            HttpUtil.send(exchange, 409,
                "{\"error\":" + MAPPER.writeValueAsString("source collection " + oldName
                    + " is retired (superseded by " + srcSuperseded + "); renaming it would carry the "
                    + "supersession onto the new name. Rename " + srcSuperseded + " instead, or clear the "
                    + "supersession first.") + "}"); return;
        }
        // nexus-gaou3: if new_name is ALREADY a registered collection, renameCollection silently
        // takes the RDR-162 cross-model COPY branch (repoints catalog_documents ONLY; chunks/
        // taxonomy/aspects are NOT moved). That is correct ONLY for the deliberate cross-model
        // migrate. A plain rename onto an existing collection is a collision: fail loud with 409
        // unless the caller opts into the COPY branch via cross_model:true.
        Map<String, Object> tgtRow = repo.getCollection(tenant, newName);
        String tgtSuperseded = tgtRow == null ? null : (String) tgtRow.get("superseded_by");
        boolean tgtRetired = tgtRow != null && tgtSuperseded != null && !tgtSuperseded.isEmpty();
        boolean tgtLive = tgtRow != null && !tgtRetired;
        // nexus-cecqy: LIVE, not merely present. A rename now retires the old name as a
        // superseded tombstone, so "a row exists at newName" stopped meaning "the name is
        // taken". This guard used collectionExists (any row) while renameCollection selects
        // its branch on a LIVE target, and the disagreement made undoing a rename
        // impossible over HTTP: rename X->Y, then rename Y->X, and the tombstone left at X
        // by the first call 409'd the second.
        if (!crossModel && tgtLive) {
            HttpUtil.send(exchange, 409,
                "{\"error\":" + MAPPER.writeValueAsString("target collection already exists: " + newName
                    + " (pass cross_model:true only for a deliberate cross-model repoint)") + "}"); return;
        }
        // nexus-v6za0 — NOT every tombstone is empty, and only ONE kind may be revived.
        // A tombstone has two provenances:
        //   (a) renameCollectionTxn step 3 leaves an EMPTY one (children re-homed first).
        //       superseded_by names the collection they moved to, so on the undo it equals
        //       oldName. This is the round trip nexus-u4e20 exists to restore.
        //   (b) POST /collections/supersede leaves a FULLY POPULATED one — supersedeCollection
        //       is a pure UPDATE that never touches chunks, and a model migration keeps all
        //       its data until an explicit later purge.
        // Reviving (b) runs the canonical FULL-REHOME: step 1's upsert overwrites the target's
        // embedding_model/owner/content_type with the source's and clears its superseded_by
        // (erasing the chain unaudited), then step 2 re-homes the source's chunks ON TOP of the
        // target's existing rows — two collections merged under one name, across two vector
        // spaces. Gate the revive on the tombstone's IDENTITY, not merely its non-liveness.
        if (!crossModel && tgtRetired) {
            // TWO conditions, and the EMPTINESS one is the load-bearing half.
            //
            // Identity alone was the 351874c5 fix and it was WRONG: superseded_by is written
            // identically by renameCollectionTxn step 3 and by supersedeCollection, so it
            // discriminates DIRECTION, not PROVENANCE. supersede(X->Y) then rename(Y->X)
            // identity-matches, and X is fully populated because supersede is a pure UPDATE —
            // the canonical branch then merges two collections. Proven with a probe: expected
            // 409, got 200. That is the same class as the liveness proxy it replaced.
            //
            // So ask the data. Emptiness is what the canonical branch actually requires, since
            // step 1 revives the row and step 2 re-homes the source's children on top of
            // whatever is there. Identity is kept as the second condition because reviving a
            // tombstone that points somewhere ELSE would erase an audit pointer even when no
            // data is at risk — the unaudited chain rewrite handleCollectionSupersede guard 2
            // refuses. Both must hold; the message names which one failed.
            boolean identityOk = oldName.equals(tgtSuperseded);
            // nexus-34wrg option (c): name WHICH table blocked so an operator can tell an
            // audit breadcrumb (relevance_log/search_telemetry/hook_failures/gc_audit) from
            // real data, rather than being told to "purge" a collection that already holds
            // nothing.
            var blocker = repo.blockingTable(tenant, newName);
            boolean empty = blocker.isEmpty();
            if (!identityOk || !empty) {
                HttpUtil.send(exchange, 409,
                    "{\"error\":" + MAPPER.writeValueAsString("target collection " + newName
                        + " is retired (superseded by " + tgtSuperseded + ") and cannot be revived by "
                        + "this rename: "
                        + (!empty
                            ? "it still holds " + blocker.get().describe() + ", so renaming onto it "
                              + "would merge two collections. Purge or restore it first."
                            : "it was retired in favour of " + tgtSuperseded + ", not of " + oldName
                              + ", so reviving it here would erase that supersession unaudited. "
                              + "Unwind the rename chain one hop at a time.")) + "}"); return;
            }
        }
        // The converse mismatch (nexus-tnx48). cross_model:true means the RDR-162 COPY branch,
        // whose whole premise is that the target already exists AND IS LIVE (the ETL just
        // populated it) so only catalog_documents needs repointing. Any NON-LIVE target —
        // absent or retired — makes renameCollection take the canonical FULL-REHOME branch
        // instead, moving chunks, taxonomy and aspects that the flag promises not to touch.
        // Fail loud rather than silently do the more destructive thing under a flag that says
        // otherwise. (Guarding on !tgtLive, not on "retired AND present": the absent case took
        // the same destructive branch and was not covered.)
        if (crossModel && !tgtLive) {
            HttpUtil.send(exchange, 409,
                "{\"error\":" + MAPPER.writeValueAsString("target collection " + newName
                    + (tgtRetired ? " is retired (superseded)" : " does not exist")
                    + ", not a live cross-model target. cross_model:true repoints documents onto an "
                    + "existing LIVE collection; it cannot revive a tombstone or create a collection. "
                    + "Drop cross_model for a plain rename.") + "}"); return;
        }
        // nexus-2sovp: thread OUR observation of the target's superseded_by into the
        // transaction's additive identity belt, rather than let the txn re-derive it —
        // "the caller's observation is what's verified". tgtSuperseded is null when the
        // target was absent at this read (nothing to verify) and the txn's belt is inert
        // whenever the target turns out live or absent anyway; it only compares when the
        // target is STILL a non-live tombstone at commit time, same as this handler's own
        // identityOk/empty gate above.
        Map<String, Integer> counts = repo.renameCollection(tenant, oldName, newName, tgtSuperseded);
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
        // nexus-v3w9n Amendment 2: the ETL import path is the "+ any import
        // route carrying tumbler_prefix" case named in the design of record —
        // an explicit tumbler_prefix here is just as much an external producer
        // as /owners/upsert. ALL-OR-NOTHING: every row is validated before the
        // single batch repository call below, so one bad row anywhere refuses
        // the whole import with zero partial writes.
        for (Map<String, Object> row : rows) {
            if (rejectIfBadOwnerPrefix(exchange, (String) row.get("tumbler_prefix"))) return;
        }
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
        // nexus-v3w9n Amendment 2 fix round 1 (substantive-critic ship-blocker):
        // this route writes an explicit per-row "tumbler" verbatim, exactly the
        // legacy /register document-half shape that produced the 1.1/1.2 phantom
        // rows the bead exists to fix — it was missing from every enumeration of
        // validated routes. ALL-OR-NOTHING: every row is validated before the
        // single batch repository call, so one bad row anywhere in the batch
        // refuses the whole import with zero partial writes (same invariant as
        // handleImportOwner / handleRegisterMany below).
        for (Map<String, Object> row : rows) {
            if (rejectIfBadDocumentTumbler(exchange, (String) row.get("tumbler"))) return;
        }
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
        try {
            repo.importLinksBatch(tenant, rows);
        } catch (CatalogRepository.DanglingEndpointException e) {
            // nexus-tk070.p1 review fix 1 (RDR-194 § D2): SAME wire shape as
            // handleLink's dangling_endpoint 400 — without this local catch
            // the exception (an IllegalArgumentException) would still reach
            // the outer dispatch ladder's generic `catch (IllegalArgumentException)`
            // and produce a 400, but WITHOUT the machine-readable `code`/`missing`
            // fields ETL callers key on.
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("error", e.getMessage());
            payload.put("code", "dangling_endpoint");
            payload.put("missing", e.missing());
            HttpUtil.send(exchange, 400, MAPPER.writeValueAsString(payload));
            return;
        }
        HttpUtil.send(exchange, 200, "{\"imported\":" + rows.size() + "}");
    }

    private void handleImportChunk(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        // RDR-191 (Hal ruling 2026-08-12): this writes the same
        // catalog_document_chunks manifest table as /manifest/write et al.,
        // so it carries the SAME caller-supplied-collection discipline.
        String collection = requireCollection(exchange, body);
        if (collection == null) return;
        // nexus-e0hd2 (critique seam): same table + same bare-chash extraction
        // as the manifest writes — same boundary treatment.
        List<Map<String, Object>> rows = strictRows(body.get("rows"));
        requireCanonicalChashes(rows);
        repo.importChunksBatch(tenant, docId, collection, rows);
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
     * Response: {"tumbler": "1.1.3", "created": true}
     *
     * <p>Uses SELECT ... FOR UPDATE on catalog_owners.next_seq to atomically claim
     * the next sequence number. {@code created} (nexus-vfef0, additive) is {@code
     * true} only when THIS call's INSERT minted the row; {@code false} for an
     * idempotency-leg hit or a concurrent first-put race loser — see {@link
     * CatalogRepository.RegisterOutcome}.
     */
    private void handleDocRegister(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String ownerPrefix = (String) body.get("owner_prefix");
        if (ownerPrefix == null || ownerPrefix.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'owner_prefix' required\"}"); return;
        }
        // nexus-v3w9n Amendment 2: owner_prefix is ALWAYS an explicit,
        // required, externally-supplied prefix on this route (never the
        // owners/upsert auto-mint path), so it is validated unconditionally;
        // an explicit "tumbler" in the body is optional (the mint path
        // ignores it) but validated too when present.
        if (rejectIfBadOwnerPrefix(exchange, ownerPrefix)) return;
        if (rejectIfBadDocumentTumbler(exchange, (String) body.get("tumbler"))) return;
        var outcome = repo.registerDocumentWithOutcome(tenant, ownerPrefix, body);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("tumbler", outcome.tumbler(), "created", outcome.created())));
    }

    /**
     * POST /v1/catalog/doc/register_many — batch-register N documents under one owner
     * in a single transaction, returning their tumblers in INPUT ORDER (nexus-9dvqy,
     * duoak.11 sink #2).
     *
     * <p>Body: {"owner_prefix": "1.1", "docs": [{"title": ..., "file_path": ...}, ...]}
     * Response: {"tumblers": ["1.1.3", "1.1.4", ...], "created": [true, false, ...]}
     * (both aligned 1:1 with docs)
     *
     * <p>Existing (idempotent) docs return their current tumbler and consume no
     * sequence number; only new docs draw from the contiguous block claimed under
     * one owner-row FOR UPDATE lock. Capped at {@value #MAX_BATCH_DOC_IDS} rows to
     * stay under PostgreSQL's 32767-parameter Bind limit (~24 cols/row). {@code
     * created} (nexus-vfef0, additive) mirrors the single-doc route's signal
     * per entry — see {@link CatalogRepository.RegisterOutcome}.
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
        // nexus-v3w9n Amendment 2: same validation as the single-doc route —
        // owner_prefix unconditionally, each doc's optional explicit
        // "tumbler" when present. ALL-OR-NOTHING: every doc is validated
        // before the single batch repository call below, so one bad doc
        // anywhere in the batch refuses the whole call with zero partial
        // writes.
        if (rejectIfBadOwnerPrefix(exchange, ownerPrefix)) return;
        for (Map<String, Object> doc : docs) {
            if (rejectIfBadDocumentTumbler(exchange, (String) doc.get("tumbler"))) return;
        }
        var outcomes = repo.registerDocumentManyWithOutcome(tenant, ownerPrefix, docs);
        List<String> tumblers = outcomes.stream()
            .map(CatalogRepository.RegisterOutcome::tumbler).toList();
        List<Boolean> created = outcomes.stream()
            .map(CatalogRepository.RegisterOutcome::created).toList();
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("tumblers", tumblers, "created", created)));
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

    /**
     * POST /v1/catalog/owners/by_type {"owner_type": X, "include_deactivated": bool}
     * — list owners filtered by owner_type. nexus-cw262: {@code include_deactivated}
     * defaults false (excludes deactivated owners) — this is the exact read path the
     * 7kl32 census and doctor's git-hooks dead-owner attribution both use.
     */
    private void handleOwnerByType(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String ownerType = (String) body.get("owner_type");
        if (ownerType == null || ownerType.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"owner_type required\"}"); return;
        }
        Object includeDeactivatedRaw = body.get("include_deactivated");
        boolean includeDeactivated = includeDeactivatedRaw instanceof Boolean b && b;
        var owners = repo.ownersByType(tenant, ownerType, includeDeactivated);
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

    /**
     * GET /v1/catalog/docs/collection-counts-all — {physical_collection: doc_count}
     * for all non-empty collections, INCLUDING soft-tombstoned documents (still
     * restorable until {@code purge_trash}) — see {@link
     * CatalogRepository#collectionDocCountsIncludingDeleted}.
     *
     * <p>nexus-8tnz2 fix-round-2 EXTENSION: this is a brand-new route path,
     * not a query param on {@link #handleDocsCollectionCounts}, precisely
     * because a query param bolted onto an EXISTING route is
     * indistinguishable from "old engine ignored it" — an old engine would
     * return HTTP 200 with silently live-only data, and the client has no
     * way to detect it was talking to a pre-upgrade engine. A new path
     * cleanly 404s on an old engine instead, matching the established
     * convention already used by {@link #handleManifestNullCollection} /
     * {@link CatalogRepository#manifestNullCollectionReport} for exactly
     * this problem. Callers (t3_orphans.py's orphan-vs-tombstoned
     * classifier) MUST treat a 404 here as "engine below the required
     * floor" and refuse to classify, never fall back to live-only counts.
     */
    private void handleDocsCollectionCountsAll(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var counts = repo.collectionDocCountsIncludingDeleted(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("counts", counts)));
    }

    /** GET /v1/catalog/docs/orphaned — documents with no incoming or outgoing links. */
    private void handleDocsOrphaned(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var docs = repo.orphanedDocs(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("documents", docs)));
    }

    /**
     * GET /v1/catalog/links/orphaned — links with an endpoint that resolves to no
     * live document (nexus-ysrwi, GH #1419 issue 7).
     *
     * <p>Mirrors {@link #handleDocsOrphaned} in shape. Each row carries {@code side}
     * ("from" / "to" / "both") so the caller can distinguish a deleted target from
     * a deleted source without a follow-up query.
     */
    private void handleLinksOrphaned(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var links = repo.orphanedLinks(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("links", links, "count", links.size())));
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
     *
     * <p>Capped at {@value #MAX_BATCH_DOC_IDS} (found during
     * CatalogHandlerEnvelopeConformanceGateTest authorship: this batch
     * endpoint's doc_ids flowed straight into a jOOQ {@code .in(...)} with no
     * size guard, unlike every sibling batch endpoint — same bind-limit
     * rationale as handleManifestGetMany/handleResolveMany).
     */
    @SuppressWarnings("unchecked")
    private void handleDocChunkCounts(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawIds = body.get("doc_ids");
        List<String> docIds = rawIds instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        if (docIds.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many doc_ids (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var counts = repo.chunkCountsForDocs(tenant, docIds);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(counts));
    }

    /**
     * POST /v1/catalog/links/from-batch — batch outbound links for a set of tumblers.
     *
     * <p>Request: {"tumblers": ["1.1.1", "1.1.2", ...]}
     * Response: {"1.1.1": [{"from_tumbler": "1.1.1", "link_type": "cites"}], ...}
     *
     * <p>Capped at {@value #MAX_BATCH_DOC_IDS} (found during
     * CatalogHandlerEnvelopeConformanceGateTest authorship: same missing-guard
     * shape as handleDocChunkCounts above).
     */
    @SuppressWarnings("unchecked")
    private void handleLinksFromBatch(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object rawT = body.get("tumblers");
        List<String> tumblers = rawT instanceof List<?> l
            ? l.stream().filter(o -> o instanceof String).map(o -> (String) o).toList()
            : List.of();
        if (tumblers.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many tumblers (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        var links = repo.linksFromBatch(tenant, tumblers);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(links));
    }

    /**
     * GET /v1/catalog/manifest/null_collection — read-only census of the
     * manifest population the (now-retired, RDR-191 Phase 6, nexus-o8dil.33)
     * {@code manifest_orphans}/{@code manifest_verify_all} functions
     * structurally could never see: rows with {@code collection IS NULL}
     * (T2 nexus/chroma-residue-plan-2026-08-10 §C2). EXPLICITLY NOT RETIRED
     * alongside them — the manifest-chunk FK does not cover NULL-collection
     * rows (MATCH SIMPLE exempts them), so this census remains the ONLY
     * visibility into that permanently-unenforced population.
     *
     * <p>Response: {@code {"total": <n>, "backfillable": <m>}}. {@code
     * total} is every live-document manifest row with a NULL collection;
     * {@code backfillable} named the subset the now-retired {@code
     * nexus.manifest_backfill()} would have stamped (the owning document HAS
     * a physical_collection) — historical framing, since that stamping
     * function no longer exists; the field itself is unchanged. {@code
     * total - backfillable} is PERMANENTLY excluded (ghost/sourceless
     * documents) — see {@link CatalogRepository#manifestNullCollectionReport}'s
     * javadoc. NEVER mutates.
     */
    private void handleManifestNullCollection(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var report = repo.manifestNullCollectionReport(tenant);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("total", report.get("total"),
                   "backfillable", report.get("backfillable"))));
    }

    /**
     * GET /v1/catalog/chash/conformance?dim=384 — per-table chash width-
     * conformance report (RDR-180, bead nexus-du2dw): the engine-route
     * counterpart to the local-only nexus_diag psql probe, for managed/cloud
     * installs with no direct substrate access.
     *
     * <p>Response: {@code {"dim": <d>, "tables": [{"table_name", "total",
     * "non_conformant", "sample_chashes"}, ...]}}. Tenant-scoped (SECURITY
     * INVOKER + FORCE RLS on the underlying tables) — see {@link
     * CatalogRepository#chashConformanceReport} for why that is the correct
     * scoping model here. An unsupported dim is a 400.
     */
    private void handleChashConformance(HttpExchange exchange, String tenant, String method) throws IOException {
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
        // requireSupportedDim inside the repo throws IllegalArgumentException for an
        // unsupported dim — caught by the outer dispatch-level handler (→ 400).
        List<Map<String, Object>> tables = repo.chashConformanceReport(tenant, dim);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(
            Map.of("dim", dim, "tables", tables)));
    }

    // ══════════════════════════════════════════════════════════════════════════
    // INDEX RUN FENCE (RUNFENCE, nexus-5xn3k.2) — memo §3.3
    //
    // RDR-191 Phase 6 (nexus-o8dil.33): the read-only diagnostic handlers
    // handleManifestVerify (GET /manifest/verify) and handleManifestVerifyAll
    // (GET /manifest/verify_all) were retired here — the manifest-chunk FK
    // (catalog-029) makes the dangling state they detected unreachable.
    // completeIndexRun below still calls through manifestVerifyCtx to the
    // SAME underlying nexus.manifest_verify(text) SQL function for its own,
    // different, write-path completeness question — that function and call
    // path are NOT removed.
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * POST /v1/catalog/index-run/begin  {doc_id, content_hash, run_id, collection}
     * Idempotent. Stamps index_state='indexing' before any chunk work.
     */
    private void handleIndexRunBegin(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        String contentHash = (String) body.get("content_hash");
        String runId       = (String) body.get("run_id");
        String collection  = (String) body.get("collection");
        repo.beginIndexRun(tenant, docId, contentHash, runId, collection);
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
    }

    /**
     * POST /v1/catalog/index-run/begin-many  {docs: [{doc_id, content_hash, run_id}], collection}
     * Batch idempotent index_state='indexing' stamp (nexus-vw594 F1) — one HTTP
     * round trip covering an entire ChunkBatcher flush instead of one round
     * trip per file. See {@link CatalogRepository#beginIndexRunMany}.
     */
    @SuppressWarnings("unchecked")
    private void handleIndexRunBeginMany(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        Object raw = body.get("docs");
        List<Map<String, Object>> docs = raw instanceof List<?> l
            ? l.stream().filter(o -> o instanceof Map<?, ?>).map(o -> (Map<String, Object>) o).toList()
            : List.of();
        if (docs.size() > MAX_BATCH_DOC_IDS) {
            HttpUtil.send(exchange, 400, "{\"error\":\"too many docs (max "
                + MAX_BATCH_DOC_IDS + ")\"}"); return;
        }
        String collection = (String) body.get("collection");
        var result = repo.beginIndexRunMany(tenant, docs, collection);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    /**
     * POST /v1/catalog/index-run/complete  {doc_id, content_hash, chunk_count}
     * FAIL-CLOSED verify-then-stamp — see {@link CatalogRepository#completeIndexRun}.
     * A refusal is caught in {@link #handle} ({@code IndexRunVerifyRefused}) and
     * mapped to 409 with the counts.
     */
    private void handleIndexRunComplete(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        String contentHash = (String) body.get("content_hash");
        if (!(body.get("chunk_count") instanceof Number)) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'chunk_count' (integer) required\"}"); return;
        }
        int chunkCount = ((Number) body.get("chunk_count")).intValue();
        var result = repo.completeIndexRun(tenant, docId, contentHash, chunkCount);
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(result));
    }

    /** POST /v1/catalog/index-run/fail  {doc_id, error} */
    private void handleIndexRunFail(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        String docId = (String) body.get("doc_id");
        if (docId == null || docId.isBlank()) {
            HttpUtil.send(exchange, 400, "{\"error\":\"'doc_id' required\"}"); return;
        }
        String error = (String) body.get("error");
        repo.failIndexRun(tenant, docId, error);
        HttpUtil.send(exchange, 200, "{\"ok\":true}");
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

    /**
     * POST /v1/catalog/gc_audit/record — append ONE destructive-T3-op audit row
     * (nexus-jqvzk).
     *
     * <p>Body: {@code {operation, collection?, actor?, dry_run?, chashes?[],
     * details?{}}}. Response: {@code {"id": <bigint>}}. A missing/blank
     * {@code operation} is a 400 (the outer IllegalArgumentException ladder).
     */
    private void handleGcAuditRecord(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"POST".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        Map<String, Object> body = readBody(exchange);
        long id = repo.recordGcAudit(tenant, body);
        HttpUtil.send(exchange, 200, "{\"id\":" + id + "}");
    }

    /**
     * GET /v1/catalog/gc_audit/list?collection=&amp;operation=&amp;limit=&amp;offset=
     * — the audit trail, newest first (nexus-jqvzk).
     *
     * <p>Response: {@code {"entries": [{id, operation, collection, actor,
     * dry_run, chash_count, chashes, details, created_at}, ...]}}.
     */
    private void handleGcAuditList(HttpExchange exchange, String tenant, String method) throws IOException {
        if (!"GET".equals(method)) { HttpUtil.send(exchange, 405, "{\"error\":\"method not allowed\"}"); return; }
        var entries = repo.listGcAudit(tenant,
            queryParam(exchange, "collection"),
            queryParam(exchange, "operation"),
            intParam(exchange, "limit", 100),
            intParam(exchange, "offset", 0));
        HttpUtil.send(exchange, 200, MAPPER.writeValueAsString(Map.of("entries", entries)));
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

    /**
     * Boolean query param. Absent/blank yields *def*, which every caller sets
     * to the PRE-EXISTING behaviour so an older client that sends no param is
     * unaffected (nexus-ekaxn: {@code follow_alias}).
     */
    private boolean boolParam(HttpExchange exchange, String key, boolean def) {
        String v = queryParam(exchange, key);
        if (v == null || v.isBlank()) return def;
        return "1".equals(v) || "true".equalsIgnoreCase(v) || "yes".equalsIgnoreCase(v);
    }

    /**
     * Strict variant of {@link #castRows} for the manifest WRITE paths
     * (nexus-z4skl review M-1): {@code castRows} silently FILTERS non-Map
     * elements, but {@code writeManifestMany} re-extracts the ORIGINAL list
     * repo-side — so a junk element (null, bare string) validated-away here
     * would reappear mid-transaction and die reason-less into
     * failed_doc_ids, the exact failure mode this bead kills. Rejecting the
     * shape up front also keeps 400 row indices aligned with the caller's
     * actual JSON array (review L-1).
     */
    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> strictRows(Object raw) {
        if (raw == null) {
            return List.of();
        }
        if (!(raw instanceof List<?> l)) {
            throw new IllegalArgumentException("'rows' must be a list");
        }
        for (int i = 0; i < l.size(); i++) {
            if (!(l.get(i) instanceof Map)) {
                throw new IllegalArgumentException(
                    "rows[" + i + "]: every row must be an object, got "
                    + (l.get(i) == null ? "null" : l.get(i).getClass().getSimpleName()));
            }
        }
        return l.stream().map(o -> (Map<String, Object>) o).toList();
    }

    /**
     * Parse-don't-validate every row's {@code chash} at the HTTP boundary
     * (nexus-z4skl lineage, polarity inverted by RDR-180): the canonical
     * form is the FULL 64-hex sha256 digest; a bare 32-hex value is a
     * legacy (pre-flip) reference with no resolution route left
     * (nexus-lgdel.l1: chash_alias is retired) — never a write. Historically
     * a malformed chash sailed through the handlers and
     * only tripped the DB CHECK deep inside a per-row transaction, where
     * batch writers swallowed it reason-less into failed_doc_ids (3
     * deploy-gate iterations on the v0.1.24 probe). Now it
     * is a uniform 400 carrying the offending length, BEFORE any transaction.
     * The parsed value is written back canonically; repositories downstream
     * see only validated chashes.
     *
     * @throws IllegalArgumentException mapped to 400 by the dispatch catch.
     */
    private static void requireCanonicalChashes(List<Map<String, Object>> rows) {
        for (int i = 0; i < rows.size(); i++) {
            Object v = rows.get(i).get("chash");
            if (!(v instanceof String s)) {
                throw new IllegalArgumentException(
                    "rows[" + i + "]: 'chash' required (string)");
            }
            rows.get(i).put("chash",
                dev.nexus.service.db.Chash.requireCanonical(s, "rows[" + i + "]"));
        }
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
