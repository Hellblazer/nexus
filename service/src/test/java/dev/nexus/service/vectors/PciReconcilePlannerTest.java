// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.Action;
import dev.nexus.service.vectors.PciReconcilePlanner.Build;
import dev.nexus.service.vectors.PciReconcilePlanner.Drop;
import dev.nexus.service.vectors.PciReconcilePlanner.DropReason;
import dev.nexus.service.vectors.PciReconcilePlanner.RegistryRow;
import dev.nexus.service.vectors.PciReconcilePlanner.SkipCountSuspect;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-227 Step 2 (nexus-43ulx.18): the decision half of the reconciler, one case per rule. No database, no clock,
 * no logging: {@link PciReconcilePlanner#plan} is a function of its arguments.
 */
class PciReconcilePlannerTest {

    private static final String MODEL = "m";
    private static final String TENANT = "t";
    private static final int B = 100;

    private static PciSettings settings(int maxPerLeaf) {
        return new PciSettings(true, B, 600, maxPerLeaf);
    }

    private static final PciSettings DEFAULT = settings(16);

    private static String name(String collection) {
        return PciCatalog.indexName(MODEL, TENANT, collection);
    }

    private static Index valid(String collection) {
        return new Index(name(collection), true, collection);
    }

    private static Index invalid(String collection) {
        return new Index(name(collection), false, collection);
    }

    private static Leaf leaf(Index... indexes) {
        return new Leaf("nexus", "chunks_leaf", MODEL, TENANT, List.of(indexes));
    }

    private static RegistryRow live(String collection) {
        return new RegistryRow(collection, "live", "");
    }

    private static RegistryRow state(String collection, String lifecycleState) {
        return new RegistryRow(collection, lifecycleState, "");
    }

    private static RegistryRow superseded(String collection, String by) {
        return new RegistryRow(collection, "live", by);
    }

    private static List<Action> plan(Leaf leaf, Map<String, Integer> counts, List<RegistryRow> registry) {
        return PciReconcilePlanner.plan(leaf, counts, registry, Set.of(), null, DEFAULT);
    }

    private static List<Action> plan(Leaf leaf, Map<String, Integer> counts, List<RegistryRow> registry,
                                     PciSettings settings) {
        return PciReconcilePlanner.plan(leaf, counts, registry, Set.of(), null, settings);
    }

    private static Build build(String collection) {
        return new Build(name(collection), collection);
    }

    private static Drop drop(String collection, DropReason reason) {
        return new Drop(name(collection), collection, reason);
    }

    // ---- switches ----

    @Test
    void switchOff_isAnEmptyPlan_whateverIsWanted() {
        var off = new PciSettings(false, B, 600, 16);

        var actions = plan(leaf(valid("old")), Map.of("new", B, "old", 0),
            List.of(live("new"), superseded("old", "x")), off);

        assertThat(actions).isEmpty();
    }

    @Test
    void emptyLeaf_noRegistry_isAnEmptyPlan() {
        assertThat(plan(leaf(), Map.of(), List.of())).isEmpty();
    }

    @Test
    void leafWhoseBoundsDidNotParse_isAnEmptyPlan() {
        var noTenant = new Leaf("nexus", "x", MODEL, null, List.of());
        var noModel = new Leaf("nexus", "x", null, TENANT, List.of());

        assertThat(plan(noTenant, Map.of("c", B), List.of(live("c")))).isEmpty();
        assertThat(plan(noModel, Map.of("c", B), List.of(live("c")))).isEmpty();
    }

    // ---- build rule ----

    @Test
    void build_liveCollectionAtB_withNoIndex() {
        var actions = plan(leaf(), Map.of("c", B), List.of(live("c")));

        assertThat(actions).containsExactly(build("c"));
        assertThat(((Build) actions.get(0)).name()).isEqualTo(PciCatalog.indexName(MODEL, TENANT, "c"));
    }

    @Test
    void build_notBelowB() {
        assertThat(plan(leaf(), Map.of("c", B - 1), List.of(live("c")))).isEmpty();
    }

    @Test
    void build_cappedCountOfBPlusOne_isDecidable() {
        // The count query stops at B+1 rows; every rule must give the same answer for B+1 as for any larger count.
        assertThat(plan(leaf(), Map.of("c", B + 1), List.of(live("c")))).containsExactly(build("c"));
        assertThat(plan(leaf(valid("c")), Map.of("c", B + 1), List.of(live("c")))).isEmpty();
        assertThat(plan(leaf(valid("c")), Map.of("c", 5_000_000), List.of(live("c")))).isEmpty();
    }

    @Test
    void build_neverForNonLiveLifecycleStates() {
        for (String s : new String[] {"quarantine", "dormant", "disputed", "", "LIVE", null}) {
            assertThat(plan(leaf(), Map.of("c", B + 1), List.of(state("c", s))))
                .as("lifecycle_state=%s", s).isEmpty();
        }
    }

    @Test
    void build_neverWhenSuperseded() {
        assertThat(plan(leaf(), Map.of("c", B + 1), List.of(superseded("c", "d")))).isEmpty();
    }

    @Test
    void build_neverWhenTheRegistryDoesNotListIt() {
        assertThat(plan(leaf(), Map.of("ghost", B + 1), List.of(live("other")))).isEmpty();
    }

    @Test
    void build_notWhenAnIndexAlreadyServesTheCollection_underAnyName() {
        var otherName = new Index("pci_" + "0".repeat(24), true, "c");

        assertThat(plan(leaf(otherName), Map.of("c", B), List.of(live("c")))).isEmpty();
    }

    @Test
    void build_notWhileBackoffHasNotExpired_andTheSlotGoesToTheNext() {
        var actions = PciReconcilePlanner.plan(leaf(), Map.of("a", B + 1, "b", B),
            List.of(live("a"), live("b")), Set.of("a"), null, settings(1));

        assertThat(actions).containsExactly(build("b"));
    }

    // ---- retirement rules ----

    @Test
    void drop_belowHalfB() {
        // 49 * 2 < 100.
        assertThat(plan(leaf(valid("c")), Map.of("c", B / 2 - 1), List.of(live("c"))))
            .containsExactly(drop("c", DropReason.BELOW_HALF_BUILD_THRESHOLD));
    }

    @Test
    void hysteresis_betweenHalfBAndB_keepsTheIndex_andBuildsNothing() {
        for (int n : new int[] {B / 2, B / 2 + 1, B - 1}) {
            assertThat(plan(leaf(valid("c")), Map.of("c", n), List.of(live("c")))).as("count %d", n).isEmpty();
        }
        assertThat(plan(leaf(), Map.of("c", B - 1), List.of(live("c")))).isEmpty();
    }

    @Test
    void hysteresis_oddB_usesCountTimesTwoBelowB() {
        // B=101: 50*2=100 < 101 drops; 51*2=102 keeps. Integer B/2 (50) would keep 50.
        var odd = new PciSettings(true, 101, 600, 16);

        assertThat(plan(leaf(valid("c")), Map.of("c", 50), List.of(live("c")), odd)).hasSize(1);
        assertThat(plan(leaf(valid("c")), Map.of("c", 51), List.of(live("c")), odd)).isEmpty();
    }

    @Test
    void drop_whenTheRegistryNoLongerListsTheCollection_whateverItsCount() {
        assertThat(plan(leaf(valid("gone")), Map.of("gone", B + 1), List.of(live("other"))))
            .containsExactly(drop("gone", DropReason.NOT_IN_REGISTRY));
    }

    @Test
    void drop_whenSuperseded_whateverItsCount() {
        assertThat(plan(leaf(valid("c")), Map.of("c", B + 1), List.of(superseded("c", "d"))))
            .containsExactly(drop("c", DropReason.SUPERSEDED));
    }

    @Test
    void drop_supersededWithZeroRows_isASupersedeNotACountingFailure() {
        assertThat(plan(leaf(valid("c")), Map.of(), List.of(superseded("c", "d"))))
            .containsExactly(drop("c", DropReason.SUPERSEDED));
    }

    @Test
    void drop_notInRegistryWithZeroRows_isARegistryDrop() {
        assertThat(plan(leaf(valid("c")), Map.of(), List.of()))
            .containsExactly(drop("c", DropReason.NOT_IN_REGISTRY));
    }

    @Test
    void nonLiveState_doesNotDropByItself_thePlainCountRulesApply() {
        // Decision 2: an existing index on a quarantine/dormant/disputed collection drops only by the count rules.
        for (String s : new String[] {"quarantine", "dormant", "disputed"}) {
            assertThat(plan(leaf(valid("c")), Map.of("c", B), List.of(state("c", s)))).as(s).isEmpty();
            assertThat(plan(leaf(valid("c")), Map.of("c", B / 2 - 1), List.of(state("c", s))))
                .as(s).containsExactly(drop("c", DropReason.BELOW_HALF_BUILD_THRESHOLD));
        }
    }

    // ---- zero-count guard ----

    @Test
    void zeroCount_forALivePopulatedCollection_isSuspect_neverADrop() {
        assertThat(plan(leaf(valid("c")), Map.of("c", 0), List.of(live("c"))))
            .containsExactly(new SkipCountSuspect("c"));
        // The counting query returns no row for a collection with no visible rows: absent means zero.
        assertThat(plan(leaf(valid("c")), Map.of(), List.of(live("c"))))
            .containsExactly(new SkipCountSuspect("c"));
    }

    @Test
    void zeroCount_forANonLiveCollection_isNotSuspect_theCountRulesDrop() {
        assertThat(plan(leaf(valid("c")), Map.of("c", 0), List.of(state("c", "quarantine"))))
            .containsExactly(drop("c", DropReason.BELOW_HALF_BUILD_THRESHOLD));
    }

    @Test
    void zeroCount_forACollectionWithNoIndex_isNothing() {
        assertThat(plan(leaf(), Map.of("c", 0), List.of(live("c")))).isEmpty();
    }

    @Test
    void smallNonZeroCount_forALivePopulatedCollection_isAnOrdinaryDrop() {
        // The guard is for zero only: one row on a tenant-correct count is a real shrink.
        assertThat(plan(leaf(valid("c")), Map.of("c", 1), List.of(live("c"))))
            .containsExactly(drop("c", DropReason.BELOW_HALF_BUILD_THRESHOLD));
    }

    // ---- invalid, in-flight and unparsed indexes ----

    @Test
    void invalidIndexOutsideTheHoldersOwnBuild_isDroppedAsFailed_andNotRebuiltInTheSamePass() {
        var actions = plan(leaf(invalid("c")), Map.of("c", B + 1), List.of(live("c")));

        assertThat(actions).containsExactly(drop("c", DropReason.FAILED_BUILD));
    }

    @Test
    void invalidIndex_isDroppedAsFailed_evenWhenTheCollectionIsGone() {
        assertThat(plan(leaf(invalid("c")), Map.of(), List.of()))
            .containsExactly(drop("c", DropReason.FAILED_BUILD));
    }

    @Test
    void theHoldersOwnInFlightBuild_isLeftAlone_whateverTheRulesSay() {
        var l = leaf(invalid("c"));

        assertThat(PciReconcilePlanner.plan(l, Map.of("c", 0), List.of(superseded("c", "d")), Set.of(), name("c"),
            DEFAULT)).isEmpty();
        assertThat(PciReconcilePlanner.plan(l, Map.of("c", B + 1), List.of(live("c")), Set.of(), name("c"),
            DEFAULT)).isEmpty();
    }

    @Test
    void unparsedIndex_isNeverTouched() {
        var unparsed = new Index("pci_foo", true, null);
        var unparsedInvalid = new Index("pci_" + "a".repeat(24) + "_ccnew", false, null);

        assertThat(plan(leaf(unparsed, unparsedInvalid), Map.of(), List.of())).isEmpty();
    }

    // ---- cap ----

    @Test
    void cap_zeroBuildsNone_butDropsStillRun() {
        var actions = plan(leaf(valid("old")), Map.of("old", 0, "new", B + 1),
            List.of(superseded("old", "new"), live("new")), settings(0));

        assertThat(actions).containsExactly(drop("old", DropReason.SUPERSEDED));
    }

    @Test
    void cap_validAndInFlightIndexesCount() {
        // Cap 2: valid "k" and the holder's in-flight "f" fill it, so "n" waits.
        var l = leaf(valid("k"), invalid("f"));
        var actions = PciReconcilePlanner.plan(l, Map.of("k", B, "f", B, "n", B + 1),
            List.of(live("k"), live("f"), live("n")), Set.of(), name("f"), settings(2));

        assertThat(actions).isEmpty();
    }

    @Test
    void cap_invalidAndUnparsedIndexesDoNotUseUpSlots() {
        // Cap 1 with only a failed and an unparsed index present: the one slot is still free for a build.
        var l = leaf(invalid("x"), new Index("pci_foo", true, null));
        var actions = plan(l, Map.of("n", B), List.of(live("n"), live("x")), settings(1));

        assertThat(actions).containsExactly(drop("x", DropReason.FAILED_BUILD), build("n"));
    }

    @Test
    void cap_keepsTheLargestCollections_thenByName() {
        var counts = Map.of("a", B, "b", B + 1, "c", B, "d", B + 1);
        var registry = List.of(live("a"), live("b"), live("c"), live("d"));

        // Largest first (b, d at B+1), tie broken by name (b before d), then the B-row ones (a before c).
        assertThat(plan(leaf(), counts, registry, settings(3))).containsExactly(build("b"), build("d"), build("a"));
        assertThat(plan(leaf(), counts, registry, settings(1))).containsExactly(build("b"));
        assertThat(plan(leaf(), counts, registry, settings(16)))
            .containsExactly(build("b"), build("d"), build("a"), build("c"));
    }

    @Test
    void cap_rankByCount_whenTheCallerGivesUncappedCounts() {
        // Name order (a-small, m-mid, z-huge) is the reverse of count order, so only the count ranking passes.
        var counts = Map.of("a-small", B, "z-huge", 5_000_000, "m-mid", 40_000);

        assertThat(plan(leaf(), counts, List.of(live("a-small"), live("z-huge"), live("m-mid")), settings(2)))
            .containsExactly(build("z-huge"), build("m-mid"));
    }

    @Test
    void cap_existingIndexesTakeSlots_andANewcomerWaitsEvenIfLarger() {
        var actions = plan(leaf(valid("old")), Map.of("old", B, "new", B + 1),
            List.of(live("old"), live("new")), settings(1));

        assertThat(actions).isEmpty();
    }

    @Test
    void cap_aDropFreesItsSlotInTheSamePlan() {
        var actions = plan(leaf(valid("old")), Map.of("old", 3, "new", B),
            List.of(live("old"), live("new")), settings(1));

        assertThat(actions).containsExactly(drop("old", DropReason.BELOW_HALF_BUILD_THRESHOLD), build("new"));
    }

    @Test
    void cap_loweredBelowWhatExists_dropsNothing_andBuildsNothing() {
        var actions = plan(leaf(valid("a"), valid("b"), valid("c")),
            Map.of("a", B, "b", B, "c", B, "n", B + 1), List.of(live("a"), live("b"), live("c"), live("n")),
            settings(2));

        assertThat(actions).isEmpty();
    }

    // ---- renames ----

    @Test
    void copyRename_oldStaysLiveWithItsRows_andKeepsItsIndex_theNewNameBuilds() {
        var actions = plan(leaf(valid("old")), Map.of("old", B + 1, "new", B + 1), List.of(live("old"), live("new")));

        assertThat(actions).containsExactly(build("new"));
    }

    @Test
    void canonicalRename_oldIsSupersededOrGone_dropped_newNameAtOrAboveBBuilds() {
        var supersededOld = plan(leaf(valid("old")), Map.of("new", B), List.of(superseded("old", "new"), live("new")));
        var goneOld = plan(leaf(valid("old")), Map.of("new", B), List.of(live("new")));

        assertThat(supersededOld).containsExactly(drop("old", DropReason.SUPERSEDED), build("new"));
        assertThat(goneOld).containsExactly(drop("old", DropReason.NOT_IN_REGISTRY), build("new"));
    }

    // ---- quarantine ----

    @Test
    void quarantine_theSourceCountFalls_theIndexDrops_andTheQuarantineCollectionNeverBuilds() {
        // Rows moved to a quarantine collection; the source is still live in the registry but nearly empty.
        var actions = plan(leaf(valid("src")), Map.of("src", 3, "src__quarantine", B + 1),
            List.of(live("src"), state("src__quarantine", "quarantine")));

        assertThat(actions).containsExactly(drop("src", DropReason.BELOW_HALF_BUILD_THRESHOLD));
    }

    // ---- ordering and shape ----

    @Test
    void order_dropsThenSuspectsThenBuilds() {
        var actions = plan(leaf(valid("z-drop"), valid("a-suspect")), Map.of("z-drop", 1, "m-build", B),
            List.of(live("z-drop"), live("a-suspect"), live("m-build")));

        assertThat(actions).containsExactly(drop("z-drop", DropReason.BELOW_HALF_BUILD_THRESHOLD),
            new SkipCountSuspect("a-suspect"), build("m-build"));
    }

    @Test
    void oneSuspectPerCollection_evenWithTwoIndexesOnIt() {
        var second = new Index("pci_" + "0".repeat(24), true, "c");

        assertThat(plan(leaf(valid("c"), second), Map.of(), List.of(live("c"))))
            .containsExactly(new SkipCountSuspect("c"));
    }

    @Test
    void negativeCount_isRefused() {
        assertThatThrownBy(() -> plan(leaf(), Map.of("c", -1), List.of(live("c"))))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void planDoesNotMutateItsInputs_andIsDeterministic() {
        var counts = new HashMap<>(Map.of("a", B, "b", B));
        var registry = new ArrayList<>(List.of(live("a"), live("b")));
        var l = leaf();

        var first = plan(l, counts, registry);
        var second = plan(l, counts, registry);

        assertThat(first).isEqualTo(second).hasSize(2);
        assertThat(counts).hasSize(2);
        assertThat(registry).hasSize(2);
    }
}
