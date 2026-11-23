// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.db.AdminConnection;
import org.jooq.DSLContext;

import java.util.List;
import java.util.function.BooleanSupplier;

/**
 * What happens once every run of every listed tenant is complete: the per-tenant and table-wide verification and
 * {@code on_complete} with the UPDATE that sets {@code completed_at} (RDR-246, The move worker step 3).
 * {@link VerifyingFinisher} (nexus-f3yqd.2.14) is the one an engine runs; {@link #RUNS_ONLY} ends the pass where the runs
 * end.
 *
 * <p><b>The seam's rule.</b> A finisher never writes {@code failed_at}, {@code failure}, {@code transient_retries},
 * {@code last_transient_at} or {@code last_transient}. What cannot finish goes back as {@link Outcome#REPORT} with a
 * {@link FailureHandler.Failure}, and {@link FailureHandler} is the single decision point (blocked or failed, and whatever
 * those columns then hold). The one registry write a finisher makes is the {@code completed_at} UPDATE, in the same
 * transaction as {@code on_complete}, because a stop between the two must leave neither.
 */
public interface MoveFinisher {

    /**
     * What the finisher is given: the move, the tenants the pass ran it for (empty for a table with no tenant scope), the
     * admin connection of the pass, and the admin values, so it can open the dedicated per-tenant connections the
     * verification runs on and the separate table-wide one ({@link CallConnection#open}); {@code stopped} says whether the
     * worker has been stopped, which a finisher reads between its steps and answers with {@link Outcome#LEFT_OPEN}.
     */
    record Context(DataMove move, List<String> tenants, DSLContext admin, AdminConnection adminConnection,
                   String applicationName, BooleanSupplier stopped, MoveReporter reporter) {

        public Context(DataMove move, List<String> tenants, DSLContext admin, AdminConnection adminConnection,
                       String applicationName, BooleanSupplier stopped) {
            this(move, tenants, admin, adminConnection, applicationName, stopped, null);
        }

        public Context(DataMove move, List<String> tenants, DSLContext admin, AdminConnection adminConnection,
                       String applicationName) {
            this(move, tenants, admin, adminConnection, applicationName, () -> false, null);
        }
    }

    enum Outcome {
        /** The move is complete in the registry ({@code completed_at} set). */
        COMPLETED,
        /** The move failed for good: a non-empty verification, or an {@code on_complete} that failed for another reason. */
        FAILED,
        /** Not now: a timeout or a lock; the move is retried after its wait. */
        BLOCKED,
        /** The runs are done and there is nothing more for this pass to do; the move is left alone for this process. */
        LEFT_OPEN,
        /** The finisher could not finish: the worker hands {@link Result#failure} to the {@link FailureHandler}, which decides. */
        REPORT,
        /** A pause was set before verification or before {@code on_complete}: nothing ran past it; {@link Result#move} is the paused row. */
        PAUSED,
        /** The move is no longer open (superseded, failed or completed meanwhile): the worker forgets it. */
        GONE,
        /** The move completed and its {@code on_complete} put it back ({@code completed_at} NULL again): not a completion, no event; the worker comes back to it. */
        REOPENED
    }

    /**
     * The outcome and, for {@link Outcome#FAILED} and {@link Outcome#BLOCKED}, the reason an operator reads;
     * {@link Outcome#REPORT} carries the {@link FailureHandler.Failure} and {@link Outcome#PAUSED} the paused row.
     */
    record Result(Outcome outcome, String reason, FailureHandler.Failure failure, DataMove move) {
        public static Result completed() {
            return new Result(Outcome.COMPLETED, null, null, null);
        }

        public static Result failed(String reason) {
            return new Result(Outcome.FAILED, reason, null, null);
        }

        public static Result blocked(String reason) {
            return new Result(Outcome.BLOCKED, reason, null, null);
        }

        public static Result leftOpen() {
            return new Result(Outcome.LEFT_OPEN, null, null, null);
        }

        public static Result report(FailureHandler.Failure failure) {
            return new Result(Outcome.REPORT, failure.detail(), failure, null);
        }

        public static Result paused(DataMove move) {
            return new Result(Outcome.PAUSED, null, null, move);
        }

        public static Result gone() {
            return new Result(Outcome.GONE, null, null, null);
        }

        public static Result reopened() {
            return new Result(Outcome.REOPENED, null, null, null);
        }
    }

    Result finish(Context context);

    /** Does nothing: the runs are done and the move stays open. */
    /** Cancels a completion statement in flight (the engine is stopping); a finisher with none does nothing. */
    default void cancelInFlight() { }

    MoveFinisher RUNS_ONLY = context -> Result.leftOpen();
}
