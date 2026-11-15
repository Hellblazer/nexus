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

import java.time.Instant;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-230 P2.3: which leaf HNSW indexes the size rule calls for a REINDEX, as a pure function of the pass's looks. */
class PciLeafReindexRuleTest {

    private static final Instant T = Instant.parse("2026-11-14T00:00:00Z");

    private static Leaf leaf(String name, int dim, long bytes, boolean valid) {
        return new Leaf("nexus", name, "m", "t", List.of(),
            List.of(new LeafIndex(name + "_embedding_" + dim + "_idx", valid, bytes, dim)), List.of());
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

    private static Map<String, Long> live(String leafId, long rows) {
        return Map.of("nexus." + leafId, rows);
    }

    @Test
    void at1024_withNoAnchors_theBuiltInReference_firesAtOneAndAHalfTimes() {
        Leaf l = leaf("leaf_a", 1024, 12_300_000, true);

        var scan = PciLeafReindexRule.scan(List.of(look(l, 1024)), id -> live("leaf_a", 1000).get(id), n -> false);

        assertThat(scan.candidates()).containsExactly(
            new Reindex("leaf_a_embedding_1024_idx", null, Trigger.LEAF_SIZE, 12_300_000));
        assertThat(scan.unanchored()).isZero();
    }

    @Test
    void justUnderTheMultiple_isQuiet() {
        Leaf l = leaf("leaf_a", 1024, 12_299_999, true);

        var scan = PciLeafReindexRule.scan(List.of(look(l, 1024)), id -> 1000L, n -> false);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
    }

    @Test
    void at384_withNoBuiltAnchor_isUnanchored_andNeverACandidate_whateverItsSize() {
        Leaf l = leaf("leaf_a", 384, 1L << 40, true);

        var scan = PciLeafReindexRule.scan(List.of(look(l, 384)), id -> 1000L, n -> false);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isEqualTo(1);
    }

    @Test
    void anAdoptedAnchor_isNotAReference_aBuiltOneAtTheDimensionIs() {
        Leaf a = leaf("leaf_a", 384, 3_000_000, true);
        Leaf b = leaf("leaf_b", 384, 100, true);
        var adopted = new Anchor(T, 1000, 1_000_000, PciCatalog.ORIGIN_ADOPTED);

        var none = PciLeafReindexRule.scan(List.of(look(a, 384, adopted), look(b, 384)), id -> 1000L, n -> false);
        var some = PciLeafReindexRule.scan(List.of(look(a, 384, built(1000, 1_000_000)), look(b, 384)),
            id -> 1000L, n -> false);

        assertThat(none.candidates()).isEmpty();
        assertThat(none.unanchored()).isEqualTo(2);
        // The reference is 1,000 bytes a row: a is at 3,000 (3x, fires), b at 0.1 (quiet).
        assertThat(some.unanchored()).isZero();
        assertThat(some.candidates()).extracting(Reindex::name).containsExactly("leaf_a_embedding_384_idx");
    }

    @Test
    void theReferenceIsPerDimension() {
        Leaf l384 = leaf("leaf_a", 384, 5_000_000, true);
        Leaf l768 = leaf("leaf_b", 768, 5_000_000, true);

        var scan = PciLeafReindexRule.scan(List.of(look(l384, 384, built(1000, 1_000_000)), look(l768, 768)),
            id -> 1000L, n -> false);

        assertThat(scan.candidates()).extracting(Reindex::name).containsExactly("leaf_a_embedding_384_idx");
        assertThat(scan.unanchored()).as("768 has no reference").isEqualTo(1);
    }

    @Test
    void anInvalidIndex_noIndexAtTheDimension_andABackedOffIndex_areSkipped() {
        Leaf invalid = leaf("leaf_i", 1024, 99_000_000, false);
        Leaf other = leaf("leaf_o", 768, 99_000_000, true);
        Leaf backedOff = leaf("leaf_b", 1024, 99_000_000, true);

        var scan = PciLeafReindexRule.scan(List.of(look(invalid, 1024), look(other, 1024), look(backedOff, 1024)),
            id -> 1000L, n -> n.equals("leaf_b_embedding_1024_idx"));

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
    }

    @Test
    void aLeafWithNoStatisticsRow_hasNoLiveRows_quietWhereAReferenceExists_unanchoredWhereItDoesNot() {
        Leaf at1024 = leaf("leaf_a", 1024, 99_000_000, true);
        Leaf at384 = leaf("leaf_b", 384, 99_000_000, true);

        var scan = PciLeafReindexRule.scan(List.of(look(at1024, 1024), look(at384, 384)), id -> null, n -> false);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isEqualTo(1);
    }

    @Test
    void laggingStatistics_neverReadBelowTheRowsThePassCounted() {
        // The leaf's index holds 300 rows of 8,200 bytes; the statistics have only flushed 40 of them. Read raw, that
        // is 61,500 bytes a row, 7.5x the reference; with the 300 counted rows it is exactly the reference.
        Leaf l = leaf("leaf_a", 1024, 300 * 8_200L, true);
        var counted = built(300, 300 * 8_200L);

        var scan = PciLeafReindexRule.scan(List.of(look(l, 1024, counted)), id -> 40L, n -> false);

        assertThat(scan.candidates()).isEmpty();
        // The same statistics with nothing counted do fire: the bound is what stops it.
        assertThat(PciLeafReindexRule.scan(List.of(look(l, 1024)), id -> 40L, n -> false).candidates()).hasSize(1);
    }

    @Test
    void anEmptyLeaf_isQuiet_andNotUnanchoredAt1024() {
        Leaf l = leaf("leaf_a", 1024, 99_000_000, true);

        var scan = PciLeafReindexRule.scan(List.of(look(l, 1024)), id -> 0L, n -> false);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
    }

    @Test
    void noLooks_isNothing() {
        var scan = PciLeafReindexRule.scan(List.of(), id -> 1L, n -> false);

        assertThat(scan.candidates()).isEmpty();
        assertThat(scan.unanchored()).isZero();
    }
}
