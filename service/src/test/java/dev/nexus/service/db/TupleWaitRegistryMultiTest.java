// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-211 Phase 1 Step 1 (bead nexus-rplay.4) — direct, package-private pin on
 * {@link TupleWaitRegistry#registerMulti} / {@link TupleWaitRegistry.MultiWaiter},
 * promoted from the throwaway {@code TupleWaitRegistryMultiSpikeTest} (T2
 * {@code nexus_rdr/211-spike-3-2026-09-16}) once the Critical Assumption it probed
 * ("the wait registry can register one waiter in several subspace groups and wake it
 * from any of them, without losing a write that lands between the first query and the
 * park") was confirmed 5/5 green.
 *
 * <p>Same package-private access shape as {@link TupleWaitRegistryTest}, which this
 * mirrors directly -- single-threaded and deterministic wherever the mechanism under
 * test allows it (the lost-wakeup closure), matching that file's own reasoning for
 * testing at this level rather than through {@code TupleRepository.waitAny}.
 */
class TupleWaitRegistryMultiTest {

    private static final String TENANT = "wait-registry-multi-tenant";

    /**
     * The exact race {@link TupleWaitRegistryTest
     * #awaitSignalOrTimer_signalBetweenRegisterAndAwait_returnsImmediately} pins for a
     * single-group {@link TupleWaitRegistry.Waiter}, extended to THREE groups: a
     * {@code signalAll} against the MIDDLE of the three subspaces, landing AFTER
     * {@code registerMulti} but BEFORE the first {@code awaitSignalOrTimer} call (the
     * gap between a caller's own first query and its first park), must not be lost.
     */
    @Test
    void awaitSignalOrTimer_signalBetweenRegisterAndAwait_returnsImmediatelyWithTheSignalledSubspace()
            throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        List<String> subspaces = List.of("multi-a", "multi-b", "multi-c");

        TupleWaitRegistry.MultiWaiter mw = registry.registerMulti(TENANT, subspaces);
        // The write landing in the gap between "query" and the first park -- exactly
        // the window `rd`'s own register-before-query contract exists to close.
        registry.signalAll(TENANT, "multi-b");

        long start = System.nanoTime();
        Set<String> woken = mw.awaitSignalOrTimer();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(woken).as("the signal that landed before the first await must not be lost")
                .containsExactly("multi-b");
        assertThat(elapsedMs)
                .as("a signal landing before the first await must not be lost to the 1s timer")
                .isLessThan(700);
        mw.release();
    }

    /** Control: no signal at all still rides out the ~1s timer, same shape as
     *  {@link TupleWaitRegistryTest#awaitSignalOrTimer_noSignal_fallsBackToOneSecondTimer}. */
    @Test
    void awaitSignalOrTimer_noSignal_fallsBackToOneSecondTimerAndReturnsEmpty() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.MultiWaiter mw = registry.registerMulti(TENANT,
                List.of("multi-idle-a", "multi-idle-b"));

        long start = System.nanoTime();
        Set<String> woken = mw.awaitSignalOrTimer();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(woken).as("no signal at all -- nothing to report").isEmpty();
        assertThat(elapsedMs).as("must still ride out the ~1s timer").isGreaterThanOrEqualTo(900);
        mw.release();
    }

    /**
     * The mechanism's central claim: a write to ANY ONE of the registered subspaces
     * wakes the multiplexed waiter, not merely the group it happens to match by
     * chance. Exercised across all three positions (first, middle, last) in separate
     * waiters so no one group's placement in the list is special-cased.
     */
    @Test
    void aSignalToAnyOneOfThreeRegisteredSubspacesWakesTheSameWaiter() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        List<String> subspaces = List.of("multi-any-a", "multi-any-b", "multi-any-c");

        for (String target : subspaces) {
            TupleWaitRegistry.MultiWaiter mw = registry.registerMulti(TENANT, subspaces);
            registry.signalAll(TENANT, target);

            Set<String> woken = mw.awaitSignalOrTimer();

            assertThat(woken).as("waking subspace '" + target + "' must be reported").containsExactly(target);
            mw.release();
        }
    }

    /**
     * A signal for a subspace NOT in this waiter's registered set must never wake it
     * -- the multi-group counterpart to {@link TupleWaitRegistryTest
     * #awaitSignalOrTimer_signalForDifferentGroup_doesNotWake}.
     */
    @Test
    void aSignalForAnUnregisteredSubspaceDoesNotWakeTheMultiWaiter() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        TupleWaitRegistry.MultiWaiter mw = registry.registerMulti(TENANT,
                List.of("multi-scoped-a", "multi-scoped-b"));
        registry.signalAll(TENANT, "multi-not-registered");

        long start = System.nanoTime();
        Set<String> woken = mw.awaitSignalOrTimer();
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - start);

        assertThat(woken).as("a signal outside the registered set must not appear").isEmpty();
        assertThat(elapsedMs)
                .as("must not be closed early by a signal this waiter never registered for")
                .isGreaterThanOrEqualTo(900);
        mw.release();
    }

    /**
     * The park-slot claim RDR-211 §Technical Design "Waiting" makes: registering
     * across N subspaces must cost the SAME park-slot accounting as registering
     * across one -- {@code registerMulti} itself never touches {@link
     * TupleWaitRegistry#tryAcquireParkSlot}, so a caller wrapping three subspaces in
     * one {@code registerMulti} plus ONE {@code tryAcquireParkSlot} call (exactly the
     * shape {@code TupleRepository.waitAny} uses) spends exactly one global slot, not
     * three.
     */
    @Test
    void registerMultiAcrossThreeSubspacesConsumesOneParkSlotNotThree() throws Exception {
        TupleWaitRegistry registry = new TupleWaitRegistry(4, 16);
        String claimant = "multi-slot-claimant";

        TupleWaitRegistry.MultiWaiter mw = registry.registerMulti(TENANT,
                List.of("multi-slot-a", "multi-slot-b", "multi-slot-c"));
        // The caller's own single park-slot acquisition, exactly as `in`'s loop makes
        // ONE tryAcquireParkSlot call regardless of how it registered.
        registry.tryAcquireParkSlot(claimant);
        try {
            assertThat(registry.perClaimantTrackedCount())
                    .as("one claimant tracked, not three -- registerMulti touched no park-slot accounting itself")
                    .isEqualTo(1);
        } finally {
            registry.releaseParkSlot(claimant);
        }
        assertThat(registry.perClaimantTrackedCount())
                .as("released back to empty").isEqualTo(0);
        mw.release();
    }
}
