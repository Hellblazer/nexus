// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-00wsf residual — bounded admission control on the local (bge/ONNX)
 * embed path.
 *
 * <p>Mirrors {@code TenantScopeAdmissionTest}'s deterministic, latch-based
 * concurrency proof (no mocking framework, no wall-clock sleeps): N workers
 * race a wrapped fake embedder whose {@code embed()} blocks until released,
 * and the test asserts the observed peak concurrency never exceeds the
 * configured permit count, then that every worker eventually completes once
 * released (no deadlock, no starvation).
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
        // 2 permits, 5 concurrent callers. Every caller blocks inside embed()
        // until released, so peak-observed concurrency is a direct proxy for
        // "how many delegate.embed() calls were allowed to run at once".
        CountDownLatch release = new CountDownLatch(1);
        BlockingFakeEmbedder fake = new BlockingFakeEmbedder(release);
        AdmissionControlledEmbedder gated = new AdmissionControlledEmbedder(fake, 2);

        int callers = 5;
        ExecutorService pool = Executors.newFixedThreadPool(callers);
        try {
            List<java.util.concurrent.Future<?>> futures = new java.util.ArrayList<>();
            for (int i = 0; i < callers; i++) {
                futures.add(pool.submit(() -> gated.embed(List.of("x"))));
            }
            // Give the two admitted workers time to actually enter embed() and
            // register themselves before checking the ceiling never rises above it.
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            while (fake.live.get() < 2 && System.nanoTime() < deadline) {
                Thread.onSpinWait();
            }
            // Hold briefly so any THIRD admission (a bug) has a chance to show up
            // in peak before we release.
            Thread.sleep(200);
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
    void embed_delegatesResultAndModelToken() {
        AdmissionControlledEmbedder gated = new AdmissionControlledEmbedder(
                new BlockingFakeEmbedder(new CountDownLatch(0)), 4);
        assertThat(gated.modelToken()).isEqualTo("fake-local");
        assertThat(gated.embed(List.of("a", "b"))).hasSize(2);
    }

    @Test
    void close_delegatesToWrappedEmbedder() {
        BlockingFakeEmbedder fake = new BlockingFakeEmbedder(new CountDownLatch(0));
        AdmissionControlledEmbedder gated = new AdmissionControlledEmbedder(fake, 4);
        gated.close();
        assertThat(fake.closeCount.get()).isEqualTo(1);
    }

    @Test
    void constructor_refusesNonPositivePermits() {
        Embedder fake = new BlockingFakeEmbedder(new CountDownLatch(0));
        assertThatThrownBy(() -> new AdmissionControlledEmbedder(fake, 0))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new AdmissionControlledEmbedder(fake, -1))
                .isInstanceOf(IllegalArgumentException.class);
    }

    // ── permitsFromEnv resolver ─────────────────────────────────────────────

    @Test
    void permitsFromEnv_defaultsToHalfAvailableCores_minimumOne() {
        assertThat(AdmissionControlledEmbedder.permitsFromEnv(name -> null, 16)).isEqualTo(8);
        assertThat(AdmissionControlledEmbedder.permitsFromEnv(name -> null, 1)).isEqualTo(1);
        assertThat(AdmissionControlledEmbedder.permitsFromEnv(name -> null, 3)).isEqualTo(1);
    }

    @Test
    void permitsFromEnv_explicitOverrideWins() {
        Map<String, String> env = Map.of(AdmissionControlledEmbedder.PERMITS_ENV, "5");
        assertThat(AdmissionControlledEmbedder.permitsFromEnv(env::get, 16)).isEqualTo(5);
    }

    @Test
    void permitsFromEnv_refusesZeroOrNegativeOverride() {
        Map<String, String> zero = Map.of(AdmissionControlledEmbedder.PERMITS_ENV, "0");
        assertThatThrownBy(() -> AdmissionControlledEmbedder.permitsFromEnv(zero::get, 16))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining(AdmissionControlledEmbedder.PERMITS_ENV);

        Map<String, String> negative = Map.of(AdmissionControlledEmbedder.PERMITS_ENV, "-3");
        assertThatThrownBy(() -> AdmissionControlledEmbedder.permitsFromEnv(negative::get, 16))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void permitsFromEnv_refusesNonNumericOverride() {
        Map<String, String> junk = Map.of(AdmissionControlledEmbedder.PERMITS_ENV, "not-a-number");
        assertThatThrownBy(() -> AdmissionControlledEmbedder.permitsFromEnv(junk::get, 16))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
