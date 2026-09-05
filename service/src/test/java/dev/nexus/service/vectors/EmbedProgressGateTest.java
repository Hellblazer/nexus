// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-s71lr — pure rate-limiting logic for {@link Bge768Embedder}'s embed-progress
 * logging. Deliberately model-free: {@link EmbedProgressGate} takes an explicit {@code nowNanos}
 * on every call rather than reading the wall clock itself, so this test needs no ONNX model,
 * no timing sleeps, and never flakes on CI scheduling jitter — unlike {@link
 * Bge768BatchCompositionTest} (which DOES need the ~416MB bge ONNX export and is gated by
 * {@code Assumptions}), this class always executes.
 */
class EmbedProgressGateTest {

    private static final long INTERVAL_NANOS = TimeUnit.SECONDS.toNanos(5);

    @Test
    void firstCallEverAlwaysLogs() {
        EmbedProgressGate gate = new EmbedProgressGate(INTERVAL_NANOS);
        // Never logged before -- must fire regardless of interval, so a run's very
        // first sub-batch is never silent while waiting for the first 5s window.
        assertThat(gate.shouldLog(0L)).isTrue();
    }

    @Test
    void secondCallImmediatelyAfterIsSuppressed() {
        EmbedProgressGate gate = new EmbedProgressGate(INTERVAL_NANOS);
        assertThat(gate.shouldLog(0L)).isTrue();
        // 1ns later -- nowhere near the 5s interval.
        assertThat(gate.shouldLog(1L)).isFalse();
    }

    @Test
    void callJustUnderTheIntervalIsSuppressed() {
        EmbedProgressGate gate = new EmbedProgressGate(INTERVAL_NANOS);
        assertThat(gate.shouldLog(0L)).isTrue();
        assertThat(gate.shouldLog(INTERVAL_NANOS - 1)).isFalse();
    }

    @Test
    void callAtOrPastTheIntervalLogsAgain() {
        EmbedProgressGate gate = new EmbedProgressGate(INTERVAL_NANOS);
        assertThat(gate.shouldLog(0L)).isTrue();
        assertThat(gate.shouldLog(INTERVAL_NANOS)).isTrue();
        // Rate limit re-arms from the moment just claimed, not the original t=0.
        assertThat(gate.shouldLog(INTERVAL_NANOS + 1)).isFalse();
        assertThat(gate.shouldLog(2 * INTERVAL_NANOS)).isTrue();
    }

    @Test
    void concurrentRacersAtTheSameInstantClaimTheSlotExactlyOnce() {
        // Simulates two threads both observing "the interval has elapsed" and racing
        // to claim the log slot at the identical nowNanos -- exactly the shape a CAS
        // must resolve to exactly one winner (the whole point of a shared, cross-call
        // rate limiter: many concurrent embed() calls on one Bge768Embedder instance
        // must never all log at once just because they all cleared the interval
        // check simultaneously).
        EmbedProgressGate gate = new EmbedProgressGate(INTERVAL_NANOS);
        assertThat(gate.shouldLog(0L)).isTrue();

        long racerNow = INTERVAL_NANOS + 100;
        int winners = 0;
        for (int i = 0; i < 8; i++) {
            if (gate.shouldLog(racerNow)) winners++;
        }
        assertThat(winners)
                .as("exactly one of many callers observing the identical instant may claim the slot")
                .isEqualTo(1);
    }

    @Test
    void zeroIntervalLogsEveryCallAfterTheFirst() {
        // Degenerate but well-defined: interval=0 means "never suppress" -- every
        // non-decreasing nowNanos clears the "at least interval since last" check.
        EmbedProgressGate gate = new EmbedProgressGate(0L);
        assertThat(gate.shouldLog(0L)).isTrue();
        assertThat(gate.shouldLog(0L)).isTrue();
        assertThat(gate.shouldLog(1L)).isTrue();
    }
}
