package dev.nexus.service.db;

import dev.nexus.service.jooq.tables.Memory;
import dev.nexus.service.jooq.tables.records.MemoryRecord;
import org.jooq.DSLContext;
import org.jooq.Result;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import static dev.nexus.service.jooq.Tables.MEMORY;
import static org.jooq.impl.DSL.*;
import org.jooq.Condition;

/**
 * RDR-152 bead nexus-gmiaf.6/.7 — jOOQ-based memory entry repository.
 *
 * <p>All methods are called within a {@link TenantScope#withTenant} transaction, which:
 * <ol>
 *   <li>stamps the {@code nexus.tenant} GUC on the connection, and</li>
 *   <li>commits on return (or rolls back on exception).</li>
 * </ol>
 *
 * <p>RLS: the {@code tenant_isolation} Postgres policy enforces
 * {@code tenant_id = current_setting('nexus.tenant', true)} on every row access.
 * Callers that pass the wrong tenant see zero rows (not an error), exactly as intended.
 *
 * <p>The generated {@link Memory} table class and {@link MemoryRecord} are the sole
 * references to table/column names. No string-literal SQL for column names is used here.
 *
 * <p>Usage:
 * <pre>{@code
 *   MemoryRepository repo = new MemoryRepository(tenantScope);
 *
 *   // insert
 *   long id = repo.upsert("my-tenant", "my-project", "my-title", "content", null, null, 30);
 *
 *   // query
 *   List<MemoryRecord> rows = repo.findByProject("my-tenant", "my-project");
 * }</pre>
 */
public final class MemoryRepository {

    private static final Logger log = LoggerFactory.getLogger(MemoryRepository.class);

    private final TenantScope tenantScope;

    public MemoryRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * Insert or update a memory entry.
     *
     * <p>ON CONFLICT key is {@code (tenant_id, project, title)} per the changelog
     * comment (cross-tenant key invariant: never use {@code (project, title)} alone).
     *
     * <p>{@code session} is the Python-side session identifier carried on every T2 write
     * and required by the .8 ETL to preserve provenance. Pass {@code null} if not known.
     *
     * @return the generated {@code id} of the inserted/updated row
     */
    public long upsert(String tenant,
                       String project,
                       String title,
                       String content,
                       String tags,
                       String session,
                       String agent,
                       Integer ttlDays) {
        return tenantScope.withTenant(tenant, ctx -> doUpsert(
                ctx, tenant, project, title, content, tags, session, agent, ttlDays));
    }

    /**
     * Find all memory entries in the given project visible to {@code tenant}.
     * RLS prevents cross-tenant leakage: the policy filters at the DB level.
     */
    public List<MemoryRecord> findByProject(String tenant, String project) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.selectFrom(MEMORY)
                          .where(MEMORY.PROJECT.eq(project))
                          .orderBy(MEMORY.TIMESTAMP.desc())
                          .fetch());
    }

    /**
     * Find a single memory entry by its unique (project, title) within the tenant.
     * Returns empty if no row matches (RLS filtered or genuinely absent).
     */
    public Optional<MemoryRecord> findByTitle(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.selectFrom(MEMORY)
                          .where(MEMORY.PROJECT.eq(project)
                              .and(MEMORY.TITLE.eq(title)))
                          .fetchOptional());
    }

    /**
     * Find a single memory entry by its numeric id.
     */
    public Optional<MemoryRecord> findById(String tenant, long id) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.selectFrom(MEMORY)
                          .where(MEMORY.ID.eq(id))
                          .fetchOptional());
    }

    /**
     * Exact-then-prefix title resolution (mirrors Python resolve_title logic).
     *
     * <p>Returns {@code ResolveResult(entry, [])} on exact or unique-prefix match;
     * {@code ResolveResult(null, candidates)} when multiple prefix matches exist;
     * {@code ResolveResult(null, [])} when nothing matches.
     */
    public ResolveResult resolveTitle(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant, ctx -> {
            // 1. Exact match
            var exact = ctx.selectFrom(MEMORY)
                           .where(MEMORY.PROJECT.eq(project).and(MEMORY.TITLE.eq(title)))
                           .fetchOptional();
            if (exact.isPresent()) {
                return new ResolveResult(exact.get(), List.of());
            }
            // 2. Prefix match — escape LIKE metacharacters
            String escaped = title.replace("\\", "\\\\")
                                  .replace("%", "\\%")
                                  .replace("_", "\\_");
            var candidates = ctx.selectFrom(MEMORY)
                                .where(MEMORY.PROJECT.eq(project)
                                    .and(MEMORY.TITLE.like(escaped + "%").escape('\\')))
                                .orderBy(MEMORY.TITLE.asc())
                                .fetch();
            if (candidates.size() == 1) {
                return new ResolveResult(candidates.get(0), List.of());
            }
            return new ResolveResult(null, candidates);
        });
    }

    /**
     * FTS search using the tsvector GIN index. Returns rows ordered by rank descending.
     * Access-count update is intentionally omitted (server-side access tracking
     * deferred to a later bead per the FTS parity contract).
     */
    public List<MemoryRecord> search(String tenant, String query, String project) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Use to_tsquery (plainto_tsquery for unstructured input)
            var condition = MEMORY.FTS_VECTOR.eq(
                    // jOOQ condition: fts_vector @@ websearch_to_tsquery('english', ?)
                    // Plain SQL condition since tsvector @@ operator has no jOOQ typed binding.
                    // The @@ call is injection-safe: the query argument is bound, not interpolated.
                    field("1=1").cast(Boolean.class) // placeholder; replaced below
            );

            // Use plain SQL for tsvector @@ operator (no jOOQ typed API for this)
            String ftsWhere = project != null && !project.isBlank()
                ? "fts_vector @@ websearch_to_tsquery('english', {0}) AND project = {1}"
                : "fts_vector @@ websearch_to_tsquery('english', {0})";

            Result<MemoryRecord> rows;
            if (project != null && !project.isBlank()) {
                rows = ctx.selectFrom(MEMORY)
                          .where(condition(ftsWhere, val(query), val(project)))
                          .orderBy(field("ts_rank(fts_vector, websearch_to_tsquery('english', {0}))", Double.class, val(query)).desc())
                          .fetch();
            } else {
                rows = ctx.selectFrom(MEMORY)
                          .where(condition(ftsWhere, val(query)))
                          .orderBy(field("ts_rank(fts_vector, websearch_to_tsquery('english', {0}))", Double.class, val(query)).desc())
                          .fetch();
            }
            return rows;
        });
    }

    /**
     * List entries (summary view) ordered by timestamp descending.
     * Returns id, project, title, agent, timestamp columns (mirrors Python list_entries).
     */
    public List<MemoryRecord> listEntries(String tenant, String project, String agent) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition where = noCondition();
            if (project != null && !project.isBlank()) {
                where = where.and(MEMORY.PROJECT.eq(project));
            }
            if (agent != null && !agent.isBlank()) {
                where = where.and(MEMORY.AGENT.eq(agent));
            }
            return ctx.selectFrom(MEMORY)
                      .where(where)
                      .orderBy(MEMORY.TIMESTAMP.desc())
                      .fetch();
        });
    }

    /**
     * Return distinct project namespaces starting with {@code prefix}, ordered by
     * latest timestamp descending (mirrors Python get_projects_with_prefix).
     * Returns a list of {@code [project, last_updated]} pairs as String arrays.
     */
    public List<String[]> getProjectsWithPrefix(String tenant, String prefix) {
        if (prefix == null || prefix.isBlank()) {
            return List.of();
        }
        return tenantScope.withTenant(tenant, ctx -> {
            String escaped = prefix.replace("\\", "\\\\")
                                   .replace("%", "\\%")
                                   .replace("_", "\\_");
            var rows = ctx.select(MEMORY.PROJECT, max(MEMORY.TIMESTAMP).as("last_updated"))
                          .from(MEMORY)
                          .where(MEMORY.PROJECT.like(escaped + "%").escape('\\'))
                          .groupBy(MEMORY.PROJECT)
                          .orderBy(max(MEMORY.TIMESTAMP).desc())
                          .fetch();
            List<String[]> result = new ArrayList<>();
            for (var r : rows) {
                OffsetDateTime lu = r.get("last_updated", OffsetDateTime.class);
                result.add(new String[]{r.get(MEMORY.PROJECT), lu != null ? lu.toString() : null});
            }
            return result;
        });
    }

    /**
     * FTS search scoped to projects matching a GLOB pattern.
     * Mirrors Python search_glob using SQL LIKE (% for *, _ stays _).
     * For GLOB semantics we map: * → %, ? → _ (standard glob-to-like).
     */
    public List<MemoryRecord> searchGlob(String tenant, String query, String projectGlob) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Convert glob pattern to LIKE pattern: * → %, ? → single char
            String likePattern = projectGlob.replace("\\", "\\\\")
                                            .replace("_", "\\_")
                                            .replace("%", "\\%")
                                            .replace("*", "%")
                                            .replace("?", "_");
            return ctx.selectFrom(MEMORY)
                      .where(condition(
                          "fts_vector @@ websearch_to_tsquery('english', {0}) AND project LIKE {1} ESCAPE '\\'",
                          val(query), val(likePattern)))
                      .orderBy(field("ts_rank(fts_vector, websearch_to_tsquery('english', {0}))", Double.class, val(query)).desc())
                      .fetch();
        });
    }

    /**
     * FTS search scoped to entries whose tags contain {@code tag} exactly (boundary-matched).
     * Mirrors Python search_by_tag using (',' || tags || ',') LIKE '%,tag,%'.
     */
    public List<MemoryRecord> searchByTag(String tenant, String query, String tag) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Escape LIKE metacharacters in the tag value
            String escapedTag = tag.replace("\\", "\\\\")
                                   .replace("%", "\\%")
                                   .replace("_", "\\_");
            String likePattern = "%," + escapedTag + ",%";
            return ctx.selectFrom(MEMORY)
                      .where(condition(
                          "fts_vector @@ websearch_to_tsquery('english', {0}) AND (',' || tags || ',') LIKE {1} ESCAPE '\\'",
                          val(query), val(likePattern)))
                      .orderBy(field("ts_rank(fts_vector, websearch_to_tsquery('english', {0}))", Double.class, val(query)).desc())
                      .fetch();
        });
    }

    /**
     * Return all entries for {@code project} with full column data, ordered by
     * timestamp descending. Mirrors Python get_all.
     */
    public List<MemoryRecord> getAll(String tenant, String project) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.selectFrom(MEMORY)
                          .where(MEMORY.PROJECT.eq(project))
                          .orderBy(MEMORY.TIMESTAMP.desc())
                          .fetch());
    }

    /**
     * Delete an entry by (project, title). Returns true if a row was deleted.
     */
    public boolean delete(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.deleteFrom(MEMORY)
                          .where(MEMORY.PROJECT.eq(project)
                              .and(MEMORY.TITLE.eq(title)))
                          .execute() > 0);
    }

    /**
     * Delete an entry by numeric id. Returns true if a row was deleted.
     */
    public boolean deleteById(String tenant, long id) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.deleteFrom(MEMORY)
                          .where(MEMORY.ID.eq(id))
                          .execute() > 0);
    }

    /**
     * Delete TTL-expired entries using heat-weighted effective TTL.
     *
     * <p>effective_ttl = base_ttl * (1 + ln(access_count + 1))
     * Mirrors Python MemoryStore.expire() logic exactly.
     *
     * @return list of deleted row IDs
     */
    public List<Long> expire(String tenant) {
        return tenantScope.withTenant(tenant, ctx -> {
            // Fetch candidates: all rows with a non-null TTL
            var candidates = ctx.select(MEMORY.ID, MEMORY.ACCESS_COUNT, MEMORY.TTL, MEMORY.TIMESTAMP)
                                .from(MEMORY)
                                .where(MEMORY.TTL.isNotNull())
                                .fetch();

            OffsetDateTime now = OffsetDateTime.now();
            List<Long> toDelete = new ArrayList<>();

            for (var row : candidates) {
                long rowId = row.get(MEMORY.ID);
                int accessCount = row.get(MEMORY.ACCESS_COUNT);
                int ttl = row.get(MEMORY.TTL);
                OffsetDateTime ts = row.get(MEMORY.TIMESTAMP);
                if (ts == null) continue;

                double effectiveTtl = ttl * (1.0 + Math.log(accessCount + 1));
                double ageDays = (double) java.time.Duration.between(ts, now).toSeconds() / 86400.0;
                if (ageDays > effectiveTtl) {
                    toDelete.add(rowId);
                }
            }

            if (!toDelete.isEmpty()) {
                ctx.deleteFrom(MEMORY)
                   .where(MEMORY.ID.in(toDelete))
                   .execute();
            }
            return toDelete;
        });
    }

    /**
     * Return entries not accessed in {@code idleDays}, using last_accessed when
     * available and falling back to timestamp. Mirrors Python flag_stale_memories.
     */
    public List<MemoryRecord> flagStaleMemories(String tenant, String project, int idleDays) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now().minusDays(idleDays);
            return ctx.selectFrom(MEMORY)
                      .where(MEMORY.PROJECT.eq(project)
                          .and(condition(
                              "CASE WHEN last_accessed IS NOT NULL THEN last_accessed < {0} ELSE timestamp < {0} END",
                              val(cutoff))))
                      .fetch();
        });
    }

    /**
     * Atomic merge: update content of {@code keepId} and delete all {@code deleteIds}.
     * Raises {@code IllegalArgumentException} if keepId is in deleteIds.
     * Raises {@code IllegalStateException} if keepId does not exist.
     */
    public void mergeMemories(String tenant, long keepId, List<Long> deleteIds, String mergedContent) {
        if (deleteIds.contains(keepId)) {
            throw new IllegalArgumentException(
                "keepId (" + keepId + ") must not be in deleteIds — would discard the entry meant to be kept");
        }
        tenantScope.withTenant(tenant, ctx -> {
            int updated = ctx.update(MEMORY)
                             .set(MEMORY.CONTENT, mergedContent)
                             .where(MEMORY.ID.eq(keepId))
                             .execute();
            if (updated == 0) {
                throw new IllegalStateException(
                    "keepId " + keepId + " not found — aborted merge to prevent data loss");
            }
            if (!deleteIds.isEmpty()) {
                ctx.deleteFrom(MEMORY)
                   .where(MEMORY.ID.in(deleteIds))
                   .execute();
            }
            return null;
        });
    }

    // ── Result type for resolve ────────────────────────────────────────────────

    /**
     * Return value for {@link #resolveTitle}: either a unique match or a list of candidates.
     */
    public record ResolveResult(MemoryRecord entry, List<MemoryRecord> candidates) {}

    // ── Private helpers ────────────────────────────────────────────────────────

    private long doUpsert(DSLContext ctx,
                           String tenant,
                           String project,
                           String title,
                           String content,
                           String tags,
                           String session,
                           String agent,
                           Integer ttlDays) {
        OffsetDateTime now = OffsetDateTime.now();

        /*
         * ON CONFLICT (tenant_id, project, title): the correct upsert key per
         * the .5 changelog comment.  Using (project, title) alone would bypass
         * tenant_id and allow cross-tenant row collisions.
         */
        var result = ctx.insertInto(MEMORY)
                        .set(MEMORY.TENANT_ID,    tenant)
                        .set(MEMORY.PROJECT,      project)
                        .set(MEMORY.TITLE,        title)
                        .set(MEMORY.CONTENT,      content)
                        .set(MEMORY.TAGS,         tags)
                        .set(MEMORY.SESSION,      session)
                        .set(MEMORY.AGENT,        agent)
                        .set(MEMORY.TIMESTAMP,    now)
                        .set(MEMORY.TTL,          ttlDays)
                        .set(MEMORY.ACCESS_COUNT, 0)
                        .onConflict(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE)
                        .doUpdate()
                        .set(MEMORY.CONTENT,      content)
                        .set(MEMORY.TAGS,         tags)
                        .set(MEMORY.SESSION,      session)
                        .set(MEMORY.AGENT,        agent)
                        .set(MEMORY.TIMESTAMP,    now)
                        .set(MEMORY.TTL,          ttlDays)
                        .returning(MEMORY.ID)
                        .fetchOne();

        long id = result != null ? result.getId() : -1L;
        log.debug("event=memory_upsert tenant={} project={} title={} id={}", tenant, project, title, id);
        return id;
    }
}
