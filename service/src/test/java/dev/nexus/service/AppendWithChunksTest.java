// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.RacedEmbedActivity;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-223 P1.1 (bead nexus-z0o2p.2) -- {@code /v1/catalog/manifest/append} takes an
 * inline {@code chunks} array: the chunk rows land in the SAME transaction as the
 * manifest rows that reference them, so an append can never leave a chunk without an
 * owner (RDR-223 Technical Design 1).
 *
 * <p>Exercised through {@link CombinedWriteService#appendCombined}, the seam that owns
 * the embed and the existence partition (RDR-181) and hands fully resolved tuples to
 * {@code CatalogRepository#appendManifestChunks}. Hermetic Testcontainers PG, mirroring
 * {@code CombinedWriteRepositoryTest} / {@code CombinedWriteRacedEmbedCounterTest}.
 */
class AppendWithChunksTest extends AtomicWriteTestBase {

    // ── Test Plan 2: known vs content-changed chashes ────────────────────────────

    @Test
    void append_knownAndContentChangedChashes_knownNotReEmbedded_changedIs() throws Exception {
        Fx f = fixture("kc");
        String known = ch("kc-known"), changed = ch("kc-changed"), fresh = ch("kc-fresh");

        // Seed: known + changed already stored (first embed of both).
        svc.writeManyCombined(TENANT, f.collection(),
            List.of(chunk(known, "known text"), chunk(changed, "changed text v1")),
            List.of(doc(f.docId(), List.of(row(0, known), row(1, changed)))),
            null, false, false);
        int embedsBefore = embedder.calls.get();

        var result = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(2, known), row(3, changed), row(4, fresh)),
            List.of(chunk(known, "known text"),            // identical text -> skipped
                    chunk(changed, "changed text v2"),     // divergent text -> re-embedded
                    chunk(fresh, "fresh text")),           // absent -> embedded
            false);

        assertThat(embedder.calls.get() - embedsBefore)
            .as("only the content-changed and the absent chash reach the embedder")
            .isEqualTo(2);
        assertThat(result.response())
            .containsEntry("ok", true)
            .containsEntry("count", 3)
            .containsEntry("chunks_written", 2)
            .containsEntry("chunks_deduped", 3)
            .containsEntry("embed_skipped", 1)
            .containsEntry("embed_embedded", 2);
        assertThat(chunkText(f.collection(), changed)).isEqualTo("changed text v2");
        assertThat(chunkText(f.collection(), known)).isEqualTo("known text");
        assertThat(chunkText(f.collection(), fresh)).isEqualTo("fresh text");
        assertThat(manifestChashes(f.docId()))
            .as("append upserts by position: rows 0,1 from the seed plus 2,3,4")
            .containsExactlyInAnyOrder(known, changed, known, changed, fresh);
    }

    // ── chunks no row references are neither embedded nor inserted ──────────────

    @Test
    void append_chunkNoRowReferences_isNeitherEmbeddedNorInserted() throws Exception {
        Fx f = fixture("un");
        String used = ch("un-used"), stray = ch("un-stray");
        int embedsBefore = embedder.calls.get();

        svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(0, used)),
            List.of(chunk(used, "used text"), chunk(stray, "stray text")),
            false);

        assertThat(embedder.calls.get() - embedsBefore).isEqualTo(1);
        assertThat(chunkText(f.collection(), used)).isEqualTo("used text");
        assertThat(chunkText(f.collection(), stray))
            .as("a chunk that no row of this request references is never inserted")
            .isNull();
    }

    // ── a row whose chash is in neither chunks nor the store fails loud ─────────

    @Test
    void append_rowReferencingUnknownChash_failsLoud_insertsNothing() throws Exception {
        Fx f = fixture("uk");
        String good = ch("uk-good"), ghost = ch("uk-ghost");

        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), f.docId(),
                List.of(row(0, good), row(1, ghost)),
                List.of(chunk(good, "good text")),
                false))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining(ghost);

        assertThat(chunkText(f.collection(), good))
            .as("the failed append's whole transaction rolled back, the good chunk included")
            .isNull();
        assertThat(repo.getManifest(TENANT, f.docId())).isEmpty();
    }

    // ── missing document: fails before any chunk is inserted ─────────────────────

    @Test
    void append_missingDocument_throwsBeforeEmbeddingOrInsertingAnyChunk() throws Exception {
        Fx f = fixture("md");
        String c = ch("md-c");
        long chunksBefore = chunkCount(f.collection());
        int embedsBefore = embedder.calls.get();

        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), "ap.no-such-doc",
                List.of(row(0, c)), List.of(chunk(c, "orphan text")), false))
            .isInstanceOf(CatalogRepository.DocumentNotFoundException.class);

        assertThat(chunkCount(f.collection())).as("zero chunks inserted").isEqualTo(chunksBefore);
        assertThat(embedder.calls.get() - embedsBefore)
            .as("the document pre-check spares the embed")
            .isZero();
    }

    // ── atomicity: a failure AFTER the chunk insert leaves zero chunks ───────────

    @Test
    void append_failingAfterTheChunkInsert_leavesZeroChunksAndNoRows() throws Exception {
        Fx f = fixture("tomb");
        String c = ch("tomb-c");
        // A tombstoned document passes the existence pre-check and the in-transaction document
        // check; the append fails at the chunk_count fold, the LAST statement, after the chunk
        // upsert and the manifest row insert have already run in the same transaction.
        assertThat(repo.deleteDocument(TENANT, f.docId())).isEqualTo(1);
        long chunksBefore = chunkCount(f.collection());
        int embedsBefore = embedder.calls.get();

        assertThatThrownBy(() -> svc.appendCombined(TENANT, f.collection(), f.docId(),
                List.of(row(0, c)), List.of(chunk(c, "tomb text")), false))
            .isInstanceOf(CatalogRepository.TombstonedDocumentException.class);

        assertThat(embedder.calls.get() - embedsBefore)
            .as("non-vacuity: the chunk WAS embedded and reached the insert before the failure").isEqualTo(1);
        assertThat(chunkCount(f.collection())).as("the rolled-back transaction took the chunk with it")
            .isEqualTo(chunksBefore);
        assertThat(chunkExists(f.collection(), c)).isFalse();
        assertThat(repo.getManifest(TENANT, f.docId())).isEmpty();
    }

    @Test
    void append_chunksNoRowReferences_areCountedAsUnreferenced() throws Exception {
        Fx f = fixture("unrefc");
        String used = ch("unrefc-used"), s1 = ch("unrefc-s1"), s2 = ch("unrefc-s2");
        var response = svc.appendCombined(TENANT, f.collection(), f.docId(),
            List.of(row(0, used)),
            List.of(chunk(used, "used"), chunk(s1, "s1"), chunk(s2, "s2"), chunk(s2, "s2")), false).response();
        assertThat(response).containsEntry("chunks_unreferenced", 2).containsEntry("chunks_written", 1);
    }

    // ── raced-embed counter (RDR-222) counts on the append path ─────────────────

    @Test
    void append_racedEmbedCounter_countsTheOverlapWithAConcurrentCombinedWrite() throws Exception {
        Fx f = fixture("rc");
        String otherDoc = "ap.rc.other." + seq.incrementAndGet();
        registerDoc(otherDoc, f.collection());
        String raced = ch("rc-raced");
        String text = "append raced-embed overlap fixture text";

        CombinedWriteService appender = new CombinedWriteService(
            tenantScope, repo, new EmbedderRouter(embedder, "document"));
        CombinedWriteService winner = new CombinedWriteService(
            tenantScope, repo, new EmbedderRouter(embedder, "document"));

        long racedBefore = RacedEmbedActivity.total();
        CountDownLatch paused = new CountDownLatch(1);
        CountDownLatch resume = new CountDownLatch(1);
        AtomicBoolean hookFired = new AtomicBoolean();
        appender.setAfterNeedEmbedResolvedHookForTests(() -> {
            hookFired.set(true);
            paused.countDown();
            try {
                if (!resume.await(30, TimeUnit.SECONDS)) {
                    throw new IllegalStateException("test timed out waiting to be resumed");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(e);
            }
        });

        AtomicReference<Throwable> err = new AtomicReference<>();
        Thread t = new Thread(() -> {
            try {
                appender.appendCombined(TENANT, f.collection(), f.docId(),
                    List.of(row(0, raced)), List.of(chunk(raced, text)), false);
            } catch (Throwable e) {
                err.set(e);
            }
        }, "append-raced-embed-writer");
        t.start();
        try {
            assertThat(paused.await(30, TimeUnit.SECONDS)).isTrue();
            winner.writeManyCombined(TENANT, f.collection(),
                List.of(chunk(raced, text)),
                List.of(doc(otherDoc, List.of(row(0, raced)))),
                null, false, false);
        } finally {
            resume.countDown();
        }
        t.join(TimeUnit.SECONDS.toMillis(30));
        assertThat(t.isAlive()).isFalse();
        assertThat(err.get()).as("a raced embed is not an error").isNull();
        assertThat(hookFired.get()).isTrue();
        assertThat(RacedEmbedActivity.total() - racedBefore)
            .as("the append path counts the one chash it raced on")
            .isEqualTo(1L);
    }

    // ── Test Plan 7: append with chunks concurrent with a superseded-chunk sweep ─

    @Test
    void append_concurrentWithSupersededChunkSweep_noDeadlock_newChunksSurvive() throws Exception {
        Fx f = fixture("sw");
        String sweeperDoc = "ap.sw.sweeper." + seq.incrementAndGet();
        registerDoc(sweeperDoc, f.collection());

        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            for (int i = 0; i < 25; i++) {
                String contested = ch("sw-contested-" + i);
                // The sweeper document owns `contested`; both writers race over it.
                svc.writeManyCombined(TENANT, f.collection(),
                    List.of(chunk(contested, "contested v1 " + i)),
                    List.of(doc(sweeperDoc, List.of(row(0, contested)))),
                    null, false, false);

                int pos = i;
                var barrier = new CyclicBarrier(2);
                // A: append re-adds `contested` with CHANGED text, so the chunk row is
                // rewritten inside the append's own transaction.
                Future<?> appendF = pool.submit(() -> {
                    barrier.await(10, TimeUnit.SECONDS);
                    return svc.appendCombined(TENANT, f.collection(), f.docId(),
                        List.of(row(pos, contested)),
                        List.of(chunk(contested, "contested v2 " + pos)), false);
                });
                // B: replaces the sweeper document with sweep=true, dropping `contested`.
                Future<?> sweepF = pool.submit(() -> {
                    barrier.await(10, TimeUnit.SECONDS);
                    return repo.writeManifestMany(TENANT,
                        List.of(doc(sweeperDoc, List.of())), f.collection(), null, true);
                });
                appendF.get(60, TimeUnit.SECONDS);   // ExecutionException here = deadlock or failure
                @SuppressWarnings("unchecked")
                Map<String, Object> sweepResult = (Map<String, Object>) sweepF.get(60, TimeUnit.SECONDS);
                assertThat(sweepResult.get("sweep_skipped"))
                    .as("iteration %d: the sweep ran to completion (a deadlock victim or a gate timeout"
                        + " would fail open as sweep_skipped=1 and pass every other assertion here)", i)
                    .isEqualTo(0);

                assertThat(manifestChashes(f.docId()))
                    .as("iteration %d: the appended row survives", i)
                    .contains(contested);
                assertThat(chunkText(f.collection(), contested))
                    .as("iteration %d: the sweep must not remove a chunk a committed manifest row owns", i)
                    .isNotNull();
            }
        } finally {
            pool.shutdownNow();
        }
    }
}
