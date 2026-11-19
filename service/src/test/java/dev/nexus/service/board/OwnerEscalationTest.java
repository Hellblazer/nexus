// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-252 Step 3: owner, then next up, then Sam. */
class OwnerEscalationTest {

    private static final Instant EPISODE = OwnerFixture.T0.plus(Duration.ofHours(1));

    private OwnerFixture openRed() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.recordNow(f.decide());
        return f;
    }

    /** The two missed deadlines: the first hands the red on, the second escalates. */
    private OwnerFixture escalated() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(21)));
        f.recordNow(f.onDeadline());
        f.now(EPISODE.plus(Duration.ofMinutes(50)));
        f.recordNow(f.onDeadline());
        return f;
    }

    @Test
    void twoMissedDeadlinesGiveOneEscalatedRow() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(21)));
        OwnerDecision.Decision first = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(first.post())).containsEntry("reason", "handoff").containsEntry("owner", "C");
        f.recordNow(first);
        f.now(EPISODE.plus(Duration.ofMinutes(50)));

        OwnerDecision.Decision second = f.onDeadline();

        Map<String, Object> b = OwnerFixture.bodyOf(second.post());
        assertThat(b).containsEntry("reason", "escalated").containsEntry("owner", "L").containsEntry("next", null)
            .containsEntry("refresh", false).containsEntry("handoffs", 2);
        assertThat(second.deadline()).isNull();
    }

    @Test
    void anOwnerWithNoNextUpWhoMissesEscalatesAtOnce() {
        OwnerFixture f = new OwnerFixture().seat("A").state("L", "lapsed", "leased");
        f.push("X", "G0", "A").done("X", "failure");
        f.recordNow(f.decide());
        f.now(EPISODE.plus(Duration.ofMinutes(21)));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("reason", "escalated").containsEntry("owner", "A").containsEntry("handoffs", 1);
    }

    @Test
    void noHandOffAndNoSecondEscalatedRowFollowsIt() {
        OwnerFixture f = escalated();
        f.now(EPISODE.plus(Duration.ofHours(5)));

        assertThat(f.onDeadline().post()).isNull();
        assertThat(f.decide().post()).isNull();
        assertThat(f.onDeadline().deadline()).as("also read fresh, as after an engine restart").isNull();
    }

    @Test
    void aRefreshAfterItCarriesReasonEscalated() {
        OwnerFixture f = escalated();
        f.push("Z", "Y", "A").done("Z", "failure");
        f.now(EPISODE.plus(Duration.ofHours(2)));

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("refresh", true).containsEntry("reason", "escalated").containsEntry("handoffs", 2);
        f.recordNow(f.decide());
        assertThat(f.onDeadline().post()).as("a refresh does not restart the deadline").isNull();
    }

    @Test
    void withNobodyLiveTheRowNamesNoOne() {
        OwnerFixture f = openRed();
        f.state("A", "lapsed", "leased").state("C", "lapsed", "leased").state("L", "lapsed", "leased");
        f.now(EPISODE.plus(Duration.ofMinutes(21)));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("reason", "escalated").containsEntry("owner", null).containsEntry("next", null);
    }

    @Test
    void escalationNamesTheFirstLiveMemberWhenTheLeaderIsNotLive() {
        OwnerFixture f = openRed();
        f.state("L", "lapsed", "leased");
        f.now(EPISODE.plus(Duration.ofMinutes(21)));
        f.recordNow(f.onDeadline());                       // handed to C
        f.now(EPISODE.plus(Duration.ofMinutes(50)));

        Map<String, Object> b = OwnerFixture.bodyOf(f.onDeadline().post());

        assertThat(b).containsEntry("reason", "escalated").containsEntry("owner", "A");
    }

    @Test
    void aDisbandDuringAnOpenRedGivesNoHandOffOrEscalatedRowAndTheGreenStillClears() {
        OwnerFixture f = openRed();
        f.served = false;
        f.now(EPISODE.plus(Duration.ofHours(2)));
        assertThat(f.onDeadline().post()).isNull();

        f.push("Z", "Y", "A").done("Z", "success");
        OwnerDecision.Post clear = f.decide().post();
        assertThat(clear).isNotNull();
        assertThat(clear.kind()).isEqualTo("clear");
    }
}
