// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import dev.nexus.service.reembed.TargetSelection.CollectionRow;
import dev.nexus.service.reembed.TargetSelection.Decision;
import dev.nexus.service.reembed.TargetSelection.Live;
import dev.nexus.service.reembed.TargetSelection.LockHolder;
import dev.nexus.service.reembed.TargetSelection.Refuse;
import dev.nexus.service.reembed.TargetSelection.Result;
import dev.nexus.service.reembed.TargetSelection.Revive;
import dev.nexus.service.reembed.TargetSelection.Superseded;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.db.ReembedJobRepository.ABORTED;
import static dev.nexus.service.db.ReembedJobRepository.CUTOVER;
import static dev.nexus.service.db.ReembedJobRepository.DONE;
import static dev.nexus.service.db.ReembedJobRepository.FAILED;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the target-selection decision on the registry rows a real job leaves
 * behind, with the repository calls that apply a {@code Revive}. {@code TargetSelectionTest} covers the decision on
 * hand-built rows; this class covers that the real rows ({@code listCollections}), the real supersede a cutover
 * performs, the real upsert that clears a supersession and the real {@code markRevived} agree with it. The start
 * route that strings these together is the next commit's; the test strings them itself.
 *
 * <p>Run: {@code -Dtest=ReembedTargetTest,TargetSelectionTest}.
 */
class ReembedTargetTest extends ReembedFixture {

    private List<CollectionRow> rows(Fx fx) {
        return catalog.listCollections(fx.tenant(), CONTENT_TYPE, null).stream().map(CollectionRow::fromMap).toList();
    }

    private String owner(Fx fx) {
        return String.valueOf(catalog.getCollection(fx.tenant(), fx.src()).get("owner_id"));
    }

    /** The job the first leg left behind, with the source superseded by the target as a cutover leaves it. */
    private Job firstLeg(Fx fx, boolean keepSource) {
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, keepSource);
        runner(fx, ONE_DOC_PER_BATCH).tick();
        return job(fx, started.jobId());
    }

    /** What a start would do about the way back (target = the old source's model), for a job whose source is the old target. */
    private Decision wayBack(Fx fx, LockHolder holder) {
        Result found = TargetSelection.select(rows(fx), CONTENT_TYPE, owner(fx), BGE);
        return TargetSelection.decide(found, fx.tgt(), holder);
    }

    private void applyRevive(Fx fx, Revive revive) {
        catalog.upsertCollection(fx.tenant(),
            Map.of("name", revive.name(), "content_type", CONTENT_TYPE, "owner_id", owner(fx)));
    }

    @Test
    void aSourceKeptByTheFlagIsRevivedWhenTheWayBackIsStarted_andTheEarlierDoneJobIsMarked() {
        Fx fx = pair("revivedone");
        Job first = firstLeg(fx, true);
        assertThat(first.state()).isEqualTo(DONE);
        assertThat(supersededBy(fx, fx.src())).isEqualTo(fx.tgt());

        Result found = TargetSelection.select(rows(fx), CONTENT_TYPE, owner(fx), BGE);
        assertThat(found).isEqualTo(new Superseded(fx.src(), fx.tgt()));
        Decision decision = wayBack(fx, null);
        assertThat(decision).isEqualTo(new Revive(fx.src()));

        applyRevive(fx, (Revive) decision);
        assertThat(jobs.markRevived(fx.tenant(), fx.src(), fx.tgt())).isPresent();

        assertThat(supersededBy(fx, fx.src())).as("the upsert cleared the supersession").isEmpty();
        assertThat(TargetSelection.select(rows(fx), CONTENT_TYPE, owner(fx), BGE)).isEqualTo(new Live(fx.src()));
        Job marked = job(fx, first.jobId());
        assertThat(marked.disposition()).isEqualTo("revived");
        assertThat(marked.state()).as("its state stands").isEqualTo(DONE);
    }

    @Test
    void anAbortedEarlierJobIsMarkedRevived_itsStateStaysAborted() {
        Fx fx = pair("revivedabort");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        ReembedRunner crashing = runner(fx, ONE_DOC_PER_BATCH);
        crashing.setPhaseHookForTests(p -> {
            if (p.equals("after_supersede")) throw new IllegalStateException("simulated crash");
        });
        crashing.tick();
        Job inCutover = job(fx, started.jobId());
        assertThat(inCutover.cutoverStep()).isEqualTo("superseded");
        Job aborted = jobs.applyTransition(fx.tenant(), inCutover.jobId(), CUTOVER, inCutover.cutoverStep(), ABORTED,
            Transition.NONE).orElseThrow();
        assertThat(supersededBy(fx, fx.src())).as("an abort after the supersede leaves it").isEqualTo(fx.tgt());

        Decision decision = wayBack(fx, null);
        assertThat(decision).isEqualTo(new Revive(fx.src()));
        applyRevive(fx, (Revive) decision);
        assertThat(jobs.markRevived(fx.tenant(), fx.src(), fx.tgt())).isPresent();

        Job marked = job(fx, aborted.jobId());
        assertThat(marked.state()).isEqualTo(ABORTED);
        assertThat(marked.disposition()).isEqualTo("revived");
        assertThat(supersededBy(fx, fx.src())).isEmpty();
    }

    @Test
    void anEarlierJobThatIsFailedHoldsItsSource_soTheWayBackIsRefusedNamingIt_andNothingIsRevived() {
        Fx fx = pair("revivefailed");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        ReembedRunner crashing = runner(fx, ONE_DOC_PER_BATCH);
        crashing.setPhaseHookForTests(p -> {
            if (p.equals("after_supersede")) throw new IllegalStateException("simulated crash");
        });
        crashing.tick();
        Job inCutover = job(fx, started.jobId());
        Job failed = jobs.applyTransition(fx.tenant(), inCutover.jobId(), CUTOVER, inCutover.cutoverStep(), FAILED,
            Transition.failed("parity_mismatch", "the target holds fewer documents than were moved")).orElseThrow();

        // the lock a start would find: a job in a lock state whose SOURCE is the collection being revived
        Job holder = jobs.listByStates(fx.tenant(), Set.of(ReembedStateMachine.State.FAILED.wire())).stream()
            .filter(j -> j.sourceCollection().equals(fx.src())).findFirst().orElseThrow();
        Decision decision = wayBack(fx, new LockHolder(holder.jobId(), holder.state()));

        assertThat(decision).isInstanceOf(Refuse.class);
        assertThat(((Refuse) decision).reason()).contains(failed.jobId().toString()).contains("failed");
        assertThat(supersededBy(fx, fx.src())).as("nothing was revived").isEqualTo(fx.tgt());
        assertThat(job(fx, failed.jobId()).disposition()).isNull();
    }

    @Test
    void aSourceSupersededByAnotherNameIsRefusedNamingBoth() {
        Fx fx = pair("elsewhere");
        assertThat(catalog.supersedeCollection(fx.tenant(), fx.src(), "somewhere-else", null)).isEqualTo(1);

        Decision decision = wayBack(fx, null);

        assertThat(decision).isInstanceOf(Refuse.class);
        assertThat(((Refuse) decision).reason()).contains(fx.src()).contains("somewhere-else").contains(fx.tgt());
        assertThat(supersededBy(fx, fx.src())).isEqualTo("somewhere-else");
    }

    @Test
    void aLiveTargetIsUsedAsItStands() {
        Fx fx = pair("live");

        Result found = TargetSelection.select(rows(fx), CONTENT_TYPE, owner(fx), MINI);

        assertThat(found).isEqualTo(new Live(fx.tgt()));
        assertThat(TargetSelection.decide(found, fx.src(), null)).isEqualTo(new TargetSelection.UseLive(fx.tgt()));
    }
}
