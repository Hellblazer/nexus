// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import static org.assertj.core.api.Assertions.assertThat;

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

    @Test
    void aLegacyGrandfatheredRowIsNotACandidate() {
        var r = new CollectionRow("a", CT, OWNER, MODEL, "v1", true, "", "live");
        assertThat(select(r)).isEqualTo(new Absent());
    }

    @Test
    void aSupersededRowWithNoOtherCandidateIsSuperseded() {
        assertThat(select(superseded("t", "v1", SOURCE))).isEqualTo(new Superseded("t", SOURCE));
    }

    @Test
    void aSupersededRowThatIsNotLiveOrIsGrandfatheredStaysOut() {
        var q = new CollectionRow("q", CT, OWNER, MODEL, "v1", false, SOURCE, "quarantine");
        var g = new CollectionRow("g", CT, OWNER, MODEL, "v1", true, SOURCE, "live");
        assertThat(select(q, g)).isEqualTo(new Absent());
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
}
