// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-236 Phase 4 Step 2a (nexus-6pfgb.66): the process-wide, since-boot count of client chunk-metadata writes
 * that carried a key outside the allow-list. One count per REQUEST, however many keys or chunks offended, and
 * counted whether or not the log line was rate limited: the counter on {@code GET /v1/status} is the complete
 * signal, the log line a sample.
 *
 * <p>Same shape as {@link OwnerlessWriteActivity}: JVM-wide, reset only by tests. Step 2a is log mode only, so
 * nothing counts a refusal here; Step 2c adds that field when it adds the 422.
 */
public final class ChunkKeyAllowlistActivity {

    private static final AtomicLong WOULD_REFUSE = new AtomicLong();

    private ChunkKeyAllowlistActivity() {
    }

    static void recordWouldRefuse() {
        WOULD_REFUSE.incrementAndGet();
    }

    /** @return client chunk-metadata writes that named a key outside the allow-list since boot. */
    public static long wouldRefuseTotal() {
        return WOULD_REFUSE.get();
    }

    /** Test-only reset: production code never calls this. */
    public static void resetForTests() {
        WOULD_REFUSE.set(0);
    }
}
