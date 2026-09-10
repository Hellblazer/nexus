// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.jooq.nexus.tables.records.TuplesRecord;
import dev.nexus.service.tuples.TemplateRegistry;
import dev.nexus.service.tuples.TemplateSchema;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.impl.DSL;
import org.jooq.types.DayToSecond;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.TreeSet;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;

/**
 * RDR-205 Phase 1 Step 4 (bead nexus-em75s.4): the Linda tuple space's jOOQ
 * repository — {@code out}, {@code rd}/{@code rdp}, {@code in}/{@code inp},
 * {@code ack}/{@code nack}, {@code registry}, {@code subspace_list}/{@code
 * subspace_stats}. See {@code docs/rdr/rdr-205-linda-tuple-space-over-
 * postgres.md} §Technical Design — this class, not the RDR's illustrative
 * jOOQ block, is the authority on the claim statement's exact shape.
 *
 * <p>Reuses {@link TenantScope} (forced-RLS tenant-scoped transactions) and
 * mirrors {@link AspectRepository#claimNext}'s {@code FOR ... SKIP LOCKED}
 * shape, but with {@code FOR NO KEY UPDATE} (RDR-205's own reasoning: the
 * update touches no key column, and the weaker lock lets the claim log's
 * foreign key coexist without a lock upgrade inside the transaction). Does
 * NOT reuse {@link AspectRepository#claimBatch} — that method's repeated-
 * single-claim loop is a different shape from the bounded dead-letter
 * re-run this class's claim statement performs internally.
 *
 * <p>Now/interval split follows the established codebase convention (see
 * {@link CatalogRepository#agedThreshold}): WHERE-clause comparisons against
 * stored columns use {@link DSL#currentOffsetDateTime()} (the Postgres
 * server clock, typed — never a raw {@code now()} string); values WRITTEN
 * into a row use the JVM clock ({@code OffsetDateTime.now(ZoneOffset.UTC)}),
 * the same split {@link AspectRepository#claimNext} already uses between its
 * WHERE-clause backoff gate and its {@code last_attempt_at} write.
 */
public final class TupleRepository {

    private static final Logger log = LoggerFactory.getLogger(TupleRepository.class);

    public static final String READ_MAX_ENV = "NX_TUPLE_READ_MAX";
    public static final int DEFAULT_READ_MAX = 300;

    public static final String CLAIM_PASSES_ENV = "NX_TUPLE_CLAIM_PASSES";
    public static final int DEFAULT_CLAIM_PASSES = 8;

    public static final String TIMEOUT_CAP_SECONDS_ENV = "NX_TUPLE_TIMEOUT_CAP_SECONDS";
    public static final int DEFAULT_TIMEOUT_CAP_SECONDS = 25;

    public static final String PARK_CAP_PER_CLAIMANT_ENV = "NX_TUPLE_PARK_CAP_PER_CLAIMANT";
    public static final int DEFAULT_PARK_CAP_PER_CLAIMANT = 4;

    public static final String PARK_CAP_GLOBAL_ENV = "NX_TUPLE_PARK_CAP_GLOBAL";
    public static final int DEFAULT_PARK_CAP_GLOBAL = 16;

    private static final String CLAIM_STATE_CLAIMED = "claimed";
    private static final String CLAIM_STATE_DEAD = "dead";
    private static final String TRANSITION_CLAIM = "claim";
    private static final String TRANSITION_ACK = "ack";
    private static final String TRANSITION_NACK = "nack";
    private static final String TRANSITION_EXPIRE = "expire";
    private static final String TRANSITION_DEAD = "dead";

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, String>> STRING_MAP = new TypeReference<>() {
    };

    /**
     * TEST-ONLY seam (RDR-205 bead nexus-em75s.6, the CA 1 regression pin): invoked
     * inside {@link #claimOnce}'s transaction between the claim {@code SELECT ... FOR
     * NO KEY UPDATE SKIP LOCKED} and its {@code UPDATE}, so a test can widen the race
     * window the lock clause must close -- the exact shape the RDR-110 CA#2 finding
     * and the {@code nexus_rdr/205-research-3} spike (measurement 1) both probe with
     * an injected {@code pg_sleep} at the same point in the raw SQL. A no-op {@link
     * Runnable} by default, so it costs nothing on the production path; package-
     * private so only a test in this exact package can reach it, and never assigned
     * outside test code. NOT a mechanism for production delay injection of any kind.
     */
    static volatile Runnable TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY = () -> { };

    /**
     * TEST-ONLY (RDR-205 bead nexus-em75s.7, the wake-test mutation pins): installs a
     * hook invoked once per {@code (tenant, subspace)} group {@link TupleWaitRegistry
     * #signalAll} actually delivers a signal to, so a test can count SIGNAL-DRIVEN
     * wakes and distinguish them from the registry's own 1-second timer fallback --
     * catching a {@code signalAll} that silently widens to every group, which an
     * un-instrumented black-box test cannot tell apart from correct behaviour. The
     * wake tests pinning this live in {@code dev.nexus.service} (a different package
     * from {@link TupleWaitRegistry}'s package-private hook field), hence this public
     * cross-package installer. Pass {@code null} to restore the no-op default. Never
     * call this outside test code.
     */
    public static void setTestOnlySignalHook(java.util.function.BiConsumer<String, String> hookOrNull) {
        TupleWaitRegistry.TEST_ONLY_SIGNAL_HOOK = hookOrNull == null ? (tenant, subspace) -> { } : hookOrNull;
    }

    private final TenantScope tenantScope;
    private final TemplateRegistry registry;
    private final TupleWaitRegistry waitRegistry;
    private final int readMax;
    private final int claimPasses;
    private final int timeoutCapSeconds;

    public TupleRepository(TenantScope tenantScope, TemplateRegistry registry) {
        this(tenantScope, registry, DEFAULT_READ_MAX, DEFAULT_CLAIM_PASSES, DEFAULT_TIMEOUT_CAP_SECONDS,
                DEFAULT_PARK_CAP_PER_CLAIMANT, DEFAULT_PARK_CAP_GLOBAL);
    }

    public TupleRepository(TenantScope tenantScope, TemplateRegistry registry,
                            int readMax, int claimPasses, int timeoutCapSeconds,
                            int parkCapPerClaimant, int parkCapGlobal) {
        this.tenantScope = tenantScope;
        this.registry = registry;
        this.readMax = readMax;
        this.claimPasses = claimPasses;
        this.timeoutCapSeconds = timeoutCapSeconds;
        this.waitRegistry = new TupleWaitRegistry(parkCapPerClaimant, parkCapGlobal);
    }

    /** Production boot call: reads every setting via {@code System.getenv} directly. */
    public static TupleRepository fromEnv(TenantScope tenantScope, TemplateRegistry registry) {
        return new TupleRepository(tenantScope, registry,
                intEnv(READ_MAX_ENV, DEFAULT_READ_MAX),
                intEnv(CLAIM_PASSES_ENV, DEFAULT_CLAIM_PASSES),
                intEnv(TIMEOUT_CAP_SECONDS_ENV, DEFAULT_TIMEOUT_CAP_SECONDS),
                intEnv(PARK_CAP_PER_CLAIMANT_ENV, DEFAULT_PARK_CAP_PER_CLAIMANT),
                intEnv(PARK_CAP_GLOBAL_ENV, DEFAULT_PARK_CAP_GLOBAL));
    }

    private static int intEnv(String name, int defaultValue) {
        String v = System.getenv(name);
        if (v == null || v.isBlank()) {
            return defaultValue;
        }
        return Integer.parseInt(v.trim());
    }

    /** Signals every parked {@code rd}/{@code in} so they return the probe result before
     *  the engine finishes stopping. Call from {@code NexusService.stop()}. */
    public void shutdown() {
        waitRegistry.shutdown();
    }

    // ── records ──────────────────────────────────────────────────────────────

    public record TupleRow(
            byte[] id, String subspace, String template,
            Map<String, String> keys, Map<String, String> dims, String body,
            String claimState, String claimant, String claimId,
            OffsetDateTime leaseUntil, int attempts,
            OffsetDateTime consumedAt, String consumedBy,
            OffsetDateTime expiresAt, OffsetDateTime createdAt) {
    }

    public record ClaimedTuple(TupleRow tuple, String claimId) {
    }

    public record ReadCursor(OffsetDateTime createdAt, byte[] id) {
    }

    public record SubspaceCensus(
            String subspace, long total, long available, long claimed, long dead,
            long consumed, long expiredUnpurged,
            OffsetDateTime oldestCreatedAt, OffsetDateTime newestCreatedAt) {
    }

    /**
     * RDR-205 Phase 1 Step 5 (bead nexus-em75s.5): one BATCH of the sweep's release
     * arm — always {@code scanned == released + deadLettered}, mirroring the RDR-204
     * ghost sweep's {@code GhostSweepResult} shape ({@code scanned} == the sum of its
     * three dispositions).
     */
    public record ReleaseBatchResult(int scanned, int released, int deadLettered) {
    }

    /**
     * RDR-205 Phase 1 follow-on (bead nexus-em75s.38, review finding M9): one BATCH
     * of a sweep PURGE arm ({@link #purgeExpiredTuplesBatch} or {@link
     * #purgeOldClaimLogBatch}) — {@code examined} is the row count the arm's own
     * candidate SELECT returned (capped at {@code batchSize}, same drained
     * convention as {@link ReleaseBatchResult#scanned}), captured BEFORE the delete
     * and independently of it; {@code purged} is the delete statement's own affected-
     * row count. The two are obtained from two different statements, so a future
     * divergence between them (a row skip-locked out from under the delete, say)
     * would be visible; today they always agree on a healthy path, but only because
     * both statements succeeded, not because one is derived from the other.
     */
    public record PurgeBatchResult(int examined, int purged) {
    }

    // ── out ──────────────────────────────────────────────────────────────────

    /** {@code out(subspace, keys, dims, body, *, nonce=None, ttl_seconds=None) -> id}. */
    public byte[] out(String tenant, String subspace, Map<String, String> keys, Map<String, String> dims,
                       String body, String nonce, Long ttlSecondsOrNull) {
        TemplateSchema t = resolveOrThrow(subspace);
        Map<String, String> keysSafe = keys == null ? Map.of() : keys;
        Map<String, String> dimsSafe = dims == null ? Map.of() : dims;
        validateOut(t, keysSafe, dimsSafe, nonce);

        if (ttlSecondsOrNull != null && ttlSecondsOrNull <= 0) {
            throw new SchemaViolationException("ttl_seconds", "must be positive");
        }
        long ttlSeconds = ttlSecondsOrNull == null ? t.retentionSeconds() : ttlSecondsOrNull;
        if (ttlSeconds > t.retentionSeconds()) {
            throw new TtlTooLongException(ttlSeconds, t.retentionSeconds(), t.name());
        }

        byte[] id = computeId(tenant, subspace, t, keysSafe, dimsSafe, nonce, body);
        JSONB keysJsonb = toJsonb(keysSafe);
        JSONB dimsJsonb = dimsSafe.isEmpty() ? null : toJsonb(dimsSafe);
        DayToSecond ttlInterval = interval(ttlSeconds);
        DayToSecond retentionInterval = interval(t.retentionSeconds());

        byte[] result = tenantScope.withTenant(tenant, ctx -> {
            Field<OffsetDateTime> candidateExpiry = DSL.currentOffsetDateTime().add(ttlInterval);
            // Refire clamp (RDR-205 §Technical Design "out"): never past the ORIGINAL
            // row's created_at plus the template's retention — TUPLES.CREATED_AT here
            // binds to the pre-existing target row, exactly as AspectRepository's
            // insertOrUpdateExtractionQueue mixes EXCLUDED.* with a plain column
            // reference for the OLD value in the same DO UPDATE clause.
            Field<OffsetDateTime> ceiling = TUPLES.CREATED_AT.add(retentionInterval);

            Field<JSONB> dimsField = dimsJsonb == null
                    ? DSL.castNull(org.jooq.impl.SQLDataType.JSONB)
                    : DSL.val(dimsJsonb);
            ctx.insertInto(TUPLES,
                            TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE,
                            TUPLES.KEYS, TUPLES.DIMS, TUPLES.BODY,
                            TUPLES.ATTEMPTS, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                    .values(DSL.val(id), DSL.val(tenant), DSL.val(subspace), DSL.val(t.name()),
                            DSL.val(keysJsonb), dimsField, DSL.val(body),
                            DSL.val(0), DSL.currentOffsetDateTime().add(ttlInterval), DSL.currentOffsetDateTime())
                    .onConflict(TUPLES.ID)
                    .doUpdate()
                    // A refire touches expires_at ONLY — never body, claim state or
                    // consumed state (every other column is simply absent from this
                    // DO UPDATE's .set() list, so Postgres leaves it untouched).
                    .set(TUPLES.EXPIRES_AT, DSL.least(candidateExpiry, ceiling))
                    .execute();

            maintainTenant(ctx, tenant);
            return id;
        });

        // Signal AFTER the transaction lambda returns (the commit) — never from inside it.
        waitRegistry.signalAll(tenant, subspace);
        return result;
    }

    private void maintainTenant(DSLContext ctx, String tenant) {
        OffsetDateTime lastSeen = ctx.select(TUPLE_TENANTS.LAST_SEEN)
                .from(TUPLE_TENANTS)
                .where(TUPLE_TENANTS.TENANT_ID.eq(tenant))
                .fetchOne(TUPLE_TENANTS.LAST_SEEN);
        if (lastSeen != null && lastSeen.isAfter(OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(1))) {
            return;
        }
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        ctx.insertInto(TUPLE_TENANTS, TUPLE_TENANTS.TENANT_ID, TUPLE_TENANTS.FIRST_SEEN, TUPLE_TENANTS.LAST_SEEN)
                .values(tenant, now, now)
                .onConflict(TUPLE_TENANTS.TENANT_ID)
                .doUpdate()
                .set(TUPLE_TENANTS.LAST_SEEN, now)
                .execute();
    }

    private void validateOut(TemplateSchema t, Map<String, String> keys, Map<String, String> dims, String nonce) {
        TreeSet<String> unknownKeys = new TreeSet<>(keys.keySet());
        unknownKeys.removeAll(t.keys());
        if (!unknownKeys.isEmpty()) {
            throw new SchemaViolationException(unknownKeys.first(), "not a declared key for this template");
        }
        for (String k : t.keys()) {
            String v = keys.get(k);
            if (v == null || v.isBlank()) {
                throw new SchemaViolationException(k, "required key missing or blank");
            }
            List<String> allowed = t.keyValues().get(k);
            if (allowed != null && !allowed.contains(v)) {
                throw new SchemaViolationException(k, "value '" + v + "' not in " + allowed);
            }
        }
        TreeSet<String> unknownDims = new TreeSet<>(dims.keySet());
        unknownDims.removeAll(t.dimensions().keySet());
        if (!unknownDims.isEmpty()) {
            throw new SchemaViolationException(unknownDims.first(), "not a declared dimension for this template");
        }
        for (var e : t.dimensions().entrySet()) {
            String name = e.getKey();
            TemplateSchema.Dimension d = e.getValue();
            String v = dims.get(name);
            if (d.required() && (v == null || v.isBlank())) {
                throw new SchemaViolationException(name, "required dimension missing or blank");
            }
            if (v != null && d.values() != null && !d.values().contains(v)) {
                throw new SchemaViolationException(name, "value '" + v + "' not in " + d.values());
            }
        }
        if (t.idFrom() == TemplateSchema.IdFrom.KEYS_NONCE && (nonce == null || nonce.isBlank())) {
            throw new SchemaViolationException("nonce", "required for id_from=keys+nonce");
        }
    }

    /**
     * {@code sha256(canonical(tenant, subspace, keys [, id_dims, nonce | body]))} per the
     * template's {@code id_from} (RDR-205 §Technical Design). Every input is caller-
     * supplied and the field ORDER is the template's own declared order (deterministic —
     * {@link TemplateSchema#keys()}/{@link TemplateSchema#idDims()} are immutable lists
     * fixed at template-load time), so the same logical tuple always hashes identically.
     * Insert time is never part of the id (RDR-110 gate finding C3).
     *
     * <p>RDR-205 Phase 1 review (nexus-em75s.7, the RDR-110 C3 class recurring): every
     * field is fed to the digest via {@link #digestField}, which length-prefixes it
     * instead of being joined into a delimited string first -- the ORIGINAL join used a
     * literal NUL byte (0x00) between fields and a plain {@code '='} inside a key/dim
     * pair, and neither is escaped: a caller-supplied key/dim/nonce/body value that
     * itself contains that same byte could make two logically distinct tuples collide
     * on the same id. Length-prefixing makes the encoding unambiguous regardless of
     * what bytes any field contains.
     */
    private static byte[] computeId(String tenant, String subspace, TemplateSchema t,
                                     Map<String, String> keys, Map<String, String> dims,
                                     String nonce, String body) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            digestField(md, tenant);
            digestField(md, subspace);
            for (String k : t.keys()) {
                digestField(md, k);
                digestField(md, keys.get(k));
            }
            for (String d : t.idDims()) {
                digestField(md, d);
                digestField(md, dims.get(d));
            }
            switch (t.idFrom()) {
                case KEYS -> {
                    // nothing further
                }
                case KEYS_NONCE -> {
                    digestField(md, "nonce");
                    digestField(md, nonce);
                }
                case KEYS_BODY -> {
                    digestField(md, "body");
                    digestField(md, body == null ? "" : body);
                }
            }
            return md.digest();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    /**
     * Feeds one field to {@code md} as a 4-byte big-endian UTF-8 byte-length prefix
     * followed by the field's own bytes -- no sequence of (length, bytes) pairs can be
     * reinterpreted as a different sequence, which is what makes {@link #computeId}
     * injective across field boundaries regardless of a field's own content. A null
     * value encodes as length -1, distinct from an empty string's length 0.
     */
    private static void digestField(MessageDigest md, String value) {
        if (value == null) {
            md.update(ByteBuffer.allocate(4).putInt(-1).array());
            return;
        }
        byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
        md.update(ByteBuffer.allocate(4).putInt(bytes.length).array());
        md.update(bytes);
    }

    // ── rd / rdp ─────────────────────────────────────────────────────────────

    /** {@code rdp(subspace, keys_pattern=None, *, n=1, since=None) -> [Tuple]} — probe, never blocks. */
    public List<TupleRow> rdp(String tenant, String subspace, Map<String, String> pattern, int n, ReadCursor since) {
        return queryOnce(tenant, subspace, pattern, n, since);
    }

    /** {@code rd(subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0) -> [Tuple]} — blocks up to {@code timeoutSeconds}. */
    public List<TupleRow> rd(String tenant, String subspace, Map<String, String> pattern, int n,
                              ReadCursor since, long timeoutSeconds) {
        validateTimeout(timeoutSeconds);
        if (timeoutSeconds <= 0) {
            return queryOnce(tenant, subspace, pattern, n, since);
        }
        // Registered BEFORE the first query, so a write landing between that query and
        // the first park is not lost (RDR-205 §Technical Design "Wake").
        TupleWaitRegistry.Waiter waiter = waitRegistry.register(tenant, subspace);
        List<TupleRow> found = queryOnce(tenant, subspace, pattern, n, since);
        if (!found.isEmpty()) {
            return found;
        }
        waitRegistry.tryAcquireParkSlot(null);
        try {
            long deadlineNanos = System.nanoTime() + TimeUnit.SECONDS.toNanos(timeoutSeconds);
            while (true) {
                if (waitRegistry.isShuttingDown() || System.nanoTime() >= deadlineNanos) {
                    return queryOnce(tenant, subspace, pattern, n, since);
                }
                try {
                    waiter.awaitSignalOrTimer();
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return queryOnce(tenant, subspace, pattern, n, since);
                }
                List<TupleRow> again = queryOnce(tenant, subspace, pattern, n, since);
                if (!again.isEmpty()) {
                    return again;
                }
            }
        } finally {
            waitRegistry.releaseParkSlot(null);
            waiter.release();
        }
    }

    private List<TupleRow> queryOnce(String tenant, String subspace, Map<String, String> pattern,
                                      int n, ReadCursor since) {
        // RDR-205 review (nexus-em75s.35, M4): rd/rdp must refuse an unregistered
        // subspace exactly as out() and in()/inp() (via claimOnce) do -- this was
        // the one "Once" helper that never resolved the template, so a probe/read
        // against a bogus subspace silently read back empty instead of raising.
        resolveOrThrow(subspace);
        Map<String, String> patternSafe = pattern == null ? Map.of() : pattern;
        int limit = Math.min(n <= 0 ? 1 : n, readMax);

        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = TUPLES.TENANT_ID.eq(tenant)
                    .and(TUPLES.SUBSPACE.eq(subspace))
                    .and(TUPLES.CONSUMED_AT.isNull())
                    .and(TUPLES.EXPIRES_AT.gt(DSL.currentOffsetDateTime()));
            for (var e : patternSafe.entrySet()) {
                cond = cond.and(DSL.jsonbGetAttributeAsText(TUPLES.KEYS, e.getKey()).eq(e.getValue()));
            }
            if (since != null) {
                cond = cond.and(DSL.row(TUPLES.CREATED_AT, TUPLES.ID)
                        .gt(DSL.row(DSL.val(since.createdAt()), DSL.val(since.id()))));
            }
            var rows = ctx.selectFrom(TUPLES)
                    .where(cond)
                    .orderBy(TUPLES.CREATED_AT.asc(), TUPLES.ID.asc())
                    .limit(limit)
                    .fetch();
            List<TupleRow> out = new ArrayList<>();
            for (TuplesRecord r : rows) {
                out.add(toRow(r));
            }
            return out;
        });
    }

    // ── in / inp ─────────────────────────────────────────────────────────────

    /** {@code inp(subspace, keys_pattern, *, claimant, lease_s) -> (Tuple, claim_id) | None} — probe, never blocks. */
    public Optional<ClaimedTuple> inp(String tenant, String subspace, Map<String, String> pattern,
                                       String claimant, long leaseSeconds) {
        return claimOnce(tenant, subspace, pattern, claimant, leaseSeconds);
    }

    /** {@code in(subspace, keys_pattern, *, claimant, lease_s, timeout_s=0) -> (Tuple, claim_id) | None} — blocks up to {@code timeoutSeconds}. */
    public Optional<ClaimedTuple> in(String tenant, String subspace, Map<String, String> pattern,
                                      String claimant, long leaseSeconds, long timeoutSeconds) {
        validateTimeout(timeoutSeconds);
        if (timeoutSeconds <= 0) {
            return claimOnce(tenant, subspace, pattern, claimant, leaseSeconds);
        }
        TupleWaitRegistry.Waiter waiter = waitRegistry.register(tenant, subspace);
        Optional<ClaimedTuple> found = claimOnce(tenant, subspace, pattern, claimant, leaseSeconds);
        if (found.isPresent()) {
            return found;
        }
        waitRegistry.tryAcquireParkSlot(claimant);
        try {
            long deadlineNanos = System.nanoTime() + TimeUnit.SECONDS.toNanos(timeoutSeconds);
            while (true) {
                if (waitRegistry.isShuttingDown() || System.nanoTime() >= deadlineNanos) {
                    return claimOnce(tenant, subspace, pattern, claimant, leaseSeconds);
                }
                try {
                    waiter.awaitSignalOrTimer();
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return claimOnce(tenant, subspace, pattern, claimant, leaseSeconds);
                }
                Optional<ClaimedTuple> again = claimOnce(tenant, subspace, pattern, claimant, leaseSeconds);
                if (again.isPresent()) {
                    return again;
                }
            }
        } finally {
            waitRegistry.releaseParkSlot(claimant);
            waiter.release();
        }
    }

    private Optional<ClaimedTuple> claimOnce(String tenant, String subspace, Map<String, String> pattern,
                                              String claimant, long leaseSeconds) {
        TemplateSchema t = resolveOrThrow(subspace);
        if (!t.take().enabled()) {
            throw new TakeDisabledException(subspace, t.name());
        }
        if (leaseSeconds <= 0) {
            throw new SchemaViolationException("lease_s", "must be positive");
        }
        Long maxLease = t.take().maxLeaseSeconds();
        if (maxLease != null && leaseSeconds > maxLease) {
            throw new LeaseTooLongException(leaseSeconds, maxLease, t.name());
        }
        Map<String, String> patternSafe = pattern == null ? Map.of() : pattern;
        for (String k : t.keys()) {
            String v = patternSafe.get(k);
            if (v == null || v.isBlank()) {
                throw new SchemaViolationException(k, "missing pinned key for in/inp match");
            }
        }
        long maxAttempts = t.take().maxAttempts() == null ? Long.MAX_VALUE : t.take().maxAttempts();

        return tenantScope.withTenant(tenant, ctx -> {
            Condition matchCond = matchCondition(tenant, subspace, patternSafe, t.keys());

            // Same-claimant idempotent retake (RDR-205 §Technical Design "Claim"): a
            // retry after a lost response must recover the SAME claim, not take a new
            // one or fail. Checked BEFORE the claim statement, no new update, no log row.
            // RDR-205 Phase 1 review (nexus-em75s.7, ship-blocker): LEASE_UNTIL must be
            // checked too — without it, a claimant whose own lease already LAPSED (but
            // nobody has re-claimed the row yet) reads back the stale, dead claim_id
            // instead of falling through to the claim loop below and taking a fresh one.
            TuplesRecord existing = ctx.selectFrom(TUPLES)
                    .where(matchCond
                            .and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_CLAIMED))
                            .and(TUPLES.CLAIMANT.eq(claimant))
                            .and(TUPLES.CONSUMED_AT.isNull())
                            .and(TUPLES.EXPIRES_AT.gt(DSL.currentOffsetDateTime()))
                            .and(TUPLES.LEASE_UNTIL.gt(DSL.currentOffsetDateTime())))
                    .orderBy(TUPLES.CREATED_AT.asc())
                    .limit(1)
                    .fetchOne();
            if (existing != null) {
                return Optional.of(new ClaimedTuple(toRow(existing), existing.getClaimId()));
            }

            for (int pass = 0; pass < claimPasses; pass++) {
                TuplesRecord row = ctx.selectFrom(TUPLES)
                        .where(matchCond
                                .and(TUPLES.CONSUMED_AT.isNull())
                                .and(TUPLES.EXPIRES_AT.gt(DSL.currentOffsetDateTime()))
                                .and(TUPLES.CLAIM_STATE.isDistinctFrom(CLAIM_STATE_DEAD))
                                .and(TUPLES.CLAIM_STATE.isNull()
                                        .or(TUPLES.LEASE_UNTIL.lt(DSL.currentOffsetDateTime()))))
                        .orderBy(TUPLES.CREATED_AT.asc())
                        .limit(1)
                        .forNoKeyUpdate()
                        .skipLocked()
                        .fetchOne();
                if (row == null) {
                    return Optional.empty();
                }
                // TEST-ONLY (nexus-em75s.6): widens the select-to-update race window
                // under test; a no-op Runnable on every production path.
                TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY.run();

                int attempts = row.getAttempts();
                OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
                if (row.getClaimState() != null) {
                    // A candidate this predicate returned with a non-null claim_state can
                    // only be a LAPSED lease (dead is excluded, claimed-and-live would fail
                    // the lease_until<now() arm) — release the previous claim first.
                    insertClaimLog(ctx, tenant, subspace, t.name(), row.getId(),
                            row.getClaimId(), row.getClaimant(), TRANSITION_EXPIRE, now);
                    attempts = attempts + 1;
                    if (attempts >= maxAttempts) {
                        ctx.update(TUPLES)
                                .set(TUPLES.CLAIM_STATE, CLAIM_STATE_DEAD)
                                .set(TUPLES.CLAIMANT, (String) null)
                                .set(TUPLES.CLAIM_ID, (String) null)
                                .set(TUPLES.LEASE_UNTIL, (OffsetDateTime) null)
                                .set(TUPLES.ATTEMPTS, attempts)
                                .where(TUPLES.ID.eq(row.getId()))
                                .execute();
                        insertClaimLog(ctx, tenant, subspace, t.name(), row.getId(),
                                null, null, TRANSITION_DEAD, now);
                        continue; // bounded re-run: NX_TUPLE_CLAIM_PASSES
                    }
                    // otherwise: claim THIS row now, below, with the incremented attempts
                }

                String newClaimId = UUID.randomUUID().toString();
                OffsetDateTime leaseUntil = now.plusSeconds(leaseSeconds);
                if (leaseUntil.isAfter(row.getExpiresAt())) {
                    leaseUntil = row.getExpiresAt(); // clamped: a claim never outlives its tuple
                }
                ctx.update(TUPLES)
                        .set(TUPLES.CLAIM_STATE, CLAIM_STATE_CLAIMED)
                        .set(TUPLES.CLAIMANT, claimant)
                        .set(TUPLES.CLAIM_ID, newClaimId)
                        .set(TUPLES.LEASE_UNTIL, leaseUntil)
                        .set(TUPLES.ATTEMPTS, attempts)
                        .where(TUPLES.ID.eq(row.getId()))
                        .execute();
                insertClaimLog(ctx, tenant, subspace, t.name(), row.getId(),
                        newClaimId, claimant, TRANSITION_CLAIM, now);

                TupleRow claimed = new TupleRow(row.getId(), subspace, t.name(),
                        fromJsonb(row.getKeys()), fromJsonb(row.getDims()), row.getBody(),
                        CLAIM_STATE_CLAIMED, claimant, newClaimId, leaseUntil, attempts,
                        null, null, row.getExpiresAt(), row.getCreatedAt());
                return Optional.of(new ClaimedTuple(claimed, newClaimId));
            }
            return Optional.empty(); // NX_TUPLE_CLAIM_PASSES exhausted: the probe result
        });
    }

    private static Condition matchCondition(String tenant, String subspace,
                                             Map<String, String> pattern, List<String> requiredKeys) {
        Condition cond = TUPLES.TENANT_ID.eq(tenant).and(TUPLES.SUBSPACE.eq(subspace));
        for (String k : requiredKeys) {
            cond = cond.and(DSL.jsonbGetAttributeAsText(TUPLES.KEYS, k).eq(pattern.get(k)));
        }
        return cond;
    }

    // ── ack / nack ───────────────────────────────────────────────────────────

    /** {@code ack(claim_id, claimant)}. */
    public void ack(String tenant, String claimId, String claimant) {
        tenantScope.withTenant(tenant, ctx -> {
            TuplesRecord row = liveClaimRow(ctx, tenant, claimId);
            if (row == null) {
                throw new ClaimNotFoundException(claimId);
            }
            if (!row.getClaimant().equals(claimant)) {
                throw new ClaimOwnershipException(claimId, claimant);
            }
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            ctx.update(TUPLES)
                    .set(TUPLES.CONSUMED_AT, now)
                    .set(TUPLES.CONSUMED_BY, claimant)
                    .where(TUPLES.ID.eq(row.getId()))
                    .execute();
            insertClaimLog(ctx, tenant, row.getSubspace(), row.getTemplate(), row.getId(),
                    claimId, claimant, TRANSITION_ACK, now);
            return null;
        });
    }

    /** {@code nack(claim_id, claimant)} — releases the claim; counts an attempt. */
    public void nack(String tenant, String claimId, String claimant) {
        tenantScope.withTenant(tenant, ctx -> {
            TuplesRecord row = liveClaimRow(ctx, tenant, claimId);
            if (row == null) {
                throw new ClaimNotFoundException(claimId);
            }
            if (!row.getClaimant().equals(claimant)) {
                throw new ClaimOwnershipException(claimId, claimant);
            }
            TemplateSchema t = resolveOrThrow(row.getSubspace());
            long maxAttempts = t.take().maxAttempts() == null ? Long.MAX_VALUE : t.take().maxAttempts();
            int attempts = row.getAttempts() + 1;
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);

            releaseOrDeadLetter(ctx, tenant, row.getSubspace(), row.getTemplate(), row.getId(),
                    claimId, claimant, TRANSITION_NACK, now, attempts, maxAttempts);
            return null;
        });
    }

    /**
     * Shared release-or-dead-letter core (RDR-205 Phase 1 Step 5, bead nexus-em75s.5):
     * {@link #nack} and the sweep's {@link #releaseLapsedClaimsBatch} release arm both
     * release a live claim back to available, dead-lettering it instead once {@code
     * attempts} reaches the template's {@code max_attempts} — the SAME rule, so it lives
     * once here rather than as two copies that can drift (Quality Criterion: "the release
     * arm and TupleRepository's dead-letter rule cannot drift"). {@code releaseTransition}
     * is the FIRST log row's transition name ({@code nack} for an explicit client call,
     * {@code expire} for the sweep finding a lapsed lease nobody re-took); the SECOND row,
     * written only on dead-letter, is always {@code dead} — matching {@link #claimOnce}'s
     * own dead-letter branch exactly.
     *
     * @return true iff this row was dead-lettered (attempts reached max_attempts)
     */
    private boolean releaseOrDeadLetter(DSLContext ctx, String tenant, String subspace, String template,
                                          byte[] tupleId, String claimId, String claimant,
                                          String releaseTransition, OffsetDateTime now,
                                          int attempts, long maxAttempts) {
        insertClaimLog(ctx, tenant, subspace, template, tupleId, claimId, claimant, releaseTransition, now);
        if (attempts >= maxAttempts) {
            ctx.update(TUPLES)
                    .set(TUPLES.CLAIM_STATE, CLAIM_STATE_DEAD)
                    .set(TUPLES.CLAIMANT, (String) null)
                    .set(TUPLES.CLAIM_ID, (String) null)
                    .set(TUPLES.LEASE_UNTIL, (OffsetDateTime) null)
                    .set(TUPLES.ATTEMPTS, attempts)
                    .where(TUPLES.ID.eq(tupleId))
                    .execute();
            insertClaimLog(ctx, tenant, subspace, template, tupleId, null, null, TRANSITION_DEAD, now);
            return true;
        }
        ctx.update(TUPLES)
                .set(TUPLES.CLAIM_STATE, (String) null)
                .set(TUPLES.CLAIMANT, (String) null)
                .set(TUPLES.CLAIM_ID, (String) null)
                .set(TUPLES.LEASE_UNTIL, (OffsetDateTime) null)
                .set(TUPLES.ATTEMPTS, attempts)
                .where(TUPLES.ID.eq(tupleId))
                .execute();
        return false;
    }

    // ── sweep (RDR-205 Phase 1 Step 5, bead nexus-em75s.5) ──────────────────────

    /**
     * One BATCH, one transaction, of the scheduled sweep's release arm: selects up
     * to {@code batchSize} rows whose lease has lapsed — {@code claimed}, unconsumed,
     * {@code lease_until < now()}, oldest-lapsed first — and releases each with an
     * {@code expire} log row, dead-lettering at the row's template {@code max_attempts}
     * via the SAME {@link #releaseOrDeadLetter} core {@link #nack} uses.
     *
     * <p>A row whose subspace no longer resolves against the live registry (its
     * template was removed or renamed since the row was written) is released with
     * an unbounded {@code max_attempts} rather than skipped — an unreachable claim
     * must not be left claimed forever on a schema drift the row itself cannot see —
     * and logged once per occurrence so the drift is visible.
     *
     * <p>{@code statementTimeout} is applied to the batch's OWN selecting enumeration
     * as well as its writes (NexusService's per-task statement bound, passed to every
     * statement the sweep issues — bounding only the writes would leave the
     * enumeration itself unbounded).
     *
     * @return {@code scanned} always equals {@code released + deadLettered}
     */
    public ReleaseBatchResult releaseLapsedClaimsBatch(String tenant, int batchSize, Duration statementTimeout) {
        return tenantScope.withTenant(tenant, ctx -> {
            SweepBounds.applyStatementTimeout(ctx, statementTimeout);
            var rows = ctx.selectFrom(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(tenant)
                            .and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_CLAIMED))
                            .and(TUPLES.CONSUMED_AT.isNull())
                            .and(TUPLES.LEASE_UNTIL.lt(DSL.currentOffsetDateTime())))
                    .orderBy(TUPLES.LEASE_UNTIL.asc(), TUPLES.ID.asc())
                    .limit(batchSize)
                    .forNoKeyUpdate()
                    .skipLocked()
                    .fetch();
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            int released = 0;
            int deadLettered = 0;
            for (TuplesRecord row : rows) {
                TemplateSchema t = registry.resolve(row.getSubspace());
                if (t == null) {
                    log.warn("event=tuple_sweep_release_unknown_subspace tenant={} subspace={} tuple_id={}",
                            tenant, row.getSubspace(), HexFormat.of().formatHex(row.getId()));
                }
                long maxAttempts = (t == null || t.take().maxAttempts() == null)
                        ? Long.MAX_VALUE : t.take().maxAttempts();
                int attempts = row.getAttempts() + 1;
                boolean dead = releaseOrDeadLetter(ctx, tenant, row.getSubspace(), row.getTemplate(), row.getId(),
                        row.getClaimId(), row.getClaimant(), TRANSITION_EXPIRE, now,
                        attempts, maxAttempts);
                if (dead) {
                    deadLettered++;
                } else {
                    released++;
                }
            }
            return new ReleaseBatchResult(rows.size(), released, deadLettered);
        });
    }

    /**
     * One BATCH, one transaction, of the scheduled sweep's purge arm: deletes up to
     * {@code batchSize} rows whose {@code expires_at} has passed — covering BOTH a
     * never-claimed expired tuple and a consumed tuple past its retention ceiling in
     * ONE predicate, since {@code expires_at} is already the retention-clamped
     * ceiling {@link #out} writes (see its refire-clamp comment: a refire never
     * moves {@code expires_at} past {@code created_at + retention_seconds}), never
     * touched by {@link #ack}. {@code nexus.tuple_claim_log.tuple_id} is {@code ON
     * DELETE SET NULL} (tuples-001-2's FK) — a purged tuple's log rows survive with
     * a null {@code tuple_id}, never deleted here; the log's own retention is the
     * separate {@link #purgeOldClaimLogBatch} arm.
     *
     * @return {@code examined} is the candidate SELECT's own row count (RDR-205
     *         Phase 1 follow-on, bead nexus-em75s.38), independent of {@code
     *         purged}, the delete's affected-row count
     */
    public PurgeBatchResult purgeExpiredTuplesBatch(String tenant, int batchSize, Duration statementTimeout) {
        return tenantScope.withTenant(tenant, ctx -> {
            SweepBounds.applyStatementTimeout(ctx, statementTimeout);
            List<byte[]> ids = ctx.select(TUPLES.ID)
                    .from(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(tenant).and(TUPLES.EXPIRES_AT.le(DSL.currentOffsetDateTime())))
                    .orderBy(TUPLES.CREATED_AT.asc(), TUPLES.ID.asc())
                    .limit(batchSize)
                    .forUpdate()
                    .skipLocked()
                    .fetch(TUPLES.ID);
            if (ids.isEmpty()) {
                return new PurgeBatchResult(0, 0);
            }
            int purged = ctx.deleteFrom(TUPLES).where(TUPLES.ID.in(ids)).execute();
            return new PurgeBatchResult(ids.size(), purged);
        });
    }

    /**
     * One BATCH, one transaction, of the scheduled sweep's log-retention arm:
     * deletes up to {@code batchSize} {@code nexus.tuple_claim_log} rows whose OWN
     * {@code expires_at} has passed. {@link #insertClaimLog} stamps that column at
     * write time as {@code at + }{@link TemplateRegistry#claimLogTtlSeconds()} — the
     * SAME value the registry's own boot check validated against every template's
     * {@code retention_seconds} (never a second, independently-parsed copy of {@code
     * NX_TUPLE_CLAIM_LOG_TTL_DAYS}) — so this arm reads the stored column directly
     * rather than recomputing the cutoff from {@code at} a second time (RDR-205 P1
     * follow-on, nexus-em75s.37, review M6 / critique S2): the two computations can
     * only drift if this arm keeps its own copy of the TTL math.
     *
     * @return {@code examined} is the candidate SELECT's own row count (RDR-205
     *         Phase 1 follow-on, bead nexus-em75s.38), independent of {@code
     *         purged}, the delete's affected-row count
     */
    public PurgeBatchResult purgeOldClaimLogBatch(String tenant, int batchSize, Duration statementTimeout) {
        return tenantScope.withTenant(tenant, ctx -> {
            SweepBounds.applyStatementTimeout(ctx, statementTimeout);
            List<Long> ids = ctx.select(TUPLE_CLAIM_LOG.LOG_ID)
                    .from(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant)
                            .and(TUPLE_CLAIM_LOG.EXPIRES_AT.lt(DSL.currentOffsetDateTime())))
                    .orderBy(TUPLE_CLAIM_LOG.LOG_ID.asc())
                    .limit(batchSize)
                    .forUpdate()
                    .skipLocked()
                    .fetch(TUPLE_CLAIM_LOG.LOG_ID);
            if (ids.isEmpty()) {
                return new PurgeBatchResult(0, 0);
            }
            int purged = ctx.deleteFrom(TUPLE_CLAIM_LOG).where(TUPLE_CLAIM_LOG.LOG_ID.in(ids)).execute();
            return new PurgeBatchResult(ids.size(), purged);
        });
    }

    /**
     * RDR-205 Phase 1 review (nexus-em75s.7, ship-blocker): LEASE_UNTIL must be part
     * of "live" here too — without it, {@code ack}/{@code nack} on a claim_id whose
     * lease already lapsed (but the sweep or a retake has not yet released it) would
     * succeed against a claim that is no longer actually held, instead of raising
     * {@link ClaimNotFoundException} the way an already-released or already-consumed
     * claim_id does.
     */
    private static TuplesRecord liveClaimRow(DSLContext ctx, String tenant, String claimId) {
        return ctx.selectFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(tenant)
                        .and(TUPLES.CLAIM_ID.eq(claimId))
                        .and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_CLAIMED))
                        .and(TUPLES.CONSUMED_AT.isNull())
                        .and(TUPLES.LEASE_UNTIL.gt(DSL.currentOffsetDateTime())))
                .fetchOne();
    }

    /**
     * RDR-205 P1 follow-on (nexus-em75s.37, review M6 / critique S2): {@code
     * tuple_claim_log.expires_at} is the LOG ROW's own retention deadline (RDR
     * §Technical Design line ~602: {@code at + NX_TUPLE_CLAIM_LOG_TTL_DAYS}), not the
     * tuple's expiry — a claim log row for a short-lived tuple must still survive the
     * full audit retention window. Computed here, once, from {@link
     * TemplateRegistry#claimLogTtlSeconds()} rather than accepted as a caller-supplied
     * parameter, so no call site can (again) pass the tuple's own {@code expires_at}
     * by mistake.
     */
    private void insertClaimLog(DSLContext ctx, String tenant, String subspace, String template,
                                 byte[] tupleId, String claimId, String claimant,
                                 String transition, OffsetDateTime at) {
        OffsetDateTime logExpiresAt = at.plusSeconds(registry.claimLogTtlSeconds());
        ctx.insertInto(TUPLE_CLAIM_LOG,
                        TUPLE_CLAIM_LOG.TENANT_ID, TUPLE_CLAIM_LOG.SUBSPACE, TUPLE_CLAIM_LOG.TEMPLATE,
                        TUPLE_CLAIM_LOG.TUPLE_ID, TUPLE_CLAIM_LOG.CLAIM_ID, TUPLE_CLAIM_LOG.CLAIMANT,
                        TUPLE_CLAIM_LOG.TRANSITION, TUPLE_CLAIM_LOG.AT, TUPLE_CLAIM_LOG.EXPIRES_AT)
                .values(tenant, subspace, template, tupleId, claimId, claimant, transition, at, logExpiresAt)
                .execute();
    }

    // ── registry ─────────────────────────────────────────────────────────────

    /** {@code registry() -> {digest, templates}}. */
    public TemplateRegistry.Snapshot registry() {
        return registry.registry();
    }

    // ── subspace_list / subspace_stats ──────────────────────────────────────

    /** {@code subspace_stats(subspace) -> {total, available, claimed, dead, consumed, expired_unpurged}}. */
    public SubspaceCensus subspaceStats(String tenant, String subspace) {
        return tenantScope.withTenant(tenant, ctx -> computeCensus(ctx, tenant, subspace));
    }

    /** {@code subspace_list(prefix) -> [{subspace, total, available, ..., oldest_created_at, newest_created_at}]}. */
    public List<SubspaceCensus> subspaceList(String tenant, String prefix) {
        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = TUPLES.TENANT_ID.eq(tenant);
            if (prefix != null && !prefix.isBlank()) {
                cond = cond.and(TUPLES.SUBSPACE.startsWith(prefix));
            }
            List<String> subspaces = ctx.selectDistinct(TUPLES.SUBSPACE)
                    .from(TUPLES)
                    .where(cond)
                    .fetch(TUPLES.SUBSPACE);
            List<SubspaceCensus> out = new ArrayList<>();
            for (String s : subspaces) {
                out.add(computeCensus(ctx, tenant, s));
            }
            return out;
        });
    }

    /**
     * total counts LIVE rows only (available+claimed+dead); the two timestamps span
     * ALL rows including expired/consumed (the census needs the newest write, not the
     * newest live row) — RDR-205 §Technical Design "Operations".
     */
    private static SubspaceCensus computeCensus(DSLContext ctx, String tenant, String subspace) {
        Field<OffsetDateTime> now = DSL.currentOffsetDateTime();
        Condition live = TUPLES.CONSUMED_AT.isNull().and(TUPLES.EXPIRES_AT.gt(now));

        var rec = ctx.select(
                        DSL.count().filterWhere(live.and(TUPLES.CLAIM_STATE.isNull())).cast(Long.class),
                        DSL.count().filterWhere(live.and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_CLAIMED))).cast(Long.class),
                        DSL.count().filterWhere(live.and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_DEAD))).cast(Long.class),
                        DSL.count().filterWhere(TUPLES.CONSUMED_AT.isNotNull()).cast(Long.class),
                        DSL.count().filterWhere(TUPLES.CONSUMED_AT.isNull().and(TUPLES.EXPIRES_AT.le(now)))
                                .cast(Long.class),
                        DSL.min(TUPLES.CREATED_AT),
                        DSL.max(TUPLES.CREATED_AT))
                .from(TUPLES)
                .where(TUPLES.TENANT_ID.eq(tenant).and(TUPLES.SUBSPACE.eq(subspace)))
                .fetchOne();

        long available = rec.value1();
        long claimed = rec.value2();
        long dead = rec.value3();
        long consumed = rec.value4();
        long expiredUnpurged = rec.value5();
        OffsetDateTime oldest = rec.value6();
        OffsetDateTime newest = rec.value7();
        return new SubspaceCensus(subspace, available + claimed + dead, available, claimed, dead,
                consumed, expiredUnpurged, oldest, newest);
    }

    // ── shared helpers ───────────────────────────────────────────────────────

    private TemplateSchema resolveOrThrow(String subspace) {
        TemplateSchema t = registry.resolve(subspace);
        if (t == null) {
            throw new UnknownSubspaceException(subspace);
        }
        return t;
    }

    private void validateTimeout(long timeoutSeconds) {
        if (timeoutSeconds < 0) {
            throw new SchemaViolationException("timeout_s", "must not be negative");
        }
        if (timeoutSeconds > timeoutCapSeconds) {
            throw new TimeoutTooLongException(timeoutSeconds, timeoutCapSeconds);
        }
    }

    private static DayToSecond interval(long seconds) {
        return DayToSecond.valueOf(Duration.ofSeconds(seconds));
    }

    private static TupleRow toRow(TuplesRecord r) {
        return new TupleRow(r.getId(), r.getSubspace(), r.getTemplate(),
                fromJsonb(r.getKeys()), fromJsonb(r.getDims()), r.getBody(),
                r.getClaimState(), r.getClaimant(), r.getClaimId(),
                r.getLeaseUntil(), r.getAttempts(),
                r.getConsumedAt(), r.getConsumedBy(),
                r.getExpiresAt(), r.getCreatedAt());
    }

    private static JSONB toJsonb(Map<String, String> map) {
        try {
            return JSONB.valueOf(MAPPER.writeValueAsString(map));
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("failed to serialize tuple keys/dims", e);
        }
    }

    private static Map<String, String> fromJsonb(JSONB jsonb) {
        if (jsonb == null) {
            return Map.of();
        }
        try {
            return MAPPER.readValue(jsonb.data(), STRING_MAP);
        } catch (IOException e) {
            throw new IllegalStateException("failed to parse tuple keys/dims", e);
        }
    }
}
