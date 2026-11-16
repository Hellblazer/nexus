// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciCatalog.LeafIndex;
import dev.nexus.service.vectors.PciReconcilePlanner.Action;
import dev.nexus.service.vectors.PciReconcilePlanner.Build;
import dev.nexus.service.vectors.PciReconcilePlanner.Drop;
import dev.nexus.service.vectors.PciReconcilePlanner.DropReason;
import dev.nexus.service.vectors.PciReconcilePlanner.RegistryRow;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** RDR-230 P2.3 (nexus-4lnn1.1.10): the leaf-leftover kind, as a pure function of what the catalog read says. */
class PciLeafLeftoverPlanTest {

    private static final String MODEL = "m";
    private static final String TENANT = "t";
    private static final String OWN = "chunks_m0a90d9bc_t_37a8eec1ce19687d_embedding_768_idx";
    private static final int B = 100;
    private static final PciSettings DEFAULT = new PciSettings(true, B, 600, 16);

    private static Index leafLeftover(String name) {
        return new Index(name, false, null, null, null, OWN);
    }

    private static Leaf leaf(String model, String tenant, List<LeafIndex> own, List<Index> leftovers,
                             Index... pci) {
        return new Leaf("nexus", "chunks_leaf", model, tenant, List.of(pci), own, leftovers);
    }

    private static final List<LeafIndex> OWN_INDEX = List.of(new LeafIndex(OWN, true, 768));

    @Test
    void aLeafLeftover_isDropped_withNoCollection_whateverTheRegistryAndTheCountsSay() {
        var l = leaf(MODEL, TENANT, OWN_INDEX, List.of(leafLeftover(OWN + "_ccnew")));

        var actions = PciReconcilePlanner.plan(l, Map.of(), List.of(), Set.of(), null, DEFAULT);

        assertThat(actions).containsExactly(new Drop(OWN + "_ccnew", null, DropReason.LEAF_REINDEX_LEFTOVER));
    }

    @Test
    void aLeafLeftover_isNotChargedAgainstTheCap_andDoesNotBlockABuild() {
        var l = leaf(MODEL, TENANT, OWN_INDEX, List.of(leafLeftover(OWN + "_ccold")));
        PciSettings oneSlot = new PciSettings(true, B, 600, 1);

        var actions = PciReconcilePlanner.plan(l, Map.of("c", B),
            List.of(new RegistryRow("c", "live", "")), Set.of(), null, oneSlot);

        assertThat(actions).hasSize(2);
        assertThat(actions.get(0)).isEqualTo(new Drop(OWN + "_ccold", null, DropReason.LEAF_REINDEX_LEFTOVER));
        assertThat(actions.get(1)).isInstanceOf(Build.class);
    }

    @Test
    void aLeafLeftover_dropsAlongsidePciLeftoversAndFailedBuilds_inNameOrder() {
        String pciName = PciCatalog.indexName(MODEL, TENANT, "c");
        var pciLeftover = new Index(pciName + "_ccnew", false, null, "c");
        var l = leaf(MODEL, TENANT, OWN_INDEX, List.of(leafLeftover(OWN + "_ccnew")), pciLeftover);

        List<Action> actions = PciReconcilePlanner.plan(l, Map.of(), List.of(), Set.of(), null, DEFAULT);

        assertThat(actions).extracting(a -> ((Drop) a).reason())
            .containsExactlyInAnyOrder(DropReason.REINDEX_LEFTOVER, DropReason.LEAF_REINDEX_LEFTOVER);
    }

    @Test
    void aLeafTheBuilderDidNotResolve_getsNoPlan() {
        var l = leaf(null, TENANT, OWN_INDEX, List.of(leafLeftover(OWN + "_ccnew")));

        assertThat(PciReconcilePlanner.plan(l, Map.of(), List.of(), Set.of(), null, DEFAULT)).isEmpty();
    }

    @Test
    void switchOff_isAnEmptyPlan_leafLeftoversIncluded() {
        var l = leaf(MODEL, TENANT, OWN_INDEX, List.of(leafLeftover(OWN + "_ccnew")));

        assertThat(PciReconcilePlanner.plan(l, Map.of(), List.of(), Set.of(), null,
            new PciSettings(false, B, 600, 16))).isEmpty();
    }

    @Test
    void aLeafLeftoverIsNeverValid_andIsNeitherParsedNorAPciLeftover() {
        assertThatThrownBy(() -> new Index(OWN + "_ccnew", true, null, null, null, OWN))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new Index(OWN + "_ccnew", false, "c", null, null, OWN))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new Index(OWN + "_ccnew", false, null, "c", null, OWN))
            .isInstanceOf(IllegalArgumentException.class);
        Index ok = leafLeftover(OWN + "_ccnew");
        assertThat(ok.leafLeftover()).isTrue();
        assertThat(ok.parsed()).isFalse();
        assertThat(ok.leftover()).isFalse();
    }

    @Test
    void theLeafLeftoverName_isAnchoredToTheLeafsOwnIndexName_notAGenericPattern() {
        assertThat(PciCatalog.isLeafLeftoverName(OWN, OWN + "_ccnew")).isTrue();
        assertThat(PciCatalog.isLeafLeftoverName(OWN, OWN + "_ccold")).isTrue();
        assertThat(PciCatalog.isLeafLeftoverName(OWN, OWN + "_ccnew12")).isTrue();
        assertThat(PciCatalog.isLeafLeftoverName(OWN, OWN)).as("the index itself").isFalse();
        assertThat(PciCatalog.isLeafLeftoverName(OWN, OWN + "_ccnewX")).isFalse();
        assertThat(PciCatalog.isLeafLeftoverName(OWN, OWN + "_cc")).isFalse();
        assertThat(PciCatalog.isLeafLeftoverName(OWN, "x" + OWN + "_ccnew")).isFalse();
        // Another index's leftover, and a pci_ leftover, are not this one's.
        assertThat(PciCatalog.isLeafLeftoverName(OWN, OWN.replace("768", "384") + "_ccnew")).isFalse();
        assertThat(PciCatalog.isLeafLeftoverName(OWN, "pci_" + "a".repeat(24) + "_ccnew")).isFalse();
        assertThat(PciCatalog.isLeafLeftoverName(null, OWN + "_ccnew")).isFalse();
        // The own name is quoted: a regex metacharacter in it is literal.
        assertThat(PciCatalog.isLeafLeftoverName("a.b", "axb_ccnew")).isFalse();
        assertThat(PciCatalog.isLeafLeftoverName("a.b", "a.b_ccnew")).isTrue();
    }

    @Test
    void hnswIndex_picksTheIndexOfTheDimension() {
        var l = leaf(MODEL, TENANT, List.of(new LeafIndex("a", true, 384), new LeafIndex("b", true, 768)),
            List.of());

        assertThat(l.hnswIndex(768)).hasValueSatisfying(i -> assertThat(i.name()).isEqualTo("b"));
        assertThat(l.hnswIndex(1024)).isEmpty();
    }
}
