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
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.Savepoint;
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
import java.util.concurrent.Callable;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_DELIVERIES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;

/**
 * RDR-205 Phase 1 Step 4 (bead nexus-em75s.4): the Linda tuple space's jOOQ
 * repository — {@code out}, {@code rd}/{@code rdp}, {@code in}/{@code inp},
 * {@code ack}/{@code nack}, {@code registry}, {@code subspace_list}/{@code
 * subspace_stats}. RDR-206 added {@code renew}; RDR-211 Phase 1 Step 1 (bead
 * nexus-rplay.2) added {@code release}, a hand-back that is not a failure. See
 * {@code docs/rdr/rdr-205-linda-tuple-space-over-postgres.md} §Technical Design
 * — this class, not the RDR's illustrative jOOQ block, is the authority on the
 * claim statement's exact shape.
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

    /** nexus-xapt8 (a scalability research pass over this design): request-path ceiling for
     *  {@link #subspaceListPage}'s own statement, mirroring {@link SweepBounds}'
     *  is_local=true pattern (reverts at transaction end, never leaks onto the
     *  pooled connection). Deliberately generous like {@link SweepBounds
     *  #STATEMENT_TIMEOUT} -- its job is a ceiling where there was none, not a
     *  tight budget a healthy call routinely brushes. */
    public static final String SUBSPACE_LIST_TIMEOUT_SECONDS_ENV = "NX_TUPLE_SUBSPACE_LIST_TIMEOUT_SECONDS";
    public static final int DEFAULT_SUBSPACE_LIST_TIMEOUT_SECONDS = 10;

    /**
     * RDR-211 Phase 1 Step 1 (bead nexus-rplay.4), a value the RDR leaves open: the
     * RDR bounds one session's subscriptions at 32 board topics plus two mailboxes
     * (§Scale and Limits), so 34 is the natural ceiling on how many subspaces one
     * {@link #waitAny} call may name. A fixed constant, not an env-configurable
     * setting like {@link #TIMEOUT_CAP_SECONDS_ENV} and its siblings above -- the
     * RDR's own subscription bound is the reason for the number, not a per-deploy
     * tuning knob. Refused with {@link SchemaViolationException}, never silently
     * truncated.
     */
    public static final int MAX_WAIT_SUBSPACES = 34;

    private static final String CLAIM_STATE_CLAIMED = "claimed";
    private static final String CLAIM_STATE_DEAD = "dead";
    private static final String TRANSITION_CLAIM = "claim";
    private static final String TRANSITION_ACK = "ack";
    private static final String TRANSITION_NACK = "nack";
    private static final String TRANSITION_RENEW = "renew";
    private static final String TRANSITION_EXPIRE = "expire";
    private static final String TRANSITION_DEAD = "dead";
    /**
     * RDR-211 Phase 1 Step 1 (bead nexus-rplay.2): a hand-back that is NOT a
     * failure -- see {@link #release}.
     */
    private static final String TRANSITION_RELEASE = "release";

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
     * every claim-MUTATING transaction — {@link #ack}, {@link #nack}, {@link
     * #renew} and, since RDR-211 Phase 1 Step 1 (bead nexus-rplay.2), {@link
     * #release} — between {@link #liveClaimRow}'s unlocked read and the
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

    /**
     * Cross-package installer for {@link #TEST_ONLY_SUBSPACE_LIST_PRE_QUERY_HOOK}
     * (nexus-xapt8, critique finding 10) -- same reasoning as {@link
     * #setTestOnlySignalHook}: the field itself stays package-private, this
     * installer is public so a test outside {@code dev.nexus.service.db}
     * (e.g. {@code TupleRepositoryTest}, package {@code dev.nexus.service})
     * can still install it. Pass {@code null} to restore the no-op default.
     * Never call this outside test code.
     */
    public static void setTestOnlySubspaceListPreQueryHook(
            java.util.function.Consumer<org.jooq.DSLContext> hookOrNull) {
        TEST_ONLY_SUBSPACE_LIST_PRE_QUERY_HOOK = hookOrNull == null ? ctx -> { } : hookOrNull;
    }

    private final TenantScope tenantScope;
    private final TemplateRegistry registry;
    private final TupleWaitRegistry waitRegistry;
    private final int readMax;
    private final int claimPasses;
    private final int timeoutCapSeconds;
    private final int subspaceListTimeoutSeconds;

    public TupleRepository(TenantScope tenantScope, TemplateRegistry registry) {
        this(tenantScope, registry, DEFAULT_READ_MAX, DEFAULT_CLAIM_PASSES, DEFAULT_TIMEOUT_CAP_SECONDS,
                DEFAULT_PARK_CAP_PER_CLAIMANT, DEFAULT_PARK_CAP_GLOBAL);
    }

    public TupleRepository(TenantScope tenantScope, TemplateRegistry registry,
                            int readMax, int claimPasses, int timeoutCapSeconds,
                            int parkCapPerClaimant, int parkCapGlobal) {
        this(tenantScope, registry, readMax, claimPasses, timeoutCapSeconds,
                parkCapPerClaimant, parkCapGlobal, DEFAULT_SUBSPACE_LIST_TIMEOUT_SECONDS);
    }

    /** nexus-xapt8: the full-arity constructor, adding {@code
     *  subspaceListTimeoutSeconds} without disturbing the 7-arg overload above
     *  (which every existing test call site uses) -- an additive overload
     *  rather than a widened existing signature. */
    public TupleRepository(TenantScope tenantScope, TemplateRegistry registry,
                            int readMax, int claimPasses, int timeoutCapSeconds,
                            int parkCapPerClaimant, int parkCapGlobal, int subspaceListTimeoutSeconds) {
        this.tenantScope = tenantScope;
        this.registry = registry;
        this.readMax = readMax;
        this.claimPasses = claimPasses;
        this.timeoutCapSeconds = timeoutCapSeconds;
        this.subspaceListTimeoutSeconds = subspaceListTimeoutSeconds;
        this.waitRegistry = new TupleWaitRegistry(parkCapPerClaimant, parkCapGlobal);
    }

    /**
     * TEST-ONLY (nexus-rplay, the register/release leak fix): injects a caller-built
     * {@link TupleWaitRegistry} directly, instead of constructing one from park-cap
     * ints -- the only way a test can drive {@code rd}/{@code in}/{@code waitAny}
     * through a registry built with the injectable {@link
     * TupleWaitRegistry#TupleWaitRegistry(int, int, java.util.function.LongSupplier)}
     * clock, so a test can advance idle time deterministically and then call this
     * repository's own {@code groupCount()}-visible package-private registry to
     * assert eviction. Package-private, matching {@link TupleWaitRegistry}'s own
     * package-private visibility (a public overload could not even name the type
     * outside this package). Never call this outside test code.
     */
    TupleRepository(TenantScope tenantScope, TemplateRegistry registry,
                     int readMax, int claimPasses, int timeoutCapSeconds, int subspaceListTimeoutSeconds,
                     TupleWaitRegistry waitRegistry) {
        this.tenantScope = tenantScope;
        this.registry = registry;
        this.readMax = readMax;
        this.claimPasses = claimPasses;
        this.timeoutCapSeconds = timeoutCapSeconds;
        this.subspaceListTimeoutSeconds = subspaceListTimeoutSeconds;
        this.waitRegistry = waitRegistry;
    }

    /** TEST-ONLY (nexus-rplay): exposes this repository's own {@link
     *  TupleWaitRegistry} so a same-package test can call its package-private
     *  {@code groupCount()} without keeping a second, disconnected registry
     *  instance of its own. Never call this outside test code. */
    TupleWaitRegistry testOnlyWaitRegistry() {
        return waitRegistry;
    }

    /** Production boot call: reads every setting via {@code System.getenv} directly. */
    public static TupleRepository fromEnv(TenantScope tenantScope, TemplateRegistry registry) {
        return new TupleRepository(tenantScope, registry,
                intEnv(READ_MAX_ENV, DEFAULT_READ_MAX),
                intEnv(CLAIM_PASSES_ENV, DEFAULT_CLAIM_PASSES),
                intEnv(TIMEOUT_CAP_SECONDS_ENV, DEFAULT_TIMEOUT_CAP_SECONDS),
                intEnv(PARK_CAP_PER_CLAIMANT_ENV, DEFAULT_PARK_CAP_PER_CLAIMANT),
                intEnv(PARK_CAP_GLOBAL_ENV, DEFAULT_PARK_CAP_GLOBAL),
                intEnv(SUBSPACE_LIST_TIMEOUT_SECONDS_ENV, DEFAULT_SUBSPACE_LIST_TIMEOUT_SECONDS));
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

    // ── park stats (RDR-211 Phase 1 Step 1, bead nexus-rplay.7) ─────────────

    /**
     * {@code park_stats() -> {max_global, max_per_claimant, global_in_use,
     * refused_global, refused_claimant, per_claimant}}: read access to {@link
     * #waitRegistry}'s park-slot bookkeeping, otherwise invisible outside
     * {@link TupleWaitRegistry}'s own package-private fields. RDR-211 §Scale
     * and Limits item 1 named this gap directly -- the park cap has existed
     * since RDR-205 (bead nexus-em75s.4), but nothing reported parked or
     * refused calls before this bead.
     *
     * <p>Counters are held on THIS JVM process, not per-tenant and not
     * aggregated across a cluster -- a caller behind a load balancer sees
     * only the instance it happens to land on. {@code GET /v1/tuples/
     * park_stats} still requires the usual {@code Authorization: Bearer}
     * and {@code X-Nexus-Tenant} headers (auth is enforced ahead of every
     * {@code /v1/tuples} route, this one included), even though the numbers
     * themselves carry no tenant dimension.
     */
    public ParkStats parkStats() {
        return new ParkStats(
                waitRegistry.maxGlobal(), waitRegistry.maxPerClaimant(),
                waitRegistry.globalInUse(), waitRegistry.globalRefusedCount(),
                waitRegistry.claimantRefusedCount(), waitRegistry.perClaimantSnapshot());
    }

    /**
     * RDR-211 Phase 1 Step 1: a snapshot of {@link #parkStats}. {@code
     * perClaimant} carries only claimants CURRENTLY parked -- an entry
     * disappears the instant its count reaches zero, same as {@link
     * TupleWaitRegistry#perClaimantSnapshot}. A null-claimant park ({@code
     * rd}, and RDR-211 Phase 1 Step 1's {@code wait}) is never a key here; it
     * counts toward {@code globalInUse} only.
     */
    public record ParkStats(int maxGlobal, int maxPerClaimant, int globalInUse,
                             long refusedGlobal, long refusedClaimant,
                             Map<String, Integer> perClaimant) {
    }

    // ── records ──────────────────────────────────────────────────────────────

    /**
     * {@code announcedAt}/{@code announceCount} (bead nexus-vsipz, RDR-213 engine
     * half): additive wire fields mirroring {@code nexus.tuples.announced_at}/{@code
     * .announce_count}. Populated for every row this repository returns, not only
     * announce-mode rows -- a row nothing has ever announced simply carries {@code
     * announcedAt=null, announceCount=0}, the column defaults. See {@link WaitSpec
     * .Announce} for the mechanism that stamps them.
     */
    public record TupleRow(
            byte[] id, String subspace, String template,
            Map<String, String> keys, Map<String, String> dims, String body,
            String claimState, String claimant, String claimId,
            OffsetDateTime leaseUntil, int attempts,
            OffsetDateTime consumedAt, String consumedBy,
            OffsetDateTime expiresAt, OffsetDateTime createdAt,
            OffsetDateTime announcedAt, int announceCount) {
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

    /** {@code subspace_list}'s paged form (nexus-xapt8): {@code nextCursor} is
     *  non-null exactly when {@code items} was truncated by a caller-supplied
     *  {@code limit} -- {@code null} means every matching subspace was returned. */
    public record SubspacePage(List<SubspaceCensus> items, String nextCursor) {
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
                                   Map<String, String> dims, String body, Long ttlSecondsOrNull,
                                   String nonce, boolean nonceDeferred) {
        // Size checks first (bead nexus-r7xao, RDR-205 amendment): subspace, keys, dims,
        // nonce, body vs the global cap, body vs the template's own (possibly lower) cap
        // -- ALL of it before the existing schema validation below, which is what lets
        // TooLarge fire ahead of validateOutShape's value-echoing SchemaViolation
        // messages ("value '...' not in [...]") rather than echoing an oversized value
        // into a log line or a response body.
        checkFieldSize("subspace", subspace, TupleLimits.MAX_SUBSPACE_BYTES);
        TemplateSchema t = resolveOrThrow(subspace);
        Map<String, String> keysSafe = keys == null ? Map.of() : keys;
        Map<String, String> dimsSafe = dims == null ? Map.of() : dims;
        for (var e : keysSafe.entrySet()) {
            checkFieldSize("keys." + e.getKey(), e.getValue(), TupleLimits.MAX_FIELD_VALUE_BYTES);
        }
        for (var e : dimsSafe.entrySet()) {
            checkFieldSize("dims." + e.getKey(), e.getValue(), TupleLimits.MAX_FIELD_VALUE_BYTES);
        }
        if (!nonceDeferred) {
            checkFieldSize("nonce", nonce, TupleLimits.MAX_NONCE_BYTES);
        }
        validateBodySize(t, body);

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

    /** One tuple field's UTF-8 byte length against *limitBytes*; a {@code null} value is
     *  0 bytes and always passes. Never echoes *value* itself (bead nexus-r7xao). */
    private static void checkFieldSize(String field, String value, int limitBytes) {
        int len = TupleLimits.utf8Length(value);
        if (len > limitBytes) {
            throw new TooLargeException(field, len, limitBytes);
        }
    }

    /** {@code keys_pattern} on {@code rd}/{@code rdp}/{@code in}/{@code inp}: every
     *  supplied value against the same per-field cap {@code out}'s keys/dims use. */
    private static void checkPatternSizes(Map<String, String> pattern) {
        for (var e : pattern.entrySet()) {
            checkFieldSize("keys_pattern." + e.getKey(), e.getValue(), TupleLimits.MAX_FIELD_VALUE_BYTES);
        }
    }

    /**
     * {@code body} against the template's own {@code max_body_bytes} when it declares
     * one, else the global {@link TupleLimits#MAX_BODY_BYTES} cap. A template ceiling of
     * 0 is satisfied by both {@code null} and {@code ""} (both are 0 bytes) and refuses
     * anything else, with no special-casing needed here.
     */
    private static void validateBodySize(TemplateSchema t, String body) {
        int len = TupleLimits.utf8Length(body);
        long limit = t.maxBodyBytes() != null ? t.maxBodyBytes() : TupleLimits.MAX_BODY_BYTES;
        if (len > limit) {
            throw new TooLargeException("body", len, limit);
        }
    }

    /** {@code claim_id}/{@code claimant} on {@code ack}/{@code nack}/{@code renew}/
     *  {@code release} (and {@code ackWithReply}, which does not otherwise call
     *  {@code ack}'s own checks on its reply-carrying path). */
    private static void checkClaimIdentifiers(String claimId, String claimant) {
        checkFieldSize("claim_id", claimId, TupleLimits.MAX_CLAIM_ID_BYTES);
        checkFieldSize("claimant", claimant, TupleLimits.MAX_CLAIMANT_BYTES);
    }

    /**
     * The upsert and the tenant bookkeeping, against a caller's {@code ctx} so it can
     * share a transaction with {@code consumeClaim}. Takes no responsibility for
     * signalling: {@code signalAll} must run AFTER the transaction commits, so it stays
     * with the callers.
     *
     * <p>RDR-211 Scale and Limits item 2 ("a runaway writer"): when the template
     * declares {@code max_live_rows}, the check below runs INSIDE this same
     * transaction, under the tenant, before the insert below — so a refusal and the
     * insert it guards can never race apart. It is skipped entirely for a row whose
     * {@code id} already exists (an idempotent refire of an existing identity, {@link
     * TemplateSchema.IdFrom#KEYS}): the {@code onConflict} below only refreshes that
     * row's {@code expires_at}, adding no new row, so it cannot be what pushes a
     * subspace over its cap and must not be refused by this check ({@link
     * TemplateSchema#maxLiveRows()}'s javadoc records this decision). The live-row
     * count and the existence check are each one query, not serialized against
     * concurrent writers with a lock — same best-effort posture the rest of this
     * design uses for a capacity guard (RDR-211 names this a guard against a runaway
     * writer, not a hard exclusion primitive like the claim CAS below); a burst of
     * concurrent {@code out} calls to one subspace can overshoot the cap by the
     * width of the race, and the next call after the burst settles is refused as
     * usual.
     */
    private byte[] writeOut(DSLContext ctx, String tenant, PreparedOut p, String body, byte[] id) {
        Long maxLiveRows = p.template().maxLiveRows();
        if (maxLiveRows != null) {
            // nexus-rplay.17 (code-review-expert finding 4): every other query in
            // this file pairs TUPLES.ID/SUBSPACE conditions with an explicit
            // TENANT_ID equality as defense in depth beside RLS -- this existence
            // check was the one exception. id is already a tenant-scoped digest
            // (computeId mixes tenant into its input), so this was never reachable
            // as a cross-tenant read; the fix brings it into line with every
            // sibling query regardless.
            boolean rowAlreadyExists = ctx.fetchExists(
                    ctx.selectOne().from(TUPLES).where(TUPLES.ID.eq(id).and(TUPLES.TENANT_ID.eq(tenant))));
            if (!rowAlreadyExists) {
                Condition live = TUPLES.CONSUMED_AT.isNull().and(TUPLES.EXPIRES_AT.gt(DSL.currentOffsetDateTime()));
                Integer liveCount = ctx.selectCount().from(TUPLES)
                        .where(TUPLES.TENANT_ID.eq(tenant).and(TUPLES.SUBSPACE.eq(p.subspace())).and(live))
                        .fetchOne(0, Integer.class);
                if (liveCount != null && liveCount >= maxLiveRows) {
                    throw new MaxLiveRowsExceededException(p.subspace(), maxLiveRows);
                }
            }
        }
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
        var insertStep = ctx.insertInto(TUPLES,
                        TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE,
                        TUPLES.KEYS, TUPLES.DIMS, TUPLES.BODY,
                        TUPLES.ATTEMPTS, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                .values(DSL.val(id), DSL.val(tenant), DSL.val(p.subspace()), DSL.val(p.template().name()),
                        DSL.val(p.keysJsonb()), dimsField, DSL.val(body),
                        DSL.val(0), DSL.currentOffsetDateTime().add(p.ttlInterval()),
                        DSL.currentOffsetDateTime())
                .onConflict(TUPLES.ID)
                .doUpdate();
        if (p.template().lock()) {
            // RDR-211 Phase 1 Step 1 (bead nexus-rplay.3), Approach item 5: scoped to
            // lock-flagged templates ONLY -- the `if` branch here, not a WHEN clause
            // every template's SQL shares, is what keeps a non-flagged template's
            // generated statement byte-identical to before this bead (regression-
            // pinned by outOnANonFlaggedTemplateWithAnExistingIdIsUnchanged). An `out`
            // that finds the existing row already EXPIRED resets it to available -- a
            // fresh created_at/expires_at ceiling and a cleared claim -- instead of
            // leaving it dead under a ceiling a refire can never move today (the "lock
            // expiry cliff", Scale and Limits item 5). A still-live lock row is
            // untouched beyond the ordinary refire clamp, so `out` stays the safe
            // "make sure the lock exists" idempotent no-op Approach item 3 promises.
            Condition expired = TUPLES.EXPIRES_AT.le(DSL.currentOffsetDateTime());
            insertStep
                    .set(TUPLES.CREATED_AT,
                            DSL.when(expired, DSL.currentOffsetDateTime()).otherwise(TUPLES.CREATED_AT))
                    .set(TUPLES.EXPIRES_AT,
                            DSL.when(expired, candidateExpiry).otherwise(DSL.least(candidateExpiry, ceiling)))
                    .set(TUPLES.CLAIM_STATE,
                            DSL.when(expired, DSL.val((String) null, TUPLES.CLAIM_STATE)).otherwise(TUPLES.CLAIM_STATE))
                    .set(TUPLES.CLAIMANT,
                            DSL.when(expired, DSL.val((String) null, TUPLES.CLAIMANT)).otherwise(TUPLES.CLAIMANT))
                    .set(TUPLES.CLAIM_ID,
                            DSL.when(expired, DSL.val((String) null, TUPLES.CLAIM_ID)).otherwise(TUPLES.CLAIM_ID))
                    .set(TUPLES.LEASE_UNTIL,
                            DSL.when(expired, DSL.val((OffsetDateTime) null, TUPLES.LEASE_UNTIL))
                                    .otherwise(TUPLES.LEASE_UNTIL))
                    .execute();
        } else {
            // A refire touches expires_at ONLY -- never body, claim state or
            // consumed state (every other column is simply absent from this
            // DO UPDATE's .set() list, so Postgres leaves it untouched). Unchanged
            // from before RDR-211 Phase 1 Step 1's lock flag above.
            insertStep.set(TUPLES.EXPIRES_AT, DSL.least(candidateExpiry, ceiling)).execute();
        }
        maintainTenant(ctx, tenant);
        return id;
    }

    /** {@code out(subspace, keys, dims, body, *, nonce=None, ttl_seconds=None) -> id}. */
    public byte[] out(String tenant, String subspace, Map<String, String> keys, Map<String, String> dims,
                       String body, String nonce, Long ttlSecondsOrNull) {
        PreparedOut prepared = prepareOut(subspace, keys, dims, body, ttlSecondsOrNull, nonce, false);
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

    /** {@code rd(subspace, keys_pattern=None, *, n=1, since=None, timeout_s=0) -> [Tuple]} — blocks up to {@code timeoutSeconds}.
     *  The {@link TupleWaitRegistry#register} call this makes is released ({@link
     *  TupleWaitRegistry.Waiter#release}) on EVERY exit -- an immediate hit and an
     *  exception from the first query included, not only the park-loop path -- so a
     *  subspace that never actually parks is still eligible for {@link
     *  TupleWaitRegistry#evictIdleGroups} (nexus-rplay). */
    public List<TupleRow> rd(String tenant, String subspace, Map<String, String> pattern, int n,
                              ReadCursor since, long timeoutSeconds) {
        validateTimeout(timeoutSeconds);
        if (timeoutSeconds <= 0) {
            return queryOnce(tenant, subspace, pattern, n, since);
        }
        // Registered BEFORE the first query, so a write landing between that query and
        // the first park is not lost (RDR-205 §Technical Design "Wake").
        TupleWaitRegistry.Waiter waiter = waitRegistry.register(tenant, subspace);
        try {
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
            }
        } finally {
            waiter.release();
        }
    }

    /** Back-compat overload (every {@code rd}/{@code rdp} call site): announce
     *  mode off, byte-for-byte today's behaviour. See the 6-arg overload below. */
    private List<TupleRow> queryOnce(String tenant, String subspace, Map<String, String> pattern,
                                      int n, ReadCursor since) {
        return queryOnce(tenant, subspace, pattern, n, since, null);
    }

    /**
     * {@code announce} (bead nexus-vsipz, RDR-213 engine half) is {@code null} for
     * every {@code rd}/{@code rdp} call and for a {@link WaitSpec} that does not
     * carry one -- exactly the 5-arg overload's prior behaviour, unchanged. When
     * non-null, the match and the stamp both move into {@link #queryOnceAnnounce};
     * {@code since} is ignored on that path ({@link #waitAny}'s validation pass
     * refuses a spec that sets both, so this method never has to choose between
     * them).
     */
    private List<TupleRow> queryOnce(String tenant, String subspace, Map<String, String> pattern,
                                      int n, ReadCursor since, WaitSpec.Announce announce) {
        checkFieldSize("subspace", subspace, TupleLimits.MAX_SUBSPACE_BYTES);
        // RDR-205 review (nexus-em75s.35, M4): rd/rdp must refuse an unregistered
        // subspace exactly as out() and in()/inp() (via claimOnce) do -- this was
        // the one "Once" helper that never resolved the template, so a probe/read
        // against a bogus subspace silently read back empty instead of raising.
        resolveOrThrow(subspace);
        Map<String, String> patternSafe = pattern == null ? Map.of() : pattern;
        checkPatternSizes(patternSafe);
        int limit = Math.min(n <= 0 ? 1 : n, readMax);

        return tenantScope.withTenant(tenant, ctx -> {
            Condition cond = TUPLES.TENANT_ID.eq(tenant)
                    .and(TUPLES.SUBSPACE.eq(subspace))
                    .and(TUPLES.CONSUMED_AT.isNull())
                    .and(TUPLES.EXPIRES_AT.gt(DSL.currentOffsetDateTime()));
            for (var e : patternSafe.entrySet()) {
                cond = cond.and(DSL.jsonbGetAttributeAsText(TUPLES.KEYS, e.getKey()).eq(e.getValue()));
            }
            if (announce != null && announce.perSubscriber()) {
                return queryOnceAnnounceSubscriber(ctx, cond, limit, announce, tenant, subspace);
            }
            if (announce != null) {
                return queryOnceAnnounce(ctx, cond, limit, announce);
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

    /**
     * Announce-mode branch of {@link #queryOnce} (bead nexus-vsipz): {@code
     * baseCond} (tenant, subspace, {@code consumed_at IS NULL}, {@code expires_at >
     * now()}, the caller's key pattern) is narrowed to CLAIMABLE rows -- {@code
     * claimOnce}'s own predicate, {@code claim_state IS DISTINCT FROM 'dead' AND
     * (claim_state IS NULL OR lease_until < now())} -- further narrowed to rows DUE
     * for a first or repeat announcement -- {@code announced_at IS NULL OR
     * (announced_at < now() - interval_s AND announce_count < max)} -- ordered
     * oldest first, capped at {@code limit}. The SELECT locks its candidates
     * ({@code FOR NO KEY UPDATE SKIP LOCKED}, {@link #claimOnce}'s own lock mode,
     * chosen for the identical reason: the UPDATE below touches no key column, and
     * this repository's other concurrent writers -- {@code out}'s claim-log FK
     * insert, {@code ack}/{@code nack}'s own claim mutation -- must not be forced
     * into a stronger lock) so a second concurrent announce-mode call cannot
     * double-stamp the same row; a locked-out row is skipped for THIS call (not
     * blocked on), same as {@code claimOnce}'s claim loop.
     *
     * <p>The stamp is one {@code UPDATE ... WHERE id IN (...)} against the exact
     * ids just selected, in the SAME transaction -- never a second SELECT to
     * re-read what was just written. The rows returned to the caller carry the
     * POST-stamp values ({@code announcedAt=now}, {@code announceCount=old+1}),
     * computed here rather than re-fetched, since every id's pre-stamp {@code
     * announceCount} is already in hand from the locking SELECT above.
     */
    private List<TupleRow> queryOnceAnnounce(DSLContext ctx, Condition baseCond, int limit,
                                              WaitSpec.Announce announce) {
        Condition claimable = TUPLES.CLAIM_STATE.isDistinctFrom(CLAIM_STATE_DEAD)
                .and(TUPLES.CLAIM_STATE.isNull().or(TUPLES.LEASE_UNTIL.lt(DSL.currentOffsetDateTime())));
        Condition due = TUPLES.ANNOUNCED_AT.isNull()
                .or(TUPLES.ANNOUNCED_AT.lt(DSL.currentOffsetDateTime().sub(interval(announce.intervalSeconds())))
                        .and(TUPLES.ANNOUNCE_COUNT.lt(announce.max())));
        Condition cond = baseCond.and(claimable).and(due);

        var rows = ctx.selectFrom(TUPLES)
                .where(cond)
                .orderBy(TUPLES.CREATED_AT.asc(), TUPLES.ID.asc())
                .limit(limit)
                .forNoKeyUpdate()
                .skipLocked()
                .fetch();
        if (rows.isEmpty()) {
            return List.of();
        }

        List<byte[]> ids = new ArrayList<>(rows.size());
        for (TuplesRecord r : rows) {
            ids.add(r.getId());
        }
        // Truncated to microseconds (RDR-205 follow-on nexus-mvfm9's own reasoning,
        // reused here): the value written matches Postgres TIMESTAMPTZ precision
        // exactly, so the in-memory TupleRow this method returns and a later
        // read-back of the same row agree on announced_at's fractional seconds.
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC).truncatedTo(java.time.temporal.ChronoUnit.MICROS);
        ctx.update(TUPLES)
                .set(TUPLES.ANNOUNCED_AT, now)
                .set(TUPLES.ANNOUNCE_COUNT, TUPLES.ANNOUNCE_COUNT.add(1))
                .where(TUPLES.ID.in(ids))
                .execute();

        List<TupleRow> out = new ArrayList<>();
        for (TuplesRecord r : rows) {
            out.add(toRow(r, now, r.getAnnounceCount() + 1));
        }
        return out;
    }

    /**
     * Per-subscriber announce-mode branch of {@link #queryOnce} (bead nexus-q82tk,
     * RDR-213 boards half). The predicate is {@link #queryOnceAnnounce}'s -- {@code
     * baseCond} narrowed to claimable rows that are DUE -- but the stamp that
     * decides "due" lives in {@code nexus.tuple_deliveries}, keyed by {@code
     * (tenant, subspace, subscriber, tuple_id)}, not on the {@code nexus.tuples}
     * row: a board post is read by many subscribers and never claimed, so a
     * row-level stamp would let the first subscriber's announcement silence the
     * post for every other. A row is due for {@code announce.subscriber()} when
     * it has NO delivery row for that subscriber, or its delivery row is older
     * than {@code intervalSeconds} with {@code announce_count < max} -- the same
     * arithmetic as the row-level shape, so {@code max=1} means "once per
     * subscriber, ever", which is what the client's board arm sends.
     *
     * <p>The claimable narrowing is kept for uniformity: a board row is never
     * claimed, so it is trivially true there, and a mailbox spec that named a
     * subscriber would behave exactly like the row-level shape with a
     * per-subscriber count.
     *
     * <p>Locking and ordering are {@link #queryOnceAnnounce}'s: the SELECT takes
     * {@code FOR NO KEY UPDATE SKIP LOCKED} on the tuple rows, so two waiters of
     * the SAME subscriber (a restart overlap) cannot both stamp one row; two
     * DIFFERENT subscribers whose calls overlap on one post see it one call
     * apart, since the second skips the row the first still holds and finds it
     * due again at its next call (the first's stamp is for a different subscriber
     * and does not exclude it). The re-scan orders by {@code created_at} ASC
     * every time with no client position, so a row whose {@code out} committed
     * late is returned on the next call regardless -- the defect this bead closes
     * for boards, {@code TupleAnnounceTest} proves it with a held transaction.
     *
     * <p>The stamp is one upsert per returned row in the SAME transaction:
     * {@code INSERT ... ON CONFLICT (pk) DO UPDATE SET announced_at = now,
     * announce_count = announce_count + 1 RETURNING announce_count}, so the rows
     * returned carry the POST-stamp per-subscriber values in {@code announcedAt}/
     * {@code announceCount} (the row's own columns are neither read for this
     * decision nor written by it).
     */
    private List<TupleRow> queryOnceAnnounceSubscriber(DSLContext ctx, Condition baseCond, int limit,
                                                        WaitSpec.Announce announce, String tenant,
                                                        String subspace) {
        String subscriber = announce.subscriber();
        Condition claimable = TUPLES.CLAIM_STATE.isDistinctFrom(CLAIM_STATE_DEAD)
                .and(TUPLES.CLAIM_STATE.isNull().or(TUPLES.LEASE_UNTIL.lt(DSL.currentOffsetDateTime())));
        // Due iff no delivery row for this subscriber blocks it: a row blocks
        // while it is younger than the interval, or once its count has reached
        // the cap. NOT EXISTS over the blocking shape is the whole due test.
        Condition blocked = TUPLE_DELIVERIES.TENANT_ID.eq(tenant)
                .and(TUPLE_DELIVERIES.SUBSPACE.eq(subspace))
                .and(TUPLE_DELIVERIES.SUBSCRIBER.eq(subscriber))
                .and(TUPLE_DELIVERIES.TUPLE_ID.eq(TUPLES.ID))
                .and(TUPLE_DELIVERIES.ANNOUNCED_AT.ge(DSL.currentOffsetDateTime().sub(interval(announce.intervalSeconds())))
                        .or(TUPLE_DELIVERIES.ANNOUNCE_COUNT.ge(announce.max())));
        Condition due = DSL.notExists(ctx.selectOne().from(TUPLE_DELIVERIES).where(blocked));
        Condition cond = baseCond.and(claimable).and(due);

        var rows = ctx.selectFrom(TUPLES)
                .where(cond)
                .orderBy(TUPLES.CREATED_AT.asc(), TUPLES.ID.asc())
                .limit(limit)
                .forNoKeyUpdate()
                .skipLocked()
                .fetch();
        if (rows.isEmpty()) {
            return List.of();
        }

        // Microsecond truncation: queryOnceAnnounce's own reasoning (Postgres
        // TIMESTAMPTZ precision), so the in-memory value and a later read-back
        // of the delivery row agree.
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC).truncatedTo(java.time.temporal.ChronoUnit.MICROS);
        List<TupleRow> out = new ArrayList<>(rows.size());
        for (TuplesRecord r : rows) {
            Integer count = ctx.insertInto(TUPLE_DELIVERIES,
                            TUPLE_DELIVERIES.TENANT_ID, TUPLE_DELIVERIES.SUBSPACE, TUPLE_DELIVERIES.SUBSCRIBER,
                            TUPLE_DELIVERIES.TUPLE_ID, TUPLE_DELIVERIES.ANNOUNCED_AT, TUPLE_DELIVERIES.ANNOUNCE_COUNT)
                    .values(tenant, subspace, subscriber, r.getId(), now, 1)
                    .onConflict(TUPLE_DELIVERIES.TENANT_ID, TUPLE_DELIVERIES.SUBSPACE,
                            TUPLE_DELIVERIES.SUBSCRIBER, TUPLE_DELIVERIES.TUPLE_ID)
                    .doUpdate()
                    .set(TUPLE_DELIVERIES.ANNOUNCED_AT, now)
                    .set(TUPLE_DELIVERIES.ANNOUNCE_COUNT, TUPLE_DELIVERIES.ANNOUNCE_COUNT.add(1))
                    .returning(TUPLE_DELIVERIES.ANNOUNCE_COUNT)
                    .fetchOne(TUPLE_DELIVERIES.ANNOUNCE_COUNT);
            if (count == null) {
                // RETURNING on an upsert always yields the row; a null here means the
                // statement did not run as written, which is a defect to fail loud on.
                throw new IllegalStateException("tuple_deliveries upsert returned no row for subscriber " + subscriber);
            }
            out.add(toRow(r, now, count));
        }
        return out;
    }

    // ── wait (multiplexed rd, RDR-211 Phase 1 Step 1, bead nexus-rplay.4) ──────

    /** One subspace subscription within a {@link #waitAny} call: {@code n}/{@code
     *  since} carry the same meaning and defaults as {@link #rd}'s own parameters --
     *  {@code n <= 0} clamps to 1, {@code since == null} reads from the start.
     *  {@code announce} (bead nexus-vsipz, RDR-213 engine half) is an ADDITIVE field:
     *  {@code null} (the 4-arg constructor below) is today's unchanged behaviour for
     *  every existing caller. {@code since} and {@code announce} together are refused
     *  ({@link #waitAny}'s own validation pass) -- announce mode tracks position on
     *  the ROW itself via {@code announced_at}/{@code announce_count}, never via a
     *  client-supplied cursor, so combining the two would silently do nothing with
     *  the cursor rather than fail loud if it were merely ignored. */
    public record WaitSpec(String subspace, Map<String, String> pattern, int n, ReadCursor since,
                            Announce announce) {

        /** Back-compat constructor: every call site that predates announce mode
         *  (every existing test, {@link TupleHandler}'s bare {@code since}-only
         *  path) gets {@code announce=null} -- byte-for-byte today's semantics. */
        public WaitSpec(String subspace, Map<String, String> pattern, int n, ReadCursor since) {
            this(subspace, pattern, n, since, null);
        }

        /**
         * Rate-limited engine announcement (bead nexus-vsipz, RDR-213 engine half,
         * T2 nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
         * 2026-09-17). When present on a {@link WaitSpec}, {@code
         * TupleRepository.queryOnce} restricts its match to CLAIMABLE rows ({@code
         * claimOnce}'s own predicate: unconsumed, unexpired, not dead-lettered,
         * and either never claimed or claimed with a lapsed lease) that are DUE for
         * a first or repeat announcement -- {@code announced_at IS NULL} (never
         * announced) OR ({@code announced_at} older than {@code intervalSeconds}
         * AND {@code announce_count < max}) -- and stamps {@code announced_at =
         * now()}, {@code announce_count += 1} on every row it returns, in the SAME
         * statement that selects them. A row a caller neither claims nor consumes
         * is simply re-announced at the next due tick, up to {@code max} times
         * total; past that it is never returned by announce mode again (though it
         * remains reachable via {@code in}/{@code inp}, which never consult these
         * columns).
         *
         * <p>Because the match always re-scans the FULL claimable-and-due set,
         * ordered oldest first, with no client-supplied cursor to have advanced
         * past anything, a row that commits its {@code out} LATE (RDR-213's
         * lost-wake defect: a slower transaction's {@code created_at} can sort
         * before a faster, earlier-committing sibling's) is picked up on the very
         * next call regardless -- there is no cursor position for it to land
         * behind.
         */
        public record Announce(long intervalSeconds, int max, String subscriber) {

            /** Back-compat constructor (every nexus-vsipz call site, every mailbox
             *  spec): {@code subscriber=null}, the row-level stamp on {@code
             *  nexus.tuples} itself. */
            public Announce(long intervalSeconds, int max) {
                this(intervalSeconds, max, null);
            }

            /** Bounds check (review round, bead nexus-vsipz): a compact constructor
             *  so EVERY construction path -- {@link TupleHandler#readAnnounce}, a
             *  direct Java caller, a future one -- is covered, not just the wire
             *  parser. Mirrors {@code claimOnce}'s own {@code lease_s <= 0} refusal
             *  ({@code SchemaViolationException("lease_s", "must be positive")}):
             *  a negative {@code intervalSeconds} would make {@code announced_at <
             *  now() - interval} read as {@code now() + |interval|}, defeating rate
             *  limiting outright rather than merely under-limiting it, and {@code
             *  max < 1} would make the due check's {@code announce_count < max}
             *  arm false for every row from its very first stamp (announce_count=1
             *  is never {@code < 0} or {@code < 1} except when max is at least 1),
             *  silently reducing "up to max announcements" to "zero, ever" for
             *  max=0 while still passing the {@code announced_at IS NULL} arm
             *  exactly once for a never-announced row -- one stray announcement
             *  before going permanently silent, not the loud refusal a caller
             *  asking for a nonsensical cap deserves. {@code max == 1} is the
             *  smallest MEANINGFUL value (announce once, never again) and is
             *  explicitly allowed. */
            public Announce {
                if (intervalSeconds < 0) {
                    throw new SchemaViolationException("interval_s", "must not be negative");
                }
                if (max < 1) {
                    throw new SchemaViolationException("max", "must be at least 1");
                }
                // Bead nexus-q82tk (RDR-213 boards half): a subscriber names the reader
                // the stamp is kept for, so a board post read by many sessions is
                // announced once to EACH of them, never once in total. It is a caller
                // identity exactly as `claimant` is, and carries the same ceiling. An
                // empty string is refused, not treated as absent -- a caller that
                // sends the field means to name someone.
                if (subscriber != null) {
                    if (subscriber.isBlank()) {
                        throw new SchemaViolationException("subscriber", "must not be blank");
                    }
                    checkFieldSize("subscriber", subscriber, TupleLimits.MAX_CLAIMANT_BYTES);
                }
            }

            /** {@code true} when the stamp lives in {@code nexus.tuple_deliveries}
             *  keyed by {@link #subscriber}, {@code false} when it lives on the
             *  {@code nexus.tuples} row itself (the nexus-vsipz mailbox shape). */
            public boolean perSubscriber() {
                return subscriber != null;
            }
        }
    }

    /** One subspace's matched tuples from a {@link #waitAny} call. Only subspaces
     *  that actually matched appear in {@link #waitAny}'s result list -- a subspace
     *  with nothing to report is simply absent, never present with an empty {@code
     *  tuples} list, so a client iterating results always has cursor-advancing work
     *  to do for every entry it sees. */
    public record WaitResult(String subspace, List<TupleRow> tuples) {
    }

    /**
     * {@code wait(subspaces: [{subspace, keys_pattern?, n?, since?}], timeout_s=0) ->
     * [{subspace, tuples}]} (RDR-211 Phase 1 Step 1 / §Approach item 6, bead
     * nexus-rplay.4): a multi-subspace {@code rd} that parks ONE call across several
     * subspaces, each with its own key pattern and cursor, and returns as soon as ANY
     * of them holds a matching tuple past its cursor.
     *
     * <p>Mirrors {@link #rd} exactly -- validate, register before the first query,
     * probe, park with no claimant, re-query on every wake, release in a finally.
     * Every subspace and pattern is validated BEFORE anything registers or parks (a
     * bad request never consumes a slot or a group registration); {@link
     * #queryEachOnce} then runs each subspace's own {@link #queryOnce} in a loop,
     * reusing that method's already-tested SQL rather than inventing a combined OR
     * query, per the bead's own design note. {@code wait} parks with NO claimant,
     * exactly as {@code rd} does, so it takes ONE global park slot and nothing
     * against the per-claimant cap, regardless of how many subspaces {@code
     * subspaces} names.
     *
     * <p>{@link TupleWaitRegistry#registerMulti} folds registration across every
     * named subspace into a single {@link TupleWaitRegistry.MultiWaiter}; {@link
     * TupleWaitRegistry#tryAcquireParkSlot}/{@link TupleWaitRegistry#releaseParkSlot}
     * are still called exactly once for the whole call -- the same one-slot-per-call
     * contract {@code rd}/{@code in} already have.
     *
     * <p>Same every-exit release contract as {@link #rd} and {@link #in}
     * (nexus-rplay): {@link TupleWaitRegistry.MultiWaiter#release} runs on EVERY
     * exit -- an immediate hit and an exception from the first per-subspace query
     * included, not only the park-loop path.
     */
    public List<WaitResult> waitAny(String tenant, List<WaitSpec> specs, long timeoutSeconds) {
        validateTimeout(timeoutSeconds);
        if (specs == null || specs.isEmpty()) {
            throw new SchemaViolationException("subspaces", "must name at least one subspace");
        }
        if (specs.size() > MAX_WAIT_SUBSPACES) {
            throw new SchemaViolationException("subspaces",
                    "at most " + MAX_WAIT_SUBSPACES + " subspaces per wait");
        }
        // Validate EVERY subspace and pattern BEFORE anything registers or parks
        // (RDR-211 Phase 1 Step 1) -- a bad request must never consume a park slot or
        // a group registration. queryOnce (via queryEachOnce below) re-validates on
        // every call, same as rd's own queryOnce does on every re-query; this pass is
        // what makes that guarantee hold for the FIRST subspace in the list too,
        // before registerMulti ever runs.
        List<String> subspaces = new ArrayList<>(specs.size());
        for (WaitSpec spec : specs) {
            checkFieldSize("subspace", spec.subspace(), TupleLimits.MAX_SUBSPACE_BYTES);
            resolveOrThrow(spec.subspace());
            checkPatternSizes(spec.pattern() == null ? Map.of() : spec.pattern());
            // nexus-vsipz (RDR-213 engine half): announce mode tracks position on the
            // ROW itself (announced_at/announce_count), never via a client-supplied
            // cursor -- a spec naming both would have the cursor silently do nothing,
            // so the combination is refused loud here rather than tolerated quietly.
            if (spec.announce() != null && spec.since() != null) {
                throw new SchemaViolationException("since", "must not be set together with announce");
            }
            subspaces.add(spec.subspace());
        }

        if (timeoutSeconds <= 0) {
            return queryEachOnce(tenant, specs);
        }
        // Registered BEFORE the first query, so a write landing between that query and
        // the first park is not lost (RDR-205 §Technical Design "Wake", the same
        // contract rd/in already honour).
        TupleWaitRegistry.MultiWaiter waiter = waitRegistry.registerMulti(tenant, subspaces);
        try {
            List<WaitResult> found = queryEachOnce(tenant, specs);
            if (!found.isEmpty()) {
                return found;
            }
            // wait parks with NO claimant, exactly as rd does -- one global slot, nothing
            // against the per-claimant cap of four.
            waitRegistry.tryAcquireParkSlot(null);
            try {
                long deadlineNanos = System.nanoTime() + TimeUnit.SECONDS.toNanos(timeoutSeconds);
                while (true) {
                    if (waitRegistry.isShuttingDown() || System.nanoTime() >= deadlineNanos) {
                        return queryEachOnce(tenant, specs);
                    }
                    try {
                        waiter.awaitSignalOrTimer();
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        return queryEachOnce(tenant, specs);
                    }
                    List<WaitResult> again = queryEachOnce(tenant, specs);
                    if (!again.isEmpty()) {
                        return again;
                    }
                }
            } finally {
                waitRegistry.releaseParkSlot(null);
            }
        } finally {
            waiter.release();
        }
    }

    /** Runs {@link #queryOnce} once per {@link WaitSpec} in {@code specs}, in list
     *  order -- the per-subspace re-query {@link #waitAny}'s javadoc describes,
     *  never a combined OR query. Only subspaces with at least one matching tuple
     *  appear in the returned list. */
    private List<WaitResult> queryEachOnce(String tenant, List<WaitSpec> specs) {
        List<WaitResult> out = new ArrayList<>();
        for (WaitSpec spec : specs) {
            List<TupleRow> rows = queryOnce(tenant, spec.subspace(), spec.pattern(), spec.n(), spec.since(),
                    spec.announce());
            if (!rows.isEmpty()) {
                out.add(new WaitResult(spec.subspace(), rows));
            }
        }
        return out;
    }

    // ── in / inp ─────────────────────────────────────────────────────────────

    /** {@code inp(subspace, keys_pattern, *, claimant, lease_s) -> (Tuple, claim_id) | None} — probe, never blocks.
     *  Back-compat overload for a caller that always has a {@code lease_s}
     *  (every existing test call site, unchanged) -- see the {@link Long}
     *  overload below for the optional form nexus-xapt8 adds. */
    public Optional<ClaimedTuple> inp(String tenant, String subspace, Map<String, String> pattern,
                                       String claimant, long leaseSeconds) {
        return claimOnce(tenant, subspace, pattern, claimant, leaseSeconds);
    }

    /** {@code inp(subspace, keys_pattern, *, claimant, lease_s?) -> (Tuple, claim_id) | None} — probe, never blocks.
     *  {@code leaseSecondsOrNull} nullable (nexus-xapt8, additive): {@code null} uses the
     *  matched template's own {@code take.default_lease_seconds}; see {@link #claimOnce}
     *  for the refusal when the template has none. A DISTINCT overload from the
     *  primitive {@code long} form above, not a widened replacement of it -- {@code int}/
     *  {@code long} literal call sites resolve to that overload unchanged (Java method
     *  overload resolution prefers a strict/widening-primitive match over one requiring
     *  a box), so this is additive at the Java API surface too. */
    public Optional<ClaimedTuple> inp(String tenant, String subspace, Map<String, String> pattern,
                                       String claimant, Long leaseSecondsOrNull) {
        return claimOnce(tenant, subspace, pattern, claimant, leaseSecondsOrNull);
    }

    /** {@code in(subspace, keys_pattern, *, claimant, lease_s, timeout_s=0) -> (Tuple, claim_id) | None} —
     *  blocks up to {@code timeoutSeconds}. Back-compat overload; see {@link #inp}'s
     *  own javadoc for why this and the {@link Long} overload below coexist. */
    public Optional<ClaimedTuple> in(String tenant, String subspace, Map<String, String> pattern,
                                      String claimant, long leaseSeconds, long timeoutSeconds) {
        return in(tenant, subspace, pattern, claimant, (Long) leaseSeconds, timeoutSeconds);
    }

    /** {@code in(subspace, keys_pattern, *, claimant, lease_s?, timeout_s=0) -> (Tuple, claim_id) | None} —
     *  blocks up to {@code timeoutSeconds}. {@code leaseSecondsOrNull} nullable
     *  (nexus-xapt8): see {@link #inp}. The {@link TupleWaitRegistry#register} call
     *  this makes is released ({@link TupleWaitRegistry.Waiter#release}) on EVERY
     *  exit -- an immediate hit and an exception from the first claim attempt
     *  included, not only the park-loop path (nexus-rplay). */
    public Optional<ClaimedTuple> in(String tenant, String subspace, Map<String, String> pattern,
                                      String claimant, Long leaseSecondsOrNull, long timeoutSeconds) {
        validateTimeout(timeoutSeconds);
        if (timeoutSeconds <= 0) {
            return claimOnce(tenant, subspace, pattern, claimant, leaseSecondsOrNull);
        }
        TupleWaitRegistry.Waiter waiter = waitRegistry.register(tenant, subspace);
        try {
            Optional<ClaimedTuple> found = claimOnce(tenant, subspace, pattern, claimant, leaseSecondsOrNull);
            if (found.isPresent()) {
                return found;
            }
            waitRegistry.tryAcquireParkSlot(claimant);
            try {
                long deadlineNanos = System.nanoTime() + TimeUnit.SECONDS.toNanos(timeoutSeconds);
                while (true) {
                    if (waitRegistry.isShuttingDown() || System.nanoTime() >= deadlineNanos) {
                        return claimOnce(tenant, subspace, pattern, claimant, leaseSecondsOrNull);
                    }
                    try {
                        waiter.awaitSignalOrTimer();
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        return claimOnce(tenant, subspace, pattern, claimant, leaseSecondsOrNull);
                    }
                    Optional<ClaimedTuple> again = claimOnce(tenant, subspace, pattern, claimant, leaseSecondsOrNull);
                    if (again.isPresent()) {
                        return again;
                    }
                }
            } finally {
                waitRegistry.releaseParkSlot(claimant);
            }
        } finally {
            waiter.release();
        }
    }

    private Optional<ClaimedTuple> claimOnce(String tenant, String subspace, Map<String, String> pattern,
                                              String claimant, Long leaseSecondsOrNull) {
        checkFieldSize("subspace", subspace, TupleLimits.MAX_SUBSPACE_BYTES);
        checkFieldSize("claimant", claimant, TupleLimits.MAX_CLAIMANT_BYTES);
        Map<String, String> patternSafe = pattern == null ? Map.of() : pattern;
        checkPatternSizes(patternSafe);
        TemplateSchema t = resolveOrThrow(subspace);
        if (!t.take().enabled()) {
            throw new TakeDisabledException(subspace, t.name());
        }
        // nexus-xapt8 (a scalability research pass over this design,
        // addition 6): lease_s is optional on the wire -- an omitted value
        // falls through to the template's own take.default_lease_seconds.
        // Review fix (nexus-xapt8 fix round, code review finding 2): a
        // template with no default configured throws the EXACT
        // pre-commit shape (IllegalArgumentException("lease_s required"),
        // rendered by TupleHandler's catch ladder as HTTP 400
        // {"error":"lease_s required"}), not the new SchemaViolation shape
        // an earlier draft used -- this branch is reachable by every
        // shipped template today (none declares a default), so the old
        // flat error body is a presently-observable contract, not merely
        // a historical one, and it must not move out from under an
        // existing caller. See TupleHandlerWiringTest's pinned-shape test.
        long leaseSeconds;
        if (leaseSecondsOrNull != null) {
            leaseSeconds = leaseSecondsOrNull;
        } else if (t.take().defaultLeaseSeconds() != null) {
            leaseSeconds = t.take().defaultLeaseSeconds();
        } else {
            throw new IllegalArgumentException("lease_s required");
        }
        if (leaseSeconds <= 0) {
            throw new SchemaViolationException("lease_s", "must be positive");
        }
        Long maxLease = t.take().maxLeaseSeconds();
        if (maxLease != null && leaseSeconds > maxLease) {
            throw new LeaseTooLongException(leaseSeconds, maxLease, t.name());
        }
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
                // RDR-211 Phase 1 Step 1 (bead nexus-rplay.3), Approach item 5: a
                // lock-flagged template's claim moves the tuple's OWN expiry forward to
                // now + retention, ahead of computing the lease clamp -- otherwise the
                // lease would still be capped by the OLD (about-to-be-stale) expires_at,
                // defeating "a lock lives as long as it is used". Every other template
                // keeps row.getExpiresAt() unchanged, exactly as before this bead.
                OffsetDateTime expiresAtForClaim = t.lock()
                        ? now.plusSeconds(t.retentionSeconds()).truncatedTo(java.time.temporal.ChronoUnit.MICROS)
                        : row.getExpiresAt();
                OffsetDateTime leaseUntil = clampedLeaseUntil(now, leaseSeconds, expiresAtForClaim);
                var claimUpdate = ctx.update(TUPLES)
                        .set(TUPLES.CLAIM_STATE, CLAIM_STATE_CLAIMED)
                        .set(TUPLES.CLAIMANT, claimant)
                        .set(TUPLES.CLAIM_ID, newClaimId)
                        .set(TUPLES.LEASE_UNTIL, leaseUntil)
                        .set(TUPLES.ATTEMPTS, attempts);
                if (t.lock()) {
                    claimUpdate.set(TUPLES.EXPIRES_AT, expiresAtForClaim);
                }
                claimUpdate.where(TUPLES.ID.eq(row.getId())).execute();
                insertClaimLog(ctx, tenant, subspace, t.name(), row.getId(),
                        newClaimId, claimant, TRANSITION_CLAIM, now);

                TupleRow claimed = new TupleRow(row.getId(), subspace, t.name(),
                        fromJsonb(row.getKeys()), fromJsonb(row.getDims()), row.getBody(),
                        CLAIM_STATE_CLAIMED, claimant, newClaimId, leaseUntil, attempts,
                        null, null, expiresAtForClaim, row.getCreatedAt(),
                        row.getAnnouncedAt(), row.getAnnounceCount());
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
     *
     * <p>The same UPDATE that sets {@code consumed_at} also sets {@code body} to NULL
     * (bead nexus-8zoyp): the tuple space is a coordination and metadata store, not a
     * value store, and a consumed row's body is already unreachable through the API
     * ({@code rd}/{@code in} both filter {@code consumed_at IS NULL}), so there is no
     * reason to keep it around for the row's remaining retention. The returned {@link
     * TuplesRecord} was read BEFORE this update via {@link #liveClaimRow}, so its
     * in-memory {@code body} still reflects the pre-consume value — {@code ackWithReply}
     * relies on that only for {@code getId()}, never {@code getBody()}, so this clears
     * nothing a caller of this method still needs. A reply written by {@code
     * ackWithReply} is a separate row (via {@code writeOut}) and keeps its own body.
     */
    private TuplesRecord consumeClaim(DSLContext ctx, String tenant, String claimId, String claimant) {
        TuplesRecord row = liveClaimRow(ctx, tenant, claimId);
        if (row == null) {
            throw new ClaimNotFoundException(claimId);
        }
        if (!row.getClaimant().equals(claimant)) {
            throw new ClaimOwnershipException(claimId, claimant);
        }
        // RDR-211 Phase 1 Step 1 (bead nexus-rplay.3): a lock claim refuses ack.
        // Placed here, inside the transaction, after liveClaimRow's read and before
        // any column is written -- beside the stale-claim and wrong-claimant refusals
        // just above -- because the template name lives on the ROW this method just
        // read, and nothing about a bare claim_id encodes it, so the check cannot
        // precede this method's own liveClaimRow lookup. Ack'ing a lock would clear
        // the claim columns but never consumed_at (see writeOut's lock-reset branch),
        // and claimOnce requires consumed_at IS NULL, so an acked lock would stay
        // dead until the sweep purged it -- exactly the failure Alternative 1 was
        // rejected for, reached here by an ordinary ack call instead. The claim stays
        // live because nothing is written before this throw.
        TemplateSchema template = resolveOrThrow(row.getSubspace());
        if (template.lock()) {
            throw new SchemaViolationException("claim_id",
                    "template '" + template.name() + "' is a lock template; ack is refused because a "
                    + "consumed lock row would be unobtainable until the sweep purges it -- call release "
                    + "instead to return the lock without consuming it");
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
                .set(TUPLES.BODY, (String) null)
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
        checkClaimIdentifiers(claimId, claimant);
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
        checkClaimIdentifiers(claimId, claimant);
        if (reply == null) {
            ack(tenant, claimId, claimant);
            return null;
        }
        PreparedOut prepared = prepareOut(reply.subspace(), reply.keys(), reply.dims(), reply.body(),
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
        checkClaimIdentifiers(claimId, claimant);
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
     * RDR-211 Phase 1 Step 1 (bead nexus-rplay.2): {@code release(claim_id, claimant)}
     * — a hand-back that is NOT a failure. Closes RDR-211 Gap 4: until this operation,
     * ending a live claim meant either consuming the tuple ({@link #ack}) or counting a
     * failed attempt ({@link #nack}); nothing let a holder return a task, or a lock, to
     * available without spending either.
     *
     * <p>Ends a live claim, returns the tuple to available WITHOUT counting an attempt
     * (unlike {@link #nack}, which always does), logs a {@value #TRANSITION_RELEASE}
     * transition (no schema change needed — {@code tuple_claim_log.transition} is a
     * plain {@code TEXT NOT NULL} column with no {@code CHECK} constraint, per {@code
     * tuples-001-baseline.xml}), and signals the subspace's waiters AFTER the commit,
     * the same placement {@link #out} and {@link #ackWithReply} use — a parked reader
     * must not be woken by a transaction that rolled back. A claim that is no longer
     * live raises {@link ClaimNotFoundException}, exactly as {@link #ack}, {@link
     * #nack} and {@link #renew} do; a claim held by someone else raises {@link
     * ClaimOwnershipException}.
     *
     * <p>Reuses {@link #releaseOrDeadLetter} with {@code attempts} passed UNCHANGED
     * (never incremented, unlike {@code nack}'s {@code attempts + 1}) and {@code
     * maxAttempts} passed as {@link Long#MAX_VALUE} so a release can never itself
     * dead-letter the tuple — a hand-back is not a failure, so it must never spend the
     * template's failure budget or trip its dead-letter ceiling, regardless of how many
     * attempts the row already carries.
     */
    public void release(String tenant, String claimId, String claimant) {
        checkClaimIdentifiers(claimId, claimant);
        String subspace = tenantScope.withTenant(tenant, ctx -> {
            TuplesRecord row = liveClaimRow(ctx, tenant, claimId);
            if (row == null) {
                throw new ClaimNotFoundException(claimId);
            }
            if (!row.getClaimant().equals(claimant)) {
                throw new ClaimOwnershipException(claimId, claimant);
            }
            // TEST-ONLY (nexus-h61dl.2): see ack.
            TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY.run();
            OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);

            ReleaseOutcome outcome = releaseOrDeadLetter(ctx, tenant, row.getSubspace(), row.getTemplate(),
                    row.getId(), claimId, claimant, TRANSITION_RELEASE, now, row.getAttempts(), Long.MAX_VALUE);
            if (outcome == ReleaseOutcome.NOT_LIVE) {
                // The compare-and-swap matched nothing: the sweep or a re-take moved the
                // row between liveClaimRow's read and this update (RDR-206 Step 1).
                throw new ClaimNotFoundException(claimId);
            }
            return row.getSubspace();
        });
        // Signal AFTER the commit, exactly as out and ackWithReply do -- a parked
        // reader must not be woken by a transaction that rolled back.
        waitRegistry.signalAll(tenant, subspace);
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
     * <p>The lease is clamped to the tuple's own expiry IN SQL, against the row as it
     * stands at UPDATE time — not by {@link #clampedLeaseUntil}, which is now
     * {@code claimOnce}'s alone because that caller reads under a lock and this one does
     * not. The reason is at the update itself. A lease longer than the template's
     * {@code max_lease_seconds} is REFUSED rather than clamped: renewal changes who
     * decides when work is long, not the cap.
     *
     * @return the new {@code lease_until}, at the precision the row stores.
     */
    public OffsetDateTime renew(String tenant, String claimId, String claimant, long leaseSeconds) {
        checkClaimIdentifiers(claimId, claimant);
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
            //
            // RDR-211 Phase 1 Step 1 (bead nexus-rplay.3), Approach item 5: a
            // lock-flagged template's renew ALSO moves the tuple's own expiry forward
            // to now + retention -- "a lock lives as long as it is used" requires
            // EXPIRES_AT itself to move, not merely LEASE_UNTIL's clamp against a
            // ceiling that otherwise never advances. Every other template computes
            // ceilingField from the OLD row (TUPLES.EXPIRES_AT, unchanged from before
            // this bead); newExpiresAtOrNull is a plain Java value substituted as a SQL
            // literal, so there is no ordering ambiguity between the two .set() calls.
            Field<OffsetDateTime> ceilingField = TUPLES.EXPIRES_AT;
            OffsetDateTime newExpiresAtOrNull = null;
            if (t.lock()) {
                newExpiresAtOrNull = now.plusSeconds(t.retentionSeconds())
                        .truncatedTo(java.time.temporal.ChronoUnit.MICROS);
                ceilingField = DSL.val(newExpiresAtOrNull);
            }
            var renewUpdate = ctx.update(TUPLES)
                    .set(TUPLES.LEASE_UNTIL, DSL.least(DSL.val(candidate), ceilingField));
            if (t.lock()) {
                renewUpdate.set(TUPLES.EXPIRES_AT, newExpiresAtOrNull);
            }
            var stored = renewUpdate
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
                // Fix round (coordinator, sweep-isolation critical sibling): savepoint-
                // guarded, so ONE row's failure (any exception -- a constraint
                // violation, a transient statement error) cannot wedge the REST of this
                // batch. Without the savepoint, a thrown exception here would leave
                // Postgres's server-side transaction in the aborted state for every
                // statement after it, including every OTHER row's release still to
                // come in this loop and the SELECT above's own row lock; with it, only
                // this row's own write is rolled back, and the loop moves on to the
                // next selected row. A row that fails EVERY tick still sorts first
                // (ORDER BY lease_until ASC) and is reselected every tick, but it no
                // longer prevents this SAME batch's other (up to batchSize-1) rows from
                // making progress the way the shared-transaction-abort failure mode
                // did. Mirrors CatalogRepository#withSavepointFailOpen's exact
                // mechanism (same doctrine: a sweep failure must never leave the
                // surrounding transaction aborted for load-bearing work after it).
                ReleaseOutcome outcome;
                try {
                    outcome = withRowSavepoint(ctx, () -> releaseOrDeadLetter(ctx, tenant, row.getSubspace(),
                            row.getTemplate(), row.getId(), row.getClaimId(), row.getClaimant(),
                            TRANSITION_EXPIRE, now, attempts, maxAttempts));
                } catch (RuntimeException ex) {
                    log.warn("event=tuple_sweep_release_row_failed tenant={} subspace={} tuple_id={} "
                                    + "claim_id={} error={}",
                            tenant, row.getSubspace(), HexFormat.of().formatHex(row.getId()), row.getClaimId(),
                            ex.toString());
                    continue;
                }
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
     * write time as {@code at + }{@link TemplateRegistry#effectiveClaimLogTtlSeconds(String)}
     * — a template's own {@code claim_log_ttl_seconds} when it declares a shorter one
     * (RDR-211 Scale and Limits item 6, bead nexus-rplay.6), else the engine-wide
     * default the registry's own boot check validated against every template's
     * {@code retention_seconds} (never a second, independently-parsed copy of {@code
     * NX_TUPLE_CLAIM_LOG_TTL_DAYS}) — so this arm reads the stored column directly
     * rather than recomputing a cutoff from {@code at} a second time (RDR-205 P1
     * follow-on, nexus-em75s.37, review M6 / critique S2), and needs no per-template
     * awareness of its own: two rows with different templates simply carry different
     * {@code expires_at} values, purged (or not) uniformly by the same comparison
     * against "now".
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
     * TemplateRegistry#effectiveClaimLogTtlSeconds(String)} — {@code template}'s OWN
     * TTL when it declares a shorter one (RDR-211 Scale and Limits item 6, bead
     * nexus-rplay.6), else the engine-wide default — rather than accepted as a
     * caller-supplied parameter, so no call site can (again) pass the tuple's own
     * {@code expires_at} by mistake. This is the ONLY site that stamps this column;
     * {@code purgeOldClaimLogBatch} purges purely by comparing the stored value against
     * "now", so it needs no per-template awareness of its own — every row already
     * carries the deadline the template in force at WRITE time computed for it, jOOQ
     * DSL throughout, no join and no per-template loop added to the purge arm.
     */
    private void insertClaimLog(DSLContext ctx, String tenant, String subspace, String template,
                                 byte[] tupleId, String claimId, String claimant,
                                 String transition, OffsetDateTime at) {
        OffsetDateTime logExpiresAt = at.plusSeconds(registry.effectiveClaimLogTtlSeconds(template));
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

    /** {@code subspace_list(prefix) -> [{subspace, total, available, ..., oldest_created_at, newest_created_at}]}.
     *  Unbounded / unpaged form: EVERY matching subspace, no cursor -- the
     *  pre-nexus-xapt8 wire shape and behaviour, byte-identical for a caller
     *  that never opts into paging. {@code HealthCheck}'s {@code
     *  tuples.oldest_unclaimed} doctor row (Python {@code health.py}) needs
     *  every claimable subspace and deliberately keeps this unbounded form. */
    public List<SubspaceCensus> subspaceList(String tenant, String prefix) {
        return subspaceListPage(tenant, prefix, null, null).items();
    }

    /**
     * {@code subspace_list(prefix, limit?, after?) -> {items, next_cursor?}}
     * (nexus-xapt8, a scalability research pass over this design, addition
     * 2): ONE {@code GROUP BY subspace} query computing every {@link
     * SubspaceCensus} field for every matching subspace in a single round
     * trip -- replaces the prior {@code SELECT DISTINCT subspace} plus one
     * {@link #computeCensus} call PER subspace, an N+1 shape whose cost
     * scaled with subspace count rather than row count. Semantics match
     * {@link #computeCensus} exactly: {@code total} is live rows only
     * (available+claimed+dead); the two timestamps span every row, live or
     * not.
     *
     * <p>{@code limit} is optional and capped at {@link #readMax} (the same
     * ceiling {@code rd}/{@code rdp}'s own {@code n} uses). Review fix
     * (nexus-xapt8 fix round): {@code null} -- the param genuinely
     * omitted -- means UNBOUNDED, matching {@link #subspaceList}'s
     * pre-existing contract exactly (the caller opted out of paging
     * entirely). A non-null but non-positive {@code limit} (an explicit
     * {@code 0} or negative value) instead clamps to 1, matching {@code
     * rd}/{@code rdp}'s OWN {@code n <= 0 -> 1} convention at {@link
     * #readMax}'s sibling call site (~line 617) -- the two "unbounded"
     * and "clamp to the minimum" cases are no longer folded into one
     * bucket the way an earlier draft of this method did. {@code after} is
     * a subspace-name cursor (the last subspace name from a prior
     * truncated page); results are always ordered by subspace name, and
     * {@link SubspacePage#nextCursor} is non-null exactly when the page
     * was truncated by {@code limit}.
     *
     * <p>A request-path {@code statement_timeout} applies ({@link
     * SweepBounds#applyStatementTimeout}'s {@code is_local=true} pattern, at
     * {@link #subspaceListTimeoutSeconds}) -- unlike the scheduled sweep's
     * own bounded batch arms, this query runs ON DEMAND against whatever
     * cardinality a tenant has accumulated, so nothing else bounds it. This
     * matters most for the UNFILTERED call ({@code prefix=null}, exactly
     * what {@link #subspaceList}'s unpaged form and the {@code
     * tuples.oldest_unclaimed} doctor row issue): {@code
     * idx_tuples_subspace_scan} (the sibling index this changeset also
     * adds) does NOT serve this shape -- there is no subspace predicate for
     * it to seek on, and none of the aggregated columns are covered, so
     * Postgres chooses a full scan + hash aggregate (MEASURED, fix-round
     * correction: {@code TupleSweepIndexPlanShapeTest
     * #unfilteredGroupByCensusQuery_doesNotUseTheSubspaceScanIndex_seqScanInstead}).
     * The statement_timeout, not the index, is what bounds this call.
     */
    public SubspacePage subspaceListPage(String tenant, String prefix, Integer limit, String after) {
        Integer effectiveLimit = (limit == null) ? null : Math.min(limit <= 0 ? 1 : limit, readMax);
        try {
            return subspaceListPageUnguarded(tenant, prefix, after, effectiveLimit);
        } catch (RuntimeException e) {
            if (isStatementTimeout(e)) {
                throw new CensusTimeoutException(subspaceListTimeoutSeconds);
            }
            throw e;
        }
    }

    /**
     * Critique finding 10 (nexus-xapt8 fix round): walks {@code e}'s cause
     * chain for a {@link java.sql.SQLException} whose SQLState is {@code
     * 57014} ({@code query_canceled}) -- the exact signal {@code
     * SweepBounds#applyStatementTimeout}'s {@code statement_timeout}
     * produces on cancellation. Same walk-the-chain idiom {@code
     * CatalogRepository#classifySweepFailureReason} already uses for the
     * identical SQLState.
     */
    private static boolean isStatementTimeout(Throwable e) {
        for (Throwable c = e; c != null; c = c.getCause()) {
            if (c instanceof java.sql.SQLException se && "57014".equals(se.getSQLState())) {
                return true;
            }
        }
        return false;
    }

    /**
     * TEST-ONLY (nexus-xapt8, critique finding 10): runs BEFORE {@link
     * #subspaceListPageUnguarded}'s own {@code SELECT}, inside the SAME
     * transaction where {@link SweepBounds#applyStatementTimeout} has
     * already run {@code SET LOCAL statement_timeout} -- a test installs a
     * {@code ctx -> ctx.resultQuery("SELECT pg_sleep(...)").fetch()} hook to
     * force a GENUINE Postgres-side {@code 57014} cancellation (the
     * statement_timeout applies to every statement in that transaction, not
     * only the census query), proving {@link #isStatementTimeout} and the
     * {@link CensusTimeoutException} rethrow against a real cancellation
     * rather than an unrealistically large seeded fixture. A no-op by
     * default (costs nothing in production); package-private, never
     * assigned outside test code -- same shape as {@link
     * #TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY}.
     */
    static volatile java.util.function.Consumer<DSLContext> TEST_ONLY_SUBSPACE_LIST_PRE_QUERY_HOOK = ctx -> { };

    private SubspacePage subspaceListPageUnguarded(String tenant, String prefix, String after, Integer effectiveLimit) {
        return tenantScope.withTenant(tenant, ctx -> {
            SweepBounds.applyStatementTimeout(ctx, Duration.ofSeconds(subspaceListTimeoutSeconds));
            TEST_ONLY_SUBSPACE_LIST_PRE_QUERY_HOOK.accept(ctx);

            Condition cond = TUPLES.TENANT_ID.eq(tenant);
            if (prefix != null && !prefix.isBlank()) {
                cond = cond.and(TUPLES.SUBSPACE.startsWith(prefix));
            }
            if (after != null && !after.isBlank()) {
                cond = cond.and(TUPLES.SUBSPACE.gt(after));
            }

            Field<OffsetDateTime> now = DSL.currentOffsetDateTime();
            Condition live = TUPLES.CONSUMED_AT.isNull().and(TUPLES.EXPIRES_AT.gt(now));

            var baseQuery = ctx.select(
                            TUPLES.SUBSPACE,
                            DSL.count().filterWhere(live.and(TUPLES.CLAIM_STATE.isNull())).cast(Long.class),
                            DSL.count().filterWhere(live.and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_CLAIMED)))
                                    .cast(Long.class),
                            DSL.count().filterWhere(live.and(TUPLES.CLAIM_STATE.eq(CLAIM_STATE_DEAD)))
                                    .cast(Long.class),
                            DSL.count().filterWhere(TUPLES.CONSUMED_AT.isNotNull()).cast(Long.class),
                            DSL.count().filterWhere(TUPLES.CONSUMED_AT.isNull().and(TUPLES.EXPIRES_AT.le(now)))
                                    .cast(Long.class),
                            DSL.min(TUPLES.CREATED_AT),
                            DSL.max(TUPLES.CREATED_AT))
                    .from(TUPLES)
                    .where(cond)
                    .groupBy(TUPLES.SUBSPACE)
                    .orderBy(TUPLES.SUBSPACE.asc());

            var recs = (effectiveLimit != null) ? baseQuery.limit(effectiveLimit + 1).fetch() : baseQuery.fetch();

            List<SubspaceCensus> out = new ArrayList<>();
            String nextCursor = null;
            for (int i = 0; i < recs.size(); i++) {
                if (effectiveLimit != null && i >= effectiveLimit) {
                    nextCursor = out.get(out.size() - 1).subspace();
                    break;
                }
                var rec = recs.get(i);
                String subspace = rec.get(0, String.class);
                long available = rec.get(1, Long.class);
                long claimed = rec.get(2, Long.class);
                long dead = rec.get(3, Long.class);
                long consumed = rec.get(4, Long.class);
                long expiredUnpurged = rec.get(5, Long.class);
                OffsetDateTime oldest = rec.get(6, OffsetDateTime.class);
                OffsetDateTime newest = rec.get(7, OffsetDateTime.class);
                out.add(new SubspaceCensus(subspace, available + claimed + dead, available, claimed, dead,
                        consumed, expiredUnpurged, oldest, newest));
            }
            return new SubspacePage(out, nextCursor);
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

    /**
     * Run {@code body} under a JDBC SAVEPOINT taken on {@code ctx}'s own connection,
     * rolling back to it (never releasing -- Postgres discards it at the enclosing
     * transaction's own COMMIT/ROLLBACK regardless, same reasoning as {@code
     * CatalogRepository#withSavepointFailOpen}) and RE-THROWING on any exception, so
     * the surrounding transaction is restored to a workable state for whatever runs
     * after this call rather than being left in Postgres's server-side aborted state.
     *
     * <p>Unlike {@code CatalogRepository#withSavepointFailOpen} this does NOT
     * swallow the exception (no {@code fallback} value) -- {@link
     * #releaseLapsedClaimsBatch}'s only caller here needs to know a row failed (to
     * log it and skip to the next row without counting it as released or dead-
     * lettered), not a silently substituted value. The savepoint is the shared
     * mechanism between the two call sites; whether to swallow or rethrow is each
     * caller's own policy.
     *
     * <p>Round-2 review finding (CRE pass 2, 2026-09-13): a row this rolls back is
     * bounded by its own {@code expires_at}, NOT by {@code attempts} -- the
     * {@code UPDATE ... SET attempts = ...} inside {@link #releaseOrDeadLetter} is
     * itself part of what this savepoint rolls back on failure, so a row that fails
     * on EVERY tick never advances {@code attempts} and can never reach {@code
     * max_attempts} that way. It is reaped only by the independent {@link
     * #purgeExpiredTuplesBatch} arm once its {@code expires_at} passes -- a raw
     * DELETE immune to whatever made this row's release throw -- never by hitting a
     * dead-letter threshold it can no longer count toward.
     */
    private static <T> T withRowSavepoint(DSLContext ctx, Callable<T> body) {
        Connection conn = ctx.configuration().connectionProvider().acquire();
        Savepoint sp = null;
        try {
            sp = conn.setSavepoint();
            return body.call();
        } catch (Exception e) {
            if (sp != null) {
                try {
                    conn.rollback(sp);
                } catch (SQLException se) {
                    log.error("event=tuple_sweep_savepoint_rollback_failed error={}", se.getMessage(), se);
                }
            }
            throw (e instanceof RuntimeException re) ? re : new RuntimeException(e);
        } finally {
            ctx.configuration().connectionProvider().release(conn);
        }
    }

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
        return toRow(r, r.getAnnouncedAt(), r.getAnnounceCount());
    }

    /** {@code announcedAt}/{@code announceCount} override (bead nexus-vsipz): used
     *  by {@link #queryOnceAnnounce} to return the POST-stamp values without a
     *  second read-back; every other caller passes {@code r}'s own persisted
     *  columns via the no-arg overload above. */
    private static TupleRow toRow(TuplesRecord r, OffsetDateTime announcedAt, int announceCount) {
        return new TupleRow(r.getId(), r.getSubspace(), r.getTemplate(),
                fromJsonb(r.getKeys()), fromJsonb(r.getDims()), r.getBody(),
                r.getClaimState(), r.getClaimant(), r.getClaimId(),
                r.getLeaseUntil(), r.getAttempts(),
                r.getConsumedAt(), r.getConsumedBy(),
                r.getExpiresAt(), r.getCreatedAt(),
                announcedAt, announceCount);
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
