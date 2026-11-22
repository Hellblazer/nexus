// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
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
import static dev.nexus.service.vectors.PciPlannerFixtures.*;

/**
 * RDR-229 P3.4 (nexus-p2m1x.22) and the gate 3 retire: the id-predicate half of the reconciler's decisions, one case per rule
 * ({@link PciReconcilePlanner#planWithIds}). No database, no clock, no logging. The builders are {@link PciPlannerFixtures}'s;
 * the name half is {@link PciReconcilePlannerTest}.
 */
class PciReconcilePlannerIdTest {

    // ---- RDR-229 P3.4 (nexus-p2m1x.22): the id predicate beside the name predicate (S-2 transition) ----

    private static String idName(long id) {
        return PciCatalog.indexNameById(MODEL, TENANT, id);
    }

    private static Index validId(long id) {
        return new Index(idName(id), true, null, null, null, null, id);
    }

    private static Index invalidId(long id) {
        return new Index(idName(id), false, null, null, null, null, id);
    }

    private static RegistryRow liveWithId(String collection, long id) {
        return new RegistryRow(collection, "live", "", id);
    }

    private static final PciReconcilePlanner.IdPlanInputs GATE2 =
        new PciReconcilePlanner.IdPlanInputs(true, Set.of());

    private static PciReconcilePlanner.Plan planIds(Leaf leaf, Map<String, Integer> counts, List<RegistryRow> registry,
                                                    PciReconcilePlanner.IdPlanInputs ids) {
        return PciReconcilePlanner.planWithIds(leaf, counts, registry, Set.of(), null, DEFAULT, ids);
    }

    private static Build buildById(String collection, long id) {
        return new Build(idName(id), collection, id);
    }

    private static Drop dropId(long id, String collection, DropReason reason) {
        return new Drop(idName(id), collection, reason);
    }

    @Test
    void id_gate2Held_buildsTheIdIndex_andKeepsTheNameIndex() {
        var plan = planIds(leaf(valid("c")), Map.of("c", B), List.of(liveWithId("c", 7)), GATE2);

        assertThat(plan.actions()).containsExactly(buildById("c", 7));
        assertThat(plan.actions()).as("no name index is dropped to make way").noneMatch(a -> a instanceof Drop);
    }

    @Test
    void id_beforeGate2_noIdBuildIsPlanned_butTheCollectionIsStillPlanned() {
        var plan = planIds(leaf(valid("c")), Map.of("c", B), List.of(liveWithId("c", 7)),
            new PciReconcilePlanner.IdPlanInputs(false, Set.of()));

        assertThat(plan.actions()).isEmpty();
        // The old entry point never plans an id build.
        assertThat(plan(leaf(valid("c")), Map.of("c", B), List.of(liveWithId("c", 7)))).isEmpty();
    }

    @Test
    void id_aNullIdRow_getsNoIdBuild_keepsItsNameIndex_andStaysPlannedSoGate3CannotHold() {
        var plan = planIds(leaf(valid("c"), valid("ok")), Map.of("c", B, "ok", B),
            List.of(liveWithId("c", 7), liveWithId("ok", 8)),
            new PciReconcilePlanner.IdPlanInputs(true, Set.of("c")));

        assertThat(plan.actions()).containsExactly(buildById("ok", 8));
    }

    @Test
    void id_aRegistryRowWithNoId_isNeverBuiltByTheId() {
        var plan = planIds(leaf(valid("c")), Map.of("c", B), List.of(live("c")), GATE2);

        assertThat(plan.actions()).isEmpty();
    }

    @Test
    void id_anInFlightBuild_isLeftAlone_nameIndexKept_andNothingIsBuiltTwice() {
        var plan = PciReconcilePlanner.planWithIds(leaf(valid("c"), invalidId(7)), Map.of("c", B),
            List.of(liveWithId("c", 7)), Set.of(), idName(7), DEFAULT, GATE2);

        assertThat(plan.actions()).isEmpty();
    }

    @Test
    void id_aKilledBuild_isDroppedAndBuiltAgainInTheSamePlan_nameIndexKept() {
        var plan = planIds(leaf(valid("c"), invalidId(7)), Map.of("c", B), List.of(liveWithId("c", 7)), GATE2);

        assertThat(plan.actions()).containsExactly(dropId(7, "c", DropReason.FAILED_BUILD), buildById("c", 7));
        assertThat(plan.actions()).noneMatch(a -> a instanceof Drop d && d.name().equals(name("c")));
    }

    @Test
    void id_aKilledBuildBeforeGate2_isDroppedButNotRebuilt() {
        var plan = planIds(leaf(valid("c"), invalidId(7)), Map.of("c", B), List.of(liveWithId("c", 7)),
            new PciReconcilePlanner.IdPlanInputs(false, Set.of()));

        assertThat(plan.actions()).containsExactly(dropId(7, "c", DropReason.FAILED_BUILD));
    }

    @Test
    void id_aValidIdIndex_isKept_andNeedsNoBuild() {
        var plan = planIds(leaf(valid("c"), validId(7)), Map.of("c", B), List.of(liveWithId("c", 7)), GATE2);

        assertThat(plan.actions()).isEmpty();
    }

    @Test
    void id_dropRules_haveIdKeyedCounterparts() {
        // NOT_IN_REGISTRY: a rename minted a new id, so the old id's index has no row and the new id builds (RDR item 6).
        var renamed = planIds(leaf(valid("c"), validId(7)), Map.of("c", B), List.of(liveWithId("c", 8)), GATE2);
        assertThat(renamed.actions()).containsExactly(dropId(7, null, DropReason.ID_NOT_IN_REGISTRY), buildById("c", 8));

        var superseded = planIds(leaf(valid("c"), validId(7)), Map.of("c", B),
            List.of(new RegistryRow("c", "live", "other", 7L)), GATE2);
        assertThat(superseded.actions()).contains(dropId(7, "c", DropReason.ID_SUPERSEDED));

        var below = planIds(leaf(valid("c"), validId(7)), Map.of("c", B / 2 - 1), List.of(liveWithId("c", 7)), GATE2);
        assertThat(below.actions()).contains(dropId(7, "c", DropReason.ID_BELOW_HALF_BUILD_THRESHOLD));
        assertThat(below.actions()).as("the name index drops by its own rule, not because the id one did")
            .contains(drop("c", DropReason.BELOW_HALF_BUILD_THRESHOLD));

        // Between B/2 and B the id index stays, as the name index does.
        var between = planIds(leaf(valid("c"), validId(7)), Map.of("c", B - 1), List.of(liveWithId("c", 7)), GATE2);
        assertThat(between.actions()).isEmpty();
    }

    @Test
    void id_aZeroCountForALiveIndexedCollection_isACountingFailure_neverADrop() {
        var plan = planIds(leaf(validId(7)), Map.of(), List.of(liveWithId("c", 7)), GATE2);

        assertThat(plan.actions()).containsExactly(new SkipCountSuspect("c"));
    }

    @Test
    void id_aLiveCollectionThatCountsZero_andKeepsANameIndex_getsAnIdIndexToo_soGate3IsReachable() {
        // N1: the name plan keeps the name index of a live collection that counts zero (the counting-failure guard), and gate 3 requires an id
        // index for every collection with a VALID name index: the id plan must build it, or gate 3 could never hold.
        var withName = planIds(leaf(valid("c")), Map.of(), List.of(liveWithId("c", 7)), GATE2);
        assertThat(withName.actions()).containsExactly(new SkipCountSuspect("c"), buildById("c", 7));

        // With no name index, a collection that counts zero is not built (nothing requires it).
        assertThat(planIds(leaf(), Map.of(), List.of(liveWithId("c", 7)), GATE2).actions()).isEmpty();
        // Between 1 and B/2 - 1 the name index drops, so no id index is built for it (it would be dropped the next pass).
        assertThat(planIds(leaf(valid("c")), Map.of("c", B / 2 - 1), List.of(liveWithId("c", 7)), GATE2).actions())
            .containsExactly(drop("c", DropReason.BELOW_HALF_BUILD_THRESHOLD));
    }

    @Test
    void id_aBackedOffCollection_isNotBuilt_theOthersAre_andItsNameIndexIsKept() {
        // Backoff holds a collection back from BUILDING only. Gate 3 does not read the plan: it requires an id index for every collection that
        // has a VALID name index, backed off or not (PciIdIndexIntegrationTest pins that against the database).
        var plan = PciReconcilePlanner.planWithIds(leaf(valid("c"), valid("d")), Map.of("c", B, "d", B),
            List.of(liveWithId("c", 7), liveWithId("d", 8)), Set.of(PciReconcilePlanner.idBackoffKey(7)), null, DEFAULT, GATE2);

        assertThat(plan.actions()).containsExactly(buildById("d", 8));
    }

    @Test
    void id_theHysteresisBand_buildsTheIdIndexWhileANameIndexExists_butNotForACollectionWithNone() {
        // B/2 <= count < B: the name index is kept, so gate 3 can only be reached if the id index is built too.
        var band = B - 1;
        var withName = planIds(leaf(valid("c")), Map.of("c", band), List.of(liveWithId("c", 7)), GATE2);
        assertThat(withName.actions()).containsExactly(buildById("c", 7));

        var noName = planIds(leaf(), Map.of("c", band), List.of(liveWithId("c", 7)), GATE2);
        assertThat(noName.actions()).as("a collection below B with no name index is not built").isEmpty();

        var tooSmall = planIds(leaf(valid("c")), Map.of("c", B / 2 - 1), List.of(liveWithId("c", 7)), GATE2);
        assertThat(tooSmall.actions()).as("below B/2 the name index goes and no id index is built")
            .containsExactly(drop("c", DropReason.BELOW_HALF_BUILD_THRESHOLD));
    }

    @Test
    void id_candidatesThatOutnumberTheBudget_areRecountedHigher_soTheLargestWinAndNotTheAlphabeticallyFirst() {
        // 6 collections at the capped count B, budget 4. Their real sizes differ; the recount keeps the four largest.
        var s = settings(4);
        var rows = new ArrayList<RegistryRow>();
        var counts = new HashMap<String, Integer>();
        var real = new HashMap<String, Integer>();
        for (int i = 1; i <= 6; i++) {
            String c = "c" + i;
            rows.add(liveWithId(c, i));
            counts.put(c, B + 1);                       // every one is capped at B + 1
            real.put(c, 10_000 - 1_000 * (6 - i));      // c6 is the largest, c1 the smallest
        }
        var recounted = new ArrayList<String>();
        var reads = new PciReconcilePlanner.IdReads() {
            @Override public Set<String> nullIdCollections(java.util.Collection<RegistryRow> candidates) {
                return Set.of();
            }

            @Override public Map<String, Integer> recount(java.util.Collection<String> collections) {
                recounted.addAll(collections);
                var m = new HashMap<String, Integer>();
                collections.forEach(c -> m.put(c, real.get(c)));
                return m;
            }
        };

        var plan = PciReconcilePlanner.planWithIds(leaf(), counts, rows, Set.of(), null, s,
            new PciReconcilePlanner.IdPlanInputs(true, reads, false));

        assertThat(recounted).as("recounted because they outnumber the budget").hasSize(6);
        assertThat(plan.actions().stream().map(a -> (Build) a).filter(Build::byId).map(Build::collection))
            .as("the four largest, not c1..c4").containsExactly("c6", "c5", "c4", "c3");
        // Within the budget no recount is asked for.
        recounted.clear();
        PciReconcilePlanner.planWithIds(leaf(), counts, rows.subList(0, 3), Set.of(), null, s,
            new PciReconcilePlanner.IdPlanInputs(true, reads, false));
        assertThat(recounted).isEmpty();
    }

    @Test
    void id_theNullIdProbeSeesOnlyTheCandidatesWithinTheBudget_andOnlyAfterGate2() {
        var s = settings(2);
        var rows = List.of(liveWithId("a", 1), liveWithId("b", 2), liveWithId("c", 3), liveWithId("d", 4));
        var counts = Map.of("a", B + 4, "b", B + 3, "c", B + 2, "d", B + 1);
        var probed = new ArrayList<String>();
        PciReconcilePlanner.IdReads reads = candidates -> {
            candidates.forEach(c -> probed.add(c.name()));
            return Set.of("a");
        };

        var plan = PciReconcilePlanner.planWithIds(leaf(), counts, rows, Set.of(), null, s,
            new PciReconcilePlanner.IdPlanInputs(true, reads, false));

        assertThat(probed).as("two slots: two probes, not four").containsExactly("a", "b");
        assertThat(plan.actions().stream().filter(a -> a instanceof Build bd && bd.byId()))
            .as("a is held back by its null-id row").containsExactly(buildById("b", 2));
        probed.clear();
        PciReconcilePlanner.planWithIds(leaf(), counts, rows, Set.of(), null, s, new PciReconcilePlanner.IdPlanInputs(false, reads, false));
        assertThat(probed).as("before gate 2 nothing is probed").isEmpty();
    }

    @Test
    void id_nameAndIdIndexesCoexistUnderTheCap_eachWithItsOwnBudget() {
        // Cap 2: two name indexes and two id indexes on the leaf is within each budget; a third collection builds nothing.
        var s = settings(2);
        var l = leaf(valid("a"), valid("b"), validId(1), validId(2));
        var rows = List.of(liveWithId("a", 1), liveWithId("b", 2), liveWithId("c", 3));
        var counts = Map.of("a", B, "b", B, "c", B);

        var plan = PciReconcilePlanner.planWithIds(l, counts, rows, Set.of(), null, s, GATE2);

        assertThat(plan.actions()).isEmpty();
    }

    @Test
    void id_aLeafAtTheCapThroughTheTransition_buildsIdIndexesUpToTheCapAndDropsNoNameIndex() {
        // S-2 (nexus-p2m1x.40): 16 collections at the per-leaf cap, each holding a valid NAME index. The id budget is
        // the cap's own, so all 16 id indexes are planned (one in flight at a time in the builder), no name index is
        // dropped, and no 17th collection is built into either family.
        var indexes = new ArrayList<Index>();
        var rows = new ArrayList<RegistryRow>();
        var counts = new HashMap<String, Integer>();
        for (int i = 1; i <= 17; i++) {
            String c = "c" + String.format("%02d", i);
            if (i <= 16) {
                indexes.add(valid(c));
            }
            rows.add(liveWithId(c, i));
            counts.put(c, B + (17 - i));      // c01 is the largest
        }
        var plan = planIds(leaf(indexes.toArray(Index[]::new)), counts, rows, GATE2);

        var builds = plan.actions().stream().filter(a -> a instanceof Build).map(a -> (Build) a).toList();
        assertThat(builds).hasSize(16).allMatch(Build::byId);
        assertThat(builds.stream().map(Build::collectionId)).doesNotContain(17L);   // c17 is the smallest: out of the ranking
        assertThat(plan.actions()).noneMatch(a -> a instanceof Drop);
        // The name plan for the same leaf: at the cap, so the 17th gets no name index either.
        assertThat(PciReconcilePlanner.plan(leaf(indexes.toArray(Index[]::new)), counts, rows, Set.of(), null, DEFAULT))
            .isEmpty();
    }

    @Test
    void id_atTheCapWithSomeIdIndexesValidAndOneInFlight_buildsOnlyTheRemainingSlots() {
        var s = settings(4);
        var l = leaf(valid("a"), valid("b"), valid("c"), valid("d"), validId(1), validId(2), invalidId(3));
        var rows = List.of(liveWithId("a", 1), liveWithId("b", 2), liveWithId("c", 3), liveWithId("d", 4));
        var counts = Map.of("a", B, "b", B, "c", B, "d", B);

        var plan = PciReconcilePlanner.planWithIds(l, counts, rows, Set.of(), idName(3), s, GATE2);

        assertThat(plan.actions()).containsExactly(buildById("d", 4));
    }

    @Test
    void id_planWithIdsDoesNotChangeTheNameHalf_andTheOldEntryPointIgnoresIdIndexes() {
        var l = leaf(valid("c"), validId(7), invalidId(9));
        var counts = Map.of("c", B, "d", B);
        var rows = List.of(liveWithId("c", 7), liveWithId("d", 8));

        var nameOnly = PciReconcilePlanner.plan(l, counts, rows, Set.of(), null, DEFAULT);

        assertThat(nameOnly).containsExactly(build("d"));
        var withIds = planIds(l, counts, rows, GATE2).actions();
        assertThat(withIds).containsAll(nameOnly);
        assertThat(withIds).contains(buildById("d", 8), dropId(9, null, DropReason.FAILED_BUILD));
    }

    @Test
    void id_dropsComeFirst_thenSuspects_thenBuilds() {
        var plan = planIds(leaf(valid("c"), validId(5), validId(6)), Map.of("c", B, "z", B),
            List.of(liveWithId("c", 6), liveWithId("z", 9)), GATE2);

        assertThat(plan.actions().get(0)).isInstanceOf(Drop.class);
        assertThat(plan.actions().get(plan.actions().size() - 1)).isInstanceOf(Build.class);
    }

    @Test
    void id_switchOff_isAnEmptyPlan() {
        var off = new PciSettings(false, B, 600, 16);

        var plan = PciReconcilePlanner.planWithIds(leaf(validId(7)), Map.of(), List.of(), Set.of(), null, off, GATE2);

        assertThat(plan.actions()).isEmpty();
    }

    // ---- linda 2026-11-22: the name family retires once gate 3 holds (REPLACED_BY_ID) ----

    private static final PciReconcilePlanner.IdPlanInputs GATE3 =
        new PciReconcilePlanner.IdPlanInputs(true, Set.of(), true);

    private static Drop replaced(String collection) {
        return new Drop(name(collection), collection, DropReason.REPLACED_BY_ID);
    }

    @Test
    void retire_beforeGate3_noNameIndexIsDropped_whateverTheIdIndexesAre() {
        var l = leaf(valid("a"), valid("b"), validId(1), validId(2));
        var rows = List.of(liveWithId("a", 1), liveWithId("b", 2));

        var plan = planIds(l, Map.of("a", B, "b", B), rows, GATE2);

        assertThat(plan.actions()).isEmpty();
    }

    @Test
    void retire_atGate3_eachNameIndexWhoseIdIndexIsValidIsDropped_inNameOrder_forTheExecutorToRunOneAtATime() {
        var l = leaf(valid("b"), valid("a"), validId(1), validId(2));
        var rows = List.of(liveWithId("a", 1), liveWithId("b", 2));

        var plan = planIds(l, Map.of("a", B, "b", B), rows, GATE3);

        assertThat(plan.actions()).containsExactly(replaced("a"), replaced("b"));
        assertThat(plan.actions()).as("the id indexes stay").noneMatch(a -> a instanceof Drop d && d.name().equals(idName(1)));
    }

    @Test
    void retire_aCollectionWhoseIdIndexIsInvalidOrMissingKeepsItsNameIndex_evenAfterGate3() {
        // c1: id index INVALID and in flight; c2: id index INVALID, not in flight (dropped and rebuilt this plan); c3: no id index
        var l = leaf(valid("c1"), valid("c2"), valid("c3"), invalidId(1), invalidId(2));
        var rows = List.of(liveWithId("c1", 1), liveWithId("c2", 2), liveWithId("c3", 3));
        var counts = Map.of("c1", B, "c2", B, "c3", B);

        var plan = PciReconcilePlanner.planWithIds(l, counts, rows, Set.of(), idName(1), DEFAULT, GATE3);

        assertThat(plan.actions()).noneMatch(a -> a instanceof Drop d && d.reason() == DropReason.REPLACED_BY_ID);
        assertThat(plan.actions()).contains(dropId(2, "c2", DropReason.FAILED_BUILD), buildById("c2", 2), buildById("c3", 3));
    }

    @Test
    void retire_atGate3_noNameIndexIsBuilt_soADroppedOneIsNotBuiltBackNextPass() {
        // c has no name index (it was replaced) and a valid id index; d is a new collection with neither: only the id index is built.
        var l = leaf(validId(1));
        var rows = List.of(liveWithId("c", 1), liveWithId("d", 2));

        var plan = planIds(l, Map.of("c", B, "d", B), rows, GATE3);

        assertThat(plan.actions()).containsExactly(buildById("d", 2));
        assertThat(plan.actions()).noneMatch(a -> a instanceof Build b && !b.byId());
        // Without gate 3 the name half still plans the name builds.
        assertThat(planIds(l, Map.of("c", B, "d", B), rows, GATE2).actions()).contains(build("c"), build("d"));
    }

    @Test
    void retire_aNameIndexThatAnotherRuleDropsIsNotDroppedTwice_andANonLiveOrSupersededCollectionIsNotReplaced() {
        var l = leaf(valid("small"), valid("quar"), validId(1), validId(2));
        var rows = List.of(liveWithId("small", 1), new RegistryRow("quar", "quarantine", "", 2L));

        var plan = planIds(l, Map.of("small", B / 2 - 1, "quar", B), rows, GATE3);

        assertThat(plan.actions().stream().filter(a -> a instanceof Drop d && d.name().equals(name("small"))).count())
            .as("dropped once, by the below-half rule").isEqualTo(1);
        assertThat(plan.actions()).contains(drop("small", DropReason.BELOW_HALF_BUILD_THRESHOLD));
        assertThat(plan.actions()).as("a quarantine's name index is not the planner's to retire")
            .noneMatch(a -> a instanceof Drop d && d.name().equals(name("quar")) && d.reason() == DropReason.REPLACED_BY_ID);
    }

    // ---- N2: the leftover of an id index's REINDEX CONCURRENTLY is dropped like the name family's ----

    @Test
    void id_aReindexLeftoverOfAnIdIndex_isDroppedAsAReindexLeftover_whateverTheGatesSay() {
        var leftover = new Index(idName(7) + "_ccnew", false, null, null, null, null, null, 7L);
        var l = leaf(valid("c"), validId(7), leftover);
        var rows = List.of(liveWithId("c", 7));

        for (var inputs : List.of(GATE2, GATE3, PciReconcilePlanner.IdPlanInputs.NONE)) {
            var plan = planIds(l, Map.of("c", B), rows, inputs);
            assertThat(plan.actions()).as("gate2=%s gate3=%s", inputs.gate2(), inputs.gate3())
                .contains(new Drop(leftover.name(), null, DropReason.REINDEX_LEFTOVER));
            assertThat(plan.actions()).as("the original id index is not touched")
                .noneMatch(a -> a instanceof Drop d && d.name().equals(idName(7)));
        }
        // The name-only entry point drops it too (it is the name family's rule, applied to the id kind).
        assertThat(plan(l, Map.of("c", B), rows)).contains(new Drop(leftover.name(), null, DropReason.REINDEX_LEFTOVER));
    }
}
