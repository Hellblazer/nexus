// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.http.HttpUtil;
import org.jooq.JSONB;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.TreeSet;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.MIGRATION_JOBS;

/**
 * RDR-178 Gap 5 (bead nexus-melvx) — jOOQ-based repository for {@code nexus.migration_jobs}.
 *
 * <p>Backs the async {@code POST /v1/migration/ingest-cloud} job contract: a bounded
 * worker pool (see {@code MigrationHandler}) runs the actual ChromaCloud→pgvector copy;
 * this repository tracks job state (queued/running/done/failed) and per-collection
 * progress so {@code GET /v1/migration/jobs/{id}} can be polled instead of the operator
 * having to "fire, absorb the 504, poll dest counts until parity" hack that motivated
 * this table (production 2026-07-01).
 *
 * <p><b>Credential non-persistence (BINDING, 2026-07-02 gate-critique constraint).</b>
 * No method on this class accepts or persists a ChromaCloud credential. The source
 * tenant/database/api-key live ONLY in {@code MigrationHandler}'s method-local worker
 * context and are never passed here. See {@code migration-001-baseline.xml} for the
 * full rationale and the schema test that enforces it.
 *
 * <p><b>Idempotency.</b> {@link #createOrReuseJob} is the sole write path for new jobs.
 * It first looks for an existing queued/running job with the same (tenant, collection-
 * set) — order-independent via {@link #canonicalKey} — and returns that job's id
 * instead of creating a second one. A concurrent-insert race is handled by catching the
 * partial unique index's 23505 violation and re-selecting the winner, all within the
 * same {@link TenantScope#withTenant} transaction.
 */
public final class MigrationJobRepository {

    private static final Logger log = LoggerFactory.getLogger(MigrationJobRepository.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<LinkedHashMap<String, Object>> MAP_TYPE = new TypeReference<>() {};

    /** PostgreSQL SQLSTATE for a unique-constraint violation (class 23). */
    private static final String SQLSTATE_UNIQUE_VIOLATION = "23505";

    private final TenantScope tenantScope;

    public MigrationJobRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * Result of {@link #createOrReuseJob}: the job id, and whether it is a NEWLY
     * created job ({@code created=true}) or an existing active job returned for
     * idempotency ({@code created=false} — the caller must NOT submit new work).
     */
    public record CreateResult(String jobId, boolean created) {}

    /**
     * Create a new queued job for {@code collections}, or — if a queued/running job
     * already exists for the same (tenant, collection-set) — return that job's id.
     *
     * @param tenant      the tenant (RLS discriminator)
     * @param collections the requested collection names, in caller order (stored
     *                    verbatim in the {@code collections} column for audit/display;
     *                    the idempotency key is order-independent)
     */
    public CreateResult createOrReuseJob(String tenant, List<String> collections) {
        String collectionsKey = canonicalKey(collections);
        return tenantScope.withTenant(tenant, ctx -> {
            String existing = findActiveJobId(ctx, collectionsKey);
            if (existing != null) {
                return new CreateResult(existing, false);
            }
            String jobId = UUID.randomUUID().toString();
            try {
                ctx.insertInto(MIGRATION_JOBS)
                    .set(MIGRATION_JOBS.JOB_ID, jobId)
                    .set(MIGRATION_JOBS.TENANT_ID, tenant)
                    .set(MIGRATION_JOBS.STATE, "queued")
                    .set(MIGRATION_JOBS.COLLECTIONS, JSONB.valueOf(toJson(collections)))
                    .set(MIGRATION_JOBS.COLLECTIONS_KEY, collectionsKey)
                    .set(MIGRATION_JOBS.PER_COLLECTION_COUNTS, JSONB.valueOf("{}"))
                    .set(MIGRATION_JOBS.CREATED_AT, OffsetDateTime.now(ZoneOffset.UTC))
                    .execute();
                return new CreateResult(jobId, true);
            } catch (RuntimeException e) {
                // A concurrent request won the (tenant_id, collections_key) partial
                // unique index race between our findActiveJobId() check and this
                // INSERT. Re-select the winner rather than propagating the conflict —
                // this IS the idempotency contract, not an error.
                if (SQLSTATE_UNIQUE_VIOLATION.equals(HttpUtil.sqlState23(e))) {
                    String winner = findActiveJobId(ctx, collectionsKey);
                    if (winner != null) {
                        return new CreateResult(winner, false);
                    }
                }
                throw e;
            }
        });
    }

    private static String findActiveJobId(org.jooq.DSLContext ctx, String collectionsKey) {
        return ctx.select(MIGRATION_JOBS.JOB_ID)
            .from(MIGRATION_JOBS)
            .where(MIGRATION_JOBS.COLLECTIONS_KEY.eq(collectionsKey))
            .and(MIGRATION_JOBS.STATE.in("queued", "running"))
            .orderBy(MIGRATION_JOBS.CREATED_AT.desc())
            .limit(1)
            .fetchOne(MIGRATION_JOBS.JOB_ID);
    }

    /** Transition a job to {@code running} and stamp {@code started_at}. */
    public void markRunning(String tenant, String jobId) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(MIGRATION_JOBS)
                .set(MIGRATION_JOBS.STATE, "running")
                .set(MIGRATION_JOBS.STARTED_AT, OffsetDateTime.now(ZoneOffset.UTC))
                .where(MIGRATION_JOBS.JOB_ID.eq(jobId))
                .execute();
            return null;
        });
    }

    /**
     * Record the result of copying one collection — a read-modify-write merge into
     * {@code per_collection_counts} so the poller sees progress incrementally rather
     * than only at job completion.
     *
     * @param copied   chunks read from the source and upserted into pgvector
     * @param dest     rows actually present in pgvector for this collection after the
     *                 upsert (the parity signal the 2026-07-01 operator hack polled for)
     * @param expected the source-reported count for this collection (today equal to
     *                 {@code copied} — page reads are exhaustive per collection — kept
     *                 as a separate field for a future pre-flight count call)
     */
    public void recordCollectionResult(String tenant, String jobId, String collection,
                                        int copied, int dest, int expected) {
        tenantScope.withTenant(tenant, ctx -> {
            JSONB current = ctx.select(MIGRATION_JOBS.PER_COLLECTION_COUNTS)
                .from(MIGRATION_JOBS)
                .where(MIGRATION_JOBS.JOB_ID.eq(jobId))
                .fetchOne(MIGRATION_JOBS.PER_COLLECTION_COUNTS);
            Map<String, Object> counts = current != null ? parseJsonObject(current.data()) : new LinkedHashMap<>();
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("copied", copied);
            entry.put("dest", dest);
            entry.put("expected", expected);
            counts.put(collection, entry);
            ctx.update(MIGRATION_JOBS)
                .set(MIGRATION_JOBS.PER_COLLECTION_COUNTS, JSONB.valueOf(toJson(counts)))
                .where(MIGRATION_JOBS.JOB_ID.eq(jobId))
                .execute();
            return null;
        });
    }

    /** Transition a job to {@code done} and stamp {@code finished_at}. */
    public void markDone(String tenant, String jobId) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(MIGRATION_JOBS)
                .set(MIGRATION_JOBS.STATE, "done")
                .set(MIGRATION_JOBS.FINISHED_AT, OffsetDateTime.now(ZoneOffset.UTC))
                .where(MIGRATION_JOBS.JOB_ID.eq(jobId))
                .execute();
            return null;
        });
    }

    /**
     * Transition a job to {@code failed}, stamp {@code finished_at}, and record
     * {@code error}. {@code error} MUST be the exception TYPE only (e.g.
     * {@code e.getClass().getName()}) — never the raw exception message, which can
     * embed a third-party (Chroma) response body. Same posture as the existing
     * sync-path log statement in {@code MigrationHandler}.
     */
    public void markFailed(String tenant, String jobId, String error) {
        tenantScope.withTenant(tenant, ctx -> {
            ctx.update(MIGRATION_JOBS)
                .set(MIGRATION_JOBS.STATE, "failed")
                .set(MIGRATION_JOBS.FINISHED_AT, OffsetDateTime.now(ZoneOffset.UTC))
                .set(MIGRATION_JOBS.ERROR, error)
                .where(MIGRATION_JOBS.JOB_ID.eq(jobId))
                .execute();
            return null;
        });
    }

    /**
     * Tenant-scoped fetch for {@code GET /v1/migration/jobs/{id}}. RLS confines the
     * WHERE to the caller's own tenant — a foreign-tenant's job_id simply matches no
     * row, so the handler renders that as 404 rather than this method needing an
     * explicit cross-tenant check.
     */
    public Optional<Map<String, Object>> getJob(String tenant, String jobId) {
        return tenantScope.withTenant(tenant, ctx -> {
            var rec = ctx.select(
                    MIGRATION_JOBS.STATE,
                    MIGRATION_JOBS.PER_COLLECTION_COUNTS,
                    MIGRATION_JOBS.STARTED_AT,
                    MIGRATION_JOBS.FINISHED_AT,
                    MIGRATION_JOBS.ERROR)
                .from(MIGRATION_JOBS)
                .where(MIGRATION_JOBS.JOB_ID.eq(jobId))
                .fetchOne();
            if (rec == null) {
                return Optional.<Map<String, Object>>empty();
            }
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("job_id", jobId);
            m.put("state", rec.value1());
            JSONB counts = rec.value2();
            m.put("per_collection", counts != null ? parseJsonObject(counts.data()) : Map.of());
            m.put("started_at", rec.value3() != null ? rec.value3().toString() : null);
            m.put("finished_at", rec.value4() != null ? rec.value4().toString() : null);
            m.put("error", rec.value5());
            return Optional.of(m);
        });
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /**
     * Canonical, order-independent idempotency key: sorted, deduped, JSON-array-
     * encoded. Two POSTs naming the same collection set in different orders collide
     * on this key regardless of list order.
     */
    static String canonicalKey(List<String> collections) {
        TreeSet<String> sorted = new TreeSet<>();
        if (collections != null) {
            for (String c : collections) {
                if (c != null && !c.isBlank()) {
                    sorted.add(c.trim());
                }
            }
        }
        return toJson(new ArrayList<>(sorted));
    }

    private static String toJson(Object o) {
        try {
            return MAPPER.writeValueAsString(o);
        } catch (Exception e) {
            throw new RuntimeException("failed to serialize migration_jobs JSONB payload", e);
        }
    }

    private static Map<String, Object> parseJsonObject(String json) {
        if (json == null || json.isBlank()) {
            return new LinkedHashMap<>();
        }
        try {
            return MAPPER.readValue(json, MAP_TYPE);
        } catch (Exception e) {
            log.warn("event=migration_job_parse_counts_failed error_type={}", e.getClass().getName());
            return new LinkedHashMap<>();
        }
    }
}
