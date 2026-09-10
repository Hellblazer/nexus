// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_TENANTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-205 Phase 1 Step 5 (bead nexus-em75s.5) — the tuple sweep's scheduled-task
 * WIRING: {@link NexusService#runScheduledTupleSweep}, a SECOND scheduled task on
 * {@code sweepScheduler} separate from {@link NexusService#runScheduledSweep}, whose
 * tenant set comes from {@code nexus.tuple_tenants} rather than the token loop's
 * {@code service_tokens}-derived set.
 *
 * <p>Every scenario below seeds rows directly via raw SQL on a superuser connection
 * (bypassing RLS and the repository's own clock) so a claim can be backdated into
 * "already lapsed" and a tuple into "already expired" — {@link
 * dev.nexus.service.db.TupleRepository}'s own API always writes the current instant.
 *
 * <p>Covers the RDR-205 §Test Plan scenarios this bead owns: seeded expired tuples,
 * lapsed claims and old log rows sweep cleanly and an idle run logs zeros; the budget
 * stops mid-list and the next run visits the unreached tenants first with no JVM
 * cursor; the budget is exhausted across several tenants, stopping at a tenant
 * boundary with every tenant reached within a bounded number of runs, and the T1
 * sweep still runs in the same cycle; a tenant reachable only through its {@code
 * tuple_tenants} row (no {@code service_tokens} row at all) is still visited.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusServiceTupleSweepTest {

    private static final String TOKEN = "tuple-sweep-test-token-7f2a9c";
    private static final String MAILBOX_TEMPLATE = "mailbox";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource ds;
    TemplateRegistry registry;
    NexusService service;
    Connection suConn;
    DSLContext su;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        ds = new com.zaxxer.hikari.HikariDataSource(cfg);

        registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);

        // Port 0: dynamic allocation, never a hardcoded test port. Full 8-arg
        // constructor so tupleRepo is wired (TupleHandlerWiringTest's precedent).
        service = new NexusService(0, TOKEN, ds, null, null, null, null, registry);

        // One superuser connection (bypasses RLS), reused for every raw-SQL seed/read
        // helper below — autoCommit=true (JDBC default), so each call is its own
        // statement, immediately visible to (and of) every other connection.
        suConn = pg.createConnection("");
        su = DSL.using(suConn, SQLDialect.POSTGRES);
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) {
            try {
                service.stop();
            } catch (Exception ignored) {
                // never started; nothing to tear down
            }
        }
        if (suConn != null) suConn.close();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── raw-SQL seeding (bypasses RLS + the repo's own clock) ───────────────────

    private static byte[] fakeId(String label) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(label.getBytes(StandardCharsets.UTF_8));
    }

    private void insertTuple(byte[] id, String tenant, String subspace, String claimState, String claimant,
                              String claimId, OffsetDateTime leaseUntil, int attempts,
                              OffsetDateTime consumedAt, OffsetDateTime expiresAt, OffsetDateTime createdAt) {
        su.insertInto(TUPLES,
                        TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE, TUPLES.KEYS,
                        TUPLES.CLAIM_STATE, TUPLES.CLAIMANT, TUPLES.CLAIM_ID, TUPLES.LEASE_UNTIL,
                        TUPLES.ATTEMPTS, TUPLES.CONSUMED_AT, TUPLES.CONSUMED_BY, TUPLES.EXPIRES_AT,
                        TUPLES.CREATED_AT)
                .values(id, tenant, subspace, MAILBOX_TEMPLATE, JSONB.valueOf("{}"),
                        claimState, claimant, claimId, leaseUntil,
                        attempts, consumedAt, consumedAt == null ? null : "sweep-test-consumer",
                        expiresAt, createdAt)
                .execute();
    }

    /** {@code tupleId} may be null (a log-retention probe with no backing tuple);
     *  when non-null it MUST already exist in {@code nexus.tuples} (the FK). */
    private void insertClaimLogRow(String tenant, String subspace, byte[] tupleId, String transition,
                                    OffsetDateTime at) {
        su.insertInto(TUPLE_CLAIM_LOG,
                        TUPLE_CLAIM_LOG.TENANT_ID, TUPLE_CLAIM_LOG.SUBSPACE, TUPLE_CLAIM_LOG.TEMPLATE,
                        TUPLE_CLAIM_LOG.TUPLE_ID, TUPLE_CLAIM_LOG.TRANSITION, TUPLE_CLAIM_LOG.AT,
                        TUPLE_CLAIM_LOG.EXPIRES_AT)
                .values(tenant, subspace, MAILBOX_TEMPLATE, tupleId, transition, at, at.plusDays(1))
                .execute();
    }

    private void insertTupleTenant(String tenant, OffsetDateTime firstSeen, OffsetDateTime lastSeen,
                                    OffsetDateTime lastSweptAt) {
        su.insertInto(TUPLE_TENANTS,
                        TUPLE_TENANTS.TENANT_ID, TUPLE_TENANTS.FIRST_SEEN, TUPLE_TENANTS.LAST_SEEN,
                        TUPLE_TENANTS.LAST_SWEPT_AT)
                .values(tenant, firstSeen, lastSeen, lastSweptAt)
                .execute();
    }

    private OffsetDateTime lastSweptAt(String tenant) {
        return su.select(TUPLE_TENANTS.LAST_SWEPT_AT)
                .from(TUPLE_TENANTS)
                .where(TUPLE_TENANTS.TENANT_ID.eq(tenant))
                .fetchOne(TUPLE_TENANTS.LAST_SWEPT_AT);
    }

    private boolean tupleExists(byte[] id) {
        return su.fetchExists(su.selectFrom(TUPLES).where(TUPLES.ID.eq(id)));
    }

    private String claimStateOf(byte[] id) {
        return su.select(TUPLES.CLAIM_STATE).from(TUPLES).where(TUPLES.ID.eq(id)).fetchOne(TUPLES.CLAIM_STATE);
    }

    private int attemptsOf(byte[] id) {
        return su.select(TUPLES.ATTEMPTS).from(TUPLES).where(TUPLES.ID.eq(id)).fetchOne(TUPLES.ATTEMPTS);
    }

    private int countClaimLogRows(String tenant, String transition) {
        return su.fetchCount(su.selectFrom(TUPLE_CLAIM_LOG)
                .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant).and(TUPLE_CLAIM_LOG.TRANSITION.eq(transition))));
    }

    private boolean claimLogRowExists(String tenant, String subspace) {
        return su.fetchExists(su.selectFrom(TUPLE_CLAIM_LOG)
                .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant).and(TUPLE_CLAIM_LOG.SUBSPACE.eq(subspace))));
    }

    // ── Scenario: seeded expired tuples, lapsed claims, old log rows ────────────

    @Test
    void sweep_releasesLapsedClaims_purgesExpiredTuples_purgesOldLogRows_countsMatchSeed() throws Exception {
        String tenant = "sweep-seed-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(tenant, now.minusDays(1), now.minusDays(1), null);

        // A lapsed (but not yet at max_attempts) claim: released, not dead-lettered.
        byte[] lapsed = fakeId(tenant + "-lapsed");
        insertTuple(lapsed, tenant, "mailbox/agent-x", "claimed", "worker-1", "claim-1",
                now.minusMinutes(5), 0, null, now.plusDays(1), now.minusHours(1));

        // A lapsed claim already at max_attempts (mailbox.yaml: max_attempts=3):
        // dead-lettered, not released.
        byte[] deadBound = fakeId(tenant + "-deadbound");
        insertTuple(deadBound, tenant, "mailbox/agent-y", "claimed", "worker-2", "claim-2",
                now.minusMinutes(5), 3, null, now.plusDays(1), now.minusHours(1));

        // An expired, never-claimed tuple: purged.
        byte[] expired = fakeId(tenant + "-expired");
        insertTuple(expired, tenant, "mailbox/agent-z", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(2));

        // A consumed tuple past its own retention ceiling (expires_at already passed): purged.
        byte[] consumedPastRetention = fakeId(tenant + "-consumed-old");
        insertTuple(consumedPastRetention, tenant, "mailbox/agent-w", null, null, null,
                null, 1, now.minusDays(8), now.minusMinutes(1), now.minusDays(8));

        // An old claim-log row, well past the 180-day default TTL, no backing tuple: purged.
        insertClaimLogRow(tenant, "mailbox/agent-old-log", null, "claim", now.minusDays(200));
        // A recent claim-log row: survives.
        insertClaimLogRow(tenant, "mailbox/agent-recent-log", null, "claim", now.minusDays(1));

        var result = service.runScheduledTupleSweep(now, Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));

        assertThat(result.tenantsVisited()).isGreaterThanOrEqualTo(1);
        assertThat(result.budgetExhausted()).isFalse();
        assertThat(result.released()).isGreaterThanOrEqualTo(1);
        assertThat(result.deadLettered()).isGreaterThanOrEqualTo(1);
        assertThat(result.purged()).isGreaterThanOrEqualTo(2);
        assertThat(result.logRowsPurged()).isGreaterThanOrEqualTo(1);

        // The lapsed-but-not-dead row is released: claim cleared, attempts incremented,
        // an `expire` log row exists.
        assertThat(claimStateOf(lapsed)).isNull();
        assertThat(attemptsOf(lapsed)).isEqualTo(1);
        assertThat(countClaimLogRows(tenant, "expire")).isGreaterThanOrEqualTo(1);

        // The already-at-max-attempts row is dead-lettered, with a `dead` log row.
        assertThat(claimStateOf(deadBound)).isEqualTo("dead");
        assertThat(countClaimLogRows(tenant, "dead")).isGreaterThanOrEqualTo(1);

        // Purged tuples are gone.
        assertThat(tupleExists(expired)).isFalse();
        assertThat(tupleExists(consumedPastRetention)).isFalse();

        // The old claim-log row is gone; the recent one survives.
        assertThat(claimLogRowExists(tenant, "mailbox/agent-old-log")).isFalse();
        assertThat(claimLogRowExists(tenant, "mailbox/agent-recent-log")).isTrue();

        // The tenant, swept cleanly, is stamped.
        assertThat(lastSweptAt(tenant)).isNotNull();
    }

    @Test
    void sweep_purgedTuple_leavesItsLogRowSurviving_withTupleIdNulled() throws Exception {
        String tenant = "sweep-fk-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(tenant, now.minusDays(1), now.minusDays(1), null);

        byte[] expired = fakeId(tenant + "-fk-expired");
        insertTuple(expired, tenant, "mailbox/agent-fk", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(2));
        insertClaimLogRow(tenant, "mailbox/agent-fk", expired, "claim", now.minusMinutes(30));

        service.runScheduledTupleSweep(now, Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));

        assertThat(tupleExists(expired)).isFalse();
        int survivingRows = su.fetchCount(su.selectFrom(TUPLE_CLAIM_LOG)
                .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant).and(TUPLE_CLAIM_LOG.SUBSPACE.eq("mailbox/agent-fk"))
                        .and(TUPLE_CLAIM_LOG.TUPLE_ID.isNull())));
        assertThat(survivingRows).isEqualTo(1);
    }

    @Test
    void sweep_idleRun_logsZeros_withoutFailing() throws Exception {
        String tenant = "sweep-idle-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(tenant, now.minusDays(1), now.minusDays(1), null);

        var result = service.runScheduledTupleSweep(now, Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));

        assertThat(result.tenantsVisited()).isGreaterThanOrEqualTo(1);
        assertThat(result.scanned()).isZero();
        assertThat(result.released()).isZero();
        assertThat(result.deadLettered()).isZero();
        assertThat(result.purged()).isZero();
        assertThat(result.budgetExhausted()).isFalse();
        assertThat(lastSweptAt(tenant)).isNotNull();
    }

    // ── Scenario: budget stops mid-list, restart visits unreached tenants first ─

    @Test
    void sweep_budgetStopsMidList_nextRunVisitsUnreachedTenantsFirst_noJvmCursor() throws Exception {
        String early = "sweep-order-a-" + UUID.randomUUID();
        String late = "sweep-order-b-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        // `early` never swept (null last_swept_at, sorts first); `late` swept recently
        // (sorts after `early` under ASC NULLS FIRST).
        insertTupleTenant(early, now.minusDays(2), now.minusDays(2), null);
        insertTupleTenant(late, now.minusDays(2), now.minusDays(2), now.minusHours(1));

        // A wall-clock budget of zero: the run stops before starting ANY tenant.
        var run1 = service.runScheduledTupleSweep(now, Duration.ofSeconds(30), 300, 50, Duration.ZERO);
        assertThat(run1.tenantsVisited()).isZero();
        assertThat(run1.budgetExhausted()).isTrue();
        // Neither tenant was touched — both keep their pre-run stamps.
        assertThat(lastSweptAt(early)).isNull();
        assertThat(lastSweptAt(late)).isNotNull();

        // A real run afterward reaches `early` first (it was never reached, no JVM
        // cursor survives between calls — every run re-derives order from the table).
        var run2 = service.runScheduledTupleSweep(
                now.plusMinutes(1), Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));
        assertThat(run2.tenantsVisited()).isGreaterThanOrEqualTo(1);
        assertThat(lastSweptAt(early)).isNotNull();
    }

    // ── Scenario: budget exhausted across several tenants ───────────────────────

    @Test
    void sweep_perTenantBatchCap_movesToNextTenant_wallClockStopsWholeRun_everyTenantReachedWithinBoundedRuns()
            throws Exception {
        String tenantA = "sweep-budget-a-" + UUID.randomUUID();
        String tenantB = "sweep-budget-b-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(tenantA, now.minusDays(2), now.minusDays(2), null);
        insertTupleTenant(tenantB, now.minusDays(2), now.minusDays(2), now.minusMinutes(1));

        // tenantA: 5 expired rows, batchSize=2, maxBatchesPerTenant=3. Every tenant
        // spends its FIRST batch on arm 1 (release) even with nothing to release —
        // that batch call still counts against the per-tenant cap. So the purge arm
        // gets 2 of the remaining 3 cap: batches 2 and 3 purge 2+2=4 of the 5 rows,
        // then the cap is hit before the 5th row or the log-purge arm — tenantA is
        // cut short, keeping its (null) stamp.
        for (int i = 0; i < 5; i++) {
            byte[] id = fakeId(tenantA + "-row-" + i);
            insertTuple(id, tenantA, "mailbox/agent-budget", null, null, null,
                    null, 0, null, now.minusMinutes(1), now.minusHours(1).plusSeconds(i));
        }
        // tenantB: a single expired row — every arm drains in one (or zero) batches
        // each, comfortably inside the same per-tenant cap. Completes cleanly.
        byte[] bRow = fakeId(tenantB + "-row");
        insertTuple(bRow, tenantB, "mailbox/agent-budget-b", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(1));

        var run1 = service.runScheduledTupleSweep(now, Duration.ofSeconds(30),
                /* batchSize */ 2, /* maxBatchesPerTenant */ 3, /* wallClockBudget */ Duration.ofMinutes(5));

        // The run stops at a tenant boundary: tenantA's own per-tenant cap does NOT
        // stop the whole run — tenantB (next in order, since it's already swept-once
        // and sorts after the never-swept tenantA) still gets visited and completed.
        assertThat(run1.tenantsVisited()).isEqualTo(2);
        assertThat(run1.budgetExhausted()).isTrue();
        assertThat(run1.purged()).isEqualTo(5); // 4 from tenantA's capped batches + 1 from tenantB
        assertThat(lastSweptAt(tenantA)).isNull(); // cut short: keeps its old (null) stamp
        assertThat(lastSweptAt(tenantB)).isNotNull(); // completed cleanly: stamped

        // Reported oldest last_swept_at after the run is the cut-short tenant's
        // PRE-run stamp (null — it was never swept).
        assertThat(run1.oldestLastSweptAt()).isNull();

        // tenantA is reached within a bounded number of runs: each further run makes
        // durable, cumulative progress (idempotent — the remaining row and both other
        // arms simply run on the next call), so the run count needed to finish it is
        // bounded, not unbounded.
        int runsToComplete = 0;
        OffsetDateTime clock = now;
        while (lastSweptAt(tenantA) == null && runsToComplete < 10) {
            clock = clock.plusMinutes(1);
            service.runScheduledTupleSweep(clock, Duration.ofSeconds(30), 2, 3, Duration.ofMinutes(5));
            runsToComplete++;
        }
        assertThat(lastSweptAt(tenantA))
                .as("tenantA must be reached within a bounded number of runs")
                .isNotNull();
        assertThat(runsToComplete).isLessThanOrEqualTo(10);
    }

    // ── Scenario: T1 sweep still runs in the same interval ──────────────────────

    @Test
    void sweep_tupleSweepAndT1Sweep_bothRunnable_independently() {
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        // The T1 crash-safety sweep (runScheduledSweep) is a SEPARATE method on the
        // same scheduler and must still be independently callable/successful — the
        // tuple sweep must not have replaced or blocked it.
        var t1 = service.runScheduledSweep(now);
        assertThat(t1).isNotNull();

        var tuple = service.runScheduledTupleSweep(now, Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));
        assertThat(tuple).isNotNull();
    }

    // ── Scenario: tenant reachable only via tuple_tenants (no service_tokens row) ─

    @Test
    void sweep_visitsTenant_viaTupleTenantsRow_evenWithNoServiceToken() throws Exception {
        String tenant = "sweep-no-token-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        // No service_tokens row is ever inserted for this tenant — runScheduledSweep's
        // token-derived tenant set would never see it. tuple_tenants is the tuple
        // sweep's OWN, independent enumeration.
        insertTupleTenant(tenant, now.minusDays(1), now.minusDays(1), null);
        byte[] id = fakeId(tenant + "-only-tuple");
        insertTuple(id, tenant, "mailbox/agent-notoken", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(1));

        var result = service.runScheduledTupleSweep(now, Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));

        assertThat(result.tenantsVisited()).isGreaterThanOrEqualTo(1);
        assertThat(tupleExists(id)).isFalse();
        assertThat(lastSweptAt(tenant)).isNotNull();
    }

    // ── resolveX settings: resolveBindHost-style testable shape ─────────────────

    @Test
    void resolveTupleSweepBatchSize_defaultsAndParses() {
        assertThat(NexusService.resolveTupleSweepBatchSize(null))
                .isEqualTo(NexusService.DEFAULT_TUPLE_SWEEP_BATCH_SIZE);
        assertThat(NexusService.resolveTupleSweepBatchSize("42")).isEqualTo(42);
    }

    @Test
    void resolveTupleSweepMaxBatchesPerTenant_defaultsAndParses() {
        assertThat(NexusService.resolveTupleSweepMaxBatchesPerTenant(null))
                .isEqualTo(NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT);
        assertThat(NexusService.resolveTupleSweepMaxBatchesPerTenant("7")).isEqualTo(7);
    }

    @Test
    void resolveTupleSweepWallClockBudgetSeconds_defaultsAndParses() {
        assertThat(NexusService.resolveTupleSweepWallClockBudgetSeconds(null))
                .isEqualTo(NexusService.DEFAULT_TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS);
        assertThat(NexusService.resolveTupleSweepWallClockBudgetSeconds("99")).isEqualTo(99L);
    }
}
