// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import dev.nexus.service.tuples.TemplateSchema;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatExceptionOfType;

/**
 * RDR-211 Phase 1 Step 2 (bead nexus-rplay.8): the three shipped templates,
 * {@code board/<topic>}, {@code queue/<name>}, and {@code lock/<resource>}
 * (Approach items 1-3, Technical Design "Templates", Test Plan). Loads the
 * REAL bundled resources via {@link TemplateRegistry#loadAtBoot} with no
 * extra template directory, so every assertion here exercises the exact
 * YAML files shipped under {@code service/src/main/resources/tuples/
 * templates/}, not a smaller stand-in.
 *
 * <p>{@code release}, the lock flag's four sites, and {@code max_live_rows}
 * are already covered end to end against test-only templates in {@link
 * TupleReleaseTest}, {@link TupleLockFlagTest}, and {@link
 * TupleMaxLiveRowsTest} (Phase 1 Step 1, bead nexus-rplay.2/.3/.5). This
 * class does not re-prove those mechanisms; it proves the three SHIPPED
 * templates actually wire the right fields to them, per the RDR's own Test
 * Plan scenarios.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleTemplatesTest {

    private static final String SVC_ROLE = "svc_tuple_templates_test";
    private static final String SVC_PASS = "svc_tuple_templates_test_pass";

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
        cfg.setMaximumPoolSize(16);
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

    // ── helpers ──────────────────────────────────────────────────────────────

    private String freshTenant(String label) {
        return "tuple-templates-" + label + "-" + UUID.randomUUID();
    }

    // ── each template loads, registry has six, names resolve ────────────────

    @Test
    void registryHasSixTemplatesAndTheThreeNewOnesResolve() {
        assertThat(registry.templates()).hasSize(6);
        assertThat(registry.byName("board/<topic>")).as("board must load").isNotNull();
        assertThat(registry.byName("queue/<name>")).as("queue must load").isNotNull();
        assertThat(registry.byName("lock/<resource>")).as("lock must load").isNotNull();
        assertThat(registry.resolve("board/release-notes")).as("board resolves a concrete topic").isNotNull();
        assertThat(registry.resolve("queue/builds")).as("queue resolves a concrete name").isNotNull();
        assertThat(registry.resolve("lock/migration")).as("lock resolves a concrete resource").isNotNull();
    }

    @Test
    void boardTemplateShape() {
        TemplateSchema board = registry.byName("board/<topic>");
        assertThat(Set.copyOf(board.keys())).containsExactly("topic");
        assertThat(board.dimensions().get("from").required()).as("from is required on board").isTrue();
        assertThat(board.dimensions().get("kind").required()).isFalse();
        assertThat(board.idFrom()).isEqualTo(TemplateSchema.IdFrom.KEYS_NONCE);
        assertThat(board.idDims()).containsExactly("from");
        assertThat(board.take().enabled()).as("board never takeable").isFalse();
        assertThat(board.retentionSeconds()).isEqualTo(604_800L);
        assertThat(board.maxBodyBytes()).isEqualTo(1024L);
        assertThat(board.maxLiveRows()).isEqualTo(500L);
    }

    @Test
    void queueTemplateShape() {
        TemplateSchema queue = registry.byName("queue/<name>");
        assertThat(Set.copyOf(queue.keys())).containsExactly("queue");
        assertThat(queue.dimensions().get("from").required()).as("from is required on queue").isTrue();
        assertThat(queue.dimensions()).containsKeys("kind", "correlation_id");
        assertThat(queue.idFrom()).isEqualTo(TemplateSchema.IdFrom.KEYS_NONCE);
        assertThat(queue.idDims()).containsExactly("from");
        assertThat(queue.take().enabled()).isTrue();
        assertThat(queue.take().maxAttempts()).isEqualTo(3L);
        assertThat(queue.take().maxLeaseSeconds()).isEqualTo(900L);
        assertThat(queue.retentionSeconds()).isEqualTo(172_800L);
        assertThat(queue.maxLiveRows()).isEqualTo(10_000L);
        assertThat(queue.claimLogTtlSeconds()).isEqualTo(2_592_000L);
    }

    @Test
    void lockTemplateShape() {
        TemplateSchema lock = registry.byName("lock/<resource>");
        assertThat(Set.copyOf(lock.keys())).containsExactly("resource");
        assertThat(lock.dimensions().get("from").required())
                .as("from is OPTIONAL on lock -- the RDR's Technical Design table spells board/queue's "
                        + "from as required but lock's only as \"dims from\", no (required) qualifier")
                .isFalse();
        assertThat(lock.idFrom()).isEqualTo(TemplateSchema.IdFrom.KEYS);
        assertThat(lock.idDims()).as("from is not required, so it cannot enter id_dims").isEmpty();
        assertThat(lock.take().enabled()).isTrue();
        assertThat(lock.take().maxAttempts()).as("max_attempts omitted -- a lock never dead-letters").isNull();
        assertThat(lock.take().maxLeaseSeconds()).isEqualTo(900L);
        assertThat(lock.retentionSeconds()).isEqualTo(604_800L);
        assertThat(lock.lock()).isTrue();
        assertThat(lock.claimLogTtlSeconds()).isEqualTo(2_592_000L);
    }

    // ── each rejects a missing required dimension; lock's from is optional ──

    @Test
    void boardRejectsAMissingFromDimension() {
        String tenant = freshTenant("board-missing-from");
        String topic = "topic-" + UUID.randomUUID();
        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.out(tenant, "board/" + topic, Map.of("topic", topic), Map.of(),
                        "hello", null, null));
    }

    @Test
    void queueRejectsAMissingFromDimension() {
        String tenant = freshTenant("queue-missing-from");
        String queue = "queue-" + UUID.randomUUID();
        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.out(tenant, "queue/" + queue, Map.of("queue", queue), Map.of(),
                        "task", null, null));
    }

    /**
     * The task brief's own instruction: lock's {@code from} is OPTIONAL per the RDR
     * shape (unlike board's and queue's required {@code from}), so an {@code out}
     * naming no {@code from} at all must succeed, not raise {@code
     * SchemaViolationException}.
     */
    @Test
    void lockAcceptsOutWithNoFromDimensionAtAll() {
        String tenant = freshTenant("lock-no-from");
        String resource = "res-" + UUID.randomUUID();
        byte[] id = repo.out(tenant, "lock/" + resource, Map.of("resource", resource), Map.of(),
                null, null, null);
        assertThat(id).isNotNull();
    }

    // ── board is never takeable ──────────────────────────────────────────────

    @Test
    void inOnABoardSubspaceRaisesTakeDisabled() {
        String tenant = freshTenant("board-takedisabled");
        String topic = "topic-" + UUID.randomUUID();
        repo.out(tenant, "board/" + topic, Map.of("topic", topic), Map.of("from", "poster-1"), "hi",
                "nonce-" + UUID.randomUUID(), null);

        assertThatExceptionOfType(TakeDisabledException.class)
                .isThrownBy(() -> repo.inp(tenant, "board/" + topic, Map.of("topic", topic), "reader-1", 60L));
    }

    // ── queue dead-letters at three failures ─────────────────────────────────

    @Test
    void queueDeadLettersAtThreeNacks() {
        String tenant = freshTenant("queue-deadletter");
        String queue = "queue-" + UUID.randomUUID();
        byte[] id = repo.out(tenant, "queue/" + queue, Map.of("queue", queue), Map.of("from", "producer-1"),
                "task-1", "nonce-" + UUID.randomUUID(), null);

        for (int attempt = 0; attempt < 3; attempt++) {
            var claimed = repo.inp(tenant, "queue/" + queue, Map.of("queue", queue), "worker-1", 60L);
            assertThat(claimed).as("attempt " + attempt + " must still be claimable").isPresent();
            repo.nack(tenant, claimed.get().claimId(), "worker-1");
        }

        // Dead-lettered: no longer claimable, but still readable by rd.
        assertThat(repo.inp(tenant, "queue/" + queue, Map.of("queue", queue), "worker-2", 60L))
                .as("a dead letter is never claimable again").isEmpty();
        var rows = repo.rdp(tenant, "queue/" + queue, Map.of("queue", queue), 10, null);
        assertThat(rows).as("dead letter still readable").hasSize(1);
        assertThat(rows.get(0).id()).isEqualTo(id);
    }

    // ── lock out() is idempotent: one tuple per resource ─────────────────────

    @Test
    void lockOutIsIdempotentAcrossTwoCalls() {
        String tenant = freshTenant("lock-idempotent");
        String resource = "res-" + UUID.randomUUID();
        byte[] id1 = repo.out(tenant, "lock/" + resource, Map.of("resource", resource),
                Map.of("from", "holder-1"), null, null, null);
        byte[] id2 = repo.out(tenant, "lock/" + resource, Map.of("resource", resource),
                Map.of("from", "holder-1"), null, null, null);

        assertThat(id2).as("same identity -- same row").isEqualTo(id1);
        var rows = repo.rdp(tenant, "lock/" + resource, Map.of("resource", resource), 10, null);
        assertThat(rows).as("exactly one lock tuple exists").hasSize(1);
    }

    @Test
    void twoThreadsRunningOutOnTheSameLockConcurrentlyLeaveOneRow() throws Exception {
        String tenant = freshTenant("lock-concurrent-out");
        String resource = "res-" + UUID.randomUUID();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CompletableFuture<byte[]> a = CompletableFuture.supplyAsync(() ->
                    repo.out(tenant, "lock/" + resource, Map.of("resource", resource),
                            Map.of("from", "holder-a"), null, null, null), pool);
            CompletableFuture<byte[]> b = CompletableFuture.supplyAsync(() ->
                    repo.out(tenant, "lock/" + resource, Map.of("resource", resource),
                            Map.of("from", "holder-b"), null, null, null), pool);
            CompletableFuture.allOf(a, b).get(20, TimeUnit.SECONDS);

            var rows = repo.rdp(tenant, "lock/" + resource, Map.of("resource", resource), 10, null);
            assertThat(rows).as("one lock tuple exists, regardless of which out() commits second").hasSize(1);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── two readers wait on a board, one post arrives ────────────────────────

    /**
     * RDR Test Plan: "two readers wait on a board, one post arrives: both wake and
     * read it; the post is still readable afterwards." Uses {@code waitAny}
     * (RDR-211 Phase 1 Step 1) rather than a raw {@code rd}, since that is the
     * multiplexed primitive the RDR's session-MCP-server waiter is built on.
     */
    @Test
    void twoReadersWaitOnABoardAndBothWakeOnOnePost() throws Exception {
        String tenant = freshTenant("board-two-readers");
        String topic = "topic-" + UUID.randomUUID();
        String subspace = "board/" + topic;
        var spec = new TupleRepository.WaitSpec(subspace, Map.of("topic", topic), 5, null);

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CompletableFuture<List<TupleRepository.WaitResult>> reader1 = CompletableFuture.supplyAsync(
                    () -> repo.waitAny(tenant, List.of(spec), 20L), pool);
            CompletableFuture<List<TupleRepository.WaitResult>> reader2 = CompletableFuture.supplyAsync(
                    () -> repo.waitAny(tenant, List.of(spec), 20L), pool);

            Thread.sleep(500); // let both readers register + park before the post lands

            repo.out(tenant, subspace, Map.of("topic", topic), Map.of("from", "poster-1"), "hello board",
                    "nonce-" + UUID.randomUUID(), null);

            var result1 = reader1.get(20, TimeUnit.SECONDS);
            var result2 = reader2.get(20, TimeUnit.SECONDS);

            assertThat(result1).as("reader 1 must see the post").hasSize(1);
            assertThat(result1.get(0).tuples()).hasSize(1);
            assertThat(result2).as("reader 2 must also see the post").hasSize(1);
            assertThat(result2.get(0).tuples()).hasSize(1);

            var stillThere = repo.rdp(tenant, subspace, Map.of("topic", topic), 10, null);
            assertThat(stillThere).as("rd never consumes -- the post is still readable afterwards").hasSize(1);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── two workers, one task: exactly one claims it ─────────────────────────

    @Test
    void twoWorkersOneTaskExactlyOneClaimsIt() throws Exception {
        String tenant = freshTenant("queue-two-workers");
        String queue = "queue-" + UUID.randomUUID();
        String subspace = "queue/" + queue;
        repo.out(tenant, subspace, Map.of("queue", queue), Map.of("from", "producer-1"), "task-1",
                "nonce-" + UUID.randomUUID(), null);

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            CompletableFuture<Optional<TupleRepository.ClaimedTuple>> w1 = CompletableFuture.supplyAsync(
                    () -> repo.inp(tenant, subspace, Map.of("queue", queue), "worker-1", 60L), pool);
            CompletableFuture<Optional<TupleRepository.ClaimedTuple>> w2 = CompletableFuture.supplyAsync(
                    () -> repo.inp(tenant, subspace, Map.of("queue", queue), "worker-2", 60L), pool);
            CompletableFuture.allOf(w1, w2).get(20, TimeUnit.SECONDS);

            boolean oneClaimed = w1.get().isPresent() ^ w2.get().isPresent();
            assertThat(oneClaimed).as("exactly one of the two workers claims the single task").isTrue();
        } finally {
            pool.shutdownNow();
        }
    }

    // ── a worker releases a task on the real queue template ──────────────────

    private org.jooq.Record1<Integer> rawAttempts(byte[] id) {
        try (Connection su = pg.createConnection("")) {
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES)
                    .select(TUPLES.ATTEMPTS)
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

    /**
     * RDR-211 Test Plan: "a worker releases a task: attempts unchanged, the task
     * is available, a waiting worker wakes" -- {@link TupleReleaseTest} already
     * proves this scenario against {@code mailbox/<address>}, and {@link
     * TupleLockFlagTest} proves release's lock-specific interaction, but nothing
     * before this bead drove it against the real, SHIPPED {@code queue/<name>}
     * template the scenario is actually about (a shared work queue, not a
     * mailbox). Producer posts one task; worker A claims it, releases it
     * (attempts unchanged); a second worker B parked on {@code in} wakes with it;
     * the claim log shows exactly {@code [claim, release, claim]}.
     */
    @Test
    void queueWorkerReleasesTask_attemptsUnchanged_parkedWorkerWakesWithIt() throws Exception {
        String tenant = freshTenant("queue-release-wake");
        String queue = "queue-" + UUID.randomUUID();
        String subspace = "queue/" + queue;
        byte[] id = repo.out(tenant, subspace, Map.of("queue", queue), Map.of("from", "producer-1"),
                "task-1", "nonce-" + UUID.randomUUID(), null);

        var claimedA = repo.inp(tenant, subspace, Map.of("queue", queue), "worker-a", 60L);
        assertThat(claimedA).as("worker-a must claim the single task").isPresent();
        int attemptsBeforeRelease = rawAttempts(id).value1();

        var signals = new AtomicInteger();
        TupleRepository.setTestOnlySignalHook((sigTenant, sigSubspace) -> {
            if (tenant.equals(sigTenant) && subspace.equals(sigSubspace)) {
                signals.incrementAndGet();
            }
        });

        ExecutorService pool = Executors.newFixedThreadPool(1);
        try {
            CompletableFuture<Optional<TupleRepository.ClaimedTuple>> parkedB = CompletableFuture.supplyAsync(
                    () -> repo.in(tenant, subspace, Map.of("queue", queue), "worker-b", 60L, 20L), pool);

            // Give worker-b's background thread time to register + park before releasing.
            Thread.sleep(500);

            repo.release(tenant, claimedA.get().claimId(), "worker-a");

            var resultB = parkedB.get(20, TimeUnit.SECONDS);
            assertThat(resultB).as("worker-b's parked in() must claim the released task").isPresent();
            assertThat(signals.get())
                    .as("release must call signalAll on this (tenant, subspace) at least once")
                    .isGreaterThanOrEqualTo(1);
        } finally {
            TupleRepository.setTestOnlySignalHook(null);
            pool.shutdownNow();
        }

        int attemptsAfterRelease = rawAttempts(id).value1();
        assertThat(attemptsAfterRelease).as("release must not count an attempt")
                .isEqualTo(attemptsBeforeRelease);
        assertThat(transitionsFor(tenant, id))
                .as("exactly claim (worker-a), release, claim (worker-b) -- no expire/nack in between")
                .containsExactly("claim", "release", "claim");
    }

    // ── board ttl_seconds: refused above 7 days, accepted below ──────────────

    @Test
    void boardPostWithTtlAboveSevenDaysIsRefusedTtlTooLong() {
        String tenant = freshTenant("board-ttl-toolong");
        String topic = "topic-" + UUID.randomUUID();
        long aboveSevenDays = 604_800L + 1;

        assertThatExceptionOfType(TtlTooLongException.class)
                .isThrownBy(() -> repo.out(tenant, "board/" + topic, Map.of("topic", topic),
                        Map.of("from", "poster-1"), "hi", "nonce-" + UUID.randomUUID(), aboveSevenDays));
    }

    @Test
    void boardPostWithTtlBelowSevenDaysIsAccepted() {
        String tenant = freshTenant("board-ttl-ok");
        String topic = "topic-" + UUID.randomUUID();
        long oneDay = 86_400L;

        byte[] id = repo.out(tenant, "board/" + topic, Map.of("topic", topic), Map.of("from", "poster-1"),
                "hi", "nonce-" + UUID.randomUUID(), oneDay);
        assertThat(id).isNotNull();
    }

    // ── the 501st live post on a board topic is refused MaxLiveRowsExceeded ──

    /**
     * Drives 500 real {@code out} calls against the SHIPPED {@code board/<topic>}
     * template (max_live_rows: 500), rather than a smaller test-only clone, so
     * this proves the actual production cap rather than a stand-in's. Distinct
     * {@code keys+nonce} identities (a fresh UUID nonce each time), matching how
     * a board's append-only posts are minted in practice.
     */
    @Test
    void the501stPostOnABoardTopicIsRefusedMaxLiveRowsExceeded() {
        String tenant = freshTenant("board-maxrows");
        String topic = "topic-" + UUID.randomUUID();
        String subspace = "board/" + topic;
        for (int i = 0; i < 500; i++) {
            repo.out(tenant, subspace, Map.of("topic", topic), Map.of("from", "poster-1"),
                    "post-" + i, "nonce-" + UUID.randomUUID(), null);
        }

        assertThatExceptionOfType(MaxLiveRowsExceededException.class)
                .isThrownBy(() -> repo.out(tenant, subspace, Map.of("topic", topic), Map.of("from", "poster-1"),
                        "post-501", "nonce-" + UUID.randomUUID(), null))
                .satisfies(e -> {
                    assertThat(e.subspace()).isEqualTo(subspace);
                    assertThat(e.maxLiveRows()).isEqualTo(500L);
                });

        // rdp is capped at NX_TUPLE_READ_MAX (300), so the row COUNT is read via the
        // census (subspaceStats), not a paged rd -- the refused out() must not have
        // grown the live-row total past 500.
        assertThat(repo.subspaceStats(tenant, subspace).total())
                .as("the refused post wrote nothing -- still exactly 500 live rows")
                .isEqualTo(500L);
    }

    // ── a lock holder's ack is refused; release returns it ───────────────────

    @Test
    void lockHolderAckIsRefusedSchemaViolationNamingReleaseAndTheHolderCanStillRelease() throws Exception {
        String tenant = freshTenant("lock-ack-refused");
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock/" + resource;
        repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"), null, null, null);
        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 60L);
        assertThat(claimed).isPresent();
        String claimId = claimed.get().claimId();

        assertThatExceptionOfType(SchemaViolationException.class)
                .isThrownBy(() -> repo.ack(tenant, claimId, "holder-1"))
                .satisfies(e -> assertThat(e.getMessage()).contains("release"));

        // The claim stays live: renew still works, and release still returns it.
        assertThat(repo.renew(tenant, claimId, "holder-1", 60)).isNotNull();
        repo.release(tenant, claimId, "holder-1");

        var reclaimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-2", 60L);
        assertThat(reclaimed).as("released -- a fresh holder can take it").isPresent();
    }

    // ── a lapsed lock lease lets the next in retake with no dead-letter ──────

    @Test
    void aLapsedLockLeaseLetsTheNextInRetakeWithNoDeadLetter() throws Exception {
        String tenant = freshTenant("lock-lapse-retake");
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock/" + resource;
        repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-1"), null, null, null);
        var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-1", 1L);
        assertThat(claimed).isPresent();

        Thread.sleep(1300); // past the 1s lease; the 604,800s retention still holds

        var reclaimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-2", 60L);
        assertThat(reclaimed).as("the lapsed lock is reclaimed, never dead-lettered (max_attempts omitted)")
                .isPresent();
    }

    /**
     * A single lapse (above) would still pass under a small finite {@code
     * max_attempts} (e.g. 3), so it alone cannot distinguish "never dead-letters"
     * from "has not yet hit its cap". This drives the lease-lapse cycle five
     * times, past any small cap, so the property is pinned against a mutation
     * that adds a finite {@code max_attempts} to lock.yaml (see the mutation
     * evidence at the end of this file / the final report).
     */
    @Test
    void fiveConsecutiveLockLeaseLapsesNeverDeadLetter() throws Exception {
        String tenant = freshTenant("lock-lapse-x5");
        String resource = "res-" + UUID.randomUUID();
        String subspace = "lock/" + resource;
        repo.out(tenant, subspace, Map.of("resource", resource), Map.of("from", "holder-0"), null, null, null);

        for (int i = 0; i < 5; i++) {
            var claimed = repo.inp(tenant, subspace, Map.of("resource", resource), "holder-" + i, 1L);
            assertThat(claimed).as("lapse cycle " + i + " must still be claimable, never a dead letter").isPresent();
            Thread.sleep(1300); // past the 1s lease each time; never renewed, never released
        }

        var stillTakeable = repo.inp(tenant, subspace, Map.of("resource", resource), "final-holder", 60L);
        assertThat(stillTakeable).as("after five lapses, still not dead-lettered").isPresent();
    }

    // Mutation evidence for the assertions above is reported in the final
    // hand-back message, not restated here: each mutation is a temporary edit
    // to the shipped board.yaml/lock.yaml, a rerun of the named test showing it
    // fail, then a revert, rather than a permanently-committed alternate test.
}
