// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-229 P3.3b (nexus-p2m1x.20): the per-leaf backoff after a failed build is 5 minutes, doubling, never more than an hour. */
class LeafIndexPassBackoffTest {

    @Test
    void theWaitStartsAtFiveMinutes_doublesWithEachFailure_andStopsAtAnHour() {
        assertThat(LeafIndexPass.backoffAfter(1)).isEqualTo(Duration.ofMinutes(5));
        assertThat(LeafIndexPass.backoffAfter(2)).isEqualTo(Duration.ofMinutes(10));
        assertThat(LeafIndexPass.backoffAfter(3)).isEqualTo(Duration.ofMinutes(20));
        assertThat(LeafIndexPass.backoffAfter(4)).isEqualTo(Duration.ofMinutes(40));
        assertThat(LeafIndexPass.backoffAfter(5)).isEqualTo(Duration.ofHours(1));
        assertThat(LeafIndexPass.backoffAfter(50)).as("no overflow, never past the cap").isEqualTo(Duration.ofHours(1));
    }
}
