// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArraySet;
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
 * both the global slot and their claimant's own slot. {@code rd}/{@code in}/
 * {@code waitAny} each release their {@link #register}/{@link #registerMulti}
 * registration on EVERY exit -- an immediate first-query hit and an exception
 * thrown by that first query included, not only the park-loop path -- because
 * an un-released registration leaves {@link Group#waiters} permanently
 * non-zero and its group permanently ineligible for {@link #evictIdleGroups}
 * (nexus-rplay, the register/release leak fix).
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
 *
 * <p><b>Multiplexed wait (RDR-211 Phase 1 Step 1, bead nexus-rplay.4).</b>
 * {@link #registerMulti} parks ONE {@link MultiWaiter} across SEVERAL
 * {@code (tenant, subspace)} groups at once — {@code TupleRepository.waitAny}'s
 * mechanism for a multi-subspace {@code rd} that returns as soon as ANY
 * registered subspace has a matching write, without one {@link Waiter}/{@link
 * #tryAcquireParkSlot} pair per subspace. Each {@link Group} notifies every
 * {@link MultiWaiter} registered on it from inside {@link #signalAll}'s own
 * lock, alongside the existing generation bump — the same lost-wakeup closure
 * {@link Waiter} gets, but via a pending-subspace set rather than a generation
 * counter (see {@link MultiWaiter}'s own javadoc for why). {@code
 * registerMulti} deliberately never touches {@link #tryAcquireParkSlot}/{@link
 * #globalParked}/{@link #perClaimantParked} — a caller wraps N subspaces in
 * one {@code registerMulti} call and spends exactly one park slot, via its own
 * single {@link #tryAcquireParkSlot} call, not N.
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
        /**
         * RDR-211 Phase 1 Step 1 (bead nexus-rplay.4): {@link MultiWaiter}s parked
         * across THIS group alongside others -- notified by {@link #signalAll} in
         * addition to this group's own {@link #condition}. A {@link
         * CopyOnWriteArraySet} because membership changes (register/release) are
         * rare relative to signals, and iteration under {@link #lock} must never
         * itself contend with a concurrent mutation. Identity-keyed (no {@code
         * equals}/{@code hashCode} override on {@link MultiWaiter}), which is
         * exactly right: two distinct waiters registered for the same subspace are
         * two distinct listeners.
         */
        final Set<MultiWaiter> multiListeners = new CopyOnWriteArraySet<>();
        /** Bead nexus-rxuiq: the newest waiter token seen per announce subscriber
         *  ({@code ""} for a row-level mailbox spec), guarded by {@link #lock}. Lives
         *  on the group so it is evicted with it; an evicted group has no parked wait
         *  left to fence. */
        final Map<String, String> currentWaiter = new HashMap<>();

        Group(long nowNanos) {
            this.lastActivityNanos = nowNanos;
        }
    }

    private long now() {
        return nanoTimeSource.getAsLong();
    }

    // ── waiter supersession (bead nexus-rxuiq) ────────────────────────────────

    private static final java.util.regex.Pattern WAITER_TOKEN =
            java.util.regex.Pattern.compile("([0-9]{1,19})-([A-Za-z0-9]{1,64})");

    /** {@code <decimal time_ns>-<alphanumeric id>}: the client mints one per waiter
     *  instance, strictly increasing within its process, so a waiter started later
     *  there has the larger token; across processes the wall clock orders them. The
     *  time must fit a {@code long}: a 19-digit value past {@link Long#MAX_VALUE}
     *  would pass the pattern and then fail every later comparison on its key, so it
     *  is refused here, at validation, instead. */
    static boolean isWellFormedWaiterToken(String token) {
        if (token == null) {
            return false;
        }
        var m = WAITER_TOKEN.matcher(token);
        if (!m.matches()) {
            return false;
        }
        try {
            Long.parseLong(m.group(1));
            return true;
        } catch (NumberFormatException overflow) {
            return false;
        }
    }

    /** Orders two well-formed tokens by their time, then by their id. */
    static int compareWaiterTokens(String a, String b) {
        var ma = WAITER_TOKEN.matcher(a);
        var mb = WAITER_TOKEN.matcher(b);
        if (!ma.matches() || !mb.matches()) {
            throw new IllegalArgumentException("malformed waiter token");
        }
        int byTime = Long.compare(Long.parseLong(ma.group(1)), Long.parseLong(mb.group(1)));
        return byTime != 0 ? byTime : ma.group(2).compareTo(mb.group(2));
    }

    /**
     * Bead nexus-rxuiq. A parked announce-mode wait outlives the reader that
     * issued it: the engine cannot see a client disconnect while a handler is
     * blocked (the JDK HTTP server exposes none), so a cancelled waiter's call,
     * or a dead process's, stays parked for up to its timeout and stamps the next
     * row as announced for nobody. Each reader therefore sends a waiter token,
     * and the newest token for a {@code (tenant, subspace, subscriber)} wins.
     *
     * <p>Returns {@code false} when {@code token} is older than the current one:
     * the caller must return superseded without querying. A NEWER token becomes
     * current and wakes the group, so an older wait still parked there re-checks
     * ({@link #isCurrentWaiter}) and returns instead of stamping.
     *
     * <p>Per engine process, like the rest of this registry: on a multi-instance
     * engine an older wait parked on another instance is not fenced, which is
     * today's behaviour, never worse.
     */
    boolean admitWaiter(String tenant, String subspace, String subscriber, String token) {
        WaitKey key = new WaitKey(tenant, subspace);
        String who = subscriber == null ? "" : subscriber;
        while (true) {
            Group g = groups.computeIfAbsent(key, k -> new Group(now()));
            g.lock.lock();
            try {
                if (groups.get(key) != g) {
                    continue; // evicted in between, same retry as register()
                }
                g.lastActivityNanos = now();
                String current = g.currentWaiter.get(who);
                int cmp = current == null ? 1 : compareWaiterTokens(token, current);
                if (cmp < 0) {
                    return false;
                }
                if (cmp > 0) {
                    g.currentWaiter.put(who, token);
                    if (current != null) {
                        wakeLocked(g, subspace);
                    }
                }
                return true;
            } finally {
                g.lock.unlock();
            }
        }
    }

    /** {@code true} unless a newer token than {@code token} has been admitted for
     *  {@code (tenant, subspace, subscriber)} since. */
    boolean isCurrentWaiter(String tenant, String subspace, String subscriber, String token) {
        Group g = groups.get(new WaitKey(tenant, subspace));
        if (g == null) {
            return true;
        }
        g.lock.lock();
        try {
            String current = g.currentWaiter.get(subscriber == null ? "" : subscriber);
            return current == null || compareWaiterTokens(token, current) >= 0;
        } finally {
            g.lock.unlock();
        }
    }

    /** Wakes every waiter on {@code g} without counting as a delivered write
     *  signal (the test hook stays write-only). Caller holds {@code g.lock}. */
    private void wakeLocked(Group g, String subspace) {
        g.generation++;
        g.condition.signalAll();
        for (MultiWaiter mw : g.multiListeners) {
            mw.notifyWake(subspace);
        }
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
            // RDR-211 Phase 1 Step 1 (bead nexus-rplay.4): also wake every
            // MultiWaiter parked across this group ALONGSIDE others -- under the
            // SAME lock, same placement as the signal hook above, so a
            // MultiWaiter's registerMulti (which also takes g.lock to add itself
            // to this set) can never race a signal into a lost update.
            for (MultiWaiter mw : g.multiListeners) {
                mw.notifyWake(subspace);
            }
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
     * RDR-211 Phase 1 Step 1 (bead nexus-rplay.4): registers ONE {@link MultiWaiter}
     * across SEVERAL {@code (tenant, subspace)} groups at once, so {@code
     * TupleRepository.waitAny} can park on many subspaces with a single thread and a
     * single park-slot acquisition, instead of one {@link Waiter} (and one {@link
     * #tryAcquireParkSlot} call) per subspace. MUST be called BEFORE the caller's
     * first query against every one of {@code subspaces}, same contract as {@link
     * #register} -- a write landing between that query and the first {@link
     * MultiWaiter#awaitSignalOrTimer} call reaches {@link MultiWaiter#notifyWake}
     * (via {@link #signalAll}) regardless of whether the caller has parked yet, so it
     * is never lost.
     *
     * <p>Deliberately does NOT touch {@link #tryAcquireParkSlot}/{@link
     * #globalParked}/{@link #perClaimantParked} -- park-slot accounting is already
     * fully decoupled from group registration in this design ({@code in}'s own loop
     * calls {@link #tryAcquireParkSlot} separately, once, after registering), so a
     * caller wrapping N subspaces in one {@code registerMulti} call and ONE {@link
     * #tryAcquireParkSlot} call spends exactly one slot, not N.
     */
    MultiWaiter registerMulti(String tenant, List<String> subspaces) {
        MultiWaiter mw = new MultiWaiter(tenant, List.copyOf(subspaces));
        for (String subspace : subspaces) {
            WaitKey key = new WaitKey(tenant, subspace);
            while (true) {
                Group g = groups.computeIfAbsent(key, k -> new Group(now()));
                g.lock.lock();
                try {
                    if (groups.get(key) != g) {
                        // Evicted between computeIfAbsent and this lock acquisition --
                        // retry against whatever's there now, same as register().
                        continue;
                    }
                    g.waiters++;
                    g.multiListeners.add(mw);
                    g.lastActivityNanos = now();
                } finally {
                    g.lock.unlock();
                }
                break;
            }
            evictIdleGroups(key);
        }
        return mw;
    }

    /**
     * RDR-211 Phase 1 Step 1 (bead nexus-rplay.4): a single waiter registered across
     * several {@code (tenant, subspace)} groups via {@link #registerMulti}, woken by
     * a {@link #signalAll} against ANY of them. Owns its own lock/condition, separate
     * from any {@link Group}'s -- each registered {@link Group} notifies THIS object
     * (via {@link #notifyWake}) rather than this object parking directly on N
     * different {@link Condition}s, which Java's lock API has no way to do in one
     * blocking call.
     */
    final class MultiWaiter {
        private final String tenant;
        private final List<String> subspaces;
        private final ReentrantLock lock = new ReentrantLock();
        private final Condition condition = lock.newCondition();
        /** Subspaces that signalled since the last {@link #awaitSignalOrTimer} drained
         *  this set, guarded by {@link #lock}. A signal landing before the first await
         *  call simply accumulates here rather than being lost -- there is no
         *  generation counter to race the way {@link Waiter} needs one, because {@link
         *  #notifyWake} pushes directly into this set instead of merely bumping a
         *  counter a not-yet-parked reader would still need to notice. */
        private final Set<String> pendingSubspaces = new HashSet<>();
        private volatile boolean released;

        private MultiWaiter(String tenant, List<String> subspaces) {
            this.tenant = tenant;
            this.subspaces = subspaces;
        }

        /** Called by {@link #signalAll}, under the signalling group's OWN lock (never
         *  this waiter's) -- see {@link #signalAll}'s own comment for why that
         *  placement is safe (fixed lock order: a group's lock is always acquired
         *  before this method takes this waiter's lock, never the reverse, so there is
         *  no deadlock cycle). */
        private void notifyWake(String subspace) {
            lock.lock();
            try {
                pendingSubspaces.add(subspace);
                condition.signal();
            } finally {
                lock.unlock();
            }
        }

        /**
         * Parks until ANY registered subspace has signalled, or one second elapses --
         * the multi-group counterpart to {@link Waiter#awaitSignalOrTimer}. Returns the
         * (possibly empty, on a timeout) set of subspaces that signalled since the
         * last call, draining {@link #pendingSubspaces}. A signal already pending on
         * entry (the lost-wakeup window this closes, same as {@link Waiter}'s
         * generation check) returns immediately without parking.
         */
        Set<String> awaitSignalOrTimer() throws InterruptedException {
            lock.lock();
            try {
                if (pendingSubspaces.isEmpty()) {
                    condition.await(1, TimeUnit.SECONDS);
                }
                Set<String> drained = Set.copyOf(pendingSubspaces);
                pendingSubspaces.clear();
                return drained;
            } finally {
                lock.unlock();
            }
        }

        /**
         * Marks this waiter done across EVERY subspace group it registered in
         * (decrementing occupancy and removing this listener from each) -- the
         * multi-group counterpart to {@link Waiter#release}. Idempotent; call exactly
         * once.
         */
        void release() {
            if (released) {
                return;
            }
            released = true;
            for (String subspace : subspaces) {
                Group g = groups.get(new WaitKey(tenant, subspace));
                if (g == null) {
                    continue; // already evicted -- nothing left to release against
                }
                g.lock.lock();
                try {
                    g.waiters--;
                    g.multiListeners.remove(this);
                    g.lastActivityNanos = now();
                } finally {
                    g.lock.unlock();
                }
            }
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
