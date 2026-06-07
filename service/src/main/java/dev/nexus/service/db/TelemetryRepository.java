package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.FrecencyRecord;
import dev.nexus.service.jooq.nexus.tables.records.RelevanceLogRecord;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.*;
import static org.jooq.impl.DSL.*;

/**
 * RDR-152 bead nexus-gmiaf.12 — jOOQ-based telemetry repository.
 *
 * <p>Covers all six telemetry tables:
 * <ul>
 *   <li>{@code relevance_log}    — event log; mirrors {@code Telemetry.log_relevance} etc.</li>
 *   <li>{@code search_telemetry} — event log; composite PK.</li>
 *   <li>{@code tier_writes}      — event log; tier-discipline audit.</li>
 *   <li>{@code nx_answer_runs}   — event log; RDR-080 run metrics.</li>
 *   <li>{@code hook_failures}    — event log; post-store hook audit.</li>
 *   <li>{@code frecency}         — live-mutable; GREATEST on conflict.</li>
 * </ul>
 *
 * <p>Import conflict strategy (relay mandate):
 * <ul>
 *   <li>Event logs: {@code ON CONFLICT ... DO NOTHING} — event timestamps are the data;
 *       never overwrite a historical event.</li>
 *   <li>frecency (live-mutable): {@code ON CONFLICT ... DO UPDATE} with
 *       {@code GREATEST()} for counters/scores/timestamps;
 *       {@code LEAST()} for {@code embedded_at} (keep oldest embed time).</li>
 * </ul>
 */
public final class TelemetryRepository {

    private static final Logger log = LoggerFactory.getLogger(TelemetryRepository.class);

    /** UTC formatter matching Python's ISO-8601 strings. */
    static final DateTimeFormatter UTC_SECOND =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'")
                             .withZone(ZoneOffset.UTC);

    private final TenantScope tenantScope;

    public TelemetryRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    /**
     * Parse an ISO-8601 text timestamp (live-write lenient path).
     * Accepts both "...Z" and "...+00:00" forms.
     * Returns {@code now()} on null/blank/malformed — safe for live-write callers
     * where the event time is "right now" and a timestamp field must not be null.
     *
     * <p><strong>DO NOT use for import paths</strong> — use {@link #parseTsStrict}
     * there so corrupt source data fails loudly instead of silently stamping
     * migration-time.
     */
    static OffsetDateTime parseTs(String s) {
        if (s == null || s.isBlank()) return OffsetDateTime.now(ZoneOffset.UTC);
        try {
            return OffsetDateTime.parse(s.endsWith("Z")
                ? s.replace("Z", "+00:00") : s);
        } catch (DateTimeParseException e) {
            log.warn("event=telemetry_parse_ts_failed raw=\"{}\"", s);
            return OffsetDateTime.now(ZoneOffset.UTC);
        }
    }

    /**
     * Parse an ISO-8601 text timestamp (ETL import strict path).
     * Accepts both "...Z" and "...+00:00" forms.
     * <strong>Throws {@link IllegalArgumentException}</strong> on null/blank/malformed
     * input so callers fail loudly (no silent now()-substitution on the import path).
     *
     * <p>Event-time IS the data on import — substituting migration-time on a parse
     * failure would corrupt the historical audit trail.  The ETL layer must surface
     * the bad row rather than silently misdating it.
     */
    static OffsetDateTime parseTsStrict(String s) {
        if (s == null || s.isBlank()) {
            throw new IllegalArgumentException(
                "import timestamp must not be null/blank (event-time is the data)");
        }
        try {
            return OffsetDateTime.parse(s.endsWith("Z")
                ? s.replace("Z", "+00:00") : s);
        } catch (DateTimeParseException e) {
            throw new IllegalArgumentException(
                "import timestamp is not valid ISO-8601: \"" + s + "\"", e);
        }
    }

    /** Normalise null/blank string to empty string. */
    private static String str(String v) {
        return v != null ? v : "";
    }

    // ── relevance_log ──────────────────────────────────────────────────────────

    /**
     * Append one relevance event. Returns the generated id.
     */
    public long logRelevance(String tenant,
                             String query,
                             String chunkId,
                             String action,
                             String sessionId,
                             String collection) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            // fetchOptional() guards against the DO NOTHING path: when the unique index
            // idx_relevance_log_etl_dedup fires a conflict, jOOQ returns an empty result
            // (no row inserted, no id generated).  fetchOne() would NPE there.
            return ctx.insertInto(RELEVANCE_LOG)
                .set(RELEVANCE_LOG.TENANT_ID, tenant)
                .set(RELEVANCE_LOG.QUERY, query)
                .set(RELEVANCE_LOG.CHUNK_ID, chunkId)
                .set(RELEVANCE_LOG.ACTION, action)
                .set(RELEVANCE_LOG.SESSION_ID, str(sessionId))
                .set(RELEVANCE_LOG.COLLECTION, str(collection))
                .set(RELEVANCE_LOG.TIMESTAMP, now)
                .onConflictDoNothing()
                .returningResult(RELEVANCE_LOG.ID)
                .fetchOptional()
                .map(r -> r.value1())
                .orElse(0L);
        });
    }

    /**
     * Batch-insert relevance events in one transaction.
     * Row tuples: (query, chunkId, collection, action, sessionId).
     * Returns number of rows attempted (not inserted — DO NOTHING may skip dupes).
     */
    public int logRelevanceBatch(String tenant,
                                 List<List<String>> rows) {
        if (rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            var step = ctx.insertInto(RELEVANCE_LOG,
                RELEVANCE_LOG.TENANT_ID,
                RELEVANCE_LOG.QUERY,
                RELEVANCE_LOG.CHUNK_ID,
                RELEVANCE_LOG.COLLECTION,
                RELEVANCE_LOG.ACTION,
                RELEVANCE_LOG.SESSION_ID,
                RELEVANCE_LOG.TIMESTAMP);
            for (var r : rows) {
                step = step.values(tenant,
                    r.get(0), r.get(1), r.size() > 2 ? r.get(2) : "",
                    r.get(3), r.size() > 4 ? r.get(4) : "", now);
            }
            return step.onConflictDoNothing().execute();
        });
    }

    /**
     * Query the relevance log with optional filters.
     * Returns rows ordered by timestamp DESC.
     */
    public List<Map<String, Object>> getRelevanceLog(String tenant,
                                                      String query,
                                                      String chunkId,
                                                      String action,
                                                      String sessionId,
                                                      int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var cond = noCondition();
            if (query != null && !query.isBlank()) cond = cond.and(RELEVANCE_LOG.QUERY.eq(query));
            if (chunkId != null && !chunkId.isBlank()) cond = cond.and(RELEVANCE_LOG.CHUNK_ID.eq(chunkId));
            if (action != null && !action.isBlank()) cond = cond.and(RELEVANCE_LOG.ACTION.eq(action));
            if (sessionId != null && !sessionId.isBlank()) cond = cond.and(RELEVANCE_LOG.SESSION_ID.eq(sessionId));

            return ctx.select(
                RELEVANCE_LOG.ID,
                RELEVANCE_LOG.QUERY,
                RELEVANCE_LOG.CHUNK_ID,
                RELEVANCE_LOG.COLLECTION,
                RELEVANCE_LOG.ACTION,
                RELEVANCE_LOG.SESSION_ID,
                RELEVANCE_LOG.TIMESTAMP)
                .from(RELEVANCE_LOG)
                .where(cond)
                .orderBy(RELEVANCE_LOG.TIMESTAMP.desc())
                .limit(limit)
                .fetch()
                .map(r -> Map.<String, Object>of(
                    "id",         r.value1(),
                    "query",      r.value2(),
                    "chunk_id",   r.value3(),
                    "collection", str(r.value4()),
                    "action",     r.value5(),
                    "session_id", str(r.value6()),
                    "timestamp",  r.value7() != null ? r.value7().toString() : ""));
        });
    }

    /**
     * Delete relevance_log entries older than {@code days} days.
     * Returns the number of rows deleted.
     */
    public int expireRelevanceLog(String tenant, int days) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusDays(days);
            return ctx.deleteFrom(RELEVANCE_LOG)
                .where(RELEVANCE_LOG.TIMESTAMP.lt(cutoff))
                .execute();
        });
    }

    /**
     * Fidelity-preserving import of a relevance_log row (ETL path).
     * Uses DO NOTHING on conflict — event timestamps are the data.
     *
     * <p>Uses {@link #parseTsStrict} — null/blank/malformed {@code timestampIso}
     * throws {@link IllegalArgumentException} so the ETL layer surfaces corrupt
     * source rows rather than silently stamping migration-time.
     */
    public void importRelevanceRow(String tenant,
                                   String query,
                                   String chunkId,
                                   String collection,
                                   String action,
                                   String sessionId,
                                   String timestampIso) {
        OffsetDateTime ts = parseTsStrict(timestampIso);  // STRICT: throws on blank/malformed
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(RELEVANCE_LOG)
                .set(RELEVANCE_LOG.TENANT_ID, tenant)
                .set(RELEVANCE_LOG.QUERY, query)
                .set(RELEVANCE_LOG.CHUNK_ID, chunkId)
                .set(RELEVANCE_LOG.COLLECTION, str(collection))
                .set(RELEVANCE_LOG.ACTION, action)
                .set(RELEVANCE_LOG.SESSION_ID, str(sessionId))
                .set(RELEVANCE_LOG.TIMESTAMP, ts)
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    // ── search_telemetry ───────────────────────────────────────────────────────

    /**
     * Batch-insert search telemetry rows.
     * Row tuples: (ts, queryHash, collection, rawCount, keptCount, topDistance, threshold).
     * Uses DO NOTHING on conflict (same-second duplicates are discarded as in SQLite).
     */
    public int logSearchBatch(String tenant, List<Object[]> rows) {
        if (rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            int count = 0;
            for (var r : rows) {
                OffsetDateTime ts = parseTs((String) r[0]);
                String queryHash   = (String) r[1];
                String collection  = (String) r[2];
                int rawCount       = ((Number) r[3]).intValue();
                int keptCount      = ((Number) r[4]).intValue();
                Double topDist     = r[5] != null ? ((Number) r[5]).doubleValue() : null;
                Double threshold   = r[6] != null ? ((Number) r[6]).doubleValue() : null;

                count += ctx.insertInto(SEARCH_TELEMETRY)
                    .set(SEARCH_TELEMETRY.TENANT_ID, tenant)
                    .set(SEARCH_TELEMETRY.TS, ts)
                    .set(SEARCH_TELEMETRY.QUERY_HASH, queryHash)
                    .set(SEARCH_TELEMETRY.COLLECTION, collection)
                    .set(SEARCH_TELEMETRY.RAW_COUNT, rawCount)
                    .set(SEARCH_TELEMETRY.KEPT_COUNT, keptCount)
                    .set(SEARCH_TELEMETRY.TOP_DISTANCE, topDist)
                    .set(SEARCH_TELEMETRY.THRESHOLD, threshold)
                    .onConflictDoNothing()
                    .execute();
            }
            return count;
        });
    }

    /**
     * Collection-level retrieval-health stats (mirrors {@code Telemetry.query_collection_stats}).
     * Returns a map with keys: {@code row_count}, {@code zero_hit_rate} (nullable),
     * {@code median_top_distance} (nullable).
     */
    public Map<String, Object> queryCollectionStats(String tenant, String collection, int days) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusDays(days);

            var agg = ctx.select(
                count().as("row_count"),
                sum(when(SEARCH_TELEMETRY.KEPT_COUNT.eq(0), 1).otherwise(0)).as("zero_count"))
                .from(SEARCH_TELEMETRY)
                .where(SEARCH_TELEMETRY.COLLECTION.eq(collection)
                    .and(SEARCH_TELEMETRY.TS.greaterOrEqual(cutoff)))
                .fetchOne();

            long rowCount = agg != null ? ((Number) agg.get("row_count")).longValue() : 0L;
            long zeroCount = agg != null && agg.get("zero_count") != null
                ? ((Number) agg.get("zero_count")).longValue() : 0L;

            // Fetch all top_distances for median calculation
            List<Double> distances = ctx.select(SEARCH_TELEMETRY.TOP_DISTANCE)
                .from(SEARCH_TELEMETRY)
                .where(SEARCH_TELEMETRY.COLLECTION.eq(collection)
                    .and(SEARCH_TELEMETRY.TS.greaterOrEqual(cutoff))
                    .and(SEARCH_TELEMETRY.RAW_COUNT.gt(0))
                    .and(SEARCH_TELEMETRY.TOP_DISTANCE.isNotNull()))
                .orderBy(SEARCH_TELEMETRY.TOP_DISTANCE.asc())
                .fetch(SEARCH_TELEMETRY.TOP_DISTANCE);

            Double zeroHitRate = rowCount > 0 ? (double) zeroCount / rowCount : null;
            Double median = median(distances);

            return Map.of(
                "row_count",           rowCount,
                "zero_hit_rate",       zeroHitRate != null ? zeroHitRate : "null",
                "median_top_distance", median != null ? median : "null");
        });
    }

    private static Double median(List<Double> sorted) {
        int n = sorted.size();
        if (n == 0) return null;
        return n % 2 == 1 ? sorted.get(n / 2)
            : (sorted.get(n / 2 - 1) + sorted.get(n / 2)) / 2.0;
    }

    /**
     * Delete search_telemetry rows older than {@code days} days.
     */
    public int trimSearchTelemetry(String tenant, int days) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusDays(days);
            return ctx.deleteFrom(SEARCH_TELEMETRY)
                .where(SEARCH_TELEMETRY.TS.lt(cutoff))
                .execute();
        });
    }

    /**
     * Rename collection in search_telemetry (and hook_failures).
     * Returns a map of {tableName -> rowCount}.
     */
    public Map<String, Integer> renameCollection(String tenant, String oldName, String newName) {
        int searchCount = tenantScope.withTenant(tenant, ctx -> ctx.update(SEARCH_TELEMETRY)
            .set(SEARCH_TELEMETRY.COLLECTION, newName)
            .where(SEARCH_TELEMETRY.COLLECTION.eq(oldName))
            .execute());

        int hookCount = tenantScope.withTenant(tenant, ctx -> ctx.update(HOOK_FAILURES)
            .set(HOOK_FAILURES.COLLECTION, newName)
            .where(HOOK_FAILURES.COLLECTION.eq(oldName))
            .execute());

        return Map.of("search_telemetry", searchCount, "hook_failures", hookCount);
    }

    /**
     * Fidelity-preserving import for search_telemetry (ETL path).
     * Uses {@link #parseTsStrict} — throws on null/blank/malformed {@code tsIso}.
     */
    public void importSearchRow(String tenant,
                                String tsIso,
                                String queryHash,
                                String collection,
                                int rawCount,
                                int keptCount,
                                Double topDistance,
                                Double threshold) {
        OffsetDateTime ts = parseTsStrict(tsIso);  // STRICT: throws on blank/malformed
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(SEARCH_TELEMETRY)
                .set(SEARCH_TELEMETRY.TENANT_ID, tenant)
                .set(SEARCH_TELEMETRY.TS, ts)
                .set(SEARCH_TELEMETRY.QUERY_HASH, queryHash)
                .set(SEARCH_TELEMETRY.COLLECTION, collection)
                .set(SEARCH_TELEMETRY.RAW_COUNT, rawCount)
                .set(SEARCH_TELEMETRY.KEPT_COUNT, keptCount)
                .set(SEARCH_TELEMETRY.TOP_DISTANCE, topDistance)
                .set(SEARCH_TELEMETRY.THRESHOLD, threshold)
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    // ── tier_writes ────────────────────────────────────────────────────────────

    /**
     * Append one tier-write event (live write path).
     */
    public void recordTierWrite(String tenant,
                                String sessionId,
                                String tsIso,
                                String tool,
                                String tier,
                                String agent,
                                String project,
                                String targetTitle) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(TIER_WRITES)
                .set(TIER_WRITES.TENANT_ID, tenant)
                .set(TIER_WRITES.SESSION_ID, sessionId)
                .set(TIER_WRITES.TS, tsIso != null ? parseTs(tsIso) : OffsetDateTime.now(ZoneOffset.UTC))
                .set(TIER_WRITES.TOOL, tool)
                .set(TIER_WRITES.TIER, tier)
                .set(TIER_WRITES.AGENT, agent)
                .set(TIER_WRITES.PROJECT, project)
                .set(TIER_WRITES.TARGET_TITLE, targetTitle)
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    /**
     * Fidelity-preserving import for tier_writes (ETL path).
     * Uses {@link #parseTsStrict} — throws on null/blank/malformed {@code tsIso}.
     * Does NOT delegate to {@code recordTierWrite} because that method uses the
     * lenient {@link #parseTs} which would silently stamp migration-time on a
     * blank ts, violating the no-silent-fallback-for-correctness rule.
     */
    public void importTierWriteRow(String tenant,
                                   String sessionId,
                                   String tsIso,
                                   String tool,
                                   String tier,
                                   String agent,
                                   String project,
                                   String targetTitle) {
        OffsetDateTime ts = parseTsStrict(tsIso);  // STRICT: throws on blank/malformed
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(TIER_WRITES)
                .set(TIER_WRITES.TENANT_ID, tenant)
                .set(TIER_WRITES.SESSION_ID, str(sessionId))
                .set(TIER_WRITES.TS, ts)
                .set(TIER_WRITES.TOOL, tool)
                .set(TIER_WRITES.TIER, tier)
                .set(TIER_WRITES.AGENT, agent)
                .set(TIER_WRITES.PROJECT, project)
                .set(TIER_WRITES.TARGET_TITLE, targetTitle)
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    // ── nx_answer_runs ─────────────────────────────────────────────────────────

    /**
     * Append one nx_answer run record (live write path).
     */
    public void recordNxAnswerRun(String tenant,
                                  String question,
                                  Long planId,
                                  Double matchedConfidence,
                                  int stepCount,
                                  String finalText,
                                  double costUsd,
                                  long durationMs,
                                  String createdAtIso) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(NX_ANSWER_RUNS)
                .set(NX_ANSWER_RUNS.TENANT_ID, tenant)
                .set(NX_ANSWER_RUNS.QUESTION, question)
                .set(NX_ANSWER_RUNS.PLAN_ID, planId)
                .set(NX_ANSWER_RUNS.MATCHED_CONFIDENCE, matchedConfidence)
                .set(NX_ANSWER_RUNS.STEP_COUNT, stepCount)
                .set(NX_ANSWER_RUNS.FINAL_TEXT, str(finalText))
                .set(NX_ANSWER_RUNS.COST_USD, costUsd)
                .set(NX_ANSWER_RUNS.DURATION_MS, durationMs)
                .set(NX_ANSWER_RUNS.CREATED_AT,
                    createdAtIso != null ? parseTs(createdAtIso) : OffsetDateTime.now(ZoneOffset.UTC))
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    /**
     * Fidelity-preserving import for nx_answer_runs (ETL path).
     * {@code createdAtIso} MUST be the source row's created_at verbatim — never now().
     * Uses {@link #parseTsStrict} — throws on null/blank/malformed {@code createdAtIso}.
     * Does NOT delegate to {@code recordNxAnswerRun} because that method uses the
     * lenient {@link #parseTs} which would silently stamp migration-time on blank input.
     */
    public void importNxAnswerRunRow(String tenant,
                                     String question,
                                     Long planId,
                                     Double matchedConfidence,
                                     int stepCount,
                                     String finalText,
                                     double costUsd,
                                     long durationMs,
                                     String createdAtIso) {
        OffsetDateTime createdAt = parseTsStrict(createdAtIso);  // STRICT: throws on blank/malformed
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(NX_ANSWER_RUNS)
                .set(NX_ANSWER_RUNS.TENANT_ID, tenant)
                .set(NX_ANSWER_RUNS.QUESTION, question)
                .set(NX_ANSWER_RUNS.PLAN_ID, planId)
                .set(NX_ANSWER_RUNS.MATCHED_CONFIDENCE, matchedConfidence)
                .set(NX_ANSWER_RUNS.STEP_COUNT, stepCount)
                .set(NX_ANSWER_RUNS.FINAL_TEXT, str(finalText))
                .set(NX_ANSWER_RUNS.COST_USD, costUsd)
                .set(NX_ANSWER_RUNS.DURATION_MS, durationMs)
                .set(NX_ANSWER_RUNS.CREATED_AT, createdAt)
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    // ── hook_failures ──────────────────────────────────────────────────────────

    /**
     * Append one hook failure (live write path — single-doc chain).
     */
    public void recordHookFailure(String tenant,
                                  String docId,
                                  String collection,
                                  String hookName,
                                  String error,
                                  String occurredAtIso,
                                  String batchDocIds,
                                  boolean isBatch,
                                  String chain) {
        tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime occurredAt = occurredAtIso != null && !occurredAtIso.isBlank()
                ? parseTs(occurredAtIso) : OffsetDateTime.now(ZoneOffset.UTC);
            ctx.insertInto(HOOK_FAILURES)
                .set(HOOK_FAILURES.TENANT_ID, tenant)
                .set(HOOK_FAILURES.DOC_ID, str(docId))
                .set(HOOK_FAILURES.COLLECTION, str(collection))
                .set(HOOK_FAILURES.HOOK_NAME, hookName)
                .set(HOOK_FAILURES.ERROR, str(error))
                .set(HOOK_FAILURES.OCCURRED_AT, occurredAt)
                .set(HOOK_FAILURES.BATCH_DOC_IDS, batchDocIds)
                .set(HOOK_FAILURES.IS_BATCH, isBatch ? 1 : 0)
                .set(HOOK_FAILURES.CHAIN, str(chain).isBlank() ? "single" : str(chain))
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    /**
     * Fidelity-preserving import for hook_failures (ETL path).
     * {@code occurredAtIso} MUST be the source row's occurred_at verbatim — never now().
     * Uses {@link #parseTsStrict} — throws on null/blank/malformed {@code occurredAtIso}.
     * Does NOT delegate to {@code recordHookFailure} because that method uses the
     * lenient {@link #parseTs} which would silently stamp migration-time on blank input.
     */
    public void importHookFailureRow(String tenant,
                                     String docId,
                                     String collection,
                                     String hookName,
                                     String error,
                                     String occurredAtIso,
                                     String batchDocIds,
                                     boolean isBatch,
                                     String chain) {
        OffsetDateTime occurredAt = parseTsStrict(occurredAtIso);  // STRICT: throws on blank/malformed
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(HOOK_FAILURES)
                .set(HOOK_FAILURES.TENANT_ID, tenant)
                .set(HOOK_FAILURES.DOC_ID, str(docId))
                .set(HOOK_FAILURES.COLLECTION, str(collection))
                .set(HOOK_FAILURES.HOOK_NAME, hookName)
                .set(HOOK_FAILURES.ERROR, str(error))
                .set(HOOK_FAILURES.OCCURRED_AT, occurredAt)
                .set(HOOK_FAILURES.BATCH_DOC_IDS, batchDocIds)
                .set(HOOK_FAILURES.IS_BATCH, isBatch ? 1 : 0)
                .set(HOOK_FAILURES.CHAIN, str(chain).isBlank() ? "single" : str(chain))
                .onConflictDoNothing()
                .execute();
            return null;
        });
    }

    // ── frecency ───────────────────────────────────────────────────────────────

    /**
     * Upsert a frecency record (live write + ETL import path).
     *
     * <p>Conflict strategy (LIVE-MUTABLE):
     * <ul>
     *   <li>{@code frecency_score} — GREATEST: preserve highest observed score.</li>
     *   <li>{@code miss_count}     — GREATEST: monotonic counter.</li>
     *   <li>{@code last_hit_at}    — GREATEST: keep latest hit timestamp.</li>
     *   <li>{@code ttl_days}       — GREATEST: take the larger TTL.</li>
     *   <li>{@code embedded_at}    — LEAST: keep the OLDEST embed time (earliest entry wins).</li>
     * </ul>
     */
    public void upsertFrecency(String tenant,
                               String chunkId,
                               String embeddedAtIso,
                               int ttlDays,
                               double frecencyScore,
                               int missCount,
                               String lastHitAtIso) {
        tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime embeddedAt = embeddedAtIso != null && !embeddedAtIso.isBlank()
                ? parseTs(embeddedAtIso) : OffsetDateTime.now(ZoneOffset.UTC);
            OffsetDateTime lastHitAt = lastHitAtIso != null && !lastHitAtIso.isBlank()
                ? parseTs(lastHitAtIso) : OffsetDateTime.now(ZoneOffset.UTC);

            ctx.insertInto(FRECENCY)
                .set(FRECENCY.TENANT_ID, tenant)
                .set(FRECENCY.CHUNK_ID, chunkId)
                .set(FRECENCY.EMBEDDED_AT, embeddedAt)
                .set(FRECENCY.TTL_DAYS, ttlDays)
                .set(FRECENCY.FRECENCY_SCORE, frecencyScore)
                .set(FRECENCY.MISS_COUNT, missCount)
                .set(FRECENCY.LAST_HIT_AT, lastHitAt)
                .onConflict(FRECENCY.TENANT_ID, FRECENCY.CHUNK_ID)
                .doUpdate()
                // GREATEST for monotonic counters and scores
                .set(FRECENCY.FRECENCY_SCORE,
                    greatest(field(name("excluded", "frecency_score"), Double.class), FRECENCY.FRECENCY_SCORE))
                .set(FRECENCY.MISS_COUNT,
                    greatest(field(name("excluded", "miss_count"), Integer.class), FRECENCY.MISS_COUNT))
                .set(FRECENCY.LAST_HIT_AT,
                    greatest(field(name("excluded", "last_hit_at"), OffsetDateTime.class), FRECENCY.LAST_HIT_AT))
                .set(FRECENCY.TTL_DAYS,
                    greatest(field(name("excluded", "ttl_days"), Integer.class), FRECENCY.TTL_DAYS))
                // LEAST: keep the oldest embedded_at (earliest embed wins)
                .set(FRECENCY.EMBEDDED_AT,
                    least(field(name("excluded", "embedded_at"), OffsetDateTime.class), FRECENCY.EMBEDDED_AT))
                .execute();
            return null;
        });
    }

    /**
     * Get frecency record for a single chunk. Returns empty Optional if not found.
     */
    public Optional<Map<String, Object>> getFrecency(String tenant, String chunkId) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rec = ctx.select(
                FRECENCY.CHUNK_ID,
                FRECENCY.EMBEDDED_AT,
                FRECENCY.TTL_DAYS,
                FRECENCY.FRECENCY_SCORE,
                FRECENCY.MISS_COUNT,
                FRECENCY.LAST_HIT_AT)
                .from(FRECENCY)
                .where(FRECENCY.CHUNK_ID.eq(chunkId))
                .fetchOne();
            if (rec == null) return Optional.<Map<String, Object>>empty();
            return Optional.<Map<String, Object>>of(Map.of(
                "chunk_id",       rec.value1(),
                "embedded_at",    rec.value2() != null ? rec.value2().toString() : "",
                "ttl_days",       rec.value3(),
                "frecency_score", rec.value4(),
                "miss_count",     rec.value5(),
                "last_hit_at",    rec.value6() != null ? rec.value6().toString() : ""));
        });
    }
}
