// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Function;

import static dev.nexus.service.jooq.nexus.Tables.REQUEST_LEDGER;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.3): the request-ledger retention arm on real Postgres.
 * Rows are seeded directly at fixed instants and {@code retain} is handed a fixed cutoff, so no
 * test reads or waits on a clock.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RequestLedgerRetentionTest {

    private static final OffsetDateTime NOW = OffsetDateTime.of(2026, 10, 18, 12, 0, 0, 0, ZoneOffset.UTC);
    private static final OffsetDateTime CUTOFF = NOW.minusDays(7);

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
    void clearLogs() {
        logs.list.clear();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private UUID seed(String tenant, String state, OffsetDateTime startedAt, OffsetDateTime leaseUntil,
                      OffsetDateTime finishedAt) {
        UUID id = UUID.randomUUID();
        byte[] sha = new byte[32];
        scope.withTenant(tenant, ctx -> ctx.insertInto(REQUEST_LEDGER)
            .set(REQUEST_LEDGER.TENANT_ID, tenant)
            .set(REQUEST_LEDGER.REQUEST_ID, id)
            .set(REQUEST_LEDGER.ROUTE, "/v1/vectors/gc/quarantine-orphans")
            .set(REQUEST_LEDGER.BODY_SHA256, sha)
            .set(REQUEST_LEDGER.STATE, state)
            .set(REQUEST_LEDGER.STARTED_AT, startedAt)
            .set(REQUEST_LEDGER.LEASE_UNTIL, leaseUntil)
            .set(REQUEST_LEDGER.FINISHED_AT, finishedAt)
            .execute());
        return id;
    }

    private UUID terminal(String tenant, String state, OffsetDateTime finishedAt) {
        return seed(tenant, state, finishedAt.minusMinutes(1), null, finishedAt);
    }

    private UUID inFlight(String tenant, OffsetDateTime startedAt, OffsetDateTime leaseUntil) {
        return seed(tenant, "in_flight", startedAt, leaseUntil, null);
    }

    private boolean exists(String tenant, UUID id) {
        return scope.withTenant(tenant, ctx -> ctx.fetchExists(
            ctx.selectOne().from(REQUEST_LEDGER)
                .where(REQUEST_LEDGER.TENANT_ID.eq(tenant).and(REQUEST_LEDGER.REQUEST_ID.eq(id)))));
    }

    private List<ILoggingEvent> logged(Level level, String needle) {
        return logs.list.stream()
            .filter(e -> e.getLevel() == level && e.getFormattedMessage().contains(needle))
            .toList();
    }

    private static Function<String, String> env(String value) {
        Map<String, String> m = new HashMap<>();
        if (value != null) m.put(RequestLedger.RETENTION_ENV, value);
        return m::get;
    }

    // ── what a pass deletes ──────────────────────────────────────────────────

    @Test
    void aPassDeletesOldTerminalRowsAndInFlightRowsExpiredBeforeTheCutoff_andNothingElse() {
        String t = "retain-predicate";
        UUID committedOld = terminal(t, "committed", CUTOFF.minusHours(1));
        UUID committedFresh = terminal(t, "committed", CUTOFF.plusHours(1));
        UUID failedOld = terminal(t, "failed", CUTOFF.minusHours(1));
        UUID failedFresh = terminal(t, "failed", CUTOFF.plusHours(1));
        // Lease ran out before the cutoff: the attempt is long dead, the row is garbage.
        UUID expiredBefore = inFlight(t, CUTOFF.minusDays(2), CUTOFF.minusHours(1));
        // Lease ran out after the cutoff: expired, but inside the retention window, so it stays.
        UUID expiredAfter = inFlight(t, CUTOFF.minusDays(2), CUTOFF.plusHours(1));
        // Live lease, started_at older than the cutoff: must never go, whatever started_at says.
        UUID liveLease = inFlight(t, CUTOFF.minusDays(30), NOW.plusHours(1));

        int deleted = ledger.retain(t, CUTOFF);

        assertThat(deleted).isEqualTo(3);
        assertThat(exists(t, committedOld)).isFalse();
        assertThat(exists(t, failedOld)).isFalse();
        assertThat(exists(t, expiredBefore)).isFalse();
        assertThat(exists(t, committedFresh)).isTrue();
        assertThat(exists(t, failedFresh)).isTrue();
        assertThat(exists(t, expiredAfter)).isTrue();
        assertThat(exists(t, liveLease)).isTrue();
    }

    @Test
    void aSecondPassOverTheSameCutoffDeletesNothing() {
        String t = "retain-idempotent";
        terminal(t, "committed", CUTOFF.minusHours(1));

        assertThat(ledger.retain(t, CUTOFF)).isEqualTo(1);
        assertThat(ledger.retain(t, CUTOFF)).isZero();
    }

    @Test
    void aPassDeletesOnlyItsOwnTenantsRows() {
        String a = "retain-tenant-a";
        String b = "retain-tenant-b";
        UUID aOld = terminal(a, "committed", CUTOFF.minusHours(1));
        UUID aFresh = terminal(a, "committed", CUTOFF.plusHours(1));
        UUID bOld = terminal(b, "failed", CUTOFF.minusHours(1));
        UUID bExpired = inFlight(b, CUTOFF.minusDays(2), CUTOFF.minusHours(1));

        assertThat(ledger.retain(a, CUTOFF)).isEqualTo(1);

        assertThat(exists(a, aOld)).isFalse();
        assertThat(exists(a, aFresh)).isTrue();
        assertThat(exists(b, bOld)).as("tenant b's old row survives a's pass").isTrue();
        assertThat(exists(b, bExpired)).as("tenant b's expired in_flight row survives a's pass").isTrue();

        assertThat(ledger.retain(b, CUTOFF)).isEqualTo(2);
        assertThat(exists(b, bOld)).isFalse();
        assertThat(exists(b, bExpired)).isFalse();
        assertThat(exists(a, aFresh)).as("and b's pass leaves a's fresh row").isTrue();
    }

    // ── the per-tenant loop ──────────────────────────────────────────────────

    @Test
    void retainAllLogsOneInfoLinePerTenantPass() {
        String a = "retain-all-a";
        String b = "retain-all-b";
        terminal(a, "committed", CUTOFF.minusHours(1));
        terminal(a, "failed", CUTOFF.minusHours(2));

        List<RequestLedger.RetentionPass> passes = ledger.retainAll(List.of(a, b), CUTOFF);

        assertThat(passes).extracting(RequestLedger.RetentionPass::tenant).containsExactly(a, b);
        assertThat(passes).extracting(RequestLedger.RetentionPass::deleted).containsExactly(2, 0);
        assertThat(passes).allMatch(p -> !p.failed());
        assertThat(logged(Level.INFO, "event=request_ledger_retention ")).hasSize(2);
        assertThat(logged(Level.INFO, "event=request_ledger_retention tenant=" + a + " deleted=2")).hasSize(1);
        assertThat(logged(Level.INFO, "event=request_ledger_retention tenant=" + b + " deleted=0")).hasSize(1);
    }

    @Test
    void aTenantsFailureIsLoggedAtWarn_andTheNextTenantStillRuns() {
        String good = "retain-after-failure";
        UUID old = terminal(good, "committed", CUTOFF.minusHours(1));

        // A blank tenant is rejected by TenantScope before any SQL: a failure confined to one pass.
        List<RequestLedger.RetentionPass> passes = ledger.retainAll(List.of(" ", good), CUTOFF);

        assertThat(passes).hasSize(2);
        assertThat(passes.get(0).failed()).isTrue();
        assertThat(passes.get(0).deleted()).isZero();
        assertThat(passes.get(1).failed()).isFalse();
        assertThat(passes.get(1).deleted()).isEqualTo(1);
        assertThat(exists(good, old)).isFalse();
        assertThat(logged(Level.WARN, "event=request_ledger_retention_tenant_failed")).hasSize(1);
        assertThat(logged(Level.INFO, "event=request_ledger_retention tenant=" + good + " deleted=1")).hasSize(1);
    }

    // ── the retention setting ────────────────────────────────────────────────

    @Test
    void retentionDefaultsToSevenDays_whenUnset_withoutAWarning() {
        assertThat(RequestLedger.resolveRetentionDays(env(null))).isEqualTo(7);
        assertThat(RequestLedger.resolveRetentionDays(env(""))).isEqualTo(7);
        assertThat(RequestLedger.resolveRetentionDays(env("   "))).isEqualTo(7);
        assertThat(logged(Level.WARN, RequestLedger.RETENTION_ENV)).isEmpty();
    }

    @Test
    void aPositiveIntegerIsTheRetention() {
        assertThat(RequestLedger.resolveRetentionDays(env("30"))).isEqualTo(30);
        assertThat(RequestLedger.resolveRetentionDays(env(" 3 "))).isEqualTo(3);
        assertThat(logged(Level.WARN, RequestLedger.RETENTION_ENV)).isEmpty();
    }

    @Test
    void anUnparseableOrNonPositiveRetentionUsesSevenWithExactlyOneWarn() {
        for (String bad : new String[] {"abc", "7.5", "0", "-3", "99999999999999999999", "1d"}) {
            logs.list.clear();
            assertThat(RequestLedger.resolveRetentionDays(env(bad))).as("value %s", bad).isEqualTo(7);
            assertThat(logged(Level.WARN, RequestLedger.RETENTION_ENV)).as("one WARN for %s", bad).hasSize(1);
        }
    }
}
