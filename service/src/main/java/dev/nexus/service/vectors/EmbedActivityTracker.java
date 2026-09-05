// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Bead nexus-s71lr, deliverable 2 — the pure, model-free lifetime-counter logic
 * behind {@link Bge768Embedder#activitySnapshot()}. Mirrors {@link
 * EmbedProgressGate}'s design: every decision takes an explicit {@code
 * nowNanos} rather than reading the wall clock itself, so {@link
 * EmbedActivityTrackerTest} needs no ONNX model and never flakes on timing.
 *
 * <p>Unlike {@link EmbedProgressGate} (which decides whether to emit a LOG
 * LINE, throttled to about once per 5s), this class is updated on EVERY
 * sub-batch unconditionally — the wire counters it feeds ({@code GET
 * /v1/status}) must reflect the true current state regardless of how often
 * the log line itself is allowed to fire.
 */
final class EmbedActivityTracker {

    private final long activeWindowNanos;
    private final AtomicLong chunksDoneTotal = new AtomicLong(0);
    private final AtomicLong subBatchesTotal = new AtomicLong(0);
    private final AtomicLong lastActivityNanos = new AtomicLong(Long.MIN_VALUE);
    private final AtomicLong lastChunksPerSecBits =
            new AtomicLong(Double.doubleToLongBits(0.0));

    /** Sentinel meaning "never recorded" — mirrors {@link EmbedProgressGate}'s
     * {@code NEVER_LOGGED} convention. */
    private static final long NEVER = Long.MIN_VALUE;

    EmbedActivityTracker(long activeWindowNanos) {
        if (activeWindowNanos <= 0) {
            throw new IllegalArgumentException(
                "activeWindowNanos must be > 0, got " + activeWindowNanos);
        }
        this.activeWindowNanos = activeWindowNanos;
    }

    /** Record one completed sub-batch. Called unconditionally from {@code
     * embedSubBatched}'s loop, independent of {@link EmbedProgressGate}'s
     * log-line throttling. */
    void record(long chunksInBatch, double chunksPerSec, long nowNanos) {
        chunksDoneTotal.addAndGet(chunksInBatch);
        subBatchesTotal.incrementAndGet();
        lastChunksPerSecBits.set(Double.doubleToLongBits(chunksPerSec));
        lastActivityNanos.set(nowNanos);
    }

    /** A point-in-time view as of {@code nowNanos}. {@code active} is a
     * half-open window: {@code [0, activeWindowNanos)} since the last record
     * counts as active, exactly at or past the boundary does not.
     *
     * @param queueDepth  passed through verbatim into the returned snapshot
     *                    (code-review-expert pass 2 finding c) — this tracker
     *                    has no admission-gate knowledge of its own; the
     *                    caller (Bge768Embedder) reads it and hands it in,
     *                    -1 when no gate is wired.
     * @param threadWidth same pass-through contract as {@code queueDepth}.
     */
    EmbedActivitySnapshot snapshot(long nowNanos, int queueDepth, int threadWidth) {
        long last = lastActivityNanos.get();
        boolean everRecorded = last != NEVER;
        long ageNanos = everRecorded ? nowNanos - last : -1L;
        long ageMs = everRecorded ? ageNanos / 1_000_000L : -1L;
        boolean active = everRecorded && ageNanos < activeWindowNanos;
        return new EmbedActivitySnapshot(
                active, chunksDoneTotal.get(), subBatchesTotal.get(),
                Double.longBitsToDouble(lastChunksPerSecBits.get()), ageMs,
                queueDepth, threadWidth);
    }
}
