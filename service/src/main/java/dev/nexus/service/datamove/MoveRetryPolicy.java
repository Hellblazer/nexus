// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.time.Duration;
import java.time.Instant;

/**
 * nexus-f3yqd.2.15 (RDR-246, Retries, pause and cancelled runs): the cadence of a blocked move. Computed from the registry
 * row ({@code transient_retries}, {@code last_transient_at}), so no timer state is kept and a restart changes nothing.
 * The worker never stops retrying; past {@link #NEEDS_OPERATOR_AT} consecutive transients the text says so.
 */
final class MoveRetryPolicy {

    private MoveRetryPolicy() { /* static */ }

    static final Duration FIRST = Duration.ofSeconds(30);
    static final Duration CAP = Duration.ofMinutes(15);
    /** The exponent stops growing here: 30 s * 2^5 is already past the cap. */
    private static final int MAX_DOUBLINGS = 5;
    /** From this many consecutive transients {@code blocked:} adds "needs an operator". */
    static final int NEEDS_OPERATOR_AT = 20;

    /** min(30 s * 2^(min(N, 6) - 1), 15 min); zero for a move with no retries. */
    static Duration backoff(int retries) {
        if (retries <= 0) {
            return Duration.ZERO;
        }
        Duration wait = FIRST.multipliedBy(1L << Math.min(retries - 1, MAX_DOUBLINGS));
        return wait.compareTo(CAP) > 0 ? CAP : wait;
    }

    /** Whether a move whose last transient failure was at {@code last}, the {@code retries}th, may be worked at {@code now}. */
    static boolean due(Instant last, int retries, Instant now) {
        return last == null || !now.isBefore(last.plus(backoff(retries)));
    }

    /** What {@code blocked:} shows: the count, the time of the latest transient error and the error. */
    static String blockedReason(int retries, Instant last, String error) {
        String base = "retry " + retries + ", last " + last + ": " + error;
        return retries >= NEEDS_OPERATOR_AT ? base + "; needs an operator" : base;
    }
}
