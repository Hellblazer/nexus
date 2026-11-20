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

    private static BoardRow row(String body, String... dims) {
        Map<String, String> d = new LinkedHashMap<>(Map.of("from", "github", "kind", "sha", "sha", "Y"));
        for (int i = 0; i < dims.length; i += 2) {
            d.put(dims[i], dims[i + 1]);
        }
        return new BoardRow("d1", OwnerFixture.T0, d, body, null);
    }

    private static String outcomeOf(String body, String... dims) {
        return verdictOf(body, dims).map(DoneVerdict.Verdict::outcome).orElse(null);
    }

    @Test
    void aBranchOutcomeOfFailureWinsOverAPerShaSuccess() {
        assertThat(outcomeOf(ONE_GREEN_RUN, "outcome", "success", "branch_outcome", "failure")).isEqualTo("failure");
    }

    @Test
    void aPerShaFailureIsAFailureWhateverTheBranchOutcomeSays() {
        for (String branch : new String[] {"success", "cancelled", "failure", "bogus", "neutral"}) {
            assertThat(outcomeOf(ONE_GREEN_RUN, "outcome", "failure", "branch_outcome", branch))
                .as("branch_outcome " + branch).isEqualTo("failure");
        }
        assertThat(outcomeOf("{\"sha\":\"Y\",\"n\":1,\"runs\":[[101,1,\"CI\",\"failure\"]]}", "branch_outcome", "success"))
            .as("the per-sha failure derived from the runs").isEqualTo("failure");
    }

    @Test
    void anUnrecognisedBranchOutcomeIsNoVerdict() {
        assertThat(verdictOf(ONE_GREEN_RUN, "outcome", "success", "branch_outcome", "bogus")).isEmpty();
        assertThat(verdictOf(ONE_GREEN_RUN, "branch_outcome", "SUCCESS")).as("case matters").isEmpty();
        assertThat(DoneVerdict.resolve(row(ONE_GREEN_RUN, "outcome", "success", "branch_outcome", "bogus")).reason())
            .isEqualTo(DoneVerdict.REASON_UNKNOWN);
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
        assertThat(outcomeOf(CARRIED_NULL, "outcome", "failure", "branch_outcome", "success"))
            .as("a per-sha failure is not overridden").isEqualTo("failure");
    }

    // ── a carried list the engine can check itself (branch_outcome absent) ──

    private static String carried(String list, String extra) {
        return "{\"sha\":\"Y\",\"n\":1,\"carried\":" + list + extra + ",\"runs\":[[100,1,\"CI\",\"success\"]]}";
    }

    private static void assertNoVerdict(String body, String reason, String why) {
        assertThat(verdictOf(body, "outcome", "success")).as(why).isEmpty();
        assertThat(DoneVerdict.resolve(row(body, "outcome", "success")).reason()).as(why + " (reason)").isEqualTo(reason);
    }

    @Test
    void aCarriedEntryThatIsPresentButNotPassingLeavesAPerShaSuccessNoVerdictForCarriedNotGreen() {
        for (String conclusion : new String[] {"\"failure\"", "\"cancelled\"", "\"timed_out\"", "\"stale\"", "\"bogus\""}) {
            assertNoVerdict(carried("[[\"Service CI\",7," + conclusion + ",\"X\"]]", ""),
                DoneVerdict.REASON_CARRIED_NOT_GREEN, conclusion);
        }
        assertNoVerdict(carried("[[\"A\",1,\"success\",\"X\"],[\"B\",2,\"failure\",\"X\"]]", ""),
            DoneVerdict.REASON_CARRIED_NOT_GREEN, "one bad entry among good ones");
    }

    @Test
    void aCarriedSkippedOrNeutralEntryIsNotPassingButThePerShaRunsKeepTheirSet() {
        // a skipped latest Service CI run must not clear an earlier red: a carried entry passes only on success
        assertNoVerdict(carried("[[\"Service CI\",7,\"skipped\",\"X\"]]", ""), DoneVerdict.REASON_CARRIED_NOT_GREEN, "carried skipped");
        assertNoVerdict(carried("[[\"Service CI\",7,\"neutral\",\"X\"]]", ""), DoneVerdict.REASON_CARRIED_NOT_GREEN, "carried neutral");
        String ownRuns = "{\"sha\":\"Y\",\"n\":2,\"runs\":[[100,1,\"CI\",\"skipped\"],[101,1,\"CI\",\"neutral\"]]}";
        assertThat(outcomeOf(ownRuns)).as("the sha's own skipped and neutral runs still read success").isEqualTo("success");
        assertThat(outcomeOf(ownRuns, "outcome", "success")).isEqualTo("success");
    }

    @Test
    void anUnreadableCarriedEntryLeavesAPerShaSuccessNoVerdictForCarryFailed() {
        assertNoVerdict(carried("[[\"Service CI\",7,null,\"X\"]]", ""), DoneVerdict.REASON_CARRY_FAILED, "null conclusion");
        assertNoVerdict(carried("[[\"Service CI\",7]]", ""), DoneVerdict.REASON_CARRY_FAILED, "a short entry");
        assertNoVerdict(carried("\"x\"", ""), DoneVerdict.REASON_CARRY_FAILED, "a carried that is not a list");
        assertNoVerdict(carried("[[\"Service CI\",7,null,\"X\"],[\"B\",2,\"failure\",\"X\"]]", ""),
            DoneVerdict.REASON_CARRIED_NOT_GREEN, "a visible red outranks an unreadable entry");
    }

    @Test
    void aCarriedListOfSuccessEntriesLeavesAPerShaSuccess() {
        String body = carried("[[\"A\",1,\"success\",\"X\"],[\"B\",2,\"success\",\"X\"],[\"C\",3,\"success\",\"X\"]]", ",\"carried_n\":3");
        assertThat(outcomeOf(body, "outcome", "success")).isEqualTo("success");
    }

    @Test
    void aTrimmedCarriedListLeavesAPerShaSuccessNoVerdict() {
        String one = "[[\"Service CI\",7,\"success\",\"X\"]]";
        assertThat(verdictOf(carried(one, ",\"carried_n\":2"), "outcome", "success")).as("n 2, one listed").isEmpty();
        assertThat(outcomeOf(carried(one, ",\"carried_n\":1"), "outcome", "success")).as("n equals the list").isEqualTo("success");
        assertThat(verdictOf(carried("[]", ",\"carried_n\":1"), "outcome", "success")).as("everything trimmed").isEmpty();
        assertNoVerdict(carried(one, ",\"carried_n\":2"), DoneVerdict.REASON_CARRY_FAILED, "trimmed");
    }

    @Test
    void aCarriedNWithNoCarriedKeyCountsAnAbsentListAsEmpty() {
        String runs = "\"runs\":[[100,1,\"CI\",\"success\"]]";
        assertNoVerdict("{\"sha\":\"Y\",\"n\":1," + runs + ",\"carried_n\":1}", DoneVerdict.REASON_CARRY_FAILED, "carried_n 1, no list");
        assertThat(outcomeOf("{\"sha\":\"Y\",\"n\":1," + runs + ",\"carried_n\":0}", "outcome", "success"))
            .as("carried_n 0 and no list: nothing to carry").isEqualTo("success");
        assertThat(outcomeOf("{\"sha\":\"Y\",\"n\":1," + runs + "}", "outcome", "success"))
            .as("neither key: as before").isEqualTo("success");
    }

    @Test
    void aPerShaFailureOrCancelledIsKeptOverABadCarriedList() {
        String bad = carried("[[\"Service CI\",7,\"failure\",\"X\"]]", ",\"carried_n\":5");
        assertThat(outcomeOf(bad, "outcome", "failure")).isEqualTo("failure");
        assertThat(outcomeOf(bad, "outcome", "cancelled")).isEqualTo("cancelled");
    }

    @Test
    void aBranchOutcomeIsNotSecondGuessedByTheCarriedList() {
        String bad = carried("[[\"Service CI\",7,\"failure\",\"X\"]]", ",\"carried_n\":5");
        assertThat(outcomeOf(bad, "outcome", "success", "branch_outcome", "success")).isEqualTo("success");
    }

    @Test
    void aRowOnTheLastLadderRungWithABadCarriedListIsJudgedFromItsBody() {
        // base dims only: no sha, outcome or branch_outcome dim; the sha and the verdict come from the body
        Map<String, String> base = new LinkedHashMap<>(Map.of("from", "github", "kind", "sha"));
        String red = "[[\"Service CI\",7,\"failure\",\"X\"]]";
        String greenRuns = "[[100,1,\"CI\",\"success\"]]";
        String redRuns = "[[100,1,\"CI\",\"failure\"]]";
        assertThat(DoneVerdict.of(new BoardRow("d1", OwnerFixture.T0, base,
            "{\"sha\":\"Y\",\"n\":1,\"carried\":" + red + ",\"runs\":" + greenRuns + "}", null)))
            .as("per-sha success over a carried red").isEmpty();
        assertThat(DoneVerdict.of(new BoardRow("d1", OwnerFixture.T0, base,
            "{\"sha\":\"Y\",\"n\":1,\"carried\":" + red + ",\"runs\":" + redRuns + "}", null)))
            .get().extracting(DoneVerdict.Verdict::sha, DoneVerdict.Verdict::outcome).containsExactly("Y", "failure");
        assertThat(DoneVerdict.of(new BoardRow("d1", OwnerFixture.T0, base,
            "{\"sha\":\"Y\",\"n\":1,\"runs\":" + greenRuns + "}", null)))
            .as("no carried key: as today").get().extracting(DoneVerdict.Verdict::outcome).isEqualTo("success");
    }

    @Test
    void theReasonNamesWhyThereIsNoCountingVerdict() {
        assertThat(DoneVerdict.resolve(row(CARRIED_NULL, "outcome", "success")).reason()).isEqualTo(DoneVerdict.REASON_CARRY_FAILED);
        assertThat(DoneVerdict.resolve(row(ONE_GREEN_RUN, "outcome", "cancelled")).reason()).isEqualTo(DoneVerdict.REASON_CANCELLED);
        assertThat(DoneVerdict.resolve(row("{\"sha\":\"Y\"}")).reason()).isEqualTo(DoneVerdict.REASON_UNKNOWN);
        assertThat(DoneVerdict.resolve(row(ONE_GREEN_RUN, "outcome", "success")).reason()).as("a verdict has none").isNull();
        assertThat(DoneVerdict.resolve(row(ONE_GREEN_RUN, "outcome", "failure")).reason()).isNull();
        // the strings an operator greps for
        assertThat(java.util.List.of(DoneVerdict.REASON_CARRY_FAILED, DoneVerdict.REASON_CARRIED_NOT_GREEN,
            DoneVerdict.REASON_CANCELLED, DoneVerdict.REASON_UNKNOWN))
            .containsExactly("carry_failed", "carried_not_green", "cancelled", "unknown");
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
