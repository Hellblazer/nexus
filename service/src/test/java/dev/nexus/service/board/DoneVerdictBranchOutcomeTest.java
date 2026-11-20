// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The carry-forward of a path-filtered workflow's last result: a --done row's {@code dims.branch_outcome}, when
 * present and not blank, is the verdict; the per-sha {@code outcome} and the body's runs are the fallbacks. A Service
 * CI red that a later sha never re-ran therefore keeps an open episode open.
 */
class DoneVerdictBranchOutcomeTest {

    private static final String ONE_GREEN_RUN = "{\"sha\":\"Y\",\"n\":1,\"runs\":[[100,1,\"CI\",\"success\"]]}";

    private static Optional<DoneVerdict.Verdict> verdictOf(String body, String... dims) {
        Map<String, String> d = new LinkedHashMap<>(Map.of("from", "github", "kind", "sha", "sha", "Y"));
        for (int i = 0; i < dims.length; i += 2) {
            d.put(dims[i], dims[i + 1]);
        }
        return DoneVerdict.of(new BoardRow("d1", OwnerFixture.T0, d, body, null));
    }

    private static String outcomeOf(String body, String... dims) {
        return verdictOf(body, dims).map(DoneVerdict.Verdict::outcome).orElse(null);
    }

    @Test
    void aBranchOutcomeOfFailureWinsOverAPerShaSuccess() {
        assertThat(outcomeOf(ONE_GREEN_RUN, "outcome", "success", "branch_outcome", "failure")).isEqualTo("failure");
    }

    @Test
    void aBranchOutcomeOfSuccessWinsOverAPerShaFailure() {
        assertThat(outcomeOf(ONE_GREEN_RUN, "outcome", "failure", "branch_outcome", "success")).isEqualTo("success");
    }

    @Test
    void aBranchOutcomeOfCancelledIsNotAVerdict() {
        Optional<DoneVerdict.Verdict> v = verdictOf(ONE_GREEN_RUN, "outcome", "success", "branch_outcome", "cancelled");
        assertThat(v).get().extracting(DoneVerdict.Verdict::outcome).isEqualTo("cancelled");
        assertThat(v.get().counts()).isFalse();
    }

    @Test
    void aBlankBranchOutcomeFallsBackToOutcomeThenToTheBody() {
        assertThat(outcomeOf(ONE_GREEN_RUN, "outcome", "failure", "branch_outcome", "  ")).isEqualTo("failure");
        assertThat(outcomeOf(ONE_GREEN_RUN, "branch_outcome", "")).as("no outcome either: the body").isEqualTo("success");
    }

    @Test
    void noBranchOutcomeFallsBackToOutcomeAndToTheBodyAsBefore() {
        assertThat(outcomeOf(ONE_GREEN_RUN, "outcome", "failure")).isEqualTo("failure");
        assertThat(outcomeOf(ONE_GREEN_RUN)).isEqualTo("success");
        assertThat(outcomeOf("{\"sha\":\"Y\",\"n\":1,\"runs\":[[101,1,\"CI\",\"failure\"]]}")).isEqualTo("failure");
        assertThat(outcomeOf("{\"sha\":\"Y\"}")).as("nothing tells it").isNull();
    }

    @Test
    void aBranchOutcomeStillNeedsAShaAndAShaKind() {
        assertThat(DoneVerdict.of(new BoardRow("d1", OwnerFixture.T0,
            new LinkedHashMap<>(Map.of("from", "github", "kind", "run", "sha", "Y", "branch_outcome", "failure")),
            "{}", null))).as("not kind=sha").isEmpty();
        assertThat(DoneVerdict.of(new BoardRow("d1", OwnerFixture.T0,
            new LinkedHashMap<>(Map.of("from", "github", "kind", "sha", "branch_outcome", "failure")),
            "{}", null))).as("no sha anywhere").isEmpty();
    }

    // ── a failed carry-forward read: "carried": null ─────────────────────────

    private static final String CARRIED_NULL = "{\"sha\":\"Y\",\"n\":1,\"carried\":null,\"runs\":[[100,1,\"CI\",\"success\"]]}";
    private static final String CARRIED_LIST =
        "{\"sha\":\"Y\",\"n\":1,\"carried\":[[\"Service CI\",7,\"success\",\"X\"]],\"runs\":[[100,1,\"CI\",\"success\"]]}";

    @Test
    void aFailedCarryReadWithAPerShaSuccessIsNoVerdict() {
        assertThat(verdictOf(CARRIED_NULL, "outcome", "success")).as("outcome dim").isEmpty();
        assertThat(verdictOf(CARRIED_NULL)).as("outcome derived from the runs").isEmpty();
    }

    @Test
    void aFailedCarryReadWithAPerShaFailureOrCancelledIsStillAVerdict() {
        assertThat(outcomeOf(CARRIED_NULL, "outcome", "failure")).isEqualTo("failure");
        assertThat(outcomeOf(CARRIED_NULL, "outcome", "cancelled")).isEqualTo("cancelled");
        assertThat(outcomeOf("{\"sha\":\"Y\",\"n\":1,\"carried\":null,\"runs\":[[101,1,\"CI\",\"failure\"]]}"))
            .as("derived red").isEqualTo("failure");
    }

    @Test
    void anAbsentCarriedKeyOrAListFallsBackAsToday() {
        assertThat(outcomeOf(ONE_GREEN_RUN, "outcome", "success")).isEqualTo("success");
        assertThat(outcomeOf(CARRIED_LIST, "outcome", "success")).isEqualTo("success");
        assertThat(outcomeOf("{\"sha\":\"Y\",\"carried\":[]}", "outcome", "success")).isEqualTo("success");
    }

    @Test
    void aBranchOutcomeWinsOverACarriedNull() {
        assertThat(outcomeOf(CARRIED_NULL, "outcome", "success", "branch_outcome", "success")).isEqualTo("success");
        assertThat(outcomeOf(CARRIED_NULL, "outcome", "success", "branch_outcome", "failure")).isEqualTo("failure");
        assertThat(outcomeOf(CARRIED_NULL, "outcome", "failure", "branch_outcome", "success")).isEqualTo("success");
    }

    // ── what the stamper's decision does with it ─────────────────────────────

    private static OwnerFixture base() {
        return new OwnerFixture().seat("A").seat("C").push("P1", "G0", "A").push("P2", "P1", "C");
    }

    @Test
    void aServiceCiRedCarriedOntoALaterGreenShaKeepsTheEpisodeOpen() {
        OwnerFixture f = base();
        f.doneBranch("P1", "failure", "failure");   // Service CI red on P1
        f.recordNow(f.decide());
        f.doneBranch("P2", "success", "failure");   // P2: CI green, Service CI absent, its red carried forward

        OwnerDecision.Decision d = f.decide();

        assertThat(d.post()).as("an owner row refreshed, never a clear").isNotNull();
        assertThat(d.post().kind()).isEqualTo("owner");
        assertThat(d.post().sha()).isEqualTo("P2");
    }

    @Test
    void aBranchOutcomeOfSuccessClearsTheOpenEpisode() {
        OwnerFixture f = base();
        f.doneBranch("P1", "failure", "failure");
        f.recordNow(f.decide());
        f.doneBranch("P2", "success", "failure");
        f.recordNow(f.decide());
        f.doneBranch("P2", "success", "success");   // Service CI later green: every workflow's latest is green

        OwnerDecision.Post clear = f.decide().post();

        assertThat(clear).isNotNull();
        assertThat(clear.kind()).isEqualTo("clear");
        assertThat(clear.sha()).isEqualTo("P2");
    }

    @Test
    void aGreenShaWithNoBranchOutcomeClearsAsBefore() {
        OwnerFixture f = base();
        f.doneBranch("P1", "failure", null);
        f.recordNow(f.decide());
        f.doneBranch("P2", "success", null);

        OwnerDecision.Post clear = f.decide().post();

        assertThat(clear).isNotNull();
        assertThat(clear.kind()).isEqualTo("clear");
    }

    @Test
    void aFailedCarryReadDoesNotClearAnOpenEpisodeAndRaisesNothing() {
        OwnerFixture f = base();
        f.doneBranch("P1", "failure", "failure");
        f.recordNow(f.decide());
        f.doneCarriedNull("P2", "success");   // the carry-forward read failed: no branch_outcome, carried null

        assertThat(f.decide().post()).as("no verdict for P2: P1's red stands, nothing posted").isNull();
        f.doneCarriedNull("P2", "failure");   // a red is red whatever was carried
        OwnerDecision.Post post = f.decide().post();
        assertThat(post).isNotNull();
        assertThat(post.kind()).isEqualTo("owner");
        assertThat(post.sha()).isEqualTo("P2");
    }
}
