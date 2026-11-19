// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: the first test, the inherited red. A replaced run leaves a pushed tip untested, so a red that a
 * later push only inherited belongs to the earlier pusher.
 */
class OwnerDecisionTest {

    @Test
    void inheritedRedAcrossAReplacedRunIsOwnedByTheEarliestSuspectsPusher() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C");
        f.done("X", "cancelled").done("Y", "failure");

        OwnerDecision.Decision d = f.decide();

        assertThat(d.post()).as("an owner row").isNotNull();
        assertThat(d.post().kind()).isEqualTo("owner");
        assertThat(d.post().sha()).isEqualTo("Y");
        Map<String, Object> body = OwnerFixture.bodyOf(d.post());
        assertThat(body).containsEntry("owner", "A").containsEntry("next", "C")
            .containsEntry("suspects", List.of("X", "Y")).containsEntry("suspects_total", 2)
            .containsEntry("refresh", false).containsEntry("handoffs", 0);
    }

    @Test
    void theVerdictIsReadFromTheRunsWhenTheResultDimsAreAbsent() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C");
        f.doneRuns("X", "cancelled", "success").doneRuns("Y", "success", "failure");

        OwnerDecision.Decision d = f.decide();

        assertThat(d.post()).isNotNull();
        assertThat(d.post().sha()).isEqualTo("Y");
        assertThat(OwnerFixture.bodyOf(d.post())).containsEntry("owner", "A").containsEntry("suspects_total", 2);
    }

    private static java.util.Optional<DoneVerdict.Verdict> verdictOf(String body) {
        return DoneVerdict.of(new BoardRow("d1", OwnerFixture.T0,
            new java.util.LinkedHashMap<>(Map.of("from", "github", "kind", "sha", "sha", "Y")), body, null));
    }

    @Test
    void aTruncatedRunsListWithNoListedFailureHasNoVerdictButAListedFailureStillIsRed() {
        String ok = "[100,1,\"CI\",\"success\"]";
        String red = "[101,1,\"CI\",\"failure\"]";

        assertThat(verdictOf("{\"sha\":\"Y\",\"n\":3,\"runs\":[" + ok + "]}")).as("two runs were dropped").isEmpty();
        assertThat(verdictOf("{\"sha\":\"Y\",\"n\":3,\"runs\":[" + ok + "," + red + "]}")).get()
            .extracting(DoneVerdict.Verdict::outcome).isEqualTo("failure");
        assertThat(verdictOf("{\"sha\":\"Y\",\"n\":1,\"runs\":[" + ok + "]}")).get()
            .extracting(DoneVerdict.Verdict::outcome).as("n equals the list: nothing dropped").isEqualTo("success");
        assertThat(verdictOf("{\"sha\":\"Y\",\"runs\":[" + ok + "]}")).get()
            .extracting(DoneVerdict.Verdict::outcome).as("no n: nothing to say about dropped runs").isEqualTo("success");
    }
}
