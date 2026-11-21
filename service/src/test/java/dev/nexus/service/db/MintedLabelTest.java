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
 * label is already live draws the next id rather than reusing or colliding (design gate accepted by Sam 2026-11-21, T2 nexus/sam-ruling-rdr229-p38-design-gate-v2-2026-11-21).
 * The label's rendering and the minted-form predicate are P3.6's {@code CollectionLabel} (elsinore, tested there);
 * the schema half (the insert, the integration tests) lands on P3.1's branch.
 */
class MintedLabelTest {

    @Test
    void aFreeLabelTakesTheFirstDraw() {
        AtomicLong seq = new AtomicLong(41);
        MintedLabel.Choice c = MintedLabel.choose(seq::incrementAndGet, id -> false, label -> false, MintedLabelTest::noHeal);
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
        }, MintedLabelTest::noHeal);
        assertThat(c).isEqualTo(new MintedLabel.Choice(44, "c44"));
        assertThat(asked).containsExactly("c42", "c43", "c44");
    }

    @Test
    void everyDrawTakenRefusesAfterTheBound() {
        AtomicLong seq = new AtomicLong();
        assertThatThrownBy(() -> MintedLabel.choose(seq::incrementAndGet, id -> false, label -> true, MintedLabelTest::noHeal))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining(String.valueOf(MintedLabel.MAX_DRAWS));
        assertThat(seq.get()).as("exactly the bound's draws").isEqualTo(MintedLabel.MAX_DRAWS);
    }

    /**
     * baton's condition on BY DEFAULT, with linda's ruling (2026-11-21): a restore that wrote explicit ids at or above
     * the sequence's next value with no setval after it leaves the sequence behind the data. The mint neither skips
     * past it silently nor surfaces a raw unique violation: it heals the sequence ONCE (the caller's setval to
     * max(collection_id), logged as a WARN) and draws again.
     */
    @Test
    void aSequenceBehindTheDataHealsOnceAndMints() {
        AtomicLong seq = new AtomicLong(99);
        Set<Long> rows = Set.of(100L, 101L, 102L);
        List<Long> heals = new ArrayList<>();
        MintedLabel.Choice c = MintedLabel.choose(seq::incrementAndGet, rows::contains, label -> false, colliding -> {
            heals.add(colliding);
            seq.set(102); // the caller's setval(seq, max(collection_id))
        });
        assertThat(c).isEqualTo(new MintedLabel.Choice(103, "c103"));
        assertThat(heals).as("one heal, told the colliding id").containsExactly(100L);
    }

    @Test
    void aSecondCollisionAfterTheHealRefusesNamingTheSequence() {
        AtomicLong seq = new AtomicLong(99);
        AtomicInteger heals = new AtomicInteger();
        assertThatThrownBy(() -> MintedLabel.choose(seq::incrementAndGet, id -> true, label -> false,
            colliding -> heals.incrementAndGet()))
            .isInstanceOf(MintedLabel.SequenceBehindDataException.class)
            .hasMessageContaining("101")
            .hasMessageContaining("setval");
        assertThat(heals.get()).as("the heal runs once, never twice").isEqualTo(1);
        assertThat(seq.get()).as("two draws: the colliding one and the one after the heal").isEqualTo(101);
    }

    @Test
    void aSequenceAheadOfEveryRowMintsNormally() {
        AtomicLong seq = new AtomicLong(200);
        assertThat(MintedLabel.choose(seq::incrementAndGet, id -> id <= 150, label -> false, MintedLabelTest::noHeal))
            .isEqualTo(new MintedLabel.Choice(201, "c201"));
    }

    private static void noHeal(long collidingId) {
        throw new AssertionError("no heal expected, got a collision at " + collidingId);
    }
}
