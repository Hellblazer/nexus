// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TooLargeException;
import dev.nexus.service.db.TupleLimits;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.jooq.nexus.tables.records.TuplesRecord;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.JSONB;
import org.jooq.impl.DSL;
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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_DELIVERIES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-vsipz (RDR-213 engine half) — {@code TupleRepository}'s announce-mode
 * {@code wait}/{@code rd} branch: a mailbox {@link TupleRepository.WaitSpec} carrying
 * an {@link TupleRepository.WaitSpec.Announce} restricts the match to claimable rows
 * due for a first-or-repeat announcement and stamps {@code announced_at}/{@code
 * announce_count} on every row it returns, in the same statement. Modeled directly on
 * {@code TupleWaitTest}'s own fixture shape (same hermetic embedded-Postgres harness),
 * against the bundled {@code mailbox/<address>} template (take-enabled, matching the
 * client waiter's own subspace).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleAnnounceTest {

    private static final String TENANT_A = "tuple-announce-tenant-a";
    private static final String SVC_ROLE = "svc_tuple_announce_test";
    private static final String SVC_PASS = "svc_tuple_announce_test_pass";

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
        repo = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                /* timeoutCapSeconds */ 10, /* parkCapPerClaimant */ 4, /* parkCapGlobal */ 16);
    }

    private static TupleRepository.WaitSpec mailboxSpec(String to, long intervalSeconds, int max) {
        return new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null,
                new TupleRepository.WaitSpec.Announce(intervalSeconds, max));
    }

    private List<TupleRepository.WaitResult> probe(TupleRepository.WaitSpec spec) {
        return repo.waitAny(TENANT_A, List.of(spec), 0);
    }

    // ── first announcement, stamped once ─────────────────────────────────────

    @Test
    void announce_firstCall_returnsClaimableRowOnce_andStampsIt() {
        String to = "announce-first-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                "hello", "nonce-1", null);

        List<TupleRepository.WaitResult> result = probe(mailboxSpec(to, 1, 5));

        assertThat(result).hasSize(1);
        assertThat(result.get(0).subscriber()).as("a row-level announce echoes no subscriber").isNull();
        TupleRepository.TupleRow row = result.get(0).tuples().get(0);
        assertThat(row.announcedAt()).as("stamped on the row it returns").isNotNull();
        assertThat(row.announceCount()).isEqualTo(1);
    }

    // ── rate limit: nothing again within interval_s, then a repeat with count 2 ──

    @Test
    void announce_secondCallWithinInterval_returnsNothing_thenAfterIntervalReturnsWithCountTwo()
            throws Exception {
        String to = "announce-interval-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                "hello", "nonce-1", null);

        TupleRepository.WaitSpec spec = mailboxSpec(to, 1, 5);
        List<TupleRepository.WaitResult> first = probe(spec);
        assertThat(first).hasSize(1);
        assertThat(first.get(0).tuples().get(0).announceCount()).isEqualTo(1);

        assertThat(probe(spec))
                .as("re-announcement due only after interval_s has elapsed")
                .isEmpty();

        Thread.sleep(1200);

        List<TupleRepository.WaitResult> third = probe(spec);
        assertThat(third).hasSize(1);
        assertThat(third.get(0).tuples().get(0).announceCount())
                .as("count continues from the first stamp, not reset")
                .isEqualTo(2);
    }

    // ── cap: nothing once announce_count reaches max ─────────────────────────

    @Test
    void announce_afterMaxReached_returnsNothing() throws Exception {
        String to = "announce-max-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                "hello", "nonce-1", null);

        TupleRepository.WaitSpec spec = mailboxSpec(to, 1, 2);
        assertThat(probe(spec).get(0).tuples().get(0).announceCount()).isEqualTo(1);
        Thread.sleep(1200);
        assertThat(probe(spec).get(0).tuples().get(0).announceCount()).isEqualTo(2);
        Thread.sleep(1200);

        assertThat(probe(spec))
                .as("announce_count(2) is no longer < max(2) -- never returned again by announce mode")
                .isEmpty();
    }

    // ── claimed rows excluded; released rows resume, count intact ────────────

    @Test
    void announce_claimedRow_excludedWhileLive_returnedAfterRelease_withCountContinuing() {
        String to = "announce-claim-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                "hello", "nonce-1", null);

        // interval_s=0: due again the instant announced_at is in the past, so the
        // claim exclusion below is the ONLY thing keeping the row from reappearing --
        // no sleep needed to isolate that variable.
        TupleRepository.WaitSpec spec = mailboxSpec(to, 0, 5);
        assertThat(probe(spec).get(0).tuples().get(0).announceCount()).isEqualTo(1);

        var claimed = repo.inp(TENANT_A, "mailbox/" + to, Map.of("to", to), "claimant-1", 30L);
        assertThat(claimed).isPresent();

        assertThat(probe(spec))
                .as("claimed-and-live rows are excluded from announce mode, exactly as in()/inp() would exclude them from a fresh claim")
                .isEmpty();

        repo.release(TENANT_A, claimed.get().claimId(), "claimant-1");

        List<TupleRepository.WaitResult> afterRelease = probe(spec);
        assertThat(afterRelease).hasSize(1);
        assertThat(afterRelease.get(0).tuples().get(0).announceCount())
                .as("release clears claim_state but never touches announce_count")
                .isEqualTo(2);
    }

    // ── dead-lettered rows never announced ────────────────────────────────────

    @Test
    void announce_deadRow_neverReturned() {
        String to = "announce-dead-" + UUID.randomUUID();
        byte[] id = repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                "hello", "nonce-1", null);
        tenantScope.withTenant(TENANT_A, ctx -> {
            ctx.update(TUPLES).set(TUPLES.CLAIM_STATE, "dead").where(TUPLES.ID.eq(id)).execute();
            return null;
        });

        assertThat(probe(mailboxSpec(to, 0, 5)))
                .as("a dead-lettered row is never returned by announce mode, at any interval")
                .isEmpty();
    }

    // ── since + announce together is refused ──────────────────────────────────

    @Test
    void waitAny_sinceWithAnnounce_isRefused() {
        String to = "announce-since-refused-" + UUID.randomUUID();
        var cursor = new TupleRepository.ReadCursor(java.time.OffsetDateTime.now(), new byte[32]);
        TupleRepository.WaitSpec bad = new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 1,
                cursor, new TupleRepository.WaitSpec.Announce(150, 5));

        assertThatThrownBy(() -> repo.waitAny(TENANT_A, List.of(bad), 0))
                .isInstanceOf(SchemaViolationException.class);
    }

    // ── Announce bounds (review round, bead nexus-vsipz) ──────────────────────

    @Test
    void announce_negativeIntervalSeconds_isRefused() {
        assertThatThrownBy(() -> new TupleRepository.WaitSpec.Announce(-1, 5))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("interval_s");
    }

    @Test
    void announce_maxZero_isRefused() {
        assertThatThrownBy(() -> new TupleRepository.WaitSpec.Announce(150, 0))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("max");
    }

    @Test
    void announce_maxNegative_isRefused() {
        assertThatThrownBy(() -> new TupleRepository.WaitSpec.Announce(150, -1))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("max");
    }

    @Test
    void announce_intervalZero_isAccepted() {
        // 0 is a valid (if aggressive) rate limit -- "due again the instant it
        // was last announced" -- distinct from a NEGATIVE interval, which would
        // invert the due comparison. Not refused.
        assertThat(new TupleRepository.WaitSpec.Announce(0, 5).intervalSeconds()).isZero();
    }

    @Test
    void announce_maxOne_isTheSmallestAcceptedValue_andAnnouncesExactlyOnce() {
        String to = "announce-max-one-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                "hello", "nonce-1", null);

        TupleRepository.WaitSpec.Announce announce = new TupleRepository.WaitSpec.Announce(0, 1);
        assertThat(announce.max()).isEqualTo(1);

        TupleRepository.WaitSpec spec = mailboxSpec(to, 0, 1);
        List<TupleRepository.WaitResult> first = probe(spec);
        assertThat(first).hasSize(1);
        assertThat(first.get(0).tuples().get(0).announceCount()).isEqualTo(1);

        assertThat(probe(spec))
                .as("announce_count(1) is never < max(1) -- a one-shot announce, never repeated")
                .isEmpty();
    }

    // ── a spec without announce is unchanged: rd's own since-cursor semantics ──

    @Test
    void waitAny_withoutAnnounce_isUnchangedFromToday() {
        String to = "announce-unset-" + UUID.randomUUID();
        repo.out(TENANT_A, "mailbox/" + to, Map.of("to", to), Map.of("from", "sender"),
                "hello", "nonce-1", null);

        TupleRepository.WaitSpec spec = new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null);
        List<TupleRepository.WaitResult> result = repo.waitAny(TENANT_A, List.of(spec), 0);

        assertThat(result).hasSize(1);
        TupleRepository.TupleRow row = result.get(0).tuples().get(0);
        assertThat(row.announcedAt()).as("a plain rd/wait read never stamps announce columns").isNull();
        assertThat(row.announceCount()).isZero();

        // A repeat probe with the SAME (no-cursor) spec must still return the row --
        // announce-mode's due-gating never applies here.
        assertThat(repo.waitAny(TENANT_A, List.of(spec), 0)).hasSize(1);
    }

    // ── the defect this bead fixes: a since cursor skips a late commit ────────

    /**
     * Reproduces the exact defect nexus-vsipz's own bead description names, on
     * ONE shared mailbox (review round S1 -- the original two-mailbox version
     * proved only ordinary READ COMMITTED visibility, since a cursor is scoped
     * per-subspace and no cursor was ever advanced past row A at all). Row A's
     * transaction STARTS first (the earlier {@code created_at}) but is held open
     * on a latch past row B's own transaction, which starts second and commits
     * immediately. A reader that has already delivered B and advanced a
     * {@code since} cursor to B's own {@code (created_at, id)} position:
     *
     * <ol>
     *   <li>(a) sees nothing further via that cursor while A is still
     *       uncommitted -- unsurprising, nothing new exists yet;</li>
     *   <li>(b) an independent announce-mode {@code wait} returns B once, on
     *       its own due check, unrelated to any cursor;</li>
     *   <li>A then commits, with a {@code created_at} STRICTLY EARLIER than
     *       B's;</li>
     *   <li>(c) THE DEFECT ITSELF: the SAME {@code since} cursor at B's
     *       position still returns nothing -- A is now committed and
     *       claimable, but {@code (A.created_at, A.id) > (B.created_at, B.id)}
     *       is FALSE (A sorts before B), so the row-order comparison every
     *       cursor query uses excludes it, silently and permanently, for any
     *       reader already holding this exact cursor;</li>
     *   <li>(d) an announce-mode {@code wait}, which holds no cursor at all,
     *       returns A: the full re-scan orders by {@code created_at} ASC
     *       every time, so A -- the oldest claimable-and-due row -- is
     *       returned regardless of when it happened to commit relative to
     *       B.</li>
     * </ol>
     *
     * The {@link CountDownLatch} synchronization (not a sleep) is what makes
     * this deterministic: A's insert is confirmed complete-but-uncommitted
     * before B's transaction ever starts, and A's commit is confirmed complete
     * before step (c)/(d) run.
     */
    @Test
    void announce_sinceCursorSkipsALateCommit_announceModeDoesNot() throws Exception {
        String toShared = "announce-late-shared-" + UUID.randomUUID();
        String templateName = registry.resolve("mailbox/" + toShared).name();
        Map<String, String> pattern = Map.of("to", toShared);

        byte[] heldId = new byte[32];
        new java.security.SecureRandom().nextBytes(heldId);

        CountDownLatch inserted = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try {
            // Writer A: opens a transaction and inserts row A (the EARLIER
            // created_at, since its transaction starts first), held open on
            // `release` before its own commit.
            Future<?> held = pool.submit(() -> tenantScope.withTenant(TENANT_A, ctx -> {
                ctx.insertInto(TUPLES,
                                TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE,
                                TUPLES.KEYS, TUPLES.BODY, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                        .values(DSL.val(heldId), DSL.val(TENANT_A), DSL.val("mailbox/" + toShared),
                                DSL.val(templateName), DSL.val(JSONB.valueOf("{\"to\":\"" + toShared + "\"}")),
                                DSL.val("A-held"), DSL.currentOffsetDateTime().add(interval(3600)),
                                DSL.currentOffsetDateTime())
                        .execute();
                inserted.countDown();
                try {
                    release.await(10, TimeUnit.SECONDS);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                }
                return (TuplesRecord) null;
            }));

            assertThat(inserted.await(5, TimeUnit.SECONDS))
                    .as("row A's insert ran (uncommitted) before we proceed")
                    .isTrue();

            // Writer B: an ordinary, immediately-committed write to the SAME
            // mailbox -- its transaction starts (and commits) strictly AFTER
            // A's started, so B's created_at sorts LATER than A's.
            repo.out(TENANT_A, "mailbox/" + toShared, Map.of("to", toShared), Map.of("from", "sender"),
                    "B-fast", "nonce-b", null);

            List<TupleRepository.TupleRow> seeded = repo.rd(TENANT_A, "mailbox/" + toShared, pattern, 10, null, 0);
            assertThat(seeded)
                    .as("A is still uncommitted -- only B is visible to a plain read")
                    .hasSize(1);
            TupleRepository.TupleRow rowB = seeded.get(0);
            var cursorAtB = new TupleRepository.ReadCursor(rowB.createdAt(), rowB.id());

            // (a) a since-shaped rd at B's own position -- exactly what a
            // cursor-advancing reader would issue right after delivering B --
            // sees nothing further while A is still uncommitted.
            assertThat(repo.rd(TENANT_A, "mailbox/" + toShared, pattern, 10, cursorAtB, 0))
                    .as("(a) nothing past B yet -- A has not committed")
                    .isEmpty();

            // (b) an announce-mode wait, independently, returns B once.
            TupleRepository.WaitSpec announceOne = new TupleRepository.WaitSpec(
                    "mailbox/" + toShared, pattern, 1, null, new TupleRepository.WaitSpec.Announce(0, 5));
            List<TupleRepository.WaitResult> announcedB = probe(announceOne);
            assertThat(announcedB).hasSize(1);
            assertThat(announcedB.get(0).tuples().get(0).id())
                    .as("(b) B is the only claimable-and-due row so far")
                    .isEqualTo(rowB.id());

            // A commits now, with a created_at STRICTLY EARLIER than B's.
            release.countDown();
            held.get(5, TimeUnit.SECONDS);

            // (c) THE DEFECT, asserted as the fact it is.
            assertThat(repo.rd(TENANT_A, "mailbox/" + toShared, pattern, 10, cursorAtB, 0))
                    .as("(c) A is committed and claimable, but its created_at sorts BEHIND the cursor's "
                            + "own position (B's) -- silently and permanently invisible to any reader already "
                            + "holding this exact since cursor. This is the defect nexus-vsipz fixes.")
                    .isEmpty();

            // (d) an announce-mode wait, which holds no cursor, returns A: the
            // oldest claimable-and-due row in the full re-scan, independent of
            // commit order.
            List<TupleRepository.WaitResult> announcedA = probe(announceOne);
            assertThat(announcedA)
                    .as("(d) announce mode has no cursor to have skipped past -- A is returned")
                    .hasSize(1);
            assertThat(announcedA.get(0).tuples().get(0).id())
                    .as("(d) A, not B -- both are due (interval_s=0), but n=1 caps the result to the "
                            + "single OLDEST due row, and A's created_at sorts before B's")
                    .isEqualTo(heldId);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── per-subscriber announce: boards (bead nexus-q82tk, RDR-213 boards half) ──

    private static TupleRepository.WaitSpec boardSpec(String topic, String subscriber, long intervalSeconds, int max) {
        return new TupleRepository.WaitSpec("board/" + topic, null, 10, null,
                new TupleRepository.WaitSpec.Announce(intervalSeconds, max, subscriber));
    }

    private void post(String topic, String from, String body, String nonce) {
        repo.out(TENANT_A, "board/" + topic, Map.of("topic", topic), Map.of("from", from), body, nonce, null);
    }

    @Test
    void subscriberAnnounce_eachSubscriberSeesAPostOnce_andNeverAgainAtMaxOne() {
        String topic = "sub-once-" + UUID.randomUUID();
        post(topic, "author", "v1 shipped", "n-1");

        TupleRepository.WaitSpec forA = boardSpec(topic, "session-a", 0, 1);
        TupleRepository.WaitSpec forB = boardSpec(topic, "session-b", 0, 1);

        List<TupleRepository.WaitResult> a1 = probe(forA);
        assertThat(a1).as("subscriber A is announced the post").hasSize(1);
        assertThat(a1.get(0).subscriber())
                .as("the result echoes the subscriber this engine honoured (the client's old-engine proof)")
                .isEqualTo("session-a");
        assertThat(a1.get(0).tuples().get(0).announceCount()).as("A's own per-subscriber count").isEqualTo(1);
        assertThat(probe(forA)).as("max=1: never again for A, interval 0 notwithstanding").isEmpty();

        List<TupleRepository.WaitResult> b1 = probe(forB);
        assertThat(b1).as("A's stamp does not silence the post for B").hasSize(1);
        assertThat(b1.get(0).tuples().get(0).announceCount()).as("B's count starts at 1, not 2").isEqualTo(1);
        assertThat(probe(forB)).isEmpty();

        List<TupleRepository.TupleRow> plain = repo.rd(TENANT_A, "board/" + topic, null, 10, null, 0);
        assertThat(plain.get(0).announceCount())
                .as("the row's OWN announce_count is untouched: the stamp lives in tuple_deliveries")
                .isEqualTo(0);
        assertThat(plain.get(0).announcedAt()).isNull();
    }

    @Test
    void subscriberAnnounce_repeatsAfterIntervalUpToMax_perSubscriber() throws Exception {
        String topic = "sub-repeat-" + UUID.randomUUID();
        post(topic, "author", "hello", "n-1");

        TupleRepository.WaitSpec spec = boardSpec(topic, "session-a", 1, 2);
        assertThat(probe(spec).get(0).tuples().get(0).announceCount()).isEqualTo(1);
        assertThat(probe(spec)).as("within interval_s: nothing").isEmpty();
        Thread.sleep(1200);
        assertThat(probe(spec).get(0).tuples().get(0).announceCount())
                .as("second announcement continues the per-subscriber count").isEqualTo(2);
        Thread.sleep(1200);
        assertThat(probe(spec)).as("max=2 reached: silent for this subscriber").isEmpty();
    }

    @Test
    void subscriberAnnounce_blankSubscriber_isRefused() {
        assertThatThrownBy(() -> new TupleRepository.WaitSpec.Announce(0, 1, "   "))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("subscriber");
    }

    @Test
    void subscriberAnnounce_oversizedSubscriber_isRefused() {
        assertThatThrownBy(() -> new TupleRepository.WaitSpec.Announce(0, 1, "s".repeat(TupleLimits.MAX_CLAIMANT_BYTES + 1)))
                .as("the same typed error every oversized field raises, claimant included")
                .isInstanceOf(TooLargeException.class)
                .hasMessageContaining("subscriber");
    }

    @Test
    void subscriberAnnounce_sinceTogetherWithAnnounce_isStillRefused() {
        String topic = "sub-since-" + UUID.randomUUID();
        var cursor = new TupleRepository.ReadCursor(OffsetDateTime.now(ZoneOffset.UTC), new byte[32]);
        TupleRepository.WaitSpec both = new TupleRepository.WaitSpec("board/" + topic, null, 10, cursor,
                new TupleRepository.WaitSpec.Announce(0, 1, "session-a"));
        assertThatThrownBy(() -> repo.waitAny(TENANT_A, List.of(both), 0))
                .isInstanceOf(SchemaViolationException.class)
                .hasMessageContaining("since");
    }

    @Test
    void subscriberAnnounce_deliveryRowsCascadeWhenTheSweepPurgesThePost() {
        String topic = "sub-cascade-" + UUID.randomUUID();
        String templateName = registry.resolve("board/" + topic).name();
        byte[] id = new byte[32];
        new java.security.SecureRandom().nextBytes(id);
        // A post that is already expired: seeded directly so the sweep's purge
        // arm has something to delete on its very first batch.
        tenantScope.withTenant(TENANT_A, ctx -> {
            ctx.insertInto(TUPLES,
                            TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE,
                            TUPLES.KEYS, TUPLES.BODY, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                    .values(DSL.val(id), DSL.val(TENANT_A), DSL.val("board/" + topic), DSL.val(templateName),
                            DSL.val(JSONB.valueOf("{\"topic\":\"" + topic + "\"}")), DSL.val("old"),
                            DSL.currentOffsetDateTime().add(interval(3600)), DSL.currentOffsetDateTime())
                    .execute();
            return (TuplesRecord) null;
        });
        assertThat(probe(boardSpec(topic, "session-a", 0, 1))).as("announced once, delivery row written").hasSize(1);
        Integer before = tenantScope.withTenant(TENANT_A, ctx -> ctx.fetchCount(TUPLE_DELIVERIES,
                TUPLE_DELIVERIES.TUPLE_ID.eq(id)));
        assertThat(before).isEqualTo(1);

        tenantScope.withTenant(TENANT_A, ctx -> ctx.update(TUPLES)
                .set(TUPLES.EXPIRES_AT, DSL.currentOffsetDateTime().sub(interval(1)))
                .where(TUPLES.ID.eq(id)).execute());
        var purged = repo.purgeExpiredTuplesBatch(TENANT_A, 100, java.time.Duration.ofSeconds(5));
        assertThat(purged.purged()).isGreaterThanOrEqualTo(1);

        Integer after = tenantScope.withTenant(TENANT_A, ctx -> ctx.fetchCount(TUPLE_DELIVERIES,
                TUPLE_DELIVERIES.TUPLE_ID.eq(id)));
        assertThat(after).as("ON DELETE CASCADE: the purge bounds tuple_deliveries").isEqualTo(0);
    }

    /**
     * The board-path form of {@link #announce_sinceCursorSkipsALateCommit_announceModeDoesNot}
     * (bead nexus-q82tk): one board, two writers, one subscriber. Post A's
     * transaction starts first and is held open past post B's commit. A
     * subscriber announced B (its per-subscriber delivery row written) is still
     * announced A when A commits, because the per-subscriber due check re-scans
     * every row with no position to have passed A -- where a {@code since}
     * cursor at B's position, (c) below, never returns A at all. Strict: this
     * is the race the client's board cursor carried since RDR-211 shipped.
     */
    @Test
    void subscriberAnnounce_lateCommittingPost_isStillAnnouncedToTheSubscriber() throws Exception {
        String topic = "sub-late-" + UUID.randomUUID();
        String templateName = registry.resolve("board/" + topic).name();
        byte[] heldId = new byte[32];
        new java.security.SecureRandom().nextBytes(heldId);

        CountDownLatch inserted = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try {
            Future<?> held = pool.submit(() -> tenantScope.withTenant(TENANT_A, ctx -> {
                ctx.insertInto(TUPLES,
                                TUPLES.ID, TUPLES.TENANT_ID, TUPLES.SUBSPACE, TUPLES.TEMPLATE,
                                TUPLES.KEYS, TUPLES.BODY, TUPLES.EXPIRES_AT, TUPLES.CREATED_AT)
                        .values(DSL.val(heldId), DSL.val(TENANT_A), DSL.val("board/" + topic),
                                DSL.val(templateName), DSL.val(JSONB.valueOf("{\"topic\":\"" + topic + "\"}")),
                                DSL.val("A-held"), DSL.currentOffsetDateTime().add(interval(3600)),
                                DSL.currentOffsetDateTime())
                        .execute();
                inserted.countDown();
                try {
                    release.await(10, TimeUnit.SECONDS);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                }
                return (TuplesRecord) null;
            }));
            assertThat(inserted.await(5, TimeUnit.SECONDS)).isTrue();

            post(topic, "author", "B-fast", "n-b");
            List<TupleRepository.TupleRow> seeded = repo.rd(TENANT_A, "board/" + topic, null, 10, null, 0);
            assertThat(seeded).as("A uncommitted: only B visible").hasSize(1);
            TupleRepository.TupleRow rowB = seeded.get(0);
            var cursorAtB = new TupleRepository.ReadCursor(rowB.createdAt(), rowB.id());

            TupleRepository.WaitSpec forA = boardSpec(topic, "session-a", 0, 1);
            List<TupleRepository.WaitResult> announcedB = probe(forA);
            assertThat(announcedB).hasSize(1);
            assertThat(announcedB.get(0).tuples().get(0).id()).isEqualTo(rowB.id());

            release.countDown();
            held.get(5, TimeUnit.SECONDS);

            // (c) the cursor a board subscriber used to hold: A is invisible to it.
            assertThat(repo.rd(TENANT_A, "board/" + topic, null, 10, cursorAtB, 0))
                    .as("(c) a since cursor at B's position never returns the late-committed A")
                    .isEmpty();
            // (d) the per-subscriber due check has no position: A is announced.
            List<TupleRepository.WaitResult> announcedA = probe(forA);
            assertThat(announcedA).as("(d) A is due for session-a: no delivery row for it yet").hasSize(1);
            assertThat(announcedA.get(0).tuples().get(0).id()).isEqualTo(heldId);
            assertThat(probe(forA)).as("both posts now stamped once for session-a").isEmpty();
        } finally {
            pool.shutdownNow();
        }
    }

    private static org.jooq.types.DayToSecond interval(long seconds) {
        return org.jooq.types.DayToSecond.valueOf(java.time.Duration.ofSeconds(seconds));
    }
}
