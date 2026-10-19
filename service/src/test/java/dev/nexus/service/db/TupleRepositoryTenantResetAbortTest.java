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
