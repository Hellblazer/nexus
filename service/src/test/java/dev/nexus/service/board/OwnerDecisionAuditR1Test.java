// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Review round 1 of the RDR-252 audit fix (da1462a038): the implicit ack is bounded by the red verdict, a push by a
 * former owner postpones the current deadline (the incident as it happened), the postponement has a hard cap, the stall
 * row says who and since when, a disclaim counts on any red tip of the episode, and the fail-safe stalls an acked red.
 */
class OwnerDecisionAuditR1Test {

    private static final Instant EPISODE = OwnerFixture.T0.plus(Duration.ofHours(1));

    private static Instant after(long minutes) {
        return EPISODE.plus(Duration.ofMinutes(minutes));
    }

    // ── 1: a push before the red verdict is no ack ───────────────────────────────────────────────────────────

    @Test
    void anOwnersFollowUpPushBeforeTheRedVerdictIsNoAckSoItGetsTheNudgeAndTheHandOff() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").push("Y", "X", "A").done("X", "failure");   // A pushed Y on top before X's verdict came in
        f.now(EPISODE);
        OwnerDecision.Decision born = f.decide();
        assertThat(OwnerFixture.bodyOf(born.post())).containsEntry("owner", "A").containsEntry("next", "L");
        assertThat(born.deadline()).as("the 20-minute hand-off deadline, not a 60-minute stall").isEqualTo(after(20));
        assertThat(born.nudgeAt()).isEqualTo(after(10));
        f.recordNow(born);

        f.now(after(11));
        assertThat(f.onDeadline().post().kind()).as("the nudge").isEqualTo("nudge");
        f.now(after(21));
        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("reason", "handoff").containsEntry("owner", "L");
    }

    @Test
    void aNextUpWhoPushedOnTopBeforeTheVerdictIsNotAckedWhenTheRedIsHandedToIt() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.leader = "C";                                // C is next up as the leader
        f.push("X", "G0", "A").push("Y", "X", "C").done("X", "failure");   // C pushed Y on top of the red suspect before the verdict
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.now(after(21));
        OwnerDecision.Decision handed = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(handed.post())).containsEntry("owner", "C").containsEntry("reason", "handoff");
        f.recordNow(handed);

        f.now(after(22));
        OwnerDecision.Decision d = f.decide();

        assertThat(d.deadline()).as("C is not acked at birth: the plain deadline from the hand-off, not a stall window")
            .isEqualTo(after(21).plus(Duration.ofMinutes(20)));
        f.now(after(42));
        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("reason", "escalated")
            .doesNotContainKey("stalled");
    }

    // ── 2: the incident as it happened ───────────────────────────────────────────────────────────────────────

    @Test
    void theOriginalOwnersPushAfterTheHandOffPostponesTheLeadersDeadline() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());                       // t0: red, owner A, next up the leader L
        f.now(after(20));
        OwnerDecision.Decision handed = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(handed.post())).containsEntry("owner", "L");
        f.recordNow(handed);                           // t0+20: handed to the leader
        f.at(after(21).plusSeconds(8)).push("Z", "X", "A");   // t0+21:08: A pushes the fix; the tip stays red

        f.now(after(40));
        OwnerDecision.Decision d = f.onDeadline();

        assertThat(d.post()).as("no escalation at t0+40: a former owner of the episode is pushing").isNull();
        assertThat(d.deadline()).as("the leader's deadline counts from the push").isEqualTo(after(21).plusSeconds(8).plus(Duration.ofMinutes(20)));
        f.now(after(42));
        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("reason", "escalated");
    }

    // ── 3: the hard cap ──────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aFixFailLoopOfOwnerPushesEscalatesAtTwiceTheStallWindowFromTheRedsFirstRow() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());                       // red since EPISODE, owner A
        String prev = "X";
        for (int i = 1; i <= 4; i++) {                 // A pushes a fix every 25 minutes and each one goes red
            String sha = "Z" + i;
            f.at(after(25L * i)).push(sha, prev, "A").done(sha, "failure");
            f.now(after(25L * i + 1));
            f.recordNow(f.decide());                   // the refresh row
            prev = sha;
        }
        f.now(after(119));
        assertThat(f.onDeadline().post()).as("inside the cap, A's pushes keep postponing").isNull();

        f.now(after(121));
        OwnerDecision.Decision capped = f.onDeadline();

        Map<String, Object> b = OwnerFixture.bodyOf(capped.post());
        assertThat(b).containsEntry("reason", "escalated").containsEntry("stalled", true);
        assertThat(b).containsEntry("stalled_owner", "A").containsEntry("red_since", EPISODE.toString());
    }

    // ── 4: the stall row says who and since when ─────────────────────────────────────────────────────────────

    @Test
    void theStalledRowNamesWhoAckedSinceWhenAndSinceWhenTheRedWasRedAndKeepsEpisodeAtsMeaning() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.at(after(3)).ack("A", "Y");
        f.now(after(64));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("stalled", true).containsEntry("stalled_owner", "A")
            .containsEntry("stalled_since", after(3).toString()).containsEntry("red_since", EPISODE.toString())
            .containsEntry("episode_at", after(64).toString());
    }

    // ── 5: a disclaim counts on any red tip of the episode ───────────────────────────────────────────────────

    @Test
    void aDisclaimOnAnEarlierRedTipStillCountsAfterTheRowWasRefreshed() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());                       // red Y, owner A
        f.push("Z", "Y", "C").done("Z", "failure");
        f.now(after(1));
        f.recordNow(f.decide());                       // refreshed to Z, owner A kept
        f.at(after(2)).disclaim("A", "Y");             // A disclaims the tip it was shown first
        f.now(after(2));

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("owner", "C").containsEntry("reason", "disclaimed");
    }

    // ── 6: the fail-safe stalls an acked red ─────────────────────────────────────────────────────────────────

    @Test
    void theFailSafeEscalatesAnAckedRedOnceItsStallWindowFromTheAckIsPast() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.at(after(2)).ack("A", "X");

        f.now(after(30));
        OwnerDecision.Input in = f.input();
        OwnerDecision.Decision early = OwnerDecision.failSafe(in.group(), in.ids(), in.owners(), OwnerFixture.BASE, in.now());
        assertThat(early.post()).as("acked, and inside the stall window").isNull();
        assertThat(early.deadline()).as("armed for the stall deadline, not cancelled").isEqualTo(after(62));

        f.now(after(63));
        in = f.input();
        OwnerDecision.Decision late = OwnerDecision.failSafe(in.group(), in.ids(), in.owners(), OwnerFixture.BASE, in.now());

        Map<String, Object> b = OwnerFixture.bodyOf(late.post());
        assertThat(b).containsEntry("reason", "escalated").containsEntry("stalled", true).containsEntry("stalled_owner", "A")
            .containsEntry("stalled_since", after(2).toString());
    }

    // ── a member's push on the red restarts the deadline (colophon fixing a red handed to halcyon) ───────────────

    /** Pushes fix number {@code n} (1 based) by {@code pusher} at {@code minutes} after t0, on the one before it. */
    private static void pushFix(OwnerFixture f, String pusher, int n, long minutes) {
        f.at(after(minutes)).push("Z" + n, n == 1 ? "X" : "Z" + (n - 1), pusher);
    }

    @Test
    void aGroupMembersPushOnTheRedRestartsTheCurrentOwnersDeadlineUntilTheHardCapEscalates() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());                       // t0: red, owner A, next up the leader L
        f.now(after(20));
        OwnerDecision.Decision handed = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(handed.post())).containsEntry("owner", "L");
        f.recordNow(handed);                           // t0+20: handed to L; C is a member who never owned the red
        pushFix(f, "C", 1, 21);                        // t0+21: C pushes the fix; the red stays open

        f.now(after(40));
        OwnerDecision.Decision d = f.onDeadline();
        assertThat(d.post()).as("no hand-off or escalation at t0+40: member C is fixing it").isNull();
        assertThat(d.deadline()).as("restarted from the push, the plain 20 minutes, no stall window")
            .isEqualTo(after(21).plus(Duration.ofMinutes(20)));

        for (int n = 2; n <= 6; n++) {                 // C keeps pushing, every 19 minutes, and the red stays open
            pushFix(f, "C", n, 21 + 19L * (n - 1));
            f.now(after(21 + 19L * (n - 1) + 1));
            assertThat(f.onDeadline().post()).as("push " + n + " postpones it again").isNull();
        }
        f.now(after(119));
        assertThat(f.onDeadline().post()).as("still inside the hard cap").isNull();
        f.now(after(121));
        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());
        assertThat(b).as("the hard cap binds however many pushes").containsEntry("reason", "escalated")
            .containsEntry("stalled", true).containsEntry("red_since", EPISODE.toString());
    }

    @Test
    void theSessionThatCausedTheRedPushesTheFixAfterTheLeaderGotItAndOnlyTheLeadersDeadlineRestarts() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").done("X", "failure");   // A pushed the commit that went red
        f.now(EPISODE);
        f.recordNow(f.decide());                       // owner A, next up the leader L (the conductor)
        f.now(after(20));
        OwnerDecision.Decision handed = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(handed.post())).containsEntry("owner", "L").containsEntry("reason", "handoff");
        f.recordNow(handed);                           // no ack: the red went to the leader
        pushFix(f, "A", 1, 21);                        // A, now a non-owner, pushes the fix chained from the red tip

        f.now(after(30));
        OwnerDecision.Decision d = f.onDeadline();
        assertThat(d.post()).isNull();
        assertThat(d.deadline()).as("the leader's deadline restarts from A's push: no ack, so no stall window")
            .isEqualTo(after(21).plus(Duration.ofMinutes(20)));
        f.now(after(42));
        OwnerDecision.Decision late = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(late.post())).as("if the red is still open at the restarted deadline it escalates, unstalled")
            .containsEntry("reason", "escalated").doesNotContainKey("stalled");
    }

    @Test
    void aPushByAnIdentityOutsideTheGroupChangesNothing() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("N");
        f.notMember("N");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());
        f.at(after(5)).push("Z", "X", "N");
        f.now(after(21));

        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).containsEntry("reason", "handoff");
    }

    @Test
    void aMembersPushIsNoAckForTheCurrentOwnerSoItGetsNoStallWindow() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").done("X", "failure");
        f.now(EPISODE);
        f.recordNow(f.decide());                       // owner A
        f.at(after(15)).push("Z", "X", "C");           // a member's push restarts A's deadline from t0+15 ...
        f.now(after(30));

        OwnerDecision.Post inside = f.onDeadline().post();
        assertThat(inside == null || "nudge".equals(inside.kind())).as("inside the restarted 20 minutes: no owner row").isTrue();
        f.now(after(36));
        assertThat(OwnerFixture.bodyOf(f.onDeadline().post())).as("... and A, who never acked, is still handed on at its end")
            .containsEntry("reason", "handoff");
    }
}
