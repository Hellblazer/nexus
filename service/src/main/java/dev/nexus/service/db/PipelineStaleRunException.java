// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A pipeline write carried a {@code run_epoch} that is no longer the row's
 * (nexus-8vu8p): another client took the run over (the failed/stale
 * "resuming" branch of {@link PipelineRepository#create}, or its
 * completed-leftover reset) since this caller's own create. Nothing was
 * written. {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} maps this
 * to an HTTP 409 {@code stale_run} naming both epochs and the remedy: the
 * stale run stops, and does not mark the row failed or wipe its WAL, since
 * both now belong to the new owner.
 */
public class PipelineStaleRunException extends RuntimeException {

    /** Textually identical to the {@code remedy} literal HttpUtil puts on the
     *  wire: the client dedups by checking {@code remedy in error}. */
    public static final String REMEDY =
        "this run was taken over by a newer resume of the same document; stop "
        + "without marking the row failed or clearing its WAL (the new owner "
        + "holds both) and re-run the document if the new owner does not finish";

    private final long pipelineId;
    private final String contentHash;
    private final int runEpoch;
    private final int currentEpoch;

    public PipelineStaleRunException(long pipelineId, String contentHash, int runEpoch, int currentEpoch) {
        super("pipeline_id=" + pipelineId + " (content_hash=" + contentHash + ") is at run_epoch "
              + currentEpoch + ", this write carried " + runEpoch + " — " + REMEDY);
        this.pipelineId = pipelineId;
        this.contentHash = contentHash;
        this.runEpoch = runEpoch;
        this.currentEpoch = currentEpoch;
    }

    public long pipelineId() { return pipelineId; }
    public String contentHash() { return contentHash; }
    /** The epoch the refused write carried. */
    public int runEpoch() { return runEpoch; }
    /** The row's epoch now. */
    public int currentEpoch() { return currentEpoch; }
}
