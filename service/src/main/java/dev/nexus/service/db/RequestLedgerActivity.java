// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.2): process-wide counts, since boot, of the four
 * {@link RequestLedger} claim outcomes worth watching: a replay of a committed key, a 409 on a
 * live in-flight key, a 422 on a reused key, and a takeover of an expired or failed attempt.
 *
 * <p>Same shape and lifetime as {@code RacedEmbedActivity}: JVM-wide, no per-tenant dimension,
 * surfaced by {@code GET /v1/status} (bead nexus-yye8l.4). Each count pairs with one INFO line
 * from {@link RequestLedger} carrying the tenant, request id and route.
 */
public final class RequestLedgerActivity {

    private static final AtomicLong REPLAYS = new AtomicLong();
    private static final AtomicLong IN_FLIGHT = new AtomicLong();
    private static final AtomicLong KEY_REUSED = new AtomicLong();
    private static final AtomicLong TAKEOVERS = new AtomicLong();

    private RequestLedgerActivity() {
    }

    /** One reading of all four counts. */
    public record Snapshot(long replays, long inFlight, long keyReused, long takeovers) {
    }

    static void recordReplay() {
        REPLAYS.incrementAndGet();
    }

    static void recordInFlight() {
        IN_FLIGHT.incrementAndGet();
    }

    static void recordKeyReused() {
        KEY_REUSED.incrementAndGet();
    }

    static void recordTakeover() {
        TAKEOVERS.incrementAndGet();
    }

    /** @return the four counts, process-wide, since boot. */
    public static Snapshot snapshot() {
        return new Snapshot(REPLAYS.get(), IN_FLIGHT.get(), KEY_REUSED.get(), TAKEOVERS.get());
    }

    /** Test-only reset; production code never calls this. */
    public static void resetForTests() {
        REPLAYS.set(0);
        IN_FLIGHT.set(0);
        KEY_REUSED.set(0);
        TAKEOVERS.set(0);
    }
}
