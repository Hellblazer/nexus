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
 * <p>Keyed by {@code route|collection}. The map is bounded: past {@link #MAX_KEYS} keys a new key
 * is logged unconditionally and not remembered, which fails toward logging rather than toward
 * silence.
 */
public final class OwnerlessLogLimiter {

    /** One line per key per this many milliseconds. */
    public static final long WINDOW_MS = 60_000L;

    static final int MAX_KEYS = 10_000;

    private static final class State {
        long lastLoggedMs;
        long suppressed;
    }

    private final LongSupplier clockMs;
    private final ConcurrentHashMap<String, State> states = new ConcurrentHashMap<>();

    public OwnerlessLogLimiter(LongSupplier clockMs) {
        this.clockMs = clockMs;
    }

    /** The limiter the repository uses. */
    public static OwnerlessLogLimiter system() {
        return new OwnerlessLogLimiter(System::currentTimeMillis);
    }

    /**
     * @return {@code -1} when this line must be suppressed (the caller logs nothing); otherwise the
     *         number of lines suppressed for this key since the last one that was logged (0 for the first)
     */
    public long tryAcquire(String key) {
        long now = clockMs.getAsLong();
        if (states.size() >= MAX_KEYS && !states.containsKey(key)) {
            return 0;
        }
        State st = states.computeIfAbsent(key, k -> {
            State s = new State();
            s.lastLoggedMs = Long.MIN_VALUE;
            return s;
        });
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
}
