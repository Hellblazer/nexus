// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-252 Step 3: commit order. Late reds and greens in both orders, and arrival time never orders linked tips. */
class OwnerDecisionOrderTest {

    private OwnerFixture base() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("B");
        f.push("G", "G0", "A").done("G", "success");
        f.push("P1", "G", "A").push("P2", "P1", "B");
        return f;
    }

    @Test
    void aCancelledRunsTipIsASuspect() {
        OwnerFixture f = base();
        f.done("P1", "cancelled").done("P2", "failure");

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("suspects", List.of("P1", "P2")).containsEntry("owner", "A");
    }

    @Test
    void aLateRedOnAnOlderTipRaisesNothing() {
        OwnerFixture f = base();
        f.done("P2", "success").done("P1", "failure");

        assertThat(f.decide().post()).as("P2, the newer tip, is green and nothing was open").isNull();
    }

    @Test
    void aLateRedOnAnOlderTipDoesNotReplaceTheOpenOwnerRow() {
        OwnerFixture f = base();
        f.done("P2", "failure");
        f.recordNow(f.decide());
        f.done("P1", "failure");

        assertThat(f.decide().post()).isNull();
    }

    @Test
    void aLateGreenOnAnOlderTipDoesNotClearANewerRed() {
        OwnerFixture f = base();
        f.done("P2", "failure");
        f.recordNow(f.decide());
        f.done("P1", "success");

        assertThat(f.decide().post()).as("P2 is still the newest tip with a verdict, and red").isNull();
    }

    @Test
    void aGreenClearsInBothArrivalOrders() {
        OwnerFixture first = base();                      // red on P1 first, then the green on P2
        first.done("P1", "failure");
        first.recordNow(first.decide());
        first.done("P2", "success");
        OwnerDecision.Post clear = first.decide().post();
        assertThat(clear).isNotNull();
        assertThat(clear.kind()).isEqualTo("clear");
        assertThat(clear.sha()).isEqualTo("P2");

        OwnerFixture second = base();                     // the green on P2 first, then a late red on P1
        second.done("P2", "success");
        assertThat(second.decide().post()).as("no open owner row, so no clear").isNull();
        second.done("P1", "failure");
        assertThat(second.decide().post()).as("a late red on an older tip raises nothing").isNull();
    }

    @Test
    void aClearIsPostedOnceAndOnlyForAnOpenOwnerRow() {
        OwnerFixture f = base();
        f.done("P1", "failure");
        f.recordNow(f.decide());
        f.done("P2", "failure");
        f.recordNow(f.decide());   // a refreshed row: the same earliest suspect
        f.done("P2", "success");   // a rerun of P2 that passed
        OwnerDecision.Post clear = f.decide().post();
        assertThat(clear).isNotNull();
        assertThat(clear.kind()).isEqualTo("clear");
        f.recordNow(new OwnerDecision.Decision(clear, null));
        assertThat(f.decide().post()).as("the clear closed it").isNull();
    }

    @Test
    void theChainNotArrivalTimeOrdersTwoLinkedTips() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("Y", "X", "C").push("X", "G0", "A");     // Y's record arrived first; the chain says X comes first
        f.done("Y", "failure");

        Map<String, Object> b = OwnerFixture.bodyOf(f.decide().post());

        assertThat(b).containsEntry("suspects", List.of("X", "Y")).containsEntry("owner", "A").containsEntry("next", "C");
    }
}
