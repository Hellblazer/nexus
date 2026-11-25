// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-252 Step 3: re-surfacing. A newer red with the same earliest suspect refreshes the row, and keeps the episode. */
class OwnerDecisionRefreshTest {

    private OwnerFixture firstRed() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.recordNow(f.decide());
        return f;
    }

    @Test
    void aFixThatGoesRedAgainPostsARefreshedRowWithTheSameEpisode() {
        OwnerFixture f = firstRed();
        Map<String, Object> first = OwnerFixture.MAPPER.convertValue(OwnerDecision.parse(f.owners.getLast().body()), new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() { });
        f.push("Z", "Y", "A").done("Z", "failure").now(OwnerFixture.T0.plus(Duration.ofHours(2)));

        OwnerDecision.Post post = f.decide().post();

        Map<String, Object> b = OwnerFixture.bodyOf(post);
        assertThat(post.sha()).isEqualTo("Z");
        assertThat(b).containsEntry("refresh", true).containsEntry("owner", "A").containsEntry("next", "C")
            .containsEntry("handoffs", 0).containsEntry("episode_at", first.get("episode_at"))
            .containsEntry("suspects", List.of("X", "Y", "Z")).containsEntry("suspects_total", 3);
    }

    @Test
    void afterAHandOffARefreshKeepsTheNewOwner() {
        OwnerFixture f = firstRed();
        Instant handedAt = f.now.plus(Duration.ofMinutes(21));   // inside the hard cap, which a 3 hour old red would meet first
        f.now(handedAt);
        OwnerDecision.Decision handoff = f.onDeadline();
        assertThat(OwnerFixture.bodyOf(handoff.post())).containsEntry("owner", "C").containsEntry("reason", "handoff");
        f.recordNow(handoff);
        f.push("Z", "Y", "A").done("Z", "failure");

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("refresh", true).containsEntry("owner", "C").containsEntry("handoffs", 1)
            .containsEntry("reason", "handoff").containsEntry("episode_at", handedAt.toString());
    }

    @Test
    void aDifferentEarliestSuspectIsANewEpisode() {
        OwnerFixture f = firstRed();
        // A late green on X moves the last green forward, so the red now starts at Y.
        f.done("X", "success");
        f.push("Z", "Y", "C").done("Z", "failure").now(OwnerFixture.T0.plus(Duration.ofHours(2)));

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("refresh", false).containsEntry("owner", "C")
            .containsEntry("episode_at", OwnerFixture.T0.plus(Duration.ofHours(2)).toString());
    }

    @Test
    void theSameRedTipTwiceIsNotPostedTwice() {
        OwnerFixture f = firstRed();
        f.done("Y", "failure");   // the adapter's retry

        assertThat(f.decide().post()).isNull();
    }
}
