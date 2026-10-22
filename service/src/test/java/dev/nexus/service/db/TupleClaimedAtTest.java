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
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-x907o: {@code claimed_at} is when the CURRENT claim started. The claim
 * transition sets it; renew moves {@code lease_until} (and a lock's {@code expires_at})
 * but never this; every path that clears {@code claim_id} (release, nack, a lapse the
 * next claim reclaims, the sweep) clears it with it. An ack leaves the claim columns
 * as they were, so it leaves this one too. It exists so a client can tell a 30-second
 * hold from a 3-day one without reading {@code tuple_claim_log}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleClaimedAtTest {

    private static final String TENANT = "tuple-tenant-claimed-at";
    private static final String SVC_ROLE = "svc_tuple_claimed_at_test";
    private static final String SVC_PASS = "svc_tuple_claimed_at_test_pass";

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

    private static String addr(String label) {
        return label + "-" + UUID.randomUUID().toString().substring(0, 10);
    }

    private TupleRepository.TupleRow onlyRow(String subspace, Map<String, String> pattern) {
        var rows = repo.rd(TENANT, subspace, pattern, 10, null, 0);
        assertThat(rows).as("exactly one row at %s", subspace).hasSize(1);
        return rows.get(0);
    }

    private record Claimed(String subspace, Map<String, String> pattern, String claimId,
                           OffsetDateTime before, OffsetDateTime after) {
    }

    private Claimed outAndClaim(String claimant, long leaseSeconds) {
        String to = addr("claimed-at");
        String subspace = "mailbox/" + to;
        Map<String, String> pattern = Map.of("to", to);
        repo.out(TENANT, subspace, pattern, Map.of("from", "a"), "b", "n-1", null);
        // Postgres keeps microseconds; the window is widened by one so a truncated stamp
        // is never judged against a nanosecond-precision bound.
        OffsetDateTime before = OffsetDateTime.now(ZoneOffset.UTC).minusNanos(1_000);
        var claimed = repo.inp(TENANT, subspace, pattern, claimant, leaseSeconds);
        OffsetDateTime after = OffsetDateTime.now(ZoneOffset.UTC);
        assertThat(claimed).as("the row must be claimable").isPresent();
        return new Claimed(subspace, pattern, claimed.get().claimId(), before, after);
    }

    @Test
    void anUnclaimedRowCarriesNoClaimedAt() {
        String to = addr("unclaimed");
        repo.out(TENANT, "mailbox/" + to, Map.of("to", to), Map.of("from", "a"), "b", "n-1", null);
        assertThat(onlyRow("mailbox/" + to, Map.of("to", to)).claimedAt()).isNull();
    }

    @Test
    void theClaimSetsClaimedAt_onTheReturnedRow_andOnTheReadBack() {
        Claimed c = outAndClaim("holder-1", 60);
        var rd = onlyRow(c.subspace(), c.pattern());
        assertThat(rd.claimedAt()).isNotNull();
        assertThat(rd.claimedAt()).isBetween(c.before(), c.after());
        // The row the claim itself returned agrees with the read-back.
        String to = c.pattern().get("to");
        var again = repo.inp(TENANT, c.subspace(), c.pattern(), "holder-1", 60);
        assertThat(again).as("same-claimant retake").isPresent();
        assertThat(again.get().claimId()).isEqualTo(c.claimId());
        assertThat(again.get().tuple().claimedAt()).as("retake of %s", to).isEqualTo(rd.claimedAt());
    }

    @Test
    void renewMovesTheLease_butNeverClaimedAt() throws Exception {
        Claimed c = outAndClaim("holder-r", 60);
        var first = onlyRow(c.subspace(), c.pattern());
        Thread.sleep(25);
        OffsetDateTime renewedTo = repo.renew(TENANT, c.claimId(), "holder-r", 600);
        var second = onlyRow(c.subspace(), c.pattern());
        // Non-vacuity: the renew really did move the lease, so an unchanged claimed_at
        // is the column being left alone and not a no-op renew.
        assertThat(second.leaseUntil()).isAfter(first.leaseUntil());
        assertThat(second.leaseUntil()).isEqualTo(renewedTo);
        assertThat(second.claimedAt()).isEqualTo(first.claimedAt());
        assertThat(second.claimedAt()).isNotNull();
    }

    @Test
    void aLockRenewMovesExpiresAt_butNeverClaimedAt() throws Exception {
        String resource = addr("lk");
        String subspace = "lock/" + resource;
        Map<String, String> pattern = Map.of("resource", resource);
        repo.out(TENANT, subspace, pattern, Map.of(), null, null, null);
        var claimed = repo.inp(TENANT, subspace, pattern, "lock-holder", 60);
        assertThat(claimed).isPresent();
        var first = onlyRow(subspace, pattern);
        Thread.sleep(25);
        repo.renew(TENANT, claimed.get().claimId(), "lock-holder", 120);
        var second = onlyRow(subspace, pattern);
        assertThat(second.expiresAt()).as("a lock renew moves expires_at").isAfter(first.expiresAt());
        assertThat(second.claimedAt()).isNotNull().isEqualTo(first.claimedAt());
    }

    @Test
    void releaseClearsClaimedAt_andTheNextClaimStampsItsOwn() throws Exception {
        Claimed c = outAndClaim("holder-rel", 60);
        var held = onlyRow(c.subspace(), c.pattern());
        repo.release(TENANT, c.claimId(), "holder-rel");
        assertThat(onlyRow(c.subspace(), c.pattern()).claimedAt()).as("released").isNull();
        Thread.sleep(25);
        var next = repo.inp(TENANT, c.subspace(), c.pattern(), "holder-two", 60);
        assertThat(next).isPresent();
        assertThat(next.get().tuple().claimedAt()).isAfter(held.claimedAt());
        assertThat(onlyRow(c.subspace(), c.pattern()).claimedAt()).isEqualTo(next.get().tuple().claimedAt());
    }

    @Test
    void nackClearsClaimedAt() {
        Claimed c = outAndClaim("holder-nack", 60);
        repo.nack(TENANT, c.claimId(), "holder-nack");
        assertThat(onlyRow(c.subspace(), c.pattern()).claimedAt()).isNull();
    }

    @Test
    void aLapsedLeaseReclaimedByAnotherClaimant_stampsTheNewClaimsStart() throws Exception {
        Claimed c = outAndClaim("holder-lapse", 60);
        var held = onlyRow(c.subspace(), c.pattern());
        try (Connection su = pg.createConnection("")) {
            org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .update(TUPLES)
                    .set(TUPLES.LEASE_UNTIL, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(5))
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(c.subspace())))
                    .execute();
        }
        Thread.sleep(25);
        var taken = repo.inp(TENANT, c.subspace(), c.pattern(), "holder-new", 60);
        assertThat(taken).isPresent();
        assertThat(taken.get().claimId()).isNotEqualTo(c.claimId());
        assertThat(taken.get().tuple().claimedAt()).isAfter(held.claimedAt());
    }

    /** Lapse the claim's lease (and optionally set attempts) the way the repo's own clock cannot. */
    private void lapse(Claimed c, Integer attempts) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var upd = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .update(TUPLES)
                    .set(TUPLES.LEASE_UNTIL, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(5));
            if (attempts != null) {
                upd.set(TUPLES.ATTEMPTS, attempts);
            }
            assertThat(upd.where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(c.subspace()))).execute())
                    .as("the lapse touched the row").isEqualTo(1);
        }
    }

    private org.jooq.Record2<String, OffsetDateTime> stateAndClaimedAt(Claimed c) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var rec = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLES.CLAIM_STATE, TUPLES.CLAIMED_AT)
                    .from(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(c.subspace())))
                    .fetchOne();
            assertThat(rec).isNotNull();
            return rec;
        }
    }

    @Test
    void aClaimThatLapsesAtMaxAttempts_isDeadLetteredByTheClaimItself_andClearsClaimedAt() throws Exception {
        // claimOnce's own dead-letter arm (not releaseOrDeadLetter): a lapsed row whose attempts,
        // plus the lapse, reach the template's max_attempts (mailbox: 3) is parked as dead by the
        // next claimant's inp, and the claim columns go with it.
        Claimed c = outAndClaim("holder-dl", 60);
        assertThat(stateAndClaimedAt(c).value2()).as("non-vacuity: the claim stamped claimed_at").isNotNull();
        lapse(c, 2);
        var taken = repo.inp(TENANT, c.subspace(), c.pattern(), "holder-next", 60);
        assertThat(taken).as("a dead-lettered row is not handed out").isEmpty();
        var after = stateAndClaimedAt(c);
        assertThat(after.value1()).as("claim_state").isEqualTo("dead");
        assertThat(after.value2()).as("claimed_at is cleared with claim_id").isNull();
    }

    @Test
    void theSweepsReleaseArm_clearsClaimedAtOnALapsedClaim() throws Exception {
        Claimed c = outAndClaim("holder-sweep", 60);
        assertThat(stateAndClaimedAt(c).value2()).as("non-vacuity: the claim stamped claimed_at").isNotNull();
        lapse(c, null);
        var result = repo.releaseLapsedClaimsBatch(TENANT, 300, null);
        assertThat(result.released()).as("the sweep released the lapsed claim").isGreaterThanOrEqualTo(1);
        var after = stateAndClaimedAt(c);
        assertThat(after.value1()).as("claim_state").isNull();
        assertThat(after.value2()).as("claimed_at is cleared with claim_id").isNull();
    }

    @Test
    void theSweepsDeadLetterArm_clearsClaimedAtAtMaxAttempts() throws Exception {
        Claimed c = outAndClaim("holder-sweep-dl", 60);
        assertThat(stateAndClaimedAt(c).value2()).as("non-vacuity: the claim stamped claimed_at").isNotNull();
        lapse(c, 2);
        var result = repo.releaseLapsedClaimsBatch(TENANT, 300, null);
        assertThat(result.deadLettered()).as("the sweep dead-lettered the exhausted claim").isGreaterThanOrEqualTo(1);
        var after = stateAndClaimedAt(c);
        assertThat(after.value1()).as("claim_state").isEqualTo("dead");
        assertThat(after.value2()).as("claimed_at is cleared with claim_id").isNull();
    }

    @Test
    void anAckLeavesTheClaimColumnsAsTheyWere_claimedAtIncluded() {
        Claimed c = outAndClaim("holder-ack", 60);
        var held = onlyRow(c.subspace(), c.pattern());
        repo.ack(TENANT, c.claimId(), "holder-ack");
        // rd hides consumed rows; read the column directly.
        try (Connection su = pg.createConnection("")) {
            var rec = org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .selectFrom(TUPLES)
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(c.subspace())))
                    .fetchOne();
            assertThat(rec).isNotNull();
            assertThat(rec.getConsumedAt()).isNotNull();
            assertThat(rec.getClaimId()).isEqualTo(c.claimId());
            assertThat(rec.getClaimedAt()).isEqualTo(held.claimedAt());
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void anExpiredLockResetByOut_clearsClaimedAtWithTheOtherClaimColumns() throws Exception {
        String resource = addr("lkreset");
        String subspace = "lock/" + resource;
        Map<String, String> pattern = Map.of("resource", resource);
        repo.out(TENANT, subspace, pattern, Map.of(), null, null, null);
        var claimed = repo.inp(TENANT, subspace, pattern, "lock-holder", 60);
        assertThat(claimed).isPresent();
        assertThat(onlyRow(subspace, pattern).claimedAt()).isNotNull();
        try (Connection su = pg.createConnection("")) {
            org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .update(TUPLES)
                    .set(TUPLES.EXPIRES_AT, OffsetDateTime.now(ZoneOffset.UTC).minusMinutes(1))
                    .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.SUBSPACE.eq(subspace)))
                    .execute();
        }
        repo.out(TENANT, subspace, pattern, Map.of(), null, null, null);
        var reset = onlyRow(subspace, pattern);
        assertThat(reset.claimState()).as("the reset cleared the claim").isNull();
        assertThat(reset.claimedAt()).isNull();
    }
}
