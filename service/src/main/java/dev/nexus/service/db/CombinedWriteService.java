// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.SuppliedVectorMismatchActivity;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;


/**
 * nexus-kl2z6 increment 1 — the orchestration seam T2 {@code
 * design-kl2z6-combined-write} §1.3 names: sits in front of {@link
 * CatalogRepository#writeManifestMany(String, List, String, Map, boolean,
 * Map)}, owns the ONE dependency that repository does not have
 * (embedding), and hands it fully-resolved {@code (chash, text, vector,
 * metadata)} tuples so every per-doc transaction only ever WRITES —
 * embedding never runs inside a manifest transaction (design memo §0).
 *
 * <p>Three phases, run in order, exactly once per call:
 * <ol>
 *   <li><b>Dedupe</b> the top-level {@code chunks} payload by chash
 *       (first occurrence wins — matches {@code
 *       PgVectorRepository.upsertChunksInternal}'s {@code Set<String>
 *       seen} discipline: a chash shared by two docs in the same flush is
 *       transmitted once).</li>
 *   <li><b>Existence-partition + embed</b> (RDR-181, design memo §0 hard
 *       requirement: "known chashes never re-embedded"). A short,
 *       independently-committed transaction reads which deduped chashes
 *       already carry IDENTICAL stored text in {@code chunks_<dim>} — those
 *       are skipped; every other chash (new, or content-divergent) is
 *       embedded via the SAME {@link EmbedderRouter} {@code
 *       PgVectorRepository} uses, in ONE batch call, STRICTLY OUTSIDE any
 *       transaction (this phase's own existence-check transaction has
 *       already committed by the time the embedder is invoked, and no
 *       per-doc manifest transaction has opened yet).</li>
 *   <li><b>Dispatch</b> the resolved {@code chash -> ResolvedChunk} map,
 *       unchanged, to {@code CatalogRepository.writeManifestMany}'s 6-arg
 *       (collection-first) overload — which is where every per-doc
 *       transaction, and thus
 *       every actual WRITE, happens.</li>
 * </ol>
 *
 * <p>A chash present in the request's {@code chunks} array but resolved
 * as "already have identical text" is OMITTED from the returned
 * {@code resolved} map (no embedding/text/vector write) but, since
 * nexus-4jj40 round 5, gets its own metadata refreshed via a direct
 * {@link PgVectorRepository#batchUpdateMetadata} call BEFORE the embed
 * phase runs -- a caller whose only change since the last index is
 * chunk metadata (byte-identical text) still lands. {@link
 * CatalogRepository}'s per-doc chunk-upsert only writes chashes present in
 * the map, treating everything else as "must already exist" (verified
 * in-transaction, per doc, at the point a manifest actually references it,
 * true by construction since this method's own existence-partition already
 * confirmed it moments earlier).
 *
 * <p><b>Observability (nexus-acvi7, T2 {@code
 * engine-embed-path-hardening-design-v0.1.70} §2.5(a)):</b> the
 * existence-partition emits one {@code event=combined_write_embed_partition}
 * INFO line per call (collection, deduped/skipped/embedded counts,
 * force_re_embed) after the partition and before the embed call, and the
 * same three counts ({@code chunks_deduped}/{@code embed_skipped}/{@code
 * embed_embedded}) are merged into the response envelope alongside {@code
 * chunks_written}. Without this the RDR-181 "known chashes never
 * re-embedded" guarantee was completely unobservable — nothing could tell
 * a silently-broken skip (every warm reindex re-embedding unchanged
 * content) from a working one.
 */
public final class CombinedWriteService {

    private static final Logger log = LoggerFactory.getLogger(CombinedWriteService.class);

    private final TenantScope    tenantScope;
    private final CatalogRepository catalogRepo;
    private final EmbedderRouter docRouter;

    public CombinedWriteService(TenantScope tenantScope, CatalogRepository catalogRepo,
                                 EmbedderRouter docRouter) {
        this.tenantScope = tenantScope;
        this.catalogRepo = catalogRepo;
        this.docRouter   = docRouter;
    }

    /**
     * Test-only interleaving seam (RDR-222 Phase 0, bead nexus-ulrjq), mirroring
     * {@link PgVectorRepository#setAfterNeedEmbedResolvedHookForTests}: an optional
     * callback invoked in {@link #writeManyCombined} immediately after Phase 2a's
     * existence-partition transaction has committed (so {@code needEmbedIdx} is
     * finalized) and BEFORE the embed call — the exact window a concurrent second
     * writer can commit one of THIS call's originally-absent chashes, producing a
     * raced embed once this call's own per-doc INSERT runs; also the window a test
     * can recreate a chash Phase 2a rerouted via the zero-row-UPDATE path (see
     * {@link #afterExistencePartitionHookForTests} below) so its per-doc INSERT
     * hits a REAL {@code ON CONFLICT}. Default {@code null} (no-op); never read or
     * written by production code.
     */
    private volatile Runnable afterNeedEmbedResolvedHookForTests;

    /** Test-only: install (or clear with {@code null}) the interleaving hook above. */
    public void setAfterNeedEmbedResolvedHookForTests(Runnable hook) {
        this.afterNeedEmbedResolvedHookForTests = hook;
    }

    /**
     * Test-only interleaving seam (RDR-222 Phase 0 fix round, bead nexus-ulrjq,
     * finding 2), mirroring {@link PgVectorRepository#setAfterExistencePartitionHookForTests}
     * exactly: an optional callback invoked INSIDE Phase 2a's existence-partition
     * transaction, immediately after the existence SELECT resolves (so {@code
     * originalAbsentIdx}/{@code need}/{@code metadataOnly} are computed) and BEFORE
     * {@code batchUpdateMetadata}'s have-vector UPDATE runs — the window a
     * concurrent delete of a {@code metadataOnly} chash makes that UPDATE affect 0
     * rows, self-healing it into {@code need} via the zero-row reroute (see
     * PgVectorRepository.NeedEmbedResolution's javadoc for why that reroute is
     * excluded from the raced-embed count). Default {@code null} (no-op); never
     * read or written by production code.
     */
    private volatile Runnable afterExistencePartitionHookForTests;

    /** Test-only: install (or clear with {@code null}) the interleaving hook above. */
    public void setAfterExistencePartitionHookForTests(Runnable hook) {
        this.afterExistencePartitionHookForTests = hook;
    }

    /**
     * RDR-204 Phase 1 (bead nexus-ft04v.6) — the LAZY per-tenant,
     * per-content-type {@code nexus.embedding_profile} seed, called from
     * {@code CatalogHandler.handleCollectionUpsert} right after a collection
     * registration succeeds. This IS "first registration for a content type"
     * for a cloud tenant: the engine has no tenant-mint route (tenants exist
     * through data-token mint at the edge), so a real client registration
     * request is the only seam available. Delegates to {@link
     * EmbedderRouter#seedEmbeddingProfileForContentType} — idempotent (a real
     * upsert, safe to call on every registration for that content type, not
     * only the first) and never touches the {@code catalog_collections} row
     * {@link CatalogRepository#upsertCollection} just wrote.
     *
     * @param tenant      tenant principal for RLS scoping
     * @param contentType the collection's content type (blank/null is a
     *                    no-op — nothing to seed a profile row against)
     */
    public void seedEmbeddingProfileForContentType(String tenant, String contentType) {
        if (contentType == null || contentType.isBlank()) return;
        docRouter.seedEmbeddingProfileForContentType(tenantScope, tenant, contentType);
    }

    /** Embed-phase token usage plus the underlying {@code writeManifestMany} response. */
    public record CombinedWriteResult(Map<String, Object> response, long tokens) {}

    /**
     * How a combined route writes the metadata of a chunk whose chash is already stored
     * (RDR-223, bead nexus-z0o2p.13). {@link #REPLACE} (the default, and every request that
     * names neither field) replaces the stored metadata with the incoming metadata. With {@code
     * merge}, stored metadata becomes {@code (stored - deleteKeys) || incoming}: the semantics of
     * {@code /v1/vectors/upsert-chunks}, so a writer that owns only some keys of a chunk (an
     * indexer re-writing content keys, leaving the {@code bib_*} enrichment another writer set)
     * does not wipe the rest. The keys the caller names in {@code deleteKeys} are the ones it
     * owns and dropped from this write. A chunk that is not stored yet takes the incoming
     * metadata as is under either mode.
     */
    public record MetadataMode(boolean merge, List<String> deleteKeys) {
        public static final MetadataMode REPLACE = new MetadataMode(false, List.of());

        public MetadataMode {
            deleteKeys = deleteKeys == null ? List.of() : List.copyOf(deleteKeys);
            if (!merge && !deleteKeys.isEmpty()) {
                throw new IllegalArgumentException(
                    "'metadata_delete_keys' requires 'metadata_merge': true");
            }
        }
    }

    /**
     * @param tenant        tenant principal for RLS scoping
     * @param collection    four-segment conformant collection name (drives
     *                      {@code chunks_<dim>} dispatch AND the manifest
     *                      rows' target). Required, non-blank.
     * @param chunks        request's top-level {@code chunks} array: each
     *                      element {@code {chash, text, metadata}}. May be
     *                      empty (a docs-only call with no new content —
     *                      every referenced chash must already exist).
     * @param docs          same shape {@link CatalogRepository#writeManifestMany}
     *                      always accepted: {@code {doc_id, rows}} per doc.
     * @param complete      optional {@code {doc_id: content_hash}} completion map.
     * @param sweep         nexus-eslkl superseded-vector sweep flag, unchanged.
     * @param forceReEmbed  bypasses the existence-partition entirely (RDR-181
     *                      escape, mirrors {@code PgVectorRepository}'s
     *                      {@code force_re_embed}) — every chash in {@code
     *                      chunks} is (re-)embedded regardless of stored state.
     */
    public CombinedWriteResult writeManyCombined(String tenant, String collection,
            List<Map<String, Object>> chunks, List<Map<String, Object>> docs,
            Map<String, String> complete, boolean sweep, boolean forceReEmbed) {
        return writeManyCombined(tenant, collection, chunks, docs, complete, sweep, forceReEmbed, null);
    }

    /**
     * {@link #writeManyCombined(String, String, List, List, Map, boolean, boolean)} with
     * client-supplied vectors (RDR-223 P1.5, bead nexus-z0o2p.6): a chunk may carry {@code
     * embedding}, and {@code embeddingModel} (the request's {@code embedding_model}) then names
     * the model that produced it. See {@link #checkSuppliedVectors} for the refusal rules and
     * {@link #resolveChunks} for how the four cells of Technical Design 2 are applied.
     */
    public CombinedWriteResult writeManyCombined(String tenant, String collection,
            List<Map<String, Object>> chunks, List<Map<String, Object>> docs,
            Map<String, String> complete, boolean sweep, boolean forceReEmbed, String embeddingModel) {
        return writeManyCombined(tenant, collection, chunks, docs, complete, sweep, forceReEmbed,
            embeddingModel, MetadataMode.REPLACE);
    }

    /**
     * {@link #writeManyCombined(String, String, List, List, Map, boolean, boolean, String)} with a
     * metadata write mode (RDR-223, bead nexus-z0o2p.13); see {@link MetadataMode}.
     */
    public CombinedWriteResult writeManyCombined(String tenant, String collection,
            List<Map<String, Object>> chunks, List<Map<String, Object>> docs,
            Map<String, String> complete, boolean sweep, boolean forceReEmbed, String embeddingModel,
            MetadataMode metadataMode) {
        checkSuppliedVectors(tenant, collection, chunks, embeddingModel);
        ResolvedBatch batch = resolveChunks(tenant, collection, chunks, forceReEmbed, metadataMode);

        // Phase 3: dispatch — every actual WRITE happens inside this call,
        // one per-doc transaction at a time.
        Map<String, Object> response =
            catalogRepo.writeManifestMany(tenant, docs, collection, complete, sweep, batch.resolved());
        int mismatchesCounted = recordMismatches(collection, batch, docs, response.get("failed_doc_ids"));
        // nexus-acvi7: merge the embed-partition counts into the SAME
        // response envelope `chunks_written` already rides — this is the
        // right seam (CatalogRepository.writeManifestMany's map, built at
        // CatalogRepository.java ~:4297-4326, knows nothing about the
        // embed phase; only CombinedWriteService does) rather than a
        // parallel channel. Additive keys: a 7.5.0 client
        // (http_catalog_client.py's write_manifest_many) reads only
        // named keys out of this map and silently ignores unknown ones,
        // so this is backward compatible with every client in the field
        // (verified: `out = {failed_doc_ids, complete_refused, ...}` is
        // built by explicit key extraction, never `dict(result)`).
        // Always present on this path (writeManyCombined is ONLY invoked
        // by CatalogHandler when the request actually carried `chunks` —
        // see CatalogHandler.handleManifestWriteMany's `rawChunks != null`
        // branch — so these three counts are never misleadingly absent
        // the way `chunks_written` is on the non-combined path).
        response.put("chunks_deduped", batch.deduped());
        response.put("embed_skipped", batch.skipped());
        response.put("embed_embedded", batch.embedded());
        response.put("vectors_supplied", batch.supplied());
        response.put("vector_mismatches", mismatchesCounted);
        // Echo of the metadata write mode (RDR-223, nexus-z0o2p.13): present only when the request asked
        // for merge and it was applied, so a client that asked for merge can tell an engine that ignored it.
        if (metadataMode.merge()) response.put("metadata_merge", true);
        return new CombinedWriteResult(response, batch.tokens());
    }

    /**
     * RDR-223 P1.1 (bead nexus-z0o2p.2) — append WITH chunks: resolves {@code chunks}
     * through the SAME dedupe / existence-partition (RDR-181) / embed phases {@link
     * #writeManyCombined} runs (outside any transaction), then hands the resolved
     * tuples to {@link CatalogRepository#appendManifestChunks(String, String, String,
     * List, Map, List[])}, which inserts them in the append's own transaction after
     * the index-run lock. The raced-embed counter (RDR-222) counts on this path
     * because it shares the partition and the repository's chunk upsert.
     *
     * <p>Only chunks the request's own {@code rows} reference are resolved: an
     * unreferenced chunk would be embedded for nothing (the repository inserts only
     * referenced chashes) and its metadata-only refresh would touch a chunk the
     * request does not own. The document is checked BEFORE the embed, so an unknown
     * {@code doc_id} costs no embedder call; the in-transaction check stays
     * authoritative.
     *
     * @param rows manifest rows to upsert by position (may be empty)
     * @param chunks the request's {@code chunks} array, each {@code {chash, text, metadata}}
     * @return {@code {ok, count, chunks_written, chunks_deduped, embed_skipped,
     *         embed_embedded}} plus the embed token usage
     */
    public CombinedWriteResult appendCombined(String tenant, String collection, String docId,
            List<Map<String, Object>> rows, List<Map<String, Object>> chunks, boolean forceReEmbed) {
        return appendCombined(tenant, collection, docId, rows, chunks, forceReEmbed, null);
    }

    /**
     * {@link #appendCombined(String, String, String, List, List, boolean)} plus the deferred
     * sweep of a multi-batch write (RDR-223 P1.3, bead nexus-z0o2p.4): after the append commits,
     * {@code sweepChashes} (at most {@link CatalogRepository#MAX_SWEEP_CHASHES_PER_APPEND}) are
     * swept in their own transaction, and the response gains {@code swept}, {@code sweep_skipped}
     * and {@code sweep_detail}. An over-cap list is refused BEFORE the embed.
     */
    public CombinedWriteResult appendCombined(String tenant, String collection, String docId,
            List<Map<String, Object>> rows, List<Map<String, Object>> chunks, boolean forceReEmbed,
            List<String> sweepChashes) {
        return appendCombined(tenant, collection, docId, rows, chunks, forceReEmbed, sweepChashes, null);
    }

    /**
     * {@link #appendCombined(String, String, String, List, List, boolean, List)} with
     * client-supplied vectors (RDR-223 P1.5, bead nexus-z0o2p.6); see {@link
     * #writeManyCombined(String, String, List, List, Map, boolean, boolean, String)}.
     */
    public CombinedWriteResult appendCombined(String tenant, String collection, String docId,
            List<Map<String, Object>> rows, List<Map<String, Object>> chunks, boolean forceReEmbed,
            List<String> sweepChashes, String embeddingModel) {
        return appendCombined(tenant, collection, docId, rows, chunks, forceReEmbed, sweepChashes,
            embeddingModel, MetadataMode.REPLACE);
    }

    /**
     * {@link #appendCombined(String, String, String, List, List, boolean, List, String)} with a
     * metadata write mode (RDR-223, bead nexus-z0o2p.13); see {@link MetadataMode}.
     */
    public CombinedWriteResult appendCombined(String tenant, String collection, String docId,
            List<Map<String, Object>> rows, List<Map<String, Object>> chunks, boolean forceReEmbed,
            List<String> sweepChashes, String embeddingModel, MetadataMode metadataMode) {
        if (docId == null || docId.isBlank()) {
            throw new IllegalArgumentException("'doc_id' required");
        }
        CatalogRepository.normalizeSweepChashes(sweepChashes);   // size check first: cheapest refusal
        checkSuppliedVectors(tenant, collection, chunks, embeddingModel);
        catalogRepo.requireDocumentRegistered(tenant, docId);

        java.util.Set<String> referenced = new HashSet<>();
        for (Map<String, Object> r : rows) {
            Object c = r.get("chash");
            if (c instanceof String s) referenced.add(s);
        }
        List<Map<String, Object>> relevant = new ArrayList<>();
        Set<String> unreferenced = new HashSet<>();
        for (Map<String, Object> c : chunks != null ? chunks : List.<Map<String, Object>>of()) {
            // A non-string chash is kept so resolveChunks rejects it loudly.
            if (!(c.get("chash") instanceof String s) || referenced.contains(s)) relevant.add(c);
            else unreferenced.add(s);
        }

        ResolvedBatch batch = resolveChunks(tenant, collection, relevant, forceReEmbed, metadataMode);
        CatalogRepository.AppendOutcome outcome = catalogRepo.appendManifestChunks(
            tenant, docId, collection, rows, batch.resolved(), null, sweepChashes);
        // The append returned, so its transaction committed: every mismatch counts.
        int mismatchesCounted = recordMismatches(collection, batch, null, null);

        Map<String, Object> response = new LinkedHashMap<>();
        response.put("ok", true);
        response.put("count", rows.size());
        response.put("chunks_written", outcome.chunksWritten());
        response.put("chunks_deduped", batch.deduped());
        response.put("embed_skipped", batch.skipped());
        response.put("embed_embedded", batch.embedded());
        response.put("vectors_supplied", batch.supplied());
        response.put("vector_mismatches", mismatchesCounted);
        // Echo of the metadata write mode (RDR-223, nexus-z0o2p.13): present only when the request asked
        // for merge and it was applied, so a client that asked for merge can tell an engine that ignored it.
        if (metadataMode.merge()) response.put("metadata_merge", true);
        // Distinct chashes of the request's chunks that no row referenced: neither embedded nor
        // inserted. Non-zero is a client bug made visible.
        response.put("chunks_unreferenced", unreferenced.size());
        outcome.addSweepFieldsTo(response);
        return new CombinedWriteResult(response, batch.tokens());
    }

    /**
     * RDR-223 P1.4 (bead nexus-z0o2p.5) -- the multi-document append behind {@code POST
     * /v1/catalog/manifest/append_many}: {@code chunks} (the request-level array the documents'
     * rows reference) are deduped, existence-partitioned (RDR-181) and embedded ONCE, outside any
     * transaction, then {@link CatalogRepository#appendManifestMany} appends each document in its
     * own transaction, each inserting only the chashes its own rows reference, each firing its
     * own deferred sweep after its own commit. Only chunks some document's rows reference are
     * resolved, as for {@link #appendCombined}. A {@code null} {@code chunks} skips the embed
     * entirely (the rows must reference chunks that already exist).
     *
     * @param docs each {@code {doc_id, rows, sweep_chashes?}}
     * @param chunks the request-level {@code chunks} array, or {@code null} for none
     */
    public CombinedWriteResult appendManyCombined(String tenant, String collection,
            List<Map<String, Object>> docs, List<Map<String, Object>> chunks, boolean forceReEmbed) {
        return appendManyCombined(tenant, collection, docs, chunks, forceReEmbed, null);
    }

    /**
     * {@link #appendManyCombined(String, String, List, List, boolean)} with client-supplied
     * vectors (RDR-223 P1.5, bead nexus-z0o2p.6); see {@link
     * #writeManyCombined(String, String, List, List, Map, boolean, boolean, String)}.
     */
    public CombinedWriteResult appendManyCombined(String tenant, String collection,
            List<Map<String, Object>> docs, List<Map<String, Object>> chunks, boolean forceReEmbed,
            String embeddingModel) {
        return appendManyCombined(tenant, collection, docs, chunks, forceReEmbed, embeddingModel,
            MetadataMode.REPLACE);
    }

    /**
     * {@link #appendManyCombined(String, String, List, List, boolean, String)} with a metadata
     * write mode (RDR-223, bead nexus-z0o2p.13); see {@link MetadataMode}.
     */
    public CombinedWriteResult appendManyCombined(String tenant, String collection,
            List<Map<String, Object>> docs, List<Map<String, Object>> chunks, boolean forceReEmbed,
            String embeddingModel, MetadataMode metadataMode) {
        checkSuppliedVectors(tenant, collection, chunks, embeddingModel);
        // Size-check every document's sweep list BEFORE the embed: the cheapest refusal.
        for (Map<String, Object> d : docs) {
            if (d.get("sweep_chashes") instanceof List<?> l) {
                @SuppressWarnings("unchecked")
                List<String> sweep = (List<String>) l;
                CatalogRepository.normalizeSweepChashes(sweep);
            }
        }
        if (chunks == null) {
            return new CombinedWriteResult(
                catalogRepo.appendManifestMany(tenant, collection, docs, null), 0L);
        }
        // One query: which documents exist at all. A chunk that only an unregistered document
        // references would be embedded for nothing (that document fails in place), so it is
        // treated as unreferenced. The per-document in-transaction check stays authoritative.
        List<String> docIds = new ArrayList<>();
        for (Map<String, Object> d : docs) {
            if (d.get("doc_id") instanceof String id) docIds.add(id);
        }
        Set<String> registered = catalogRepo.registeredDocIds(tenant, docIds);
        Set<String> referenced = new HashSet<>();
        for (Map<String, Object> d : docs) {
            if (!(d.get("doc_id") instanceof String id) || !registered.contains(id)) continue;
            if (d.get("rows") instanceof List<?> rows) {
                for (Object r : rows) {
                    if (r instanceof Map<?, ?> m && m.get("chash") instanceof String c) referenced.add(c);
                }
            }
        }
        List<Map<String, Object>> relevant = new ArrayList<>();
        Set<String> unreferenced = new HashSet<>();
        for (Map<String, Object> c : chunks) {
            if (!(c.get("chash") instanceof String s) || referenced.contains(s)) relevant.add(c);
            else unreferenced.add(s);
        }
        ResolvedBatch batch = resolveChunks(tenant, collection, relevant, forceReEmbed, metadataMode);
        Map<String, Object> response =
            catalogRepo.appendManifestMany(tenant, collection, docs, batch.resolved());
        int mismatchesCounted = recordMismatches(collection, batch, docs, response.get("failed_doc_ids"));
        response.put("chunks_deduped", batch.deduped());
        response.put("embed_skipped", batch.skipped());
        response.put("embed_embedded", batch.embedded());
        response.put("vectors_supplied", batch.supplied());
        response.put("vector_mismatches", mismatchesCounted);
        // Echo of the metadata write mode (RDR-223, nexus-z0o2p.13): present only when the request asked
        // for merge and it was applied, so a client that asked for merge can tell an engine that ignored it.
        if (metadataMode.merge()) response.put("metadata_merge", true);
        response.put("chunks_unreferenced", unreferenced.size());
        return new CombinedWriteResult(response, batch.tokens());
    }

    /** Output of the dedupe / existence-partition / embed phases. */
    private record ResolvedBatch(Map<String, CatalogRepository.ResolvedChunk> resolved,
                                 int deduped, int skipped, int embedded, int supplied,
                                 List<String> mismatchedChashes, long tokens) {
        int mismatches() { return mismatchedChashes.size(); }
    }

    /**
     * Counts and logs the supplied-vector mismatches of a batch, once the write that used it has
     * COMMITTED (a request that fails and is retried by the client must not count twice).
     */
    private static int recordMismatches(String collection, ResolvedBatch batch,
                                        List<Map<String, Object>> docs, Object failedDocIds) {
        List<String> m = new ArrayList<>(batch.mismatchedChashes());
        if (m.isEmpty()) return 0;
        if (docs != null) {
            // Multi-document write: a document that failed in place rolled back, so a mismatch
            // only counts if a document that COMMITTED references the chash.
            Set<String> failed = new HashSet<>();
            if (failedDocIds instanceof java.util.Collection<?> f) {
                for (Object o : f) failed.add(String.valueOf(o));
            }
            Set<String> committedChashes = new HashSet<>();
            for (Map<String, Object> d : docs) {
                if (d.get("doc_id") instanceof String id && failed.contains(id)) continue;
                if (d.get("rows") instanceof List<?> rows) {
                    for (Object r : rows) {
                        if (r instanceof Map<?, ?> row && row.get("chash") instanceof String c) committedChashes.add(c);
                    }
                }
            }
            m.retainAll(committedChashes);
            if (m.isEmpty()) return 0;
        }
        SuppliedVectorMismatchActivity.record(m.size());
        log.info("event=supplied_vector_mismatch collection={} mismatched={} chashes={}",
                 collection, m.size(), String.join(",", m.subList(0, Math.min(8, m.size()))));
        return m.size();
    }

    /**
     * RDR-223 P1.5 (bead nexus-z0o2p.6) -- validate every client-supplied vector BEFORE any
     * transaction or embed, and refuse the WHOLE request on the first problem: a chunk's {@code
     * embedding} must be an array of finite numbers whose length is the collection's dimension,
     * and the request's {@code embedding_model} (required as soon as any chunk carries a vector)
     * must equal the collection's registered {@code embedding_model} (F-8). Indexes in the
     * messages are into {@code chunks} as the client sent it. A request with no vectors is not
     * checked at all, so an {@code embedding_model} riding without vectors is ignored.
     *
     * @param embeddingModel the request's top-level {@code embedding_model}, or {@code null}
     * @throws IllegalArgumentException naming both values on a mismatch (mapped to 400)
     */
    private void checkSuppliedVectors(String tenant, String collection,
                                      List<Map<String, Object>> chunks, String embeddingModel) {
        if (chunks == null) return;
        int first = -1;
        for (int i = 0; i < chunks.size(); i++) {
            if (chunks.get(i).get("embedding") != null) { first = i; break; }
        }
        if (first < 0) return;
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("'collection' is required and must be non-blank");
        }
        CollectionRow row = CollectionRegistry.lookup(tenantScope, tenant, collection);
        if (embeddingModel == null || embeddingModel.isBlank()) {
            throw new IllegalArgumentException("'embedding_model' is required when a chunk carries an"
                + " 'embedding' (chunks[" + first + "] does); collection '" + collection
                + "' is registered with embedding_model '" + row.embeddingModel() + "'");
        }
        if (!embeddingModel.equals(row.embeddingModel())) {
            throw new IllegalArgumentException("embedding_model '" + embeddingModel
                + "' does not match collection '" + collection + "' embedding_model '"
                + row.embeddingModel() + "'; nothing was stored");
        }
        for (int i = first; i < chunks.size(); i++) {
            float[] v = toFloatArray(chunks.get(i).get("embedding"), "chunks[" + i + "].embedding");
            if (v != null && v.length != row.dimension()) {
                throw new IllegalArgumentException("chunks[" + i + "].embedding has " + v.length
                    + " dimensions; collection '" + collection + "' (embedding_model '"
                    + row.embeddingModel() + "') has " + row.dimension() + "; nothing was stored");
            }
        }
    }

    /**
     * A chunk's {@code embedding} as a {@code float[]}: a {@code float[]} as is, or a list of
     * numbers (what Jackson hands the engine). {@code null} in, {@code null} out.
     *
     * @throws IllegalArgumentException for anything else, or a non-finite component
     */
    static float[] toFloatArray(Object raw, String what) {
        if (raw == null) return null;
        if (raw instanceof float[] f) {
            for (float x : f) {
                if (!Float.isFinite(x)) throw new IllegalArgumentException(what + " contains a non-finite component");
            }
            return f;
        }
        if (!(raw instanceof List<?> nums)) {
            throw new IllegalArgumentException(what + " must be an array of numbers");
        }
        float[] out = new float[nums.size()];
        for (int i = 0; i < out.length; i++) {
            if (!(nums.get(i) instanceof Number n)) {
                throw new IllegalArgumentException(what + " contains a non-numeric component");
            }
            out[i] = n.floatValue();
            if (!Float.isFinite(out[i])) {
                throw new IllegalArgumentException(what + " contains a non-finite component");
            }
        }
        return out;
    }

    /**
     * Phases 1-2b, shared by {@link #writeManyCombined} and {@link #appendCombined}:
     * dedupe by chash, existence-partition (with the metadata-only refresh), and
     * embed the rest, all OUTSIDE any manifest transaction.
     */
    private ResolvedBatch resolveChunks(String tenant, String collection,
            List<Map<String, Object>> chunks, boolean forceReEmbed, MetadataMode metadataMode) {
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("'collection' is required and must be non-blank");
        }
        int dim = CollectionRegistry.lookup(tenantScope, tenant, collection).dimension();
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);

        // RDR-204 Phase 1 (bead nexus-ft04v.7): require catalog_collections to
        // already carry a row for `collection` before any chunk write — checked here
        // (in its OWN short transaction — never inside a per-doc manifest transaction)
        // instead of the RDR-156-era auto-stub. A combined write against an
        // unregistered collection now fails loud (UnregisteredCollectionException,
        // mapped to 422) rather than silently registering a blank-attribute row.
        ensureCollectionRegistered(tenant, collection);

        List<Map<String, Object>> src = chunks != null ? chunks : List.of();

        // Phase 1: dedupe by chash, first occurrence wins.
        LinkedHashMap<String, Map<String, Object>> dedup = new LinkedHashMap<>();
        for (int i = 0; i < src.size(); i++) {
            Map<String, Object> c = src.get(i);
            Object rawChash = c.get("chash");
            if (!(rawChash instanceof String chash) || chash.isBlank()) {
                throw new IllegalArgumentException("chunks[" + i + "].chash required (string)");
            }
            dedup.putIfAbsent(chash, c);
        }

        List<String> dedupChashes = new ArrayList<>(dedup.keySet());
        List<String> dedupTexts   = new ArrayList<>(dedupChashes.size());
        List<Map<String, Object>> dedupMetas = new ArrayList<>(dedupChashes.size());
        // RDR-223 P1.5: the client-supplied vector of each deduped chunk, or null. First
        // occurrence wins, like the text. Already validated by checkSuppliedVectors.
        List<float[]> dedupVectors = new ArrayList<>(dedupChashes.size());
        for (String chash : dedupChashes) {
            Map<String, Object> c = dedup.get(chash);
            dedupVectors.add(toFloatArray(c.get("embedding"), "chunks[].embedding"));
            Object rawText = c.get("text");
            String text = stripNul(rawText instanceof String s ? s : "");
            dedupTexts.add(text);
            Object rawMeta = c.get("metadata");
            @SuppressWarnings("unchecked")
            Map<String, Object> meta = rawMeta instanceof Map<?, ?> m
                ? (Map<String, Object>) m : Map.of();
            dedupMetas.add(sanitizeNulDeep(meta));
        }

        // Phase 2a: existence-partition + metadata-only refresh -- ONE
        // short, independently-committed transaction (never a manifest
        // write; composes safely ahead of the combined write's per-doc
        // transactions below). RDR-181: a chash already stored with
        // IDENTICAL text is never re-embedded.
        //
        // nexus-4jj40 round 6 (T2 code-review-nexus-4jj40-be444581c
        // [24633] Important finding): the metadata-only UPDATE below runs
        // in the SAME transaction as the existence SELECT that found the
        // chash -- true parity with PgVectorRepository.resolveNeedEmbedIdx
        // (SELECT + have-vector UPDATE in one transaction), and one fewer
        // DB round trip per flush batch than round 5's version (which ran
        // the UPDATE in a second, separate withTenant call; the round-5
        // comment claimed "same discipline" without this actually being
        // true, since correctness there still held via the zeroAffected
        // reroute regardless of transaction boundary -- see that finding's
        // full analysis).
        //
        // Chashes that already carry IDENTICAL stored text and are NOT
        // forced -- the RDR-181 skip set -- used to be OMITTED from
        // `resolved` entirely (original nexus-kl2z6 design: "no
        // chunks_<dim> write at all"), so a caller whose ONLY change
        // between two indexing runs is chunk METADATA (byte-identical
        // text, e.g. RDR-200 Phase 1c's section_type reclassification)
        // never saw that metadata land. The metadataOnly subset below gets
        // an explicit metadata-only UPDATE, like PgVectorRepository's own
        // have-vector branch (resolveNeedEmbedIdx / batchUpdateMetadata)
        // -- the DIRECT upsert path already had this; the combined-write
        // path did not.
        //
        // nexus-w94eo / RDR-223 (nexus-z0o2p.13): the write mode of a stored
        // chash's metadata is the request's MetadataMode. REPLACE (the default):
        // this call passes null delete keys to batchUpdateMetadata, which replaces,
        // matching CatalogRepository.upsertManifestChunkVectors' insert on the same
        // payload; a caller that omits a key (the normalize() sparse drops) clears
        // it, and so does the bib_* enrichment on a docs/rdr/code chunk re-indexed
        // by `nx index repo`. Pinned by PgVectorRepositoryContractTest
        // .batchUpdateMetadata_sixArgCombinedWriteMode_stillReplaces. MERGE
        // (metadata_merge): this call passes the request's delete keys, so
        // batchUpdateMetadata MERGES in SQL ((stored - delete_keys) || incoming), and
        // the insert branch merges in its ON CONFLICT SET in one statement (the
        // ResolvedChunk carries the keys), so a metadata write that lands between
        // this transaction and the insert is not overwritten.
        //
        // nexus-hxrcm: under the same 40P01 retry belt as every multi-row
        // vector write (DeadlockRetry). batchUpdateMetadata now orders its
        // row locks by chash, which removes the same-path cycle; a residual
        // deadlock against a DIFFERENT lock order (the superseded-chunk
        // sweep DELETE, orphan GC) is still possible, and this SELECT +
        // UPDATE transaction is idempotent, so re-running it is safe.
        // RDR-222 Phase 0 (bead nexus-ulrjq): originalAbsentIdx is the STRICT SUBSET
        // of needEmbedIdx that was ABSENT at this existence SELECT (stored == null),
        // as opposed to present-but-content-divergent or the zero-row reroute below.
        // Threaded onto each ResolvedChunk (originalAbsent) so
        // CatalogRepository#upsertManifestChunkVectors's own RETURNING-based raced
        // count can tell "this chash genuinely raced another writer" from "this
        // chash's presence was already known to this call" — see
        // PgVectorRepository.NeedEmbedResolution's javadoc for the identical
        // distinction on the direct upsert-chunks path.
        Set<Integer> originalAbsentIdx = new HashSet<>();
        // RDR-223 P1.5: chashes whose stored vector (identical text) differs from the supplied one.
        List<String> mismatchedChashes = new ArrayList<>();
        // RDR-223: chashes kept as stored although the request's text differs (a supplied vector
        // without force never rewrites an existing chash). Logged at debug, not counted.
        List<String> keptDivergent = new ArrayList<>();
        List<Integer> needEmbedIdx = dedupChashes.isEmpty() ? new ArrayList<>()
            : DeadlockRetry.run(collection + " combined-write metadata refresh", () -> tenantScope.withTenant(tenant, ctx -> {
                // nexus-hxrcm residual: SHARED sweep gate first, like every manifest
                // writer (CatalogRepository.acquireSweepGateShared's writer table). The
                // UPDATE below touches rows the sweep DELETEs under the EXCLUSIVE half;
                // gated, this transaction waits for a running sweep instead of racing it.
                // The wait compounds (sweeps queued ahead times SWEEP_STATEMENT_TIMEOUT_MS,
                // see that javadoc), not a flat 5 s; the writer side has no lock_timeout
                // by design, the same trade every manifest writer already makes.
                CatalogRepository.acquireSweepGateShared(ctx, tenant, collection);
                Map<String, String> existingText =
                    selectExistingText(ctx, ch, tenant, collection, dedupChashes);
                // RDR-222 Phase 0: reset per DeadlockRetry attempt (a retried attempt
                // re-runs this whole transaction from scratch — see
                // PgVectorRepository.upsertChunksInternal's identical racedThisWrite
                // reset for the full rationale).
                originalAbsentIdx.clear();
                mismatchedChashes.clear();
                keptDivergent.clear();
                List<Integer> need = new ArrayList<>();
                List<Integer> metadataOnly = new ArrayList<>();
                List<Integer> existingWithSupplied = new ArrayList<>();
                for (int i = 0; i < dedupChashes.size(); i++) {
                    String stored = existingText.get(dedupChashes.get(i));
                    if (stored == null) {
                        originalAbsentIdx.add(i);
                    }
                    boolean supplied = dedupVectors.get(i) != null;
                    if (supplied && stored != null) {
                        existingWithSupplied.add(i);
                    }
                    if (supplied && !forceReEmbed && stored != null) {
                        // R-14 cell 4: an existing chash keeps its stored vector (and text)
                        // whatever the supplied one says; only the metadata is refreshed.
                        if (!stored.equals(dedupTexts.get(i))) keptDivergent.add(dedupChashes.get(i));
                        metadataOnly.add(i);
                    } else if (forceReEmbed || stored == null || !stored.equals(dedupTexts.get(i))) {
                        need.add(i);
                    } else {
                        metadataOnly.add(i);
                    }
                }
                // Test-only interleaving seam (RDR-222 Phase 0 fix round) — see
                // afterExistencePartitionHookForTests javadoc. Fires INSIDE this
                // transaction, AFTER the existence SELECT resolves and BEFORE
                // batchUpdateMetadata's have-vector UPDATE below runs. No-op (null)
                // in production.
                Runnable existencePartitionHook = afterExistencePartitionHookForTests;
                if (existencePartitionHook != null) {
                    existencePartitionHook.run();
                }
                // RDR-223 P1.5, Technical Design 2 (R-14): an existing chash that also carries a
                // supplied vector KEEPS its stored vector unless the client forced the write.
                // Compare the two so a disagreement is counted and logged (under force the
                // supplied vector is written, and the differing stored one is what is counted).
                if (!existingWithSupplied.isEmpty()) {
                    List<String> hexes = new ArrayList<>(existingWithSupplied.size());
                    for (int i : existingWithSupplied) hexes.add(dedupChashes.get(i));
                    Map<String, float[]> storedVectors = selectStoredVectors(ctx, ch, tenant, collection, hexes);
                    for (int i : existingWithSupplied) {
                        float[] stored = storedVectors.get(dedupChashes.get(i));
                        // A chash whose vector cannot be read back (concurrently deleted) is
                        // not a mismatch: the zero-row reroute below re-stores it.
                        if (stored != null && !java.util.Arrays.equals(stored, dedupVectors.get(i))) {
                            mismatchedChashes.add(dedupChashes.get(i));
                        }
                    }
                }
                if (!metadataOnly.isEmpty()) {
                    // A chash present at the existence SELECT above but
                    // gone by the time this UPDATE runs, INSIDE THIS SAME
                    // transaction (concurrent orphan-GC pass), affects 0
                    // rows; batchUpdateMetadata reports it back and it is
                    // rerouted to need-embed, never silently dropped --
                    // the SAME concurrent-delete race guard
                    // PgVectorRepository's own caller already relies on.
                    // NOT added to originalAbsentIdx: this chash WAS present
                    // (with identical text) at the SELECT above, so it is the
                    // zero-row-reroute class, deliberately excluded from the
                    // raced-embed count exactly like the direct upsert path.
                    // Merge mode passes the caller's delete keys (non-null list = MERGE in
                    // SQL: (stored - delete_keys) || incoming); replace mode passes null.
                    need.addAll(PgVectorRepository.batchUpdateMetadata(
                        ctx, ch, collection, dedupChashes, dedupMetas, metadataOnly,
                        metadataMode.merge() ? metadataMode.deleteKeys() : null));
                }
                return need;
            }));

        if (!keptDivergent.isEmpty() && log.isDebugEnabled()) {
            log.debug("event=supplied_vector_kept_divergent_text collection={} kept={} chashes={}",
                      collection, keptDivergent.size(),
                      String.join(",", keptDivergent.subList(0, Math.min(8, keptDivergent.size()))));
        }

        // Test-only interleaving seam (RDR-222 Phase 0) — see
        // afterNeedEmbedResolvedHookForTests javadoc. Fires AFTER Phase 2a's
        // existence-partition transaction has committed and BEFORE the embed call
        // below, so a test can let a concurrent second writer commit one of THIS
        // call's originally-absent chashes (or recreate a zero-row-rerouted one)
        // before this call's own per-doc INSERT runs. No-op (null) in production.
        Runnable needEmbedResolvedHook = afterNeedEmbedResolvedHookForTests;
        if (needEmbedResolvedHook != null) {
            needEmbedResolvedHook.run();
        }

        // RDR-223 P1.5: a chunk that needs writing AND carries a supplied vector stores that
        // vector as-is (no embedder call); only the rest go to the embedder. A supplied vector
        // is stored for a chash that is absent, or under force_re_embed (explicit intent to
        // overwrite); for a chash that already exists it never replaces the stored vector.
        List<String> textsToEmbed = new ArrayList<>(needEmbedIdx.size());
        int suppliedCount = 0;
        for (int idx : needEmbedIdx) {
            if (dedupVectors.get(idx) != null) {
                suppliedCount++;
            } else {
                textsToEmbed.add(dedupTexts.get(idx));
            }
        }

        // nexus-acvi7: the existence-partition above is otherwise completely
        // unobservable — the RDR-181 "known chashes are never re-embedded"
        // hard requirement (design memo §0) had zero log lines and zero
        // response accounting, so a silently-broken skip (every warm
        // reindex re-embedding unchanged chunks) was indistinguishable from
        // a working one. One INFO line per call, emitted AFTER the
        // partition and BEFORE the embed call below (2.5(a) of T2
        // [22162]) — this is deliberately the FIRST log line
        // CombinedWriteService ever emits.
        int embeddedCount = textsToEmbed.size();
        int skippedCount  = dedupChashes.size() - needEmbedIdx.size();
        log.info("event=combined_write_embed_partition collection={} deduped={} skipped={} embedded={} supplied={} force_re_embed={}",
                  collection, dedupChashes.size(), skippedCount, embeddedCount, suppliedCount, forceReEmbed);

        // Phase 2b: embed OUTSIDE any transaction — the existence-check
        // transaction above has already committed, and no per-doc manifest
        // transaction has opened yet (design memo §0: "ALL embedding for
        // the ENTIRE call ... completes BEFORE the first per-doc
        // transaction opens"). Same embedder PgVectorRepository uses.
        EmbedResult embedResult = textsToEmbed.isEmpty()
            ? new EmbedResult(List.of(), 0L)
            : docRouter.embedForCollectionWithUsage(tenantScope, tenant, collection, textsToEmbed);
        List<float[]> embeddings = embedResult.embeddings();

        // Fail loud BEFORE any per-doc transaction if a vector's dimension
        // does not match the dispatched table (no truncation, no padding) —
        // mirrors PgVectorRepository.upsertChunksInternal's identical guard.
        for (float[] vec : embeddings) {
            if (vec.length != dim) {
                throw new IllegalArgumentException(
                    "embedder produced a " + vec.length + "-dim vector for collection '"
                    + collection + "' which dispatches to embedding_" + dim);
            }
        }

        Map<String, CatalogRepository.ResolvedChunk> resolved = new HashMap<>();
        int nextEmbedding = 0;
        for (int k = 0; k < needEmbedIdx.size(); k++) {
            int idx = needEmbedIdx.get(k);
            String chash = dedupChashes.get(idx);
            float[] supplied = dedupVectors.get(idx);
            String metadataJson;
            try {
                metadataJson = CatalogRepository.MAPPER.writeValueAsString(dedupMetas.get(idx));
            } catch (Exception e) {
                throw new IllegalArgumentException(
                    "chunks[].metadata for chash '" + chash + "' is not JSON-serializable", e);
            }
            // A supplied vector cost no embed, so a race on it is not a duplicate embed:
            // originalAbsent (which feeds the raced-embed counter) stays false for it.
            resolved.put(chash,
                new CatalogRepository.ResolvedChunk(dedupTexts.get(idx),
                    supplied != null ? supplied : embeddings.get(nextEmbedding++), metadataJson,
                    supplied == null && originalAbsentIdx.contains(idx),
                    // A supplied vector written without force must not overwrite one a racing
                    // writer stored between the existence check and the insert.
                    supplied != null && !forceReEmbed,
                    // Merge mode: the insert's ON CONFLICT merges in the same statement.
                    metadataMode.merge() ? metadataMode.deleteKeys() : null));
        }

        return new ResolvedBatch(resolved, dedupChashes.size(), skippedCount, embeddedCount,
            suppliedCount, new ArrayList<>(mismatchedChashes), embedResult.tokens());
    }

    /**
     * RDR-204 Phase 1 (bead nexus-ft04v.7): require catalog_collections to already
     * carry a row for {@code (tenant, collection)}, in its own short transaction
     * (never inside a per-doc manifest transaction), skipped when {@link
     * CollectionRegistry} already knows the pair. Replaces the RDR-70r3c-era
     * auto-stub {@code INSERT ... ON CONFLICT DO NOTHING} (which used to derive
     * content_type/owner_id/embedding_model/model_version by splitting the name on
     * {@code "__"}) with a fail-loud check — a combined write against an
     * unregistered collection throws {@link UnregisteredCollectionException} instead
     * of ever writing a row.
     */
    private void ensureCollectionRegistered(String tenant, String collection) {
        if (CollectionRegistry.isKnown(tenant, collection)) return;
        tenantScope.withTenant(tenant, ctx -> {
            CollectionRegistry.requireRegistered(ctx, tenant, collection);
            return null;
        });
    }

    /** The stored vector of each of {@code chashes} that exists in the collection (RDR-223 P1.5). */
    private static Map<String, float[]> selectStoredVectors(DSLContext ctx, DimTables.ChunkTable ch,
            String tenant, String collection, List<String> chashes) {
        Map<String, float[]> out = new HashMap<>();
        ctx.select(ch.chash(), ch.embedding()).from(ch.table())
           .where(ch.tenantId().eq(tenant)
                  .and(ch.collection().eq(collection))
                  .and(ch.chash().in(chashes)))
           .fetch()
           .forEach(r -> { if (r.value2() != null) out.put(r.value1(), r.value2().floats()); });
        return out;
    }

    private static Map<String, String> selectExistingText(DSLContext ctx, DimTables.ChunkTable ch,
            String tenant, String collection, List<String> chashes) {
        Map<String, String> out = new HashMap<>();
        ctx.select(ch.chash(), ch.chunkText()).from(ch.table())
           .where(ch.tenantId().eq(tenant)
                  .and(ch.collection().eq(collection))
                  .and(ch.chash().in(chashes)))
           .fetch()
           .forEach(r -> out.put(r.value1(), r.value2()));
        return out;
    }

    /** Strip NUL (0x00) — unstorable in Postgres {@code text}/{@code jsonb} (nexus-rvfwj),
     *  mirrors {@code PgVectorRepository.stripNul}. */
    private static String stripNul(String s) {
        return (s != null && s.indexOf('\u0000') >= 0) ? s.replace("\u0000", "") : s;
    }

    /** Recursively strip NULs from metadata, mirrors {@code PgVectorRepository.sanitizeNulDeep}. */
    private static Map<String, Object> sanitizeNulDeep(Map<String, Object> meta) {
        if (meta == null) return Map.of();
        Map<String, Object> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : meta.entrySet()) {
            out.put(stripNul(e.getKey()), sanitizeNulValue(e.getValue()));
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    private static Object sanitizeNulValue(Object v) {
        if (v instanceof String s) return stripNul(s);
        if (v instanceof Map<?, ?> m) return sanitizeNulDeep((Map<String, Object>) m);
        if (v instanceof List<?> l) {
            List<Object> out = new ArrayList<>(l.size());
            for (Object o : l) out.add(sanitizeNulValue(o));
            return out;
        }
        return v;
    }
}
