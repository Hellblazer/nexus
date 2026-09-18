// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TenantScope;
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
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
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

    // ── the defect this bead fixes: a late-committing row is not skipped ──────

    /**
     * Reproduces the exact defect nexus-vsipz's own bead description names: a
     * transaction that STARTS first (and so gets the earlier {@code created_at})
     * but COMMITS second, after a later-starting transaction has already been
     * announced. A cursor design (the one this same tree's client half replaces)
     * would have advanced past the second row's {@code (created_at, id)} and never
     * revisit anything sorting before it. Announce mode has no cursor to skip past
     * -- it re-scans the full claimable-and-due set, oldest first, on every call --
     * so the late-committing row is picked up the very next time anyone asks.
     */
    @Test
    void announce_lateCommittingRow_isReturnedAtNextCall() throws Exception {
        String toHeld = "announce-late-held-" + UUID.randomUUID();
        String toFast = "announce-late-fast-" + UUID.randomUUID();
        String templateName = registry.resolve("mailbox/" + toHeld).name();

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
                        .values(DSL.val(heldId), DSL.val(TENANT_A), DSL.val("mailbox/" + toHeld),
                                DSL.val(templateName), DSL.val(JSONB.valueOf("{\"to\":\"" + toHeld + "\"}")),
                                DSL.val("held"), DSL.currentOffsetDateTime().add(interval(3600)),
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
                    .as("the held transaction's insert ran before we proceed")
                    .isTrue();

            // A second, ordinary write to a DIFFERENT mailbox -- its own transaction
            // starts (and commits) strictly AFTER the held one started, so its
            // created_at sorts LATER.
            repo.out(TENANT_A, "mailbox/" + toFast, Map.of("to", toFast), Map.of("from", "sender"),
                    "fast", "nonce-fast", null);

            // The held row is uncommitted (invisible under READ COMMITTED): only the
            // fast mailbox's own spec sees anything.
            assertThat(probe(mailboxSpec(toFast, 0, 5))).hasSize(1);
            assertThat(probe(mailboxSpec(toHeld, 0, 5)))
                    .as("not yet committed -- invisible to every other transaction")
                    .isEmpty();

            release.countDown();
            held.get(5, TimeUnit.SECONDS);

            // Now committed, with a created_at STRICTLY EARLIER than the fast row's --
            // and it has never been announced. There is no cursor to have skipped past
            // it, so the very next call returns it.
            List<TupleRepository.WaitResult> afterCommit = probe(mailboxSpec(toHeld, 0, 5));
            assertThat(afterCommit)
                    .as("the late-committing row is returned at the next call -- the defect the cursor "
                            + "design (nexus-gomuo.1) carries and this design does not")
                    .hasSize(1);
            assertThat(afterCommit.get(0).tuples().get(0).announceCount()).isEqualTo(1);
        } finally {
            pool.shutdownNow();
        }
    }

    private static org.jooq.types.DayToSecond interval(long seconds) {
        return org.jooq.types.DayToSecond.valueOf(java.time.Duration.ofSeconds(seconds));
    }
}
