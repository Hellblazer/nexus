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
import java.time.OffsetDateTime;
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

    // ── rd/rdp: template resolution (nexus-em75s.35) ────────────────────────
    //
    // rd and rdp both funnel through the private queryOnce, which — unlike
    // out's and in/inp's own "Once" helper (claimOnce) — never resolved the
    // subspace against the TemplateRegistry: an unregistered subspace read
    // as an empty result instead of UnknownSubspaceException, same class of
    // gap out_unknownSubspace_refused above already pins for out().

    @Test
    void rdp_unknownSubspace_refused() {
        assertThatThrownBy(() -> repo.rdp(TENANT_A, "bogus/nowhere", Map.of("k", "v"), 10, null))
                .isInstanceOf(UnknownSubspaceException.class);
    }

    @Test
    void rd_unknownSubspace_refused() {
        assertThatThrownBy(() -> repo.rd(TENANT_A, "bogus/nowhere", Map.of("k", "v"), 10, null, 0))
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

    /**
     * nexus-em75s.36: {@code ledger/<session_id>}'s {@code kind} key is pinned to
     * {@code {start, report}} (RDR-205 §Technical Design "Registry" ~844). A value
     * outside that set is refused as a {@code SchemaViolation} naming {@code kind},
     * and no row is written.
     */
    @Test
    void out_ledgerKindOutsidePinnedSet_schemaViolation_namesKind_noRowWritten() {
        assertThatThrownBy(() -> repo.out(TENANT_A, "ledger/session-kind-breach",
                Map.of("agent_id", "agent-x", "kind", "other"), Map.of(), null, null, null))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("kind");

        var rows = repo.rdp(TENANT_A, "ledger/session-kind-breach", null, 10, null);
        assertThat(rows).isEmpty();
    }

    /**
     * RDR-205 Phase 1 review (nexus-em75s.7, the RDR-110 C3 class recurring):
     * {@code computeId}'s ORIGINAL join delimited each field with a fixed separator
     * byte and joined a key/dim's name to its value with a plain {@code '='}, neither
     * escaped -- so a value that itself embeds "{@code <delimiter>from=...}" could
     * make two logically distinct {@code (from, nonce)} pairs hash to the identical
     * id. Constructed below with an underscore standing in for that separator (a
     * printable, storable character demonstrating the same field-boundary-collision
     * class): {@code from1 = "sender1" + delim + "nonce=" + nonce1}, {@code nonce2 =
     * nonce1 + delim + "nonce=" + nonce1B} against {@code from2 = "sender1"} -- the
     * two "to=agentX<delim>from=sender1<delim>nonce=..." byte sequences a naive
     * delimiter-joined encoding would produce are identical for both rows despite
     * every one of {@code from}/{@code nonce} differing. The length-prefixed fix
     * (RDR-205 §Technical Design) makes this unreachable regardless of what a field
     * contains.
     */
    @Test
    void out_distinctFromNoncePairsWithEmbeddedFieldBoundary_produceDistinctIds() {
        String delim = "_"; // stand-in field separator (see javadoc)
        String to = "agent-collide-" + UUID.randomUUID();
        String subspace = "mailbox/" + to;

        String from1 = "sender1" + delim + "nonce=n1";
        String nonce1 = "n1b";

        String from2 = "sender1";
        String nonce2 = "n1" + delim + "nonce=n1b";

        byte[] id1 = repo.out(TENANT_A, subspace, Map.of("to", to), Map.of("from", from1),
                "body", nonce1, null);
        byte[] id2 = repo.out(TENANT_A, subspace, Map.of("to", to), Map.of("from", from2),
                "body", nonce2, null);

        assertThat(id2)
                .as("distinct (from, nonce) splits of the same field-boundary bytes must not collide")
                .isNotEqualTo(id1);

        var rows = repo.rdp(TENANT_A, subspace, Map.of("to", to), 10, null);
        assertThat(rows).as("two distinct rows, not one collapsed by a shared id").hasSize(2);
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

    /**
     * RDR-205 Phase 1 review (nexus-em75s.7, ship-blocker): the retake SELECT and
     * {@code liveClaimRow} both had no {@code LEASE_UNTIL} bound, so a claimant whose
     * OWN lease had already lapsed (nobody has re-claimed the row yet) read back the
     * stale, dead claim_id from {@code in_sameClaimantTwiceWithinLease}'s retake path
     * instead of falling through to the claim loop and taking a fresh one.
     */
    @Test
    void in_sameClaimant_afterOwnLeaseLapsed_takesNewClaim_notTheStaleRetake() throws Exception {
        String to = "agent-retake-lapsed-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-retake-lapsed"), "body", "nonce-retake-lapsed-1", null);

        var first = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "retaker-lapsed", 1);
        assertThat(first).isPresent();

        Thread.sleep(1_500); // let the 1-second lease lapse

        var second = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "retaker-lapsed", 60);
        assertThat(second)
                .as("a lapsed OWN lease must be reclaimed as a NEW claim, not the stale retake")
                .isPresent();
        assertThat(second.get().claimId()).isNotEqualTo(first.get().claimId());
        assertThat(second.get().tuple().attempts()).isEqualTo(1);

        // The old claim_id is dead: ack against it must raise ClaimNotFound, not
        // succeed against a claim that is no longer actually held.
        assertThatThrownBy(() -> repo.ack(TENANT_A, first.get().claimId(), "retaker-lapsed"))
                .isInstanceOf(ClaimNotFoundException.class);

        // Exactly one `expire` log row -- the lapsed claim's own release, written once
        // by the claim loop's lapsed-lease branch, not by the (bypassed) retake path.
        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            int expireRows = dsl.fetchCount(dsl.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG)
                    .where(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT_A)
                            .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE.eq("mailbox/" + to))
                            .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TRANSITION.eq("expire"))));
            assertThat(expireRows).isEqualTo(1);
        }
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

    /**
     * RDR-205 Phase 1 review (nexus-em75s.7): as originally written this test passed
     * identically with {@code TupleWaitRegistry.signalAll} deleted outright, because
     * the registry's own 1-second timer fallback alone was enough to find the row
     * within the test's 5-second {@code Future#get} bound. Asserting the ELAPSED time
     * from {@code out} to the result -- well under the 1-second timer -- distinguishes
     * "woke on the signal" from "woke on the next timer tick", which is what actually
     * pins {@link dev.nexus.service.db.TupleWaitRegistry#signalAll}.
     */
    @Test
    void rd_withTimeoutS_wakesOnAnotherClientOut() throws Exception {
        String session = "session-wake-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.TupleRow>> parked = pool.submit(() ->
                    repo.rd(TENANT_A, "ledger/" + session, null, 10, null, 8));

            Thread.sleep(300); // let the reader register + park
            long beforeOut = System.nanoTime();
            repo.out(TENANT_A, "ledger/" + session,
                    Map.of("agent_id", "waker", "kind", "start"), Map.of(), null, null, null);

            List<TupleRepository.TupleRow> result = parked.get(5, TimeUnit.SECONDS);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - beforeOut);
            assertThat(result).hasSize(1);
            assertThat(elapsedMs)
                    .as("woke on the signal, not the registry's 1-second timer fallback")
                    .isLessThan(700);
        } finally {
            pool.shutdownNow();
        }
    }

    /**
     * RDR-205 Phase 1 review (nexus-em75s.7): as originally written this test passed
     * identically with {@code TupleWaitRegistry.signalAll} WIDENED to signal every
     * group regardless of subspace, because an early-but-spurious wake on B still
     * re-queries, still finds nothing (nothing was ever written to B), and still rides
     * out the same 3-second timeout to the same empty result. {@link
     * TupleRepository#setTestOnlySignalHook} counts SIGNAL-DRIVEN wakes for subspace
     * B specifically, so a write to subspace A that (incorrectly) signals B's group is
     * now directly observable and asserted to never happen.
     */
    @Test
    void rd_parkedCallersOnSubspaceB_doNotWakeOnWriteToSubspaceA() throws Exception {
        String sessionA = "session-wakeA-" + UUID.randomUUID();
        String sessionB = "session-wakeB-" + UUID.randomUUID();
        String subspaceB = "ledger/" + sessionB;
        java.util.concurrent.atomic.AtomicInteger subspaceBSignals = new java.util.concurrent.atomic.AtomicInteger();
        TupleRepository.setTestOnlySignalHook((tenant, subspace) -> {
            if (subspaceB.equals(subspace)) {
                subspaceBSignals.incrementAndGet();
            }
        });
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.TupleRow>> parkedOnB = pool.submit(() ->
                    repo.rd(TENANT_A, subspaceB, null, 10, null, 3));

            Thread.sleep(300);
            repo.out(TENANT_A, "ledger/" + sessionA,
                    Map.of("agent_id", "writer-a", "kind", "start"), Map.of(), null, null, null);

            // B's reader must NOT wake early on A's write -- it rides out its own
            // timeout and returns empty (no row ever landed on subspace B).
            List<TupleRepository.TupleRow> result = parkedOnB.get(6, TimeUnit.SECONDS);
            assertThat(result).isEmpty();
            assertThat(subspaceBSignals.get())
                    .as("A's write must never signal B's wait group")
                    .isZero();
        } finally {
            pool.shutdownNow();
            TupleRepository.setTestOnlySignalHook(null);
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

    // ── sweep batch arms (RDR-205 Phase 1 Step 5, bead nexus-em75s.5) ───────────

    /**
     * Raw-SQL seeding bypasses RLS (superuser) and the repo's own clock — the only
     * way to construct an "already lapsed"/"already expired" row directly, since
     * every {@link TupleRepository} write uses the current instant.
     */
    private byte[] seedExpiredTuple(String tenant, String label, OffsetDateTime createdAt) throws Exception {
        byte[] id = java.security.MessageDigest.getInstance("SHA-256")
                .digest(label.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        try (Connection su = pg.createConnection("")) {
            org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .insertInto(dev.nexus.service.jooq.nexus.Tables.TUPLES,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.ID,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.TENANT_ID,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.SUBSPACE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.TEMPLATE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.KEYS,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.ATTEMPTS,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.EXPIRES_AT,
                            dev.nexus.service.jooq.nexus.Tables.TUPLES.CREATED_AT)
                    .values(id, tenant, "mailbox/batch-probe", "mailbox", org.jooq.JSONB.valueOf("{}"),
                            0, createdAt.minusMinutes(1), createdAt)
                    .execute();
        }
        return id;
    }

    @Test
    void purgeExpiredTuplesBatch_respectsBatchSize_multipleCallsDrainTheRest() throws Exception {
        String tenant = "tuple-tenant-purge-batch-" + UUID.randomUUID();
        OffsetDateTime base = OffsetDateTime.now(java.time.ZoneOffset.UTC).minusHours(1);
        for (int i = 0; i < 5; i++) {
            seedExpiredTuple(tenant, tenant + "-purge-row-" + i, base.plusSeconds(i));
        }

        var first = repo.purgeExpiredTuplesBatch(tenant, 2, null);
        assertThat(first.purged()).isEqualTo(2);
        assertThat(first.examined()).isEqualTo(2);
        var second = repo.purgeExpiredTuplesBatch(tenant, 2, null);
        assertThat(second.purged()).isEqualTo(2);
        assertThat(second.examined()).isEqualTo(2);
        var third = repo.purgeExpiredTuplesBatch(tenant, 2, null);
        assertThat(third.purged()).isEqualTo(1); // drained: fewer than batchSize remained
        assertThat(third.examined()).isEqualTo(1);
        var fourth = repo.purgeExpiredTuplesBatch(tenant, 2, null);
        assertThat(fourth.purged()).isZero(); // nothing left
        assertThat(fourth.examined()).isZero();
    }

    @Test
    void releaseLapsedClaimsBatch_scannedEqualsReleasedPlusDeadLettered() throws Exception {
        String tenant = "tuple-tenant-release-batch-" + UUID.randomUUID();
        String to = "agent-release-batch-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(java.time.ZoneOffset.UTC);
        byte[] id = repo.out(tenant, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender-x"),
                null, "nonce-release-batch", null);

        // Force the row into an already-lapsed claimed state directly, bypassing the
        // repo's own clock (its API always writes the current instant).
        try (Connection su = pg.createConnection("")) {
            org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .update(dev.nexus.service.jooq.nexus.Tables.TUPLES)
                    .set(dev.nexus.service.jooq.nexus.Tables.TUPLES.CLAIM_STATE, "claimed")
                    .set(dev.nexus.service.jooq.nexus.Tables.TUPLES.CLAIMANT, "worker-release-batch")
                    .set(dev.nexus.service.jooq.nexus.Tables.TUPLES.CLAIM_ID, "claim-release-batch")
                    .set(dev.nexus.service.jooq.nexus.Tables.TUPLES.LEASE_UNTIL, now.minusMinutes(5))
                    .where(dev.nexus.service.jooq.nexus.Tables.TUPLES.ID.eq(id))
                    .execute();
        }

        var result = repo.releaseLapsedClaimsBatch(tenant, 300, null);
        assertThat(result.scanned()).isEqualTo(result.released() + result.deadLettered());
        assertThat(result.scanned()).isEqualTo(1);
        assertThat(result.released()).isEqualTo(1);
        assertThat(result.deadLettered()).isZero();
    }

    @Test
    void purgeOldClaimLogBatch_purgesRowsPastTtl_leavesRecentRows() throws Exception {
        String tenant = "tuple-tenant-log-batch-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(java.time.ZoneOffset.UTC);
        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            dsl.insertInto(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TEMPLATE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TRANSITION,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.AT,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.EXPIRES_AT)
                    .values(tenant, "mailbox/log-batch-old", "mailbox", "claim",
                            now.minusDays(200), now.minusDays(199))
                    .execute();
            dsl.insertInto(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TEMPLATE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TRANSITION,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.AT,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.EXPIRES_AT)
                    .values(tenant, "mailbox/log-batch-recent", "mailbox", "claim",
                            now.minusDays(1), now.plusDays(1))
                    .execute();
        }

        var purgeResult = repo.purgeOldClaimLogBatch(tenant, 300, null);
        assertThat(purgeResult.purged()).isEqualTo(1);
        assertThat(purgeResult.examined()).isEqualTo(1);

        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            boolean oldGone = !dsl.fetchExists(dsl.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG)
                    .where(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant)
                            .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE.eq(
                                    "mailbox/log-batch-old"))));
            boolean recentSurvives = dsl.fetchExists(dsl.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG)
                    .where(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant)
                            .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE.eq(
                                    "mailbox/log-batch-recent"))));
            assertThat(oldGone).isTrue();
            assertThat(recentSurvives).isTrue();
        }
    }

    /**
     * RDR-205 P1 follow-on (nexus-em75s.37, review M6): the purge arm must filter on
     * the log row's OWN {@code expires_at} column, not recompute a cutoff from {@code
     * at}. This row is built to disagree between the two: {@code at} is recent (a
     * naive {@code at}-based cutoff of "now minus the TTL" would keep it), but {@code
     * expires_at} has already passed -- exactly what a row written by the pre-fix
     * {@code insertClaimLog} (the tuple's own, much shorter, expiry) would look like.
     * A sibling row with a matching recent {@code at} but a still-future {@code
     * expires_at} must survive, proving this isn't simply "purge everything."
     */
    @Test
    void purgeOldClaimLogBatch_readsExpiresAtColumn_notRecomputedFromAt() throws Exception {
        String tenant = "tuple-tenant-log-batch-expires-" + UUID.randomUUID();
        OffsetDateTime now = OffsetDateTime.now(java.time.ZoneOffset.UTC);
        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            // Recent AT (an at-based cutoff of "now - TTL" would never touch this row),
            // but EXPIRES_AT already in the past -- must be purged under the new rule.
            dsl.insertInto(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TEMPLATE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TRANSITION,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.AT,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.EXPIRES_AT)
                    .values(tenant, "mailbox/log-batch-expired-despite-recent-at", "mailbox", "claim",
                            now.minusHours(1), now.minusMinutes(1))
                    .execute();
            // Same recent AT, but EXPIRES_AT still in the future -- must survive.
            dsl.insertInto(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TEMPLATE,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TRANSITION,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.AT,
                            dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.EXPIRES_AT)
                    .values(tenant, "mailbox/log-batch-not-yet-expired", "mailbox", "claim",
                            now.minusHours(1), now.plusDays(180))
                    .execute();
        }

        int purged = repo.purgeOldClaimLogBatch(tenant, 300, null);
        assertThat(purged).isEqualTo(1);

        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            boolean expiredGone = !dsl.fetchExists(dsl.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG)
                    .where(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant)
                            .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE.eq(
                                    "mailbox/log-batch-expired-despite-recent-at"))));
            boolean notYetExpiredSurvives = dsl.fetchExists(
                    dsl.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG)
                            .where(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID.eq(tenant)
                                    .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE.eq(
                                            "mailbox/log-batch-not-yet-expired"))));
            assertThat(expiredGone)
                    .as("a row past its OWN expires_at must be purged even with a recent `at`")
                    .isTrue();
            assertThat(notYetExpiredSurvives)
                    .as("a row not yet past its OWN expires_at must survive")
                    .isTrue();
        }
    }

    /**
     * RDR-205 P1 follow-on (nexus-em75s.37, review M6 / critique S2, RDR §Technical
     * Design line ~602): {@code tuple_claim_log.expires_at} is the LOG's own TTL
     * ({@code at + claimLogTtlSeconds()}), not the tuple's expiry. The fixture's
     * mailbox template retains tuples for 7 days ({@code retention_seconds:
     * 604800}); the registry's default claim-log TTL is 180 days -- two very
     * different numbers, so a log row landing near either one is an unambiguous
     * signal of which expiry actually got written.
     */
    @Test
    void ack_writesClaimLogExpiresAt_asAtPlusClaimLogTtl_notTheTuplesOwnExpiry() throws Exception {
        String to = "agent-log-expiry-" + UUID.randomUUID();
        OffsetDateTime beforeOut = OffsetDateTime.now(java.time.ZoneOffset.UTC);
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to),
                Map.of("from", "sender-log-expiry"), "body", "nonce-log-expiry-1", null);

        var claimed = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "claimant-log-expiry", 300);
        assertThat(claimed).isPresent();
        OffsetDateTime tuplesOwnExpiry = claimed.get().tuple().expiresAt();

        repo.ack(TENANT_A, claimed.get().claimId(), "claimant-log-expiry");

        OffsetDateTime logAt;
        OffsetDateTime logExpiresAt;
        try (Connection su = pg.createConnection("")) {
            var dsl = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES);
            var row = dsl.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG)
                    .where(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT_A)
                            .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.SUBSPACE.eq("mailbox/" + to))
                            .and(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.TRANSITION.eq("ack")))
                    .fetchOne();
            assertThat(row).isNotNull();
            logAt = row.get(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.AT);
            logExpiresAt = row.get(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.EXPIRES_AT);
        }

        assertThat(logAt).isAfterOrEqualTo(beforeOut);

        OffsetDateTime expectedLogExpiresAt = logAt.plusSeconds(registry.claimLogTtlSeconds());
        assertThat(java.time.Duration.between(expectedLogExpiresAt, logExpiresAt).abs())
                .as("expires_at must equal at + claimLogTtlSeconds(), the log's own TTL")
                .isLessThan(java.time.Duration.ofSeconds(5));

        assertThat(java.time.Duration.between(logExpiresAt, tuplesOwnExpiry).abs())
                .as("the log's expires_at must NOT be the tuple's own (7-day mailbox retention) expiry")
                .isGreaterThan(java.time.Duration.ofDays(1));
    }
}
