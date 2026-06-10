// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantScope;
import org.jooq.Record;
import org.jooq.Result;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * RDR-155 Phase 2 - vector operations repository backed by pgvector
 * ({@code nexus.chunks_384} / {@code nexus.chunks_768} / {@code nexus.chunks_1024})
 * instead of Chroma.
 *
 * <p>Implemented by P2.2 (bead nexus-tqeg6) against the locked P2.1 contract suite
 * ({@code PgVectorRepositoryContractTest}, bead nexus-duf53).
 *
 * <p>Contract (RDR-155 Proposed Solution / Query path):
 * <ul>
 *   <li><strong>Tenant scoping.</strong> Every operation takes an explicit {@code tenant}
 *       and executes inside {@link TenantScope#withTenant} so the {@code nexus.tenant} GUC
 *       stamps the transaction and FORCE RLS scopes every row. Unlike the Chroma-backed
 *       {@link VectorRepository} (where collection names were the access boundary), RLS is
 *       the tenant boundary here.
 *   <li><strong>Runtime per-dim dispatch.</strong> The collection-name embedding-model
 *       segment (RDR-103 collection-name authority, third {@code __}-separated segment)
 *       selects the physical table: {@code voyage-code-3} / {@code voyage-context-3} /
 *       {@code voyage-3} to {@code chunks_1024}; {@code bge-base-en-v15-768} to
 *       {@code chunks_768}; {@code minilm-l6-v2-384} to {@code chunks_384}. Unknown model
 *       segments FAIL LOUD ({@link IllegalArgumentException}) - never a silent fallback
 *       (RDR-109 hazard class).
 *   <li><strong>Collection is a column.</strong> Multi-collection reads are a filtered
 *       union ({@code collection IN (...)}), not N separate stores.
 *   <li><strong>Server-side embed unchanged.</strong> Chunk TEXT comes in; this class embeds
 *       via the injected embedders and stores the vector. This is a storage/ANN swap, not a
 *       chunking/embedding rewrite: texts pass through verbatim with zero transformation.
 *       <strong>Wiring caveat (Seam B):</strong> collection-aware routing only happens
 *       through the {@link EmbedderRouter} constructor - {@code EmbedderRouter.embed()}
 *       (the plain {@link Embedder} interface) always falls back to ONNX regardless of
 *       collection. Production wiring MUST use the router constructor (exactly like the
 *       Chroma {@link VectorRepository}); wiring a router through the plain-Embedder
 *       constructor would produce 384-dim ONNX vectors for 1024-dim collections (caught
 *       fail-loud by the dim check, but only at the first upsert). With the router
 *       constructor the embedding path is identical to the Chroma path's
 *       {@code embedForCollection}, so the RDR-152 Phase 3 Seam B embedding-equivalence
 *       parity gate stays the verification seam - not waived.
 *   <li><strong>Manifest join (RDR-108).</strong> {@link #fetchDocumentChunks} resolves
 *       {@code catalog_documents.tumbler -> catalog_document_chunks(collection, chash) ->
 *       chunks_<dim>} entirely in-database, replacing the cross-store lookup. Referential
 *       integrity is application-enforced (RDR-155 P1.G decision,
 *       T2 nexus_rdr/155-manifest-fk-decision): the write paths enforce existence, and the
 *       read path fails loud on unresolvable manifest rows instead of silently returning
 *       a partial document.
 *   <li><strong>Filtered-ANN session setting.</strong> {@link #search} runs with
 *       {@code SET LOCAL hnsw.iterative_scan = 'relaxed_order'} so HNSW keeps scanning
 *       past {@code ef_search} when RLS + collection + metadata predicates narrow the
 *       candidate set (RDR-155 research resolution; txn-local, pool-safe - same
 *       {@code SET LOCAL} discipline as the TenantScope GUC stamp).
 * </ul>
 *
 * <p>The Chroma-backed {@link VectorRepository} stays RUNNABLE through Phase 3 as the
 * hybrid-parity comparand (plan invariant 3); Phase 4a retires it.
 *
 * <p><strong>P4a seam note:</strong> this class shares no interface with the Chroma
 * {@link VectorRepository} and its methods take an explicit {@code tenant} first parameter
 * (RLS is the tenant boundary here; Chroma had none). The Phase 4a serving cutover must
 * either introduce a port interface or rewrite {@code VectorHandler}'s call sites - it is
 * NOT a drop-in substitution. Recorded on the P4a impl bead (nexus-1k8s1).
 */
public final class PgVectorRepository {

    private static final Logger log = LoggerFactory.getLogger(PgVectorRepository.class);

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /**
     * RDR-103 model-segment to dimension registry. Mirrors the Python authorities:
     * {@code corpus.py CANONICAL_EMBEDDING_MODELS} (voyage tokens, 1024) and
     * {@code LOCAL_EMBEDDING_MODELS} (local tokens, dim encoded in the suffix).
     */
    private static final Map<String, Integer> MODEL_DIMS = Map.of(
            "voyage-code-3",       1024,
            "voyage-context-3",    1024,
            "voyage-3",            1024,
            "bge-base-en-v15-768",  768,
            "minilm-l6-v2-384",     384);

    private final TenantScope    tenantScope;
    private final Embedder       docEmbedder;
    private final Embedder       queryEmbedder;
    private final EmbedderRouter docRouter;      // nullable; preferred over docEmbedder
    private final EmbedderRouter queryRouter;    // nullable; preferred over queryEmbedder

    /**
     * Simple constructor: no collection-aware routing (single fixed embedder - test
     * fixtures and single-model local mode).
     *
     * @param tenantScope   the ONLY DSLContext factory - every operation runs inside
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
        this.docRouter     = null;
        this.queryRouter   = null;
    }

    /**
     * Collection-aware constructor - the PRODUCTION wiring (Seam B). Routes each
     * embed call by collection prefix via {@link EmbedderRouter#embedForCollection},
     * exactly like the Chroma {@link VectorRepository} path.
     *
     * @param tenantScope the ONLY DSLContext factory
     * @param docRouter   collection-aware embedder router for document indexing
     * @param queryRouter collection-aware embedder router for query embedding
     */
    public PgVectorRepository(TenantScope tenantScope, EmbedderRouter docRouter,
                              EmbedderRouter queryRouter) {
        this.tenantScope   = tenantScope;
        this.docEmbedder   = docRouter;   // EmbedderRouter implements Embedder (ONNX fallback)
        this.queryEmbedder = queryRouter;
        this.docRouter     = docRouter;
        this.queryRouter   = queryRouter;
    }

    /**
     * Resolve the pgvector table dimension for a collection name by parsing the
     * embedding-model segment (RDR-103 collection-name authority).
     *
     * <p>Known model tokens (the canonical + local registries in {@code corpus.py}):
     * <ul>
     *   <li>{@code voyage-code-3}, {@code voyage-context-3}, {@code voyage-3}: 1024
     *   <li>{@code bge-base-en-v15-768}: 768
     *   <li>{@code minilm-l6-v2-384}: 384
     * </ul>
     *
     * @param collection four-segment conformant collection name
     *                   ({@code <content_type>__<owner>__<model>__v<n>})
     * @return 384, 768, or 1024
     * @throws IllegalArgumentException if the name is not four-segment conformant or the
     *                                  model segment is not a known token (fail loud -
     *                                  no silent fallback dimension)
     */
    public static int dimForCollection(String collection) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("collection must not be null or blank");
        }
        String[] segments = collection.split("__");
        if (segments.length != 4) {
            throw new IllegalArgumentException(
                "collection '" + collection + "' is not four-segment conformant "
                + "(<content_type>__<owner>__<model>__v<n>)");
        }
        Integer dim = MODEL_DIMS.get(segments[2]);
        if (dim == null) {
            throw new IllegalArgumentException(
                "unknown embedding-model segment '" + segments[2] + "' in collection '"
                + collection + "' - known tokens: " + MODEL_DIMS.keySet());
        }
        return dim;
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
     *       (ON CONFLICT update - Chroma upsert semantics).
     *   <li>Empty {@code ids} is a no-op.
     *   <li>A vector whose dimension does not match the dispatched table fails loud
     *       and writes nothing.
     * </ul>
     *
     * @param tenant     tenant principal for RLS scoping
     * @param collection four-segment conformant collection name (drives dim dispatch)
     * @param ids        chunk natural IDs (sha256(text)[:32] - the chash)
     * @param documents  chunk texts (embedded server-side)
     * @param metadatas  per-chunk metadata maps (stored as JSONB; may contain nulls)
     */
    public void upsertChunks(String tenant, String collection,
                             List<String> ids,
                             List<String> documents,
                             List<Map<String, Object>> metadatas) {
        if (ids.isEmpty()) return;
        int dim = dimForCollection(collection);

        // De-duplicate IDs (first-wins, matching T3Database._write_batch). Also required
        // for correctness: ON CONFLICT cannot affect the same row twice within one
        // statement's snapshot, and the batch shares a transaction.
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

        // Server-side embed - collection-aware routing when wired with the router
        // (production / Seam B path), identical to the Chroma VectorRepository flow.
        List<float[]> embeddings = (docRouter != null)
                ? docRouter.embedForCollection(collection, dedupDocs)
                : docEmbedder.embed(dedupDocs);

        // Fail loud BEFORE any SQL if the embedder's output does not match the
        // dispatched table dimension (no truncation, no padding).
        for (float[] vec : embeddings) {
            if (vec.length != dim) {
                throw new IllegalArgumentException(
                    "embedder produced a " + vec.length + "-dim vector for collection '"
                    + collection + "' which dispatches to chunks_" + dim);
            }
        }

        String table = chunksTable(dim);
        tenantScope.withTenant(tenant, ctx -> {
            for (int i = 0; i < dedupIds.size(); i++) {
                ctx.execute(
                    "INSERT INTO " + table
                    + " (tenant_id, collection, chash, chunk_text, embedding, metadata)"
                    + " VALUES (?, ?, ?, ?, ?::vector, ?::jsonb)"
                    + " ON CONFLICT (tenant_id, collection, chash) DO UPDATE SET"
                    + "   chunk_text = EXCLUDED.chunk_text,"
                    + "   embedding  = EXCLUDED.embedding,"
                    + "   metadata   = EXCLUDED.metadata",
                    tenant, collection, dedupIds.get(i), dedupDocs.get(i),
                    vectorLiteral(embeddings.get(i)), toJson(dedupMetas.get(i)));
            }
            return null;
        });
        log.debug("event=upsert_chunks_done collection={} table={} count={}",
                collection, table, dedupIds.size());
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
        if (collectionNames == null || collectionNames.isEmpty()) {
            return List.of();
        }
        int dim = dimForCollection(collectionNames.get(0));
        for (String col : collectionNames) {
            int colDim = dimForCollection(col);
            if (colDim != dim) {
                throw new IllegalArgumentException(
                    "mixed dimensions in one search call: '" + collectionNames.get(0)
                    + "' is " + dim + "-dim but '" + col + "' is " + colDim
                    + "-dim - one query vector cannot serve both spaces");
            }
        }

        // Route by the first collection - the same-dim check above guarantees the set is
        // homogeneous, and the Python client never mixes embedder families in one call
        // (same convention as the Chroma path).
        float[] queryVec = (queryRouter != null)
                ? queryRouter.embedOneForCollection(collectionNames.get(0), queryText)
                : queryEmbedder.embedOne(queryText);
        if (queryVec.length != dim) {
            throw new IllegalArgumentException(
                "query embedder produced a " + queryVec.length
                + "-dim vector but the collections dispatch to chunks_" + dim);
        }

        StringBuilder sql = new StringBuilder()
            .append("SELECT chash, chunk_text, collection, metadata::text AS metadata_json,")
            .append(" (embedding <=> ?::vector) AS distance")
            .append(" FROM ").append(chunksTable(dim))
            .append(" WHERE collection IN (").append(placeholders(collectionNames.size())).append(")");
        List<Object> binds = new ArrayList<>();
        binds.add(vectorLiteral(queryVec));
        binds.addAll(collectionNames);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                sql.append(" AND metadata->>? = ?");
                binds.add(e.getKey());
                binds.add(String.valueOf(e.getValue()));
            }
        }
        sql.append(" ORDER BY distance ASC, chash ASC LIMIT ?");
        binds.add(nResults);

        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            // Filtered-ANN recall: keep HNSW scanning past ef_search when the RLS +
            // collection + metadata predicates narrow the candidate set. SET LOCAL is
            // txn-scoped (same pool discipline as the TenantScope GUC stamp).
            ctx.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'");
            return ctx.fetch(sql.toString(), binds.toArray());
        });

        List<Map<String, Object>> rows = new ArrayList<>(result.size());
        for (Record rec : result) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id",         rec.get("chash", String.class));
            row.put("content",    rec.get("chunk_text", String.class));
            row.put("distance",   rec.get("distance", Double.class));
            row.put("collection", rec.get("collection", String.class));
            row.putAll(fromJson(rec.get("metadata_json", String.class)));
            rows.add(row);
        }
        return rows;
    }

    /**
     * RDR-155 Phase 3 - hybrid search: text signals ({@code tsvector} FTS + {@code pg_trgm}
     * trigram similarity) gate the candidate set, vector cosine distance ranks it, fused in
     * ONE query against the dispatched {@code chunks_<dim>} table. Replaces the engine's
     * legacy FTS5 + Chroma two-path fusion.
     *
     * <p>Implemented by P3.2 (bead nexus-eap5l) against the locked P3.1 contract suite
     * ({@code PgVectorHybridSearchContractTest} + {@code HybridParityIntegrationTest},
     * bead nexus-sbvg0).
     *
     * <p>Contract pinned by the P3.1 suite (RDR-155 §Query path Hybrid search; aligned with
     * the conexus xr7.8.7 fused reference that the xr7.8.9 go-live gate drives):
     * <ul>
     *   <li><strong>Text gate.</strong> A returned row must match at least one text signal:
     *       {@code chunk_tsv @@ plainto_tsquery('english', queryText)} OR trigram similarity
     *       between {@code queryText} and {@code chunk_text} above the implementation's
     *       threshold. A row with NO text signal never appears, however close its vector -
     *       semantic-only retrieval stays on {@link #search}. Zero text candidates returns
     *       an empty list (no silent vector fallback).
     *   <li><strong>Vector rank.</strong> Candidates are ordered by cosine distance
     *       ({@code embedding <=> query}) ascending, {@code chash} ascending on ties - the
     *       same ordering contract as {@link #search}.
     *   <li><strong>Trigram rescue.</strong> The {@code pg_trgm} leg exists for queries the
     *       english stemmer mishandles (typos, identifiers): a query that matches no FTS
     *       lexeme still returns rows whose text is trigram-similar.
     *   <li><strong>Same envelope as {@link #search}.</strong> Tenant RLS scope, per-dim
     *       dispatch with mixed-dim fail-loud, {@code collection IN (...)} multi-collection
     *       union, metadata {@code where} equality predicates ANDed with the text gate,
     *       {@code nResults} cap, flat row shape ({@code id}, {@code content},
     *       {@code distance}, {@code collection}, metadata flattened in).
     *   <li><strong>Filtered-ANN session setting.</strong> The implementation MUST run
     *       {@code SET LOCAL hnsw.iterative_scan = 'relaxed_order'} before the query,
     *       exactly like {@link #search} - the text gate + RLS + {@code where} predicates
     *       narrow the candidate set even harder than plain search, which is precisely the
     *       filtered-recall risk the setting exists for (RDR-155 research resolution; the
     *       fixture-scale suite cannot detect its absence, the conexus xr7.8.9
     *       production-scale recall gate can).
     *   <li><strong>Trigram gate calibration anchor.</strong> The contract fixture
     *       pins the gate's discriminating range, not an exact threshold: the typo probe's
     *       candidate rows sit at word-similarity ≈ 0.9 (and plain trigram similarity
     *       ≈ 0.5 against these short fixture texts) and MUST pass; the no-signal rows sit
     *       at ≈ 0.1 and MUST NOT. <strong>P3.2 decision (recorded):</strong>
     *       {@code queryText <% chunk_text} (word_similarity) with
     *       {@code SET LOCAL pg_trgm.word_similarity_threshold = 0.6} - the operator form
     *       is gin_trgm_ops-indexable (vectors-002) where the function-call form is not;
     *       word_similarity (vs plain similarity) does not dilute with chunk length; the
     *       per-transaction pin removes cluster-config dependence. P3.G cross-checks this
     *       against the conexus xr7.8.9 production-scale calibration.
     * </ul>
     *
     * @param tenant          tenant principal for RLS scoping
     * @param queryText       search query - used for BOTH the text gate and the
     *                        server-side query embedding
     * @param collectionNames collection names to search (filtered union, single query)
     * @param nResults        maximum rows returned
     * @param where           optional metadata equality predicates (ANDed); null/empty = none
     * @return text-gated rows sorted by cosine distance ascending; same flat row shape
     *         as {@link #search}
     */
    public List<Map<String, Object>> hybridSearch(String tenant, String queryText,
                                                  List<String> collectionNames,
                                                  int nResults,
                                                  Map<String, Object> where) {
        if (collectionNames == null || collectionNames.isEmpty()) {
            return List.of();
        }
        int dim = dimForCollection(collectionNames.get(0));
        for (String col : collectionNames) {
            int colDim = dimForCollection(col);
            if (colDim != dim) {
                throw new IllegalArgumentException(
                    "mixed dimensions in one hybrid-search call: '" + collectionNames.get(0)
                    + "' is " + dim + "-dim but '" + col + "' is " + colDim
                    + "-dim - one query vector cannot serve both spaces");
            }
        }

        float[] queryVec = (queryRouter != null)
                ? queryRouter.embedOneForCollection(collectionNames.get(0), queryText)
                : queryEmbedder.embedOne(queryText);
        if (queryVec.length != dim) {
            throw new IllegalArgumentException(
                "query embedder produced a " + queryVec.length
                + "-dim vector but the collections dispatch to chunks_" + dim);
        }

        // Text gate: FTS lexeme match OR word-trigram similarity. The <% operator form
        // (word_similarity(query, chunk_text) >= pg_trgm.word_similarity_threshold) is
        // chosen over the explicit function call because only the operator family is
        // supported by the gin_trgm_ops index (vectors-002); the threshold GUC is pinned
        // per-transaction below so the gate never depends on cluster config.
        // word_similarity (not plain similarity): plain similarity dilutes with
        // chunk_text length and would silently kill the trgm leg on production-size
        // chunks; word_similarity matches the query against the best continuous extent.
        StringBuilder sql = new StringBuilder()
            .append("SELECT chash, chunk_text, collection, metadata::text AS metadata_json,")
            .append(" (embedding <=> ?::vector) AS distance")
            .append(" FROM ").append(chunksTable(dim))
            .append(" WHERE collection IN (").append(placeholders(collectionNames.size())).append(")")
            .append(" AND (chunk_tsv @@ plainto_tsquery('english', ?) OR ? <% chunk_text)");
        List<Object> binds = new ArrayList<>();
        binds.add(vectorLiteral(queryVec));
        binds.addAll(collectionNames);
        binds.add(queryText);
        binds.add(queryText);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                sql.append(" AND metadata->>? = ?");
                binds.add(e.getKey());
                binds.add(String.valueOf(e.getValue()));
            }
        }
        sql.append(" ORDER BY distance ASC, chash ASC LIMIT ?");
        binds.add(nResults);

        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            // Filtered-ANN recall: the text gate + RLS + where predicates narrow the
            // candidate set even harder than plain search - keep HNSW scanning past
            // ef_search (contract requirement; SET LOCAL is txn-scoped, pool-safe).
            ctx.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'");
            // Trigram gate calibration (contract anchor): word_similarity >= 0.6,
            // pg_trgm's default - typo-probe candidates sit at ~0.9 and pass, no-signal
            // rows at ~0.1 do not. Pinned per-transaction so the gate is independent of
            // cluster-level GUC configuration.
            ctx.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.6");
            return ctx.fetch(sql.toString(), binds.toArray());
        });

        List<Map<String, Object>> rows = new ArrayList<>(result.size());
        for (Record rec : result) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id",         rec.get("chash", String.class));
            row.put("content",    rec.get("chunk_text", String.class));
            row.put("distance",   rec.get("distance", Double.class));
            row.put("collection", rec.get("collection", String.class));
            row.putAll(fromJson(rec.get("metadata_json", String.class)));
            rows.add(row);
        }
        return rows;
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
        int dim = dimForCollection(collection);
        if (ids == null || ids.isEmpty()) {
            return Map.of("ids", List.of(), "documents", List.of(), "metadatas", List.of());
        }

        List<Object> binds = new ArrayList<>();
        binds.add(collection);
        binds.addAll(ids);
        binds.add(limit);
        binds.add(offset);
        Result<Record> result = tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("SELECT chash, chunk_text, metadata::text AS metadata_json FROM "
                      + chunksTable(dim)
                      + " WHERE collection = ? AND chash IN (" + placeholders(ids.size()) + ")"
                      + " ORDER BY chash ASC LIMIT ? OFFSET ?",
                      binds.toArray()));

        List<String> outIds = new ArrayList<>(result.size());
        List<String> outDocs = new ArrayList<>(result.size());
        List<Map<String, Object>> outMetas = new ArrayList<>(result.size());
        for (Record rec : result) {
            outIds.add(rec.get("chash", String.class));
            outDocs.add(rec.get("chunk_text", String.class));
            outMetas.add(fromJson(rec.get("metadata_json", String.class)));
        }
        return Map.of("ids", outIds, "documents", outDocs, "metadatas", outMetas);
    }

    /**
     * List entries in a collection (metadata only), paginated by chash ordering.
     *
     * @return Chroma-style envelope {@code {ids: List<String>, metadatas: List<Map>}}
     */
    public Map<String, Object> list(String tenant, String collection,
                                    int limit, int offset) {
        int dim = dimForCollection(collection);
        Result<Record> result = tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("SELECT chash, metadata::text AS metadata_json FROM " + chunksTable(dim)
                      + " WHERE collection = ? ORDER BY chash ASC LIMIT ? OFFSET ?",
                      collection, limit, offset));

        List<String> outIds = new ArrayList<>(result.size());
        List<Map<String, Object>> outMetas = new ArrayList<>(result.size());
        for (Record rec : result) {
            outIds.add(rec.get("chash", String.class));
            outMetas.add(fromJson(rec.get("metadata_json", String.class)));
        }
        return Map.of("ids", outIds, "metadatas", outMetas);
    }

    /**
     * Delete chunks by ID.
     *
     * <p><strong>Manifest obligation (application-enforced FK, T2
     * nexus_rdr/155-manifest-fk-decision):</strong> callers are responsible for removing
     * or updating {@code catalog_document_chunks} rows that reference these chunks BEFORE
     * deleting them. Deleting a chunk still referenced by a manifest row creates a
     * dangling reference, and {@link #fetchDocumentChunks} on the affected document will
     * fail loud with {@link IllegalStateException}. Whether this class should pre-check
     * the manifest itself is a Phase 4a/5 write-path decision (recorded on nexus-1k8s1).
     *
     * @return number of rows actually deleted (RLS makes other tenants' rows invisible,
     *         so cross-tenant attempts delete exactly 0)
     */
    public int delete(String tenant, String collection, List<String> ids) {
        int dim = dimForCollection(collection);
        if (ids == null || ids.isEmpty()) return 0;
        List<Object> binds = new ArrayList<>();
        binds.add(collection);
        binds.addAll(ids);
        return tenantScope.withTenant(tenant, ctx ->
            ctx.execute("DELETE FROM " + chunksTable(dim)
                        + " WHERE collection = ? AND chash IN (" + placeholders(ids.size()) + ")",
                        binds.toArray()));
    }

    /**
     * Count chunks in a collection visible to {@code tenant}.
     */
    public int count(String tenant, String collection) {
        int dim = dimForCollection(collection);
        long c = tenantScope.withTenant(tenant, ctx ->
            ctx.fetchOne("SELECT count(*) FROM " + chunksTable(dim) + " WHERE collection = ?",
                         collection)
               .get(0, Long.class));
        // PG count(*) is bigint; refuse to wrap rather than silently narrow.
        if (c > Integer.MAX_VALUE) {
            throw new IllegalStateException("count overflow for collection '" + collection
                                            + "': " + c);
        }
        return (int) c;
    }

    /**
     * Metadata-only update on existing chunks - no re-embedding, {@code chunk_text} and
     * {@code embedding} unchanged (frecency reindex path, RDR-152 nexus-enehl).
     *
     * @param metadatas replacement metadata maps aligned with {@code ids}
     */
    public void updateMetadata(String tenant, String collection,
                               List<String> ids,
                               List<Map<String, Object>> metadatas) {
        int dim = dimForCollection(collection);
        if (ids == null || ids.isEmpty()) return;
        if (ids.size() != metadatas.size()) {
            throw new IllegalArgumentException(
                "ids (" + ids.size() + ") and metadatas (" + metadatas.size()
                + ") must be aligned");
        }
        tenantScope.withTenant(tenant, ctx -> {
            for (int i = 0; i < ids.size(); i++) {
                ctx.execute("UPDATE " + chunksTable(dim)
                            + " SET metadata = ?::jsonb WHERE collection = ? AND chash = ?",
                            toJson(metadatas.get(i)), collection, ids.get(i));
            }
            return null;
        });
    }

    /**
     * RDR-108 manifest join: resolve a catalog document's chunks in-database via
     * {@code catalog_documents.tumbler -> catalog_document_chunks(collection, chash) ->
     * chunks_<dim>}, ordered by manifest {@code position}.
     *
     * <p>Shared-chash semantics: two manifest positions pointing at the same chash return
     * two rows (the manifest preserves position; identical text collapses to one chunk row
     * by design - CLAUDE.md Catalog/T3 split).
     *
     * @param tenant  tenant principal for RLS scoping
     * @param tumbler the catalog document's tumbler
     * @return one row per manifest position, ordered by position ascending; each row
     *         carries {@code position}, {@code chash}, {@code chunk_text}, {@code collection}
     * @throws IllegalStateException if the tumbler does not resolve to a visible catalog
     *                               document, or any manifest row's {@code (collection,
     *                               chash)} does not resolve to a chunk row - fail loud,
     *                               never a silently partial document (application-enforced
     *                               referential check, T2 nexus_rdr/155-manifest-fk-decision)
     */
    public List<Map<String, Object>> fetchDocumentChunks(String tenant, String tumbler) {
        return tenantScope.withTenant(tenant, ctx -> {
            // 1. The document must be visible under RLS. A foreign tenant's tumbler is
            //    indistinguishable from an unknown one (no existence leak).
            Record doc = ctx.fetchOne(
                "SELECT 1 FROM nexus.catalog_documents WHERE tumbler = ?", tumbler);
            if (doc == null) {
                throw new IllegalStateException(
                    "tumbler '" + tumbler + "' does not resolve to a visible catalog document");
            }

            // 2. Manifest rows in position order.
            Result<Record> manifest = ctx.fetch(
                "SELECT position, chash, collection FROM nexus.catalog_document_chunks"
                + " WHERE doc_id = ? ORDER BY position ASC", tumbler);
            if (manifest.isEmpty()) {
                return List.of();
            }

            // 3. Resolve chunk text per collection group (each collection dispatches to
            //    its own chunks_<dim> table).
            Map<String, Set<String>> chashesByCollection = new LinkedHashMap<>();
            for (Record m : manifest) {
                String col = m.get("collection", String.class);
                if (col == null || col.isBlank()) {
                    throw new IllegalStateException(
                        "manifest row for doc '" + tumbler + "' position "
                        + m.get("position", Integer.class)
                        + " has no collection - cannot dispatch to a chunks_<dim> table"
                        + " (pre-migration manifest rows are resolved by the Phase 5 ETL)");
                }
                chashesByCollection.computeIfAbsent(col, k -> new LinkedHashSet<>())
                                   .add(m.get("chash", String.class));
            }

            Map<String, Map<String, String>> textByColThenChash = new HashMap<>();
            for (Map.Entry<String, Set<String>> e : chashesByCollection.entrySet()) {
                String col = e.getKey();
                int dim = dimForCollection(col);
                List<Object> binds = new ArrayList<>();
                binds.add(col);
                binds.addAll(e.getValue());
                Result<Record> chunks = ctx.fetch(
                    "SELECT chash, chunk_text FROM " + chunksTable(dim)
                    + " WHERE collection = ? AND chash IN (" + placeholders(e.getValue().size()) + ")",
                    binds.toArray());
                Map<String, String> byChash =
                    textByColThenChash.computeIfAbsent(col, k -> new HashMap<>());
                for (Record c : chunks) {
                    byChash.put(c.get("chash", String.class),
                                c.get("chunk_text", String.class));
                }
            }

            // 4. Walk the manifest in position order; any unresolved (collection, chash)
            //    fails loud - never a silently partial document.
            List<Map<String, Object>> rows = new ArrayList<>(manifest.size());
            for (Record m : manifest) {
                String col   = m.get("collection", String.class);
                String chash = m.get("chash", String.class);
                Map<String, String> byChash = textByColThenChash.get(col);
                String text  = byChash != null ? byChash.get(chash) : null;
                if (text == null) {
                    throw new IllegalStateException(
                        "manifest row for doc '" + tumbler + "' position "
                        + m.get("position", Integer.class) + " references (" + col + ", "
                        + chash + ") which has no chunk row - refusing to return a"
                        + " partial document (application-enforced referential check)");
                }
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("position",   m.get("position", Integer.class));
                row.put("chash",      chash);
                row.put("chunk_text", text);
                row.put("collection", col);
                rows.add(row);
            }
            return rows;
        });
    }

    // -- Internal helpers -------------------------------------------------------

    private static String chunksTable(int dim) {
        return "nexus.chunks_" + dim;
    }

    /** pgvector cast-safe text literal: {@code [f1,f2,...]}. */
    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }

    /** {@code IN}-list placeholder string: {@code ?,?,...} (n >= 1). */
    private static String placeholders(int n) {
        if (n <= 0) {
            // "IN ()" is invalid SQL - every caller must guard the empty case first.
            throw new IllegalArgumentException("placeholders requires n >= 1, got " + n);
        }
        return String.join(",", java.util.Collections.nCopies(n, "?"));
    }

    private static String toJson(Map<String, Object> metadata) {
        Map<String, Object> m = metadata != null ? metadata : Map.of();
        try {
            return MAPPER.writeValueAsString(m);
        } catch (Exception e) {
            throw new IllegalArgumentException("metadata is not JSON-serializable: " + m, e);
        }
    }

    private static Map<String, Object> fromJson(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return MAPPER.readValue(json, MAP_TYPE);
        } catch (Exception e) {
            throw new IllegalStateException("stored metadata is not valid JSON: " + json, e);
        }
    }
}
