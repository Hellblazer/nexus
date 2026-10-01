// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.concurrent.ConcurrentHashMap;
import java.util.function.LongSupplier;

/**
 * Rate limit for the {@code ownerless_chunk_write_*} WARN line (RDR-223 Phase 3 Step 2,
 * nexus-z0o2p.24): at most one line per key per window, carrying how many were suppressed since
 * the last one, so a client that retries a refused write in a loop cannot flood the engine log.
 * The counters on {@code /v1/status} are NOT limited; they count every request.
 *
 * <p>Keyed by {@code route|tenant|collection}: two tenants that registered the same collection name
 * each get their own line. The map is bounded at {@link #MAX_KEYS} keys, and the bound neither
 * leaks nor floods. A new key that arrives at the cap first evicts every key whose window has
 * elapsed (such a key remembers only a suppressed count, which a quiet key no longer needs; the sweep
 * runs at most once per {@link #SWEEP_INTERVAL_MS}). If the map is still full, every new key shares ONE
 * overflow bucket with the same one-line-per-window rule, its suppressed count included. So past the
 * cap the engine logs at most one extra line a minute, however many distinct (tenant, collection)
 * pairs a client registers and writes to, and a line it does log still names the real key.
 */
public final class OwnerlessLogLimiter {

    /** One line per key per this many milliseconds. */
    public static final long WINDOW_MS = 60_000L;

    static final int MAX_KEYS = 10_000;

    /** The expired-key sweep at the cap runs at most this often, so a full map of live keys is not rescanned per write. */
    static final long SWEEP_INTERVAL_MS = 1_000L;

    private static final class State {
        long lastLoggedMs;
        long suppressed;
    }

    private final LongSupplier clockMs;
    private final ConcurrentHashMap<String, State> states = new ConcurrentHashMap<>();
    /** Shared by every key that does not fit in {@link #states}; not counted in {@link #size()}. */
    private final State overflow = newState();
    private long lastSweepMs = Long.MIN_VALUE;   // guarded by this

    public OwnerlessLogLimiter(LongSupplier clockMs) {
        this.clockMs = clockMs;
    }

    /** The limiter the repository uses. */
    public static OwnerlessLogLimiter system() {
        return new OwnerlessLogLimiter(System::currentTimeMillis);
    }

    private static State newState() {
        State s = new State();
        s.lastLoggedMs = Long.MIN_VALUE;
        return s;
    }

    /** Number of keys currently remembered, the overflow bucket excluded; never above {@link #MAX_KEYS}. */
    int size() {
        return states.size();
    }

    /**
     * @return {@code -1} when this line must be suppressed (the caller logs nothing); otherwise the
     *         number of lines suppressed for this key since the last one that was logged (0 for the first)
     */
    public long tryAcquire(String key) {
        long now = clockMs.getAsLong();
        State st = states.get(key);
        if (st == null) {
            st = admit(key, now);
        }
        synchronized (st) {
            if (st.lastLoggedMs == Long.MIN_VALUE || now - st.lastLoggedMs >= WINDOW_MS) {
                long suppressed = st.suppressed;
                st.suppressed = 0;
                st.lastLoggedMs = now;
                return suppressed;
            }
            st.suppressed++;
            return -1;
        }
    }

    /**
     * The state for a key not in the map: its own when there is room (after evicting expired keys if
     * the map is full), else the shared overflow bucket. Serialised, so the map never passes the cap;
     * it is only reached on a miss, which after warm-up is a new (tenant, collection) pair.
     */
    private synchronized State admit(String key, long now) {
        State existing = states.get(key);
        if (existing != null) {
            return existing;
        }
        if (states.size() >= MAX_KEYS && (lastSweepMs == Long.MIN_VALUE || now - lastSweepMs >= SWEEP_INTERVAL_MS)) {
            lastSweepMs = now;
            states.entrySet().removeIf(e -> {
                State s = e.getValue();
                synchronized (s) {
                    return s.lastLoggedMs != Long.MIN_VALUE && now - s.lastLoggedMs >= WINDOW_MS;
                }
            });
        }
        if (states.size() >= MAX_KEYS) {
            return overflow;
        }
        State fresh = newState();
        states.put(key, fresh);
        return fresh;
    }
}
