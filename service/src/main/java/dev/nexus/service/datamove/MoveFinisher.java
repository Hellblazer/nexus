// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;

import java.util.List;

/**
 * What happens once every run of every listed tenant is complete: the per-tenant and table-wide verification and
 * {@code on_complete} with the UPDATE that sets {@code completed_at} (RDR-246, The move worker step 3). That is the
 * verification bead (nexus-f3yqd.2.14); this bead's pass ends where the runs end.
 */
public interface MoveFinisher {

    /**
     * The result of {@link #finish}. {@code closed} is true when the move has left the open set (completed or failed in
     * the registry); a move that is not closed and carries no {@code blockedReason} is left alone for the rest of this
     * process, since its runs are done and there is nothing more for the pass to do.
     */
    record Result(boolean closed, String blockedReason) {
        public static Result completed() {
            return new Result(true, null);
        }

        public static Result blocked(String reason) {
            return new Result(false, reason);
        }

        public static Result leftOpen() {
            return new Result(false, null);
        }
    }

    /**
     * @param move    the move whose runs are all complete
     * @param tenants the tenant list this pass ran the move for (empty for a table with no tenant column)
     * @param admin   the admin connection
     */
    Result finish(DataMove move, List<String> tenants, DSLContext admin);

    /** Does nothing: the runs are done and the move stays open. */
    MoveFinisher RUNS_ONLY = (move, tenants, admin) -> Result.leftOpen();
}
