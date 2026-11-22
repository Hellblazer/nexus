// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.reembed.TargetSelection.Absent;
import dev.nexus.service.reembed.TargetSelection.CollectionRow;
import dev.nexus.service.reembed.TargetSelection.Decision;
import dev.nexus.service.reembed.TargetSelection.Live;
import dev.nexus.service.reembed.TargetSelection.LockHolder;
import dev.nexus.service.reembed.TargetSelection.Refuse;
import dev.nexus.service.reembed.TargetSelection.Register;
import dev.nexus.service.reembed.TargetSelection.Revive;
import dev.nexus.service.reembed.TargetSelection.Superseded;
import dev.nexus.service.reembed.TargetSelection.UseLive;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

class TargetSelectionTest {

    private static final String CT = "docs";
    private static final String OWNER = "1-7";
    private static final String MODEL = "bge-base-en-v15-768";
    private static final String SOURCE = "docs__1-7__voyage-context-3__v1";

    private static CollectionRow row(String name, String version) {
        return new CollectionRow(name, CT, OWNER, MODEL, version, false, "", "live");
    }

    private static CollectionRow superseded(String name, String version, String by) {
        return new CollectionRow(name, CT, OWNER, MODEL, version, false, by, "live");
    }

    private static TargetSelection.Result select(CollectionRow... rows) {
        return TargetSelection.select(List.of(rows), CT, OWNER, MODEL);
    }

    // ---- selection: which rows are candidates ----

    @Test
    void noRowsIsAbsent() {
        assertThat(select()).isEqualTo(new Absent());
    }

    @Test
    void aRowOfAnotherOwnerModelOrContentTypeIsNotACandidate() {
        var otherOwner = new CollectionRow("a", CT, "1-8", MODEL, "v1", false, "", "live");
        var otherModel = new CollectionRow("b", CT, OWNER, "voyage-context-3", "v1", false, "", "live");
        var otherType = new CollectionRow("c", "code", OWNER, MODEL, "v1", false, "", "live");
        assertThat(select(otherOwner, otherModel, otherType)).isEqualTo(new Absent());
    }

    @ParameterizedTest
    @ValueSource(strings = {"quarantine", "dormant", "disputed", ""})
    void aRowThatIsNotLiveIsNotACandidate(String lifecycle) {
        var r = new CollectionRow("a", CT, OWNER, MODEL, "v1", false, "", lifecycle);
        assertThat(select(r)).isEqualTo(new Absent());
    }

    /** RDR-229 P2.8 (engine 0.1.200): a grandfathered row is a candidate, as it is for collectionForTuple. */
    @Test
    void aLegacyGrandfatheredRowIsACandidate() {
        var r = new CollectionRow("a", CT, OWNER, MODEL, "v1", true, "", "live");
        assertThat(select(r)).isEqualTo(new Live("a"));
    }

    @Test
    void aGrandfatheredRowTiesByTheSameOrderAsAnyOther() {
        var g = new CollectionRow("a-grand", CT, OWNER, MODEL, "v1", true, "", "live");
        assertThat(select(row("b-conformant", "v1"), g)).as("equal version: the lexically lower name").isEqualTo(new Live("a-grand"));
        var g2 = new CollectionRow("z-grand", CT, OWNER, MODEL, "v2", true, "", "live");
        assertThat(select(row("a-conformant", "v1"), g2)).as("version dominates").isEqualTo(new Live("z-grand"));
    }

    @Test
    void aSupersededRowWithNoOtherCandidateIsSuperseded() {
        assertThat(select(superseded("t", "v1", SOURCE))).isEqualTo(new Superseded("t", SOURCE));
    }

    @Test
    void aSupersededRowThatIsNotLiveStaysOutAndAGrandfatheredOneDoesNot() {
        var q = new CollectionRow("q", CT, OWNER, MODEL, "v1", false, SOURCE, "quarantine");
        var g = new CollectionRow("g", CT, OWNER, MODEL, "v1", true, SOURCE, "live");
        assertThat(select(q)).as("a quarantine row stays out").isEqualTo(new Absent());
        assertThat(select(q, g)).as("a superseded grandfathered row is the Superseded candidate (RDR-229 P2.8)")
            .isEqualTo(new Superseded("g", SOURCE));
    }

    // ---- selection: which candidate wins ----

    @Test
    void aNonSupersededRowBeatsASupersededOneEvenAtALowerVersion() {
        assertThat(select(superseded("hi", "v9", SOURCE), row("lo", "v1"))).isEqualTo(new Live("lo"));
    }

    @Test
    void theHighestNumericVersionWinsNotTheLexicallyHighest() {
        // "v10" sorts below "v9" as text and above it as a number.
        assertThat(select(row("nine", "v9"), row("ten", "v10"), row("two", "v2"))).isEqualTo(new Live("ten"));
    }

    @Test
    void equalVersionsGoToTheLexicallyLowerName() {
        assertThat(select(row("b", "v2"), row("a", "v2"), row("c", "v2"))).isEqualTo(new Live("a"));
    }

    @Test
    void aHigherVersionBeatsALowerNameAtALowerVersion() {
        assertThat(select(row("a", "v1"), row("z", "v2"))).isEqualTo(new Live("z"));
    }

    @Test
    void anUnparseableVersionCountsAsZero() {
        assertThat(select(row("odd", "x"), row("blank", ""), row("v1row", "v1"))).isEqualTo(new Live("v1row"));
        assertThat(select(row("b", "x"), row("a", "v0"))).isEqualTo(new Live("a"));
        assertThat(TargetSelection.versionNumber(null)).isZero();
        assertThat(TargetSelection.versionNumber("v007")).isEqualTo(7);
        assertThat(TargetSelection.versionNumber("v99999999999999999999")).isEqualTo(Integer.MAX_VALUE);
    }

    @Test
    void amongSupersededRowsTheSameOrderApplies() {
        assertThat(select(superseded("old", "v1", SOURCE), superseded("newer", "v3", "other"),
            superseded("also3", "v3", SOURCE))).isEqualTo(new Superseded("also3", SOURCE));
    }

    @Test
    void theInputOrderDoesNotMatter() {
        var a = row("a", "v1");
        var b = row("b", "v3");
        var c = row("c", "v3");
        assertThat(select(a, b, c)).isEqualTo(select(c, b, a)).isEqualTo(new Live("b"));
    }

    @Test
    void aNullSupersededByMeansNotSuperseded() {
        var r = new CollectionRow("n", CT, OWNER, MODEL, "v1", false, null, "live");
        assertThat(select(r)).isEqualTo(new Live("n"));
    }

    @Test
    void aRowBuiltFromTheListRouteMapKeepsItsFields() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", "t");
        m.put("content_type", CT);
        m.put("owner_id", OWNER);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v4");
        m.put("legacy_grandfathered", Boolean.FALSE);
        m.put("superseded_by", "s");
        m.put("lifecycle_state", "live");
        m.put("display_name", "ignored");
        assertThat(CollectionRow.fromMap(m)).isEqualTo(superseded("t", "v4", "s"));
        m.put("legacy_grandfathered", Boolean.TRUE);
        assertThat(CollectionRow.fromMap(m).legacyGrandfathered()).isTrue();
    }

    // ---- the tie at equal version: the first registered, then the name (nexus-p2m1x.4) ----

    private static CollectionRow rowAt(String name, String createdAt) {
        return new CollectionRow(name, CT, OWNER, MODEL, "v1", false, "", "live", createdAt);
    }

    @Test
    void atEqualVersionTheFirstRegisteredWinsNotTheLowerName() {
        var older = rowAt("z-older", "2026-01-01 00:00:00+00");
        var newer = rowAt("a-newer", "2026-02-01 00:00:00+00");
        assertThat(select(older, newer)).isEqualTo(select(newer, older)).isEqualTo(new Live("z-older"));
    }

    @Test
    void stampsAreComparedAsInstantsNotAsText() {
        // "a" sorts lower as text, but 08:00-02 is 10:00 UTC, later than "b"'s 09:00 UTC, so "b" was
        // registered first and wins. The fraction and the half-hour offset are the other Postgres shapes.
        assertThat(select(rowAt("a", "2026-01-01 08:00:00-02"), rowAt("b", "2026-01-01 09:00:00+00")))
            .isEqualTo(new Live("b"));
        assertThat(select(rowAt("a", "2026-01-01 00:00:00.5+00"), rowAt("b", "2026-01-01 00:00:00+00")))
            .isEqualTo(new Live("b"));
        assertThat(select(rowAt("a", "2026-01-01 05:30:00+05:30"), rowAt("b", "2026-01-01 00:00:01+00")))
            .isEqualTo(new Live("a"));
    }

    @Test
    void equalOrBlankStampsFallToTheName() {
        assertThat(select(rowAt("b", "2026-01-01 00:00:00+00"), rowAt("a", "2026-01-01 00:00:00+00")))
            .isEqualTo(new Live("a"));
        assertThat(select(rowAt("b", ""), rowAt("a", ""))).isEqualTo(new Live("a"));
    }

    /**
     * A blank stamp, one that does not parse and Postgres's {@code infinity} sort AFTER a real stamp, so the
     * real row wins whatever the names; if {@code registeredAt} mapped them to {@code Instant.MIN} or
     * {@code EPOCH} the odd row ("a", the lower name) would win in either input order.
     */
    @ParameterizedTest
    @ValueSource(strings = {"", "garbage", "infinity"})
    void aBlankOrUnparseableStampSortsAfterARealOne(String odd) {
        var real = rowAt("z", "2026-01-01 00:00:00+00");
        var strange = rowAt("a", odd);
        assertThat(select(real, strange)).isEqualTo(select(strange, real)).isEqualTo(new Live("z"));
    }

    @Test
    void aHigherVersionStillBeatsAnOlderRow() {
        var newerV2 = new CollectionRow("n", CT, OWNER, MODEL, "v2", false, "", "live", "2026-05-01 00:00:00+00");
        assertThat(select(rowAt("o", "2026-01-01 00:00:00+00"), newerV2)).isEqualTo(new Live("n"));
    }

    @Test
    void theListRouteMapCarriesItsCreatedAt() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", "t");
        m.put("content_type", CT);
        m.put("owner_id", OWNER);
        m.put("embedding_model", MODEL);
        m.put("model_version", "v1");
        m.put("lifecycle_state", "live");
        m.put("created_at", "2026-01-01 00:00:00+00");
        assertThat(CollectionRow.fromMap(m).createdAt()).isEqualTo("2026-01-01 00:00:00+00");
    }

    // ---- decision ----

    @Test
    void absentIsRegistered() {
        assertThat(TargetSelection.decide(new Absent(), SOURCE, null)).isEqualTo(new Register());
    }

    @Test
    void aLiveTargetIsUsed() {
        assertThat(TargetSelection.decide(new Live("t"), SOURCE, null)).isEqualTo(new UseLive("t"));
    }

    @Test
    void aLiveTargetThatAnotherLockStateJobHasAsItsSourceIsRefusedNamingTheJob() {
        var holder = new LockHolder(UUID.randomUUID(), "running");
        Decision d = TargetSelection.decide(new Live("t"), SOURCE, holder);
        assertThat(d).isInstanceOf(Refuse.class);
        assertThat(((Refuse) d).reason()).contains(holder.jobId().toString()).contains("running").contains("t");
    }

    @Test
    void aSupersededTargetNamingTheSourceIsRevived() {
        Decision d = TargetSelection.decide(new Superseded("t", SOURCE), SOURCE, null);
        assertThat(d).isEqualTo(new Revive("t"));
    }

    @Test
    void aSupersededTargetNamingAnotherSuccessorIsRefusedNamingBoth() {
        Decision d = TargetSelection.decide(new Superseded("t", "docs__1-7__other__v1"), SOURCE, null);
        assertThat(d).isInstanceOf(Refuse.class);
        assertThat(((Refuse) d).reason()).contains("t").contains("docs__1-7__other__v1").contains(SOURCE);
    }

    @Test
    void aSupersededTargetNamingAnotherSuccessorIsRefusedEvenWithNoLockHolder() {
        // The successor mismatch is the more basic refusal; a holder does not turn it into a revive or change it.
        var holder = new LockHolder(UUID.randomUUID(), "failed");
        Decision d = TargetSelection.decide(new Superseded("t", "elsewhere"), SOURCE, holder);
        assertThat(((Refuse) d).reason()).contains("elsewhere");
    }

    @ParameterizedTest
    @CsvSource({"planned", "running", "paused", "cutover", "failed"})
    void aLockStateJobWithTheTargetAsItsSourceBlocksTheRevive(String holderState) {
        var holder = new LockHolder(UUID.randomUUID(), holderState);
        Decision d = TargetSelection.decide(new Superseded("t", SOURCE), SOURCE, holder);
        assertThat(d).isInstanceOf(Refuse.class);
        assertThat(((Refuse) d).reason()).contains(holder.jobId().toString()).contains(holderState).contains("t");
    }

    // ---- ruling 2 as narrowed: an earlier done or aborted job is marked revived; a failed one holds the lock ----

    @ParameterizedTest
    @ValueSource(strings = {"done", "aborted"})
    void theEarlierJobIsMarkedRevivedWhenDoneOrAborted(String earlierState) {
        assertThat(Revive.markEarlierJob(earlierState)).isTrue();
    }

    @ParameterizedTest
    @ValueSource(strings = {"failed", "planned", "running", "paused", "cutover", "done_with_skips", "", "bogus"})
    void anyOtherEarlierJobStateIsNotMarked(String earlierState) {
        // failed and the other lock states hold the lock, so the start refuses before marking; done_with_skips
        // never superseded its source.
        assertThat(Revive.markEarlierJob(earlierState)).isFalse();
    }

    @Test
    void theEarlierDoneJobRevivesButAFailedOneHoldsTheLockAndRefuses() {
        // done: kept_by_flag or kept_tombstoned left the target superseded by this source and holds no lock.
        assertThat(TargetSelection.decide(new Superseded("t", SOURCE), SOURCE, null)).isEqualTo(new Revive("t"));
        // aborted in cutover after the supersede: also no lock, same decision.
        assertThat(TargetSelection.decide(new Superseded("t", SOURCE), SOURCE, null)).isEqualTo(new Revive("t"));
        // failed (parity_mismatch) after the supersede: the failed job keeps the source's lock, so the revive
        // is refused naming it until it is aborted; once aborted the lock is gone and the revive proceeds.
        var failed = new LockHolder(UUID.randomUUID(), "failed");
        assertThat(TargetSelection.decide(new Superseded("t", SOURCE), SOURCE, failed)).isInstanceOf(Refuse.class);
        assertThat(TargetSelection.decide(new Superseded("t", SOURCE), SOURCE, null)).isEqualTo(new Revive("t"));
    }

    // ---- nexus-p2m1x.28: two sources on one tuple must not meet in one target ----

    private static CollectionRow sourceRow(String name, String version) {
        return new CollectionRow(name, CT, OWNER, "voyage-context-3", version, false, "", "live");
    }

    @Test
    void aSourceAloneOnItsTupleHasNoMates() {
        assertThat(TargetSelection.tupleMates(List.of(sourceRow(SOURCE, "v1")), SOURCE, CT, OWNER,
            "voyage-context-3")).isEmpty();
    }

    @Test
    void anotherLiveRowOnTheSourcesTupleIsAMate_butASupersededQuarantinedOrForeignRowIsNot() {
        var mate = sourceRow("docs__wow-addon-dev__voyage-context-3__v1", "");
        var superseded = new CollectionRow("docs__old__voyage-context-3__v1", CT, OWNER, "voyage-context-3", "v1",
            false, SOURCE, "live");
        var quarantined = new CollectionRow("docs__q__voyage-context-3__v1", CT, OWNER, "voyage-context-3", "v1",
            false, "", "quarantine");
        var otherOwner = new CollectionRow("docs__o__voyage-context-3__v1", CT, "1-8", "voyage-context-3", "v1",
            false, "", "live");
        var otherModel = new CollectionRow("docs__m__bge__v1", CT, OWNER, MODEL, "v1", false, "", "live");
        // The one held predicate (not superseded, not quarantine): a dormant holder is a mate too.
        var dormant = new CollectionRow("docs__d__voyage-context-3__v1", CT, OWNER, "voyage-context-3", "v1",
            false, "", "dormant");
        assertThat(TargetSelection.tupleMates(
            List.of(sourceRow(SOURCE, "v1"), mate, superseded, quarantined, otherOwner, otherModel, dormant), SOURCE,
            CT, OWNER, "voyage-context-3")).containsExactly(dormant.name(), mate.name());
    }

    private static final String VC3 = "voyage-context-3";
    private static final String TARGET = "docs__1-7__bge-base-en-v15-768__v1";

    /** A job of the log: {@code source} moved {@code moved} documents into {@link #TARGET}. */
    private static Job job(String source, long moved) {
        return new Job(UUID.randomUUID(), source, TARGET, "done", "deleted", "", 1, null, moved, 0, 0, 0, 0, null,
            false, false, null, null, null, null, null);
    }

    private static CollectionRow rowOf(String name, String owner, String model) {
        return new CollectionRow(name, CT, owner, model, "v1", false, "", "live");
    }

    private static Job foreign(List<Job> jobs, List<CollectionRow> rows) {
        return TargetSelection.foreignOrigin(jobs, rows, TARGET, SOURCE, VC3);
    }

    @Test
    void aForeignJobWhoseSourceHasTheSameTupleIsTheOrigin_evenWhenItsRowIsSuperseded() {
        Job moved = job("docs__wow-addon-dev__voyage-context-3__v1", 3);
        var rows = List.of(new CollectionRow(moved.sourceCollection(), CT, OWNER, VC3, "v1", false, TARGET, "live"));
        assertThat(foreign(List.of(moved), rows)).isSameAs(moved);
    }

    @Test
    void aSiblingOnAnotherModelIsNotATwin_whetherItsRowIsThereOrGone() {
        Job moved = job("docs__1-7__bge-base-en-v15-768__v1", 3);
        assertThat(foreign(List.of(moved), List.of(rowOf(moved.sourceCollection(), OWNER, "bge-base-en-v15-768"))))
            .as("row present").isNull();
        assertThat(foreign(List.of(moved), List.of())).as("row gone: the conformant name's model token").isNull();
    }

    @Test
    void aTwinWhoseRowIsGoneIsReadByItsNamesModel_andAnUnreadableNameCounts() {
        assertThat(foreign(List.of(job("docs__wow-addon-dev__voyage-context-3__v1", 3)), List.of())).isNotNull();
        assertThat(foreign(List.of(job("docs__legacy", 3)), List.of())).as("model unknown: refuse, never merge")
            .isNotNull();
    }

    @Test
    void aForeignSourceReOwnedSinceItsJobStillCounts_itsDocumentsAreInTheTarget() {
        Job moved = job("docs__wow-addon-dev__voyage-context-3__v1", 3);
        assertThat(foreign(List.of(moved), List.of(rowOf(moved.sourceCollection(), "1-9", VC3)))).isSameAs(moved);
        assertThat(foreign(List.of(moved), List.of(new CollectionRow(moved.sourceCollection(), "code", "1-9", VC3,
            "v1", false, "", "live")))).as("nor does its content type matter: only the model").isSameAs(moved);
    }

    @Test
    void aJobThatMovedNoDocumentIsNotAnOrigin_andNeitherIsOneIntoAnotherTargetOrFromThisSource() {
        assertThat(foreign(List.of(job("docs__wow-addon-dev__voyage-context-3__v1", 0)), List.of())).isNull();
        var other = new Job(UUID.randomUUID(), "docs__wow-addon-dev__voyage-context-3__v1", "docs__elsewhere__v1",
            "done", "deleted", "", 1, null, 3, 0, 0, 0, 0, null, false, false, null, null, null, null, null);
        assertThat(foreign(List.of(other), List.of())).isNull();
        assertThat(foreign(List.of(job(SOURCE, 3)), List.of())).isNull();
    }

    @Test
    void aV2OfAMovedSourceIsRefused_aDeliberateConservativeRefusal() {
        Job moved = job("docs__1-7__voyage-context-3__v1", 3);
        // The v1 row is gone and its name carries this source's model; the log is keyed by name, so v2 is another source.
        assertThat(TargetSelection.foreignOrigin(List.of(moved), List.of(), TARGET,
            "docs__1-7__voyage-context-3__v2", VC3)).isSameAs(moved);
    }

    @Test
    void theRefusalsNameBothCollections_sayWhy_andGiveTheOwnerIsSourceExit() {
        String shared = TargetSelection.sharedSourceTupleReason(SOURCE, List.of("docs__wow-addon-dev__x__v1"), CT,
            OWNER, VC3);
        assertThat(shared).contains(SOURCE).contains("docs__wow-addon-dev__x__v1").contains(OWNER)
            .contains("same owner").contains("client cannot change a collection's owner in place")
            .contains("engine data move").contains("rename that re-homes the collection")
            .contains("owner-is-source").contains("re-owned or superseded")
            .contains("rdr229_reown_skipped").contains(TargetSelection.REOWN_SKIPPED_QUERY);
        Job origin = job("docs__other__x__v1", 3);
        String foreign = TargetSelection.foreignOriginReason(SOURCE, TARGET, origin);
        assertThat(foreign).contains(SOURCE).contains(TARGET).contains("docs__other__x__v1")
            .contains("the job log shows").contains(origin.jobId().toString()).contains("3 document(s) moved")
            .contains("engine data move").contains("rename that re-homes the collection")
            .contains(SOURCE + " is re-owned, which gives it a different target tuple")
            .contains("re-owning docs__other__x__v1 does not take its documents out of the target")
            .contains("earlier version of " + SOURCE).doesNotContain("holds documents")
            .doesNotContain("superseded");
    }
}
