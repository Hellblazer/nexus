// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import dev.nexus.service.db.ReembedSourceReader.DeleteCounts;
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
import static dev.nexus.service.db.ReembedJobRepository.FAILED;
import static dev.nexus.service.db.ReembedJobRepository.RUNNING;
import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_LINKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the runner's cutover, on a real Postgres. The marker on the job row
 * ({@code cutover_step}) is committed before each irreversible step, so these tests crash the runner at each point
 * (its phase hook throws) and start a second runner over the same database. "No second delete" is pinned by a
 * count of the calls the runner makes to {@code deleteCollection} (its {@code delete} phase), together with the
 * tenant's {@code gc_audit} rows.
 *
 * <p>Step 4 (bead nexus-09iho, with nexus-6grho): the ordered steps after the supersede. A tombstoned document ends
 * {@code kept_tombstoned} before any parity check; {@code keep_source} ends {@code kept_by_flag}; the parity check
 * fails a source that still has a manifest row, a live homed document or a moved document that is not on the target
 * (the job goes {@code failed/parity_mismatch}, nothing deleted, {@code --resume} fails the same way while the cause
 * stands, {@code --abort} leaves the source superseded and undeleted); rows naming the source that match no moved
 * document end {@code kept_residue} unless {@code force_delete}; and a {@code kept_tombstoned} job re-enters
 * {@code cutover} or {@code running} once the trash is purged or restored.
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



    // ── the ordered steps after the supersede (Step 4, nexus-09iho) ──────────

    /** Bare registration of a document: no manifest, homed at {@code home}. */
    private String registerBare(Fx fx, String name, String home) {
        String docId = "rr." + fx.tag() + "." + name;
        catalog.upsertDocument(fx.tenant(), java.util.Map.of("tumbler", docId, "title", "title-" + docId,
            "content_type", CONTENT_TYPE, "corpus", CONTENT_TYPE, "physical_collection", home, "chunk_count", 0));
        return docId;
    }

    /** The original chunk texts {@link #seedDocs} wrote for document number {@code d}. */
    private static List<String> seededTexts(Fx fx, int d, int perDoc) {
        List<String> texts = new ArrayList<>();
        for (int k = 0; k < perDoc; k++) texts.add("doc " + fx.tag() + " d" + d + " chunk " + k + " lorem ipsum");
        return texts;
    }

    private static final String SAT_ASPECTS = "document_aspects";
    private static final String SAT_QUEUE = "aspect_extraction_queue";
    private static final String SAT_HIGHLIGHTS = "document_highlights";

    /** One satellite row of {@code table} naming {@code collection}, for document {@code doc}. */
    private void seedSatellite(String table, Fx fx, String collection, String doc) {
        su(ctx -> switch (table) {
            case SAT_ASPECTS -> ctx.insertInto(DOCUMENT_ASPECTS,
                    DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION, DOCUMENT_ASPECTS.SOURCE_PATH,
                    DOCUMENT_ASPECTS.EXTRACTED_AT, DOCUMENT_ASPECTS.MODEL_VERSION, DOCUMENT_ASPECTS.EXTRACTOR_NAME,
                    DOCUMENT_ASPECTS.DOC_ID, DOCUMENT_ASPECTS.SOURCE_URI)
                .values(fx.tenant(), collection, "p/" + doc, OffsetDateTime.now(), "m1", "docling", doc,
                    "file:///p/" + doc).execute();
            case SAT_QUEUE -> ctx.insertInto(ASPECT_EXTRACTION_QUEUE,
                    ASPECT_EXTRACTION_QUEUE.TENANT_ID, ASPECT_EXTRACTION_QUEUE.COLLECTION,
                    ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, ASPECT_EXTRACTION_QUEUE.STATUS,
                    ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, ASPECT_EXTRACTION_QUEUE.DOC_ID)
                .values(fx.tenant(), collection, "p/" + doc, "pending", OffsetDateTime.now(), doc).execute();
            case SAT_HIGHLIGHTS -> ctx.insertInto(DOCUMENT_HIGHLIGHTS,
                    DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.DOC_ID, DOCUMENT_HIGHLIGHTS.COLLECTION,
                    DOCUMENT_HIGHLIGHTS.SOURCE_URI, DOCUMENT_HIGHLIGHTS.HIGHLIGHTS_MD, DOCUMENT_HIGHLIGHTS.INGESTED_AT)
                .values(fx.tenant(), doc, collection, "file:///hl/" + doc, "highlight", OffsetDateTime.now())
                .execute();
            default -> throw new IllegalArgumentException(table);
        });
    }

    /** Rows of {@code table} naming {@code collection}. */
    private int satelliteRows(String table, Fx fx, String collection) {
        return su(ctx -> switch (table) {
            case SAT_ASPECTS -> ctx.fetchCount(DOCUMENT_ASPECTS, DOCUMENT_ASPECTS.TENANT_ID.eq(fx.tenant()),
                DOCUMENT_ASPECTS.COLLECTION.eq(collection));
            case SAT_QUEUE -> ctx.fetchCount(ASPECT_EXTRACTION_QUEUE, ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq(fx.tenant()),
                ASPECT_EXTRACTION_QUEUE.COLLECTION.eq(collection));
            case SAT_HIGHLIGHTS -> ctx.fetchCount(DOCUMENT_HIGHLIGHTS, DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(fx.tenant()),
                DOCUMENT_HIGHLIGHTS.COLLECTION.eq(collection));
            default -> throw new IllegalArgumentException(table);
        });
    }

    private static final List<String> SATELLITE_TABLES = List.of(SAT_ASPECTS, SAT_QUEUE, SAT_HIGHLIGHTS);

    /** A runner whose hook counts deletes and runs {@code during} on every phase. */
    private ReembedRunner probedRunner(Fx fx, Probe probe, java.util.function.Consumer<String> during) {
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        java.util.function.Consumer<String> base = probe.hook(Set.of());
        runner.setPhaseHookForTests(p -> {
            base.accept(p);
            during.accept(p);
        });
        return runner;
    }

    // kept_tombstoned and kept_by_flag

    @Test
    void aTombstoneOutranksKeepSource_theOrderIsTombstonedThenFlag() {
        Fx fx = pair("tombflag");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, true);
        AtomicInteger moves = new AtomicInteger();
        Probe probe = new Probe();
        ReembedRunner runner = probedRunner(fx, probe, p -> {
            if (p.equals("before_move") && moves.incrementAndGet() == 2) {
                assertThat(catalog.deleteDocument(fx.tenant(), docs.get(1))).isEqualTo(1);
            }
        });

        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).as("RDR order: kept_tombstoned (3) before keep_source (4)")
            .isEqualTo("kept_tombstoned");
        assertThat(probe.deletes).hasValue(0);
    }

    // the parity check

    @Test
    void aDocumentRegisteredAtTheSourceAfterTheSupersede_failsParity_deletesNothing_andResumeMovesItAndFinishes() {
        Fx fx = pair("late");
        List<String> docs = seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        String late = "rr." + fx.tag() + ".late";
        ReembedRunner runner = probedRunner(fx, probe, p -> {
            if (p.equals("after_supersede")) {
                registerBare(fx, "late", fx.src());   // after the pass looked and after the supersede
            }
        });

        runner.tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).as("failed, not blocked: the parity check refused the delete").isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("parity_mismatch");
        assertThat(failed.lastError()).contains("1 live document(s) are homed at the source").contains(late)
            .contains("Inspect those documents' manifest rows").contains("--abort");
        assertThat(failed.cutoverStep()).as("the delete never began").isEqualTo("superseded");
        assertThat(probe.deletes).hasValue(0);
        assertThat(probe.phases).doesNotContain("before_delete");
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNotNull();
        assertThat(supersededBy(fx, fx.src())).isEqualTo(fx.tgt());
        assertThat(physical(fx, late)).as("the late document survives").isEqualTo(fx.src());

        // --resume re-runs the pass, which moves the late document (a candidate: homed here with no manifest)
        runner.setPhaseHookForTests(null);
        force(fx, failed, RUNNING, Transition.NONE);
        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).as(done.failureReason() + ": " + done.lastError()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(done.documentsMoved()).isEqualTo(3);
        assertThat(physical(fx, late)).isEqualTo(fx.tgt());
        for (String d : docs) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
    }

    @Test
    void aMovedDocumentHomedBackAtTheSource_failsParity_failsAgainOnResume_andAbortLeavesTheSourceSupersededAndUndeleted() {
        Fx fx = pair("homedback");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        ReembedRunner runner = probedRunner(fx, probe, p -> {
            if (p.equals("after_supersede")) {
                // a client registers a moved document again: physical_collection back to the source, manifest on the target
                registerBare(fx, "d000", fx.src());
            }
        });

        runner.tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("parity_mismatch");
        assertThat(failed.lastError()).contains("1 live document(s) are homed at the source").contains(docs.get(0))
            .contains("2 of 3 moved document(s)");
        assertThat(manifestCollections(fx, docs.get(0))).as("its manifest still names the target")
            .containsExactly(fx.tgt());
        int sourceChunks = chunkCount(fx, fx.src());
        assertThat(sourceChunks).as("the moved documents' source chunks are still there").isPositive();

        // --resume: the document is no candidate (its manifest names the target), so the pass ends and the same check fails again
        runner.setPhaseHookForTests(probe.hook(Set.of()));
        force(fx, failed, RUNNING, Transition.NONE);
        runner.tick();

        Job again = job(fx, started.jobId());
        assertThat(again.state()).as("the same check fails the same way while its cause stands").isEqualTo(FAILED);
        assertThat(again.failureReason()).isEqualTo("parity_mismatch");
        assertThat(again.lastError()).contains(docs.get(0));
        assertThat(probe.deletes).hasValue(0);

        // --abort: the source stays superseded and undeleted, for nx collection delete by hand
        Job aborted = force(fx, again, ABORTED, Transition.NONE);
        assertThat(aborted.state()).isEqualTo(ABORTED);
        assertThat(aborted.failureReason()).as("an aborted job still says why it failed").isEqualTo("parity_mismatch");
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNotNull();
        assertThat(supersededBy(fx, fx.src())).isEqualTo(fx.tgt());
        assertThat(chunkCount(fx, fx.src())).as("nothing was deleted").isEqualTo(sourceChunks);
        runner.tick();
        assertThat(job(fx, started.jobId()).state()).as("an aborted job is left alone").isEqualTo(ABORTED);
        assertThat(probe.deletes).hasValue(0);
    }

    @Test
    void aMovedDocumentPurgedSinceItsMove_failsParity_becauseTheCountsDiffer() {
        Fx fx = pair("purged");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        ReembedRunner runner = probedRunner(fx, probe, p -> {
            if (p.equals("after_supersede")) {
                su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant()))
                    .and(CATALOG_DOCUMENTS.TUMBLER.eq(docs.get(1))).execute());
            }
        });

        runner.tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("parity_mismatch");
        assertThat(failed.lastError()).contains("0 manifest row(s) still name the source")
            .contains("2 of 3 moved document(s)").contains(docs.get(1));
        assertThat(probe.deletes).hasValue(0);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("errs toward keeping the source").isNotNull();
    }

    @Test
    void aManifestRowWrittenBackBeforeTheSupersede_holdsTheSupersede_nothingIsSupersededOrDeleted() {
        // nexus-6grho: the cutover looks for manifest rows naming the source BEFORE it supersedes.
        Fx fx = pair("prewrite");
        List<String> docs = seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        ReembedRunner first = runner(fx, ONE_DOC_PER_BATCH);
        first.setPhaseHookForTests(probe.hook(Set.of("before_supersede")));
        first.tick();
        Job crashed = job(fx, started.jobId());
        assertThat(crashed.state()).as("crashed at the head of the cutover").isEqualTo(CUTOVER);
        assertThat(crashed.cutoverStep()).isNull();
        assertThat(supersededBy(fx, fx.src())).isNotEqualTo(fx.tgt());

        clientWrite(fx, fx.src(), docs.get(0), seededTexts(fx, 0, 3));   // a client writes a moved document back

        ReembedRunner second = runner(fx, ONE_DOC_PER_BATCH);
        second.setPhaseHookForTests(probe.hook(Set.of()));
        second.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE_WITH_SKIPS);
        assertThat(done.cutoverStep()).isNull();
        assertThat(supersededBy(fx, fx.src())).as("the source was not superseded").isNotEqualTo(fx.tgt());
        assertThat(probe.deletes).hasValue(0);
        assertThat(manifestCollections(fx, docs.get(0))).as("the client's write is the live copy").containsExactly(fx.src());
    }

    @Test
    void aManifestRowWrittenInTheWindowBeforeTheSupersede_failsParityNamingTheDocument_andResumeHealsItWithBothColumnsAgreeing() {
        // nexus-6grho / S-R2-2: a write that lands between the candidate check and the supersede is not lost.
        Fx fx = pair("window");
        List<String> docs = seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Probe probe = new Probe();
        AtomicInteger writes = new AtomicInteger();
        ReembedRunner runner = probedRunner(fx, probe, p -> {
            if (p.equals("before_supersede") && writes.getAndIncrement() == 0) {
                clientWrite(fx, fx.src(), docs.get(0), seededTexts(fx, 0, 3));
            }
        });

        runner.tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("parity_mismatch");
        assertThat(failed.lastError()).contains("3 manifest row(s) still name the source").contains(docs.get(0));
        assertThat(probe.deletes).hasValue(0);
        assertThat(manifestCollections(fx, docs.get(0))).as("the write is intact").containsExactly(fx.src());
        assertThat(supersededBy(fx, fx.src())).isEqualTo(fx.tgt());

        // --resume: the document is a candidate again (its manifest names the source), moves, and the source is deleted
        force(fx, failed, RUNNING, Transition.NONE);
        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        for (String d : docs) {
            assertThat(manifestCollections(fx, d)).as("manifest of " + d).containsExactly(fx.tgt());
            assertThat(physical(fx, d)).as("physical_collection of " + d).isEqualTo(fx.tgt());
        }
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNull();
    }

    // the residue check

    @Test
    void rowsNamingTheSourceThatMatchNoMovedDocument_endKeptResidue_inAnySatelliteTable() {
        for (String table : SATELLITE_TABLES) {
            Fx fx = pair("residue");
            seedDocs(fx, 2, 3);
            String other = registerBare(fx, "other", fx.tgt());     // a document that is not the job's to move
            seedSatellite(table, fx, fx.src(), other);
            Job started = startJob(fx, null, false);
            Probe probe = new Probe();
            ReembedRunner runner = probedRunner(fx, probe, p -> { });

            runner.tick();

            Job kept = job(fx, started.jobId());
            assertThat(kept.state()).as(table).isEqualTo(DONE);
            assertThat(kept.disposition()).as(table).isEqualTo("kept_residue");
            assertThat(kept.lastError()).as(table).contains("1 " + table + " row(s)")
                .contains("nx collection delete " + fx.src()).contains("--force-delete");
            assertThat(probe.deletes).as(table).hasValue(0);
            assertThat(supersededBy(fx, fx.src())).as(table).isEqualTo(fx.tgt());
            assertThat(satelliteRows(table, fx, fx.src())).as(table + " row is intact").isEqualTo(1);
        }
    }

    @Test
    void forceDeleteLetsTheResidueThrough_theRowsGoWithTheSource_andTheJobSaysSo() {
        for (String table : SATELLITE_TABLES) {
            Fx fx = pair("force");
            seedDocs(fx, 2, 3);
            String other = registerBare(fx, "other", fx.tgt());
            seedSatellite(table, fx, fx.src(), other);
            Job started = jobs.start(fx.tenant(), fx.src(), fx.tgt(), null, false, true);
            Probe probe = new Probe();
            ReembedRunner runner = probedRunner(fx, probe, p -> { });

            runner.tick();

            Job done = job(fx, started.jobId());
            assertThat(done.state()).as(table).isEqualTo(DONE);
            assertThat(done.disposition()).as(table).isEqualTo("deleted");
            assertThat(done.forceDelete()).isTrue();
            assertThat(done.lastError()).as(table).contains("--force-delete deleted").contains("1 " + table + " row(s)");
            assertThat(probe.deletes).as(table).hasValue(1);
            assertThat(catalog.getCollection(fx.tenant(), fx.src())).as(table).isNull();
            assertThat(satelliteRows(table, fx, fx.src())).as(table + " row went with the source").isZero();
        }
    }

    @Test
    void forceDeleteDoesNotReachTheParityCheck_noFlagDeletesALiveDocumentsOnlyCopy() {
        Fx fx = pair("forceparity");
        seedDocs(fx, 2, 3);
        Job started = jobs.start(fx.tenant(), fx.src(), fx.tgt(), null, false, true);
        Probe probe = new Probe();
        ReembedRunner runner = probedRunner(fx, probe, p -> {
            if (p.equals("after_supersede")) {
                registerBare(fx, "late", fx.src());
            }
        });

        runner.tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("parity_mismatch");
        assertThat(probe.deletes).hasValue(0);
    }

    @Test
    void afterTheDelete_noSatelliteRowOfAMovedDocumentNamesTheSource_andTheMovedDocumentsAspectsAreIntact() {
        Fx fx = pair("satmoved");
        List<String> docs = seedDocs(fx, 2, 3);
        for (String table : SATELLITE_TABLES) seedSatellite(table, fx, fx.src(), docs.get(0));
        Job started = startJob(fx, null, false);

        runner(fx, ONE_DOC_PER_BATCH).tick();

        assertThat(job(fx, started.jobId()).disposition()).isEqualTo("deleted");
        for (String table : SATELLITE_TABLES) {
            assertThat(satelliteRows(table, fx, fx.src())).as(table + " naming the source").isZero();
            assertThat(satelliteRows(table, fx, fx.tgt())).as(table + " of the moved document is intact").isEqualTo(1);
        }
    }

    // --status counts

    @Test
    void theCountsStatusShowsBeforeTheDelete_matchWhatTheDeleteRemoved() {
        Fx fx = pair("counts");
        seedDocs(fx, 2, 2);
        String sibling = "quarantine-" + fx.src();
        String otherText = "chunk of another collection " + fx.tag();
        long topicA = 9_000_000L + Math.abs((long) (fx.tenant() + "a").hashCode());
        long topicB = topicA + 1;
        long topicElsewhere = topicA + 2;
        su(ctx -> {
            PgContainerHelper.insertCollection(ctx, fx.tenant(), sibling);
            for (int i = 0; i < 3; i++) {
                String text = "quarantined " + i + " " + fx.tag();
                PgContainerHelper.insertChunks(ctx, fx.tenant(), sibling, List.of(ch(text)), List.of(text),
                    List.of(new float[768]), List.of(java.util.Map.of("origin_collection", fx.src())));
            }
            PgContainerHelper.insertChunks(ctx, fx.tenant(), fx.tgt(), List.of(ch(otherText)), List.of(otherText),
                List.of(new float[384]), List.of(java.util.Map.of()));
            for (var spec : List.of(new Object[] {topicA, fx.src()}, new Object[] {topicB, fx.src()},
                                    new Object[] {topicElsewhere, fx.tgt()})) {
                ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.DOC_COUNT,
                        TOPICS.CREATED_AT, TOPICS.REVIEW_STATUS)
                    .values((Long) spec[0], fx.tenant(), "t" + spec[0], (String) spec[1], 0, OffsetDateTime.now(),
                        "pending")
                    .execute();
            }
            // two assignments of ANOTHER collection to the source's topic A, and one to B
            for (long topic : new long[] {topicA, topicB}) {
                ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION,
                        TOPIC_ASSIGNMENTS.ASSIGNED_AT, TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                    .values(fx.tenant(), Chash.fromHex(ch(otherText)).toBytes(), topic, "projection", fx.tgt(),
                        OffsetDateTime.now(), MINI)
                    .execute();
            }
            ctx.insertInto(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                .values(fx.tenant(), topicA, topicB).execute();
            ctx.insertInto(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID, TOPIC_LINKS.FROM_TOPIC_ID, TOPIC_LINKS.TO_TOPIC_ID)
                .values(fx.tenant(), topicElsewhere, topicB).execute();
            return null;
        });
        java.util.function.Supplier<int[]> present = () -> su(ctx -> new int[] {
            ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(fx.tenant()), CHUNKS.COLLECTION.eq(sibling)),
            ctx.fetchCount(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID.eq(fx.tenant()),
                TOPIC_ASSIGNMENTS.TOPIC_ID.in(topicA, topicB), TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.ne(fx.src())),
            ctx.fetchCount(TOPIC_LINKS, TOPIC_LINKS.TENANT_ID.eq(fx.tenant()),
                TOPIC_LINKS.FROM_TOPIC_ID.in(topicA, topicB).or(TOPIC_LINKS.TO_TOPIC_ID.in(topicA, topicB)))});
        Job started = startJob(fx, null, false);
        java.util.concurrent.atomic.AtomicReference<DeleteCounts> shown = new java.util.concurrent.atomic.AtomicReference<>();
        java.util.concurrent.atomic.AtomicReference<int[]> before = new java.util.concurrent.atomic.AtomicReference<>();
        Probe probe = new Probe();
        ReembedRunner runner = probedRunner(fx, probe, p -> {
            if (p.equals("before_delete")) {
                shown.set(reader.deleteCounts(fx.tenant(), fx.src()));
                before.set(present.get());
            }
        });

        runner.tick();

        assertThat(job(fx, started.jobId()).disposition()).isEqualTo("deleted");
        int[] after = present.get();
        assertThat(shown.get().quarantineRows()).as("quarantine-sibling rows shown").isEqualTo(3)
            .isEqualTo(before.get()[0] - after[0]);
        assertThat(shown.get().foreignTopicAssignments()).as("cross-collection assignments shown").isEqualTo(2)
            .isEqualTo(before.get()[1] - after[1]);
        assertThat(shown.get().topicLinks()).as("topic links shown").isEqualTo(2)
            .isEqualTo(before.get()[2] - after[2]);
        assertThat(shown.get().unmatchedSatelliteRows()).isZero();
        assertThat(after).as("nothing of the three is left").containsExactly(0, 0, 0);
    }

    // re-entry of a kept_tombstoned job

    private Job keptTombstoned(Fx fx, List<String> docs) {
        Job started = startJob(fx, null, false);
        AtomicInteger moves = new AtomicInteger();
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        runner.setPhaseHookForTests(p -> {
            if (p.equals("before_move") && moves.incrementAndGet() == 2) {
                assertThat(catalog.deleteDocument(fx.tenant(), docs.get(1))).isEqualTo(1);
            }
        });
        runner.tick();
        Job kept = job(fx, started.jobId());
        assertThat(kept.state()).isEqualTo(DONE);
        assertThat(kept.disposition()).isEqualTo("kept_tombstoned");
        return kept;
    }

    @Test
    void aKeptTombstonedJob_onceTheTrashIsPurged_reentersCutover_andDeletesTheSource() {
        Fx fx = pair("reentercut");
        List<String> docs = seedDocs(fx, 3, 3);
        Job kept = keptTombstoned(fx, docs);
        su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant()))
            .and(CATALOG_DOCUMENTS.TUMBLER.eq(docs.get(1))).execute());

        Job back = jobs.reenter(fx.tenant(), kept.jobId(), CUTOVER).orElseThrow();

        assertThat(back.state()).isEqualTo(CUTOVER);
        assertThat(back.disposition()).as("the new end sets one").isNull();
        assertThat(back.lastError()).isNull();
        assertThat(back.finishedAt()).isNull();
        assertThat(back.cutoverStep()).as("the marker only moves forward").isEqualTo("superseded");
        assertThat(jobs.reenter(fx.tenant(), kept.jobId(), CUTOVER)).as("no longer done: a second re-entry applies to nothing")
            .isEmpty();

        // the purged document was never moved, so this job's move record is 2 documents and the parity check holds
        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, kept.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNull();
        assertThat(done.documentsMoved()).isEqualTo(2);
    }

    @Test
    void aKeptTombstonedJob_onceTheTombstoneIsRestored_reentersRunning_movesIt_andFinishes() {
        Fx fx = pair("enterrun");
        List<String> docs = seedDocs(fx, 3, 3);
        Job kept = keptTombstoned(fx, docs);
        assertThat(catalog.restoreDocument(fx.tenant(), docs.get(1))).isEqualTo(1);

        Job back = jobs.reenter(fx.tenant(), kept.jobId(), RUNNING).orElseThrow();

        assertThat(back.state()).isEqualTo(RUNNING);
        assertThat(back.passNumber()).as("a fresh walk").isEqualTo(1);
        assertThat(back.docCursor()).isEmpty();

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, kept.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(done.documentsMoved()).isEqualTo(3);
        assertThat(done.documentsSkipped()).as("the skipped tombstone moved after its restore").isZero();
        for (String d : docs) {
            assertThat(manifestCollections(fx, d)).containsExactly(fx.tgt());
            assertThat(physical(fx, d)).isEqualTo(fx.tgt());
        }
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNull();
    }

    @Test
    void aReentryOfAJobThatIsNotKeptTombstonedAppliesToNothing() {
        Fx fx = pair("noreenter");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, true);   // kept_by_flag
        runner(fx, ONE_DOC_PER_BATCH).tick();
        assertThat(job(fx, started.jobId()).disposition()).isEqualTo("kept_by_flag");

        assertThat(jobs.reenter(fx.tenant(), started.jobId(), CUTOVER)).isEmpty();
        assertThat(jobs.reenter(fx.tenant(), started.jobId(), RUNNING)).isEmpty();
        assertThat(job(fx, started.jobId()).state()).isEqualTo(DONE);
    }
}
