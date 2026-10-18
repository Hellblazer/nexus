// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.jooq.types.DayToSecond;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import javax.sql.DataSource;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.2): request identity for a mutating engine route, kept in
 * {@code nexus.request_ledger} ({@code vectors-033-request-ledger.xml}).
 *
 * <p>A keyed route runs three steps. {@link #claim} records the attempt in its own short
 * transaction, committed before the work starts, so {@link #status} can say {@code in_flight}
 * while the work runs. The work then runs in one transaction that ends with
 * {@link #commitMarker}, so the mutation and the stored response commit or roll back together.
 * An exception before that commit calls {@link #failMarker}; an exception after it (writing the
 * response to a client the edge already cut) does not, so a committed request never reads
 * {@code rolled_back}.
 *
 * <p>Each attempt holds the row through a random {@code holder} nonce and a lease. A resend finds
 * the row and replays it ({@code committed}), waits ({@code in_flight}, lease live), or takes it
 * over ({@code in_flight} with the lease expired, or {@code failed}). Both markers match on the
 * nonce, so an attempt that lost its row to a takeover can neither commit nor fail it, and the
 * commit marker also requires a live lease, so an attempt whose lease ran out cannot commit late.
 *
 * <p>Every time comparison and stamp uses {@code clock_timestamp()}, the wall clock at the
 * statement, never {@code now()}, which is the transaction's start (nexus-icdmw item 3).
 */
public final class RequestLedger {

    private static final Logger log = LoggerFactory.getLogger(RequestLedger.class);

    /** HikariCP's own default, used when the DataSource is not a Hikari pool. */
    private static final long DEFAULT_CONNECTION_TIMEOUT_MS = 30_000L;

    /** Slack beyond the two pool waits, for the claim, the marker and clock skew between them. */
    static final long LEASE_SLACK_MS = 10_000L;

    static final String IN_FLIGHT = "in_flight";
    static final String COMMITTED = "committed";
    static final String FAILED = "failed";

    /** The retention setting: days a finished (or lease-expired) row is kept. */
    public static final String RETENTION_ENV = "NX_REQUEST_LEDGER_RETENTION_DAYS";

    /** Retention when {@link #RETENTION_ENV} is unset or invalid (Sam, 2026-10-17). */
    public static final int DEFAULT_RETENTION_DAYS = 7;

    private static final int SHA256_BYTES = 32;
    private static final int MAX_CLAIM_ROUNDS = 3;

    /** The outcome of {@link #claim}. */
    public sealed interface Claim permits Claimed, Replay, InFlight, KeyReused {
    }

    /** This attempt holds the row: run the work, then {@link #commitMarker} with {@code nonce}. */
    public record Claimed(UUID requestId, UUID nonce, boolean takeover) implements Claim {
    }

    /** The key already committed: answer with the stored status and body; the work does not run. */
    public record Replay(UUID requestId, int status, String responseJson) implements Claim {
    }

    /** Another attempt holds a live lease on the key: the handler answers 409. */
    public record InFlight(UUID requestId, String route, OffsetDateTime startedAt, OffsetDateTime leaseUntil)
        implements Claim {
    }

    /** The key was first used for a different route or body: the handler answers 422. */
    public record KeyReused(UUID requestId, String storedRoute) implements Claim {
    }

    /** What {@code GET /v1/requests/<id>} reports. */
    public enum StatusKind { COMMITTED, IN_FLIGHT, ROLLED_BACK, UNKNOWN }

    /**
     * One status reading. {@code COMMITTED} carries the stored response; {@code IN_FLIGHT} the
     * start and lease; {@code ROLLED_BACK} (an expired lease or a failure marker) and
     * {@code UNKNOWN} (no row for this tenant) carry what the row has, or nulls.
     */
    public record Status(StatusKind kind, String route, OffsetDateTime startedAt, OffsetDateTime leaseUntil,
                         OffsetDateTime finishedAt, Integer responseStatus, String responseJson) {
        static final Status UNKNOWN = new Status(StatusKind.UNKNOWN, null, null, null, null, null, null);
    }

    /**
     * The commit marker matched no row: the attempt's lease expired or another attempt took the
     * row over. Thrown inside the mutation's transaction, so the mutation rolls back with it.
     */
    public static final class LedgerMarkerLost extends RuntimeException {
        public LedgerMarkerLost(String message) {
            super(message);
        }
    }

    private final TenantScope scope;
    private final long connectionTimeoutMs;

    /**
     * @param scope      the tenant gateway every ledger read and write goes through
     * @param dataSource the pool behind {@code scope}; its connection timeout sizes the lease
     *                   margin, read the way {@link TenantScope} reads it
     */
    public RequestLedger(TenantScope scope, DataSource dataSource) {
        this.scope = scope;
        long timeoutMs = DEFAULT_CONNECTION_TIMEOUT_MS;
        if (dataSource instanceof com.zaxxer.hikari.HikariDataSource hikari && hikari.getConnectionTimeout() > 0) {
            timeoutMs = hikari.getConnectionTimeout();
        }
        this.connectionTimeoutMs = timeoutMs;
    }

    /**
     * The lease for an attempt bounded by {@code attemptBoundMs} (nexus-zxgld gap 2): the bound,
     * plus two pool waits ({@link TenantScope#withTenant} first waits up to the connection
     * timeout for an admission permit, then {@code getConnection} can wait the same again), plus
     * {@link #LEASE_SLACK_MS}. A keyed sweep on the default pool: 25 s + 2 x 30 s + 10 s = 95 s.
     */
    public long leaseMs(long attemptBoundMs) {
        return attemptBoundMs + 2 * connectionTimeoutMs + LEASE_SLACK_MS;
    }

    /**
     * Claim {@code requestId} for one attempt, in a transaction of its own that commits before
     * this returns.
     *
     * @param bodySha256     SHA-256 of the request body, exactly 32 bytes
     * @param attemptBoundMs the attempt's statement bound; the lease is {@link #leaseMs} of it
     */
    public Claim claim(String tenant, UUID requestId, String route, byte[] bodySha256, long attemptBoundMs) {
        if (requestId == null) {
            throw new IllegalArgumentException("requestId must not be null");
        }
        if (route == null || route.isBlank()) {
            throw new IllegalArgumentException("route must not be null or blank");
        }
        if (bodySha256 == null || bodySha256.length != SHA256_BYTES) {
            throw new IllegalArgumentException("bodySha256 must be " + SHA256_BYTES + " bytes");
        }
        Field<OffsetDateTime> leaseUntil = leaseUntil(leaseMs(attemptBoundMs));

        // A row can vanish between the insert's conflict and the read (the retention arm), and a
        // takeover can lose to another attempt; each re-runs the round, which then classifies the
        // row as it now stands. Three rounds cover both.
        for (int round = 0; round < MAX_CLAIM_ROUNDS; round++) {
            UUID nonce = UUID.randomUUID();
            int inserted = scope.withTenant(tenant, ctx -> ctx.insertInto(REQUEST_LEDGER)
                .set(REQUEST_LEDGER.TENANT_ID, tenant)
                .set(REQUEST_LEDGER.REQUEST_ID, requestId)
                .set(REQUEST_LEDGER.ROUTE, route)
                .set(REQUEST_LEDGER.BODY_SHA256, bodySha256)
                .set(REQUEST_LEDGER.STATE, IN_FLIGHT)
                .set(REQUEST_LEDGER.HOLDER, nonce)
                .set(REQUEST_LEDGER.STARTED_AT, clock())
                .set(REQUEST_LEDGER.LEASE_UNTIL, leaseUntil)
                .onConflictDoNothing()
                .execute());
            if (inserted == 1) {
                return new Claimed(requestId, nonce, false);
            }

            LedgerRow row = read(tenant, requestId);
            if (row == null) {
                continue;
            }
            if (!row.route().equals(route) || !Arrays.equals(row.bodySha256(), bodySha256)) {
                RequestLedgerActivity.recordKeyReused();
                log.info("event=request_ledger_key_reused tenant={} request_id={} route={} stored_route={}",
                    tenant, requestId, route, row.route());
                return new KeyReused(requestId, row.route());
            }
            if (COMMITTED.equals(row.state())) {
                RequestLedgerActivity.recordReplay();
                log.info("event=request_ledger_replay tenant={} request_id={} route={} status={}",
                    tenant, requestId, route, row.responseStatus());
                return new Replay(requestId, row.responseStatus() == null ? 200 : row.responseStatus(),
                    row.responseJson());
            }
            if (IN_FLIGHT.equals(row.state()) && row.leaseLive()) {
                RequestLedgerActivity.recordInFlight();
                log.info("event=request_ledger_in_flight tenant={} request_id={} route={} lease_until={}",
                    tenant, requestId, route, row.leaseUntil());
                return new InFlight(requestId, row.route(), row.startedAt(), row.leaseUntil());
            }

            // Takeover (nexus-zxgld gap 1): the failure marker leaves holder NULL, so the guard is
            // IS NOT DISTINCT FROM; an equality guard would never match a failed row.
            int taken = scope.withTenant(tenant, ctx -> ctx.update(REQUEST_LEDGER)
                .set(REQUEST_LEDGER.HOLDER, nonce)
                .set(REQUEST_LEDGER.STATE, IN_FLIGHT)
                .set(REQUEST_LEDGER.LEASE_UNTIL, leaseUntil)
                .setNull(REQUEST_LEDGER.FINISHED_AT)
                .setNull(REQUEST_LEDGER.RESPONSE_STATUS)
                .where(key(tenant, requestId))
                .and(REQUEST_LEDGER.HOLDER.isNotDistinctFrom(row.holder()))
                .and(REQUEST_LEDGER.STATE.eq(row.state()))
                .execute());
            if (taken == 1) {
                RequestLedgerActivity.recordTakeover();
                log.info("event=request_ledger_takeover tenant={} request_id={} route={} prior_state={}",
                    tenant, requestId, route, row.state());
                return new Claimed(requestId, nonce, true);
            }
        }
        throw new IllegalStateException(
            "request_ledger claim did not settle in " + MAX_CLAIM_ROUNDS + " rounds: tenant=" + tenant
                + " request_id=" + requestId);
    }

    /**
     * What {@link #claim} would answer for a key whose row is already {@code committed}, without claiming
     * anything: {@link Replay} when the route and body match, {@link KeyReused} when they do not. Null when there
     * is no row or it is not committed. Writes nothing, so a request that cannot run (its collection is gone) can
     * still be answered from the ledger without leaving a row behind for a key that never ran.
     */
    public Claim peekCommitted(String tenant, UUID requestId, String route, byte[] bodySha256) {
        LedgerRow row = read(tenant, requestId);
        if (row == null || !COMMITTED.equals(row.state())) {
            return null;
        }
        if (!row.route().equals(route) || !Arrays.equals(row.bodySha256(), bodySha256)) {
            RequestLedgerActivity.recordKeyReused();
            log.info("event=request_ledger_key_reused tenant={} request_id={} route={} stored_route={}",
                tenant, requestId, route, row.route());
            return new KeyReused(requestId, row.route());
        }
        RequestLedgerActivity.recordReplay();
        log.info("event=request_ledger_replay tenant={} request_id={} route={} status={}",
            tenant, requestId, route, row.responseStatus());
        return new Replay(requestId, row.responseStatus() == null ? 200 : row.responseStatus(), row.responseJson());
    }

    /**
     * Mark the attempt committed, inside the mutation's own transaction ({@code dsl}), after the
     * mutation's SQL. Throws {@link LedgerMarkerLost} when the attempt no longer holds a live lease
     * on the row, so the mutation rolls back with it.
     */
    public void commitMarker(DSLContext dsl, String tenant, UUID requestId, UUID nonce, int status,
                             String responseJson) {
        int updated = dsl.update(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.STATE, COMMITTED)
            .setNull(REQUEST_LEDGER.HOLDER)
            .set(REQUEST_LEDGER.FINISHED_AT, clock())
            .set(REQUEST_LEDGER.RESPONSE_STATUS, status)
            .set(REQUEST_LEDGER.RESPONSE, responseJson == null ? null : JSONB.valueOf(responseJson))
            .where(key(tenant, requestId))
            .and(REQUEST_LEDGER.HOLDER.eq(nonce))
            .and(REQUEST_LEDGER.LEASE_UNTIL.gt(clock()))
            .execute();
        if (updated != 1) {
            throw new LedgerMarkerLost("request_ledger commit marker lost: tenant=" + tenant
                + " request_id=" + requestId + " (lease expired or taken over)");
        }
    }

    /**
     * Mark the attempt failed, best effort, in a transaction of its own. A failure here is logged
     * at WARN and never thrown: if the engine dies instead, the lease expiry covers it. Matches
     * only this attempt's in-flight row, so it cannot touch a committed row or a newer holder's.
     */
    public void failMarker(String tenant, UUID requestId, UUID nonce, int status) {
        try {
            scope.withTenant(tenant, ctx -> ctx.update(REQUEST_LEDGER)
                .set(REQUEST_LEDGER.STATE, FAILED)
                .setNull(REQUEST_LEDGER.HOLDER)
                .set(REQUEST_LEDGER.FINISHED_AT, clock())
                .set(REQUEST_LEDGER.RESPONSE_STATUS, status)
                .where(key(tenant, requestId))
                .and(REQUEST_LEDGER.HOLDER.eq(nonce))
                .and(REQUEST_LEDGER.STATE.eq(IN_FLIGHT))
                .execute());
        } catch (RuntimeException e) {
            log.warn("event=request_ledger_fail_marker_failed tenant={} request_id={} reason={}",
                tenant, requestId, e.toString());
        }
    }

    /** The status of {@code requestId} as {@code tenant} sees it. */
    public Status status(String tenant, UUID requestId) {
        LedgerRow row = read(tenant, requestId);
        if (row == null) {
            return Status.UNKNOWN;
        }
        StatusKind kind;
        if (COMMITTED.equals(row.state())) {
            kind = StatusKind.COMMITTED;
        } else if (IN_FLIGHT.equals(row.state()) && row.leaseLive()) {
            kind = StatusKind.IN_FLIGHT;
        } else {
            kind = StatusKind.ROLLED_BACK;
        }
        return new Status(kind, row.route(), row.startedAt(), row.leaseUntil(), row.finishedAt(),
            row.responseStatus(), row.responseJson());
    }

    // ── retention (nexus-yye8l.3) ────────────────────────────────────────────

    /** One tenant's retention pass: rows deleted, and whether the pass failed (then {@code deleted} is 0). */
    public record RetentionPass(String tenant, int deleted, boolean failed) {
    }

    /**
     * Retention in days from {@link #RETENTION_ENV}, read through {@code env}. Unset or blank is
     * {@link #DEFAULT_RETENTION_DAYS}. A value that is not a positive integer also falls back to
     * the default, with exactly one WARN line.
     */
    public static int resolveRetentionDays(Function<String, String> env) {
        String raw = env.apply(RETENTION_ENV);
        if (raw == null || raw.isBlank()) {
            return DEFAULT_RETENTION_DAYS;
        }
        try {
            int days = Integer.parseInt(raw.strip());
            if (days > 0) {
                return days;
            }
        } catch (NumberFormatException e) {
            // falls through to the single WARN below
        }
        log.warn("event=request_ledger_retention_invalid setting={} value={} using_days={}",
            RETENTION_ENV, raw, DEFAULT_RETENTION_DAYS);
        return DEFAULT_RETENTION_DAYS;
    }

    /**
     * Delete {@code tenant}'s old rows in one transaction and return the count: a {@code committed}
     * or {@code failed} row finished before {@code cutoff}, or an {@code in_flight} row whose lease
     * ran out before {@code cutoff}. A live lease is later than any cutoff taken from the past, so a
     * live {@code in_flight} row never matches, whatever its {@code started_at}. A row whose lease
     * is NULL does not match either (NULL is not less than anything).
     *
     * <p>The tenant filter is explicit as well as enforced by RLS, as in {@link #key}.
     */
    public int retain(String tenant, OffsetDateTime cutoff) {
        return scope.withTenant(tenant, ctx -> ctx.deleteFrom(REQUEST_LEDGER)
            .where(REQUEST_LEDGER.TENANT_ID.eq(tenant))
            .and(REQUEST_LEDGER.STATE.in(COMMITTED, FAILED).and(REQUEST_LEDGER.FINISHED_AT.lt(cutoff))
                .or(REQUEST_LEDGER.STATE.eq(IN_FLIGHT).and(REQUEST_LEDGER.LEASE_UNTIL.lt(cutoff))))
            .execute());
    }

    /**
     * One {@link #retain} pass per tenant, each logged at INFO. A tenant's failure is logged at WARN
     * and the next tenant still runs. The caller supplies the tenant list because the ledger is
     * FORCE RLS: a tenant cannot be enumerated from the table itself.
     */
    public List<RetentionPass> retainAll(List<String> tenants, OffsetDateTime cutoff) {
        List<RetentionPass> passes = new ArrayList<>(tenants.size());
        for (String tenant : tenants) {
            try {
                int deleted = retain(tenant, cutoff);
                log.info("event=request_ledger_retention tenant={} deleted={}", tenant, deleted);
                passes.add(new RetentionPass(tenant, deleted, false));
            } catch (RuntimeException e) {
                log.warn("event=request_ledger_retention_tenant_failed tenant={} reason={}", tenant, e.toString());
                passes.add(new RetentionPass(tenant, 0, true));
            }
        }
        return passes;
    }

    // ── internals ────────────────────────────────────────────────────────────

    private record LedgerRow(String route, byte[] bodySha256, String state, UUID holder, OffsetDateTime startedAt,
                             OffsetDateTime leaseUntil, OffsetDateTime finishedAt, Integer responseStatus,
                             String responseJson, boolean leaseLive) {
    }

    private LedgerRow read(String tenant, UUID requestId) {
        Field<Boolean> live = DSL.field(REQUEST_LEDGER.LEASE_UNTIL.gt(clock())).as("lease_live");
        return scope.withTenant(tenant, ctx -> ctx.select(
                    REQUEST_LEDGER.ROUTE, REQUEST_LEDGER.BODY_SHA256, REQUEST_LEDGER.STATE, REQUEST_LEDGER.HOLDER,
                    REQUEST_LEDGER.STARTED_AT, REQUEST_LEDGER.LEASE_UNTIL, REQUEST_LEDGER.FINISHED_AT,
                    REQUEST_LEDGER.RESPONSE_STATUS, REQUEST_LEDGER.RESPONSE, live)
                .from(REQUEST_LEDGER)
                .where(key(tenant, requestId))
                .fetchOptional(r -> new LedgerRow(
                    r.get(REQUEST_LEDGER.ROUTE), r.get(REQUEST_LEDGER.BODY_SHA256), r.get(REQUEST_LEDGER.STATE),
                    r.get(REQUEST_LEDGER.HOLDER), r.get(REQUEST_LEDGER.STARTED_AT),
                    r.get(REQUEST_LEDGER.LEASE_UNTIL), r.get(REQUEST_LEDGER.FINISHED_AT),
                    r.get(REQUEST_LEDGER.RESPONSE_STATUS),
                    r.get(REQUEST_LEDGER.RESPONSE) == null ? null : r.get(REQUEST_LEDGER.RESPONSE).data(),
                    Boolean.TRUE.equals(r.get(live)))))
            .orElse(null);
    }

    /** The tenant filter is explicit as well as enforced by RLS: a superuser pool bypasses RLS. */
    private static Condition key(String tenant, UUID requestId) {
        return REQUEST_LEDGER.TENANT_ID.eq(tenant).and(REQUEST_LEDGER.REQUEST_ID.eq(requestId));
    }

    private static Field<OffsetDateTime> clock() {
        return DSL.function("clock_timestamp", SQLDataType.TIMESTAMPWITHTIMEZONE);
    }

    private static Field<OffsetDateTime> leaseUntil(long leaseMs) {
        return clock().plus(DayToSecond.valueOf(Duration.ofMillis(leaseMs)));
    }
}
