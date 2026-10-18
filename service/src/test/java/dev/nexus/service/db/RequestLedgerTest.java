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
}
