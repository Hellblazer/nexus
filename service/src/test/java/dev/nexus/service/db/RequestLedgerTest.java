// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.2): {@link RequestLedger} claim, commit marker, failure
 * marker, status read and event counters on real Postgres.
 *
 * <p>Each test drives the handler-shaped sequence a keyed route will run (claim in its own
 * transaction, then the work and the commit marker in one transaction, the failure marker on an
 * exception before that commit), with a work callback that counts its invocations. The work's
 * "mutation" is an extra ledger row under a separate key, so a rollback is visible as that row's
 * absence without a second table.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RequestLedgerTest {

    private static final String TENANT = "ledger-component";
    private static final String ROUTE = "/v1/vectors/gc/quarantine-orphans";
    private static final String OTHER_ROUTE = "/v1/vectors/gc/expire-quarantine";
    private static final long BOUND_MS = PgSession.DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource ds;
    private TenantScope scope;
    private RequestLedger ledger;
    private ListAppender<ILoggingEvent> logs;
    private ch.qos.logback.classic.Logger logger;

    @BeforeAll
    void startAll() {
        pg = PgContainerHelper.start();
        ds = PgContainerHelper.superuserDataSource(pg);
        scope = new TenantScope(ds);
        ledger = new RequestLedger(scope, ds);
        logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(RequestLedger.class);
        logs = new ListAppender<>();
        logs.start();
        logger.addAppender(logs);
    }

    @AfterAll
    void stopAll() {
        if (logger != null) logger.detachAppender(logs);
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void resetEvents() {
        RequestLedgerActivity.resetForTests();
        logs.list.clear();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static byte[] sha(int seed) {
        byte[] b = new byte[32];
        b[0] = (byte) seed;
        return b;
    }

    /** The work's own mutation: an extra committed ledger row under {@code marker}. */
    private static void mutate(DSLContext ctx, UUID marker) {
        OffsetDateTime now = OffsetDateTime.now();
        ctx.insertInto(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.TENANT_ID, TENANT)
            .set(REQUEST_LEDGER.REQUEST_ID, marker)
            .set(REQUEST_LEDGER.ROUTE, "/test/mutation")
            .set(REQUEST_LEDGER.BODY_SHA256, sha(99))
            .set(REQUEST_LEDGER.STATE, "committed")
            .set(REQUEST_LEDGER.STARTED_AT, now)
            .set(REQUEST_LEDGER.FINISHED_AT, now)
            .execute();
    }

    private boolean exists(UUID requestId) {
        return scope.withTenant(TENANT, ctx -> ctx.fetchExists(
            ctx.selectOne().from(REQUEST_LEDGER)
                .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(requestId)))));
    }

    /**
     * The keyed-route sequence: claim; on Claimed run work + mutation + commit marker in one
     * transaction; the failure marker only for an exception before that commit.
     */
    private RequestLedger.Claim runKeyed(UUID key, String route, byte[] body, AtomicInteger work,
                                         boolean failWork) {
        RequestLedger.Claim claim = ledger.claim(TENANT, key, route, body, BOUND_MS);
        if (claim instanceof RequestLedger.Claimed c) {
            try {
                scope.withTenant(TENANT, ctx -> {
                    work.incrementAndGet();
                    if (failWork) {
                        throw new IllegalStateException("work failed");
                    }
                    mutate(ctx, UUID.randomUUID());
                    ledger.commitMarker(ctx, TENANT, key, c.nonce(), 200, "{\"moved\": 3}");
                    return null;
                });
            } catch (RuntimeException e) {
                ledger.failMarker(TENANT, key, c.nonce(), 500);
                if (!failWork) throw e;
            }
        }
        return claim;
    }

    private void forceLeaseExpired(UUID key) {
        scope.withTenant(TENANT, ctx -> ctx.update(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.LEASE_UNTIL, OffsetDateTime.now().minusSeconds(5))
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key)))
            .execute());
    }

    private List<String> eventLines(String event) {
        return logs.list.stream()
            .map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.startsWith("event=" + event + " "))
            .toList();
    }

    // ── claim ────────────────────────────────────────────────────────────────

    @Test
    void freshKey_claimInsertsInFlightWithLiveLease() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claim claim = ledger.claim(TENANT, key, ROUTE, sha(1), BOUND_MS);

        assertThat(claim).isInstanceOf(RequestLedger.Claimed.class);
        var row = scope.withTenant(TENANT, ctx -> ctx.select(
                    REQUEST_LEDGER.STATE, REQUEST_LEDGER.HOLDER, REQUEST_LEDGER.ROUTE,
                    DSL.field(REQUEST_LEDGER.LEASE_UNTIL.gt(DSL.currentOffsetDateTime())).as("live"))
                .from(REQUEST_LEDGER)
                .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key)))
                .fetchOne());
        assertThat(row).isNotNull();
        assertThat(row.get(REQUEST_LEDGER.STATE)).isEqualTo("in_flight");
        assertThat(row.get(REQUEST_LEDGER.HOLDER)).isEqualTo(((RequestLedger.Claimed) claim).nonce());
        assertThat(row.get(REQUEST_LEDGER.ROUTE)).isEqualTo(ROUTE);
        assertThat(row.get("live", Boolean.class)).as("lease is live right after the claim").isTrue();
        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.IN_FLIGHT);
    }

    @Test
    void committedKey_replaysStoredStatusAndBody_workDoesNotRun() {
        UUID key = UUID.randomUUID();
        AtomicInteger work = new AtomicInteger();
        assertThat(runKeyed(key, ROUTE, sha(2), work, false)).isInstanceOf(RequestLedger.Claimed.class);
        assertThat(work).hasValue(1);

        RequestLedger.Claim again = runKeyed(key, ROUTE, sha(2), work, false);

        assertThat(again).isInstanceOf(RequestLedger.Replay.class);
        RequestLedger.Replay replay = (RequestLedger.Replay) again;
        assertThat(replay.status()).isEqualTo(200);
        assertThat(JSONB.valueOf(replay.responseJson())).isEqualTo(JSONB.valueOf("{\"moved\": 3}"));
        assertThat(work).as("a replay never runs the work").hasValue(1);
    }

    @Test
    void liveInFlight_returnsInFlight_workDoesNotRun() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed first = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(3), BOUND_MS);
        AtomicInteger work = new AtomicInteger();

        RequestLedger.Claim second = runKeyed(key, ROUTE, sha(3), work, false);

        assertThat(second).isInstanceOf(RequestLedger.InFlight.class);
        RequestLedger.InFlight inFlight = (RequestLedger.InFlight) second;
        assertThat(inFlight.requestId()).isEqualTo(key);
        assertThat(inFlight.route()).isEqualTo(ROUTE);
        assertThat(inFlight.leaseUntil()).isAfter(inFlight.startedAt());
        assertThat(work).hasValue(0);
        assertThat(first.nonce()).isNotNull();
    }

    @Test
    void sameKeyChangedBody_isKeyReused() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(4), BOUND_MS);
        AtomicInteger work = new AtomicInteger();

        assertThat(runKeyed(key, ROUTE, sha(5), work, false)).isInstanceOf(RequestLedger.KeyReused.class);
        assertThat(work).hasValue(0);
    }

    @Test
    void sameKeySameBodyDifferentRoute_isKeyReused() {
        UUID key = UUID.randomUUID();
        AtomicInteger work = new AtomicInteger();
        runKeyed(key, ROUTE, sha(6), work, false);

        assertThat(runKeyed(key, OTHER_ROUTE, sha(6), work, false)).isInstanceOf(RequestLedger.KeyReused.class);
        assertThat(work).hasValue(1);
    }

    // ── takeover ─────────────────────────────────────────────────────────────

    @Test
    void expiredLease_takeoverSucceeds_workRunsOnce() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed dead = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(7), BOUND_MS);
        forceLeaseExpired(key);
        AtomicInteger work = new AtomicInteger();

        RequestLedger.Claim takeover = runKeyed(key, ROUTE, sha(7), work, false);

        assertThat(takeover).isInstanceOf(RequestLedger.Claimed.class);
        assertThat(((RequestLedger.Claimed) takeover).takeover()).isTrue();
        assertThat(((RequestLedger.Claimed) takeover).nonce()).isNotEqualTo(dead.nonce());
        assertThat(work).hasValue(1);
        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.COMMITTED);
    }

    @Test
    void failedRowWithNullHolder_takeoverSucceeds_workRunsOnce() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed first = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(8), BOUND_MS);
        ledger.failMarker(TENANT, key, first.nonce(), 503);
        UUID holder = scope.withTenant(TENANT, ctx -> ctx.select(REQUEST_LEDGER.HOLDER).from(REQUEST_LEDGER)
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key)))
            .fetchOne(REQUEST_LEDGER.HOLDER));
        assertThat(holder).as("the failure marker clears the holder").isNull();
        AtomicInteger work = new AtomicInteger();

        RequestLedger.Claim takeover = runKeyed(key, ROUTE, sha(8), work, false);

        assertThat(takeover).as("zxgld gap 1: a NULL holder must still be taken over")
            .isInstanceOf(RequestLedger.Claimed.class);
        assertThat(work).hasValue(1);
    }

    @Test
    void takeoverLostToAnotherAttempt_returnsInFlight() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(9), BOUND_MS);
        forceLeaseExpired(key);
        RequestLedger.Claim winner = ledger.claim(TENANT, key, ROUTE, sha(9), BOUND_MS);
        assertThat(winner).isInstanceOf(RequestLedger.Claimed.class);

        assertThat(ledger.claim(TENANT, key, ROUTE, sha(9), BOUND_MS))
            .as("the winner's fresh lease is live, so a later claim waits")
            .isInstanceOf(RequestLedger.InFlight.class);
    }

    // ── commit marker ────────────────────────────────────────────────────────

    @Test
    void leaseExpiredMidWork_commitMarkerThrows_andTheMutationRollsBack() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed c = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(10), BOUND_MS);
        forceLeaseExpired(key);
        UUID marker = UUID.randomUUID();

        assertThatThrownBy(() -> scope.withTenant(TENANT, ctx -> {
            mutate(ctx, marker);
            ledger.commitMarker(ctx, TENANT, key, c.nonce(), 200, "{}");
            return null;
        })).isInstanceOf(RequestLedger.LedgerMarkerLost.class);

        assertThat(exists(marker)).as("the late attempt's mutation rolled back").isFalse();
        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.ROLLED_BACK);
    }

    @Test
    void commitMarkerFromATakenOverHolder_throws() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed old = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(11), BOUND_MS);
        forceLeaseExpired(key);
        ledger.claim(TENANT, key, ROUTE, sha(11), BOUND_MS);

        assertThatThrownBy(() -> scope.withTenant(TENANT, ctx -> {
            ledger.commitMarker(ctx, TENANT, key, old.nonce(), 200, "{}");
            return null;
        })).isInstanceOf(RequestLedger.LedgerMarkerLost.class);
    }

    @Test
    void failedThenResend_workExecutesExactlyOnce() {
        UUID key = UUID.randomUUID();
        AtomicInteger failedAttempts = new AtomicInteger();
        runKeyed(key, ROUTE, sha(12), failedAttempts, true);
        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.ROLLED_BACK);

        AtomicInteger work = new AtomicInteger();
        assertThat(runKeyed(key, ROUTE, sha(12), work, false)).isInstanceOf(RequestLedger.Claimed.class);
        assertThat(runKeyed(key, ROUTE, sha(12), work, false)).isInstanceOf(RequestLedger.Replay.class);
        assertThat(runKeyed(key, ROUTE, sha(12), work, false)).isInstanceOf(RequestLedger.Replay.class);

        assertThat(work).as("the work commits once across the resends").hasValue(1);
    }

    @Test
    void committedThenExceptionAfterCommit_stillReadsCommitted() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed c = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(13), BOUND_MS);
        scope.withTenant(TENANT, ctx -> {
            ledger.commitMarker(ctx, TENANT, key, c.nonce(), 200, "{\"moved\": 1}");
            return null;
        });
        // The response write to a client the edge already cut throws after the commit. A caller
        // wraps only the work up to the commit, but even a stray failure marker is a no-op here.
        ledger.failMarker(TENANT, key, c.nonce(), 500);

        RequestLedger.Status status = ledger.status(TENANT, key);
        assertThat(status.kind()).isEqualTo(RequestLedger.StatusKind.COMMITTED);
        assertThat(status.responseStatus()).isEqualTo(200);
        assertThat(JSONB.valueOf(status.responseJson())).isEqualTo(JSONB.valueOf("{\"moved\": 1}"));
    }

    @Test
    void finishedAt_comesFromClockTimestamp_notTransactionStart() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed c = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(14), BOUND_MS);
        OffsetDateTime txStart = scope.withTenant(TENANT, ctx -> {
            OffsetDateTime start = ctx.select(DSL.currentOffsetDateTime()).fetchOne(0, OffsetDateTime.class);
            try {
                Thread.sleep(200);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(e);
            }
            ledger.commitMarker(ctx, TENANT, key, c.nonce(), 200, "{}");
            return start;
        });

        OffsetDateTime finishedAt = ledger.status(TENANT, key).finishedAt();
        assertThat(Duration.between(txStart, finishedAt))
            .as("finished_at is clock_timestamp(), so it trails now() by the 200 ms sleep")
            .isGreaterThanOrEqualTo(Duration.ofMillis(200));
    }

    // ── failure marker ───────────────────────────────────────────────────────

    @Test
    void failMarkerByAStaleHolder_doesNotTouchTheNewHoldersRow() {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed old = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(15), BOUND_MS);
        forceLeaseExpired(key);
        ledger.claim(TENANT, key, ROUTE, sha(15), BOUND_MS);

        ledger.failMarker(TENANT, key, old.nonce(), 500);

        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.IN_FLIGHT);
    }

    // ── status ───────────────────────────────────────────────────────────────

    @Test
    void status_unknownKey_andOtherTenant_readUnknown() {
        UUID key = UUID.randomUUID();
        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.UNKNOWN);
        ledger.claim(TENANT, key, ROUTE, sha(16), BOUND_MS);
        assertThat(ledger.status("some-other-tenant", key).kind()).isEqualTo(RequestLedger.StatusKind.UNKNOWN);
    }

    @Test
    void status_expiredInFlight_readsRolledBack() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(17), BOUND_MS);
        forceLeaseExpired(key);
        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.ROLLED_BACK);
    }

    // ── lease ────────────────────────────────────────────────────────────────

    @Test
    void lease_isBoundPlusTwoConnectionTimeoutsPlusTenSeconds_forANonDefaultPool() {
        HikariConfig cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(2);
        cfg.setConnectionTimeout(7_000);
        try (HikariDataSource custom = new HikariDataSource(cfg)) {
            RequestLedger l = new RequestLedger(new TenantScope(custom), custom);
            assertThat(l.leaseMs(BOUND_MS)).isEqualTo(BOUND_MS + 2 * 7_000L + 10_000L);

            UUID key = UUID.randomUUID();
            l.claim(TENANT, key, ROUTE, sha(18), BOUND_MS);
            Duration stored = scope.withTenant(TENANT, ctx -> {
                var r = ctx.select(REQUEST_LEDGER.STARTED_AT, REQUEST_LEDGER.LEASE_UNTIL).from(REQUEST_LEDGER)
                    .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key))).fetchOne();
                return Duration.between(r.value1(), r.value2());
            });
            assertThat(stored.toMillis()).isBetween(l.leaseMs(BOUND_MS) - 50, l.leaseMs(BOUND_MS) + 50);
        }
    }

    @Test
    void lease_forAKeyedSweepOnTheDefaultPool_is95Seconds() {
        // Sam's ruling 2026-10-18: statement bound + 2 x connectionTimeout + 10 s, with the 30 s
        // Hikari default, derived from the PgSession constant rather than a literal.
        HikariConfig cfg = new HikariConfig();
        assertThat(ledger.leaseMs(PgSession.DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS))
            .isEqualTo(PgSession.DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS
                + 2 * cfg.getConnectionTimeout() + 10_000L)
            .isEqualTo(95_000L);
    }

    // ── events ───────────────────────────────────────────────────────────────

    @Test
    void eachEvent_incrementsItsCounterByOne_andLogsOneLine() {
        AtomicInteger work = new AtomicInteger();

        UUID replayKey = UUID.randomUUID();
        runKeyed(replayKey, ROUTE, sha(20), work, false);
        runKeyed(replayKey, ROUTE, sha(20), work, false);

        UUID inFlightKey = UUID.randomUUID();
        ledger.claim(TENANT, inFlightKey, ROUTE, sha(21), BOUND_MS);
        ledger.claim(TENANT, inFlightKey, ROUTE, sha(21), BOUND_MS);

        UUID reusedKey = UUID.randomUUID();
        ledger.claim(TENANT, reusedKey, ROUTE, sha(22), BOUND_MS);
        ledger.claim(TENANT, reusedKey, ROUTE, sha(23), BOUND_MS);

        UUID takeoverKey = UUID.randomUUID();
        ledger.claim(TENANT, takeoverKey, ROUTE, sha(24), BOUND_MS);
        forceLeaseExpired(takeoverKey);
        ledger.claim(TENANT, takeoverKey, ROUTE, sha(24), BOUND_MS);

        RequestLedgerActivity.Snapshot s = RequestLedgerActivity.snapshot();
        assertThat(s.replays()).isEqualTo(1);
        assertThat(s.inFlight()).isEqualTo(1);
        assertThat(s.keyReused()).isEqualTo(1);
        assertThat(s.takeovers()).isEqualTo(1);

        assertThat(eventLines("request_ledger_replay")).singleElement().asString()
            .contains("tenant=" + TENANT).contains("request_id=" + replayKey).contains("route=" + ROUTE);
        assertThat(eventLines("request_ledger_in_flight")).singleElement().asString()
            .contains("request_id=" + inFlightKey);
        assertThat(eventLines("request_ledger_key_reused")).singleElement().asString()
            .contains("request_id=" + reusedKey);
        assertThat(eventLines("request_ledger_takeover")).singleElement().asString()
            .contains("request_id=" + takeoverKey);
    }

    @Test
    void claim_rejectsAFingerprintThatIsNot32Bytes() {
        assertThatThrownBy(() -> ledger.claim(TENANT, UUID.randomUUID(), ROUTE, new byte[31], BOUND_MS))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── peekCommitted: the ledger's answer for a committed key, with nothing claimed ──

    /** Every ledger row, with the columns a peek must leave alone (state, holder, lease, finish, status, response). */
    private List<java.util.Map<String, Object>> ledgerSnapshot() {
        return scope.withTenant(TENANT, ctx -> ctx.select(REQUEST_LEDGER.TENANT_ID, REQUEST_LEDGER.REQUEST_ID,
                REQUEST_LEDGER.ROUTE, REQUEST_LEDGER.STATE, REQUEST_LEDGER.HOLDER, REQUEST_LEDGER.STARTED_AT,
                REQUEST_LEDGER.LEASE_UNTIL, REQUEST_LEDGER.FINISHED_AT, REQUEST_LEDGER.RESPONSE_STATUS,
                REQUEST_LEDGER.RESPONSE)
            .from(REQUEST_LEDGER).orderBy(REQUEST_LEDGER.TENANT_ID, REQUEST_LEDGER.REQUEST_ID).fetch().intoMaps());
    }

    /** Peek and assert the table is exactly as it was: same rows, and each row's state, holder and lease. */
    private RequestLedger.Claim peekLeavingTableUnchanged(String tenant, UUID key, String route, byte[] body) {
        var before = ledgerSnapshot();
        RequestLedger.Claim answer = ledger.peekCommitted(tenant, key, route, body);
        assertThat(ledgerSnapshot()).as("peekCommitted writes nothing").isEqualTo(before);
        return answer;
    }

    @Test
    void peekCommitted_isNullForNoRow_aLiveInFlightRow_andAFailedRow() {
        UUID absent = UUID.randomUUID();
        UUID inFlight = UUID.randomUUID();
        UUID failed = UUID.randomUUID();
        ledger.claim(TENANT, inFlight, ROUTE, sha(30), BOUND_MS);
        var f = (RequestLedger.Claimed) ledger.claim(TENANT, failed, ROUTE, sha(31), BOUND_MS);
        ledger.failMarker(TENANT, failed, f.nonce(), 500);
        RequestLedgerActivity.resetForTests();

        assertThat(peekLeavingTableUnchanged(TENANT, absent, ROUTE, sha(32))).as("no row").isNull();
        assertThat(exists(absent)).as("and a peek of an absent key creates none").isFalse();
        assertThat(peekLeavingTableUnchanged(TENANT, inFlight, ROUTE, sha(30))).as("in_flight").isNull();
        assertThat(peekLeavingTableUnchanged(TENANT, failed, ROUTE, sha(31))).as("failed").isNull();
        // An in_flight row whose lease has run out is still not committed.
        forceLeaseExpired(inFlight);
        assertThat(peekLeavingTableUnchanged(TENANT, inFlight, ROUTE, sha(30))).as("lease-expired in_flight").isNull();
        var counts = RequestLedgerActivity.snapshot();
        assertThat(counts.replays()).isZero();
        assertThat(counts.keyReused()).isZero();
    }

    @Test
    void peekCommitted_isAReplayForACommittedKeyWithTheSameRouteAndBody() {
        UUID key = UUID.randomUUID();
        assertThat(runKeyed(key, ROUTE, sha(33), new AtomicInteger(), false)).isInstanceOf(RequestLedger.Claimed.class);
        RequestLedgerActivity.resetForTests();

        RequestLedger.Claim answer = peekLeavingTableUnchanged(TENANT, key, ROUTE, sha(33));

        assertThat(answer).isInstanceOf(RequestLedger.Replay.class);
        var replay = (RequestLedger.Replay) answer;
        assertThat(replay.requestId()).isEqualTo(key);
        assertThat(replay.status()).isEqualTo(200);
        assertThat(JSONB.valueOf(replay.responseJson())).isEqualTo(JSONB.valueOf("{\"moved\": 3}"));
        assertThat(RequestLedgerActivity.snapshot().replays()).as("counted as a replay, like a claim's").isEqualTo(1);
        assertThat(eventLines("request_ledger_replay")).hasSize(1);
    }

    @Test
    void peekCommitted_isKeyReusedForACommittedKeyWithAnotherRouteOrBody() {
        UUID key = UUID.randomUUID();
        assertThat(runKeyed(key, ROUTE, sha(34), new AtomicInteger(), false)).isInstanceOf(RequestLedger.Claimed.class);
        RequestLedgerActivity.resetForTests();

        RequestLedger.Claim otherBody = peekLeavingTableUnchanged(TENANT, key, ROUTE, sha(35));
        RequestLedger.Claim otherRoute = peekLeavingTableUnchanged(TENANT, key, OTHER_ROUTE, sha(34));

        assertThat(otherBody).isInstanceOf(RequestLedger.KeyReused.class);
        assertThat(otherRoute).isInstanceOf(RequestLedger.KeyReused.class);
        assertThat(((RequestLedger.KeyReused) otherRoute).storedRoute()).isEqualTo(ROUTE);
        assertThat(RequestLedgerActivity.snapshot().keyReused()).isEqualTo(2);
        assertThat(RequestLedgerActivity.snapshot().replays()).isZero();
    }

    @Test
    void peekCommitted_readsAnotherTenantsCommittedKeyAsNoRow() {
        UUID key = UUID.randomUUID();
        assertThat(runKeyed(key, ROUTE, sha(36), new AtomicInteger(), false)).isInstanceOf(RequestLedger.Claimed.class);
        RequestLedgerActivity.resetForTests();

        assertThat(peekLeavingTableUnchanged("some-other-tenant", key, ROUTE, sha(36))).isNull();
        assertThat(RequestLedgerActivity.snapshot().replays()).isZero();
    }

    // ── nexus-yye8l.19: rolled_back is stable in the marker-to-COMMIT window ──

    private void shortenLease(UUID key, long millisFromNow) {
        scope.withTenant(TENANT, ctx -> ctx.update(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.LEASE_UNTIL, OffsetDateTime.now().plus(Duration.ofMillis(millisFromNow)))
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key)))
            .execute());
    }

    private static void sleepPast(OffsetDateTime instant) throws InterruptedException {
        while (OffsetDateTime.now().isBefore(instant.plus(Duration.ofMillis(300)))) {
            Thread.sleep(50);
        }
    }

    private OffsetDateTime leaseOf(UUID key) {
        return scope.withTenant(TENANT, ctx -> ctx.select(REQUEST_LEDGER.LEASE_UNTIL).from(REQUEST_LEDGER)
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key)))
            .fetchOne(REQUEST_LEDGER.LEASE_UNTIL));
    }

    @Test
    void statusDuringAMarkerTransactionHeldPastTheLease_saysInFlight_thenCommitted() throws Exception {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed c = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(40), BOUND_MS);
        shortenLease(key, 1_500);
        OffsetDateTime lease = leaseOf(key);
        try (java.sql.Connection tx = pg.createConnection("")) {
            tx.setAutoCommit(false);
            // The marker UPDATE runs while the lease is live and its row lock stays held, uncommitted.
            ledger.commitMarker(DSL.using(tx, org.jooq.SQLDialect.POSTGRES), TENANT, key, c.nonce(), 200,
                "{\"moved\": 1}");
            sleepPast(lease);

            assertThat(ledger.status(TENANT, key).kind())
                .as("the marker is about to commit: an expired lease must not read rolled_back")
                .isEqualTo(RequestLedger.StatusKind.IN_FLIGHT);

            tx.commit();
        }
        assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.COMMITTED);
    }

    @Test
    void statusAfterAMarkerTransactionHeldPastTheLeaseRollsBack_readsRolledBack() throws Exception {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed c = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(41), BOUND_MS);
        shortenLease(key, 1_500);
        OffsetDateTime lease = leaseOf(key);
        try (java.sql.Connection tx = pg.createConnection("")) {
            tx.setAutoCommit(false);
            ledger.commitMarker(DSL.using(tx, org.jooq.SQLDialect.POSTGRES), TENANT, key, c.nonce(), 200, "{}");
            sleepPast(lease);
            assertThat(ledger.status(TENANT, key).kind()).isEqualTo(RequestLedger.StatusKind.IN_FLIGHT);

            tx.rollback();
        }
        assertThat(ledger.status(TENANT, key).kind())
            .as("the marker never committed and its lease is gone: now rolled_back is final")
            .isEqualTo(RequestLedger.StatusKind.ROLLED_BACK);
    }

    @Test
    void claimDuringAMarkerTransactionHeldPastTheLease_waitsForTheCommitWithinTheBound_thenReplays_neverTakesOver()
            throws Exception {
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed c = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(42), BOUND_MS);
        shortenLease(key, 1_500);
        OffsetDateTime lease = leaseOf(key);
        var pool = java.util.concurrent.Executors.newVirtualThreadPerTaskExecutor();
        try (java.sql.Connection tx = pg.createConnection("")) {
            tx.setAutoCommit(false);
            ledger.commitMarker(DSL.using(tx, org.jooq.SQLDialect.POSTGRES), TENANT, key, c.nonce(), 200, "{}");
            sleepPast(lease);

            // The claim's INSERT ... ON CONFLICT waits on the marker transaction's uncommitted update of the
            // row, so a resend cannot take the row over underneath a marker that is about to commit.
            var resend = pool.submit(() -> ledger.claim(TENANT, key, ROUTE, sha(42), BOUND_MS));
            Thread.sleep(700);
            assertThat(resend.isDone()).as("the resend waits for the marker transaction").isFalse();
            // The commit below lands at ~700 ms, inside RequestLedger.CLAIM_LOCK_TIMEOUT_MS (2 s): this test is
            // the wait-then-replay side of the bound; the next test is the timeout side.

            tx.commit();
            assertThat(resend.get(10, java.util.concurrent.TimeUnit.SECONDS))
                .as("after the commit the key replays; a takeover would run the sweep twice")
                .isInstanceOf(RequestLedger.Replay.class);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void claimDuringAMarkerTransactionHeldPastTheLockBound_isContendedWhileTheMarkerIsStillOpen_andWritesNothing()
            throws Exception {
        // nexus-yye8l.22: a marker transaction that stalls between its UPDATE and its COMMIT used to hold the
        // resend's admission permit and connection for as long as the stall lasted. The claim now runs under
        // lock_timeout, and the trip is the contended outcome (503 request_claim_contended), not a hang.
        UUID key = UUID.randomUUID();
        RequestLedger.Claimed c = (RequestLedger.Claimed) ledger.claim(TENANT, key, ROUTE, sha(49), BOUND_MS);
        shortenLease(key, 1_500);
        OffsetDateTime lease = leaseOf(key);
        try (java.sql.Connection tx = pg.createConnection("")) {
            tx.setAutoCommit(false);
            ledger.commitMarker(DSL.using(tx, org.jooq.SQLDialect.POSTGRES), TENANT, key, c.nonce(), 200, "{}");
            sleepPast(lease);

            long start = System.nanoTime();
            assertThatThrownBy(() -> ledger.claim(TENANT, key, ROUTE, sha(49), BOUND_MS))
                .as("the marker transaction is still open: the claim gives up at the bound, it does not hang")
                .isInstanceOf(RequestLedger.ClaimContendedException.class)
                .hasMessageContaining(key.toString());
            long elapsedMs = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

            assertThat(elapsedMs).as("it waited the lock bound (a trip before it would mean another cause)")
                .isGreaterThanOrEqualTo(RequestLedger.CLAIM_LOCK_TIMEOUT_MS - 250L);
            assertThat(elapsedMs).as("and no longer than the bound plus a pool borrow and a round trip")
                .isLessThan(RequestLedger.CLAIM_LOCK_TIMEOUT_MS + 5_000L);

            tx.commit();
        }
        assertThat(ledger.claim(TENANT, key, ROUTE, sha(49), BOUND_MS))
            .as("the marker committed after the timed-out claim: the resend with the same key replays")
            .isInstanceOf(RequestLedger.Replay.class);
    }

    @Test
    void aRowLockedAfterTheInsertConflictAndBeforeTheRead_isInFlight_notTakenOver() throws Exception {
        // The race the probe in read() closes for claim(): the INSERT's conflict saw no lock, then a marker
        // transaction (which matched while the lease was live) takes the row before the claim's read.
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(48), BOUND_MS);
        forceLeaseExpired(key);
        try (java.sql.Connection tx = pg.createConnection("")) {
            tx.setAutoCommit(false);
            ledger.setClaimPhaseHookForTests((phase, round) -> {
                if (phase == RequestLedger.ClaimPhase.AFTER_INSERT_CONFLICT && round == 0) {
                    DSL.using(tx, org.jooq.SQLDialect.POSTGRES).select(REQUEST_LEDGER.REQUEST_ID)
                        .from(REQUEST_LEDGER)
                        .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key)))
                        .forUpdate().fetch();
                }
            });
            try {
                RequestLedger.Claim claim = ledger.claim(TENANT, key, ROUTE, sha(48), BOUND_MS);

                assertThat(claim).isInstanceOf(RequestLedger.InFlight.class);
                assertThat(((RequestLedger.InFlight) claim).leaseUntil())
                    .as("the lease is behind the reading: held, not live")
                    .isBefore(((RequestLedger.InFlight) claim).observedAt());
            } finally {
                ledger.setClaimPhaseHookForTests(null);
                tx.rollback();
            }
        }
    }

    // ── nexus-yye8l.20: claim() branches that need an interleaving ───────────

    private void setHolder(UUID key, UUID holder) {
        scope.withTenant(TENANT, ctx -> ctx.update(REQUEST_LEDGER).set(REQUEST_LEDGER.HOLDER, holder)
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key))).execute());
    }

    private UUID holderOf(UUID key) {
        return scope.withTenant(TENANT, ctx -> ctx.select(REQUEST_LEDGER.HOLDER).from(REQUEST_LEDGER)
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key)))
            .fetchOne(REQUEST_LEDGER.HOLDER));
    }

    @Test
    void aTakeoverLostToAnotherAttemptBetweenTheReadAndTheUpdate_isReclassifiedAsInFlight() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(43), BOUND_MS);
        forceLeaseExpired(key);
        UUID rival = UUID.randomUUID();
        ledger.setClaimPhaseHookForTests((phase, round) -> {
            if (phase == RequestLedger.ClaimPhase.BEFORE_TAKEOVER && round == 0) {
                // The rival takes the row between this attempt's read and its takeover UPDATE.
                setHolder(key, rival);
                shortenLease(key, 60_000);
            }
        });
        try {
            RequestLedger.Claim claim = ledger.claim(TENANT, key, ROUTE, sha(43), BOUND_MS);

            assertThat(claim).as("taken == 0 re-runs the round, which now sees the rival's live lease")
                .isInstanceOf(RequestLedger.InFlight.class);
            assertThat(holderOf(key)).as("the loser did not overwrite the rival").isEqualTo(rival);
        } finally {
            ledger.setClaimPhaseHookForTests(null);
        }
    }

    @Test
    void aRowDeletedBetweenTheInsertConflictAndTheRead_isClaimedOnTheNextRound() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(44), BOUND_MS);
        ledger.setClaimPhaseHookForTests((phase, round) -> {
            if (phase == RequestLedger.ClaimPhase.AFTER_INSERT_CONFLICT && round == 0) {
                // The retention arm purges the row between the conflict and the read.
                scope.withTenant(TENANT, ctx -> ctx.deleteFrom(REQUEST_LEDGER)
                    .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key))).execute());
            }
        });
        try {
            RequestLedger.Claim claim = ledger.claim(TENANT, key, ROUTE, sha(44), BOUND_MS);

            assertThat(claim).isInstanceOf(RequestLedger.Claimed.class);
            assertThat(((RequestLedger.Claimed) claim).takeover()).as("a fresh insert, not a takeover").isFalse();
            assertThat(holderOf(key)).isEqualTo(((RequestLedger.Claimed) claim).nonce());
        } finally {
            ledger.setClaimPhaseHookForTests(null);
        }
    }

    @Test
    void aClaimThatNeverSettles_throwsClaimContended_notAnIllegalState() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(45), BOUND_MS);
        forceLeaseExpired(key);
        ledger.setClaimPhaseHookForTests((phase, round) -> {
            if (phase == RequestLedger.ClaimPhase.BEFORE_TAKEOVER) {
                setHolder(key, UUID.randomUUID()); // the lease stays expired, so every round tries and loses
            }
        });
        try {
            assertThatThrownBy(() -> ledger.claim(TENANT, key, ROUTE, sha(45), BOUND_MS))
                .isInstanceOf(RequestLedger.ClaimContendedException.class)
                .isNotInstanceOf(IllegalStateException.class)
                .hasMessageContaining(key.toString());
        } finally {
            ledger.setClaimPhaseHookForTests(null);
        }
    }

    @Test
    void aTakeover_refreshesStartedAtAlongsideTheFreshLease() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(46), BOUND_MS);
        forceLeaseExpired(key);
        OffsetDateTime longAgo = OffsetDateTime.now().minusHours(1);
        scope.withTenant(TENANT, ctx -> ctx.update(REQUEST_LEDGER).set(REQUEST_LEDGER.STARTED_AT, longAgo)
            .where(REQUEST_LEDGER.TENANT_ID.eq(TENANT).and(REQUEST_LEDGER.REQUEST_ID.eq(key))).execute());

        RequestLedger.Claim takeover = ledger.claim(TENANT, key, ROUTE, sha(46), BOUND_MS);

        assertThat(takeover).isInstanceOf(RequestLedger.Claimed.class);
        RequestLedger.Status status = ledger.status(TENANT, key);
        assertThat(status.startedAt()).as("started_at belongs to the attempt that holds the lease")
            .isAfter(OffsetDateTime.now().minusMinutes(1));
        assertThat(status.leaseUntil()).isAfter(status.startedAt());
    }

    @Test
    void theLeaseMarginAndTheAdmissionWaitReadTheSameConnectionTimeout_byValue_underAChangedSetting() {
        // Two independent observations of one configured value: the admission wait TenantScope applies and the
        // wait the lease margin is built from (two of them plus the slack). The expected value is the setting
        // the test chose, not either side's own reading, so a margin that stopped reading it, or a wait that
        // read a different source, fails here.
        HikariConfig cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(2);
        cfg.setConnectionTimeout(7_000);
        try (HikariDataSource custom = new HikariDataSource(cfg)) {
            TenantScope customScope = new TenantScope(custom);
            RequestLedger l = new RequestLedger(customScope, custom);

            assertThat(customScope.admissionTimeoutMs()).as("admission wait").isEqualTo(7_000L);
            assertThat((l.leaseMs(0) - RequestLedger.LEASE_SLACK_MS) / 2).as("wait inside the lease margin")
                .isEqualTo(7_000L);
        }

        // A DataSource that is not a Hikari pool: both fall back to the one default TenantScope owns.
        javax.sql.DataSource plain = new org.postgresql.ds.PGSimpleDataSource();
        TenantScope plainScope = new TenantScope(plain);
        RequestLedger lp = new RequestLedger(plainScope, plain);
        assertThat(plainScope.admissionTimeoutMs()).isEqualTo(30_000L);
        assertThat((lp.leaseMs(0) - RequestLedger.LEASE_SLACK_MS) / 2).isEqualTo(30_000L);
    }

    @Test
    void anInFlightAnswerCarriesTheDatabaseClockReading_soRetryAfterNeedsNoJvmClock() {
        UUID key = UUID.randomUUID();
        ledger.claim(TENANT, key, ROUTE, sha(47), BOUND_MS);

        RequestLedger.InFlight inFlight = (RequestLedger.InFlight) ledger.claim(TENANT, key, ROUTE, sha(47), BOUND_MS);

        long remainingMs = Duration.between(inFlight.observedAt(), inFlight.leaseUntil()).toMillis();
        assertThat(remainingMs).as("lease left at the database's own reading")
            .isBetween(ledger.leaseMs(BOUND_MS) - 5_000, ledger.leaseMs(BOUND_MS));
        // This test shares the JVM's host clock with the container, so it cannot tell a database reading from
        // a JVM one by value; the Retry-After arithmetic is pinned against instants no host clock can match in
        // KeyedSweepRetryAfterTest (nexus-yye8l.22).
    }
}
