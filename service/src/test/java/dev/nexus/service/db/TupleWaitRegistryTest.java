// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.concurrent.TimeUnit;

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
}
