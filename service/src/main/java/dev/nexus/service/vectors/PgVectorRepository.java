// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.TenantScope;

import java.util.List;
import java.util.Map;

/**
 * RDR-155 Phase 2 — vector operations repository backed by pgvector
 * ({@code nexus.chunks_384} / {@code nexus.chunks_768} / {@code nexus.chunks_1024})
 * instead of Chroma.
 *
 * <p><strong>P2.1 (bead nexus-duf53) ships this class as a SIGNATURE-ONLY skeleton:</strong>
 * every method throws {@link UnsupportedOperationException} until P2.2 (bead nexus-tqeg6)
 * implements the storage path. The signatures and the javadoc contract below are pinned by
 * {@code PgVectorRepositoryContractTest}; P2.2 makes that suite green without changing it.
 *
 * <p>Contract (RDR-155 §Proposed Solution, §Query path):
 * <ul>
 *   <li><strong>Tenant scoping.</strong> Every operation takes an explicit {@code tenant}
 *       and executes inside {@link TenantScope#withTenant} so the {@code nexus.tenant} GUC
 *       stamps the transaction and FORCE RLS scopes every row. Unlike the Chroma-backed
 *       {@link VectorRepository} (where collection names were the access boundary), RLS is
 *       the tenant boundary here.
 *   <li><strong>Runtime per-dim dispatch.</strong> The collection-name embedding-model
 *       segment (RDR-103 collection-name authority, third {@code __}-separated segment)
 *       selects the physical table: {@code voyage-code-3} / {@code voyage-context-3} /
 *       {@code voyage-3} → {@code chunks_1024}; {@code bge-base-en-v15-768} →
 *       {@code chunks_768}; {@code minilm-l6-v2-384} → {@code chunks_384}. Unknown model
 *       segments FAIL LOUD ({@link IllegalArgumentException}) — never a silent fallback
 *       (RDR-109 hazard class).
 *   <li><strong>Collection is a column.</strong> Multi-collection reads are a filtered
 *       union ({@code collection IN (...)}), not N separate stores.
 *   <li><strong>Server-side embed unchanged.</strong> Chunk TEXT comes in; this class embeds
 *       via the injected {@link Embedder}s and stores the vector. This is a storage/ANN swap,
 *       not a chunking/embedding rewrite (RDR-152 Phase 3 Seam B parity gate inherited).
 *   <li><strong>Manifest join (RDR-108).</strong> {@link #fetchDocumentChunks} resolves
 *       {@code catalog_documents.tumbler → catalog_document_chunks(collection, chash) →
 *       chunks_<dim>} entirely in-database, replacing the cross-store lookup. Referential
 *       integrity is application-enforced (RDR-155 P1.G decision,
 *       T2 nexus_rdr/155-manifest-fk-decision): the write paths enforce existence, and the
 *       read path fails loud on unresolvable manifest rows instead of silently returning
 *       a partial document.
 * </ul>
 *
 * <p>The Chroma-backed {@link VectorRepository} stays RUNNABLE through Phase 3 as the
 * hybrid-parity comparand (plan invariant 3); Phase 4a retires it.
 */
public final class PgVectorRepository {

    private static final String NOT_IMPLEMENTED =
            "RDR-155 P2.2 (nexus-tqeg6) implements the pgvector storage path";

    private final TenantScope tenantScope;
    private final Embedder    docEmbedder;
    private final Embedder    queryEmbedder;

    /**
     * @param tenantScope   the ONLY DSLContext factory — every operation runs inside
     *                      {@code withTenant(tenant, ...)}
     * @param docEmbedder   embedder for document indexing (input_type="document")
     * @param queryEmbedder embedder for query search (input_type="query"); may be the
     *                      same instance
     */
    public PgVectorRepository(TenantScope tenantScope, Embedder docEmbedder,
                              Embedder queryEmbedder) {
        this.tenantScope   = tenantScope;
        this.docEmbedder   = docEmbedder;
        this.queryEmbedder = queryEmbedder;
    }

    /**
     * Resolve the pgvector table dimension for a collection name by parsing the
     * embedding-model segment (RDR-103 collection-name authority).
     *
     * <p>Known model tokens (the canonical + local registries in {@code corpus.py}):
     * <ul>
     *   <li>{@code voyage-code-3}, {@code voyage-context-3}, {@code voyage-3} → 1024
     *   <li>{@code bge-base-en-v15-768} → 768
     *   <li>{@code minilm-l6-v2-384} → 384
     * </ul>
     *
     * @param collection four-segment conformant collection name
     *                   ({@code <content_type>__<owner>__<model>__v<n>})
     * @return 384, 768, or 1024
     * @throws IllegalArgumentException if the name is not four-segment conformant or the
     *                                  model segment is not a known token (fail loud —
     *                                  no silent fallback dimension)
     */
    public static int dimForCollection(String collection) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * Server-side embed + upsert into the dispatched {@code chunks_<dim>} table.
     *
     * <p>Semantics pinned by the contract suite:
     * <ul>
     *   <li>Duplicate IDs within one batch collapse first-wins (matches
     *       {@code T3Database._write_batch} and the Chroma path).
     *   <li>Re-upserting an existing {@code (tenant, collection, chash)} updates
     *       {@code chunk_text}, {@code embedding}, and {@code metadata} in place
     *       (ON CONFLICT update — Chroma upsert semantics).
     *   <li>Empty {@code ids} is a no-op.
     *   <li>A vector whose dimension does not match the dispatched table fails loud
     *       and writes nothing.
     * </ul>
     *
     * @param tenant     tenant principal for RLS scoping
     * @param collection four-segment conformant collection name (drives dim dispatch)
     * @param ids        chunk natural IDs (sha256(text)[:32] — the chash)
     * @param documents  chunk texts (embedded server-side)
     * @param metadatas  per-chunk metadata maps (stored as JSONB; may contain nulls)
     */
    public void upsertChunks(String tenant, String collection,
                             List<String> ids,
                             List<String> documents,
                             List<Map<String, Object>> metadatas) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * Semantic search: embed the query server-side, then
     * {@code ORDER BY embedding <=> $q} with the tenant RLS scope, an optional metadata
     * {@code where} predicate, and {@code collection IN (...)} for multi-collection.
     *
     * <p>All collections in one call must share a dimension (they share the query
     * embedder); mixing dims is a caller error and fails loud.
     *
     * @param tenant          tenant principal for RLS scoping
     * @param queryText       search query (embedded server-side)
     * @param collectionNames collection names to search (filtered union, single query)
     * @param nResults        maximum rows returned
     * @param where           optional metadata equality predicates (ANDed); null/empty = none
     * @return flat result rows sorted by cosine distance ascending; each row carries
     *         {@code id}, {@code content}, {@code distance}, {@code collection}, plus the
     *         chunk's metadata keys flattened in (same shape as the Chroma path's
     *         flattened rows so handlers port unchanged)
     */
    public List<Map<String, Object>> search(String tenant, String queryText,
                                            List<String> collectionNames,
                                            int nResults,
                                            Map<String, Object> where) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * Fetch specific chunk IDs from a collection.
     *
     * @return Chroma-style envelope {@code {ids: List<String>, documents: List<String>,
     *         metadatas: List<Map>}} aligned by index; IDs not present (or not visible
     *         under RLS) are omitted; {@code limit}/{@code offset} paginate in chash
     *         order (same ordering as {@link #list})
     */
    public Map<String, Object> get(String tenant, String collection,
                                   List<String> ids, int limit, int offset) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * List entries in a collection (metadata only), paginated by chash ordering.
     *
     * @return Chroma-style envelope {@code {ids: List<String>, metadatas: List<Map>}}
     */
    public Map<String, Object> list(String tenant, String collection,
                                    int limit, int offset) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * Delete chunks by ID.
     *
     * @return number of rows actually deleted (RLS makes other tenants' rows invisible,
     *         so cross-tenant attempts delete exactly 0)
     */
    public int delete(String tenant, String collection, List<String> ids) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * Count chunks in a collection visible to {@code tenant}.
     */
    public int count(String tenant, String collection) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * Metadata-only update on existing chunks — no re-embedding, {@code chunk_text} and
     * {@code embedding} unchanged (frecency reindex path, RDR-152 nexus-enehl).
     *
     * @param metadatas replacement metadata maps aligned with {@code ids}
     */
    public void updateMetadata(String tenant, String collection,
                               List<String> ids,
                               List<Map<String, Object>> metadatas) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }

    /**
     * RDR-108 manifest join: resolve a catalog document's chunks in-database via
     * {@code catalog_documents.tumbler → catalog_document_chunks(collection, chash) →
     * chunks_<dim>}, ordered by manifest {@code position}.
     *
     * <p>Shared-chash semantics: two manifest positions pointing at the same chash return
     * two rows (the manifest preserves position; identical text collapses to one chunk row
     * by design — CLAUDE.md §Catalog/T3 split).
     *
     * @param tenant  tenant principal for RLS scoping
     * @param tumbler the catalog document's tumbler
     * @return one row per manifest position, ordered by position ascending; each row
     *         carries {@code position}, {@code chash}, {@code chunk_text}, {@code collection}
     * @throws IllegalStateException if the tumbler does not resolve to a visible catalog
     *                               document, or any manifest row's {@code (collection,
     *                               chash)} does not resolve to a chunk row — fail loud,
     *                               never a silently partial document (application-enforced
     *                               referential check, T2 nexus_rdr/155-manifest-fk-decision)
     */
    public List<Map<String, Object>> fetchDocumentChunks(String tenant, String tumbler) {
        throw new UnsupportedOperationException(NOT_IMPLEMENTED);
    }
}
