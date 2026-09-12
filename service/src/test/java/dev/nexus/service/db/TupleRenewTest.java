// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatExceptionOfType;

/**
 * RDR-206 Phase 1 Step 3 (bead nexus-h61dl.4): {@code renew}, which extends a live
 * claim's lease without consuming the tuple and without counting an attempt.
 *
 * <p>The property that matters most here is what renew must NOT do. A renew on a
 * lapsed claim must fail rather than resurrect it: that is the pgmq {@code set_vt}
 * anti-pattern the design avoids by construction, since {@link
 * TupleRepository#liveClaimRow} already requires {@code lease_until > now()}. Every
 * surveyed system with renewal bounds it at an absolute deadline, and the two that
 * fail loud (JavaSpaces {@code UnknownLeaseException}, SQS {@code MessageNotInflight})
 * are the ones this matches.
 *
 * <p>Package-local to {@code dev.nexus.service.db} to reach the package-private
 * read-to-update seam, the same shape as {@link TupleClaimCasTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleRenewTest {

    private static final String SVC_ROLE = "svc_tuple_renew_test";
    private static final String SVC_PASS = "svc_tuple_renew_test_pass";

    /** ``mailbox.yaml``'s cap. A renew above it is refused, never clamped. */
    private static final long MAILBOX_MAX_LEASE_S = 900;

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
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

        tenantScope = new TenantScope(svcDs);
        registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        repo = new TupleRepository(tenantScope, registry);
    }

    @AfterEach
    void restoreSeam() {
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
    }

    @AfterAll
    void stopAll() {
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private record Seeded(String tenant, String subspace, String to, byte[] id, String claimId) {
    }

    /** out + in on a fresh tenant and address, with an optional tuple ttl. */
    private Seeded outAndClaim(String label, String claimant, Long ttlSeconds, long leaseSeconds) {
        String tenant = "tuple-renew-" + label + "-" + UUID.randomUUID();
        String to = "agent-renew-" + label + "-" + UUID.randomUUID();
        String subspace = "mailbox/" + to;
        byte[] id = repo.out(tenant, subspace, Map.of("to", to), Map.of("from", "sender-renew"),
                "body", "nonce-renew-" + label, ttlSeconds);
        var claimed = repo.inp(tenant, subspace, Map.of("to", to), claimant, leaseSeconds);
        assertThat(claimed).as("the request must be claimable").isPresent();
        return new Seeded(tenant, subspace, to, id, claimed.get().claimId());
    }

    private Seeded outAndClaim(String label, String claimant) {
        return outAndClaim(label, claimant, null, 60);
    }

    /** The row as the database holds it, read outside the repo's own API. */
    private org.jooq.Record4<OffsetDateTime, OffsetDateTime, Integer, String> rawRow(byte[] id) {
        try (Connection su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLES.LEASE_UNTIL, TUPLES.EXPIRES_AT, TUPLES.ATTEMPTS, TUPLES.CLAIM_STATE)
                    .from(TUPLES)
                    .where(TUPLES.ID.eq(id))
                    .fetchOne();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Lapse the row's lease directly; the repo's API only ever writes "now". */
    private void lapseLease(byte[] id) {
        try (Connection su = pg.createConnection("")) {
            org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .update(TUPLES)
                    .set(TUPLES.LEASE_UNTIL, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(5))
                    .where(TUPLES.ID.eq(id))
                    .execute();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private List<String> transitionsFor(String tenant, byte[] id) {
        try (Connection su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLE_CLAIM_LOG.TRANSITION)
                    .from(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant).and(TUPLE_CLAIM_LOG.TUPLE_ID.eq(id)))
                    .orderBy(TUPLE_CLAIM_LOG.LOG_ID.asc())
                    .fetch(TUPLE_CLAIM_LOG.TRANSITION);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    // ── the happy path ───────────────────────────────────────────────────────

    @Test
    void renewWithinTheLeaseMovesLeaseUntilForwardAndKeepsTheClaim() {
        Seeded s = outAndClaim("happy", "worker-1");
        OffsetDateTime before = rawRow(s.id()).value1();

        OffsetDateTime renewed = repo.renew(s.tenant(), s.claimId(), "worker-1", 600);

        assertThat(renewed).as("the new deadline is returned").isAfter(before);
        assertThat(rawRow(s.id()).value4()).as("the claim is still held").isEqualTo("claimed");
        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("one renew log row, after the claim").containsExactly("claim", "renew");
    }

    /**
     * A renew is not an attempt. {@code nack} counts one and can dead-letter at the cap;
     * a holder saying "still working" must never spend one, or a long task would
     * dead-letter itself by doing exactly what the feature asks of it.
     */
    @Test
    void renewDoesNotCountAnAttempt() {
        Seeded s = outAndClaim("attempts", "worker-1");
        int before = rawRow(s.id()).value3();

        repo.renew(s.tenant(), s.claimId(), "worker-1", 600);
        repo.renew(s.tenant(), s.claimId(), "worker-1", 600);
        repo.renew(s.tenant(), s.claimId(), "worker-1", 600);

        assertThat(rawRow(s.id()).value3())
                .as("three renews, no attempt spent").isEqualTo(before);
    }

    /**
     * The returned deadline is the one the database holds, to the microsecond. The
     * claim path already pins this (RDR-205 follow-on nexus-mvfm9): the JVM clock can
     * carry more digits than a Postgres TIMESTAMPTZ, so a value returned from memory
     * and the same row read back must not disagree on the fractional second.
     */
    @Test
    void theReturnedDeadlineMatchesTheStoredRowToTheMicrosecond() {
        Seeded s = outAndClaim("micros", "worker-1");

        OffsetDateTime renewed = repo.renew(s.tenant(), s.claimId(), "worker-1", 600);

        assertThat(rawRow(s.id()).value1().toInstant())
                .as("returned value and stored value are the same instant exactly")
                .isEqualTo(renewed.toInstant());
        assertThat(renewed.truncatedTo(ChronoUnit.MICROS))
                .as("and it carries no sub-microsecond digits of its own")
                .isEqualTo(renewed);
    }

    // ── the clamp ────────────────────────────────────────────────────────────

    /**
     * A claim never outlives its tuple. The same rule the claim path applies, which is
     * why both now call one helper: two copies of this arithmetic could drift, and the
     * one that drifted would hand out a lease past a row the sweep is entitled to purge.
     */
    @Test
    void aRenewPastTheTuplesExpiryIsClampedToIt() {
        // A 30s tuple, so expires_at is well inside the 900s the renew asks for.
        Seeded s = outAndClaim("clamp", "worker-1", 30L, 20);
        OffsetDateTime expiresAt = rawRow(s.id()).value2();

        OffsetDateTime renewed = repo.renew(s.tenant(), s.claimId(), "worker-1", MAILBOX_MAX_LEASE_S);

        assertThat(renewed).as("clamped to the tuple's own expiry, never past it")
                .isEqualTo(expiresAt.truncatedTo(ChronoUnit.MICROS));
        assertThat(rawRow(s.id()).value1().toInstant()).isEqualTo(renewed.toInstant());
    }

    // ── refusals ─────────────────────────────────────────────────────────────

    /**
     * No resurrection. A lapsed claim is gone, and the holder that missed its window
     * learns that instead of silently extending a claim it no longer holds. pgmq's
     * {@code set_vt} is the surveyed counter-example that does resurrect.
     */
    @Test
    void aRenewAfterTheLeaseHasLapsedIsRefusedAndWritesNoLogRow() {
        Seeded s = outAndClaim("lapsed", "worker-1");
        lapseLease(s.id());

        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.renew(s.tenant(), s.claimId(), "worker-1", 600));

        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("no renew row for a claim that was not live").containsExactly("claim");
    }

    @Test
    void aRenewByAnotherClaimantIsRefused() {
        Seeded s = outAndClaim("owner", "worker-1");

        assertThatExceptionOfType(ClaimOwnershipException.class)
                .isThrownBy(() -> repo.renew(s.tenant(), s.claimId(), "worker-2", 600));

        assertThat(transitionsFor(s.tenant(), s.id())).containsExactly("claim");
    }

    @Test
    void aRenewAboveTheTemplateCapIsRefusedRatherThanClamped() {
        Seeded s = outAndClaim("cap", "worker-1");

        assertThatExceptionOfType(LeaseTooLongException.class)
                .isThrownBy(() -> repo.renew(s.tenant(), s.claimId(), "worker-1",
                        MAILBOX_MAX_LEASE_S + 1));

        assertThat(transitionsFor(s.tenant(), s.id())).containsExactly("claim");
    }

    /**
     * A non-positive lease needs neither the row nor the template to refuse, so it is
     * refused before the transaction opens — the same placement rule Step 2 settled for
     * reply refusals. Asserted through the exception's field rather than its message,
     * because the message names the value too.
     */
    @Test
    void aNonPositiveLeaseIsRefusedAsASchemaViolation() {
        Seeded s = outAndClaim("zero", "worker-1");

        for (long bad : new long[]{0, -1}) {
            assertThatExceptionOfType(SchemaViolationException.class)
                    .isThrownBy(() -> repo.renew(s.tenant(), s.claimId(), "worker-1", bad))
                    .satisfies(e -> assertThat(e.field()).isEqualTo("lease_s"));
        }
        assertThat(transitionsFor(s.tenant(), s.id())).containsExactly("claim");
    }

    // ── the compare-and-swap ─────────────────────────────────────────────────

    /**
     * {@link TupleRepository#liveClaimRow} reads without a lock, so the sweep's release
     * arm can move the row between that read and the update. The renew must then match
     * zero rows and fail, not extend a lease on a row it no longer holds.
     *
     * <p>Built deterministically rather than with a sleep: the read-to-update seam runs
     * the REAL sweep batch inside the window. The vacuity guard is the same one
     * {@link TupleClaimCasTest} uses — assert the hook fired exactly once AND that the
     * sweep it ran actually released a row, so a run where the window never opened
     * fails instead of passing on an unraced path.
     */
    @Test
    void renew_sweepReleasedBetweenReadAndUpdate_claimNotFound_noRenewLogRow() {
        Seeded s = outAndClaim("race", "worker-1");
        AtomicInteger hookRuns = new AtomicInteger();
        AtomicReference<TupleRepository.ReleaseBatchResult> sweep = new AtomicReference<>();
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> {
            hookRuns.incrementAndGet();
            lapseLease(s.id());
            sweep.set(repo.releaseLapsedClaimsBatch(s.tenant(), 300, null));
        };

        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.renew(s.tenant(), s.claimId(), "worker-1", 600));

        assertThat(hookRuns.get()).as("the race window opened exactly once").isEqualTo(1);
        assertThat(sweep.get()).isNotNull();
        assertThat(sweep.get().released()).as("the sweep really did release the row").isEqualTo(1);
        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("the sweep's expire row, and NO renew row after it")
                .containsExactly("claim", "expire");
    }

    /**
     * The OTHER way the row can move under a renew, and the one term of
     * {@link TupleRepository#liveClaimCondition} nothing exercised until now
     * (nexus-h61dl.4 review, substantive-critic significant 1): the holder's own ack
     * lands in the window, so the row is still {@code claimed} under the same
     * {@code claim_id} and only {@code consumed_at} has changed.
     *
     * <p>The sweep-race case above cannot reach this. It moves {@code claim_state} and
     * {@code claim_id}, so it would still fail with a predicate carrying no
     * {@code consumed_at IS NULL} term. Here those two are untouched and that term is
     * the only thing standing between this renew and extending the lease on a tuple
     * that has already been consumed.
     *
     * <p>The seam is disarmed inside its own hook before the nested ack, which shares
     * it — otherwise the ack re-enters the window and recurses.
     */
    @Test
    void renew_ackLandedBetweenReadAndUpdate_claimNotFound_noRenewLogRow() {
        Seeded s = outAndClaim("consumed", "worker-1");
        AtomicInteger hookRuns = new AtomicInteger();
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> {
            TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
            hookRuns.incrementAndGet();
            repo.ack(s.tenant(), s.claimId(), "worker-1");
        };

        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.renew(s.tenant(), s.claimId(), "worker-1", 600));

        assertThat(hookRuns.get()).as("the race window opened exactly once").isEqualTo(1);
        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("the ack consumed it; no renew row may follow")
                .containsExactly("claim", "ack");
    }

    // ── RDR-206 Phase 1 Step 4 (bead nexus-h61dl.5): a renew is invisible to the
    //    sweep and to the census, and it is not an attempt ──────────────────────

    /**
     * The sweep's release arm does not touch a renewed claim.
     *
     * <p>The arm selects on {@code claim_state = 'claimed' AND consumed_at IS NULL AND
     * lease_until < now()}, so a renewed row simply stops matching. There is no
     * JVM-side cache of deadlines to go stale. The bead asks this be PROVED rather than
     * read off that predicate.
     *
     * <p>TWO rows, identical in every respect except the renew: each claimed with a
     * one-second lease, both left until that second has passed, only one renewed. The
     * control is what makes this test able to fail — a sweep that released nothing
     * because it found nothing looks exactly like a sweep that correctly spared a
     * renewed row, so the un-renewed sibling's release is what proves the sweep was
     * live, looking at this tenant, and would have taken the other one too.
     *
     * <p>The wait is real elapsed time and cannot be faked: a renew of an ALREADY
     * lapsed claim is refused by design, so the renew has to happen while the claim
     * lives and the original deadline has to pass afterwards. It is safe in the only
     * direction that matters — a loaded box makes MORE time pass, never less, so
     * contention can only strengthen the precondition, and the control asserts the
     * precondition actually held.
     */
    @Test
    void theSweepReleasesALapsedSiblingButNotTheRenewedClaim() throws Exception {
        String tenant = "tuple-renew-sweep-" + UUID.randomUUID();
        String keptTo = "agent-sweep-kept-" + UUID.randomUUID();
        String goneTo = "agent-sweep-released-" + UUID.randomUUID();

        byte[] keptId = repo.out(tenant, "mailbox/" + keptTo, Map.of("to", keptTo),
                Map.of("from", "sender-renew"), "kept", "nonce-kept", null);
        byte[] goneId = repo.out(tenant, "mailbox/" + goneTo, Map.of("to", goneTo),
                Map.of("from", "sender-renew"), "released", "nonce-released", null);
        var keptClaim = repo.inp(tenant, "mailbox/" + keptTo, Map.of("to", keptTo), "worker-1", 1);
        var goneClaim = repo.inp(tenant, "mailbox/" + goneTo, Map.of("to", goneTo), "worker-2", 1);
        assertThat(keptClaim).isPresent();
        assertThat(goneClaim).isPresent();

        // Renewed while still live; its sibling is left to lapse.
        repo.renew(tenant, keptClaim.get().claimId(), "worker-1", 600);
        Thread.sleep(1500);   // the one-second leases are now past, both of them

        TupleRepository.ReleaseBatchResult batch = repo.releaseLapsedClaimsBatch(tenant, 300, null);

        assertThat(batch.released())
                .as("the control lapsed and was released, so the sweep was live and "
                    + "capable — which is what makes the other row's survival mean "
                    + "something")
                .isEqualTo(1);
        assertThat(rawRow(goneId).value4())
                .as("the un-renewed sibling went back to available").isNull();
        assertThat(rawRow(keptId).value4())
                .as("the renewed claim is untouched").isEqualTo("claimed");
        assertThat(transitionsFor(tenant, keptId))
                .as("claim then renew, and no expire: the sweep never released it")
                .containsExactly("claim", "renew");
        assertThat(transitionsFor(tenant, goneId))
                .as("the control's own expire row").containsExactly("claim", "expire");
    }

    /**
     * The census counts a renewed claim as {@code claimed} — not available, not dead.
     * A renew moves {@code lease_until} and nothing else, so nothing about how the row
     * is counted may move with it.
     */
    @Test
    void theCensusCountsARenewedClaimAsClaimed() {
        Seeded s = outAndClaim("census", "worker-1");

        repo.renew(s.tenant(), s.claimId(), "worker-1", 600);

        var census = repo.subspaceStats(s.tenant(), s.subspace());
        assertThat(census.claimed()).as("still claimed").isEqualTo(1);
        assertThat(census.available()).as("not handed back").isZero();
        assertThat(census.dead()).as("not dead-lettered").isZero();
        assertThat(census.consumed()).as("not consumed").isZero();
        assertThat(census.total()).isEqualTo(1);
    }

    /**
     * The claim log of a renewed-then-acked claim reads exactly claim, renew, ack.
     *
     * <p>The absence of {@code expire} is the assertion that matters: it is what an
     * audit would read as the claim having lapsed, and a renew must never look like
     * one. {@code containsExactly} also pins that {@code renew} is non-terminal — the
     * ack still follows it.
     */
    @Test
    void theClaimLogOfARenewedThenAckedClaimReadsClaimRenewAck() {
        Seeded s = outAndClaim("log", "worker-1");

        repo.renew(s.tenant(), s.claimId(), "worker-1", 600);
        repo.ack(s.tenant(), s.claimId(), "worker-1");

        assertThat(transitionsFor(s.tenant(), s.id()))
                .containsExactly("claim", "renew", "ack");
    }

    /**
     * Attempts survive a renew AND the sweep pass that follows it.
     *
     * <p>{@code renewDoesNotCountAnAttempt} above already pins the repository call in
     * isolation (Step 3). This is the Step 4 half the bead asks for and deliberately
     * not a copy of it: the question here is whether a renewed row that a sweep has
     * since walked past still carries its original attempt count, since the sweep's
     * release arm is the other writer of that column.
     */
    @Test
    void attemptsSurviveARenewAndTheSweepThatWalksPastIt() {
        Seeded s = outAndClaim("attempts-sweep", "worker-1");
        int before = rawRow(s.id()).value3();

        repo.renew(s.tenant(), s.claimId(), "worker-1", 600);
        TupleRepository.ReleaseBatchResult batch = repo.releaseLapsedClaimsBatch(s.tenant(), 300, null);

        assertThat(batch.released()).as("the sweep found nothing lapsed to release").isZero();
        assertThat(rawRow(s.id()).value3())
                .as("neither the renew nor the sweep spent an attempt").isEqualTo(before);
    }
}
