// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.vectors.PciCanary.Result;
import dev.nexus.service.vectors.PciCanary.State;
import dev.nexus.service.vectors.PciCatalog.Index;
import dev.nexus.service.vectors.PciCatalog.Leaf;
import dev.nexus.service.vectors.PciReconcilePlanner.Trigger;
import dev.nexus.service.vectors.PciReconciler.IndexReading;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-230 Phase 2: what the REINDEX half remembers between passes (nexus-4lnn1.1.10), against the state alone: no
 * database, no clock but the one the test passes.
 */
class PciReindexStateTest {

    static final Instant NOW = Instant.parse("2026-11-14T00:00:00Z");
    static final String NAME = "pci_0123456789abcdef01234567";
    static final PciCatalog.Anchor ANCHOR = new PciCatalog.Anchor(NOW.minus(Duration.ofDays(2)), 1000, 8_000_000,
        PciCatalog.ORIGIN_BUILT);

    static final Index INDEX = new Index(NAME, true, "c");
    static final Leaf LEAF = new Leaf("nexus", "chunks_x", "m", "t", List.of(INDEX));

    static PciReindexState.Look look(long rows) {
        var reading = new IndexReading(ANCHOR, 8_000_000, rows, 0, NOW);
        return new PciReindexState.Look(LEAF, 1024, Map.of(NAME, reading));
    }

    static List<PciReindexPolicy.Candidate> pass(PciReindexState state, long rows, Instant now,
                                                 Function<String, Result> canary) {
        return state.candidates(List.of(look(rows)), Set.of(), Set.of(NAME), canary, now, new HashMap<>());
    }

    static Result result(State s, int queries) {
        return new Result(new PciCanary.Target("nexus.chunks_x", "t", "m", "c", NAME), "daily", s, null, queries,
            0.9, 0.95, 1, NOW, null, null);
    }

    @Test
    void aRebuildBySomeoneElse_startsTheBaselineOver_andEndsAnOwedBulkDelete() {
        var state = new PciReindexState();
        pass(state, 1000, NOW, n -> null);
        state.failed(NAME, NOW);
        assertThat(pass(state, 880, NOW.plusSeconds(60), n -> null).get(0).bulkPending()).isTrue();

        state.rebaseline(NAME, 880);   // a hand REINDEX was found: the fall is moot, 880 is the baseline

        var later = pass(state, 880, NOW.plus(Duration.ofMinutes(11)), n -> null).get(0);
        assertThat(later.backedOff()).isFalse();
        assertThat(later.bulkPending()).as("no longer owed").isFalse();
        assertThat(later.previousRows()).hasValue(880);
        assertThat(PciReindexPolicy.trigger(later, NOW.plus(Duration.ofMinutes(11)))).isEmpty();
    }

    @Test
    void aBulkFallSeenWhileTheIndexIsBackedOff_staysOwedUntilItsReindexRuns() {
        var state = new PciReindexState();
        assertThat(pass(state, 1000, NOW, n -> null)).singleElement()
            .satisfies(c -> assertThat(c.previousRows()).isEmpty());
        state.failed(NAME, NOW);   // a failed REINDEX: backed off for ten minutes

        // 12% fewer rows while backed off: the candidate is backed off, and the fall is owed.
        var backedOff = pass(state, 880, NOW.plusSeconds(60), n -> null).get(0);
        assertThat(backedOff.backedOff()).isTrue();
        assertThat(backedOff.bulkPending()).as("the fall is not lost with the overwritten previous count").isTrue();
        assertThat(PciReindexPolicy.trigger(backedOff, NOW)).as("backed off: nothing yet").isEmpty();

        // Ten minutes on, no further fall: the previous count is now 880, yet the REINDEX is still owed.
        var later = pass(state, 880, NOW.plus(Duration.ofMinutes(11)), n -> null).get(0);
        assertThat(later.backedOff()).isFalse();
        assertThat(later.bulkPending()).isTrue();
        assertThat(PciReindexPolicy.trigger(later, NOW.plus(Duration.ofMinutes(11)))).contains(Trigger.BULK_DELETE);

        state.finished(NAME, Trigger.BULK_DELETE, NOW.plus(Duration.ofMinutes(12)), false);
        assertThat(pass(state, 880, NOW.plus(Duration.ofMinutes(13)), n -> null).get(0).bulkPending()).isFalse();
    }

    @Test
    void anIndexOwedItsAnchor_isNoCandidate_untilTheAnchorIsWritten() {
        var state = new PciReindexState();
        pass(state, 1000, NOW, n -> null);
        state.anchorOwed(NAME, LEAF, 1024, "c");

        assertThat(pass(state, 500, NOW.plusSeconds(60), n -> null))
            .as("the old anchor would fire the deletes rule on every pass").isEmpty();
        assertThat(state.owedAnchors()).containsOnlyKeys(NAME);
        assertThat(state.owedAnchors().get(NAME).collection()).isEqualTo("c");

        state.anchorWritten(NAME);
        assertThat(pass(state, 500, NOW.plusSeconds(120), n -> null)).hasSize(1);
        assertThat(state.owedAnchors()).isEmpty();
    }

    @Test
    void anOwedAnchorOfAnIndexThatLeftTheSnapshotIsForgotten_butASkippedLeafKeepsItsHistory() {
        var state = new PciReindexState();
        pass(state, 1000, NOW, n -> null);
        state.anchorOwed(NAME, LEAF, 1024, "c");
        state.finished(NAME, Trigger.RED_CANARY, NOW, true);

        // A pass that skipped the leaf (no look) still lists the index in the snapshot: the history stands.
        state.candidates(List.of(), Set.of(), Set.of(NAME), n -> null, NOW.plusSeconds(60), new HashMap<>());
        assertThat(state.owedAnchors()).containsOnlyKeys(NAME);
        assertThat(state.previousRows(NAME)).hasValue(1000);

        // A snapshot without the index: it was dropped or renamed.
        state.candidates(List.of(), Set.of(), Set.of(), n -> null, NOW.plusSeconds(120), new HashMap<>());
        assertThat(state.owedAnchors()).isEmpty();
        assertThat(state.previousRows(NAME)).isEmpty();
    }

    @Test
    void aReindexThatStartedWithARedCanary_stampsTheRedLimit_whateverTheTriggersLabel() {
        var state = new PciReindexState();
        pass(state, 1000, NOW, n -> null);

        state.finished(NAME, Trigger.BULK_DELETE, NOW, true);

        var c = pass(state, 1000, NOW.plusSeconds(60), n -> null).get(0);
        assertThat(c.lastRedReindexAt()).as("a bulk delete that fired with a red canary").isEqualTo(NOW);
        // A REINDEX with no red canary behind it stamps nothing.
        var other = new PciReindexState();
        pass(other, 1000, NOW, n -> null);
        other.finished(NAME, Trigger.SIZE, NOW, false);
        assertThat(pass(other, 1000, NOW.plusSeconds(60), n -> null).get(0).lastRedReindexAt()).isNull();
    }

    @Test
    void redAfterReindex_clearsOnALaterGreenRun_andExpiresAfterADay_andANewReindexResetsIt() {
        var state = new PciReindexState();
        pass(state, 1000, NOW, n -> null);
        state.finished(NAME, Trigger.RED_CANARY, NOW, true);
        state.stillRed(NAME);
        assertThat(state.redAfterReindex(NAME, NOW.plusSeconds(60))).isTrue();

        // Still red on a later run: it stands.
        pass(state, 1000, NOW.plus(Duration.ofHours(1)), n -> result(State.RED, 10));
        assertThat(state.redAfterReindex(NAME, NOW.plus(Duration.ofHours(1)))).isTrue();

        // A day: expired, whatever the canary says.
        assertThat(state.redAfterReindex(NAME, NOW.plus(Duration.ofHours(24)))).isFalse();

        // A later green run clears it inside the day.
        pass(state, 1000, NOW.plus(Duration.ofHours(2)), n -> result(State.GREEN, 10));
        assertThat(state.redAfterReindex(NAME, NOW.plus(Duration.ofHours(2)))).isFalse();

        // A new REINDEX is a new measurement: the flag starts clear until its own re-measure says otherwise.
        state.stillRed(NAME);
        state.finished(NAME, Trigger.RED_CANARY, NOW.plus(Duration.ofHours(30)), true);
        assertThat(state.redAfterReindex(NAME, NOW.plus(Duration.ofHours(30)))).isFalse();
    }
}
