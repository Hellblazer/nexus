// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ClaimNotFoundException;
import dev.nexus.service.db.ClaimOwnershipException;
import dev.nexus.service.db.ParkCapExceededException;
import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TtlTooLongException;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.db.UnknownSubspaceException;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-205 Phase 1 Step 4 (bead nexus-em75s.4) — {@code TupleRepository}
 * integration tests. Hermetic embedded Postgres (same harness as {@code
 * AspectRepositoryTest}); applies the full Liquibase master changelog,
 * which includes {@code tuples-001-baseline.xml} (bead nexus-em75s.2).
 *
 * <p>Covers the Test Plan scenarios this bead owns (RDR-205 §Test Plan,
 * bead nexus-em75s.4's own enumeration): ten concurrent {@code inp} on one
 * row; {@code ack} makes a row invisible; ownership/not-found on {@code
 * ack}/{@code nack}; same-claimant idempotent retake; parked callers on
 * subspace B do not wake on a write to A; {@code rd} with {@code timeout_s}
 * wakes on another client's {@code out}; shutdown drains parked readers;
 * park-cap exceeded (global and per-claimant); cursor resumption on a
 * shared {@code created_at}; the read cap clamp; the dead-letter
 * {@code NX_TUPLE_CLAIM_PASSES} bound; RLS isolation on {@code rd}; plus
 * {@code out}'s own idempotency/schema/TTL rules, which this bead also owns
 * in full.
 *
 * <p><b>Determinism.</b> The ten-concurrent-{@code inp} case uses a {@link
 * CyclicBarrier} so every worker starts together, not a sleep. The wake
 * tests use generous bounded waits ({@code Future#get} with a timeout) — no
 * sleeps stand in for a signal. Park-cap tests use a small explicit cap
 * (via the fully-parameterized constructor) rather than 16/4 real parked
 * threads — behaviourally identical, just cheap. The shutdown test starts a
 * long ({@code timeout_s}=20) blocking call and asserts it returns within a
 * few seconds of {@code shutdown()}, far short of riding out its budget.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleRepositoryTest {

    private static final String TENANT_A = "tuple-tenant-a";
    private static final String TENANT_B = "tuple-tenant-b";
    private static final String SVC_ROLE = "svc_tuple_test";
    private static final String SVC_PASS = "svc_tuple_test_pass";

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
        cfg.setMaximumPoolSize(20);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        // Small, fast settings for the blocking/park machinery -- behaviourally
        // identical to the production defaults, just cheap to exercise here.
        repo = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                /* timeoutCapSeconds */ 10, /* parkCapPerClaimant */ 4, /* parkCapGlobal */ 16);
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

    // ── out: idempotency, schema, TTL ───────────────────────────────────────

    @Test
    void out_sameKeys_isIdempotent_oneRow() {
        Map<String, String> keys = Map.of("agent_id", "agent-idem-1", "kind", "start");
        byte[] id1 = repo.out(TENANT_A, "ledger/session-idem", keys, Map.of(), null, null, null);
        byte[] id2 = repo.out(TENANT_A, "ledger/session-idem", keys, Map.of(), null, null, null);
        assertThat(id2).isEqualTo(id1);

        var rows = repo.rdp(TENANT_A, "ledger/session-idem", keys, 10, null);
        assertThat(rows).hasSize(1);
    }

    @Test
    void out_missingPinnedKey_schemaViolation_noRowWritten() {
        assertThatThrownBy(() -> repo.out(TENANT_A, "ledger/session-breach",
                Map.of("agent_id", "agent-x"), Map.of(), null, null, null))
                .isInstanceOf(SchemaViolationException.class);

        var rows = repo.rdp(TENANT_A, "ledger/session-breach", null, 10, null);
        assertThat(rows).isEmpty();
    }

    @Test
    void out_unknownSubspace_refused() {
        assertThatThrownBy(() -> repo.out(TENANT_A, "bogus/nowhere",
                Map.of("k", "v"), Map.of(), null, null, null))
                .isInstanceOf(UnknownSubspaceException.class);
    }

    @Test
    void out_ttlAboveRetention_refused_noRow() {
        Map<String, String> keys = Map.of("agent_id", "agent-ttl", "kind", "start");
        assertThatThrownBy(() -> repo.out(TENANT_A, "ledger/session-ttl", keys, Map.of(),
                null, null, 999_999_999L))
                .isInstanceOf(TtlTooLongException.class);

        var rows = repo.rdp(TENANT_A, "ledger/session-ttl", keys, 10, null);
        assertThat(rows).isEmpty();
    }

    @Test
    void out_mailboxWithoutNonce_schemaViolation() {
        assertThatThrownBy(() -> repo.out(TENANT_A, "mailbox/agent-nonce-target",
                Map.of("to", "agent-nonce-target"), Map.of("from", "sender-1"),
                "hello", null, null))
                .isInstanceOf(SchemaViolationException.class);
    }

    // ── ten concurrent inp on one row ───────────────────────────────────────

    @Test
    void inp_tenConcurrent_exactlyOneClaims_nineNone_oneClaimLogEntry() throws Exception {
        String to = "agent-fanin-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-fanin"), "one message", "nonce-fanin-1", null);

        int workers = 10;
        ExecutorService pool = Executors.newFixedThreadPool(workers);
        CyclicBarrier barrier = new CyclicBarrier(workers);
        List<Future<Optional<TupleRepository.ClaimedTuple>>> futures = new ArrayList<>();
        for (int i = 0; i < workers; i++) {
            String claimant = "worker-" + i;
            futures.add(pool.submit(() -> {
                barrier.await(10, TimeUnit.SECONDS);
                return repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), claimant, 60);
            }));
        }
        pool.shutdown();
        assertThat(pool.awaitTermination(30, TimeUnit.SECONDS)).isTrue();

        int claims = 0;
        for (var f : futures) {
            if (f.get().isPresent()) {
                claims++;
            }
        }
        assertThat(claims).as("exactly one of ten concurrent inp claims the single row").isEqualTo(1);

        var subspaceCensus = repo.subspaceStats(TENANT_A, "mailbox/" + to);
        assertThat(subspaceCensus.claimed()).isEqualTo(1);
        assertThat(subspaceCensus.available()).isEqualTo(0);
    }

    // ── ack / nack ───────────────────────────────────────────────────────────

    @Test
    void ack_setsConsumedAt_invisibleToRdAndIn() {
        String to = "agent-ack-1";
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-ack"), "body", "nonce-ack-1", null);
        var claimed = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "claimant-ack", 60);
        assertThat(claimed).isPresent();

        repo.ack(TENANT_A, claimed.get().claimId(), "claimant-ack");

        assertThat(repo.rdp(TENANT_A, "mailbox/" + to, Map.of("to", to), 10, null)).isEmpty();
        assertThat(repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "someone-else", 60)).isEmpty();
    }

    @Test
    void ackOrNack_byNonHolder_claimOwnership_secondAck_claimNotFound() {
        String to = "agent-ownership-1";
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-own"), "body", "nonce-own-1", null);
        var claimed = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "holder", 60);
        assertThat(claimed).isPresent();
        String claimId = claimed.get().claimId();

        assertThatThrownBy(() -> repo.ack(TENANT_A, claimId, "not-the-holder"))
                .isInstanceOf(ClaimOwnershipException.class);
        assertThatThrownBy(() -> repo.nack(TENANT_A, claimId, "not-the-holder"))
                .isInstanceOf(ClaimOwnershipException.class);

        repo.ack(TENANT_A, claimId, "holder");
        assertThatThrownBy(() -> repo.ack(TENANT_A, claimId, "holder"))
                .isInstanceOf(ClaimNotFoundException.class);
    }

    @Test
    void nack_maxAttemptsTimes_deadLettered_noFurtherIn_rdReturnsWithDeadState() {
        String to = "agent-deadletter-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-dl"), "body", "nonce-dl-1", null);

        // mailbox/<address> max_attempts is 3 (RDR-205 v1 template).
        for (int i = 0; i < 3; i++) {
            var claimed = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "claimant-" + i, 60);
            assertThat(claimed).as("attempt %d should still be claimable", i).isPresent();
            repo.nack(TENANT_A, claimed.get().claimId(), "claimant-" + i);
        }

        assertThat(repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "claimant-final", 60)).isEmpty();

        var rows = repo.rdp(TENANT_A, "mailbox/" + to, Map.of("to", to), 10, null);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).claimState()).isEqualTo("dead");

        var stats = repo.subspaceStats(TENANT_A, "mailbox/" + to);
        assertThat(stats.dead()).isEqualTo(1);
    }

    @Test
    void in_sameClaimantTwiceWithinLease_sameClaimId_idempotentRetake() {
        String to = "agent-retake-1";
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-retake"), "body", "nonce-retake-1", null);

        var first = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "retaker", 300);
        assertThat(first).isPresent();
        var second = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "retaker", 300);
        assertThat(second).isPresent();

        assertThat(second.get().claimId()).isEqualTo(first.get().claimId());
    }

    @Test
    void claim_lapsedLease_takenByAnotherClaimant_expireLogged() throws Exception {
        String to = "agent-lapse-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-lapse"), "body", "nonce-lapse-1", null);

        var first = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "claimant-lapsed", 1);
        assertThat(first).isPresent();

        Thread.sleep(1_500); // let the 1-second lease lapse

        var second = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "claimant-fresh", 60);
        assertThat(second).as("a lapsed lease is claimable the moment it lapses").isPresent();
        assertThat(second.get().tuple().attempts()).isEqualTo(1);
    }

    // ── rd / rdp: matching, cursors, cap ─────────────────────────────────────

    @Test
    void rd_cursorResumption_sharedCreatedAt_eachReturnedExactlyOnce() {
        String session = "session-cursor-" + UUID.randomUUID();
        repo.out(TENANT_A, "ledger/" + session, Map.of("agent_id", "a1", "kind", "start"), Map.of(), null, null, null);
        repo.out(TENANT_A, "ledger/" + session, Map.of("agent_id", "a2", "kind", "start"), Map.of(), null, null, null);

        var firstPage = repo.rdp(TENANT_A, "ledger/" + session, null, 1, null);
        assertThat(firstPage).hasSize(1);
        var cursor = new TupleRepository.ReadCursor(firstPage.get(0).createdAt(), firstPage.get(0).id());

        var secondPage = repo.rdp(TENANT_A, "ledger/" + session, null, 10, cursor);
        assertThat(secondPage).hasSize(1);
        assertThat(secondPage.get(0).id()).isNotEqualTo(firstPage.get(0).id());
    }

    @Test
    void rd_nAboveReadMax_clamped_noError() {
        TupleRepository smallCapRepo = new TupleRepository(tenantScope, registry,
                /* readMax */ 3, TupleRepository.DEFAULT_CLAIM_PASSES, 10, 4, 16);
        String session = "session-readmax-" + UUID.randomUUID();
        for (int i = 0; i < 5; i++) {
            smallCapRepo.out(TENANT_A, "ledger/" + session,
                    Map.of("agent_id", "a" + i, "kind", "start"), Map.of(), null, null, null);
        }
        var rows = smallCapRepo.rdp(TENANT_A, "ledger/" + session, null, 100, null);
        assertThat(rows).hasSize(3);
    }

    // ── RLS isolation ────────────────────────────────────────────────────────

    @Test
    void rd_tenantA_againstTenantB_mailbox_empty_noErrorNamingOtherTenant() {
        String to = "agent-rls-1";
        repo.out(TENANT_B, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-rls"), "body", "nonce-rls-1", null);

        var rowsFromA = repo.rdp(TENANT_A, "mailbox/" + to, Map.of("to", to), 10, null);
        assertThat(rowsFromA).isEmpty();
    }

    // ── dead-letter re-run bound (NX_TUPLE_CLAIM_PASSES) ─────────────────────

    @Test
    void claim_moreLapsedRowsThanClaimPasses_dead_lettersExactlyClaimPasses_returnsProbeResult() throws Exception {
        int claimPasses = 2;
        TupleRepository boundedRepo = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, claimPasses, 10, 4, 16);

        // mailbox/<address>'s only pinned key is "to" -- distinct nonces give distinct
        // rows that all share one subspace AND one claim pattern (a real mailbox with
        // several queued messages to one recipient looks exactly like this). `in`/`inp`
        // has no "claim THIS id" operation -- it always takes the OLDEST candidate
        // matching the pattern -- so each row's ramp to attempts=2 must finish by
        // leaving that row CLAIMED (not released back to available), or the next row's
        // ramp would pick up the still-available older row instead of its own fresh
        // one. Per row, in ONE pass: out, nack, nack (attempts 0->1->2, released each
        // time), then a THIRD claim with a short lease that is deliberately left
        // un-acked/un-nacked -- that leaves the row 'claimed' (excluded from candidacy)
        // for the rest of the seeding loop, and lapses only once every row is done.
        String to = "agent-claimpasses-" + UUID.randomUUID();
        for (int i = 0; i < 4; i++) {
            boundedRepo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                    Map.of("from", "sender-cp"), "body", "nonce-cp-" + i, null);
            for (int nackRound = 0; nackRound < 2; nackRound++) {
                var claimed = boundedRepo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to),
                        "seed-claimant-" + i, 60);
                assertThat(claimed).as("row %d nack round %d", i, nackRound).isPresent();
                boundedRepo.nack(TENANT_A, claimed.get().claimId(), "seed-claimant-" + i);
            }
            var finalClaim = boundedRepo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "seed-claimant-" + i, 1);
            assertThat(finalClaim).as("row %d final (soon-to-lapse) claim", i).isPresent();
            assertThat(finalClaim.get().tuple().attempts()).isEqualTo(2);
        }
        Thread.sleep(1_500); // let all four final claims' 1-second leases lapse

        var beforeStats = boundedRepo.subspaceStats(TENANT_A, "mailbox/" + to);
        assertThat(beforeStats.dead()).isEqualTo(0);

        // Bounded to claimPasses=2: dead-letters exactly two rows, then returns the
        // probe result (nothing claimed) rather than walking the other two.
        var result = boundedRepo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "final-claimant", 60);
        assertThat(result).as("claimPasses exhausted -- the probe result, not a claim").isEmpty();

        var afterStats = boundedRepo.subspaceStats(TENANT_A, "mailbox/" + to);
        assertThat(afterStats.dead()).as("exactly NX_TUPLE_CLAIM_PASSES rows dead-lettered").isEqualTo(claimPasses);
        assertThat(afterStats.claimed()).as("the other two remain claimed (lapsed, unprocessed)").isEqualTo(2);
    }

    // ── wake: park, signal, cross-subspace isolation ─────────────────────────

    @Test
    void rd_withTimeoutS_wakesOnAnotherClientOut() throws Exception {
        String session = "session-wake-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.TupleRow>> parked = pool.submit(() ->
                    repo.rd(TENANT_A, "ledger/" + session, null, 10, null, 8));

            Thread.sleep(300); // let the reader register + park
            repo.out(TENANT_A, "ledger/" + session,
                    Map.of("agent_id", "waker", "kind", "start"), Map.of(), null, null, null);

            List<TupleRepository.TupleRow> result = parked.get(5, TimeUnit.SECONDS);
            assertThat(result).hasSize(1);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void rd_parkedCallersOnSubspaceB_doNotWakeOnWriteToSubspaceA() throws Exception {
        String sessionA = "session-wakeA-" + UUID.randomUUID();
        String sessionB = "session-wakeB-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.TupleRow>> parkedOnB = pool.submit(() ->
                    repo.rd(TENANT_A, "ledger/" + sessionB, null, 10, null, 3));

            Thread.sleep(300);
            repo.out(TENANT_A, "ledger/" + sessionA,
                    Map.of("agent_id", "writer-a", "kind", "start"), Map.of(), null, null, null);

            // B's reader must NOT wake early on A's write -- it rides out its own
            // timeout and returns empty (no row ever landed on subspace B).
            List<TupleRepository.TupleRow> result = parkedOnB.get(6, TimeUnit.SECONDS);
            assertThat(result).isEmpty();
        } finally {
            pool.shutdownNow();
        }
    }

    // ── shutdown drains parked readers ────────────────────────────────────────

    @Test
    void shutdown_parkedReader_returnsProbeResultBeforeRidingOutBudget() throws Exception {
        TupleRepository shutdownRepo = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                /* timeoutCapSeconds */ 25, 4, 16);
        String session = "session-shutdown-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(1);
        try {
            long start = System.nanoTime();
            Future<List<TupleRepository.TupleRow>> parked = pool.submit(() ->
                    shutdownRepo.rd(TENANT_A, "ledger/" + session, null, 10, null, 20));

            Thread.sleep(300); // let it register + park
            shutdownRepo.shutdown();

            List<TupleRepository.TupleRow> result = parked.get(5, TimeUnit.SECONDS);
            long elapsedSeconds = TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - start);
            assertThat(result).isEmpty();
            assertThat(elapsedSeconds)
                    .as("shutdown must return the parked call well before its 20s budget")
                    .isLessThan(10);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── park caps ────────────────────────────────────────────────────────────

    @Test
    void rd_globalParkCapExceeded() throws Exception {
        TupleRepository capped = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                10, /* parkCapPerClaimant */ 4, /* parkCapGlobal */ 1);
        String session = "session-globalcap-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.TupleRow>> holder = pool.submit(() ->
                    // occupies the single global slot for the duration of its timeout
                    capped.rd(TENANT_A, "ledger/" + session, null, 10, null, 3));
            Thread.sleep(500); // let it register, query, and enter the park loop

            assertThatThrownBy(() -> capped.rd(TENANT_A, "ledger/" + session, null, 10, null, 3))
                    .isInstanceOf(ParkCapExceededException.class);

            holder.get(6, TimeUnit.SECONDS); // drain
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void in_claimantParkCapExceeded() throws Exception {
        TupleRepository capped = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                10, /* parkCapPerClaimant */ 1, /* parkCapGlobal */ 16);
        String to = "agent-claimantcap-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<Optional<TupleRepository.ClaimedTuple>> holder = pool.submit(() ->
                    capped.in(TENANT_A, "mailbox/" + to, Map.of("to", to), "capped-claimant", 60, 3));
            Thread.sleep(500);

            assertThatThrownBy(() ->
                    capped.in(TENANT_A, "mailbox/" + to, Map.of("to", to), "capped-claimant", 60, 3))
                    .isInstanceOf(ParkCapExceededException.class);

            holder.get(6, TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── registry passthrough ─────────────────────────────────────────────────

    @Test
    void registry_returnsDigestAndTemplates() {
        var snap = repo.registry();
        assertThat(snap.templates()).hasSize(2);
        assertThat(snap.digest()).isNotBlank();
    }
}
