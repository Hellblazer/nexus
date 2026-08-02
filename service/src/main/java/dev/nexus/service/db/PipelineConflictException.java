// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.time.Duration;
import java.time.OffsetDateTime;

/**
 * A {@link PipelineRepository#create} retry hit a {@code running} pipeline row whose
 * heartbeat is still fresh (younger than {@link PipelineRepository#STALE_THRESHOLD}) —
 * nexus-lcmbp.
 *
 * <p>Prior behaviour returned {@code "skip"} for this case, identical on the wire to
 * every other short-circuit. The client exited {@code rc=0} having written zero
 * chunks — a silent no-op reported as success. Worse, the SAME retry was a loud
 * {@code "resuming"} failure once the row's heartbeat aged past the threshold, so the
 * observable contract was time-dependent: whether a stranded row was reported as
 * success or failure depended only on how long ago it stranded, never on anything the
 * caller could see. A remediation driver checking return codes would mark a
 * still-corrupt document remediated.
 *
 * <p>This is the loud alternative: the row is still owned by another (possibly
 * genuinely live) run, and {@code create} refuses rather than pretends to have done
 * the caller's work. {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} maps
 * this to an HTTP 409 naming the stranded row and the remedy — wait out the resume
 * window, or investigate directly via {@code GET /v1/pipeline/state}.
 */
public class PipelineConflictException extends RuntimeException {

    private final String contentHash;
    private final OffsetDateTime startedAt;
    private final long heartbeatAgeSeconds;
    private final long staleThresholdSeconds;

    // nexus-lcmbp fix-list #5: GET /v1/pipeline/state is an authed ENGINE route
    // with no `nx` verb — the prior remedy text read as CLI-actionable but is
    // not. This text must stay TEXTUALLY IDENTICAL to the "remedy" JSON literal
    // in HttpUtil.sendTypedDbError's pipelineConflict branch: the client
    // (HttpPipelineDB.create_pipeline / PipelineConflictRunning) dedups by
    // checking `remedy in error` before appending — if the two literals drift,
    // the client's user-facing message doubles up the remedy text.
    public PipelineConflictException(String contentHash, OffsetDateTime startedAt,
                                      Duration heartbeatAge, Duration staleThreshold) {
        super("pipeline for content_hash=" + contentHash + " is already running "
              + "(last heartbeat " + heartbeatAge.toSeconds() + "s ago; resumable once "
              + "the heartbeat exceeds " + staleThreshold.toSeconds() + "s) — "
              + "wait for the resume window (retry after the heartbeat exceeds "
              + "the stale threshold) or inspect the pipeline row via "
              + "GET /v1/pipeline/state (engine route; requires service auth)");
        this.contentHash = contentHash;
        this.startedAt = startedAt;
        this.heartbeatAgeSeconds = heartbeatAge.toSeconds();
        this.staleThresholdSeconds = staleThreshold.toSeconds();
    }

    /** The content_hash of the stranded row (the create() caller's own key). */
    public String contentHash() {
        return contentHash;
    }

    /** When the pipeline run that currently owns the row began. */
    public OffsetDateTime startedAt() {
        return startedAt;
    }

    /** Seconds since the row's last heartbeat (updated_at) — what is compared
     *  against the stale threshold. */
    public long heartbeatAgeSeconds() {
        return heartbeatAgeSeconds;
    }

    /** {@link PipelineRepository#STALE_THRESHOLD}, in seconds, for the client to
     *  compute its own remaining wait. */
    public long staleThresholdSeconds() {
        return staleThresholdSeconds;
    }
}
