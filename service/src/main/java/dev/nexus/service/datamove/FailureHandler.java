// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;

import java.sql.SQLException;

/**
 * Where every failure of a pass is decided. Each way a pass can stop, whatever the stage, comes here as one {@link Failure}
 * and goes back as one {@link Disposition}; the worker never reports a move blocked or failed on its own. This bead's
 * handler ({@link #BLOCK_ALWAYS}) reports every failure {@code blocked:} with the reason; the failure-class bead
 * (nexus-f3yqd.2.15) supplies the one that tells a transient error from a permanent one by the SQLSTATE lists and the
 * {@code error_id} mark, counts retries and sets {@code failed_at}.
 */
public interface FailureHandler {

    /** Where in a pass the failure arose. */
    enum Stage {
        /** Step 1: the library install, a grant, the installed entry point, the admin view or a worker slot. */
        PREFLIGHT,
        /** The tenant list or the catalog could not be read. */
        PROBE,
        /** The dedicated connection could not be opened. */
        CONNECTION,
        /** A CALL raised; {@link Failure#raised} is what it raised. */
        CALL,
        /** A CALL returned and the run's active row has {@code cancelled_at} set; {@link Failure#detail} says when. */
        CANCELLED,
        /** A CALL returned and the run is neither complete nor cancelled. */
        INCOMPLETE,
        /**
         * A verification run could not be completed (nexus-f3yqd.2.14): the query raised ({@link Failure#raised} is what it
         * raised, 57014 for {@code statement_timeout_ms}), or a connection it needs could not be shown clean
         * ({@code raised} null, {@link Failure#detail} says why). {@link Failure#tenant} names the tenant of the run, or
         * is null for the table-wide run.
         */
        VERIFICATION,
        /**
         * The verification text itself is not one the worker will run (nexus-f3yqd.2.14): a {@code ?}, a {@code $}-digit that
         * is not a standalone {@code $1}, or a {@code $1} the driver does not count as a parameter. {@link Failure#raised}
         * is null and {@link Failure#detail} names the rule. Permanent by nature: no retry changes the text. Distinct from
         * {@link #VERIFICATION}, whose causes (a timeout, a connection that carries a tenant) can pass.
         */
        VERIFICATION_REFUSED,
        /**
         * Every verification ran and at least one returned rows (nexus-f3yqd.2.14): old-shape rows remain.
         * {@link Failure#raised} is null and {@link Failure#detail} carries the count per tenant and table-wide.
         */
        VERIFICATION_NONEMPTY,
        /**
         * {@code on_complete} and the {@code completed_at} UPDATE ran in one transaction and it failed (nexus-f3yqd.2.14):
         * {@link Failure#raised} is what raised (57014 for {@code statement_timeout_ms}, 55P03 for its {@code lock_timeout}),
         * and neither took effect.
         */
        ON_COMPLETE,
        /** Anything else the pass did not expect (a surprise from a query, say). */
        PASS
    }

    /** What the worker does with the move after a failure. */
    enum Kind {
        /** The pass ends and the move is retried after its wait. */
        BLOCKED,
        /** The pass ends and the move is not retried by this process. The handler is the one that sets {@code failed_at}. */
        FAILED
    }

    /** The decision: the move is reported with {@code reason}, as {@code blocked:} or {@code failed:}. */
    record Disposition(Kind kind, String reason) {
        public static Disposition blocked(String reason) {
            return new Disposition(Kind.BLOCKED, reason);
        }

        public static Disposition failed(String reason) {
            return new Disposition(Kind.FAILED, reason);
        }
    }

    /**
     * One failure, with everything the worker knows of where it arose.
     *
     * @param move          the move
     * @param stage         where the pass stopped
     * @param runLabel      the dml_utils run the pass was at; null before any run
     * @param drivingSchema the relation the run's CALL named as its driving table (a leaf, for a per-leaf run); the move's
     *                      driving schema otherwise
     * @param drivingTable  that relation's name
     * @param tenant        the tenant whose session the run used; null for a table with no tenant scope
     * @param errorMark     the run's highest {@code error_id} held before the CALL (0 outside a CALL); an error row above
     *                      it is this pass's recorded chunk error
     * @param raised        what the CALL raised; null for any other stage
     * @param detail        the worker's own account of the failure, one line
     * @param admin         the admin connection of the pass, to read {@code migration_error} with; null when the pass
     *                      could not open one
     */
    record Failure(DataMove move, Stage stage, String runLabel, String drivingSchema, String drivingTable,
                   String tenant, long errorMark, SQLException raised, String detail, DSLContext admin) { }

    Disposition onFailure(Failure failure);

    /** Reports every failure as {@code blocked:} with the error text; nothing is made permanent. */
    FailureHandler BLOCK_ALWAYS = failure -> failure.stage() == Stage.CALL
        ? Disposition.blocked("run " + failure.runLabel() + " raised sqlstate=" + failure.raised().getSQLState() + ": "
            + oneLine(failure.raised().getMessage()))
        : Disposition.blocked(failure.detail());

    /** The first line of an error text, bounded, for a reason. */
    static String oneLine(String text) {
        if (text == null) {
            return "";
        }
        String line = text.strip().split("\\R", 2)[0];
        return line.length() <= 300 ? line : line.substring(0, 300) + "...";
    }
}
