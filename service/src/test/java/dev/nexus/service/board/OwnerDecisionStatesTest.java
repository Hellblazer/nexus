// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-252 Step 3: owner, next up and everyone else, from fixed push records. */
class OwnerDecisionStatesTest {

    private static Map<String, Object> body(OwnerDecision.Decision d) {
        assertThat(d.post()).isNotNull();
        return OwnerFixture.bodyOf(d.post());
    }

    @Test
    void ownerNextUpAndEveryoneElse() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("B").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "B").done("X", "success").done("Y", "failure");
        f.push("Z", "Y", "C").done("Z", "failure");

        Map<String, Object> b = body(f.decide());

        assertThat(b).containsEntry("owner", "B").containsEntry("next", "C");
        assertThat(b.values()).as("everyone else (A, L) is named nowhere").doesNotContain("A", "L");
    }

    @Test
    void nextUpIsNeverTheOwner() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").push("Y", "X", "A").done("Y", "failure");

        Map<String, Object> b = body(f.decide());

        assertThat(b).containsEntry("owner", "A");
        assertThat(b.get("next")).as("both suspects are A's, so the leader is next up").isEqualTo("L");
    }

    @Test
    void nextUpSkipsALapsedPusherAndANonMember() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("B").seat("N").seat("C");
        f.state("B", "lapsed", "leased").notMember("N");
        f.push("X", "G0", "A").push("Y", "X", "B").push("Z", "Y", "N").push("W", "Z", "C").done("W", "failure");

        Map<String, Object> b = body(f.decide());

        assertThat(b).containsEntry("owner", "A").containsEntry("next", "C");
    }

    @Test
    void nextUpFallsToTheLeaderThenTheFirstOtherLiveMemberThenNull() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("M1").seat("M2");
        f.push("X", "G0", "A").done("X", "failure");
        assertThat(body(f.decide())).as("the leader").containsEntry("next", "L");

        f.state("L", "lapsed", "leased");
        assertThat(body(f.decide())).as("the first other live member by join order").containsEntry("next", "M1");

        f.state("M1", "unheld", null).state("M2", "ended", null);
        assertThat(body(f.decide()).get("next")).as("nobody else is live").isNull();
    }

    @Test
    void aDelegatedIdentityOnTheRosterIsNotNextUp() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("D").seat("M");
        f.kind("D", "delegated").state("L", "lapsed", "leased");
        f.push("X", "G0", "A").done("X", "failure");

        Map<String, Object> b = body(f.decide());

        assertThat(b).as("D joined the roster before the group had a record; M is the live seat").containsEntry("next", "M");
    }

    @Test
    void anUnleasedMemberIsNotLiveForNextUp() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("U").seat("M");
        f.state("U", "live", "unleased").state("L", "ended", null);
        f.push("X", "G0", "A").done("X", "failure");

        assertThat(body(f.decide())).containsEntry("next", "M");
    }
}
