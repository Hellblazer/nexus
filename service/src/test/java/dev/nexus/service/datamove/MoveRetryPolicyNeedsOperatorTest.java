// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

import java.time.Instant;

/**
 * RDR-246 (nexus-p2m1x.22): the one rule of the needs-an-operator state and the texts built from it. The log event, the
 * blocked reason, the registry-row read, {@code /v1/status} and {@code nx doctor} all read {@link MoveRetryPolicy#needsOperator}.
 */
class MoveRetryPolicyNeedsOperatorTest {

    private static final Instant AT = Instant.parse("2026-11-23T10:00:00Z");

    @Test
    void theRule_isTwentyTransientsOrThreeOnCompleteBounds() {
        assertThat(MoveRetryPolicy.needsOperator(19, 0)).isFalse();
        assertThat(MoveRetryPolicy.needsOperator(20, 0)).isTrue();
        assertThat(MoveRetryPolicy.needsOperator(2, 2)).isFalse();
        assertThat(MoveRetryPolicy.needsOperator(3, 3)).isTrue();
        assertThat(MoveRetryPolicy.needsOperator(7, 3)).isTrue();
    }

    @Test
    void onlyOnCompleteLockAndStatementTimeoutsAreBounds() {
        var onComplete = FailureHandler.Stage.ON_COMPLETE;
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, "57014")).isTrue();
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, "55P03")).isTrue();
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, "40001")).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(onComplete, null)).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(FailureHandler.Stage.RECHECK, "57014")).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(FailureHandler.Stage.VERIFICATION, "57014")).isFalse();
        assertThat(MoveRetryPolicy.isOnCompleteBound(FailureHandler.Stage.CALL, "55P03")).isFalse();
    }

    @Test
    void aFailureEntersTheStateOnce_onTheCountThatCrossesIt() {
        assertThat(MoveRetryPolicy.entersNeedsOperator(2, 2, 3, 3)).as("the third bound in a row").isTrue();
        assertThat(MoveRetryPolicy.entersNeedsOperator(3, 3, 4, 4)).as("the fourth: already in").isFalse();
        assertThat(MoveRetryPolicy.entersNeedsOperator(19, 0, 20, 0)).as("the twentieth").isTrue();
        assertThat(MoveRetryPolicy.entersNeedsOperator(20, 0, 21, 0)).as("the twenty-first").isFalse();
        assertThat(MoveRetryPolicy.entersNeedsOperator(19, 2, 20, 3)).as("both rules at once enter once").isTrue();
        assertThat(MoveRetryPolicy.entersNeedsOperator(19, 3, 20, 4)).as("the run was in already").isFalse();
    }

    @Test
    void theRunBreaksOnAnotherFailureOnAGapAndOnAReset() {
        var runs = new MoveRetryPolicy.OnCompleteRuns();
        assertThat(runs.record("m", 1, true).run()).isEqualTo(1);
        assertThat(runs.record("m", 2, true).run()).isEqualTo(2);
        assertThat(runs.record("m", 3, false).run()).as("another kind between").isZero();
        assertThat(runs.record("m", 4, true).run()).isEqualTo(1);
        assertThat(runs.record("m", 6, true).run()).as("a count that skipped one: not seen in a row").isEqualTo(1);
        assertThat(runs.record("m", 1, true).run()).as("the row's count was reset and started over").isEqualTo(1);
        assertThat(runs.record("other", 1, true).run()).as("one run a move").isEqualTo(1);
    }

    @Test
    void theBlockedReason_carriesTheNoteAndTheEndingTheRuleCalls() {
        assertThat(MoveRetryPolicy.blockedReason(2, 2, true, AT, "e")).doesNotContain("needs an operator");
        String early = MoveRetryPolicy.blockedReason(3, 3, true, AT, "e");
        assertThat(early).endsWith("; needs an operator").contains(MoveRetryPolicy.INDEX_BUILDS_NOTE);
        assertThat(MoveRetryPolicy.blockedReason(3, 3, false, AT, "e")).endsWith("; needs an operator")
            .doesNotContain("index builds");
        assertThat(MoveRetryPolicy.blockedReason(20, 0, false, AT, "e")).endsWith("; needs an operator");
        assertThat(MoveRetryPolicy.blockedReason(20, AT, "e")).as("the three-argument form is the 20 rule")
            .endsWith("; needs an operator");
        assertThat(MoveReasons.needsOperator(early)).isTrue();
        assertThat(MoveReasons.terminatesIndexBuilds(early)).isTrue();
        assertThat(MoveReasons.needsOperator(MoveRetryPolicy.blockedReason(19, AT, "e"))).isFalse();
        assertThat(MoveReasons.classOf(early)).as("a paged move loses any class, so a doctor row is red").isNull();
    }

    @Test
    void thePublicForm_keepsTheNoteAndTheEnding_andNothingTheRunWrote() {
        String reason = MoveRetryPolicy.blockedReason(3, 3, true, AT,
            "on_complete of m did not complete: sqlstate=57014 canceling statement for tenant acme-secret");

        String shown = MoveReasons.publicForm(reason);

        assertThat(shown).startsWith("retry 3: on_complete did not complete sqlstate=57014")
            .contains(MoveRetryPolicy.INDEX_BUILDS_NOTE).endsWith("; needs an operator").doesNotContain("acme-secret");
    }
}
