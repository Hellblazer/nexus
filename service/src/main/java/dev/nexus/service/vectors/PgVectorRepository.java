// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.CollectionRegistry;
import dev.nexus.service.db.DeadlockRetry;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.impl.DSL;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_1024;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_384;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS_768;
import static dev.nexus.service.jooq.nexus.Tables.COLLECTION_VECTOR_STATS;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_GRAPH_HOP_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_768;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_1024;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_TOPIC_SCOPED_768;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

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

    /**
     * Test-visibility hook (S1, RDR-169 G5): counts invocations of {@link #sourceUrisByChash}
     * so cross-package integration tests (e.g. {@code dev.nexus.service.BridgeAddressFieldsTest})
     * can assert that the default path (includeSourceUri=false) runs ZERO catalog JOINs and the
     * opt-in path runs ≥1. Private with read/reset accessors (wave review: as a public
     * mutable field, any caller could {@code set()} it and corrupt whatever a concurrent
     * test asserts). Production code never reads it.
     */
    private final AtomicInteger sourceUriJoinCalls = new AtomicInteger();

    /** Test-only instrumentation read — see {@link #sourceUriJoinCalls}. */
    public int sourceUriJoinCallCount() {
        return sourceUriJoinCalls.get();
    }

    /** Test-only instrumentation reset — see {@link #sourceUriJoinCalls}. */
    public void resetSourceUriJoinCallsForTests() {
        sourceUriJoinCalls.set(0);
    }

    /**
     * Test-visibility hook (RDR-181, bead nexus-f0r8p.3): counts invocations of
     * {@link #resolveNeedEmbedIdx} — i.e. how many times the existence-SELECT +
     * have-vector-UPDATE existence-partition transaction actually ran. Lets
     * integration tests assert that {@code forceReEmbed=true} bypasses the
     * existence check ENTIRELY (count stays 0) rather than merely inferring it
     * from the embedder's call count. Same read/reset-accessor shape as
     * {@link #sourceUriJoinCalls} — production code never reads it.
     */
    private final AtomicInteger existenceSelectCalls = new AtomicInteger();

    /** Test-only instrumentation read — see {@link #existenceSelectCalls}. */
    public int existenceSelectCallCount() {
        return existenceSelectCalls.get();
    }

    /** Test-only instrumentation reset — see {@link #existenceSelectCalls}. */
    public void resetExistenceSelectCallsForTests() {
        existenceSelectCalls.set(0);
    }

    /**
     * Test-only interleaving seam (RDR-181, bead nexus-f0r8p.4): an optional callback
     * invoked by {@link #resolveNeedEmbedIdx} immediately after the existence SELECT
     * resolves (the have-vector/need-embed partition is computed) and BEFORE the
     * have-vector UPDATE loop begins. This is the exact window the RDR's Risks and
     * Mitigations section describes — chash H is confirmed present by the SELECT, then
     * a concurrent orphan-GC {@link #delete} of H can commit, then the have-vector
     * UPDATE runs and must observe 0 affected rows (self-healing reroute to need-embed,
     * never silent loss). Production code has no way to pause a transaction
     * deterministically at this point, and the regression test proving the self-heal
     * is safe cannot rely on {@code Thread.sleep} timing (flaky) — so this hook exists
     * purely to let {@code PgVectorEmbedSkipGcRaceTest} block the writer thread here
     * while a second thread's concurrent delete commits, then release it.
     *
     * <p>Default {@code null} (no-op): every call site does a single null-check before
     * invoking it, costing nothing when unset. Never read or written by production
     * code. A package-private setter would not reach the test class — it lives in
     * {@code dev.nexus.service}, this repository in {@code dev.nexus.service.vectors} —
     * so the setter is {@code public}, following the same public-accessor shape already
     * established by {@link #sourceUriJoinCalls} / {@link #existenceSelectCalls}.
     */
    private volatile Runnable afterExistencePartitionHookForTests;

    /**
     * Test-only: install (or clear with {@code null}) the post-existence-partition
     * pause hook — see {@link #afterExistencePartitionHookForTests}.
     */
    public void setAfterExistencePartitionHookForTests(Runnable hook) {
        this.afterExistencePartitionHookForTests = hook;
    }

    /**
     * Pairs a value with the embedding token count consumed to produce it (bead nexus-ehc4q).
     *
     * <p>Returned by the {@code *WithTokens} sibling methods so the caller (VectorHandler)
     * receives the token count as a plain return value rather than via a side-channel.
     * Tokens = 0 means the embedder does not report billable usage (e.g. ONNX local-mode).
     */
    public record Tokened<T>(T value, long tokens) {}

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
     * @param ids        chunk natural IDs (the full sha256 hexdigest — the chash (RDR-180))
     * @param documents  chunk texts (embedded server-side)
     * @param metadatas  per-chunk metadata maps (stored as JSONB; may contain nulls)
     */
    /**
     * Token-aware sibling of {@link #upsertChunks} (bead nexus-ehc4q).
     * Returns the chunk IDs upserted alongside the embedding token count.
     * The token count is 0 when the embedder does not report billable usage
     * (e.g. ONNX local-mode; see {@link OnnxEmbedder#embedWithUsage}).
     */
    public Tokened<Integer> upsertChunksWithTokens(String tenant, String collection,
                                                    List<String> ids,
                                                    List<String> documents,
                                                    List<Map<String, Object>> metadatas) {
        return upsertChunksWithTokens(tenant, collection, ids, documents, metadatas, false);
    }

    /**
     * {@code forceReEmbed}-aware sibling (RDR-181, bead nexus-f0r8p.3): when
     * {@code true}, bypasses the existence-partition entirely (every chash embeds,
     * as if the collection had never been indexed) — the rare model-drift-within-
     * a-collection recompute, and the escape for the (0%-hit) first-index path
     * that would otherwise pay for a existence SELECT with no offsetting benefit.
     * Wired from {@code VectorHandler}'s {@code force_re_embed} request field.
     */
    public Tokened<Integer> upsertChunksWithTokens(String tenant, String collection,
                                                    List<String> ids,
                                                    List<String> documents,
                                                    List<Map<String, Object>> metadatas,
                                                    boolean forceReEmbed) {
        long[] tokensOut = {0L};
        upsertChunksInternal(tenant, collection, ids, documents, metadatas, tokensOut, null, forceReEmbed);
        return new Tokened<>(ids.size(), tokensOut[0]);
    }

    /** Delegates to {@link #upsertChunksInternal}; discards the token count. */
    public void upsertChunks(String tenant, String collection,
                             List<String> ids,
                             List<String> documents,
                             List<Map<String, Object>> metadatas) {
        upsertChunksInternal(tenant, collection, ids, documents, metadatas, null, null, false);
    }

    /** {@code forceReEmbed}-aware sibling of {@link #upsertChunks} — see {@link #upsertChunksWithTokens(String, String, List, List, List, boolean)}. */
    public void upsertChunks(String tenant, String collection,
                             List<String> ids,
                             List<String> documents,
                             List<Map<String, Object>> metadatas,
                             boolean forceReEmbed) {
        upsertChunksInternal(tenant, collection, ids, documents, metadatas, null, null, forceReEmbed);
    }

    /**
     * Same-model vector PASSTHROUGH (nexus-hxry2, RDR-166): store caller-supplied
     * embeddings VERBATIM, skipping the server-side embedder.
     *
     * <p>The migration uses this when the source collection's embedding model
     * equals the target's wired model: the stored Chroma vectors were produced by
     * exactly the model the target collection is searched against, so re-embedding
     * would only re-bill the operator's Voyage key for identical vectors. Each
     * vector's dimension MUST match the dispatched {@code chunks_<dim>} table — a
     * mismatch fails loud (the contamination guard; never a silent embed fallback).
     * No embedder is invoked, so the token count is always 0.
     *
     * @param embeddings one vector per id (length must equal {@code ids})
     */
    public void upsertChunksWithVectors(String tenant, String collection,
                                        List<String> ids,
                                        List<String> documents,
                                        List<float[]> embeddings,
                                        List<Map<String, Object>> metadatas) {
        // nexus-e0hd2 review F2: this is the server-to-server ingest path
        // (MigrationHandler /ingest-cloud) — ids arrive from an EXTERNAL
        // source with no HTTP-boundary validation. Validate here so a
        // malformed id fails loud with its index BEFORE the batch
        // transaction, not reason-poor at the chunks CHECK. RDR-180
        // (nexus-jxizy.7): ONE strict tier — the full-digest 64-hex is the
        // only accepted id shape (the byte[16]-era length-only tolerance is
        // retired; PgVectorServingContractTest pins the rejection).
        if (ids != null) {
            for (int i = 0; i < ids.size(); i++) {
                dev.nexus.service.db.Chash.requireCanonical(ids.get(i), "ids[" + i + "]");
            }
        }
        if (embeddings == null || embeddings.size() != ids.size()) {
            throw new IllegalArgumentException(
                "upsertChunksWithVectors requires one embedding per id: ids="
                + ids.size() + " embeddings=" + (embeddings == null ? "null" : embeddings.size()));
        }
        // forceReEmbed is irrelevant to the passthrough path: dedupProvided != null
        // gates the existence-partition check off unconditionally below, before
        // forceReEmbed is ever consulted. Pass false — never wire this true here,
        // it would be dead plumbing with no behavioral effect.
        upsertChunksInternal(tenant, collection, ids, documents, metadatas, null, embeddings, false);
    }

    private void upsertChunksInternal(String tenant, String collection,
                                      List<String> ids,
                                      List<String> documents,
                                      List<Map<String, Object>> metadatas,
                                      long[] tokensOut,
                                      List<float[]> providedEmbeddings,
                                      boolean forceReEmbed) {
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
        List<float[]> dedupProvided = providedEmbeddings != null ? new ArrayList<>() : null;
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
                if (dedupProvided != null) dedupProvided.add(providedEmbeddings.get(i));
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

        // nexus-ps9wb: PgVector DEADLOCK (SQLState 40P01) fix. Two concurrent upsert
        // batches into the SAME collection that touch overlapping chashes in different
        // arrival orders lock the shared rows in opposite orders on the multi-row
        // INSERT ... ON CONFLICT below -> lock cycle -> Postgres kills a victim -> the
        // caller sees HTTP 500. Sorting the dedup'd rows by chash (the ON CONFLICT key)
        // gives EVERY batch one global lock-acquisition order, so no cycle can form.
        // Done BEFORE embedding so the computed embeddings inherit the sorted order (no
        // separate reorder of the possibly-immutable embeddings list). Mutated IN PLACE
        // (clear + re-add) so the list references stay effectively-final for the lambda
        // captures in the write transaction below.
        if (dedupIds.size() > 1) {
            Integer[] perm = new Integer[dedupIds.size()];
            for (int i = 0; i < perm.length; i++) perm[i] = i;
            Arrays.sort(perm, Comparator.comparing(dedupIds::get));
            List<String> oIds = new ArrayList<>(dedupIds);
            List<String> oDocs = new ArrayList<>(dedupDocs);
            List<Map<String, Object>> oMetas = new ArrayList<>(dedupMetas);
            List<float[]> oProvided = dedupProvided != null ? new ArrayList<>(dedupProvided) : null;
            dedupIds.clear();
            dedupDocs.clear();
            dedupMetas.clear();
            if (dedupProvided != null) dedupProvided.clear();
            for (int idx : perm) {
                dedupIds.add(oIds.get(idx));
                dedupDocs.add(oDocs.get(idx));
                dedupMetas.add(oMetas.get(idx));
                if (dedupProvided != null) dedupProvided.add(oProvided.get(idx));
            }
        }

        // RDR-181 (bead nexus-f0r8p.2): existence-partition — skip re-embedding
        // chunks whose vector is already stored for (tenant, collection, chash).
        // Only applies to the server-embed path: the passthrough path (dedupProvided
        // != null, upsertChunksWithVectors) already skips the embedder unconditionally
        // and writes caller-supplied vectors verbatim, so running the existence check
        // there would be pure overhead with no win.
        //
        // insertIdx: indices into dedupIds/dedupDocs/dedupMetas that will be embedded
        // (or, for passthrough, already carry a supplied vector) and written via the
        // INSERT ... ON CONFLICT below. A chash whose have-vector branch succeeds
        // (metadata-only UPDATE affected 1 row, inside resolveNeedEmbedIdx's single
        // short transaction) is FULLY handled there and excluded from insertIdx —
        // re-inserting it would be redundant: its vector is untouched, which is the
        // whole point of the optimization.
        List<Integer> insertIdx = null;
        if (dedupProvided == null && !dedupIds.isEmpty() && !forceReEmbed) {
            // RDR-181 (bead nexus-f0r8p.3): forceReEmbed bypasses the existence
            // check entirely — the rare model-drift-within-collection recompute,
            // and the escape for the (0%-hit) first-index path so it never pays
            // for the existence SELECT with no offsetting benefit.
            insertIdx = resolveNeedEmbedIdx(tenant, collection, dim, dedupIds, dedupDocs, dedupMetas);
        }
        if (insertIdx == null) {
            // Passthrough, forceReEmbed, an empty batch, or the existence-check
            // transaction itself failing (fail-safe: a SELECT/UPDATE error must never
            // be read as "everything already has a vector", which would silently skip
            // embedding a genuinely new chunk) — every row is embedded/written,
            // exactly as today.
            insertIdx = new ArrayList<>(dedupIds.size());
            for (int i = 0; i < dedupIds.size(); i++) insertIdx.add(i);
        }
        List<String> docsToEmbed = new ArrayList<>(insertIdx.size());
        for (int idx : insertIdx) docsToEmbed.add(dedupDocs.get(idx));

        int embedSkipped = dedupIds.size() - insertIdx.size();
        if (embedSkipped > 0) {
            log.info("event=upsert_embed_skipped collection={} skipped={} embedded={}",
                    collection, embedSkipped, insertIdx.size());
        }

        // Embeddings: either caller-supplied (same-model PASSTHROUGH, nexus-hxry2)
        // or computed server-side (the default Seam B path), for exactly the
        // insertIdx subset. When the caller supplies vectors we skip the embedder
        // entirely — no Voyage call, token count stays 0 — because the source model
        // equals the target's wired model (validated client-side) and re-embedding
        // would re-bill for identical vectors. Collection-aware routing when wired
        // with the router (production / Seam B path), identical to the Chroma
        // VectorRepository flow. Uses *WithUsage to capture the token count (bead
        // nexus-ehc4q), surfaced to VectorHandler via the Tokened<T> return value
        // (not a ThreadLocal). embed() runs OUTSIDE any transaction (RDR-181
        // Technical Design) — resolveNeedEmbedIdx's existence-check transaction
        // above has already committed by the time we get here, and the DeadlockRetry-
        // wrapped INSERT below has not yet started; never wrap this call in either.
        List<float[]> embeddings;
        if (dedupProvided != null) {
            embeddings = dedupProvided;  // passthrough — no embed, tokensOut stays 0
        } else if (docsToEmbed.isEmpty()) {
            // RDR-181: every chash in this batch was settled by the have-vector
            // metadata-only UPDATE above — skip the embedder round-trip entirely
            // rather than invoking it with an empty batch (the whole point of this
            // bead: a batch of pure metadata refreshes bills Voyage for nothing).
            embeddings = List.of();
        } else {
            EmbedResult embedResult = (docRouter != null)
                    ? docRouter.embedForCollectionWithUsage(collection, docsToEmbed)
                    : docEmbedder.embedWithUsage(docsToEmbed);
            if (tokensOut != null) tokensOut[0] = embedResult.tokens();
            embeddings = embedResult.embeddings();
        }

        // Fail loud BEFORE any SQL if a vector's dimension does not match the
        // dispatched table dimension (no truncation, no padding). For the
        // passthrough path this is ALSO the contamination guard: a supplied vector
        // whose dim disagrees with the target table is rejected, never stored.
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

        // Ensure-registered in its OWN short committed transaction (v0.1.21,
        // ChashVectorConcurrencyTest full-suite failure). The nexus-h8rf6.2
        // CollectionRegistry cache bounds convoy COUNT (registration attempts after
        // the first are skipped process-wide), but registration-inside-the-batch-txn
        // left convoy DURATION unbounded: the FIRST writer to a brand-new collection
        // held the catalog_collections ON CONFLICT value lock for its ENTIRE batch
        // (60x1024-dim inserts + HNSW maintenance — seconds on a loaded host), so
        // every concurrent racer blew the pool's connectionTimeout and got the typed
        // 503 the cache was built to prevent. Committing the single-statement
        // registration BEFORE the batch caps the lock hold at micro-transaction
        // length. Rollback trade-off: a zero-chunk stub row may persist if the batch
        // then fails — benign (existence is live-chunk-count-based everywhere;
        // deleteCollection removes stubs; any retry would recreate it anyway).
        // Mirrors ChashRepository.registerCollectionShortTxn.
        if (!CollectionRegistry.isKnown(tenant, collection)) {
            tenantScope.withTenant(tenant, ctx -> {
                ctx.insertInto(CATALOG_COLLECTIONS,
                                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
                   .values(tenant, collection, regContentType, regOwner, regModel, regModelVersion)
                   .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                   .doNothing()
                   .execute();
                return null;
            });
            // Post-commit discipline per CollectionRegistry class doc.
            CollectionRegistry.markKnown(tenant, collection);
        }

        // nexus-ps9wb belt-and-suspenders: the chash sort above removes the
        // same-collection lock cycle, but a residual deadlock is still possible
        // against a concurrent writer on a DIFFERENT lock order (e.g. a concurrent
        // delete, or the catalog_collections registration path). The deadlock victim's
        // transaction is ALREADY rolled back by Postgres, so re-running the batch is
        // safe (idempotent ON CONFLICT upsert). Bounded retries with jitter; on
        // exhaustion the original exception propagates unchanged.
        // nexus-ps9wb belt: the chash sort above removes the same-collection lock
        // cycle, but a residual deadlock is still possible against a concurrent writer
        // on a DIFFERENT lock order (e.g. a concurrent delete). The victim's txn is
        // already rolled back, so re-running the idempotent ON CONFLICT batch is safe.
        // Embeddings were computed ABOVE, outside this retry, so a retry never re-bills
        // Voyage. Shared helper — same belt guards every multi-row upsert path.
        // Effectively-final copy for the lambda captures below: insertIdx is assigned
        // via one of two branches above (the existence-partition result or the
        // identity fallback), so javac sees more than one assignment statement and
        // refuses to treat it as effectively final even though exactly one runs.
        final List<Integer> finalInsertIdx = insertIdx;
        // RDR-181 (bead nexus-f0r8p.2): when the existence-partition resolved every
        // chash via the have-vector metadata-only UPDATE (a pure re-index-with-no-
        // content-change batch), finalInsertIdx is empty and there is NOTHING left to
        // insert — skip this transaction's connection acquisition entirely rather
        // than borrowing a pool connection just to run a no-op INSERT ... ON CONFLICT
        // over zero rows. Under concurrent load with a bounded HikariCP pool (see
        // ChashVectorConcurrencyTest), an unconditional extra checkout per call is
        // real, avoidable contention.
        if (!finalInsertIdx.isEmpty()) {
            DeadlockRetry.run(collection, () -> tenantScope.withTenant(tenant, ctx -> {
                // Bead nexus-h8rf6.2 (reduce per-request connection hold time): ONE
                // multi-row INSERT ... ON CONFLICT instead of dedupIds.size() sequential
                // round trips. The old per-row loop held this transaction's connection
                // (and, transitively, the catalog_collections row lock any concurrent
                // registration attempt for this collection was blocked on) open for N
                // round trips — cheap on a near-zero-RTT localhost DB, but on a real
                // network hop to Postgres every extra round trip is directly extra
                // lock-hold time for every OTHER concurrent writer to this collection.
                // Mirrors ChashRepository.upsertMany / doImportBatch, which already
                // batch this way. Same ON CONFLICT semantics, same bound values, just
                // one statement.
                // nexus-xtmtf: chained .values() keeps this ONE multi-row
                // statement (the h8rf6.2 lock-hold rationale); float[] (VectorBinding) +
                // JSONB typed binds retire the ?::vector / ?::jsonb casts.
                // RDR-181 (bead nexus-f0r8p.2): only insertIdx rows land here — chashes
                // whose have-vector branch already succeeded via a metadata-only UPDATE
                // are excluded (see insertIdx construction above); embeddings is aligned
                // to insertIdx (position k), NOT to dedupIds (position idx) — the two
                // lists diverge whenever the existence-partition skipped any embeds, so
                // embeddings.get(idx) would silently pair the wrong vector with a chash.
                DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
                var insert = ctx.insertInto(ch.table())
                    .columns(ch.tenantId(), ch.collection(), ch.chash(),
                             ch.chunkText(), ch.embedding(), ch.metadata());
                for (int k = 0; k < finalInsertIdx.size(); k++) {
                    int idx = finalInsertIdx.get(k);
                    insert = insert.values(tenant, collection, dedupIds.get(idx),
                            dedupDocs.get(idx),
                            Vector.of(embeddings.get(k)),
                            JSONB.jsonb(toJson(dedupMetas.get(idx))));
                }
                insert.onConflict(ch.tenantId(), ch.collection(), ch.chash())
                      .doUpdate()
                      .set(ch.chunkText(), DSL.excluded(ch.chunkText()))
                      .set(ch.embedding(), DSL.excluded(ch.embedding()))
                      .set(ch.metadata(),  DSL.excluded(ch.metadata()))
                      .execute();
                return null;
            }));
        }
        log.debug("event=upsert_chunks_done collection={} table={} count={} embedded={} metadata_only={}",
                collection, table, dedupIds.size(), insertIdx.size(), embedSkipped);
    }

    // -------------------------------------------------------------------------
    // RDR-169 G4: embed-without-store / reference-only upsert
    // -------------------------------------------------------------------------

    /**
     * Phase-A write gate (RDR-169 G4, nexus-xvb6b): set to {@code false} until Phase B
     * (nexus-dtnpu) adds the {@code retention} column + nullable {@code chunk_text} to every
     * {@code chunks_<dim>} table.  Flipping this to {@code true} without the schema migration
     * will cause every {@link #upsertReferenceOnlyChunk} call to fail at the DB layer.
     *
     * <p>When {@code false}, {@link #upsertReferenceOnlyChunk} runs all pre-SQL validation
     * (null/dim check, full→reference-only guard SELECT) but short-circuits before the
     * retention-binding INSERT, throwing {@link IllegalStateException}.  No Phase-A code
     * path reaches the INSERT.
     */
    static final boolean REFERENCE_ONLY_WRITES_ENABLED = false;

    /**
     * Returns the INSERT SQL used by {@link #upsertReferenceOnlyChunk} for the given
     * {@code chunks_<dim>} table name.
     *
     * <p>Package-private (accessed from {@code dev.nexus.service.vectors} test sources):
     * tests assert the SQL fragment is correctly formed (NULL chunk_text, retention column
     * present) without executing it against the live schema (the {@code retention} column
     * does not exist until Phase B / nexus-dtnpu).
     */
    /**
     * Build the reference-only upsert as a jOOQ query (nexus-xtmtf: DSL form of
     * the retired {@code referenceOnlyInsertSql} string). ``retention`` is a
     * Phase-B column absent from the generated schema (the caller is gated OFF
     * in Phase A, unreachable); the ad-hoc field reference fails at runtime on
     * the missing column exactly like the raw SQL did, and Phase B's regen
     * replaces it with the generated field. chunk_text is intentionally
     * EXCLUDED from the DO UPDATE — reference-only rewrites refresh
     * embedding+metadata but must never overwrite a non-NULL chunk_text (the
     * caller's guard catches full→ref before SQL; this omission is
     * defense-in-depth). Package-private so the SQL-shape test renders it.
     */
    static org.jooq.Query referenceOnlyInsertQuery(
            org.jooq.DSLContext ctx, int dim, String tenant, String collection,
            String chash, float[] embedding, String metadataJson) {
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        var retention = DSL.field(DSL.name("retention"), String.class);
        return ctx.insertInto(ch.table())
                  .columns(ch.tenantId(), ch.collection(), ch.chash(), ch.chunkText(),
                           ch.embedding(), ch.metadata(), retention)
                  .values(tenant, collection, chash, null,
                          Vector.of(embedding),
                          JSONB.jsonb(metadataJson), "reference-only")
                  .onConflict(ch.tenantId(), ch.collection(), ch.chash())
                  .doUpdate()
                  .set(ch.embedding(), DSL.excluded(ch.embedding()))
                  .set(ch.metadata(),  DSL.excluded(ch.metadata()))
                  .set(retention,      DSL.excluded(retention));
    }

    /**
     * Upserts a reference-only chunk: stores a pre-computed embedding + metadata with
     * {@code chunk_text=NULL} and {@code retention='reference-only'} (RDR-169 G4,
     * embed-without-store).
     *
     * <h3>Phase-A status (non-executing)</h3>
     * No {@code /v1} HTTP route is registered in Phase A — option (b) gate: the route
     * registration line lands in Phase B (nexus-dtnpu) alongside the retention schema slice.
     * Additionally, {@link #REFERENCE_ONLY_WRITES_ENABLED} is {@code false} in Phase A:
     * the method runs all pre-SQL validation (null/dim check, full→reference-only guard
     * SELECT) but short-circuits with {@link IllegalStateException} before the
     * retention-binding INSERT.  No Phase-A code path reaches the INSERT.
     *
     * <h3>Null and dim validation (pre-SQL)</h3>
     * {@code embedding} must be non-null, non-empty, and its length must equal the dimension
     * implied by {@code collection}.  Mismatch fails loud — no silent truncation.
     *
     * <h3>full → reference-only transition guard (pre-INSERT SELECT)</h3>
     * If a chunk with the same {@code (tenant, collection, chash)} already exists and has a
     * non-NULL {@code chunk_text}, this method throws {@link IllegalStateException}.  The
     * caller must explicitly delete + re-insert to change retention (RDR-169 §Re-index).
     * {@code reference-only → reference-only} rewrites (embedding/metadata refresh) are
     * permitted; the INSERT's DO UPDATE clause omits {@code chunk_text} as defense-in-depth.
     *
     * <h3>Phase-B deferred (nexus-dtnpu) — open seams</h3>
     * <ul>
     *   <li>TOCTOU between guard SELECT and INSERT: needs a DB-level CHECK or trigger (M1).</li>
     *   <li>reference-only→reference-only rewrite path: SQL correct, untestable until schema.</li>
     *   <li>FTS-NULL-exclusion end-to-end: seed reference-only row, hybrid search, assert absent.</li>
     *   <li>Retention migration: CHECK constraint, DEFAULT 'full' backfill, idempotency.</li>
     *   <li>Guard reads only {@code chunk_text} (not {@code retention}); correctness relies on
     *       the schema invariant {@code chunk_text NOT NULL ⇒ retention='full'}.</li>
     * </ul>
     *
     * @param tenant     tenant principal for RLS scoping
     * @param collection four-segment conformant collection name
     * @param chash      64-hex content-addressed chunk ID (the full sha256, RDR-180)
     * @param embedding  precomputed vector (non-null, non-empty) — dim must match collection
     * @param metadata   chunk metadata (may be empty, not null)
     * @throws IllegalArgumentException if {@code embedding} is null/empty or dim mismatches
     * @throws IllegalStateException    if a full-content chunk already occupies this chash,
     *                                  or if Phase-A write gate is closed
     */
    public void upsertReferenceOnlyChunk(String tenant, String collection,
                                         String chash,
                                         float[] embedding,
                                         Map<String, Object> metadata) {
        // (1) Null / empty guard — pre-SQL, mirrors upsertChunksWithVectors null check.
        if (embedding == null || embedding.length == 0) {
            throw new IllegalArgumentException(
                "upsertReferenceOnlyChunk: embedding must be non-null and non-empty for chash '"
                + chash + "' in collection '" + collection + "'");
        }

        // (2) Dim validation — pre-SQL, fail loud, no silent truncation.
        int dim = dimForCollection(collection);
        if (embedding.length != dim) {
            throw new IllegalArgumentException(
                "upsertReferenceOnlyChunk: " + embedding.length + "-dim vector for collection '"
                + collection + "' which dispatches to chunks_" + dim
                + " (dim mismatch — no silent truncation)");
        }

        String[] collSegs  = collection.split("__");
        boolean conformant = collSegs.length == 4;

        tenantScope.withTenant(tenant, ctx -> {
            // (3) full → reference-only guard: SELECT before INSERT.
            // Reads only chunk_text — safe against Phase-A schema (no retention column needed).
            // A previously-full chunk must never be silently NULLed (RDR-169 §Re-index PROHIBITS).
            DimTables.ChunkTable existingCh = DimTables.CHUNKS.get(dim);
            var existing = ctx.select(existingCh.chunkText()).from(existingCh.table())
                              .where(existingCh.tenantId().eq(tenant)
                                  .and(existingCh.collection().eq(collection))
                                  .and(existingCh.chash().eq(chash)))
                              .fetchOne();
            if (existing != null && existing.value1() != null) {
                throw new IllegalStateException(
                    "upsertReferenceOnlyChunk: chash '" + chash + "' in collection '"
                    + collection + "' already has full content (chunk_text IS NOT NULL). "
                    + "A full→reference-only transition is prohibited by RDR-169 §Re-index. "
                    + "Delete + re-insert to change retention.");
            }

            // (4) Phase-A write gate — short-circuits BEFORE the retention-binding INSERT.
            // REFERENCE_ONLY_WRITES_ENABLED is false until Phase B (nexus-dtnpu) adds the
            // retention column.  The ensure-registered and chunk INSERT below are correct
            // code that will execute once the gate is flipped; they do not run in Phase A.
            if (!REFERENCE_ONLY_WRITES_ENABLED) {
                throw new IllegalStateException(
                    "upsertReferenceOnlyChunk: reference-only writes are disabled until "
                    + "RDR-169 Phase B (retention column absent) — nexus-dtnpu flips "
                    + "REFERENCE_ONLY_WRITES_ENABLED");
            }

            // (5) Ensure-registered: INSERT stub collection row before any chunk write.
            // Standing rule (RDR-156 P0.2, bead nexus-70r3c.2) — mirrors upsertChunksInternal.
            // ON CONFLICT DO NOTHING: preserves fully-populated rows from the catalog ETL.
            ctx.insertInto(CATALOG_COLLECTIONS,
                            CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                            CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                            CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION)
               .values(tenant, collection,
                       conformant ? collSegs[0] : "",
                       conformant ? collSegs[1] : "",
                       conformant ? collSegs[2] : "",
                       conformant ? collSegs[3] : "")
               .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
               .doNothing()
               .execute();

            // (6) Reference-only chunk INSERT (Phase B — requires retention column;
            // see referenceOnlyInsertQuery for the Phase-A/B contract).
            referenceOnlyInsertQuery(ctx, dimForCollection(collection), tenant,
                    collection, chash, embedding,
                    toJson(sanitizeNulDeep(metadata))).execute();
            return null;
        });
        log.debug("event=upsert_reference_only_done collection={} chash={}", collection, chash);
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
    /** Delegates to {@link #searchWithTokens}; discards the token count. source_uri not included. */
    public List<Map<String, Object>> search(String tenant, String queryText,
                                            List<String> collectionNames,
                                            int nResults,
                                            Map<String, Object> where) {
        return searchWithTokens(tenant, queryText, collectionNames, nResults, where, false).value();
    }

    /**
     * Backward-compat 5-arg overload of {@link #searchWithTokens} (source_uri not included).
     * Existing callers (contract tests, graph-hop, metadata-scoped) use this form.
     */
    public Tokened<List<Map<String, Object>>> searchWithTokens(String tenant, String queryText,
                                                               List<String> collectionNames,
                                                               int nResults,
                                                               Map<String, Object> where) {
        return searchWithTokens(tenant, queryText, collectionNames, nResults, where, false);
    }

    /**
     * Token-aware sibling of {@link #search} (bead nexus-ehc4q).
     * Returns search results alongside the embedding token count.
     *
     * @param includeSourceUri when true, resolves source_uri via catalog JOIN (opt-in, RDR-169 G5)
     */
    public Tokened<List<Map<String, Object>>> searchWithTokens(String tenant, String queryText,
                                                               List<String> collectionNames,
                                                               int nResults,
                                                               Map<String, Object> where,
                                                               boolean includeSourceUri) {
        if (collectionNames == null || collectionNames.isEmpty()) {
            return new Tokened<>(List.of(), 0L);
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
        EmbedResult embedResult = embedQuery(collectionNames.get(0), queryText, dim);
        float[] queryVec = embedResult.embeddings().get(0);

        StringBuilder sql = new StringBuilder()
            // RDR-180: bytea storage — hex at the SQL seam (raw-SQL twin of
            // the ChashHex converted type the jOOQ paths use).
            .append("SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,")
            .append(" (embedding <=> ?::vector) AS distance")
            .append(" FROM ").append(chunksTable(dim))
            .append(" WHERE collection IN (").append(placeholders(collectionNames.size())).append(")");
        List<Object> binds = new ArrayList<>();
        binds.add(vectorLiteral(queryVec));
        binds.addAll(collectionNames);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                appendWherePredicate(sql, binds, e.getKey(), e.getValue());
            }
        }
        sql.append(" ORDER BY distance ASC, chash ASC LIMIT ?");
        binds.add(nResults);

        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            // Filtered-ANN recall: keep HNSW scanning past ef_search when the RLS +
            // collection + metadata predicates narrow the candidate set. SET LOCAL is
            // txn-scoped (same pool discipline as the TenantScope GUC stamp).
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            return rawVectorFetch(ctx, sql.toString(), binds.toArray());
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
        // RDR-169 G5: surface address triple additively (chash + span always; source_uri opt-in)
        enrichSearchRows(tenant, rows, includeSourceUri);
        return new Tokened<>(rows, embedResult.tokens());
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
     *   <li><strong>Selectivity-aware dispatch (nexus-lcogi; single-gate-eval nexus-x7z7l).</strong>
     *       ONE bounded fetch of the gate's chashes ({@code LIMIT} {@link #SELECTIVE_GATE_MAX}
     *       {@code + 1}) both picks the plan and, for a SELECTIVE gate ({@code matches <=}
     *       {@link #SELECTIVE_GATE_MAX}), IS the gate evaluation: the complete gate comes back
     *       and is ranked by EXACT cosine distance via a {@code chash IN (...)} PK filter, so
     *       the expensive {@code <%} trigram heap-recheck runs ONCE, not twice (the prior
     *       design ran a standalone {@code COUNT(*)} probe AND re-ran the gate in the ranked
     *       query, two {@code <%} rechecks per call on a large code corpus; conexus-qsa).
     *       This preserves the lcogi fix: the rank never routes through the HNSW index, so a
     *       selective gate cannot be starved past {@code hnsw.max_scan_tuples} (lcogi: the
     *       retired HNSW-first plan returned 6/116k to 0). For a NON-SELECTIVE gate the bounded
     *       fetch hits the {@code LIMIT}, is discarded, and the HNSW-first plan is kept (a
     *       dense gate is found within the scan budget; materializing a huge gated set would
     *       spill {@code work_mem}). Superseded by RDR-156 P5.2 server-side RRF fusion (unified
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
    /**
     * Delegates to the enriched path (includeSourceUri=false) so callers get chash+span,
     * symmetric with {@link #search(String, String, List, int, Map)} (fix #M3-cr).
     */
    public List<Map<String, Object>> hybridSearch(String tenant, String queryText,
                                                  List<String> collectionNames,
                                                  int nResults,
                                                  Map<String, Object> where) {
        return hybridSearchWithTokens(tenant, queryText, collectionNames, nResults, where, false).value();
    }

    /**
     * Token-aware sibling of {@link #hybridSearch} (bead nexus-ehc4q).
     * Returns hybrid search results alongside the embedding token count.
     *
     * @param includeSourceUri when true, resolves source_uri via catalog JOIN (opt-in, RDR-169 G5)
     */
    public Tokened<List<Map<String, Object>>> hybridSearchWithTokens(String tenant, String queryText,
                                                                      List<String> collectionNames,
                                                                      int nResults,
                                                                      Map<String, Object> where,
                                                                      boolean includeSourceUri) {
        long[] tokensOut = {0L};
        List<Map<String, Object>> rows =
            hybridSearch(tenant, queryText, collectionNames, nResults, where, SELECTIVE_GATE_MAX,
                         tokensOut);
        // RDR-169 G5: surface address triple additively (chash + span always; source_uri opt-in)
        enrichSearchRows(tenant, rows, includeSourceUri);
        return new Tokened<>(rows, tokensOut[0]);
    }

    /**
     * Backward-compat 5-arg overload of {@link #hybridSearchWithTokens} (source_uri not included).
     */
    public Tokened<List<Map<String, Object>>> hybridSearchWithTokens(String tenant, String queryText,
                                                                      List<String> collectionNames,
                                                                      int nResults,
                                                                      Map<String, Object> where) {
        return hybridSearchWithTokens(tenant, queryText, collectionNames, nResults, where, false);
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
    /** Package-private overload for tests pinning selectiveGateMax; discards token count. */
    public List<Map<String, Object>> hybridSearch(String tenant, String queryText,
                                           List<String> collectionNames,
                                           int nResults,
                                           Map<String, Object> where,
                                           int selectiveGateMax) {
        return hybridSearch(tenant, queryText, collectionNames, nResults, where, selectiveGateMax, null);
    }

    private List<Map<String, Object>> hybridSearch(String tenant, String queryText,
                                           List<String> collectionNames,
                                           int nResults,
                                           Map<String, Object> where,
                                           int selectiveGateMax,
                                           long[] tokensOut) {
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

        EmbedResult hybridEmbed = embedQuery(collectionNames.get(0), queryText, dim);
        if (tokensOut != null) tokensOut[0] = hybridEmbed.tokens();
        float[] queryVec = hybridEmbed.embeddings().get(0);

        // Non-text scope (collection IN + metadata where). Shared by the selective
        // rank-by-chash query (nexus-x7z7l): that query re-applies these cheap predicates
        // but NOT the text gate - the gate's matching chashes already satisfy it, so the
        // expensive <% trigram heap-recheck runs ONCE (in the bounded fetch below), not
        // again at rank time. (The metadata->>? predicate is kept on the rank query, not
        // dropped: two same-text rows in different collections share a chash, so chash
        // alone would not re-impose a per-row metadata filter.)
        StringBuilder scope = new StringBuilder()
            .append(" WHERE collection IN (").append(placeholders(collectionNames.size())).append(")");
        List<Object> scopeBinds = new ArrayList<>(collectionNames);
        // Full gate = scope AND a text signal. FTS lexeme match OR word-trigram similarity:
        // the <% operator form (word_similarity >= pg_trgm.word_similarity_threshold) is
        // gin_trgm_ops-indexable (vectors-002) where the function-call form is not;
        // word_similarity (vs plain similarity) does not dilute with chunk_text length.
        // The threshold GUC is pinned per-transaction below.
        StringBuilder gate = new StringBuilder(scope)
            .append(" AND (chunk_tsv @@ plainto_tsquery('english', ?) OR ? <% chunk_text)");
        List<Object> gateBinds = new ArrayList<>(scopeBinds);
        gateBinds.add(queryText);
        gateBinds.add(queryText);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                appendWherePredicate(gate, gateBinds, e.getKey(), e.getValue());
                appendWherePredicate(scope, scopeBinds, e.getKey(), e.getValue());
            }
        }
        final String table = chunksTable(dim);
        final String gateSql = gate.toString();
        final String scopeSql = scope.toString();
        final String vecLit = vectorLiteral(queryVec);

        // SELECTIVITY-AWARE DISPATCH (nexus-lcogi; single-gate-eval, nexus-x7z7l). ONE
        // bounded fetch of the gate's chashes (LIMIT SELECTIVE_GATE_MAX+1) both picks the
        // plan AND, for the selective case, IS the gate evaluation - the ranked query then
        // filters by chash (PK lookup), so the expensive <% trigram heap-recheck runs once,
        // not twice. The prior design ran a standalone COUNT(*) probe AND re-ran the gate in
        // the ranked query: on a large code corpus that was two ~650ms <% heap-rechecks per
        // call (conexus-qsa EXPLAIN: count probe 700ms + materialized-CTE rank 654ms, both
        // dominated by the lossy gin_trgm_ops recheck over ~1900 candidate rows).
        //
        //   * SELECTIVE gate (matches <= SELECTIVE_GATE_MAX): the bounded fetch returns the
        //     COMPLETE gate (all matches, since it did not hit the LIMIT). Rank those exact
        //     chashes by cosine distance via a chash IN (...) filter + the cheap non-text
        //     scope (collection/metadata). No HNSW, no re-gate: ranks the small gated set
        //     exactly, with NO dependence on hnsw.max_scan_tuples (the lcogi collapse fix is
        //     preserved - the prior HNSW-first single-query plan starved selective gates).
        //
        //   * NON-SELECTIVE gate (matches > SELECTIVE_GATE_MAX): the bounded fetch hit the
        //     LIMIT (returned SELECTIVE_GATE_MAX+1 chashes) and is discarded - keep the
        //     HNSW-first plan (gate as scan filter, iterative_scan). A dense gate is usually
        //     found within the scan budget; materializing a huge gated set (~4 KB/row
        //     embeddings) would spill work_mem. The bounded fetch caps this probe's cost
        //     (the prior unbounded COUNT scanned the full dense gate). Same SEMI-selective
        //     caveat as before applies; P5.2's RRF fusion closes that window.
        //
        // matches == 0 -> empty gate -> selective branch returns an empty result (no silent
        // vector fallback), handled explicitly (chash IN () is not valid SQL).
        List<Object> probeBinds = new ArrayList<>(gateBinds);
        probeBinds.add(selectiveGateMax + 1);
        Result<Record> result = tenantScope.withTenant(tenant, ctx -> {
            // Trigram gate calibration (contract anchor): word_similarity >= 0.6, pg_trgm's
            // default - typo-probe candidates sit at ~0.9 and pass, no-signal rows at ~0.1
            // do not. Pinned per-transaction so the gate is independent of cluster config.
            PgSession.setLocal(ctx, "pg_trgm.word_similarity_threshold", "0.6");

            List<String> gateChashes = rawVectorFetch(
                ctx, "SELECT encode(chash, 'hex') AS chash FROM " + table + gateSql + " LIMIT ?",
                probeBinds.toArray())
                .map(r -> r.get("chash", String.class));

            if (gateChashes.size() <= selectiveGateMax) {
                // Selective: the bounded fetch returned the COMPLETE gate (the LIMIT did NOT
                // fire - fewer than selectiveGateMax+1 matches exist, so it scanned the full
                // GIN candidate set, same work the old COUNT(*) did). The win is not a
                // cheaper probe: this single gate scan REPLACES both the old COUNT(*) probe
                // AND the MATERIALIZED-CTE gate re-evaluation - the rank below filters by
                // chash with NO text gate, so the <% heap-recheck happens once, not twice.
                if (gateChashes.isEmpty()) {
                    // Empty gate: typed-empty result (chash IN () is invalid SQL).
                    return rawVectorFetch(ctx,
                        "SELECT NULL::text AS chash, NULL::text AS chunk_text,"
                        + " NULL::text AS collection, NULL::text AS metadata_json,"
                        + " NULL::float8 AS distance WHERE false");
                }
                // chash is NOT unique across collections (the table key is
                // (tenant_id, collection, chash)): a multi-collection gate can return the
                // same chash from N collections. Dedup the IN list - the collection scope in
                // scopeSql still yields one ranked row per (collection, chash). Dedup runs
                // AFTER the size-based dispatch so the selective/non-selective boundary stays
                // identical to the old per-row COUNT(*).
                List<String> inChashes = gateChashes.stream().distinct().toList();
                String sql = "SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,"
                    + " (embedding <=> ?::vector) AS distance FROM " + table + scopeSql
                    + " AND chash IN (" + decodePlaceholders(inChashes.size()) + ")"
                    + " ORDER BY distance ASC, chash ASC LIMIT ?";
                List<Object> b = new ArrayList<>();
                b.add(vecLit);
                b.addAll(scopeBinds);
                b.addAll(inChashes);
                b.add(nResults);
                return rawVectorFetch(ctx, sql, b.toArray());
            }
            // HNSW-first for a dense gate: keep HNSW scanning past ef_search.
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            String sql = "SELECT encode(chash, 'hex') AS chash, chunk_text, collection, metadata::text AS metadata_json,"
                + " (embedding <=> ?::vector) AS distance FROM " + table + gateSql
                + " ORDER BY distance ASC, chash ASC LIMIT ?";
            List<Object> b = new ArrayList<>();
            b.add(vecLit);
            b.addAll(gateBinds);
            b.add(nResults);
            return rawVectorFetch(ctx, sql, b.toArray());
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
     * @param includeSourceUri when true, resolves source_uris via catalog JOIN (opt-in, RDR-169 G5)
     * @return Chroma-style envelope {@code {ids: List<String>, documents: List<String>,
     *         metadatas: List<Map>}} plus {@code chashes}/{@code spans} always and
     *         {@code source_uris} when requested; IDs not present (or not visible
     *         under RLS) are omitted; {@code limit}/{@code offset} paginate in chash
     *         order (same ordering as {@link #list})
     */
    public Map<String, Object> get(String tenant, String collection,
                                   List<String> ids, int limit, int offset,
                                   boolean includeSourceUri) {
        int dim = dimForCollection(collection);
        if (ids == null || ids.isEmpty()) {
            return Map.of("ids", List.of(), "documents", List.of(), "metadatas", List.of());
        }

        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        var result = tenantScope.withTenant(tenant, ctx ->
            ctx.select(ch.chash(), ch.chunkText(), ch.metadata())
               .from(ch.table())
               .where(ch.collection().eq(collection).and(ch.chash().in(ids)))
               .orderBy(ch.chash().asc())
               .limit(limit).offset(offset)
               .fetch());

        List<String> outIds = new ArrayList<>(result.size());
        List<String> outDocs = new ArrayList<>(result.size());
        List<Map<String, Object>> outMetas = new ArrayList<>(result.size());
        for (var rec : result) {
            outIds.add(rec.value1());
            outDocs.add(rec.value2());
            JSONB meta = rec.value3();
            outMetas.add(fromJson(meta != null ? meta.data() : null));
        }
        // RDR-169 G5: surface address triple additively as parallel lists (source_uri opt-in)
        return enrichGetEnvelope(tenant,
            new LinkedHashMap<>(Map.of("ids", outIds, "documents", outDocs, "metadatas", outMetas)),
            includeSourceUri);
    }

    /** Backward-compat 5-arg overload of {@link #get} (source_uri not included). */
    public Map<String, Object> get(String tenant, String collection,
                                   List<String> ids, int limit, int offset) {
        return get(tenant, collection, ids, limit, offset, false);
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
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        var result = tenantScope.withTenant(tenant, ctx ->
            ctx.select(ch.chash(), ch.embedding())
               .from(ch.table())
               .where(ch.collection().eq(collection).and(ch.chash().in(ids)))
               .fetch());

        Map<String, List<Float>> byChash = new HashMap<>();
        for (var rec : result) {
            Vector v = rec.value2();
            List<Float> floats = new ArrayList<>();
            if (v != null) for (float f : v.floats()) floats.add(f);
            byChash.put(rec.value1(), floats);
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
        putWithTokens(tenant, collection, docId, content, metadata);
        return docId;
    }

    /**
     * Token-aware sibling of {@link #put} (bead nexus-ehc4q).
     * Delegates to {@link #upsertChunksWithTokens}; the token count is the
     * embedding cost for the single chunk.
     */
    public Tokened<String> putWithTokens(String tenant, String collection, String docId,
                                          String content, Map<String, Object> metadata) {
        Tokened<Integer> result = upsertChunksWithTokens(
            tenant, collection, List.of(docId), List.of(content),
            List.of(metadata != null ? metadata : Map.of()));
        return new Tokened<>(docId, result.tokens());
    }

    /**
     * Get chunks matching a metadata {@code where} equality filter, paginated in
     * chash order (RDR-155 P4a.2, bead nexus-1k8s1).
     *
     * <p>The incremental-sync staleness check's shape: the Python
     * {@code _ServiceCollectionStub.get(where=...)} asks for chunks whose
     * {@code source_key} / {@code content_hash} match. Plain-equality predicates
     * (ANDed) and the common single-field Chroma operator-form
     * ({@code $eq}/{@code $ne}/{@code $in}/{@code $nin}, plus the
     * {@code $gte}/{@code $lte}/{@code $gt}/{@code $lt} range operators since
     * nexus-4l80g) are supported via {@link #metadataCondition} — the jOOQ twin
     * of {@link #appendWherePredicate}, kept to the same operator subset
     * {@link #search} applies (nexus-05bfd). Unsupported operator shapes fail
     * loud with 400 rather than silently matching nothing; compound
     * {@code $and}/{@code $or} remains untranslated.
     *
     * @param where metadata equality predicates (ANDed); null/empty returns the
     *              collection paginated (the {@code store-get}-without-ids shape)
     * @return Chroma-style envelope {@code {ids, documents, metadatas}} aligned by
     *         index, chash ascending
     */
    public Map<String, Object> getWhere(String tenant, String collection,
                                        Map<String, Object> where,
                                        int limit, int offset,
                                        boolean includeSourceUri) {
        int dim = dimForCollection(collection);
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        org.jooq.Condition cond = ch.collection().eq(collection);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                cond = cond.and(metadataCondition(e.getKey(), e.getValue()));
            }
        }
        org.jooq.Condition finalCond = cond;
        var result = tenantScope.withTenant(tenant, ctx ->
            ctx.select(ch.chash(), ch.chunkText(), ch.metadata())
               .from(ch.table())
               .where(finalCond)
               .orderBy(ch.chash().asc())
               .limit(limit).offset(offset)
               .fetch());

        List<String> outIds = new ArrayList<>(result.size());
        List<String> outDocs = new ArrayList<>(result.size());
        List<Map<String, Object>> outMetas = new ArrayList<>(result.size());
        for (var rec : result) {
            outIds.add(rec.value1());
            outDocs.add(rec.value2());
            JSONB meta = rec.value3();
            outMetas.add(fromJson(meta != null ? meta.data() : null));
        }
        // RDR-169 G5: surface address triple additively as parallel lists (source_uri opt-in)
        return enrichGetEnvelope(tenant,
            new LinkedHashMap<>(Map.of("ids", outIds, "documents", outDocs, "metadatas", outMetas)),
            includeSourceUri);
    }

    /** Backward-compat 5-arg overload of {@link #getWhere} (source_uri not included). */
    public Map<String, Object> getWhere(String tenant, String collection,
                                        Map<String, Object> where,
                                        int limit, int offset) {
        return getWhere(tenant, collection, where, limit, offset, false);
    }

    /**
     * Upper bound on rows a single {@link #getAllMetadata} call will return.
     * Above this, the caller (indexer staleness-cache build) falls back to
     * paginated {@code /v1/vectors/get} rather than the server materializing
     * an unbounded response in memory.
     */
    public static final int GET_ALL_METADATA_MAX_ROWS = 200_000;

    /**
     * Batch cap for the sourceUrisByChash IN-clause join — bounds the
     * per-query bind-parameter budget. Rehomed from
     * ChromaQuotaValidator.MAX_RECORDS_PER_WRITE at RDR-155 P4b P0e
     * (same own-the-constant precedent as RemapRepository.MAX_BATCH):
     * the value is a generic bind-param ceiling, not a Chroma quota,
     * and ChromaQuotaValidator deletes with the chroma trio at P2-J.
     */
    public static final int SOURCE_URI_JOIN_BATCH = 300;

    /**
     * Fetch ids + metadata for EVERY chunk in *collection* in ONE round trip
     * (nexus-duoak follow-up: staleness-cache-build phase). No {@code
     * documents} column (staleness only needs metadata: {@code
     * chunk_text_hash}, spans, {@code doc_id}) and no LIMIT/OFFSET — this
     * collapses the ``ceil(chunk_count / 300)`` client round trips
     * {@code build_staleness_cache}'s paginated {@code /v1/vectors/get} loop
     * pays (measured: ~113s of the phase's ~116s total on this repo's own
     * 24k-chunk {@code code__} collection) into ONE Postgres query + ONE
     * HTTP response. Same rationale as the catalog {@code update_many}/
     * {@code delete_many} batch endpoints: server-to-Postgres round trips
     * are same-process and cheap; client-to-server round trips pay real
     * network latency each time.
     *
     * <p>Capped at {@value #GET_ALL_METADATA_MAX_ROWS} rows — above that the
     * caller should fall back to the paginated path rather than have the
     * server materialize an unbounded result set in memory. Throws {@link
     * IllegalStateException} when the cap is exceeded (fail loud, not a
     * silent truncation — a caller silently getting a partial staleness
     * cache would treat un-fetched rows as "unknown", which the existing
     * cache-miss contract already treats as safely stale-and-reindex, but
     * silently truncating without signaling would hide that this fast path
     * degraded).
     */
    public Map<String, Object> getAllMetadata(String tenant, String collection,
                                              Map<String, Object> where) {
        int dim = dimForCollection(collection);
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        org.jooq.Condition cond = ch.collection().eq(collection);
        if (where != null) {
            for (Map.Entry<String, Object> e : where.entrySet()) {
                cond = cond.and(metadataCondition(e.getKey(), e.getValue()));
            }
        }
        org.jooq.Condition finalCond = cond;
        var result = tenantScope.withTenant(tenant, ctx ->
            ctx.select(ch.chash(), ch.metadata())
               .from(ch.table())
               .where(finalCond)
               .orderBy(ch.chash().asc())
               .limit(GET_ALL_METADATA_MAX_ROWS + 1)
               .fetch());

        if (result.size() > GET_ALL_METADATA_MAX_ROWS) {
            throw new IllegalStateException(
                "getAllMetadata: collection '" + collection + "' has more than "
                + GET_ALL_METADATA_MAX_ROWS + " matching rows; caller must fall "
                + "back to paginated /v1/vectors/get");
        }

        List<String> outIds = new ArrayList<>(result.size());
        List<Map<String, Object>> outMetas = new ArrayList<>(result.size());
        for (var rec : result) {
            outIds.add(rec.value1());
            JSONB meta = rec.value2();
            outMetas.add(fromJson(meta != null ? meta.data() : null));
        }
        return new LinkedHashMap<>(Map.of("ids", outIds, "metadatas", outMetas));
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
        var names = tenantScope.withTenant(tenant, ctx ->
            ctx.selectDistinct(CHUNKS_384.COLLECTION).from(CHUNKS_384)
               .union(ctx.selectDistinct(CHUNKS_768.COLLECTION).from(CHUNKS_768))
               .union(ctx.selectDistinct(CHUNKS_1024.COLLECTION).from(CHUNKS_1024))
               .orderBy(1)
               .fetch());
        List<Map<String, Object>> out = new ArrayList<>(names.size());
        for (var rec : names) {
            out.add(Map.of("name", rec.value1()));
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
     * <p>nexus-889ff extends the catalog dance coverage so the {@code query}-tool repoint
     * can fully collapse: {@code subtree} is a tumbler-prefix scope; {@code where} is a
     * chunk-metadata equality map (JSONB containment); {@code author} is matched
     * case-insensitively as a SUBSTRING (ILIKE) — mirroring {@code query()}'s
     * {@code author.lower() IN} semantics, not exact equality. Each returned row also
     * carries the MATCHED chunk's {@code chash} (audit HIGH: the repoint populates the
     * RDR-086 {@code chunk_text_hash} from it).
     *
     * @param contentType catalog content_type filter; null = no filter
     * @param author      catalog author SUBSTRING filter (ILIKE); null = no filter
     * @param year        catalog year filter; null = no filter
     * @param corpus      catalog corpus filter; null = no filter
     * @param subtree     tumbler-prefix scope — DESCENDANTS only (root-exclusive, matching
     *                    {@code cat.descendants()}); also filters alias rows; null = no filter
     * @param where       chunk-metadata equality predicates (JSONB containment); null/empty
     *                    = no filter
     */
    /** 8-arg back-compat (nexus-889ff): delegates with null subtree/where; discards tokens. */
    public List<Map<String, Object>> searchMetadataScoped(
            String tenant, String queryText, List<String> collectionNames,
            String contentType, String author, Integer year, String corpus, int nResults) {
        return searchMetadataScoped(tenant, queryText, collectionNames, contentType, author,
                                    year, corpus, null, null, nResults);
    }

    /** Full metadata-scoped search with subtree + where (nexus-889ff); discards the token count. */
    public List<Map<String, Object>> searchMetadataScoped(
            String tenant, String queryText, List<String> collectionNames,
            String contentType, String author, Integer year, String corpus,
            String subtree, Map<String, Object> where, int nResults) {
        return searchMetadataScopedWithTokens(tenant, queryText, collectionNames, contentType,
                                              author, year, corpus, subtree, where, nResults).value();
    }

    /**
     * Token-aware sibling (bead nexus-ehc4q) of the full metadata-scoped search — returns
     * the matched rows plus the embedding token count for X-Nexus-Usage-Tokens emission.
     */
    public Tokened<List<Map<String, Object>>> searchMetadataScopedWithTokens(
            String tenant, String queryText, List<String> collectionNames,
            String contentType, String author, Integer year, String corpus,
            String subtree, Map<String, Object> where, int nResults) {
        if (collectionNames == null || collectionNames.isEmpty()) {
            return new Tokened<>(List.of(), 0L);
        }
        if (nResults < 1) {
            throw new IllegalArgumentException("nResults must be >= 1, got " + nResults);
        }
        int dim = requireHomogeneousDim(collectionNames);
        requireHomogeneousModel(collectionNames);
        EmbedResult embed = embedQuery(collectionNames.get(0), queryText, dim);
        Vector queryVec = Vector.of(embed.embeddings().get(0));
        JSONB whereJsonb = (where == null || where.isEmpty()) ? null : JSONB.jsonb(toJson(where));
        String[] colls = collectionNames.toArray(String[]::new);

        org.jooq.Table<?> fn = switch (dim) {
            case 384  -> SEARCH_METADATA_SCOPED_384.call(
                queryVec, colls, contentType, author, year, corpus, subtree, whereJsonb, nResults);
            case 768  -> SEARCH_METADATA_SCOPED_768.call(
                queryVec, colls, contentType, author, year, corpus, subtree, whereJsonb, nResults);
            case 1024 -> SEARCH_METADATA_SCOPED_1024.call(
                queryVec, colls, contentType, author, year, corpus, subtree, whereJsonb, nResults);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
        return new Tokened<>(runCombinedQueryWithChash(tenant, fn), embed.tokens());
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
    /** Delegates to {@link #searchTopicScopedWithTokens}; discards the token count. */
    public List<Map<String, Object>> searchTopicScoped(
            String tenant, String queryText, String topicLabel, String collection, int nResults) {
        return searchTopicScopedWithTokens(tenant, queryText, topicLabel, collection, nResults).value();
    }

    /**
     * Token-aware sibling of {@link #searchTopicScoped} (bead nexus-ehc4q).
     */
    public Tokened<List<Map<String, Object>>> searchTopicScopedWithTokens(
            String tenant, String queryText, String topicLabel, String collection, int nResults) {
        if (collection == null || collection.isBlank()) {
            return new Tokened<>(List.of(), 0L);
        }
        if (nResults < 1) {
            throw new IllegalArgumentException("nResults must be >= 1, got " + nResults);
        }
        int dim = dimForCollection(collection);
        EmbedResult embed = embedQuery(collection, queryText, dim);
        Vector queryVec = Vector.of(embed.embeddings().get(0));

        org.jooq.Table<?> fn = switch (dim) {
            case 384  -> SEARCH_TOPIC_SCOPED_384.call(queryVec, topicLabel, collection, nResults);
            case 768  -> SEARCH_TOPIC_SCOPED_768.call(queryVec, topicLabel, collection, nResults);
            case 1024 -> SEARCH_TOPIC_SCOPED_1024.call(queryVec, topicLabel, collection, nResults);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
        return new Tokened<>(runCombinedQuery(tenant, fn), embed.tokens());
    }

    /**
     * Graph-hop combined search (RDR-156 P4 follow-on, Decision 5, bead nexus-houg9).
     *
     * <p>Calls {@code nexus.search_graph_hop_<dim>} (catalog-007): a {@code WITH
     * RECURSIVE} BFS over {@code nexus.catalog_links} from {@code seeds} to {@code depth}
     * hops collects the reachable document set, which is joined to {@code chunks_<dim>}
     * and vector-ranked. This retires the {@code query} tool's app-side {@code follow_links}
     * dance ({@link dev.nexus.service.db.CatalogRepository#graphBFS} + per-collection
     * search + re-join). Traversal semantics mirror {@code graphBFS} exactly: seeds are
     * at depth 0 and included; {@code direction} is {@code "out"}/{@code "in"}/{@code "both"}
     * (default {@code "both"}, matching {@code Catalog.graph}); a {@code null} link type
     * follows all edge types. {@code depth} is clamped to [1,3] — the same bound graphBFS
     * applies.
     *
     * <p>Returns the document tumbler as {@code id} (document-level, like
     * {@link #searchMetadataScoped}) AND the matched chunk's {@code chash} (audit HIGH:
     * the {@code query}-repoint populates the RDR-086 {@code chunk_text_hash} from this
     * matched-chunk chash, never a per-doc manifest guess). A document with multiple
     * matching chunks can appear more than once; consumer-side de-dup is the repoint's job.
     *
     * @param seeds     seed document tumblers to traverse from
     * @param linkType  catalog link_type filter; null = follow all types
     * @param depth     BFS depth (clamped to [1,3])
     * @param direction "out" | "in" | "both"
     */
    /** Delegates to {@link #searchGraphHopWithTokens}; discards the token count. */
    public List<Map<String, Object>> searchGraphHop(
            String tenant, String queryText, List<String> seeds, List<String> collectionNames,
            String linkType, int depth, String direction, int nResults) {
        return searchGraphHop(
            tenant, queryText, seeds, collectionNames, linkType, depth, direction, null, nResults);
    }

    /** Where-filtered variant (bead nexus-7ndh3); discards the token count. */
    public List<Map<String, Object>> searchGraphHop(
            String tenant, String queryText, List<String> seeds, List<String> collectionNames,
            String linkType, int depth, String direction, Map<String, Object> where, int nResults) {
        return searchGraphHopWithTokens(
            tenant, queryText, seeds, collectionNames, linkType, depth, direction, where, nResults)
            .value();
    }

    /** No-where compatibility overload of {@link #searchGraphHopWithTokens}. */
    public Tokened<List<Map<String, Object>>> searchGraphHopWithTokens(
            String tenant, String queryText, List<String> seeds, List<String> collectionNames,
            String linkType, int depth, String direction, int nResults) {
        return searchGraphHopWithTokens(
            tenant, queryText, seeds, collectionNames, linkType, depth, direction, null, nResults);
    }

    /**
     * Token-aware sibling of {@link #searchGraphHop} (bead nexus-ehc4q).
     *
     * <p>{@code where} (bead nexus-7ndh3, RDR-156 H2 residual) is a chunk-metadata
     * equality map applied as JSONB containment ({@code c.metadata @> p_where}) in the
     * post-BFS rank — byte-for-byte the {@link #searchMetadataScoped} semantics. Null or
     * empty means no filter. This retires the {@code query} tool's follow_links+where
     * app-side fallback (the {@code _skip_service} arm).
     */
    public Tokened<List<Map<String, Object>>> searchGraphHopWithTokens(
            String tenant, String queryText, List<String> seeds, List<String> collectionNames,
            String linkType, int depth, String direction, Map<String, Object> where, int nResults) {
        if (seeds == null || seeds.isEmpty()
                || collectionNames == null || collectionNames.isEmpty()) {
            return new Tokened<>(List.of(), 0L);
        }
        if (nResults < 1) {
            throw new IllegalArgumentException("nResults must be >= 1, got " + nResults);
        }
        String dir = (direction == null || direction.isBlank()) ? "both" : direction;
        if (!dir.equals("out") && !dir.equals("in") && !dir.equals("both")) {
            throw new IllegalArgumentException(
                "direction must be 'out', 'in', or 'both', got '" + direction + "'");
        }
        int clampedDepth = Math.min(Math.max(depth, 1), 3);  // mirror graphBFS bound
        int dim = requireHomogeneousDim(collectionNames);
        requireHomogeneousModel(collectionNames);
        EmbedResult embed = embedQuery(collectionNames.get(0), queryText, dim);
        Vector queryVec = Vector.of(embed.embeddings().get(0));
        JSONB whereJsonb = (where == null || where.isEmpty()) ? null : JSONB.jsonb(toJson(where));
        String[] seedArr = seeds.toArray(String[]::new);
        String[] colls = collectionNames.toArray(String[]::new);

        org.jooq.Table<?> fn = switch (dim) {
            case 384  -> SEARCH_GRAPH_HOP_384.call(
                queryVec, seedArr, colls, linkType, clampedDepth, dir, whereJsonb, nResults);
            case 768  -> SEARCH_GRAPH_HOP_768.call(
                queryVec, seedArr, colls, linkType, clampedDepth, dir, whereJsonb, nResults);
            case 1024 -> SEARCH_GRAPH_HOP_1024.call(
                queryVec, seedArr, colls, linkType, clampedDepth, dir, whereJsonb, nResults);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
        return new Tokened<>(runCombinedQueryWithChash(tenant, fn), embed.tokens());
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

    /**
     * Validate every collection dispatches to the same embedding MODEL (nexus-9y5om,
     * nexus-3l6gz): same-DIM, different-MODEL collections pass requireHomogeneousDim but
     * silently mis-embed — one query vector embedded against the FIRST collection's model
     * cannot serve a second collection's different embedding space even at equal
     * dimensionality (voyage-code-3 and voyage-context-3 are both 1024-dim). Parses the same
     * 3rd '__' segment {@link #dimForCollection} uses.
     */
    private static void requireHomogeneousModel(List<String> collectionNames) {
        String model = modelSegment(collectionNames.get(0));
        for (String col : collectionNames) {
            String colModel = modelSegment(col);
            if (!colModel.equals(model)) {
                throw new IllegalArgumentException(
                    "mixed embedding models in one combined-query call: '" + collectionNames.get(0)
                    + "' uses '" + model + "' but '" + col + "' uses '" + colModel
                    + "' - one query vector cannot serve two different embedding spaces");
            }
        }
    }

    private static String modelSegment(String collection) {
        String[] segments = collection.split("__");
        if (segments.length != 4) {
            throw new IllegalArgumentException(
                "collection '" + collection + "' is not four-segment conformant "
                + "(<content_type>__<owner>__<model>__v<n>)");
        }
        return segments[2];
    }

    /**
     * Embed the query server-side, routing by collection; fail loud on dim mismatch.
     * Returns the embedding result including the token count so callers can propagate
     * it as a return value (bead nexus-ehc4q — no ThreadLocal side-channel).
     */
    private EmbedResult embedQuery(String collection, String queryText, int dim) {
        EmbedResult result = (queryRouter != null)
                ? queryRouter.embedOneForCollectionWithUsage(collection, queryText)
                : queryEmbedder.embedWithUsage(List.of(queryText));
        float[] queryVec = result.embeddings().get(0);
        if (queryVec.length != dim) {
            throw new IllegalArgumentException(
                "query embedder produced a " + queryVec.length
                + "-dim vector but the collection dispatches to chunks_" + dim);
        }
        return result;
    }

    /**
     * Execute a combined-query function call under the tenant RLS scope with the
     * filtered-ANN session setting, and map the (id, content, collection, distance)
     * rows to the flat search() envelope.
     *
     * <p>{@code fn} is a jOOQ generated table-valued-function reference
     * ({@code SEARCH_<KIND>_SCOPED_<dim>.call(...)} / {@code SEARCH_GRAPH_HOP_<dim>
     * .call(...)}) built by the caller's dim-dispatch switch — the typed-DSL
     * conversion of the former caller-built SQL strings (nexus-7ndh3; retired the
     * {@code runCombinedQuery*} entries from {@code RawSqlGateTest}'s sanctioned
     * allowlist).
     */
    private List<Map<String, Object>> runCombinedQuery(
            String tenant, org.jooq.Table<?> fn) {
        Result<? extends Record> result = tenantScope.withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            return ctx.selectFrom(fn).fetch();
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
     * Like {@link #runCombinedQuery} but also maps the matched chunk's {@code chash}
     * column (graph-hop + metadata-scoped, bead nexus-houg9). Kept separate because the
     * topic function does not expose chash — {@code rec.get("chash", ...)} would throw.
     */
    private List<Map<String, Object>> runCombinedQueryWithChash(
            String tenant, org.jooq.Table<?> fn) {
        Result<? extends Record> result = tenantScope.withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            return ctx.selectFrom(fn).fetch();
        });
        List<Map<String, Object>> rows = new ArrayList<>(result.size());
        for (Record rec : result) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("id",         rec.get("id", String.class));
            row.put("content",    rec.get("content", String.class));
            row.put("distance",   rec.get("distance", Double.class));
            row.put("collection", rec.get("collection", String.class));
            row.put("chash",      rec.get("chash", String.class));
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
        var result = tenantScope.withTenant(tenant, ctx ->
            ctx.select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.DIM,
                       COLLECTION_VECTOR_STATS.CHUNK_COUNT, COLLECTION_VECTOR_STATS.LAST_WRITE)
               .from(COLLECTION_VECTOR_STATS)
               .orderBy(COLLECTION_VECTOR_STATS.COLLECTION.asc(), COLLECTION_VECTOR_STATS.DIM.asc())
               .fetch());
        List<Map<String, Object>> out = new ArrayList<>(result.size());
        for (var rec : result) {
            Map<String, Object> row = new java.util.LinkedHashMap<>();
            row.put("name",  rec.value1());
            row.put("dim",   rec.value2());
            row.put("count", rec.value3());
            var lastWrite = rec.value4();
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
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        var result = tenantScope.withTenant(tenant, ctx ->
            ctx.select(ch.chash(), ch.metadata())
               .from(ch.table())
               .where(ch.collection().eq(collection))
               .orderBy(ch.chash().asc())
               .limit(limit).offset(offset)
               .fetch());

        List<String> outIds = new ArrayList<>(result.size());
        List<Map<String, Object>> outMetas = new ArrayList<>(result.size());
        for (var rec : result) {
            outIds.add(rec.value1());
            JSONB meta = rec.value2();
            outMetas.add(fromJson(meta != null ? meta.data() : null));
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
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        return tenantScope.withTenant(tenant, ctx ->
            ctx.deleteFrom(ch.table())
               .where(ch.collection().eq(collection).and(ch.chash().in(ids)))
               .execute());
    }

    /**
     * Count chunks in a collection visible to {@code tenant}.
     */
    public int count(String tenant, String collection) {
        int dim = dimForCollection(collection);
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        long c = tenantScope.withTenant(tenant, ctx ->
            (long) ctx.fetchCount(ch.table(), ch.collection().eq(collection)));
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
     * <p>RDR-181 (bead nexus-f0r8p.2): returns the total affected-row count summed
     * across {@code ids} rather than {@code void}. A count lower than {@code ids.size()}
     * means one or more rows no longer exist (e.g. a concurrent delete) — callers that
     * care WHICH ids missed (the have-vector reroute in {@link #upsertChunksInternal})
     * call this once per id so the single-id count is directly the race signal; batch
     * callers (the HTTP {@code update-metadata} endpoint) can use the summed count as
     * a coarser "how many actually existed" signal.
     *
     * @param metadatas replacement metadata maps aligned with {@code ids}
     * @return total rows affected across all {@code ids} (0 to {@code ids.size()})
     */
    public int updateMetadata(String tenant, String collection,
                               List<String> ids,
                               List<Map<String, Object>> metadatas) {
        int dim = dimForCollection(collection);
        if (ids == null || ids.isEmpty()) return 0;
        if (ids.size() != metadatas.size()) {
            throw new IllegalArgumentException(
                "ids (" + ids.size() + ") and metadatas (" + metadatas.size()
                + ") must be aligned");
        }
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        return tenantScope.withTenant(tenant, ctx -> {
            int affected = 0;
            for (int i = 0; i < ids.size(); i++) {
                affected += updateMetadataOneRow(ctx, ch, collection, ids.get(i), metadatas.get(i));
            }
            return affected;
        });
    }

    /**
     * Ctx-level metadata-only UPDATE for exactly one chash — the shared SQL shape
     * behind both {@link #updateMetadata} (its own short transaction, the HTTP
     * frecency-reindex path) and {@link #resolveNeedEmbedIdx}'s have-vector branch
     * (same transaction as that method's existence SELECT, RDR-181 bead nexus-f0r8p.2).
     * Factored out so both call sites share identical sanitization + JSON shape — the
     * metadata a have-vector UPDATE writes MUST be indistinguishable from what a fresh
     * INSERT would have written (metadata-parity acceptance criterion).
     *
     * @return rows affected (0 or 1) — 0 means no row currently matches
     *         {@code (collection, chash)} under RLS
     */
    private static int updateMetadataOneRow(DSLContext ctx, DimTables.ChunkTable ch,
                                             String collection, String chash,
                                             Map<String, Object> metadata) {
        // Same NUL defense as upsertChunks: jsonb rejects NUL just like text does
        // (nexus-rvfwj, dual-review M2).
        return ctx.update(ch.table())
                  .set(ch.metadata(), JSONB.jsonb(toJson(sanitizeNulDeep(metadata))))
                  .where(ch.collection().eq(collection).and(ch.chash().eq(chash)))
                  .execute();
    }

    /**
     * RDR-181 (bead nexus-f0r8p.1): PK-indexed existence lookup — which of the given
     * chashes already have a stored row (and therefore a stored vector) in
     * {@code (tenant, collection)}. This is the existence-partition primitive behind
     * the embed-skip design (a later bead wires the partition into
     * {@link #upsertChunksInternal}; this method only answers "which chashes are
     * already present").
     *
     * <p>Keys on {@code (collection, chash)} exactly like {@link #delete} and
     * {@link #count} — the same {@code PRIMARY KEY (tenant_id, collection, chash)}
     * the ON CONFLICT target uses (RDR-181 Research Findings), so a batch
     * {@code chash = ANY(?)} over a few hundred keys is a millisecond PK-index
     * lookup. No explicit {@code tenant_id} predicate — RLS scopes the visible rows,
     * the same trust boundary {@link #delete} / {@link #count} already rely on.
     *
     * <p>SQL errors propagate as a {@link RuntimeException} (via
     * {@link TenantScope#withTenant}) — the fail-safe (a SELECT error must never be
     * read as "everything exists", which would silently skip embedding a genuinely
     * new chunk) lives one layer up in {@link #selectExistingChashesOrEmpty}, not
     * here.
     *
     * @param tenant     tenant principal for RLS scoping
     * @param collection four-segment conformant collection name (drives dim dispatch)
     * @param chashes    candidate chunk natural IDs to probe for existence
     * @return the subset of {@code chashes} that already have a stored row; empty
     *         (never null) when {@code chashes} is null or empty — no DB round-trip
     *         is made in that case
     */
    public Set<String> selectExistingChashes(String tenant, String collection,
                                              List<String> chashes) {
        if (chashes == null || chashes.isEmpty()) {
            return Set.of();
        }
        int dim = dimForCollection(collection);
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        return tenantScope.withTenant(tenant, ctx -> selectExistingChashesCtx(ctx, ch, collection, chashes));
    }

    /**
     * Ctx-level existence SELECT — the shared SQL shape behind both
     * {@link #selectExistingChashes} (its own short transaction) and
     * {@link #resolveNeedEmbedIdx} (same transaction as that method's have-vector
     * UPDATE loop, RDR-181 bead nexus-f0r8p.2).
     */
    private static Set<String> selectExistingChashesCtx(DSLContext ctx, DimTables.ChunkTable ch,
                                                         String collection, List<String> chashes) {
        return new HashSet<>(ctx.select(ch.chash())
                                 .from(ch.table())
                                 .where(ch.collection().eq(collection).and(ch.chash().in(chashes)))
                                 .fetch(ch.chash()));
    }

    /**
     * Fail-safe wrapper over {@link #selectExistingChashes} (RDR-181): a SELECT
     * error is treated as "nothing exists" — never as "everything exists". The
     * distinction is load-bearing: mistaking an indeterminate result for "present"
     * would silently skip embedding a genuinely new chunk (permanent data loss on
     * the have-vector path); treating an error as "absent" only costs one redundant
     * embed — exactly today's unconditional-embed behavior, never worse.
     *
     * <p><b>Not on the production hot path.</b> {@link #resolveNeedEmbedIdx} (the
     * method {@link #upsertChunksInternal} actually calls) does NOT call this
     * method — it needs the existing chunk's stored TEXT alongside presence (for
     * the content-divergence guard) and both the existence check and the
     * have-vector UPDATE must share one transaction, so it inlines its own
     * SELECT + its own {@code catch (RuntimeException)} fail-safe (returning
     * {@code null}, meaning "embed everything") rather than composing this
     * method's own separate transaction. This method exists as a public,
     * independently-tested existence-only primitive (see
     * {@code PgVectorEmbedSkipTest#selectExistingChashesOrEmpty_selectErrors_failSafeReturnsEmptySet}
     * for its own fail-safe coverage, and
     * {@code PgVectorEmbedSkipIntegrationTest#resolveNeedEmbedIdx_existenceSelectConnectionFails_failSafeEmbedsEverythingAndWrites}
     * for {@code resolveNeedEmbedIdx}'s independent fail-safe branch) for callers
     * that only need presence, not the full embed-skip transaction contract.
     *
     * @return the subset of {@code chashes} known to exist, or an empty set (not a
     *         propagated exception) if the existence check itself failed
     */
    public Set<String> selectExistingChashesOrEmpty(String tenant, String collection,
                                                      List<String> chashes) {
        try {
            return selectExistingChashes(tenant, collection, chashes);
        } catch (RuntimeException e) {
            log.warn("event=existence_select_failed collection={} count={} fail_safe=embed_all err={}",
                collection, chashes == null ? 0 : chashes.size(), e.toString());
            return Set.of();
        }
    }

    /**
     * Result of {@link #partitionByExistence} — indices (not chashes) into the
     * caller's original batch, so a caller can slice its aligned ids/documents/
     * metadatas lists in lockstep (RDR-181).
     */
    public record ExistencePartition(List<Integer> needEmbedIdx, List<Integer> haveVectorIdx) {}

    /**
     * Pure partition of a chash batch into need-embed vs have-vector indices
     * (RDR-181), given the set of chashes {@link #selectExistingChashesOrEmpty}
     * found present. No DB dependency — deliberately kept separate from the
     * (Testcontainers-only) existence SELECT so the partition logic itself has a
     * fast, hermetic unit test.
     *
     * @param chashes the batch's chashes, in order
     * @param present chashes known to already have a stored row (from
     *                {@link #selectExistingChashesOrEmpty})
     * @return the partition: indices into {@code chashes} needing embed vs already
     *         having a stored vector
     */
    public static ExistencePartition partitionByExistence(List<String> chashes, Set<String> present) {
        List<Integer> need = new ArrayList<>();
        List<Integer> have = new ArrayList<>();
        for (int i = 0; i < chashes.size(); i++) {
            if (present.contains(chashes.get(i))) have.add(i); else need.add(i);
        }
        return new ExistencePartition(need, have);
    }

    /**
     * RDR-181 (bead nexus-f0r8p.2): resolve the finalized need-embed indices for
     * {@link #upsertChunksInternal}'s embed-skip path — the existence SELECT AND the
     * have-vector metadata-only UPDATE-with-reroute loop run inside ONE short
     * transaction ({@link TenantScope#withTenant} opens exactly one connection/
     * transaction for this whole call) that commits before this method returns —
     * i.e. strictly before the caller embeds anything (Technical Design, RDR-181
     * lines ~227-261). Deliberately inlines the SELECT and per-chash UPDATE against a
     * single shared {@link DSLContext} rather than composing the public
     * {@link #selectExistingChashes} / {@link #updateMetadata} methods, each of which
     * opens its OWN transaction — the RDR's "steps 1+3 are one short transaction"
     * requirement is literal, not merely "eventually correct under READ COMMITTED".
     *
     * <p>Race-safety: a chash present at the existence SELECT can still be
     * hard-deleted (concurrent orphan-GC pass) before its have-vector UPDATE runs
     * inside this SAME transaction. That UPDATE's affected-row count catches it — 0
     * rows means the chash is gone, so it is added to the returned need-embed list
     * rather than silently dropped. The have-vector branch is self-healing against
     * this race, never lossy.
     *
     * <p>Content-divergence guard (locked contract:
     * {@code PgVectorRepositoryContractTest#upsert_reUpsertSameChash_updatesInPlace}):
     * a chash's presence is NOT by itself sufficient to skip embedding — the caller's
     * incoming text is compared against the currently stored {@code chunk_text}, and
     * only a BYTE-IDENTICAL match takes the metadata-only path. A re-upsert of the
     * same id with different text (Chroma upsert semantics: any id can be
     * re-upserted with new content) must still re-embed and rewrite {@code
     * chunk_text} — RDR-181's skip is an optimization for the common case where
     * chash is genuinely content-derived, not a license to assume it always is;
     * this method never assumes chash-implies-content on the caller's behalf.
     *
     * <p><b>KNOWN, UNRESOLVED-WITH-CERTAINTY RISK (RDR-181 review, bead
     * nexus-f0r8p.2/.4):</b> this method adds one extra connection checkout per
     * {@code upsertChunksInternal} call (this transaction) on top of the
     * catalog-registration transaction and the final INSERT transaction, all
     * drawn from the SAME shared HikariCP pool. During bead .2's implementation,
     * a full {@code mvn test} run showed 6 transient pool-exhaustion 503s in
     * {@code ChashVectorConcurrencyTest} (a zero-5xx-tolerance stress test,
     * POOL_SIZE=6, 12 threads) attributed to this extra checkout; 3 isolated
     * reruns and a subsequent full-suite rerun were clean, and an independent
     * code-review isolation rerun (66s) also did not reproduce it. Neither the
     * one dirty run nor the clean reruns are strong evidence either way for a
     * stress test built specifically to surface this failure class — treat this
     * as an open risk, not a resolved one, when tuning pool size or investigating
     * 503s under load. See T2 memory {@code nexus-f0r8p-pool-exhaustion-known-risk}
     * (project "nexus") for the full writeup and the recommendation to rerun
     * {@code ChashVectorConcurrencyTest} under load before cutting the engine tag.
     *
     * @return the finalized need-embed indices (original absentees, any have-vector
     *         chash whose stored text differs from the incoming text, plus any
     *         have-vector chash whose metadata-only UPDATE affected 0 rows), or
     *         {@code null} if the existence-check transaction itself failed —
     *         fail-safe: the caller must treat {@code null} exactly like "skip the
     *         optimization, embed everything" (today's behavior), never as "nothing
     *         needs embedding"
     */
    private List<Integer> resolveNeedEmbedIdx(String tenant, String collection, int dim,
                                               List<String> dedupIds,
                                               List<String> dedupDocs,
                                               List<Map<String, Object>> dedupMetas) {
        existenceSelectCalls.incrementAndGet();
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        try {
            return tenantScope.withTenant(tenant, ctx -> {
                Map<String, String> existingText = selectExistingChashTextCtx(ctx, ch, collection, dedupIds);
                ExistencePartition partition = partitionByExistence(dedupIds, existingText.keySet());
                // Test-only interleaving seam (bead nexus-f0r8p.4) — see
                // afterExistencePartitionHookForTests javadoc. Fires AFTER the existence
                // SELECT resolves and BEFORE the have-vector UPDATE loop below, inside
                // this SAME still-open transaction, so a test can pause here while a
                // concurrent GC delete commits on another connection. No-op (null) in
                // production.
                Runnable hook = afterExistencePartitionHookForTests;
                if (hook != null) {
                    hook.run();
                }
                // Mutable working copy: ExistencePartition is an immutable record, and
                // the reroute step ("if 0 rows, move that chash into need-embed") is a
                // mutation partition.needEmbedIdx() cannot express directly.
                List<Integer> needEmbedIdx = new ArrayList<>(partition.needEmbedIdx());
                for (int idx : partition.haveVectorIdx()) {
                    String storedText = existingText.get(dedupIds.get(idx));
                    if (!Objects.equals(storedText, dedupDocs.get(idx))) {
                        // Content-divergence guard (see javadoc): the id already
                        // exists but with DIFFERENT text — this is a genuine content
                        // update, not a redundant re-index. Route to need-embed
                        // exactly like an absent chash; no metadata-only UPDATE is
                        // issued here (the insert path below rewrites chunk_text,
                        // embedding, AND metadata together).
                        needEmbedIdx.add(idx);
                        continue;
                    }
                    int affected = updateMetadataOneRow(ctx, ch, collection,
                            dedupIds.get(idx), dedupMetas.get(idx));
                    if (affected == 0) {
                        needEmbedIdx.add(idx);
                    }
                }
                return needEmbedIdx;
            });
        } catch (RuntimeException e) {
            log.warn("event=existence_partition_failed collection={} count={} fail_safe=embed_all err={}",
                    collection, dedupIds.size(), e.toString());
            return null;
        }
    }

    /**
     * Ctx-level existence + stored-text lookup — like {@link #selectExistingChashesCtx}
     * but also returns each present chash's currently stored {@code chunk_text}, so
     * {@link #resolveNeedEmbedIdx} can detect the content-divergence case (same id,
     * different text) without a second round trip.
     */
    private static Map<String, String> selectExistingChashTextCtx(DSLContext ctx, DimTables.ChunkTable ch,
                                                                    String collection, List<String> chashes) {
        Map<String, String> out = new HashMap<>();
        ctx.select(ch.chash(), ch.chunkText())
           .from(ch.table())
           .where(ch.collection().eq(collection).and(ch.chash().in(chashes)))
           .fetch()
           .forEach(r -> out.put(r.value1(), r.value2()));
        return out;
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
            Integer doc = ctx.select(DSL.one()).from(CATALOG_DOCUMENTS)
                             .where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
                             .fetchOne(0, Integer.class);
            if (doc == null) {
                throw new IllegalStateException(
                    "tumbler '" + tumbler + "' does not resolve to a visible catalog document");
            }

            // 2. Manifest rows in position order.
            var manifest = ctx.select(CATALOG_DOCUMENT_CHUNKS.POSITION,
                                      ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH),
                                      CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                              .from(CATALOG_DOCUMENT_CHUNKS)
                              .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(tumbler))
                              .orderBy(CATALOG_DOCUMENT_CHUNKS.POSITION.asc())
                              .fetch();
            if (manifest.isEmpty()) {
                return List.of();
            }

            // 3. Resolve chunk text per collection group (each collection dispatches to
            //    its own chunks_<dim> table).
            Map<String, Set<String>> chashesByCollection = new LinkedHashMap<>();
            for (var m : manifest) {
                String col = m.value3();
                if (col == null || col.isBlank()) {
                    throw new IllegalStateException(
                        "manifest row for doc '" + tumbler + "' position "
                        + m.value1()
                        + " has no collection - cannot dispatch to a chunks_<dim> table"
                        + " (pre-migration manifest rows are resolved by the Phase 5 ETL)");
                }
                chashesByCollection.computeIfAbsent(col, k -> new LinkedHashSet<>())
                                   .add(m.value2());
            }

            Map<String, Map<String, String>> textByColThenChash = new HashMap<>();
            for (Map.Entry<String, Set<String>> e : chashesByCollection.entrySet()) {
                String col = e.getKey();
                int dim = dimForCollection(col);
                DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
                var chunks = ctx.select(ch.chash(), ch.chunkText()).from(ch.table())
                                .where(ch.collection().eq(col).and(ch.chash().in(e.getValue())))
                                .fetch();
                Map<String, String> byChash =
                    textByColThenChash.computeIfAbsent(col, k -> new HashMap<>());
                for (var c : chunks) {
                    byChash.put(c.value1(), c.value2());
                }
            }

            // 4. Walk the manifest in position order; any unresolved (collection, chash)
            //    fails loud - never a silently partial document.
            List<Map<String, Object>> rows = new ArrayList<>(manifest.size());
            for (var m : manifest) {
                Integer position = m.value1();
                String chash     = m.value2();
                String col       = m.value3();
                Map<String, String> byChash = textByColThenChash.get(col);
                String text  = byChash != null ? byChash.get(chash) : null;
                if (text == null) {
                    throw new IllegalStateException(
                        "manifest row for doc '" + tumbler + "' position "
                        + position + " references (" + col + ", "
                        + chash + ") which has no chunk row - refusing to return a"
                        + " partial document (application-enforced referential check)");
                }
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("position",   position);
                row.put("chash",      chash);
                row.put("chunk_text", text);
                row.put("collection", col);
                rows.add(row);
            }
            return rows;
        });
    }

    /**
     * Fetch the {@code chunk_text} for a single {@code (tenant, collection, chash)}
     * triple — targeted single-row lookup for the RDR-169 G3 URI-resolver path.
     *
     * <p>Returns {@code null} when:
     * <ul>
     *   <li>the row does not exist (missing chunk), or</li>
     *   <li>{@code chunk_text} is {@code NULL} (reference-only retention, RDR-169 G1).</li>
     * </ul>
     *
     * <p>Callers must distinguish "missing row" from "reference-only" at the
     * resolver level if needed; this method treats both as "no stored text."
     * RLS is enforced via {@link dev.nexus.service.db.TenantScope#withTenant} —
     * cross-tenant rows are invisible.
     *
     * @param tenant     the requesting tenant principal
     * @param collection four-segment conformant collection name (drives dim dispatch)
     * @param chash      the chunk's natural ID (the full sha256 hexdigest, RDR-180)
     * @return the stored {@code chunk_text}, or {@code null} if none
     */
    public String fetchChunkText(String tenant, String collection, String chash) {
        int dim = dimForCollection(collection);
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(ch.chunkText()).from(ch.table())
               .where(ch.collection().eq(collection).and(ch.chash().eq(chash)))
               .fetchOne(ch.chunkText()));
    }

    // -- Internal helpers -------------------------------------------------------

    private static String chunksTable(int dim) {
        return "nexus.chunks_" + dim;
    }

    /**
     * SANCTIONED RAW (nexus-mzuj9): the single execution chokepoint for every genuinely
     * raw-SQL fetch left in this class after the fetch-side jOOQ conversion sweep. Callers
     * ({@link #searchWithTokens} and {@link #hybridSearch}) build the SQL text because it
     * combines things jOOQ's typed DSL cannot express together in one statement: the
     * pgvector {@code <=>} distance operator ordering directly off a bind-parameter vector
     * literal, the {@code pg_trgm} {@code <%} similarity operator, a per-call dynamic-arity
     * {@code WHERE} (an arbitrary count of caller-supplied metadata predicates plus an
     * IN-list sized to the collection/chash set), and — for {@code hybridSearch} — a
     * selectivity-dependent PLAN CHOICE (materialize-then-rank vs. HNSW-first) made in Java
     * between two structurally different follow-up queries. Rewriting this as nested DSL
     * would either lose the operator-level control the selectivity dispatch depends on or
     * require a bespoke dynamic-condition builder whose behavior would need to be
     * re-verified bit-for-bit against the existing (well-tested) selectivity contract —
     * not a safe mechanical transform. Registered in {@code RawSqlGateTest}'s sanctioned
     * method allowlist; this is the ONLY place in the class where a caller-built raw SQL
     * string reaches {@code ctx.fetch}.
     */
    private static Result<Record> rawVectorFetch(DSLContext ctx, String sql, Object... binds) {
        return ctx.fetch(sql, binds);
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

    /** RDR-180: hex-string binds against the bytea chash column. */
    private static String decodePlaceholders(int n) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i++) {
            if (i > 0) sb.append(",");
            sb.append("decode(?, 'hex')");
        }
        return sb.toString();
    }

    /**
     * Translate one metadata {@code where} entry into a SQL predicate appended to
     * {@code sql}, with its binds added to {@code binds} in placeholder order
     * (nexus-05bfd, consumer-driven from conexus RDR-001).
     *
     * <p>Supports plain equality (a scalar value) and the common Chroma
     * operator-form on a single field, where the value is a one-entry map:
     * <ul>
     *   <li>{@code {"k": "v"}}            → {@code metadata->>'k' = 'v'}</li>
     *   <li>{@code {"k": {"$eq": "v"}}}   → {@code metadata->>'k' = 'v'}</li>
     *   <li>{@code {"k": {"$ne": "v"}}}   → {@code metadata->>'k' IS DISTINCT FROM 'v'}</li>
     *   <li>{@code {"k": {"$in":  [...]}}} → {@code metadata->>'k' IN (...)}</li>
     *   <li>{@code {"k": {"$nin": [...]}}} → {@code (metadata->>'k' IS NULL OR metadata->>'k' NOT IN (...))}</li>
     *   <li>{@code {"k": {"$gte"|"$lte"|"$gt"|"$lt": n}}} (numeric operand) →
     *       {@code jsonb_typeof(metadata->'k') = 'number' AND (metadata->>'k')::numeric ≥ n}
     *       — operand-typed (nexus-4l80g): only JSON-number metadata values participate;
     *       non-numeric rows are excluded, never a cast error.</li>
     *   <li>{@code {"k": {"$gte"|...: "s"}}} (string operand) → lexical text compare
     *       ({@code '9' > '10'}); pass numbers to compare numerically.</li>
     * </ul>
     *
     * <p>{@code $ne}/{@code $nin} use {@code IS DISTINCT FROM} / {@code IS NULL OR}
     * semantics so a row whose metadata key is ABSENT is KEPT. This is NULL-inclusive
     * and intentionally chosen for the noise-dropping use case (e.g. drop only the rows
     * explicitly tagged {@code section_type = references}, keep everything else including
     * untagged chunks). Note this DIFFERS from Chroma local-mode, whose DuckDB filter
     * treats {@code field != value} as NULL-exclusive ({@code NULL != 'x'} is false, so
     * absent-key rows are dropped). The divergence is moot for the driving consumer:
     * {@code section_type} is schema-defaulted to {@code ""} (never absent) on indexed
     * chunks, so both engines keep the same rows in practice.
     *
     * <p>Unsupported shapes fail loud with {@link IllegalArgumentException} (mapped
     * to HTTP 400 by {@code VectorHandler}) rather than silently matching nothing:
     * a {@code $}-prefixed FIELD key (compound {@code $and}/{@code $or}), an operator
     * map with more than one operator, a non-list operand for {@code $in}/{@code $nin},
     * or an unrecognized operator. Before nexus-05bfd an operator-form value was
     * {@code String.valueOf(map)}-bound and matched no rows with no error.
     */
    /** NaN/Infinity cannot come from JSON parsing, but a programmatic caller
     * could pass them — rewrap so the handler's 400 ladder catches it, never
     * an unhandled NumberFormatException 500 (review c0e4493e finding 6). */
    private static java.math.BigDecimal toBigDecimalOperand(String operator, String key, Number n) {
        try {
            return new java.math.BigDecimal(n.toString());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                operator + " for field '" + key + "' got a non-finite numeric operand: " + n);
        }
    }

    static void appendWherePredicate(StringBuilder sql, List<Object> binds,
                                     String key, Object value) {
        if (key.startsWith("$")) {
            throw new IllegalArgumentException(
                "compound where operator '" + key + "' is not supported on the vector "
                + "bridge; express each field as its own predicate (all fields are ANDed)");
        }
        if (!(value instanceof Map<?, ?> ops)) {
            sql.append(" AND metadata->>? = ?");
            binds.add(key);
            binds.add(String.valueOf(value));
            return;
        }
        if (ops.size() != 1) {
            throw new IllegalArgumentException(
                "where operator map for field '" + key + "' must hold exactly one operator, got "
                + ops.keySet());
        }
        Map.Entry<?, ?> op = ops.entrySet().iterator().next();
        String operator = String.valueOf(op.getKey());
        Object operand = op.getValue();
        switch (operator) {
            case "$eq" -> {
                sql.append(" AND metadata->>? = ?");
                binds.add(key);
                binds.add(String.valueOf(operand));
            }
            case "$ne" -> {
                sql.append(" AND metadata->>? IS DISTINCT FROM ?");
                binds.add(key);
                binds.add(String.valueOf(operand));
            }
            case "$in", "$nin" -> {
                if (!(operand instanceof List<?> items)) {
                    throw new IllegalArgumentException(
                        operator + " for field '" + key + "' expects a list operand, got "
                        + (operand == null ? "null" : operand.getClass().getSimpleName()));
                }
                if (items.isEmpty()) {
                    // $in [] matches nothing; $nin [] excludes nothing.
                    sql.append("$in".equals(operator) ? " AND FALSE" : " AND TRUE");
                    return;
                }
                String ph = placeholders(items.size());
                if ("$in".equals(operator)) {
                    sql.append(" AND metadata->>? IN (").append(ph).append(")");
                    binds.add(key);
                } else {
                    sql.append(" AND (metadata->>? IS NULL OR metadata->>? NOT IN (")
                       .append(ph).append("))");
                    binds.add(key);
                    binds.add(key);
                }
                for (Object it : items) binds.add(String.valueOf(it));
            }
            case "$gte", "$lte", "$gt", "$lt" -> {
                // nexus-4l80g: range operators, operand-TYPED (mirrors Chroma's
                // client contract). Numeric operand -> numeric compare, guarded
                // by jsonb_typeof so a non-numeric metadata value on some row
                // simply doesn't match instead of aborting the whole query on
                // the ::numeric cast (absent key -> NULL typeof -> no match).
                // A JSON-string "2020" does NOT match a numeric operand.
                // String operand -> lexical text compare (the documented
                // '9' > '10' hazard; callers comparing numbers pass numbers —
                // the nx CLI parses unquoted numerics into JSON numbers).
                String cmp = switch (operator) {
                    case "$gte" -> ">=";
                    case "$lte" -> "<=";
                    case "$gt" -> ">";
                    default -> "<";
                };
                if (operand instanceof Number n) {
                    sql.append(" AND jsonb_typeof(metadata->?) = 'number'")
                       .append(" AND (metadata->>?)::numeric ").append(cmp).append(" ?");
                    binds.add(key);
                    binds.add(key);
                    binds.add(toBigDecimalOperand(operator, key, n));
                } else if (operand instanceof String str) {
                    sql.append(" AND metadata->>? ").append(cmp).append(" ?");
                    binds.add(key);
                    binds.add(str);
                } else {
                    throw new IllegalArgumentException(
                        operator + " for field '" + key + "' expects a numeric or string "
                        + "operand, got "
                        + (operand == null ? "null" : operand.getClass().getSimpleName()));
                }
            }
            default -> throw new IllegalArgumentException(
                "unsupported where operator '" + operator + "' for field '" + key
                + "'; supported: $eq, $ne, $in, $nin, $gte, $lte, $gt, $lt");
        }
    }

    /**
     * Typed jOOQ sibling of {@link #appendWherePredicate} (nexus-mzuj9): same Chroma
     * operator-subset ({@code $eq}/{@code $ne}/{@code $in}/{@code $nin}, plain-scalar
     * shorthand for {@code $eq}), expressed as a {@link org.jooq.Condition} via a
     * {@code metadata ->> {0}} DSL.field template (bind placeholder, not string
     * concatenation) instead of hand-built SQL text. Used by the plain-equality
     * {@link #getWhere} path, which has no vector/trigram operator to entangle with.
     */
    static org.jooq.Condition metadataCondition(String key, Object value) {
        if (key.startsWith("$")) {
            throw new IllegalArgumentException(
                "compound where operator '" + key + "' is not supported on the vector "
                + "bridge; express each field as its own predicate (all fields are ANDed)");
        }
        org.jooq.Field<String> mv = DSL.field("metadata ->> {0}", String.class, key);
        if (!(value instanceof Map<?, ?> ops)) {
            return mv.eq(String.valueOf(value));
        }
        if (ops.size() != 1) {
            throw new IllegalArgumentException(
                "where operator map for field '" + key + "' must hold exactly one operator, got "
                + ops.keySet());
        }
        Map.Entry<?, ?> op = ops.entrySet().iterator().next();
        String operator = String.valueOf(op.getKey());
        Object operand = op.getValue();
        return switch (operator) {
            case "$eq" -> mv.eq(String.valueOf(operand));
            case "$ne" -> mv.isDistinctFrom(String.valueOf(operand));
            case "$in", "$nin" -> {
                if (!(operand instanceof List<?> items)) {
                    throw new IllegalArgumentException(
                        operator + " for field '" + key + "' expects a list operand, got "
                        + (operand == null ? "null" : operand.getClass().getSimpleName()));
                }
                if (items.isEmpty()) {
                    // $in [] matches nothing; $nin [] excludes nothing.
                    yield "$in".equals(operator) ? DSL.falseCondition() : DSL.trueCondition();
                }
                List<String> strItems = items.stream().map(String::valueOf).toList();
                yield "$in".equals(operator)
                    ? mv.in(strItems)
                    : mv.isNull().or(mv.notIn(strItems));
            }
            case "$gte", "$lte", "$gt", "$lt" -> {
                // nexus-4l80g twin of appendWherePredicate's range case — the
                // two translators must not drift (same operand-typed semantics).
                String cmp = switch (operator) {
                    case "$gte" -> ">=";
                    case "$lte" -> "<=";
                    case "$gt" -> ">";
                    default -> "<";
                };
                if (operand instanceof Number n) {
                    yield DSL.condition(
                        "jsonb_typeof(metadata->{0}) = 'number' AND (metadata->>{0})::numeric "
                        + cmp + " {1}",
                        DSL.val(key), DSL.val(toBigDecimalOperand(operator, key, n)));
                } else if (operand instanceof String str) {
                    yield DSL.condition("metadata->>{0} " + cmp + " {1}",
                        DSL.val(key), DSL.val(str));
                } else {
                    throw new IllegalArgumentException(
                        operator + " for field '" + key + "' expects a numeric or string "
                        + "operand, got "
                        + (operand == null ? "null" : operand.getClass().getSimpleName()));
                }
            }
            default -> throw new IllegalArgumentException(
                "unsupported where operator '" + operator + "' for field '" + key
                + "'; supported: $eq, $ne, $in, $nin, $gte, $lte, $gt, $lt");
        };
    }

    private static String toJson(Map<String, Object> metadata) {
        Map<String, Object> m = metadata != null ? metadata : Map.of();
        try {
            return MAPPER.writeValueAsString(m);
        } catch (Exception e) {
            throw new IllegalArgumentException("metadata is not JSON-serializable: " + m, e);
        }
    }

    // ── RDR-169 G5: address-triple enrichment (chash / source_uri / span) ────────

    /**
     * Fetch {@code source_uri} per chash in one round-trip (RDR-169 G5, bead nexus-jkv85).
     *
     * <p>Joins {@code catalog_document_chunks} to {@code catalog_documents} to resolve
     * the document's {@code source_uri} from each chunk's {@code chash}. Runs under the
     * tenant's RLS so cross-tenant leaks are impossible. Returns a map from chash to
     * source_uri (null entry when the chash has no catalog document row).
     *
     * <p>Runs as a SINGLE IN-clause query — not N+1. Chunks with no catalog entry
     * map to null; the caller emits null in the response (graceful, never 500).
     *
     * @param tenant  the tenant whose catalog rows to query (RLS-scoped)
     * @param chashes the set of chunk chashes to resolve
     * @return map from chash → source_uri (null value when not in catalog)
     */
    private Map<String, String> sourceUrisByChash(String tenant, Set<String> chashes) {
        sourceUriJoinCalls.incrementAndGet();
        if (chashes.isEmpty()) return Map.of();

        // Batch into ≤300 chashes per IN-clause (SOURCE_URI_JOIN_BATCH)
        // so large get-envelope id lists never overflow the bind-param budget.
        List<String> chashList = new ArrayList<>(chashes);
        Map<String, String> out = new HashMap<>(chashList.size() * 2);
        int batchSize = SOURCE_URI_JOIN_BATCH;

        for (int start = 0; start < chashList.size(); start += batchSize) {
            List<String> batch = chashList.subList(start, Math.min(start + batchSize, chashList.size()));

            // d.tenant_id = ? guards against cross-tenant source_uri leak when two tenants
            // share a tumbler string. FORCE RLS scopes the chunks table; the explicit bind
            // adds defense-in-depth on the catalog_documents side (fix #H2).
            // Note: last-writer-wins when a chash appears in multiple catalog_document_chunks
            // rows (shared chunk text across documents) — non-determinism accepted for now.
            var result = tenantScope.withTenant(tenant, ctx ->
                ctx.select(ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH), CATALOG_DOCUMENTS.SOURCE_URI)
                   .from(CATALOG_DOCUMENT_CHUNKS)
                   .join(CATALOG_DOCUMENTS)
                   .on(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID)
                       .and(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)))
                   .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                       .and(ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH).in(batch)))
                   .fetch());

            for (var rec : result) {
                String chash     = rec.value1();
                String sourceUri = rec.value2();
                if (chash != null) out.put(chash, sourceUri);
            }
        }
        return out;
    }

    /** Precompiled hex pattern for chunk_text_hash validation (fix #M1-cr: length+charset). */
    private static final Pattern HEX64 = Pattern.compile("[0-9a-f]{64}");

    /** Legacy pre-RDR-180 chunk id shape: 32 lowercase hex (the [:32] truncation era). */
    private static final Pattern HEX32 = Pattern.compile("[0-9a-f]{32}");

    /**
     * True when {@code s} is a legacy 32-lowercase-hex chunk id — the shape every
     * pre-RDR-180 row carries (as a 16-byte key post-conversion) until the
     * chash-rekey rung runs. Read paths tolerate this width in the auto-converge
     * window; WRITE boundaries reject it ({@link dev.nexus.service.db.Chash}).
     */
    private static boolean isLegacyHex32(String s) {
        return s != null && HEX32.matcher(s).matches();
    }

    /**
     * Compute a span string from chunk metadata (RDR-169 G5, bead nexus-jkv85).
     *
     * <p>Span priority (highest-fidelity first):
     * <ol>
     *   <li>{@code chash:<chunk_text_hash>} — full 64-char lowercase-hex sha256 from stored
     *       metadata (matches the {@code _SPAN_PATTERN} {@code chash:} form used by catalog
     *       links). A non-hex 64-char value falls through to the next priority rather than
     *       emitting a malformed span.
     *   <li>{@code line_start-line_end} — line range when both fields are present and
     *       {@code line_end > 0}.
     *   <li>{@code ""} — whole-document span when no positional data is available.
     * </ol>
     *
     * @param meta the chunk's metadata map (already deserialized from JSONB)
     * @return span string matching the Python {@code _SPAN_PATTERN}; never null
     */
    static String spanFromMeta(Map<String, Object> meta) {
        // Priority 1: chash:<full_sha256> from chunk_text_hash metadata field.
        // Length AND hex-charset check: a 64-char non-hex value must not emit a malformed span.
        Object cth = meta.get("chunk_text_hash");
        if (cth instanceof String s && HEX64.matcher(s).matches()) {
            return "chash:" + s;
        }
        // Priority 2: line_start-line_end (both must be present; line_end > 0 = positioned)
        Object ls = meta.get("line_start");
        Object le = meta.get("line_end");
        if (ls instanceof Number lsn && le instanceof Number len) {
            int lineEnd = len.intValue();
            if (lineEnd > 0) {
                return lsn.intValue() + "-" + lineEnd;
            }
        }
        // Fallback: whole-document (empty span)
        return "";
    }

    /**
     * Enrich CHUNK-LEVEL search result rows with the address triple (RDR-169 G5, bead nexus-jkv85).
     *
     * <p><strong>Chunk-level callers only.</strong> {@code row.get("id")} IS the chash — this
     * is the invariant for {@link #searchWithTokens} and {@link #hybridSearchWithTokens} rows.
     * Document-level paths ({@code searchMetadataScoped}, {@code searchGraphHop}) return tumblers
     * as id and MUST NOT call this method.
     *
     * <p>Always adds {@code chash} and {@code span} to each row in-place (free — no I/O).
     * Adds {@code source_uri} only when {@code includeSourceUri} is {@code true} (opt-in,
     * default false — gates the catalog JOIN so the default path pays zero extra I/O).
     *
     * <p>Field-presence contract:
     * <ul>
     *   <li>{@code includeSourceUri=false}: {@code source_uri} is ABSENT from each row
     *       (byte-identical wire shape to callers that never set the flag).
     *   <li>{@code includeSourceUri=true}: {@code source_uri} present; value is the URI
     *       string when a catalog row exists, {@code null} when not.
     * </ul>
     *
     * @param tenant           the tenant for the catalog JOIN (RLS-scoped)
     * @param rows             the chunk-level search result rows to enrich (mutated in-place)
     * @param includeSourceUri when true, resolves source_uri via a catalog JOIN
     */
    private void enrichSearchRows(String tenant, List<Map<String, Object>> rows,
                                  boolean includeSourceUri) {
        if (rows.isEmpty()) return;
        Map<String, String> uriMap = Map.of();
        if (includeSourceUri) {
            Set<String> chashes = new LinkedHashSet<>(rows.size() * 2);
            for (Map<String, Object> row : rows) {
                Object id = row.get("id");
                if (id instanceof String s) chashes.add(s);
            }
            uriMap = sourceUrisByChash(tenant, chashes);
        }
        for (Map<String, Object> row : rows) {
            Object idVal = row.get("id");
            // Fail loud if id is not chash-shaped (RDR-180) — document-level
            // callers (tumbler ids) must not reach here. LEGACY 32-hex ids
            // are chunk rows too (nexus-p78a0 rehearsal catch, run 3): in the
            // auto-converge window — cohort engine booted, chash-rekey rung
            // not yet run — every pre-existing row still carries its 16-byte
            // legacy key, and a hard 64-only guard here 422'd EVERY search of
            // pre-existing content, making an un-rekeyed store unreadable.
            // The window contract is degrade-per-row, never fail-the-read:
            // serve the row (chash = the legacy hex; the client's dual-width
            // read seam resolves it via the alias route once rekeyed); the
            // span still derives from metadata chunk_text_hash, which has
            // carried the FULL 64-hex in every era.
            if (!(idVal instanceof String chashStr)
                    || !(chashStr.length() == 64 || isLegacyHex32(chashStr))) {
                throw new IllegalStateException(
                    "enrichSearchRows: id '" + idVal + "' is not a 64-hex chash "
                    + "(or a legacy 32-hex window row) — only chunk-level search "
                    + "rows may be enriched");
            }
            row.put("chash", chashStr);
            if (includeSourceUri) {
                row.put("source_uri", uriMap.get(chashStr));
            }
            // spanFromMeta reads chunk_text_hash / line_start / line_end from the FLATTENED row:
            // search rows include metadata keys at the top level (via row.putAll(fromJson(...))).
            row.put("span", spanFromMeta(row));
        }
    }

    /**
     * Enrich a {@code {ids, documents, metadatas}} get-envelope with address triple parallel
     * lists (RDR-169 G5, bead nexus-jkv85).
     *
     * <p>Always adds {@code chashes} and {@code spans} as parallel lists aligned with
     * {@code ids}. The existing three keys are UNTOUCHED. Adds {@code source_uris} only when
     * {@code includeSourceUri} is {@code true} (gates the catalog JOIN).
     *
     * <p>Field-presence contract:
     * <ul>
     *   <li>{@code includeSourceUri=false}: {@code source_uris} is ABSENT from the envelope.
     *   <li>{@code includeSourceUri=true}: {@code source_uris} present and parallel with ids;
     *       null entries where no catalog row exists.
     * </ul>
     *
     * @param tenant           the tenant for the catalog JOIN (RLS-scoped)
     * @param envelope         the raw get envelope (must contain {@code ids} and {@code metadatas})
     * @param includeSourceUri when true, resolves source_uris via a catalog JOIN
     * @return a new map containing all existing keys plus the new parallel lists
     */
    @SuppressWarnings("unchecked")
    private Map<String, Object> enrichGetEnvelope(String tenant, Map<String, Object> envelope,
                                                   boolean includeSourceUri) {
        Object idsRaw = envelope.get("ids");
        if (!(idsRaw instanceof List<?>)) return envelope;
        List<String> ids = (List<String>) idsRaw;
        if (ids.isEmpty()) return envelope;

        // Defensive cast: bail gracefully if metadatas is not the expected shape
        Object metasRaw = envelope.get("metadatas");
        List<Map<String, Object>> metas = (metasRaw instanceof List<?>) ? (List<Map<String, Object>>) metasRaw : null;

        Map<String, String> uriMap = Map.of();
        if (includeSourceUri) {
            uriMap = sourceUrisByChash(tenant, new LinkedHashSet<>(ids));
        }

        List<String> chashes   = new ArrayList<>(ids.size());
        List<String> sourceUris = includeSourceUri ? new ArrayList<>(ids.size()) : null;
        List<String> spans     = new ArrayList<>(ids.size());

        for (int i = 0; i < ids.size(); i++) {
            String chash = ids.get(i);
            chashes.add(chash);
            if (includeSourceUri) {
                sourceUris.add(chash != null ? uriMap.get(chash) : null);
            }
            Map<String, Object> meta = (metas != null && i < metas.size()) ? metas.get(i) : Map.of();
            spans.add(spanFromMeta(meta != null ? meta : Map.of()));
        }

        Map<String, Object> out = new LinkedHashMap<>(envelope);
        out.put("chashes", chashes);
        if (includeSourceUri) {
            out.put("source_uris", sourceUris);
        }
        out.put("spans", spans);
        return out;
    }

    // ── JSON / SQL helpers ────────────────────────────────────────────────────

    private static Map<String, Object> fromJson(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return MAPPER.readValue(json, MAP_TYPE);
        } catch (Exception e) {
            throw new IllegalStateException("stored metadata is not valid JSON: " + json, e);
        }
    }
}
