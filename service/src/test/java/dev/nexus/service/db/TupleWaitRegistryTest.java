// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-205 Phase 1 review (nexus-em75s.7, the lost-wakeup finding) — direct,
 * single-threaded pin on {@link TupleWaitRegistry#register} / {@link
 * TupleWaitRegistry#signalAll} / {@link TupleWaitRegistry.Waiter#awaitSignalOrTimer}.
 * Same package as {@link TupleWaitRegistry} (its members here are package-private),
 * so this test reaches the mechanism directly rather than through {@code
 * TupleRepository}'s {@code rd}/{@code in} — {@code TupleRepositoryTest}'s wake
 * tests (a different package) pin the observable end-to-end behaviour instead.
 */
class TupleWaitRegistryTest {

    private static final String TENANT = "wait-registry-tenant";
    private static final String SUBSPACE = "wait-registry-subspace";

    /**
     * The exact race the lost-wakeup bug missed: {@link TupleWaitRegistry#signalAll}
     * landing AFTER {@link TupleWaitRegistry#register} but BEFORE the caller's first
     * {@link TupleWaitRegistry.Waiter#awaitSignalOrTimer} call. Before the generation
     * counter, {@code signalAll} calling {@code Condition#signalAll()} with no thread
     * yet parked on it is simply lost — {@link TupleWaitRegistry.Waiter
     * #awaitSignalOrTimer} would then park for the FULL one-second timer despite the
     * signal having already happened. Single-threaded and deterministic: no other
     * thread races this test, so there is no timing window to get unlucky on.
     */
    @Test
    void awaitSignalOrTimer_signalBetweenRegisterAndAwait_returnsImmediately() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);

        TupleWaitRegistry.Waiter waiter = registry.register(TENANT, SUBSPACE);
        registry.signalAll(TENANT, SUBSPACE); // lands BEFORE the first await -- the lost-wakeup window

        long start = System.nanoTime();
        waiter.awaitSignalOrTimer();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(elapsedMs)
                .as("a signal landing before the first await must not be lost to the 1s timer")
                .isLessThan(700);
    }

    /**
     * Control case: no signal at all -- {@code awaitSignalOrTimer} must still fall
     * back to the one-second timer (not return instantly), so the fix above is not
     * simply "always return immediately."
     */
    @Test
    void awaitSignalOrTimer_noSignal_fallsBackToOneSecondTimer() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter waiter = registry.register(TENANT, SUBSPACE);

        long start = System.nanoTime();
        waiter.awaitSignalOrTimer();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(elapsedMs)
                .as("no signal at all must still ride out the ~1s timer")
                .isGreaterThanOrEqualTo(900);
    }

    /**
     * A signal for a DIFFERENT (tenant, subspace) group must never wake this waiter --
     * the counterpart, at this same package-private level, to {@code
     * TupleRepositoryTest}'s subspace-isolation wake test.
     */
    @Test
    void awaitSignalOrTimer_signalForDifferentGroup_doesNotWake() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter waiter = registry.register(TENANT, SUBSPACE);
        registry.signalAll(TENANT, "a-different-subspace");

        long start = System.nanoTime();
        waiter.awaitSignalOrTimer();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(elapsedMs)
                .as("a signal for a different group must not close this waiter's timer early")
                .isGreaterThanOrEqualTo(900);
    }

    // ── group eviction (RDR-205 §Memory management, bead nexus-em75s.37) ───────

    /**
     * {@code groups} must not grow forever: a group with no live waiter and no
     * signal for {@link TupleWaitRegistry#IDLE_EVICT_NANOS} is reclaimed. Uses an
     * injected {@link java.util.concurrent.atomic.AtomicLong}-backed clock rather
     * than a real sleep, per the bead's own test description ("with a fixed clock
     * or an injected time source") -- deterministic and instant.
     */
    @Test
    void register_manySubspaces_parkedNothing_idleGroupsAreEvictedAfterTheIdlePeriod() {
        java.util.concurrent.atomic.AtomicLong clock = new java.util.concurrent.atomic.AtomicLong(0L);
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16, clock::get);

        int subspaceCount = 50;
        for (int i = 0; i < subspaceCount; i++) {
            TupleWaitRegistry.Waiter waiter = registry.register(TENANT, SUBSPACE + "-" + i);
            waiter.release(); // parks nothing: registers, then immediately stops waiting
        }
        assertThat(registry.groupCount()).isEqualTo(subspaceCount);

        // Advance the injected clock well past the idle threshold, then register one
        // more (unrelated) group -- register() runs the opportunistic sweep.
        clock.addAndGet(TupleWaitRegistry.IDLE_EVICT_NANOS * 2);
        TupleWaitRegistry.Waiter trigger = registry.register(TENANT, "trigger-subspace");
        trigger.release();

        assertThat(registry.groupCount())
                .as("every idle, waiter-less group from before the clock jump must be reclaimed")
                .isEqualTo(1); // only the just-registered trigger group survives
    }

    /**
     * The counterpart to the eviction test above: a group with a LIVE waiter (never
     * released) must never be evicted, even once the clock says it is idle --
     * {@code waiters} is a genuine occupancy count, not a last-register timestamp.
     */
    @Test
    void register_liveWaiterNeverReleased_groupSurvivesPastTheIdlePeriod() {
        java.util.concurrent.atomic.AtomicLong clock = new java.util.concurrent.atomic.AtomicLong(0L);
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16, clock::get);

        TupleWaitRegistry.Waiter stillWaiting = registry.register(TENANT, "still-parked-subspace");
        registry.register(TENANT, "idle-subspace").release();
        assertThat(registry.groupCount()).isEqualTo(2);

        clock.addAndGet(TupleWaitRegistry.IDLE_EVICT_NANOS * 2);
        registry.register(TENANT, "trigger-subspace").release();

        assertThat(registry.groupCount())
                .as("a group with a live (never-released) waiter must survive the sweep")
                .isEqualTo(2); // still-parked-subspace + trigger-subspace; idle-subspace is gone

        stillWaiting.release(); // avoid leaking state past the test, though nothing reads it after
    }

    // ── per-claimant park counters (RDR-211 scalability research, nexus-xapt8) ──

    /**
     * Before the fix, {@code perClaimantParked} grew one entry per DISTINCT
     * claimant string ever parked and nothing ever removed one — a many-
     * distinct-claimants workload (the common shape: a claimant identity is
     * typically a one-shot agent/session id, not a small closed set) leaked
     * the map without bound. {@link TupleWaitRegistry#releaseParkSlot} now
     * removes a claimant's entry the moment its count reaches zero, in the
     * same atomic {@code compute} step as the decrement.
     */
    @Test
    void tryAcquireParkSlot_manyDistinctClaimants_thenReleaseAll_mapReturnsToEmpty() {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 1_000);

        int claimantCount = 200;
        for (int i = 0; i < claimantCount; i++) {
            String claimant = "claimant-" + i;
            registry.tryAcquireParkSlot(claimant);
        }
        assertThat(registry.perClaimantTrackedCount())
            .as("one tracked entry per distinct claimant while parked")
            .isEqualTo(claimantCount);

        for (int i = 0; i < claimantCount; i++) {
            registry.releaseParkSlot("claimant-" + i);
        }
        assertThat(registry.perClaimantTrackedCount())
            .as("every entry must be removed once its count reaches zero -- not merely decremented to zero")
            .isZero();
    }

    /**
     * The race the un-atomic {@code computeIfAbsent} + {@code incrementAndGet}
     * pair exposed: many threads hammering acquire/release for the SAME
     * claimant, concurrently, must never let more than {@code maxPerClaimant}
     * acquisitions be live at once. Asserts the invariant DIRECTLY (a
     * shared counter of currently-held slots, checked against the cap at
     * every acquisition) rather than inferring it from exception counts,
     * which a lost update could satisfy by accident. A brief hold between
     * acquire and release widens the race window (same reasoning as {@code
     * TupleClaimContentionTest}'s injected delay), and a vacuity guard fails
     * loud if concurrent holding was never actually observed -- a run that
     * happened to fully serialize would prove nothing about the fix.
     */
    @Test
    void tryAcquireParkSlot_concurrentSameClaimant_neverExceedsThePerClaimantCap() throws Exception {
        int cap = 4;
        TupleWaitRegistry registry = new TupleWaitRegistry(cap, 1_000);
        String claimant = "shared-claimant";
        int threads = 32;
        int roundsPerThread = 50;

        AtomicInteger currentlyHeld = new AtomicInteger();
        AtomicInteger maxObservedHeld = new AtomicInteger();
        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            for (int t = 0; t < threads; t++) {
                pool.submit(() -> {
                    try {
                        start.await();
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                    for (int r = 0; r < roundsPerThread; r++) {
                        try {
                            registry.tryAcquireParkSlot(claimant);
                        } catch (ParkCapExceededException e) {
                            continue; // refused -- correct under contention, not a failure
                        }
                        int held = currentlyHeld.incrementAndGet();
                        maxObservedHeld.updateAndGet(prev -> Math.max(prev, held));
                        try {
                            Thread.sleep(1); // widen the race window
                        } catch (InterruptedException ie) {
                            Thread.currentThread().interrupt();
                        }
                        currentlyHeld.decrementAndGet();
                        registry.releaseParkSlot(claimant);
                    }
                });
            }
            start.countDown();
            pool.shutdown();
            assertThat(pool.awaitTermination(60, TimeUnit.SECONDS))
                .as("all threads must finish within the test's own budget")
                .isTrue();
        } finally {
            pool.shutdownNow();
        }

        assertThat(maxObservedHeld.get())
            .as("vacuity guard: this run must have observed genuine concurrent holding "
                + "(peak > 1), otherwise it never exercised the race at all")
            .isGreaterThan(1);
        assertThat(maxObservedHeld.get())
            .as("the per-claimant cap must never be exceeded, even under concurrent contention")
            .isLessThanOrEqualTo(cap);
        assertThat(registry.perClaimantTrackedCount())
            .as("every acquisition was paired with a release -- the tracked entry must be gone")
            .isZero();
    }
}
