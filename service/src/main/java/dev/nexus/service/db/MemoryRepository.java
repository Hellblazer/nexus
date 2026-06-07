package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.Memory;
import dev.nexus.service.jooq.nexus.tables.records.MemoryRecord;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Result;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static org.jooq.impl.DSL.*;

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

    /**
     * UTC second-precision ISO-8601 formatter matching Python's
     * {@code datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}.
     *
     * <p>Used for {@code timestamp} and {@code last_accessed} so string
     * compares and the .9 parity harness see identical formats on both
     * sides of the seam.
     */
    public static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

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
     *
     * <p>Increments {@code access_count} and sets {@code last_accessed} on the
     * returned row, mirroring Python {@code MemoryStore.get(project=, title=)}.
     *
     * @return the entry with updated access tracking, or empty if absent / RLS-filtered
     */
    public Optional<MemoryRecord> findByTitle(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant, ctx -> {
            var opt = ctx.selectFrom(MEMORY)
                         .where(MEMORY.PROJECT.eq(project)
                             .and(MEMORY.TITLE.eq(title)))
                         .fetchOptional();
            opt.ifPresent(row -> {
                trackAccess(ctx, row.getId());
                row.setAccessCount(row.getAccessCount() + 1);
                row.setLastAccessed(OffsetDateTime.now(ZoneOffset.UTC));
            });
            return opt;
        });
    }

    /**
     * Find a single memory entry by its numeric id.
     *
     * <p>Increments {@code access_count} and sets {@code last_accessed},
     * mirroring Python {@code MemoryStore.get(id=)}.
     *
     * @return the entry with updated access tracking, or empty if absent / RLS-filtered
     */
    public Optional<MemoryRecord> findById(String tenant, long id) {
        return tenantScope.withTenant(tenant, ctx -> {
            var opt = ctx.selectFrom(MEMORY)
                         .where(MEMORY.ID.eq(id))
                         .fetchOptional();
            opt.ifPresent(row -> {
                trackAccess(ctx, row.getId());
                row.setAccessCount(row.getAccessCount() + 1);
                row.setLastAccessed(OffsetDateTime.now(ZoneOffset.UTC));
            });
            return opt;
        });
    }

    /**
     * Exact-then-prefix title resolution (mirrors Python resolve_title logic).
     *
     * <p>Returns {@code ResolveResult(entry, [])} on exact or unique-prefix match;
     * {@code ResolveResult(null, candidates)} when multiple prefix matches exist;
     * {@code ResolveResult(null, [])} when nothing matches.
     *
     * <p>Access tracking: increments {@code access_count} and sets
     * {@code last_accessed} when a unique entry is returned (exact or unique prefix),
     * mirroring Python {@code MemoryStore.resolve_title} which calls {@code get()} for
     * unique results and {@code get()} always tracks.
     */
    public ResolveResult resolveTitle(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant, ctx -> {
            // 1. Exact match — track access
            var exact = ctx.selectFrom(MEMORY)
                           .where(MEMORY.PROJECT.eq(project).and(MEMORY.TITLE.eq(title)))
                           .fetchOptional();
            if (exact.isPresent()) {
                MemoryRecord row = exact.get();
                trackAccess(ctx, row.getId());
                row.setAccessCount(row.getAccessCount() + 1);
                row.setLastAccessed(OffsetDateTime.now(ZoneOffset.UTC));
                return new ResolveResult(row, List.of());
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
                MemoryRecord row = candidates.get(0);
                trackAccess(ctx, row.getId());
                row.setAccessCount(row.getAccessCount() + 1);
                row.setLastAccessed(OffsetDateTime.now(ZoneOffset.UTC));
                return new ResolveResult(row, List.of());
            }
            return new ResolveResult(null, candidates);
        });
    }

    /**
     * FTS search using the {@code fts_vector} GIN index.
     *
     * <p>Uses a dual-tsquery OR to cover both prose and identifier columns:
     * <ul>
     *   <li>{@code plainto_tsquery('english', ?)} — stems prose tokens (title/content
     *       columns, weights A/C). "Indexing" → "index", etc.</li>
     *   <li>{@code plainto_tsquery('simple', ?)} — exact match for the tags column
     *       (weight B, stored with simple tokenizer). "indexing" matches stored
     *       "indexing" that English stemming cannot reach.</li>
     * </ul>
     * The OR union ensures PG ⊇ FTS5 (superset criterion, Option B decision 2026-06-07).
     * Mirrors {@code docs/rdr/rdr-152-fts-parity-contract.md} Store 1, amended §OPTION-B.
     *
     * <p>Access tracking: when {@code trackAccess=true} (default — mirrors
     * Python {@code access="track"}), increments {@code access_count} and sets
     * {@code last_accessed} on every returned row.  Pass {@code false} for
     * internal scans that must not contaminate the staleness signal
     * (mirrors Python {@code access="silent"}).
     *
     * @param tenant       tenant scope
     * @param query        prose search query (sanitized by plainto_tsquery)
     * @param project      optional project filter; null or blank = all projects
     * @param trackAccess  true to increment access_count + set last_accessed on hits
     */
    public List<MemoryRecord> search(String tenant, String query, String project, boolean trackAccess) {
        return tenantScope.withTenant(tenant, ctx -> {
            // OR'd tsquery: prose stemming (english) + exact identifier/tag match (simple).
            // plainto_tsquery is injection-safe: bound via {0}.
            String ftsWhere = project != null && !project.isBlank()
                ? "fts_vector @@ (plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0})) AND project = {1}"
                : "fts_vector @@ (plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0}))";

            Result<MemoryRecord> rows;
            if (project != null && !project.isBlank()) {
                rows = ctx.selectFrom(MEMORY)
                          .where(condition(ftsWhere, val(query), val(project)))
                          .orderBy(field("ts_rank(fts_vector, plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0}))", Double.class, val(query)).desc())
                          .fetch();
            } else {
                rows = ctx.selectFrom(MEMORY)
                          .where(condition(ftsWhere, val(query)))
                          .orderBy(field("ts_rank(fts_vector, plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0}))", Double.class, val(query)).desc())
                          .fetch();
            }
            if (trackAccess && !rows.isEmpty()) {
                List<Long> ids = rows.map(r -> r.getId());
                batchTrackAccess(ctx, ids);
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                for (var r : rows) {
                    r.setAccessCount(r.getAccessCount() + 1);
                    r.setLastAccessed(now);
                }
            }
            return rows;
        });
    }

    /**
     * Convenience overload with {@code trackAccess=true} (default, mirrors Python
     * {@code search(query, access="track")}).
     */
    public List<MemoryRecord> search(String tenant, String query, String project) {
        return search(tenant, query, project, true);
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
                String luStr = lu != null ? UTC_SECOND.format(lu.withOffsetSameInstant(ZoneOffset.UTC)) : null;
                result.add(new String[]{r.get(MEMORY.PROJECT), luStr});
            }
            return result;
        });
    }

    /**
     * FTS search scoped to projects matching a GLOB pattern.
     *
     * <p>Mirrors Python {@code search_glob} using SQL LIKE (converts {@code *} to
     * {@code %} and escapes LIKE metacharacters). Uses dual-tsquery OR (english || simple)
     * per the amended FTS parity contract (Option B, 2026-06-07).
     * For GLOB semantics: {@code *} → {@code %}, {@code ?} → {@code _}.
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
                          "fts_vector @@ (plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0})) AND project LIKE {1} ESCAPE '\\'",
                          val(query), val(likePattern)))
                      .orderBy(field("ts_rank(fts_vector, plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0}))", Double.class, val(query)).desc())
                      .fetch();
        });
    }

    /**
     * FTS search scoped to entries whose tags contain {@code tag} exactly (boundary-matched).
     *
     * <p>Mirrors Python {@code search_by_tag} using
     * {@code (',' || tags || ',') LIKE '%,tag,%'}. Uses dual-tsquery OR (english || simple)
     * per the amended FTS parity contract (Option B, 2026-06-07).
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
                          "fts_vector @@ (plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0})) AND (',' || tags || ',') LIKE {1} ESCAPE '\\'",
                          val(query), val(likePattern)))
                      .orderBy(field("ts_rank(fts_vector, plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0}))", Double.class, val(query)).desc())
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
     * Atomic merge: update content + refresh timestamp of {@code keepId} and delete all {@code deleteIds}.
     *
     * <p>The timestamp refresh mirrors Python {@code MemoryStore.put_or_merge} which
     * does {@code UPDATE memory SET content = ?, timestamp = ? WHERE id = ?}. Without
     * a timestamp update, merged entries lose their TTL extension
     * (heat-weighted expire would see the original creation time, not the merge time).
     *
     * <p>Raises {@code IllegalArgumentException} if keepId is in deleteIds.
     * Raises {@code IllegalStateException} if keepId does not exist.
     */
    public void mergeMemories(String tenant, long keepId, List<Long> deleteIds, String mergedContent) {
        if (deleteIds.contains(keepId)) {
            throw new IllegalArgumentException(
                "keepId (" + keepId + ") must not be in deleteIds — would discard the entry meant to be kept");
        }
        tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            int updated = ctx.update(MEMORY)
                             .set(MEMORY.CONTENT,   mergedContent)
                             .set(MEMORY.TIMESTAMP, now)
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

    /**
     * Jaccard-overlap scan + conditional merge or insert, executed atomically in one
     * {@link TenantScope#withTenant} transaction.
     *
     * <p>Server-side: consolidates the Python {@code MemoryStore.put_or_merge} client-side
     * path into a single RPC call, eliminating the TOCTOU window that the two-step
     * client-composed variant (get_all → merge_memories) would introduce.
     *
     * <p>Algorithm (mirrors Python exactly):
     * <ol>
     *   <li>Compute the word set for {@code content} (lower-cased, length &gt; 2, stopwords removed).</li>
     *   <li>Scan all existing entries in {@code project} whose title ≠ {@code title}.</li>
     *   <li>Compute Jaccard similarity on word sets.</li>
     *   <li>If best Jaccard ≥ {@code minSimilarity}: UPDATE the best-match entry with
     *       appended content + provenance comment + refreshed timestamp.
     *       Return {@code (bestId, "merged")}.</li>
     *   <li>Otherwise: upsert the new entry via {@code doUpsert}.
     *       Return {@code (rowId, "inserted")}.</li>
     * </ol>
     *
     * @return {@code long[2]} where {@code [0]} is the row id and
     *         {@code [1]} is {@code 0L} for inserted or {@code 1L} for merged.
     */
    public long[] putOrMerge(String tenant,
                             String project,
                             String title,
                             String content,
                             String tags,
                             String session,
                             String agent,
                             Integer ttlDays,
                             double minSimilarity) {
        return tenantScope.withTenant(tenant, ctx -> {
            Set<String> newWords = contentWords(content);
            if (!newWords.isEmpty()) {
                // Scan all project entries except same-title (identity upsert path)
                var existing = ctx.selectFrom(MEMORY)
                                  .where(MEMORY.PROJECT.eq(project)
                                      .and(MEMORY.TITLE.ne(title)))
                                  .fetch();
                long bestId = -1L;
                double bestJaccard = 0.0;
                String bestContent = "";

                for (var row : existing) {
                    Set<String> ew = contentWords(row.getContent() != null ? row.getContent() : "");
                    if (ew.isEmpty()) continue;
                    Set<String> union = new java.util.HashSet<>(newWords);
                    union.addAll(ew);
                    Set<String> inter = new java.util.HashSet<>(newWords);
                    inter.retainAll(ew);
                    double j = (double) inter.size() / union.size();
                    if (j > bestJaccard) {
                        bestJaccard = j;
                        bestId = row.getId();
                        bestContent = row.getContent() != null ? row.getContent() : "";
                    }
                }

                if (bestId >= 0 && bestJaccard >= minSimilarity) {
                    OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                    String ts = UTC_SECOND.format(now);
                    String merged = bestContent
                        + "\n\n<!-- merged from " + escapeHtmlAttr(title) + " @ " + ts
                        + " (jaccard=" + String.format("%.2f", bestJaccard) + ") -->\n"
                        + content;
                    ctx.update(MEMORY)
                       .set(MEMORY.CONTENT,   merged)
                       .set(MEMORY.TIMESTAMP, now)
                       .where(MEMORY.ID.eq(bestId))
                       .execute();
                    return new long[]{bestId, 1L}; // 1 = merged
                }
            }
            // Normal upsert
            long rowId = doUpsert(ctx, tenant, project, title, content, tags, session, agent, ttlDays);
            return new long[]{rowId, 0L}; // 0 = inserted
        });
    }

    // ── Result type for resolve ────────────────────────────────────────────────

    /**
     * Return value for {@link #resolveTitle}: either a unique match or a list of candidates.
     */
    public record ResolveResult(MemoryRecord entry, List<MemoryRecord> candidates) {}

    // ── Private helpers ────────────────────────────────────────────────────────

    /**
     * Stopwords shared with Python {@code MemoryStore._STOPWORDS} for Jaccard computation.
     */
    private static final java.util.Set<String> STOPWORDS = java.util.Set.of(
        "the", "a", "an", "in", "of", "for", "to", "and", "or", "is", "are", "was",
        "it", "that", "this", "with", "on", "at", "by", "from", "as", "be", "not"
    );

    /** Compute lowercased content word set for Jaccard (mirrors Python _content_words). */
    private static Set<String> contentWords(String text) {
        if (text == null || text.isBlank()) return java.util.Set.of();
        Set<String> words = new java.util.HashSet<>();
        for (String w : text.split("\\s+")) {
            String lower = w.toLowerCase();
            if (lower.length() > 2 && !STOPWORDS.contains(lower)) {
                words.add(lower);
            }
        }
        return words;
    }

    /** Escape {@code '} for embedding in an HTML-comment provenance string. */
    private static String escapeHtmlAttr(String s) {
        return s == null ? "" : s.replace("'", "\\'");
    }

    /**
     * Increment access_count and set last_accessed for a single row.
     * Best-effort: does NOT throw on failure (mirrors Python's SQLITE_BUSY skip).
     */
    private void trackAccess(DSLContext ctx, long id) {
        try {
            ctx.update(MEMORY)
               .set(MEMORY.ACCESS_COUNT, MEMORY.ACCESS_COUNT.add(1))
               .set(MEMORY.LAST_ACCESSED, OffsetDateTime.now(ZoneOffset.UTC))
               .where(MEMORY.ID.eq(id))
               .execute();
        } catch (Exception e) {
            log.debug("event=access_tracking_skipped id={} reason={}", id, e.getMessage());
        }
    }

    /**
     * Batch-increment access_count and set last_accessed for multiple rows.
     * Best-effort: does NOT throw on failure.
     */
    private void batchTrackAccess(DSLContext ctx, List<Long> ids) {
        if (ids.isEmpty()) return;
        try {
            ctx.update(MEMORY)
               .set(MEMORY.ACCESS_COUNT, MEMORY.ACCESS_COUNT.add(1))
               .set(MEMORY.LAST_ACCESSED, OffsetDateTime.now(ZoneOffset.UTC))
               .where(MEMORY.ID.in(ids))
               .execute();
        } catch (Exception e) {
            log.debug("event=batch_access_tracking_skipped count={} reason={}", ids.size(), e.getMessage());
        }
    }

    /**
     * Fidelity-preserving import for ETL use (bead nexus-gmiaf.8, RDR-152 P1.8).
     *
     * <p>Unlike {@link #upsert}, which stamps {@code timestamp=now()} and resets
     * {@code access_count=0}, this method copies the source row's
     * {@code timestamp}, {@code access_count}, and {@code last_accessed} verbatim.
     *
     * <p>ON CONFLICT (tenant_id, project, title) DO UPDATE propagates the source
     * values for ALL fields including timestamp/access_count/last_accessed via
     * {@code EXCLUDED.*} semantics — so re-running the ETL with the same source
     * data is a no-op, and running it after a source content change applies the
     * new content while preserving the source timestamp.
     *
     * <p>Still routes through {@link TenantScope#withTenant} so RLS and tenant
     * stamping are enforced identically to normal upserts.
     *
     * @param tenant       tenant to stamp (DEFAULT_TENANT for single-tenant SQLite migration)
     * @param project      source project namespace
     * @param title        source entry title (upsert key with project)
     * @param content      source entry content
     * @param tags         source tags; null normalised to ""
     * @param session      source session (may be null)
     * @param agent        source agent (may be null)
     * @param ttlDays      source TTL in days (may be null)
     * @param timestamp    source creation timestamp (non-null; required for fidelity)
     * @param accessCount  source access count (0 if never accessed)
     * @param lastAccessed source last-accessed timestamp (null when source value was "")
     * @return the id of the inserted/updated row
     */
    public long importRow(String tenant,
                          String project,
                          String title,
                          String content,
                          String tags,
                          String session,
                          String agent,
                          Integer ttlDays,
                          OffsetDateTime timestamp,
                          int accessCount,
                          OffsetDateTime lastAccessed) {
        return tenantScope.withTenant(tenant, ctx -> doImport(
                ctx, tenant, project, title, content, tags, session, agent,
                ttlDays, timestamp, accessCount, lastAccessed));
    }

    private long doImport(DSLContext ctx,
                          String tenant,
                          String project,
                          String title,
                          String content,
                          String tags,
                          String session,
                          String agent,
                          Integer ttlDays,
                          OffsetDateTime timestamp,
                          int accessCount,
                          OffsetDateTime lastAccessed) {
        String normalizedTags = tags != null ? tags : "";

        var result = ctx.insertInto(MEMORY)
                        .set(MEMORY.TENANT_ID,    tenant)
                        .set(MEMORY.PROJECT,      project)
                        .set(MEMORY.TITLE,        title)
                        .set(MEMORY.CONTENT,      content)
                        .set(MEMORY.TAGS,         normalizedTags)
                        .set(MEMORY.SESSION,      session)
                        .set(MEMORY.AGENT,        agent)
                        .set(MEMORY.TIMESTAMP,    timestamp)
                        .set(MEMORY.TTL,          ttlDays)
                        .set(MEMORY.ACCESS_COUNT, accessCount)
                        .set(MEMORY.LAST_ACCESSED, lastAccessed)
                        .onConflict(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE)
                        .doUpdate()
                        // Content fields: propagate source values (idempotent on same data,
                        // applies source mutations on re-run)
                        .set(MEMORY.CONTENT,       excluded(MEMORY.CONTENT))
                        .set(MEMORY.TAGS,          excluded(MEMORY.TAGS))
                        .set(MEMORY.SESSION,       excluded(MEMORY.SESSION))
                        .set(MEMORY.AGENT,         excluded(MEMORY.AGENT))
                        .set(MEMORY.TTL,           excluded(MEMORY.TTL))
                        // Fidelity fields: copy verbatim from source (not now(), not 0)
                        .set(MEMORY.TIMESTAMP,     excluded(MEMORY.TIMESTAMP))
                        .set(MEMORY.ACCESS_COUNT,  excluded(MEMORY.ACCESS_COUNT))
                        .set(MEMORY.LAST_ACCESSED, excluded(MEMORY.LAST_ACCESSED))
                        .returning(MEMORY.ID)
                        .fetchOne();

        long id = result != null ? result.getId() : -1L;
        log.debug("event=memory_import tenant={} project={} title={} id={}", tenant, project, title, id);
        return id;
    }

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
        // Normalize tags: always store "" not NULL so Python callers can do entry["tags"]
        // without a .get("tags", "") default — matches SQLite column default ''.
        String normalizedTags = tags != null ? tags : "";

        var result = ctx.insertInto(MEMORY)
                        .set(MEMORY.TENANT_ID,    tenant)
                        .set(MEMORY.PROJECT,      project)
                        .set(MEMORY.TITLE,        title)
                        .set(MEMORY.CONTENT,      content)
                        .set(MEMORY.TAGS,         normalizedTags)
                        .set(MEMORY.SESSION,      session)
                        .set(MEMORY.AGENT,        agent)
                        .set(MEMORY.TIMESTAMP,    now)
                        .set(MEMORY.TTL,          ttlDays)
                        .set(MEMORY.ACCESS_COUNT, 0)
                        .onConflict(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE)
                        .doUpdate()
                        .set(MEMORY.CONTENT,      content)
                        .set(MEMORY.TAGS,         normalizedTags)
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
