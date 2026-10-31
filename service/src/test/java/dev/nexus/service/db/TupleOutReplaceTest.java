// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.types.DayToSecond;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static dev.nexus.service.db.TupleRepository.OutOutcome.CREATED;
import static dev.nexus.service.db.TupleRepository.OutOutcome.DIRTIED;
import static dev.nexus.service.db.TupleRepository.OutOutcome.REPLACED;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-5r2sc.2.2 (RDR-235 E4, first half): {@code out} with {@code mode=replace} on a
 * template whose id comes from keys plus nonce (the aspect queue's nonce is the doc_id).
 * One test class per row state: none, available, claimed, consumed, dead and expired. The
 * outcome (created, replaced, dirtied) is the return value. A claimed row's holder is not
 * disturbed; its {@code ack} returns the row to available, attempts unchanged, carrying the
 * new body, instead of consuming it.
 *
 * <p>Test templates (a second registry source, as {@code TupleMaxLiveRowsTest} does):
 * {@code probe-rep/<room>} is keys+nonce with a live-row cap of 2 and {@code max_attempts} 3;
 * {@code probe-repkeys/<room>} is keys-only, on which replace must be refused.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleOutReplaceTest {

    private static final String TENANT = "tuple-tenant-out-replace";
    private static final String SVC_ROLE = "svc_tuple_out_replace_test";
    private static final String SVC_PASS = "svc_tuple_out_replace_test_pass";
    private static final long CAP = 2;
    private static final long RETENTION = 3600;

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TupleRepository repo;

    @BeforeAll
    void startAll(@TempDir Path extraTemplateDir) throws Exception {
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
        Files.writeString(extraTemplateDir.resolve("probe-rep.yaml"), """
                name: probe-rep/<room>
                keys:
                  - id
                id_from: keys+nonce
                take:
                  enabled: true
                  max_attempts: 3
                  max_lease_seconds: 300
                retention_seconds: %d
                max_live_rows: %d
                """.formatted(RETENTION, CAP), StandardCharsets.UTF_8);
        Files.writeString(extraTemplateDir.resolve("probe-repkeys.yaml"), """
                name: probe-repkeys/<room>
                keys:
                  - id
                id_from: keys
                take:
                  enabled: true
                  max_attempts: 3
                  max_lease_seconds: 300
                retention_seconds: 3600
                """, StandardCharsets.UTF_8);
        Files.writeString(extraTemplateDir.resolve("probe-replock.yaml"), """
                name: probe-replock/<room>
                keys:
                  - id
                id_from: keys+nonce
                take:
                  enabled: true
                  max_lease_seconds: 300
                retention_seconds: 3600
                lock: true
                """, StandardCharsets.UTF_8);
        repo = new TupleRepository(new TenantScope(svcDs),
                TemplateRegistry.loadAtBoot(extraTemplateDir.toString(), null,
                        NexusService.SWEEP_INTERVAL_HOURS * 3600L));
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

    @AfterEach
    void clearHook() {
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
        repo.testOnlyAtParkLoopTop = null;
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    /** One room is one subspace, so the live-row cap of 2 applies to it alone. */
    private record Room(String subspace, Map<String, String> keys) {
    }

    private static Room newRoom() {
        String name = "r-" + UUID.randomUUID().toString().substring(0, 10);
        return new Room("probe-rep/" + name, Map.of("id", name));
    }

    private byte[] out(Room r, String nonce, String body) {
        return repo.out(TENANT, r.subspace(), r.keys(), Map.of(), body, nonce, null, null, null);
    }

    private TupleRepository.OutResult replace(Room r, String nonce, String body) {
        return replace(r, nonce, body, null);
    }

    private TupleRepository.OutResult replace(Room r, String nonce, String body, Long delayS) {
        return repo.outReplace(TENANT, r.subspace(), r.keys(), Map.of(), body, nonce, null, null, delayS);
    }

    private byte[] outAs(Room r, String nonce, String body, String session) {
        return repo.out(TENANT, r.subspace(), r.keys(), Map.of(), body, nonce, null, session, null);
    }

    private TupleRepository.OutResult replaceAs(Room r, String nonce, String body, String session) {
        return repo.outReplace(TENANT, r.subspace(), r.keys(), Map.of(), body, nonce, null, session, null);
    }

    /** A room on the shipped {@code queue/<name>} template: keys+nonce, dims from (id-bearing), kind, correlation_id. */
    private static Room newQueueRoom() {
        String name = "q-" + UUID.randomUUID().toString().substring(0, 10);
        return new Room("queue/" + name, Map.of("queue", name));
    }

    private byte[] outQ(Room r, String nonce, Map<String, String> dims) {
        return repo.out(TENANT, r.subspace(), r.keys(), dims, "b", nonce, null, null, null);
    }

    private TupleRepository.OutResult replaceQ(Room r, String nonce, Map<String, String> dims) {
        return repo.outReplace(TENANT, r.subspace(), r.keys(), dims, "b", nonce, null, null, null);
    }

    /** The stored dims of a row as a plain map, read through the database's own jsonb accessors. */
    private Map<String, String> dims(byte[] id) throws Exception {
        return admin(ctx -> {
            Map<String, String> out = new java.util.TreeMap<>();
            for (String k : List.of("from", "kind", "correlation_id")) {
                String v = ctx.select(DSL.jsonbGetAttributeAsText(TUPLES.DIMS, k))
                        .from(TUPLES).where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.ID.eq(id)))
                        .fetchOne(0, String.class);
                if (v != null) {
                    out.put(k, v);
                }
            }
            return out;
        });
    }

    private TupleRepository.ClaimedTuple claim(Room r, String claimant) {
        var c = repo.inp(TENANT, r.subspace(), r.keys(), claimant, 60);
        assertThat(c).as("the row must be claimable").isPresent();
        return c.get();
    }

    private <T> T admin(java.util.function.Function<DSLContext, T> f) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return f.apply(DSL.using(su, SQLDialect.POSTGRES));
        }
    }

    private record Row(String body, String claimState, String claimId, String claimant, int attempts,
                       boolean dirty, OffsetDateTime availableAt, OffsetDateTime consumedAt,
                       OffsetDateTime createdAt, OffsetDateTime expiresAt, OffsetDateTime leaseUntil,
                       String senderSession) {
    }

    private Row row(byte[] id) throws Exception {
        return admin(ctx -> {
            var r = ctx.selectFrom(TUPLES).where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.ID.eq(id))).fetchOne();
            assertThat(r).as("the row exists").isNotNull();
            return new Row(r.getBody(), r.getClaimState(), r.getClaimId(), r.getClaimant(), r.getAttempts(),
                    r.getDirty(), r.getAvailableAt(), r.getConsumedAt(), r.getCreatedAt(), r.getExpiresAt(),
                    r.getLeaseUntil(), r.getSenderSession());
        });
    }

    private List<String> transitions(byte[] id) throws Exception {
        return admin(ctx -> ctx.select(TUPLE_CLAIM_LOG.TRANSITION).from(TUPLE_CLAIM_LOG)
                .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT).and(TUPLE_CLAIM_LOG.TUPLE_ID.eq(id)))
                .orderBy(TUPLE_CLAIM_LOG.LOG_ID.asc())
                .fetch(TUPLE_CLAIM_LOG.TRANSITION));
    }

    private static DayToSecond secs(long s) {
        return DayToSecond.valueOf(Duration.ofSeconds(s));
    }

    /** Moves created_at back by {@code createdAgo} and expires_at to {@code expiresIn} from the database now(). */
    private void age(byte[] id, long createdAgoSeconds, long expiresInSeconds) throws Exception {
        int n = admin(ctx -> ctx.update(TUPLES)
                .set(TUPLES.CREATED_AT, DSL.currentOffsetDateTime().sub(secs(createdAgoSeconds)))
                .set(TUPLES.EXPIRES_AT, expiresInSeconds >= 0
                        ? DSL.currentOffsetDateTime().add(secs(expiresInSeconds))
                        : DSL.currentOffsetDateTime().sub(secs(-expiresInSeconds)))
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.ID.eq(id))).execute());
        assertThat(n).isEqualTo(1);
    }

    private void setAvailableAtAhead(byte[] id, long seconds) throws Exception {
        int n = admin(ctx -> ctx.update(TUPLES)
                .set(TUPLES.AVAILABLE_AT, DSL.currentOffsetDateTime().add(secs(seconds)))
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.ID.eq(id))).execute());
        assertThat(n).isEqualTo(1);
    }

    private void lapseLease(byte[] id) throws Exception {
        int n = admin(ctx -> ctx.update(TUPLES)
                .set(TUPLES.LEASE_UNTIL, DSL.currentOffsetDateTime().sub(secs(300)))
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.ID.eq(id))).execute());
        assertThat(n).isEqualTo(1);
    }

    private double secondsFromNow(OffsetDateTime t) throws Exception {
        OffsetDateTime now = admin(ctx -> ctx.select(DSL.currentOffsetDateTime()).fetchOne(0, OffsetDateTime.class));
        return Duration.between(now, t).toMillis() / 1000.0;
    }

    private static String hex(byte[] id) {
        return HexFormat.of().formatHex(id);
    }

    // ── no row: created ──────────────────────────────────────────────────────

    @Test
    void replaceWithNoExistingRow_isCreated_andTheIdIsTheDeterministicOne() throws Exception {
        Room r = newRoom();
        var res = replace(r, "doc-1", "v1");
        assertThat(res.outcome()).isEqualTo(CREATED);
        assertThat(hex(res.id())).as("the same id a plain out computes")
                .isEqualTo(hex(repo.out(TENANT, r.subspace(), r.keys(), Map.of(), "v1", "doc-1", null, null, null)));
        Row row = row(res.id());
        assertThat(row.body()).isEqualTo("v1");
        assertThat(row.dirty()).isFalse();
        assertThat(row.attempts()).isZero();
    }

    // ── available: replaced, body only ───────────────────────────────────────

    @Test
    void replaceOnAnAvailableRow_replacesTheBody_keepsAttempts_andKeepsAvailableAt() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        repo.nack(TENANT, c.claimId(), "w1", 600L);   // attempts 1, a pending retry 600 s ahead
        Row before = row(id);
        assertThat(before.availableAt()).isNotNull();

        var res = replace(r, "doc-1", "v2", 5L);   // even a delay_s on the replace does not move it

        assertThat(res.outcome()).isEqualTo(REPLACED);
        assertThat(hex(res.id())).isEqualTo(hex(id));
        Row after = row(id);
        assertThat(after.body()).isEqualTo("v2");
        assertThat(after.attempts()).as("a pending retry's attempt count is worker state").isEqualTo(1);
        assertThat(after.availableAt()).as("a pending retry's backoff survives a bulk re-index")
                .isEqualTo(before.availableAt());
        assertThat(after.claimState()).isNull();
        assertThat(after.dirty()).isFalse();
    }

    @Test
    void replaceOnAnAvailableRow_withoutABackoff_doesNotStampAvailableAt() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        assertThat(replace(r, "doc-1", "v2", 60L).outcome()).isEqualTo(REPLACED);
        assertThat(row(id).availableAt()).as("replace of an available row never hides it").isNull();
        assertThat(claim(r, "w1").tuple().body()).isEqualTo("v2");
    }

    // ── claimed: dirtied ─────────────────────────────────────────────────────

    @Test
    void replaceOnAClaimedRow_replacesTheBodyAndMarksItDirty_changingNothingElse() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        Row before = row(id);

        var res = replace(r, "doc-1", "v2", 60L);

        assertThat(res.outcome()).isEqualTo(DIRTIED);
        Row after = row(id);
        assertThat(after.body()).isEqualTo("v2");
        assertThat(after.dirty()).isTrue();
        assertThat(after.claimState()).isEqualTo("claimed");
        assertThat(after.claimId()).as("the holder is not disturbed").isEqualTo(c.claimId());
        assertThat(after.claimant()).isEqualTo("w1");
        assertThat(after.attempts()).isEqualTo(before.attempts());
        assertThat(after.leaseUntil()).isEqualTo(before.leaseUntil());
        assertThat(after.createdAt()).isEqualTo(before.createdAt());
        assertThat(after.expiresAt()).isEqualTo(before.expiresAt());
        assertThat(after.availableAt()).as("never stamped on a claimed row, delay_s or not").isNull();
    }

    // ── scenario: re-enqueue during extraction ───────────────────────────────

    @Test
    void ackOfADirtyClaim_returnsTheRowToAvailable_attemptsUnchanged_withTheNewBody() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        replace(r, "doc-1", "v2");

        repo.ack(TENANT, c.claimId(), "w1");

        Row row = row(id);
        assertThat(row.consumedAt()).as("not consumed: the new content has not been extracted").isNull();
        assertThat(row.body()).isEqualTo("v2");
        assertThat(row.attempts()).as("an ack is not a failure: attempts unchanged").isZero();
        assertThat(row.dirty()).isFalse();
        assertThat(row.claimState()).isNull();
        assertThat(row.claimId()).isNull();
        assertThat(row.availableAt()).isNull();
        assertThat(transitions(id)).as("its own transition name, not nack").containsExactly("claim", "ack_dirty");

        var next = claim(r, "w2");
        assertThat(next.tuple().body()).as("the next claim sees the new body").isEqualTo("v2");
        repo.ack(TENANT, next.claimId(), "w2");
        assertThat(row(id).consumedAt()).as("the second ack, with no replace in between, consumes").isNotNull();
        assertThat(transitions(id)).containsExactly("claim", "ack_dirty", "claim", "ack");
    }

    /**
     * A parked {@code in} re-runs its claim on a one-second timer even with no signal, so a pickup alone
     * proves nothing. This one proves the signal: the park loop's top hook (a latch) shows the park has
     * started and its timer is running; the ack lands 150 ms later, so the timer alone would pick the row up
     * about 850 ms after the ack; the pickup must land within 600 ms of it. A box so loaded that the
     * signalled claim itself takes over 600 ms fails this, which is the margin chosen on purpose.
     */
    @Test
    void ackOfADirtyClaim_wakesAParkedIn_withinTheSignalNotTheTimer() throws Exception {
        Room r = newRoom();
        out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        replace(r, "doc-1", "v2");
        var parkStarted = new java.util.concurrent.CountDownLatch(1);
        repo.testOnlyAtParkLoopTop = parkStarted::countDown;
        var parked = java.util.concurrent.CompletableFuture.supplyAsync(() -> {
            var got = repo.in(TENANT, r.subspace(), r.keys(), "w2", 60L, 20);
            return new Object[] {got, System.nanoTime()};
        });
        assertThat(parkStarted.await(10, java.util.concurrent.TimeUnit.SECONDS)).as("the in has parked").isTrue();
        Thread.sleep(150);
        long ackedAt = System.nanoTime();
        repo.ack(TENANT, c.claimId(), "w1");
        Object[] result = parked.get(10, java.util.concurrent.TimeUnit.SECONDS);
        @SuppressWarnings("unchecked")
        var got = (java.util.Optional<TupleRepository.ClaimedTuple>) result[0];
        long pickupMillis = (((long) result[1]) - ackedAt) / 1_000_000L;
        assertThat(got).isPresent();
        assertThat(got.get().tuple().body()).isEqualTo("v2");
        assertThat(pickupMillis).as("picked up on the ack's signal, not the one-second timer")
                .isLessThan(600L);
    }

    @Test
    void ackWithReplyOnADirtyClaim_writesTheReply_andReturnsTheRequestToAvailable() throws Exception {
        Room r = newRoom();
        Room replies = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        replace(r, "doc-1", "v2");

        byte[] replyId = repo.ackWithReply(TENANT, c.claimId(), "w1",
                new TupleRepository.ReplySpec(replies.subspace(), replies.keys(), Map.of(), "done", null));

        assertThat(replyId).isNotNull();
        assertThat(row(replyId).body()).isEqualTo("done");
        Row req = row(id);
        assertThat(req.consumedAt()).isNull();
        assertThat(req.body()).isEqualTo("v2");
        assertThat(req.attempts()).isZero();
        assertThat(transitions(id)).containsExactly("claim", "ack_dirty");
    }

    @Test
    void aReplaceThatLandsBetweenTheAcksReadAndItsUpdate_isNotLost() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        // The ack read the row clean (dirty=false); the replace lands before the ack's UPDATE.
        TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> {
            TupleRepository.TEST_ONLY_CLAIM_MUTATION_READ_TO_UPDATE_DELAY = () -> { };
            assertThat(replace(r, "doc-1", "v2").outcome()).isEqualTo(DIRTIED);
        };

        repo.ack(TENANT, c.claimId(), "w1");

        Row row = row(id);
        assertThat(row.consumedAt()).as("the ack did not consume content it never saw").isNull();
        assertThat(row.body()).isEqualTo("v2");
        assertThat(transitions(id)).containsExactly("claim", "ack_dirty");
    }

    // ── gate observation O2: dirty is cleared on every claim ─────────────────

    @Test
    void dirtyIsClearedByTheNextClaimAfterALapse_soTheNextHoldersAckConsumes() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        claim(r, "w1");
        replace(r, "doc-1", "v2");
        assertThat(row(id).dirty()).isTrue();
        lapseLease(id);

        var second = claim(r, "w2");

        assertThat(second.tuple().body()).isEqualTo("v2");
        Row held = row(id);
        assertThat(held.dirty()).as("the lapsed holder's mark does not carry to the next holder").isFalse();
        assertThat(held.attempts()).as("the lapse counted an attempt").isEqualTo(1);
        repo.ack(TENANT, second.claimId(), "w2");
        assertThat(row(id).consumedAt()).as("w2 extracted the new body: its ack consumes").isNotNull();
        assertThat(transitions(id)).containsExactly("claim", "expire", "claim", "ack");
    }

    @Test
    void aLapsedClaimIsNotDirtied_itIsAPlainBodyReplace() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        claim(r, "w1");
        lapseLease(id);

        var res = replace(r, "doc-1", "v2");

        assertThat(res.outcome()).as("nobody holds it: no ack is coming").isEqualTo(REPLACED);
        Row row = row(id);
        assertThat(row.body()).isEqualTo("v2");
        assertThat(row.dirty()).isFalse();
    }

    @Test
    void everyPathThatEndsAClaimClearsDirty() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        replace(r, "doc-1", "v2");
        repo.release(TENANT, c.claimId(), "w1");
        assertThat(row(id).dirty()).as("release").isFalse();
        c = claim(r, "w1");
        replace(r, "doc-1", "v3");
        repo.nack(TENANT, c.claimId(), "w1", null);
        assertThat(row(id).dirty()).as("nack").isFalse();
        c = claim(r, "w1");
        replace(r, "doc-1", "v4");
        repo.nack(TENANT, c.claimId(), "w1", null, true);
        // nexus-7rwaj: a dead nack on a dirty row requeues it with the new body instead of dead-lettering.
        Row requeued = row(id);
        assertThat(requeued.claimState()).as("dead nack on a dirty row requeues").isNull();
        assertThat(requeued.dirty()).as("dead nack").isFalse();
        assertThat(requeued.body()).isEqualTo("v4");
        assertThat(requeued.attempts()).isZero();
    }

    // ── consumed or dead: reset to available ─────────────────────────────────

    @Test
    void replaceOnAConsumedRow_resetsItToAvailable_withARestartedRetentionClock() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        repo.ack(TENANT, c.claimId(), "w1");
        age(id, 1800, 600);    // created half an hour ago, ten minutes from expiry
        setAvailableAtAhead(id, 100);
        Row old = row(id);
        assertThat(old.consumedAt()).isNotNull();

        var res = replace(r, "doc-1", "v2");

        assertThat(res.outcome()).isEqualTo(REPLACED);
        Row row = row(id);
        assertThat(row.consumedAt()).isNull();
        assertThat(row.body()).isEqualTo("v2");
        assertThat(row.attempts()).isZero();
        assertThat(row.dirty()).isFalse();
        assertThat(row.claimState()).isNull();
        assertThat(row.claimId()).isNull();
        assertThat(row.leaseUntil()).isNull();
        assertThat(row.availableAt()).as("set from THIS write's delay_s: none, so NULL").isNull();
        assertThat(secondsFromNow(row.createdAt())).as("created_at restarted").isBetween(-30.0, 1.0);
        assertThat(secondsFromNow(row.expiresAt())).as("expires_at is now + the write's ttl (retention)")
                .isBetween(RETENTION - 30.0, RETENTION + 1.0);
        assertThat(claim(r, "w2").tuple().body()).isEqualTo("v2");
    }

    @Test
    void replaceOnADeadRow_resetsItToAvailable_withARestartedRetentionClock() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        repo.nack(TENANT, c.claimId(), "w1", null, true);
        age(id, 3000, 120);   // an old dead row, two minutes from its expiry
        assertThat(row(id).claimState()).isEqualTo("dead");

        var res = replace(r, "doc-1", "v2");

        assertThat(res.outcome()).isEqualTo(REPLACED);
        Row row = row(id);
        assertThat(row.claimState()).isNull();
        assertThat(row.attempts()).isZero();
        assertThat(row.body()).isEqualTo("v2");
        assertThat(secondsFromNow(row.createdAt())).as("created_at restarted").isBetween(-30.0, 1.0);
        assertThat(secondsFromNow(row.expiresAt())).as("an old dead row would otherwise expire soon after revival")
                .isBetween(RETENTION - 30.0, RETENTION + 1.0);
        assertThat(claim(r, "w2").tuple().attempts()).isZero();
    }

    @Test
    void aResetTakesThisWritesDelay_andClearsAStaleOne() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        repo.ack(TENANT, c.claimId(), "w1");
        replace(r, "doc-1", "v2", 60L);
        assertThat(secondsFromNow(row(id).availableAt())).as("hidden for this write's delay_s")
                .isBetween(50.0, 61.0);
        assertThat(repo.inp(TENANT, r.subspace(), r.keys(), "w2", 60)).isEmpty();
    }

    @Test
    void replaceOnAnExpiredButUnpurgedRow_isAReset() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        age(id, 600, -10);
        var res = replace(r, "doc-1", "v2");
        assertThat(res.outcome()).isEqualTo(REPLACED);
        assertThat(secondsFromNow(row(id).expiresAt())).isBetween(RETENTION - 30.0, RETENTION + 1.0);
        assertThat(claim(r, "w1").tuple().body()).isEqualTo("v2");
    }

    // ── decision: max_live_rows applies to a revival of a row that was not live ──

    @Test
    void revivingAConsumedRow_countsAgainstMaxLiveRows_andIsRefusedOverTheCap() throws Exception {
        Room r = newRoom();
        byte[] a = out(r, "doc-a", "a");
        var c = claim(r, "w1");
        repo.ack(TENANT, c.claimId(), "w1");        // doc-a consumed: not live
        out(r, "doc-b", "b");
        out(r, "doc-c", "c");                         // two live rows: the cap

        assertThatThrownBy(() -> replace(r, "doc-a", "a2"))
                .isInstanceOf(MaxLiveRowsExceededException.class);

        Row row = row(a);
        assertThat(row.consumedAt()).as("the refused revival changed nothing").isNotNull();
        assertThat(row.body()).isNull();
    }

    @Test
    void revivingAConsumedRow_underTheCap_isAllowed() throws Exception {
        Room r = newRoom();
        out(r, "doc-a", "a");
        var c = claim(r, "w1");
        repo.ack(TENANT, c.claimId(), "w1");
        out(r, "doc-b", "b");                          // one live row, cap 2
        assertThat(replace(r, "doc-a", "a2").outcome()).isEqualTo(REPLACED);
    }

    @Test
    void revivingADeadRow_atTheCap_isAllowed_becauseADeadRowIsAlreadyLive() throws Exception {
        Room r = newRoom();
        byte[] a = out(r, "doc-a", "a");
        var c = claim(r, "w1");
        repo.nack(TENANT, c.claimId(), "w1", null, true);   // dead, still counted as live
        out(r, "doc-b", "b");                                // live count 2: the cap

        assertThat(replace(r, "doc-a", "a2").outcome()).isEqualTo(REPLACED);
        assertThat(row(a).claimState()).isNull();
        assertThatThrownBy(() -> out(r, "doc-c", "c")).as("a new identity is still refused at the cap")
                .isInstanceOf(MaxLiveRowsExceededException.class);
    }

    @Test
    void revivingAnExpiredRow_countsAgainstMaxLiveRows() throws Exception {
        Room r = newRoom();
        byte[] a = out(r, "doc-a", "a");
        age(a, 600, -10);                                  // expired, unpurged: not live
        out(r, "doc-b", "b");
        out(r, "doc-c", "c");
        assertThatThrownBy(() -> replace(r, "doc-a", "a2")).isInstanceOf(MaxLiveRowsExceededException.class);
    }

    @Test
    void aCreatedByReplace_isHeldToTheCapToo() {
        Room r = newRoom();
        out(r, "doc-a", "a");
        out(r, "doc-b", "b");
        assertThatThrownBy(() -> replace(r, "doc-c", "c")).isInstanceOf(MaxLiveRowsExceededException.class);
    }

    @Test
    void aDeadRowThatPassesItsRetention_isRemovedBySweep_andStopsCountingAgainstTheCap() throws Exception {
        Room r = newRoom();
        byte[] a = out(r, "doc-a", "a");
        var c = claim(r, "w1");
        repo.nack(TENANT, c.claimId(), "w1", null, true);
        out(r, "doc-b", "b");
        assertThatThrownBy(() -> out(r, "doc-c", "c")).isInstanceOf(MaxLiveRowsExceededException.class);

        age(a, 4000, -10);                                 // the dead row passes its retention

        out(r, "doc-c", "c");                              // no longer counted: accepted
        repo.purgeExpiredTuplesBatch(TENANT, 1000, null);
        boolean stillThere = admin(ctx -> ctx.fetchExists(ctx.selectOne().from(TUPLES)
                .where(TUPLES.TENANT_ID.eq(TENANT).and(TUPLES.ID.eq(a)))));
        assertThat(stillThere).as("the sweep removed it").isFalse();
    }

    // ── mode validation and the unchanged plain path ─────────────────────────

    @Test
    void replaceOnAKeysOnlyTemplate_isRefused_namingTheTemplate() {
        String name = "k-" + UUID.randomUUID().toString().substring(0, 8);
        assertThatThrownBy(() -> repo.outReplace(TENANT, "probe-repkeys/" + name, Map.of("id", name), Map.of(),
                "x", null, null, null, null))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("mode")
                .hasMessageContaining("probe-repkeys/<room>");
    }

    @Test
    void aPlainOutWithAChangedBody_stillFailsNonceReuseConflict() {
        Room r = newRoom();
        out(r, "doc-1", "v1");
        assertThatThrownBy(() -> out(r, "doc-1", "v2")).isInstanceOf(NonceReuseConflictException.class);
        assertThat(replace(r, "doc-1", "v2").outcome()).as("replace is the sanctioned way").isEqualTo(REPLACED);
    }

    @Test
    void aPlainOutOfTheSameWrite_isStillAnIdempotentRefire() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        var c = claim(r, "w1");
        assertThat(hex(out(r, "doc-1", "v1"))).isEqualTo(hex(id));
        assertThat(row(id).dirty()).as("only replace marks a row dirty").isFalse();
        repo.ack(TENANT, c.claimId(), "w1");
        assertThat(row(id).consumedAt()).isNotNull();
    }

    @Test
    void replaceReturnsTheSameIdAPlainOutWouldCompute_acrossStates() throws Exception {
        Room r = newRoom();
        byte[] id = out(r, "doc-1", "v1");
        assertThat(hex(replace(r, "doc-1", "v2").id())).isEqualTo(hex(id));
        claim(r, "w1");
        assertThat(hex(replace(r, "doc-1", "v3").id())).isEqualTo(hex(id));
    }

    // ── review round: sender_session names the writer of the stored body, on every replace branch ─────

    @Test
    void replaceStampsTheReplacingWritersSenderSession_onEveryBranch() throws Exception {
        // available
        Room avail = newRoom();
        byte[] a = outAs(avail, "d", "v1", "sess-A");
        assertThat(row(a).senderSession()).isEqualTo("sess-A");
        assertThat(replaceAs(avail, "d", "v2", "sess-B").outcome()).isEqualTo(REPLACED);
        assertThat(row(a).senderSession()).as("available branch: the replacing writer's").isEqualTo("sess-B");

        // claimed with a live lease (dirtied)
        Room claimed = newRoom();
        byte[] c = outAs(claimed, "d", "v1", "sess-A");
        claim(claimed, "w1");
        assertThat(replaceAs(claimed, "d", "v2", "sess-C").outcome()).isEqualTo(DIRTIED);
        assertThat(row(c).senderSession()).as("claimed branch: the replacing writer's").isEqualTo("sess-C");

        // claimed whose lease lapsed (plain replace)
        Room lapsed = newRoom();
        byte[] l = outAs(lapsed, "d", "v1", "sess-A");
        claim(lapsed, "w1");
        lapseLease(l);
        assertThat(replaceAs(lapsed, "d", "v2", "sess-D").outcome()).isEqualTo(REPLACED);
        assertThat(row(l).senderSession()).as("lapsed-claim branch: the replacing writer's").isEqualTo("sess-D");

        // consumed (the revival branch)
        Room consumed = newRoom();
        byte[] k = outAs(consumed, "d", "v1", "sess-A");
        repo.ack(TENANT, claim(consumed, "w1").claimId(), "w1");
        assertThat(replaceAs(consumed, "d", "v2", "sess-E").outcome()).isEqualTo(REPLACED);
        assertThat(row(k).senderSession()).as("revival branch: the replacing writer's").isEqualTo("sess-E");

        // a replace with no session token stamps NULL: the stored body is not that of the first writer
        Room unstamped = newRoom();
        byte[] u = outAs(unstamped, "d", "v1", "sess-A");
        replaceAs(unstamped, "d", "v2", null);
        assertThat(row(u).senderSession()).as("no token: the stamp does not name a writer who did not write").isNull();
    }

    // ── review round: a replace carries the new dims ─────────────────────────────────────────────────

    @Test
    void replaceOnAnAvailableRow_storesTheNewDims() throws Exception {
        Room q = newQueueRoom();
        byte[] id = outQ(q, "doc-1", Map.of("from", "a", "kind", "k1"));
        assertThat(dims(id)).containsOnly(Map.entry("from", "a"), Map.entry("kind", "k1"));

        var res = replaceQ(q, "doc-1", Map.of("from", "a", "kind", "k2", "correlation_id", "c1"));

        assertThat(res.outcome()).isEqualTo(REPLACED);
        assertThat(hex(res.id())).as("kind and correlation_id are not id dims: the id holds").isEqualTo(hex(id));
        assertThat(dims(id)).containsOnly(Map.entry("from", "a"), Map.entry("kind", "k2"),
                Map.entry("correlation_id", "c1"));
    }

    @Test
    void replaceOnAClaimedRow_storesTheNewDims_andDropsOnesTheReplaceOmits() throws Exception {
        Room q = newQueueRoom();
        byte[] id = outQ(q, "doc-1", Map.of("from", "a", "kind", "k1", "correlation_id", "c1"));
        assertThat(repo.inp(TENANT, q.subspace(), q.keys(), "w1", 60L)).isPresent();

        var res = replaceQ(q, "doc-1", Map.of("from", "a", "kind", "k3"));

        assertThat(res.outcome()).isEqualTo(DIRTIED);
        assertThat(dims(id)).as("the stored dims are the replace's, not a merge")
                .containsOnly(Map.entry("from", "a"), Map.entry("kind", "k3"));
    }

    // ── review round: lock templates ─────────────────────────────────────────────────────────────

    @Test
    void replaceOnALockTemplate_isRefused_whateverItsIdFrom() throws Exception {
        String name = "l-" + UUID.randomUUID().toString().substring(0, 8);
        String subspace = "probe-replock/" + name;
        Map<String, String> keys = Map.of("id", name);
        assertThatThrownBy(() -> repo.outReplace(TENANT, subspace, keys, Map.of(), "x", "n1", null, null, null))
                .as("no row yet")
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("mode")
                .hasMessageContaining("lock");
        byte[] id = repo.out(TENANT, subspace, keys, Map.of(), "held", "n1", null, null, null);
        assertThat(repo.inp(TENANT, subspace, keys, "holder", 60L)).isPresent();
        assertThatThrownBy(() -> repo.outReplace(TENANT, subspace, keys, Map.of(), "stolen", "n1", null, null, null))
                .as("a held lock's row is not rewritten from under its holder")
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("lock");
        Row after = row(id);
        assertThat(after.body()).isEqualTo("held");
        assertThat(after.dirty()).isFalse();
        assertThat(after.claimant()).isEqualTo("holder");
    }

    // ── review round: the lapse-to-dead branch of claimOnce clears dirty ────────────────────────────

    @Test
    void aDirtyClaimWhoseLeaseLapsesAtMaxAttempts_isDeadLetteredWithDirtyClear() throws Exception {
        Room r = newRoom();   // max_attempts 3
        byte[] id = out(r, "doc-1", "v1");
        repo.nack(TENANT, claim(r, "w1").claimId(), "w1", null);   // attempts 1
        repo.nack(TENANT, claim(r, "w2").claimId(), "w2", null);   // attempts 2
        claim(r, "w3");
        assertThat(replace(r, "doc-1", "v2").outcome()).isEqualTo(DIRTIED);
        assertThat(row(id).dirty()).isTrue();
        lapseLease(id);

        // The next claim finds the lapsed lease, counts attempt 3 (== max_attempts), dead-letters, finds nothing.
        assertThat(repo.inp(TENANT, r.subspace(), r.keys(), "w4", 60L)).isEmpty();

        Row dead = row(id);
        assertThat(dead.claimState()).isEqualTo("dead");
        assertThat(dead.attempts()).isEqualTo(3);
        assertThat(dead.dirty()).as("a dead row holds no claim, so no dirty mark").isFalse();
        assertThat(dead.body()).as("the new body is kept").isEqualTo("v2");
        assertThat(transitions(id)).endsWith("expire", "dead");
    }
}
