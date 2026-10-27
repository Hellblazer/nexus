// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.reembed;

import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.db.ReembedJobRepository.CUTOVER;
import static dev.nexus.service.db.ReembedJobRepository.DONE;
import static dev.nexus.service.db.ReembedJobRepository.DONE_WITH_SKIPS;
import static dev.nexus.service.db.ReembedJobRepository.FAILED;
import static dev.nexus.service.db.ReembedJobRepository.PAUSED;
import static dev.nexus.service.db.ReembedJobRepository.RUNNING;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOB_MOVES;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): {@link ReembedRunner}'s pass loop on a real Postgres with fake
 * embedders ({@link ReembedFixture}). Cutover has its own class, {@code ReembedCutoverTest}. Where a test needs the
 * world to change between the runner's read of a batch and the move, it does so from the runner's phase hook
 * ({@code before_move}); where it needs a crash, the hook throws, which stops that job's work for the tick exactly
 * as a crash at that point would, and a second runner over the same database picks the job up.
 *
 * <p>Run: {@code -Dtest=ReembedRunnerTest}.
 */
class ReembedRunnerTest extends ReembedFixture {

    /** doc id to {@code outcome:reason} for every row the job recorded. */
    private Map<String, String> moves(Fx fx, UUID jobId) {
        Map<String, String> out = new LinkedHashMap<>();
        su(ctx -> ctx.select(REEMBED_JOB_MOVES.DOC_ID, REEMBED_JOB_MOVES.OUTCOME, REEMBED_JOB_MOVES.REASON)
            .from(REEMBED_JOB_MOVES)
            .where(REEMBED_JOB_MOVES.TENANT_ID.eq(fx.tenant())).and(REEMBED_JOB_MOVES.JOB_ID.eq(jobId))
            .orderBy(REEMBED_JOB_MOVES.DOC_ID)
            .fetch()).forEach(r -> out.put(r.value1(), r.value2() + (r.value3() == null ? "" : ":" + r.value3())));
        return out;
    }

    // ── the whole job ────────────────────────────────────────────────────────

    @Test
    void aPlannedJobRunsToCompletion_everyDocumentMovesOnce_andTheSourceIsDeleted() {
        Fx fx = pair("full");
        List<String> docs = seedDocs(fx, 4, 3);
        Job started = startJob(fx, null, false);
        int embeddedBefore = tgtEmbed.calls.get();

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.disposition()).isEqualTo("deleted");
        assertThat(done.documentsMoved()).isEqualTo(4);
        assertThat(done.documentsSkipped()).isZero();
        assertThat(done.chunksEmbedded()).isEqualTo(12);
        assertThat(done.chunksReused()).isZero();
        assertThat(done.tokensBilled()).as("the fake embedder bills one token a text").isEqualTo(12);
        assertThat(done.passNumber()).isEqualTo(1);
        assertThat(done.docCursor()).isEqualTo(docs.get(3));
        assertThat(done.finishedAt()).isNotNull();
        assertThat(done.failureReason()).isNull();
        assertThat(tgtEmbed.calls.get() - embeddedBefore).as("each chunk embedded once").isEqualTo(12);
        for (String d : docs) {
            assertThat(physical(fx, d)).isEqualTo(fx.tgt());
            assertThat(manifestCollections(fx, d)).containsExactly(fx.tgt());
        }
        assertThat(chunkCount(fx, fx.tgt())).isEqualTo(12);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("the source is deleted").isNull();
        assertThat(moves(fx, done.jobId()).values()).containsOnly("moved");
    }

    @Test
    void aBatchPacksWholeDocumentsUpToTheChunkLimit_andALargerDocumentGoesAlone() {
        Fx fx = pair("pack");
        // d000 has 5 chunks (alone: over the limit of 4), d001 and d002 have 2 each (together: exactly 4)
        List<String> big = new ArrayList<>();
        for (int k = 0; k < 5; k++) big.add("big doc " + fx.tag() + " chunk " + k);
        String d0 = seedDoc(fx, "d000", big);
        String d1 = seedDoc(fx, "d001", List.of("a " + fx.tag(), "b " + fx.tag()));
        String d2 = seedDoc(fx, "d002", List.of("c " + fx.tag(), "d " + fx.tag()));
        Job started = startJob(fx, null, true);
        List<Integer> movedAtEachBatch = new ArrayList<>();
        ReembedRunner runner = runner(fx, new ReembedRunner.Settings(4, 3, java.time.Duration.ofSeconds(60)));
        runner.setPhaseHookForTests(p -> {
            if (p.equals("before_batch")) {
                movedAtEachBatch.add((int) job(fx, started.jobId()).documentsMoved());
            }
        });

        runner.tick();

        assertThat(movedAtEachBatch).as("d000 alone, then d001 and d002 together").containsExactly(0, 1);
        for (String d : List.of(d0, d1, d2)) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
    }

    // ── restart ──────────────────────────────────────────────────────────────

    @Test
    void restartMidJobResumesFromTheCursor_noDocumentIsEmbeddedTwice() {
        Fx fx = pair("restart");
        List<String> docs = seedDocs(fx, 6, 3);
        Job started = startJob(fx, null, false);
        int embeddedBefore = tgtEmbed.calls.get();
        ReembedRunner first = runner(fx, ONE_DOC_PER_BATCH);
        AtomicInteger batches = new AtomicInteger();
        first.setPhaseHookForTests(p -> {
            if (p.equals("before_batch") && batches.incrementAndGet() == 3) {
                throw new IllegalStateException("simulated crash before the third batch");
            }
        });

        first.tick();

        Job mid = job(fx, started.jobId());
        assertThat(mid.state()).as("the crash left the job where it was").isEqualTo(RUNNING);
        assertThat(mid.docCursor()).isEqualTo(docs.get(1));
        assertThat(mid.documentsMoved()).isEqualTo(2);
        assertThat(physical(fx, docs.get(1))).isEqualTo(fx.tgt());
        assertThat(physical(fx, docs.get(2))).isEqualTo(fx.src());
        assertThat(tgtEmbed.calls.get() - embeddedBefore).isEqualTo(6);

        // a new runner over the same database: the boot pickup of a running job
        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.documentsMoved()).isEqualTo(6);
        assertThat(tgtEmbed.calls.get() - embeddedBefore).as("the first two documents were not embedded again")
            .isEqualTo(18);
        for (String d : docs) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
    }

    // ── skips and failed batches ─────────────────────────────────────────────

    @Test
    void aDocumentChangedAfterTheRead_isASkipNotAFailedBatch_andALaterPassMovesIt() {
        Fx fx = pair("changed");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        AtomicInteger moves = new AtomicInteger();
        AtomicReference<Job> afterFirstPass = new AtomicReference<>();
        runner.setPhaseHookForTests(p -> {
            if (!p.equals("before_move")) return;
            int n = moves.incrementAndGet();
            if (n == 1) {
                // a client re-indexes docs[0] in the source between the runner's read and the move
                List<String> rewritten = new ArrayList<>(List.of("client chunk one " + fx.tag(),
                    "client chunk two " + fx.tag(), "client chunk three " + fx.tag(), "client chunk four " + fx.tag()));
                clientWrite(fx, fx.src(), docs.get(0), rewritten);
            } else if (n == 2) {
                afterFirstPass.set(job(fx, started.jobId()));
            }
        });

        runner.tick();

        Job seen = afterFirstPass.get();
        assertThat(seen.state()).as("a skip does not fail the job").isEqualTo(RUNNING);
        assertThat(seen.documentsSkipped()).isEqualTo(1);
        assertThat(seen.documentsMoved()).isZero();
        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.passNumber()).as("the skipped document needed a second pass").isEqualTo(2);
        assertThat(done.documentsMoved()).isEqualTo(3);
        assertThat(done.documentsSkipped()).as("moved after it was skipped, so no longer a skip").isZero();
        assertThat(moves(fx, done.jobId()).values()).containsOnly("moved");
        for (String d : docs) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
        assertThat(manifestChashes(fx, docs.get(0))).hasSize(4);
    }

    @Test
    void aTombstonedDocument_isASkipWithTheReasonTombstoned_andTheSourceEndsKeptTombstoned_withNoParityCheck() {
        // Deleting the source would delete the tombstoned document's rows, restorable from trash until then
        // (RDR-210 Cutover): the job ends done/kept_tombstoned, the source superseded and kept. It does so BEFORE
        // the parity check: a moved document purged since its move would fail parity (the counts differ), and here
        // one is, so a job that ended failed/parity_mismatch would show the check ran.
        Fx fx = pair("tomb");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        AtomicInteger moves = new AtomicInteger();
        AtomicInteger deletes = new AtomicInteger();
        AtomicInteger sourceChunksBeforeCutover = new AtomicInteger(-1);
        runner.setPhaseHookForTests(p -> {
            if (p.equals("delete")) deletes.incrementAndGet();
            if (p.equals("before_move") && moves.incrementAndGet() == 2) {
                assertThat(catalog.deleteDocument(fx.tenant(), docs.get(1))).isEqualTo(1);
                sourceChunksBeforeCutover.set(chunkCount(fx, fx.src()));
            }
            if (p.equals("before_move") && moves.get() == 3) {
                // a moved document purged since its move: parity would refuse (1 of 2 moved documents agree)
                su(ctx -> ctx.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TENANT_ID.eq(fx.tenant()))
                    .and(CATALOG_DOCUMENTS.TUMBLER.eq(docs.get(0))).execute());
            }
        });

        runner.tick();

        Job kept = job(fx, started.jobId());
        assertThat(kept.state()).as("done, not failed: the tombstone decides before parity is asked").isEqualTo(DONE);
        assertThat(kept.disposition()).isEqualTo("kept_tombstoned");
        assertThat(kept.failureReason()).isNull();
        assertThat(kept.documentsMoved()).isEqualTo(2);
        assertThat(kept.documentsSkipped()).isEqualTo(1);
        assertThat(moves(fx, kept.jobId())).containsEntry(docs.get(1), "skipped:tombstoned");
        assertThat(kept.passNumber()).as("a tombstoned document is no candidate for another pass").isEqualTo(1);
        assertThat(kept.lastError()).contains("1 tombstoned document(s)").contains("re-embed --all");
        assertThat(kept.cutoverStep()).as("superseded, the delete never began").isEqualTo("superseded");
        assertThat(deletes).hasValue(0);
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("the source row is intact").isNotNull();
        assertThat(supersededBy(fx, fx.src())).as("kept superseded, out of the routing listing").isEqualTo(fx.tgt());
        assertThat(sourceChunksBeforeCutover.get()).isPositive();
        assertThat(chunkCount(fx, fx.src())).as("the source's chunks are intact: the cutover deleted none")
            .isEqualTo(sourceChunksBeforeCutover.get());
        assertThat(physical(fx, docs.get(1))).as("the tombstoned document is still homed at the source")
            .isEqualTo(fx.src());

        // a finished job is not picked up again
        runner.tick();
        assertThat(job(fx, started.jobId()).state()).isEqualTo(DONE);
        assertThat(deletes).hasValue(0);
    }

    // ── RDR-231 step 4: the move rewrites a note's source_uri; a taken URI is a per-document skip ─────────

    private List<String> threeNotes(Fx fx) {
        List<String> docs = new ArrayList<>();
        for (int d = 0; d < 3; d++) {
            List<String> texts = new ArrayList<>();
            for (int k = 0; k < 3; k++) texts.add("note " + fx.tag() + " d" + d + " chunk " + k + " lorem ipsum");
            docs.add(seedNote(fx, String.format("d%03d", d), texts));
        }
        return docs;
    }

    @Test
    void aNoteMovesWithItsUriRewritten_toTheSibling() {
        // RDR-231 Test Plan row 553 through the runner.
        Fx fx = pair("noteuri");
        List<String> docs = threeNotes(fx);
        Job started = startJob(fx, null, false);

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        for (int d = 0; d < 3; d++) {
            assertThat(sourceUri(fx, docs.get(d))).isEqualTo(noteUri(fx.tgt(), String.format("d%03d", d)));
            assertThat(physical(fx, docs.get(d))).isEqualTo(fx.tgt());
        }
        assertThat(jobs.skippedDocuments(fx.tenant(), done.jobId())).isEmpty();
    }

    @Test
    void aTakenUri_isCheckedBeforeEmbedding_theDocumentIsSkippedWithItsHolder_theOthersMove_theJobDoesNotFail() {
        // RDR-231 Test Plan row 554 through the runner. The holder exists before the job starts, so the
        // pre-check catches it and the colliding document is embedded zero times.
        Fx fx = pair("uritaken");
        List<String> docs = threeNotes(fx);
        String holder = seedHolder(fx, "holder", noteUri(fx.tgt(), "d001"));
        Job started = startJob(fx, null, false);
        int embeddedBefore = tgtEmbed.calls.get();

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).as("a taken URI is a skip, never a failed job").isEqualTo(DONE_WITH_SKIPS);
        assertThat(done.failureReason()).isNull();
        assertThat(done.documentsMoved()).isEqualTo(2);
        assertThat(done.documentsSkipped()).as("the document counts once across every pass").isEqualTo(1);
        assertThat(done.passNumber()).as("a skip leaves a candidate, so the passes run to the bound").isEqualTo(3);
        assertThat(tgtEmbed.calls.get() - embeddedBefore)
            .as("the two moved documents' six chunks; the colliding document is embedded zero times").isEqualTo(6);
        assertThat(chunkCount(fx, fx.tgt())).isEqualTo(6);
        assertThat(moves(fx, done.jobId())).containsEntry(docs.get(1), "skipped:" + ReembedRunner.REASON_URI_TAKEN + ":" + holder)
            .containsEntry(docs.get(0), "moved").containsEntry(docs.get(2), "moved");
        assertThat(physical(fx, docs.get(1))).isEqualTo(fx.src());
        assertThat(sourceUri(fx, docs.get(1))).isEqualTo(noteUri(fx.src(), "d001"));
        assertThat(sourceUri(fx, docs.get(0))).isEqualTo(noteUri(fx.tgt(), "d000"));
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("nothing is deleted under a skip").isNotNull();
        // the job report names the document and its holder
        assertThat(jobs.skippedDocuments(fx.tenant(), done.jobId())).containsExactly(
            new dev.nexus.service.db.ReembedJobRepository.SkippedDocument(docs.get(1), ReembedRunner.REASON_URI_TAKEN, holder));
    }

    @Test
    void aUriTakenAfterThePreCheck_theEngineBackstopIsASkipWithTheHolder_notAFailedBatch() {
        // The holder appears between the runner's pre-check and the move (the before_move seam). completeMove's own
        // check throws SourceUriCollision; failureDetail passes it through as the stable prefix and the holder, and
        // the runner records a skip, so the batch is not failed and the other documents commit.
        Fx fx = pair("urirace");
        List<String> docs = threeNotes(fx);
        Job started = startJob(fx, null, false);
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        AtomicInteger moves = new AtomicInteger();
        AtomicReference<String> holder = new AtomicReference<>();
        runner.setPhaseHookForTests(p -> {
            if (p.equals("before_move") && moves.incrementAndGet() == 2) {
                holder.set(seedHolder(fx, "late", noteUri(fx.tgt(), "d001")));
            }
        });

        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE_WITH_SKIPS);
        assertThat(done.failureReason()).isNull();
        assertThat(done.documentsMoved()).isEqualTo(2);
        assertThat(done.documentsSkipped()).isEqualTo(1);
        assertThat(moves(fx, done.jobId())).containsEntry(docs.get(1),
            "skipped:" + ReembedRunner.REASON_URI_TAKEN + ":" + holder.get());
        assertThat(physical(fx, docs.get(1))).isEqualTo(fx.src());
        assertThat(sourceUri(fx, docs.get(1))).isEqualTo(noteUri(fx.src(), "d001"));
        assertThat(physical(fx, docs.get(0))).isEqualTo(fx.tgt());
        assertThat(physical(fx, docs.get(2))).isEqualTo(fx.tgt());
        assertThat(jobs.skippedDocuments(fx.tenant(), done.jobId())).hasSize(1);
    }

    @Test
    void aDocumentWhoseSourceChunkHasNoText_isASkip_leftBehindAfterThreePasses_andTheSourceIsKept() {
        Fx fx = pair("notext");
        List<String> docs = seedDocs(fx, 3, 3);
        // a reference-only chunk keeps its row and loses its text: it cannot be embedded again
        String victim = ch("doc " + fx.tag() + " d1 chunk 0 lorem ipsum");
        su(ctx -> ctx.update(CHUNKS).set(CHUNKS.CHUNK_TEXT, (String) null).set(CHUNKS.RETENTION, "reference-only")
            .where(CHUNKS.TENANT_ID.eq(fx.tenant())).and(CHUNKS.COLLECTION.eq(fx.src()))
            .and(CHUNKS.CHASH.eq(dev.nexus.service.db.Chash.fromHex(victim).toBytes())).execute());
        Job started = startJob(fx, null, false);

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE_WITH_SKIPS);
        assertThat(done.passNumber()).isEqualTo(3);
        assertThat(done.documentsMoved()).isEqualTo(2);
        assertThat(done.documentsSkipped()).isEqualTo(1);
        assertThat(moves(fx, done.jobId())).containsEntry(docs.get(1), "skipped:source_chunk_missing");
        assertThat(physical(fx, docs.get(1))).isEqualTo(fx.src());
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("nothing is deleted under a skip").isNotNull();
    }

    @Test
    void aFailedDocument_failsTheBatch_theCursorStaysAtTheLastCommittedDocument_andAResumeFinishesTheJob() {
        Fx fx = pair("fail");
        List<String> docs = seedDocs(fx, 5, 3);
        Job started = startJob(fx, null, false);
        catalog.setAfterMoveCompletedHookForTests(id -> {
            if (id.equals(docs.get(2))) throw new IllegalStateException("simulated failure after the move of " + id);
        });
        try {
            runner(fx, ONE_DOC_PER_BATCH).tick();
        } finally {
            catalog.setAfterMoveCompletedHookForTests(null);
        }

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("batch_failed");
        assertThat(failed.lastError()).contains(docs.get(2));
        assertThat(failed.docCursor()).as("the last committed document").isEqualTo(docs.get(1));
        assertThat(failed.documentsMoved()).isEqualTo(2);
        assertThat(failed.finishedAt()).isNull();
        assertThat(physical(fx, docs.get(2))).as("the failed document's move rolled back").isEqualTo(fx.src());
        assertThat(manifestCollections(fx, docs.get(2))).containsExactly(fx.src());

        // a failed job does nothing until it is resumed
        runner(fx, ONE_DOC_PER_BATCH).tick();
        assertThat(job(fx, started.jobId()).state()).isEqualTo(FAILED);

        force(fx, failed, RUNNING, Transition.NONE);
        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.documentsMoved()).isEqualTo(5);
        assertThat(done.failureReason()).as("a resume leaves the failure behind").isNull();
        for (String d : docs) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
    }

    @Test
    void aFailedMiddleDocumentOfAMultiDocumentBatch_isRetriedByTheResume_notSteppedPastByTheCursor() {
        Fx fx = pair("failmid");
        List<String> docs = seedDocs(fx, 5, 3);
        Job started = startJob(fx, null, false);
        // nine chunks to a batch: d0, d1 and d2 together
        ReembedRunner.Settings threeDocsAtATime = new ReembedRunner.Settings(9, 3, java.time.Duration.ofSeconds(60));
        catalog.setAfterMoveCompletedHookForTests(id -> {
            if (id.equals(docs.get(1))) throw new IllegalStateException("simulated failure after the move of " + id);
        });
        try {
            runner(fx, threeDocsAtATime).tick();
        } finally {
            catalog.setAfterMoveCompletedHookForTests(null);
        }

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("batch_failed");
        assertThat(failed.lastError()).as("the real failure is kept").contains(docs.get(1));
        assertThat(physical(fx, docs.get(0))).isEqualTo(fx.tgt());
        assertThat(physical(fx, docs.get(1))).as("the failed document's move rolled back").isEqualTo(fx.src());
        assertThat(physical(fx, docs.get(2))).as("the document after it committed regardless").isEqualTo(fx.tgt());
        assertThat(failed.documentsMoved()).isEqualTo(2);
        assertThat(failed.docCursor()).as("just before the failed document, not past the one after it")
            .isEqualTo(docs.get(0));

        force(fx, failed, RUNNING, Transition.NONE);
        runner(fx, threeDocsAtATime).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.passNumber()).as("the resume read the failed document first: no second pass").isEqualTo(1);
        assertThat(done.documentsMoved()).isEqualTo(5);
        assertThat(done.documentsSkipped()).isZero();
        assertThat(moves(fx, done.jobId()).values()).containsOnly("moved");
        for (String d : docs) assertThat(physical(fx, d)).isEqualTo(fx.tgt());
    }

    @Test
    void anUnregisteredTarget_failsTheJobAsBatchFailed_notAsAProfileChange() {
        Fx fx = pair("throws");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        // the target is no longer registered: the move is refused before anything is embedded or written
        su(ctx -> ctx.deleteFrom(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS)
            .where(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant()))
            .and(dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS.NAME.eq(fx.tgt())).execute());
        dev.nexus.service.db.CollectionRegistry.evictTenant(fx.tenant());

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).as("an unregistered target is a failed job, not a profile change")
            .isEqualTo("batch_failed");
        assertThat(failed.lastError()).contains(fx.tgt());
        assertThat(failed.lastError()).as("refused at the profile check, before any batch: no token note")
            .doesNotContain("tokens may have been billed");
        assertThat(failed.documentsMoved()).isZero();
        assertThat(failed.tokensBilled()).isZero();
        assertThat(moves(fx, failed.jobId())).isEmpty();
    }

    @Test
    void aMoveThatThrows_failsTheBatch_andRecordsTheUncountedTokenBound() {
        Fx fx = pair("movethrows");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        runner.setPhaseHookForTests(p -> {
            if ("move_call".equals(p)) throw new IllegalStateException("injected move failure");
        });

        runner.tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("batch_failed");
        assertThat(failed.lastError()).contains("injected move failure")
            .as("the batch's billed tokens died with the exception: the bound is recorded")
            .contains("tokens may have been billed").contains("not counted in tokens_billed");
        assertThat(failed.tokensBilled()).isZero();
    }

    // ── the token cap ────────────────────────────────────────────────────────

    @Test
    void theTokenCap_failsTheJobBeforeTheBatchThatWouldPassIt_andAResumeMayRaiseIt() {
        Fx fx = pair("cap");
        List<String> docs = seedDocs(fx, 4, 3);
        Job started = startJob(fx, 5L, false);
        int embeddedBefore = tgtEmbed.calls.get();

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job capped = job(fx, started.jobId());
        assertThat(capped.state()).isEqualTo(FAILED);
        assertThat(capped.failureReason()).isEqualTo("token_cap");
        assertThat(capped.lastError()).contains("cap of 5");
        assertThat(capped.documentsMoved()).isZero();
        assertThat(capped.tokensBilled()).isZero();
        assertThat(tgtEmbed.calls.get() - embeddedBefore).as("nothing was embedded under the cap").isZero();
        assertThat(physical(fx, docs.get(0))).isEqualTo(fx.src());

        force(fx, capped, RUNNING, new Transition(null, null, null, 1_000_000L));
        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.tokenCap()).as("the raised cap survives").isEqualTo(1_000_000L);
        assertThat(done.tokensBilled()).isEqualTo(done.chunksEmbedded()).isEqualTo(12);
    }

    // ── passes ───────────────────────────────────────────────────────────────

    @Test
    void aClientWritingBackAfterTheDocumentMoved_isMovedByALaterPass_withTheClientsNewText() {
        Fx fx = pair("drift");
        List<String> docs = seedDocs(fx, 3, 3);
        Job started = startJob(fx, null, false);
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        AtomicInteger batches = new AtomicInteger();
        List<String> newTexts = freshTexts(fx, docs.get(0));
        runner.setPhaseHookForTests(p -> {
            // before the second batch, docs[0] has moved: a client writes it back into the source (t5mg2-1)
            if (p.equals("before_batch") && batches.incrementAndGet() == 2) {
                clientWrite(fx, fx.src(), docs.get(0), newTexts);
                assertThat(physical(fx, docs.get(0))).as("the bare write leaves physical_collection on the target")
                    .isEqualTo(fx.tgt());
            }
        });

        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE);
        assertThat(done.passNumber()).as("the write-back was found by the second pass").isEqualTo(2);
        assertThat(done.documentsMoved()).isEqualTo(3);
        assertThat(done.documentsSkipped()).isZero();
        assertThat(manifestCollections(fx, docs.get(0))).containsExactly(fx.tgt());
        assertThat(manifestChashes(fx, docs.get(0))).containsExactlyElementsOf(
            newTexts.stream().map(t -> dev.nexus.service.db.Chash.ofText(t).toHex()).toList());
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).as("deleted once nothing was left").isNull();
    }

    @Test
    void passesAreBoundedAtThree_whatIsStillThereAfterwardIsASkip_andTheJobEndsWithSkips() {
        Fx fx = pair("bound");
        List<String> docs = seedDocs(fx, 2, 3);
        String d0 = docs.get(0);
        String d1 = docs.get(1);
        Job started = startJob(fx, null, false);
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        // A client that writes the OTHER document back into the source before every batch, once it has moved:
        // every pass leaves one document behind, however many passes there are.
        runner.setPhaseHookForTests(p -> {
            if (!p.equals("before_batch")) return;
            String upcoming = reader.candidates(fx.tenant(), fx.src(), job(fx, started.jobId()).docCursor(), 1)
                .get(0).docId();
            String other = upcoming.equals(d0) ? d1 : d0;
            if (fx.tgt().equals(physical(fx, other))) {
                clientWrite(fx, fx.src(), other, freshTexts(fx, other));
            }
        });

        runner.tick();

        Job done = job(fx, started.jobId());
        assertThat(done.state()).isEqualTo(DONE_WITH_SKIPS);
        assertThat(done.passNumber()).as("three passes and no more").isEqualTo(3);
        assertThat(done.documentsMoved()).isEqualTo(1);
        assertThat(done.documentsSkipped()).isEqualTo(1);
        assertThat(moves(fx, done.jobId())).containsEntry(d0, "skipped:pass_limit").containsEntry(d1, "moved");
        assertThat(manifestCollections(fx, d0)).as("the leftover is in the source").containsExactly(fx.src());
        assertThat(done.cutoverStep()).as("no cutover step ran under a skip").isNull();
        assertThat(catalog.getCollection(fx.tenant(), fx.src())).isNotNull();
        assertThat(supersededBy(fx, fx.src())).isNotEqualTo(fx.tgt());
    }

    // ── pause, abort, other states ───────────────────────────────────────────

    @Test
    void aPausedOrAbortedJobIsLeftAlone() {
        Fx fx = pair("paused");
        List<String> docs = seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Job running = force(fx, started, RUNNING, Transition.NONE);
        Job paused = force(fx, running, PAUSED, Transition.NONE);

        runner(fx, ONE_DOC_PER_BATCH).tick();

        assertThat(job(fx, started.jobId()).state()).isEqualTo(PAUSED);
        assertThat(physical(fx, docs.get(0))).isEqualTo(fx.src());

        Job aborted = force(fx, paused, "aborted", Transition.NONE);
        runner(fx, ONE_DOC_PER_BATCH).tick();
        assertThat(job(fx, aborted.jobId()).state()).isEqualTo("aborted");
        assertThat(physical(fx, docs.get(0))).isEqualTo(fx.src());
    }

    @Test
    void aPauseThatLandsMidJob_takesEffectAtTheNextBatchBoundary() {
        Fx fx = pair("pauseMid");
        List<String> docs = seedDocs(fx, 4, 3);
        Job started = startJob(fx, null, false);
        ReembedRunner runner = runner(fx, ONE_DOC_PER_BATCH);
        AtomicInteger batches = new AtomicInteger();
        runner.setPhaseHookForTests(p -> {
            // a control route pauses the job while the second batch is about to run
            if (p.equals("before_batch") && batches.incrementAndGet() == 2) {
                force(fx, job(fx, started.jobId()), PAUSED, Transition.NONE);
            }
        });

        runner.tick();

        Job paused = job(fx, started.jobId());
        assertThat(paused.state()).isEqualTo(PAUSED);
        assertThat(paused.documentsMoved()).as("the batch in flight finished, the next never started").isEqualTo(2);
        assertThat(physical(fx, docs.get(1))).isEqualTo(fx.tgt());
        assertThat(physical(fx, docs.get(2))).isEqualTo(fx.src());
    }

    // ── the profile ──────────────────────────────────────────────────────────

    @Test
    void aProfileChangedBeforeAResume_failsTheJobWithProfileChanged_andMovesNothing() {
        Fx fx = pair("profile");
        List<String> docs = seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        Job paused = force(fx, force(fx, started, RUNNING, Transition.NONE), PAUSED, Transition.NONE);
        setProfile(fx.tenant(), BGE, 768);    // the tenant's model for knowledge is no longer the target's
        int embeddedBefore = tgtEmbed.calls.get();
        force(fx, paused, RUNNING, Transition.NONE);    // the resume

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("profile_changed");
        assertThat(failed.lastError()).contains(BGE).contains(fx.tgt());
        assertThat(failed.documentsMoved()).isZero();
        assertThat(tgtEmbed.calls.get() - embeddedBefore).isZero();
        assertThat(physical(fx, docs.get(0))).isEqualTo(fx.src());
    }

    @Test
    void theBootPickupOfAJobAlreadyRunning_checksTheProfileToo() {
        // Entering running and the PROFILE_CHANGED that follows a mismatch are two compare-and-set steps; a crash
        // between them leaves a running row nobody has checked. The first tick after boot must check it.
        Fx fx = pair("bootcheck");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        force(fx, started, RUNNING, Transition.NONE);
        setProfile(fx.tenant(), BGE, 768);
        int embeddedBefore = tgtEmbed.calls.get();

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("profile_changed");
        assertThat(tgtEmbed.calls.get() - embeddedBefore).isZero();
    }

    @Test
    void aPlannedJobIsCheckedAgainstTheProfileOnPickup() {
        Fx fx = pair("pickupcheck");
        seedDocs(fx, 2, 3);
        Job started = startJob(fx, null, false);
        setProfile(fx.tenant(), BGE, 768);

        runner(fx, ONE_DOC_PER_BATCH).tick();

        Job failed = job(fx, started.jobId());
        assertThat(failed.state()).isEqualTo(FAILED);
        assertThat(failed.failureReason()).isEqualTo("profile_changed");
        assertThat(failed.documentsMoved()).isZero();
    }

    @Test
    void aLargeJobDoesNotHoldACutoverBehindItsWholeRun_theTickIsRoundRobin() {
        Fx big = pair("fairbig");
        seedDocs(big, 4, 3);
        Job bigJob = startJob(big, null, false);
        Fx small = pair("fairsmall");
        Job smallStarted = startJob(small, null, false);
        // a cutover picked up at boot: an empty source whose pass completed before the engine went down
        Job smallJob = force(small, force(small, smallStarted, RUNNING, Transition.NONE), CUTOVER, Transition.NONE);
        List<String> phases = new ArrayList<>();
        ReembedRunner both = new ReembedRunner(jobs, reader, catalog, moveSvc,
            () -> List.of(big.tenant(), small.tenant()),
            new ReembedRunner.Settings(3, 3, java.time.Duration.ofSeconds(60), 1));
        both.setPhaseHookForTests(phases::add);

        both.tick();

        int firstCutover = phases.indexOf("before_supersede");
        int secondBatch = -1;
        int seen = 0;
        for (int i = 0; i < phases.size(); i++) {
            if (phases.get(i).equals("before_batch") && ++seen == 2) {
                secondBatch = i;
                break;
            }
        }
        assertThat(firstCutover).as("the small job's cutover ran").isGreaterThanOrEqualTo(0);
        assertThat(secondBatch).as("the large job ran a second batch").isGreaterThanOrEqualTo(0);
        assertThat(firstCutover).as("the cutover ran after one batch of the large job, not after its whole run")
            .isLessThan(secondBatch);
        assertThat(job(small, smallJob.jobId()).state()).isEqualTo(DONE);
        assertThat(job(big, bigJob.jobId()).state()).as("and the large job still finished").isEqualTo(DONE);
        assertThat(job(big, bigJob.jobId()).documentsMoved()).isEqualTo(4);
    }

    @Test
    void aTickVisitsEveryTenantItIsGiven_andOnlyTheOnesWithJobsChange() {
        Fx quiet = pair("quiet");
        Fx busy = pair("busy");
        seedDocs(busy, 2, 3);
        Job started = startJob(busy, null, false);
        ReembedRunner both = new ReembedRunner(jobs, reader, catalog, moveSvc,
            () -> List.of(quiet.tenant(), busy.tenant()), ONE_DOC_PER_BATCH);

        both.tick();

        assertThat(job(busy, started.jobId()).state()).isEqualTo(DONE);
        assertThat(jobs.listByStates(quiet.tenant(), List.of("planned", "running", "cutover", "done"))).isEmpty();
    }
}
