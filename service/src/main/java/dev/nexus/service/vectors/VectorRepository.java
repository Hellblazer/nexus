// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-152 bead nexus-gmiaf.20 — Seam B vector operations repository.
 *
 * <p>Orchestrates: embedder + quota validation + Chroma REST client.
 *
 * <p>Tenant mapping: The {@code tenant} parameter in service endpoints is used
 * only for Postgres RLS (handled by {@code TenantScope}).  Chroma collections are
 * named by the four-segment nexus convention ({@code <content_type>__<owner>__<model>__v<n>}),
 * which already encodes scope.  The service does NOT apply per-tenant access control
 * to Chroma — Chroma collection names are the access-control boundary.
 */
public final class VectorRepository {

    private static final Logger log = LoggerFactory.getLogger(VectorRepository.class);

    private final Embedder              docEmbedder;
    private final Embedder              queryEmbedder;
    private final ChromaRestClient      chroma;
    private final ChromaQuotaValidator  quota;

    /**
     * @param docEmbedder   embedder for document indexing (input_type="document")
     * @param queryEmbedder embedder for query search (input_type="query"); may be same instance
     * @param chroma        configured Chroma REST client
     */
    public VectorRepository(Embedder docEmbedder, Embedder queryEmbedder,
                            ChromaRestClient chroma) {
        this.docEmbedder   = docEmbedder;
        this.queryEmbedder = queryEmbedder;
        this.chroma        = chroma;
        this.quota         = new ChromaQuotaValidator();
    }

    // ── upsert-chunks ─────────────────────────────────────────────────────────

    /**
     * Server-side embed + quota validate + Chroma upsert.
     *
     * <p>This is the primary Seam B write path: Python sends chunk TEXT (not vectors);
     * the service embeds + validates quota + writes to Chroma.
     *
     * @param collection collection name (four-segment conformant)
     * @param ids        chunk natural IDs (sha256(text)[:32])
     * @param documents  chunk texts (to be embedded server-side)
     * @param metadatas  per-chunk metadata maps
     */
    public void upsertChunks(String collection,
                              List<String> ids,
                              List<String> documents,
                              List<Map<String, Object>> metadatas) {
        if (ids.isEmpty()) return;

        // 1. Validate quota per record
        quota.validateBatch(ids, documents, null, metadatas);

        // 2. De-duplicate IDs (first-wins, matching T3Database._write_batch)
        List<String> dedupIds  = new ArrayList<>();
        List<String> dedupDocs = new ArrayList<>();
        List<Map<String, Object>> dedupMetas = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < ids.size(); i++) {
            if (seen.add(ids.get(i))) {
                dedupIds.add(ids.get(i));
                dedupDocs.add(documents.get(i));
                dedupMetas.add(metadatas.get(i));
            }
        }
        int collapsed = ids.size() - dedupIds.size();
        if (collapsed > 0) {
            log.info("event=upsert_dedup_collapsed collection={} received={} kept={} collapsed={}",
                    collection, ids.size(), dedupIds.size(), collapsed);
        }

        // 3. Server-side embed
        log.debug("event=upsert_chunks_embedding collection={} count={}", collection, dedupIds.size());
        List<float[]> embeddings = docEmbedder.embed(dedupDocs);

        // 4. Chroma upsert (paginated inside ChromaRestClient)
        chroma.upsert(collection, dedupIds, dedupDocs, embeddings, dedupMetas);
        log.debug("event=upsert_chunks_done collection={} count={}", collection, dedupIds.size());
    }

    // ── search ────────────────────────────────────────────────────────────────

    /**
     * Semantic search: embed query server-side, query Chroma.
     *
     * @param queryText       search query
     * @param collectionNames list of collection names to search
     * @param nResults        results per collection
     * @param where           optional metadata filter
     * @return flat sorted result list (distance ascending)
     */
    @SuppressWarnings("unchecked")
    public List<Map<String, Object>> search(String queryText,
                                             List<String> collectionNames,
                                             int nResults,
                                             Map<String, Object> where) {
        quota.validateQuery(queryText, nResults, where);

        float[] queryVec = queryEmbedder.embedOne(queryText);

        List<Map<String, Object>> results = new ArrayList<>();
        for (String colName : collectionNames) {
            int count = chroma.count(colName);
            if (count == 0) continue;

            int actualN = Math.min(nResults, Math.min(count, ChromaQuotaValidator.MAX_QUERY_RESULTS));
            try {
                Map<String, Object> qr = chroma.query(colName, queryVec, actualN, where);
                results.addAll(flattenQueryResult(colName, qr));
            } catch (Exception e) {
                log.warn("event=search_collection_failed collection={} error={}", colName, e.getMessage());
            }
        }

        results.sort((a, b) -> Double.compare(
                ((Number) a.get("distance")).doubleValue(),
                ((Number) b.get("distance")).doubleValue()));
        return results;
    }

    // ── store_put (single-chunk, MCP put path) ────────────────────────────────

    /**
     * Single-document MCP put: embed + upsert one chunk.
     *
     * @return the chunk ID (sha256(content)[:32])
     */
    public String put(String collection, String docId, String content,
                      Map<String, Object> metadata) {
        quota.validateRecord(docId, content, null, metadata);
        List<float[]> vecs = docEmbedder.embed(List.of(content));
        chroma.upsert(collection, List.of(docId), List.of(content), vecs, List.of(metadata));
        return docId;
    }

    // ── store_get ─────────────────────────────────────────────────────────────

    /**
     * Fetch specific chunk IDs from a collection.
     */
    public Map<String, Object> get(String collection, List<String> ids,
                                    int limit, int offset) {
        return chroma.get(collection, ids, limit, offset,
                List.of("documents", "metadatas"), null);
    }

    // ── store_list ────────────────────────────────────────────────────────────

    /**
     * List entries in a collection (metadata only).
     */
    public Map<String, Object> list(String collection, int limit, int offset) {
        return chroma.get(collection, null, limit, offset,
                List.of("metadatas"), null);
    }

    // ── store_delete ──────────────────────────────────────────────────────────

    /**
     * Delete chunks by ID.
     */
    public int delete(String collection, List<String> ids) {
        return chroma.delete(collection, ids);
    }

    // ── list_collections ──────────────────────────────────────────────────────

    public List<Map<String, Object>> listCollections() {
        return chroma.listCollections();
    }

    public int count(String collection) {
        return chroma.count(collection);
    }

    // ── Internal helpers ──────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> flattenQueryResult(String collectionName,
                                                          Map<String, Object> qr) {
        List<List<String>>                  idBatches   = (List<List<String>>)                  qr.get("ids");
        List<List<String>>                  docBatches  = (List<List<String>>)                  qr.get("documents");
        List<List<Map<String, Object>>>     metaBatches = (List<List<Map<String, Object>>>)     qr.get("metadatas");
        List<List<Double>>                  distBatches = (List<List<Double>>)                  qr.get("distances");

        List<Map<String, Object>> flat = new ArrayList<>();
        if (idBatches == null || idBatches.isEmpty()) return flat;

        List<String>              rowIds   = idBatches.get(0);
        List<String>              rowDocs  = docBatches  != null ? docBatches.get(0)  : List.of();
        List<Map<String, Object>> rowMetas = metaBatches != null ? metaBatches.get(0) : List.of();
        List<Double>              rowDists = distBatches != null ? distBatches.get(0) : List.of();

        for (int i = 0; i < rowIds.size(); i++) {
            Map<String, Object> row = new java.util.LinkedHashMap<>();
            row.put("id",         rowIds.get(i));
            row.put("content",    i < rowDocs.size()  ? rowDocs.get(i)  : "");
            row.put("distance",   i < rowDists.size() ? rowDists.get(i) : 0.0);
            row.put("collection", collectionName);
            if (i < rowMetas.size() && rowMetas.get(i) != null) {
                row.putAll(rowMetas.get(i));
            }
            flat.add(row);
        }
        return flat;
    }
}
