// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (bead nexus-p2m1x.28), item 8: the minted label is {@code "c" + collection_id}, and a mint whose
 * label is already live draws the next id rather than reusing or colliding (design-gate proposal, pending Sam).
 * The label's rendering and the minted-form predicate are P3.6's {@code CollectionLabel} (elsinore, tested there);
 * the schema half (the insert, the integration tests) lands on P3.1's branch.
 */
class MintedLabelTest {

    @Test
    void aFreeLabelTakesTheFirstDraw() {
        AtomicLong seq = new AtomicLong(41);
        MintedLabel.Choice c = MintedLabel.choose(seq::incrementAndGet, id -> false, label -> false);
        assertThat(c).isEqualTo(new MintedLabel.Choice(42, "c42"));
        assertThat(seq.get()).as("one draw").isEqualTo(42);
    }

    @Test
    void aTakenLabelDrawsTheNextIdAndTheLabelStillMatchesItsId() {
        AtomicLong seq = new AtomicLong(41);
        Set<String> live = Set.of("c42", "c43");
        List<String> asked = new ArrayList<>();
        MintedLabel.Choice c = MintedLabel.choose(seq::incrementAndGet, id -> false, label -> {
            asked.add(label);
            return live.contains(label);
        });
        assertThat(c).isEqualTo(new MintedLabel.Choice(44, "c44"));
        assertThat(asked).containsExactly("c42", "c43", "c44");
    }

    @Test
    void everyDrawTakenRefusesAfterTheBound() {
        AtomicLong seq = new AtomicLong();
        assertThatThrownBy(() -> MintedLabel.choose(seq::incrementAndGet, id -> false, label -> true))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining(String.valueOf(MintedLabel.MAX_DRAWS));
        assertThat(seq.get()).as("exactly the bound's draws").isEqualTo(MintedLabel.MAX_DRAWS);
    }

    /**
     * baton's condition on BY DEFAULT (via linda, 2026-11-21): a restore that wrote explicit ids at or above the
     * sequence's next value, with no setval after it, leaves the sequence behind the data. The mint must not skip
     * past that silently (that would hide the missing setval) nor surface a raw unique violation: the first drawn id
     * that is already a row refuses at once, naming the sequence.
     */
    @Test
    void aDrawnIdThatIsAlreadARowRefusesAtOnceNamingTheSequence() {
        AtomicLong seq = new AtomicLong(99);
        AtomicInteger labelChecks = new AtomicInteger();
        assertThatThrownBy(() -> MintedLabel.choose(seq::incrementAndGet, id -> id == 100, label -> {
            labelChecks.incrementAndGet();
            return false;
        }))
            .isInstanceOf(MintedLabel.SequenceBehindDataException.class)
            .hasMessageContaining("100")
            .hasMessageContaining("setval");
        assertThat(seq.get()).as("one draw, no skipping").isEqualTo(100);
        assertThat(labelChecks.get()).as("the label is not consulted for an id that is taken").isZero();
    }

    @Test
    void aSequenceAheadOfEveryRowMintsNormally() {
        AtomicLong seq = new AtomicLong(200);
        assertThat(MintedLabel.choose(seq::incrementAndGet, id -> id <= 150, label -> false))
            .isEqualTo(new MintedLabel.Choice(201, "c201"));
    }
}
