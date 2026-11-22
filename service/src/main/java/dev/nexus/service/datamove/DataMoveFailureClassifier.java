// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.datamove.FailureHandler.Stage;
import dev.nexus.service.db.AdminDdlSession;
import dev.nexus.service.log.SecretMask;

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
 * it timed out (57014), lost its connection (class 08), met a shutdown or startup (57P01 to 57P03), ran out of resources
 * (class 53) or carries no error, and is otherwise permanent, 42501 and 42883 included, since a verification text that names
 * a missing function or an ungranted table is the text's own fault; a verification connection that cannot be opened is a
 * {@link Stage#CONNECTION} failure, always transient. {@code on_complete} is transient for those same errors plus its lock
 * timeout (55P03), a deadlock (40P01) and a serialization failure (40001): its completion transaction rolls back whole, so a
 * retry is safe. Everything else, no error included, is permanent.
 */
final class DataMoveFailureClassifier {

    private DataMoveFailureClassifier() { /* static */ }

    /** The decision and the reason an operator reads (the {@code failure} column for a permanent one). */
    record Verdict(boolean permanent, String reason, String sqlstate) {
        Verdict {
            reason = SecretMask.mask(reason);
        }

        static Verdict transientFailure(String reason) {
            return new Verdict(false, reason, null);
        }

        static Verdict permanentFailure(String reason) {
            return new Verdict(true, reason, null);
        }

        /** The same decision resting on {@code state} (for the engine log); a decision that already has one keeps it. */
        Verdict resting(String state) {
            return sqlstate != null ? this : new Verdict(permanent, reason, state);
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
     * Whether a failure of a verification or {@code on_complete} statement is the host's: the statement timeout (57014), a lost
     * connection (class 08), a shutdown or startup (57P01 to 57P03) and a busy host (class 53). {@code on_complete} also takes
     * its lock timeout (55P03), a deadlock (40P01) and a serialization failure (40001), which live writers cause during its
     * DDL; its transaction rolls back whole, so a retry is safe. A verification is a READ ONLY SELECT at the server's default
     * isolation with no lock_timeout, so in the shipped session it cannot raise 40001 or 55P03 (a lock wait ends at the
     * statement timeout, 57014) and 40P01 needs a lock cycle with DDL; those three are kept permanent there as unreachable,
     * not because they are the text's fault.
     */
    static boolean transientSession(String sqlstate, boolean onComplete) {
        if (sqlstate == null) {
            return false;
        }
        return switch (sqlstate) {
            case "57014", "57P01", "57P02", "57P03" -> true;
            case "55P03", "40P01", "40001" -> onComplete;
            default -> inClass(sqlstate, "08") || inClass(sqlstate, "53");
        };
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
        // the SQLSTATE a decision rests on is the recorded chunk error's when there is one (byRecorded), else what was raised
        return decide(stage, raised, recorded, detail).resting(raised == null ? null : raised.getSQLState());
    }

    private static Verdict decide(Stage stage, SQLException raised, List<DataMoveRuns.RunError> recorded, String detail) {
        return switch (stage) {
            // a named miss (no install, missing role, no slot, the admin view) carries no SQLException and is transient; a
            // check that itself raised follows the raised list, so a missing driving schema or table (3F000, 42P01) is permanent
            case PREFLIGHT -> raised == null || transientRaised(raised.getSQLState())
                ? Verdict.transientFailure(detail) : Verdict.permanentFailure(detail);
            case CONNECTION -> Verdict.transientFailure(detail);
            case CANCELLED -> Verdict.permanentFailure(detail);
            // old-shape rows remain, or the verification text is one the worker will not run: no retry changes either
            case VERIFICATION_NONEMPTY, VERIFICATION_REFUSED -> Verdict.permanentFailure(detail);
            // a connection still carrying a tenant (null), the statement timeout, a lost connection, a shutdown or a busy host
            // can pass; any other error is the verification text's own (a missing function, an ungranted table): permanent
            case VERIFICATION -> raised == null || transientSession(raised.getSQLState(), false)
                ? Verdict.transientFailure(detail) : Verdict.permanentFailure(detail);
            // the completion transaction rolled back whole, so a lock or statement timeout, a deadlock, a serialization failure,
            // a lost connection, a shutdown or a busy host retries safely; anything else, no error included, is the move's
            case ON_COMPLETE -> raised != null && transientSession(raised.getSQLState(), true)
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
                return new Verdict(true, describe(e), e.sqlstate());
            }
        }
        DataMoveRuns.RunError latest = recorded.get(recorded.size() - 1);
        return new Verdict(false, describe(latest), latest.sqlstate());
    }

    private static String describe(DataMoveRuns.RunError e) {
        return "chunk " + e.boundaryNo() + " recorded sqlstate=" + e.sqlstate() + ": " + FailureHandler.oneLine(e.message());
    }
}
