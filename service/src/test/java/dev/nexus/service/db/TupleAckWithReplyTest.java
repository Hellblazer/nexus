// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatExceptionOfType;

/**
 * RDR-206 Phase 1 Step 2 (bead nexus-h61dl.3) — {@code ackWithReply}: consume the
 * request and write the reply in ONE transaction, so a reader sees both or neither.
 *
 * <p>The load-bearing property under test is not "a reply gets written". It is that
 * every way a reply can be REFUSED leaves the request STILL CLAIMED and no reply row
 * behind. Four of the scenarios below are that shape. They are what forces the
 * validation to happen before the transaction opens rather than inside it: a rollback
 * would restore the claim too, so an end-state assertion alone cannot distinguish
 * "never started" from "started and undone", and only the former keeps the guarantee
 * when someone later moves the signal or splits the transaction.
 *
 * <p>Templates used, both loaded at boot from {@code service/src/main/resources/tuples/
 * templates/}: {@code mailbox/<address>} is {@code id_from: keys+nonce} with
 * {@code take.enabled}, so it can be both request and reply target;
 * {@code ledger/<session_id>} is {@code id_from: keys} with take disabled, which makes
 * it the natural probe for the keys-only reply-target refusal.
 *
 * <p>No call-order test. RDR-206 was amended (1c8f109da) to drop the claim that
 * {@code consumeClaim} must run before {@code writeOut}: they share one transaction, so
 * the order is immaterial and only atomicity is pinned. A test asserting only the end
 * state and calling itself an order pin is what that amendment exists to prevent.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleAckWithReplyTest {

    private static final String TENANT = "tuple-tenant-reply";
    private static final String SVC_ROLE = "svc_tuple_reply_test";
    private static final String SVC_PASS = "svc_tuple_reply_test_pass";

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

    @AfterAll
    void stopAll() {
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    private String addr(String label) {
        return label + "-" + UUID.randomUUID().toString().substring(0, 10);
    }

    /** Writes a request into {@code mailbox/<to>} and claims it, returning the claim id. */
    private String requestAndClaim(String to, String claimant) {
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "asker"),
                "the request", "nonce-" + UUID.randomUUID(), null);
        Optional<TupleRepository.ClaimedTuple> claimed =
                repo.inp(TENANT, "mailbox/" + to, Map.of("to", to), claimant, 60);
        assertThat(claimed).as("the request must be claimable").isPresent();
        return claimed.get().claimId();
    }

    private TupleRepository.ReplySpec reply(String to, String body, Long ttlSeconds) {
        return new TupleRepository.ReplySpec(
                "mailbox/" + to, Map.of("to", to), Map.of("from", "answerer"), body, ttlSeconds);
    }

    private int rowCount(String subspace) {
        return repo.rd(TENANT, subspace, Map.of(), 50, null, 0).size();
    }

    // ── the happy path ───────────────────────────────────────────────────────

    /**
     * The "request consumed AND reply present in one read" half of the RDR test plan.
     * The "or NEITHER" half is not here -- it is the refusal set below, each of which
     * asserts the request is still claimed and no reply row exists. Naming this one
     * "atomically" would overclaim: it only shows both halves landed.
     */
    @Test
    void ackWithReply_consumesTheRequestAndWritesTheReply_bothVisibleInOneRead() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");

        byte[] replyId = repo.ackWithReply(TENANT, claimId, "worker-1",
                reply(asker, "the answer", null));

        assertThat(replyId).as("the reply id is returned").isNotNull();
        // Both halves visible in one read: the request gone, the reply present.
        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).consumed())
                .as("the request is consumed").isEqualTo(1);
        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).available())
                .as("nothing left available at the request address").isEqualTo(0);
        var replies = repo.rd(TENANT, "mailbox/" + asker, Map.of("to", asker), 10, null, 0);
        assertThat(replies).as("exactly one reply row").hasSize(1);
        assertThat(replies.get(0).body()).isEqualTo("the answer");
    }

    @Test
    void theReplyNonceIsTheConsumedRequestId_setByTheEngine() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");
        byte[] requestId = repo.rd(TENANT, "mailbox/" + answerer, Map.of("to", answerer), 10, null, 0)
                .get(0).id();

        repo.ackWithReply(TENANT, claimId, "worker-1", reply(asker, "correlated", null));

        // Proved through the PUBLIC api rather than a test-only accessor on computeId:
        // an independent `out` to the same reply subspace, with the nonce set to
        // hex(request id), must land on the SAME row rather than minting a second one.
        // If the engine had used any other nonce, this would be a different id and the
        // subspace would hold two rows.
        assertThat(rowCount("mailbox/" + asker)).isEqualTo(1);
        repo.out(TENANT, "mailbox/" + asker, Map.of("to", asker), Map.of("from", "answerer"),
                "correlated", HexFormat.of().formatHex(requestId), null);
        assertThat(rowCount("mailbox/" + asker))
                .as("the reply's nonce is hex(request id): a refire on that nonce is the SAME row")
                .isEqualTo(1);
    }

    /**
     * The reader half is best-effort by construction: if the box is loaded enough that
     * the reader has not parked within the setup window, it simply reads the committed
     * row directly and still returns it. That is why the SIGNAL is pinned separately
     * through {@link TupleRepository#setTestOnlySignalHook}, which fires on the
     * arguments {@code ackWithReply} hands {@code signalAll} and so is independent of
     * load: without it, an {@code ackWithReply} that forgot to signal at all would pass
     * this test on any machine busy enough to lose the parking race.
     *
     * <p>The hook also pins WHICH address is signalled. Signalling the request's
     * subspace instead of the reply's is the plausible slip here, and it would strand
     * every parked requester while looking correct in an end-state read.
     */
    @Test
    void aReaderParkedOnTheReplySubspaceWakesAfterTheCommit() throws Exception {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");
        var replySignals = new java.util.concurrent.atomic.AtomicInteger();
        var requestSignals = new java.util.concurrent.atomic.AtomicInteger();
        TupleRepository.setTestOnlySignalHook((tenant, subspace) -> {
            if (("mailbox/" + asker).equals(subspace)) {
                replySignals.incrementAndGet();
            } else if (("mailbox/" + answerer).equals(subspace)) {
                requestSignals.incrementAndGet();
            }
        });

        var pool = java.util.concurrent.Executors.newSingleThreadExecutor();
        try {
            var parked = pool.submit(() ->
                    repo.rd(TENANT, "mailbox/" + asker, Map.of("to", asker), 1, null, 20));
            Thread.sleep(300);  // give the reader a chance to park before the write
            repo.ackWithReply(TENANT, claimId, "worker-1", reply(asker, "woke you", null));
            // A hang bound, not a load bound: the reader's own rd budget is 20s, so
            // anything past 25s is a wedge, not a slow box.
            var rows = parked.get(25, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(rows).as("the reply is visible to the reader").hasSize(1);
            assertThat(rows.get(0).body()).isEqualTo("woke you");

            assertThat(replySignals.get())
                    .as("the commit signalled the REPLY's subspace exactly once")
                    .isEqualTo(1);
            assertThat(requestSignals.get())
                    .as("consuming the request adds no row there, so it is never signalled")
                    .isZero();
        } finally {
            pool.shutdownNow();
            TupleRepository.setTestOnlySignalHook(null);
        }
    }

    @Test
    void ackWithNoReplySignalsNothing() {
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");
        var signals = new java.util.concurrent.atomic.AtomicInteger();
        TupleRepository.setTestOnlySignalHook((tenant, subspace) -> signals.incrementAndGet());
        try {
            repo.ackWithReply(TENANT, claimId, "worker-1", null);
            assertThat(signals.get())
                    .as("a reply-less ack writes no row, so it must wake nobody")
                    .isZero();
        } finally {
            TupleRepository.setTestOnlySignalHook(null);
        }
    }

    // ── every refusal leaves the request claimed and writes no reply ─────────

    @Test
    void anInvalidReplyRefusesAndLeavesTheRequestClaimed() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");

        // 'nope' is not a declared key on the mailbox template.
        var bad = new TupleRepository.ReplySpec(
                "mailbox/" + asker, Map.of("to", asker, "nope", "x"),
                Map.of("from", "answerer"), "body", null);

        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-1", bad));

        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).claimed())
                .as("the request is STILL CLAIMED -- the ack must not have consumed it")
                .isEqualTo(1);
        assertThat(rowCount("mailbox/" + asker)).as("no reply row").isEqualTo(0);
    }

    @Test
    void aReplyTtlOverTheTemplateRetentionRefusesAndLeavesTheRequestClaimed() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");

        assertThatExceptionOfType(TtlTooLongException.class)
                .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-1",
                        reply(asker, "body", 99_999_999L)));

        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).claimed()).isEqualTo(1);
        assertThat(rowCount("mailbox/" + asker)).isEqualTo(0);
    }

    @Test
    void aReplyToAnUnregisteredSubspaceRefusesAndLeavesTheRequestClaimed() {
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");

        var bad = new TupleRepository.ReplySpec(
                "no-such-template/whatever", Map.of("to", "x"), Map.of(), "body", null);

        assertThatExceptionOfType(UnknownSubspaceException.class)
                .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-1", bad));

        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).claimed()).isEqualTo(1);
    }

    @Test
    void aReplyToAKeysOnlyTemplateRefusesAndLeavesTheRequestClaimed() {
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");
        String session = addr("session");

        // ledger/<session_id> is id_from: keys. computeId would IGNORE the nonce, so two
        // replies to the same ledger keys would collide on one id and out's refire clamp
        // would silently discard the second one's body -- the exact failure class RDR-206
        // exists to close, which is why this is refused rather than merely documented.
        var keysOnly = new TupleRepository.ReplySpec(
                "ledger/" + session, Map.of("agent_id", "a1", "kind", "report"),
                Map.of("agent_type", "developer"), "body", null);

        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-1", keysOnly));

        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).claimed()).isEqualTo(1);
        assertThat(rowCount("ledger/" + session)).as("no reply row").isEqualTo(0);
    }

    // ── retry after a lost response ─────────────────────────────────────────

    @Test
    void aRetriedAckWithReplyFailsClaimNotFoundAndLeavesExactlyOneReply() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");

        repo.ackWithReply(TENANT, claimId, "worker-1", reply(asker, "once", null));

        // The caller never saw the response and retries the same claim id. The
        // compare-and-swap from nexus-h61dl.2 is what makes this ClaimNotFound rather
        // than a second consume, and the reply must not be duplicated either.
        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-1",
                        reply(asker, "once", null)));

        assertThat(rowCount("mailbox/" + asker)).as("exactly one reply row").isEqualTo(1);
    }

    // ── ack with no reply still behaves exactly as ack ───────────────────────

    @Test
    void ackWithANullReplyConsumesTheRequestAndWritesNothingElse() {
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");

        assertThat(repo.ackWithReply(TENANT, claimId, "worker-1", null))
                .as("no reply written, so no reply id").isNull();
        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).consumed()).isEqualTo(1);
        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).claimed()).isEqualTo(0);
    }

    @Test
    void aStaleClaimIsRefusedBeforeAnyReplyIsWritten() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");
        repo.ack(TENANT, claimId, "worker-1");   // consumed by a plain ack first

        assertThatExceptionOfType(ClaimNotFoundException.class)
                .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-1",
                        reply(asker, "should not exist", null)));

        assertThat(rowCount("mailbox/" + asker))
                .as("a refused ack must not leave a reply behind").isEqualTo(0);
    }

    @Test
    void anotherClaimantsAckWithReplyIsRefusedAndWritesNoReply() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");

        assertThatExceptionOfType(ClaimOwnershipException.class)
                .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-2",
                        reply(asker, "not yours", null)));

        assertThat(repo.subspaceStats(TENANT, "mailbox/" + answerer).claimed()).isEqualTo(1);
        assertThat(rowCount("mailbox/" + asker)).isEqualTo(0);
    }

    // ── the refusal path never opens the transaction ─────────────────────────

    /**
     * The property `ackWithReply`'s javadoc claims: a reply refusal is decided before any
     * transaction opens, so the request is still claimed because the ack never STARTED,
     * not because a rollback undid it.
     *
     * <p>SCOPE, because this is easy to overread (nexus-h61dl.3 review). End-state
     * assertions cannot tell the two designs apart: `TenantScope` rolls back on any
     * RuntimeException, so every refusal test in this file would pass identically under a
     * naive "do it all in one transaction and rely on rollback" implementation. What this
     * test adds is one observation an end state cannot give -- `consumeClaim` never ran.
     * The seam fires between `liveClaimRow`'s read and the compare-and-swap update, so if
     * a validation that currently sits in `prepareOut` were moved into the transaction
     * lambda (which runs `consumeClaim` first), the counter would be 1 and this fails.
     *
     * <p>It does NOT prove the transaction never opened, and a hypothetical rewrite that
     * validated the reply inside the lambda BEFORE consuming would still pass. That
     * rewrite is not the regression worth guarding; migrating a check out of `prepareOut`
     * into the existing lambda is, and that is what this catches.
     */
    @Test
    void aRefusedReplyNeverOpensTheTransaction() {
        String asker = addr("asker");
        String answerer = addr("answerer");
        String claimId = requestAndClaim(answerer, "worker-1");
        var claimReads = new java.util.concurrent.atomic.AtomicInteger();
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = claimReads::incrementAndGet;
        try {
            var bad = new TupleRepository.ReplySpec(
                    "mailbox/" + asker, Map.of("to", asker, "nope", "x"),
                    Map.of("from", "answerer"), "body", null);
            assertThatExceptionOfType(SchemaViolationException.class)
                    .isThrownBy(() -> repo.ackWithReply(TENANT, claimId, "worker-1", bad));
            assertThat(claimReads.get())
                    .as("a refused reply must not have reached consumeClaim, so the request "
                        + "is still claimed because the ack never started")
                    .isZero();

            // The control: a VALID reply on the same claim does reach it, so the counter
            // above is measuring something rather than never incrementing at all.
            repo.ackWithReply(TENANT, claimId, "worker-1", reply(asker, "fine", null));
            assertThat(claimReads.get()).as("the seam does fire on the accepted path").isEqualTo(1);
        } finally {
            TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
        }
    }

    // ── out()'s validation order, unchanged by the Step 2 extraction ─────────

    /**
     * `out` raises on a MISSING NONCE before it raises on a bad ttl. Both reviewers of
     * nexus-h61dl.3 found that the extraction had silently swapped these: the combined
     * `validateOut` checked shape and nonce together, ahead of the ttl checks, and the
     * split moved the nonce check after them, flipping which exception a caller sees when
     * a call violates both at once.
     *
     * <p>The 50-case `TupleRepositoryTest` pin did not catch it because no case combines
     * the two violations: its ttl case uses the nonce-free ledger template and its
     * missing-nonce case passes ttl=null. This is that missing case. Swapping the two
     * checks back in `prepareOut` fails it.
     */
    @Test
    void outRaisesOnAMissingNonceBeforeAnOverLongTtl() {
        String to = addr("order");
        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.out(TENANT, "mailbox/" + to, Map.of("to", to),
                        Map.of("from", "asker"), "body", null, 99_999_999L))
                .satisfies(e -> assertThat(e.field())
                        .as("the NONCE is the rejected field, not the ttl")
                        .isEqualTo("nonce"));
    }
}
