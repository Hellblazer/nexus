// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import dev.nexus.service.reembed.ReembedStateMachine.Applied;
import dev.nexus.service.reembed.ReembedStateMachine.Event;
import dev.nexus.service.reembed.ReembedStateMachine.Refused;
import dev.nexus.service.reembed.ReembedStateMachine.State;
import org.junit.jupiter.api.Test;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.db.ReembedJobRepository.ABORTED;
import static dev.nexus.service.db.ReembedJobRepository.CUTOVER;
import static dev.nexus.service.db.ReembedJobRepository.DONE;
import static dev.nexus.service.db.ReembedJobRepository.DONE_WITH_SKIPS;
import static dev.nexus.service.db.ReembedJobRepository.RUNNING;
import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the runner's cutover, on a real Postgres. The marker on the job row
 * ({@code cutover_step}) is committed before each irreversible step, so these tests crash the runner at each point
 * (its phase hook throws) and start a second runner over the same database. "No second delete" is pinned by a
 * count of the calls the runner makes to {@code deleteCollection} (its {@code delete} phase), together with the
 * tenant's {@code gc_audit} rows.
 *
 * <p>Parity, the {@code kept_tombstoned} and residue dispositions and {@code --wait} are Step 4 (nexus-09iho) and
 * have no tests here.
 *
 * <p>Run: {@code -Dtest=ReembedCutoverTest}.
 */
class ReembedCutoverTest extends ReembedFixture {

    private static final long BIG_CAP = 10_000_000L;

    /** Counts the runner's calls to {@code deleteCollection}, records every cutover phase (the move phases are not
     *  this class's subject), and throws at {@code crashAt}. */
    private static final class Probe {
        final AtomicInteger deletes = new AtomicInteger();
        final List<String> phases = new ArrayList<>();

        java.util.function.Consumer<String> hook(Set<String> crashAt) {
            return p -> {
                if (!p.equals("move_call") && !p.equals("before_move")) {
                    phases.add(p);
                }
                if (p.equals("delete")) {
                    deletes.incrementAndGet();
                }
                if (crashAt.contains(p)) {
                    throw new IllegalStateException("simulated crash at " + p);
                }
            };
        }
    }

    private Job inCutover(Fx fx, Job started) {
        return force(fx, force(fx, started, RUNNING, Transition.NONE), CUTOVER, Transition.NONE);
    }

    // ── the finished dispositions ────────────────────────────────────────────

    @Test
    void theSourceIsSuperseded_thenDeleted_inThatOrder_exactlyOnce() {
        Fx fx = pair("deleted");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        runner.setPhaseHookForTests(probe.hook(Set.of()));

        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(done.cutoverStep()).isEqualTo("delete_started");
        assertThat(probe.phases.stream().filter(p -> !p.startsWith("before_b") && !p.equals("before_move")).toList())
            .containsExactly("before_supersede", "after_supersede", "before_delete", "delete", "after_delete");
        assertThat(probe.deletes).hasValue(1);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNull();
        assertThat(chunkCount(fx, fx.src())).as("the source's chunks went with it").isZero();
        assertThat(chunkCount(fx, fx.tgt())).isEqualTo(9);
        for (String d : docs) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
    }

    @Test
    void keepSource_endsDoneKeptByFlag_theSourceIsSupersededAndKept_nothingIsDeleted() {
        Fx fx = pair("keep");
        seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, true);
        Probe probe = new Probe();
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        runner.setPhaseHookForTests(probe.hook(Set.of()));

        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("kept_by_flag");
        assertThat(done.cutoverStep()).isEqualTo("superseded");
        assertThat(probe.deletes).hasValue(0);
        assertThat(probe.phases).doesNotContain("before_delete", "after_delete");
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNotNull();
        assertThat(supersededBy(fx, fx.src())).isEqualTo(fx.tgt());
        assertThat(chunkCount(fx, fx.src())).as("the source keeps its chunks").isEqualTo(9);
    }

    @Test
    void candidatesLeftAtCutover_endDoneWithSkips_nothingIsSupersededOrDeleted() {
        Fx fx = pair("leftover");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Job cutover = inCutover(fx, started);    // as if a pass had completed with the documents still in the source
        Probe probe = new Probe();
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        runner.setPhaseHookForTests(probe.hook(Set.of()));

        runner.tick();

        Job done = job(fx, cutover.jobId());
        assertThat(done.state()).isEqualTo(DONE_WITH_SKIPS);
        assertThat(done.cutoverStep()).isNull();
        assertThat(done.disposition()).isNull();
        assertThat(probe.phases).as("no cutover step ran").isEmpty();
        assertThat(probe.deletes).hasValue(0);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNotNull();
        assertThat(supersededBy(fx, fx.src())).isNotEqualTo(fx.tgt());
    }

    @Test
    void aJobFoundInCutoverAtBoot_isFinished() {
        Fx fx = pair("bootcutover");
        Job started = startJob(fx, null, false);
        Job cutover = inCutover(fx, started);    // an empty source: the pass completed, the engine then went down

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, cutover.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNull();
    }

    // ── restart across the irreversible step ─────────────────────────────────

    @Test
    void restartBeforeTheDeleteCommits_deletesOnce_andAbortWasRefusedFromTheMarkerOn() {
        Fx fx = pair("beforedelete");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        ReembedRunner first = runner(fx, ONE_DOC_PER_BATCH);
        first.setPhaseHookForTests(probe.hook(Set.of("before_delete")));

        first.tick();

        Job crashed = job(fx, started.jobId());
        assertThat(crashed.state()).isEqualTo(CUTOVER);
        assertThat(crashed.cutoverStep()).as("committed before the delete").isEqualTo("delete_started");
        assertThat(probe.deletes).hasValue(0);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("the delete did not happen").isNotNull();
        // abort is refused from the marker on, by the decision and by the compare-and-set
        assertThat(ReembedStateMachine.decide(State.CUTOVER, Event.ABORT, crashed.cutoverStep()))
            .isInstanceOf(Refused.class);
        assertThat(jobs.applyTransition(fx.tenant(), crashed.jobId(), CUTOVER, null, ABORTED, Transition.NONE))
            .as("an abort decided before the marker applies to nothing").isEmpty();
        assertThat(job(fx, crashed.jobId()).state()).isEqualTo(CUTOVER);

        ReembedRunner second = runner(fx, ONE_DOC_PER_BATCH);
        second.setPhaseHookForTests(probe.hook(Set.of()));
        second.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(probe.deletes).as("one delete across the restart").hasValue(1);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNull();
        for (String d : docs) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
    }

    @Test
    void restartAfterTheDeleteCommitted_finishesWithoutASecondDelete() {
        Fx fx = pair("afterdelete");
        seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        ReembedRunner first = runner(fx, ONE_DOC_PER_BATCH);
        first.setPhaseHookForTests(probe.hook(Set.of("after_delete")));

        first.tick();

        Job crashed = job(fx, started.jobId());
        assertThat(crashed.state()).as("the delete committed, the job did not hear of it").isEqualTo(CUTOVER);
        assertThat(crashed.cutoverStep()).isEqualTo("delete_started");
        assertThat(probe.deletes).hasValue(1);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNull();
        int auditRows = gcAuditRows(fx);

        ReembedRunner second = runner(fx, ONE_DOC_PER_BATCH);
        second.setPhaseHookForTests(probe.hook(Set.of()));
        second.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(probe.deletes).as("the source row was gone, so there was nothing to delete again").hasValue(1);
        assertThat(gcAuditRows(fx)).as("a second delete would have audited again").isEqualTo(auditRows);
        assertThat(probe.phases.stream().filter(p -> p.equals("before_supersede")).count())
            .as("a marker at delete_started skips the earlier steps").isEqualTo(1);
    }

    @Test
    void abortIsAcceptedFromCutoverBeforeTheDeleteStarted_andTheRunnerThenLeavesTheJobAlone() {
        Fx fx = pair("abortcut");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        ReembedRunner first = runner(fx, ONE_DOC_PER_BATCH);
        first.setPhaseHookForTests(probe.hook(Set.of("after_supersede")));

        first.tick();

        Job crashed = job(fx, started.jobId());
        assertThat(crashed.state()).isEqualTo(CUTOVER);
        assertThat(crashed.cutoverStep()).isEqualTo("superseded");
        assertThat(ReembedStateMachine.decide(State.CUTOVER, Event.ABORT, crashed.cutoverStep()))
            .isEqualTo(new Applied(State.ABORTED));
        Job aborted = jobs.applyTransition(fx.tenant(), crashed.jobId(), CUTOVER, crashed.cutoverStep(), ABORTED,
            Transition.NONE).orElseThrow();
        assertThat(aborted.state()).isEqualTo(ABORTED);
        assertThat(aborted.finishedAt()).isNotNull();

        ReembedRunner second = runner(fx, ONE_DOC_PER_BATCH);
        second.setPhaseHookForTests(probe.hook(Set.of()));
        second.tick();

        assertThat(job(fx, aborted.jobId()).state()).isEqualTo(ABORTED);
        assertThat(probe.deletes).hasValue(0);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("an aborted job deletes nothing").isNotNull();
    }

    @Test
    void theFlagsAndTheCapSurviveARestartInCutover_andAKeepSourceJobEndsKeptByFlag() {
        Fx fx = pair("flags");
        seedDocs(fx, 3, 3);
        Job started = startJob(fx, BIG_CAP, true);
        Probe probe = new Probe();
        ReembedRunner first = runner(fx, ONE_DOC_PER_BATCH);
        first.setPhaseHookForTests(probe.hook(Set.of("after_supersede")));

        first.tick();

        Job crashed = job(fx, started.jobId());
        assertThat(crashed.state()).isEqualTo(CUTOVER);
        assertThat(crashed.cutoverStep()).isEqualTo("superseded");
        assertThat(crashed.keepSource()).isTrue();
        assertThat(crashed.tokenCap()).isEqualTo(BIG_CAP);
        assertThat(crashed.tokensBilled()).isEqualTo(9);

        ReembedRunner second = runner(fx, ONE_DOC_PER_BATCH);
        second.setPhaseHookForTests(probe.hook(Set.of()));
        second.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("kept_by_flag");
        assertThat(done.keepSource()).isTrue();
        assertThat(done.tokenCap()).isEqualTo(BIG_CAP);
        assertThat(probe.deletes).hasValue(0);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNotNull();
    }

    // ── a cutover that cannot proceed ────────────────────────────────────────

    @Test
    void aSourceSupersededByAnotherCollection_blocksTheCutover_itStaysInCutoverAndAbortStillWorks() {
        Fx fx = pair("blocked");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        assertThat(catalog.supersedeCollection(fx.tenant(), fx.src(), "somewhere-else", null)).isEqualTo(1);
        Probe probe = new Probe();
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        runner.setPhaseHookForTests(probe.hook(Set.of()));

        runner.tick();

        Job blocked = job(fx, started.jobId());
        assertThat(blocked.state()).as("not failed, not done: it waits for an operator").isEqualTo(CUTOVER);
        assertThat(blocked.lastError()).as("the status shows why, not a bare cutover")
            .contains("cutover is blocked").contains("already superseded by somewhere-else").contains(fx.tgt());
        assertThat(blocked.cutoverStep()).isNull();
        assertThat(probe.deletes).hasValue(0);
        assertThat(supersededBy(fx, fx.src())).as("the other successor stands").isEqualTo("somewhere-else");
        assertThat(jobs.applyTransition(fx.tenant(), blocked.jobId(), CUTOVER, null, ABORTED, Transition.NONE))
            .isPresent();
    }

    @Test
    void aSourceThatIsNoLongerRegistered_blocksTheCutover_andSaysSoInLastError() {
        Fx fx = pair("unreg");
        Job started = startJob(fx, null, false);
        Job cutover = inCutover(fx, started);    // an empty source: the pass completed
        su(ctx -> ctx.deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant()))
            .and(CATALOG_COLLECTIONS.NAME.eq(fx.src())).execute());
        dev.nexus.service.db.CollectionRegistry.evictTenant(fx.tenant());

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job blocked = job(fx, cutover.jobId());
        assertThat(blocked.state()).isEqualTo(CUTOVER);
        assertThat(blocked.lastError()).contains("cutover is blocked").contains("no longer registered");
    }

    // ── the delete guard (the interim refusal before Step 4's dispositions) ──

    @Test
    void aLiveDocumentThatAppearsBetweenTheSupersedeAndTheDelete_blocksTheDelete_nothingIsDeleted() {
        Fx fx = pair("late");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        java.util.function.Consumer<String> base = probe.hook(Set.of());
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        runner.setPhaseHookForTests(p -> {
            base.accept(p);
            if (p.equals("after_supersede")) {
                // a document registered against the source after the pass looked and after the supersede
                catalog.upsertDocument(fx.tenant(), java.util.Map.of("tumbler", "rr." + fx.tag() + ".late",
                    "title", "late", "content_type", CONTENT_TYPE, "corpus", CONTENT_TYPE,
                    "physical_collection", fx.src(), "chunk_count", 0));
            }
        });

        runner.tick();

        Job blocked = job(fx, started.jobId());
        assertThat(blocked.state()).as("not done: the delete would have destroyed the late document")
            .isEqualTo(CUTOVER);
        assertThat(blocked.cutoverStep()).as("the delete never began").isEqualTo("superseded");
        assertThat(blocked.lastError()).contains("cutover is blocked").contains("a live document");
        assertThat(probe.deletes).hasValue(0);
        assertThat(probe.phases).doesNotContain("before_delete");
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNotNull();
        assertThat(physical(fx, "rr." + fx.tag() + ".late")).as("the late document survives").isEqualTo(fx.src());

        // the next tick sees a live candidate at the top of the cutover, the existing rule: done with skips
        runner.setPhaseHookForTests(null);
        runner.tick();
        assertThat(job(fx, started.jobId()).state()).isEqualTo(DONE_WITH_SKIPS);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("still nothing deleted").isNotNull();
    }

    @Test
    void rowsStillNamingTheSource_inAnySatelliteTable_blockTheDelete_andLastErrorNamesTheTable() {
        record Case(String table, java.util.function.BiConsumer<Fx, String> seed) { }
        List<Case> cases = List.of(
            new Case("document_aspects", (fx, doc) -> su(ctx -> ctx.insertInto(DOCUMENT_ASPECTS,
                    DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION, DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(fx.tenant(), fx.src(), "p/" + doc, OffsetDateTime.now(), "m1", "docling", doc,
                    "file:///p/" + doc).execute())),
            new Case("aspect_extraction_queue", (fx, doc) -> su(ctx -> ctx.insertInto(ASPECT_EXTRACTION_QUEUE,
                    ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                    ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .values(fx.tenant(), fx.src(), "p/" + doc, "pending", OffsetDateTime.now(), doc).execute())),
            new Case("document_highlights", (fx, doc) -> su(ctx -> ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                    DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(fx.tenant(), doc, fx.src(), "file:///hl/" + doc, "highlight", OffsetDateTime.now())
                .execute())));
        for (Case c : cases) {
            Fx fx = pair("residue");
            List<String> docs = seedDocs(fx, 2, 3);
            Job started = startJob(fx, null, false);
            Probe probe = new Probe();
            java.util.function.Consumer<String> base = probe.hook(Set.of());
            ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
            runner.setPhaseHookForTests(p -> {
                base.accept(p);
                if (p.equals("after_supersede")) {
                    c.seed().accept(fx, docs.get(0));    // a row written against the source after its document moved
                }
            });

            runner.tick();

            Job blocked = job(fx, started.jobId());
            assertThat(blocked.state()).as(c.table()).isEqualTo(CUTOVER);
            assertThat(blocked.lastError()).as(c.table()).contains("cutover is blocked").contains(c.table() + " row(s)");
            assertThat(probe.deletes).as(c.table()).hasValue(0);
            assertThat(catalog.getCollection(fx.tenant(), fx.src())).as(c.table()).isNotNull();
        }
    }
}
