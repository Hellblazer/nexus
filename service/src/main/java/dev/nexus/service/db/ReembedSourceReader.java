// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): what a re-embed job reads from its source collection. Read-only;
 * every read goes through {@link TenantScope#withTenant}.
 *
 * <p><b>Candidates.</b> A document is a candidate of {@code source} when it is live and either (a) at least one
 * of its manifest rows names {@code source}, or (b) it has no manifest rows at all and its
 * {@code physical_collection} is {@code source} (the shape {@code requireMovePrecondition} compares). The job
 * never enumerates by {@code physical_collection} alone: a document whose manifest was re-written under another
 * collection can keep a stale {@code physical_collection}, and one whose manifest names the source can name
 * another there (t5mg2-1).
 */
public final class ReembedSourceReader {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<LinkedHashMap<String, Object>> JSON_OBJECT = new TypeReference<>() { };

    /** A candidate document and the number of manifest rows it has (in any collection). */
    public record Candidate(String docId, int manifestRows) {
    }

    private final TenantScope scope;

    public ReembedSourceReader(TenantScope scope) {
        this.scope = scope;
    }

    /**
     * The live candidates of {@code source} whose tumbler sorts after {@code afterDocId} (the empty string is
     * before every tumbler), in tumbler order, at most {@code limit}. The order is the column's own text order,
     * the same one {@code ReembedJobRepository}'s monotone cursor uses.
     */
    public List<Candidate> candidates(String tenant, String source, String afterDocId, int limit) {
        if (limit <= 0) {
            return List.of();
        }
        return scope.withTenant(tenant, ctx -> {
            var d = CATALOG_DOCUMENTS;
            var c = CATALOG_DOCUMENT_CHUNKS;
            Condition namesSource = DSL.exists(ctx.selectOne().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)).and(c.COLLECTION.eq(source)));
            Condition hasNoManifest = DSL.notExists(ctx.selectOne().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)));
            Field<Integer> rows = DSL.field(ctx.selectCount().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)));
            return ctx.select(d.TUMBLER, rows).from(d)
                .where(d.TENANT_ID.eq(tenant))
                .and(d.DELETED_AT.isNull())
                .and(d.TUMBLER.gt(afterDocId))
                .and(namesSource.or(hasNoManifest.and(d.PHYSICAL_COLLECTION.eq(source))))
                .orderBy(d.TUMBLER)
                .limit(limit)
                .fetch(r -> new Candidate(r.value1(), r.value2()));
        });
    }

    /** Whether {@code source} has any live candidate at all. */
    public boolean anyCandidate(String tenant, String source) {
        return !candidates(tenant, source, "", 1).isEmpty();
    }

    /**
     * What still names the source, read just before the cutover deletes it. The delete removes every document row
     * homed at the source (live or tombstoned, whether or not a manifest names it) and every aspect, queue and
     * highlight row naming it, so anything counted here is lost if the delete runs.
     *
     * @param liveCandidates       whether a live candidate document remains (a client wrote one after the pass)
     * @param tombstoned           soft-deleted documents whose manifest names the source or that are homed at it
     *                             (the delete removes them, trash included)
     * @param liveHomedElsewhere   live documents homed at the source whose manifest names only other collections
     *                             (not a candidate, so never moved; the delete removes their catalog row)
     * @param aspects              {@code document_aspects} rows naming the source
     * @param queue                {@code aspect_extraction_queue} rows naming the source
     * @param highlights           {@code document_highlights} rows naming the source
     */
    public record Residue(boolean liveCandidates, long tombstoned, long liveHomedElsewhere, long aspects,
                          long queue, long highlights) {

        public boolean isEmpty() {
            return !liveCandidates && tombstoned == 0 && liveHomedElsewhere == 0 && aspects == 0 && queue == 0
                && highlights == 0;
        }

        /** A sentence for {@code last_error}: what the source still holds. */
        public String describe() {
            List<String> parts = new ArrayList<>();
            if (liveCandidates) parts.add("a live document");
            if (tombstoned > 0) parts.add(tombstoned + " tombstoned document(s)");
            if (liveHomedElsewhere > 0) parts.add(liveHomedElsewhere + " live document(s) homed here whose chunks are elsewhere");
            if (aspects > 0) parts.add(aspects + " document_aspects row(s)");
            if (queue > 0) parts.add(queue + " aspect_extraction_queue row(s)");
            if (highlights > 0) parts.add(highlights + " document_highlights row(s)");
            return String.join(", ", parts);
        }
    }

    /** What {@code source} still holds that deleting it would destroy; see {@link Residue}. One snapshot per statement. */
    public Residue residue(String tenant, String source) {
        return scope.withTenant(tenant, ctx -> {
            var d = CATALOG_DOCUMENTS;
            var c = CATALOG_DOCUMENT_CHUNKS;
            Condition namesSource = DSL.exists(ctx.selectOne().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)).and(c.COLLECTION.eq(source)));
            Condition hasNoManifest = DSL.notExists(ctx.selectOne().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)));
            Condition candidateShape = namesSource.or(hasNoManifest.and(d.PHYSICAL_COLLECTION.eq(source)));
            Condition homedHere = d.PHYSICAL_COLLECTION.eq(source);
            boolean live = ctx.fetchExists(ctx.selectOne().from(d)
                .where(d.TENANT_ID.eq(tenant)).and(d.DELETED_AT.isNull()).and(candidateShape));
            long tombstoned = ctx.fetchCount(d, d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNotNull())
                .and(namesSource.or(homedHere)));
            long liveElsewhere = ctx.fetchCount(d, d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull())
                .and(homedHere).and(DSL.not(candidateShape)));
            long aspects = ctx.fetchCount(DOCUMENT_ASPECTS,
                DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.COLLECTION.eq(source)));
            long queue = ctx.fetchCount(ASPECT_EXTRACTION_QUEUE,
                ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq(tenant).and(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(source)));
            long highlights = ctx.fetchCount(DOCUMENT_HIGHLIGHTS,
                DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(source)));
            return new Residue(live, tombstoned, liveElsewhere, aspects, queue, highlights);
        });
    }

    /**
     * The source's chunks for {@code chashHexes}, as {@code {chash, text, metadata}} maps (the shape
     * {@code CombinedWriteService#moveDocsCombined} takes). A chash the source holds no row for is absent from
     * the result; a row whose text is gone has a null {@code text}, so the caller can tell the two apart from
     * an empty string. {@code metadata} is the stored JSON object.
     */
    public List<Map<String, Object>> chunks(String tenant, String source, Collection<String> chashHexes) {
        if (chashHexes.isEmpty()) {
            return List.of();
        }
        List<byte[]> keys = chashHexes.stream().map(h -> Chash.fromHex(h).toBytes()).toList();
        return scope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : ctx.select(CHUNKS.CHASH, CHUNKS.CHUNK_TEXT, CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant))
                .and(CHUNKS.COLLECTION.eq(source))
                .and(CHUNKS.CHASH.in(keys))
                .fetch()) {
                Map<String, Object> chunk = new LinkedHashMap<>();
                chunk.put("chash", java.util.HexFormat.of().formatHex(r.value1()));
                chunk.put("text", r.value2());
                chunk.put("metadata", metadata(r.value3().data()));
                out.add(chunk);
            }
            return out;
        });
    }

    private static Map<String, Object> metadata(String json) {
        if (json == null || json.isBlank()) {
            return Map.of();
        }
        try {
            return MAPPER.readValue(json, JSON_OBJECT);
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            throw new IllegalStateException("stored chunk metadata is not a JSON object", e);
        }
    }
}
