// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-252 Step 3: the cases in which the leader takes the red, each with its named reason. */
class OwnerDecisionLeaderCasesTest {

    private static void leaderOwns(OwnerDecision.Decision d, String reason) {
        assertThat(d.post()).isNotNull();
        Map<String, Object> b = OwnerFixture.bodyOf(d.post());
        assertThat(b).containsEntry("owner", "L").containsEntry("reason", reason).containsEntry("refresh", false);
    }

    private OwnerFixture oneRed() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").done("X", "failure");
        return f;
    }

    @Test
    void anUnleasedOwner() {
        leaderOwns(oneRed().state("A", "live", "unleased").decide(), "owner_unleased");
    }

    @Test
    void aLapsedUnheldOrEndedOwner() {
        leaderOwns(oneRed().state("A", "lapsed", "leased").decide(), "owner_not_live");
        leaderOwns(oneRed().state("A", "unheld", null).decide(), "owner_not_live");
        leaderOwns(oneRed().state("A", "ended", null).decide(), "owner_not_live");
    }

    @Test
    void anOwnerWhoIsNotAMember() {
        leaderOwns(oneRed().notMember("A").decide(), "owner_not_member");
    }

    @Test
    void noStampedPushRecordAndAnUnmappedStamp() {
        OwnerFixture none = new OwnerFixture().seat("A");
        none.push("X", "G0", null).done("X", "failure");
        leaderOwns(none.decide(), "no_record");

        OwnerFixture unmapped = new OwnerFixture().seat("A");
        unmapped.pushStamped("X", "G0", "a-session-the-index-does-not-know").done("X", "failure");
        leaderOwns(unmapped.decide(), "no_record");
    }

    @Test
    void aRedTipInNoPushRecord() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A");
        f.done("Z", "failure");   // a hand push: only its CI post names it

        leaderOwns(f.decide(), "no_record");
    }

    @Test
    void aChainGapIsTheLeadersToo() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "UNRECORDED", "C").done("Y", "failure");

        leaderOwns(f.decide(), "chain_gap");
    }

    @Test
    void noRecordWinsOverAChainGap() {
        OwnerFixture f = new OwnerFixture().seat("C");
        f.push("X", "G0", null).push("Y", "UNRECORDED", "C").done("Y", "failure");

        leaderOwns(f.decide(), "no_record");
    }

    @Test
    void theFirstReasonThatHoldsIsPosted() {
        OwnerFixture f = oneRed().state("A", "live", "unleased").notMember("A");
        leaderOwns(f.decide(), "owner_unleased");
    }

    @Test
    void aGreenAnchoredByAVerdictOnlyTipIsNotAGap() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.done("G0", "success");                  // a hand push that passed: no push record
        f.push("X", "G0", "A").done("X", "failure");

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("owner", "A").containsEntry("reason", OwnerDecision.REASON_EARLIEST);
    }

    @Test
    void aStamperErrorPostsTheLeader() {
        OwnerFixture f = oneRed();
        OwnerDecision.Input in = f.input();

        OwnerDecision.Decision d = OwnerDecision.stamperError(in.group(), OwnerFixture.BASE, "X", in.owners(),
            Instant.parse("2026-11-18T13:00:00Z"));

        leaderOwns(d, "stamper_error");
        assertThat(d.post().kind()).isEqualTo("owner");
        assertThat(d.post().sha()).isEqualTo("X");
        assertThat(OwnerDecision.stamperError(null, OwnerFixture.BASE, "X", List.of(), Instant.EPOCH).post())
            .as("no live group: no row").isNull();
    }

    @Test
    void aStamperErrorRowRunsTheDeadlineFromItsEpisodeLikeAnyOwnerRow() {
        OwnerFixture f = oneRed();
        f.byTopic.put(OwnerFixture.BASE, 7);   // the topic's override wins over the group default, as for every owner row
        OwnerDecision.Input in = f.input();
        Instant now = Instant.parse("2026-11-18T13:00:00Z");

        OwnerDecision.Decision d = OwnerDecision.stamperError(in.group(), OwnerFixture.BASE, "X", in.owners(), now);

        assertThat(d.deadline()).as("counted from the row's episode_at, which is its post time")
            .isEqualTo(now.plus(java.time.Duration.ofMinutes(7)));
        assertThat(d.post().body()).containsEntry("episode_at", now.toString());
    }
}
