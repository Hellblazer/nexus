// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

/**
 * What the move worker says about each move as it works. The in-memory {@link MoveStates} is the one implementation:
 * boot_work and the nx doctor line (nexus-f3yqd.2.17) read it, never {@code nexus.data_moves} on the serving pool, since
 * {@code nexus_svc} holds no grant there. {@code blocked} and {@code failed} carry the reason an operator reads after
 * {@code blocked:} or {@code failed:}.
 */
public interface MoveReporter {

    /** A pass over the move has begun. */
    void working(DataMove move);

    /** The pass stopped for a reason an operator reads; the move is retried after its wait. */
    void blocked(DataMove move, String reason);

    /** The move failed for good; it stays reported until a move supersedes it. */
    void failed(DataMove move, String reason);

    /** The move is paused ({@code paused_at} set): open, and not worked. */
    void paused(DataMove move);

    /** Every run of every listed tenant is complete; verification and {@code on_complete} are the finisher's. */
    void runsComplete(DataMove move);

    /** A CALL for the run has begun on the dedicated connection whose server backend is {@code pid}. */
    void callStarted(DataMove move, String runLabel, int pid);

    /** The CALL has returned or raised. */
    void callEnded(DataMove move);

    /** The move is no longer open (completed, or superseded) and nothing more is to be said of it. */
    void forget(String label);
}
