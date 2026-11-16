// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import org.jooq.DSLContext;

import java.sql.SQLException;
import java.time.OffsetDateTime;

/**
 * Where a failed or cancelled run is decided. This bead's pass stops the move on either and reports it
 * {@code blocked:} with the reason ({@link #BLOCK_ALWAYS}); the failure-class bead (nexus-f3yqd.2.15) supplies the
 * implementation that tells a transient error from a permanent one by the SQLSTATE lists and the {@code error_id} mark,
 * counts retries, and sets {@code failed_at}.
 */
public interface FailureHandler {

    /** What the worker does with the move after a failure. */
    enum Kind { BLOCKED }

    /** The decision: the move's pass ends and the move is reported with {@code reason}. */
    record Disposition(Kind kind, String reason) {
        public static Disposition blocked(String reason) {
            return new Disposition(Kind.BLOCKED, reason);
        }
    }

    /**
     * A CALL raised.
     *
     * @param move      the move
     * @param runLabel  the dml_utils run the CALL named
     * @param errorMark the run's highest {@code error_id} held before the CALL; an error row above it is this pass's
     * @param raised    what the CALL raised
     * @param admin     the admin connection, to read {@code migration_error} with
     */
    Disposition onCallRaised(DataMove move, String runLabel, long errorMark, SQLException raised, DSLContext admin);

    /** A CALL returned, and the run's active row has {@code cancelled_at} set (dml_utils' {@code cancel_migration_run}). */
    Disposition onRunCancelled(DataMove move, String runLabel, OffsetDateTime cancelledAt);

    /** Reports every failure as {@code blocked:} with the error text; nothing is retried differently or made permanent. */
    FailureHandler BLOCK_ALWAYS = new FailureHandler() {
        @Override
        public Disposition onCallRaised(DataMove move, String runLabel, long errorMark, SQLException raised,
                                        DSLContext admin) {
            return Disposition.blocked("run " + runLabel + " raised sqlstate=" + raised.getSQLState() + ": "
                + oneLine(raised.getMessage()));
        }

        @Override
        public Disposition onRunCancelled(DataMove move, String runLabel, OffsetDateTime cancelledAt) {
            return Disposition.blocked("run " + runLabel + " cancelled at " + cancelledAt);
        }
    };

    /** The first line of an error text, bounded, for a reason. */
    static String oneLine(String text) {
        if (text == null) {
            return "";
        }
        String line = text.strip().split("\\R", 2)[0];
        return line.length() <= 300 ? line : line.substring(0, 300) + "...";
    }
}
