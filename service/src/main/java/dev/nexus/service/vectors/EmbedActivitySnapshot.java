// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * Bead nexus-s71lr, deliverable 2 — a point-in-time snapshot of {@link
 * Bge768Embedder}'s local-embed activity, read by {@code
 * dev.nexus.service.http.StatusHandler} for {@code GET /v1/status} so a client
 * can poll "is the engine still embedding, or has it hung?" without tailing
 * logs.
 *
 * <p>Lifetime counters, not per-request counters: {@code chunksDoneTotal} and
 * {@code subBatchesTotal} accumulate across the WHOLE process, not just the
 * most recent {@code embed()} call — a single cumulative view is enough to
 * answer "is there recent activity" (via {@code active} / {@code
 * lastActivityAgeMs}) without needing per-call correlation IDs the wire
 * contract does not otherwise carry.
 *
 * @param active            true iff a sub-batch completed within the last
 *                          "active window" (see {@code Bge768Embedder
 *                          .ACTIVE_WINDOW_MS}) — the client-facing liveness
 *                          bit; false either means idle (nothing to embed
 *                          right now) or hung (a caller distinguishes those
 *                          two using its own knowledge of whether a request
 *                          is in flight — this snapshot alone cannot).
 * @param chunksDoneTotal   cumulative chunks embedded since this embedder was
 *                          constructed (process lifetime, not per-call).
 * @param subBatchesTotal   cumulative ONNX sub-batch calls since construction.
 * @param lastChunksPerSec  the throughput rate computed at the most recent
 *                          sub-batch completion (0.0 if none yet).
 * @param lastActivityAgeMs milliseconds since the most recent sub-batch
 *                          completion, or -1 if none has ever completed.
 * @param queueDepth        callers currently blocked waiting for an admission
 *                          permit (read from {@code LocalOnnxAdmission}, never
 *                          recomputed — code-review-expert pass 2 finding c), or
 *                          -1 when no admission gate is wired (e.g. every direct
 *                          test construction of {@code Bge768Embedder}).
 * @param threadWidth       the configured admission-permit ceiling (read from
 *                          {@code LocalOnnxAdmission}), or -1 when no admission
 *                          gate is wired.
 */
public record EmbedActivitySnapshot(
        boolean active,
        long chunksDoneTotal,
        long subBatchesTotal,
        double lastChunksPerSec,
        long lastActivityAgeMs,
        int queueDepth,
        int threadWidth) {
}
