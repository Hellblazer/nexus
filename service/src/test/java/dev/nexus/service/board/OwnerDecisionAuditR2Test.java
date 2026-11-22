// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Confirmation round 2 of the RDR-252 audit fix: a postponing push holds the deadline until its own verdict arrives
 * (live push-to-verdict ran over a deadline for 11 of 23 pushes), a stall or cap escalation says how long the red has
 * been open and what kind of sign it saw, and a push by a member who is not live postpones nothing.
 */
class OwnerDecisionAuditR2Test {

    private static final Instant EPISODE = OwnerFixture.T0.plus(Duration.ofHours(1));

    private static Instant after(long minutes) {
        return EPISODE.plus(Duration.ofMinutes(minutes));
    }

    /** t0: red X pushed by A, owner A; t0+20: handed to the leader L (no ack). C is a member who owns nothing. */
    private OwnerFixture handedToTheLeader() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.now(after(20));
        OwnerDecision.Decision handed = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(handed.post())).containsEntry("owner", "L");
        f.recordNow(handed);
        return f;
    }

    // ── 1: a postponing push holds the deadline until its own verdict ────────────────────────────────────────

    @Test
    void aPostponingPushHoldsTheDeadlineUntilItsOwnVerdictThenCountsOneDeadlineFromThatVerdict() {
        OwnerFixture f = handedToTheLeader();
        f.at(after(21)).push("Z1", "X", "C");           // member C pushes the fix; CI takes 30 minutes

        f.now(after(50));
        OwnerDecision.Decision held = f.onDeadline();
        assertThat(held.post()).as("the fix is still in CI at t0+50").isNull();
        assertThat(held.deadline()).as("held until the verdict, bounded by the hard cap").isEqualTo(after(120));

        f.at(after(51)).done("Z1", "failure");           // the verdict lands red at t0+51
        f.now(after(51));
        f.recordNow(f.decide());                          // the row is refreshed to the new tip, the leader keeps the red
        f.now(after(70));
        OwnerDecision.Decision counting = f.onDeadline();
        assertThat(counting.post()).as("no escalation before the verdict time plus one deadline").isNull();
        assertThat(counting.deadline()).isEqualTo(after(71));

        f.now(after(72));
        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());
        assertThat(b).containsEntry("reason", "escalated").doesNotContainKey("stalled");
    }

    @Test
    void aPostponingPushWhoseVerdictNeverArrivesEscalatesAtTheHardCap() {
        OwnerFixture f = handedToTheLeader();
        f.at(after(21)).push("Z1", "X", "C");

        f.now(after(119));
        assertThat(f.onDeadline().post()).as("held, still inside the cap").isNull();
        f.now(after(121));
        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("reason", "escalated").containsEntry("stalled", true).containsEntry("red_since", EPISODE.toString())
            .containsEntry("stalled_owner", "C").containsEntry("stalled_sign", "push");
    }

    @Test
    void aPushByAMemberWhoIsNotLiveHoldsAndPostponesNothing() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.state("C", "lapsed", "leased");               // a member, but not live
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.at(after(5)).push("Z1", "X", "C");
        f.now(after(21));

        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("reason", "handoff");
    }

    @Test
    void aHeldDeadlineHasNoNudgeEitherWhileTheMembersFixIsStillInCi() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());                        // owner A, next up the leader L
        f.at(after(5)).push("Z1", "X", "C");            // member C pushes on the red tip after the verdict; no verdict for Z1 yet
        f.now(after(11));                               // past half the deadline, where a nudge would otherwise be due

        OwnerDecision.Decision d = f.onDeadline();

        assertThat(d.post()).as("no owner row and no nudge: the fix is in CI").isNull();
        assertThat(d.nudgeAt()).as("none pending either").isNull();
    }

    // ── 2: open_minutes and stalled_sign ─────────────────────────────────────────────────────────────────────

    @Test
    void aStallRowSaysHowLongTheRedHasBeenOpenAndWhetherTheSignWasAnAck() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.at(after(3)).ack("A", "Y");
        f.now(after(64));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("stalled", true).containsEntry("stalled_sign", "ack").containsEntry("stalled_owner", "A")
            .containsEntry("open_minutes", 64).containsEntry("red_since", EPISODE.toString());
    }

    @Test
    void aStallRowFromTheOwnersOwnPushesSaysPush() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.at(after(5)).push("Z1", "X", "A");
        f.now(after(70));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("stalled", true).containsEntry("stalled_sign", "push").containsEntry("open_minutes", 70);
    }

    @Test
    void aCapEscalationWithNoSignHasNoStalledOwnerSinceOrSignButStillSaysHowLongItWasOpen() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.stallFactor = 1;                              // cap = 2 x 1 x 20 = 40 minutes
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.now(after(35));                               // the timer was late: the hand-off to the leader is made at t0+35
        OwnerDecision.Decision handed = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(handed.post())).containsEntry("reason", "handoff");
        f.recordNow(handed);                            // its own deadline, t0+55, is past the cap, t0+40
        f.now(after(41));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("reason", "escalated").containsEntry("stalled", true).containsEntry("open_minutes", 41)
            .containsEntry("red_since", EPISODE.toString())
            .doesNotContainKeys("stalled_owner", "stalled_since", "stalled_sign");
    }
}
