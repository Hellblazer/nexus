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
    private static final String TRANSITION_RENEW = "renew";
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
     * TEST-ONLY seam (RDR-206 Phase 1 Step 1, bead nexus-h61dl.2): invoked inside
     * every claim-MUTATING transaction — {@link #ack}, {@link #nack} and {@link
     * #renew} — between {@link #liveClaimRow}'s unlocked read and the
     * compare-and-swap {@code UPDATE}, so a test can release the row in that window
     * (lapse the lease, run the sweep's release arm) and assert the stale update
     * matches zero rows and raises {@link ClaimNotFoundException} with no claim-log
     * row. Renamed from {@code ..._ACK_NACK_...} when renew joined at Step 3
     * (nexus-h61dl.4): a seam whose name lists two of its three callers reads as a
     * guarantee that the third is not covered. Same shape and rules as {@link
     * #TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY}: a no-op by default, package-private,
     * never assigned outside test code, not a production delay mechanism.
     */
    static volatile Runnable TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };

    /**
     * TEST-ONLY (RDR-205 bead nexus-em75s.7, the wake-test mutation pins): installs a
     * hook invoked once per {@code (tenant, subspace)} group {@link TupleWaitRegistry
     * #signalAll} actually delivers a signal to, so a test can count SIGNAL-DRIVEN
     * wakes and distinguish them from the registry's own 1-second timer fallback --
     * catching a {@code signalAll} that silently widens to every group, which an
     * un-instrumented black-box test cannot tell apart from correct behaviour. The
     * wake tests pinning this live in {@code dev.nexus.service} (a different package
     * from {@link TupleWaitRegistry}'s package-private hook field), hence this public
     * cross-package installer -- the field itself stays package-private, matching
     * {@link #TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY}'s shape; only the installer
     * needs the wider (public) visibility, for the cross-package reach this field's
     * own package cannot avoid. Pass {@code null} to restore the no-op default.
     * Never call this outside test code. The installed hook now runs UNDER {@code
     * signalAll}'s own lock (nexus-em75s.40) -- see the field's javadoc.
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
     * arm — {@code scanned == released + deadLettered} for every row whose
     * compare-and-swap matched (RDR-206 Step 1; under the batch's row lock that is
     * every selected row), mirroring the RDR-204 ghost sweep's {@code GhostSweepResult}
     * shape ({@code scanned} == the sum of its dispositions).
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

    /**
     * Everything {@code out} derives and validates BEFORE any transaction opens: the
     * resolved template, the normalised keys and dims, the ttl bounds, and the JSONB and
     * interval conversions. The tuple id is NOT here, because {@code ackWithReply} cannot
     * compute a reply's id until the request it consumed hands back the nonce.
     *
     * <p>The split does real work rather than tidying (RDR-206 Phase 1 Step 2). Every
     * way a reply can be refused must leave the request STILL CLAIMED, and if validation
     * ran inside the transaction that guarantee would rest on the rollback restoring the
     * claim rather than on the ack never having started. Those look identical in an
     * end-state assertion and differ the moment someone splits the transaction or moves
     * the signal, so the refusals happen out here where they cannot consume anything.
     */
    private record PreparedOut(TemplateSchema template, String subspace,
                               Map<String, String> keys, Map<String, String> dims,
                               JSONB keysJsonb, JSONB dimsJsonb,
                               DayToSecond ttlInterval, DayToSecond retentionInterval) {
    }

    private PreparedOut prepareOut(String subspace, Map<String, String> keys,
                                   Map<String, String> dims, Long ttlSecondsOrNull,
                                   String nonce, boolean nonceDeferred) {
        TemplateSchema t = resolveOrThrow(subspace);
        Map<String, String> keysSafe = keys == null ? Map.of() : keys;
        Map<String, String> dimsSafe = dims == null ? Map.of() : dims;
        validateOutShape(t, keysSafe, dimsSafe);
        // The nonce check runs HERE, between the shape checks and the ttl checks,
        // because that is where the combined validateOut ran it before this split.
        // Moving it after the ttl checks changed which exception `out` raises when a
        // call violates both at once, and no test covered that pair (nexus-h61dl.3
        // review, found independently by both reviewers). `ackWithReply` defers it:
        // a reply's nonce is hex(request id) and does not exist until the request has
        // been consumed, so there is nothing to check at this point on that path.
        if (!nonceDeferred) {
            validateNonce(t, nonce);
        }

        if (ttlSecondsOrNull != null && ttlSecondsOrNull <= 0) {
            throw new SchemaViolationException("ttl_seconds", "must be positive");
        }
        long ttlSeconds = ttlSecondsOrNull == null ? t.retentionSeconds() : ttlSecondsOrNull;
        if (ttlSeconds > t.retentionSeconds()) {
            throw new TtlTooLongException(ttlSeconds, t.retentionSeconds(), t.name());
        }
        JSONB dimsJsonb = dimsSafe.isEmpty() ? null : toJsonb(dimsSafe);
        return new PreparedOut(t, subspace, keysSafe, dimsSafe, toJsonb(keysSafe), dimsJsonb,
                interval(ttlSeconds), interval(t.retentionSeconds()));
    }

    /**
     * The upsert and the tenant bookkeeping, against a caller's {@code ctx} so it can
     * share a transaction with {@code consumeClaim}. Takes no responsibility for
     * signalling: {@code signalAll} must run AFTER the transaction commits, so it stays
     * with the callers.
     */
    private byte[] writeOut(DSLContext ctx, String tenant, PreparedOut p, String body, byte[] id) {
        Field<OffsetDateTime> candidateExpiry = DSL.currentOffsetDateTime().add(p.ttlInterval());
        // Refire clamp (RDR-205 §Technical Design "out"): never past the ORIGINAL
        // row's created_at plus the template's retention -- TUPLES.CREATED_AT here
        // binds to the pre-existing target row, exactly as AspectRepository's
        // insertOrUpdateExtractionQueue mixes EXCLUDED.* with a plain column
        // reference for the OLD value in the same DO UPDATE clause.
        Field<OffsetDateTime> ceiling = TUPLES.CREATED_AT.add(p.retentionInterval());
        Field<JSONB> dimsField = p.dimsJsonb() == null
                ? DSL.castNull(org.jooq.impl.SQLDataType.JSONB)
                : DSL.val(p.dimsJsonb());
        ctx.insertInto(TUPLES,
                        TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE,
                        TUPLES.KEYS, TUPLES.DIMS, TUPLES.BODY,
                        TUPLES.ATTEMPTS, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                .values(DSL.val(id), DSL.val(tenant), DSL.val(p.subspace()), DSL.val(p.template().name()),
                        DSL.val(p.keysJsonb()), dimsField, DSL.val(body),
                        DSL.val(0), DSL.currentOffsetDateTime().add(p.ttlInterval()),
                        DSL.currentOffsetDateTime())
                .onConflict(TUPLES.ID)
                .doUpdate()
                // A refire touches expires_at ONLY -- never body, claim state or
                // consumed state (every other column is simply absent from this
                // DO UPDATE's .set() list, so Postgres leaves it untouched).
                .set(TUPLES.EXPIRES_AT, DSL.least(candidateExpiry, ceiling))
                .execute();
        maintainTenant(ctx, tenant);
        return id;
    }

    /** {@code out(subspace, keys, dims, body, *, nonce=None, ttl_seconds=None) -> id}. */
    public byte[] out(String tenant, String subspace, Map<String, String> keys, Map<String, String> dims,
                       String body, String nonce, Long ttlSecondsOrNull) {
        PreparedOut prepared = prepareOut(subspace, keys, dims, ttlSecondsOrNull, nonce, false);
        byte[] id = computeId(tenant, prepared.subspace(), prepared.template(),
                prepared.keys(), prepared.dims(), nonce, body);
        byte[] result = tenantScope.withTenant(tenant, ctx -> writeOut(ctx, tenant, prepared, body, id));
        // Signal AFTER the transaction lambda returns (the commit) -- never from inside it.
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

    /**
     * The shape half of {@code out}'s validation: keys, dims and their allowed values.
     * Split from the nonce check (RDR-206 Phase 1 Step 2) because {@code ackWithReply}
     * must validate a reply's shape BEFORE consuming the request, while the reply's nonce
     * does not exist until the request has been consumed and handed back its id.
     */
    private void validateOutShape(TemplateSchema t, Map<String, String> keys, Map<String, String> dims) {
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
    }

    /** The nonce half of {@code out}'s validation. See {@link #validateOutShape}. */
    private void validateNonce(TemplateSchema t, String nonce) {
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
                // RDR-205 follow-on (nexus-mvfm9): truncate to microseconds -- Postgres
                // TIMESTAMPTZ (and the JDBC driver reading it back) is microsecond-
                // precision, but the JVM clock underneath OffsetDateTime.now() can carry
                // more digits. Without truncating here, the claim RESPONSE (built from
                // this in-memory value, never re-fetched) and a later READ-BACK of the
                // same row disagree on lease_until's fractional-second precision even
                // though both name the identical instant once rounded. row.getExpiresAt()
                // in the clamp branch already came from a DB fetch, so it is already at
                // this precision; truncating it too is a no-op, not a second source of
                // truth.
                OffsetDateTime leaseUntil = clampedLeaseUntil(now, leaseSeconds, row.getExpiresAt());
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

    /**
     * Consume a claimed row inside a caller's transaction and return the row consumed.
     *
     * <p>Extracted from {@code ack} (RDR-206 Phase 1 Step 2) so {@code ackWithReply} can
     * run it and {@code writeOut} in ONE transaction. It carries the compare-and-swap
     * from nexus-h61dl.2, which is why {@code ackWithReply} could not have shipped before
     * this extraction: a reply written beside a consume that lost a race would be a reply
     * to a request someone else now holds.
     */
    private TuplesRecord consumeClaim(DSLContext ctx, String tenant, String claimId, String claimant) {
        TuplesRecord row = liveClaimRow(ctx, tenant, claimId);
        if (row == null) {
            throw new ClaimNotFoundException(claimId);
        }
        if (!row.getClaimant().equals(claimant)) {
            throw new ClaimOwnershipException(claimId, claimant);
        }
        // TEST-ONLY (nexus-h61dl.2): widens the read-to-update race window under
        // test; a no-op Runnable on every production path.
        TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY.run();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        // RDR-206 Phase 1 Step 1: compare-and-swap. liveClaimRow read without a
        // lock, so the sweep's release arm (or a re-take after a lapse) may have
        // moved the row since; matching on the claim's identity, not the id alone,
        // makes a stale ack fail ClaimNotFound instead of consuming a row this
        // claimant no longer holds. The log row is written only after a one-row
        // update.
        int updated = ctx.update(TUPLES)
                .set(TUPLES.CONSUMED_AT, now)
                .set(TUPLES.CONSUMED_BY, claimant)
                .where(liveClaimCondition(row.getId(), claimId))
                .execute();
        if (updated == 0) {
            throw new ClaimNotFoundException(claimId);
        }
        insertClaimLog(ctx, tenant, row.getSubspace(), row.getTemplate(), row.getId(),
                claimId, claimant, TRANSITION_ACK, now);
        return row;
    }

    /** {@code ack(claim_id, claimant)}. */
    public void ack(String tenant, String claimId, String claimant) {
        tenantScope.withTenant(tenant, ctx -> consumeClaim(ctx, tenant, claimId, claimant));
    }

    /**
     * A reply to write in the same transaction that consumes the request. The nonce is
     * deliberately absent: the engine sets it to {@code hex(consumed row id)} and no
     * caller, tool or flag may supply one (RDR-206, Sam's decision 2026-09-11).
     */
    public record ReplySpec(String subspace, Map<String, String> keys, Map<String, String> dims,
                            String body, Long ttlSeconds) {
    }

    /**
     * {@code ack(claim_id, claimant, reply=...)} — consume the request and write the reply
     * in ONE transaction, returning the reply's id, or null when there was no reply.
     *
     * <p>A reader therefore sees the request consumed and the reply present, or neither.
     * Ordering inside the transaction is immaterial and deliberately unpinned (RDR-206
     * amendment 1c8f109da): only atomicity is a contract.
     *
     * <p>Every way the REPLY can be refused happens before the transaction opens, so the
     * request is still claimed and no reply row exists. That is a stronger guarantee than
     * a rollback would give, because a rollback produces the same end state while
     * depending on the ack having started; see {@link #prepareOut}. The scoping word
     * carries the whole claim and was missing here for one commit: a refusal of the CLAIM
     * itself
     * (stale claim id, wrong claimant) is raised by {@code consumeClaim} INSIDE the
     * transaction, exactly as a plain {@code ack} always has, and relies on rollback like
     * any other. For a stale claim the request is not "still claimed" at all -- someone
     * else consumed it, which is why the call failed.
     *
     * <p>The before-the-transaction property is enforced by code placement and pinned
     * only in the narrow sense {@code aRefusedReplyNeverOpensTheTransaction} describes;
     * read that test's scope note before assuming the suite would catch a check that
     * migrated inside.
     *
     * <p>The reply's target template MUST be {@code id_from: keys+nonce}. A {@code keys}
     * template ignores the nonce in {@code computeId}, so two replies to the same keys
     * would collide on one id and {@code out}'s refire clamp would silently discard the
     * second one's body -- exactly the class of silent loss this RDR exists to close, so
     * it is refused rather than documented.
     */
    public byte[] ackWithReply(String tenant, String claimId, String claimant, ReplySpec reply) {
        if (reply == null) {
            ack(tenant, claimId, claimant);
            return null;
        }
        PreparedOut prepared = prepareOut(reply.subspace(), reply.keys(), reply.dims(),
                reply.ttlSeconds(), null, /* nonceDeferred */ true);
        if (prepared.template().idFrom() != TemplateSchema.IdFrom.KEYS_NONCE) {
            throw new SchemaViolationException("reply.subspace",
                    "reply target template '" + prepared.template().name() + "' is id_from="
                    + prepared.template().idFrom().wire() + "; a reply target must be "
                    + "id_from=keys+nonce, because the engine identifies a reply by the "
                    + "request it answers and a keys-only template would collapse two "
                    + "replies onto one id");
        }
        byte[] replyId = tenantScope.withTenant(tenant, ctx -> {
            TuplesRecord consumed = consumeClaim(ctx, tenant, claimId, claimant);
            String nonce = HexFormat.of().formatHex(consumed.getId());
            byte[] id = computeId(tenant, prepared.subspace(), prepared.template(),
                    prepared.keys(), prepared.dims(), nonce, reply.body());
            return writeOut(ctx, tenant, prepared, reply.body(), id);
        });
        // Signal AFTER the commit, and only for the reply's subspace: a parked reader
        // there must not be woken by a transaction that rolled back.
        waitRegistry.signalAll(tenant, reply.subspace());
        return replyId;
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
            // TEST-ONLY (nexus-h61dl.2): see ack.
            TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY.run();
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);

            ReleaseOutcome outcome = releaseOrDeadLetter(ctx, tenant, row.getSubspace(), row.getTemplate(),
                    row.getId(), claimId, claimant, TRANSITION_NACK, now, attempts, maxAttempts);
            if (outcome == ReleaseOutcome.NOT_LIVE) {
                // The compare-and-swap matched nothing: the sweep or a re-take moved the
                // row between liveClaimRow's read and this update (RDR-206 Step 1).
                throw new ClaimNotFoundException(claimId);
            }
            return null;
        });
    }

    /**
     * {@code renew(claim_id, claimant, lease_s) -> lease_until} — extend a LIVE claim's
     * lease without consuming the tuple and without spending an attempt.
     *
     * <p>NO RESURRECTION, and that is the point of the operation. {@link #liveClaimRow}
     * already requires {@code claim_state = 'claimed'}, {@code consumed_at IS NULL} and
     * {@code lease_until > now()}, so a renew arriving after the lease lapsed finds
     * nothing and fails {@link ClaimNotFoundException}, exactly as a late {@code ack}
     * does. A holder that missed its window learns it lost the claim instead of
     * extending one it no longer holds. Among the systems surveyed for RDR-206 only
     * pgmq's {@code set_vt} resurrects; JavaSpaces raises {@code UnknownLeaseException}
     * and SQS returns {@code MessageNotInflight}, which is the behaviour this matches.
     *
     * <p>A renew is NOT an attempt. {@code nack} counts one and dead-letters at the cap,
     * so if renew touched {@code ATTEMPTS} a long task would dead-letter itself by doing
     * precisely what this operation exists to let it do.
     *
     * <p>The lease is clamped to the tuple's own expiry by {@link #clampedLeaseUntil},
     * the same helper the claim statement uses. A lease longer than the template's
     * {@code max_lease_seconds} is REFUSED rather than clamped: renewal changes who
     * decides when work is long, not the cap.
     *
     * @return the new {@code lease_until}, at the precision the row stores.
     */
    public OffsetDateTime renew(String tenant, String claimId, String claimant, long leaseSeconds) {
        // Refused before the transaction opens: a non-positive lease needs neither the
        // row nor the template to reject, so there is nothing to roll back. Same
        // placement rule Step 2 settled for reply refusals.
        if (leaseSeconds <= 0) {
            throw new SchemaViolationException("lease_s", "must be positive");
        }
        return tenantScope.withTenant(tenant, ctx -> {
            TuplesRecord row = liveClaimRow(ctx, tenant, claimId);
            if (row == null) {
                throw new ClaimNotFoundException(claimId);
            }
            if (!row.getClaimant().equals(claimant)) {
                throw new ClaimOwnershipException(claimId, claimant);
            }
            // Re-resolved from the ROW's subspace, as nack does: the caller names a
            // claim, not a subspace, so the cap has to come from the row.
            TemplateSchema t = resolveOrThrow(row.getSubspace());
            Long maxLease = t.take().maxLeaseSeconds();
            if (maxLease != null && leaseSeconds > maxLease) {
                throw new LeaseTooLongException(leaseSeconds, maxLease, t.name());
            }
            // TEST-ONLY (nexus-h61dl.2): see ack.
            TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY.run();
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
            OffsetDateTime candidate = now.plusSeconds(leaseSeconds)
                    .truncatedTo(java.time.temporal.ChronoUnit.MICROS);

            // RDR-206 Phase 1 Step 1's compare-and-swap: liveClaimRow read without a
            // lock, so the sweep's release arm or a re-take may have moved the row.
            // ATTEMPTS is deliberately absent from this .set() list.
            //
            // The ceiling is applied HERE, in SQL, against the row as it stands at
            // UPDATE time -- not in Java against the expires_at that liveClaimRow read
            // (nexus-h61dl.6 phase review). liveClaimRow does not lock, and `out`'s
            // refire path rewrites EXPIRES_AT on any refire of the same tuple with no
            // claim-state gate, so a refire landing in that window would shrink the
            // tuple's expiry under a lease computed from the older, larger value. The
            // result would be lease_until > expires_at: a claim outliving its tuple,
            // which is the invariant RDR-205 states and which this operation's own
            // mitigation for "a holder renews forever" depends on. It is not a
            // bookkeeping slip either, because purgeExpiredTuplesBatch deletes purely on
            // expires_at <= now() with no claim-state filter, so the row would be hard
            // deleted under a holder that had just been told its lease was extended.
            //
            // TUPLES.EXPIRES_AT binds to the live row exactly as TUPLES.CREATED_AT does
            // in writeOut's own refire clamp, so the read-to-update window closes
            // without a lock and without turning a benign refire into a failed renew.
            // claimOnce keeps the Java-side helper because it reads under
            // forNoKeyUpdate + skipLocked, where a concurrent refire blocks instead of
            // racing; renew is the first caller to want this clamp against an UNLOCKED
            // read, which is why the two express one rule in two places. See
            // clampedLeaseUntil.
            var stored = ctx.update(TUPLES)
                    .set(TUPLES.LEASE_UNTIL, DSL.least(DSL.val(candidate), TUPLES.EXPIRES_AT))
                    .where(liveClaimCondition(row.getId(), claimId))
                    .returningResult(TUPLES.LEASE_UNTIL)
                    .fetchOne();
            if (stored == null) {
                throw new ClaimNotFoundException(claimId);
            }
            // Returned from the row rather than from the candidate: after a SQL-side
            // clamp the caller must be told what was actually stored, and this is also
            // what makes the microsecond-precision pin an assertion about the database
            // rather than about a value this method never wrote.
            OffsetDateTime leaseUntil = stored.value1();
            insertClaimLog(ctx, tenant, row.getSubspace(), row.getTemplate(), row.getId(),
                    claimId, claimant, TRANSITION_RENEW, now);
            return leaseUntil;
        });
    }

    /** Disposition of one {@link #releaseOrDeadLetter} call. */
    enum ReleaseOutcome {
        /** Released back to available; one {@code releaseTransition} log row. */
        RELEASED,
        /** Dead-lettered at {@code max_attempts}; {@code releaseTransition} then {@code dead} log rows. */
        DEAD_LETTERED,
        /**
         * The compare-and-swap matched zero rows: the row is no longer claimed under
         * this {@code claimId} (RDR-206 Phase 1 Step 1). Nothing written, no log row.
         */
        NOT_LIVE
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
     * <p>RDR-206 Phase 1 Step 1 (bead nexus-h61dl.2): the update is a compare-and-swap
     * on the claim's identity ({@link #liveClaimCondition}), its affected-row count is
     * checked, and the log rows are written only after a one-row update. On zero rows
     * the outcome is {@link ReleaseOutcome#NOT_LIVE} and nothing is written; the caller
     * decides what that means ({@link #nack} raises {@link ClaimNotFoundException}, the
     * sweep logs and continues, because it holds the row lock and one exception would
     * abort its whole batch).
     */
    private ReleaseOutcome releaseOrDeadLetter(DSLContext ctx, String tenant, String subspace, String template,
                                               byte[] tupleId, String claimId, String claimant,
                                               String releaseTransition, OffsetDateTime now,
                                               int attempts, long maxAttempts) {
        boolean dead = attempts >= maxAttempts;
        int updated = ctx.update(TUPLES)
                .set(TUPLES.CLAIM_STATE, dead ? CLAIM_STATE_DEAD : null)
                .set(TUPLES.CLAIMANT, (String) null)
                .set(TUPLES.CLAIM_ID, (String) null)
                .set(TUPLES.LEASE_UNTIL, (OffsetDateTime) null)
                .set(TUPLES.ATTEMPTS, attempts)
                .where(liveClaimCondition(tupleId, claimId))
                .execute();
        if (updated == 0) {
            return ReleaseOutcome.NOT_LIVE;
        }
        insertClaimLog(ctx, tenant, subspace, template, tupleId, claimId, claimant, releaseTransition, now);
        if (dead) {
            insertClaimLog(ctx, tenant, subspace, template, tupleId, null, null, TRANSITION_DEAD, now);
            return ReleaseOutcome.DEAD_LETTERED;
        }
        return ReleaseOutcome.RELEASED;
    }

    /**
     * The compare-and-swap predicate every update of a live claim uses (RDR-206 Phase 1
     * Step 1): the row must still be claimed under exactly this {@code claimId} and not
     * consumed. A row the sweep released, another claimant re-took, or an earlier ack
     * consumed fails to match, so the update touches zero rows instead of writing over
     * state this caller no longer owns.
     */
    /**
     * The deadline a lease may run to: {@code now + leaseSeconds}, never past the
     * tuple's own {@code expiresAt}, at microsecond precision.
     *
     * <p>Used by the claim statement, which reads its row under
     * {@code forNoKeyUpdate} + {@code skipLocked}: the lock holds {@code expiresAt}
     * still between that read and the update, so computing the clamp in Java here is
     * safe. {@link #renew} applies the SAME RULE but in SQL, against the live row at
     * UPDATE time, because {@code liveClaimRow} does NOT lock and a concurrent refire
     * could otherwise shrink the expiry under a lease computed from the stale value
     * (nexus-h61dl.6 phase review). One rule, two expressions, and the reason they
     * differ is the locking, not an oversight -- change one and change the other. The
     * rule: never hand out a lease running past a row the sweep is entitled to purge,
     * which is RDR-205's "a claim never outlives its tuple".
     *
     * <p>Truncation is not cosmetic (RDR-205 follow-on, nexus-mvfm9): Postgres
     * TIMESTAMPTZ is microsecond-precision, while the JVM clock under
     * {@code OffsetDateTime.now()} can carry more digits. Without truncating, a value
     * returned from memory and the same row read back disagree on the fractional second
     * though both name one instant. {@code expiresAt} already came from the database
     * and is at that precision, so truncating it again is a no-op rather than a second
     * source of truth.
     *
     * <p>PRECONDITION, and it is the caller's: {@code leaseSeconds} must already have
     * been checked against the template's {@code max_lease_seconds}. This method does
     * NOT enforce the cap, and its postcondition holds only for a caller that did.
     * The cap is deliberately not a third term of the minimum here, because clamping to
     * it would hand a caller who asked for too long a shorter lease instead of the
     * {@link LeaseTooLongException} it has coming; the error is the point. Both callers
     * today reject first, but that is a fact about them rather than a guarantee of this
     * helper, so a third caller that skipped the check would exceed the cap with nothing
     * to catch it (nexus-h61dl.4 review, substantive-critic significant 2).
     */
    private static OffsetDateTime clampedLeaseUntil(OffsetDateTime now, long leaseSeconds,
                                                    OffsetDateTime expiresAt) {
        OffsetDateTime leaseUntil = now.plusSeconds(leaseSeconds)
                .truncatedTo(java.time.temporal.ChronoUnit.MICROS);
        if (leaseUntil.isAfter(expiresAt)) {
            // Clamped: a claim never outlives its tuple.
            return expiresAt.truncatedTo(java.time.temporal.ChronoUnit.MICROS);
        }
        return leaseUntil;
    }

    private static Condition liveClaimCondition(byte[] tupleId, String claimId) {
        return TUPLES.ID.eq(tupleId)
                .and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_CLAIMED))
                .and(TUPLES.CLAIM_ID.eq(claimId))
                .and(TUPLES.CONSUMED_AT.isNull());
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
     * @return {@code scanned} equals {@code released + deadLettered} for every row the
     *         compare-and-swap matched, which under the batch's row lock is every row it
     *         selected; a row it did not match is logged and counted in neither
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
                ReleaseOutcome outcome = releaseOrDeadLetter(ctx, tenant, row.getSubspace(), row.getTemplate(),
                        row.getId(), row.getClaimId(), row.getClaimant(), TRANSITION_EXPIRE, now,
                        attempts, maxAttempts);
                switch (outcome) {
                    case DEAD_LETTERED -> deadLettered++;
                    case RELEASED -> released++;
                    case NOT_LIVE -> {
                        // Defence in depth (RDR-206 Phase 1 Step 1): this row was selected
                        // FOR NO KEY UPDATE above, so no other transaction can have moved
                        // it and the compare-and-swap always matches today. If it ever
                        // does not, log and continue, exactly as the unresolved-subspace
                        // case above does: a raise here would abort the whole batch
                        // transaction and every row already released in it.
                        log.warn("event=tuple_sweep_release_not_live tenant={} subspace={} tuple_id={} claim_id={}",
                                tenant, row.getSubspace(), HexFormat.of().formatHex(row.getId()), row.getClaimId());
                    }
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

    /**
     * {@code subspace_stats(subspace) -> {total, available, claimed, dead, consumed, expired_unpurged}}.
     *
     * <p>RDR-205 follow-on (nexus-mvfm9): resolves the subspace against the
     * registry FIRST — before this fix an unknown subspace silently answered
     * a zero census (no rows match a subspace nothing ever wrote to) instead
     * of the same {@code UnknownSubspaceException} every other operation
     * raises. {@link #subspaceList}, which enumerates subspaces that
     * genuinely hold rows, deliberately keeps its own unchecked call to
     * {@link #computeCensus} — those subspace names come from live data,
     * not caller input, and may legitimately outlive a template that was
     * since removed from the registry.
     */
    public SubspaceCensus subspaceStats(String tenant, String subspace) {
        resolveOrThrow(subspace);
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
