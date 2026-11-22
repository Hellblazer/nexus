// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.Build;
import dev.nexus.service.vectors.PciReconcilePlanner.Drop;
import dev.nexus.service.vectors.PciReconcilePlanner.DropReason;
import dev.nexus.service.vectors.PciReconcilePlanner.Reindex;
import dev.nexus.service.vectors.PciReconcilePlanner.Trigger;
import dev.nexus.service.vectors.PciReconcilePlanner.SkipCountSuspect;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.OptionalDouble;
import java.util.OptionalLong;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static dev.nexus.service.vectors.PciPlannerFixtures.*;

/**
 * RDR-227 Step 2 (nexus-43ulx.18): the decision half of the reconciler, one case per rule. No database, no clock,
 * no logging: {@link PciReconcilePlanner#plan} is a function of its arguments.
 */
class PciReconcilePlannerTest {

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
    void invalidIndexOutsideTheHoldersOwnBuild_isDroppedAsFailed_andRebuiltInTheSamePass() {
        // nexus-f3yqd (F): the drop frees the name and the slot, so the same plan builds it again: a wait of a full
        // sweep period for the rebuild is a lost build slot of that length after a restart that ended a build. The
        // executor skips the build when the drop did not happen (a CIC IF NOT EXISTS would no-op on the failed index).
        var actions = plan(leaf(invalid("c")), Map.of("c", B + 1), List.of(live("c")));

        assertThat(actions).containsExactly(drop("c", DropReason.FAILED_BUILD), build("c"));
    }

    @Test
    void invalidIndexOfACollectionThatCannotBuild_isDroppedAndNothingIsBuilt() {
        // Below B, not live, superseded, or backed off: the drop stands alone, as before.
        assertThat(plan(leaf(invalid("c")), Map.of("c", B - 1), List.of(live("c"))))
            .containsExactly(drop("c", DropReason.FAILED_BUILD));
        assertThat(plan(leaf(invalid("c")), Map.of("c", B + 1), List.of(state("c", "quarantine"))))
            .containsExactly(drop("c", DropReason.FAILED_BUILD));
        assertThat(PciReconcilePlanner.plan(leaf(invalid("c")), Map.of("c", B + 1), List.of(live("c")), Set.of("c"),
            null, DEFAULT)).as("backed off after a failed build").containsExactly(drop("c", DropReason.FAILED_BUILD));
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

    // ---- REINDEX CONCURRENTLY leftovers (nexus-rn1yd, T2 nexus_rdr/227-decision-4-amendment-reindex-leftovers) ----

    private static Index leftover(String collection, String suffix) {
        return new Index(name(collection) + suffix, false, null, collection);
    }

    private static Drop dropLeftover(String collection, String suffix) {
        return new Drop(name(collection) + suffix, collection, DropReason.REINDEX_LEFTOVER);
    }

    @Test
    void aReindexLeftover_isDroppedByName_whateverTheRegistryAndTheCountSay() {
        // Gone from the registry, zero rows, live and counting well above B: none of it matters to a leftover.
        assertThat(plan(leaf(leftover("c", "_ccnew")), Map.of(), List.of()))
            .containsExactly(dropLeftover("c", "_ccnew"));
        assertThat(plan(leaf(leftover("c", "_ccold")), Map.of("c", 0), List.of(superseded("c", "d"))))
            .containsExactly(dropLeftover("c", "_ccold"));
        assertThat(plan(leaf(valid("c"), leftover("c", "_ccnew")), Map.of("c", B + 1), List.of(live("c"))))
            .as("the valid base index stays").containsExactly(dropLeftover("c", "_ccnew"));
    }

    @Test
    void reindexLeftovers_runWithTheOtherDrops_inNameOrder_aheadOfSuspectsAndBuilds() {
        var l = leaf(valid("gone"), leftover("z", "_ccnew2"), valid("zero"), leftover("a", "_ccold"));
        var actions = plan(l, Map.of("zero", 0, "n", B), List.of(live("zero"), live("n"), live("a"), live("z")));

        var drops = actions.subList(0, 3);
        assertThat(drops).containsExactlyInAnyOrder(drop("gone", DropReason.NOT_IN_REGISTRY),
            dropLeftover("a", "_ccold"), dropLeftover("z", "_ccnew2"));
        assertThat(drops).extracting(a -> ((Drop) a).name()).isSorted();
        assertThat(actions.subList(3, actions.size())).containsExactly(new SkipCountSuspect("zero"), build("n"));
    }

    @Test
    void aReindexLeftover_isNeverChargedAgainstTheCap_andNeverBlocksABuild() {
        // Cap 1: the leftover is dropped and does not use the slot, so "n" still builds.
        assertThat(plan(leaf(leftover("x", "_ccnew")), Map.of("n", B), List.of(live("n")), settings(1)))
            .containsExactly(dropLeftover("x", "_ccnew"), build("n"));
        // A leftover for the very collection that wants an index is not "a parsed pci_ index": the build goes ahead.
        assertThat(plan(leaf(leftover("c", "_ccnew")), Map.of("c", B), List.of(live("c")), settings(1)))
            .containsExactly(dropLeftover("c", "_ccnew"), build("c"));
    }

    @Test
    void aReindexLeftover_withTheSwitchOff_isAnEmptyPlan() {
        var off = new PciSettings(false, B, 600, 16);

        assertThat(plan(leaf(leftover("c", "_ccnew")), Map.of(), List.of(), off)).isEmpty();
    }

    @Test
    void aLeftoverOnALeafWhoseBoundsDidNotParse_isAnEmptyPlan() {
        var noTenant = new Leaf("nexus", "x", MODEL, null, List.of(leftover("c", "_ccnew")));

        assertThat(plan(noTenant, Map.of(), List.of())).isEmpty();
    }

    @Test
    void aValidCcnewAndOperatorIndexes_areStillNeverTouched() {
        // Built directly, the way PciCatalog hands them over: unparsed, no leftover attribution.
        var validCcnew = new Index(name("c") + "_ccnew", true, null);
        var invalidUnattributed = new Index(name("c") + "_ccold", false, null);
        var operator = new Index("pci_foo", false, null);

        assertThat(plan(leaf(validCcnew, invalidUnattributed, operator), Map.of(), List.of())).isEmpty();
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

    // -- REINDEX triggers (RDR-230 Technical Design 3, nexus-4lnn1.1.10) ------------------------------------------

    private static final Instant NOW = Instant.parse("2026-11-14T00:00:00Z");

    private static PciCatalog.Anchor built(long rows, long bytes) {
        return new PciCatalog.Anchor(NOW.minus(Duration.ofDays(1)), rows, bytes, PciCatalog.ORIGIN_BUILT);
    }

    private static PciCatalog.Anchor adopted(long rows, long bytes) {
        return new PciCatalog.Anchor(NOW.minus(Duration.ofDays(1)), rows, bytes, PciCatalog.ORIGIN_ADOPTED);
    }

    /** A candidate with nothing to trigger on: built at 1000 rows and 8,000,000 bytes, unchanged, no canary result. */
    private static final class Cand {
        String collection = "c";
        PciCatalog.Anchor anchor = built(1000, 8_000_000);
        long bytes = 8_000_000;
        long rows = 1000;
        long written;
        OptionalLong previous = OptionalLong.empty();
        boolean bulkPending;
        PciCanary.State canary;
        int queries;
        Instant lastRedReindexAt;
        boolean backedOff;
        OptionalDouble reference = OptionalDouble.empty();

        Cand collection(String v) { collection = v; return this; }
        Cand anchor(PciCatalog.Anchor v) { anchor = v; return this; }
        Cand now(long bytesNow, long rowsNow, long writtenSince) {
            bytes = bytesNow; rows = rowsNow; written = writtenSince; return this;
        }
        Cand previous(long v) { previous = OptionalLong.of(v); return this; }
        Cand red(int q) { canary = PciCanary.State.RED; queries = q; return this; }
        Cand green() { canary = PciCanary.State.GREEN; queries = 10; return this; }
        Cand lastRed(Duration ago) { lastRedReindexAt = NOW.minus(ago); return this; }
        Cand reference(double v) { reference = OptionalDouble.of(v); return this; }

        PciReindexPolicy.Candidate build() {
            return new PciReindexPolicy.Candidate(name(collection), collection, 1024, anchor, bytes, rows, written,
                previous, bulkPending, canary, queries, lastRedReindexAt, backedOff, reference);
        }
    }

    private static List<Reindex> reindexes(Cand... cands) {
        return PciReconcilePlanner.reindexes(java.util.Arrays.stream(cands).map(Cand::build).toList(), NOW);
    }

    private static Trigger only(Cand c) {
        List<Reindex> out = reindexes(c);
        assertThat(out).as("one REINDEX").hasSize(1);
        return out.get(0).trigger();
    }

    @Test
    void pureIngestToTwiceTheSize_triggersNothing() {
        // 1000 rows at build, 1000 more written since, 2000 now: bytes per row unchanged, nothing deleted.
        assertThat(reindexes(new Cand().now(16_000_000, 2000, 1000))).isEmpty();
        // And a tenfold ingest.
        assertThat(reindexes(new Cand().now(80_000_000, 10_000, 9000))).isEmpty();
    }

    @Test
    void churnOfAQuarter_triggersDeletesSinceBuild_atTheThresholdAndNotBelow() {
        // anchor 1000 rows, none written since, 800 now: 200 deleted = 25% of 800 exactly.
        assertThat(only(new Cand().now(8_000_000, 800, 0))).isEqualTo(Trigger.DELETES_SINCE_BUILD);
        // 801 now: 199 deleted, below 25% of 801.
        assertThat(reindexes(new Cand().now(8_000_000, 801, 0))).isEmpty();
        // Re-writes count as dead graph entries: 1000 at build, 300 re-written, 1000 now: 300 dead = 30%.
        assertThat(only(new Cand().now(8_000_000, 1000, 300))).isEqualTo(Trigger.DELETES_SINCE_BUILD);
        // A collection that grew past its anchor has no deletes (max(0, ...)).
        assertThat(reindexes(new Cand().now(8_000_000, 1500, 0))).isEmpty();
    }

    @Test
    void aTenPercentBulkDelete_triggersBulkDelete_whichVacuumsAfter() {
        // The previous check saw 1000 rows, now 900: fell by exactly 10%. (anchor 900 so no other rule fires.)
        var bulk = new Cand().anchor(built(900, 7_200_000)).now(7_200_000, 900, 0).previous(1000);

        List<Reindex> out = reindexes(bulk);

        assertThat(out).hasSize(1);
        assertThat(out.get(0).trigger()).isEqualTo(Trigger.BULK_DELETE);
        assertThat(out.get(0).vacuumAfter()).as("REINDEX, then VACUUM (ANALYZE)").isTrue();
        // 9.9% is not enough.
        assertThat(reindexes(new Cand().anchor(built(901, 7_208_000)).now(7_208_000, 901, 0).previous(1000))).isEmpty();
        // Another trigger's REINDEX is not followed by a vacuum.
        var deletes = new Cand().now(8_000_000, 700, 0);
        assertThat(only(deletes)).isEqualTo(Trigger.DELETES_SINCE_BUILD);
        assertThat(reindexes(deletes).get(0).vacuumAfter()).isFalse();
    }

    @Test
    void theBulkDeleteRule_hasNoPreviousCountAfterABoot_andFiresFromAPendingFlag() {
        // Without a previous count (a restart, a new holder) a 40% fall is the deletes rule's to judge, not the bulk rule's.
        var afterBoot = new Cand().anchor(built(600, 4_800_000)).now(4_800_000, 600, 0);
        assertThat(reindexes(afterBoot)).isEmpty();
        // A bulk delete seen on an earlier pass whose REINDEX the per-pass limit deferred stays owed.
        var pending = new Cand().anchor(built(600, 4_800_000)).now(4_800_000, 600, 0);
        pending.bulkPending = true;
        assertThat(only(pending)).isEqualTo(Trigger.BULK_DELETE);
    }

    @Test
    void aRedCanary_triggersOnlyWithTenQueries_andOnlyOncePerDay() {
        assertThat(only(new Cand().red(10))).isEqualTo(Trigger.RED_CANARY);
        // Below ten queries the red is recorded and shown, and triggers nothing (Sam, 2026-11-14).
        assertThat(reindexes(new Cand().red(9))).isEmpty();
        assertThat(reindexes(new Cand().red(3))).isEmpty();
        // A green result, or none, triggers nothing.
        assertThat(reindexes(new Cand().green())).isEmpty();
        assertThat(reindexes(new Cand())).isEmpty();
        // The last red-canary REINDEX of this index was an hour ago: still red, not rebuilt (red_after_reindex).
        assertThat(reindexes(new Cand().red(10).lastRed(Duration.ofHours(1)))).isEmpty();
        assertThat(reindexes(new Cand().red(10).lastRed(Duration.ofHours(23)))).isEmpty();
        // 24 hours on it may run again, once.
        assertThat(only(new Cand().red(10).lastRed(Duration.ofHours(24)))).isEqualTo(Trigger.RED_CANARY);
    }

    /** The day's limit is also read from the anchor, which survives a restart and a change of holder. */
    @Test
    void aRedCanary_waitsADayFromTheAnchor_whateverTheEnginesMemoryOfItsLastRedReindex() {
        // The memory of the last red-canary REINDEX is gone (a restart): lastRedReindexAt is null. The anchor says the
        // index was built, adopted or reindexed an hour ago.
        var fresh = new Cand().anchor(new PciCatalog.Anchor(NOW.minus(Duration.ofHours(1)), 1000, 8_000_000,
            PciCatalog.ORIGIN_BUILT)).red(10);
        assertThat(reindexes(fresh)).isEmpty();
        var almost = new Cand().anchor(new PciCatalog.Anchor(NOW.minus(Duration.ofHours(24)).plusSeconds(1), 1000,
            8_000_000, PciCatalog.ORIGIN_BUILT)).red(10);
        assertThat(reindexes(almost)).isEmpty();
        var aDay = new Cand().anchor(new PciCatalog.Anchor(NOW.minus(Duration.ofHours(24)), 1000, 8_000_000,
            PciCatalog.ORIGIN_BUILT)).red(10);
        assertThat(only(aDay)).isEqualTo(Trigger.RED_CANARY);
        // An adopted anchor's built_at is the adoption time: the same day.
        var adoptedNow = new Cand().anchor(new PciCatalog.Anchor(NOW.minus(Duration.ofHours(2)), 1000, 8_000_000,
            PciCatalog.ORIGIN_ADOPTED)).red(10);
        assertThat(reindexes(adoptedNow)).isEmpty();
        // The other triggers do not wait on the anchor's age.
        var deletes = new Cand().anchor(new PciCatalog.Anchor(NOW.minus(Duration.ofHours(1)), 1000, 8_000_000,
            PciCatalog.ORIGIN_BUILT)).now(8_000_000, 700, 0);
        assertThat(only(deletes)).isEqualTo(Trigger.DELETES_SINCE_BUILD);
    }

    @Test
    void theSizeRule_comparesAgainstTheAnchor_orForAnAdoptedIndexTheInstallReference() {
        // built at 8000 bytes a row: 1.4x is 11,200.
        assertThat(only(new Cand().now(11_300_000, 1000, 0))).isEqualTo(Trigger.SIZE);
        assertThat(reindexes(new Cand().now(11_100_000, 1000, 0))).isEmpty();
        // An adopted anchor is a snapshot of an index of unknown age: it is judged against the reference, 8200 a row.
        var adoptedBig = new Cand().anchor(adopted(1000, 8_000_000)).now(11_500_000, 1000, 0).reference(8_200);
        assertThat(only(adoptedBig)).isEqualTo(Trigger.SIZE);
        var adoptedSmall = new Cand().anchor(adopted(1000, 8_000_000)).now(11_400_000, 1000, 0).reference(8_200);
        assertThat(reindexes(adoptedSmall)).isEmpty();
        // No reference for the dimension: the rule is off for an adopted index, however big.
        assertThat(reindexes(new Cand().anchor(adopted(1000, 8_000_000)).now(80_000_000, 1000, 0))).isEmpty();
    }

    @Test
    void zeroRows_triggerNothing() {
        assertThat(reindexes(new Cand().now(8_000_000, 0, 0))).isEmpty();
        assertThat(reindexes(new Cand().now(0, 0, 0).red(10))).as("not even a red canary: nothing to rebuild").isEmpty();
    }

    @Test
    void aBackedOffIndex_isNotReindexed_whateverTheTrigger() {
        var c = new Cand().now(8_000_000, 700, 0);
        c.backedOff = true;
        assertThat(reindexes(c)).isEmpty();
    }

    @Test
    void whenSeveralRulesFire_bulkDeleteNamesTheTrigger_soItsVacuumIsNotLost() {
        var c = new Cand().anchor(built(1000, 8_000_000)).now(8_000_000, 700, 0).previous(1000).red(10);
        assertThat(only(c)).isEqualTo(Trigger.BULK_DELETE);
    }

    @Test
    void theReindexesAreLargestFirst_andAPassTakesTwo() {
        var small = new Cand().collection("small").now(1_000, 700, 0).anchor(built(1000, 1_000));
        var middle = new Cand().collection("middle").now(5_000, 700, 0).anchor(built(1000, 5_000));
        var big = new Cand().collection("big").now(9_000, 700, 0).anchor(built(1000, 9_000));

        List<Reindex> ordered = reindexes(small, middle, big);

        assertThat(ordered).extracting(Reindex::collection).containsExactly("big", "middle", "small");
        assertThat(ordered).extracting(Reindex::bytes).containsExactly(9_000L, 5_000L, 1_000L);
        assertThat(PciReconcilePlanner.firstPerPass(ordered)).extracting(Reindex::collection)
            .containsExactly("big", "middle");
        assertThat(PciReindexPolicy.MAX_PER_PASS).isEqualTo(2);
    }

    @Test
    void theThresholds_areThoseSamRecordedOn2026_11_14() {
        assertThat(PciReindexPolicy.SIZE_RATIO).isEqualTo(1.4);
        assertThat(PciReindexPolicy.LEAF_SIZE_RATIO).isEqualTo(1.5);
        assertThat(PciReindexPolicy.DELETES_SINCE_BUILD_FRACTION).isEqualTo(0.25);
        assertThat(PciReindexPolicy.BULK_DELETE_FRACTION).isEqualTo(0.10);
        assertThat(PciReindexPolicy.MAX_PER_PASS).isEqualTo(2);
        assertThat(PciReindexPolicy.RED_REINDEX_MIN_INTERVAL).isEqualTo(Duration.ofHours(24));
        assertThat(PciReindexPolicy.RED_CANARY_MIN_QUERIES).isEqualTo(10);
    }
}
