// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;
import java.util.function.BiConsumer;
import java.util.function.LongSupplier;

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
 * <p><b>Group eviction (RDR-205 §Memory management, bead nexus-em75s.37,
 * critique S5).</b> {@link #groups} would otherwise grow one {@link Group}
 * per {@code (tenant, subspace)} ever seen, forever — nothing previously
 * removed an entry once created. {@link #register} now also runs a cheap,
 * non-blocking sweep ({@link #evictIdleGroups}) that removes any OTHER
 * group with no live waiter ({@link Group#waiters} == 0) and no activity
 * ({@link Group#lastActivityNanos}, bumped by {@link #register}, {@link
 * Waiter#release}, and {@link #signalAll}) for {@link #IDLE_EVICT_NANOS}.
 * Eviction and registration race safely because both touch a group's state
 * only under that group's own {@link Group#lock}: whichever acquires the
 * lock first wins — a concurrent {@link #register} that increments {@code
 * waiters} first makes the group ineligible; an eviction that removes the
 * mapping first is detected by {@link #register} re-checking identity
 * under the lock and retrying against a fresh group. {@code waiters} is a
 * genuine occupancy count, not a last-register timestamp: a caller parked
 * in a long {@code rd}/{@code in} poll loop keeps its group alive for the
 * whole loop (via {@link Waiter#release} only decrementing at the very
 * end), so a real in-flight waiter is never evicted out from under it.
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
     *
     * <p>RDR-205 P1 follow-on (nexus-em75s.40, fix-check M-minor): invoked from
     * inside {@link #signalAll}'s {@code g.lock} critical section, immediately
     * after the generation bump and {@code condition.signalAll()} — guarded the
     * same way {@link TupleRepository
     * #TEST_ONLY_CLAIM_SELECT_TO_UPDATE_DELAY} runs inside its own surrounding
     * transaction, rather than after the commit/unlock. Before this fix the hook
     * fired AFTER {@code g.lock.unlock()}, so a test asserting on the hook's
     * side effect (a counter, say) raced the lock's own release with no
     * happens-before relationship between the two beyond this field's own
     * {@code volatile} read.
     */
    static volatile BiConsumer<String, String> TEST_ONLY_SIGNAL_HOOK = (tenant, subspace) -> { };

    /**
     * A group with no live waiter and no signal for at least this long is eligible
     * for eviction by {@link #evictIdleGroups} (bead nexus-em75s.37). Package-private
     * so {@code TupleWaitRegistryTest} can reason about it directly; a real deploy
     * never needs a value other than this one, so there is no env/config knob.
     */
    static final long IDLE_EVICT_NANOS = TimeUnit.MINUTES.toNanos(1);

    private final int maxPerClaimant;
    private final int maxGlobal;
    private final LongSupplier nanoTimeSource;

    private final ConcurrentHashMap<WaitKey, Group> groups = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, AtomicInteger> perClaimantParked = new ConcurrentHashMap<>();
    private final AtomicInteger globalParked = new AtomicInteger();
    /** Cumulative {@code ParkCapExceededException("global")} refusal count (RDR-211
     *  Phase 1 Step 1, bead nexus-rplay.7) -- see {@link #globalRefusedCount}. */
    private final AtomicLong globalRefused = new AtomicLong();
    /** Cumulative {@code ParkCapExceededException("claimant")} refusal count
     *  (RDR-211 Phase 1 Step 1) -- see {@link #claimantRefusedCount}. */
    private final AtomicLong claimantRefused = new AtomicLong();
    private volatile boolean shuttingDown = false;

    TupleWaitRegistry(int maxPerClaimant, int maxGlobal) {
        this(maxPerClaimant, maxGlobal, System::nanoTime);
    }

    /** Test-injectable clock (bead nexus-em75s.37): a fixed/advanceable {@link
     *  LongSupplier} lets {@code TupleWaitRegistryTest} exercise {@link
     *  #evictIdleGroups} deterministically without a real one-minute sleep. */
    TupleWaitRegistry(int maxPerClaimant, int maxGlobal, LongSupplier nanoTimeSource) {
        this.maxPerClaimant = maxPerClaimant;
        this.maxGlobal = maxGlobal;
        this.nanoTimeSource = nanoTimeSource;
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
        /** Live {@link Waiter} count for this group, guarded by {@link #lock}:
         *  incremented by {@link #register}, decremented by {@link Waiter#release}.
         *  Zero is the eviction precondition (nexus-em75s.37) -- a group with a
         *  genuinely parked caller is never evicted mid-wait. */
        int waiters;
        /** Nanotime of the last registration, release, or signal on this group,
         *  guarded by {@link #lock}. The other eviction precondition. */
        long lastActivityNanos;

        Group(long nowNanos) {
            this.lastActivityNanos = nowNanos;
        }
    }

    private long now() {
        return nanoTimeSource.getAsLong();
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
            g.lastActivityNanos = now();
            g.condition.signalAll();
            // nexus-em75s.40: invoked UNDER the lock, right after the generation
            // bump/signal -- see the field's own javadoc for why.
            TEST_ONLY_SIGNAL_HOOK.accept(tenant, subspace);
        } finally {
            g.lock.unlock();
        }
    }

    /**
     * Registers interest in {@code (tenant, subspace)} BEFORE the caller's first
     * query. Retries against a freshly-created {@link Group} if the one {@link
     * ConcurrentHashMap#computeIfAbsent} handed back was concurrently evicted by
     * {@link #evictIdleGroups} between that call and this method acquiring its lock
     * (nexus-em75s.37) -- so a registration can never silently attach to a group that
     * future {@link #signalAll} calls will no longer find in {@link #groups}.
     */
    Waiter register(String tenant, String subspace) {
        WaitKey key = new WaitKey(tenant, subspace);
        while (true) {
            Group g = groups.computeIfAbsent(key, k -> new Group(now()));
            long seenGeneration;
            g.lock.lock();
            try {
                if (groups.get(key) != g) {
                    // Evicted between computeIfAbsent and this lock acquisition --
                    // g is orphaned; retry against whatever's there now (or create
                    // a fresh one).
                    continue;
                }
                g.waiters++;
                g.lastActivityNanos = now();
                seenGeneration = g.generation;
            } finally {
                g.lock.unlock();
            }
            evictIdleGroups(key);
            return new Waiter(g, seenGeneration);
        }
    }

    /**
     * Removes every group other than {@code exempt} that has no live waiter and no
     * activity for {@link #IDLE_EVICT_NANOS} (nexus-em75s.37). Non-blocking: a group
     * currently locked by a concurrent {@link #register}/{@link Waiter#release}/
     * {@link #signalAll} is simply skipped this pass rather than waited on -- it will
     * be reconsidered on the next {@link #register} call, and an idle group is in no
     * hurry to be reclaimed by exactly one minute versus a few minutes later.
     */
    private void evictIdleGroups(WaitKey exempt) {
        long nowNanos = now();
        for (var entry : groups.entrySet()) {
            WaitKey key = entry.getKey();
            if (key.equals(exempt)) {
                continue;
            }
            Group g = entry.getValue();
            if (!g.lock.tryLock()) {
                continue;
            }
            try {
                if (g.waiters == 0 && (nowNanos - g.lastActivityNanos) >= IDLE_EVICT_NANOS) {
                    groups.remove(key, g);
                }
            } finally {
                g.lock.unlock();
            }
        }
    }

    /** Current group count -- test-only visibility into {@link #groups}' size
     *  (nexus-em75s.37), so a test can assert eviction actually shrank the map. */
    int groupCount() {
        return groups.size();
    }

    /** A registered interest; parks the calling thread until signalled or one second elapses. */
    final class Waiter {
        private final Group g;
        private long seenGeneration;
        private boolean released;

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

        /**
         * Marks this waiter done (nexus-em75s.37): decrements the group's live-waiter
         * count and refreshes its activity clock, so the idle-eviction window starts
         * from the moment the last waiter actually stopped waiting, not from {@link
         * #register} time. Idempotent; call exactly once, from the same {@code
         * finally} block that calls {@link #releaseParkSlot}.
         */
        void release() {
            g.lock.lock();
            try {
                if (released) {
                    return;
                }
                released = true;
                g.waiters--;
                g.lastActivityNanos = now();
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
     * <p>The per-claimant branch (nexus-xapt8, a scalability research pass over this design addition
     * 15) goes through {@link ConcurrentHashMap#compute}, whose remapping function
     * runs atomically for the given key — the increment-then-check the previous
     * {@code computeIfAbsent} + {@code incrementAndGet} pair performed as TWO separate
     * operations is now one. That pairing had a second defect beyond the race: nothing
     * ever removed a claimant's entry once its count reached zero, so {@link
     * #perClaimantParked} grew one entry per DISTINCT claimant string EVER parked,
     * forever — for a claimant identity that is typically a one-shot agent/session id
     * rather than a small closed set, that is an unbounded map keyed on cardinality
     * this registry has no way to bound. The {@code compute} call below folds the
     * removal in: a claimant whose count reaches zero (in {@link #releaseParkSlot})
     * has its entry removed in the SAME atomic step that decrements it.
     *
     * @param claimantOrNull null for a claimant-less caller ({@code rd}); non-null names
     *                       the claimant whose own cap is also checked ({@code in}/{@code inp})
     */
    void tryAcquireParkSlot(String claimantOrNull) {
        int newGlobal = globalParked.incrementAndGet();
        if (newGlobal > maxGlobal) {
            globalParked.decrementAndGet();
            // RDR-211 Phase 1 Step 1 (bead nexus-rplay.7): counted AFTER the rollback,
            // same ordering as the per-claimant branch below -- a refusal must never
            // skew globalInUse(), only the separate refusal counter.
            globalRefused.incrementAndGet();
            throw new ParkCapExceededException("global");
        }
        if (claimantOrNull != null) {
            boolean[] exceeded = {false};
            perClaimantParked.compute(claimantOrNull, (k, v) -> {
                int current = (v == null) ? 0 : v.get();
                if (current + 1 > maxPerClaimant) {
                    exceeded[0] = true;
                    return v; // unchanged -- the cap refuses, nothing to acquire
                }
                if (v == null) {
                    return new AtomicInteger(1);
                }
                v.incrementAndGet();
                return v;
            });
            if (exceeded[0]) {
                globalParked.decrementAndGet();
                // RDR-211 Phase 1 Step 1 (bead nexus-rplay.7): see the global branch's
                // matching comment above.
                claimantRefused.incrementAndGet();
                throw new ParkCapExceededException("claimant");
            }
        }
    }

    /** Releases a park slot acquired via {@link #tryAcquireParkSlot}. Always call in a
     *  {@code finally}. Removes {@code claimantOrNull}'s {@link #perClaimantParked}
     *  entry the moment its count reaches zero (nexus-xapt8), atomically with the
     *  decrement via {@link ConcurrentHashMap#compute} -- the counterpart to {@link
     *  #tryAcquireParkSlot}'s own {@code compute} call, so the map never accumulates
     *  an entry for a claimant with no currently-parked call. */
    void releaseParkSlot(String claimantOrNull) {
        globalParked.decrementAndGet();
        if (claimantOrNull != null) {
            perClaimantParked.compute(claimantOrNull, (k, v) -> {
                if (v == null) {
                    return null; // never acquired (or already reaped) -- nothing to release
                }
                return (v.decrementAndGet() <= 0) ? null : v;
            });
        }
    }

    /** Number of claimants currently tracked with a non-zero parked count
     *  (nexus-xapt8) -- test-only visibility so a test can assert the map
     *  returns to empty once every parked call releases. */
    int perClaimantTrackedCount() {
        return perClaimantParked.size();
    }

    // ── park report (RDR-211 Phase 1 Step 1, bead nexus-rplay.7) ────────────

    /** This registry's configured global park cap -- exposed so {@link
     *  TupleRepository#parkStats} can report it without keeping its own copy
     *  of the constructor argument it already handed to this registry. */
    int maxGlobal() {
        return maxGlobal;
    }

    /** This registry's configured per-claimant park cap. See {@link #maxGlobal}. */
    int maxPerClaimant() {
        return maxPerClaimant;
    }

    /** Current global in-use gauge -- the same value {@link #tryAcquireParkSlot}
     *  compares against {@link #maxGlobal}. A null-claimant park ({@code rd}, and
     *  RDR-211 Phase 1 Step 1's {@code wait}) is counted here and ONLY here --
     *  it never appears in {@link #perClaimantSnapshot}. */
    int globalInUse() {
        return globalParked.get();
    }

    /** Cumulative count of {@code ParkCapExceededException("global")} refusals
     *  since this registry was constructed. Never reset; a fresh count starts
     *  only with a fresh registry (one per JVM process in production, so this
     *  is a process lifetime total, not a point-in-time gauge like {@link
     *  #globalInUse}). */
    long globalRefusedCount() {
        return globalRefused.get();
    }

    /** Cumulative count of {@code ParkCapExceededException("claimant")}
     *  refusals. See {@link #globalRefusedCount}. */
    long claimantRefusedCount() {
        return claimantRefused.get();
    }

    /**
     * Point-in-time snapshot of {@link #perClaimantParked} as a plain
     * claimant-to-count map: unlike {@link #perClaimantTrackedCount} (a bare
     * size, kept for the existing map-shrinks-back-to-empty test), the park
     * report distinguishes slots BY CLAIMANT so a caller can observe "one slot
     * per session" and "never two slots for one session" directly, rather than
     * inferring it from a total. A claimant with no currently-parked call is
     * never a key here (its entry is removed the instant {@link
     * #releaseParkSlot} brings its count to zero) -- an empty map means no
     * claimant-scoped park is in flight, not that none was ever counted. A
     * plain copy, not a live view: the caller gets one moment's numbers, never
     * a reference that mutates under it.
     */
    Map<String, Integer> perClaimantSnapshot() {
        Map<String, Integer> out = new LinkedHashMap<>();
        for (var e : perClaimantParked.entrySet()) {
            out.put(e.getKey(), e.getValue().get());
        }
        return out;
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
