// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.time.Duration;
import java.time.Instant;
import java.util.Collection;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * nexus-f3yqd.2.15 (RDR-246, Retries, pause and cancelled runs): the cadence of a blocked move. Computed from the registry
 * row ({@code transient_retries}, {@code last_transient_at}), so no timer state is kept and a restart changes nothing.
 * The worker never stops retrying; past {@link #NEEDS_OPERATOR_AT} consecutive transients the text says so, the engine logs
 * {@code event=data_move_needs_operator} once, on the failure that enters the state ({@link #entersNeedsOperator}), and
 * {@code /v1/status} lists the move for as long as {@link #needsOperator(int, boolean)} holds. The state is entered at
 * {@link #NEEDS_OPERATOR_AT} consecutive transients, or at {@link #NEEDS_OPERATOR_AT_ON_COMPLETE} consecutive lock or statement
 * timeouts at {@code on_complete}; the second is latched ({@link OnCompleteRuns}) until the move completes or is forgotten, or
 * the row's count breaks continuity.
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

    /** {@link #blockedReason(int, boolean, boolean, Instant, String)} for a failure outside the early rule. */
    static String blockedReason(int retries, Instant last, String error) {
        return blockedReason(retries, false, false, last, error);
    }

    /**
     * What {@code blocked:} shows: the count, the time of the latest transient error and the error, then, for a move in the
     * needs-an-operator state ({@link #needsOperator}), {@link #INDEX_BUILDS_NOTE} when {@code indexBuilds} and "needs an
     * operator" last.
     */
    static String blockedReason(int retries, boolean latched, boolean indexBuilds, Instant last, String error) {
        String base = "retry " + retries + ", last " + last + ": " + error;
        boolean needs = needsOperator(retries, latched);
        if (needs && indexBuilds) {
            base += "; " + INDEX_BUILDS_NOTE;
        }
        return needs ? base + "; needs an operator" : base;
    }

    /** The reason the page's log event gives: what each retry costs when it ends index builds, else the count. */
    static String pageReason(int retries, boolean indexBuilds) {
        return indexBuilds ? INDEX_BUILDS_NOTE
            : "the move has failed transiently " + retries + " times in a row and keeps retrying";
    }

    /**
     * Whether a failure is the kind whose every retry costs index builds when the move is over the chunk family: it arose at
     * {@code on_complete} and is a lock timeout (55P03) or a statement timeout (57014).
     */
    static boolean isOnCompleteBound(FailureHandler.Stage stage, String sqlstate) {
        return stage == FailureHandler.Stage.ON_COMPLETE && isTimeout(sqlstate);
    }

    /** A lock timeout (55P03) or a statement timeout (57014): the only SQLSTATEs that mean a bound was missed. */
    static boolean isTimeout(String sqlstate) {
        return "55P03".equals(sqlstate) || "57014".equals(sqlstate);
    }

    /**
     * THE rule, in one place: a move needs an operator from {@link #NEEDS_OPERATOR_AT} consecutive transients of any kind, or
     * once {@link OnCompleteRuns} has latched {@link #NEEDS_OPERATOR_AT_ON_COMPLETE} consecutive {@link #isOnCompleteBound}
     * ones. {@code retries} is the row's {@code transient_retries}. The log event, {@link #blockedReason}, the registry-row read
     * ({@link #needsOperator(DataMove)}) and so {@code /v1/status} and {@code nx doctor} all go through it.
     */
    static boolean needsOperator(int retries, boolean latched) {
        return retries >= NEEDS_OPERATOR_AT || latched;
    }

    /** Whether a failure making the counts {@code retries} and {@code latched} enters the state: held after it, not before. */
    static boolean entersNeedsOperator(int retriesBefore, boolean latchedBefore, int retries, boolean latched) {
        return needsOperator(retries, latched) && !needsOperator(retriesBefore, latchedBefore);
    }

    /**
     * The early rule's run, per move, as this process has seen it. The registry row keeps one count of consecutive transients
     * and not the stage or SQLSTATE of each, so the run is held here. A failure is CHAINED to the last one this object saw when
     * the row's count went up by exactly one; anything else (a reset of the count, a gap, a restart) starts afresh.
     * A chained {@link #isOnCompleteBound} failure extends the run and any other chained failure zeroes it; once the run reaches
     * {@link #NEEDS_OPERATOR_AT_ON_COMPLETE} the move is LATCHED, and stays so, whatever else fails, until continuity breaks
     * or the entry is dropped ({@link #forget}, {@link #retainOnly}) when the move completes or leaves the open set. So one
     * other transient after the page does not clear it, and three more timeouts do not page again. Held per label in memory
     * only: a restart starts the run again (the 20 rule is read from the row and does not).
     */
    static final class OnCompleteRuns {
        private record Run(int retries, int count, boolean latched) { }

        /** What one failure did: whether the move was latched before it, whether it is after, and the run's length now. */
        record Step(boolean latchedBefore, boolean latched, int run) { }

        private final Map<String, Run> byLabel = new ConcurrentHashMap<>();

        /** Records the transient that made the row's count {@code retries}; {@code bound} is whether it is a bound failure. */
        Step record(String label, int retries, boolean bound) {
            Step[] out = new Step[1];
            byLabel.compute(label, (k, prior) -> {
                boolean chained = prior != null && prior.retries() == retries - 1;
                int run = bound ? (chained ? prior.count() : 0) + 1 : 0;
                boolean before = chained && prior.latched();
                boolean latched = before || run >= NEEDS_OPERATOR_AT_ON_COMPLETE;
                out[0] = new Step(before, latched, run);
                return new Run(retries, run, latched);
            });
            return out[0];
        }

        /** The move completed or is gone: nothing more is kept for it. */
        void forget(String label) {
            byLabel.remove(label);
        }

        /** Drops every entry whose move is not in {@code openLabels}. */
        void retainOnly(Collection<String> openLabels) {
            byLabel.keySet().retainAll(openLabels);
        }
    }

    /**
     * Whether an open move's ROW is in the needs-an-operator state by the 20-retries rule: the count has reached
     * {@link #NEEDS_OPERATOR_AT} and the move is not paused (a paused move is one an operator already holds). Read from the
     * row, so a restart changes nothing. The latched early rule is not in the row, so {@link MoveBootWork} reads it from
     * {@link MoveStates.State#needsOperator()} as well; this is the persisted half.
     */
    static boolean needsOperator(DataMove move) {
        return !move.paused() && needsOperator(move.transientRetries(), false) && move.lastTransientAt() != null;
    }

    /** The step a failure arose in, as the event and the doctor name it: the stage in lower case ({@code on_complete}). */
    static String step(FailureHandler.Stage stage) {
        return stage.name().toLowerCase(Locale.ROOT);
    }

    /**
     * The bound in milliseconds that the failing step missed, or {@link FailureHandler.PageInfo#NO_BOUND}: only a lock timeout
     * (55P03) or a statement timeout (57014) missed one. For {@code on_complete} and its re-check it is the completion's
     * {@code statement_timeout_ms} (the lock timeout, half of it, for 55P03); for the first verification pass,
     * {@code verify_statement_timeout_ms}.
     */
    static int boundMs(FailureHandler.Stage stage, DataMove move, String sqlstate) {
        if (!isTimeout(sqlstate)) {
            return FailureHandler.PageInfo.NO_BOUND;
        }
        return switch (stage) {
            case ON_COMPLETE, RECHECK -> "55P03".equals(sqlstate)
                ? VerifyingFinisher.lockTimeoutMs(move) : move.statementTimeoutMs();
            case VERIFICATION -> "57014".equals(sqlstate) ? move.verifyStatementTimeoutMs() : FailureHandler.PageInfo.NO_BOUND;
            default -> FailureHandler.PageInfo.NO_BOUND;
        };
    }
}
