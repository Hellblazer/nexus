// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The RDR-252 post-close audit (2026-11-22) as the decision reads it, S1 to S2 on recorded rows: the owner's own
 * push after the red tip is an implicit ack (a), a {@code disclaim} row hands the red on at once (b), an acknowledged
 * red that stays open escalates after stall_factor times the deadline (c), and next up is nudged once at half the
 * deadline when nothing has been acknowledged (S2).
 *
 * <p>Layout, as in {@code OwnerEscalationTest}: A pushed X, C pushed Y, Y is red, so A owns it, C is next up, L leads.
 */
class OwnerDecisionAuditTest {

    private static final Instant EPISODE = OwnerFixture.T0.plus(Duration.ofHours(1));

    private OwnerFixture openRed() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.recordNow(f.decide());
        return f;
    }

    private static Instant after(long minutes) {
        return EPISODE.plus(Duration.ofMinutes(minutes));
    }

    // ── S1(a): the owner's push after the red tip is an implicit ack and resets the deadline ──────────────────

    @Test
    void theOwnersPushAfterTheRedTipIsAnImplicitAckSoNoHandOffFollowsAtTheDeadlineAndTheDeadlineCountsFromThePush() {
        OwnerFixture f = openRed();
        f.at(after(15)).push("Z", "Y", "A");            // A pushes the fix 15 minutes in, before the 20-minute deadline
        f.now(after(21));

        OwnerDecision.Decision d = f.onDeadline();

        assertThat(d.post()).as("no hand-off at the old deadline: the owner is fixing it").isNull();
        assertThat(d.deadline()).as("the stall deadline counts from the push, three deadlines on")
            .isEqualTo(after(15).plus(Duration.ofMinutes(60)));
    }

    @Test
    void anImplicitAckStaysInForceWhenTheFixItselfGoesRedAndTheRowIsRefreshed() {
        OwnerFixture f = openRed();
        f.at(after(15)).push("Z", "Y", "A").done("Z", "failure");   // the push is red too: same earliest suspect, a refresh
        f.now(after(16));
        OwnerDecision.Decision refresh = f.decide();
        assertThat(OwnerFixture.bodyOf(refresh.post())).containsEntry("refresh", true).containsEntry("owner", "A");
        f.recordNow(refresh);
        f.now(after(21));

        assertThat(f.onDeadline().post()).as("A's push still counts after the refresh").isNull();
    }

    @Test
    void aPushByAnIdentityOutsideTheGroupIsNoAckAndTheDeadlineStillHandsOff() {
        OwnerFixture f = openRed();
        f.seat("N").notMember("N");
        f.at(after(15)).push("Z", "Y", "N");            // N is not in the group, let alone the owner
        f.now(after(21));

        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("reason", "handoff").containsEntry("owner", "C");
    }

    @Test
    void aPushThatDoesNotChainFromTheRedTipIsNoAck() {
        OwnerFixture f = openRed();
        f.at(after(15)).push("Z", "Q", "A");            // base Q is no tip the chain knows: no link from the red tip
        f.now(after(21));

        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("reason", "handoff");
    }

    // ── S1(b): a disclaim from the current owner hands off at once ───────────────────────────────────────────

    @Test
    void aDisclaimFromTheCurrentOwnerHandsTheRedOnAtOnceWithReasonDisclaimed() {
        OwnerFixture f = openRed();
        f.at(after(2)).disclaim("A", "Y");
        f.now(after(2));

        OwnerDecision.Decision d = f.decide();

        Map<String, Object> b = OwnerFixture.bodyOf(d.post());
        assertThat(d.post().kind()).isEqualTo("owner");
        assertThat(b).containsEntry("owner", "C").containsEntry("reason", "disclaimed").containsEntry("handoffs", 1)
            .containsEntry("next", null);
        assertThat(d.deadline()).as("the new owner's deadline runs from the hand-off").isEqualTo(after(2).plus(Duration.ofMinutes(20)));
    }

    @Test
    void aDisclaimFromAnyoneElseIsIgnored() {
        OwnerFixture f = openRed();
        f.at(after(2)).disclaim("C", "Y");
        f.now(after(2));

        assertThat(f.decide().post()).as("C is next up, not the owner").isNull();
        assertThat(f.onDeadline().post()).isNull();
    }

    @Test
    void aDisclaimNamingAnotherTipOrPostedBeforeTheOwnerRowIsIgnored() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C");
        f.at(after(-5)).disclaim("A", "Y");            // before the owner row of the episode
        f.done("Y", "failure");
        f.now(after(0));
        f.recordNow(f.decide());
        f.at(after(2)).disclaim("A", "NOT-THE-TIP");
        f.now(after(2));

        assertThat(f.decide().post()).isNull();
    }

    @Test
    void aDisclaimWithNoNextUpEscalatesAtOnceAndIsNeverSwallowed() {
        OwnerFixture f = new OwnerFixture().seat("A").state("L", "lapsed", "leased");   // nobody else is live: no next up
        f.push("X", "G0", "A").done("X", "failure");
        f.recordNow(f.decide());
        f.at(after(1)).disclaim("A", "X");
        f.now(after(1));

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("reason", "escalated").containsEntry("handoffs", 1);
    }

    @Test
    void theHandOffAnswersTheDisclaimOnce() {
        OwnerFixture f = openRed();
        f.at(after(2)).disclaim("A", "Y");
        f.now(after(2));
        f.recordNow(f.decide());
        f.now(after(3));

        assertThat(f.decide().post()).as("the disclaim named the old owner; the new row answers it").isNull();
    }

    // ── S1(c): an acknowledged red that stays open escalates after stall_factor x deadline ───────────────────

    @Test
    void anAcknowledgedRedStillOpenAfterThreeDeadlinesFromTheAckEscalatesAsStalled() {
        OwnerFixture f = openRed();
        f.at(after(1)).ack("A", "Y");
        f.now(after(60));

        OwnerDecision.Decision early = f.onDeadline();
        assertThat(early.post()).as("59 minutes after the ack is inside the stall window").isNull();
        assertThat(early.deadline()).isEqualTo(after(1).plus(Duration.ofMinutes(60)));

        f.now(after(62));
        OwnerDecision.Decision due = f.onDeadline();
        Map<String, Object> b = OwnerFixture.bodyOf(due.post());
        assertThat(b).containsEntry("reason", "escalated").containsEntry("stalled", true).containsEntry("owner", "L")
            .containsEntry("next", null).containsEntry("handoffs", 1);
        assertThat(due.deadline()).as("an escalated row runs no deadline").isNull();
    }

    @Test
    void theStallFactorIsTheGroupsSettingAndDefaultsToThree() {
        OwnerFixture f = openRed();
        assertThat(f.input().group().stallFactor()).isEqualTo(3);
        f.stallFactor = 2;
        f.at(after(1)).ack("A", "Y");
        f.now(after(42));

        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("stalled", true);
    }

    @Test
    void anImplicitAckAlsoStartsTheStallWindow() {
        OwnerFixture f = openRed();
        f.at(after(15)).push("Z", "Y", "A");
        f.now(after(15).plus(Duration.ofMinutes(61)));

        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("stalled", true);
    }

    // ── S2: next up is nudged once, at half the deadline, only when nothing is acknowledged ──────────────────

    @Test
    void nextUpIsNudgedOnceAtHalfTheDeadlineWithNoAckAndTheRowNamesIt() {
        OwnerFixture f = openRed();
        f.now(after(9));
        OwnerDecision.Decision before = f.onDeadline();
        assertThat(before.post()).as("not yet half spent").isNull();
        assertThat(before.nudgeAt()).isEqualTo(after(10));
        assertThat(before.deadline()).isEqualTo(after(20));
        assertThat(before.wakeAt()).as("the timer wakes for the nudge first").isEqualTo(after(10));

        f.now(after(11));
        OwnerDecision.Decision nudge = f.onDeadline();

        assertThat(nudge.post().kind()).isEqualTo("nudge");
        assertThat(nudge.post().sha()).isEqualTo("Y");
        assertThat(OwnerFixture.bodyOf(nudge.post())).containsEntry("next", "C").containsEntry("owner", "A");
        assertThat(nudge.nudgeAt()).isNull();
        assertThat(nudge.deadline()).isEqualTo(after(20));

        f.recordNow(nudge);
        f.now(after(12));
        OwnerDecision.Decision again = f.onDeadline();
        assertThat(again.post()).as("once per episode").isNull();
        assertThat(again.nudgeAt()).isNull();
        assertThat(again.deadline()).isEqualTo(after(20));

        f.now(after(21));
        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).as("the nudge did not disturb the hand-off")
            .containsEntry("reason", "handoff");
    }

    @Test
    void noNudgeWhenNextUpIsNull() {
        OwnerFixture f = new OwnerFixture().seat("A").state("L", "lapsed", "leased");
        f.push("X", "G0", "A").done("X", "failure");
        f.recordNow(f.decide());
        f.now(after(11));

        OwnerDecision.Decision d = f.onDeadline();

        assertThat(d.post()).isNull();
        assertThat(d.nudgeAt()).isNull();
    }

    @Test
    void noNudgeOnceTheOwnerHasAcknowledgedOrPushed() {
        OwnerFixture acked = openRed();
        acked.at(after(2)).ack("A", "Y");
        acked.now(after(11));
        assertThat(acked.onDeadline().post()).as("explicit ack").isNull();

        OwnerFixture pushed = openRed();
        pushed.at(after(2)).push("Z", "Y", "A");
        pushed.now(after(11));
        assertThat(pushed.onDeadline().post()).as("implicit ack").isNull();
    }

    @Test
    void aHandOffRowCarriesNoNextUpSoItIsNeverNudged() {
        OwnerFixture f = openRed();
        f.now(after(21));
        f.recordNow(f.onDeadline());
        f.now(after(35));

        assertThat(f.onDeadline().post()).isNull();
    }

    // ── the fields the client notice reads (nexus-igea9 round 1, notice 5a69eaa086) ──────────────────────────

    @Test
    void theStallRowCarriesStalledOwnerStalledSinceAndRedSinceAsIsoInstantsTheNoticeSubtracts() {
        OwnerFixture f = openRed();
        f.at(after(7)).ack("A", "Y");
        f.now(after(70));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("stalled", true).containsEntry("stalled_owner", "A");
        Instant since = Instant.parse((String) b.get("stalled_since"));
        Instant red = Instant.parse((String) b.get("red_since"));
        assertThat(Duration.between(red, since)).as("the notice's 'after N min'").isEqualTo(Duration.ofMinutes(7));
    }

    @Test
    void theDisclaimHandOffRowNamesWhoDisclaimedAndAnOrdinaryHandOffDoesNot() {
        OwnerFixture disclaimed = openRed();
        disclaimed.at(after(2)).disclaim("A", "Y");
        disclaimed.now(after(2));
        assertThat(OwnerFixture.bodyOf(disclaimed.decide().post())).containsEntry("reason", "disclaimed")
            .containsEntry("previous_owner", "A");

        OwnerFixture missed = openRed();
        missed.now(after(21));
        assertThat(OwnerFixture.bodyOf(missed.onDeadline().post())).containsEntry("reason", "handoff")
            .doesNotContainKey("previous_owner");
    }
}
