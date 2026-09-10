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
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-205 Phase 1 Step 6 (bead nexus-em75s.6) — the CA 1 regression pin. Reproduces
 * {@code nexus_rdr/205-research-3}'s measurement 1 and 3 (a 50ms {@code pg_sleep}
 * injected between the claim {@code SELECT ... FOR NO KEY UPDATE SKIP LOCKED} and
 * its {@code UPDATE}, ten concurrent claimants racing a shared candidate set) but
 * against the real {@link TupleRepository}, not the spike's hand-rolled PL/pgSQL —
 * so THIS is the test that fails if the claim statement's lock shape is ever
 * weakened, not a one-off measurement. Package-local to {@code dev.nexus.service.db}
 * specifically so it can reach {@link TupleRepository#TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY},
 * a package-private seam that is a no-op {@code Runnable} on every production path.
 *
 * <p><b>Weakening experiment (performed once while authoring this test, then
 * reverted):</b> with {@code .forNoKeyUpdate().skipLocked()} removed from
 * {@link TupleRepository}'s claim {@code SELECT} (an unlocked plain select), this
 * test's {@code tenConcurrentClaimants_...} case failed exactly as expected: several
 * runs reported {@code claimRows} well above {@code ROWS} (the same row selected and
 * updated by more than one concurrent transaction before either committed — no
 * SKIP LOCKED to steer later claimants to a different candidate, and no row lock to
 * block the second UPDATE until the first committed) and {@code distinctTupleIds}
 * dropped below {@code ROWS} (multiple {@code claim} log rows sharing one
 * {@code tuple_id}, each with its own distinct {@code claim_id} — the exact
 * double-claim CA 1 exists to rule out). The injected delay is what made the window
 * wide enough to observe reliably; without it the race was narrow enough that a
 * weakened lock still usually produced a clean run, which would have made this a
 * flaky, not a load-bearing, regression pin. Restored immediately after (this file's
 * committed diff never carried the weakening).
 *
 * <p><b>Vacuity guard:</b> {@code maxConcurrentInHook} tracks the peak number of
 * claim transactions simultaneously inside their post-select pre-update window
 * (instrumented inside the delay hook itself, not inferred from timing). A run that
 * never actually raced — every claim serialized through the hook one at a time —
 * asserts a hard failure rather than a silent pass; see the CLAUDE.md vacuous-gate
 * doctrine.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleClaimContentionTest {

    private static final String TENANT = "tuple-tenant-contend";
    private static final String SVC_ROLE = "svc_tuple_contend_test";
    private static final String SVC_PASS = "svc_tuple_contend_test_pass";

    private static final int ROWS = 30;
    private static final int WORKERS = 10;
    private static final long DELAY_MS = 40;
    private static final int MAX_ATTEMPTS_PER_WORKER = 500;

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
        cfg.setMaximumPoolSize(WORKERS + 5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        repo = new TupleRepository(tenantScope, registry);
    }

    @AfterAll
    void stopAll() {
        // Belt-and-braces: the test body's own finally already restores this, but a
        // JVM-fork-shared static field must never leak a non-no-op hook into whatever
        // test class runs next in the same process.
        TupleRepository.TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY = () -> {
        };
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    /**
     * Ten concurrent claimants racing {@code ROWS} available rows in one shared
     * mailbox, with a test-only delay forced between each claim's {@code SELECT}
     * and its {@code UPDATE} — widening the race window RDR-110 CA#2 names. Asserts
     * the May MVV audit shape: {@code ROWS} consumed after ack, zero claimed, zero
     * available, exactly {@code ROWS} claim and {@code ROWS} ack log rows, zero
     * expire rows, no tuple ever claimed twice.
     */
    @Test
    void tenConcurrentClaimants_onNRows_exactlyOneHolderPerRow_zeroDoubleClaims() throws Exception {
        String to = "agent-contend-" + UUID.randomUUID();
        for (int i = 0; i < ROWS; i++) {
            repo.out(TENANT, "mailbox/" + to, Map.of("to", to),
                    Map.of("from", "sender-contend"), "body", "nonce-contend-" + i, null);
        }

        AtomicInteger inHook = new AtomicInteger();
        AtomicInteger maxConcurrentInHook = new AtomicInteger();
        TupleRepository.TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY = () -> {
            int cur = inHook.incrementAndGet();
            maxConcurrentInHook.updateAndGet(prev -> Math.max(prev, cur));
            try {
                Thread.sleep(DELAY_MS);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            } finally {
                inHook.decrementAndGet();
            }
        };

        int totalClaimedByWorkers;
        try {
            AtomicInteger consumedCount = new AtomicInteger();
            ExecutorService pool = Executors.newFixedThreadPool(WORKERS);
            CyclicBarrier barrier = new CyclicBarrier(WORKERS);
            List<Future<Integer>> futures = new ArrayList<>();
            for (int w = 0; w < WORKERS; w++) {
                String claimant = "contender-" + w;
                futures.add(pool.submit(() -> {
                    barrier.await(20, TimeUnit.SECONDS);
                    int myClaims = 0;
                    int attempts = 0;
                    while (consumedCount.get() < ROWS && attempts < MAX_ATTEMPTS_PER_WORKER) {
                        attempts++;
                        var claimed = repo.inp(TENANT, "mailbox/" + to, Map.of("to", to), claimant, 60);
                        if (claimed.isPresent()) {
                            repo.ack(TENANT, claimed.get().claimId(), claimant);
                            consumedCount.incrementAndGet();
                            myClaims++;
                        } else if (consumedCount.get() < ROWS) {
                            // Lost this round to SKIP LOCKED or the queue was briefly
                            // empty of unlocked candidates -- brief backoff, not a sleep
                            // standing in for a signal (the loop condition, not this
                            // sleep, decides when to stop).
                            Thread.sleep(5);
                        }
                    }
                    return myClaims;
                }));
            }
            pool.shutdown();
            assertThat(pool.awaitTermination(60, TimeUnit.SECONDS))
                    .as("all ten claimant workers must finish inside the bound")
                    .isTrue();

            int total = 0;
            for (var f : futures) {
                total += f.get();
            }
            totalClaimedByWorkers = total;
        } finally {
            TupleRepository.TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY = () -> {
            };
        }

        assertThat(totalClaimedByWorkers)
                .as("every one of the %d rows claimed exactly once across all ten workers", ROWS)
                .isEqualTo(ROWS);

        // Vacuity guard (nexus-moht0 doctrine): the harness must have actually
        // contended -- multiple claim transactions concurrently inside their
        // post-select pre-update window, not one at a time by accident.
        assertThat(maxConcurrentInHook.get())
                .as("harness must have actually contended: a run that never raced proves nothing")
                .isGreaterThanOrEqualTo(2);

        var stats = repo.subspaceStats(TENANT, "mailbox/" + to);
        assertThat(stats.consumed()).as("N consumed after ack").isEqualTo(ROWS);
        assertThat(stats.claimed()).as("zero still claimed").isEqualTo(0);
        assertThat(stats.available()).as("zero still available").isEqualTo(0);
        assertThat(stats.dead()).as("zero dead-lettered (leases never lapsed)").isEqualTo(0);

        tenantScope.withTenant(TENANT, ctx -> {
            var claimRows = ctx.selectFrom(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT)
                            .and(TUPLE_CLAIM_LOG.SUBSPACE.eq("mailbox/" + to))
                            .and(TUPLE_CLAIM_LOG.TRANSITION.eq("claim")))
                    .fetch();
            var ackRows = ctx.selectFrom(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT)
                            .and(TUPLE_CLAIM_LOG.SUBSPACE.eq("mailbox/" + to))
                            .and(TUPLE_CLAIM_LOG.TRANSITION.eq("ack")))
                    .fetch();
            var expireRows = ctx.selectFrom(TUPLE_CLAIM_LOG)
                    .where(TUPLE_CLAIM_LOG.TENANT_ID.eq(TENANT)
                            .and(TUPLE_CLAIM_LOG.SUBSPACE.eq("mailbox/" + to))
                            .and(TUPLE_CLAIM_LOG.TRANSITION.eq("expire")))
                    .fetch();

            assertThat(claimRows).as("exactly N claim log rows -- one per claim").hasSize(ROWS);
            assertThat(ackRows).as("exactly N ack log rows -- one per ack").hasSize(ROWS);
            assertThat(expireRows).as("zero expire log rows -- no lease ever lapsed").isEmpty();

            long distinctTupleIds = claimRows.stream()
                    .map(r -> java.util.Base64.getEncoder().encodeToString(r.getTupleId()))
                    .distinct()
                    .count();
            assertThat(distinctTupleIds)
                    .as("no tuple with two claim ids -- zero double-claims, each of the N tuples claimed exactly once")
                    .isEqualTo(ROWS);
            return null;
        });
    }
}
