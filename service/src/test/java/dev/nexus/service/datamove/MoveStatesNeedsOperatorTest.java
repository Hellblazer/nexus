// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;

/**
 * RDR-246 (nexus-p2m1x.22): what {@link MoveStates} keeps of the needs-an-operator page. A retry that runs for a whole statement
 * timeout is WORKING, so a status sample taken then must still see the page; and the announce set is what keeps the worker and
 * the status sampler from logging one page twice.
 */
class MoveStatesNeedsOperatorTest {

    private static final DataMove MOVE =
        new DataMove("m", "t", "s", "t", 10, 0, 1_400, "v", null, null, null, null, 3, null, null, 60_000);
    private static final FailureHandler.PageInfo PAGED =
        new FailureHandler.PageInfo(true, true, "on_complete", 700, true);
    private static final FailureHandler.PageInfo BELOW =
        new FailureHandler.PageInfo(false, false, "verification", FailureHandler.PageInfo.NO_BOUND, false);

    private final MoveStates states = new MoveStates(Clock.fixed(Instant.parse("2026-11-23T10:00:00Z"), ZoneOffset.UTC));

    private MoveStates.State state() {
        return states.state("m").orElseThrow();
    }

    @Test
    void aBlockedReportWithAPage_isKept_whileTheMoveIsRetried() {
        states.blocked(MOVE, "retry 3: x; needs an operator", PAGED);
        assertThat(state().needsOperator()).isTrue();
        assertThat(state().step()).isEqualTo("on_complete");
        assertThat(state().boundMs()).isEqualTo(700);
        assertThat(state().terminatesIndexBuilds()).isTrue();

        states.working(MOVE);

        assertThat(state().kind()).isEqualTo(MoveStates.Kind.WORKING);
        assertThat(state().needsOperator()).as("a retry in flight keeps the page").isTrue();
        assertThat(state().step()).isEqualTo("on_complete");
        assertThat(state().boundMs()).isEqualTo(700);
        assertThat(state().terminatesIndexBuilds()).isTrue();
    }

    @Test
    void aWorkingMoveWithNothingBefore_hasNoPage() {
        states.working(MOVE);

        assertThat(state().needsOperator()).isFalse();
        assertThat(state().step()).isNull();
        assertThat(state().boundMs()).isEqualTo(-1);
        assertThat(state().terminatesIndexBuilds()).isFalse();
    }

    @Test
    void theNextFailureReplacesThePage_andAReportWithoutOneClearsIt() {
        states.blocked(MOVE, "r", PAGED);
        states.working(MOVE);
        states.blocked(MOVE, "r", BELOW);
        assertThat(state().needsOperator()).isFalse();
        assertThat(state().step()).as("the step of the last failure is still the last failure's").isEqualTo("verification");
        assertThat(state().terminatesIndexBuilds()).isFalse();

        states.blocked(MOVE, "r", PAGED);
        states.blocked(MOVE, "no page given");
        assertThat(state().needsOperator()).isFalse();
        assertThat(state().step()).isNull();
    }

    @Test
    void failedPausedAndRunsCompleteClearThePage() {
        for (Runnable end : List.<Runnable>of(() -> states.failed(MOVE, "r"), () -> states.paused(MOVE),
            () -> states.runsComplete(MOVE))) {
            states.blocked(MOVE, "r", PAGED);
            end.run();
            assertThat(state().needsOperator()).isFalse();
            assertThat(state().step()).isNull();
            states.working(MOVE);
            assertThat(state().needsOperator()).as("and a retry after it carries nothing over").isFalse();
        }
    }

    @Test
    void aMoveIsAnnouncedOncePerLabel_untilItLeavesTheListOrIsForgotten() {
        assertThat(states.announce("m")).isTrue();
        assertThat(states.announce("m")).as("the sampler after the worker, or the worker after the sampler").isFalse();
        assertThat(states.announce("other")).isTrue();

        states.retainAnnounced(List.of("other"));
        assertThat(states.announce("m")).as("it left the list: a new entry pages again").isTrue();
        assertThat(states.announce("other")).isFalse();

        states.forget("other");
        assertThat(states.announce("other")).as("forgotten with the move").isTrue();
    }
}
