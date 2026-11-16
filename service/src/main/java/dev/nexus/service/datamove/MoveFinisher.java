// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import org.jooq.DSLContext;

import java.util.List;

/**
 * What happens once every run of every listed tenant is complete: the per-tenant and table-wide verification and
 * {@code on_complete} with the UPDATE that sets {@code completed_at} (RDR-246, The move worker step 3). That is the
 * verification bead (nexus-f3yqd.2.14); this bead's pass ends where the runs end.
 */
public interface MoveFinisher {

    /**
     * What the finisher is given: the move, the tenants the pass ran it for (empty for a table with no tenant scope), the
     * admin connection of the pass, and the admin values, so it can open the dedicated per-tenant connections the
     * verification runs on and the separate table-wide one ({@link CallConnection#open}).
     */
    record Context(DataMove move, List<String> tenants, DSLContext admin, AdminConnection adminConnection,
                   String applicationName) { }

    /** How the finish ended. */
    enum Outcome {
        /** The move is complete in the registry ({@code completed_at} set). */
        COMPLETED,
        /** The move failed for good: a non-empty verification, or an {@code on_complete} that failed for another reason. */
        FAILED,
        /** Not now: a timeout or a lock; the move is retried after its wait. */
        BLOCKED,
        /** The runs are done and there is nothing more for this pass to do; the move is left alone for this process. */
        LEFT_OPEN
    }

    /** The outcome and, for {@link Outcome#FAILED} and {@link Outcome#BLOCKED}, the reason an operator reads. */
    record Result(Outcome outcome, String reason) {
        public static Result completed() {
            return new Result(Outcome.COMPLETED, null);
        }

        public static Result failed(String reason) {
            return new Result(Outcome.FAILED, reason);
        }

        public static Result blocked(String reason) {
            return new Result(Outcome.BLOCKED, reason);
        }

        public static Result leftOpen() {
            return new Result(Outcome.LEFT_OPEN, null);
        }
    }

    Result finish(Context context);

    /** Does nothing: the runs are done and the move stays open. */
    MoveFinisher RUNS_ONLY = context -> Result.leftOpen();
}
