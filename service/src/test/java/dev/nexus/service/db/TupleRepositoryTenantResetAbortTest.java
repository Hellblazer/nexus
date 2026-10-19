// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.NexusService;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-0bl77 (rtg20 phase A, fix round): the abort checks in {@link TupleRepository}'s three park loops
 * ({@code rd}, {@code in}, {@code waitAny}). A reset aborts the tenant's wait groups; a parked waiter wakes and must
 * throw {@link TenantResetException} WITHOUT querying again, because after the reset the tenant's rows belong to the
 * next test and a re-query would hand a dead test's waiter the next test's tuple (for {@code in}, claim it).
 *
 * <p>Making the check observable. The tuple is written by a SECOND {@link TupleRepository} over the same database,
 * so its {@code out} signals that repository's wait registry and never wakes the waiter. The waiter then holds a
 * parked wait over a row that exists, and the ONLY thing that wakes it is the abort. Without the check it wakes,
 * re-queries and returns the row (or claims it); with the check it throws and the row is untouched. The write is
 * made only after the waiter is parked in its timed wait, then the abort follows at once, so the waiter's own
 * one-second timer cannot be the thing that finds the row.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleRepositoryTenantResetAbortTest {

    private static final long PARK_SECONDS = 8;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
    /** Writes the tuples; its registry is not the waiter's, so a write never signals the parked wait. */
    TupleRepository writer;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        registry = TemplateRegistry.loadAtBoot(NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        writer = repo();
        // Warm the write path (template resolution, tenant bookkeeping) so the timed write below is short.
        writer.out("warm-" + UUID.randomUUID(), "mailbox/warm", Map.of("to", "warm"), Map.of("from", "w"), "b",
            "nonce-warm", null);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private TupleRepository repo() {
        return new TupleRepository(tenantScope, registry, TupleRepository.DEFAULT_READ_MAX,
            TupleRepository.DEFAULT_CLAIM_PASSES, /* timeoutCapSeconds */ 10, /* parkCapPerClaimant */ 4,
            /* parkCapGlobal */ 16);
    }

    /** One parked wait on its own thread: the outcome is the value it returned or the exception it threw. */
    private static final class Parked {
        final Thread thread;
        final AtomicReference<Object> value = new AtomicReference<>();
        final AtomicReference<Throwable> error = new AtomicReference<>();

        Parked(Supplier<Object> wait) {
            thread = new Thread(() -> {
                try {
                    value.set(wait.get());
                } catch (Throwable t) {
                    error.set(t);
                }
            }, "tenant-reset-parked-wait");
            thread.setDaemon(true);
        }
    }

    /** Starts {@code wait} and returns once it is inside its timed park (parked, not merely about to be). */
    private Parked park(TupleRepository waiter, Supplier<Object> wait) throws InterruptedException {
        Parked p = new Parked(wait);
        p.thread.start();
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (waiter.parkStats().globalInUse() < 1 && System.nanoTime() < deadline) {
            Thread.sleep(2);
        }
        assertThat(waiter.parkStats().globalInUse()).as("the wait took its park slot").isEqualTo(1);
        while (p.thread.getState() != Thread.State.TIMED_WAITING && System.nanoTime() < deadline) {
            Thread.sleep(1);
        }
        assertThat(p.thread.getState()).as("and is inside its timed park").isEqualTo(Thread.State.TIMED_WAITING);
        return p;
    }

    private void plant(String tenant, String to) {
        writer.out(tenant, "mailbox/" + to, Map.of("to", to), Map.of("from", "next-test"), "the next test's tuple",
            "nonce-" + UUID.randomUUID(), null);
    }

    private static void awaitDone(Parked p) throws InterruptedException {
        p.thread.join(TimeUnit.SECONDS.toMillis(5));
        assertThat(p.thread.isAlive()).as("the aborted wait returned instead of riding out its timeout").isFalse();
    }

    // ── in ────────────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aParkedInIsAbortedWithTenantResetAndDoesNotClaimTheNextTestsTuple() throws Exception {
        String tenant = "reset-in-" + UUID.randomUUID();
        String to = "agent-in-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        Parked p = park(waiter, () ->
            waiter.in(tenant, "mailbox/" + to, Map.of("to", to), "dead-test-worker", 60L, PARK_SECONDS));

        plant(tenant, to);   // exists now; nothing signalled the waiter
        waiter.abortTenantWaiters(tenant, Duration.ofSeconds(3));
        awaitDone(p);

        assertThat(p.error.get()).as("the dead test's in threw, it did not return a claim")
            .isInstanceOf(TenantResetException.class);
        assertThat(p.value.get()).as("and returned nothing").isNull();
        assertThat(writer.inp(tenant, "mailbox/" + to, Map.of("to", to), "next-test-worker", 60L))
            .as("the tuple written after the reset was not consumed by the dead waiter").isPresent();
    }

    // ── waitAny ───────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aParkedWaitAnyIsAbortedWithTenantResetAndReturnsNothing() throws Exception {
        String tenant = "reset-wait-" + UUID.randomUUID();
        String to = "agent-wait-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        var spec = new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null);
        Parked p = park(waiter, () -> waiter.waitAny(tenant, List.of(spec), PARK_SECONDS));

        plant(tenant, to);
        waiter.abortTenantWaiters(tenant, Duration.ofSeconds(3));
        awaitDone(p);

        assertThat(p.error.get()).as("the dead test's wait threw, it did not report the next test's tuple")
            .isInstanceOf(TenantResetException.class);
        assertThat(p.value.get()).isNull();
        assertThat(writer.rd(tenant, "mailbox/" + to, Map.of("to", to), 10, null, 0))
            .as("the tuple is still there for the next test").hasSize(1);
    }

    // ── rd ────────────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aParkedRdIsAbortedWithTenantResetAndReturnsNothing() throws Exception {
        String tenant = "reset-rd-" + UUID.randomUUID();
        String to = "agent-rd-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        Parked p = park(waiter, () -> waiter.rd(tenant, "mailbox/" + to, Map.of("to", to), 10, null, PARK_SECONDS));

        plant(tenant, to);
        waiter.abortTenantWaiters(tenant, Duration.ofSeconds(3));
        awaitDone(p);

        assertThat(p.error.get()).isInstanceOf(TenantResetException.class);
        assertThat(p.value.get()).isNull();
    }

    // ── an abort that lands between register and the first query, or at the deadline (nexus-vr8wk) ──────
    //
    // Both windows sit inside the park loop's own time, not in a wake, so the post-wake check above never sees
    // them. Each is made deterministic with a hook on the waiting thread that plants the next test's tuple (through
    // the second repository, so nothing signals the waiter) and aborts the tenant at exactly that point. Without
    // the check the wait queries, and returns or claims the row; with it the wait throws and the row is untouched.

    /** Plants the row and aborts {@code tenant}'s waits from the waiting thread (bound 0: the caller is the waiter). */
    private void plantAndAbort(TupleRepository waiter, String tenant, String to) {
        plant(tenant, to);
        waiter.testOnlyWaitRegistry().abortTenant(tenant, 0);
    }

    /** Runs {@code action} once per call of the hook, at most once overall. */
    private static Runnable once(Runnable action) {
        var fired = new java.util.concurrent.atomic.AtomicBoolean();
        return () -> {
            if (fired.compareAndSet(false, true)) {
                action.run();
            }
        };
    }

    /** At the park loop's top on its first pass: the next test's row appears, the tenant is aborted, and the wait's
     *  one-second deadline passes, all before the loop's own checks run. */
    private Runnable abortAtTheDeadline(TupleRepository waiter, String tenant, String to) {
        return once(() -> {
            plantAndAbort(waiter, tenant, to);
            try {
                Thread.sleep(1_300);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        });
    }

    @Test
    void anRdAbortedBetweenRegisterAndItsFirstQueryThrowsWithoutQuerying() {
        String tenant = "reset-rd-first-" + UUID.randomUUID();
        String to = "agent-rd-first-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        waiter.testOnlyAfterRegister = once(() -> plantAndAbort(waiter, tenant, to));

        assertThatThrownBy(() ->
            waiter.rd(tenant, "mailbox/" + to, Map.of("to", to), 10, null, PARK_SECONDS))
            .isInstanceOf(TenantResetException.class);
    }

    @Test
    void anInAbortedBetweenRegisterAndItsFirstClaimThrowsAndLeavesTheTuple() {
        String tenant = "reset-in-first-" + UUID.randomUUID();
        String to = "agent-in-first-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        waiter.testOnlyAfterRegister = once(() -> plantAndAbort(waiter, tenant, to));

        assertThatThrownBy(() ->
            waiter.in(tenant, "mailbox/" + to, Map.of("to", to), "dead-test-worker", 60L, PARK_SECONDS))
            .isInstanceOf(TenantResetException.class);
        assertThat(writer.inp(tenant, "mailbox/" + to, Map.of("to", to), "next-test-worker", 60L))
            .as("the next test's tuple was not claimed by the dead waiter").isPresent();
    }

    @Test
    void aWaitAnyAbortedBetweenRegisterAndItsFirstQueryThrowsWithoutQuerying() {
        String tenant = "reset-wait-first-" + UUID.randomUUID();
        String to = "agent-wait-first-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        var spec = new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null);
        waiter.testOnlyAfterRegister = once(() -> plantAndAbort(waiter, tenant, to));

        assertThatThrownBy(() -> waiter.waitAny(tenant, List.of(spec), PARK_SECONDS))
            .isInstanceOf(TenantResetException.class);
    }

    @Test
    void anRdWhoseDeadlinePassesWithTheAbortThrowsInsteadOfRunningItsFinalQuery() {
        String tenant = "reset-rd-deadline-" + UUID.randomUUID();
        String to = "agent-rd-deadline-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        waiter.testOnlyAtParkLoopTop = abortAtTheDeadline(waiter, tenant, to);

        assertThatThrownBy(() -> waiter.rd(tenant, "mailbox/" + to, Map.of("to", to), 10, null, 1))
            .isInstanceOf(TenantResetException.class);
    }

    @Test
    void anInWhoseDeadlinePassesWithTheAbortThrowsInsteadOfClaiming() {
        String tenant = "reset-in-deadline-" + UUID.randomUUID();
        String to = "agent-in-deadline-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        waiter.testOnlyAtParkLoopTop = abortAtTheDeadline(waiter, tenant, to);

        assertThatThrownBy(() ->
            waiter.in(tenant, "mailbox/" + to, Map.of("to", to), "dead-test-worker", 60L, 1))
            .isInstanceOf(TenantResetException.class);
        assertThat(writer.inp(tenant, "mailbox/" + to, Map.of("to", to), "next-test-worker", 60L))
            .as("the next test's tuple was not claimed by the dead waiter").isPresent();
    }

    @Test
    void aWaitAnyWhoseDeadlinePassesWithTheAbortThrowsInsteadOfRunningItsFinalQuery() {
        String tenant = "reset-wait-deadline-" + UUID.randomUUID();
        String to = "agent-wait-deadline-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        var spec = new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null);
        waiter.testOnlyAtParkLoopTop = abortAtTheDeadline(waiter, tenant, to);

        assertThatThrownBy(() -> waiter.waitAny(tenant, List.of(spec), 1))
            .isInstanceOf(TenantResetException.class);
    }

    // ── an abort that lands in the InterruptedException branch (nexus-vr8wk fix round, F1) ───────────────
    //
    // The first pass plants the next test's row and interrupts the waiting thread, so the park's await throws at
    // once and the loop takes its InterruptedException branch. The abort lands inside that branch (a hook that
    // runs before the branch's own check), so the only thing between the wait and the row is that check.
    // The interrupt flag is set again by that branch, so without the check the final query fails at the pool's
    // interruptible admission with a RuntimeException rather than returning the row: what the check changes is the
    // outcome the caller sees (TenantResetException, a 409) and that no query or claim is attempted at all.

    /** At the park loop's top on its first pass: the next test's row appears, and the waiting thread is interrupted. */
    private Runnable interruptWithTheRowPlanted(String tenant, String to) {
        return once(() -> {
            plant(tenant, to);
            Thread.currentThread().interrupt();
        });
    }

    @Test
    void anRdAbortedAndInterruptedThrowsInsteadOfRunningItsFinalQuery() {
        String tenant = "reset-rd-intr-" + UUID.randomUUID();
        String to = "agent-rd-intr-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        waiter.testOnlyAtParkLoopTop = interruptWithTheRowPlanted(tenant, to);
        waiter.testOnlyAfterInterrupt = () -> waiter.testOnlyWaitRegistry().abortTenant(tenant, 0);

        assertThatThrownBy(() -> waiter.rd(tenant, "mailbox/" + to, Map.of("to", to), 10, null, PARK_SECONDS))
            .isInstanceOf(TenantResetException.class);
        Thread.interrupted();   // the wait restores the interrupt flag; do not leak it into the next test
    }

    @Test
    void anInAbortedAndInterruptedThrowsAndLeavesTheTuple() {
        String tenant = "reset-in-intr-" + UUID.randomUUID();
        String to = "agent-in-intr-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        waiter.testOnlyAtParkLoopTop = interruptWithTheRowPlanted(tenant, to);
        waiter.testOnlyAfterInterrupt = () -> waiter.testOnlyWaitRegistry().abortTenant(tenant, 0);

        assertThatThrownBy(() ->
            waiter.in(tenant, "mailbox/" + to, Map.of("to", to), "dead-test-worker", 60L, PARK_SECONDS))
            .isInstanceOf(TenantResetException.class);
        Thread.interrupted();
        assertThat(writer.inp(tenant, "mailbox/" + to, Map.of("to", to), "next-test-worker", 60L))
            .as("the next test's tuple was not claimed by the dead waiter").isPresent();
    }

    @Test
    void aWaitAnyAbortedAndInterruptedThrowsInsteadOfRunningItsFinalQuery() {
        String tenant = "reset-wait-intr-" + UUID.randomUUID();
        String to = "agent-wait-intr-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        var spec = new TupleRepository.WaitSpec("mailbox/" + to, Map.of("to", to), 10, null);
        waiter.testOnlyAtParkLoopTop = interruptWithTheRowPlanted(tenant, to);
        waiter.testOnlyAfterInterrupt = () -> waiter.testOnlyWaitRegistry().abortTenant(tenant, 0);

        assertThatThrownBy(() -> waiter.waitAny(tenant, List.of(spec), PARK_SECONDS))
            .isInstanceOf(TenantResetException.class);
        Thread.interrupted();
    }

    // ── the evictor reports a tenant that did not quiesce (critic S3) ─────────────────────────────────────

    @Test
    void abortTenantWaitersThrowsWhenAWaiterIsStillRegisteredAfterTheBound() {
        String tenant = "reset-stuck-" + UUID.randomUUID();
        TupleRepository waiter = repo();
        // A registration that nothing ever releases: a stand-in for a wait that does not leave when aborted.
        var stuck = waiter.testOnlyWaitRegistry().register(tenant, "mailbox/stuck");

        assertThatThrownBy(() -> waiter.abortTenantWaiters(tenant, Duration.ofMillis(100)))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("1 tuple waiter(s)")
            .hasMessageContaining(tenant)
            .hasMessageContaining("not quiesced");
        assertThat(waiter.testOnlyWaitRegistry().groupCount()).as("the groups are dropped regardless").isZero();
        stuck.release();
        assertThat(waiter.abortTenantWaiters(tenant, Duration.ofMillis(100))).as("a quiesced tenant is clean").isZero();
    }
}
