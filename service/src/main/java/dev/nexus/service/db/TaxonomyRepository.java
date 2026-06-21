package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.*;
import static org.jooq.impl.DSL.*;

/**
 * RDR-152 bead nexus-gmiaf.14 — jOOQ-based taxonomy repository.
 *
 * <p>Mirrors {@code CatalogTaxonomy} (SQLite) for the Postgres service tier.
 * All methods route through {@link TenantScope#withTenant} so every row
 * access is stamped with the tenant GUC and enforced by RLS.
 *
 * <p>FTS contract (Store 4, docs/rdr/rdr-152-fts-parity-contract.md):
 * NO FTS — taxonomy has no tsvector/GIN. Topics are queried by exact
 * label/collection equality + doc_count DESC sort only.
 *
 * <p>Import conflict strategy per relay mandate:
 * <ul>
 *   <li>topics.doc_count: GREATEST(EXCLUDED, existing) — PG may be ahead of SQLite snapshot</li>
 *   <li>topics.review_status: EXCLUDED (mutable human annotation; preserve verbatim)</li>
 *   <li>topics.created_at: existing (keep oldest; never overwrite origin timestamp)</li>
 *   <li>topics.label: existing (operator may have renamed; do not clobber live label)</li>
 *   <li>topics.centroid_hash / terms: EXCLUDED (allow ETL to refresh)</li>
 *   <li>topic_assignments.similarity: GREATEST to preserve best projection quality</li>
 *   <li>topic_links.link_count: GREATEST(EXCLUDED, existing) — for the ETL
 *       {@code importTopicLink} path ONLY. The live-compute {@code upsertTopicLink}
 *       path uses EXCLUDED (overwrite) to mirror the oracle's authoritative
 *       full-recompute — see that method's javadoc (RDR-152 nexus-1di3r.4).</li>
 *   <li>taxonomy_meta counters: GREATEST(EXCLUDED, existing)</li>
 * </ul>
 */
public final class TaxonomyRepository {

    private static final Logger log = LoggerFactory.getLogger(TaxonomyRepository.class);

    static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

    private final TenantScope tenantScope;

    public TaxonomyRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    static OffsetDateTime parseTs(String s) {
        if (s == null || s.isBlank()) return OffsetDateTime.now(ZoneOffset.UTC);
        try {
            return OffsetDateTime.parse(s.endsWith("Z") ? s.replace("Z", "+00:00") : s);
        } catch (DateTimeParseException e) {
            log.warn("event=taxonomy_parse_ts_failed raw=\"{}\"", s);
            return OffsetDateTime.now(ZoneOffset.UTC);
        }
    }

    static OffsetDateTime parseTsStrict(String s) {
        if (s == null || s.isBlank())
            throw new IllegalArgumentException("taxonomy import: timestamp is required but was blank");
        try {
            return OffsetDateTime.parse(s.endsWith("Z") ? s.replace("Z", "+00:00") : s);
        } catch (DateTimeParseException e) {
            throw new IllegalArgumentException(
                "taxonomy import: malformed timestamp (must be ISO-8601): " + s, e);
        }
    }

    private static String fmtTs(OffsetDateTime dt) {
        return dt == null ? null : dt.format(UTC_SECOND);
    }

    /**
     * RDR-156 P0.2: ensure catalog_collections has a stub row for the given collection
     * before any topic_assignment write that carries source_collection.
     * Idempotent — ON CONFLICT DO NOTHING.
     */
    private static void ensureCollectionRegistered(DSLContext ctx, String tenant, String collection) {
        if (collection == null || collection.isBlank()) return;
        ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .values(tenant, collection)
           .onConflict(CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
           .doNothing()
           .execute();
    }

    // ⚠ DRIFT RISK (RDR-164 review S4): several ON CONFLICT DO UPDATE sites below
    // (mergeTopics, assignTopic, recordDiscoverCount, importAssignment, importTaxonomyMeta,
    // computeIcfRows) use inline field("...GREATEST/COALESCE/CASE/EXCLUDED...", Type.class)
    // fragments that embed literal column names jOOQ codegen cannot type-check (no typed API
    // for the EXCLUDED pseudo-table or cross-row GREATEST). Referenced columns:
    // topic_assignments.{similarity,assigned_at,assigned_by,source_collection},
    // taxonomy_meta.{last_discover_doc_count,last_discover_at}. If any is renamed in a
    // Liquibase changelog, these strings compile but fail at runtime — update them at each site.

    private static Map<String, Object> buildTopicMap(org.jooq.Record r) {
        var m = new LinkedHashMap<String, Object>();
        m.put("id",            r.get(TOPICS.ID));
        m.put("label",         r.get(TOPICS.LABEL));
        m.put("parent_id",     r.get(TOPICS.PARENT_ID));
        m.put("collection",    r.get(TOPICS.COLLECTION));
        m.put("centroid_hash", r.get(TOPICS.CENTROID_HASH));
        m.put("doc_count",     r.get(TOPICS.DOC_COUNT));
        Object ca = r.get(TOPICS.CREATED_AT);
        m.put("created_at",    ca instanceof OffsetDateTime odt ? odt.format(UTC_SECOND) : (String) ca);
        m.put("review_status", r.get(TOPICS.REVIEW_STATUS));
        m.put("terms",         r.get(TOPICS.TERMS));
        return Collections.unmodifiableMap(m);
    }

    // ── Topics ─────────────────────────────────────────────────────────────────

    /** Return root topics (parent_id IS NULL), ordered by doc_count DESC. */
    public List<Map<String, Object>> getRootTopics(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(
                    TOPICS.ID, TOPICS.LABEL, TOPICS.PARENT_ID, TOPICS.COLLECTION,
                    TOPICS.CENTROID_HASH, TOPICS.DOC_COUNT, TOPICS.CREATED_AT,
                    TOPICS.REVIEW_STATUS, TOPICS.TERMS)
               .from(TOPICS)
               .where(TOPICS.PARENT_ID.isNull())
               .orderBy(TOPICS.DOC_COUNT.desc())
               .fetch()
               .map(TaxonomyRepository::buildTopicMap));
    }

    /** Return children of a topic, ordered by doc_count DESC. */
    public List<Map<String, Object>> getChildTopics(String tenant, long parentId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(
                    TOPICS.ID, TOPICS.LABEL, TOPICS.PARENT_ID, TOPICS.COLLECTION,
                    TOPICS.CENTROID_HASH, TOPICS.DOC_COUNT, TOPICS.CREATED_AT,
                    TOPICS.REVIEW_STATUS, TOPICS.TERMS)
               .from(TOPICS)
               .where(TOPICS.PARENT_ID.eq(parentId))
               .orderBy(TOPICS.DOC_COUNT.desc())
               .fetch()
               .map(TaxonomyRepository::buildTopicMap));
    }

    /** Return all topics, optionally filtered by collection, ordered by doc_count DESC. */
    public List<Map<String, Object>> getAllTopics(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var q = ctx.select(
                    TOPICS.ID, TOPICS.LABEL, TOPICS.PARENT_ID, TOPICS.COLLECTION,
                    TOPICS.CENTROID_HASH, TOPICS.DOC_COUNT, TOPICS.CREATED_AT,
                    TOPICS.REVIEW_STATUS, TOPICS.TERMS)
               .from(TOPICS);
            if (collection != null && !collection.isBlank())
                return q.where(TOPICS.COLLECTION.eq(collection))
                        .orderBy(TOPICS.DOC_COUNT.desc())
                        .fetch()
                        .map(TaxonomyRepository::buildTopicMap);
            return q.orderBy(TOPICS.DOC_COUNT.desc())
                    .fetch()
                    .map(TaxonomyRepository::buildTopicMap);
        });
    }

    /** Return topics with review_status='pending', ordered by doc_count DESC. */
    public List<Map<String, Object>> getUnreviewedTopics(String tenant, String collection, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var q = ctx.select(
                    TOPICS.ID, TOPICS.LABEL, TOPICS.PARENT_ID, TOPICS.COLLECTION,
                    TOPICS.CENTROID_HASH, TOPICS.DOC_COUNT, TOPICS.CREATED_AT,
                    TOPICS.REVIEW_STATUS, TOPICS.TERMS)
               .from(TOPICS)
               .where(collection != null && !collection.isBlank()
                   ? TOPICS.REVIEW_STATUS.eq("pending").and(TOPICS.COLLECTION.eq(collection))
                   : TOPICS.REVIEW_STATUS.eq("pending"))
               .orderBy(TOPICS.DOC_COUNT.desc())
               .limit(limit);
            return q.fetch().map(TaxonomyRepository::buildTopicMap);
        });
    }

    /** Return a single topic by id, or empty. */
    public Optional<Map<String, Object>> getTopicById(String tenant, long id) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(
                    TOPICS.ID, TOPICS.LABEL, TOPICS.PARENT_ID, TOPICS.COLLECTION,
                    TOPICS.CENTROID_HASH, TOPICS.DOC_COUNT, TOPICS.CREATED_AT,
                    TOPICS.REVIEW_STATUS, TOPICS.TERMS)
               .from(TOPICS)
               .where(TOPICS.ID.eq(id))
               .fetch()
               .map(TaxonomyRepository::buildTopicMap)
               .stream().findFirst());
    }

    /** Resolve topic label to id (exact match). Optionally scoped by collection. */
    public Optional<Long> resolveLabel(String tenant, String label, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var q = ctx.select(TOPICS.ID)
                .from(TOPICS)
                .where(collection != null && !collection.isBlank()
                    ? TOPICS.LABEL.eq(label).and(TOPICS.COLLECTION.eq(collection))
                    : TOPICS.LABEL.eq(label))
                .limit(1);
            return q.fetch().stream().findFirst().map(r -> r.get(TOPICS.ID));
        });
    }

    /** Return distinct collection names that have at least one topic. */
    public List<String> getDistinctCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectDistinct(TOPICS.COLLECTION)
               .from(TOPICS)
               .orderBy(TOPICS.COLLECTION)
               .fetch()
               .map(r -> r.get(TOPICS.COLLECTION)));
    }

    /** Insert a new topic row. Returns the generated id. */
    public long insertTopic(String tenant, String label, Long parentId,
                             String collection, int docCount, String createdAt,
                             String terms) {
        OffsetDateTime createdAtTs = parseTs(createdAt);
        return tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            return ctx.insertInto(TOPICS,
                    TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.PARENT_ID,
                    TOPICS.COLLECTION, TOPICS.DOC_COUNT, TOPICS.CREATED_AT,
                    TOPICS.REVIEW_STATUS, TOPICS.TERMS)
                .values(tenant, label, parentId, collection, docCount, createdAtTs, "pending", terms)
                .returningResult(TOPICS.ID)
                .fetchOne()
                .get(TOPICS.ID);
        });
    }

    /** Update topic label without changing review_status. */
    public void updateTopicLabel(String tenant, long topicId, String newLabel) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(TOPICS)
               .set(TOPICS.LABEL, newLabel)
               .where(TOPICS.ID.eq(topicId))
               .execute();
            return null;
        });
    }

    /** Rename topic and mark as accepted. */
    public void renameTopic(String tenant, long topicId, String newLabel) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(TOPICS)
               .set(TOPICS.LABEL, newLabel)
               .set(TOPICS.REVIEW_STATUS, "accepted")
               .where(TOPICS.ID.eq(topicId))
               .execute();
            return null;
        });
    }

    /** Update review_status. */
    public void markTopicReviewed(String tenant, long topicId, String status) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(TOPICS)
               .set(TOPICS.REVIEW_STATUS, status)
               .where(TOPICS.ID.eq(topicId))
               .execute();
            return null;
        });
    }

    // RDR-154 P0 (nexus-i7ivk): updateDocCount() removed. doc_count is now
    // maintained solely by the trg_topic_assignments_doc_count_{ins,del}
    // statement-level triggers; an app-side resync would re-introduce the
    // split-maintenance drift the trigger exists to eliminate.

    /**
     * Pure read: count assignments for a topic. RDR-154 P0 (nexus-i7ivk):
     * doc_count is now trigger-maintained — do NOT feed this value into any
     * topics.doc_count write; the topic_assignments triggers are the sole writer.
     */
    public int countAssignments(String tenant, long topicId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.selectCount()
               .from(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId))
               .fetchOne(0, Integer.class));
    }

    /**
     * Delete a topic and its assignments (cascade via FK).
     * Returns the collection name so the caller can clean the chroma centroid.
     */
    public Optional<String> deleteTopic(String tenant, long topicId) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(TOPICS.COLLECTION)
                .from(TOPICS)
                .where(TOPICS.ID.eq(topicId))
                .fetch();
            if (rows.isEmpty()) return Optional.<String>empty();
            String collection = rows.get(0).get(TOPICS.COLLECTION);
            // Assignments cascade via FK ON DELETE CASCADE
            ctx.deleteFrom(TOPICS).where(TOPICS.ID.eq(topicId)).execute();
            return Optional.of(collection);
        });
    }

    /**
     * Merge source topic into target (T2 half only — caller handles chroma centroid cleanup).
     * Returns the source topic's collection name.
     */
    public Optional<String> mergeTopics(String tenant, long sourceId, long targetId) {
        if (sourceId == targetId) return Optional.empty();
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(TOPICS.COLLECTION)
                .from(TOPICS)
                .where(TOPICS.ID.eq(sourceId))
                .fetch();
            if (rows.isEmpty()) return Optional.<String>empty();
            String collection = rows.get(0).get(TOPICS.COLLECTION);

            // Move assignments: prefer higher similarity on conflict.
            // GREATEST(COALESCE(...), COALESCE(...)) + CASE WHEN expressions referencing
            // both EXCLUDED.* and the existing table row are Postgres-specific constructs
            // with no clean typed DSL equivalent; retained as DSL.field() fragments per spec.
            ctx.insertInto(TOPIC_ASSIGNMENTS,
                    TOPIC_ASSIGNMENTS.TENANT_ID,
                    TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID,
                    TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                    TOPIC_ASSIGNMENTS.SIMILARITY,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
               .select(
                    select(
                        TOPIC_ASSIGNMENTS.TENANT_ID,
                        TOPIC_ASSIGNMENTS.DOC_ID,
                        inline(targetId),
                        TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                        TOPIC_ASSIGNMENTS.SIMILARITY,
                        TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                        TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                    .from(TOPIC_ASSIGNMENTS)
                    .where(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(sourceId)))
               .onConflict(
                    TOPIC_ASSIGNMENTS.TENANT_ID,
                    TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID)
               .doUpdate()
               .set(TOPIC_ASSIGNMENTS.SIMILARITY,
                    field("GREATEST(COALESCE(nexus.topic_assignments.similarity, -1.0),"
                        + " COALESCE(EXCLUDED.similarity, -1.0))", Double.class))
               .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                    field("CASE WHEN COALESCE(EXCLUDED.similarity, -1.0)"
                        + " > COALESCE(nexus.topic_assignments.similarity, -1.0)"
                        + " THEN EXCLUDED.assigned_at"
                        + " ELSE nexus.topic_assignments.assigned_at END", OffsetDateTime.class))
               .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    field("CASE WHEN COALESCE(EXCLUDED.similarity, -1.0)"
                        + " > COALESCE(nexus.topic_assignments.similarity, -1.0)"
                        + " THEN EXCLUDED.source_collection"
                        + " ELSE nexus.topic_assignments.source_collection END", String.class))
               .execute();

            ctx.deleteFrom(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(sourceId))
               .execute();

            // RDR-154 P0 (nexus-i7ivk): no manual doc_count resync. The assignment
            // move (INSERT) and source purge (DELETE) above each fire the
            // statement-level triggers, which recompute target.doc_count from the
            // live assignment rows; the trigger is the sole writer.
            ctx.deleteFrom(TOPICS).where(TOPICS.ID.eq(sourceId)).execute();

            return Optional.of(collection);
        });
    }

    // ── Assignments ────────────────────────────────────────────────────────────

    /**
     * Upsert a topic assignment.
     * - projection rows: MAX(existing.similarity, incoming.similarity)
     * - non-projection rows: INSERT OR IGNORE (idempotent)
     */
    public void assignTopic(String tenant, String docId, long topicId,
                             String assignedBy, Double similarity,
                             String sourceCollection, String assignedAt) {
        tenantScope.withTenant(tenant, ctx -> {
            if ("projection".equals(assignedBy)) {
                // RDR-156 P0.2: ensure collection is registered before the assignment write
                ensureCollectionRegistered(ctx, tenant, sourceCollection);
                OffsetDateTime assignedAtTs = assignedAt != null
                    ? parseTs(assignedAt)
                    : OffsetDateTime.now(ZoneOffset.UTC);
                // GREATEST(COALESCE(...), ...) + CASE WHEN EXCLUDED.similarity > ... patterns
                // referencing both EXCLUDED.* and the existing table row are Postgres-specific
                // constructs retained as DSL.field() fragments per spec.
                ctx.insertInto(TOPIC_ASSIGNMENTS,
                        TOPIC_ASSIGNMENTS.TENANT_ID,
                        TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID,
                        TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                        TOPIC_ASSIGNMENTS.SIMILARITY,
                        TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                        TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                   .values(tenant, docId, topicId, "projection", similarity, assignedAtTs, sourceCollection)
                   .onConflict(
                        TOPIC_ASSIGNMENTS.TENANT_ID,
                        TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID)
                   .doUpdate()
                   .set(TOPIC_ASSIGNMENTS.SIMILARITY,
                        field("GREATEST(COALESCE(nexus.topic_assignments.similarity, -1.0),"
                            + " EXCLUDED.similarity)", Double.class))
                   .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                        field("CASE WHEN EXCLUDED.similarity"
                            + " > COALESCE(nexus.topic_assignments.similarity, -1.0)"
                            + " THEN EXCLUDED.assigned_at"
                            + " ELSE nexus.topic_assignments.assigned_at END", OffsetDateTime.class))
                   .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                        field("CASE WHEN EXCLUDED.similarity"
                            + " > COALESCE(nexus.topic_assignments.similarity, -1.0)"
                            + " THEN EXCLUDED.source_collection"
                            + " ELSE nexus.topic_assignments.source_collection END", String.class))
                   .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "projection")
                   .execute();
            } else {
                ctx.insertInto(TOPIC_ASSIGNMENTS,
                        TOPIC_ASSIGNMENTS.TENANT_ID,
                        TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID,
                        TOPIC_ASSIGNMENTS.ASSIGNED_BY)
                   .values(tenant, docId, topicId, assignedBy)
                   .onConflict(
                       TOPIC_ASSIGNMENTS.TENANT_ID,
                       TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID)
                   .doNothing()
                   .execute();
            }
            // RDR-154 P0 (nexus-i7ivk): no manual doc_count resync. A fresh
            // assignment INSERT fires the AFTER INSERT statement-level trigger,
            // which recomputes topics.doc_count from the live rows. (An ON CONFLICT
            // DO NOTHING / DO UPDATE that changes no assignment count leaves
            // doc_count correctly unchanged.) The trigger is the sole writer.
            return null;
        });
    }

    /** Return doc_ids assigned to a topic. limit=0 means no limit. */
    public List<String> getTopicDocIds(String tenant, long topicId, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var q = ctx.select(TOPIC_ASSIGNMENTS.DOC_ID)
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId));
            var rows = (limit > 0 ? q.limit(limit) : q).fetch();
            return rows.map(r -> r.get(TOPIC_ASSIGNMENTS.DOC_ID));
        });
    }

    /** Return {doc_id, topic_id} pairs for given doc_ids. */
    public List<Map<String, Object>> getAssignmentsForDocs(String tenant, List<String> docIds) {
        if (docIds == null || docIds.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID)
               .from(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.DOC_ID.in(docIds))
               .fetch()
               .map(r -> Map.of(
                   "doc_id",   r.get(TOPIC_ASSIGNMENTS.DOC_ID),
                   "topic_id", r.get(TOPIC_ASSIGNMENTS.TOPIC_ID))));
    }

    /** Return doc_ids labeled with a given topic label. */
    public List<String> getDocIdsForLabel(String tenant, String label) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(TOPIC_ASSIGNMENTS.DOC_ID)
               .from(TOPIC_ASSIGNMENTS)
               .join(TOPICS).on(TOPICS.ID.eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
               .where(TOPICS.LABEL.eq(label))
               .fetch()
               .map(r -> r.get(TOPIC_ASSIGNMENTS.DOC_ID)));
    }

    /**
     * Purge topic_assignments for a deleted doc. Matches purge_assignments_for_doc.
     * Returns count of removed assignment rows.
     */
    public int purgeAssignmentsForDoc(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant, ctx -> {
            int removed = ctx.deleteFrom(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.DOC_ID.eq(title)
                    .and(TOPIC_ASSIGNMENTS.TOPIC_ID.in(
                        select(TOPICS.ID).from(TOPICS).where(TOPICS.COLLECTION.eq(project)))))
                .execute();
            ctx.deleteFrom(TOPICS)
               .where(TOPICS.COLLECTION.eq(project)
                   .and(TOPICS.ID.notIn(
                       selectDistinct(TOPIC_ASSIGNMENTS.TOPIC_ID).from(TOPIC_ASSIGNMENTS))))
               .execute();
            return removed;
        });
    }

    /** Purge all taxonomy rows for a collection. */
    public Map<String, Integer> purgeCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var doomedIds = ctx.select(TOPICS.ID)
                .from(TOPICS)
                .where(TOPICS.COLLECTION.eq(collection))
                .fetch()
                .map(r -> r.get(TOPICS.ID));
            int links = 0, assignments = 0;
            if (!doomedIds.isEmpty()) {
                links = ctx.deleteFrom(TOPIC_LINKS)
                    .where(TOPIC_LINKS.FROM_TOPIC_ID.in(doomedIds)
                        .or(TOPIC_LINKS.TO_TOPIC_ID.in(doomedIds)))
                    .execute();
                assignments = ctx.deleteFrom(TOPIC_ASSIGNMENTS)
                    .where(TOPIC_ASSIGNMENTS.TOPIC_ID.in(doomedIds))
                    .execute();
            }
            assignments += ctx.deleteFrom(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection))
                .execute();
            int topics = ctx.deleteFrom(TOPICS)
                .where(TOPICS.COLLECTION.eq(collection))
                .execute();
            int meta = ctx.deleteFrom(TAXONOMY_META)
                .where(TAXONOMY_META.COLLECTION.eq(collection))
                .execute();
            return Map.of("topics", topics, "assignments", assignments, "links", links, "meta", meta);
        });
    }

    /** Rename all taxonomy rows from old to new collection. */
    public Map<String, Integer> renameCollection(String tenant, String oldCol, String newCol) {
        return tenantScope.withTenant(tenant, ctx -> {
            // RDR-164 P1a: the new collection name must be registered before the denorm
            // columns are re-pointed at it (topics/taxonomy_meta carry NOT VALID RESTRICT
            // FKs to catalog_collections; topic_assignments' FK is ON UPDATE CASCADE but
            // the child UPDATE to newCol still requires the value to exist in the registry).
            ensureCollectionRegistered(ctx, tenant, newCol);
            int topics = ctx.update(TOPICS)
                .set(TOPICS.COLLECTION, newCol)
                .where(TOPICS.COLLECTION.eq(oldCol))
                .execute();
            int assignments = ctx.update(TOPIC_ASSIGNMENTS)
                .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, newCol)
                .where(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(oldCol))
                .execute();
            int meta = ctx.update(TAXONOMY_META)
                .set(TAXONOMY_META.COLLECTION, newCol)
                .where(TAXONOMY_META.COLLECTION.eq(oldCol))
                .execute();
            return Map.of("topics", topics, "assignments", assignments, "meta", meta);
        });
    }

    // ── Taxonomy meta ──────────────────────────────────────────────────────────

    /** Record discover count. Matches record_discover_count. */
    public void recordDiscoverCount(String tenant, String collection, int docCount, String discoveredAt) {
        OffsetDateTime discoveredAtTs = parseTs(discoveredAt);
        tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            // GREATEST(existing_col, EXCLUDED.col) — references both the table row and
            // EXCLUDED in the same expression; retained as DSL.field() fragments per spec.
            ctx.insertInto(TAXONOMY_META,
                    TAXONOMY_META.TENANT_ID,
                    TAXONOMY_META.COLLECTION,
                    TAXONOMY_META.LAST_DISCOVER_DOC_COUNT,
                    TAXONOMY_META.LAST_DISCOVER_AT)
               .values(tenant, collection, docCount, discoveredAtTs)
               .onConflict(TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .doUpdate()
               .set(TAXONOMY_META.LAST_DISCOVER_DOC_COUNT,
                    field("GREATEST(nexus.taxonomy_meta.last_discover_doc_count,"
                        + " EXCLUDED.last_discover_doc_count)", Integer.class))
               .set(TAXONOMY_META.LAST_DISCOVER_AT,
                    field("GREATEST(nexus.taxonomy_meta.last_discover_at,"
                        + " EXCLUDED.last_discover_at)", OffsetDateTime.class))
               .execute();
            return null;
        });
    }

    /** Get the last discover doc_count for rebalance check. */
    public Optional<Integer> getLastDiscoverDocCount(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(TAXONOMY_META.LAST_DISCOVER_DOC_COUNT)
                .from(TAXONOMY_META)
                .where(TAXONOMY_META.COLLECTION.eq(collection))
                .fetch();
            return rows.isEmpty() ? Optional.empty()
                : Optional.of(rows.get(0).get(TAXONOMY_META.LAST_DISCOVER_DOC_COUNT));
        });
    }

    // ── Topic links ────────────────────────────────────────────────────────────

    /** Get topic link pairs for a set of topic ids. */
    public List<Map<String, Object>> getTopicLinkPairs(String tenant, List<Long> topicIds) {
        if (topicIds == null || topicIds.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT)
               .from(TOPIC_LINKS)
               .where(TOPIC_LINKS.FROM_TOPIC_ID.in(topicIds)
                   .and(TOPIC_LINKS.TO_TOPIC_ID.in(topicIds)))
               .fetch()
               .map(r -> Map.of(
                   "from_topic_id", r.get(TOPIC_LINKS.FROM_TOPIC_ID),
                   "to_topic_id",   r.get(TOPIC_LINKS.TO_TOPIC_ID),
                   "link_count",    r.get(TOPIC_LINKS.LINK_COUNT))));
    }

    /**
     * Upsert a topic link pair from a live recompute (mirrors the oracle
     * {@code upsert_topic_links} INSERT OR REPLACE, catalog_taxonomy.py:1405).
     *
     * <p>Conflict policy is EXCLUDED (overwrite), NOT GREATEST. The caller
     * ({@code compute_topic_links}) recomputes the COMPLETE, authoritative link
     * count for the pair on every run, so the freshly computed value IS the
     * truth — a GREATEST would floor the stored count at a historical maximum
     * and never reflect a decrement (catalog pruning / topic split). This is the
     * live-compute counterpart to the ETL {@link #importTopicLink} path, which
     * correctly uses GREATEST to avoid clobbering a live PG value that may be
     * ahead of an older SQLite snapshot. Sister recompute methods
     * ({@code generateCooccurrenceLinks}, {@code refreshProjectionLinks}) use
     * EXCLUDED for the same reason (RDR-152 nexus-1di3r.4).
     */
    public void upsertTopicLink(String tenant, long fromId, long toId, int linkCount, String linkTypes) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(TOPIC_LINKS,
                    TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                    TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
               .values(tenant, fromId, toId, linkCount, linkTypes)
               .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
               .doUpdate()
               .set(TOPIC_LINKS.LINK_COUNT, field("EXCLUDED.link_count", Integer.class))
               .set(TOPIC_LINKS.LINK_TYPES, field("EXCLUDED.link_types", String.class))
               .execute();
            return null;
        });
    }

    // ── ICF aggregation ────────────────────────────────────────────────────────

    /** Count distinct source_collections for projection rows (N_effective for ICF). */
    public int countDistinctSourceCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION))
               .from(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
               .fetchOne(0, Integer.class));
    }

    /**
     * Return ICF rows {topic_id, icf_raw} for N_effective>=2.
     * icf_raw = N_effective / DF — caller applies log2.
     *
     * <p>CAST(? AS DOUBLE PRECISION) / COUNT(DISTINCT ...) — the numeric division of a
     * bind value cast to double by an aggregate is expressible via jOOQ arithmetic;
     * retained as a DSL.field() cast fragment for the CAST expression per spec.
     */
    public List<Map<String, Object>> computeIcfRows(String tenant, int nEffective) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(
                    TOPIC_ASSIGNMENTS.TOPIC_ID,
                    field("CAST({0} AS DOUBLE PRECISION)", Double.class, val(nEffective))
                        .div(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION))
                        .as("icf_raw"))
               .from(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
               .groupBy(TOPIC_ASSIGNMENTS.TOPIC_ID)
               .having(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).gt(0))
               .fetch()
               .map(r -> Map.of(
                   "topic_id", r.get(TOPIC_ASSIGNMENTS.TOPIC_ID),
                   "icf_raw",  r.get("icf_raw", Double.class))));
    }

    // ── Top topics / corpus evidence ───────────────────────────────────────────

    /** Return top-N projection topics for a collection. */
    public List<Map<String, Object>> topTopicsForCollection(String tenant, String collection, int topN) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(
                    TOPICS.LABEL,
                    count().as("chunks"),
                    sum(TOPIC_ASSIGNMENTS.SIMILARITY).as("sum_sim"))
               .from(TOPIC_ASSIGNMENTS)
               .join(TOPICS).on(TOPICS.ID.eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection))
                   .and(TOPIC_ASSIGNMENTS.SIMILARITY.isNotNull()))
               .groupBy(TOPIC_ASSIGNMENTS.TOPIC_ID, TOPICS.LABEL)
               .orderBy(sum(TOPIC_ASSIGNMENTS.SIMILARITY).desc(), count().desc())
               .limit(topN)
               .fetch()
               .map(r -> Map.of(
                   "label",          r.get(TOPICS.LABEL),
                   "chunks",         r.get("chunks", Integer.class),
                   "sum_similarity", r.get("sum_sim", Double.class))));
    }

    /** Return max similarity for a doc's projection into a source_collection. */
    public Optional<Double> chunkGroundedIn(String tenant, String docId, String sourceCollection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(max(TOPIC_ASSIGNMENTS.SIMILARITY).as("ms"))
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                    .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(docId))
                    .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(sourceCollection))
                    .and(TOPIC_ASSIGNMENTS.SIMILARITY.isNotNull()))
                .fetch();
            if (rows.isEmpty()) return Optional.<Double>empty();
            Double v = rows.get(0).get("ms", Double.class);
            return v == null ? Optional.<Double>empty() : Optional.of(v);
        });
    }

    /** Return projection count by source_collection. */
    public List<Map<String, Object>> getProjectionCountsByCollection(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, count().as("cnt"))
               .from(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull())
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.ne("")))
               .groupBy(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
               .fetch()
               .map(r -> Map.of(
                   "source_collection", r.get(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION),
                   "count",             r.get("cnt", Integer.class))));
    }

    // ── Fidelity ETL import ────────────────────────────────────────────────────

    /**
     * Fidelity-preserving import for a topics row.
     * Uses OVERRIDING SYSTEM VALUE to preserve the source integer id so
     * FK references in topic_assignments / topic_links remain consistent.
     */
    public long importTopic(String tenant, long srcId, String label, Long parentId,
                             String collection, String centroidHash,
                             int docCount, String createdAt,
                             String reviewStatus, String terms) {
        // BIGSERIAL allows explicit ID insertion without OVERRIDING SYSTEM VALUE
        // (that clause only applies to GENERATED ALWAYS identity columns).
        OffsetDateTime createdAtTs = parseTsStrict(createdAt);
        tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            ctx.insertInto(TOPICS,
                    TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.PARENT_ID,
                    TOPICS.COLLECTION, TOPICS.CENTROID_HASH, TOPICS.DOC_COUNT,
                    TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS, TOPICS.TERMS)
               .values(srcId, tenant, label, parentId, collection, centroidHash,
                       docCount, createdAtTs, reviewStatus, terms)
               .onConflict(TOPICS.ID)
               .doUpdate()
               // RDR-154 P0 (nexus-i7ivk): doc_count is trigger-maintained and
               // is NOT an ETL merge participant. The INSERT branch seeds it for
               // a brand-new topic; on conflict the live (trigger-computed) value
               // is left untouched so a lossy snapshot can never clobber it.
               .set(TOPICS.REVIEW_STATUS, field("EXCLUDED.review_status", String.class))
               .set(TOPICS.CENTROID_HASH, field("EXCLUDED.centroid_hash", String.class))
               .set(TOPICS.TERMS,         field("EXCLUDED.terms",         String.class))
               .execute();
            return null;
        });
        return srcId;
    }

    /** Fidelity-preserving import for a topic_assignments row. */
    /**
     * Fidelity ETL import of one topic_assignments row.
     *
     * <p>doc_id is a CHUNK content-hash (the HDBSCAN taxonomy clusters chunk
     * embeddings), not a document tumbler. fk_ta_catalog_doc was dropped (nexus-sa14p)
     * because it referenced catalog_documents(tumbler) — a different identity space —
     * and could never be satisfied for chash-keyed rows. So this is a plain idempotent
     * insert with no cross-store existence guard.
     *
     * @return always {@code true} (the row is applied). The boolean return is retained
     *         for caller-API stability; nothing skips now that the FK is gone.
     */
    public boolean importAssignment(String tenant, String docId, long topicId,
                                     String assignedBy, Double similarity,
                                     String assignedAt, String sourceCollection) {
        OffsetDateTime assignedAtTs = (assignedAt != null && !assignedAt.isBlank())
            ? parseTsStrict(assignedAt) : null;
        tenantScope.withTenant(tenant, ctx -> {
            // RDR-156 P0.2: ensure collection is registered before the assignment write
            ensureCollectionRegistered(ctx, tenant, sourceCollection);
            // GREATEST(COALESCE(existing, -1.0), COALESCE(EXCLUDED, -1.0)) +
            // CASE WHEN EXCLUDED.assigned_by = 'projection' referencing the existing table row:
            // Postgres-specific; retained as DSL.field() fragments per spec.
            ctx.insertInto(TOPIC_ASSIGNMENTS,
                    TOPIC_ASSIGNMENTS.TENANT_ID,
                    TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID,
                    TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                    TOPIC_ASSIGNMENTS.SIMILARITY,
                    TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
               .values(tenant, docId, topicId, assignedBy, similarity, assignedAtTs, sourceCollection)
               .onConflict(
                    TOPIC_ASSIGNMENTS.TENANT_ID,
                    TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID)
               .doUpdate()
               // Never downgrade 'projection' to 'hdbscan' or similar:
               // keep existing assigned_by unless the incoming row is 'projection'.
               .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                    field("CASE WHEN EXCLUDED.assigned_by = 'projection'"
                        + " THEN 'projection'"
                        + " ELSE nexus.topic_assignments.assigned_by END", String.class))
               .set(TOPIC_ASSIGNMENTS.SIMILARITY,
                    field("GREATEST(COALESCE(nexus.topic_assignments.similarity, -1.0),"
                        + " COALESCE(EXCLUDED.similarity, -1.0))", Double.class))
               .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                    field("EXCLUDED.assigned_at", OffsetDateTime.class))
               .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                    field("EXCLUDED.source_collection", String.class))
               .execute();
            return null;
        });
        return true;
    }

    /** Fidelity-preserving import for a topic_links row. */
    public void importTopicLink(String tenant, long fromId, long toId,
                                 int linkCount, String linkTypes) {
        tenantScope.withTenant(tenant, ctx -> {
            // GREATEST(existing.link_count, EXCLUDED.link_count) — ETL path uses GREATEST
            // to never downgrade a live PG value from a stale SQLite snapshot.
            // GREATEST over two table references is an irreducible plain-SQL fragment.
            ctx.insertInto(TOPIC_LINKS,
                    TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                    TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
               .values(tenant, fromId, toId, linkCount, linkTypes)
               .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
               .doUpdate()
               .set(TOPIC_LINKS.LINK_COUNT,
                    field("GREATEST(nexus.topic_links.link_count, EXCLUDED.link_count)", Integer.class))
               .set(TOPIC_LINKS.LINK_TYPES, field("EXCLUDED.link_types", String.class))
               .execute();
            return null;
        });
    }

    /** Fidelity-preserving import for a taxonomy_meta row. */
    public void importTaxonomyMeta(String tenant, String collection,
                                    int lastDiscoverDocCount, String lastDiscoverAt) {
        OffsetDateTime lastDiscoverAtTs = (lastDiscoverAt != null && !lastDiscoverAt.isBlank())
            ? parseTsStrict(lastDiscoverAt) : null;
        tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            // GREATEST(existing_col, EXCLUDED.col) — references both the table row and
            // EXCLUDED in the same expression; retained as DSL.field() fragments per spec.
            ctx.insertInto(TAXONOMY_META,
                    TAXONOMY_META.TENANT_ID,
                    TAXONOMY_META.COLLECTION,
                    TAXONOMY_META.LAST_DISCOVER_DOC_COUNT,
                    TAXONOMY_META.LAST_DISCOVER_AT)
               .values(tenant, collection, lastDiscoverDocCount, lastDiscoverAtTs)
               .onConflict(TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
               .doUpdate()
               .set(TAXONOMY_META.LAST_DISCOVER_DOC_COUNT,
                    field("GREATEST(nexus.taxonomy_meta.last_discover_doc_count,"
                        + " EXCLUDED.last_discover_doc_count)", Integer.class))
               .set(TAXONOMY_META.LAST_DISCOVER_AT,
                    field("GREATEST(nexus.taxonomy_meta.last_discover_at,"
                        + " EXCLUDED.last_discover_at)", OffsetDateTime.class))
               .execute();
            return null;
        });
    }

    // ── Analytical methods (nexus-gmiaf.14 drop-in completion) ────────────────

    /**
     * Compute ICF map atomically: returns [{topic_id, n_effective, df}] in one transaction.
     * Callers compute log2(n_effective / df) in Python.
     */
    public Map<String, Object> computeIcfMapAtomic(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            int nEffective = ctx.select(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION))
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                    .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
                .fetchOne(0, Integer.class);

            List<Map<String, Object>> rows = new ArrayList<>();
            if (nEffective >= 2) {
                var icfRows = ctx.select(
                        TOPIC_ASSIGNMENTS.TOPIC_ID,
                        countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).as("df"))
                    .from(TOPIC_ASSIGNMENTS)
                    .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                        .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
                    .groupBy(TOPIC_ASSIGNMENTS.TOPIC_ID)
                    .having(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).gt(0))
                    .fetch();
                for (var r : icfRows) {
                    var m = new LinkedHashMap<String, Object>();
                    m.put("topic_id", r.get(TOPIC_ASSIGNMENTS.TOPIC_ID));
                    m.put("df", r.get("df", Integer.class));
                    rows.add(m);
                }
            }
            var result = new LinkedHashMap<String, Object>();
            result.put("n_effective", nEffective);
            result.put("rows", rows);
            return result;
        });
    }

    /**
     * Hub detection data: returns per-topic DF + total_chunks + label + collection + source set.
     * Python-side computes ICF, stopword matching, and score.
     *
     * @param minCollections minimum distinct source_collections (DF threshold)
     */
    public List<Map<String, Object>> detectHubsData(String tenant, int minCollections) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rows = ctx.select(
                    TOPICS.ID.as("topic_id"),
                    TOPICS.LABEL,
                    TOPICS.COLLECTION,
                    countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).as("df"),
                    count().as("total_chunks"),
                    max(TOPIC_ASSIGNMENTS.ASSIGNED_AT).as("last_assigned_at"))
               .from(TOPIC_ASSIGNMENTS)
               .join(TOPICS).on(TOPICS.ID.eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
               .groupBy(TOPICS.ID, TOPICS.LABEL, TOPICS.COLLECTION)
               .having(countDistinct(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION).ge(minCollections))
               .orderBy(count().desc())
               .fetch();

            // Per-hub source collection sets
            var allSources = ctx.select(TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                    .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNotNull()))
                .orderBy(TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                .fetch();

            // Build topic_id -> [source_collection, ...] map
            java.util.Map<Long, List<String>> sourcesMap = new java.util.HashMap<>();
            for (var r : allSources) {
                long tid = r.get(TOPIC_ASSIGNMENTS.TOPIC_ID);
                String sc = r.get(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION);
                sourcesMap.computeIfAbsent(tid, k -> new ArrayList<>()).add(sc);
            }

            List<Map<String, Object>> result = new ArrayList<>();
            for (var r : rows) {
                long tid = r.get("topic_id", Long.class);
                var m = new LinkedHashMap<String, Object>();
                m.put("topic_id", tid);
                m.put("label", r.get(TOPICS.LABEL));
                m.put("collection", r.get(TOPICS.COLLECTION));
                m.put("df", r.get("df", Integer.class));
                m.put("total_chunks", r.get("total_chunks", Integer.class));
                Object lastAt = r.get("last_assigned_at");
                m.put("last_assigned_at", lastAt != null ? lastAt.toString() : null);
                m.put("source_collections", sourcesMap.getOrDefault(tid, List.of()));
                result.add(m);
            }
            return result;
        });
    }

    /**
     * Audit collection: returns similarity distribution data and top receiving hub topics.
     * Python-side computes quantiles; we return sorted similarities + hub rows.
     */
    public Map<String, Object> auditCollectionData(String tenant, String collection, int topN) {
        return tenantScope.withTenant(tenant, ctx -> {
            var simRows = ctx.select(TOPIC_ASSIGNMENTS.SIMILARITY)
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                    .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection))
                    .and(TOPIC_ASSIGNMENTS.SIMILARITY.isNotNull()))
                .orderBy(TOPIC_ASSIGNMENTS.SIMILARITY.asc())
                .fetch();

            List<Double> sims = new ArrayList<>();
            for (var r : simRows) {
                sims.add(r.get(TOPIC_ASSIGNMENTS.SIMILARITY));
            }

            var hubRows = ctx.select(
                    TOPIC_ASSIGNMENTS.TOPIC_ID,
                    TOPICS.LABEL,
                    count().as("chunks"))
               .from(TOPIC_ASSIGNMENTS)
               .join(TOPICS).on(TOPICS.ID.eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
               .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection")
                   .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection)))
               .groupBy(TOPIC_ASSIGNMENTS.TOPIC_ID, TOPICS.LABEL)
               .orderBy(count().desc())
               .limit(topN)
               .fetch();

            List<Map<String, Object>> hubs = new ArrayList<>();
            for (var r : hubRows) {
                var m = new LinkedHashMap<String, Object>();
                m.put("topic_id", r.get(TOPIC_ASSIGNMENTS.TOPIC_ID));
                m.put("label", r.get(TOPICS.LABEL));
                m.put("chunk_count", r.get("chunks", Integer.class));
                hubs.add(m);
            }

            var result = new LinkedHashMap<String, Object>();
            result.put("collection", collection);
            result.put("similarities", sims);
            result.put("hub_rows", hubs);
            return result;
        });
    }

    /**
     * Generate cooccurrence links: find topic pairs sharing docs across different collections.
     * Returns the count of upserted link pairs.
     */
    public int generateCooccurrenceLinks(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // LEAST/GREATEST over column references (a.topic_id, b.topic_id) for
            // canonical pair ordering: these are Postgres-specific aggregate functions
            // applied to column expressions (not per-row scalar), retained as DSL.sql.
            var ta = TOPIC_ASSIGNMENTS.as("a");
            var tb = TOPIC_ASSIGNMENTS.as("b");
            var ta2 = TOPICS.as("ta");
            var tb2 = TOPICS.as("tb");
            var pairs = ctx.select(
                    field("LEAST(a.topic_id, b.topic_id)", Long.class).as("from_id"),
                    field("GREATEST(a.topic_id, b.topic_id)", Long.class).as("to_id"),
                    count().as("cnt"))
               .from(ta)
               .join(tb).on(ta.DOC_ID.eq(tb.DOC_ID))
               .join(ta2).on(ta.TOPIC_ID.eq(ta2.ID))
               .join(tb2).on(tb.TOPIC_ID.eq(tb2.ID))
               .where(ta.TOPIC_ID.lt(tb.TOPIC_ID)
                   .and(ta2.COLLECTION.ne(tb2.COLLECTION)))
               .groupBy(
                   field("LEAST(a.topic_id, b.topic_id)", Long.class),
                   field("GREATEST(a.topic_id, b.topic_id)", Long.class))
               .fetch();

            if (pairs.isEmpty()) return 0;

            for (var r : pairs) {
                long fromId = r.get("from_id", Long.class);
                long toId   = r.get("to_id",   Long.class);
                int  cnt    = r.get("cnt",      Integer.class);
                ctx.insertInto(TOPIC_LINKS,
                        TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                        TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
                   .values(tenant, fromId, toId, cnt, "[\"cooccurrence\"]")
                   .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                   .doUpdate()
                   .set(TOPIC_LINKS.LINK_COUNT, field("EXCLUDED.link_count", Integer.class))
                   .set(TOPIC_LINKS.LINK_TYPES, "[\"cooccurrence\"]")
                   .execute();
            }
            log.info("cooccurrence_links generated count={}", pairs.size());
            return pairs.size();
        });
    }

    /**
     * Refresh projection links: rebuild projection entries in topic_links from assignments.
     * Returns the count of link pairs written/updated.
     */
    public int refreshProjectionLinks(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            var tgt = TOPIC_ASSIGNMENTS.as("tgt");
            var src = TOPIC_ASSIGNMENTS.as("src");
            var rows = ctx.select(
                    src.TOPIC_ID.as("src_id"),
                    tgt.TOPIC_ID.as("tgt_id"),
                    count().as("cnt"))
               .from(tgt)
               .join(src).on(src.DOC_ID.eq(tgt.DOC_ID)
                   .and(src.TOPIC_ID.ne(tgt.TOPIC_ID))
                   .and(src.ASSIGNED_BY.ne("projection")))
               .where(tgt.ASSIGNED_BY.eq("projection"))
               .groupBy(src.TOPIC_ID, tgt.TOPIC_ID)
               .having(count().gt(0))
               .fetch();

            if (rows.isEmpty()) return 0;

            // Canonicalize pair ordering
            java.util.Map<String, Integer> aggregated = new java.util.LinkedHashMap<>();
            for (var r : rows) {
                long s = r.get("src_id", Long.class);
                long t = r.get("tgt_id", Long.class);
                long fromId = Math.min(s, t);
                long toId   = Math.max(s, t);
                String key = fromId + ":" + toId;
                aggregated.merge(key, r.get("cnt", Integer.class), Integer::sum);
            }

            for (var entry : aggregated.entrySet()) {
                String[] parts = entry.getKey().split(":");
                long fromId = Long.parseLong(parts[0]);
                long toId   = Long.parseLong(parts[1]);

                // Fetch existing link_types to merge 'projection' in
                var existing = ctx.select(TOPIC_LINKS.LINK_TYPES)
                    .from(TOPIC_LINKS)
                    .where(TOPIC_LINKS.TENANT_ID.eq(tenant)
                        .and(TOPIC_LINKS.FROM_TOPIC_ID.eq(fromId))
                        .and(TOPIC_LINKS.TO_TOPIC_ID.eq(toId)))
                    .fetch();

                String mergedTypes;
                if (!existing.isEmpty() && existing.get(0).get(TOPIC_LINKS.LINK_TYPES) != null) {
                    String lt = existing.get(0).get(TOPIC_LINKS.LINK_TYPES);
                    if (!lt.contains("\"projection\"")) {
                        // Insert projection into the JSON array
                        mergedTypes = lt.replace("]", ", \"projection\"]")
                                        .replace("[ ", "[")
                                        .replace("[, ", "[\"projection\"]");
                        if (!mergedTypes.contains("projection")) {
                            mergedTypes = "[\"projection\"]";
                        }
                    } else {
                        mergedTypes = lt;
                    }
                } else {
                    mergedTypes = "[\"projection\"]";
                }

                ctx.insertInto(TOPIC_LINKS,
                        TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                        TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
                   .values(tenant, fromId, toId, entry.getValue(), mergedTypes)
                   .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                   .doUpdate()
                   .set(TOPIC_LINKS.LINK_COUNT, field("EXCLUDED.link_count", Integer.class))
                   .set(TOPIC_LINKS.LINK_TYPES,  field("EXCLUDED.link_types",  String.class))
                   .execute();
            }

            log.info("projection_links refreshed count={}", aggregated.size());
            return aggregated.size();
        });
    }

    /**
     * Persist a topic split: delete parent assignments, insert child topics + assignments.
     * Returns list of new child topic IDs.
     *
     * @param topicId      parent topic id
     * @param childSpecs   list of child specs; each has: label, doc_count, created_at, terms_json, doc_ids
     * @param collectionName collection the parent topic belongs to
     */
    @SuppressWarnings("unchecked")
    public List<Long> persistSplit(String tenant, long topicId,
                                    String collectionName,
                                    List<Map<String, Object>> childSpecs) {
        return tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collectionName);
            // Delete parent assignments
            ctx.deleteFrom(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)
                   .and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId)))
               .execute();

            List<Long> childIds = new ArrayList<>();
            for (var spec : childSpecs) {
                String label      = (String) spec.get("label");
                int    docCount   = ((Number) spec.get("doc_count")).intValue();
                String createdAt  = (String) spec.get("created_at");
                String termsJson  = (String) spec.getOrDefault("terms_json", null);
                List<String> docIds = (List<String>) spec.getOrDefault("doc_ids", List.of());

                OffsetDateTime createdAtTs = parseTsStrict(createdAt);
                long childId = ctx.insertInto(TOPICS,
                        TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.PARENT_ID,
                        TOPICS.COLLECTION, TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.TERMS)
                    .values(tenant, label, topicId, collectionName, docCount, createdAtTs, termsJson)
                    .returningResult(TOPICS.ID)
                    .fetchOne()
                    .get(TOPICS.ID);
                childIds.add(childId);

                batchInsertAssignments(ctx, tenant, childId, docIds, "split");
            }

            // RDR-154 P0 (nexus-i7ivk): no manual parent zero-out. The parent's
            // assignments were DELETEd above, firing the AFTER DELETE trigger which
            // recomputes the parent's doc_count to its live value (0). The trigger
            // is the sole writer.

            log.info("persist_split topic_id={} children={}", topicId, childIds.size());
            return childIds;
        });
    }

    // ── RDR-152 nexus-1di3r Phase 3: chroma-free taxonomy persist/read ─────────

    /**
     * Read the pre-rebuild T2 state for {@code rebuild_taxonomy} — the read-only
     * T2 half of oracle {@code CatalogTaxonomy.read_rebuild_old_state}
     * (catalog_taxonomy.py:2960, RDR-151 Phase 3).
     *
     * <p>Pure reads. Returns {@code {old_topic_map:[{id,label,review_status}],
     * manual_assignments:[{doc_id,topic_id}]}}. The chroma centroid half of the
     * oracle method is supplied separately by the centroid-port
     * ({@code get_by_collection}); the Python orchestrator composes the two.
     */
    public Map<String, Object> readRebuildOldState(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            List<Map<String, Object>> oldTopicMap = ctx.select(
                    TOPICS.ID, TOPICS.LABEL, TOPICS.REVIEW_STATUS)
                .from(TOPICS)
                .where(TOPICS.COLLECTION.eq(collection))
                .fetch()
                .map(r -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("id",            r.get(TOPICS.ID));
                    m.put("label",         r.get(TOPICS.LABEL));
                    m.put("review_status", r.get(TOPICS.REVIEW_STATUS));
                    return m;
                });
            List<Map<String, Object>> manualAssignments = ctx.select(
                    TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID)
                .from(TOPIC_ASSIGNMENTS)
                .join(TOPICS).on(TOPICS.ID.eq(TOPIC_ASSIGNMENTS.TOPIC_ID))
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("manual")
                    .and(TOPICS.COLLECTION.eq(collection)))
                .fetch()
                .map(r -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("doc_id",   r.get(TOPIC_ASSIGNMENTS.DOC_ID));
                    m.put("topic_id", r.get(TOPIC_ASSIGNMENTS.TOPIC_ID));
                    return m;
                });
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("old_topic_map", oldTopicMap);
            out.put("manual_assignments", manualAssignments);
            return out;
        });
    }

    /**
     * Apply a rebuild plan — the pure-T2 PERSIST half of oracle
     * {@code persist_rebuild_topics} (catalog_taxonomy.py:3140, RDR-151 Phase 3).
     *
     * <p>ONE transaction: DELETE old topics + assignments for {@code collection},
     * INSERT the new spec rows (+ their {@code INSERT OR IGNORE} chunk
     * assignments), then apply {@code manualTransfers} ({@code doc_id ->
     * spec_index} into the freshly generated topic_ids, {@code assigned_by =
     * 'manual'}). Returns the new topic_ids aligned to {@code specs} order.
     *
     * <p>REPLACE semantics: the old rows are cleared even when {@code specs} is
     * empty (the {@code < 5} docs / all-noise case), matching the monolithic
     * {@code rebuild_taxonomy}'s unconditional clear. A non-atomic Python
     * delete+insert loop cannot preserve this; hence a batch endpoint.
     */
    @SuppressWarnings("unchecked")
    public List<Long> persistRebuildTopics(String tenant, String collection,
                                            List<Map<String, Object>> specs,
                                            Map<String, Object> manualTransfers) {
        List<Map<String, Object>> safeSpecs = specs == null ? List.of() : specs;
        Map<String, Object> transfers = manualTransfers == null ? Map.of() : manualTransfers;
        return tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            // REPLACE semantics — clear old rows even when there are no new specs.
            ctx.deleteFrom(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.TOPIC_ID.in(
                   select(TOPICS.ID).from(TOPICS).where(TOPICS.COLLECTION.eq(collection))))
               .execute();
            ctx.deleteFrom(TOPICS).where(TOPICS.COLLECTION.eq(collection)).execute();

            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            List<Long> topicIds = new ArrayList<>();
            for (var spec : safeSpecs) {
                String label        = (String) spec.get("label");
                int    docCount     = ((Number) spec.get("doc_count")).intValue();
                String terms        = (String) spec.getOrDefault("terms", null);
                String reviewStatus = (String) spec.getOrDefault("review_status", "pending");
                String assignedBy   = (String) spec.getOrDefault("assigned_by", "hdbscan");
                List<String> docIds = (List<String>) spec.getOrDefault("doc_ids", List.of());

                long topicId = ctx.insertInto(TOPICS,
                        TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                        TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.TERMS, TOPICS.REVIEW_STATUS)
                    .values(tenant, label, collection, docCount, now, terms, reviewStatus)
                    .returningResult(TOPICS.ID)
                    .fetchOne()
                    .get(TOPICS.ID);
                topicIds.add(topicId);

                batchInsertAssignments(ctx, tenant, topicId, docIds, assignedBy);
            }

            // Manual transfers are intentionally NOT batched (nexus-eh89h): they
            // use ON CONFLICT DO UPDATE (distinct from the helper's DO NOTHING) and
            // are sparse (curated reassignments, expected well under ~100 per
            // rebuild), so the per-row trigger cost is immaterial. If a bulk
            // manual-transfer path ever emerges, batch it with a DO UPDATE variant.
            for (var e : transfers.entrySet()) {
                int specIndex = ((Number) e.getValue()).intValue();
                if (specIndex >= 0 && specIndex < topicIds.size()) {
                    ctx.insertInto(TOPIC_ASSIGNMENTS,
                            TOPIC_ASSIGNMENTS.TENANT_ID,
                            TOPIC_ASSIGNMENTS.DOC_ID,
                            TOPIC_ASSIGNMENTS.TOPIC_ID,
                            TOPIC_ASSIGNMENTS.ASSIGNED_BY)
                       .values(tenant, e.getKey(), topicIds.get(specIndex), "manual")
                       .onConflict(
                           TOPIC_ASSIGNMENTS.TENANT_ID,
                           TOPIC_ASSIGNMENTS.DOC_ID,
                           TOPIC_ASSIGNMENTS.TOPIC_ID)
                       .doUpdate()
                       .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "manual")
                       .execute();
                }
            }
            log.info("persist_rebuild collection={} topics={}", collection, topicIds.size());
            return topicIds;
        });
    }

    /**
     * Persist discovered topic specs — the pure-T2 PERSIST half of oracle
     * {@code persist_discovered_topics} (catalog_taxonomy.py:1996, RDR-151
     * Phase 3).
     *
     * <p>ONE transaction: the existing-topics guard (COUNT topics WHERE
     * collection; return {@code []} no-op if any exist, matching the monolithic
     * {@code discover_topics} skip), then INSERT each spec (+ its {@code INSERT
     * OR IGNORE} chunk assignments). Returns topic_ids aligned to {@code specs}
     * order. The batch endpoint preserves the guard atomically vs a TOCTOU
     * Python count+loop.
     */
    @SuppressWarnings("unchecked")
    public List<Long> persistDiscoveredTopics(String tenant, String collection,
                                               List<Map<String, Object>> specs) {
        if (specs == null || specs.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx -> {
            ensureCollectionRegistered(ctx, tenant, collection);
            int existing = ctx.selectCount()
                .from(TOPICS)
                .where(TOPICS.COLLECTION.eq(collection))
                .fetchOne(0, Integer.class);
            if (existing > 0) {
                log.info("discover_skip_existing collection={} existing_topics={}",
                         collection, existing);
                return List.of();
            }
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            List<Long> topicIds = new ArrayList<>();
            for (var spec : specs) {
                String label        = (String) spec.get("label");
                int    docCount     = ((Number) spec.get("doc_count")).intValue();
                String terms        = (String) spec.getOrDefault("terms", null);
                String assignedBy   = (String) spec.getOrDefault("assigned_by", "hdbscan");
                List<String> docIds = (List<String>) spec.getOrDefault("doc_ids", List.of());

                long topicId = ctx.insertInto(TOPICS,
                        TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                        TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.TERMS)
                    .values(tenant, label, collection, docCount, now, terms)
                    .returningResult(TOPICS.ID)
                    .fetchOne()
                    .get(TOPICS.ID);
                topicIds.add(topicId);

                batchInsertAssignments(ctx, tenant, topicId, docIds, assignedBy);
            }
            log.info("persist_discovered collection={} topics={}", collection, topicIds.size());
            return topicIds;
        });
    }

    /**
     * Insert a topic's assignments in a single multi-row statement (chunked under
     * the PostgreSQL parameter limit) instead of one INSERT per doc_id.
     *
     * <p>RDR-154 P0 follow-on (nexus-eh89h): the {@code doc_count} trigger is
     * statement-level and recomputes a full {@code COUNT(*)} for the affected
     * topic on every firing. A per-row insert loop therefore fired the trigger
     * once per doc_id, each scanning the topic's growing assignment set — O(N^2)
     * per topic on the bulk rebuild / discovery / split paths. Batching collapses
     * that to one trigger firing per chunk (one per topic for any realistic size).
     *
     * <p>{@code ON CONFLICT DO NOTHING} preserves the prior idempotency, including
     * for duplicate doc_ids within a single batch (a self-conflict is skipped, not
     * an error — DO NOTHING, not DO UPDATE).
     *
     * <p>Dynamic multi-row VALUES with variable count is the sole reason this
     * method uses raw SQL string building — jOOQ's typed multi-row INSERT requires
     * a statically-known row count and would require a separate parameter object
     * per row (losing the batch-statement benefit). This is the minimal irreducible
     * plain-SQL fragment per spec.
     */
    private static void batchInsertAssignments(org.jooq.DSLContext ctx, String tenant,
                                               long topicId, List<String> docIds,
                                               String assignedBy) {
        if (docIds == null || docIds.isEmpty()) return;
        // 4 bind params per row → 5000 rows = 20000 params, under PG's Int16
        // Bind-message parameter-count limit of 32767. (A topic with >5000 docs
        // fires the trigger ceil(N/5000) times — still vastly better than per-row;
        // realistic topics are hundreds to low-thousands.)
        final int MAX_ROWS = 5000;
        for (int start = 0; start < docIds.size(); start += MAX_ROWS) {
            List<String> batch = docIds.subList(start, Math.min(start + MAX_ROWS, docIds.size()));
            StringBuilder sql = new StringBuilder(
                "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by) VALUES ");
            List<Object> params = new ArrayList<>(batch.size() * 4);
            for (int i = 0; i < batch.size(); i++) {
                sql.append(i == 0 ? "(?, ?, ?, ?)" : ", (?, ?, ?, ?)");
                params.add(tenant);
                params.add(batch.get(i));
                params.add(topicId);
                params.add(assignedBy);
            }
            sql.append(" ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING");
            ctx.execute(sql.toString(), params.toArray());
        }
    }
}
