package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.FrecencyRecord;
import dev.nexus.service.jooq.nexus.tables.records.RelevanceLogRecord;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static dev.nexus.service.db.JsonbSupport.jsonbOrNull;
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

    // nexus-cefa1.4: jsonbOrNull moved to the shared JsonbSupport (same
    // package) when AspectRepository needed the identical helper a second
    // time — statically imported above rather than copy-pasted again.

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
            int deleted = ctx.deleteFrom(RELEVANCE_LOG)
                .where(RELEVANCE_LOG.TIMESTAMP.lt(cutoff))
                .execute();
            if (deleted > 0) {
                // nexus-24p05: publish the cumulative-deletes retention marker
                // in the SAME tenant scope — the verify-fill watermark's
                // rollback detector (a fresh schema resets it; ordinary sweep
                // activity advances it monotonically).
                ctx.insertInto(RETENTION_MARKERS,
                        RETENTION_MARKERS.TENANT_ID, RETENTION_MARKERS.RELATION,
                        RETENTION_MARKERS.TOTAL_DELETED, RETENTION_MARKERS.UPDATED_AT)
                    .values(tenant, "nexus.relevance_log",
                        (long) deleted, OffsetDateTime.now(ZoneOffset.UTC))
                    .onConflict(RETENTION_MARKERS.TENANT_ID, RETENTION_MARKERS.RELATION)
                    .doUpdate()
                    .set(RETENTION_MARKERS.TOTAL_DELETED,
                        RETENTION_MARKERS.TOTAL_DELETED.plus((long) deleted))
                    .set(RETENTION_MARKERS.UPDATED_AT, OffsetDateTime.now(ZoneOffset.UTC))
                    .execute();
            }
            return deleted;
        });
    }

    /**
     * Cumulative-deletes retention markers for *relations* (nexus-24p05) —
     * the verify-fill watermark's rollback detector. Relations with no
     * marker row (never swept, or a fresh post-rollback schema) are simply
     * absent from the result; the Python side treats absent as 0.
     */
    public Map<String, Long> getRetentionMarkers(String tenant, List<String> relations) {
        return tenantScope.withTenant(tenant, ctx -> {
            Map<String, Long> out = new java.util.LinkedHashMap<>();
            ctx.select(RETENTION_MARKERS.RELATION, RETENTION_MARKERS.TOTAL_DELETED)
                .from(RETENTION_MARKERS)
                .where(RETENTION_MARKERS.RELATION.in(relations))
                .fetch()
                .forEach(r -> out.put(r.value1(), r.value2()));
            return out;
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
        return trimSearchTelemetry(tenant, days, false);
    }

    /**
     * Delete (or, with {@code dryRun=true}, COUNT without deleting) search_telemetry
     * rows older than {@code days} days.
     *
     * <p>DESIGN NOTE (search_telemetry trim preview gap): the dry-run path reuses the
     * EXACT SAME {@code ts < cutoff} predicate as the delete — a {@code SELECT
     * count(*)} substituted for the {@code DELETE}, never a second, independently
     * maintained WHERE clause or a separate count endpoint. This is deliberate: a
     * census computed by a DIFFERENT predicate than the action it authorises can
     * drift from that action (the nexus-3rr3x class — {@code purge-trash}'s dry-run
     * once reported 340 against a live census of 11,156 because the two were
     * computed by different queries). Sharing the cutoff computation and the
     * {@link org.jooq.Condition} object between both branches here makes that
     * drift impossible by construction, not merely unlikely.
     */
    public int trimSearchTelemetry(String tenant, int days, boolean dryRun) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusDays(days);
            var predicate = SEARCH_TELEMETRY.TS.lt(cutoff);
            if (dryRun) {
                Integer count = ctx.selectCount()
                    .from(SEARCH_TELEMETRY)
                    .where(predicate)
                    .fetchOne(0, Integer.class);
                return count != null ? count : 0;
            }
            return ctx.deleteFrom(SEARCH_TELEMETRY)
                .where(predicate)
                .execute();
        });
    }

    /**
     * Delete hook_failures rows older than {@code days} days (RDR-164 P0 nexus-7365x).
     *
     * <p>Audit-table TTL parity with {@link #trimSearchTelemetry}: hook_failures is a
     * no-cascade audit table reaped by age. Filters on the occurred_at timestamp;
     * RLS-scoped via {@code withTenant}.
     */
    public int trimHookFailures(String tenant, int days) {
        return trimHookFailures(tenant, days, false);
    }

    /**
     * Delete (or, with {@code dryRun=true}, COUNT without deleting) hook_failures
     * rows older than {@code days} days. Same dry-run-reuses-the-delete's-own-
     * predicate discipline as {@link #trimSearchTelemetry(String, int, boolean)} —
     * required so {@code nx doctor --trim-telemetry --dry-run} cannot preview one
     * of the two trimmed tables while silently mutating the other.
     */
    public int trimHookFailures(String tenant, int days, boolean dryRun) {
        return tenantScope.withTenant(tenant, ctx -> {
            OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusDays(days);
            var predicate = HOOK_FAILURES.OCCURRED_AT.lt(cutoff);
            if (dryRun) {
                Integer count = ctx.selectCount()
                    .from(HOOK_FAILURES)
                    .where(predicate)
                    .fetchOne(0, Integer.class);
                return count != null ? count : 0;
            }
            return ctx.deleteFrom(HOOK_FAILURES)
                .where(predicate)
                .execute();
        });
    }

    /**
     * Render a timestamp for the wire as UTC ISO-8601 with explicit seconds.
     *
     * <p>NOT {@code OffsetDateTime.toString()} (nexus-onjvy): that renders in whatever
     * offset the value carries and elides zero seconds, so the same instant crosses as
     * "2026-04-08T17:00-07:00" on one box and "2026-04-09T00:00:00Z" on another.
     * This class already declares {@code UTC_SECOND} for exactly this shape.
     */
    private static String utcIso(OffsetDateTime ts) {
        return ts == null ? "" : UTC_SECOND.format(
            ts.withOffsetSameInstant(ZoneOffset.UTC));
    }

    /**
     * Read hook_failures rows, newest first (nexus-onjvy).
     *
     * <p>WHY THIS EXISTS. Until now hook_failures was WRITE-ONLY over HTTP:
     * {@code /hook_failures/record} and {@code /hook_failures/trim} and no read
     * route at all. The only readers anywhere in the client were raw SQLite
     * SELECTs in {@code nx taxonomy status} and {@code nx doctor}, and those die
     * with the SQLite T2 stores in nexus-i711w. Without this method, the failure
     * log that exists to surface SILENT hook failures becomes permanently
     * uninspectable — an observability hole on exactly the path where silence is
     * the failure mode.
     *
     * <p>RETURNS A PAGE PLUS EXACT AGGREGATES, deliberately. The two consumers
     * want different things: {@code nx taxonomy status} lists recent failures,
     * while {@code nx doctor} reports a total count and the oldest timestamp
     * across the WHOLE filtered set. Serving doctor from a limited page would
     * silently under-report the moment failures exceeded the page size — the
     * caller would see "12 failures" because it asked for 12. So {@code total}
     * and {@code oldest_occurred_at} are computed over the full predicate,
     * independent of {@code limit}.
     *
     * @param tenant    RLS tenant
     * @param days      only rows within the last N days; {@code <= 0} means no
     *                  time bound
     * @param hookNames restrict to these hook names; empty means all
     * @param limit     max rows in the returned page (aggregates ignore it)
     */
    public Map<String, Object> getHookFailures(String tenant,
                                               int days,
                                               List<String> hookNames,
                                               int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var cond = noCondition();
            if (days > 0) {
                OffsetDateTime cutoff = OffsetDateTime.now(ZoneOffset.UTC).minusDays(days);
                cond = cond.and(HOOK_FAILURES.OCCURRED_AT.ge(cutoff));
            }
            if (hookNames != null && !hookNames.isEmpty()) {
                cond = cond.and(HOOK_FAILURES.HOOK_NAME.in(hookNames));
            }

            var agg = ctx.select(count(), min(HOOK_FAILURES.OCCURRED_AT))
                .from(HOOK_FAILURES)
                .where(cond)
                .fetchOne();
            int total = agg != null && agg.value1() != null ? agg.value1() : 0;
            OffsetDateTime oldest = agg != null ? agg.value2() : null;

            List<Map<String, Object>> rows = ctx.select(
                HOOK_FAILURES.ID,
                HOOK_FAILURES.DOC_ID,
                HOOK_FAILURES.COLLECTION,
                HOOK_FAILURES.HOOK_NAME,
                HOOK_FAILURES.ERROR,
                HOOK_FAILURES.OCCURRED_AT,
                HOOK_FAILURES.BATCH_DOC_IDS,
                HOOK_FAILURES.IS_BATCH,
                HOOK_FAILURES.CHAIN)
                .from(HOOK_FAILURES)
                .where(cond)
                .orderBy(HOOK_FAILURES.OCCURRED_AT.desc(), HOOK_FAILURES.ID.desc())
                .limit(limit)
                .fetch()
                .map(r -> {
                    Map<String, Object> m = new java.util.LinkedHashMap<>();
                    m.put("id",            r.value1());
                    m.put("doc_id",        str(r.value2()));
                    m.put("collection",    str(r.value3()));
                    m.put("hook_name",     r.value4());
                    m.put("error",         str(r.value5()));
                    m.put("occurred_at",   utcIso(r.value6()));
                    // nexus-cefa1.3: batch_doc_ids is jsonb now — .data() renders the raw
                    // JSON text on the wire (unchanged shape: a JSON-encoded array string,
                    // or "" when NULL, matching str()'s prior null->"" convention).
                    m.put("batch_doc_ids", r.value7() != null ? r.value7().data() : "");
                    // is_batch is boolean now (catalog-031-style hygiene pass); NOT NULL
                    // DEFAULT false is preserved automatically, but stay defensive to match
                    // CatalogRepository's legacy_grandfathered convention.
                    m.put("is_batch",      r.value8() != null ? r.value8() : Boolean.FALSE);
                    m.put("chain",         str(r.value9()));
                    return m;
                });

            Map<String, Object> out = new java.util.LinkedHashMap<>();
            out.put("rows", rows);
            out.put("total", total);
            out.put("oldest_occurred_at", oldest != null ? utcIso(oldest) : "");
            return out;
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
     * Aggregated tier-write counts for {@code nx tier-status} in service mode
     * (nexus-59wjj — read parity for the local SQLite {@code _query}).
     *
     * Filter precedence mirrors the CLI: {@code lastN} (last N distinct
     * sessions by most-recent write) &gt; {@code sessionId} &gt; {@code sinceIso};
     * empty/zero filters mean "all rows for the tenant". Returns
     * {@code [{tool, tier, agent, project, count}, ...]} grouped by all four,
     * ordered by (tier, tool). {@code agent}/{@code project} are "" when NULL
     * (the Python reader maps "" back to None).
     */
    public List<Map<String, Object>> queryTierWrites(String tenant,
                                                     String sessionId,
                                                     String sinceIso,
                                                     int lastN) {
        return tenantScope.withTenant(tenant, ctx -> {
            var cond = noCondition();
            if (lastN > 0) {
                List<String> sids = ctx.select(TIER_WRITES.SESSION_ID)
                    .from(TIER_WRITES)
                    .groupBy(TIER_WRITES.SESSION_ID)
                    .orderBy(max(TIER_WRITES.TS).desc())
                    .limit(lastN)
                    .fetch(TIER_WRITES.SESSION_ID);
                if (sids.isEmpty()) {
                    return List.of();
                }
                cond = TIER_WRITES.SESSION_ID.in(sids);
            } else if (sessionId != null && !sessionId.isEmpty()) {
                cond = TIER_WRITES.SESSION_ID.eq(sessionId);
            } else if (sinceIso != null && !sinceIso.isEmpty()) {
                cond = TIER_WRITES.TS.ge(parseTs(sinceIso));
            }
            return ctx.select(
                    TIER_WRITES.TOOL,
                    TIER_WRITES.TIER,
                    TIER_WRITES.AGENT,
                    TIER_WRITES.PROJECT,
                    count())
                .from(TIER_WRITES)
                .where(cond)
                .groupBy(TIER_WRITES.TOOL, TIER_WRITES.TIER,
                         TIER_WRITES.AGENT, TIER_WRITES.PROJECT)
                .orderBy(TIER_WRITES.TIER.asc(), TIER_WRITES.TOOL.asc())
                .fetch()
                .map(r -> Map.<String, Object>of(
                    "tool",    str(r.value1()),
                    "tier",    str(r.value2()),
                    "agent",   str(r.value3()),
                    "project", str(r.value4()),
                    "count",   r.value5()));
        });
    }

    /**
     * Per-row tier-write detail (nexus-onjvy gap 4 — {@code target_title} was
     * accepted by {@link #recordTierWrite} and stored, but readable through NO
     * route: {@link #queryTierWrites} is an AGGREGATE grouped by
     * (tool, tier, agent, project) with no target slot, and a per-row title
     * carried on an aggregated group would be incoherent (which of N rows'
     * titles would it be?). This is a SEPARATE, unaggregated read path, not a
     * widened projection on the aggregate.
     *
     * <p>Same filter precedence as {@link #queryTierWrites}: {@code lastN} (last
     * N distinct sessions by most-recent write) &gt; {@code sessionId} &gt;
     * {@code sinceIso}; empty/zero filters mean "no additional predicate" —
     * NOT "all rows unbounded", since {@code limit} still caps the page (review
     * finding: this route was the sole per-row list route in this handler with
     * no limit/pagination, unlike {@link #getHookFailures} et al.). Same
     * capped-page-plus-exact-total envelope discipline as
     * {@link #getHookFailures}: {@code total} is computed over the FULL
     * {@code cond}-filtered set, independent of {@code limit}, so a caller
     * asking for the last 20 rows never sees a total of 20.
     *
     * <p>Ordered most-recent-first ({@code ts desc, id desc}). Returns
     * {@code {rows: [{session_id, ts, tool, tier, agent, project,
     * target_title}, ...], total: N}} — unlike the aggregate, NULL
     * {@code agent}/{@code project}/{@code target_title} cross the wire as
     * JSON {@code null}, not {@code ""}: there is no ambiguity to paper over
     * on a per-row read the way there is when grouping collapses many NULLs
     * into one bucket.
     */
    public Map<String, Object> listTierWrites(String tenant,
                                               String sessionId,
                                               String sinceIso,
                                               int lastN,
                                               int limit) {
        return tenantScope.withTenant(tenant, ctx -> {
            var cond = noCondition();
            if (lastN > 0) {
                List<String> sids = ctx.select(TIER_WRITES.SESSION_ID)
                    .from(TIER_WRITES)
                    .groupBy(TIER_WRITES.SESSION_ID)
                    .orderBy(max(TIER_WRITES.TS).desc())
                    .limit(lastN)
                    .fetch(TIER_WRITES.SESSION_ID);
                if (sids.isEmpty()) {
                    return Map.of("rows", List.of(), "total", 0);
                }
                cond = TIER_WRITES.SESSION_ID.in(sids);
            } else if (sessionId != null && !sessionId.isEmpty()) {
                cond = TIER_WRITES.SESSION_ID.eq(sessionId);
            } else if (sinceIso != null && !sinceIso.isEmpty()) {
                cond = TIER_WRITES.TS.ge(parseTs(sinceIso));
            }

            int total = ctx.fetchCount(TIER_WRITES, cond);

            List<Map<String, Object>> rows = ctx.select(
                    TIER_WRITES.ID,
                    TIER_WRITES.SESSION_ID,
                    TIER_WRITES.TS,
                    TIER_WRITES.TOOL,
                    TIER_WRITES.TIER,
                    TIER_WRITES.AGENT,
                    TIER_WRITES.PROJECT,
                    TIER_WRITES.TARGET_TITLE)
                .from(TIER_WRITES)
                .where(cond)
                .orderBy(TIER_WRITES.TS.desc(), TIER_WRITES.ID.desc())
                .limit(limit)
                .fetch()
                .map(r -> {
                    Map<String, Object> m = new java.util.LinkedHashMap<>();
                    m.put("session_id",   r.value2());
                    m.put("ts",           utcIso(r.value3()));
                    m.put("tool",         r.value4());
                    m.put("tier",         r.value5());
                    m.put("agent",        r.value6());
                    m.put("project",      r.value7());
                    m.put("target_title", r.value8());
                    return m;
                });

            Map<String, Object> out = new java.util.LinkedHashMap<>();
            out.put("rows", rows);
            out.put("total", total);
            return out;
        });
    }

    /**
     * Record a consent event (RDR-182 P1.2 / nexus-ng2sy — service-mode
     * parity for {@code Telemetry.record_consent}). Append-only: a grant AND
     * a revoke are each their own row ({@code granted} distinguishes them).
     * {@code tsIso} is caller-supplied (the consent gesture's timestamp).
     */
    public void recordConsent(String tenant,
                              String scope,
                              String tsIso,
                              boolean granted) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.insertInto(CLAUDE_ASSISTED_REMEDIATION_CONSENTS)
                .set(CLAUDE_ASSISTED_REMEDIATION_CONSENTS.TENANT_ID, tenant)
                .set(CLAUDE_ASSISTED_REMEDIATION_CONSENTS.SCOPE, scope)
                .set(CLAUDE_ASSISTED_REMEDIATION_CONSENTS.TS,
                     tsIso != null ? parseTs(tsIso) : OffsetDateTime.now(ZoneOffset.UTC))
                .set(CLAUDE_ASSISTED_REMEDIATION_CONSENTS.GRANTED, granted)
                .execute();
            return null;
        });
    }

    /**
     * Read the consent-audit trail for the tenant, in insertion order
     * (grants and revokes; the {@code nx remediate --history} read surface).
     * Returns {@code [{scope, ts, granted}, ...]}.
     */
    public List<Map<String, Object>> listConsents(String tenant) {
        return tenantScope.withTenant(tenant, ctx ->
            ctx.select(
                CLAUDE_ASSISTED_REMEDIATION_CONSENTS.SCOPE,
                CLAUDE_ASSISTED_REMEDIATION_CONSENTS.TS,
                CLAUDE_ASSISTED_REMEDIATION_CONSENTS.GRANTED)
                .from(CLAUDE_ASSISTED_REMEDIATION_CONSENTS)
                .orderBy(CLAUDE_ASSISTED_REMEDIATION_CONSENTS.ID.asc())
                .fetch()
                .map(r -> Map.<String, Object>of(
                    "scope",   str(r.value1()),
                    "ts",      r.value2() != null ? r.value2().toString() : "",
                    "granted", r.value3() != null && r.value3())));
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

    // ── nx_answer_runs / nx_answer_steps ─────────────────────────────────────────

    /**
     * One parsed {@code nx_answer_steps} child row, as handed to
     * {@link #recordNxAnswerRun(String, String, Long, Double, int, String, Double, long, String, List)}
     * by {@code TelemetryHandler.parseSteps} (RDR-196 .p1c, nexus-nyry9.9).
     * Field-for-field mirror of the Python-side {@code StepRecord}
     * (src/nexus/plans/runner.py:242) — see telemetry-007-nx-answer-steps.xml's
     * header for the column-by-column rationale.
     */
    public record StepInput(int stepIndex,
                             String operator,
                             String source,
                             String model,
                             Integer inputTokens,
                             Integer outputTokens,
                             Double costUsd,
                             int elapsedMs,
                             boolean ok,
                             List<Integer> bundledSteps) {}

    /**
     * Append one nx_answer run record (live write path), no step children.
     * Delegates to the steps-carrying overload with an empty list — every
     * existing caller (ETL-adjacent live-write sites, every pre-nexus-nyry9.9
     * test) keeps its exact prior behavior unchanged.
     *
     * <p>{@code costUsd} is {@code Double} (boxed), not {@code double}
     * (RDR-196 .p1c-b, nexus-lme1s): {@code nx_answer_runs.cost_usd} lost
     * its {@code NOT NULL DEFAULT 0.0} at telemetry-007-3 specifically so a
     * caller's "no usage observed" can be written as SQL {@code NULL}
     * instead of being forced to a stored {@code 0.0} indistinguishable
     * from a genuine free call (RDR-196 risk 1). A primitive parameter
     * cannot represent that distinction at all, so this is a signature
     * change, not just a schema change.
     */
    public void recordNxAnswerRun(String tenant,
                                  String question,
                                  Long planId,
                                  Double matchedConfidence,
                                  int stepCount,
                                  String finalText,
                                  Double costUsd,
                                  long durationMs,
                                  String createdAtIso) {
        recordNxAnswerRun(tenant, question, planId, matchedConfidence, stepCount, finalText,
            costUsd, durationMs, createdAtIso, List.of());
    }

    /**
     * Append one nx_answer run record plus its (optional) per-step children,
     * in ONE transaction (RDR-196 .p1c, nexus-nyry9.9 — the {@code steps}
     * wire field on {@code POST /v1/telemetry/nx_answer_runs/record} is
     * OPTIONAL, the {@code .p1d} degradation contract: an empty/absent
     * {@code steps} list writes the parent exactly as before and nothing
     * else).
     *
     * <p>Atomicity: both the parent insert and every child insert run inside
     * the SAME {@link TenantScope#withTenant} lambda, which is one PG
     * transaction (autoCommit=false, commit on lambda return, rollback on any
     * thrown exception — see {@code TenantScope.stampAndRun}). A child-row
     * failure (e.g. the {@code source} CHECK, a NOT NULL violation) throws a
     * jOOQ {@code DataAccessException} (a {@code RuntimeException}), which
     * propagates out of this lambda and rolls back the parent insert too —
     * partial telemetry is worse than none (this bead's own DO instruction).
     *
     * <p>Dedup interaction: the parent insert keeps its
     * {@code onConflictDoNothing()} (the existing {@code nx_answer_runs} ETL
     * dedup key, unchanged). {@code RETURNING id} on that insert yields
     * {@code null} when the conflict-skip fires — in that case children are
     * NOT written (there is no new row to attach them to, and this call
     * cannot safely infer which pre-existing run row is the "same" one
     * without a broader dedup redesign that is out of this bead's scope).
     * This is the same rare-collision edge case the parent's own dedup
     * already accepted; it is not new in kind, only extended to also skip
     * children on that already-existing skip path.
     *
     * <p>Does NOT delegate from {@code importNxAnswerRunRow} (the ETL path)
     * — that method has no steps concept in this bead's scope and stays
     * exactly as it was, per this bead's own DO instruction to leave that
     * split intact.
     */
    public void recordNxAnswerRun(String tenant,
                                  String question,
                                  Long planId,
                                  Double matchedConfidence,
                                  int stepCount,
                                  String finalText,
                                  Double costUsd,
                                  long durationMs,
                                  String createdAtIso,
                                  List<StepInput> steps) {
        tenantScope.withTenant(tenant, ctx -> {
            Long runId = ctx.insertInto(NX_ANSWER_RUNS)
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
                .returning(NX_ANSWER_RUNS.ID)
                .fetchOne(NX_ANSWER_RUNS.ID);

            if (runId != null && steps != null) {
                for (StepInput s : steps) {
                    Integer[] bundled = s.bundledSteps() == null
                        ? new Integer[0]
                        : s.bundledSteps().toArray(new Integer[0]);
                    ctx.insertInto(NX_ANSWER_STEPS)
                        .set(NX_ANSWER_STEPS.RUN_ID, runId)
                        .set(NX_ANSWER_STEPS.TENANT_ID, tenant)
                        .set(NX_ANSWER_STEPS.STEP_INDEX, s.stepIndex())
                        .set(NX_ANSWER_STEPS.OPERATOR, s.operator())
                        .set(NX_ANSWER_STEPS.SOURCE, s.source())
                        .set(NX_ANSWER_STEPS.MODEL, s.model())
                        .set(NX_ANSWER_STEPS.INPUT_TOKENS, s.inputTokens())
                        .set(NX_ANSWER_STEPS.OUTPUT_TOKENS, s.outputTokens())
                        .set(NX_ANSWER_STEPS.COST_USD,
                            s.costUsd() != null ? BigDecimal.valueOf(s.costUsd()) : null)
                        .set(NX_ANSWER_STEPS.ELAPSED_MS, s.elapsedMs())
                        .set(NX_ANSWER_STEPS.OK, s.ok())
                        .set(NX_ANSWER_STEPS.BUNDLED_STEPS, bundled)
                        .execute();
                }
            }
            return null;
        });
    }

    /**
     * Fidelity-preserving import for nx_answer_runs (ETL path).
     * {@code createdAtIso} MUST be the source row's created_at verbatim — never now().
     * Uses {@link #parseTsStrict} — throws on null/blank/malformed {@code createdAtIso}.
     * Does NOT delegate to {@code recordNxAnswerRun} because that method uses the
     * lenient {@link #parseTs} which would silently stamp migration-time on blank input.
     *
     * <p>{@code costUsd} is {@code Double} (boxed), not {@code double}
     * (RDR-196 .p1c-b, nexus-lme1s, same reasoning as {@link #recordNxAnswerRun}):
     * fidelity preservation means a source row's null cost_usd must import
     * as SQL {@code NULL}, not get coerced to {@code 0.0} en route.
     */
    public void importNxAnswerRunRow(String tenant,
                                     String question,
                                     Long planId,
                                     Double matchedConfidence,
                                     int stepCount,
                                     String finalText,
                                     Double costUsd,
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

    /** Fixed latency bucket edges (ms), matching the RDR-080 production
     *  distribution cited in {@code nx_answer}'s own docstring and pinned by
     *  the shakedown playbook §4.5 ("SAME QUERIES, SAME BUCKETS, EVERY TIME"):
     *  under 5s, 5s-30s, 30s-2min, 2min-5min, over 5min (residual — not in the
     *  original 100-run distribution, but required so bucket counts always
     *  sum to {@code total} rather than silently dropping outlier runs). */
    private static final long BUCKET_5S   = 5_000L;
    private static final long BUCKET_30S  = 30_000L;
    private static final long BUCKET_2MIN = 120_000L;
    private static final long BUCKET_5MIN = 300_000L;

    /**
     * Read {@code nx_answer_runs} rows plus exact aggregates over the whole
     * filtered set (nexus-eho3u — the read half of a write-only instrument).
     *
     * <p>Same non-vacuity shape as {@link #getHookFailures}: {@code total},
     * {@code oldest_created_at}, {@code hit_count}/{@code fallback_count},
     * {@code avg_duration_ms}/{@code avg_cost_usd}, and the latency-bucket
     * histogram are ALL computed over the full {@code since}-filtered set,
     * independent of {@code limit} — a caller asking for the last 5 rows
     * must not see a total of 5.
     *
     * <p>{@code hit_count} counts rows with a REAL matched plan: {@code
     * plan_id} non-null AND {@code != 0}. {@code plan_id = 0} is NOT a
     * matched plan — it is the synthetic ad-hoc {@code Match} sentinel
     * {@code _nx_answer_plan_miss} (core.py) returns on every SUCCESSFUL
     * inline-planner run (see {@code Match(plan_id=0, name="ad-hoc", ...)}
     * at that function's tail); {@code plans.id} is {@code BIGSERIAL}, so
     * {@code 0} can never collide with a real plan row and is exclusively
     * this sentinel (nexus-eho3u review fix — the original {@code plan_id
     * IS NOT NULL} predicate counted every successful ad-hoc run as a
     * "hit", inverting the plan-match-rate figure the shakedown playbook's
     * §4.5 baseline reports). {@code fallback_count} is the complement:
     * {@code plan_id IS NULL OR plan_id = 0} — a genuine plan-match miss
     * (null, e.g. a planner error before any Match existed) and a
     * successful ad-hoc run (0) are both "not a matched plan" for this
     * metric. There is no {@code session_id} or verb column on this table
     * (checked against the live schema, RDR-152 telemetry-001 baseline) —
     * a session filter or per-verb breakdown would be a speculative field
     * this table cannot back, so neither is offered here.
     *
     * @param tenant   RLS tenant
     * @param sinceIso only rows with {@code created_at >= sinceIso}; blank/null
     *                 means no time bound
     * @param limit    max rows in the returned page; does not affect the
     *                 aggregates
     */
    public Map<String, Object> queryNxAnswerRuns(String tenant, String sinceIso, int limit) {
        return queryNxAnswerRuns(tenant, sinceIso, limit, false);
    }

    /**
     * {@code includeSteps} overload (RDR-196 .p1c-b, nexus-lme1s): when
     * {@code true}, each row in the returned page gains a {@code steps}
     * entry — the page's {@code nx_answer_steps} children, ordered by
     * {@code step_index}, fetched in ONE query over all page run ids
     * (grouped in Java, not per-row — no N+1) and RLS-scoped through the
     * SAME {@code tenantScope.withTenant} connection as the parent query,
     * so a tenant can never see another tenant's steps. {@code false}
     * (the 3-arg overload above) is byte-for-byte the pre-existing
     * behavior — no {@code steps} key at all, not even an empty one.
     */
    public Map<String, Object> queryNxAnswerRuns(String tenant, String sinceIso, int limit,
                                                  boolean includeSteps) {
        return tenantScope.withTenant(tenant, ctx -> {
            var cond = noCondition();
            if (sinceIso != null && !sinceIso.isBlank()) {
                cond = cond.and(NX_ANSWER_RUNS.CREATED_AT.ge(parseTs(sinceIso)));
            }

            var agg = ctx.select(
                    count().as("total"),
                    min(NX_ANSWER_RUNS.CREATED_AT).as("oldest"),
                    avg(NX_ANSWER_RUNS.DURATION_MS).as("avg_duration_ms"),
                    avg(NX_ANSWER_RUNS.COST_USD).as("avg_cost_usd"),
                    // plan_id = 0 is the ad-hoc-Match sentinel (core.py
                    // _nx_answer_plan_miss), never a real matched plan —
                    // see the method docstring.
                    sum(when(NX_ANSWER_RUNS.PLAN_ID.isNotNull()
                            .and(NX_ANSWER_RUNS.PLAN_ID.ne(0L)), 1).otherwise(0)).as("hit_count"),
                    sum(when(NX_ANSWER_RUNS.PLAN_ID.isNull()
                            .or(NX_ANSWER_RUNS.PLAN_ID.eq(0L)), 1).otherwise(0)).as("fallback_count"),
                    sum(when(NX_ANSWER_RUNS.DURATION_MS.lt(BUCKET_5S), 1).otherwise(0)).as("b_under_5s"),
                    sum(when(NX_ANSWER_RUNS.DURATION_MS.ge(BUCKET_5S)
                            .and(NX_ANSWER_RUNS.DURATION_MS.lt(BUCKET_30S)), 1).otherwise(0)).as("b_5s_30s"),
                    sum(when(NX_ANSWER_RUNS.DURATION_MS.ge(BUCKET_30S)
                            .and(NX_ANSWER_RUNS.DURATION_MS.lt(BUCKET_2MIN)), 1).otherwise(0)).as("b_30s_2min"),
                    sum(when(NX_ANSWER_RUNS.DURATION_MS.ge(BUCKET_2MIN)
                            .and(NX_ANSWER_RUNS.DURATION_MS.le(BUCKET_5MIN)), 1).otherwise(0)).as("b_2min_5min"),
                    sum(when(NX_ANSWER_RUNS.DURATION_MS.gt(BUCKET_5MIN), 1).otherwise(0)).as("b_over_5min"))
                .from(NX_ANSWER_RUNS)
                .where(cond)
                .fetchOne();

            int total = agg != null && agg.get("total") != null ? ((Number) agg.get("total")).intValue() : 0;
            OffsetDateTime oldest = agg != null ? (OffsetDateTime) agg.get("oldest") : null;
            Double avgDurationMs = agg != null && agg.get("avg_duration_ms") != null
                ? ((Number) agg.get("avg_duration_ms")).doubleValue() : null;
            Double avgCostUsd = agg != null && agg.get("avg_cost_usd") != null
                ? ((Number) agg.get("avg_cost_usd")).doubleValue() : null;
            long hitCount = agg != null && agg.get("hit_count") != null
                ? ((Number) agg.get("hit_count")).longValue() : 0L;
            long fallbackCount = agg != null && agg.get("fallback_count") != null
                ? ((Number) agg.get("fallback_count")).longValue() : 0L;

            Map<String, Object> buckets = new java.util.LinkedHashMap<>();
            buckets.put("under_5s",    bucketVal(agg, "b_under_5s"));
            buckets.put("5s_to_30s",   bucketVal(agg, "b_5s_30s"));
            buckets.put("30s_to_2min", bucketVal(agg, "b_30s_2min"));
            buckets.put("2min_to_5min", bucketVal(agg, "b_2min_5min"));
            buckets.put("over_5min",   bucketVal(agg, "b_over_5min"));

            var runRecords = ctx.select(
                    NX_ANSWER_RUNS.ID,
                    NX_ANSWER_RUNS.QUESTION,
                    NX_ANSWER_RUNS.PLAN_ID,
                    NX_ANSWER_RUNS.MATCHED_CONFIDENCE,
                    NX_ANSWER_RUNS.STEP_COUNT,
                    NX_ANSWER_RUNS.FINAL_TEXT,
                    NX_ANSWER_RUNS.COST_USD,
                    NX_ANSWER_RUNS.DURATION_MS,
                    NX_ANSWER_RUNS.CREATED_AT)
                .from(NX_ANSWER_RUNS)
                .where(cond)
                .orderBy(NX_ANSWER_RUNS.CREATED_AT.desc(), NX_ANSWER_RUNS.ID.desc())
                .limit(limit)
                .fetch();

            // ONE query over the page's run ids, grouped in Java — no N+1
            // (RDR-196 .p1c-b, nexus-lme1s).
            Map<Long, List<Map<String, Object>>> stepsByRunId = includeSteps
                ? fetchNxAnswerStepsGrouped(ctx, runRecords.getValues(NX_ANSWER_RUNS.ID))
                : Map.of();

            List<Map<String, Object>> rows = runRecords.map(r -> {
                    Map<String, Object> m = new java.util.LinkedHashMap<>();
                    m.put("id",                 r.value1());
                    m.put("question",           r.value2());
                    m.put("plan_id",            r.value3());
                    m.put("matched_confidence", r.value4());
                    m.put("step_count",         r.value5());
                    m.put("final_text",         r.value6());
                    m.put("cost_usd",           r.value7());
                    m.put("duration_ms",        r.value8());
                    m.put("created_at",         utcIso(r.value9()));
                    if (includeSteps) {
                        m.put("steps", stepsByRunId.getOrDefault(r.value1(), List.of()));
                    }
                    return m;
                });

            Map<String, Object> out = new java.util.LinkedHashMap<>();
            out.put("rows", rows);
            out.put("total", total);
            out.put("oldest_created_at", oldest != null ? utcIso(oldest) : "");
            out.put("hit_count", hitCount);
            out.put("fallback_count", fallbackCount);
            out.put("avg_duration_ms", avgDurationMs);
            out.put("avg_cost_usd", avgCostUsd);
            out.put("latency_buckets", buckets);
            return out;
        });
    }

    private static long bucketVal(org.jooq.Record agg, String key) {
        Object v = agg != null ? agg.get(key) : null;
        return v != null ? ((Number) v).longValue() : 0L;
    }

    /**
     * Fetch every {@code nx_answer_steps} row for the given {@code run_id}s in
     * ONE query, grouped by {@code run_id} and ordered by {@code step_index}
     * within each group (RDR-196 .p1c-b, nexus-lme1s — the read half of
     * {@code nx_answer_steps}, written at .p1c but never read back). Field
     * names in each returned map are the SAME ones {@code
     * TelemetryHandler.parseNxAnswerSteps} accepts on the write side —
     * {@code step_index, operator, source, model, input_tokens,
     * output_tokens, cost_usd, elapsed_ms, ok, bundled_steps} — deliberately
     * omitting {@code run_id}/{@code tenant_id}, both already implied by
     * which run's {@code steps} array a caller finds this map inside.
     * {@code cost_usd} is the raw (possibly-null) {@code BigDecimal} from
     * the NUMERIC column; {@code bundled_steps} the raw {@code Integer[]} —
     * both serialize correctly via Jackson without a manual conversion.
     *
     * <p>Empty/null {@code runIds} short-circuits to an empty map rather
     * than issuing a query with an empty {@code IN ()} — jOOQ's own
     * {@code Field.in(Collection)} degrades to a false condition for that
     * case, but skipping the round trip entirely is both cheaper and more
     * explicit about the empty-page case.
     */
    private static Map<Long, List<Map<String, Object>>> fetchNxAnswerStepsGrouped(
            DSLContext ctx, List<Long> runIds) {
        if (runIds == null || runIds.isEmpty()) {
            return Map.of();
        }
        Map<Long, List<Map<String, Object>>> out = new java.util.LinkedHashMap<>();
        ctx.select(
                NX_ANSWER_STEPS.RUN_ID,
                NX_ANSWER_STEPS.STEP_INDEX,
                NX_ANSWER_STEPS.OPERATOR,
                NX_ANSWER_STEPS.SOURCE,
                NX_ANSWER_STEPS.MODEL,
                NX_ANSWER_STEPS.INPUT_TOKENS,
                NX_ANSWER_STEPS.OUTPUT_TOKENS,
                NX_ANSWER_STEPS.COST_USD,
                NX_ANSWER_STEPS.ELAPSED_MS,
                NX_ANSWER_STEPS.OK,
                NX_ANSWER_STEPS.BUNDLED_STEPS)
            .from(NX_ANSWER_STEPS)
            .where(NX_ANSWER_STEPS.RUN_ID.in(runIds))
            .orderBy(NX_ANSWER_STEPS.RUN_ID.asc(), NX_ANSWER_STEPS.STEP_INDEX.asc())
            .fetch()
            .forEach(r -> {
                Map<String, Object> step = new java.util.LinkedHashMap<>();
                step.put("step_index",    r.get(NX_ANSWER_STEPS.STEP_INDEX));
                step.put("operator",      r.get(NX_ANSWER_STEPS.OPERATOR));
                step.put("source",        r.get(NX_ANSWER_STEPS.SOURCE));
                step.put("model",         r.get(NX_ANSWER_STEPS.MODEL));
                step.put("input_tokens",  r.get(NX_ANSWER_STEPS.INPUT_TOKENS));
                step.put("output_tokens", r.get(NX_ANSWER_STEPS.OUTPUT_TOKENS));
                step.put("cost_usd",      r.get(NX_ANSWER_STEPS.COST_USD));
                step.put("elapsed_ms",    r.get(NX_ANSWER_STEPS.ELAPSED_MS));
                step.put("ok",            r.get(NX_ANSWER_STEPS.OK));
                step.put("bundled_steps", r.get(NX_ANSWER_STEPS.BUNDLED_STEPS));
                out.computeIfAbsent(r.get(NX_ANSWER_STEPS.RUN_ID), k -> new java.util.ArrayList<>())
                   .add(step);
            });
        return out;
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
                .set(HOOK_FAILURES.BATCH_DOC_IDS, jsonbOrNull(batchDocIds))
                .set(HOOK_FAILURES.IS_BATCH, isBatch)
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
                .set(HOOK_FAILURES.BATCH_DOC_IDS, jsonbOrNull(batchDocIds))
                .set(HOOK_FAILURES.IS_BATCH, isBatch)
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
     *   <li>{@code ttl_days}       — GREATEST: take the larger TTL. {@code null}
     *       (RDR-194 D5: the permanent sentinel, {@code 0} retired —
     *       {@code telemetry-006-frecency-ttl-null.xml}) is ignored by
     *       Postgres's {@code GREATEST} unless BOTH sides are {@code null},
     *       so a permanent row merged against any concrete TTL keeps the
     *       concrete value — identical merge behavior to the pre-migration
     *       {@code GREATEST(0, x)}, since {@code 0} always lost to any
     *       positive competitor there too (verified, not assumed: this is
     *       why the sentinel flip needed no change to the merge SQL
     *       itself, only to the parameter's nullability).</li>
     *   <li>{@code embedded_at}    — LEAST: keep the OLDEST embed time (earliest entry wins).</li>
     * </ul>
     *
     * @param ttlDays {@code null} for permanent (RDR-194 D5); a caller-
     *     supplied {@code 0} is rejected before reaching this method by
     *     {@code TelemetryHandler}'s boundary validation on the live
     *     single-row path, or by {@code frecency_ttl_days_positive_chk} at
     *     INSERT time on the ETL import paths (which pass values verbatim).
     */
    public void upsertFrecency(String tenant,
                               String chunkId,
                               String embeddedAtIso,
                               Integer ttlDays,
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

    /** PG Int16 bind-count limit is 32767; keep a safety margin (nexus-1usso). */
    private static final int MAX_BATCH_PARAMS = 30_000;

    /**
     * nexus-1usso: fidelity-preserving BULK import for any of the six
     * telemetry tables — ONE multi-row {@code INSERT} statement per chunk
     * (chunked at {@link #MAX_BATCH_PARAMS} bind params), mirroring {@code
     * ChashRepository.doImportBatch} (f0ab406f). Previously this looped a
     * per-row {@code .set(...).execute()} inside one transaction (GUC set
     * once, but still N round-trips) — the plan-audit finding on nexus-1usso
     * ("has the endpoint" != "batches at the DB") applies here too.
     *
     * <p>{@code DO NOTHING} for the five event logs (no dedup needed — intra-
     * statement conflicts against {@code DO NOTHING} are a documented no-op,
     * unlike {@code DO UPDATE} which cannot affect the same row twice).
     * frecency's {@code GREATEST}/{@code LEAST} {@code DO UPDATE} dedupes on
     * {@code chunk_id} within a chunk, last occurrence wins. Rows come from
     * the trusted ETL; the strict timestamp parse ({@link #parseTsStrict}) is
     * applied per row so a blank ts fails the batch rather than silently
     * stamping {@code now()}. The per-row methods remain for the live/
     * single-write path. Empty batch is a no-op.
     *
     * @return number of rows submitted.
     */
    public int importBatch(String tenant, String table, List<Map<String, Object>> rows) {
        if (rows == null || rows.isEmpty()) return 0;
        return tenantScope.withTenant(tenant, ctx -> {
            switch (table) {
                case "relevance_log"    -> importRelevanceLogBatch(ctx, tenant, rows);
                case "search_telemetry" -> importSearchTelemetryBatch(ctx, tenant, rows);
                case "tier_writes"      -> importTierWritesBatch(ctx, tenant, rows);
                case "nx_answer_runs"   -> importNxAnswerRunsBatch(ctx, tenant, rows);
                case "hook_failures"    -> importHookFailuresBatch(ctx, tenant, rows);
                case "frecency"         -> importFrecencyBatch(ctx, tenant, rows);
                default -> throw new IllegalArgumentException("Unknown table: " + table);
            }
            log.debug("event=telemetry_import_batch tenant={} table={} rows={}", tenant, table, rows.size());
            return rows.size();
        });
    }

    private static void importRelevanceLogBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 7);
        for (int start = 0; start < rows.size(); start += chunkSize) {
            var batch = rows.subList(start, Math.min(start + chunkSize, rows.size()));
            var insert = ctx.insertInto(RELEVANCE_LOG,
                    RELEVANCE_LOG.TENANT_ID, RELEVANCE_LOG.QUERY, RELEVANCE_LOG.CHUNK_ID,
                    RELEVANCE_LOG.COLLECTION, RELEVANCE_LOG.ACTION, RELEVANCE_LOG.SESSION_ID,
                    RELEVANCE_LOG.TIMESTAMP);
            for (var r : batch) {
                insert = insert.values(tenant, reqS(r, "query"), reqS(r, "chunk_id"),
                        str(optS(r, "collection")), reqS(r, "action"), str(optS(r, "session_id")),
                        parseTsStrict(reqS(r, "timestamp")));
            }
            insert.onConflictDoNothing().execute();
        }
    }

    private static void importSearchTelemetryBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 8);
        for (int start = 0; start < rows.size(); start += chunkSize) {
            var batch = rows.subList(start, Math.min(start + chunkSize, rows.size()));
            var insert = ctx.insertInto(SEARCH_TELEMETRY,
                    SEARCH_TELEMETRY.TENANT_ID, SEARCH_TELEMETRY.TS, SEARCH_TELEMETRY.QUERY_HASH,
                    SEARCH_TELEMETRY.COLLECTION, SEARCH_TELEMETRY.RAW_COUNT, SEARCH_TELEMETRY.KEPT_COUNT,
                    SEARCH_TELEMETRY.TOP_DISTANCE, SEARCH_TELEMETRY.THRESHOLD);
            for (var r : batch) {
                insert = insert.values(tenant, parseTsStrict(reqS(r, "ts")), reqS(r, "query_hash"),
                        reqS(r, "collection"), reqI(r, "raw_count"), reqI(r, "kept_count"),
                        optD(r, "top_distance"), optD(r, "threshold"));
            }
            insert.onConflictDoNothing().execute();
        }
    }

    private static void importTierWritesBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 8);
        for (int start = 0; start < rows.size(); start += chunkSize) {
            var batch = rows.subList(start, Math.min(start + chunkSize, rows.size()));
            var insert = ctx.insertInto(TIER_WRITES,
                    TIER_WRITES.TENANT_ID, TIER_WRITES.SESSION_ID, TIER_WRITES.TS, TIER_WRITES.TOOL,
                    TIER_WRITES.TIER, TIER_WRITES.AGENT, TIER_WRITES.PROJECT, TIER_WRITES.TARGET_TITLE);
            for (var r : batch) {
                insert = insert.values(tenant, str(optS(r, "session_id")), parseTsStrict(reqS(r, "ts")),
                        reqS(r, "tool"), reqS(r, "tier"), optS(r, "agent"), optS(r, "project"),
                        optS(r, "target_title"));
            }
            insert.onConflictDoNothing().execute();
        }
    }

    private static void importNxAnswerRunsBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 9);
        for (int start = 0; start < rows.size(); start += chunkSize) {
            var batch = rows.subList(start, Math.min(start + chunkSize, rows.size()));
            var insert = ctx.insertInto(NX_ANSWER_RUNS,
                    NX_ANSWER_RUNS.TENANT_ID, NX_ANSWER_RUNS.QUESTION, NX_ANSWER_RUNS.PLAN_ID,
                    NX_ANSWER_RUNS.MATCHED_CONFIDENCE, NX_ANSWER_RUNS.STEP_COUNT, NX_ANSWER_RUNS.FINAL_TEXT,
                    NX_ANSWER_RUNS.COST_USD, NX_ANSWER_RUNS.DURATION_MS, NX_ANSWER_RUNS.CREATED_AT);
            for (var r : batch) {
                // cost_usd: optD (nullable, no default) — not optDd (RDR-196 .p1c-b,
                // nexus-lme1s). Fidelity-preserving import must not coerce a source
                // row's absent/null cost_usd to a stored 0.0 (indistinguishable from
                // a genuine known-zero, same reasoning as importNxAnswerRunRow above).
                insert = insert.values(tenant, reqS(r, "question"), optL(r, "plan_id"),
                        optD(r, "matched_confidence"), optI(r, "step_count", 0), str(optS(r, "final_text")),
                        optD(r, "cost_usd"), optLd(r, "duration_ms", 0L),
                        parseTsStrict(reqS(r, "created_at")));
            }
            insert.onConflictDoNothing().execute();
        }
    }

    private static void importHookFailuresBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 9);
        for (int start = 0; start < rows.size(); start += chunkSize) {
            var batch = rows.subList(start, Math.min(start + chunkSize, rows.size()));
            var insert = ctx.insertInto(HOOK_FAILURES,
                    HOOK_FAILURES.TENANT_ID, HOOK_FAILURES.DOC_ID, HOOK_FAILURES.COLLECTION,
                    HOOK_FAILURES.HOOK_NAME, HOOK_FAILURES.ERROR, HOOK_FAILURES.OCCURRED_AT,
                    HOOK_FAILURES.BATCH_DOC_IDS, HOOK_FAILURES.IS_BATCH, HOOK_FAILURES.CHAIN);
            for (var r : batch) {
                String chain = str(optS(r, "chain"));
                insert = insert.values(tenant, str(optS(r, "doc_id")), str(optS(r, "collection")),
                        reqS(r, "hook_name"), str(optS(r, "error")), parseTsStrict(reqS(r, "occurred_at")),
                        jsonbOrNull(optS(r, "batch_doc_ids")), Boolean.TRUE.equals(r.get("is_batch")),
                        chain.isBlank() ? "single" : chain);
            }
            insert.onConflictDoNothing().execute();
        }
    }

    private static void importFrecencyBatch(DSLContext ctx, String tenant, List<Map<String, Object>> rows) {
        // Conflict key: (tenant_id, chunk_id). tenant is constant for this call.
        // Dedupe last-wins (defensive; the ETL source's PK makes intra-batch
        // duplicates impossible in practice — same rationale as ChashRepository).
        // nexus-tk070.p6b (RDR-194 D5): ttl_days's ETL-fidelity default is
        // optInteger(r, "ttl_days") — null when omitted, matching the new
        // permanent sentinel — not optI(..., 0) (0 is retired and would
        // hit the CHECK on every row that simply omits the field). An
        // EXPLICIT ttl_days in the source row (including an explicit 0)
        // still flows through verbatim and hits the CHECK at INSERT if
        // invalid — same ETL-verbatim scope decision as memory-003-
        // ttl-days.xml's parseImportRow precedent.
        var unique = new java.util.LinkedHashMap<String, Map<String, Object>>(rows.size());
        for (var r : rows) unique.put(reqS(r, "chunk_id"), r);
        List<Map<String, Object>> deduped = List.copyOf(unique.values());

        final int chunkSize = Math.max(1, MAX_BATCH_PARAMS / 7);
        for (int start = 0; start < deduped.size(); start += chunkSize) {
            var batch = deduped.subList(start, Math.min(start + chunkSize, deduped.size()));
            var insert = ctx.insertInto(FRECENCY,
                    FRECENCY.TENANT_ID, FRECENCY.CHUNK_ID, FRECENCY.EMBEDDED_AT, FRECENCY.TTL_DAYS,
                    FRECENCY.FRECENCY_SCORE, FRECENCY.MISS_COUNT, FRECENCY.LAST_HIT_AT);
            for (var r : batch) {
                OffsetDateTime embeddedAt = optTsLenient(r, "embedded_at");
                OffsetDateTime lastHitAt  = optTsLenient(r, "last_hit_at");
                insert = insert.values(tenant, reqS(r, "chunk_id"), embeddedAt,
                        optInteger(r, "ttl_days"), optDd(r, "frecency_score", 0.0),
                        optI(r, "miss_count", 0), lastHitAt);
            }
            insert.onConflict(FRECENCY.TENANT_ID, FRECENCY.CHUNK_ID).doUpdate()
                  .set(FRECENCY.FRECENCY_SCORE, greatest(field(name("excluded", "frecency_score"), Double.class), FRECENCY.FRECENCY_SCORE))
                  .set(FRECENCY.MISS_COUNT, greatest(field(name("excluded", "miss_count"), Integer.class), FRECENCY.MISS_COUNT))
                  .set(FRECENCY.LAST_HIT_AT, greatest(field(name("excluded", "last_hit_at"), OffsetDateTime.class), FRECENCY.LAST_HIT_AT))
                  .set(FRECENCY.TTL_DAYS, greatest(field(name("excluded", "ttl_days"), Integer.class), FRECENCY.TTL_DAYS))
                  .set(FRECENCY.EMBEDDED_AT, least(field(name("excluded", "embedded_at"), OffsetDateTime.class), FRECENCY.EMBEDDED_AT))
                  .execute();
        }
    }

    // ── ids probe (RDR-178 wave-2 P1, bead nexus-s3dd4.3) ─────────────────────

    /**
     * Membership-probe for the verify-fill inner loop: given up to 300
     * candidate conflict-key tuples for one of the six telemetry tables,
     * return the SUBSET that already exists in this tenant's rows.
     *
     * <p>Matched tuples are echoed back VERBATIM from the input — never
     * reconstructed from the stored TIMESTAMPTZ/TEXT values — so a caller
     * computing {@code source_keys - present_keys} in Python cannot
     * false-negative on timestamp string-formatting drift (e.g. Postgres
     * rendering {@code "+00:00"} where the source sent {@code "Z"}). The
     * batch-size cap itself is enforced by the HTTP handler
     * ({@code TelemetryHandler}); this method just probes whatever it is
     * given (empty input is a no-op).
     *
     * <p>Conflict-key column order per table (tenant_id is implicit via RLS;
     * matches the UNIQUE indexes / PK defined in
     * {@code telemetry-001-baseline.xml} verbatim — see that changelog for
     * the authoritative source):
     * <ul>
     *   <li>{@code relevance_log}:    [query, chunk_id, action, session_id, timestamp]</li>
     *   <li>{@code search_telemetry}: [ts, query_hash, collection]</li>
     *   <li>{@code tier_writes}:      [session_id, ts, tool, tier]</li>
     *   <li>{@code nx_answer_runs}:   [question, created_at]</li>
     *   <li>{@code hook_failures}:    [doc_id, hook_name, occurred_at]</li>
     *   <li>{@code frecency}:         [chunk_id]</li>
     * </ul>
     *
     * <p>Implemented as one {@code EXISTS} check per candidate (not a single
     * row-value {@code IN} or {@code VALUES}-CTE join) inside ONE
     * {@link TenantScope#withTenant} transaction (RLS GUC set once) — at
     * <=300 candidates this is a handful of index-backed lookups, and
     * per-key EXISTS sidesteps the round-trip-formatting problem above by
     * construction (the DB comparison is instant-vs-instant on TIMESTAMPTZ
     * columns; only the ECHOED input string ever leaves this method).
     *
     * @throws IllegalArgumentException on an unknown {@code table}, or a key
     *     tuple whose arity does not match that table's conflict-key column
     *     count.
     */
    public List<List<Object>> probeIds(String tenant, String table, List<List<Object>> keys) {
        if (keys == null || keys.isEmpty()) return List.of();
        return tenantScope.withTenant(tenant, ctx -> {
            List<List<Object>> present = new ArrayList<>();
            for (List<Object> key : keys) {
                boolean exists = switch (table) {
                    case "relevance_log" -> {
                        requireArity(table, key, 5);
                        yield ctx.fetchExists(ctx.selectFrom(RELEVANCE_LOG).where(
                            RELEVANCE_LOG.QUERY.eq(ks(key, 0))
                                .and(RELEVANCE_LOG.CHUNK_ID.eq(ks(key, 1)))
                                .and(RELEVANCE_LOG.ACTION.eq(ks(key, 2)))
                                .and(RELEVANCE_LOG.SESSION_ID.eq(ks(key, 3)))
                                .and(RELEVANCE_LOG.TIMESTAMP.eq(parseTsStrict(ks(key, 4))))));
                    }
                    case "search_telemetry" -> {
                        requireArity(table, key, 3);
                        yield ctx.fetchExists(ctx.selectFrom(SEARCH_TELEMETRY).where(
                            SEARCH_TELEMETRY.TS.eq(parseTsStrict(ks(key, 0)))
                                .and(SEARCH_TELEMETRY.QUERY_HASH.eq(ks(key, 1)))
                                .and(SEARCH_TELEMETRY.COLLECTION.eq(ks(key, 2)))));
                    }
                    case "tier_writes" -> {
                        requireArity(table, key, 4);
                        yield ctx.fetchExists(ctx.selectFrom(TIER_WRITES).where(
                            TIER_WRITES.SESSION_ID.eq(ks(key, 0))
                                .and(TIER_WRITES.TS.eq(parseTsStrict(ks(key, 1))))
                                .and(TIER_WRITES.TOOL.eq(ks(key, 2)))
                                .and(TIER_WRITES.TIER.eq(ks(key, 3)))));
                    }
                    case "nx_answer_runs" -> {
                        requireArity(table, key, 2);
                        yield ctx.fetchExists(ctx.selectFrom(NX_ANSWER_RUNS).where(
                            NX_ANSWER_RUNS.QUESTION.eq(ks(key, 0))
                                .and(NX_ANSWER_RUNS.CREATED_AT.eq(parseTsStrict(ks(key, 1))))));
                    }
                    case "hook_failures" -> {
                        requireArity(table, key, 3);
                        yield ctx.fetchExists(ctx.selectFrom(HOOK_FAILURES).where(
                            HOOK_FAILURES.DOC_ID.eq(ks(key, 0))
                                .and(HOOK_FAILURES.HOOK_NAME.eq(ks(key, 1)))
                                .and(HOOK_FAILURES.OCCURRED_AT.eq(parseTsStrict(ks(key, 2))))));
                    }
                    case "frecency" -> {
                        requireArity(table, key, 1);
                        yield ctx.fetchExists(ctx.selectFrom(FRECENCY).where(
                            FRECENCY.CHUNK_ID.eq(ks(key, 0))));
                    }
                    default -> throw new IllegalArgumentException("Unknown table: " + table);
                };
                if (exists) present.add(key);
            }
            return present;
        });
    }

    /** Candidate-key element accessor: null-safe stringify (empty-string columns never store NULL). */
    private static String ks(List<Object> key, int idx) {
        Object v = key.get(idx);
        return v == null ? "" : v.toString();
    }

    private static void requireArity(String table, List<Object> key, int expected) {
        if (key.size() != expected) {
            throw new IllegalArgumentException(
                "table '" + table + "' conflict key must have " + expected +
                " elements, got " + key.size());
        }
    }

    // ── batch map-extraction helpers (mirror TelemetryHandler's per-row parse) ──
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
    private static double optDd(Map<String, Object> r, String k, double def) {
        Double d = optD(r, k);
        return d != null ? d : def;
    }
    private static Long optL(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null) return null;
        if (v instanceof Number n) return n.longValue();
        try { return Long.parseLong(v.toString()); } catch (NumberFormatException e) { return null; }
    }
    private static long optLd(Map<String, Object> r, String k, long def) {
        Long l = optL(r, k);
        return l != null ? l : def;
    }
    private static int reqI(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v instanceof Number n) return n.intValue();
        if (v != null) { try { return Integer.parseInt(v.toString()); } catch (NumberFormatException ignored) { } }
        throw new IllegalArgumentException("Missing required field: " + k);
    }
    private static int optI(Map<String, Object> r, String k, int def) {
        Object v = r.get(k);
        if (v instanceof Number n) return n.intValue();
        if (v != null) { try { return Integer.parseInt(v.toString()); } catch (NumberFormatException ignored) { } }
        return def;
    }
    /**
     * Nullable {@code Integer} read, mirroring {@link #optL}. nexus-tk070.p6b
     * (RDR-194 D5): {@code ttl_days}'s ETL default must be {@code null}
     * (permanent), not {@code 0} (retired, now CHECK-rejected) — an omitted
     * field means "no opinion, keep it permanent," not "explicitly zero."
     * An explicit {@code ttl_days} value (including an explicit {@code 0})
     * still passes through verbatim, ETL-fidelity-preserving, exactly like
     * {@link #optI}'s siblings — {@code 0} then fails loud at the CHECK,
     * never silently reinterpreted.
     */
    private static Integer optInteger(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null) return null;
        if (v instanceof Number n) return n.intValue();
        try { return Integer.parseInt(v.toString()); } catch (NumberFormatException e) { return null; }
    }
    private static OffsetDateTime optTsLenient(Map<String, Object> r, String k) {
        String s = optS(r, k);
        return (s != null && !s.isBlank()) ? parseTs(s) : OffsetDateTime.now(ZoneOffset.UTC);
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
            // nexus-tk070.p6b (RDR-194 D5): ttl_days can now be legitimately
            // NULL (permanent) — Map.of throws NPE on a null value, a latent
            // bug this migration would otherwise expose the first time a
            // permanent frecency row is fetched. A mutable LinkedHashMap
            // tolerates null; chunk_id/frecency_score/miss_count remain
            // NOT NULL columns and can never be null here regardless.
            var result = new java.util.LinkedHashMap<String, Object>();
            result.put("chunk_id",       rec.value1());
            result.put("embedded_at",    rec.value2() != null ? rec.value2().toString() : "");
            result.put("ttl_days",       rec.value3());
            result.put("frecency_score", rec.value4());
            result.put("miss_count",     rec.value5());
            result.put("last_hit_at",    rec.value6() != null ? rec.value6().toString() : "");
            return Optional.<Map<String, Object>>of(result);
        });
    }
}
