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
 * nexus-00wsf residual, review round 2 (T2 [24238]/[24239]) — {@link
 * AdmissionControlledEmbedder} gated through a SHARED {@link
 * LocalOnnxAdmission}.
 *
 * <p>{@link #twoRoutersOverOneSharedAdmission_combinedPeakEqualsOnePermitSet}
 * is the review's explicitly requested regression test: it reproduces
 * {@code Main.java}'s actual wiring shape (two {@link EmbedderRouter}
 * instances — document, query — both wrapping the SAME delegate through the
 * SAME {@link LocalOnnxAdmission}) and proves the combined bound is ONE
 * permit set, not two. Round 1's defect (a semaphore owned per {@code
 * AdmissionControlledEmbedder} instance, one constructed per router) would
 * fail this test by admitting up to {@code 2 * permits} at once.
 */
class AdmissionControlledEmbedderTest {

    /** Fake local embedder: blocks inside embed() until {@code release} counts down,
     *  tracking live/peak concurrency so the test can assert the bound held. */
    private static final class BlockingFakeEmbedder implements Embedder {
        final CountDownLatch release;
        final AtomicInteger live = new AtomicInteger(0);
        final AtomicInteger peak = new AtomicInteger(0);
        final AtomicInteger closeCount = new AtomicInteger(0);

        BlockingFakeEmbedder(CountDownLatch release) {
            this.release = release;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            int current = live.incrementAndGet();
            peak.updateAndGet(prev -> Math.max(prev, current));
            try {
                assertThat(release.await(10, TimeUnit.SECONDS))
                        .as("release latch reached before timeout")
                        .isTrue();
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

        @Override
        public void close() {
            closeCount.incrementAndGet();
        }
    }

    @Test
    void concurrentEmbeds_neverExceedConfiguredPermits() throws Exception {
        // 2 permits, 5 concurrent callers, all non-interactive (document-path:
        // blocking acquire). Every caller blocks inside embed() until released,
        // so peak-observed concurrency is a direct proxy for "how many
        // delegate.embed() calls were allowed to run at once".
        CountDownLatch release = new CountDownLatch(1);
        BlockingFakeEmbedder fake = new BlockingFakeEmbedder(release);
        LocalOnnxAdmission admission = new LocalOnnxAdmission(2, 5000);
        AdmissionControlledEmbedder gated = new AdmissionControlledEmbedder(fake, admission, false);

        int callers = 5;
        ExecutorService pool = Executors.newFixedThreadPool(callers);
        try {
            List<Future<?>> futures = new java.util.ArrayList<>();
            for (int i = 0; i < callers; i++) {
                futures.add(pool.submit(() -> gated.embed(List.of("x"))));
            }
            waitForLive(fake, 2);
            Thread.sleep(200); // headroom for a bug to over-admit before we assert
            assertThat(fake.peak.get())
                    .as("peak concurrent delegate.embed() calls must never exceed the permit bound")
                    .isEqualTo(2);

            release.countDown();
            for (var f : futures) {
                f.get(10, TimeUnit.SECONDS);
            }
            assertThat(fake.peak.get()).isEqualTo(2);
            assertThat(fake.live.get()).isZero();
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void twoRoutersOverOneSharedAdmission_combinedPeakEqualsOnePermitSet() throws Exception {
        // Reproduces Main.java's actual production wiring: ONE delegate, ONE
        // LocalOnnxAdmission, TWO AdmissionControlledEmbedder wrappers (doc:
        // non-interactive, query: interactive), TWO EmbedderRouter instances.
        // Concurrent traffic through BOTH routers must still respect a SINGLE
        // combined permit ceiling.
        CountDownLatch release = new CountDownLatch(1);
        BlockingFakeEmbedder fake = new BlockingFakeEmbedder(release);
        LocalOnnxAdmission admission = new LocalOnnxAdmission(3, 5000);

        AdmissionControlledEmbedder docGated = new AdmissionControlledEmbedder(fake, admission, false);
        AdmissionControlledEmbedder qryGated = new AdmissionControlledEmbedder(fake, admission, true);
        EmbedderRouter docRouter = new EmbedderRouter(docGated, "document");
        EmbedderRouter qryRouter = new EmbedderRouter(qryGated, "query");

        int callersPerRouter = 4; // 8 total callers racing 3 combined permits
        ExecutorService pool = Executors.newFixedThreadPool(callersPerRouter * 2);
        try {
            List<Future<?>> futures = new java.util.ArrayList<>();
            for (int i = 0; i < callersPerRouter; i++) {
                futures.add(pool.submit(() -> docRouter.embed(List.of("doc"))));
                futures.add(pool.submit(() -> qryRouter.embed(List.of("qry"))));
            }
            waitForLive(fake, 3);
            Thread.sleep(200);
            assertThat(fake.peak.get())
                    .as("combined peak across BOTH routers must equal the ONE shared permit set, "
                            + "not permits-per-router")
                    .isEqualTo(3);

            release.countDown();
            for (var f : futures) {
                f.get(10, TimeUnit.SECONDS);
            }
            assertThat(fake.peak.get()).isEqualTo(3);
            assertThat(fake.live.get()).isZero();
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void interactiveEmbed_admittedPromptlyWhenPermitFree() {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(2, 1000);
        AdmissionControlledEmbedder gated = new AdmissionControlledEmbedder(
                new BlockingFakeEmbedder(new CountDownLatch(0)), admission, true);
        assertThat(gated.embed(List.of("a", "b"))).hasSize(2);
    }

    @Test
    void interactiveEmbed_failsLoudWithTypedRetryableSignal_whenAdmissionQueueFull() throws Exception {
        // 1 permit, held by a blocked non-interactive caller for the whole test;
        // the interactive caller must time out and fail loud rather than wait
        // forever or silently degrade.
        CountDownLatch release = new CountDownLatch(1);
        BlockingFakeEmbedder fake = new BlockingFakeEmbedder(release);
        LocalOnnxAdmission admission = new LocalOnnxAdmission(1, 150); // 150ms query timeout
        AdmissionControlledEmbedder blockingGated = new AdmissionControlledEmbedder(fake, admission, false);
        AdmissionControlledEmbedder interactiveGated = new AdmissionControlledEmbedder(fake, admission, true);

        ExecutorService pool = Executors.newFixedThreadPool(1);
        try {
            Future<?> holder = pool.submit(() -> blockingGated.embed(List.of("holds the only permit")));
            waitForLive(fake, 1);

            assertThatThrownBy(() -> interactiveGated.embed(List.of("query text")))
                    .isInstanceOf(RuntimeException.class)
                    .hasRootCauseInstanceOf(SQLTransientConnectionException.class);

            release.countDown();
            holder.get(10, TimeUnit.SECONDS);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void embed_delegatesResultAndModelToken() {
        LocalOnnxAdmission admission = new LocalOnnxAdmission(4, 1000);
        AdmissionControlledEmbedder gated = new AdmissionControlledEmbedder(
                new BlockingFakeEmbedder(new CountDownLatch(0)), admission, false);
        assertThat(gated.modelToken()).isEqualTo("fake-local");
        assertThat(gated.embed(List.of("a", "b"))).hasSize(2);
    }

    @Test
    void close_delegatesToWrappedEmbedder() {
        BlockingFakeEmbedder fake = new BlockingFakeEmbedder(new CountDownLatch(0));
        AdmissionControlledEmbedder gated = new AdmissionControlledEmbedder(
                fake, new LocalOnnxAdmission(4, 1000), false);
        gated.close();
        assertThat(fake.closeCount.get()).isEqualTo(1);
    }

    private static void waitForLive(BlockingFakeEmbedder fake, int target) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (fake.live.get() < target && System.nanoTime() < deadline) {
            Thread.sleep(5);
        }
    }
}
