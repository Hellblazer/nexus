// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.jooq.Condition;
import org.jooq.DSLContext;
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
import java.util.UUID;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOB_MOVES;
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

    // The document shapes, defined once for the pass, the cutover's checks and the plan, so the plan cannot count a
    // different set than the cutover acts on. Each is a condition on CATALOG_DOCUMENTS (unaliased) rows.

    /** A manifest row of the document names {@code source}. */
    private static Condition namesSource(DSLContext ctx, String tenant, String source) {
        var d = CATALOG_DOCUMENTS;
        var c = CATALOG_DOCUMENT_CHUNKS;
        return DSL.exists(ctx.selectOne().from(c)
            .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)).and(c.COLLECTION.eq(source)));
    }

    /** A candidate's shape: a manifest row names {@code source}, or there is no manifest and the document is homed there. */
    private static Condition candidateShape(DSLContext ctx, String tenant, String source) {
        var d = CATALOG_DOCUMENTS;
        var c = CATALOG_DOCUMENT_CHUNKS;
        Condition hasNoManifest = DSL.notExists(ctx.selectOne().from(c)
            .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)));
        return namesSource(ctx, tenant, source).or(hasNoManifest.and(d.PHYSICAL_COLLECTION.eq(source)));
    }

    /** A soft-deleted document whose manifest names {@code source} or that is homed there. */
    private static Condition tombstonedShape(DSLContext ctx, String tenant, String source) {
        var d = CATALOG_DOCUMENTS;
        return d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNotNull())
            .and(namesSource(ctx, tenant, source).or(d.PHYSICAL_COLLECTION.eq(source)));
    }

    /** A live document homed at {@code source} that is not a candidate: its manifest names only other collections. */
    private static Condition liveHomedElsewhereShape(DSLContext ctx, String tenant, String source) {
        var d = CATALOG_DOCUMENTS;
        return d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull()).and(d.PHYSICAL_COLLECTION.eq(source))
            .and(DSL.not(candidateShape(ctx, tenant, source)));
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
            Field<Integer> rows = DSL.field(ctx.selectCount().from(c)
                .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(d.TUMBLER)));
            return ctx.select(d.TUMBLER, rows).from(d)
                .where(d.TENANT_ID.eq(tenant))
                .and(d.DELETED_AT.isNull())
                .and(d.TUMBLER.gt(afterDocId))
                .and(candidateShape(ctx, tenant, source))
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
     * The aspect, queue and highlight rows naming the source, which the source's delete takes with it. The cutover's
     * residue check reads them; a {@code force_delete} lets them go. (The tombstoned and live-homed documents the
     * delete would also take are met earlier, as {@code kept_tombstoned} and the parity check ({@link Parity}),
     * which no flag lifts.)
     */
    public record Residue(long aspects, long queue, long highlights, long aspectTasks) {

        /** The rows still naming the source: what {@code force_delete} lets the delete take. */
        public long satelliteRows() {
            return aspects + queue + highlights + aspectTasks;
        }

        /** A sentence for {@code last_error}: the satellite rows, by table. */
        public String describeSatellites() {
            List<String> parts = new ArrayList<>();
            if (aspects > 0) parts.add(aspects + " document_aspects row(s)");
            if (queue > 0) parts.add(queue + " aspect_extraction_queue row(s)");
            if (aspectTasks > 0) parts.add(aspectTasks + " aspects task(s)");
            if (highlights > 0) parts.add(highlights + " document_highlights row(s)");
            return String.join(", ", parts);
        }
    }

    public Residue residue(String tenant, String source) {
        return scope.withTenant(tenant, ctx -> new Residue(
            ctx.fetchCount(DOCUMENT_ASPECTS,
                DOCUMENT_ASPECTS.TENANT_ID.eq(tenant).and(DOCUMENT_ASPECTS.COLLECTION.eq(source))),
            ctx.fetchCount(ASPECT_EXTRACTION_QUEUE,
                ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq(tenant).and(ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(source))),
            ctx.fetchCount(DOCUMENT_HIGHLIGHTS,
                DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(tenant).and(DOCUMENT_HIGHLIGHTS.COLLECTION.eq(source))),
            // RDR-235 (nexus-5r2sc.2.6): the unconsumed aspects tasks naming the source; the source's purge deletes them.
            AspectTaskRefs.countNaming(ctx, tenant, source)));
    }

    /**
     * Whether {@code source} has a live candidate this job has not already recorded as skipped: a document that
     * arrived since the job last looked. A candidate the job tried and could not move stays a recorded skip, so
     * the cutover does not send the job round the pass loop again for it.
     */
    public boolean anyUntriedCandidate(String tenant, String source, UUID jobId) {
        return scope.withTenant(tenant, ctx -> {
            var d = CATALOG_DOCUMENTS;
            var m = REEMBED_JOB_MOVES;
            return ctx.fetchExists(ctx.selectOne().from(d)
                .where(d.TENANT_ID.eq(tenant)).and(d.DELETED_AT.isNull()).and(candidateShape(ctx, tenant, source))
                .and(DSL.notExists(ctx.selectOne().from(m)
                    .where(m.TENANT_ID.eq(tenant)).and(m.JOB_ID.eq(jobId)).and(m.DOC_ID.eq(d.TUMBLER))
                    .and(m.OUTCOME.eq(ReembedJobRepository.OUTCOME_SKIPPED)))));
        });
    }

    /** Documents soft-deleted whose manifest names {@code source} or that are homed there: what ends a job {@code kept_tombstoned}. */
    public long tombstonedDocuments(String tenant, String source) {
        return scope.withTenant(tenant, ctx -> ctx.fetchCount(CATALOG_DOCUMENTS, tombstonedShape(ctx, tenant, source)));
    }

    /** How many document ids a {@link Parity} names, so a message points at the rows without listing thousands. */
    static final int PARITY_SAMPLE = 5;

    /**
     * The parity check before the source's delete (RDR-210 Cutover, "Parity before the delete"), as three counts.
     * The delete is irreversible, so it runs only when {@link #holds()}:
     * <ul>
     *   <li>{@code manifestRowsNamingSource}: manifest rows (any document's) whose {@code collection} is the source.
     *       The delete removes them whatever their document is, and each is the only copy of a chunk the source
     *       still holds for that document;</li>
     *   <li>{@code liveHomedAtSource}: live documents whose {@code physical_collection} is the source (a moved
     *       document homed back by a client's registration, a document registered after the pass looked). The delete
     *       removes their catalog row;</li>
     *   <li>{@code movedAgreeing} against {@code documentsMoved}: the documents of this job's move record
     *       (outcome {@code moved}) whose {@code physical_collection} is the target and all of whose manifest rows
     *       (it may have none) name the target. A document purged since its move makes the counts differ, which errs toward keeping
     *       the source.</li>
     * </ul>
     * The three {@code ...DocIds} lists are up to {@link #PARITY_SAMPLE} document ids each, for the message that tells an
     * operator which manifest rows to inspect before any hand delete.
     */
    public record Parity(long manifestRowsNamingSource, long liveHomedAtSource, long movedRecorded,
                         long movedAgreeing, long documentsMoved, String manifestDocIds,
                         String homedDocIds, String disagreeingDocIds) {

        public boolean holds() {
            return manifestRowsNamingSource == 0 && liveHomedAtSource == 0 && movedRecorded == documentsMoved
                && movedAgreeing == documentsMoved;
        }

        /** The counts and the rows to look at, for {@code last_error}. */
        public String describe(String source) {
            StringBuilder sb = new StringBuilder("parity check refused the delete: ")
                .append(manifestRowsNamingSource).append(" manifest row(s) still name the source")
                .append(sample(manifestDocIds)).append("; ")
                .append(liveHomedAtSource).append(" live document(s) are homed at the source")
                .append(sample(homedDocIds)).append("; ")
                .append(movedAgreeing).append(" of ").append(documentsMoved)
                .append(" moved document(s) have their manifest and physical collection on the target");
            if (movedRecorded != documentsMoved) {
                sb.append(" (the move record lists ").append(movedRecorded).append(")");
            }
            sb.append(sample(disagreeingDocIds))
                .append(". Nothing was deleted and the source stays superseded. Inspect those documents' manifest "
                    + "rows before any hand delete of ").append(source)
                .append(": --resume re-runs the pass and the check, --abort leaves the source as it is.");
            return sb.toString();
        }

        private static String sample(String docIds) {
            return docIds.isEmpty() ? "" : " (" + docIds + ")";
        }
    }

    /** The sampled ids as one string, {@code ", ..."} appended when the sample is full (there may be more). */
    private static String sampleOf(List<String> ids) {
        return String.join(", ", ids) + (ids.size() >= PARITY_SAMPLE ? ", ..." : "");
    }

    public Parity parity(String tenant, String source, String target, UUID jobId, long documentsMoved) {
        return scope.withTenant(tenant, ctx -> {
            var d = CATALOG_DOCUMENTS;
            var c = CATALOG_DOCUMENT_CHUNKS;
            var m = REEMBED_JOB_MOVES;
            Condition namesSourceRow = c.TENANT_ID.eq(tenant).and(c.COLLECTION.eq(source));
            long manifestRows = ctx.fetchCount(c, namesSourceRow);
            List<String> manifestDocIds = ctx.selectDistinct(c.DOC_ID).from(c).where(namesSourceRow)
                .orderBy(c.DOC_ID).limit(PARITY_SAMPLE).fetch(c.DOC_ID);

            Condition homed = d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull()).and(d.PHYSICAL_COLLECTION.eq(source));
            long liveHomed = ctx.fetchCount(d, homed);
            List<String> homedDocIds = ctx.select(d.TUMBLER).from(d).where(homed).orderBy(d.TUMBLER)
                .limit(PARITY_SAMPLE).fetch(d.TUMBLER);

            Condition moved = m.TENANT_ID.eq(tenant).and(m.JOB_ID.eq(jobId))
                .and(m.OUTCOME.eq(ReembedJobRepository.OUTCOME_MOVED));
            long recorded = ctx.fetchCount(m, moved);
            // A moved document agrees when its row is homed at the target and every manifest row it has (a document
            // with no chunks has none) names the target.
            Condition agrees = DSL.exists(ctx.selectOne().from(d)
                    .where(d.TENANT_ID.eq(tenant)).and(d.TUMBLER.eq(m.DOC_ID)).and(d.PHYSICAL_COLLECTION.eq(target)))
                .and(DSL.notExists(ctx.selectOne().from(c)
                    .where(c.TENANT_ID.eq(tenant)).and(c.DOC_ID.eq(m.DOC_ID)).and(c.COLLECTION.ne(target))));
            long agreeing = ctx.fetchCount(m, moved.and(agrees));
            List<String> disagreeing = ctx.select(m.DOC_ID).from(m).where(moved.and(DSL.not(agrees)))
                .orderBy(m.DOC_ID).limit(PARITY_SAMPLE).fetch(m.DOC_ID);
            return new Parity(manifestRows, liveHomed, recorded, agreeing, documentsMoved, sampleOf(manifestDocIds),
                sampleOf(homedDocIds), sampleOf(disagreeing));
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
     * @param liveHomedElsewhere      live documents homed at the source whose manifest names only other collections:
     *                                never candidates, so never moved, and the delete removes their catalog row; the
     *                                same count as {@link Residue#liveHomedElsewhere}, so a source that plans clean
     *                                here cannot end {@code kept_residue} for this reason (nexus-bi56e)
     */
    public record PlanCounts(long documents, long tombstonedDocuments, long chunks, long ownerlessChunks,
                             long tokenEstimate, long quarantineRows, long foreignTopicAssignments, long topicLinks,
                             long unmatchedSatelliteRows, long liveHomedElsewhere) {
    }

    public PlanCounts planCounts(String tenant, String source) {
        return scope.withTenant(tenant, ctx -> {
            var d = CATALOG_DOCUMENTS;
            long documents = ctx.fetchCount(d, d.TENANT_ID.eq(tenant).and(d.DELETED_AT.isNull())
                .and(candidateShape(ctx, tenant, source)));
            long tombstoned = ctx.fetchCount(d, tombstonedShape(ctx, tenant, source));
            long liveElsewhere = ctx.fetchCount(d, liveHomedElsewhereShape(ctx, tenant, source));

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

            DeleteCounts removal = deleteCounts(ctx, tenant, source);

            return new PlanCounts(documents, tombstoned, chunks, Math.max(0, stored - chunks),
                tokens == null ? 0 : tokens.longValue(), removal.quarantineRows(), removal.foreignTopicAssignments(),
                removal.topicLinks(), removal.unmatchedSatelliteRows(), liveElsewhere);
        });
    }

    /**
     * What the source's delete takes besides the source's own documents and chunks (RDR-210 Cutover, "What that
     * delete removes"): the four counts {@code --status} shows beside a job whose source still exists, and the
     * plan shows before it starts. One snapshot per statement; nothing is written.
     *
     * @param quarantineRows          the source's rows in its quarantine siblings
     * @param foreignTopicAssignments topic assignments of OTHER collections that point at the source's topics
     * @param topicLinks              links of the source's topics
     * @param unmatchedSatelliteRows  aspect, queue and highlight rows naming the source whose document the job does
     *                                not move
     */
    public record DeleteCounts(long quarantineRows, long foreignTopicAssignments, long topicLinks,
                               long unmatchedSatelliteRows) {
    }

    public DeleteCounts deleteCounts(String tenant, String source) {
        return scope.withTenant(tenant, ctx -> deleteCounts(ctx, tenant, source));
    }

    private static DeleteCounts deleteCounts(DSLContext ctx, String tenant, String source) {
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
                .and(DSL.not(moved.apply(DOCUMENT_HIGHLIGHTS.DOC_ID))))
            // RDR-235 (nexus-5r2sc.2.6): and the unconsumed aspects tasks naming the source whose document the job does not move.
            + AspectTaskRefs.countNamingWhereDoc(ctx, tenant, source, docId -> DSL.not(moved.apply(docId)));
        return new DeleteCounts(quarantine, foreignAssignments, links, unmatched);
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
