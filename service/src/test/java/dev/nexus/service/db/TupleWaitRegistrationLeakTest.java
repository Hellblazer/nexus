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
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicLong;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatExceptionOfType;

/**
 * A pre-existing defect found while landing RDR-211 Step 1 (bead epic nexus-rplay):
 * {@code rd}, {@code in}, and {@code waitAny} each call {@link
 * TupleWaitRegistry#register}/{@link TupleWaitRegistry#registerMulti} BEFORE their
 * first query -- bumping the group's {@code waiters} count -- but on an IMMEDIATE
 * hit (the first, non-blocking query already finds a match) or an exception thrown
 * BY that first query, they returned/threw before ever reaching the {@code try {
 * ... } finally { ...; waiter.release(); }} block that balances the increment. The
 * leaked {@code waiters == 1} then makes {@link TupleWaitRegistry#evictIdleGroups}'s
 * {@code g.waiters == 0} precondition permanently false for that subspace's group,
 * so it can never be reclaimed -- every subspace that ever answers an immediate-hit
 * read with {@code timeout_s > 0} leaks one group for the life of the process.
 *
 * <p>Package-local to {@code dev.nexus.service.db} to reach {@link
 * TupleWaitRegistry}'s package-private {@code groupCount()}/{@code
 * IDLE_EVICT_NANOS} and the test-only injectable-clock constructor, plus {@link
 * TupleRepository}'s package-private test constructor that wires a caller-built
 * {@link TupleWaitRegistry} straight in (added by this same fix) -- the same
 * fixture shape as {@link TupleReleaseTest}/{@link TupleLockFlagTest}, real
 * Postgres via {@link PgContainerHelper}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleWaitRegistrationLeakTest {

    private static final String SVC_ROLE = "svc_tuple_wait_leak_test";
    private static final String SVC_PASS = "svc_tuple_wait_leak_test_pass";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;

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

    // ── fixture ──────────────────────────────────────────────────────────────

    /** A fresh {@link TupleRepository} wired to a fresh {@link TupleWaitRegistry}
     *  built with the given test-injectable clock -- one per test method, so
     *  group-count assertions in one test can never see another test's groups. */
    private TupleRepository repoWithClock(AtomicLong clock) {
        TupleWaitRegistry waitRegistry = new TupleWaitRegistry(4, 16, clock::get);
        return new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                TupleRepository.DEFAULT_TIMEOUT_CAP_SECONDS, TupleRepository.DEFAULT_SUBSPACE_LIST_TIMEOUT_SECONDS,
                waitRegistry);
    }

    /** Advances {@code clock} past the idle-eviction threshold and runs the
     *  opportunistic sweep via an unrelated trigger subspace -- the same
     *  pattern {@code TupleWaitRegistryTest} uses at the registry level. */
    private void advanceClockAndTriggerEviction(AtomicLong clock, TupleWaitRegistry waitRegistry) {
        clock.addAndGet(TupleWaitRegistry.IDLE_EVICT_NANOS * 2);
        TupleWaitRegistry.Waiter trigger = waitRegistry.register("trigger-tenant", "trigger-subspace");
        trigger.release();
    }

    // ── rd: immediate hit ───────────────────────────────────────────────────

    /**
     * {@code rd} with {@code timeout_s > 0} whose first (non-blocking) query
     * already finds the tuple must not leak the subspace's {@link
     * TupleWaitRegistry} group registration. Under the unfixed code, {@code
     * waiter.release()} lives only in the {@code finally} of the park-loop
     * {@code try} block that an immediate hit never enters, so {@code waiters}
     * stays at 1 forever and {@link TupleWaitRegistry#evictIdleGroups}'s {@code
     * g.waiters == 0} precondition can never hold for this subspace.
     */
    @Test
    void rd_immediateHit_releasesTheRegistrationSoTheGroupIsEvictable() {
        AtomicLong clock = new AtomicLong(0L);
        TupleRepository repo = repoWithClock(clock);
        TupleWaitRegistry waitRegistry = repo.testOnlyWaitRegistry();

        String tenant = "tuple-wait-leak-rd-" + UUID.randomUUID();
        String to = "agent-rd-leak-" + UUID.randomUUID();
        String subspace = "mailbox/" + to;
        repo.out(tenant, subspace, Map.of("to", to), Map.of("from", "sender"), "body", "nonce-rd-leak", null);

        List<TupleRepository.TupleRow> found = repo.rd(tenant, subspace, Map.of("to", to), 1, null, 5L);
        assertThat(found).as("the immediate (non-blocking) query must already find the seeded tuple")
                .hasSize(1);

        advanceClockAndTriggerEviction(clock, waitRegistry);

        assertThat(waitRegistry.groupCount())
                .as("an immediate-hit rd must release its registration -- only the trigger group survives")
                .isEqualTo(1);
    }

    // ── rd: exception from the first query ──────────────────────────────────

    /**
     * {@code rd}'s first query validates the subspace via {@code
     * resolveOrThrow}, which runs AFTER {@code register} -- an unresolvable
     * subspace throws {@link UnknownSubspaceException} from inside that first
     * query, exactly the "exception thrown by that first query" case the
     * register-then-try restructuring must also cover. Under the unfixed code
     * this exception propagates straight out of {@code rd}, past the {@code
     * waiter.release()} call that only exists inside the never-reached
     * park-loop's {@code finally}.
     */
    @Test
    void rd_firstQueryThrows_stillReleasesTheRegistration() {
        AtomicLong clock = new AtomicLong(0L);
        TupleRepository repo = repoWithClock(clock);
        TupleWaitRegistry waitRegistry = repo.testOnlyWaitRegistry();

        String tenant = "tuple-wait-leak-rd-throw-" + UUID.randomUUID();
        String subspace = "no-such-template-subspace-" + UUID.randomUUID();

        assertThatExceptionOfType(UnknownSubspaceException.class)
                .isThrownBy(() -> repo.rd(tenant, subspace, Map.of(), 1, null, 5L));

        advanceClockAndTriggerEviction(clock, waitRegistry);

        assertThat(waitRegistry.groupCount())
                .as("an rd whose first query threw must still release its registration")
                .isEqualTo(1);
    }

    // ── in: immediate hit ────────────────────────────────────────────────────

    /** The {@code in} counterpart to {@link #rd_immediateHit_releasesTheRegistrationSoTheGroupIsEvictable}:
     *  a claim that succeeds on the first (non-blocking) {@code claimOnce} must
     *  release its registration the same way. */
    @Test
    void in_immediateHit_releasesTheRegistrationSoTheGroupIsEvictable() {
        AtomicLong clock = new AtomicLong(0L);
        TupleRepository repo = repoWithClock(clock);
        TupleWaitRegistry waitRegistry = repo.testOnlyWaitRegistry();

        String tenant = "tuple-wait-leak-in-" + UUID.randomUUID();
        String to = "agent-in-leak-" + UUID.randomUUID();
        String subspace = "mailbox/" + to;
        repo.out(tenant, subspace, Map.of("to", to), Map.of("from", "sender"), "body", "nonce-in-leak", null);

        var claimed = repo.in(tenant, subspace, Map.of("to", to), "worker-1", 60L, 5L);
        assertThat(claimed).as("the immediate (non-blocking) claim must already take the seeded tuple")
                .isPresent();

        advanceClockAndTriggerEviction(clock, waitRegistry);

        assertThat(waitRegistry.groupCount())
                .as("an immediate-hit in must release its registration -- only the trigger group survives")
                .isEqualTo(1);
    }

    // ── in: exception from the first query ───────────────────────────────────

    /** The {@code in} counterpart to {@link #rd_firstQueryThrows_stillReleasesTheRegistration}:
     *  {@code claimOnce}'s own {@code resolveOrThrow} call runs after {@code
     *  register}, so an unresolvable subspace throws from inside the first
     *  claim attempt. */
    @Test
    void in_firstQueryThrows_stillReleasesTheRegistration() {
        AtomicLong clock = new AtomicLong(0L);
        TupleRepository repo = repoWithClock(clock);
        TupleWaitRegistry waitRegistry = repo.testOnlyWaitRegistry();

        String tenant = "tuple-wait-leak-in-throw-" + UUID.randomUUID();
        String subspace = "no-such-template-subspace-" + UUID.randomUUID();

        assertThatExceptionOfType(UnknownSubspaceException.class)
                .isThrownBy(() -> repo.in(tenant, subspace, Map.of("to", "x"), "worker-1", 60L, 5L));

        advanceClockAndTriggerEviction(clock, waitRegistry);

        assertThat(waitRegistry.groupCount())
                .as("an in whose first claim attempt threw must still release its registration")
                .isEqualTo(1);
    }

    // ── waitAny: immediate hit ───────────────────────────────────────────────

    /** The {@code waitAny} counterpart, via {@link TupleWaitRegistry.MultiWaiter}:
     *  a multi-subspace wait whose first {@code queryEachOnce} pass already
     *  finds a match must release EVERY subspace group {@code registerMulti}
     *  registered across, not just the one that matched. */
    @Test
    void waitAny_immediateHit_releasesEveryRegisteredSubspaceGroup() {
        AtomicLong clock = new AtomicLong(0L);
        TupleRepository repo = repoWithClock(clock);
        TupleWaitRegistry waitRegistry = repo.testOnlyWaitRegistry();

        String tenant = "tuple-wait-leak-waitany-" + UUID.randomUUID();
        String to = "agent-waitany-leak-" + UUID.randomUUID();
        String hitSubspace = "mailbox/" + to;
        String otherTo = "agent-waitany-leak-other-" + UUID.randomUUID();
        String otherSubspace = "mailbox/" + otherTo;
        repo.out(tenant, hitSubspace, Map.of("to", to), Map.of("from", "sender"), "body", "nonce-waitany-leak", null);

        List<TupleRepository.WaitResult> found = repo.waitAny(tenant, List.of(
                new TupleRepository.WaitSpec(hitSubspace, Map.of("to", to), 1, null),
                new TupleRepository.WaitSpec(otherSubspace, Map.of("to", otherTo), 1, null)), 5L);
        assertThat(found).as("the immediate (non-blocking) per-subspace pass must find the seeded tuple")
                .hasSize(1);

        advanceClockAndTriggerEviction(clock, waitRegistry);

        assertThat(waitRegistry.groupCount())
                .as("an immediate-hit waitAny must release BOTH registered subspace groups "
                    + "(the matched one and the one that never matched) -- only the trigger group survives")
                .isEqualTo(1);
    }

    // ── waitAny: no post-register exception path exists ──────────────────────

    // waitAny validates every spec's subspace and pattern (checkFieldSize,
    // resolveOrThrow, checkPatternSizes) BEFORE calling registerMulti at all --
    // see its own javadoc ("Validate EVERY subspace and pattern BEFORE anything
    // registers or parks"). A bad request therefore never reaches registerMulti
    // in the first place, so there is no refusal that fires AFTER registration
    // for this method the way there is for rd/in's resolveOrThrow-inside-the-
    // first-query shape; per this bead's own instructions, no exception-path
    // test is forced here for waitAny.
}
