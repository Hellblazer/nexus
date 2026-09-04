// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.sql.SQLTransientConnectionException;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-00wsf residual, review round 2, finding 4 (T2 [24238]) —
 * {@link AdmissionControlledReranker} shares its budget with embeds via the
 * SAME {@link LocalOnnxAdmission}, rather than sitting outside admission
 * control entirely (round 1's gap: the reranker was thread-capped via
 * {@link OnnxThreadPolicy} but free to stack an uncapped extra workload on
 * top of the embed admission budget).
 */
class AdmissionControlledRerankerTest {

    private static final class BlockingFakeReranker implements Reranker {
        final CountDownLatch release;
        final AtomicInteger live = new AtomicInteger(0);
        final AtomicInteger peak = new AtomicInteger(0);

        BlockingFakeReranker(CountDownLatch release) {
            this.release = release;
        }

        @Override
        public String modelToken() {
            return "fake-reranker";
        }

        @Override
        public List<Scored> rerank(String query, List<String> documents, Integer topK) {
            int current = live.incrementAndGet();
            peak.updateAndGet(prev -> Math.max(prev, current));
            try {
                assertThat(release.await(10, TimeUnit.SECONDS)).isTrue();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(e);
            } finally {
                live.decrementAndGet();
            }
            return List.of(new Scored(0, 1.0));
        }
    }

    private static final class BlockingFakeEmbedder implements Embedder {
        final CountDownLatch release;
        final AtomicInteger live;
        final AtomicInteger peak;

        BlockingFakeEmbedder(CountDownLatch release, AtomicInteger live, AtomicInteger peak) {
            this.release = release;
            this.live = live;
            this.peak = peak;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            int current = live.incrementAndGet();
            peak.updateAndGet(prev -> Math.max(prev, current));
            try {
                assertThat(release.await(10, TimeUnit.SECONDS)).isTrue();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(e);
            } finally {
                live.decrementAndGet();
            }
            return texts.stream().map(t -> new float[8]).toList();
        }

        @Override
        public String modelToken() {
            return "fake-local";
        }
    }

    @Test
    void rerank_delegatesResultAndModelToken() {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(2, 1000);
        AdmissionControlledReranker gated = new AdmissionControlledReranker(
                new BlockingFakeReranker(new CountDownLatch(0)), admission, true);
        assertThat(gated.modelToken()).isEqualTo("fake-reranker");
        assertThat(gated.rerank("q", List.of("d1", "d2"), null)).hasSize(1);
    }

    @Test
    void interactiveRerank_failsLoudWithTypedRetryableSignal_whenAdmissionQueueFull() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        BlockingFakeReranker fake = new BlockingFakeReranker(release);
        LocalOnnxAdmission admission = new LocalOnnxAdmission(1, 150);
        AdmissionControlledReranker blockingGated = new AdmissionControlledReranker(fake, admission, false);
        AdmissionControlledReranker interactiveGated = new AdmissionControlledReranker(fake, admission, true);

        ExecutorService pool = Executors.newFixedThreadPool(1);
        try {
            Future<?> holder = pool.submit(() -> blockingGated.rerank("q", List.of("d"), null));
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            while (fake.live.get() < 1 && System.nanoTime() < deadline) {
                Thread.sleep(5);
            }

            assertThatThrownBy(() -> interactiveGated.rerank("q2", List.of("d2"), null))
                    .isInstanceOf(RuntimeException.class)
                    .hasRootCauseInstanceOf(SQLTransientConnectionException.class);

            release.countDown();
            holder.get(10, TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void embedsAndReranks_shareOneAdmissionBudget_combinedPeakEqualsOnePermitSet() throws Exception {
        // The finding-4 proof: a rerank and a document embed racing the SAME
        // LocalOnnxAdmission must respect ONE combined ceiling, not compete in
        // two separate, uncoordinated pools.
        CountDownLatch release = new CountDownLatch(1);
        AtomicInteger combinedLive = new AtomicInteger(0);
        AtomicInteger combinedPeak = new AtomicInteger(0);
        BlockingFakeEmbedder embedder = new BlockingFakeEmbedder(release, combinedLive, combinedPeak);
        BlockingFakeRerankerSharingCounters reranker =
                new BlockingFakeRerankerSharingCounters(release, combinedLive, combinedPeak);

        LocalOnnxAdmission admission = new LocalOnnxAdmission(2, 5000);
        AdmissionControlledEmbedder gatedEmbedder =
                new AdmissionControlledEmbedder(embedder, admission, false);
        AdmissionControlledReranker gatedReranker =
                new AdmissionControlledReranker(reranker, admission, false);

        ExecutorService pool = Executors.newFixedThreadPool(4);
        try {
            List<Future<?>> futures = new java.util.ArrayList<>();
            for (int i = 0; i < 2; i++) {
                futures.add(pool.submit(() -> gatedEmbedder.embed(List.of("doc"))));
                futures.add(pool.submit(() -> gatedReranker.rerank("q", List.of("d"), null)));
            }
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            while (combinedLive.get() < 2 && System.nanoTime() < deadline) {
                Thread.sleep(5);
            }
            Thread.sleep(200);
            assertThat(combinedPeak.get())
                    .as("embeds and reranks must share ONE admission budget")
                    .isEqualTo(2);

            release.countDown();
            for (var f : futures) {
                f.get(10, TimeUnit.SECONDS);
            }
        } finally {
            pool.shutdownNow();
        }
    }

    /** Same shape as {@link BlockingFakeReranker} but shares the embedder's live/peak counters. */
    private static final class BlockingFakeRerankerSharingCounters implements Reranker {
        final CountDownLatch release;
        final AtomicInteger live;
        final AtomicInteger peak;

        BlockingFakeRerankerSharingCounters(CountDownLatch release, AtomicInteger live, AtomicInteger peak) {
            this.release = release;
            this.live = live;
            this.peak = peak;
        }

        @Override
        public String modelToken() {
            return "fake-reranker";
        }

        @Override
        public List<Scored> rerank(String query, List<String> documents, Integer topK) {
            int current = live.incrementAndGet();
            peak.updateAndGet(prev -> Math.max(prev, current));
            try {
                assertThat(release.await(10, TimeUnit.SECONDS)).isTrue();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(e);
            } finally {
                live.decrementAndGet();
            }
            return List.of(new Scored(0, 1.0));
        }
    }
}
