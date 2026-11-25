// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/** The window and the two figures of {@link UnrecordedTips}, on recorded rows. */
class UnrecordedTipsTest {

    private static final Instant NOW = Instant.parse("2026-11-22T12:00:00Z");

    private static BoardRow done(String sha, Instant expires) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        dims.put("sha", sha);
        dims.put("outcome", "success");
        return new BoardRow("d-" + sha, NOW.minus(Duration.ofHours(20)), dims, "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}",
            null, expires);
    }

    private static BoardRow push(String sha, String session) {
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "x");
        dims.put("kind", "push");
        dims.put("sha", sha);
        return new BoardRow("p-" + sha, NOW.minus(Duration.ofHours(21)), dims, "{\"base\":\"G0\",\"n\":1}", session, null);
    }

    @Test
    void aDoneRowInItsLastTwoHoursOfLifeIsLeftOutSoRetentionSkewGivesNoFalsePositive() {
        List<BoardRow> dones = List.of(
            done("EXPIRING", NOW.plus(Duration.ofHours(1))),      // its push row (made earlier) is already gone
            done("EDGE", NOW.plus(Duration.ofHours(2))),          // exactly the margin: still left out
            done("FRESH", NOW.plus(Duration.ofHours(3))),
            done("UNKNOWN", null));                               // a row whose expiry was not read counts

        assertThat(UnrecordedTips.missing(List.of(), dones, NOW)).containsExactly("FRESH", "UNKNOWN");
    }

    @Test
    void aTipCountsOnceHoweverManyDoneRowsItHasAndOnlyTheRecordedOnesDropOut() {
        List<BoardRow> dones = List.of(done("A", null), done("A", null), done("B", null));

        assertThat(UnrecordedTips.missing(List.of(push("B", "s1")), dones, NOW)).containsExactly("A");
    }

    @Test
    void aRecordWithANullBlankOrUnmappedSessionIsUnattributedAndAMappedOneIsNot() {
        List<BoardRow> dones = List.of(done("NULL", null), done("BLANK", null), done("UNMAPPED", null), done("MAPPED", null),
            done("NORECORD", null));
        List<BoardRow> pushes = List.of(push("NULL", null), push("BLANK", " "), push("UNMAPPED", "s-gone"), push("MAPPED", "s-ok"));

        assertThat(UnrecordedTips.unattributed(pushes, dones, NOW, Set.of("s-ok")))
            .as("a tip with no record at all is the other figure, not this one")
            .containsExactly("NULL", "BLANK", "UNMAPPED");
    }
}
