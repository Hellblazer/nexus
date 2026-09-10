// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;
import java.util.function.BiConsumer;

/**
 * RDR-205 §Technical Design "Wake": one {@link Condition} per {@code
 * (tenant, subspace)} in a concurrent map, plus the per-claimant and
 * engine-wide park caps (bead nexus-em75s.4).
 *
 * <p><b>Wake.</b> {@code TupleRepository.out} calls {@link #signalAll}
 * AFTER its tenant-scoped transaction lambda has returned (the commit),
 * never from inside it — signalling before commit could wake a parked
 * reader into a query that still misses the just-inserted row.
 *
 * <p><b>Park.</b> A blocking {@code rd}/{@code in} registers a {@link
 * Waiter} via {@link #register} BEFORE running its first query (so a write
 * landing between that query and the first park is not lost), then loops:
 * query, return the connection, park on the waiter until signalled or a
 * one-second timer, repeat. The caller acquires a park slot ({@link
 * #tryAcquireParkSlot}) only once it is actually about to park — a call
 * whose OWN first (non-blocking) query already found a match never touches
 * the cap at all. {@code rd} has no claimant (its signature carries none)
 * and only ever consumes the global slot; {@code in}/{@code inp} consume
 * both the global slot and their claimant's own slot.
 *
 * <p><b>Lost-wakeup closure (RDR-205 Phase 1 review, bead nexus-em75s.7).</b>
 * {@link #register} alone records nothing a signal can observe — a {@code
 * signalAll} landing between the caller's own first {@link #register}/query
 * and its first {@link Waiter#awaitSignalOrTimer} call would previously be
 * dropped: {@link Condition#signalAll()} wakes only threads ALREADY parked
 * on the condition at the moment it runs, and is not sticky. Each {@link
 * Group} now carries a monotonic {@code generation}, bumped under its own
 * lock by every {@link #signalAll}; a {@link Waiter} captures the
 * generation at {@link #register} and compares it at each {@link
 * Waiter#awaitSignalOrTimer} call — a mismatch means a signal already
 * happened since the last observation, so the waiter returns immediately
 * instead of parking for the full one-second timer.
 *
 * <p><b>Shutdown.</b> {@link #shutdown} signals every waiter and flips
 * {@link #isShuttingDown()} so a parked call's next wake runs one final
 * query and returns instead of re-parking, riding out its budget past
 * process exit.
 */
final class TupleWaitRegistry {

    /**
     * TEST-ONLY (RDR-205 bead nexus-em75s.7, the wake-test mutation pins): invoked
     * once per {@code (tenant, subspace)} GROUP that {@link #signalAll} actually
     * delivers a signal to. The wake tests pinning subspace isolation live in {@code
     * dev.nexus.service} (a different package from this class), so they cannot reach
     * a package-private field here directly — {@link TupleRepository
     * #setTestOnlySignalHook} is the cross-package installer. Counting hook
     * invocations lets a test distinguish "woke because signalled" from "woke
     * because the 1-second timer elapsed" and catch a {@code signalAll} that
     * silently widens to every group instead of the one it was called for. No-op by
     * default; never assigned outside test code.
     */
    static volatile BiConsumer<String, String> TEST_ONLY_SIGNAL_HOOK = (tenant, subspace) -> { };

    private final int maxPerClaimant;
    private final int maxGlobal;

    private final ConcurrentHashMap<WaitKey, Group> groups = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, AtomicInteger> perClaimantParked = new ConcurrentHashMap<>();
    private final AtomicInteger globalParked = new AtomicInteger();
    private volatile boolean shuttingDown = false;

    TupleWaitRegistry(int maxPerClaimant, int maxGlobal) {
        this.maxPerClaimant = maxPerClaimant;
        this.maxGlobal = maxGlobal;
    }

    private record WaitKey(String tenant, String subspace) {
    }

    private static final class Group {
        final ReentrantLock lock = new ReentrantLock();
        final Condition condition = lock.newCondition();
        /** Bumped, under {@link #lock}, by every {@link #signalAll} delivered to this
         *  group — the lost-wakeup fix (nexus-em75s.7): a {@link Waiter} compares its
         *  own last-observed value against this to detect a signal it never parked
         *  for. */
        long generation;
    }

    private Group group(String tenant, String subspace) {
        return groups.computeIfAbsent(new WaitKey(tenant, subspace), k -> new Group());
    }

    /** Signals every waiter parked on {@code (tenant, subspace)}. Call ONLY after commit. */
    void signalAll(String tenant, String subspace) {
        Group g = groups.get(new WaitKey(tenant, subspace));
        if (g == null) {
            return;
        }
        g.lock.lock();
        try {
            g.generation++;
            g.condition.signalAll();
        } finally {
            g.lock.unlock();
        }
        TEST_ONLY_SIGNAL_HOOK.accept(tenant, subspace);
    }

    /** Registers interest in {@code (tenant, subspace)} BEFORE the caller's first query. */
    Waiter register(String tenant, String subspace) {
        Group g = group(tenant, subspace);
        long seenGeneration;
        g.lock.lock();
        try {
            seenGeneration = g.generation;
        } finally {
            g.lock.unlock();
        }
        return new Waiter(g, seenGeneration);
    }

    /** A registered interest; parks the calling thread until signalled or one second elapses. */
    final class Waiter {
        private final Group g;
        private long seenGeneration;

        private Waiter(Group g, long seenGeneration) {
            this.g = g;
            this.seenGeneration = seenGeneration;
        }

        /**
         * Parks until {@link #signalAll} bumps this waiter's group's generation past
         * what it last observed, or one second elapses — whichever comes first. A
         * generation mismatch found on ENTRY (a signal landed since the last
         * observation, before this call ever parked) returns immediately without
         * calling {@link Condition#await}, closing the lost-wakeup window between a
         * caller's {@link #register}/first query and its first park.
         */
        void awaitSignalOrTimer() throws InterruptedException {
            g.lock.lock();
            try {
                if (g.generation != seenGeneration) {
                    seenGeneration = g.generation;
                    return;
                }
                g.condition.await(1, TimeUnit.SECONDS);
                seenGeneration = g.generation;
            } finally {
                g.lock.unlock();
            }
        }
    }

    /**
     * Attempts to acquire a park slot before the caller actually parks. Throws {@link
     * ParkCapExceededException} rather than returning false — every call site's next
     * action on refusal is to give up and return the (already-computed, empty) probe
     * result, which is what the exception carries by construction (see its javadoc).
     *
     * @param claimantOrNull null for a claimant-less caller ({@code rd}); non-null names
     *                       the claimant whose own cap is also checked ({@code in}/{@code inp})
     */
    void tryAcquireParkSlot(String claimantOrNull) {
        int newGlobal = globalParked.incrementAndGet();
        if (newGlobal > maxGlobal) {
            globalParked.decrementAndGet();
            throw new ParkCapExceededException("global");
        }
        if (claimantOrNull != null) {
            AtomicInteger perClaimant = perClaimantParked.computeIfAbsent(claimantOrNull, k -> new AtomicInteger());
            int newPerClaimant = perClaimant.incrementAndGet();
            if (newPerClaimant > maxPerClaimant) {
                perClaimant.decrementAndGet();
                globalParked.decrementAndGet();
                throw new ParkCapExceededException("claimant");
            }
        }
    }

    /** Releases a park slot acquired via {@link #tryAcquireParkSlot}. Always call in a {@code finally}. */
    void releaseParkSlot(String claimantOrNull) {
        globalParked.decrementAndGet();
        if (claimantOrNull != null) {
            AtomicInteger perClaimant = perClaimantParked.get(claimantOrNull);
            if (perClaimant != null) {
                perClaimant.decrementAndGet();
            }
        }
    }

    boolean isShuttingDown() {
        return shuttingDown;
    }

    /** Signals every waiter and flips {@link #isShuttingDown()}. Idempotent. */
    void shutdown() {
        shuttingDown = true;
        for (Group g : groups.values()) {
            g.lock.lock();
            try {
                g.condition.signalAll();
            } finally {
                g.lock.unlock();
            }
        }
    }
}
