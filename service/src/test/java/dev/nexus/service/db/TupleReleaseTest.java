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
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatExceptionOfType;

/**
 * RDR-211 Phase 1 Step 1 (bead nexus-rplay.2): {@code release(claim_id, claimant)}
 * — a hand-back that is NOT a failure. Closes RDR-211 Gap 4: nothing today ends a
 * claim without either consuming the tuple ({@code ack}) or counting a failed
 * attempt ({@code nack}). See {@code docs/rdr/rdr-211-board-queue-and-lock-tuple-
 * templates.md} §Technical Design "The release operation" and §Test Plan.
 *
 * <p>Promoted from the throwaway {@code TupleReleaseSpikeTest} (T2
 * {@code nexus_rdr/211-spike-1-2026-09-16}), which proved the Critical Assumption
 * this method rests on: {@code releaseOrDeadLetter} can be reused with {@code
 * attempts} unchanged and {@code maxAttempts} as {@link Long#MAX_VALUE} so a
 * release can never itself spend the template's failure budget or dead-letter the
 * tuple. Modeled on {@link TupleRenewTest}'s fixture shape, with the compare-and-
 * swap race test {@link TupleRenewTest#renew_sweepReleasedBetweenReadAndUpdate_claimNotFound_noRenewLogRow}
 * exercises for renew, driven here for release.
 *
 * <p>Package-local to {@code dev.nexus.service.db} to reach the package-private
 * {@link TupleRepository#TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY} seam, the
 * same shape as {@link TupleRenewTest} and {@link TupleClaimCasTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleReleaseTest {

    private static final String SVC_ROLE = "svc_tuple_release_test";
    private static final String SVC_PASS = "svc_tuple_release_test_pass";

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

    // ── helpers (mirrors TupleRenewTest) ────────────────────────────────────

    private record Seeded(String tenant, String subspace, String to, byte[] id, String claimId) {
    }

    private Seeded outAndClaim(String label, String claimant, Long ttlSeconds, long leaseSeconds) {
        String tenant = "tuple-release-" + label + "-" + UUID.randomUUID();
        String to = "agent-release-" + label + "-" + UUID.randomUUID();
        String subspace = "mailbox/" + to;
        byte[] id = repo.out(tenant, subspace, Map.of("to", to), Map.of("from", "sender-release"),
                "body", "nonce-release-" + label, ttlSeconds);
        var claimed = repo.inp(tenant, subspace, Map.of("to", to), claimant, leaseSeconds);
        assertThat(claimed).as("the request must be claimable").isPresent();
        return new Seeded(tenant, subspace, to, id, claimed.get().claimId());
    }

    private Seeded outAndClaim(String label, String claimant) {
        return outAndClaim(label, claimant, null, 60);
    }

    private org.jooq.Record2<Integer, String> rawAttemptsAndClaimState(byte[] id) {
        try (Connection su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLES.ATTEMPTS, TUPLES.CLAIM_STATE)
                    .from(TUPLES)
                    .where(TUPLES.ID.eq(id))
                    .fetchOne();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

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

    // ── the happy path: attempts unchanged, transition logged, tuple available ─

    /**
     * RDR Test Plan: "a worker releases a task: attempts unchanged, the task is
     * available, a waiting worker wakes" (the wake half is
     * {@link #aParkedInOnTheSubspaceWakesAfterRelease} below).
     */
    @Test
    void releaseLeavesAttemptsUnchangedAndLogsANewReleaseTransitionWithNoSchemaChange() {
        Seeded s = outAndClaim("happy", "worker-1");
        int before = rawAttemptsAndClaimState(s.id()).value1();

        repo.release(s.tenant(), s.claimId(), "worker-1");

        var after = rawAttemptsAndClaimState(s.id());
        assertThat(after.value1()).as("release must not count an attempt").isEqualTo(before);
        assertThat(after.value2()).as("released back to available -- claim_state cleared").isNull();
        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("a NEW 'release' transition value, accepted by the plain TEXT column with no CHECK constraint")
                .containsExactly("claim", "release");

        // The tuple is available again: a fresh claimant can take it.
        var reclaimed = repo.inp(s.tenant(), s.subspace(), Map.of("to", s.to()), "worker-2", 60);
        assertThat(reclaimed).as("released tuple must be claimable by someone else").isPresent();
    }

    /**
     * RDR Test Plan: "two processes run out on the same lock at once... a lock
     * holder releases: the tuple is not consumed and the next in gets it" is
     * covered for the lock template specifically in TupleLockFlagTest (bead
     * nexus-rplay.3); this proves the same release-never-dead-letters property
     * against an ordinary take-enabled template at its own attempts ceiling.
     */
    @Test
    void releaseNeverDeadLettersEvenAtTheTemplatesMaxAttemptsCeiling() {
        // mailbox/<address> caps max_attempts at 3 (mailbox.yaml). Drive attempts to
        // the ceiling via repeated nack, then confirm release still just releases.
        Seeded s = outAndClaim("ceiling", "worker-1");
        repo.nack(s.tenant(), s.claimId(), "worker-1");
        var reclaim1 = repo.inp(s.tenant(), s.subspace(), Map.of("to", s.to()), "worker-1", 60);
        assertThat(reclaim1).isPresent();
        repo.nack(s.tenant(), reclaim1.get().claimId(), "worker-1");
        var reclaim2 = repo.inp(s.tenant(), s.subspace(), Map.of("to", s.to()), "worker-1", 60);
        assertThat(reclaim2).isPresent();
        // attempts is now 2; one more nack would dead-letter at max_attempts=3. Release instead.
        int beforeAttempts = rawAttemptsAndClaimState(s.id()).value1();
        assertThat(beforeAttempts).isEqualTo(2);

        repo.release(s.tenant(), reclaim2.get().claimId(), "worker-1");

        var after = rawAttemptsAndClaimState(s.id());
        assertThat(after.value1()).as("release leaves attempts exactly where they were").isEqualTo(2);
        assertThat(after.value2()).as("never dead-lettered by a release").isNull();
    }

    // ── refusals ─────────────────────────────────────────────────────────────

    /** RDR Test Plan: "release on a lapsed claim: ClaimNotFoundException". */
    @Test
    void releaseOfALapsedClaimRaisesClaimNotFoundExceptionAndWritesNoLogRow() {
        Seeded s = outAndClaim("lapsed", "worker-1");
        lapseLease(s.id());

        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.release(s.tenant(), s.claimId(), "worker-1"));

        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("no release row for a claim that was not live").containsExactly("claim");
    }

    @Test
    void releaseOfAConsumedClaimRaisesClaimNotFoundException() {
        Seeded s = outAndClaim("consumed", "worker-1");
        repo.ack(s.tenant(), s.claimId(), "worker-1");

        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.release(s.tenant(), s.claimId(), "worker-1"));

        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("ack's own log row only -- no release row for an already-consumed tuple")
                .containsExactly("claim", "ack");
    }

    @Test
    void releaseByAnotherClaimantIsRefused() {
        Seeded s = outAndClaim("owner", "worker-1");

        assertThatExceptionOfType(ClaimOwnershipException.class)
                .isThrownBy(() -> repo.release(s.tenant(), s.claimId(), "worker-2"));

        assertThat(transitionsFor(s.tenant(), s.id())).containsExactly("claim");
    }

    // ── the waiter wake ──────────────────────────────────────────────────────

    /**
     * The RDR's third clause: "signal waiters after commit". A blocking {@code in}
     * parked on the subspace (worker-2, with its own long timeout) must wake almost
     * immediately once {@code release} commits -- not ride out its full timeout.
     * RDR Test Plan: "a worker releases a task: ... a waiting worker wakes."
     *
     * <p>The elapsed-time bound alone is NOT a sufficient pin: {@link
     * TupleWaitRegistry} also has its own 1-second timer fallback (see {@link
     * TupleRepository#setTestOnlySignalHook}'s javadoc), so a {@code release} that
     * forgot to signal at all would still pass an elapsed-time-only assertion on any
     * box where that fallback fires inside the bound -- confirmed by mutation (this
     * bead's own fix round: deleting {@code release}'s {@code signalAll} call left the
     * elapsed-time assertion green). The signal count, pinned through the
     * cross-package test hook exactly as {@code TupleAckWithReplyTest} pins {@code
     * ackWithReply}'s signal, is what a missing {@code signalAll} call actually fails.
     */
    @Test
    void aParkedInOnTheSubspaceWakesAfterRelease() throws Exception {
        Seeded s = outAndClaim("wake", "worker-1");
        var signals = new AtomicInteger();
        TupleRepository.setTestOnlySignalHook((tenant, subspace) -> {
            if (s.tenant().equals(tenant) && s.subspace().equals(subspace)) {
                signals.incrementAndGet();
            }
        });

        try {
            CompletableFuture<Optional<TupleRepository.ClaimedTuple>> parked = CompletableFuture.supplyAsync(() ->
                    repo.in(s.tenant(), s.subspace(), Map.of("to", s.to()), "worker-2", 60L, 20L));

            // Give the background thread time to register + park before releasing.
            Thread.sleep(500);

            long start = System.nanoTime();
            repo.release(s.tenant(), s.claimId(), "worker-1");

            var result = parked.get(20, TimeUnit.SECONDS);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

            assertThat(result).as("worker-2's parked in() must claim the released tuple").isPresent();
            assertThat(elapsedMs)
                    .as("woken quickly -- generous bound for CI jitter")
                    .isLessThan(15_000);
            assertThat(signals.get())
                    .as("release must itself call signalAll on this (tenant, subspace) at least once "
                        + "-- the property the elapsed-time bound above cannot, by itself, tell apart "
                        + "from the registry's own 1s timer fallback")
                    .isGreaterThanOrEqualTo(1);
        } finally {
            TupleRepository.setTestOnlySignalHook(null);
        }
    }

    // ── the compare-and-swap ─────────────────────────────────────────────────

    /**
     * {@link TupleRepository#liveClaimRow} reads without a lock, so the sweep's
     * release arm can move the row between that read and the update. release must
     * then match zero rows and fail, not release a row it no longer holds. Same
     * shape as {@link TupleRenewTest#renew_sweepReleasedBetweenReadAndUpdate_claimNotFound_noRenewLogRow}.
     */
    @Test
    void release_sweepReleasedBetweenReadAndUpdate_claimNotFound_noReleaseLogRow() {
        Seeded s = outAndClaim("race", "worker-1");
        AtomicInteger hookRuns = new AtomicInteger();
        AtomicReference<TupleRepository.ReleaseBatchResult> sweep = new AtomicReference<>();
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> {
            hookRuns.incrementAndGet();
            lapseLease(s.id());
            sweep.set(repo.releaseLapsedClaimsBatch(s.tenant(), 300, null));
        };

        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.release(s.tenant(), s.claimId(), "worker-1"));

        assertThat(hookRuns.get()).as("the race window opened exactly once").isEqualTo(1);
        assertThat(sweep.get()).isNotNull();
        assertThat(sweep.get().released()).as("the sweep really did release the row").isEqualTo(1);
        assertThat(transitionsFor(s.tenant(), s.id()))
                .as("the sweep's expire row, and NO release row after it")
                .containsExactly("claim", "expire");
    }
}
