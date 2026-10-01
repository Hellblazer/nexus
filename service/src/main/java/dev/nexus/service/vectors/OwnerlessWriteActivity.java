// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): process-wide, since-boot counters for the ownerless
 * chunk-write check. One count per REQUEST that carried at least one chash with no live manifest
 * row, not per chash.
 *
 * <ul>
 *   <li>{@code refused}: {@link OwnerlessWriteMode#ENFORCE} answered 422;</li>
 *   <li>{@code wouldRefuse}: {@link OwnerlessWriteMode#LOG_ONLY} wrote it anyway.</li>
 * </ul>
 *
 * <p>Same shape as {@link RacedEmbedActivity}: JVM-wide, surfaced by {@code GET /v1/status} as
 * sibling top-level fields, reset only by tests.
 */
public final class OwnerlessWriteActivity {

    private static final AtomicLong REFUSED = new AtomicLong();
    private static final AtomicLong WOULD_REFUSE = new AtomicLong();

    private OwnerlessWriteActivity() {
    }

    public static void recordRefused() {
        REFUSED.incrementAndGet();
    }

    public static void recordWouldRefuse() {
        WOULD_REFUSE.incrementAndGet();
    }

    /** @return requests refused with 422 since boot. */
    public static long refusedTotal() {
        return REFUSED.get();
    }

    /** @return requests log-only mode let through since boot. */
    public static long wouldRefuseTotal() {
        return WOULD_REFUSE.get();
    }

    /** Test-only reset — production code never calls this. */
    public static void resetForTests() {
        REFUSED.set(0);
        WOULD_REFUSE.set(0);
    }
}
