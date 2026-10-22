// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.Record1;
import org.jooq.Select;
import org.jooq.impl.DSL;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_LINKS;

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
     * What a job on {@code source} would move and what its cutover delete would take, counted before anything
     * runs (RDR-210 Phase 6 Step 3, bead nexus-cnxjk). One snapshot per statement; nothing is written.
     *
     * @param documents               live candidate documents: the ones the job moves
     * @param tombstonedDocuments     soft-deleted documents homed at or naming the source: never moved, and a source
     *                                that holds one ends {@code kept_tombstoned} instead of deleted
     * @param chunks                  distinct chunks a live document of the source owns by a manifest row: the ones
     *                                that are embedded into the target
     * @param ownerlessChunks         chunks of the source no live manifest row names: never moved
     * @param tokenEstimate           the provisional token estimate of {@code chunks}' text, in the unit the runner's
     *                                cap check uses ({@code VoyageEmbedder#estimateTokens}: UTF-8 bytes / 4, at least 1
     *                                for a non-empty text); it overstates by the chunks the target already holds
     * @param quarantineRows          the source's rows in its quarantine siblings, which the source's delete removes
     * @param foreignTopicAssignments topic assignments of OTHER collections that point at the source's topics, which
     *                                the topic delete's cascade removes
     * @param topicLinks              links of the source's topics, removed by the same cascade
     * @param unmatchedSatelliteRows  aspect, queue and highlight rows naming the source whose document the job does not
     *                                move (a tombstoned document's, or one homed here whose chunks are elsewhere):
     *                                the delete refuses to run while any remain unless the job's {@code force_delete} is set
     */
    public record PlanCounts(long documents, long tombstonedDocuments, long chunks, long ownerlessChunks,
                             long tokenEstimate, long quarantineRows, long foreignTopicAssignments, long topicLinks,
                             long unmatchedSatelliteRows) {
    }

    public PlanCounts planCounts(String tenant, String source) {
        return scope.withTenant(tenant, ctx -> {
            var d = CATALOG_DOCUMENTS;
            var c = CATALOG_DOCUMENT_CHUNKS;
            Condition namesSource = DSL.exists(ctx.selectOne().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)).and(c.COLLECTION.eq(source)));
            Condition hasNoManifest = DSL.notExists(ctx.selectOne().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)));
            Condition candidateShape = namesSource.or(hasNoManifest.and(d.PHYSICAL_COLLECTION.eq(source)));
            long documents = ctx.fetchCount(d, d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull()).and(candidateShape));
            long tombstoned = ctx.fetchCount(d, d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNotNull())
                .and(namesSource.or(d.PHYSICAL_COLLECTION.eq(source))));

            // Chunks a live document owns by a manifest row naming the source: what the job embeds.
            var oc = CATALOG_DOCUMENT_CHUNKS.as("plan_owner_chunks");
            var od = CATALOG_DOCUMENTS.as("plan_owner_docs");
            Select<Record1<byte[]>> owned = ctx.selectDistinct(oc.CHASH).from(oc).join(od)
                .on(od.TENANT_ID.eq(oc.TENANT_ID).and(od.TUMBLER.eq(oc.DOC_ID)))
                .where(oc.TENANT_ID.eq(tenant).and(oc.COLLECTION.eq(source)).and(od.DELETED_AT.isNull()));
            long chunks = ctx.fetchCount(owned);
            long stored = ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(source)));

            Field<Integer> bytes = DSL.octetLength(CHUNKS.CHUNK_TEXT);
            Field<Integer> estimate = DSL.when(bytes.isNull().or(bytes.eq(0)), DSL.inline(0))
                .otherwise(DSL.greatest(bytes.div(4), DSL.inline(1)));
            BigDecimal tokens = ctx.select(DSL.coalesce(DSL.sum(estimate), BigDecimal.ZERO)).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(tenant).and(CHUNKS.COLLECTION.eq(source)).and(CHUNKS.CHASH.in(owned)))
                .fetchOne(0, BigDecimal.class);

            long quarantine = QuarantineOrigin.countRowsOf(ctx, tenant, source);

            Select<Record1<Long>> sourceTopics = ctx.select(TOPICS.ID).from(TOPICS)
                .where(TOPICS.TENANT_ID.eq(tenant).and(TOPICS.COLLECTION.eq(source)));
            long foreignAssignments = ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)
                .and(TOPIC_ASSIGNMENTS.TOPIC_ID.in(sourceTopics))
                .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.ne(source)));
            long links = ctx.fetchCount(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID.eq(tenant)
                .and(TOPIC_LINKS.FROM_TOPIC_ID.in(sourceTopics).or(TOPIC_LINKS.TO_TOPIC_ID.in(sourceTopics))));

            // A satellite row belongs to a document by doc_id; one whose document the job does not move stays
            // naming the source, and the delete would take it.
            var md = CATALOG_DOCUMENTS.as("plan_matched_docs");
            var mc = CATALOG_DOCUMENT_CHUNKS.as("plan_matched_chunks");
            Condition matchedShape = DSL.exists(ctx.selectOne().from(mc)
                    .where(mc.TENANT_ID.eq(tenant)).and(mc.DOC_ID.eq(md.TUMBLER)).and(mc.COLLECTION.eq(source)))
                .or(DSL.notExists(ctx.selectOne().from(mc)
                    .where(mc.TENANT_ID.eq(tenant)).and(mc.DOC_ID.eq(md.TUMBLER)))
                    .and(md.PHYSICAL_COLLECTION.eq(source)));
            Function<Field<String>, Condition> moved = docId -> DSL.exists(ctx.selectOne().from(md)
                .where(md.TENANT_ID.eq(tenant)).and(md.TUMBLER.eq(docId)).and(md.DELETED_AT.isNull())
                .and(matchedShape));
            long unmatched =
                ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID.eq(tenant)
                    .and(DOCUMENT_ASPECTS.COLLECTION.eq(source)).and(DSL.not(moved.apply(DOCUMENT_ASPECTS.DOC_ID))))
                + ctx.fetchCount(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq(tenant)
                    .and(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(source))
                    .and(DSL.not(moved.apply(ASPECT_EXTRACTION_QUEUE.DOC_ID))))
                + ctx.fetchCount(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant)
                    .and(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(source))
                    .and(DSL.not(moved.apply(DOCUMENT_HIGHLIGHTS.DOC_ID))));

            return new PlanCounts(documents, tombstoned, chunks, Math.max(0, stored - chunks),
                tokens == null ? 0 : tokens.longValue(), quarantine, foreignAssignments, links, unmatched);
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
