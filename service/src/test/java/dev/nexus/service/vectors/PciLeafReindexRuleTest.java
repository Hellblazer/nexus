// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciCatalog.Anchor;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciCatalog.LeafIndex;
import dev.nexus.service.vectors.PciReconcilePlanner.Reindex;
import dev.nexus.service.vectors.PciReconcilePlanner.Trigger;
import dev.nexus.service.vectors.PciReconciler.IndexReading;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.BiFunction;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-230 P2.3: which leaf HNSW indexes the size rule calls for a REINDEX, as a pure function of the pass's inputs. */
class PciLeafReindexRuleTest {

    private static final Instant T = Instant.parse("2026-11-14T00:00:00Z");
    private static final Instant NOW = Instant.parse("2026-11-20T00:00:00Z");

    private static Leaf leaf(String name, int dim, boolean valid) {
        return new Leaf("nexus", name, "m", "t", List.of(),
            List.of(new LeafIndex(name + "_embedding_" + dim + "_idx", valid, dim)), List.of());
    }

    private static String idx(String leaf, int dim) {
        return leaf + "_embedding_" + dim + "_idx";
    }

    private static PciReindexState.Look look(Leaf leaf, int dim, Anchor... anchors) {
        var readings = new java.util.LinkedHashMap<String, IndexReading>();
        int i = 0;
        for (Anchor a : anchors) {
            readings.put("pci_" + leaf.name() + i++, new IndexReading(a, a.bytes(), a.rows(), 0, T));
        }
        return new PciReindexState.Look(leaf, dim, readings);
    }

    private static Anchor built(long rows, long bytes) {
        return new Anchor(T, rows, bytes, PciCatalog.ORIGIN_BUILT);
    }

    /** The facts the lazy read would return, by index name; also records which indexes were read. */
    private static final class Facts implements BiFunction<Leaf, LeafIndex, PciAnchorReads.IndexFacts> {
        final Map<String, PciAnchorReads.IndexFacts> byName = new HashMap<>();
        final List<String> read = new ArrayList<>();

        Facts put(String index, long bytes, String comment) {
            byName.put(index, new PciAnchorReads.IndexFacts(comment, bytes));
            return this;
        }

        Facts put(String index, long bytes, String comment, long relfilenode) {
            byName.put(index, new PciAnchorReads.IndexFacts(comment, bytes, relfilenode));
            return this;
        }

        @Override
        public PciAnchorReads.IndexFacts apply(Leaf leaf, LeafIndex own) {
            read.add(own.name());
            return byName.get(own.name());
        }
    }

    private static PciLeafReindexRule.Scan scan(List<PciReindexState.Look> looks, Map<String, Long> rows, Facts facts) {
        return PciLeafReindexRule.scan(looks, rows::get, facts, n -> false, NOW);
    }

    @Test
    void at1024_withNoAnchors_theBuiltInReference_firesAtOneAndAHalfTimes() {
        Leaf l = leaf("leaf_a", 1024, true);
        var facts = new Facts().put(idx("leaf_a", 1024), 12_300_000, null);

        var scan = scan(List.of(look(l, 1024)), Map.of("nexus.leaf_a", 1000L), facts);

        assertThat(scan.candidates()).containsExactly(
            new Reindex(idx("leaf_a", 1024), null, Trigger.LEAF_SIZE, 12_300_000));
        assertThat(scan.unanchored()).isZero();
    }

    @Test
    void justUnderTheMultiple_isQuiet() {
        Leaf l = leaf("leaf_a", 1024, true);
        var facts = new Facts().put(idx("leaf_a", 1024), 12_299_999, null);

        var scan = scan(List.of(look(l, 1024)), Map.of("nexus.leaf_a", 1000L), facts);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
    }

    @Test
    void at384_withNoBuiltAnchor_isUnanchored_neverACandidate_andItsSizeIsNotEvenRead() {
        Leaf l = leaf("leaf_a", 384, true);
        var facts = new Facts().put(idx("leaf_a", 384), 1L << 40, null);

        var scan = scan(List.of(look(l, 384)), Map.of("nexus.leaf_a", 1000L), facts);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isEqualTo(1);
        assertThat(facts.read).as("no lock is taken on an index that cannot be judged").isEmpty();
    }

    @Test
    void anAdoptedAnchor_isNotAReference_aBuiltOneAtTheDimensionIs() {
        Leaf a = leaf("leaf_a", 384, true);
        Leaf b = leaf("leaf_b", 384, true);
        var adopted = new Anchor(T, 1000, 1_000_000, PciCatalog.ORIGIN_ADOPTED);
        var facts = new Facts().put(idx("leaf_a", 384), 3_000_000, null).put(idx("leaf_b", 384), 100, null);
        Map<String, Long> rows = Map.of("nexus.leaf_a", 1000L, "nexus.leaf_b", 1000L);

        var none = scan(List.of(look(a, 384, adopted), look(b, 384)), rows, facts);
        var some = scan(List.of(look(a, 384, built(1000, 1_000_000)), look(b, 384)), rows, facts);

        assertThat(none.candidates()).isEmpty();
        assertThat(none.unanchored()).isEqualTo(2);
        // The reference is 1,000 bytes a row: a is at 3,000 (3x, fires), b at 0.1 (quiet).
        assertThat(some.unanchored()).isZero();
        assertThat(some.candidates()).extracting(Reindex::name).containsExactly(idx("leaf_a", 384));
    }

    @Test
    void theReferenceIsPerDimension() {
        Leaf l384 = leaf("leaf_a", 384, true);
        Leaf l768 = leaf("leaf_b", 768, true);
        var facts = new Facts().put(idx("leaf_a", 384), 5_000_000, null).put(idx("leaf_b", 768), 5_000_000, null);

        var scan = scan(List.of(look(l384, 384, built(1000, 1_000_000)), look(l768, 768)),
            Map.of("nexus.leaf_a", 1000L, "nexus.leaf_b", 1000L), facts);

        assertThat(scan.candidates()).extracting(Reindex::name).containsExactly(idx("leaf_a", 384));
        assertThat(scan.unanchored()).as("768 has no reference").isEqualTo(1);
    }

    @Test
    void anInvalidIndex_noIndexAtTheDimension_aBackedOffIndex_andAnIndexThatCannotBeRead_areSkipped() {
        Leaf invalid = leaf("leaf_i", 1024, false);
        Leaf other = leaf("leaf_o", 768, true);
        Leaf backedOff = leaf("leaf_b", 1024, true);
        Leaf unread = leaf("leaf_u", 1024, true);
        var facts = new Facts().put(idx("leaf_i", 1024), 99_000_000, null)
            .put(idx("leaf_b", 1024), 99_000_000, null);   // leaf_u has no facts: its read timed out
        Map<String, Long> rows = Map.of("nexus.leaf_i", 1000L, "nexus.leaf_b", 1000L, "nexus.leaf_u", 1000L);

        var scan = PciLeafReindexRule.scan(List.of(look(invalid, 1024), look(other, 1024), look(backedOff, 1024),
            look(unread, 1024)), rows::get, facts, n -> n.equals(idx("leaf_b", 1024)), NOW);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
        assertThat(facts.read).as("a backed-off index is not read").containsExactly(idx("leaf_u", 1024));
    }

    @Test
    void aLeafWithNoRowEstimate_hasNoRows_quietWhereAReferenceExists_unanchoredWhereItDoesNot() {
        Leaf at1024 = leaf("leaf_a", 1024, true);
        Leaf at384 = leaf("leaf_b", 384, true);
        var facts = new Facts().put(idx("leaf_a", 1024), 99_000_000, null).put(idx("leaf_b", 384), 99_000_000, null);

        var scan = scan(List.of(look(at1024, 1024), look(at384, 384)), Map.of(), facts);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isEqualTo(1);
    }

    @Test
    void anEmptyLeaf_isQuiet() {
        Leaf l = leaf("leaf_a", 1024, true);
        var facts = new Facts().put(idx("leaf_a", 1024), 99_000_000, null);

        var scan = scan(List.of(look(l, 1024)), Map.of("nexus.leaf_a", 0L), facts);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
        assertThat(facts.read).as("no lock is taken on an index of a leaf with no rows").isEmpty();
    }

    @Test
    void laggingOrZeroedEstimates_neverReadBelowTheRowsThePassCounted() {
        // The index holds 300 rows of 8,200 bytes; the estimate (statistics and reltuples both) says 40. Read raw, that
        // is 61,500 bytes a row, 7.5x the reference; with the 300 counted rows it is exactly the reference.
        Leaf l = leaf("leaf_a", 1024, true);
        var facts = new Facts().put(idx("leaf_a", 1024), 300 * 8_200L, null);
        var counted = built(300, 300 * 8_200L);

        assertThat(scan(List.of(look(l, 1024, counted)), Map.of("nexus.leaf_a", 40L), facts).candidates()).isEmpty();
        assertThat(scan(List.of(look(l, 1024)), Map.of("nexus.leaf_a", 40L), facts).candidates())
            .as("the same estimate with nothing counted does fire: the bound is what stops it").hasSize(1);
    }

    @Test
    void theCooldown_isTwentyFourHoursFromTheAnchorOnTheIndexItself() {
        Leaf l = leaf("leaf_a", 1024, true);
        Map<String, Long> rows = Map.of("nexus.leaf_a", 1000L);
        String anchor = new Anchor(T, 1000, 8_200_000, PciCatalog.ORIGIN_BUILT).toJson();
        var facts = new Facts().put(idx("leaf_a", 1024), 99_000_000, anchor);
        Instant ready = T.plus(Duration.ofHours(24));

        var justBefore = PciLeafReindexRule.scan(List.of(look(l, 1024)), rows::get, facts, n -> false,
            ready.minusNanos(1_000));
        var atTheMark = PciLeafReindexRule.scan(List.of(look(l, 1024)), rows::get, facts, n -> false, ready);

        assertThat(justBefore.candidates()).as("inside the 24 hours").isEmpty();
        assertThat(atTheMark.candidates()).as("at 24 hours").hasSize(1);
    }

    @Test
    void noAnchor_orOneThatDoesNotParse_isNoCooldown() {
        Leaf l = leaf("leaf_a", 1024, true);
        Map<String, Long> rows = Map.of("nexus.leaf_a", 1000L);

        for (String comment : new String[] {null, "", "not json", "{\"nexus_pci\":2}"}) {
            var facts = new Facts().put(idx("leaf_a", 1024), 99_000_000, comment);
            assertThat(scan(List.of(look(l, 1024)), rows, facts).candidates()).as("comment " + comment).hasSize(1);
        }
    }

    @Test
    void anAnchorWhoseRelfilenodeMoved_isRebuilt_andNoCandidate_whileOneWithoutAnAndUnmovedOnesAreJudgedAsBefore() {
        Leaf l = leaf("leaf_a", 1024, true);
        Map<String, Long> rows = Map.of("nexus.leaf_a", 1000L);
        String index = idx("leaf_a", 1024);
        var v2 = new Anchor(T, 1000, 8_200_000, PciCatalog.ORIGIN_BUILT, 500L);
        var unstamped = new Anchor(T, 1000, 8_200_000, PciCatalog.ORIGIN_BUILT);
        // The size rule fires on 99 MB for 1000 rows, and the cooldown (T is long before NOW) is over.
        var moved = scan(List.of(look(l, 1024)), rows, new Facts().put(index, 99_000_000, v2.toJson(), 777L));
        var unmoved = scan(List.of(look(l, 1024)), rows, new Facts().put(index, 99_000_000, v2.toJson(), 500L));
        var legacy = scan(List.of(look(l, 1024)), rows,
            new Facts().put(index, 99_000_000, unstamped.toJson(), 777L));

        assertThat(moved.candidates()).as("a rebuilt index is re-anchored, not judged, this pass").isEmpty();
        assertThat(moved.rebuilt()).singleElement().satisfies(r -> {
            assertThat(r.own().name()).isEqualTo(index);
            assertThat(r.oldRelfilenode()).isEqualTo(500L);
            assertThat(r.newRelfilenode()).isEqualTo(777L);
        });
        assertThat(unmoved.rebuilt()).isEmpty();
        assertThat(unmoved.stamps()).isEmpty();
        assertThat(unmoved.candidates()).hasSize(1);
        assertThat(legacy.rebuilt()).as("no relfilenode, nothing to differ from").isEmpty();
        assertThat(legacy.stamps()).as("stamped once, with its own figures").singleElement().satisfies(st -> {
            assertThat(st.anchor()).isEqualTo(unstamped);
            assertThat(st.relfilenode()).isEqualTo(777L);
        });
        assertThat(legacy.candidates()).as("judged as before").hasSize(1);
    }

    @Test
    void noLooks_isNothing() {
        var scan = scan(List.of(), Map.of(), new Facts());

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
    }
}
