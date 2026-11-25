// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;

/**
 * RDR-246 (nexus-p2m1x.22): the one rule of the needs-an-operator state and the texts built from it. The log event, the
 * blocked reason, the registry-row read, {@code /v1/status} and {@code nx doctor} all read {@link MoveRetryPolicy#needsOperator}.
 */
class MoveRetryPolicyNeedsOperatorTest {

    private static final Instant AT = Instant.parse("2026-11-23T10:00:00Z");

    private static DataMove move(int statementMs, int verifyMs) {
        return new DataMove("m", "t", "s", "t", 10, 0, statementMs, "v", null, null, null, null, 0, null, null, verifyMs);
    }

    @Test
    void theRule_isTwentyTransientsOrALatchedOnCompleteRun() {
        assertThat(MoveRetryPolicy.needsOperator(19, false)).isFalse();
        assertThat(MoveRetryPolicy.needsOperator(20, false)).isTrue();
        assertThat(MoveRetryPolicy.needsOperator(2, false)).isFalse();
        assertThat(MoveRetryPolicy.needsOperator(3, true)).isTrue();
        assertThat(MoveRetryPolicy.needsOperator(7, true)).isTrue();
    }

    @Test
    void onlyOnCompleteLockAndStatementTimeoutsAreBounds() {
        var onComplete = FailureHandler.Stage.ON_COMPLETE;
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, "57014")).isTrue();
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, "55P03")).isTrue();
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, "40001")).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, "08006")).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, null)).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(FailureHandler.Stage.RECHECK, "57014")).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(FailureHandler.Stage.VERIFICATION, "57014")).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(FailureHandler.Stage.CALL, "55P03")).isFalse();
    }

    @Test
    void aBoundIsNamedOnlyForATimeout_andByTheStepThatMissedIt() {
        var m = move(1_400, 60_000);
        var onComplete = FailureHandler.Stage.ON_COMPLETE;
        assertThat(MoveRetryPolicy.boundMs(onComplete, m, "57014")).isEqualTo(1_400);
        assertThat(MoveRetryPolicy.boundMs(onComplete, m, "55P03")).as("the lock timeout, half of the statement bound")
            .isEqualTo(VerifyingFinisher.lockTimeoutMs(1_400));
        assertThat(MoveRetryPolicy.boundMs(FailureHandler.Stage.VERIFICATION, m, "57014")).isEqualTo(60_000);
        assertThat(MoveRetryPolicy.boundMs(onComplete, m, "40001")).as("not a timeout").isEqualTo(-1);
        assertThat(MoveRetryPolicy.boundMs(onComplete, m, "08006")).isEqualTo(-1);
        assertThat(MoveRetryPolicy.boundMs(onComplete, m, null)).isEqualTo(-1);
        assertThat(MoveRetryPolicy.boundMs(FailureHandler.Stage.CALL, m, "57014")).isEqualTo(-1);
    }

    @Test
    void aFailureEntersTheStateOnce_onTheCountThatCrossesIt() {
        assertThat(MoveRetryPolicy.entersNeedsOperator(2, false, 3, true)).as("the third bound in a row").isTrue();
        assertThat(MoveRetryPolicy.entersNeedsOperator(3, true, 4, true)).as("the fourth: already in").isFalse();
        assertThat(MoveRetryPolicy.entersNeedsOperator(19, false, 20, false)).as("the twentieth").isTrue();
        assertThat(MoveRetryPolicy.entersNeedsOperator(20, false, 21, false)).as("the twenty-first").isFalse();
        assertThat(MoveRetryPolicy.entersNeedsOperator(19, true, 20, true)).as("the latch was in already").isFalse();
    }

    @Test
    void theRunLatchesAtThree_andOnlyAGapOrAResetOrAForgetClearsIt() {
        var runs = new MoveRetryPolicy.OnCompleteRuns();
        assertThat(runs.record("m", 1, true).latched()).isFalse();
        assertThat(runs.record("m", 2, true).latched()).isFalse();
        var third = runs.record("m", 3, true);
        assertThat(third.latched()).isTrue();
        assertThat(third.latchedBefore()).isFalse();
        var other = runs.record("m", 4, false);
        assertThat(other.latched()).as("another failure after the page does not clear it").isTrue();
        assertThat(other.latchedBefore()).isTrue();
        assertThat(other.run()).as("the run of bounds itself starts again").isZero();
        assertThat(runs.record("m", 5, true).latched()).isTrue();
        assertThat(runs.record("m", 6, true).latched()).isTrue();
        var again = runs.record("m", 7, true);
        assertThat(again.latchedBefore()).as("three more bounds: already latched, so no second page").isTrue();

        assertThat(runs.record("m", 9, true).latched()).as("a count that skipped one: not seen in a row").isFalse();
        assertThat(runs.record("m", 10, true).latched()).isFalse();
        assertThat(runs.record("m", 11, true).latched()).isTrue();
        assertThat(runs.record("m", 1, true).latched()).as("the row's count was reset and started over").isFalse();
        assertThat(runs.record("other", 1, true).run()).as("one run a move").isEqualTo(1);
    }

    @Test
    void aRunBrokenBeforeThreeNeedsThreeInARowAfterTheBreak() {
        var runs = new MoveRetryPolicy.OnCompleteRuns();
        runs.record("m", 1, true);
        runs.record("m", 2, true);
        assertThat(runs.record("m", 3, false).run()).isZero();
        assertThat(runs.record("m", 4, true).latched()).isFalse();
        assertThat(runs.record("m", 5, true).latched()).isFalse();
        assertThat(runs.record("m", 6, true).latched()).isTrue();
    }

    @Test
    void forgetAndRetainOnly_dropTheRunOfAMoveThatIsGone() {
        var runs = new MoveRetryPolicy.OnCompleteRuns();
        for (int i = 1; i <= 3; i++) {
            runs.record("done", i, true);
            runs.record("open", i, true);
        }
        runs.forget("done");
        assertThat(runs.record("done", 4, true).latchedBefore()).as("forgotten: the count 4 is no continuation").isFalse();

        runs.retainOnly(List.of("open"));
        assertThat(runs.record("done", 5, true).run()).as("pruned again").isEqualTo(1);
        assertThat(runs.record("open", 4, true).latchedBefore()).as("kept").isTrue();
    }

    @Test
    void theBlockedReason_carriesTheNoteAndTheEndingTheRuleCalls() {
        assertThat(MoveRetryPolicy.blockedReason(2, false, true, AT, "e")).doesNotContain("needs an operator")
            .doesNotContain("index builds");
        String early = MoveRetryPolicy.blockedReason(3, true, true, AT, "e");
        assertThat(early).endsWith("; needs an operator").contains(MoveRetryPolicy.INDEX_BUILDS_NOTE);
        assertThat(MoveRetryPolicy.blockedReason(3, true, false, AT, "e")).endsWith("; needs an operator")
            .doesNotContain("index builds");
        assertThat(MoveRetryPolicy.blockedReason(20, false, true, AT, "e")).as("the 20 rule says it too")
            .endsWith("; needs an operator").contains(MoveRetryPolicy.INDEX_BUILDS_NOTE);
        assertThat(MoveRetryPolicy.blockedReason(20, AT, "e")).endsWith("; needs an operator");
        assertThat(MoveRetryPolicy.blockedReason(19, AT, "e")).doesNotContain("needs an operator");
        assertThat(MoveReasons.classOf(early)).as("a paged move loses any class, so a doctor row is red").isNull();
    }

    @Test
    void thePublicForm_keepsTheNoteAndTheEnding_andNothingTheRunWrote() {
        String reason = MoveRetryPolicy.blockedReason(3, true, true, AT,
            "on_complete of m did not complete: sqlstate=57014 canceling statement for tenant acme-secret");

        String shown = MoveReasons.publicForm(reason);

        assertThat(shown).startsWith("retry 3: on_complete did not complete sqlstate=57014")
            .contains(MoveRetryPolicy.INDEX_BUILDS_NOTE).endsWith("; needs an operator").doesNotContain("acme-secret");
    }

    @Test
    void theRowRule_readsOnlyTheCount() {
        var at = OffsetDateTime.parse("2026-11-23T10:00:00Z");
        DataMove twenty = new DataMove("m", "t", "s", "t", 10, 0, 1000, "v", null, null, null, null, 20, at, "x", 60_000);
        DataMove paused = new DataMove("m", "t", "s", "t", 10, 0, 1000, "v", null, null, at, "op", 20, at, "x", 60_000);
        DataMove three = new DataMove("m", "t", "s", "t", 10, 0, 1000, "v", null, null, null, null, 3, at, "x", 60_000);
        assertThat(MoveRetryPolicy.needsOperator(twenty)).isTrue();
        assertThat(MoveRetryPolicy.needsOperator(paused)).as("an operator holds it").isFalse();
        assertThat(MoveRetryPolicy.needsOperator(three)).as("the latch is not in the row").isFalse();
    }
}
