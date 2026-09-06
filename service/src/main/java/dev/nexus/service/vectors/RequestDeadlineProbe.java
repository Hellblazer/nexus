// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.http.RequestContext;

/**
 * The cooperative deadline check shared by the embed-loop check points
 * (nexus-8hdg9 phases 3 and 4: {@link Bge768Embedder#embedSubBatched} between
 * ONNX sub-batches, {@link CceEmbedder} before each collected future).
 *
 * <p>Cost discipline (design record §4): a check point reads the deadline ONCE
 * per embed call via {@link #currentDeadlineNanos()} and then evaluates
 * {@link #expired(long, long)} against a {@code System.nanoTime()} reading the
 * loop already takes for its progress counters. The per-iteration cost is one
 * long subtraction and compare; no second clock read.
 *
 * <p>{@link #NONE} (0) means "no deadline in context" -- a request that never
 * passed through {@code AuthFilter} (direct test construction, in-process
 * callers) is never aborted. Nanotime can in principle equal 0, but a deadline
 * minted as {@code nanoTime() + budget} landing on exactly 0 would merely
 * disable the check for that one request, never abort a healthy one.
 */
final class RequestDeadlineProbe {

    /** Sentinel for "no deadline in context": never expires. */
    static final long NONE = 0L;

    private RequestDeadlineProbe() {
    }

    /** The current request's deadline, or {@link #NONE} outside a filtered request. */
    static long currentDeadlineNanos() {
        Long d = RequestContext.deadlineNanos();
        return d == null ? NONE : d;
    }

    /**
     * True iff {@code deadlineNanos} is set and {@code nowNanos} is at or past it.
     * Overflow-safe: the comparison is on the difference, as {@link System#nanoTime()}'s
     * contract requires.
     */
    static boolean expired(long deadlineNanos, long nowNanos) {
        return deadlineNanos != NONE && nowNanos - deadlineNanos >= 0L;
    }
}
