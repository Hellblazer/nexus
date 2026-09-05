// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Bead nexus-s71lr — rate-limits engine embed-progress log lines to at most one per {@code
 * intervalNanos}, shared (via a single {@link AtomicLong} + CAS) across every concurrent {@code
 * embed()} call on one embedder instance.
 *
 * <p>Sam, 2026-09-04 (T2 {@code nexus/release-7.30.0-ship-2026-09-04} incident retro): "the
 * v0.1.99 regression (engine capped at 2 threads) and a healthy v0.1.100 run were
 * indistinguishable from outside for the first ten minutes" — {@link Bge768Embedder}'s
 * {@code embedSubBatched} already knows sub-batch index, size, and elapsed time per call, and
 * logged none of it above {@code DEBUG}. This class is the pure "when do we emit the next
 * progress line" decision, extracted so it is unit-testable without the ~416MB bge ONNX export
 * ({@link Bge768BatchCompositionTest}'s gate) — every call takes an explicit {@code nowNanos}
 * rather than reading the wall clock, so a test drives the decision deterministically.
 *
 * <p><b>Why global (instance-wide), not per-call.</b> A per-call rate limiter (reset at the
 * start of every {@code embed()} invocation) would still flood at INFO under a workload of many
 * small, fast, back-to-back calls — each call's own "first call ever" exemption would fire on
 * every single request. Sharing ONE gate across the embedder's whole lifetime means the "large
 * run logs about once every N seconds" guarantee holds regardless of how the caller chunks its
 * requests into individual {@code embed()} calls.
 *
 * <p><b>The one exemption: the very first progress line this gate ever grants.</b> Without it,
 * an operator watching logs from process start would wait up to {@code intervalNanos} before
 * seeing ANY embed activity — silence indistinguishable from a hang at exactly the moment
 * reassurance matters most. Every subsequent decision is purely time-gated.
 */
final class EmbedProgressGate {

    /** Sentinel meaning "never granted a log line yet" — {@link Long#MIN_VALUE} so that even
     * {@code nowNanos == 0} (as a test might pass) is unambiguously "in the future" of it. */
    private static final long NEVER_LOGGED = Long.MIN_VALUE;

    private final long intervalNanos;
    private final AtomicLong lastLogNanos = new AtomicLong(NEVER_LOGGED);

    EmbedProgressGate(long intervalNanos) {
        if (intervalNanos < 0) {
            throw new IllegalArgumentException("intervalNanos must be >= 0, got " + intervalNanos);
        }
        this.intervalNanos = intervalNanos;
    }

    /**
     * Returns {@code true} (and atomically claims the slot) iff this is the very first call
     * this gate has ever granted, or at least {@code intervalNanos} have elapsed since the last
     * claimed slot. CAS-based so concurrent callers observing the same stale {@code
     * lastLogNanos} never both win the same slot (see {@link
     * EmbedProgressGateTest#concurrentRacersAtTheSameInstantClaimTheSlotExactlyOnce()}).
     *
     * @param nowNanos the caller's current {@link System#nanoTime()} reading (or, in tests, an
     *                  arbitrary deterministic value — this class never reads the clock itself)
     */
    boolean shouldLog(long nowNanos) {
        while (true) {
            long last = lastLogNanos.get();
            if (last != NEVER_LOGGED && nowNanos - last < intervalNanos) {
                return false;
            }
            if (lastLogNanos.compareAndSet(last, nowNanos)) {
                return true;
            }
            // CAS lost the race to a concurrent caller — re-read and retry the decision
            // against whatever slot that caller just claimed (may now suppress this one).
        }
    }
}
