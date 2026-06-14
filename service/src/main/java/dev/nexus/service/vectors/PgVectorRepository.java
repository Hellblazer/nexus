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
 *       chunking/embedding rewrite: texts pass through verbatim with exactly ONE carve-out —
 *       NUL (0x00) bytes are stripped from chunk text and metadata strings before embed+bind,
 *       because Postgres {@code text}/{@code jsonb} physically cannot store them (Chroma and
 *       SQLite tolerated them; bead nexus-rvfwj). For NUL-bearing chunks the stored text and
 *       its embedding therefore differ from the Chroma-era original by NUL removal only; the
 *       chash remains the caller's identity and is never recomputed from the stored text.
 *       All other content is stored verbatim.
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
        // Postgres text/jsonb cannot carry NUL (0x00) — Chroma and SQLite tolerated it,
        // so legacy PDF-extraction chunks arrive with NUL noise (bead nexus-rvfwj; 62 of
        // 5,233 production dt-papers chunks). Strip NULs from chunk text and metadata
        // string values before embed+bind; without this the whole batch dies with
        // "invalid byte sequence for encoding UTF8: 0x00". The chash is the caller's
        // identity and is never recomputed from the sanitized text — affected chashes
        // are logged so the sanitization delta stays auditable.
        List<String> dedupIds  = new ArrayList<>();
        List<String> dedupDocs = new ArrayList<>();
        List<Map<String, Object>> dedupMetas = new ArrayList<>();
        List<String> nulSanitized = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (int i = 0; i < ids.size(); i++) {
            if (seen.add(ids.get(i))) {
                String doc = documents.get(i);
                String clean = stripNul(doc);
                if (!clean.equals(doc)) {
                    nulSanitized.add(ids.get(i));
                }
                dedupIds.add(ids.get(i));
                dedupDocs.add(clean);
                dedupMetas.add(sanitizeNulDeep(metadatas.get(i)));
            }
        }
        if (!nulSanitized.isEmpty()) {
            log.warn("event=upsert_nul_sanitized collection={} count={} chashes={}",
                    collection, nulSanitized.size(), String.join(",", nulSanitized));
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

        // Standing rule (RDR-156 P0.2, bead nexus-70r3c.2):
        // Collection registration precedes chunk writes — enforced server-side here
        // (auto-stub in the write transaction) and by the chunks_<dim>/chash_index/
        // topic_assignments -> catalog_collections FKs (NOT VALID until RDR-153 data
        // lands; VALIDATE is nexus-70r3c.3).  Stub rows (all metadata='') are upgraded
        // by the catalog ETL's importCollection DO UPDATE...WHERE-stub logic.
        // Never add a chunk write path that bypasses this ensure-registered step.
        String[] collSegs = collection.split("__");
        // Non-conformant path (collSegs.length != 4) is unreachable in practice:
        // dimForCollection() above fails loud for any non-four-segment name, so by
        // the time we reach this point, segments.length == 4 is guaranteed.
        // The branch is retained as defense-in-depth to produce a name-only stub
        // rather than crash if the invariant is ever violated by a future caller.
        boolean conformant = collSegs.length == 4;
        String regContentType  = conformant ? collSegs[0] : "";
        String regOwner        = conformant ? collSegs[1] : "";
        String regModel        = conformant ? collSegs[2] : "";
        String regModelVersion = conformant ? collSegs[3] : "";

        tenantScope.withTenant(tenant, ctx -> {
            // Ensure-registered: INSERT stub row for this collection before any chunk write.
            // ON CONFLICT DO NOTHING: a fully-populated row from the catalog ETL is never
            // overwritten; a stub inserted by a prior upsertChunks call is also preserved.
            ctx.execute(
                "INSERT INTO nexus.catalog_collections"
                + " (tenant_id, name, content_type, owner_id, embedding_model, model_version)"
                + " VALUES (?, ?, ?, ?, ?, ?)"
                + " ON CONFLICT (tenant_id, name) DO NOTHING",
                tenant, collection, regContentType, regOwner, regModel, regModelVersion);

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
     * Gate-selectivity cutoff for {@link #hybridSearch} plan dispatch (nexus-lcogi). At or
     * below this many text-gate matches the gate is materialized first and ranked by exact
     * distance (bounds materialization at ~{@code SELECTIVE_GATE_MAX × 4 KB} of embeddings);
     * above it, the HNSW-first plan is kept (a dense gate is found within the scan budget).
     * Heuristic — superseded by the RDR-156 P5.2 unified selectivity-aware RRF plan.
     */
    static final int SELECTIVE_GATE_MAX = 5000;

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
     *   <li><strong>Selectivity-aware dispatch (nexus-lcogi).</strong> A cheap COUNT over
     *       the text gate picks the plan. For a SELECTIVE gate ({@code count <=}
     *       {@link #SELECTIVE_GATE_MAX}) the gate is materialized FIRST via a
     *       {@code MATERIALIZED} CTE (GIN bitmap on {@code chunk_tsv} + {@code gin_trgm_ops})
     *       and the small gated set is ranked by EXACT cosine distance — the lcogi fix:
     *       the prior HNSW-first plan ({@code ORDER BY embedding}, gate as scan filter)
     *       collapsed here because a few matches in a large corpus rank past
     *       {@code hnsw.max_scan_tuples} (lcogi: 6/116k → 0). For a NON-SELECTIVE gate the
     *       HNSW-first plan is kept (a dense gate is found within the scan budget, and
     *       materializing a huge gated set would spill {@code work_mem} and risk the same
     *       timeout). Superseded by RDR-156 P5.2 server-side RRF fusion (unified
     *       selectivity-aware); its P5.G gate verifies the selective case at production
     *       scale rather than re-fixing it.
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
     * <p><strong>Seam B coverage note (P3.2):</strong> all current suites construct this
     * repository through the plain-Embedder constructor ({@code queryRouter} null); the
     * {@link EmbedderRouter#embedOneForCollection} branch of the hybrid query embed is
     * exercised by the P3.E harness (nexus-h3ked) which wires the production router
     * constructor - recorded there, not a silent gap.
     *
     * <p>No upper bound is applied to {@code nResults} by design: the result-size caps
     * the Chroma path enforces are Chroma-imposed quotas (RDR-155 §Retire - they fall
     * away with pgvector). Non-positive values fail loud.
     *
     * @param tenant          tenant principal for RLS scoping
     * @param queryText       search query - used for BOTH the text gate and the
     *                        server-side query embedding
     * @param collectionNames collection names to search (filtered union, single query)
     * @param nResults        maximum rows returned; must be >= 1
     * @param where           optional metadata equality predicates (ANDed); null/empty = none
     * @return text-gated rows sorted by cosine distance ascending; same flat row shape
     *         as {@link #search}
     * @throws IllegalArgumentException if {@code nResults < 1} (a non-positive LIMIT would
     *                                  silently unbound the query: LIMIT -1 means no limit)
     */
    public List<Map<String, Object>> hybridSearch(String tenant, String queryText,
                                                  List<String> collectionNames,
                                                  int nResults,
                                                  Map<String, Object> where) {
        return hybridSearch(tenant, queryText, collectionNames, nResults, where, SELECTIVE_GATE_MAX);
    }

    /**
     * Overload exposing the gate-selectivity threshold (nexus-lcogi). The 5-arg method
     * delegates here with {@link #SELECTIVE_GATE_MAX}; a caller (or test) that knows its
     * gate's selectivity can pin the dispatch — passing a small value forces the
     * non-selective (HNSW-first) branch on a fixture-scale corpus without seeding
     * {@code > SELECTIVE_GATE_MAX} matching rows.
     *
     * @param selectiveGateMax gate-match cutoff for the text-first vs HNSW-first dispatch;
     *                         must be {@code >= 1} (a non-positive value would route every
     *                         gate to HNSW-first and re-enable the collapse, so it is
     *                         rejected).
     */
    public List<Map<String, Object>> hybridSearch(String tenant, String queryText,
                                           List<String> collectionNames,
                                           int nResults,
                                           Map<String, Object> where,
                                           int selectiveGateMax) {
        if (collectionNames == null || collectionNames.isEmpty()) {
            return List.of();
        }
        // queryText is bound twice below as a raw text parameter (plainto_tsquery +
        // trgm <%); a NUL-bearing query would hit the same UTF8-0x00 rejection the
        // upsert path sanitizes (nexus-rvfwj sibling hole, dual-review H1).
        queryText = stripNul(queryText);
        if (nResults < 1) {
            // LIMIT -1 is "no limit" in Postgres - a non-positive value would silently
            // unbound the query instead of capping it.
            throw new IllegalArgumentException("nResults must be >= 1, got " + nResults);
        }
        if (selectiveGateMax < 1) {
            // A non-positive threshold routes EVERY gate to the HNSW-first branch
            // (matchCount >= 0 is always > a non-positive cutoff), silently re-enabling
            // the lcogi selective-gate collapse. Reject rather than mis-dispatch.
            throw new IllegalArgumentException(
                "selectiveGateMax must be >= 1, got " + selectiveGateMax);
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

        // Text gate fragment, shared by the COUNT probe and the ranked query. FTS lexeme
        // match OR word-trigram similarity: the <% operator form (word_similarity >=
        // pg_trgm.word_similarity_threshold) is gin_trgm_ops-indexable (vectors-002) where
        // the function-call form is not; word_similarity (vs plain similarity) does not
        // dilute with chunk_text length. The threshold GUC is pinned per-transaction below.
        StringBuilder gate = new StringBuilder()
            .append(" WHERE collection IN (").append(placeholders(collectionNames.size())).append(")")
            .append(" AND (chunk_tsv @@ plainto_tsquery('english', ?) OR ? <% chunk_text)");
        List<Object> gateBinds = new ArrayList<>();
        gateBinds.addAll(collectionNames);
        gateBinds.add(queryText);
        gateBinds.add(queryText);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                gate.append(" AND metadata->>? = ?");
                gateBinds.add(e.getKey());
                gateBinds.add(String.valueOf(e.getValue()));
            }
        }
        final String table = chunksTable(dim);
        final String gateSql = gate.toString();
        final String vecLit = vectorLiteral(queryVec);

        // SELECTIVITY-AWARE DISPATCH (nexus-lcogi). A cheap COUNT over the text gate (GIN
        // bitmap, no embedding fetch) picks the plan that serves the gate's match count:
        //
        //   * SELECTIVE gate (count <= SELECTIVE_GATE_MAX): a MATERIALIZED CTE evaluates the
        //     gate FIRST via the GIN indexes (optimization fence), then ranks the small
        //     gated set by EXACT cosine distance. This is the lcogi fix — the prior
        //     single-query HNSW-first plan (ORDER BY embedding, gate as scan filter)
        //     collapsed here: a handful of matches in a large corpus rank past
        //     hnsw.max_scan_tuples from the query vector, so the scan stops before reaching
        //     them and the endpoint returns 0 rows (6 / 116k -> 0; full recall ~95s past
        //     every HTTP timeout). Materializing the gate first is instant and exact, with
        //     NO dependence on hnsw.max_scan_tuples / iterative_scan.
        //
        //   * NON-SELECTIVE gate (count > SELECTIVE_GATE_MAX): keep the HNSW-first plan
        //     (gate as filter, iterative_scan). A dense gate is USUALLY found within the
        //     scan budget at corpus-average embedding distributions, so it does not
        //     collapse — and materializing a huge gated set (embeddings are ~4 KB/row: 80k
        //     matches ≈ 320 MB) would spill work_mem and risk the very timeout lcogi is
        //     about. NOTE this is a heuristic, not a guarantee: a SEMI-selective gate
        //     (count in (SELECTIVE_GATE_MAX, hnsw.max_scan_tuples] whose matches all cluster
        //     FAR from the query vector) can still under-return on this branch — the same
        //     geometry as the original bug, in a narrower window. P5.2's RRF fusion closes
        //     that window; the conexus xr7.8.9 gate should verify the non-selective path too
        //     (latency + recall), not only the 6/116k selective case.
        //
        // COST: the non-selective path evaluates the gate TWICE (the COUNT probe + the scan
        // filter), an accepted overhead of the targeted dispatch that P5.2 eliminates.
        //
        // count == 0 falls into the selective branch and returns an empty gated set — no
        // special case.
        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            // Trigram gate calibration (contract anchor): word_similarity >= 0.6, pg_trgm's
            // default - typo-probe candidates sit at ~0.9 and pass, no-signal rows at ~0.1
            // do not. Pinned per-transaction so the gate is independent of cluster config.
            ctx.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.6");

            long matchCount = ctx.fetchOne(
                "SELECT count(*) FROM " + table + gateSql, gateBinds.toArray())
                .get(0, Long.class);

            if (matchCount <= selectiveGateMax) {
                // Text-first: gate materialized via GIN, ranked by exact distance.
                String sql = "WITH gated AS MATERIALIZED ("
                    + "SELECT chash, chunk_text, collection, metadata, embedding FROM " + table + gateSql + ")"
                    + " SELECT chash, chunk_text, collection, metadata::text AS metadata_json,"
                    + " (embedding <=> ?::vector) AS distance"
                    + " FROM gated ORDER BY distance ASC, chash ASC LIMIT ?";
                List<Object> b = new ArrayList<>(gateBinds);
                b.add(vecLit);
                b.add(nResults);
                return ctx.fetch(sql, b.toArray());
            }
            // HNSW-first for a dense gate: keep HNSW scanning past ef_search.
            ctx.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'");
            String sql = "SELECT chash, chunk_text, collection, metadata::text AS metadata_json,"
                + " (embedding <=> ?::vector) AS distance FROM " + table + gateSql
                + " ORDER BY distance ASC, chash ASC LIMIT ?";
            List<Object> b = new ArrayList<>();
            b.add(vecLit);
            b.addAll(gateBinds);
            b.add(nResults);
            return ctx.fetch(sql, b.toArray());
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
     * Stored embeddings for {@code ids} (bead nexus-pebfx.7).
     *
     * <p>The Python search engine fetches result vectors post-search for the
     * contradiction check and Ward-clustering grouping
     * ({@code search_engine._fetch_embeddings_for_results}); without this the
     * client raised {@code NotImplementedError} and both features silently
     * degraded on every service-mode search.
     *
     * @return envelope {@code {ids: List<String>, embeddings: List<List<Float>>}}
     *         in REQUEST order; ids not present (or invisible under RLS) are
     *         OMITTED — Chroma {@code get(include=["embeddings"])} parity; the
     *         Python caller treats {@code N < len(ids)} as a per-collection
     *         fetch failure.
     */
    public Map<String, Object> getEmbeddings(String tenant, String collection,
                                             List<String> ids) {
        int dim = dimForCollection(collection);
        if (ids == null || ids.isEmpty()) {
            return Map.of("ids", List.of(), "embeddings", List.of());
        }
        List<Object> binds = new ArrayList<>();
        binds.add(collection);
        binds.addAll(ids);
        Result<Record> result = tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("SELECT chash, embedding::text AS embedding_text FROM "
                      + chunksTable(dim)
                      + " WHERE collection = ? AND chash IN ("
                      + placeholders(ids.size()) + ")",
                      binds.toArray()));

        Map<String, List<Float>> byChash = new HashMap<>();
        for (Record rec : result) {
            byChash.put(rec.get("chash", String.class),
                        parseVectorLiteral(rec.get("embedding_text", String.class)));
        }
        List<String> outIds = new ArrayList<>();
        List<List<Float>> outEmbeddings = new ArrayList<>();
        for (String id : ids) {
            List<Float> vec = byChash.get(id);
            if (vec != null) {
                outIds.add(id);
                outEmbeddings.add(vec);
            }
        }
        return Map.of("ids", outIds, "embeddings", outEmbeddings);
    }

    /** Parse a pgvector text literal {@code "[0.1,0.2,...]"} into floats. */
    private static List<Float> parseVectorLiteral(String literal) {
        if (literal == null || literal.length() < 2) {
            // Schema says NOT NULL; a null/short literal means a malformed
            // row. Return an empty row — the Python caller's ndarray
            // construction rejects the ragged shape and fails the
            // collection's fetch (degrade, never misattribute).
            log.warn("event=embedding_literal_malformed literal={}", literal);
            return List.of();
        }
        String body = literal.substring(1, literal.length() - 1);
        if (body.isBlank()) {
            return List.of();
        }
        String[] parts = body.split(",");
        List<Float> out = new ArrayList<>(parts.length);
        for (String part : parts) {
            out.add(Float.parseFloat(part.trim()));
        }
        return out;
    }

    /**
     * Single-chunk put (MCP {@code store_put} path) — embed + upsert one chunk.
     *
     * <p>RDR-155 P4a.2 (bead nexus-1k8s1): mirrors the Chroma
     * {@code VectorRepository.put} envelope (returns the chunk ID verbatim).
     * Delegates to {@link #upsertChunks} so dim dispatch, router-aware embedding,
     * and the fail-loud dimension check are identical to the batch path.
     *
     * @return the chunk ID, unchanged
     */
    public String put(String tenant, String collection, String docId,
                      String content, Map<String, Object> metadata) {
        upsertChunks(tenant, collection, List.of(docId), List.of(content),
                     List.of(metadata != null ? metadata : Map.of()));
        return docId;
    }

    /**
     * Get chunks matching a metadata {@code where} equality filter, paginated in
     * chash order (RDR-155 P4a.2, bead nexus-1k8s1).
     *
     * <p>The incremental-sync staleness check's shape: the Python
     * {@code _ServiceCollectionStub.get(where=...)} asks for chunks whose
     * {@code source_key} / {@code content_hash} match. Only plain equality
     * predicates are supported (ANDed) — the same subset {@link #search} applies;
     * Chroma operator-form filters ({@code $and}, {@code $gte}, ...) are NOT
     * translated (deliberately unpinned by the P4a.1 contract, recorded on
     * nexus-1k8s1).
     *
     * @param where metadata equality predicates (ANDed); null/empty returns the
     *              collection paginated (the {@code store-get}-without-ids shape)
     * @return Chroma-style envelope {@code {ids, documents, metadatas}} aligned by
     *         index, chash ascending
     */
    public Map<String, Object> getWhere(String tenant, String collection,
                                        Map<String, Object> where,
                                        int limit, int offset) {
        int dim = dimForCollection(collection);
        StringBuilder sql = new StringBuilder()
            .append("SELECT chash, chunk_text, metadata::text AS metadata_json FROM ")
            .append(chunksTable(dim))
            .append(" WHERE collection = ?");
        List<Object> binds = new ArrayList<>();
        binds.add(collection);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                sql.append(" AND metadata->>? = ?");
                binds.add(e.getKey());
                binds.add(String.valueOf(e.getValue()));
            }
        }
        sql.append(" ORDER BY chash ASC LIMIT ? OFFSET ?");
        binds.add(limit);
        binds.add(offset);

        Result<Record> result = tenantScope.withTenant(tenant, ctx ->
            ctx.fetch(sql.toString(), binds.toArray()));

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
     * List the collections visible to {@code tenant} (RDR-155 P4a.2,
     * bead nexus-1k8s1).
     *
     * <p>Union across all three {@code chunks_<dim>} tables — collection is a
     * column, not a table, so "a collection exists" means "at least one chunk row
     * carries the name". RLS scopes the union to the tenant's rows, so a foreign
     * tenant's collections are invisible (no existence leak).
     *
     * @return Chroma-style envelope {@code [{"name": ...}, ...]}, name ascending
     */
    public List<Map<String, Object>> listCollections(String tenant) {
        Result<Record> result = tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("SELECT collection FROM ("
                      + "  SELECT DISTINCT collection FROM nexus.chunks_384"
                      + "  UNION SELECT DISTINCT collection FROM nexus.chunks_768"
                      + "  UNION SELECT DISTINCT collection FROM nexus.chunks_1024"
                      + ") cols ORDER BY collection ASC"));
        List<Map<String, Object>> out = new ArrayList<>(result.size());
        for (Record rec : result) {
            out.add(Map.of("name", rec.get("collection", String.class)));
        }
        return out;
    }

    /**
     * Metadata-scoped combined search (RDR-156 P4, Decision 5, bead nexus-70r3c.15/joesk).
     *
     * <p>Unifies the {@code query} MCP tool's catalog-aware-routing dance into one
     * planner-optimizable statement: calls {@code nexus.search_metadata_scoped_<dim>}
     * (catalog-006) which joins {@code chunks_<dim> ⋈ catalog_document_chunks ⋈
     * catalog_documents}, filters by the catalog metadata dimensions (NULL = skip),
     * tombstone-filters, and ranks by cosine distance. The query vector is embedded
     * server-side and passed as a function ARGUMENT so the HNSW index engages
     * (Finding 5a). {@code runCombinedQuery} applies {@code SET LOCAL
     * hnsw.iterative_scan='relaxed_order'} — the same filtered-ANN setting
     * {@link #search}/{@link #hybridSearch} use; the inlinable SQL function has no
     * in-function selectivity switch (kept inlinable by decision). NOTE: this alone does
     * NOT tune {@code hnsw.max_scan_tuples}, so the Finding-5b narrow/distant scoped
     * under-return ceiling is not yet fully defended — tracked separately (nexus-0zcn9);
     * the production-scale recall gate is owned by conexus xr7.8.9.
     *
     * <p>Returns the document tumbler as {@code id} (document-level retrieval). A
     * document with multiple matching chunks can appear more than once; consumer-side
     * de-duplication (keep best distance per id) is the {@code query}-tool repoint's
     * responsibility, not this method's.
     *
     * @param contentType catalog content_type filter; null = no filter
     * @param author      catalog author filter; null = no filter
     * @param year        catalog year filter; null = no filter
     * @param corpus      catalog corpus filter; null = no filter
     */
    public List<Map<String, Object>> searchMetadataScoped(
            String tenant, String queryText, List<String> collectionNames,
            String contentType, String author, Integer year, String corpus, int nResults) {
        if (collectionNames == null || collectionNames.isEmpty()) {
            return List.of();
        }
        if (nResults < 1) {
            throw new IllegalArgumentException("nResults must be >= 1, got " + nResults);
        }
        int dim = requireHomogeneousDim(collectionNames);
        float[] queryVec = embedQuery(collectionNames.get(0), queryText, dim);

        String sql = "SELECT id, content, collection, distance"
                   + " FROM nexus.search_metadata_scoped_" + dim
                   + "(?::vector, ARRAY[" + placeholders(collectionNames.size()) + "]::text[],"
                   + " ?::text, ?::text, ?::int, ?::text, ?)";
        List<Object> binds = new ArrayList<>();
        binds.add(vectorLiteral(queryVec));
        binds.addAll(collectionNames);
        binds.add(contentType);
        binds.add(author);
        binds.add(year);
        binds.add(corpus);
        binds.add(nResults);

        return runCombinedQuery(tenant, sql, binds);
    }

    /**
     * Topic-scoped combined search (RDR-156 P4, Decision 5, bead nexus-70r3c.15/joesk).
     *
     * <p>Calls {@code nexus.search_topic_scoped_<dim>} (catalog-006). Topic membership is
     * CHUNK-level: {@code topic_assignments.doc_id} is a chunk chash (nexus-sa14p), so the
     * function joins {@code chunks_<dim>.chash = topic_assignments.doc_id}, live-filters,
     * and ranks by cosine distance. Returns the chunk chash as {@code id} (chunk-level,
     * matching {@link #search}). Same query-vector-as-argument + iterative_scan discipline
     * as {@link #searchMetadataScoped}.
     */
    public List<Map<String, Object>> searchTopicScoped(
            String tenant, String queryText, String topicLabel, String collection, int nResults) {
        if (collection == null || collection.isBlank()) {
            return List.of();
        }
        if (nResults < 1) {
            throw new IllegalArgumentException("nResults must be >= 1, got " + nResults);
        }
        int dim = dimForCollection(collection);
        float[] queryVec = embedQuery(collection, queryText, dim);

        String sql = "SELECT id, content, collection, distance"
                   + " FROM nexus.search_topic_scoped_" + dim
                   + "(?::vector, ?::text, ?::text, ?)";
        List<Object> binds = new ArrayList<>();
        binds.add(vectorLiteral(queryVec));
        binds.add(topicLabel);
        binds.add(collection);
        binds.add(nResults);

        return runCombinedQuery(tenant, sql, binds);
    }

    /**
     * Validate every collection dispatches to the same dim and return it.
     * Mirrors the same-dim guard in {@link #search}.
     */
    private static int requireHomogeneousDim(List<String> collectionNames) {
        int dim = dimForCollection(collectionNames.get(0));
        for (String col : collectionNames) {
            int colDim = dimForCollection(col);
            if (colDim != dim) {
                throw new IllegalArgumentException(
                    "mixed dimensions in one combined-query call: '" + collectionNames.get(0)
                    + "' is " + dim + "-dim but '" + col + "' is " + colDim
                    + "-dim - one query vector cannot serve both spaces");
            }
        }
        return dim;
    }

    /** Embed the query server-side, routing by collection; fail loud on dim mismatch. */
    private float[] embedQuery(String collection, String queryText, int dim) {
        float[] queryVec = (queryRouter != null)
                ? queryRouter.embedOneForCollection(collection, queryText)
                : queryEmbedder.embedOne(queryText);
        if (queryVec.length != dim) {
            throw new IllegalArgumentException(
                "query embedder produced a " + queryVec.length
                + "-dim vector but the collection dispatches to chunks_" + dim);
        }
        return queryVec;
    }

    /**
     * Execute a combined-query function call under the tenant RLS scope with the
     * filtered-ANN session setting, and map the (id, content, collection, distance)
     * rows to the flat search() envelope.
     */
    private List<Map<String, Object>> runCombinedQuery(
            String tenant, String sql, List<Object> binds) {
        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'");
            return ctx.fetch(sql, binds.toArray());
        });
        List<Map<String, Object>> rows = new ArrayList<>(result.size());
        for (Record rec : result) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id",         rec.get("id", String.class));
            row.put("content",    rec.get("content", String.class));
            row.put("distance",   rec.get("distance", Double.class));
            row.put("collection", rec.get("collection", String.class));
            rows.add(row);
        }
        return rows;
    }

    /**
     * Per-collection vector statistics for {@code tenant} (RDR-156 P3, Decision 4,
     * bead nexus-70r3c.12).
     *
     * <p>Reads {@code nexus.collection_vector_stats} — the SECURITY INVOKER aggregate
     * over {@code live_chunks} — so counts are TOMBSTONE-FILTERED (a chunk whose only
     * manifest rows point to trashed documents is not counted; manifest-less note
     * chunks are). This deliberately diverges from {@link #count} under tombstones:
     * doctor/status surfaces want live counts, migration parity checks keep raw.
     *
     * <p>RLS scopes the view to the tenant's rows (security_invoker propagates the
     * caller's context through both view layers), so a foreign tenant's collections
     * are invisible — same guarantee as {@link #listCollections}.
     *
     * @return one entry per (collection, dim):
     *         {@code [{"name": ..., "dim": 384, "count": N, "last_write": "..."}]},
     *         name ascending. {@code last_write} is ISO-8601 with offset, or absent
     *         if null. Collections with zero live chunks do not appear.
     */
    public List<Map<String, Object>> collectionStats(String tenant) {
        Result<Record> result = tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("SELECT collection, dim, chunk_count, last_write"
                      + " FROM nexus.collection_vector_stats"
                      + " ORDER BY collection ASC, dim ASC"));
        List<Map<String, Object>> out = new ArrayList<>(result.size());
        for (Record rec : result) {
            Map<String, Object> row = new java.util.LinkedHashMap<>();
            row.put("name",  rec.get("collection", String.class));
            row.put("dim",   rec.get("dim", Integer.class));
            row.put("count", rec.get("chunk_count", Long.class));
            var lastWrite = rec.get("last_write", java.time.OffsetDateTime.class);
            if (lastWrite != null) {
                row.put("last_write", lastWrite.toString());
            }
            out.add(row);
        }
        return out;
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
                            // Same NUL defense as upsertChunks: jsonb rejects NUL just
                            // like text does (nexus-rvfwj, dual-review M2).
                            toJson(sanitizeNulDeep(metadatas.get(i))), collection, ids.get(i));
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

    /** Strip NUL (0x00) — unstorable in Postgres {@code text}/{@code jsonb} (nexus-rvfwj). */
    private static String stripNul(String s) {
        return (s != null && s.indexOf('\u0000') >= 0) ? s.replace("\u0000", "") : s;
    }

    /**
     * Recursively strip NULs from metadata string values (and keys). Postgres
     * {@code jsonb} rejects {@code NUL} escapes just as {@code text} rejects raw
     * NUL bytes, so metadata needs the same sanitization as chunk text.
     */
    @SuppressWarnings("unchecked")
    private static Map<String, Object> sanitizeNulDeep(Map<String, Object> meta) {
        if (meta == null) return null;
        return (Map<String, Object>) sanitizeNulValue(meta);
    }

    private static Object sanitizeNulValue(Object v) {
        if (v instanceof String s) {
            return stripNul(s);
        }
        if (v instanceof Map<?, ?> m) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                out.put(stripNul(String.valueOf(e.getKey())), sanitizeNulValue(e.getValue()));
            }
            return out;
        }
        if (v instanceof List<?> l) {
            List<Object> out = new ArrayList<>(l.size());
            for (Object o : l) {
                out.add(sanitizeNulValue(o));
            }
            return out;
        }
        return v;
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
