// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCanary.Target;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 Technical Design 7 (nexus-4lnn1.1.5): the maintenance status object's data half. The wire half is
 * {@code StatusHandlerTest}; the reconciler's use of it is {@code PciReconcilerIntegrationTest}.
 */
class PciMaintenanceStatusTest {

    private static final Instant T0 = Instant.parse("2026-10-20T08:00:00Z");

    private static String indexName(int n) {
        return String.format("pci_%024x", n);
    }

    private static Target target(int n) {
        return new Target("chunks_leaf_" + n, "tenant-" + n, "model", "collection-" + n, indexName(n));
    }

    private static Result result(int n, State state, Instant at) {
        boolean measured = state != State.UNMEASURED;
        return new Result(target(n), "daily", state, measured ? null : "no_queries", measured ? 10 : 2,
            measured ? 0.98 : null, measured ? 0.99 : null, state == State.RED ? 1 : 0, at, null, null);
    }

    private static PciMaintenanceStatus.Entry entryOf(int n, State state, Instant at) {
        return PciMaintenanceStatus.entry(result(n, state, at), null, Optional.empty());
    }

    private static PciReconciler.IndexReading reading(String origin, long anchorRows, long anchorBytes, long bytes,
                                                      long rows, long writtenSince) {
        return new PciReconciler.IndexReading(new PciCatalog.Anchor(T0, anchorRows, anchorBytes, origin), bytes, rows,
            writtenSince, T0);
    }

    // -- test 5: an engine that never held the lock ---------------------------------------------------------

    @Test
    void anEngineWithNoResultsReportsZerosAndAnEmptyList_andTheReindexHalfIsOff() {
        var s = PciMaintenanceStatus.phase1(List.of());

        assertThat(s.reindex()).isEqualTo("off");
        assertThat(s.reindexing()).isNull();
        assertThat(s.lastReindexAt()).isNull();
        assertThat(s.reindexes()).isZero();
        assertThat(s.canary().green()).isZero();
        assertThat(s.canary().red()).isZero();
        assertThat(s.canary().unmeasured()).isZero();
        assertThat(s.canary().lastRunAt()).isNull();
        assertThat(s.canary().indexes()).isEmpty();
    }

    // -- test 3: the cap and the order -----------------------------------------------------------------------

    @Test
    void theListIsCappedAt64_redFirst_thenUnmeasured_thenGreen_newestFirstWithinAState_andTheCountsCoverEveryResult() {
        var all = new ArrayList<PciMaintenanceStatus.Entry>();
        // 70 greens, 3 unmeasured and 5 reds, the reds spread through the input so input order cannot be what sorts.
        for (int n = 0; n < 70; n++) {
            all.add(entryOf(n, State.GREEN, T0.plusSeconds(n)));
            if (n % 14 == 0) {
                all.add(entryOf(1000 + n, State.RED, T0.plusSeconds(n)));
            }
            if (n % 25 == 0) {
                all.add(entryOf(2000 + n, State.UNMEASURED, T0.plusSeconds(n)));
            }
        }

        var s = PciMaintenanceStatus.phase1(all);

        assertThat(s.canary().indexes()).hasSize(64);
        assertThat(s.canary().red()).as("counts cover every result, not the 64 shown").isEqualTo(5);
        assertThat(s.canary().unmeasured()).isEqualTo(3);
        assertThat(s.canary().green()).isEqualTo(70);
        var states = s.canary().indexes().stream().map(PciMaintenanceStatus.Entry::state).toList();
        assertThat(states.subList(0, 5)).as("every red ahead of any other").containsOnly("red");
        assertThat(states.subList(5, 8)).containsOnly("unmeasured");
        assertThat(states.subList(8, 64)).containsOnly("green");
        var greens = s.canary().indexes().subList(8, 64);
        assertThat(greens.get(0).measuredAt()).as("newest first within a state").isEqualTo(T0.plusSeconds(69));
        assertThat(greens).extracting(PciMaintenanceStatus.Entry::measuredAt)
            .isSortedAccordingTo(java.util.Comparator.reverseOrder());
        assertThat(s.canary().lastRunAt()).isEqualTo(T0.plusSeconds(69));
    }

    @Test
    void entriesOfTheSameStateAndTimeAreOrderedByIndexName() {
        var s = PciMaintenanceStatus.phase1(List.of(entryOf(9, State.GREEN, T0), entryOf(3, State.GREEN, T0),
            entryOf(5, State.GREEN, T0)));

        assertThat(s.canary().indexes()).extracting(PciMaintenanceStatus.Entry::index)
            .containsExactly(indexName(3), indexName(5), indexName(9));
    }

    // -- test 2: an unmeasured entry carries its queries; test 4: no name ------------------------------------

    @Test
    void anUnmeasuredEntryCarriesItsQueriesAndNoRecall() {
        var e = entryOf(1, State.UNMEASURED, T0);

        assertThat(e.state()).isEqualTo("unmeasured");
        assertThat(e.reason()).isEqualTo("no_queries");
        assertThat(e.queries()).as("the ring's count of distinct queries, not zero").isEqualTo(2);
        assertThat(e.minRecall()).isNull();
        assertThat(e.meanRecall()).isNull();
        assertThat(e.top1Misses()).isZero();
        assertThat(e.trigger()).isEqualTo("daily");
    }

    @Test
    void anEntryIsKeyedByTheOpaqueIndexNameAndCarriesNothingElseThatNamesATenantOrACollection() {
        var e = entryOf(7, State.GREEN, T0);

        assertThat(e.index()).matches("^pci_[0-9a-f]{24}$");
        assertThat(e.toString()).doesNotContain("tenant-7").doesNotContain("collection-7").doesNotContain("chunks_leaf");
    }

    // -- the three anchor-derived fields -----------------------------------------------------------------------

    @Test
    void aBuiltIndexReportsItsOriginItsSizeRatioAndItsDeletesSinceBuild() {
        // Built with 1000 rows in 8,000,000 bytes (8000 per row). Now 8,800,000 bytes over 800 rows (11,000 per row):
        // ratio 1.375. 100 rows were written since the build: 1000 + 100 - 800 = 300 deleted, of the 800 now present.
        var e = PciMaintenanceStatus.entry(result(1, State.GREEN, T0),
            reading("built", 1000, 8_000_000, 8_800_000, 800, 100), Optional.empty());

        assertThat(e.anchor()).isEqualTo("built");
        assertThat(e.sizeRatio()).isCloseTo(1.375, org.assertj.core.data.Offset.offset(1e-9));
        assertThat(e.deletesSinceBuild()).isCloseTo(0.375, org.assertj.core.data.Offset.offset(1e-9));
    }

    @Test
    void deletesSinceBuildIsZeroWhenRowsOnlyGrew_notNegative() {
        var e = PciMaintenanceStatus.entry(result(1, State.GREEN, T0),
            reading("built", 1000, 8_000_000, 9_000_000, 1200, 200), Optional.empty());

        assertThat(e.deletesSinceBuild()).isZero();
    }

    @Test
    void anAdoptedIndexReportsItsOriginButNoSizeRatio_theInstallReferenceItWouldBeJudgedAgainstIsAPhase2Thing() {
        var e = PciMaintenanceStatus.entry(result(1, State.GREEN, T0),
            reading("adopted", 1000, 12_000_000, 12_000_000, 1000, 0), Optional.empty());

        assertThat(e.anchor()).isEqualTo("adopted");
        assertThat(e.sizeRatio()).isNull();
        assertThat(e.deletesSinceBuild()).isZero();
    }

    @Test
    void withNoReadingTheOriginComesFromTheSweepAndTheRatiosAreNull() {
        var anchor = new PciCatalog.Anchor(T0, 10, 10, "adopted");

        var e = PciMaintenanceStatus.entry(result(1, State.GREEN, T0), null, Optional.of(anchor.origin()));

        assertThat(e.anchor()).isEqualTo("adopted");
        assertThat(e.sizeRatio()).isNull();
        assertThat(e.deletesSinceBuild()).isNull();
        assertThat(entryOf(2, State.GREEN, T0).anchor()).as("no reading and no sweep anchor").isNull();
    }

    @Test
    void anOriginWordOtherThanBuiltOrAdoptedIsNotPublished() {
        var e = PciMaintenanceStatus.entry(result(1, State.GREEN, T0),
            reading("hand written by an operator", 1000, 8_000_000, 8_000_000, 1000, 0), Optional.empty());

        assertThat(e.anchor()).isNull();
    }

    @Test
    void zeroRowsGiveNoRatio_notADivisionByZero() {
        var empty = PciMaintenanceStatus.entry(result(1, State.GREEN, T0),
            reading("built", 0, 0, 100, 0, 0), Optional.empty());

        assertThat(empty.sizeRatio()).isNull();
        assertThat(empty.deletesSinceBuild()).isNull();
    }
}
