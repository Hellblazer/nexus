// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * nexus-kl2z6 increment 1 — the orchestration seam T2 {@code
 * design-kl2z6-combined-write} §1.3 names: sits in front of {@link
 * CatalogRepository#writeManifestMany(String, List, Map, boolean, String,
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
 *       overload — which is where every per-doc transaction, and thus
 *       every actual WRITE, happens.</li>
 * </ol>
 *
 * <p>A chash present in the request's {@code chunks} array but resolved
 * as "already have identical text" is deliberately OMITTED from the
 * returned map — it needs no chunks_&lt;dim&gt; write at all, and {@link
 * CatalogRepository}'s per-doc chunk-upsert only writes chashes present in
 * the map, treating everything else as "must already exist" (verified
 * in-transaction, per doc, at the point a manifest actually references it).
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

    /** Embed-phase token usage plus the underlying {@code writeManifestMany} response. */
    public record CombinedWriteResult(Map<String, Object> response, long tokens) {}

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
        if (collection == null || collection.isBlank()) {
            throw new IllegalArgumentException("'collection' required when 'chunks' is present");
        }
        int dim = PgVectorRepository.dimForCollection(collection);
        DimTables.ChunkTable ch = DimTables.CHUNKS.get(dim);

        // Standing rule (RDR-156 P0.2, bead nexus-70r3c.2), same discipline
        // PgVectorRepository.upsertChunksInternal applies: collection
        // registration precedes chunk writes, enforced here (auto-stub, in
        // its OWN short committed transaction — never inside a per-doc
        // manifest transaction) and by the chunks_<dim> -> catalog_collections
        // FK. Without this, the FIRST combined write to a brand-new
        // collection fails every doc with a chunks_<dim>_collection_fk
        // violation.
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
        for (String chash : dedupChashes) {
            Map<String, Object> c = dedup.get(chash);
            Object rawText = c.get("text");
            String text = stripNul(rawText instanceof String s ? s : "");
            dedupTexts.add(text);
            Object rawMeta = c.get("metadata");
            @SuppressWarnings("unchecked")
            Map<String, Object> meta = rawMeta instanceof Map<?, ?> m
                ? (Map<String, Object>) m : Map.of();
            dedupMetas.add(sanitizeNulDeep(meta));
        }

        // Phase 2a: existence-partition — one short, independently-committed
        // transaction (never a manifest write; composes safely ahead of the
        // combined write's per-doc transactions below). RDR-181: a chash
        // already stored with IDENTICAL text is never re-embedded.
        Map<String, String> existingText = dedupChashes.isEmpty() ? Map.of()
            : tenantScope.withTenant(tenant, ctx -> selectExistingText(ctx, ch, tenant, collection, dedupChashes));

        List<Integer> needEmbedIdx = new ArrayList<>();
        for (int i = 0; i < dedupChashes.size(); i++) {
            String stored = existingText.get(dedupChashes.get(i));
            if (forceReEmbed || stored == null || !stored.equals(dedupTexts.get(i))) {
                needEmbedIdx.add(i);
            }
        }

        List<String> textsToEmbed = new ArrayList<>(needEmbedIdx.size());
        for (int idx : needEmbedIdx) {
            textsToEmbed.add(dedupTexts.get(idx));
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
        int embeddedCount = needEmbedIdx.size();
        int skippedCount  = dedupChashes.size() - embeddedCount;
        log.info("event=combined_write_embed_partition collection={} deduped={} skipped={} embedded={} force_re_embed={}",
                  collection, dedupChashes.size(), skippedCount, embeddedCount, forceReEmbed);

        // Phase 2b: embed OUTSIDE any transaction — the existence-check
        // transaction above has already committed, and no per-doc manifest
        // transaction has opened yet (design memo §0: "ALL embedding for
        // the ENTIRE call ... completes BEFORE the first per-doc
        // transaction opens"). Same embedder PgVectorRepository uses.
        EmbedResult embedResult = textsToEmbed.isEmpty()
            ? new EmbedResult(List.of(), 0L)
            : docRouter.embedForCollectionWithUsage(collection, textsToEmbed);
        List<float[]> embeddings = embedResult.embeddings();

        // Fail loud BEFORE any per-doc transaction if a vector's dimension
        // does not match the dispatched table (no truncation, no padding) —
        // mirrors PgVectorRepository.upsertChunksInternal's identical guard.
        for (float[] vec : embeddings) {
            if (vec.length != dim) {
                throw new IllegalArgumentException(
                    "embedder produced a " + vec.length + "-dim vector for collection '"
                    + collection + "' which dispatches to chunks_" + dim);
            }
        }

        Map<String, CatalogRepository.ResolvedChunk> resolved = new HashMap<>();
        for (int k = 0; k < needEmbedIdx.size(); k++) {
            int idx = needEmbedIdx.get(k);
            String chash = dedupChashes.get(idx);
            String metadataJson;
            try {
                metadataJson = CatalogRepository.MAPPER.writeValueAsString(dedupMetas.get(idx));
            } catch (Exception e) {
                throw new IllegalArgumentException(
                    "chunks[].metadata for chash '" + chash + "' is not JSON-serializable", e);
            }
            resolved.put(chash,
                new CatalogRepository.ResolvedChunk(dedupTexts.get(idx), embeddings.get(k), metadataJson));
        }

        // Phase 3: dispatch — every actual WRITE happens inside this call,
        // one per-doc transaction at a time.
        Map<String, Object> response =
            catalogRepo.writeManifestMany(tenant, docs, complete, sweep, collection, resolved);
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
        response.put("chunks_deduped", dedupChashes.size());
        response.put("embed_skipped", skippedCount);
        response.put("embed_embedded", embeddedCount);
        return new CombinedWriteResult(response, embedResult.tokens());
    }

    /**
     * Mirrors {@code PgVectorRepository.upsertChunksInternal}'s ensure-registered
     * stub-insert exactly: a short, independently-committed transaction (never
     * inside a per-doc manifest transaction), skipped when {@link
     * CollectionRegistry} already knows this {@code (tenant, collection)} pair.
     */
    private void ensureCollectionRegistered(String tenant, String collection) {
        if (CollectionRegistry.isKnown(tenant, collection)) return;
        String[] collSegs = collection.split("__");
        boolean conformant = collSegs.length == 4;
        String regContentType  = conformant ? collSegs[0] : "";
        String regOwner        = conformant ? collSegs[1] : "";
        String regModel        = conformant ? collSegs[2] : "";
        String regModelVersion = conformant ? collSegs[3] : "";
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
        CollectionRegistry.markKnown(tenant, collection);
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
