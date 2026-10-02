// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.concurrent.atomic.AtomicLong;

/**
 * RDR-223 P1.5 (bead nexus-z0o2p.6) -- process-wide, lifetime counter of client-supplied
 * vectors the engine did NOT write because the chash already had a stored vector for the same
 * text, and the supplied vector differed from it (RDR-223 Technical Design 2, R-14: an existing
 * chash with a supplied vector keeps the stored vector; the mismatch is counted and logged).
 * Same shape and lifetime as {@link RacedEmbedActivity}, surfaced beside it on {@code GET
 * /v1/status} as {@code supplied_vector_mismatches_total}.
 *
 * <p>A steady non-zero reading means a client is supplying vectors from a different embedding
 * than the ones already stored for the same text (a re-import from a different model version, or
 * a corrupted export), which the engine deliberately does not act on.
 */
public final class SuppliedVectorMismatchActivity {

    private static final AtomicLong MISMATCHES_TOTAL = new AtomicLong();

    private SuppliedVectorMismatchActivity() {
    }

    /** Record {@code count} mismatched supplied vectors from one write. No-op for {@code count <= 0}. */
    public static void record(long count) {
        if (count > 0) {
            MISMATCHES_TOTAL.addAndGet(count);
        }
    }

    /** @return total mismatched supplied vectors so far, process-wide, since boot. */
    public static long total() {
        return MISMATCHES_TOTAL.get();
    }

    /** Test-only reset -- production code never calls this. */
    public static void resetForTests() {
        MISMATCHES_TOTAL.set(0);
    }
}
