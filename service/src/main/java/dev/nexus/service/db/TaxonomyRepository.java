package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static dev.nexus.service.db.JsonbSupport.jsonbRequired;
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

    /**
     * Serialize taxonomy persist operations per (tenant, collection) within the
     * CURRENT transaction (nexus-n2ls1).
     *
     * <p>The persist paths' existing-topics guard is a plain unlocked SELECT
     * COUNT: two concurrent persists for the same (tenant, collection) both
     * counted 0 under READ COMMITTED, both inserted the same root label, and
     * the loser hit the taxonomy-004 partial unique index (SQLSTATE 23505 →
     * HTTP 409, observed live 2026-07-07). {@code pg_advisory_xact_lock}
     * blocks the second persist until the first commits, so the loser then
     * sees the winner's committed rows and takes the guard-skip (discover) or
     * a fresh replace (rebuild). Released automatically at commit/rollback.
     * A {@code hashtext} collision between two different keys merely
     * over-serializes — never a correctness issue.
     */
    private static void lockTaxonomyCollection(DSLContext ctx, String tenant, String collection) {
        // Bounded wait FIRST (critique, nexus-n2ls1): without a lock_timeout a
        // stuck holder turns the fast retryable 409 this fix removes into an
        // indefinite in-transaction wait surfacing as an edge 504 — worse
        // observability than the original bug. set_config(..., true) is
        // SET LOCAL (txn-scoped, auto-reverts at commit/rollback); a timed-out
        // acquire raises SQLSTATE 55P03, a clean retryable error. The timeout
        // also bounds this txn's subsequent row-lock waits — acceptable, these
        // persist txns are short writes.
        ctx.select(function("set_config", String.class,
                   val("lock_timeout"), val("5000"), val(true)))
           .fetch();
        // Typed-DSL function composition (house rule: no raw string-SQL —
        // RawSqlGateTest): SELECT pg_advisory_xact_lock(hashtext(:key)).
        // hashtext returns int4, implicitly widened to the bigint overload.
        ctx.select(function("pg_advisory_xact_lock", Object.class,
                   function("hashtext", Integer.class, val(tenant + "/" + collection))))
           .fetch();
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
            assignOne(ctx, tenant, docId, topicId, assignedBy, similarity, sourceCollection, assignedAt);
            return null;
        });
    }

    /**
     * Batch upsert of topic assignments (bead nexus-71988). Loops the SAME two
     * insert shapes as {@link #assignTopic} inside ONE
     * {@link TenantScope#withTenant} transaction (GUC {@code nexus.tenant} set
     * once). Projection rows keep best-similarity-wins; non-projection rows are
     * dup-safe DO NOTHING. doc_count stays trigger-maintained (RDR-154) — the
     * per-row INSERTs fire the statement-level triggers, no manual resync.
     * Empty list is a no-op.
     *
     * @param rows each carries doc_id/topic_id/assigned_by (required) and
     *             optional similarity/source_collection/assigned_at.
     * @return number of rows submitted.
     */
    public int assignMany(String tenant, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        // nexus-ps9wb: assignMany accumulates one row lock per assignOne across the
        // whole transaction, in arrival order. Two concurrent assignMany calls (the
        // flush-grain assign_many hook under multi-worker indexing) that touch
        // overlapping (doc_id, topic_id) keys in different orders deadlock (SQLSTATE
        // 40P01) — the same class as PgVectorRepository.upsertChunks. Sort rows by the
        // (tenant constant) conflict key so every concurrent batch locks in one global
        // order. Copy first — the caller's list may be immutable.
        List<Map<String, Object>> ordered = new ArrayList<>(rows);
        ordered.sort(Comparator
                .comparing((Map<String, Object> r) -> reqS(r, "doc_id"))
                .thenComparing(r -> reqL(r, "topic_id")));
        // Belt: retry a residual cross-path deadlock; idempotent upserts, victim
        // already rolled back → safe.
        return DeadlockRetry.run("taxonomy.assignMany", () -> tenantScope.withTenant(tenant, ctx -> {
            // Review #5: register each distinct source_collection ONCE per
            // batch instead of one idempotent INSERT per projection row
            // (batches routinely share a single collection).
            ordered.stream()
                .filter(r -> "projection".equals(r.get("assigned_by")))
                .map(r -> optS(r, "source_collection"))
                .filter(c -> c != null && !c.isBlank())
                .distinct()
                .forEach(c -> ensureCollectionRegistered(ctx, tenant, c));
            for (Map<String, Object> r : ordered) {
                assignOne(ctx, tenant, reqS(r, "doc_id"), reqL(r, "topic_id"),
                          reqS(r, "assigned_by"), optD(r, "similarity"),
                          optS(r, "source_collection"), optS(r, "assigned_at"),
                          false);
            }
            return ordered.size();
        }));
    }

    /**
     * Shared single-assignment upsert body used by both {@link #assignTopic}
     * (one row per transaction) and {@link #assignMany} (N rows per
     * transaction). Assumes {@code ctx} is already scoped to {@code tenant}.
     */
    private static void assignOne(DSLContext ctx, String tenant, String docId, long topicId,
                                  String assignedBy, Double similarity,
                                  String sourceCollection, String assignedAt) {
        assignOne(ctx, tenant, docId, topicId, assignedBy, similarity,
                  sourceCollection, assignedAt, true);
    }

    private static void assignOne(DSLContext ctx, String tenant, String docId, long topicId,
                                  String assignedBy, Double similarity,
                                  String sourceCollection, String assignedAt,
                                  boolean ensureCollection) {
        if ("projection".equals(assignedBy)) {
            // RDR-156 P0.2: ensure collection is registered before the assignment
            // write (assignMany pre-registers distinct collections and passes false)
            if (ensureCollection) ensureCollectionRegistered(ctx, tenant, sourceCollection);
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
    }

    /** Upper bound on chashes accepted by {@link #assignFromChashes} per call
     * (mirrors {@code assign_many}'s {@code MAX_ASSIGN_MANY} cap — one flush's
     * worth of chashes, never an unbounded batch). */
    public static final int MAX_ASSIGN_FROM_CHASHES = 1000;

    /**
     * POST /v1/taxonomy/assignments/assign_from_chashes (nexus-lns3o engine half,
     * T2 [21580] flush-tail-attribution-2026-08-07 NEW-A).
     *
     * <p>Server-side replacement for the client's two-pass
     * {@code HttpTaxonomyStore.compute_assignments} + {@code persist_assignments}
     * dance. The engine already holds both the embeddings ({@code chunks_<dim>},
     * just upserted by the SAME flush) and the centroids
     * ({@code taxonomy_centroids_<dim>}, RDR-156), so compute-and-persist
     * collapses into ONE server-side round trip via
     * {@code nexus.assign_from_chashes_<dim>} (taxonomy-006) — eliminating the
     * per-flush ~3MB embedding re-download the client compute path required.
     *
     * <p>PARITY (pinned by {@code TaxonomyAssignFromChashesRepositoryTest},
     * derived from {@code HttpTaxonomyStore.compute_assignments} directly — see
     * that test's class javadoc for the line-by-line derivation): argmax cosine
     * similarity, NO minimum-similarity threshold; own pass persists
     * {@code assigned_by='centroid'} with similarity/source_collection always
     * NULL, {@code ON CONFLICT DO NOTHING}; cross pass persists
     * {@code assigned_by='projection'} with {@code GREATEST}-wins similarity on
     * conflict. An empty centroid set in scope silently yields zero assignments
     * (normal empty-taxonomy state, matches the client's
     * {@code if not c_embs: return []} short-circuit) — this is DIFFERENT from an
     * unmatched chash (a chash never actually upserted into {@code collection}),
     * which this method names explicitly rather than silently dropping.
     *
     * @param rawChashes    the just-upserted chunk chashes to assign; normalized to
     *                      lowercase hex internally before both the existence probe
     *                      and the SQL call so an uppercase-hex input still probes
     *                      and assigns consistently
     * @param crossCollection whether to also run the foreign-collection
     *                        ("projection") pass; the own-collection
     *                        ("centroid") pass always runs
     * @return {@code {assigned: int, cross_assigned: int, unmatched_chashes: List<String>}}
     * @throws IllegalArgumentException if {@code collection} is not four-segment
     *         conformant (dim cannot be resolved) or {@code rawChashes} exceeds
     *         {@link #MAX_ASSIGN_FROM_CHASHES}
     */
    public Map<String, Object> assignFromChashes(
            String tenant, String collection, List<String> rawChashes, boolean crossCollection) {
        if (rawChashes == null || rawChashes.isEmpty()) {
            Map<String, Object> empty = new LinkedHashMap<>();
            empty.put("assigned", 0);
            empty.put("cross_assigned", 0);
            empty.put("unmatched_chashes", List.of());
            return empty;
        }
        if (rawChashes.size() > MAX_ASSIGN_FROM_CHASHES) {
            throw new IllegalArgumentException(
                "too many chashes (max " + MAX_ASSIGN_FROM_CHASHES + ")");
        }
        // Normalize to lowercase hex ONCE, at the repository boundary (nexus-lns3o
        // review fix, code-review-expert MEDIUM): chunks_<dim>.chash is decoded
        // case-INSENSITIVELY by the SQL function (decode(x,'hex') -> bytea, then a
        // byte-level match), but ChashHex.hex(...) always FETCHES/formats lowercase
        // (HexFormat.of().formatHex) and the existence-probe WHERE below compares
        // that lowercase column rendering against these Java strings via a TEXT
        // `.in(...)`. Without normalizing here, an uppercase-hex input chash would
        // decode and assign successfully in the SQL pass (byte-level, case-blind)
        // while the Java probe's text comparison missed it — reporting a chash the
        // SQL just assigned as "unmatched", contradicting the route's "never
        // silently dropped" contract. Normalizing once up front keeps the probe and
        // the SQL call working from the identical casing.
        List<String> chashes = rawChashes.stream().map(String::toLowerCase).toList();
        // Fail loud BEFORE opening a transaction — an unresolvable dim means no
        // per-dim table exists to query at all (dimForCollection's own contract).
        int dim = dev.nexus.service.vectors.PgVectorRepository.dimForCollection(collection);
        String[] chashArr = chashes.toArray(new String[0]);

        return tenantScope.withTenant(tenant, ctx -> {
            // chunks_<dim>.chash is bytea (RDR-180); the HTTP/route boundary carries
            // hex text, so the existence probe goes through ChashHex.hex(...) — the
            // house-blessed jOOQ seam that binds hex->bytes / fetches bytes->hex
            // uniformly (same idiom PgVectorRepository uses for catalog_document_chunks).
            // RDR-191 (nexus-o8dil.48): the three per-dim existence probes
            // collapsed onto the unified nexus.chunks. (tenant RLS, collection,
            // chash) identifies the row regardless of dim, so no embedding-column
            // predicate is needed for THIS probe; dim stays load-bearing for the
            // per-dim assign_from_chashes_<dim>() calls below, whose own switch
            // keeps the unsupported-dim fail-loud arm.
            List<String> found = ctx.select(ChashHex.hex(CHUNKS.CHASH)).from(CHUNKS)
                    .where(CHUNKS.COLLECTION.eq(collection)
                        .and(ChashHex.hex(CHUNKS.CHASH).in(chashArr)))
                    .fetch(ChashHex.hex(CHUNKS.CHASH));
            java.util.Set<String> foundSet = new java.util.HashSet<>(found);
            List<String> unmatched = chashes.stream()
                .filter(c -> !foundSet.contains(c))
                .distinct()
                .toList();

            int assigned = assignFromChashesOnePass(ctx, dim, collection, chashArr, false).size();
            int crossAssigned = crossCollection
                ? assignFromChashesOnePass(ctx, dim, collection, chashArr, true).size()
                : 0;

            Map<String, Object> out = new LinkedHashMap<>();
            out.put("assigned", assigned);
            out.put("cross_assigned", crossAssigned);
            out.put("unmatched_chashes", unmatched);
            return out;
        });
    }

    /**
     * One call to {@code nexus.assign_from_chashes_<dim>()} — computes AND
     * persists in the same statement (taxonomy-006). Called EXACTLY ONCE per
     * (dim, crossCollection) pass: the function is VOLATILE (it writes via a
     * data-modifying CTE), so invoking it twice for the same logical pass (e.g.
     * a separate count-then-fetch) would fire the INSERT/upsert twice. The
     * returned similarity reflects the FRESHLY COMPUTED argmax value, not the
     * post-upsert DB value (see taxonomy-006's changelog comment for why).
     */
    private static List<Map<String, Object>> assignFromChashesOnePass(
            DSLContext ctx, int dim, String collection, String[] chashes, boolean crossCollection) {
        org.jooq.Table<?> fn = switch (dim) {
            case 384  -> ASSIGN_FROM_CHASHES_384.call(collection, chashes, crossCollection);
            case 768  -> ASSIGN_FROM_CHASHES_768.call(collection, chashes, crossCollection);
            case 1024 -> ASSIGN_FROM_CHASHES_1024.call(collection, chashes, crossCollection);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
        var result = ctx.selectFrom(fn).fetch();
        List<Map<String, Object>> rows = new ArrayList<>(result.size());
        for (var rec : result) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("chash",      rec.get("chash", String.class));
            row.put("topic_id",   rec.get("topic_id", Long.class));
            row.put("similarity", rec.get("similarity", Double.class));
            rows.add(row);
        }
        return rows;
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

    /**
     * Render a timestamp for the wire as UTC ISO-8601 with explicit seconds.
     *
     * <p>NOT {@code OffsetDateTime.toString()} (nexus-onjvy). That renders in whatever
     * offset the value carries — on a developer box the same instant comes back as
     * "2026-04-08T17:00-07:00" rather than "2026-04-09T00:00:00Z" — and it ELIDES ZERO
     * SECONDS. Both properties break the client, which compares and displays these as
     * strings: a lexicographic comparison across mixed offsets is simply wrong, and
     * "T00:00Z" sorts differently from "T00:00:00Z". This class already
     * declares {@code UTC_SECOND} for exactly this shape; it just was not being used
     * on these reads.
     */
    private static String utcIso(Object ts) {
        if (!(ts instanceof OffsetDateTime odt)) return null;
        return UTC_SECOND.format(odt.withOffsetSameInstant(ZoneOffset.UTC));
    }

    /**
     * Full assignment rows for the given doc_ids, quality columns included (nexus-onjvy).
     *
     * <p>{@link #getAssignmentsForDocs} projects DOC_ID and TOPIC_ID only, and no other
     * route returned the rest — so {@code similarity}, {@code assigned_at} and
     * {@code source_collection} were WRITTEN by the assign path and readable by nobody.
     * In service mode an operator could not ask how confident an assignment was, when it
     * was made, or which collection it came from.
     *
     * <p>A SEPARATE ROUTE rather than a widened projection, deliberately:
     * {@code getAssignmentsForDocs} is consumed as a {@code {doc_id: topic_id}} map on
     * the client, so widening it would change a return TYPE that existing callers
     * destructure. Additive is the non-breaking shape, and the two reads have genuinely
     * different costs — the map read stays cheap for the hot path.
     */
    public List<Map<String, Object>> getAssignmentDetails(String tenant, List<String> docIds) {
        if (docIds == null || docIds.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID,
                       TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                       TOPIC_ASSIGNMENTS.SIMILARITY,
                       TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                       TOPIC_ASSIGNMENTS.ASSIGNED_AT)
               .from(TOPIC_ASSIGNMENTS)
               .where(TOPIC_ASSIGNMENTS.DOC_ID.in(docIds))
               .orderBy(TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID)
               .fetch()
               .map(r -> {
                   Map<String, Object> m = new LinkedHashMap<>();
                   m.put("doc_id",            r.get(TOPIC_ASSIGNMENTS.DOC_ID));
                   m.put("topic_id",          r.get(TOPIC_ASSIGNMENTS.TOPIC_ID));
                   m.put("assigned_by",       r.get(TOPIC_ASSIGNMENTS.ASSIGNED_BY));
                   m.put("similarity",        r.get(TOPIC_ASSIGNMENTS.SIMILARITY));
                   m.put("source_collection", r.get(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION));
                   m.put("assigned_at",       utcIso(r.get(TOPIC_ASSIGNMENTS.ASSIGNED_AT)));
                   return m;
               }));
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
        // nexus-cefa1.6: link_types is jsonb NOT NULL now (taxonomy-008-link-
        // types-jsonb.xml) — jsonbRequired rejects null/blank as a
        // repository-layer backstop (the primary gate is TaxonomyHandler's
        // own 400, mirroring AspectHandler.rejectMalformedJson).
        JSONB linkTypesJb = jsonbRequired(linkTypes, "link_types");
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(TOPIC_LINKS,
                    TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                    TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
               .values(tenant, fromId, toId, linkCount, linkTypesJb)
               .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
               .doUpdate()
               .set(TOPIC_LINKS.LINK_COUNT, field("EXCLUDED.link_count", Integer.class))
               .set(TOPIC_LINKS.LINK_TYPES, field("EXCLUDED.link_types", JSONB.class))
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

    // ══════════════════════════════════════════════════════════════════════════
    // UNIQUE-KEY COMPLETENESS (nexus-q2ign — nexus-0ehwe arbiter class, 4th instance)
    // ══════════════════════════════════════════════════════════════════════════
    //
    // nexus.topics carries TWO caller-determined unique keys:
    //   topics_pk                              PRIMARY KEY (id)                     taxonomy-001-baseline.xml:58
    //   idx_topics_root_tenant_collection_label UNIQUE (tenant_id, collection, label)
    //                                           WHERE parent_id IS NULL             taxonomy-004-dedup-root-topics.xml:87
    //
    // id is BIGSERIAL, but the fidelity ETL import (importTopic / importTopicsBatch
    // below) supplies it explicitly to preserve topic_assignments/topic_links FK
    // references across a migration — so for THIS write path id is caller-determined,
    // not server-generated, and the "server-generated keys are exempt" rule
    // (5c8c978e / catalog_links, memory, *_etl_dedup) does not apply to it. Those two
    // sites arbitrate ON CONFLICT (TOPICS.ID) and never name the root-label key, so an
    // imported row whose (tenant, collection, label) already lives at a DIFFERENT id
    // (root-topic re-parented/relabeled between snapshots, or two partial imports of
    // overlapping history) raises a raw, undiagnosable 23505 on
    // idx_topics_root_tenant_collection_label.
    //
    // PER-TABLE DECISION (Hal, matching 5c8c978e's catalog_documents shape): id is the
    // ADDRESS, (tenant, collection, label WHERE parent_id IS NULL) is the IDENTITY.
    // REFUSE, do not converge — converging onto the pre-existing id would silently
    // renumber the row the caller is fidelity-importing, breaking the very
    // topic_assignments/topic_links FK references (which point at the SOURCE id) this
    // import exists to preserve. Same reasoning as guardDocumentIdentity's source_uri
    // guard in CatalogRepository (silent convergence there would misroute a write;
    // here it would corrupt referential fidelity).
    //
    // persistRebuildTopics / persistDiscoveredTopics (the LIVE discovery write paths,
    // not ETL) are NOT in scope: they never supply an explicit id (server-generated,
    // exempt), and they already arbitrate the ONLY key they can hit
    // (TOPICS.TENANT_ID, TOPICS.COLLECTION, TOPICS.LABEL) WHERE PARENT_ID IS NULL) —
    // plus lockTaxonomyCollection serializes per-collection writers, a stronger belt
    // than the retry this class's import siblings use. Non-root topics (parent_id NOT
    // NULL) are outside the partial index's predicate entirely — no identity key to
    // guard.
    //
    // No UniqueRaceRetry here, unlike upsertOwner/upsertDocument (the LIVE write
    // paths in CatalogRepository): importOwner/importOwnersBatch/importDocument/
    // importDocumentsBatch — this method's direct siblings — carry the same guard
    // WITHOUT a retry wrapper, because fidelity ETL import is a migration/seeding
    // path, never concurrent with live writes for the same tenant (see
    // advanceTopicsIdSequence's javadoc immediately below, which accepts the same
    // non-concurrency assumption for the sequence advance).

    /** Address key of {@code nexus.topics} (taxonomy-001-baseline.xml:58). */
    static final String TOPICS_PK = "topics_pk";
    /** Root-topic identity key of {@code nexus.topics}, partial (taxonomy-004-dedup-root-topics.xml:87). */
    static final String TOPICS_ROOT_LABEL = "idx_topics_root_tenant_collection_label";

    /**
     * The id of the LIVE root topic already holding {@code (tenant, collection,
     * label)}, or {@code null}. Only root topics (parent_id IS NULL) are governed
     * by this identity key — mirrors the partial index's own predicate exactly.
     */
    private static Long resolveRootTopicId(DSLContext ctx, String tenant,
                                           String collection, String label) {
        return ctx.select(TOPICS.ID).from(TOPICS)
                  .where(TOPICS.TENANT_ID.eq(tenant)
                         .and(TOPICS.COLLECTION.eq(collection))
                         .and(TOPICS.LABEL.eq(label))
                         .and(TOPICS.PARENT_ID.isNull()))
                  .limit(1)
                  .fetchOne(TOPICS.ID);
    }

    /**
     * Refuse a fidelity-import write that would give a root-topic label identity
     * to a second id (nexus-q2ign). {@code parentId != null} (a non-root topic)
     * is never governed by this key and returns quietly.
     */
    private static void guardTopicIdentity(DSLContext ctx, String tenant, long attemptedId,
                                           Long parentId, String collection, String label) {
        if (parentId != null) return;
        Long existing = resolveRootTopicId(ctx, tenant, collection, label);
        if (existing != null && existing != attemptedId) {
            throw new CatalogIdentityConflictException(
                TOPICS_ROOT_LABEL,
                "collection=" + collection + " label=" + label,
                String.valueOf(existing), String.valueOf(attemptedId));
        }
    }

    // ── Fidelity ETL import ────────────────────────────────────────────────────

    /**
     * g37fr FINDING 2 (RDR-155 P4b, engine v0.1.53): fidelity import
     * preserves source ids verbatim, which leaves the topics BIGSERIAL
     * sequence BEHIND the imported ids — the next live serial INSERT
     * (persist_rebuild) then collides 409 on a shared store. Standard PG
     * import discipline: advance the sequence past the imported id inside
     * the import transaction. The GREATEST against the sequence's own
     * last_value means setval never moves the sequence backward. Requires
     * UPDATE on the sequence (granted to nexus_svc by taxonomy-005).
     *
     * NOT atomic against a concurrent nextval() on the same sequence: the
     * last_value read and the setval are two steps, so a live serial
     * INSERT racing this import could claim an id above the value setval
     * then writes. Accepted: fidelity import is a migration/seeding path,
     * never concurrent with live topic writes for the same tenant; if an
     * import-during-serving path ever appears, revisit with a lock on the
     * sequence or an advisory lock keyed on the table.
     */
    // SANCTIONED RAW (rdr155-p4b F-C): setval / pg_get_serial_sequence /
    // sequence last_value are sequence-state functions with no generated
    // jOOQ form (codegen models tables, not sequences); one statement,
    // import-path only, never serving-path.
    private static void advanceTopicsIdSequence(DSLContext ctx, long maxImportedId) {
        ctx.execute(
            "SELECT setval(pg_get_serial_sequence('nexus.topics', 'id'), "
            + "GREATEST((SELECT last_value FROM nexus.topics_id_seq), ?))",
            maxImportedId);
    }

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
            // nexus-q2ign: this INSERT arbitrates TOPICS.ID only, so a root topic
            // (parent_id null) whose (collection, label) already lives at a
            // DIFFERENT id must be refused before the write, not left to raise a
            // raw 23505 on idx_topics_root_tenant_collection_label.
            guardTopicIdentity(ctx, tenant, srcId, parentId, collection, label);
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
            advanceTopicsIdSequence(ctx, srcId);
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
        // nexus-cefa1.6: see upsertTopicLink's identical comment above.
        JSONB linkTypesJb = jsonbRequired(linkTypes, "link_types");
        tenantScope.withTenant(tenant, ctx -> {
            // GREATEST(existing.link_count, EXCLUDED.link_count) — ETL path uses GREATEST
            // to never downgrade a live PG value from a stale SQLite snapshot.
            // GREATEST over two table references is an irreducible plain-SQL fragment.
            ctx.insertInto(TOPIC_LINKS,
                    TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                    TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
               .values(tenant, fromId, toId, linkCount, linkTypesJb)
               .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
               .doUpdate()
               .set(TOPIC_LINKS.LINK_COUNT,
                    field("GREATEST(nexus.topic_links.link_count, EXCLUDED.link_count)", Integer.class))
               .set(TOPIC_LINKS.LINK_TYPES, field("EXCLUDED.link_types", JSONB.class))
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

    /**
     * RDR-176 P3 (Gap 1, bead nexus-t9rmg.18): fidelity-preserving BULK import
     * for one taxonomy *kind* ({@code topic} | {@code assignment} | {@code link}
     * | {@code meta}).
     *
     * <p>Collapses a kind's batch to ONE round-trip and ONE tenant transaction:
     * a single {@link TenantScope#withTenant} (RLS GUC {@code nexus.tenant} set
     * ONCE per batch) wrapping the SAME INSERT each per-row {@code import*} method
     * issues (topic: EXCLUDED merge keeping trigger-maintained doc_count;
     * assignment: never-downgrade-projection + GREATEST similarity; link/meta:
     * GREATEST). Strict timestamp parse per row. The per-row methods remain for
     * the live/single-write path; this is the migration leg's GUC-once batch.
     * topic_assignments is the 190k-row dogfood offender this fixes. Empty batch
     * is a no-op.
     *
     * @return number of rows submitted.
     */
    public int importBatch(String tenant, String kind, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        // nexus-ps9wb belt: the import*Batch methods sort their deduped rows by the
        // ON CONFLICT key (global lock order), and this retry covers a residual
        // cross-path deadlock. Idempotent ON CONFLICT batch, victim already rolled
        // back → safe.
        return DeadlockRetry.run("taxonomy.importBatch." + kind, () -> tenantScope.withTenant(tenant, ctx -> {
            int n = switch (kind) {
                case "topic"      -> importTopicsBatch(ctx, tenant, rows);
                case "assignment" -> importAssignmentsBatch(ctx, tenant, rows);
                case "link"       -> importLinksBatch(ctx, tenant, rows);
                case "meta"       -> importMetaBatch(ctx, tenant, rows);
                default -> throw new IllegalArgumentException("Unknown taxonomy kind: " + kind);
            };
            log.debug("event=taxonomy_import_batch tenant={} kind={} rows={}", tenant, kind, rows.size());
            return n;
        }));
    }

    /**
     * nexus-1usso: every {@code importBatch} kind below lands its whole
     * request in ONE multi-row {@code INSERT ... ON CONFLICT} statement
     * (chunked at {@link #MAX_BATCH_PARAMS} bind params for PG's Int16
     * bind-count limit), mirroring {@code ChashRepository.doImportBatch}
     * (f0ab406f). Rows are deduped on each table's conflict key within a
     * chunk — last occurrence wins — because a single multi-row
     * {@code ON CONFLICT DO UPDATE} cannot affect the same row twice.
     */
    private static final int MAX_BATCH_PARAMS = 30_000;

    private static int importTopicsBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        // Register each DISTINCT collection once (was: once per row).
        var collections = new java.util.LinkedHashSet<String>();
        for (var r : rows) {
            String c = optS(r, "collection");
            if (c != null) collections.add(c);
        }
        for (String c : collections) ensureCollectionRegistered(ctx, tenant, c);

        // Dedupe on id (the conflict key), last occurrence wins. Sort by id (the
        // ON CONFLICT key) so concurrent batches lock TOPICS rows in one global order
        // — deadlock avoidance, nexus-ps9wb.
        var unique = new LinkedHashMap<Long, Map<String, Object>>(rows.size());
        for (var r : rows) unique.put(reqL(r, "id"), r);
        List<Map<String, Object>> deduped = new ArrayList<>(unique.values());
        deduped.sort(Comparator.comparing(r -> reqL(r, "id")));

        final int cols = 10;
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
        for (int start = 0; start < deduped.size(); start += chunkSize) {
            List<Map<String, Object>> batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
            var insert = ctx.insertInto(TOPICS,
                    TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.PARENT_ID,
                    TOPICS.COLLECTION, TOPICS.CENTROID_HASH, TOPICS.DOC_COUNT,
                    TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS, TOPICS.TERMS);
            for (var r : batch) {
                // nexus-q2ign: same guard as the single-row importTopic — the
                // batch form is not exempt from the key its ON CONFLICT (TOPICS.ID)
                // omits.
                guardTopicIdentity(ctx, tenant, reqL(r, "id"), optL(r, "parent_id"),
                                    optS(r, "collection"), optS(r, "label"));
                insert = insert.values(reqL(r, "id"), tenant, optS(r, "label"), optL(r, "parent_id"),
                        optS(r, "collection"), optS(r, "centroid_hash"), optI(r, "doc_count", 0),
                        parseTsStrict(reqS(r, "created_at")), optS(r, "review_status"),
                        optS(r, "terms"));
            }
            insert.onConflict(TOPICS.ID).doUpdate()
                  .set(TOPICS.REVIEW_STATUS, field("EXCLUDED.review_status", String.class))
                  .set(TOPICS.CENTROID_HASH, field("EXCLUDED.centroid_hash", String.class))
                  .set(TOPICS.TERMS,         field("EXCLUDED.terms",         String.class))
                  .execute();
        }
        // Deduped list is id-sorted — the last element carries the max id.
        advanceTopicsIdSequence(ctx, reqL(deduped.get(deduped.size() - 1), "id"));
        return rows.size();
    }

    private static int importAssignmentsBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        var collections = new java.util.LinkedHashSet<String>();
        for (var r : rows) {
            String c = optS(r, "source_collection");
            if (c != null) collections.add(c);
        }
        for (String c : collections) ensureCollectionRegistered(ctx, tenant, c);

        // Conflict key: (tenant_id, doc_id, topic_id). tenant is constant for this
        // call. Sort by (doc_id, topic_id) so concurrent batches lock TOPIC_ASSIGNMENTS
        // rows in one global order — deadlock avoidance, nexus-ps9wb.
        var unique = new LinkedHashMap<String, Map<String, Object>>(rows.size());
        for (var r : rows) unique.put(reqS(r, "doc_id") + "::" + reqL(r, "topic_id"), r);
        List<Map<String, Object>> deduped = new ArrayList<>(unique.values());
        deduped.sort(Comparator
                .comparing((Map<String, Object> r) -> reqS(r, "doc_id"))
                .thenComparing(r -> reqL(r, "topic_id")));

        final int cols = 7;
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
        for (int start = 0; start < deduped.size(); start += chunkSize) {
            List<Map<String, Object>> batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
            var insert = ctx.insertInto(TOPIC_ASSIGNMENTS,
                    TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                    TOPIC_ASSIGNMENTS.SIMILARITY, TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION);
            for (var r : batch) {
                String assignedAt = optS(r, "assigned_at");
                OffsetDateTime assignedAtTs = (assignedAt != null && !assignedAt.isBlank())
                    ? parseTsStrict(assignedAt) : null;
                insert = insert.values(tenant, reqS(r, "doc_id"), reqL(r, "topic_id"),
                        optS(r, "assigned_by"), optD(r, "similarity"), assignedAtTs,
                        optS(r, "source_collection"));
            }
            insert.onConflict(TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                               TOPIC_ASSIGNMENTS.TOPIC_ID)
                  .doUpdate()
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
        }
        return rows.size();
    }

    private static int importLinksBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        // Conflict key: (tenant_id, from_topic_id, to_topic_id). Sort by
        // (from_topic_id, to_topic_id) so concurrent batches lock TOPIC_LINKS rows in
        // one global order — deadlock avoidance, nexus-ps9wb.
        var unique = new LinkedHashMap<String, Map<String, Object>>(rows.size());
        for (var r : rows) unique.put(reqL(r, "from_topic_id") + "::" + reqL(r, "to_topic_id"), r);
        List<Map<String, Object>> deduped = new ArrayList<>(unique.values());
        deduped.sort(Comparator
                .comparing((Map<String, Object> r) -> reqL(r, "from_topic_id"))
                .thenComparing(r -> reqL(r, "to_topic_id")));

        final int cols = 5;
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
        for (int start = 0; start < deduped.size(); start += chunkSize) {
            List<Map<String, Object>> batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
            var insert = ctx.insertInto(TOPIC_LINKS,
                    TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                    TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES);
            for (var r : batch) {
                // nexus-cefa1.6: see upsertTopicLink's identical jsonbRequired comment.
                insert = insert.values(tenant, reqL(r, "from_topic_id"), reqL(r, "to_topic_id"),
                        optI(r, "link_count", 0), jsonbRequired(optS(r, "link_types"), "link_types"));
            }
            insert.onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                  .doUpdate()
                  .set(TOPIC_LINKS.LINK_COUNT,
                       field("GREATEST(nexus.topic_links.link_count, EXCLUDED.link_count)", Integer.class))
                  .set(TOPIC_LINKS.LINK_TYPES, field("EXCLUDED.link_types", JSONB.class))
                  .execute();
        }
        return rows.size();
    }

    private static int importMetaBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        var collections = new java.util.LinkedHashSet<String>();
        for (var r : rows) collections.add(reqS(r, "collection"));
        for (String c : collections) ensureCollectionRegistered(ctx, tenant, c);

        // Conflict key: (tenant_id, collection). Sort by collection so concurrent
        // batches lock TAXONOMY_META rows in one global order — deadlock avoidance,
        // nexus-ps9wb.
        var unique = new LinkedHashMap<String, Map<String, Object>>(rows.size());
        for (var r : rows) unique.put(reqS(r, "collection"), r);
        List<Map<String, Object>> deduped = new ArrayList<>(unique.values());
        deduped.sort(Comparator.comparing(r -> reqS(r, "collection")));

        final int cols = 4;
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / cols);
        for (int start = 0; start < deduped.size(); start += chunkSize) {
            List<Map<String, Object>> batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
            var insert = ctx.insertInto(TAXONOMY_META,
                    TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION,
                    TAXONOMY_META.LAST_DISCOVER_DOC_COUNT, TAXONOMY_META.LAST_DISCOVER_AT);
            for (var r : batch) {
                String lastAt = optS(r, "last_discover_at");
                OffsetDateTime lastAtTs = (lastAt != null && !lastAt.isBlank())
                    ? parseTsStrict(lastAt) : null;
                insert = insert.values(tenant, reqS(r, "collection"),
                        optI(r, "last_discover_doc_count", 0), lastAtTs);
            }
            insert.onConflict(TAXONOMY_META.TENANT_ID, TAXONOMY_META.COLLECTION)
                  .doUpdate()
                  .set(TAXONOMY_META.LAST_DISCOVER_DOC_COUNT,
                       field("GREATEST(nexus.taxonomy_meta.last_discover_doc_count,"
                           + " EXCLUDED.last_discover_doc_count)", Integer.class))
                  .set(TAXONOMY_META.LAST_DISCOVER_AT,
                       field("GREATEST(nexus.taxonomy_meta.last_discover_at,"
                           + " EXCLUDED.last_discover_at)", OffsetDateTime.class))
                  .execute();
        }
        return rows.size();
    }

    // ── batch map-extraction helpers (mirror TaxonomyHandler's per-row parse) ──
    private static String reqS(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null || v.toString().isEmpty()) throw new IllegalArgumentException("Missing required field: " + k);
        return v.toString();
    }
    private static String optS(Map<String, Object> r, String k) {
        Object v = r.get(k);
        return v == null ? null : v.toString();
    }
    private static Double optD(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null) return null;
        if (v instanceof Number n) return n.doubleValue();
        try { return Double.parseDouble(v.toString()); } catch (NumberFormatException e) { return null; }
    }
    private static Long optL(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null) return null;
        if (v instanceof Number n) return n.longValue();
        try { return Long.parseLong(v.toString()); } catch (NumberFormatException e) { return null; }
    }
    private static long reqL(Map<String, Object> r, String k) {
        Long l = optL(r, k);
        if (l == null) throw new IllegalArgumentException("Missing required field: " + k);
        return l;
    }
    private static int optI(Map<String, Object> r, String k, int def) {
        Object v = r.get(k);
        if (v instanceof Number n) return n.intValue();
        if (v != null) { try { return Integer.parseInt(v.toString()); } catch (NumberFormatException ignored) { } }
        return def;
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
     * Hub detection data: returns per-topic DF + total_chunks + label + collection + source set,
     * plus the staleness aggregates behind {@code --warn-stale} (nexus-onjvy).
     * Python-side computes ICF, stopword matching, and score.
     *
     * <p>STALENESS (RDR-077 C-2 semantics, previously SQLite-only). Per hub, over the
     * source collections actually contributing to it:
     * <ul>
     *   <li>{@code max_last_discover_at} — MAX(taxonomy_meta.last_discover_at), NULLs
     *       excluded by MAX, exactly as the retired SQLite oracle did.</li>
     *   <li>{@code never_discovered_count} — contributing collections with a NULL
     *       last_discover_at PLUS those with no taxonomy_meta row at all. Both are
     *       "never discovered" from the command's point of view.</li>
     *   <li>{@code is_stale} — the hub has assignments newer than the latest discover,
     *       or any contributing collection was never discovered.</li>
     * </ul>
     *
     * <p>{@code is_stale} IS COMPUTED HERE rather than client-side, deliberately, even
     * though ICF and score are computed client-side. The oracle compared ISO-8601
     * strings lexicographically, which was sound on SQLite's TEXT timestamps. Over HTTP
     * the values cross as {@code OffsetDateTime.toString()}, which ELIDES ZERO SECONDS
     * ("2026-04-09T00:00Z", not "...T00:00:00Z") — so the same lexicographic comparison
     * would silently misorder timestamps that differ only in whether seconds were zero.
     * Comparing real TIMESTAMPTZ values server-side avoids reintroducing that trap; the
     * client passes the verdict through.
     *
     * <p>One extra query, not N+1: taxonomy_meta is fetched once for every collection
     * referenced by any hub, then folded per topic in memory.
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

            // taxonomy_meta for every collection any hub draws from, in one query.
            // Only non-null last_discover_at values are kept: a collection with a NULL
            // timestamp and one with no taxonomy_meta row at all are both "never
            // discovered" here, and the oracle counted them together too (its
            // never_count summed the NULLs and the missing rows). So a single
            // "absent from this map" test covers both states.
            var contributing = sourcesMap.values().stream()
                .flatMap(List::stream).distinct().toList();
            java.util.Map<String, OffsetDateTime> discoverAt = new java.util.HashMap<>();
            if (!contributing.isEmpty()) {
                for (var r : ctx.select(TAXONOMY_META.COLLECTION, TAXONOMY_META.LAST_DISCOVER_AT)
                        .from(TAXONOMY_META)
                        .where(TAXONOMY_META.COLLECTION.in(contributing))
                        .fetch()) {
                    OffsetDateTime at = r.get(TAXONOMY_META.LAST_DISCOVER_AT);
                    if (at != null) discoverAt.put(r.get(TAXONOMY_META.COLLECTION), at);
                }
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
                m.put("last_assigned_at", utcIso(lastAt));
                List<String> sources = sourcesMap.getOrDefault(tid, List.of());
                m.put("source_collections", sources);

                OffsetDateTime maxDiscover = null;
                int neverCount = 0;
                for (String coll : new java.util.LinkedHashSet<>(sources)) {
                    OffsetDateTime at = discoverAt.get(coll);
                    if (at == null) {
                        // NULL last_discover_at, or no taxonomy_meta row at all.
                        neverCount++;
                        continue;
                    }
                    if (maxDiscover == null || at.isAfter(maxDiscover)) maxDiscover = at;
                }

                boolean isStale = neverCount > 0;
                if (!isStale && maxDiscover != null
                        && lastAt instanceof OffsetDateTime assignedAt) {
                    isStale = assignedAt.isAfter(maxDiscover);
                }
                m.put("max_last_discover_at", utcIso(maxDiscover));
                m.put("never_discovered_count", neverCount);
                m.put("is_stale", isStale);
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
     * Topics carrying projection assignments that have no {@code topic_links}
     * row, restricted to those a link is structurally POSSIBLE for.
     *
     * <p>The predicate mirrors {@link #refreshProjectionLinks} exactly, and that
     * is the whole point: a drift audit whose notion of "linkable" differs from
     * the materializer's reports fiction. {@code refreshProjectionLinks} pairs a
     * projection TARGET with a NON-projection SOURCE, so a topic is only drifted
     * if some doc gives it a non-projection partner. Two projection assignments
     * on one doc produce no link and are not drift; a lone assignment cannot
     * pair at all.
     *
     * <p>This route exists because the client-side check could not ask the
     * engine and audited the frozen SQLite migration source instead
     * (nexus-ypori) — reporting stale relic rows as live faults while the real
     * store went unexamined. Computing it here is also the only affordable
     * shape: reconstructing it client-side costs one round trip per topic plus
     * a bulk assignment read.
     *
     * @param limit max drift rows returned; the total count is exact regardless
     * @return {@code {projection_total, drift_count, rows:[{topic_id,label,collection}]}}
     */
    public Map<String, Object> linkDrift(String tenant, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var tgt = TOPIC_ASSIGNMENTS.as("ta");
            var src = TOPIC_ASSIGNMENTS.as("ta2");

            var linkable = tgt.ASSIGNED_BY.eq("projection")
                .and(notExists(
                    ctx.selectOne().from(TOPIC_LINKS)
                       .where(TOPIC_LINKS.FROM_TOPIC_ID.eq(tgt.TOPIC_ID)
                           .or(TOPIC_LINKS.TO_TOPIC_ID.eq(tgt.TOPIC_ID)))))
                .and(exists(
                    ctx.selectOne().from(src)
                       .where(src.DOC_ID.eq(tgt.DOC_ID)
                           .and(src.TOPIC_ID.ne(tgt.TOPIC_ID))
                           .and(src.ASSIGNED_BY.ne("projection")))));

            int projectionTotal = ctx.select(countDistinct(TOPIC_ASSIGNMENTS.TOPIC_ID))
                .from(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.ASSIGNED_BY.eq("projection"))
                .fetchOne(0, int.class);

            int driftCount = ctx.select(countDistinct(tgt.TOPIC_ID))
                .from(tgt).where(linkable)
                .fetchOne(0, int.class);

            var rows = ctx.selectDistinct(tgt.TOPIC_ID, TOPICS.LABEL, tgt.SOURCE_COLLECTION)
                .from(tgt)
                .leftJoin(TOPICS).on(TOPICS.ID.eq(tgt.TOPIC_ID))
                .where(linkable)
                .orderBy(tgt.TOPIC_ID)
                .limit(Math.max(0, limit))
                .fetch();

            List<Map<String, Object>> out = new ArrayList<>();
            for (var r : rows) {
                var m = new LinkedHashMap<String, Object>();
                m.put("topic_id", r.get(tgt.TOPIC_ID));
                m.put("label", r.get(TOPICS.LABEL));
                m.put("collection", r.get(tgt.SOURCE_COLLECTION));
                out.add(m);
            }
            var result = new LinkedHashMap<String, Object>();
            result.put("projection_total", projectionTotal);
            result.put("drift_count", driftCount);
            result.put("rows", out);
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

            // nexus-cefa1.6: link_types is jsonb now — a hardcoded, always-valid
            // literal, so JSONB.valueOf directly rather than jsonbRequired's
            // null-check (this value is never client-supplied).
            JSONB cooccurrenceTypes = JSONB.valueOf("[\"cooccurrence\"]");
            for (var r : pairs) {
                long fromId = r.get("from_id", Long.class);
                long toId   = r.get("to_id",   Long.class);
                int  cnt    = r.get("cnt",      Integer.class);
                ctx.insertInto(TOPIC_LINKS,
                        TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                        TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
                   .values(tenant, fromId, toId, cnt, cooccurrenceTypes)
                   .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                   .doUpdate()
                   .set(TOPIC_LINKS.LINK_COUNT, field("EXCLUDED.link_count", Integer.class))
                   .set(TOPIC_LINKS.LINK_TYPES, cooccurrenceTypes)
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
                    // nexus-cefa1.6: link_types is jsonb now — .data() recovers the
                    // raw JSON-array text so the merge algorithm below runs UNCHANGED
                    // (jsonb array element order survives the round trip verbatim;
                    // only object-key canonicalization would reorder anything, and
                    // this column never holds an object).
                    String lt = existing.get(0).get(TOPIC_LINKS.LINK_TYPES).data();
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

                // nexus-cefa1.6: mergedTypes is always a valid JSON array here — either
                // a hardcoded literal above, the untouched jsonb-read-back-then-.data()
                // value, or that value with "projection" spliced in by the same string
                // surgery this arc did not change — jsonbRequired is the uniform write
                // helper the rest of this class uses (this value is never null/blank).
                JSONB mergedTypesJb = jsonbRequired(mergedTypes, "link_types");

                ctx.insertInto(TOPIC_LINKS,
                        TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID,
                        TOPIC_LINKS.TO_TOPIC_ID, TOPIC_LINKS.LINK_COUNT, TOPIC_LINKS.LINK_TYPES)
                   .values(tenant, fromId, toId, entry.getValue(), mergedTypesJb)
                   .onConflict(TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                   .doUpdate()
                   .set(TOPIC_LINKS.LINK_COUNT, field("EXCLUDED.link_count", Integer.class))
                   .set(TOPIC_LINKS.LINK_TYPES,  field("EXCLUDED.link_types",  JSONB.class))
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
            // nexus-n2ls1: split shares the guardless delete-then-insert shape;
            // children escape the taxonomy-004 partial index (parent_id NOT
            // NULL) so a race here duplicates children rather than 409ing —
            // the same per-collection lock closes both.
            lockTaxonomyCollection(ctx, tenant, collectionName);
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
            // nexus-n2ls1: same guard-then-write shape as persistDiscoveredTopics
            // (delete-then-insert here) — serialize per-collection first.
            lockTaxonomyCollection(ctx, tenant, collection);
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

                // nexus-n2ls1 (critique M2): same in-request belt as
                // persistDiscoveredTopics — rebuild's inserts are root topics
                // (parent_id NULL → taxonomy-004 partial unique), the preceding
                // DELETE clears the collection, so a conflict here can only be
                // an in-batch duplicate label from a raw (non-nexus) client.
                // First spec wins the row (label identity + terms); the losing
                // spec's doc_ids union onto the shared topic via the
                // DO-NOTHING assignments insert — matching the nexus-slcn7
                // client-side union-merge. The losing spec's terms/
                // review_status are deliberately dropped (display aids, first
                // wins — same as the client dedup).
                Long topicId = ctx.insertInto(TOPICS,
                        TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                        TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.TERMS, TOPICS.REVIEW_STATUS)
                    .values(tenant, label, collection, docCount, now, terms, reviewStatus)
                    .onConflict(TOPICS.TENANT_ID, TOPICS.COLLECTION, TOPICS.LABEL)
                    .where(TOPICS.PARENT_ID.isNull())
                    .doNothing()
                    .returningResult(TOPICS.ID)
                    .fetchOne(TOPICS.ID);
                if (topicId == null) {
                    topicId = ctx.select(TOPICS.ID).from(TOPICS)
                        .where(TOPICS.TENANT_ID.eq(tenant),
                               TOPICS.COLLECTION.eq(collection),
                               TOPICS.LABEL.eq(label),
                               TOPICS.PARENT_ID.isNull())
                        .fetchOne(TOPICS.ID);
                    if (topicId == null) {
                        throw new IllegalStateException(
                            "persist_rebuild conflict-skip found no existing root topic for label '"
                            + label + "' in " + collection);
                    }
                }
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
            // nexus-n2ls1: serialize per-collection BEFORE the guard — the guard
            // alone is a TOCTOU race under concurrent discovery (both count 0,
            // both insert, loser 23505 → 409).
            lockTaxonomyCollection(ctx, tenant, collection);
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

                // nexus-n2ls1 defense-in-depth: DO NOTHING on the taxonomy-004
                // partial unique target. The advisory lock above already
                // serializes cross-request races; this belt absorbs the
                // remaining in-request shape — a raw (non-nexus-client) caller
                // sending two specs with the SAME label (the nexus client
                // dedups, the server must not 500/409 on it). A skipped insert
                // reuses the existing root topic's id so topic_ids stays
                // aligned with specs order. First spec wins the row: the losing
                // spec's doc_ids union onto the shared topic (DO-NOTHING
                // assignments insert, matching the nexus-slcn7 client-side
                // union-merge); its terms are deliberately dropped (display
                // aid, first wins — same as the client dedup).
                Long topicId = ctx.insertInto(TOPICS,
                        TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                        TOPICS.DOC_COUNT, TOPICS.CREATED_AT, TOPICS.TERMS)
                    .values(tenant, label, collection, docCount, now, terms)
                    .onConflict(TOPICS.TENANT_ID, TOPICS.COLLECTION, TOPICS.LABEL)
                    .where(TOPICS.PARENT_ID.isNull())
                    .doNothing()
                    .returningResult(TOPICS.ID)
                    .fetchOne(TOPICS.ID);
                if (topicId == null) {
                    topicId = ctx.select(TOPICS.ID).from(TOPICS)
                        .where(TOPICS.TENANT_ID.eq(tenant),
                               TOPICS.COLLECTION.eq(collection),
                               TOPICS.LABEL.eq(label),
                               TOPICS.PARENT_ID.isNull())
                        .fetchOne(TOPICS.ID);
                    if (topicId == null) {
                        // Conflict-skipped yet no visible row — cannot happen
                        // under the advisory lock (same-txn rows are visible);
                        // fail loud rather than desync the specs alignment.
                        throw new IllegalStateException(
                            "persist_discovered conflict-skip found no existing root topic for label '"
                            + label + "' in " + collection);
                    }
                }
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
     * <p>nexus-xtmtf: jOOQ's chained {@code .values()} supports a dynamic row
     * count per statement, so the batch stays ONE multi-row INSERT per chunk
     * (one trigger firing per chunk preserved) with zero raw SQL. The earlier
     * "jOOQ requires a statically-known row count" rationale was incorrect.
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
            var insert = ctx.insertInto(TOPIC_ASSIGNMENTS,
                    TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY);
            for (String docId : batch) {
                insert = insert.values(tenant, docId, topicId, assignedBy);
            }
            insert.onConflict(TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                              TOPIC_ASSIGNMENTS.TOPIC_ID)
                  .doNothing()
                  .execute();
        }
    }
}
