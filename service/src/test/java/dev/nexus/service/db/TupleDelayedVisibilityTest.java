// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-5r2sc.2.1 (RDR-235 E1): delayed visibility. {@code nexus.tuples.available_at}
 * hides a row from the claim until the DATABASE clock reaches it. {@code out} stamps it
 * from {@code delay_s}, {@code nack} from {@code retry_after_s}; both go through
 * {@code now() + interval} on the database. NULL (the default) is claimed exactly as
 * before. A delayed row still counts as available in the census (gate observation O5), and
 * every path that clears {@code claim_id} clears {@code available_at} with it.
 *
 * <p>The 60 s case is tested without waiting: the row is stamped through the repository
 * (so the stamp is checked to be about 60 s ahead on the database clock), then moved with a
 * direct UPDATE to one second ahead of, and one second behind, the database's {@code now()}.
 * The production path is exercised end to end by the 2 s cases, which really sleep.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleDelayedVisibilityTest {

    private static final String TENANT = "tuple-tenant-delayed-visibility";
    private static final String SVC_ROLE = "svc_tuple_delayed_vis_test";
    private static final String SVC_PASS = "svc_tuple_delayed_vis_test_pass";
    /** queue/<name>: retention_seconds 172800, take.max_attempts 3. */
    private static final long QUEUE_RETENTION = 172_800L;

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TupleRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new TupleRepository(new TenantScope(svcDs),
                TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    private record Q(String subspace, Map<String, String> pattern) {
    }

    private static Q newQueue() {
        String name = "dv-" + UUID.randomUUID().toString().substring(0, 10);
        return new Q("queue/" + name, Map.of("queue", name));
    }

    private void out(Q q, Long delayS) {
        repo.out(TENANT, q.subspace(), q.pattern(), Map.of("from", "producer"), "task", "n-1", null, null, delayS);
    }

    private TupleRepository.ClaimedTuple claimOrFail(Q q, String claimant) {
        var c = repo.inp(TENANT, q.subspace(), q.pattern(), claimant, 60);
        assertThat(c).as("the row must be claimable").isPresent();
        return c.get();
    }

    private <T> T admin(java.util.function.Function<DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private OffsetDateTime availableAt(Q q) throws Exception {
        return admin(ctx -> ctx.select(TUPLES.AVAILABLE_AT).from(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace())))
                .fetchOne(TUPLES.AVAILABLE_AT));
    }

    /** Seconds from the DATABASE's now() to the row's available_at (positive = in the future). */
    private double secondsAhead(Q q) throws Exception {
        // Both values come from ONE statement, so now() is the database's and not this JVM's.
        return admin(ctx -> {
            var r = ctx.select(TUPLES.AVAILABLE_AT, DSL.currentOffsetDateTime())
                    .from(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace())))
                    .fetchOne();
            return java.time.Duration.between(r.value2(), r.value1()).toMillis() / 1000.0;
        });
    }

    /** Moves available_at to {@code offset} seconds from the database's now() (negative = past). */
    private void moveAvailableAt(Q q, int offsetSeconds) throws Exception {
        int n = admin(ctx -> ctx.update(TUPLES)
                // add or sub of a POSITIVE interval: a negative DayToSecond renders as "-0 00:00:01",
                // whose sign Postgres drops, which would move the row FORWARD.
                .set(TUPLES.AVAILABLE_AT, offsetSeconds >= 0
                        ? DSL.currentOffsetDateTime().add(org.jooq.types.DayToSecond.valueOf(
                                java.time.Duration.ofSeconds(offsetSeconds)))
                        : DSL.currentOffsetDateTime().sub(org.jooq.types.DayToSecond.valueOf(
                                java.time.Duration.ofSeconds(-offsetSeconds))))
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace())))
                .execute());
        assertThat(n).as("the move touched the row").isEqualTo(1);
    }

    private void setAttempts(Q q, int attempts) throws Exception {
        int n = admin(ctx -> ctx.update(TUPLES).set(TUPLES.ATTEMPTS, attempts)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace()))).execute());
        assertThat(n).isEqualTo(1);
    }

    private record State(String claimState, OffsetDateTime availableAt, int attempts, String claimId) {
    }

    private State state(Q q) throws Exception {
        return admin(ctx -> {
            var r = ctx.select(TUPLES.CLAIM_STATE, TUPLES.AVAILABLE_AT, TUPLES.ATTEMPTS, TUPLES.CLAIM_ID)
                    .from(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace())))
                    .fetchOne();
            assertThat(r).isNotNull();
            return new State(r.value1(), r.value2(), r.value3(), r.value4());
        });
    }

    private boolean claimable(Q q) {
        var c = repo.inp(TENANT, q.subspace(), q.pattern(), "probe", 60);
        c.ifPresent(x -> repo.release(TENANT, x.claimId(), "probe"));
        return c.isPresent();
    }

    /** The shared 60 s scenario, run once the row has been stamped with a 60 s delay by the repository. */
    private void assertSixtySecondsThenClaimable(Q q) throws Exception {
        assertThat(availableAt(q)).as("stamped").isNotNull();
        assertThat(secondsAhead(q)).as("about 60 s ahead on the database clock").isBetween(50.0, 61.0);
        assertThat(claimable(q)).as("not claimable before the delay has passed").isFalse();
        moveAvailableAt(q, +30);  // the database clock is thirty seconds short of available_at (a 1 s margin flaked on a stalled box)
        assertThat(claimable(q)).as("thirty seconds short of available_at: still hidden").isFalse();
        moveAvailableAt(q, -30);  // the database clock has passed available_at
        assertThat(claimable(q)).as("past available_at: claimable").isTrue();
    }

    // ── (a) nack retry_after_s ───────────────────────────────────────────────

    @Test
    void nackWithRetryAfter60_isHiddenUntilTheDatabaseClockReachesIt_andCountsTheAttempt() throws Exception {
        Q q = newQueue();
        out(q, null);
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", 60L);
        State s = state(q);
        assertThat(s.attempts()).as("the attempt counts exactly as a plain nack counts it").isEqualTo(1);
        assertThat(s.claimState()).as("returned to available, not dead").isNull();
        assertThat(s.claimId()).isNull();
        assertSixtySecondsThenClaimable(q);
    }

    @Test
    void nackWithRetryAfter2_realClock_becomesClaimableAfterTwoSeconds() throws Exception {
        Q q = newQueue();
        out(q, null);
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", 2L);
        assertThat(claimable(q)).as("immediately after the nack").isFalse();
        Thread.sleep(2_500);
        assertThat(claimable(q)).as("after the retry_after_s has elapsed").isTrue();
    }

    // ── (b) out delay_s ──────────────────────────────────────────────────────

    @Test
    void outWithDelay60_isHiddenUntilTheDatabaseClockReachesIt() throws Exception {
        Q q = newQueue();
        out(q, 60L);
        assertSixtySecondsThenClaimable(q);
    }

    @Test
    void outWithDelay2_realClock_becomesClaimableAfterTwoSeconds() throws Exception {
        Q q = newQueue();
        out(q, 2L);
        assertThat(claimable(q)).as("immediately after the out").isFalse();
        Thread.sleep(2_500);
        assertThat(claimable(q)).as("after delay_s has elapsed").isTrue();
    }

    @Test
    void aDelayedRow_isStillVisibleToPlainRd() {
        Q q = newQueue();
        out(q, 60L);
        assertThat(repo.rd(TENANT, q.subspace(), q.pattern(), 10, null, 0))
                .as("rd is not a claim: the delay hides the row from the claim only").hasSize(1);
    }

    // ── (c) no delay: exactly as before ──────────────────────────────────────

    @Test
    void aTaskWithNoDelay_leavesAvailableAtNull_andIsClaimedAtOnce() throws Exception {
        Q q = newQueue();
        out(q, null);
        assertThat(availableAt(q)).isNull();
        assertThat(repo.inp(TENANT, q.subspace(), q.pattern(), "w1", 60)).isPresent();
    }

    @Test
    void delayZero_andNackWithoutRetryAfter_leaveAvailableAtNull() throws Exception {
        Q q = newQueue();
        out(q, 0L);
        assertThat(availableAt(q)).as("delay_s=0 is no delay").isNull();
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1");
        assertThat(availableAt(q)).isNull();
        assertThat(claimOrFail(q, "w2")).as("a plain nack is claimable at once, as before").isNotNull();
    }

    @Test
    void nackWithRetryAfterZero_isAPlainNack() throws Exception {
        Q q = newQueue();
        out(q, null);
        var c = claimOrFail(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", 0L);
        assertThat(availableAt(q)).isNull();
        assertThat(claimable(q)).isTrue();
    }

    // ── (d) validation ───────────────────────────────────────────────────────

    @Test
    void outRefusesANegativeDelay_andAnythingAboveRetention_andWritesNothing() throws Exception {
        Q q = newQueue();
        assertThatThrownBy(() -> out(q, -1L)).isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("delay_s");
        assertThatThrownBy(() -> out(q, QUEUE_RETENTION + 1)).isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("delay_s")
                .hasMessageContaining(String.valueOf(QUEUE_RETENTION + 1))
                .hasMessageContaining("retention_seconds " + QUEUE_RETENTION);
        assertThat(repo.rd(TENANT, q.subspace(), q.pattern(), 10, null, 0)).as("nothing was written").isEmpty();
        out(q, QUEUE_RETENTION);   // exactly the retention is allowed
        assertThat(repo.rd(TENANT, q.subspace(), q.pattern(), 10, null, 0)).hasSize(1);
    }

    @Test
    void nackRefusesANegativeRetryAfter_andAnythingAboveRetention_leavingTheClaimLive() throws Exception {
        Q q = newQueue();
        out(q, null);
        var c = claimOrFail(q, "w1");
        assertThatThrownBy(() -> repo.nack(TENANT, c.claimId(), "w1", -1L))
                .isInstanceOf(SchemaViolationException.class).hasMessageContaining("retry_after_s");
        assertThatThrownBy(() -> repo.nack(TENANT, c.claimId(), "w1", QUEUE_RETENTION + 1))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("retry_after_s")
                .hasMessageContaining("retention_seconds " + QUEUE_RETENTION);
        State s = state(q);
        assertThat(s.claimState()).as("a refused nack leaves the claim live").isEqualTo("claimed");
        assertThat(s.attempts()).as("and spends no attempt").isZero();
        assertThat(s.availableAt()).isNull();
        repo.ack(TENANT, c.claimId(), "w1");   // the claim is still good
    }

    // ── (e) dead letter leaves available_at NULL ─────────────────────────────

    @Test
    void aNackAtTheAttemptCap_deadLetters_andLeavesAvailableAtNull() throws Exception {
        Q q = newQueue();
        out(q, null);
        var c = claimOrFail(q, "w1");
        setAttempts(q, 2);   // queue max_attempts is 3; this nack is the third
        repo.nack(TENANT, c.claimId(), "w1", 60L);
        State s = state(q);
        assertThat(s.claimState()).isEqualTo("dead");
        assertThat(s.attempts()).isEqualTo(3);
        assertThat(s.availableAt()).as("a dead-lettered row carries no retry time").isNull();
    }

    // ── (f) every path that clears claim_id clears available_at ──────────────

    /** A row that is claimed while still carrying a (past) available_at, the state a claim leaves. */
    private TupleRepository.ClaimedTuple claimedWithStaleAvailableAt(Q q, String claimant) throws Exception {
        out(q, 60L);
        moveAvailableAt(q, -1);
        var c = claimOrFail(q, claimant);
        assertThat(state(q).availableAt()).as("non-vacuity: a claim leaves available_at as it was").isNotNull();
        return c;
    }

    @Test
    void releaseClearsAvailableAt() throws Exception {
        Q q = newQueue();
        var c = claimedWithStaleAvailableAt(q, "w1");
        repo.release(TENANT, c.claimId(), "w1");
        assertThat(state(q).availableAt()).isNull();
    }

    @Test
    void nackWithoutRetryAfterClearsAvailableAt() throws Exception {
        Q q = newQueue();
        var c = claimedWithStaleAvailableAt(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1");
        assertThat(state(q).availableAt()).isNull();
    }

    @Test
    void nackWithRetryAfterReplacesAStaleAvailableAt() throws Exception {
        Q q = newQueue();
        var c = claimedWithStaleAvailableAt(q, "w1");
        repo.nack(TENANT, c.claimId(), "w1", 60L);
        assertThat(secondsAhead(q)).as("the new stamp, not the stale one").isBetween(50.0, 61.0);
    }

    private void lapse(Q q) throws Exception {
        int n = admin(ctx -> ctx.update(TUPLES)
                .set(TUPLES.LEASE_UNTIL, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(5))
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace()))).execute());
        assertThat(n).isEqualTo(1);
    }

    @Test
    void theSweepsLapseRelease_clearsAvailableAt() throws Exception {
        Q q = newQueue();
        claimedWithStaleAvailableAt(q, "w1");
        lapse(q);
        var result = repo.releaseLapsedClaimsBatch(TENANT, 300, null);
        assertThat(result.released()).isGreaterThanOrEqualTo(1);
        assertThat(state(q).availableAt()).isNull();
    }

    @Test
    void theSweepsDeadLetterArm_clearsAvailableAt() throws Exception {
        Q q = newQueue();
        claimedWithStaleAvailableAt(q, "w1");
        setAttempts(q, 2);
        lapse(q);
        var result = repo.releaseLapsedClaimsBatch(TENANT, 300, null);
        assertThat(result.deadLettered()).isGreaterThanOrEqualTo(1);
        State s = state(q);
        assertThat(s.claimState()).isEqualTo("dead");
        assertThat(s.availableAt()).isNull();
    }

    @Test
    void aClaimThatLapsesAtMaxAttempts_isDeadLetteredByTheNextClaim_andClearsAvailableAt() throws Exception {
        Q q = newQueue();
        claimedWithStaleAvailableAt(q, "w1");
        setAttempts(q, 2);
        lapse(q);
        assertThat(repo.inp(TENANT, q.subspace(), q.pattern(), "w2", 60)).isEmpty();
        State s = state(q);
        assertThat(s.claimState()).isEqualTo("dead");
        assertThat(s.availableAt()).isNull();
    }

    // ── (g) the census counts a delayed row as available (O5) ────────────────

    @Test
    void subspaceStats_countsADelayedRowAsAvailable() {
        Q q = newQueue();
        out(q, 60L);
        var census = repo.subspaceStats(TENANT, q.subspace());
        assertThat(census.available()).as("depth: a delayed task counts as available (gate observation O5)")
                .isEqualTo(1);
        assertThat(census.total()).isEqualTo(1);
        assertThat(census.claimed()).isZero();
    }

    // ── (g2) nexus-p909o: the census tells a delayed row from a claimable one ─

    /** Both census forms must agree: the single-subspace query and the GROUP BY behind subspace_list. */
    private List<TupleRepository.SubspaceCensus> bothCensuses(Q q) {
        var stats = repo.subspaceStats(TENANT, q.subspace());
        var listed = repo.subspaceList(TENANT, q.subspace());
        assertThat(listed).as("subspace_list returns the one matching subspace").hasSize(1);
        assertThat(listed.get(0)).as("subspace_list and subspace_stats agree field for field").isEqualTo(stats);
        return List.of(stats, listed.get(0));
    }

    @Test
    void aDelayedRow_countsAsAvailableAndDelayed_andIsNotTheOldestClaimable() {
        Q q = newQueue();
        out(q, 60L);
        for (var c : bothCensuses(q)) {
            assertThat(c.available()).as("available keeps counting the delayed row").isEqualTo(1);
            assertThat(c.delayed()).isEqualTo(1);
            assertThat(c.oldestClaimableCreatedAt()).as("nothing a claim could take now").isNull();
        }
    }

    @Test
    void onceTheDelayElapses_theRowIsClaimable_andNoLongerDelayed() throws Exception {
        Q q = newQueue();
        out(q, 60L);
        moveAvailableAt(q, -30);  // the database clock has passed available_at
        for (var c : bothCensuses(q)) {
            assertThat(c.available()).isEqualTo(1);
            assertThat(c.delayed()).as("no longer waiting").isZero();
            assertThat(c.oldestClaimableCreatedAt()).as("now the oldest claimable row").isNotNull()
                    .isEqualTo(c.oldestCreatedAt());
        }
    }

    @Test
    void theOldestClaimable_skipsAnOlderDelayedRow_andAClaimedRow() throws Exception {
        Q q = newQueue();
        repo.out(TENANT, q.subspace(), q.pattern(), Map.of("from", "producer"), "delayed-first", "n-1", null, null, 60L);
        Thread.sleep(20);  // distinct created_at
        repo.out(TENANT, q.subspace(), q.pattern(), Map.of("from", "producer"), "plain-second", "n-2", null, null, null);
        var delayedCreated = admin(ctx -> ctx.select(TUPLES.CREATED_AT).from(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(q.subspace()))
                        .and(TUPLES.AVAILABLE_AT.isNotNull()))
                .fetchOne(TUPLES.CREATED_AT));
        for (var c : bothCensuses(q)) {
            assertThat(c.available()).isEqualTo(2);
            assertThat(c.delayed()).isEqualTo(1);
            assertThat(c.oldestClaimableCreatedAt()).as("the plain row, not the older delayed one").isNotNull()
                    .isAfter(delayedCreated);
        }
        // Claim the plain row: it leaves the claimable set; only the delayed row remains and it is not claimable.
        claimOrFail(q, "w1");
        for (var c : bothCensuses(q)) {
            assertThat(c.available()).isEqualTo(1);
            assertThat(c.claimed()).isEqualTo(1);
            assertThat(c.delayed()).isEqualTo(1);
            assertThat(c.oldestClaimableCreatedAt()).as("a claimed row is not claimable").isNull();
        }
    }

    @Test
    void aRenderedRow_carriesAvailableAt_untilAClaimClearingPathClearsIt() throws Exception {
        Q q = newQueue();
        out(q, 60L);
        var delayed = repo.rd(TENANT, q.subspace(), q.pattern(), 10, null, 0);
        assertThat(delayed).hasSize(1);
        assertThat(delayed.get(0).availableAt()).as("the delay is readable on the row").isNotNull();
        assertThat(delayed.get(0).availableAt().toInstant()).isEqualTo(availableAt(q).toInstant());
        moveAvailableAt(q, -30);
        var claimed = claimOrFail(q, "w1");
        // The claim itself leaves the (now past) stamp; the paths that clear the claim clear it.
        assertThat(claimed.tuple().availableAt()).as("a claim does not clear it").isNotNull();
        repo.release(TENANT, claimed.claimId(), "w1");
        assertThat(repo.rd(TENANT, q.subspace(), q.pattern(), 10, null, 0).get(0).availableAt())
                .as("release clears it").isNull();
    }

    // ── (h) a parked in does not spin on a delayed row; (i) announce mirrors ──

    @Test
    void aParkedIn_onASubspaceHoldingOnlyADelayedRow_returnsEmptyAtItsTimeout() {
        Q q = newQueue();
        out(q, 60L);
        long t0 = System.nanoTime();
        var got = repo.in(TENANT, q.subspace(), q.pattern(), "w1", (Long) 60L, 1L);
        long elapsedMs = (System.nanoTime() - t0) / 1_000_000;
        assertThat(got).as("the delayed row is not claimable").isEmpty();
        assertThat(elapsedMs).as("it parked for its timeout rather than returning at once").isGreaterThanOrEqualTo(900);
        assertThat(elapsedMs).as("and returned at the timeout").isLessThan(5_000);
    }

    @Test
    void anAnnounceProbe_skipsADelayedRow_andDoesNotSpendItsAnnounceBudget() throws Exception {
        Q q = newQueue();
        out(q, 60L);
        var spec = new TupleRepository.WaitSpec(q.subspace(), q.pattern(), 10, null,
                new TupleRepository.WaitSpec.Announce(1, 5));
        assertThat(repo.waitAny(TENANT, List.of(spec), 0)).as("a row nobody can claim is not announced").isEmpty();
        moveAvailableAt(q, -1);
        var announced = repo.waitAny(TENANT, List.of(spec), 0);
        assertThat(announced).hasSize(1);
        assertThat(announced.get(0).tuples().get(0).announceCount())
                .as("the first announcement is the first: the delayed probe spent none").isEqualTo(1);
    }

    @Test
    void aSubscriberAnnounceProbe_skipsADelayedRow() throws Exception {
        Q q = newQueue();
        out(q, 60L);
        var spec = new TupleRepository.WaitSpec(q.subspace(), q.pattern(), 10, null,
                new TupleRepository.WaitSpec.Announce(1, 5, "sub-1", null));
        assertThat(repo.waitAny(TENANT, List.of(spec), 0)).isEmpty();
        moveAvailableAt(q, -1);
        assertThat(repo.waitAny(TENANT, List.of(spec), 0)).hasSize(1);
    }

    // ── (i) a refire never moves available_at (review S3) ────────────────────

    @Test
    void aRefireOfTheSameWrite_keepsTheFirstWritesAvailableAt() throws Exception {
        Q q = newQueue();
        out(q, 60L);
        OffsetDateTime first = availableAt(q);
        assertThat(first).isNotNull();
        out(q, null);    // same keys+nonce, no delay: a refire, not a new row
        assertThat(availableAt(q)).as("a refire without a delay does not cancel the delay").isEqualTo(first);
        out(q, 5L);      // same write again with a different delay
        assertThat(availableAt(q)).as("a refire with a delay does not move it either").isEqualTo(first);
        assertThat(claimable(q)).as("still hidden by the first write's delay").isFalse();
    }

    // ── (j) a parked in picks a lapsing delay up on its one-second timer (review S1) ─

    @Test
    void aParkedIn_claimsADelayedRowWithinAboutASecondOfItsDelayPassing() throws Exception {
        Q q = newQueue();
        out(q, 2L);
        long t0 = System.nanoTime();
        // Parked for up to 10 s on a subspace whose only row is hidden for 2 s. No out or release
        // fires when the delay passes; the park loop's own one-second timer re-runs the claim.
        var got = repo.in(TENANT, q.subspace(), q.pattern(), "w1", (Long) 60L, 10L);
        double elapsed = (System.nanoTime() - t0) / 1e9;
        assertThat(got).as("claimed once the delay passed, without any signal").isPresent();
        assertThat(elapsed).as("about 2 s of delay plus at most one timer tick, never the 10 s timeout")
                .isBetween(1.5, 5.0);
    }
}
