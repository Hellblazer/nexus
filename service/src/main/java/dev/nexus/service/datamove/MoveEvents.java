// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import dev.nexus.service.log.SecretMask;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * nexus-f3yqd.2.18 (RDR-246, Failure Modes, Diagnosis): the engine log's {@code event=data_move_*} lines for the worker's
 * transitions, in the engine's key=value style. A transition is one of: a pass starting on a move, a run completing, the
 * move becoming blocked, failed or complete.
 *
 * <ul>
 *   <li>{@code data_move_started label=}: a pass begins on a move that was not already working (a retry of a blocked move
 *       is a new pass, so it logs one);</li>
 *   <li>{@code data_move_run_complete label= run= tenant=}: one run's CALL returned and the run is complete;</li>
 *   <li>{@code data_move_blocked label= stage= tenant= retries= sqlstate= reason=}: the transition to blocked, to be
 *       retried; {@code retries} is the consecutive transient count after this failure;</li>
 *   <li>{@code data_move_needs_operator label= step= sqlstate= bound_ms= retries= terminates_index_builds= reason=}: ERROR,
 *       once, on the transient failure that enters the needs-an-operator state ({@link MoveRetryPolicy#needsOperator}: the
 *       {@value MoveRetryPolicy#NEEDS_OPERATOR_AT}th consecutive transient, or the
 *       {@value MoveRetryPolicy#NEEDS_OPERATOR_AT_ON_COMPLETE}rd consecutive lock or statement timeout at {@code on_complete});
 *       the move keeps retrying. {@code step} is the stage ({@code on_complete}), {@code bound_ms} the bound that step missed
 *       ({@code -} when no move setting bounds it), {@code terminates_index_builds} whether each retry ends in-flight index
 *       builds (an {@code on_complete} run of bounds on a chunk-family move). Not logged again for a later retry, and logged
 *       again only after the state has been left and is entered anew;</li>
 *   <li>{@code data_move_failed label= stage= tenant= sqlstate= error=}: the transition to failed, for good;</li>
 *   <li>{@code data_move_completed label= on_complete=}: the move is complete in the registry;</li>
 *   <li>{@code data_move_verification_failed label= found=}: a detail line, not a transition. Every verification ran and
 *       old-shape rows remain; {@code found} is the count per tenant and table-wide. The transition it causes is the
 *       {@code data_move_failed} (or {@code blocked}) line with {@code stage=VERIFICATION_NONEMPTY} that follows it.</li>
 * </ul>
 *
 * <p>An absent value (a move with no tenant scope, a failure nothing raised) is {@code -}. Every value has its double quotes
 * turned into single quotes, so a quoted field stays one field.
 *
 * <p><b>What a line can carry.</b> The arguments are the move's label, a tenant id, a run label, a stage name, a SQLSTATE and
 * the one-line reason or error text the failure handler or finisher produced. No method takes a template, a verification or
 * an {@code on_complete} text, and the worker's own texts never include one. The first line of a server error message is
 * passed through, though ({@link FailureHandler#oneLine}), and PostgreSQL can quote a value or an identifier in it: a line
 * may carry a fragment of what the server said, never the statement the worker sent.
 *
 * <p><b>Credentials.</b> The passwords the engine holds (the admin and the application password, set once at startup in
 * {@link dev.nexus.service.log.SecretMask}) are replaced by {@code <redacted>}, case-insensitively and in the forms a driver
 * or server echoes: as is, with a quote doubled or backslash-escaped, and URL-encoded. That covers every value written here;
 * every reason ({@link FailureHandler#oneLine} removes them before it cuts a text to a line, and {@code Disposition},
 * {@code Verdict} and {@code Failure} mask the text they hold, so what the handler writes to {@code failure} and
 * {@code last_transient} is covered too); the state a move is reported with; and, through the log encoder
 * ({@link dev.nexus.service.log.LogMasking}), every line the engine logs with its stack trace and causes. A password under four
 * characters is redacted too, and also masks any unrelated text that contains it. Not covered: a credential the engine does not
 * hold, a form of one not listed (a Base64 or hashed echo), and output that bypasses the logback appenders.
 *
 * <p>{@code stage=FINISH} (a finisher's own {@code FAILED} or {@code BLOCKED} verdict) is reachable only from a test
 * finisher: {@link VerifyingFinisher} reports every failure as {@code REPORT} instead.
 */
final class MoveEvents {

    private static final Logger log = LoggerFactory.getLogger(MoveEvents.class);

    private MoveEvents() { /* static */ }

    /** The value in a field: {@code -} when absent, and no double quote that would end a quoted field early. */
    static String value(String v) {
        return v == null || v.isEmpty() ? "-" : SecretMask.mask(v).replace('"', '\'');
    }

    static void started(String label) {
        log.info("event=data_move_started label=\"{}\"", value(label));
    }

    static void runComplete(String label, String run, String tenant) {
        log.info("event=data_move_run_complete label=\"{}\" run=\"{}\" tenant=\"{}\"", value(label), value(run), value(tenant));
    }

    static void blocked(String label, String stage, String tenant, int retries, String sqlstate, String reason) {
        log.warn("event=data_move_blocked label=\"{}\" stage={} tenant=\"{}\" retries={} sqlstate={} reason=\"{}\"",
            value(label), value(stage), value(tenant), retries, value(sqlstate), value(reason));
    }

    static void needsOperator(String label, String step, String sqlstate, int boundMs, int retries,
                              boolean terminatesIndexBuilds, String reason) {
        log.error("event=data_move_needs_operator label=\"{}\" step={} sqlstate={} bound_ms={} retries={} "
                + "terminates_index_builds={} reason=\"{}\"", value(label), value(step), value(sqlstate),
            boundMs < 0 ? "-" : Integer.toString(boundMs), retries, terminatesIndexBuilds, value(reason));
    }

    static void verificationFailed(String label, String found) {
        log.warn("event=data_move_verification_failed label=\"{}\" found=\"{}\"", value(label), value(found));
    }

    static void failed(String label, String stage, String tenant, String sqlstate, String error) {
        log.error("event=data_move_failed label=\"{}\" stage={} tenant=\"{}\" sqlstate={} error=\"{}\"", value(label),
            value(stage), value(tenant), value(sqlstate), value(error));
    }

    static void completed(String label, boolean onComplete) {
        log.info("event=data_move_completed label=\"{}\" on_complete={}", value(label), onComplete);
    }
}
