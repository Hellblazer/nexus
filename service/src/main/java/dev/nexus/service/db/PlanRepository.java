package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.PlansRecord;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.PLANS;
import static org.jooq.impl.DSL.*;

/**
 * RDR-152 bead nexus-gmiaf.11 — jOOQ-based plan repository.
 *
 * <p>Mirrors {@code PlanLibrary} (SQLite) for the Postgres service tier.
 * All methods route through {@link TenantScope#withTenant} so every row
 * access is stamped with the tenant GUC and enforced by RLS.
 *
 * <p>FTS parity contract (Store 2, docs/rdr/rdr-152-fts-parity-contract.md, amended Option B 2026-06-07):
 * <ul>
 *   <li>{@code match_text} indexed with {@code 'english'} config (stemmed prose, weight A).
 *   <li>{@code tags} and {@code project} indexed with {@code 'simple'} config (identifier, weights B/C).
 *   <li>Query uses OR'd tsquery: {@code plainto_tsquery('english', ?) || plainto_tsquery('simple', ?)}
 *       so prose stems AND exact identifier tokens both match. PG results ⊇ FTS5 results.
 * </ul>
 *
 * <p>Metric columns ({@code use_count}, {@code match_count}, {@code match_conf_sum},
 * {@code success_count}, {@code failure_count}) must be preserved verbatim on ETL
 * import so the fidelity-import path uses {@code EXCLUDED.*} for all counter columns.
 */
public final class PlanRepository {

    private static final Logger log = LoggerFactory.getLogger(PlanRepository.class);

    /**
     * UTC second-precision formatter matching Python's
     * {@code datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}.
     */
    public static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

    private final TenantScope tenantScope;

    public PlanRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── Save / upsert ──────────────────────────────────────────────────────────

    /**
     * Insert a new plan or replace it on conflict of (tenant_id, project, query).
     *
     * <p>Unlike {@link #importRow}, which preserves source fidelity fields, this
     * method stamps {@code created_at=now()} and resets all counters to zero,
     * mirroring Python {@code PlanLibrary.save_plan}.
     *
     * @return the generated id of the inserted/updated row
     */
    public long savePlan(String tenant,
                         String project,
                         String query,
                         String planJson,
                         String outcome,
                         String tags,
                         Integer ttlDays,
                         String name,
                         String verb,
                         String scope,
                         String dimensions,
                         String defaultBindings,
                         String parentDims,
                         String scopeTags,
                         String matchText) {
        return tenantScope.withTenant(tenant, ctx -> doSave(
                ctx, tenant, project, query, planJson, outcome, tags,
                OffsetDateTime.now(ZoneOffset.UTC), ttlDays,
                name, verb, scope, dimensions, defaultBindings, parentDims,
                scopeTags, matchText));
    }

    // ── Get ────────────────────────────────────────────────────────────────────

    /**
     * Return a plan by numeric id, or empty if absent or RLS-filtered.
     */
    public Optional<PlansRecord> getById(String tenant, long id) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.selectFrom(PLANS)
                   .where(PLANS.ID.eq(id))
                   .fetchOptional());
    }

    /**
     * Return a plan by (project, dimensions) — mirrors
     * {@code PlanLibrary.get_plan_by_dimensions}. NULL dimensions are excluded
     * from the unique index; this method only looks for non-null dimensions.
     */
    public Optional<PlansRecord> getByDimensions(String tenant, String project, String dimensions) {
        if (dimensions == null) return Optional.empty();
        return tenantScope.withTenant(tenant, ctx ->
                ctx.selectFrom(PLANS)
                   .where(PLANS.PROJECT.eq(project)
                       .and(PLANS.DIMENSIONS.eq(dimensions)))
                   .limit(1)
                   .fetchOptional());
    }

    // ── Delete ─────────────────────────────────────────────────────────────────

    /**
     * Delete a plan by id. Returns true if a row was deleted.
     */
    public boolean delete(String tenant, long id) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.deleteFrom(PLANS)
                   .where(PLANS.ID.eq(id))
                   .execute() > 0);
    }

    // ── Disable / enable ───────────────────────────────────────────────────────

    /**
     * Soft-disable a plan by stamping {@code disabled_at = now()}.
     * When {@code reason} is non-empty, appends {@code disable-reason:<reason>}
     * to the {@code tags} column (replacing any existing disable-reason tag),
     * mirroring {@code PlanLibrary.set_plan_disabled}.
     *
     * @return true if the row was updated, false if the id does not exist
     */
    public boolean disable(String tenant, long id) {
        return disable(tenant, id, "");
    }

    /**
     * Soft-disable a plan, optionally tagging with a reason.
     * Mirrors {@code PlanLibrary.set_plan_disabled(reason=...)}.
     *
     * @param reason non-empty string appended as {@code disable-reason:<reason>} tag;
     *               empty or null means no tag change
     * @return true if the row was updated, false if the id does not exist
     */
    public boolean disable(String tenant, long id, String reason) {
        String trimmedReason = reason != null ? reason.trim() : "";
        return tenantScope.withTenant(tenant, ctx -> {
            if (trimmedReason.isEmpty()) {
                // No reason: just stamp disabled_at
                return ctx.update(PLANS)
                          .set(PLANS.DISABLED_AT, OffsetDateTime.now(ZoneOffset.UTC))
                          .where(PLANS.ID.eq(id))
                          .execute() > 0;
            }
            // Reason: fetch existing tags, remove old disable-reason:*, append new one
            var existing = ctx.select(PLANS.TAGS)
                              .from(PLANS)
                              .where(PLANS.ID.eq(id))
                              .fetchOne();
            if (existing == null) return false;
            String currentTags = existing.get(PLANS.TAGS);
            if (currentTags == null) currentTags = "";
            String newTags = appendDisableReason(currentTags, trimmedReason);
            return ctx.update(PLANS)
                      .set(PLANS.DISABLED_AT, OffsetDateTime.now(ZoneOffset.UTC))
                      .set(PLANS.TAGS, newTags)
                      .where(PLANS.ID.eq(id))
                      .execute() > 0;
        });
    }

    /**
     * Append (or replace) a {@code disable-reason:<reason>} tag in a comma-separated tag string.
     * Existing {@code disable-reason:*} tokens are removed first to avoid duplicates.
     * Mirrors Python {@code PlanLibrary.set_plan_disabled} tag logic.
     */
    static String appendDisableReason(String existingTags, String reason) {
        java.util.List<String> parts = new java.util.ArrayList<>();
        for (String t : existingTags.split(",")) {
            String trimmed = t.trim();
            if (!trimmed.isEmpty() && !trimmed.startsWith("disable-reason:")) {
                parts.add(trimmed);
            }
        }
        parts.add("disable-reason:" + reason);
        return String.join(", ", parts);
    }

    /**
     * Re-enable a previously disabled plan by clearing {@code disabled_at}.
     * Mirrors {@code PlanLibrary.set_plan_enabled}.
     *
     * @return true if the row was updated, false if the id does not exist
     */
    public boolean enable(String tenant, long id) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.update(PLANS)
                   .set(PLANS.DISABLED_AT, (OffsetDateTime) null)
                   .where(PLANS.ID.eq(id))
                   .execute() > 0);
    }

    // ── Scope tags ─────────────────────────────────────────────────────────────

    /**
     * Write explicit scope_tags for the plan with the given id.
     * Mirrors {@code PlanLibrary.set_scope_tags} (normalization applied Python-side).
     *
     * @return true if the row was updated, false if the id does not exist
     */
    public boolean setScopeTags(String tenant, long id, String scopeTags) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.update(PLANS)
                   .set(PLANS.SCOPE_TAGS, scopeTags != null ? scopeTags : "")
                   .where(PLANS.ID.eq(id))
                   .execute() > 0);
    }

    // ── List / search ──────────────────────────────────────────────────────────

    /**
     * Return all active (non-expired, non-disabled) plans for the given outcome,
     * ordered by created_at DESC. Mirrors {@code PlanLibrary.list_active_plans}.
     */
    public List<PlansRecord> listActivePlans(String tenant, String outcome, String project) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition expiry = PLANS.TTL.isNull().or(
                field("extract(epoch from now() - created_at) / 86400", Double.class)
                    .le(PLANS.TTL.cast(Double.class)));
            Condition active = PLANS.DISABLED_AT.isNull();
            Condition cond = PLANS.OUTCOME.eq(outcome).and(expiry).and(active);
            if (project != null && !project.isBlank()) {
                cond = cond.and(PLANS.PROJECT.eq(project));
            }
            return ctx.selectFrom(PLANS)
                      .where(cond)
                      .orderBy(PLANS.CREATED_AT.desc())
                      .fetch();
        });
    }

    /**
     * FTS search using the {@code fts_vector} GIN index.
     *
     * <p>Uses a dual-tsquery OR to cover both prose and identifier columns:
     * <ul>
     *   <li>{@code plainto_tsquery('english', ?)} — stems prose tokens (match_text column,
     *       weight A). "Indexing" → "index", "searches" → "search", etc.</li>
     *   <li>{@code plainto_tsquery('simple', ?)} — exact match for identifier/tag columns
     *       (tags, project, weights B/C).  "indexing" matches the stored simple lexeme
     *       "indexing" that English stemming cannot reach.</li>
     * </ul>
     * The OR union ensures PG ⊇ FTS5 (superset criterion per the amended FTS parity
     * contract, Option B decision, 2026-06-07).  ts_rank uses the same OR'd tsquery so
     * rows that satisfy either path receive a non-zero rank.
     *
     * <p>Skips expired and soft-disabled rows.
     * Mirrors {@code PlanLibrary.search_plans}.
     */
    public List<PlansRecord> searchPlans(String tenant, String query, String project, int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition expiry = PLANS.TTL.isNull().or(
                field("extract(epoch from now() - created_at) / 86400", Double.class)
                    .le(PLANS.TTL.cast(Double.class)));
            Condition active = PLANS.DISABLED_AT.isNull();
            // OR'd tsquery: prose stemming (english) + exact identifier match (simple)
            Condition fts = condition(
                "fts_vector @@ (plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0}))",
                val(query));
            Condition cond = fts.and(expiry).and(active);
            if (project != null && !project.isBlank()) {
                cond = cond.and(PLANS.PROJECT.eq(project));
            }
            return ctx.selectFrom(PLANS)
                      .where(cond)
                      .orderBy(field(
                          "ts_rank(fts_vector, plainto_tsquery('english', {0}) || plainto_tsquery('simple', {0}))",
                          Double.class, val(query)).desc())
                      .limit(limit)
                      .fetch();
        });
    }

    /**
     * Return most recent non-expired plans ordered by created_at DESC.
     * Soft-disabled rows are excluded unless {@code includeDisabled=true}.
     * Mirrors {@code PlanLibrary.list_plans}.
     */
    public List<PlansRecord> listPlans(String tenant, String project, int limit, boolean includeDisabled) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition expiry = PLANS.TTL.isNull().or(
                field("extract(epoch from now() - created_at) / 86400", Double.class)
                    .le(PLANS.TTL.cast(Double.class)));
            Condition cond = expiry;
            if (!includeDisabled) {
                cond = cond.and(PLANS.DISABLED_AT.isNull());
            }
            if (project != null && !project.isBlank()) {
                cond = cond.and(PLANS.PROJECT.eq(project));
            }
            return ctx.selectFrom(PLANS)
                      .where(cond)
                      .orderBy(PLANS.CREATED_AT.desc())
                      .limit(limit)
                      .fetch();
        });
    }

    /**
     * Return true if any plan with the given query has the given tag as a
     * comma-boundary-matched token. Mirrors {@code PlanLibrary.plan_exists}.
     */
    public boolean planExists(String tenant, String query, String tag) {
        return tenantScope.withTenant(tenant, ctx ->
                ctx.fetchExists(
                    ctx.selectOne().from(PLANS)
                       .where(PLANS.QUERY.eq(query)
                           .and(condition("(',' || tags || ',') LIKE {0}",
                                          val("%," + tag + ",%"))))
                       .limit(1)));
    }

    // ── Metric increment methods ───────────────────────────────────────────────

    /**
     * Increment {@code match_count} and (when {@code confidence != null}) add to
     * {@code match_conf_sum}. Mirrors {@code PlanLibrary.increment_match_metrics}.
     */
    public void incrementMatchMetrics(String tenant, long id, Double confidence) {
        tenantScope.withTenant(tenant, ctx -> {
            if (confidence == null) {
                ctx.update(PLANS)
                   .set(PLANS.MATCH_COUNT, PLANS.MATCH_COUNT.add(1))
                   .where(PLANS.ID.eq(id))
                   .execute();
            } else {
                ctx.update(PLANS)
                   .set(PLANS.MATCH_COUNT, PLANS.MATCH_COUNT.add(1))
                   .set(PLANS.MATCH_CONF_SUM, PLANS.MATCH_CONF_SUM.add(confidence))
                   .where(PLANS.ID.eq(id))
                   .execute();
            }
            return null;
        });
    }

    /**
     * Bump {@code use_count} and stamp {@code last_used}.
     * Mirrors {@code PlanLibrary.increment_run_started}.
     */
    public void incrementRunStarted(String tenant, long id) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(PLANS)
               .set(PLANS.USE_COUNT, PLANS.USE_COUNT.add(1))
               .set(PLANS.LAST_USED, OffsetDateTime.now(ZoneOffset.UTC))
               .where(PLANS.ID.eq(id))
               .execute();
            return null;
        });
    }

    /**
     * Bump {@code success_count} or {@code failure_count}.
     * Mirrors {@code PlanLibrary.increment_run_outcome}.
     *
     * @param success true to increment success_count, false for failure_count
     */
    public void incrementRunOutcome(String tenant, long id, boolean success) {
        tenantScope.withTenant(tenant, ctx -> {
            if (success) {
                ctx.update(PLANS)
                   .set(PLANS.SUCCESS_COUNT, PLANS.SUCCESS_COUNT.add(1))
                   .where(PLANS.ID.eq(id))
                   .execute();
            } else {
                ctx.update(PLANS)
                   .set(PLANS.FAILURE_COUNT, PLANS.FAILURE_COUNT.add(1))
                   .where(PLANS.ID.eq(id))
                   .execute();
            }
            return null;
        });
    }

    // ── Fidelity-preserving import (ETL path) ──────────────────────────────────

    /**
     * Fidelity-preserving import for ETL use (bead nexus-gmiaf.11, RDR-152 P2.1).
     *
     * <p>Unlike {@link #savePlan}, this method preserves source fidelity fields:
     * {@code created_at}, {@code use_count}, {@code last_used}, {@code match_count},
     * {@code match_conf_sum}, {@code success_count}, {@code failure_count}.
     *
     * <p>ON CONFLICT (tenant_id, project, query) propagates ALL source values
     * via {@code EXCLUDED.*} semantics so re-running is idempotent and metric
     * evolution is applied on content-change re-runs.
     *
     * @return the id of the inserted/updated row
     */
    public long importRow(String tenant,
                          String project,
                          String query,
                          String planJson,
                          String outcome,
                          String tags,
                          OffsetDateTime createdAt,
                          Integer ttlDays,
                          String name,
                          String verb,
                          String scope,
                          String dimensions,
                          String defaultBindings,
                          String parentDims,
                          int useCount,
                          OffsetDateTime lastUsed,
                          int matchCount,
                          double matchConfSum,
                          int successCount,
                          int failureCount,
                          String scopeTags,
                          String matchText,
                          OffsetDateTime disabledAt) {
        return tenantScope.withTenant(tenant, ctx -> doImport(
                ctx, tenant, project, query, planJson, outcome, tags,
                createdAt, ttlDays, name, verb, scope, dimensions, defaultBindings, parentDims,
                useCount, lastUsed, matchCount, matchConfSum, successCount, failureCount,
                scopeTags, matchText, disabledAt));
    }

    // ── Private helpers ────────────────────────────────────────────────────────

    private long doSave(DSLContext ctx,
                        String tenant,
                        String project,
                        String query,
                        String planJson,
                        String outcome,
                        String tags,
                        OffsetDateTime createdAt,
                        Integer ttlDays,
                        String name,
                        String verb,
                        String scope,
                        String dimensions,
                        String defaultBindings,
                        String parentDims,
                        String scopeTags,
                        String matchText) {
        String normTags  = tags       != null ? tags       : "";
        String normScope = scopeTags  != null ? scopeTags  : "";
        String normMatch = matchText  != null ? matchText  : "";
        String normOut   = outcome    != null ? outcome    : "success";

        var result = ctx.insertInto(PLANS)
                        .set(PLANS.TENANT_ID,        tenant)
                        .set(PLANS.PROJECT,          project != null ? project : "")
                        .set(PLANS.QUERY,            query)
                        .set(PLANS.PLAN_JSON,        planJson)
                        .set(PLANS.OUTCOME,          normOut)
                        .set(PLANS.TAGS,             normTags)
                        .set(PLANS.CREATED_AT,       createdAt)
                        .set(PLANS.TTL,              ttlDays)
                        .set(PLANS.NAME,             name)
                        .set(PLANS.VERB,             verb)
                        .set(PLANS.SCOPE,            scope)
                        .set(PLANS.DIMENSIONS,       dimensions)
                        .set(PLANS.DEFAULT_BINDINGS, defaultBindings)
                        .set(PLANS.PARENT_DIMS,      parentDims)
                        .set(PLANS.USE_COUNT,        0)
                        .set(PLANS.MATCH_COUNT,      0)
                        .set(PLANS.MATCH_CONF_SUM,   0.0)
                        .set(PLANS.SUCCESS_COUNT,    0)
                        .set(PLANS.FAILURE_COUNT,    0)
                        .set(PLANS.SCOPE_TAGS,       normScope)
                        .set(PLANS.MATCH_TEXT,       normMatch)
                        .onConflict(PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY)
                        .doUpdate()
                        .set(PLANS.PLAN_JSON,        planJson)
                        .set(PLANS.OUTCOME,          normOut)
                        .set(PLANS.TAGS,             normTags)
                        .set(PLANS.CREATED_AT,       createdAt)
                        .set(PLANS.TTL,              ttlDays)
                        .set(PLANS.NAME,             name)
                        .set(PLANS.VERB,             verb)
                        .set(PLANS.SCOPE,            scope)
                        .set(PLANS.DIMENSIONS,       dimensions)
                        .set(PLANS.DEFAULT_BINDINGS, defaultBindings)
                        .set(PLANS.PARENT_DIMS,      parentDims)
                        .set(PLANS.SCOPE_TAGS,       normScope)
                        .set(PLANS.MATCH_TEXT,       normMatch)
                        .returning(PLANS.ID)
                        .fetchOne();

        long id = result != null ? result.getId() : -1L;
        log.debug("event=plan_save tenant={} project={} id={}", tenant, project, id);
        return id;
    }

    private long doImport(DSLContext ctx,
                          String tenant,
                          String project,
                          String query,
                          String planJson,
                          String outcome,
                          String tags,
                          OffsetDateTime createdAt,
                          Integer ttlDays,
                          String name,
                          String verb,
                          String scope,
                          String dimensions,
                          String defaultBindings,
                          String parentDims,
                          int useCount,
                          OffsetDateTime lastUsed,
                          int matchCount,
                          double matchConfSum,
                          int successCount,
                          int failureCount,
                          String scopeTags,
                          String matchText,
                          OffsetDateTime disabledAt) {
        String normTags  = tags      != null ? tags      : "";
        String normScope = scopeTags != null ? scopeTags : "";
        String normMatch = matchText != null ? matchText : "";
        String normOut   = outcome   != null ? outcome   : "success";

        var result = ctx.insertInto(PLANS)
                        .set(PLANS.TENANT_ID,        tenant)
                        .set(PLANS.PROJECT,          project != null ? project : "")
                        .set(PLANS.QUERY,            query)
                        .set(PLANS.PLAN_JSON,        planJson)
                        .set(PLANS.OUTCOME,          normOut)
                        .set(PLANS.TAGS,             normTags)
                        .set(PLANS.CREATED_AT,       createdAt)
                        .set(PLANS.TTL,              ttlDays)
                        .set(PLANS.NAME,             name)
                        .set(PLANS.VERB,             verb)
                        .set(PLANS.SCOPE,            scope)
                        .set(PLANS.DIMENSIONS,       dimensions)
                        .set(PLANS.DEFAULT_BINDINGS, defaultBindings)
                        .set(PLANS.PARENT_DIMS,      parentDims)
                        .set(PLANS.USE_COUNT,        useCount)
                        .set(PLANS.LAST_USED,        lastUsed)
                        .set(PLANS.MATCH_COUNT,      matchCount)
                        .set(PLANS.MATCH_CONF_SUM,   matchConfSum)
                        .set(PLANS.SUCCESS_COUNT,    successCount)
                        .set(PLANS.FAILURE_COUNT,    failureCount)
                        .set(PLANS.SCOPE_TAGS,       normScope)
                        .set(PLANS.MATCH_TEXT,       normMatch)
                        .set(PLANS.DISABLED_AT,      disabledAt)
                        .onConflict(PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY)
                        .doUpdate()
                        // Content fields: propagate source values (content changes allowed)
                        .set(PLANS.PLAN_JSON,        excluded(PLANS.PLAN_JSON))
                        .set(PLANS.OUTCOME,          excluded(PLANS.OUTCOME))
                        .set(PLANS.TAGS,             excluded(PLANS.TAGS))
                        .set(PLANS.TTL,              excluded(PLANS.TTL))
                        .set(PLANS.NAME,             excluded(PLANS.NAME))
                        .set(PLANS.VERB,             excluded(PLANS.VERB))
                        .set(PLANS.SCOPE,            excluded(PLANS.SCOPE))
                        .set(PLANS.DIMENSIONS,       excluded(PLANS.DIMENSIONS))
                        .set(PLANS.DEFAULT_BINDINGS, excluded(PLANS.DEFAULT_BINDINGS))
                        .set(PLANS.PARENT_DIMS,      excluded(PLANS.PARENT_DIMS))
                        .set(PLANS.SCOPE_TAGS,       excluded(PLANS.SCOPE_TAGS))
                        .set(PLANS.MATCH_TEXT,       excluded(PLANS.MATCH_TEXT))
                        // created_at: immutable once set, keep source value
                        .set(PLANS.CREATED_AT,       excluded(PLANS.CREATED_AT))
                        // disabled_at: live-mutable — keep whichever is non-null (PG-side wins)
                        .set(PLANS.DISABLED_AT,
                             coalesce(PLANS.DISABLED_AT, excluded(PLANS.DISABLED_AT)))
                        // Live-mutable monotonic counters: GREATEST(source, live) so idempotent
                        // re-runs after live traffic never roll back PG-advanced counters.
                        // During initial seeding PG=0 so source wins; after live traffic PG>source.
                        .set(PLANS.USE_COUNT,
                             greatest(excluded(PLANS.USE_COUNT), PLANS.USE_COUNT))
                        .set(PLANS.MATCH_COUNT,
                             greatest(excluded(PLANS.MATCH_COUNT), PLANS.MATCH_COUNT))
                        .set(PLANS.MATCH_CONF_SUM,
                             greatest(excluded(PLANS.MATCH_CONF_SUM), PLANS.MATCH_CONF_SUM))
                        .set(PLANS.SUCCESS_COUNT,
                             greatest(excluded(PLANS.SUCCESS_COUNT), PLANS.SUCCESS_COUNT))
                        .set(PLANS.FAILURE_COUNT,
                             greatest(excluded(PLANS.FAILURE_COUNT), PLANS.FAILURE_COUNT))
                        // last_used: keep the later timestamp (GREATEST is null-safe: GREATEST(null,x)=x)
                        .set(PLANS.LAST_USED,
                             greatest(excluded(PLANS.LAST_USED), PLANS.LAST_USED))
                        .returning(PLANS.ID)
                        .fetchOne();

        long id = result != null ? result.getId() : -1L;
        log.debug("event=plan_import tenant={} project={} query_prefix={} id={}",
                  tenant, project, query.length() > 40 ? query.substring(0, 40) : query, id);
        return id;
    }

    // ── recordToMap helper (for HTTP serialization) ───────────────────────────

    /**
     * Convert a {@link PlansRecord} to a serialization-friendly map.
     * Mirrors Python {@code _row_to_dict} column ordering.
     * Timestamp fields are formatted to UTC second precision.
     */
    public static java.util.Map<String, Object> recordToMap(PlansRecord r) {
        var m = new java.util.LinkedHashMap<String, Object>();
        m.put("id",               r.getId());
        m.put("project",          r.getProject() != null ? r.getProject() : "");
        m.put("query",            r.getQuery());
        m.put("plan_json",        r.getPlanJson());
        m.put("outcome",          r.getOutcome() != null ? r.getOutcome() : "success");
        m.put("tags",             r.getTags() != null ? r.getTags() : "");
        m.put("created_at",       r.getCreatedAt() != null
                                   ? UTC_SECOND.format(r.getCreatedAt().withOffsetSameInstant(ZoneOffset.UTC))
                                   : null);
        m.put("ttl",              r.getTtl());
        m.put("name",             r.getName());
        m.put("verb",             r.getVerb());
        m.put("scope",            r.getScope());
        m.put("dimensions",       r.getDimensions());
        m.put("default_bindings", r.getDefaultBindings());
        m.put("parent_dims",      r.getParentDims());
        m.put("use_count",        r.getUseCount() != null ? r.getUseCount() : 0);
        m.put("last_used",        r.getLastUsed() != null
                                   ? UTC_SECOND.format(r.getLastUsed().withOffsetSameInstant(ZoneOffset.UTC))
                                   : null);
        m.put("match_count",      r.getMatchCount() != null ? r.getMatchCount() : 0);
        m.put("match_conf_sum",   r.getMatchConfSum() != null ? r.getMatchConfSum() : 0.0);
        m.put("success_count",    r.getSuccessCount() != null ? r.getSuccessCount() : 0);
        m.put("failure_count",    r.getFailureCount() != null ? r.getFailureCount() : 0);
        m.put("scope_tags",       r.getScopeTags() != null ? r.getScopeTags() : "");
        m.put("match_text",       r.getMatchText() != null ? r.getMatchText() : "");
        m.put("disabled_at",      r.getDisabledAt() != null
                                   ? UTC_SECOND.format(r.getDisabledAt().withOffsetSameInstant(ZoneOffset.UTC))
                                   : null);
        return m;
    }
}
