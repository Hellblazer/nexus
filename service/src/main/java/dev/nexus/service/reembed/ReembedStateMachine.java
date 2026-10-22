// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import static java.util.Map.entry;

import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the re-embed job's state machine, as data. No I/O. The runner and the
 * control routes ask {@link #decide} what an event does to a job in a given state; the repository then applies the
 * answer as a compare-and-set ({@code ReembedJobRepository#applyTransition}), so two actors deciding from one
 * snapshot cannot both win.
 *
 * <p>{@link #TRANSITIONS} is the RDR's state table ("Leaves to" column of the table that opens with the
 * {@code planned} row, {@code docs/rdr/rdr-210-dual-mode-local-engine.md}), one entry per allowed (state, event):
 * <ul>
 *   <li>{@code planned} leaves to {@code running} (the runner picks it up) or {@code aborted};</li>
 *   <li>{@code running} leaves to {@code paused}; {@code failed} (a batch failed, the profile changed, the token
 *       cap was reached); {@code aborted}; {@code cutover} when a pass reaches the end;</li>
 *   <li>{@code paused} leaves to {@code running} or {@code aborted};</li>
 *   <li>{@code failed} leaves to {@code running} (resume) or {@code aborted};</li>
 *   <li>{@code cutover} leaves to {@code done}; {@code done_with_skips}; {@code failed} (the parity check refused
 *       the delete); {@code aborted}, but only before the delete step starts;</li>
 *   <li>{@code done} leaves to {@code running} or {@code cutover} only for a {@code kept_tombstoned} source
 *       re-listed by {@code --all}; {@code aborted} and {@code done_with_skips} leave to nothing.</li>
 * </ul>
 * The profile re-check of "every entry into running" is two events in sequence: the entry
 * ({@code PICKUP}, {@code RESUME}, {@code REENTER_RUNNING}) lands in {@code running}, and a mismatch then fires
 * {@code PROFILE_CHANGED} from {@code running}, so the table needs no direct edge from the entering states.
 *
 * <p>Three self-transitions are idempotent and change nothing: pause on {@code paused}, resume on
 * {@code running}, abort on {@code aborted} ({@link Unchanged}). Every other (state, event) pair outside the table
 * is {@link Refused}.
 */
public final class ReembedStateMachine {

    private ReembedStateMachine() {
    }

    /** A job's state. {@link #wire()} is the string stored in {@code reembed_jobs.state}. */
    public enum State {
        PLANNED, RUNNING, PAUSED, FAILED, ABORTED, CUTOVER, DONE, DONE_WITH_SKIPS;

        public String wire() {
            return name().toLowerCase(Locale.ROOT);
        }

        /** The state stored as {@code wire}, or throws {@link IllegalArgumentException} for an unknown string. */
        public static State fromWire(String wire) {
            for (State s : values()) {
                if (s.wire().equals(wire)) {
                    return s;
                }
            }
            throw new IllegalArgumentException("unknown re-embed job state: " + wire);
        }
    }

    /** What happened to a job: a control request, a runner outcome, or a cutover outcome. */
    public enum Event {
        /** The runner picked a {@code planned} job up. */
        PICKUP,
        PAUSE,
        RESUME,
        ABORT,
        /** A pass reached the end of the source's live documents. */
        PASS_COMPLETE,
        /** A batch failed for a reason other than a skip. */
        BATCH_FAILED,
        /** The target's model is no longer the profile's model for the source's content type. */
        PROFILE_CHANGED,
        /** The next batch would pass the job's token cap. */
        TOKEN_CAP,
        /** Cutover finished: the source is superseded and handled per its disposition. */
        CUTOVER_DONE,
        /** Cutover found a live document still in the source (a skip). */
        CUTOVER_SKIPS,
        /** The parity check refused the delete. */
        PARITY_FAILED,
        /** {@code --all} re-listed a {@code kept_tombstoned} source that now holds a live document. */
        REENTER_RUNNING,
        /** {@code --all} re-listed a {@code kept_tombstoned} source whose tombstones are gone. */
        REENTER_CUTOVER
    }

    /** The marker written to {@code reembed_jobs.cutover_step} after the source is superseded. */
    public static final String CUTOVER_STEP_SUPERSEDED = "superseded";
    /** The marker written just before the irreversible delete; abort is refused from here. */
    public static final String CUTOVER_STEP_DELETE_STARTED = "delete_started";

    /** The only disposition from which a finished job re-enters {@code running} or {@code cutover}. */
    public static final String DISPOSITION_KEPT_TOMBSTONED = "kept_tombstoned";

    /** The states in which a job holds its source's lock; the same five as the partial unique index. */
    public static final Set<State> LOCK_STATES = Set.of(
        State.PLANNED, State.RUNNING, State.PAUSED, State.CUTOVER, State.FAILED);

    /** Every allowed edge, straight from the RDR's table. A pair absent here is refused or idempotent. */
    static final Map<State, Map<Event, State>> TRANSITIONS = Map.ofEntries(
        entry(State.PLANNED, Map.of(
            Event.PICKUP, State.RUNNING,
            Event.ABORT, State.ABORTED)),
        entry(State.RUNNING, Map.of(
            Event.PAUSE, State.PAUSED,
            Event.BATCH_FAILED, State.FAILED,
            Event.PROFILE_CHANGED, State.FAILED,
            Event.TOKEN_CAP, State.FAILED,
            Event.ABORT, State.ABORTED,
            Event.PASS_COMPLETE, State.CUTOVER)),
        entry(State.PAUSED, Map.of(
            Event.RESUME, State.RUNNING,
            Event.ABORT, State.ABORTED)),
        entry(State.FAILED, Map.of(
            Event.RESUME, State.RUNNING,
            Event.ABORT, State.ABORTED)),
        entry(State.ABORTED, Map.of()),
        entry(State.CUTOVER, Map.of(
            Event.CUTOVER_DONE, State.DONE,
            Event.CUTOVER_SKIPS, State.DONE_WITH_SKIPS,
            Event.PARITY_FAILED, State.FAILED,
            Event.ABORT, State.ABORTED)),
        entry(State.DONE, Map.of(
            Event.REENTER_RUNNING, State.RUNNING,
            Event.REENTER_CUTOVER, State.CUTOVER)),
        entry(State.DONE_WITH_SKIPS, Map.of()));

    /** The three idempotent self-transitions: the state that already is what the event asks for. */
    static final Map<State, Event> IDEMPOTENT = Map.of(
        State.PAUSED, Event.PAUSE,
        State.RUNNING, Event.RESUME,
        State.ABORTED, Event.ABORT);

    /** What {@link #decide} answers. */
    public sealed interface Decision permits Applied, Unchanged, Refused {
    }

    /** The event applies: move to {@code next}. */
    public record Applied(State next) implements Decision {
    }

    /** An idempotent self-transition: answer with the current state and change nothing. */
    public record Unchanged() implements Decision {
    }

    /** The event does not apply; {@code reason} names the state and event, for a 409. */
    public record Refused(String reason) implements Decision {
    }

    /** {@link #decide(State, Event, String, String)} for a caller with no disposition to consult. */
    public static Decision decide(State state, Event event, String cutoverStep) {
        return decide(state, event, cutoverStep, null);
    }

    /**
     * The {@code failure_reason} a transition into {@code failed} records for {@code event}, or null when the event
     * does not lead to {@code failed}. The column's CHECK holds these values (vectors-037).
     */
    public static String failureReason(Event event) {
        return switch (event) {
            case BATCH_FAILED -> "batch_failed";
            case PROFILE_CHANGED -> "profile_changed";
            case TOKEN_CAP -> "token_cap";
            case PARITY_FAILED -> "parity_mismatch";
            default -> null;
        };
    }

    /**
     * What {@code event} does to a job in {@code state}. Only the table: an entry into a lock state from
     * {@code done} (a {@code kept_tombstoned} re-entry) can still be refused by the repository, whose start and
     * transitions map the lock index's unique violation to a refusal naming the holder; a caller applies an
     * Applied decision through that CAS, never around it.
     *
     * @param cutoverStep the job's cutover marker, or null before one is written; only an abort in
     *                    {@code cutover} reads it
     * @param disposition the job's disposition, or null; only a re-entry from {@code done} reads it
     */
    public static Decision decide(State state, Event event, String cutoverStep, String disposition) {
        if (IDEMPOTENT.get(state) == event) {
            return new Unchanged();
        }
        State next = TRANSITIONS.get(state).get(event);
        if (next == null) {
            return refused(state, event, "no such transition");
        }
        if (state == State.CUTOVER && event == Event.ABORT && CUTOVER_STEP_DELETE_STARTED.equals(cutoverStep)) {
            return refused(state, event, "the delete step has started");
        }
        if (state == State.DONE && !DISPOSITION_KEPT_TOMBSTONED.equals(disposition)) {
            return refused(state, event, "only a source kept as " + DISPOSITION_KEPT_TOMBSTONED
                + " is re-entered, this job's disposition is " + (disposition == null ? "none" : disposition));
        }
        return new Applied(next);
    }

    private static Refused refused(State state, Event event, String why) {
        return new Refused("cannot apply " + event.name().toLowerCase(Locale.ROOT) + " to a job in state "
            + state.wire() + ": " + why);
    }
}
