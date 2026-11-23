// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.time.Duration;
import java.time.Instant;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * nexus-f3yqd.2.15 (RDR-246, Retries, pause and cancelled runs): the cadence of a blocked move. Computed from the registry
 * row ({@code transient_retries}, {@code last_transient_at}), so no timer state is kept and a restart changes nothing.
 * The worker never stops retrying; past {@link #NEEDS_OPERATOR_AT} consecutive transients the text says so, the engine logs
 * {@code event=data_move_needs_operator} once, on the failure that reaches it ({@link #reachesNeedsOperator}), and
 * {@code /v1/status} lists the move for as long as {@link #needsOperator(int, int)} holds. The state is entered at
 * {@link #NEEDS_OPERATOR_AT} consecutive transients, or at {@link #NEEDS_OPERATOR_AT_ON_COMPLETE} consecutive lock or statement
 * timeouts at {@code on_complete}.
 */
final class MoveRetryPolicy {

    private MoveRetryPolicy() { /* static */ }

    static final Duration FIRST = Duration.ofSeconds(30);
    static final Duration CAP = Duration.ofMinutes(15);
    /** The exponent stops growing here: 30 s * 2^5 is already past the cap. */
    private static final int MAX_DOUBLINGS = 5;
    /** From this many consecutive transients {@code blocked:} adds "needs an operator". */
    static final int NEEDS_OPERATOR_AT = 20;
    /**
     * From this many consecutive {@code on_complete} transients with 55P03 or 57014 the move needs an operator at once
     * (linda, tamsin's analysis): each such retry re-runs the full-heap warm-up and, for a move over the chunk family, ends every
     * in-flight index build ({@code PciBuilders.terminateOrThrow}: PCI, leaf, id-unique), so waiting for
     * {@link #NEEDS_OPERATOR_AT} would keep killing builds for hours.
     */
    static final int NEEDS_OPERATOR_AT_ON_COMPLETE = 3;
    /** The words a reason carries, before "needs an operator", when each retry ends in-flight index builds. */
    static final String INDEX_BUILDS_NOTE = "each retry ends in-flight index builds (PCI, leaf, id-unique)";

    /** min(30 s * 2^(min(N, 6) - 1), 15 min); zero for a move with no retries. */
    static Duration backoff(int retries) {
        if (retries <= 0) {
            return Duration.ZERO;
        }
        Duration wait = FIRST.multipliedBy(1L << Math.min(retries - 1, MAX_DOUBLINGS));
        return wait.compareTo(CAP) > 0 ? CAP : wait;
    }

    /** Whether a move whose last transient failure was at {@code last}, the {@code retries}th, may be worked at {@code now}. */
    static boolean due(Instant last, int retries, Instant now) {
        return last == null || !now.isBefore(last.plus(backoff(retries)));
    }

    /** {@link #blockedReason(int, int, boolean, Instant, String)} for a failure that is not a run of {@code on_complete} bounds. */
    static String blockedReason(int retries, Instant last, String error) {
        return blockedReason(retries, 0, false, last, error);
    }

    /**
     * What {@code blocked:} shows: the count, the time of the latest transient error and the error, then, for a move in the
     * needs-an-operator state ({@link #needsOperator}), {@link #INDEX_BUILDS_NOTE} when {@code indexBuilds} (it is, and the run
     * is what put it there) and "needs an operator" last.
     */
    static String blockedReason(int retries, int onCompleteRun, boolean indexBuilds, Instant last, String error) {
        String base = "retry " + retries + ", last " + last + ": " + error;
        if (indexBuilds && onCompleteRun >= NEEDS_OPERATOR_AT_ON_COMPLETE) {
            base += "; " + INDEX_BUILDS_NOTE;
        }
        return needsOperator(retries, onCompleteRun) ? base + "; needs an operator" : base;
    }

    /**
     * Whether a failure is the kind whose every retry costs index builds: it arose at {@code on_complete} and is a lock timeout
     * (55P03) or a statement timeout (57014).
     */
    static boolean isOnCompleteBound(FailureHandler.Stage stage, String sqlstate) {
        return stage == FailureHandler.Stage.ON_COMPLETE && ("55P03".equals(sqlstate) || "57014".equals(sqlstate));
    }

    /**
     * THE rule, in one place: a move needs an operator from {@link #NEEDS_OPERATOR_AT} consecutive transients of any kind, or
     * from {@link #NEEDS_OPERATOR_AT_ON_COMPLETE} consecutive ones that are {@link #isOnCompleteBound}. {@code retries} is the
     * row's {@code transient_retries}; {@code onCompleteRun} is the length of the unbroken run of such failures that ends at the
     * latest transient ({@link OnCompleteRuns}). The log event, {@link #blockedReason}, the registry-row read
     * ({@link #needsOperator(DataMove)}) and so {@code /v1/status} and {@code nx doctor} all go through it.
     */
    static boolean needsOperator(int retries, int onCompleteRun) {
        return retries >= NEEDS_OPERATOR_AT || onCompleteRun >= NEEDS_OPERATOR_AT_ON_COMPLETE;
    }

    /**
     * Whether a failure that makes the counts {@code retries} and {@code onCompleteRun} is the one that enters the state:
     * the state held after it and did not hold before ({@code retriesBefore}, {@code runBefore}).
     */
    static boolean entersNeedsOperator(int retriesBefore, int runBefore, int retries, int onCompleteRun) {
        return needsOperator(retries, onCompleteRun) && !needsOperator(retriesBefore, runBefore);
    }

    /**
     * The runs of {@link #isOnCompleteBound} failures, per move, as this process has seen them. The registry row keeps one
     * count of consecutive transients and not the stage or SQLSTATE of each, so the run is held here: a transient extends it
     * only when it is itself a bound failure AND it is the next one after the last this object saw (the row's count went up by
     * exactly one); any other transient, a reset of the row's count, or a restart of the engine, starts it again. So "N in a
     * row" means: the latest N transients this engine process has seen for the move, with no other kind between them.
     */
    static final class OnCompleteRuns {
        private record Run(int retries, int count) { }

        /** The run before a failure and after it. */
        record Step(int before, int run) { }

        private final Map<String, Run> byLabel = new ConcurrentHashMap<>();

        /** Records the transient that made the row's count {@code retries}; {@code bound} is whether it is a bound failure. */
        Step record(String label, int retries, boolean bound) {
            Run prior = byLabel.get(label);
            int before = prior != null && prior.retries() == retries - 1 ? prior.count() : 0;
            int run = bound ? before + 1 : 0;
            byLabel.put(label, new Run(retries, run));
            return new Step(before, run);
        }
    }

    /**
     * Whether an open move's ROW is in the needs-an-operator state by the 20-retries rule: the count has reached
     * {@link #NEEDS_OPERATOR_AT} and the move is not paused (a paused move is one an operator already holds). Read from the
     * row, so a restart changes nothing. The run of {@code on_complete} bounds is not in the row, so that rule is read from
     * {@link MoveStates.State#needsOperator()} as well ({@link MoveBootWork}); this is the persisted half.
     */
    static boolean needsOperator(DataMove move) {
        return !move.paused() && needsOperator(move.transientRetries(), 0) && move.lastTransientAt() != null;
    }

    /** The step a failure arose in, as the event and the doctor name it: the stage in lower case ({@code on_complete}). */
    static String step(FailureHandler.Stage stage) {
        return stage.name().toLowerCase(Locale.ROOT);
    }

    /**
     * The bound in milliseconds that the failing step missed, or -1 when no move setting bounds that step: the completion's
     * {@code statement_timeout_ms} for {@code on_complete} and its re-check (the lock timeout, half of it, when the SQLSTATE
     * is 55P03), {@code verify_statement_timeout_ms} for the first verification pass.
     */
    static int boundMs(FailureHandler.Stage stage, DataMove move, String sqlstate) {
        return switch (stage) {
            case ON_COMPLETE, RECHECK -> "55P03".equals(sqlstate)
                ? VerifyingFinisher.lockTimeoutMs(move.statementTimeoutMs()) : move.statementTimeoutMs();
            case VERIFICATION -> move.verifyStatementTimeoutMs();
            default -> -1;
        };
    }
}
