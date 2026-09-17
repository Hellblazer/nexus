// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.ParkCapExceededException;
import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.db.UnknownSubspaceException;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-211 Phase 1 Step 1 (bead nexus-rplay.4) — {@code TupleRepository.waitAny}
 * integration tests, modeled directly on {@code TupleRepositoryTest}'s own {@code rd}
 * wake/park-cap tests (same hermetic embedded-Postgres harness, same fixture shape).
 * A separate file rather than additions to {@code TupleRepositoryTest} because {@code
 * waitAny} needs its own extra template ({@code wait-topic/<topic>}, a read-only
 * broadcast-shaped subspace standing in for the "board" shape this bead's design
 * uses informally — the actual board/lock templates are a sibling bead's own
 * machinery, not this one's) alongside the bundled {@code mailbox/<address>}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleWaitTest {

    private static final String TENANT_A = "tuple-wait-tenant-a";
    private static final String SVC_ROLE = "svc_tuple_wait_test";
    private static final String SVC_PASS = "svc_tuple_wait_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
    TupleRepository repo;

    @BeforeAll
    void startAll(@org.junit.jupiter.api.io.TempDir java.nio.file.Path extraTemplateDir) throws Exception {
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
        // A read-only (take.enabled=false), keys+nonce broadcast-shaped subspace --
        // multiple distinct posts to the SAME topic, exactly what a "board" needs and
        // what neither bundled template (mailbox is take-enabled, ledger's keys are
        // agent_id/kind, not a single topic) can stand in for directly.
        java.nio.file.Files.writeString(extraTemplateDir.resolve("wait-topic.yaml"), """
                name: wait-topic/<topic>
                keys:
                  - topic
                dimensions:
                  from:
                    type: string
                id_from: keys+nonce
                take:
                  enabled: false
                retention_seconds: 3600
                """, java.nio.charset.StandardCharsets.UTF_8);
        registry = TemplateRegistry.loadAtBoot(extraTemplateDir.toString(), null,
                NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        repo = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                /* timeoutCapSeconds */ 10, /* parkCapPerClaimant */ 4, /* parkCapGlobal */ 16);
    }

    // ── wake, from any registered subspace, at one park slot ────────────────

    /**
     * The mechanism's central end-to-end claim: a {@code wait} across three
     * subspaces (a mailbox and two broadcast topics) wakes on a write to ANY one of
     * them, via the signal path (not the registry's 1-second timer fallback -- same
     * latency-bound proof {@code TupleRepositoryTest#rd_withTimeoutS_wakesOnAnotherClientOut}
     * uses), and costs exactly ONE global park slot regardless of subspace count
     * (read through the .7 park report: one slot while parked, zero after) -- the
     * same one-slot-per-call claim {@code TupleWaitRegistryMultiTest
     * #registerMultiAcrossThreeSubspacesConsumesOneParkSlotNotThree} pins at the
     * registry level, now proven through the full repository call.
     */
    @Test
    void waitAny_onThreeSubspaces_wakesOnWriteToAnyOne_viaSignal_andTakesExactlyOneParkSlot()
            throws Exception {
        String to = "wait-wake-mailbox-" + UUID.randomUUID();
        String topicA = "wait-wake-topic-a-" + UUID.randomUUID();
        String topicB = "wait-wake-topic-b-" + UUID.randomUUID();
        List<TupleRepository.WaitSpec> specs = List.of(
                new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null),
                new TupleRepository.WaitSpec("wait-topic/" + topicA, Map.of("topic", topicA), 10, null),
                new TupleRepository.WaitSpec("wait-topic/" + topicB, Map.of("topic", topicB), 10, null));

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.WaitResult>> parked = pool.submit(() ->
                    repo.waitAny(TENANT_A, specs, 8));
            Thread.sleep(600); // let it register, probe, and enter the park loop

            assertThat(repo.parkStats().globalInUse())
                    .as("RDR-211 Phase 1 Step 1: wait parks with no claimant -- one global "
                            + "slot regardless of how many subspaces it registered")
                    .isEqualTo(1);

            long beforeOut = System.nanoTime();
            repo.out(TENANT_A, "wait-topic/" + topicB, Map.of("topic", topicB), Map.of(),
                    "post", "nonce-wake-b", null);

            List<TupleRepository.WaitResult> result = parked.get(6, TimeUnit.SECONDS);
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - beforeOut);

            assertThat(result).hasSize(1);
            assertThat(result.get(0).subspace()).isEqualTo("wait-topic/" + topicB);
            assertThat(result.get(0).tuples()).hasSize(1);
            assertThat(elapsedMs)
                    .as("woke on the signal, not the registry's 1-second timer fallback")
                    .isLessThan(300);

            assertThat(repo.parkStats().globalInUse())
                    .as("the slot is released once the call returns")
                    .isZero();
        } finally {
            pool.shutdownNow();
        }
    }

    // ── per-subspace cursors across a polling loop ───────────────────────────

    /**
     * A client's real usage shape: probe once (delivers topic A's already-posted
     * tuple), advance topic A's cursor, then park again across the SAME four
     * subspaces -- a repeat post to topic A must not redeliver (cursor already past
     * it), while a write to an entirely different registered subspace (a mailbox)
     * still wakes the same parked call. Exercises two mailboxes and two
     * broadcast-shaped topics together, per the bead's own test-plan enumeration.
     */
    @Test
    void waitAny_fourSubspaces_deliversAPostOnceWithCursorAdvance_thenWakesOnAMailboxWrite()
            throws Exception {
        String to1 = "wait-cursor-mailbox-1-" + UUID.randomUUID();
        String to2 = "wait-cursor-mailbox-2-" + UUID.randomUUID();
        String topicA = "wait-cursor-topic-a-" + UUID.randomUUID();
        String topicB = "wait-cursor-topic-b-" + UUID.randomUUID();

        // Seeded BEFORE the first wait call -- a non-blocking probe (timeout_s=0)
        // must find and deliver it exactly once.
        repo.out(TENANT_A, "wait-topic/" + topicA, Map.of("topic", topicA), Map.of(),
                "post-1", "nonce-a-1", null);

        List<TupleRepository.WaitSpec> initial = List.of(
                new TupleRepository.WaitSpec("mailbox/" + to1, Map.of("to", to1), 10, null),
                new TupleRepository.WaitSpec("mailbox/" + to2, Map.of("to", to2), 10, null),
                new TupleRepository.WaitSpec("wait-topic/" + topicA, Map.of("topic", topicA), 10, null),
                new TupleRepository.WaitSpec("wait-topic/" + topicB, Map.of("topic", topicB), 10, null));

        List<TupleRepository.WaitResult> first = repo.waitAny(TENANT_A, initial, 0);
        assertThat(first).hasSize(1);
        assertThat(first.get(0).subspace()).isEqualTo("wait-topic/" + topicA);
        assertThat(first.get(0).tuples()).hasSize(1);
        var seeded = first.get(0).tuples().get(0);
        var advancedCursor = new TupleRepository.ReadCursor(seeded.createdAt(), seeded.id());

        // The client's own cursor-advance step: topic A's spec now carries the cursor
        // past the post it already delivered; the other three specs are unchanged.
        List<TupleRepository.WaitSpec> advanced = List.of(
                initial.get(0), initial.get(1),
                new TupleRepository.WaitSpec("wait-topic/" + topicA, Map.of("topic", topicA), 10, advancedCursor),
                initial.get(3));

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.WaitResult>> parked = pool.submit(() ->
                    repo.waitAny(TENANT_A, advanced, 8));
            Thread.sleep(600);

            repo.out(TENANT_A, "mailbox/" + to2, Map.of("to", to2), Map.of("from", "waker"),
                    "hello", "nonce-wake-mailbox2", null);

            List<TupleRepository.WaitResult> woken = parked.get(6, TimeUnit.SECONDS);
            assertThat(woken)
                    .as("only the mailbox that actually received a write -- topic A's already-"
                            + "delivered post must not reappear past its advanced cursor")
                    .hasSize(1);
            assertThat(woken.get(0).subspace()).isEqualTo("mailbox/" + to2);
        } finally {
            pool.shutdownNow();
        }
    }

    // ── validation before any slot is taken ──────────────────────────────────

    /**
     * The subspace-count cap ({@link TupleRepository#MAX_WAIT_SUBSPACES}, 34) is
     * refused BEFORE registration or park-slot acquisition -- the global in-use
     * gauge must read zero both before and after the refused call.
     */
    @Test
    void waitAny_aboveMaxWaitSubspaces_isRefusedBeforeAnySlotIsTaken() {
        List<TupleRepository.WaitSpec> tooMany = new ArrayList<>();
        for (int i = 0; i < TupleRepository.MAX_WAIT_SUBSPACES + 1; i++) {
            String to = "wait-cap-mailbox-" + i + "-" + UUID.randomUUID();
            tooMany.add(new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 1, null));
        }
        assertThat(repo.parkStats().globalInUse()).isZero();

        assertThatThrownBy(() -> repo.waitAny(TENANT_A, tooMany, 5))
                .isInstanceOf(SchemaViolationException.class);

        assertThat(repo.parkStats().globalInUse())
                .as("a refused wait must never have taken a park slot")
                .isZero();
    }

    /**
     * An unknown subspace anywhere in the list is refused before any of the OTHER,
     * valid subspaces register or park -- the upfront validation pass {@code
     * TupleRepository#waitAny}'s own javadoc describes, not a per-subspace failure
     * discovered mid-loop after some subspaces already registered.
     */
    @Test
    void waitAny_unknownSubspaceAmongValidOnes_isRefusedBeforeAnySlotIsTaken() {
        String to = "wait-badsubspace-mailbox-" + UUID.randomUUID();
        List<TupleRepository.WaitSpec> specs = List.of(
                new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 1, null),
                new TupleRepository.WaitSpec("not-a-registered-template/x", Map.of(), 1, null));

        assertThatThrownBy(() -> repo.waitAny(TENANT_A, specs, 5))
                .isInstanceOf(UnknownSubspaceException.class);

        assertThat(repo.parkStats().globalInUse())
                .as("no slot taken -- the bad subspace was caught before registerMulti ran")
                .isZero();
    }

    /** {@code timeout_s=0} probes each subspace exactly once and returns fast, never
     *  parking -- the multi-subspace counterpart to {@code rdp}. */
    @Test
    void waitAny_timeoutZero_probesOnceAndReturnsFastWithNoMatch() throws Exception {
        String to = "wait-probe-mailbox-" + UUID.randomUUID();
        String topic = "wait-probe-topic-" + UUID.randomUUID();
        List<TupleRepository.WaitSpec> specs = List.of(
                new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 1, null),
                new TupleRepository.WaitSpec("wait-topic/" + topic, Map.of("topic", topic), 1, null));

        long start = System.nanoTime();
        List<TupleRepository.WaitResult> result = repo.waitAny(TENANT_A, specs, 0);
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(result).isEmpty();
        assertThat(elapsedMs)
                .as("a probe must never ride out any part of the 1s registry timer")
                .isLessThan(500);
        assertThat(repo.parkStats().globalInUse()).isZero();
    }

    // ── park-cap exceeded still applies to wait ───────────────────────────────

    /**
     * {@code wait} parks with no claimant, exactly like {@code rd} -- a global park
     * cap exceeded while a {@code wait} call is parked refuses a SECOND parking
     * call the same way {@code TupleRepositoryTest#rd_globalParkCapExceeded} proves
     * for {@code rd}.
     */
    @Test
    void waitAny_globalParkCapExceeded() throws Exception {
        TupleRepository capped = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                10, /* parkCapPerClaimant */ 4, /* parkCapGlobal */ 1);
        String topic = "wait-globalcap-topic-" + UUID.randomUUID();
        List<TupleRepository.WaitSpec> specs = List.of(
                new TupleRepository.WaitSpec("wait-topic/" + topic, Map.of("topic", topic), 1, null));
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<List<TupleRepository.WaitResult>> holder = pool.submit(() ->
                    capped.waitAny(TENANT_A, specs, 3));
            Thread.sleep(500);

            assertThatThrownBy(() -> capped.waitAny(TENANT_A, specs, 3))
                    .isInstanceOf(ParkCapExceededException.class);

            holder.get(6, TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
    }
}
