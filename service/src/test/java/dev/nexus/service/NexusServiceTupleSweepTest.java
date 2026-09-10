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

import static dev.nexus.service.NexusService.TupleSweepIncompleteCause.NONE;
import static dev.nexus.service.NexusService.TupleSweepIncompleteCause.TENANT_CAP;
import static dev.nexus.service.NexusService.TupleSweepIncompleteCause.WALL_CLOCK;
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
 * cursor; a tenant cut short by its own per-tenant cap does not stop the whole run,
 * so later tenants are still reached within the same run; a tenant whose backlog
 * always exceeds that cap never gets stamped and never starves the rest (RDR-205
 * Phase 1 review, Sam's ruling, nexus-em75s.7 — a tenant is stamped ONLY on a clean
 * finish, and the wall clock is checked ONLY at a tenant boundary, never mid-tenant,
 * so a tenant already underway always finishes cleanly or hits its own cap first);
 * the T1 sweep still runs in the same cycle; a tenant reachable only through its
 * {@code tuple_tenants} row (no {@code service_tokens} row at all) is still visited;
 * each arm gets its own bounded per-visit share of the per-tenant cap, so a
 * release-heavy tenant capped on arm 1 alone does not starve the purge arms within
 * the same visit (RDR-205 Phase 1 follow-on, bead nexus-em75s.34).
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

    /** Row count for a tenant across ALL subspaces — a direct, stamp-independent
     *  "is this tenant's backlog fully drained" signal for the convergence loops
     *  below, rather than inferring completion from {@link #lastSweptAt} advancing
     *  (RDR-205 Phase 1 review, Sam's ruling, nexus-em75s.7: a cut-short tenant's
     *  stamp stays put across every run until the one that finally finishes it
     *  cleanly, so {@code lastSweptAt} non-null WOULD also signal completion here —
     *  this is the more direct check). */
    private int countTuples(String tenant) {
        return su.fetchCount(su.selectFrom(TUPLES).where(TUPLES.TENANT_ID.eq(tenant)));
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
        assertThat(result.incompleteCause()).isEqualTo(NONE);
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
        assertThat(result.incompleteCause()).isEqualTo(NONE);
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
        assertThat(run1.incompleteCause()).isEqualTo(WALL_CLOCK);
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

        // tenantA: 5 expired rows, batchSize=2, maxBatchesPerTenant=3. nexus-em75s.34:
        // the cap of 3 splits into a share of 1 batch per arm (release, purge-tuples,
        // purge-log), each with its OWN counter. Arm 1 (release) has nothing to
        // release, drains cleanly in its 1-batch share. Arm 2 (purge-tuples) spends
        // its own 1-batch share purging 2 of the 5 rows, then hits ITS OWN share
        // (independently of arm 1) and stops -- tenantA is cut short, keeping its
        // (null) stamp. Arm 3 (purge-log) still runs its own share regardless (finds
        // nothing, since arm 1 never released anything to log).
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
        // Lower-bound, not exact: this test class is @TestInstance(PER_CLASS) and
        // shares one `tuple_tenants` table across every test method, so a tenant
        // already stamped (drained) by an earlier-run sibling test can legitimately
        // be re-visited (trivially, at zero cost) within this run's generous budget.
        assertThat(run1.tenantsVisited()).isGreaterThanOrEqualTo(2);
        assertThat(run1.incompleteCause()).isEqualTo(TENANT_CAP);
        assertThat(run1.purged()).isEqualTo(3); // 2 from tenantA's own capped purge-arm share + 1 from tenantB
        // RDR-205 Phase 1 review, Sam's ruling (nexus-em75s.7): a tenant is stamped
        // ONLY on a clean finish, per the RDR verbatim — tenantA's own cap cut it
        // short, so it keeps its OLD (null) stamp and sorts first again next run.
        // This does not starve tenantB: TENANT_CAP never stops the whole run, so
        // tenantB is still visited and completes within this SAME run.
        assertThat(lastSweptAt(tenantA)).as("cut short by its own cap -- keeps its old stamp").isNull();
        assertThat(lastSweptAt(tenantB)).isNotNull(); // completed cleanly: stamped

        // tenantA is the only tenant left unstamped, and (least-recently-swept-first
        // order) the oldest remaining: "oldest last_swept_at" reports its true,
        // unchanged (null) stamp -- never `now`.
        assertThat(run1.oldestLastSweptAt()).isNull();

        // tenantA's remaining backlog (3 of 5 rows) is drained within a bounded number
        // of further runs: each makes durable, cumulative progress (idempotent — the
        // remaining rows and every other arm simply run on the next call), even though
        // tenantA is never stamped until a run finally finishes it cleanly.
        int runsToComplete = 0;
        OffsetDateTime clock = now;
        while (countTuples(tenantA) > 0 && runsToComplete < 10) {
            clock = clock.plusMinutes(1);
            service.runScheduledTupleSweep(clock, Duration.ofSeconds(30), 2, 3, Duration.ofMinutes(5));
            runsToComplete++;
        }
        assertThat(countTuples(tenantA))
                .as("tenantA's backlog must be fully drained within a bounded number of runs")
                .isZero();
        assertThat(runsToComplete).isLessThanOrEqualTo(10);
    }

    // ── Scenario: per-arm shares (nexus-em75s.34) — a capped release arm must not
    //    starve the purge arms within the SAME visit ──────────────────────────────

    /**
     * RDR-205 Phase 1 follow-on (bead nexus-em75s.34): before the per-arm split,
     * the three arms shared ONE counter and ONE cap — a tenant whose release-arm
     * backlog alone exceeded {@code maxBatchesPerTenant} would exhaust the WHOLE
     * cap inside arm 1's own loop, and arms 2/3 (the purge arms) would never run a
     * single batch for that tenant this visit: their {@code while} loops were
     * gated on the same {@code complete} flag arm 1 had already cleared. This test
     * seeds a backlog on arm 1 alone that exceeds its (now per-arm) share, plus
     * small backlogs for arms 2 and 3, and asserts the purge arms still ran in the
     * SAME visit — {@link NexusService#tupleSweepArmBatchShare} gives each arm its
     * own bounded share of {@code maxBatchesPerTenant} (1 batch each, here, for a
     * cap of 3) so a release-heavy tenant can no longer starve its own purge arms.
     */
    @Test
    void sweep_armsGetIndependentShares_arm1CappedAlone_purgeArmsStillRunSameVisit() throws Exception {
        String tenant = "sweep-arm-share-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(tenant, now.minusDays(2), now.minusDays(2), null);

        // Arm 1 (release): 5 lapsed claims — far more than a 1-batch share at
        // batchSize=1 can drain in one visit.
        for (int i = 0; i < 5; i++) {
            byte[] id = fakeId(tenant + "-lapsed-" + i);
            insertTuple(id, tenant, "mailbox/agent-share", "claimed", "worker-" + i, "claim-" + i,
                    now.minusMinutes(5), 0, null, now.plusDays(1), now.minusHours(1).plusSeconds(i));
        }
        // Arm 2 (purge expired tuples): 2 expired, never-claimed rows — a small
        // backlog, unrelated to arm 1's.
        for (int i = 0; i < 2; i++) {
            byte[] id = fakeId(tenant + "-expired-" + i);
            insertTuple(id, tenant, "mailbox/agent-share-expired", null, null, null,
                    null, 0, null, now.minusMinutes(1), now.minusHours(2).plusSeconds(i));
        }
        // Arm 3 (purge old claim-log rows): 2 rows well past the 180-day default TTL.
        insertClaimLogRow(tenant, "mailbox/agent-share-log-1", null, "claim", now.minusDays(200));
        insertClaimLogRow(tenant, "mailbox/agent-share-log-2", null, "claim", now.minusDays(201));

        // maxBatchesPerTenant=3, batchSize=1: tupleSweepArmBatchShare splits this
        // 1/1/1 across the three arms.
        var result = service.runScheduledTupleSweep(now, Duration.ofSeconds(30),
                /* batchSize */ 1, /* maxBatchesPerTenant */ 3, /* wallClockBudget */ Duration.ofMinutes(5));

        assertThat(result.incompleteCause()).isEqualTo(TENANT_CAP);
        // Arm 1 spent its own 1-batch share and released exactly one lapsed claim;
        // its 5-row backlog is nowhere near drained.
        assertThat(result.released()).isEqualTo(1);
        int stillClaimed = su.fetchCount(su.selectFrom(TUPLES)
                .where(TUPLES.TENANT_ID.eq(tenant).and(TUPLES.CLAIM_STATE.eq("claimed"))));
        assertThat(stillClaimed).as("arm 1's own cap left most lapsed claims unreleased").isEqualTo(4);
        // The purge arms still ran THIS SAME visit, despite arm 1 hitting its own
        // share first — the starvation this bead fixes.
        assertThat(result.purged())
                .as("purge-expired arm ran in the same visit as the capped release arm")
                .isGreaterThanOrEqualTo(1);
        assertThat(result.logRowsPurged())
                .as("purge-log arm ran in the same visit as the capped release arm")
                .isGreaterThanOrEqualTo(1);
        assertThat(lastSweptAt(tenant)).as("cut short by arm 1's own cap -- keeps its old (null) stamp").isNull();

        // Drain fully so this test does not leave a permanently unswept tenant
        // behind for PER_CLASS siblings sharing this table.
        service.runScheduledTupleSweep(now.plusMinutes(1), Duration.ofSeconds(30), 300, 300, Duration.ofMinutes(2));
        assertThat(lastSweptAt(tenant)).as("cleaned up -- no longer polluting later tests").isNotNull();
    }

    // ── Scenario: per-tenant-cap starvation across several tenants (nexus-em75s.7,
    //    Sam's ruling) ──────────────────────────────────────────────────────────

    /**
     * RDR-205 Phase 1 review, Sam's ruling (nexus-em75s.7, superseding the prior
     * "stamp on any progress" fix): a tenant is stamped ONLY on a clean finish, so a
     * tenant whose own backlog always exceeds its {@code maxBatchesPerTenant} NEVER
     * gets stamped and sorts first ({@code last_swept_at ASC NULLS FIRST}) again
     * every run, forever. This does not starve the rest: unlike {@code WALL_CLOCK},
     * {@code TENANT_CAP} never stops the WHOLE run — the task moves on to the next
     * tenant in the SAME run, so every other tenant is still reached.
     */
    @Test
    void sweep_tenantAlwaysExhaustsPerTenantCap_laterTenantsStillVisitedWithinTheSameRun() throws Exception {
        // Lexicographic tenant_id order ("...-1-" < "...-2-" < "...-3-") matches the
        // ASC-NULLS-FIRST sort's tie-break while every tenant is still unswept, so
        // `starver` is picked first every run.
        String starver = "sweep-cap-starve-1-" + UUID.randomUUID();
        String second = "sweep-cap-starve-2-" + UUID.randomUUID();
        String third = "sweep-cap-starve-3-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(starver, now.minusDays(2), now.minusDays(2), null);
        insertTupleTenant(second, now.minusDays(2), now.minusDays(2), null);
        insertTupleTenant(third, now.minusDays(2), now.minusDays(2), null);

        // `starver` has far more expired rows than a per-tenant cap of 5 batches at
        // batchSize=1 can ever drain in a single run — it hits TENANT_CAP every run,
        // never completes, and (per the ruling) is therefore never stamped.
        for (int i = 0; i < 200; i++) {
            byte[] id = fakeId(starver + "-row-" + i);
            insertTuple(id, starver, "mailbox/agent-cap-starve", null, null, null,
                    null, 0, null, now.minusMinutes(1), now.minusHours(1).plusSeconds(i));
        }
        // `second`/`third` each have a single trivial row — either completes
        // comfortably inside the same per-tenant cap once reached.
        insertTuple(fakeId(second + "-row"), second, "mailbox/agent-cap-starve-2", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(1));
        insertTuple(fakeId(third + "-row"), third, "mailbox/agent-cap-starve-3", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(1));

        // A generous wall-clock budget: TENANT_CAP, never WALL_CLOCK, is the only
        // possible cause of incompleteness in this run.
        var run1 = service.runScheduledTupleSweep(now, Duration.ofSeconds(30),
                /* batchSize */ 1, /* maxBatchesPerTenant */ 5, /* wallClockBudget */ Duration.ofSeconds(30));

        // Lower-bound, not exact: this test class is @TestInstance(PER_CLASS) and
        // shares one `tuple_tenants` table across every test method, so a tenant
        // already stamped (drained) by an earlier-run sibling test can legitimately
        // be re-visited (trivially, at zero cost) within this run's generous budget.
        assertThat(run1.tenantsVisited())
                .as("starver's own cap does not stop the run -- second and third are still reached")
                .isGreaterThanOrEqualTo(3);
        assertThat(run1.incompleteCause()).isEqualTo(TENANT_CAP);
        assertThat(lastSweptAt(starver)).as("cut short by its own cap -- keeps its old (null) stamp").isNull();
        assertThat(lastSweptAt(second)).isNotNull(); // completed cleanly: stamped
        assertThat(lastSweptAt(third)).isNotNull(); // completed cleanly: stamped

        // A second run confirms this is not a one-off: starver's backlog is barely
        // dented by one run's worth of capped batches, so it hits its cap again and
        // stays unstamped, while second/third simply stay stamped.
        var run2 = service.runScheduledTupleSweep(now.plusMinutes(1), Duration.ofSeconds(30), 1, 5,
                Duration.ofSeconds(30));
        assertThat(run2.incompleteCause()).isEqualTo(TENANT_CAP);
        assertThat(lastSweptAt(starver)).as("still cut short on run 2 -- still unstamped").isNull();

        // Drain starver's remaining backlog and let it finish cleanly, so this test
        // does not leave a PERMANENTLY unswept tenant behind: this class is
        // @TestInstance(PER_CLASS) and shares one `tuple_tenants` table across every
        // test method, and every OTHER test here is self-cleaning by the time its own
        // method returns.
        service.runScheduledTupleSweep(now.plusMinutes(2), Duration.ofSeconds(30), 300, 300, Duration.ofMinutes(2));
        assertThat(lastSweptAt(starver)).as("cleaned up -- no longer polluting later tests").isNotNull();
    }

    // ── Scenario: wall-clock stop lands only at a tenant boundary, never mid-tenant
    //    (RDR-205 Phase 1 review, Sam's ruling, nexus-em75s.7) ────────────────────

    /**
     * RDR-205 Phase 1 review, Sam's ruling (nexus-em75s.7): the wall-clock budget is
     * checked ONLY before starting a tenant, never inside one — a tenant already
     * underway always finishes cleanly (and IS stamped) or hits its own {@code
     * maxBatchesPerTenant} cap first, however far past the nominal budget that takes;
     * only THEN, at the NEXT tenant boundary, does the already-exhausted budget stop
     * the run.
     */
    @Test
    void sweep_wallClockExhaustedMidTenant_tenantFinishesAnyway_runStopsOnlyAtNextBoundary() throws Exception {
        // Warm up the (pooled) DataSource this service's sweep queries actually run
        // through -- distinct from `su`'s own dedicated superuser connection used by
        // this file's seeding helpers -- BEFORE the timing-sensitive budget below. A
        // cold first connection acquisition through a freshly-constructed
        // HikariDataSource can itself cost more than a tiny nominal budget, which
        // would make the very first tenant-boundary check fire before ANY tenant
        // (`midTenant` included) ever starts -- a false negative unrelated to the
        // property this test exists to prove.
        service.runScheduledTupleSweep(
                OffsetDateTime.now(ZoneOffset.UTC), Duration.ofSeconds(30), 300, 50, Duration.ofSeconds(30));

        // Lexicographic order picks `midTenant` first (both start unswept, null
        // last_swept_at, so the tie-break is tenant_id).
        String midTenant = "sweep-boundary-1-" + UUID.randomUUID();
        String afterTenant = "sweep-boundary-2-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);
        insertTupleTenant(midTenant, now.minusDays(2), now.minusDays(2), null);
        insertTupleTenant(afterTenant, now.minusDays(2), now.minusDays(2), null);

        // `midTenant` has far more expired rows than a 100ms wall-clock budget at
        // batchSize=1 can drain (each batch is its own round trip against the real
        // Testcontainers Postgres): the deadline WILL be exceeded partway through its
        // own processing, not merely before it starts (this same row-count/budget
        // ratio is what proved, empirically, a reliable mid-processing overrun under
        // the pre-ruling code).
        for (int i = 0; i < 3000; i++) {
            byte[] id = fakeId(midTenant + "-row-" + i);
            insertTuple(id, midTenant, "mailbox/agent-boundary", null, null, null,
                    null, 0, null, now.minusMinutes(1), now.minusHours(1).plusNanos(i * 1_000_000L));
        }
        insertTuple(fakeId(afterTenant + "-row"), afterTenant, "mailbox/agent-boundary-2", null, null, null,
                null, 0, null, now.minusMinutes(1), now.minusHours(1));

        // maxBatchesPerTenant is generous enough to never bind against midTenant's
        // own 3000-row backlog — the ONLY bound in play is the wall clock, and it is
        // checked at tenant boundaries only. "sweep-boundary-" sorts before every
        // other tenant-id prefix this file uses (and before any never-swept tenant a
        // sibling test method may have left behind), so `midTenant` is guaranteed to
        // be the first tenant this run touches, however many other tenants a sibling
        // test method has already added to the shared `tuple_tenants` table.
        var result = service.runScheduledTupleSweep(now, Duration.ofSeconds(30),
                /* batchSize */ 1, /* maxBatchesPerTenant */ 100_000, /* wallClockBudget */ Duration.ofMillis(100));

        assertThat(result.incompleteCause()).isEqualTo(WALL_CLOCK);
        assertThat(lastSweptAt(midTenant))
                .as("midTenant, already underway, finished cleanly despite the expired budget -- stamped")
                .isNotNull();
        assertThat(countTuples(midTenant)).as("midTenant's whole backlog drained in the one run").isZero();
        assertThat(lastSweptAt(afterTenant))
                .as("the run stopped at the boundary before it -- never started, keeps its pre-run (null) stamp")
                .isNull();

        // Drain afterTenant too, so this test does not leave a permanently unswept
        // tenant behind for sibling test methods sharing this PER_CLASS instance.
        service.runScheduledTupleSweep(now.plusMinutes(1), Duration.ofSeconds(30), 300, 50, Duration.ofMinutes(2));
        assertThat(lastSweptAt(afterTenant)).as("cleaned up -- no longer polluting later tests").isNotNull();
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

    // ── tupleSweepArmBatchShare: the per-arm split (nexus-em75s.34) ──────────────

    @Test
    void tupleSweepArmBatchShare_evenlyDivisible_splitsEqually() {
        // 3 -> 1/1/1, summing to the original total.
        assertThat(NexusService.tupleSweepArmBatchShare(3, 0)).isEqualTo(1);
        assertThat(NexusService.tupleSweepArmBatchShare(3, 1)).isEqualTo(1);
        assertThat(NexusService.tupleSweepArmBatchShare(3, 2)).isEqualTo(1);
    }

    @Test
    void tupleSweepArmBatchShare_remainder_goesToEarlierArmsFirst_sumEqualsTotal() {
        // 5 -> 2/2/1: remainder 2 goes to arm indices 0 and 1.
        assertThat(NexusService.tupleSweepArmBatchShare(5, 0)).isEqualTo(2);
        assertThat(NexusService.tupleSweepArmBatchShare(5, 1)).isEqualTo(2);
        assertThat(NexusService.tupleSweepArmBatchShare(5, 2)).isEqualTo(1);

        // The production default (50) -> 17/17/16, still summing to 50.
        int a = NexusService.tupleSweepArmBatchShare(NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT, 0);
        int b = NexusService.tupleSweepArmBatchShare(NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT, 1);
        int c = NexusService.tupleSweepArmBatchShare(NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT, 2);
        assertThat(a + b + c).isEqualTo(NexusService.DEFAULT_TUPLE_SWEEP_MAX_BATCHES_PER_TENANT);
        assertThat(a).isEqualTo(17);
        assertThat(b).isEqualTo(17);
        assertThat(c).isEqualTo(16);
    }

    @Test
    void tupleSweepArmBatchShare_belowArmCount_someArmsGetZero() {
        // maxBatchesPerTenant=1: only arm 0 gets a share; arms 1/2 get none this
        // visit -- an unavoidable consequence of splitting a cap smaller than the
        // arm count, documented on tupleSweepArmBatchShare's own javadoc.
        assertThat(NexusService.tupleSweepArmBatchShare(1, 0)).isEqualTo(1);
        assertThat(NexusService.tupleSweepArmBatchShare(1, 1)).isEqualTo(0);
        assertThat(NexusService.tupleSweepArmBatchShare(1, 2)).isEqualTo(0);
    }

    @Test
    void resolveTupleSweepWallClockBudgetSeconds_defaultsAndParses() {
        assertThat(NexusService.resolveTupleSweepWallClockBudgetSeconds(null))
                .isEqualTo(NexusService.DEFAULT_TUPLE_SWEEP_WALL_CLOCK_BUDGET_SECONDS);
        assertThat(NexusService.resolveTupleSweepWallClockBudgetSeconds("99")).isEqualTo(99L);
    }
}
