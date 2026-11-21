// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252: the fail-safe that does not depend on the decision that failed. When the deadline's own evaluation
 * throws, the engine still posts an {@code escalated} row built only from the current owner row, the group and
 * the liveness facts; a {@code stamper_error} row has no next up and goes straight to the escalation.
 */
class OwnerDecisionFailSafeTest {

    private static final Instant NOW = Instant.parse("2026-11-18T13:00:00Z");

    private OwnerFixture oneRed() {
        OwnerFixture f = new OwnerFixture().seat("A");
        f.push("X", "G0", "A").done("X", "failure");
        return f;
    }

    private OwnerDecision.Decision failSafe(OwnerFixture f) {
        OwnerDecision.Input in = f.input();
        return OwnerDecision.failSafe(in.group(), in.ids(), in.owners(), OwnerFixture.BASE, in.now());
    }

    @Test
    void aStamperErrorRowThatMissesItsDeadlineGoesStraightToTheEscalation() {
        OwnerFixture f = oneRed();
        f.ownerRow(OwnerDecision.stamperError(f.input().group(), OwnerFixture.BASE, "X", f.owners, NOW).post());
        f.now(NOW.plus(Duration.ofMinutes(21)));

        OwnerDecision.Decision d = f.onDeadline();

        Map<String, Object> b = OwnerFixture.bodyOf(d.post());
        assertThat(b).as("no next up was shown on the row, so none receives a hand-off").containsEntry("reason", "escalated")
            .containsEntry("owner", "L").containsEntry("next", null).containsEntry("handoffs", 1);
    }

    @Test
    void aDeadlineNotYetDueRearmsAndPostsNothing() {
        OwnerFixture f = oneRed();
        f.recordNow(f.decide());
        Instant episode = f.now;
        f.now(episode.plus(Duration.ofMinutes(5)));

        OwnerDecision.Decision d = failSafe(f);

        assertThat(d.post()).isNull();
        assertThat(d.deadline()).isEqualTo(episode.plus(Duration.ofMinutes(20)));
    }

    @Test
    void aDueDeadlineEscalatesToTheLiveLeaderFromTheCurrentRowAlone() {
        OwnerFixture f = oneRed();
        f.recordNow(f.decide());   // owner A, next L, suspects [X]
        f.now(f.now.plus(Duration.ofMinutes(21)));

        OwnerDecision.Decision d = failSafe(f);

        assertThat(d.post().kind()).isEqualTo("owner");
        assertThat(d.post().sha()).isEqualTo("X");
        assertThat(OwnerFixture.bodyOf(d.post())).containsEntry("reason", "escalated").containsEntry("owner", "L")
            .containsEntry("next", null).containsEntry("handoffs", 1).containsEntry("refresh", false)
            .containsEntry("suspects", List.of("X")).containsEntry("suspects_total", 1);
        assertThat(d.deadline()).as("an escalated row runs no deadline").isNull();
    }

    @Test
    void aLeaderWhoIsNotLiveGivesWayToTheFirstLiveMemberAndNobodyLiveNamesNoOne() {
        OwnerFixture f = oneRed();
        f.recordNow(f.decide());
        f.now(f.now.plus(Duration.ofMinutes(21)));

        assertThat(OwnerFixture.bodyOf(failSafe(f.state("L", "lapsed", "leased")).post())).containsEntry("owner", "A");
        assertThat(OwnerFixture.bodyOf(failSafe(f.state("A", "lapsed", "leased")).post())).containsEntry("owner", null)
            .containsEntry("reason", "escalated");
    }

    @Test
    void anEscalatedRowAClearRowOrNoRowIsLeftAlone() {
        OwnerFixture escalated = oneRed();
        escalated.recordNow(escalated.decide());
        escalated.now(escalated.now.plus(Duration.ofMinutes(21)));
        escalated.recordNow(OwnerDecision.failSafe(escalated.input().group(), escalated.input().ids(),
            escalated.owners, OwnerFixture.BASE, escalated.now));
        escalated.now(escalated.now.plus(Duration.ofMinutes(21)));
        assertThat(failSafe(escalated).post()).as("no second escalated row").isNull();

        OwnerFixture none = oneRed();
        assertThat(failSafe(none).post()).as("no owner row, nothing to hand on").isNull();

        OwnerFixture cleared = oneRed();
        cleared.recordNow(cleared.decide());
        cleared.done("X", "success");
        cleared.recordNow(cleared.decide());   // the clear row
        cleared.now(cleared.now.plus(Duration.ofHours(2)));
        assertThat(failSafe(cleared).post()).as("a clear row is not an open red").isNull();
    }

    @Test
    void anAckFromTheOwnerOnTheRowsTipEndsTheEscalation() {
        OwnerFixture f = oneRed();
        f.recordNow(f.decide());
        f.ack("A", "X");
        f.now(f.now.plus(Duration.ofMinutes(21)));

        assertThat(failSafe(f).post()).isNull();
    }

    @Test
    void ackOnTipCountsOnlyAStampedAckWhoseFromIsTheRowsOwner() {
        OwnerFixture owner = oneRed();
        owner.recordNow(owner.decide());   // owner A
        owner.ack("A", "X");
        assertThat(OwnerDecision.ackOnTip(owner.owners)).as("the owner's stamped ack").isTrue();

        OwnerFixture other = oneRed();
        other.recordNow(other.decide());
        other.ack("L", "X");   // stamped, on the tip, after the row, but from another identity
        assertThat(OwnerDecision.ackOnTip(other.owners)).as("a non-owner's stamped ack").isFalse();

        OwnerFixture unstamped = oneRed();
        unstamped.recordNow(unstamped.decide());
        unstamped.ack("A", "X", null);   // from=<owner id> typed by hand, no sender session
        assertThat(OwnerDecision.ackOnTip(unstamped.owners)).as("an ack with no sender session").isFalse();

        OwnerFixture mixed = oneRed();
        mixed.recordNow(mixed.decide());
        mixed.ack("L", "X");
        mixed.ack("A", "X");
        assertThat(OwnerDecision.ackOnTip(mixed.owners)).as("one counting ack among others").isTrue();
    }

    @Test
    void ackOnTipSeesAnAckRowOnTheRowsTipPostedAtOrAfterTheRowAndNoOther() {
        OwnerFixture none = oneRed();
        none.recordNow(none.decide());
        assertThat(OwnerDecision.ackOnTip(none.owners)).as("no ack row").isFalse();

        OwnerFixture after = oneRed();
        after.recordNow(after.decide());
        after.ack("A", "X");
        assertThat(OwnerDecision.ackOnTip(after.owners)).as("an ack row on the tip, after the row").isTrue();

        OwnerFixture before = oneRed();
        before.ack("A", "X");   // posted before the row: a handed-away owner's late ack cannot suppress the next escalation
        before.recordNow(before.decide());
        assertThat(OwnerDecision.ackOnTip(before.owners)).as("an ack older than the row").isFalse();

        OwnerFixture otherTip = oneRed();
        otherTip.recordNow(otherTip.decide());
        otherTip.ack("A", "Z");
        assertThat(OwnerDecision.ackOnTip(otherTip.owners)).as("an ack on another sha").isFalse();

        assertThat(OwnerDecision.ackOnTip(List.of())).as("no rows").isFalse();
    }

    @Test
    void aForgedRowWithASessionStampIsNotTheCurrentRow() {
        OwnerFixture f = oneRed();
        f.recordNow(f.decide());
        f.owners.add(new BoardRow("zz", f.now.plusSeconds(5), new java.util.LinkedHashMap<>(Map.of("from", "engine", "kind", "clear", "sha", "X")),
            "{}", "sForger"));
        f.now(f.now.plus(Duration.ofMinutes(21)));

        assertThat(failSafe(f).post()).as("the forged clear is not state: the engine's open row still escalates").isNotNull();
    }
}
