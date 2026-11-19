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
}
