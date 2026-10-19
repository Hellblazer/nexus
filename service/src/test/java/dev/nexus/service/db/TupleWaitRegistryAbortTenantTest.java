// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-0bl77 (rtg20 phase A): {@link TupleWaitRegistry#abortTenant} is the tenant-reset route's evictor for the
 * tuple wait groups. A parked wait of the tenant being reset must wake at once and learn it was aborted (so it does
 * not re-query and claim the NEXT test's tuple), the call must wait a bounded time for those waiters to leave, and the
 * tenant's groups must be gone afterwards. Another tenant's groups and waiters must be untouched.
 */
class TupleWaitRegistryAbortTenantTest {

    private static final String X = "abort-tenant-x";
    private static final String Y = "abort-tenant-y";
    private static final long BOUND_NANOS = TimeUnit.SECONDS.toNanos(3);

    @Test
    void abortsTenantXWaitersAndLeavesTenantYAlone() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter x1 = registry.register(X, "s1");
        TupleWaitRegistry.Waiter x2 = registry.register(X, "s2");
        TupleWaitRegistry.Waiter y1 = registry.register(Y, "s1");
        assertThat(registry.groupCount()).isEqualTo(3);

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            // Each X waiter, once it sees the abort, leaves the way TupleRepository does (release in a finally).
            Future<Boolean> sawAbort1 = pool.submit(() -> parkThenRelease(x1));
            Future<Boolean> sawAbort2 = pool.submit(() -> parkThenRelease(x2));
            Thread.sleep(200);   // both are parked on a one-second timer at most

            long start = System.nanoTime();
            int removed = registry.abortTenant(X, BOUND_NANOS).groupsRemoved();
            long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

            assertThat(sawAbort1.get(2, TimeUnit.SECONDS)).as("x1 woke aborted").isTrue();
            assertThat(sawAbort2.get(2, TimeUnit.SECONDS)).as("x2 woke aborted").isTrue();
            assertThat(removed).as("both X groups removed").isEqualTo(2);
            assertThat(elapsedMs).as("returned as the waiters left, not at the bound").isLessThan(2500);
            assertThat(registry.groupCount()).as("only Y's group remains").isEqualTo(1);
            assertThat(y1.aborted()).as("Y's waiter is not aborted").isFalse();
            // Y still wakes on a Y signal: the registry still works for the surviving tenant.
            registry.signalAll(Y, "s1");
            y1.awaitSignalOrTimer();
            assertThat(y1.aborted()).isFalse();
            y1.release();
        } finally {
            pool.shutdownNow();
        }
    }

    private static boolean parkThenRelease(TupleWaitRegistry.Waiter w) throws InterruptedException {
        try {
            w.awaitSignalOrTimer();
            // The one-second timer can end the first wait before the abort lands: wait again until aborted.
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
            while (!w.aborted() && System.nanoTime() < deadline) {
                w.awaitSignalOrTimer();
            }
            return w.aborted();
        } finally {
            w.release();
        }
    }

    @Test
    void abortWaitsForTheWaitersToLeave_butOnlyUpToTheBound() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter stuck = registry.register(X, "s1");   // never released by anyone

        long start = System.nanoTime();
        int removed = registry.abortTenant(X, TimeUnit.MILLISECONDS.toNanos(300)).groupsRemoved();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(elapsedMs).as("waited for the stuck waiter, then gave up").isBetween(250L, 1500L);
        assertThat(removed).as("the group is removed even though its waiter never left").isEqualTo(1);
        assertThat(registry.groupCount()).isZero();
        assertThat(stuck.aborted()).as("the lingering waiter still learns it was aborted").isTrue();
        stuck.release();   // safe after the group is gone
    }

    @Test
    void abortReturnsAtOnceWhenTheTenantHasNoGroups() {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        registry.register(Y, "s1");
        long start = System.nanoTime();
        assertThat(registry.abortTenant(X, BOUND_NANOS).groupsRemoved()).isZero();
        assertThat(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start)).isLessThan(500);
        assertThat(registry.groupCount()).isEqualTo(1);
    }

    @Test
    void aWaiterRegisteredAfterTheAbortGetsAFreshGroupThatIsNotAborted() {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter before = registry.register(X, "s1");
        registry.abortTenant(X, TimeUnit.MILLISECONDS.toNanos(50));
        assertThat(before.aborted()).isTrue();

        TupleWaitRegistry.Waiter after = registry.register(X, "s1");
        assertThat(after.aborted()).as("the next test's waiter on the same tenant and subspace is live").isFalse();
        after.release();
        before.release();
    }

    @Test
    void aMultiWaiterOnTenantXIsAbortedAndOneOnYIsNot() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.MultiWaiter mx = registry.registerMulti(X, java.util.List.of("a", "b"));
        TupleWaitRegistry.MultiWaiter my = registry.registerMulti(Y, java.util.List.of("a"));
        CountDownLatch woke = new CountDownLatch(1);
        AtomicBoolean sawAbort = new AtomicBoolean();
        Thread t = Thread.ofVirtual().start(() -> {
            try {
                long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
                while (!mx.aborted() && System.nanoTime() < deadline) {
                    mx.awaitSignalOrTimer();
                }
                sawAbort.set(mx.aborted());
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            } finally {
                mx.release();
                woke.countDown();
            }
        });
        Thread.sleep(150);

        int removed = registry.abortTenant(X, BOUND_NANOS).groupsRemoved();

        assertThat(woke.await(2, TimeUnit.SECONDS)).isTrue();
        t.join();
        assertThat(sawAbort).isTrue();
        assertThat(removed).as("both of X's groups").isEqualTo(2);
        assertThat(my.aborted()).isFalse();
        assertThat(registry.groupCount()).isEqualTo(1);
        my.release();
    }

    @Test
    void tenantNamesAreMatchedExactly_notByPrefix() {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter longer = registry.register(X + "-longer", "s1");
        TupleWaitRegistry.Waiter exact = registry.register(X, "s1");
        registry.abortTenant(X, TimeUnit.MILLISECONDS.toNanos(50));
        assertThat(exact.aborted()).isTrue();
        assertThat(longer.aborted()).as("a tenant whose name merely starts with X's is not aborted").isFalse();
        assertThat(registry.groupCount()).isEqualTo(1);
        longer.release();
        exact.release();
    }

    // ── MultiWaiter.release against the groups it registered in (code review ship-blocker 2) ─────────

    /**
     * A MultiWaiter that outlives the abort bound is still registered in a group {@code abortTenant} has dropped. A
     * fresh wait on the same key then creates a NEW group. The old waiter's {@code release()} must decrement the
     * group it registered in, not look the key up again and decrement the fresh group (which would drop its waiter
     * count below the truth and let idle eviction reap a group with a parked wait in it).
     */
    @Test
    void anOldMultiWaiterReleasedAfterTheAbortDoesNotDecrementAFreshGroupOfTheSameKey() {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.MultiWaiter old = registry.registerMulti(X, java.util.List.of("a"));
        TupleWaitRegistry.AbortResult aborted = registry.abortTenant(X, TimeUnit.MILLISECONDS.toNanos(50));
        assertThat(aborted.waitersStillParked()).as("the old waiter outlived the bound").isEqualTo(1);

        TupleWaitRegistry.Waiter fresh = registry.register(X, "a");
        assertThat(registry.waiterCount(X, "a")).as("the fresh group holds exactly its own waiter").isEqualTo(1);

        old.release();

        assertThat(registry.waiterCount(X, "a")).as("the old waiter's release left the fresh group alone")
            .isEqualTo(1);
        fresh.release();
        assertThat(registry.waiterCount(X, "a")).isZero();
    }

    @Test
    void anOldMultiWaiterReleasedAfterTheAbortDoesNotDecrementAFreshMultiGroupEither() {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.MultiWaiter old = registry.registerMulti(X, java.util.List.of("a", "b"));
        registry.abortTenant(X, TimeUnit.MILLISECONDS.toNanos(50));

        TupleWaitRegistry.MultiWaiter fresh = registry.registerMulti(X, java.util.List.of("a", "b"));
        old.release();

        assertThat(registry.waiterCount(X, "a")).isEqualTo(1);
        assertThat(registry.waiterCount(X, "b")).isEqualTo(1);
        fresh.release();
        assertThat(registry.waiterCount(X, "a")).isZero();
        assertThat(registry.waiterCount(X, "b")).isZero();
    }

    // ── the abort reports a tenant that did not quiesce (critic S3) ────────────────────────────────

    @Test
    void theResultCountsTheWaitersStillRegisteredWhenTheBoundExpires() {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter stuck1 = registry.register(X, "s1");
        TupleWaitRegistry.Waiter stuck2 = registry.register(X, "s1");
        TupleWaitRegistry.MultiWaiter stuck3 = registry.registerMulti(X, java.util.List.of("s2"));

        TupleWaitRegistry.AbortResult r = registry.abortTenant(X, TimeUnit.MILLISECONDS.toNanos(50));

        assertThat(r.groupsRemoved()).isEqualTo(2);
        assertThat(r.waitersStillParked()).as("three waiters never left").isEqualTo(3);
        stuck1.release();
        stuck2.release();
        stuck3.release();
        assertThat(registry.abortTenant(X, BOUND_NANOS))
            .as("nothing is registered any more").isEqualTo(new TupleWaitRegistry.AbortResult(0, 0));
    }

    @Test
    void aCleanAbortReportsNoWaitersStillParked() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter w = registry.register(X, "s1");
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try {
            Future<Boolean> left = pool.submit(() -> parkThenRelease(w));
            Thread.sleep(150);
            TupleWaitRegistry.AbortResult r = registry.abortTenant(X, BOUND_NANOS);
            assertThat(left.get(2, TimeUnit.SECONDS)).isTrue();
            assertThat(r).isEqualTo(new TupleWaitRegistry.AbortResult(1, 0));
        } finally {
            pool.shutdownNow();
        }
    }

    // ── a registration inside the abort window gets a fresh group (code review B2) ─────────────────

    /**
     * {@code abortTenant} flags a group and then waits for its waiters to leave before it drops it. A NEW wait that
     * registers inside that window must not join the flagged group: it would read as aborted at once (a plain
     * waiter) or be left on a group that is about to leave {@code groups}, never signalled again (a multi waiter).
     */
    @Test
    void aWaitRegisteredWhileTheAbortIsStillWaitingGetsAFreshGroup() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.Waiter stuck = registry.register(X, "s1");
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try {
            Future<TupleWaitRegistry.AbortResult> abort = pool.submit(() -> registry.abortTenant(X, BOUND_NANOS));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
            while (!stuck.aborted() && System.nanoTime() < deadline) {
                Thread.sleep(5);
            }
            assertThat(stuck.aborted()).as("the abort has flagged the group and is waiting").isTrue();

            TupleWaitRegistry.Waiter late = registry.register(X, "s1");
            TupleWaitRegistry.MultiWaiter lateMulti = registry.registerMulti(X, java.util.List.of("s1"));

            assertThat(late.aborted()).as("a wait that begins after the abort is not aborted").isFalse();
            assertThat(lateMulti.aborted()).isFalse();
            assertThat(registry.waiterCount(X, "s1")).as("both late waits sit in the live group").isEqualTo(2);

            stuck.release();
            assertThat(abort.get(2, TimeUnit.SECONDS)).isEqualTo(new TupleWaitRegistry.AbortResult(1, 0));
            assertThat(registry.waiterCount(X, "s1")).as("the abort's removal left the live group alone")
                .isEqualTo(2);
            // And a write still reaches them: they are in the group signalAll looks up.
            registry.signalAll(X, "s1");
            late.awaitSignalOrTimer();
            lateMulti.awaitSignalOrTimer();
            late.release();
            lateMulti.release();
        } finally {
            pool.shutdownNow();
        }
    }
}
