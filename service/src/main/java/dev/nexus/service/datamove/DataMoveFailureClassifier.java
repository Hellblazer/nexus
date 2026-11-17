// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.datamove.FailureHandler.Stage;
import dev.nexus.service.db.AdminDdlSession;

import java.sql.SQLException;
import java.util.List;

/**
 * nexus-f3yqd.2.15 (RDR-246, The move worker, the failure classes): tells a transient failure, retried after the move's
 * backoff with the move {@code blocked:}, from a permanent one, which sets {@code failed_at}. Pure: it reads what the worker
 * can read and touches nothing.
 *
 * <p><b>Two lists.</b> A chunk error dml_utils <em>recorded</em> ({@code migration_error}, above the pass's {@code error_id}
 * mark) is transient for 40001, 40P01, 55P03, 57014, class 53 and 57P01 to 57P03: live writers or a busy or restarting host
 * cause these, and a later pass re-claims the boundary. Any other recorded SQLSTATE is permanent, 42501, 42883 and class 08
 * included: a chunk records its own statement's error, so it is the template's fault, never the coordinator's connection.
 * An error the CALL <em>raised</em> with nothing recorded is transient for the same states plus class 08, 42501 (a grant not
 * yet made) and 42883 (an install that vanished between the check and the CALL). Anything else, an unknown or missing
 * SQLSTATE included, is permanent, so nothing loops.
 *
 * <p><b>By stage.</b> A preflight miss the worker names and a connection that cannot be opened are the environment's,
 * always transient; a preflight check that itself raised follows the raised list. A surprise from a query (the tenant probe, the pass itself) follows the raised list. A CALL, and a CALL that returned without
 * completing its run, are read through the recorded rows first. A run dml_utils recorded as cancelled is permanent. A
 * verification that found rows, or whose text the worker refuses, is permanent; one that could not finish is transient when
 * it timed out or carries no error, and otherwise follows the raised list; {@code on_complete} is transient only for its
 * statement timeout (57014) and its lock timeout (55P03).
 */
final class DataMoveFailureClassifier {

    private DataMoveFailureClassifier() { /* static */ }

    /** The decision and the reason an operator reads (the {@code failure} column for a permanent one). */
    record Verdict(boolean permanent, String reason) {
        static Verdict transientFailure(String reason) {
            return new Verdict(false, reason);
        }

        static Verdict permanentFailure(String reason) {
            return new Verdict(true, reason);
        }
    }

    private static boolean inClass(String sqlstate, String prefix) {
        return sqlstate != null && sqlstate.length() == 5 && sqlstate.startsWith(prefix);
    }

    /** Whether a chunk error dml_utils recorded with this SQLSTATE is transient. */
    static boolean transientRecorded(String sqlstate) {
        if (sqlstate == null) {
            return false;
        }
        return switch (sqlstate) {
            case "40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03" -> true;
            default -> inClass(sqlstate, "53");
        };
    }

    /** Whether an error the CALL raised, with no chunk error recorded for it, is transient. */
    static boolean transientRaised(String sqlstate) {
        if (sqlstate == null) {
            return false;
        }
        return transientRecorded(sqlstate) || sqlstate.equals("42501") || sqlstate.equals("42883")
            || inClass(sqlstate, "08");
    }

    /**
     * Whether a raised error with no recorded row yet should be looked at again after a short wait: dml_utils records a chunk's
     * error through an asynchronous background run, so the row for a raised 42501, 42883 or class 08 error may arrive after
     * the CALL did, and it would make the failure permanent (a recorded one of those is the template's own).
     */
    static boolean needsRecorderRecheck(Stage stage, SQLException raised) {
        if (stage != Stage.CALL || raised == null) {
            return false;
        }
        String state = raised.getSQLState();
        return "42501".equals(state) || "42883".equals(state) || inClass(state, "08");
    }

    /**
     * The first {@link SQLException} in the cause chain of {@code t} (itself included), or null. A {@link AdminDdlSession.Refused}
     * has dropped its SQLException: one that names a statement's SQLSTATE yields an exception with it, and the 503
     * {@code admin_connection_failed} of a session that could not be opened yields class 08 (a connection exception), so a
     * flaky connection is never read as an unplaceable surprise.
     */
    static SQLException sqlExceptionOf(Throwable t) {
        for (int depth = 0; t != null && depth < 16; depth++, t = t.getCause()) {
            if (t instanceof SQLException sql) {
                return sql;
            }
            if (t instanceof AdminDdlSession.Refused refused) {
                if (refused.sqlState() != null) {
                    return new SQLException(refused.getMessage(), refused.sqlState());
                }
                if ("admin_connection_failed".equals(refused.reason())) {
                    return new SQLException(refused.getMessage(), "08001");
                }
            }
        }
        return null;
    }

    /**
     * @param stage    where the pass stopped
     * @param raised   what the CALL raised, or the SQLException behind a surprise; null when there is none
     * @param recorded the chunk errors the run recorded since the pass began (empty outside a CALL)
     * @param detail   the worker's own account of the failure, used where the stage says nothing more precise
     */
    static Verdict classify(Stage stage, SQLException raised, List<DataMoveRuns.RunError> recorded, String detail) {
        return switch (stage) {
            // a named miss (no install, missing role, no slot, the admin view) carries no SQLException and is transient; a
            // check that itself raised follows the raised list, so a missing driving schema or table (3F000, 42P01) is permanent
            case PREFLIGHT -> raised == null || transientRaised(raised.getSQLState())
                ? Verdict.transientFailure(detail) : Verdict.permanentFailure(detail);
            case CONNECTION -> Verdict.transientFailure(detail);
            case CANCELLED -> Verdict.permanentFailure(detail);
            // old-shape rows remain, or the verification text is one the worker will not run: no retry changes either
            case VERIFICATION_NONEMPTY, VERIFICATION_REFUSED -> Verdict.permanentFailure(detail);
            // a timeout (57014) or a connection still carrying a tenant (null) can pass; anything else follows the raised list
            case VERIFICATION -> raised == null || transientRaised(raised.getSQLState())
                ? Verdict.transientFailure(detail) : Verdict.permanentFailure(detail);
            // the completion transaction rolled back whole: only its two timeouts are the host's, anything else is the move's
            case ON_COMPLETE -> raised != null && ("55P03".equals(raised.getSQLState()) || "57014".equals(raised.getSQLState()))
                ? Verdict.transientFailure(detail) : Verdict.permanentFailure(detail);
            case PROBE, PASS -> raised != null && transientRaised(raised.getSQLState())
                ? Verdict.transientFailure(detail) : Verdict.permanentFailure(detail);
            case CALL -> recorded.isEmpty()
                ? (raised != null && transientRaised(raised.getSQLState())
                    ? Verdict.transientFailure(detail) : Verdict.permanentFailure(detail))
                : byRecorded(recorded);
            // the CALL returned, so the next pass resumes the run: transient, unless a chunk recorded a fault of its own
            case INCOMPLETE -> recorded.isEmpty() ? Verdict.transientFailure(detail) : byRecorded(recorded);
        };
    }

    private static Verdict byRecorded(List<DataMoveRuns.RunError> recorded) {
        for (DataMoveRuns.RunError e : recorded) {
            if (!transientRecorded(e.sqlstate())) {
                return Verdict.permanentFailure(describe(e));
            }
        }
        return Verdict.transientFailure(describe(recorded.get(recorded.size() - 1)));
    }

    private static String describe(DataMoveRuns.RunError e) {
        return "chunk " + e.boundaryNo() + " recorded sqlstate=" + e.sqlstate() + ": " + FailureHandler.oneLine(e.message());
    }
}
