// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-s71lr, deliverable 2 — pure lifetime-counter logic backing {@code
 * Bge768Embedder#activitySnapshot()}, the wire counters {@code
 * dev.nexus.service.http.StatusHandler} serves on {@code GET /v1/status}.
 * Deliberately model-free (explicit {@code nowNanos} on every call, like
 * {@link EmbedProgressGate}) so this test needs no ONNX model and never
 * flakes on timing.
 */
class EmbedActivityTrackerTest {

    private static final long ACTIVE_WINDOW_NANOS = TimeUnit.SECONDS.toNanos(10);

    @Test
    void neverRecordedSnapshotIsInactiveWithNoAgeAndZeroCounts() {
        EmbedActivityTracker tracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);
        EmbedActivitySnapshot snap = tracker.snapshot(12345L, -1, -1);

        assertThat(snap.active()).isFalse();
        assertThat(snap.chunksDoneTotal()).isZero();
        assertThat(snap.subBatchesTotal()).isZero();
        assertThat(snap.lastChunksPerSec()).isZero();
        assertThat(snap.lastActivityAgeMs())
                .as("never-recorded age is the -1 sentinel, not 0 (0 would read as "
                    + "'just happened')")
                .isEqualTo(-1L);
    }

    @Test
    void oneRecordMakesItActiveWithMatchingCounts() {
        EmbedActivityTracker tracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);
        tracker.record(16, 8.5, 0L);

        EmbedActivitySnapshot snap = tracker.snapshot(0L, -1, -1);
        assertThat(snap.active()).isTrue();
        assertThat(snap.chunksDoneTotal()).isEqualTo(16);
        assertThat(snap.subBatchesTotal()).isEqualTo(1);
        assertThat(snap.lastChunksPerSec()).isEqualTo(8.5);
        assertThat(snap.lastActivityAgeMs()).isZero();
    }

    @Test
    void countsAccumulateAcrossMultipleRecords() {
        EmbedActivityTracker tracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);
        tracker.record(10, 5.0, 0L);
        tracker.record(6, 3.0, 1_000_000L); // +1ms

        EmbedActivitySnapshot snap = tracker.snapshot(1_000_000L, -1, -1);
        assertThat(snap.chunksDoneTotal()).isEqualTo(16);
        assertThat(snap.subBatchesTotal()).isEqualTo(2);
        // lastChunksPerSec reflects the MOST RECENT record, not an average.
        assertThat(snap.lastChunksPerSec()).isEqualTo(3.0);
    }

    @Test
    void agedPastTheActiveWindowReportsInactiveButKeepsTheCounts() {
        EmbedActivityTracker tracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);
        tracker.record(20, 4.0, 0L);

        long farLater = ACTIVE_WINDOW_NANOS + 1;
        EmbedActivitySnapshot snap = tracker.snapshot(farLater, -1, -1);

        assertThat(snap.active())
                .as("past the active window, no more recent activity has occurred")
                .isFalse();
        assertThat(snap.chunksDoneTotal())
                .as("lifetime counts never reset just because activity went stale")
                .isEqualTo(20);
        assertThat(snap.lastActivityAgeMs()).isEqualTo(farLater / 1_000_000L);
    }

    @Test
    void exactlyAtTheActiveWindowBoundaryIsInactive() {
        // Half-open window: [0, activeWindowNanos) is active, exactly at the
        // boundary is not -- avoids an off-by-one flip-flop right at the edge.
        EmbedActivityTracker tracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);
        tracker.record(1, 1.0, 0L);

        assertThat(tracker.snapshot(ACTIVE_WINDOW_NANOS - 1, -1, -1).active()).isTrue();
        assertThat(tracker.snapshot(ACTIVE_WINDOW_NANOS, -1, -1).active()).isFalse();
    }

    @Test
    void concurrentRecordsAreAllCounted() throws InterruptedException {
        EmbedActivityTracker tracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);
        int threads = 8;
        int recordsPerThread = 200;
        Thread[] workers = new Thread[threads];
        for (int t = 0; t < threads; t++) {
            workers[t] = new Thread(() -> {
                for (int i = 0; i < recordsPerThread; i++) {
                    tracker.record(1, 1.0, System.nanoTime());
                }
            });
        }
        for (Thread w : workers) w.start();
        for (Thread w : workers) w.join();

        EmbedActivitySnapshot snap = tracker.snapshot(System.nanoTime(), -1, -1);
        assertThat(snap.chunksDoneTotal()).isEqualTo((long) threads * recordsPerThread);
        assertThat(snap.subBatchesTotal()).isEqualTo((long) threads * recordsPerThread);
    }

    @Test
    void queueDepthAndThreadWidthArePassedThroughVerbatim() {
        // code-review-expert pass 2 finding c: this tracker has no admission-gate
        // knowledge of its own -- the caller (Bge768Embedder) reads the real
        // values and hands them in; this proves the tracker does not silently
        // drop, clamp, or recompute them.
        EmbedActivityTracker tracker = new EmbedActivityTracker(ACTIVE_WINDOW_NANOS);
        tracker.record(5, 2.0, 0L);

        assertThat(tracker.snapshot(0L, 3, 8).queueDepth()).isEqualTo(3);
        assertThat(tracker.snapshot(0L, 3, 8).threadWidth()).isEqualTo(8);
        // -1/-1 (no admission gate wired) passes through unchanged too.
        assertThat(tracker.snapshot(0L, -1, -1).queueDepth()).isEqualTo(-1);
        assertThat(tracker.snapshot(0L, -1, -1).threadWidth()).isEqualTo(-1);
    }
}
