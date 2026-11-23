// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), {@link RenameWindow#latchWriter()} on its own: the regime flips only after the row write returned, a row write that throws
 * leaves the regime off and the lock free, and a pinned rename past the bounded wait makes the writer give up without running the row write.
 */
class RenameWindowTest {

    @Test
    void theRegimeFlipsOnlyAfterTheRowWriteReturned() {
        var window = new RenameWindow();
        var seen = new AtomicInteger();
        window.latchWriter().latch(() -> seen.set(window.oneRowRegime() ? 1 : 2));
        assertThat(seen.get()).as("the row write ran with the regime off").isEqualTo(2);
        assertThat(window.oneRowRegime()).isTrue();
    }

    @Test
    void aRowWriteThatThrowsLeavesTheRegimeOffAndTheLockFree() {
        var window = new RenameWindow();
        assertThatThrownBy(() -> window.latchWriter().latch(() -> {
            throw new IllegalStateException("the row write failed");
        })).isInstanceOf(IllegalStateException.class).hasMessage("the row write failed");
        assertThat(window.oneRowRegime()).as("nothing was latched: the regime stays off").isFalse();
        assertThat(window.pinned(() -> "a rename runs: the write lock was released")).isEqualTo("a rename runs: the write lock was released");
        window.latchWriter().latch(() -> { });
        assertThat(window.oneRowRegime()).as("the next evaluation retries and latches").isTrue();
    }

    @Test
    void aPinnedRenamePastTheBoundedWaitMakesTheWriterGiveUpWithoutRunningTheRowWrite_andTheBusyHookRuns() throws Exception {
        var window = new RenameWindow();
        window.wait(Duration.ofMillis(100));
        var busy = new AtomicInteger();
        window.onBusy(busy::incrementAndGet);
        var ran = new AtomicInteger();
        ExecutorService pool = Executors.newSingleThreadExecutor();
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        try {
            Future<?> rename = pool.submit(() -> window.pinned(() -> {
                inside.countDown();
                try {
                    release.await(30, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return null;
            }));
            assertThat(inside.await(10, TimeUnit.SECONDS)).isTrue();

            assertThatThrownBy(() -> window.latchWriter().latch(ran::incrementAndGet)).isInstanceOf(IllegalStateException.class);

            assertThat(ran.get()).as("the row write did not run").isZero();
            assertThat(window.oneRowRegime()).isFalse();
            assertThat(busy.get()).as("the completion check was asked to read again soon").isEqualTo(1);
            release.countDown();
            rename.get(10, TimeUnit.SECONDS);
            window.latchWriter().latch(ran::incrementAndGet);
            assertThat(ran.get()).isEqualTo(1);
            assertThat(window.oneRowRegime()).isTrue();
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }
}
