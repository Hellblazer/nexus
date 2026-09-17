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

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatExceptionOfType;

/**
 * RDR-211 Phase 1 Step 1 (bead nexus-rplay.3): the template-level {@code lock} flag.
 * Read at four sites (Technical Design "The lock flag"): {@code writeOut} (an {@code
 * out} that meets an EXPIRED lock row resets it to available), {@code claimOnce} and
 * {@code renew} (both move the tuple's own {@code expires_at} forward to now plus
 * retention, ahead of the lease clamp -- "a lock lives as long as it is used"), and
 * {@code consumeClaim} (the body {@code ack} and {@code ackWithReply} share -- refuses
 * with {@link SchemaViolationException} naming {@code release}, because a consumed
 * lock row would be unobtainable until the sweep purges it).
 *
 * <p>Promoted from the throwaway {@code TupleLockFlagSpikeTest} (T2
 * {@code nexus_rdr/211-spike-2-2026-09-16}), which proved the Critical Assumption for
 * the first three of those four sites; the {@code ack} refusal and the remaining RDR
 * Test Plan scenarios (nack returns a lock, a lapsed lock is reclaimed with no
 * dead-letter) are added here.
 *
 * <p>Loads a test-only {@code lock-flag/<resource>} template carrying {@code
 * lock: true} through {@link TemplateRegistry#loadAtBoot}'s existing {@code
 * NX_TUPLE_TEMPLATE_DIR} test source, alongside the shipped resources (mailbox,
 * ledger, directory) -- so the non-lock regression check below runs against the SAME
 * registry a lock template lives in, not a separate one.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleLockFlagTest {

    private static final String SVC_ROLE = "svc_tuple_lock_flag_test";
    private static final String SVC_PASS = "svc_tuple_lock_flag_test_pass";

    /** No {@code max_attempts}: a lock template never dead-letters (Approach item 3). */
    private static final String LOCK_TEMPLATE = """
            name: lock-flag/<resource>
            keys:
              - resource
            dimensions:
              from:
                type: string
            id_from: keys
            take:
              enabled: true
              max_lease_seconds: 900
            retention_seconds: 3
            lock: true
            """;

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

        Path dir = Files.createTempDirectory("nx-tuple-lock-flag");
        Files.writeString(dir.resolve("lock-flag.yaml"), LOCK_TEMPLATE, StandardCharsets.UTF_8);
        registry = TemplateRegistry.loadAtBoot(dir.toString(), null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
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

    private org.jooq.Record3<OffsetDateTime, String, OffsetDateTime> rawRow(byte[] id) {
        try (Connection su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLES.EXPIRES_AT, TUPLES.CLAIM_STATE, TUPLES.CREATED_AT)
                    .from(TUPLES)
                    .where(TUPLES.ID.eq(id))
                    .fetchOne();
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

    // ── effect (a): claim / renew move expiry forward ───────────────────────

    /**
     * retention_seconds is 3 on this template. Claim with a 3s LEASE (so each renew
     * below always lands while the lease is still live -- renew's own no-resurrection
     * rule must not confound this assumption with the lease boundary, only the tuple's
     * own {@code expires_at} ceiling matters here), then renew twice with sleeps short
     * enough to stay inside each lease but long enough, summed, to pass what would have
     * been the ORIGINAL 3s retention ceiling with no lock flag. Confirms the claim
     * survives and stays renewable instead of hitting the "lock expiry cliff" the RDR
     * names (Scale and Limits item 5). RDR Test Plan: "a lock holder's lease lapses"
     * covered separately below; this is the renew-keeps-it-held half.
     */
    @Test
    void aLockClaimedAndRenewedAcrossItsRetentionBoundaryStaysHeldAndRenewable() throws Exception {
        String tenant = "tuple-lock-flag-boundary-" + UUID.randomUUID();
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock-flag/" + resource;
        byte[] id = repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"),
                null, null, null);

        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 3L);
        assertThat(claimed).as("must be claimable").isPresent();
        OffsetDateTime expiresAtAtClaim = rawRow(id).value1(); // now + 3s retention

        Thread.sleep(1500); // well inside the 3s lease

        OffsetDateTime firstRenew = repo.renew(tenant, claimed.get().claimId(), "holder-1", 3L);
        assertThat(firstRenew).as("renew succeeds while the lease is still live").isNotNull();
        OffsetDateTime expiresAtAfterFirstRenew = rawRow(id).value1();
        assertThat(expiresAtAfterFirstRenew)
                .as("renew must have pushed the tuple's OWN expiry forward, past the ORIGINAL ceiling "
                        + "(claim time + 3s) even though only 1.5s has elapsed")
                .isAfter(expiresAtAtClaim);

        Thread.sleep(1500); // total elapsed now ~3s -- past the ORIGINAL retention ceiling, still inside the
                             // new (renewed) lease/expiry window

        OffsetDateTime secondRenew = repo.renew(tenant, claimed.get().claimId(), "holder-1", 3L);
        assertThat(secondRenew)
                .as("still renewable past the original retention window -- the cliff is closed for a lock")
                .isNotNull();
        assertThat(rawRow(id).value2()).as("still claimed, never dead-lettered or swept").isEqualTo("claimed");
    }

    /**
     * The boundary test above cannot, by itself, isolate {@code claimOnce}'s OWN
     * expiry-forward effect from {@code renew}'s: the first renew there independently
     * pushes {@code expires_at} forward regardless of what claim did, so a claim that
     * silently kept the stale, out()-time expiry would still pass it (confirmed by
     * mutation: disabling {@code claimOnce}'s lock branch alone left that test green).
     *
     * <p>This isolates the claim-time effect directly: {@code out} sets {@code
     * expires_at} to creation time plus the 3s retention; sleeping most of that away
     * before claiming, with a LEASE far longer than what remains, proves whether the
     * lease was clamped against the STALE out-time expiry (no lock effect: lease lands
     * within about a second of claim time) or against a freshly recomputed claim-time
     * + retention ceiling (lock effect: lease lands close to 3s out). No renew is
     * involved anywhere in this test.
     */
    @Test
    void aLockClaimMovesTheTuplesOwnExpiryForwardAtClaimTimeIndependentlyOfRenew() throws Exception {
        String tenant = "tuple-lock-flag-claimpush-" + UUID.randomUUID();
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock-flag/" + resource;
        repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"), null, null, null);

        Thread.sleep(1500); // out()'s 3s expiry now has roughly 1.5s left

        OffsetDateTime beforeClaim = OffsetDateTime.now();
        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 10L);
        assertThat(claimed).as("must still be claimable -- not yet past its own expiry").isPresent();

        assertThat(claimed.get().tuple().leaseUntil())
                .as("claimOnce itself must push expires_at to (claim time + 3s retention) BEFORE "
                        + "clamping the lease against it -- a stale out()-time ceiling would land the "
                        + "lease under 2s from claim time, not close to the full 3s")
                .isAfter(beforeClaim.plusSeconds(2));
    }

    // ── effect (b): out on an expired lock row resets it to available ───────

    /** RDR Test Plan: "two processes run out on the same lock at once: one lock tuple exists." */
    @Test
    void outOnAnExpiredLockRowMakesItAvailable() throws Exception {
        String tenant = "tuple-lock-flag-reset-" + UUID.randomUUID();
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock-flag/" + resource;
        byte[] id = repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"),
                null, null, null);

        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 1L);
        assertThat(claimed).as("must be claimable").isPresent();

        // Never renewed: past both the 1s lease AND the 3s retention -- expired, not
        // merely lapsed. claimOnce's own candidate query already excludes an expired
        // row (EXPIRES_AT > now()), so nothing can claim it in this state.
        Thread.sleep(3300);
        assertThat(rawRow(id).value1()).as("row must now be past its own expiry").isBefore(OffsetDateTime.now());

        repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"), null, null, null);

        var afterReset = rawRow(id);
        assertThat(afterReset.value2()).as("claim state cleared -- available again").isNull();
        assertThat(afterReset.value1()).as("expiry refreshed into the future").isAfter(OffsetDateTime.now());

        var reclaimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-2", 60L);
        assertThat(reclaimed).as("a fresh claimant can take the reset lock immediately").isPresent();
    }

    // ── the ack refusal ──────────────────────────────────────────────────────

    /**
     * RDR Test Plan: "a lock holder calls ack: SchemaViolationException, the claim is
     * still live, the holder can still release." Naming release in the message is the
     * RDR's own requirement (Technical Design "The lock flag").
     */
    @Test
    void ackOnALockClaimIsRefusedAndTheClaimStaysLive() throws Exception {
        String tenant = "tuple-lock-flag-ack-" + UUID.randomUUID();
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock-flag/" + resource;
        byte[] id = repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"),
                null, null, null);
        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 60L);
        assertThat(claimed).isPresent();
        String claimId = claimed.get().claimId();

        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.ack(tenant, claimId, "holder-1"))
                .satisfies(e -> assertThat(e.getMessage()).contains("release"));

        assertThat(rawRow(id).value2()).as("the claim stays live -- ack wrote nothing").isEqualTo("claimed");
        assertThat(transitionsFor(tenant, id))
                .as("no ack row -- consumeClaim's own transaction never opened a write")
                .containsExactly("claim");

        // The holder can still renew and then release, exactly as the RDR names.
        assertThat(repo.renew(tenant, claimId, "holder-1", 60)).isNotNull();
        repo.release(tenant, claimId, "holder-1");
        assertThat(rawRow(id).value2()).as("released back to available").isNull();
    }

    /**
     * Same refusal on the reply-carrying path -- consumeClaim is the shared core the
     * body of {@code ack} and {@code ackWithReply} both call. The refusal is on the
     * REQUEST's own (lock-flagged) template, so the reply target must be an ordinary
     * {@code id_from: keys+nonce} template ({@code mailbox/<address>}, already loaded
     * from the shipped resources alongside this test's lock template) -- otherwise
     * {@code ackWithReply}'s own reply-shape check (RDR-206) would refuse first, for an
     * unrelated reason, before the lock check is ever reached.
     */
    @Test
    void ackWithReplyOnALockClaimIsRefusedAndTheClaimStaysLive() throws Exception {
        String tenant = "tuple-lock-flag-ackreply-" + UUID.randomUUID();
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock-flag/" + resource;
        byte[] id = repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"),
                null, null, null);
        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 60L);
        assertThat(claimed).isPresent();
        String claimId = claimed.get().claimId();
        String asker = "agent-lock-flag-ackreply-asker-" + UUID.randomUUID();
        var reply = new TupleRepository.ReplySpec("mailbox/" + asker, Map.of("to", asker),
                Map.of("from", "holder-1"), "unused", null);

        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.ackWithReply(tenant, claimId, "holder-1", reply))
                .satisfies(e -> assertThat(e.getMessage()).contains("release"));

        assertThat(rawRow(id).value2()).as("the claim stays live").isEqualTo("claimed");
        var rdReply = repo.rd(tenant, "mailbox/" + asker, Map.of("to", asker), 1, null, 0);
        assertThat(rdReply).as("the refused ack must never write the reply either").isEmpty();
    }

    // ── nack stays allowed ───────────────────────────────────────────────────

    /** RDR Test Plan / Approach item 3: "nack on a lock stays allowed... counts an attempt". */
    @Test
    void nackOnALockReturnsItAndCountsAnAttempt() throws Exception {
        String tenant = "tuple-lock-flag-nack-" + UUID.randomUUID();
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock-flag/" + resource;
        repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"), null, null, null);
        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 60L);
        assertThat(claimed).isPresent();

        repo.nack(tenant, claimed.get().claimId(), "holder-1");

        var reclaimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-2", 60L);
        assertThat(reclaimed).as("nack returns the lock -- a fresh claimant can take it").isPresent();
    }

    // ── a lapsed lock is reclaimed, never dead-lettered ──────────────────────

    /**
     * RDR Test Plan: "a lock holder's lease lapses: the next in reclaims the lock, and
     * the lock is not dead-lettered under the chosen attempts rule." This template
     * declares no {@code max_attempts}, so {@code nack}/the sweep's release arm treat
     * {@code maxAttempts} as {@link Long#MAX_VALUE} (existing {@code claimOnce}/{@code
     * nack} behaviour, unchanged by the lock flag) -- a lock template is never
     * dead-lettered by construction, not by a special case in the lock branch itself.
     */
    @Test
    void aLapsedLockLeaseIsReclaimedByTheNextInWithNoDeadLetter() throws Exception {
        String tenant = "tuple-lock-flag-lapse-" + UUID.randomUUID();
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock-flag/" + resource;
        byte[] id = repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"),
                null, null, null);
        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 1L);
        assertThat(claimed).isPresent();

        Thread.sleep(1200); // past the 1s lease, but the tuple's own 3s expiry still holds

        var reclaimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-2", 60L);
        assertThat(reclaimed).as("the lapsed lock is reclaimed by the next in").isPresent();
        assertThat(rawRow(id).value2()).as("claimed again, never dead").isEqualTo("claimed");
    }

    // ── non-lock templates: unchanged (regression check) ────────────────────

    /**
     * mailbox/<address> carries no {@code lock} flag. A second {@code out} on the same
     * id must be the SAME idempotent no-op it always was: same row, same claim_state,
     * expires_at only ever refreshed under the pre-existing refire-clamp formula --
     * never the lock-branch's reset behaviour.
     */
    @Test
    void outOnANonFlaggedTemplateWithAnExistingIdIsUnchanged() {
        String tenant = "tuple-lock-flag-nonlock-" + UUID.randomUUID();
        String to = "agent-lock-flag-nonlock-" + UUID.randomUUID();
        String subspace = "mailbox/" + to;
        byte[] id = repo.out(tenant, subspace, Map.of("to", to), Map.of("from", "sender"),
                "body", "nonce-nonlock", null);
        var claimed = repo.inp(tenant, subspace, Map.of("to", to), "worker-1", 60L);
        assertThat(claimed).as("must be claimable").isPresent();
        OffsetDateTime expiresAtBefore = rawRow(id).value1();
        String claimStateBefore = rawRow(id).value2();

        byte[] refireId = repo.out(tenant, subspace, Map.of("to", to), Map.of("from", "sender"),
                "body", "nonce-nonlock", null);

        assertThat(refireId).as("same id -- same row").isEqualTo(id);
        var after = rawRow(id);
        assertThat(after.value2()).as("claim state untouched by a non-lock refire").isEqualTo(claimStateBefore);
        assertThat(after.value1())
                .as("expires_at refreshed by the ordinary refire clamp, not reset via the lock branch")
                .isEqualTo(expiresAtBefore);
    }
}
