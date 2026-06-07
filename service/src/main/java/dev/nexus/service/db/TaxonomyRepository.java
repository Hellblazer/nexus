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
 *   <li>topic_links.link_count: GREATEST(EXCLUDED, existing)</li>
 *   <li>taxonomy_meta counters: GREATEST(EXCLUDED, existing)</li>
 * </ul>
 *
 * <p>NOTE: Uses raw SQL queries via DSLContext.fetch/execute to avoid a
 * chicken-and-egg compile dependency on jOOQ-generated table classes that
 * are themselves generated from this schema. After {@code mvn -Pcodegen},
 * the generated Tables.* classes are available; future refactors may switch
 * to typed references for IDE navigation. Raw SQL is equivalent and safe
 * under RLS (the GUC stamp is applied by TenantScope before each query).
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

    private static Map<String, Object> buildTopicMap(org.jooq.Record r) {
        var m = new LinkedHashMap<String, Object>();
        m.put("id",            r.get("id",             Long.class));
        m.put("label",         r.get("label",          String.class));
        m.put("parent_id",     r.get("parent_id",      Long.class));
        m.put("collection",    r.get("collection",     String.class));
        m.put("centroid_hash", r.get("centroid_hash",  String.class));
        m.put("doc_count",     r.get("doc_count",      Integer.class));
        Object ca = r.get("created_at");
        m.put("created_at",    ca instanceof OffsetDateTime odt ? odt.format(UTC_SECOND) : (String) ca);
        m.put("review_status", r.get("review_status",  String.class));
        m.put("terms",         r.get("terms",          String.class));
        return Collections.unmodifiableMap(m);
    }

    // ── Topics ─────────────────────────────────────────────────────────────────

    /** Return root topics (parent_id IS NULL), ordered by doc_count DESC. */
    public List<Map<String, Object>> getRootTopics(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("""
                SELECT id, label, parent_id, collection, centroid_hash,
                       doc_count, created_at, review_status, terms
                FROM nexus.topics WHERE parent_id IS NULL ORDER BY doc_count DESC
                """).map(TaxonomyRepository::buildTopicMap));
    }

    /** Return children of a topic, ordered by doc_count DESC. */
    public List<Map<String, Object>> getChildTopics(String tenant, long parentId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("""
                SELECT id, label, parent_id, collection, centroid_hash,
                       doc_count, created_at, review_status, terms
                FROM nexus.topics WHERE parent_id = ? ORDER BY doc_count DESC
                """, parentId).map(TaxonomyRepository::buildTopicMap));
    }

    /** Return all topics, optionally filtered by collection, ordered by doc_count DESC. */
    public List<Map<String, Object>> getAllTopics(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            if (collection != null && !collection.isBlank())
                return ctx.fetch("""
                    SELECT id, label, parent_id, collection, centroid_hash,
                           doc_count, created_at, review_status, terms
                    FROM nexus.topics WHERE collection = ? ORDER BY doc_count DESC
                    """, collection).map(TaxonomyRepository::buildTopicMap);
            return ctx.fetch("""
                SELECT id, label, parent_id, collection, centroid_hash,
                       doc_count, created_at, review_status, terms
                FROM nexus.topics ORDER BY doc_count DESC
                """).map(TaxonomyRepository::buildTopicMap);
        });
    }

    /** Return topics with review_status='pending', ordered by doc_count DESC. */
    public List<Map<String, Object>> getUnreviewedTopics(String tenant, String collection, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            if (collection != null && !collection.isBlank())
                return ctx.fetch("""
                    SELECT id, label, parent_id, collection, centroid_hash,
                           doc_count, created_at, review_status, terms
                    FROM nexus.topics WHERE review_status = 'pending' AND collection = ?
                    ORDER BY doc_count DESC LIMIT ?
                    """, collection, limit).map(TaxonomyRepository::buildTopicMap);
            return ctx.fetch("""
                SELECT id, label, parent_id, collection, centroid_hash,
                       doc_count, created_at, review_status, terms
                FROM nexus.topics WHERE review_status = 'pending'
                ORDER BY doc_count DESC LIMIT ?
                """, limit).map(TaxonomyRepository::buildTopicMap);
        });
    }

    /** Return a single topic by id, or empty. */
    public Optional<Map<String, Object>> getTopicById(String tenant, long id) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("""
                SELECT id, label, parent_id, collection, centroid_hash,
                       doc_count, created_at, review_status, terms
                FROM nexus.topics WHERE id = ?
                """, id).map(TaxonomyRepository::buildTopicMap).stream().findFirst());
    }

    /** Resolve topic label to id (exact match). Optionally scoped by collection. */
    public Optional<Long> resolveLabel(String tenant, String label, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            org.jooq.Result<?> r = (collection != null && !collection.isBlank())
                ? ctx.fetch("SELECT id FROM nexus.topics WHERE label = ? AND collection = ? LIMIT 1",
                            label, collection)
                : ctx.fetch("SELECT id FROM nexus.topics WHERE label = ? LIMIT 1", label);
            return r.stream().findFirst().map(row -> row.get("id", Long.class));
        });
    }

    /** Return distinct collection names that have at least one topic. */
    public List<String> getDistinctCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("SELECT DISTINCT collection FROM nexus.topics ORDER BY collection")
               .map(r -> r.get("collection", String.class)));
    }

    /** Insert a new topic row. Returns the generated id. */
    public long insertTopic(String tenant, String label, Long parentId,
                             String collection, int docCount, String createdAt,
                             String terms) {
        String tsStr = fmtTs(parseTs(createdAt));
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetchOne("""
                INSERT INTO nexus.topics
                    (tenant_id, label, parent_id, collection, doc_count, created_at, review_status, terms)
                VALUES (?, ?, ?, ?, ?, ?::timestamptz, 'pending', ?)
                RETURNING id
                """, tenant, label, parentId, collection, docCount, tsStr, terms)
               .get("id", Long.class));
    }

    /** Update topic label without changing review_status. */
    public void updateTopicLabel(String tenant, long topicId, String newLabel) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("UPDATE nexus.topics SET label = ? WHERE id = ?", newLabel, topicId);
            return null;
        });
    }

    /** Rename topic and mark as accepted. */
    public void renameTopic(String tenant, long topicId, String newLabel) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("UPDATE nexus.topics SET label = ?, review_status = 'accepted' WHERE id = ?",
                        newLabel, topicId);
            return null;
        });
    }

    /** Update review_status. */
    public void markTopicReviewed(String tenant, long topicId, String status) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("UPDATE nexus.topics SET review_status = ? WHERE id = ?", status, topicId);
            return null;
        });
    }

    /** Update doc_count for a topic (denormalized cache resync). */
    public void updateDocCount(String tenant, long topicId, int docCount) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("UPDATE nexus.topics SET doc_count = ? WHERE id = ?", docCount, topicId);
            return null;
        });
    }

    /** Count assignments for a topic (used for doc_count resync). */
    public int countAssignments(String tenant, long topicId) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetchOne("SELECT COUNT(*) AS c FROM nexus.topic_assignments WHERE topic_id = ?", topicId)
               .get("c", Integer.class));
    }

    /**
     * Delete a topic and its assignments (cascade via FK).
     * Returns the collection name so the caller can clean the chroma centroid.
     */
    public Optional<String> deleteTopic(String tenant, long topicId) {
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.fetchOne("SELECT collection FROM nexus.topics WHERE id = ?", topicId);
            if (row == null) return Optional.<String>empty();
            String collection = row.get("collection", String.class);
            // Assignments cascade via FK ON DELETE CASCADE
            ctx.execute("DELETE FROM nexus.topics WHERE id = ?", topicId);
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
            var row = ctx.fetchOne("SELECT collection FROM nexus.topics WHERE id = ?", sourceId);
            if (row == null) return Optional.<String>empty();
            String collection = row.get("collection", String.class);

            // Move assignments: prefer higher similarity on conflict
            ctx.execute("""
                INSERT INTO nexus.topic_assignments
                    (tenant_id, doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection)
                SELECT tenant_id, doc_id, ?, assigned_by, similarity, assigned_at, source_collection
                FROM nexus.topic_assignments WHERE topic_id = ?
                ON CONFLICT (tenant_id, doc_id, topic_id) DO UPDATE SET
                    similarity = GREATEST(
                        COALESCE(nexus.topic_assignments.similarity, -1.0),
                        COALESCE(EXCLUDED.similarity, -1.0)),
                    assigned_at = CASE
                        WHEN COALESCE(EXCLUDED.similarity, -1.0) >
                             COALESCE(nexus.topic_assignments.similarity, -1.0)
                        THEN EXCLUDED.assigned_at
                        ELSE nexus.topic_assignments.assigned_at END,
                    source_collection = CASE
                        WHEN COALESCE(EXCLUDED.similarity, -1.0) >
                             COALESCE(nexus.topic_assignments.similarity, -1.0)
                        THEN EXCLUDED.source_collection
                        ELSE nexus.topic_assignments.source_collection END
                """, targetId, sourceId);

            ctx.execute("DELETE FROM nexus.topic_assignments WHERE topic_id = ?", sourceId);

            int newCount = ctx.fetchOne(
                "SELECT COUNT(*) AS c FROM nexus.topic_assignments WHERE topic_id = ?", targetId)
               .get("c", Integer.class);
            ctx.execute("UPDATE nexus.topics SET doc_count = ? WHERE id = ?", newCount, targetId);
            ctx.execute("DELETE FROM nexus.topics WHERE id = ?", sourceId);

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
                String tsStr = fmtTs(assignedAt != null ? parseTs(assignedAt)
                                                        : OffsetDateTime.now(ZoneOffset.UTC));
                ctx.execute("""
                    INSERT INTO nexus.topic_assignments
                        (tenant_id, doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection)
                    VALUES (?, ?, ?, 'projection', ?, ?::timestamptz, ?)
                    ON CONFLICT (tenant_id, doc_id, topic_id) DO UPDATE SET
                        similarity = GREATEST(
                            COALESCE(nexus.topic_assignments.similarity, -1.0),
                            EXCLUDED.similarity),
                        assigned_at = CASE
                            WHEN EXCLUDED.similarity >
                                 COALESCE(nexus.topic_assignments.similarity, -1.0)
                            THEN EXCLUDED.assigned_at
                            ELSE nexus.topic_assignments.assigned_at END,
                        source_collection = CASE
                            WHEN EXCLUDED.similarity >
                                 COALESCE(nexus.topic_assignments.similarity, -1.0)
                            THEN EXCLUDED.source_collection
                            ELSE nexus.topic_assignments.source_collection END,
                        assigned_by = 'projection'
                    """, tenant, docId, topicId, similarity, tsStr, sourceCollection);
            } else {
                ctx.execute("""
                    INSERT INTO nexus.topic_assignments
                        (tenant_id, doc_id, topic_id, assigned_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (tenant_id, doc_id, topic_id) DO NOTHING
                    """, tenant, docId, topicId, assignedBy);
            }
            // Resync doc_count (mirrors nexus-n41p)
            int cnt = ctx.fetchOne(
                "SELECT COUNT(*) AS c FROM nexus.topic_assignments WHERE topic_id = ?", topicId)
               .get("c", Integer.class);
            ctx.execute("UPDATE nexus.topics SET doc_count = ? WHERE id = ?", cnt, topicId);
            return null;
        });
    }

    /** Return doc_ids assigned to a topic. limit=0 means no limit. */
    public List<String> getTopicDocIds(String tenant, long topicId, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            if (limit > 0)
                return ctx.fetch("SELECT doc_id FROM nexus.topic_assignments WHERE topic_id = ? LIMIT ?",
                                 topicId, limit)
                          .map(r -> r.get("doc_id", String.class));
            return ctx.fetch("SELECT doc_id FROM nexus.topic_assignments WHERE topic_id = ?", topicId)
                      .map(r -> r.get("doc_id", String.class));
        });
    }

    /** Return {doc_id, topic_id} pairs for given doc_ids. */
    public List<Map<String, Object>> getAssignmentsForDocs(String tenant, List<String> docIds) {
        if (docIds == null || docIds.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx -> {
            String placeholders = "?,".repeat(docIds.size()).replaceAll(",$", "");
            List<Object> params = new ArrayList<>(docIds);
            return ctx.fetch(
                "SELECT doc_id, topic_id FROM nexus.topic_assignments WHERE doc_id IN ("
                + placeholders + ")", params.toArray())
               .map(r -> Map.of("doc_id", r.get("doc_id", String.class),
                                "topic_id", r.get("topic_id", Long.class)));
        });
    }

    /** Return doc_ids labeled with a given topic label. */
    public List<String> getDocIdsForLabel(String tenant, String label) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("""
                SELECT ta.doc_id FROM nexus.topic_assignments ta
                JOIN nexus.topics t ON t.id = ta.topic_id WHERE t.label = ?
                """, label)
               .map(r -> r.get("doc_id", String.class)));
    }

    /**
     * Purge topic_assignments for a deleted doc. Matches purge_assignments_for_doc.
     * Returns count of removed assignment rows.
     */
    public int purgeAssignmentsForDoc(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant, ctx -> {
            int removed = ctx.execute("""
                DELETE FROM nexus.topic_assignments
                WHERE doc_id = ?
                  AND topic_id IN (SELECT id FROM nexus.topics WHERE collection = ?)
                """, title, project);
            ctx.execute("""
                DELETE FROM nexus.topics
                WHERE collection = ?
                  AND id NOT IN (SELECT DISTINCT topic_id FROM nexus.topic_assignments)
                """, project);
            return removed;
        });
    }

    /** Purge all taxonomy rows for a collection. */
    public Map<String, Integer> purgeCollection(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var doomedIds = ctx.fetch("SELECT id FROM nexus.topics WHERE collection = ?", collection)
                               .map(r -> r.get("id", Long.class));
            int links = 0, assignments = 0;
            if (!doomedIds.isEmpty()) {
                String ph = "?,".repeat(doomedIds.size()).replaceAll(",$", "");
                List<Object> ids = new ArrayList<>(doomedIds);
                List<Object> doubleIds = new ArrayList<>(ids);
                doubleIds.addAll(ids);
                links = ctx.execute("DELETE FROM nexus.topic_links WHERE from_topic_id IN ("
                    + ph + ") OR to_topic_id IN (" + ph + ")", doubleIds.toArray());
                assignments = ctx.execute("DELETE FROM nexus.topic_assignments WHERE topic_id IN ("
                    + ph + ")", ids.toArray());
            }
            assignments += ctx.execute("DELETE FROM nexus.topic_assignments WHERE source_collection = ?",
                                       collection);
            int topics = ctx.execute("DELETE FROM nexus.topics WHERE collection = ?", collection);
            int meta   = ctx.execute("DELETE FROM nexus.taxonomy_meta WHERE collection = ?", collection);
            return Map.of("topics", topics, "assignments", assignments, "links", links, "meta", meta);
        });
    }

    /** Rename all taxonomy rows from old to new collection. */
    public Map<String, Integer> renameCollection(String tenant, String oldCol, String newCol) {
        return tenantScope.withTenant(tenant, ctx -> {
            int topics = ctx.execute("UPDATE nexus.topics SET collection = ? WHERE collection = ?",
                                     newCol, oldCol);
            int assignments = ctx.execute(
                "UPDATE nexus.topic_assignments SET source_collection = ? WHERE source_collection = ?",
                newCol, oldCol);
            int meta = ctx.execute("UPDATE nexus.taxonomy_meta SET collection = ? WHERE collection = ?",
                                   newCol, oldCol);
            return Map.of("topics", topics, "assignments", assignments, "meta", meta);
        });
    }

    // ── Taxonomy meta ──────────────────────────────────────────────────────────

    /** Record discover count. Matches record_discover_count. */
    public void recordDiscoverCount(String tenant, String collection, int docCount, String discoveredAt) {
        String tsStr = fmtTs(parseTs(discoveredAt));
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("""
                INSERT INTO nexus.taxonomy_meta (tenant_id, collection, last_discover_doc_count, last_discover_at)
                VALUES (?, ?, ?, ?::timestamptz)
                ON CONFLICT (tenant_id, collection) DO UPDATE SET
                    last_discover_doc_count = EXCLUDED.last_discover_doc_count,
                    last_discover_at = EXCLUDED.last_discover_at
                """, tenant, collection, docCount, tsStr);
            return null;
        });
    }

    /** Get the last discover doc_count for rebalance check. */
    public Optional<Integer> getLastDiscoverDocCount(String tenant, String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.fetchOne(
                "SELECT last_discover_doc_count FROM nexus.taxonomy_meta WHERE collection = ?",
                collection);
            return row == null ? Optional.empty()
                               : Optional.of(row.get("last_discover_doc_count", Integer.class));
        });
    }

    // ── Topic links ────────────────────────────────────────────────────────────

    /** Get topic link pairs for a set of topic ids. */
    public List<Map<String, Object>> getTopicLinkPairs(String tenant, List<Long> topicIds) {
        if (topicIds == null || topicIds.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx -> {
            String ph = "?,".repeat(topicIds.size()).replaceAll(",$", "");
            List<Object> doubleIds = new ArrayList<>(topicIds);
            doubleIds.addAll(topicIds);
            return ctx.fetch(
                "SELECT from_topic_id, to_topic_id, link_count FROM nexus.topic_links "
                + "WHERE from_topic_id IN (" + ph + ") AND to_topic_id IN (" + ph + ")",
                doubleIds.toArray())
               .map(r -> Map.of(
                   "from_topic_id", r.get("from_topic_id", Long.class),
                   "to_topic_id",   r.get("to_topic_id",   Long.class),
                   "link_count",    r.get("link_count",     Integer.class)));
        });
    }

    /** Upsert a topic link pair (link_count uses GREATEST). */
    public void upsertTopicLink(String tenant, long fromId, long toId, int linkCount, String linkTypes) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("""
                INSERT INTO nexus.topic_links (tenant_id, from_topic_id, to_topic_id, link_count, link_types)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, from_topic_id, to_topic_id) DO UPDATE SET
                    link_count = GREATEST(nexus.topic_links.link_count, EXCLUDED.link_count),
                    link_types = EXCLUDED.link_types
                """, tenant, fromId, toId, linkCount, linkTypes);
            return null;
        });
    }

    // ── ICF aggregation ────────────────────────────────────────────────────────

    /** Count distinct source_collections for projection rows (N_effective for ICF). */
    public int countDistinctSourceCollections(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetchOne("""
                SELECT COUNT(DISTINCT source_collection) AS n
                FROM nexus.topic_assignments
                WHERE assigned_by = 'projection' AND source_collection IS NOT NULL
                """).get("n", Integer.class));
    }

    /**
     * Return ICF rows {topic_id, icf_raw} for N_effective>=2.
     * icf_raw = N_effective / DF — caller applies log2.
     */
    public List<Map<String, Object>> computeIcfRows(String tenant, int nEffective) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("""
                SELECT topic_id,
                       CAST(? AS DOUBLE PRECISION) / COUNT(DISTINCT source_collection) AS icf_raw
                FROM nexus.topic_assignments
                WHERE assigned_by = 'projection' AND source_collection IS NOT NULL
                GROUP BY topic_id
                HAVING COUNT(DISTINCT source_collection) > 0
                """, nEffective)
               .map(r -> Map.of(
                   "topic_id", r.get("topic_id", Long.class),
                   "icf_raw",  r.get("icf_raw",  Double.class))));
    }

    // ── Top topics / corpus evidence ───────────────────────────────────────────

    /** Return top-N projection topics for a collection. */
    public List<Map<String, Object>> topTopicsForCollection(String tenant, String collection, int topN) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("""
                SELECT t.label, COUNT(*) AS chunks, SUM(ta.similarity) AS sum_sim
                FROM nexus.topic_assignments ta
                JOIN nexus.topics t ON t.id = ta.topic_id
                WHERE ta.assigned_by = 'projection'
                  AND ta.source_collection = ?
                  AND ta.similarity IS NOT NULL
                GROUP BY ta.topic_id, t.label
                ORDER BY sum_sim DESC, chunks DESC
                LIMIT ?
                """, collection, topN)
               .map(r -> Map.of(
                   "label",          r.get("label",   String.class),
                   "chunks",         r.get("chunks",  Integer.class),
                   "sum_similarity", r.get("sum_sim", Double.class))));
    }

    /** Return max similarity for a doc's projection into a source_collection. */
    public Optional<Double> chunkGroundedIn(String tenant, String docId, String sourceCollection) {
        return tenantScope.withTenant(tenant, ctx -> {
            var row = ctx.fetchOne("""
                SELECT MAX(similarity) AS ms
                FROM nexus.topic_assignments
                WHERE assigned_by = 'projection'
                  AND doc_id = ? AND source_collection = ?
                  AND similarity IS NOT NULL
                """, docId, sourceCollection);
            if (row == null) return Optional.<Double>empty();
            Double v = row.get("ms", Double.class);
            return v == null ? Optional.<Double>empty() : Optional.of(v);
        });
    }

    /** Return projection count by source_collection. */
    public List<Map<String, Object>> getProjectionCountsByCollection(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.fetch("""
                SELECT source_collection, COUNT(*) AS cnt
                FROM nexus.topic_assignments
                WHERE assigned_by = 'projection'
                  AND source_collection IS NOT NULL AND source_collection != ''
                GROUP BY source_collection
                """)
               .map(r -> Map.of(
                   "source_collection", r.get("source_collection", String.class),
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
        String tsStr = fmtTs(parseTsStrict(createdAt));
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("""
                INSERT INTO nexus.topics
                    (id, tenant_id, label, parent_id, collection, centroid_hash,
                     doc_count, created_at, review_status, terms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?::timestamptz, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    doc_count     = GREATEST(nexus.topics.doc_count, EXCLUDED.doc_count),
                    review_status = EXCLUDED.review_status,
                    centroid_hash = EXCLUDED.centroid_hash,
                    terms         = EXCLUDED.terms
                """, srcId, tenant, label, parentId, collection, centroidHash,
                     docCount, tsStr, reviewStatus, terms);
            return null;
        });
        return srcId;
    }

    /** Fidelity-preserving import for a topic_assignments row. */
    public void importAssignment(String tenant, String docId, long topicId,
                                  String assignedBy, Double similarity,
                                  String assignedAt, String sourceCollection) {
        String tsStr = (assignedAt != null && !assignedAt.isBlank())
            ? fmtTs(parseTsStrict(assignedAt)) : null;
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("""
                INSERT INTO nexus.topic_assignments
                    (tenant_id, doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection)
                VALUES (?, ?, ?, ?, ?, ?::timestamptz, ?)
                ON CONFLICT (tenant_id, doc_id, topic_id) DO UPDATE SET
                    assigned_by       = EXCLUDED.assigned_by,
                    similarity        = GREATEST(
                        COALESCE(nexus.topic_assignments.similarity, -1.0),
                        COALESCE(EXCLUDED.similarity, -1.0)),
                    assigned_at       = EXCLUDED.assigned_at,
                    source_collection = EXCLUDED.source_collection
                """, tenant, docId, topicId, assignedBy, similarity, tsStr, sourceCollection);
            return null;
        });
    }

    /** Fidelity-preserving import for a topic_links row. */
    public void importTopicLink(String tenant, long fromId, long toId,
                                 int linkCount, String linkTypes) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("""
                INSERT INTO nexus.topic_links (tenant_id, from_topic_id, to_topic_id, link_count, link_types)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, from_topic_id, to_topic_id) DO UPDATE SET
                    link_count = GREATEST(nexus.topic_links.link_count, EXCLUDED.link_count),
                    link_types = EXCLUDED.link_types
                """, tenant, fromId, toId, linkCount, linkTypes);
            return null;
        });
    }

    /** Fidelity-preserving import for a taxonomy_meta row. */
    public void importTaxonomyMeta(String tenant, String collection,
                                    int lastDiscoverDocCount, String lastDiscoverAt) {
        String tsStr = (lastDiscoverAt != null && !lastDiscoverAt.isBlank())
            ? fmtTs(parseTsStrict(lastDiscoverAt)) : null;
        tenantScope.withTenant(tenant, ctx -> {
            ctx.execute("""
                INSERT INTO nexus.taxonomy_meta (tenant_id, collection, last_discover_doc_count, last_discover_at)
                VALUES (?, ?, ?, ?::timestamptz)
                ON CONFLICT (tenant_id, collection) DO UPDATE SET
                    last_discover_doc_count =
                        GREATEST(nexus.taxonomy_meta.last_discover_doc_count,
                                 EXCLUDED.last_discover_doc_count),
                    last_discover_at =
                        GREATEST(nexus.taxonomy_meta.last_discover_at,
                                 EXCLUDED.last_discover_at)
                """, tenant, collection, lastDiscoverDocCount, tsStr);
            return null;
        });
    }
}
