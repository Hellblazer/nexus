// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import static dev.nexus.service.reembed.ReembedStateMachine.CUTOVER_STEP_DELETE_STARTED;
import static dev.nexus.service.reembed.ReembedStateMachine.CUTOVER_STEP_SUPERSEDED;
import static dev.nexus.service.reembed.ReembedStateMachine.DISPOSITION_KEPT_TOMBSTONED;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.ReembedJobRepository;
import dev.nexus.service.reembed.ReembedStateMachine.Applied;
import dev.nexus.service.reembed.ReembedStateMachine.Decision;
import dev.nexus.service.reembed.ReembedStateMachine.Event;
import dev.nexus.service.reembed.ReembedStateMachine.Refused;
import dev.nexus.service.reembed.ReembedStateMachine.State;
import dev.nexus.service.reembed.ReembedStateMachine.Unchanged;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.EnumSource;
import org.junit.jupiter.params.provider.MethodSource;

import java.util.ArrayList;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * The state machine against the RDR-210 state table, over EVERY (state, event) pair. The expectation here is written
 * out independently of {@code ReembedStateMachine.TRANSITIONS} (the table the code is built from), so a wrong edge in
 * the code is a mismatch, not a copy of itself.
 */
class ReembedStateMachineTest {

    private static final int STATES = 8;
    private static final int EVENTS = 13;

    /** Allowed edges, from the "Leaves to" column of the RDR's state table. */
    private static final Map<State, Map<Event, State>> EXPECTED = new EnumMap<>(State.class);

    static {
        edge(State.PLANNED, Event.PICKUP, State.RUNNING);
        edge(State.PLANNED, Event.ABORT, State.ABORTED);

        edge(State.RUNNING, Event.PAUSE, State.PAUSED);
        edge(State.RUNNING, Event.BATCH_FAILED, State.FAILED);
        edge(State.RUNNING, Event.PROFILE_CHANGED, State.FAILED);
        edge(State.RUNNING, Event.TOKEN_CAP, State.FAILED);
        edge(State.RUNNING, Event.ABORT, State.ABORTED);
        edge(State.RUNNING, Event.PASS_COMPLETE, State.CUTOVER);

        edge(State.PAUSED, Event.RESUME, State.RUNNING);
        edge(State.PAUSED, Event.ABORT, State.ABORTED);

        edge(State.FAILED, Event.RESUME, State.RUNNING);
        edge(State.FAILED, Event.ABORT, State.ABORTED);

        edge(State.CUTOVER, Event.CUTOVER_DONE, State.DONE);
        edge(State.CUTOVER, Event.CUTOVER_SKIPS, State.DONE_WITH_SKIPS);
        edge(State.CUTOVER, Event.PARITY_FAILED, State.FAILED);
        edge(State.CUTOVER, Event.ABORT, State.ABORTED);

        edge(State.DONE, Event.REENTER_RUNNING, State.RUNNING);
        edge(State.DONE, Event.REENTER_CUTOVER, State.CUTOVER);
    }

    private static void edge(State from, Event on, State to) {
        EXPECTED.computeIfAbsent(from, k -> new EnumMap<>(Event.class)).put(on, to);
    }

    /** The three idempotent self-transitions. */
    private static boolean selfTransition(State s, Event e) {
        return (s == State.PAUSED && e == Event.PAUSE)
            || (s == State.RUNNING && e == Event.RESUME)
            || (s == State.ABORTED && e == Event.ABORT);
    }

    /** Every pair, with the outcome kind the table gives it. */
    static Stream<Arguments> everyPair() {
        List<Arguments> all = new ArrayList<>();
        for (State s : State.values()) {
            for (Event e : Event.values()) {
                State next = EXPECTED.getOrDefault(s, Map.of()).get(e);
                String kind = selfTransition(s, e) ? "unchanged" : next != null ? "applied" : "refused";
                all.add(Arguments.of(s, e, kind, next));
            }
        }
        return all.stream();
    }

    @ParameterizedTest(name = "{0} + {1} is {2}")
    @MethodSource("everyPair")
    void everyStateEventPairIsDecidedAsTheRdrTableSays(State state, Event event, String kind, State next) {
        // A re-entry from done is the one guarded edge; it is allowed only for kept_tombstoned, so decide with it.
        Decision d = ReembedStateMachine.decide(state, event, null, DISPOSITION_KEPT_TOMBSTONED);
        switch (kind) {
            case "applied" -> assertThat(d).isEqualTo(new Applied(next));
            case "unchanged" -> assertThat(d).isEqualTo(new Unchanged());
            default -> {
                assertThat(d).isInstanceOf(Refused.class);
                String reason = ((Refused) d).reason();
                assertThat(reason).contains(state.wire()).contains(event.name().toLowerCase());
            }
        }
    }

    @Test
    void theTableCoversAllPairsAndEachKindHasTheCountTheRdrImplies() {
        List<Arguments> pairs = everyPair().toList();
        // Non-vacuity: 8 states x 13 events, none skipped.
        assertThat(State.values()).hasSize(STATES);
        assertThat(Event.values()).hasSize(EVENTS);
        assertThat(pairs).hasSize(STATES * EVENTS);

        Map<Object, Long> byKind = pairs.stream()
            .collect(Collectors.groupingBy(a -> a.get()[2], Collectors.counting()));
        assertThat(byKind.get("applied")).isEqualTo(18L);
        assertThat(byKind.get("unchanged")).isEqualTo(3L);
        assertThat(byKind.get("refused")).isEqualTo(83L);

        // The code's own table has exactly the edges the independent expectation has, per state.
        assertThat(ReembedStateMachine.TRANSITIONS).hasSize(STATES);
        for (State s : State.values()) {
            assertThat(ReembedStateMachine.TRANSITIONS.get(s))
                .as("edges out of %s", s)
                .isEqualTo(EXPECTED.getOrDefault(s, Map.of()));
        }
        assertThat(ReembedStateMachine.TRANSITIONS.values().stream().mapToInt(Map::size).sum()).isEqualTo(18);
    }

    @Test
    void aSelfTransitionIsNeverAlsoATableEdge() {
        for (State s : State.values()) {
            for (Event e : Event.values()) {
                if (selfTransition(s, e)) {
                    assertThat(ReembedStateMachine.TRANSITIONS.get(s)).doesNotContainKey(e);
                }
            }
        }
    }

    @Test
    void abortInCutoverIsRefusedOnceTheDeleteHasStarted() {
        assertThat(CUTOVER_STEP_DELETE_STARTED).isEqualTo("delete_started");
        Decision d = ReembedStateMachine.decide(State.CUTOVER, Event.ABORT, CUTOVER_STEP_DELETE_STARTED);
        assertThat(d).isInstanceOf(Refused.class);
        assertThat(((Refused) d).reason()).contains("cutover").contains("abort").contains("delete step");
    }

    @Test
    void abortInCutoverIsAllowedBeforeTheDeleteStarts() {
        assertThat(ReembedStateMachine.decide(State.CUTOVER, Event.ABORT, null))
            .isEqualTo(new Applied(State.ABORTED));
        assertThat(ReembedStateMachine.decide(State.CUTOVER, Event.ABORT, CUTOVER_STEP_SUPERSEDED))
            .isEqualTo(new Applied(State.ABORTED));
    }

    @Test
    void theDeleteStartedMarkerBlocksOnlyAbort() {
        // Cutover's own outcomes still apply after the marker: a restart re-runs cutover to its end.
        assertThat(ReembedStateMachine.decide(State.CUTOVER, Event.CUTOVER_DONE, CUTOVER_STEP_DELETE_STARTED))
            .isEqualTo(new Applied(State.DONE));
        assertThat(ReembedStateMachine.decide(State.CUTOVER, Event.PARITY_FAILED, CUTOVER_STEP_DELETE_STARTED))
            .isEqualTo(new Applied(State.FAILED));
    }

    @ParameterizedTest
    @EnumSource(value = Event.class, names = {"REENTER_RUNNING", "REENTER_CUTOVER"})
    void aDoneJobReEntersOnlyForAKeptTombstonedDisposition(Event event) {
        State expected = event == Event.REENTER_RUNNING ? State.RUNNING : State.CUTOVER;
        assertThat(ReembedStateMachine.decide(State.DONE, event, null, DISPOSITION_KEPT_TOMBSTONED))
            .isEqualTo(new Applied(expected));
        for (String other : new String[] {"deleted", "kept_by_flag", "kept_residue", "revived", "", null}) {
            Decision d = ReembedStateMachine.decide(State.DONE, event, null, other);
            assertThat(d).as("disposition %s", other).isInstanceOf(Refused.class);
            assertThat(((Refused) d).reason()).contains(DISPOSITION_KEPT_TOMBSTONED);
        }
        // The three-argument form has no disposition, so a re-entry is refused through it.
        assertThat(ReembedStateMachine.decide(State.DONE, event, null)).isInstanceOf(Refused.class);
    }

    @Test
    void stateWireStringsMatchTheRepositoryConstantsAndLockStatesMatchTheIndexPredicate() {
        assertThat(State.PLANNED.wire()).isEqualTo(ReembedJobRepository.PLANNED);
        assertThat(State.RUNNING.wire()).isEqualTo(ReembedJobRepository.RUNNING);
        assertThat(State.PAUSED.wire()).isEqualTo(ReembedJobRepository.PAUSED);
        assertThat(State.FAILED.wire()).isEqualTo(ReembedJobRepository.FAILED);
        assertThat(State.ABORTED.wire()).isEqualTo(ReembedJobRepository.ABORTED);
        assertThat(State.CUTOVER.wire()).isEqualTo(ReembedJobRepository.CUTOVER);
        assertThat(State.DONE.wire()).isEqualTo(ReembedJobRepository.DONE);
        assertThat(State.DONE_WITH_SKIPS.wire()).isEqualTo(ReembedJobRepository.DONE_WITH_SKIPS);

        assertThat(ReembedStateMachine.LOCK_STATES.stream().map(State::wire).collect(Collectors.toSet()))
            .isEqualTo(ReembedJobRepository.LOCK_STATES);
        for (State s : State.values()) {
            assertThat(State.fromWire(s.wire())).isEqualTo(s);
        }
    }

    @Test
    void aLockStateIsExactlyAStateWithAWayOutOtherThanAKeptTombstonedReEntry() {
        // A job holds its source's lock exactly while the runner or an operator can still move it: the five states
        // with a non-empty exit set that is not the done re-entry. (done is excluded: re-entry takes the lock check.)
        for (State s : State.values()) {
            boolean hasExit = !ReembedStateMachine.TRANSITIONS.get(s).isEmpty() && s != State.DONE;
            assertThat(ReembedStateMachine.LOCK_STATES.contains(s)).as("lock state %s", s).isEqualTo(hasExit);
        }
    }

    @org.junit.jupiter.api.Test
    void eachEventIntoFailedNamesTheFailureReasonTheColumnAccepts() {
        java.util.Map<ReembedStateMachine.Event, String> expected = java.util.Map.of(
                ReembedStateMachine.Event.BATCH_FAILED, "batch_failed",
                ReembedStateMachine.Event.PROFILE_CHANGED, "profile_changed",
                ReembedStateMachine.Event.TOKEN_CAP, "token_cap",
                ReembedStateMachine.Event.PARITY_FAILED, "parity_mismatch");
        for (ReembedStateMachine.Event e : ReembedStateMachine.Event.values()) {
            org.assertj.core.api.Assertions.assertThat(ReembedStateMachine.failureReason(e)).as(e.name())
                    .isEqualTo(expected.get(e));
        }
    }
}
