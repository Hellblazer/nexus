// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the rename window of {@link CompletionBootWork#publish} (O-5), review item 5: the flip of gate 3 to true
 * takes the write lock under a bounded wait, and reads again under it. Hermetic: a scripted reader, no database.
 */
class CompletionBootWorkRenameWindowTest {

    /** A reader that answers from a script, one result per read, the last one repeating. */
    private static final class Scripted implements CompletionBootWork.Reader {
        final AtomicInteger reads = new AtomicInteger();
        final boolean[] gate3;

        Scripted(boolean... gate3) {
            this.gate3 = gate3;
        }

        @Override
        public CompletionCheck.Result read() {
            boolean g = gate3[Math.min(reads.getAndIncrement(), gate3.length - 1)];
            return new CompletionCheck.Result(true, true, g, List.of(), List.of(), true, 0L, List.of(), List.of(), g ? "" : "no id index");
        }

        @Override
        public void abort() { }

        @Override
        public void close() { }
    }

    private static CompletionBootWork work(Scripted reader) {
        return new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, Clock.systemUTC(), Runnable::run);
    }

    @Test
    void theFlipReadsAgainUnderTheLock_andPublishesGate3OnlyIfItStillHolds() {
        // the first read says gate 3 holds; by the time the lock is taken it has dropped again (an id index went INVALID while a rename ran)
        var reader = new Scripted(true, false);
        var work = work(reader);

        work.publish();

        assertThat(reader.reads.get()).as("the flip re-read under the lock").isEqualTo(2);
        assertThat(work.latest().gate3()).as("gate 3 is published as the fresh read found it: not holding").isFalse();
        assertThat(work.gate3().holds()).isFalse();

        // control: it still holds on the second read, so it flips
        var steady = new Scripted(true, true);
        var work2 = work(steady);
        work2.publish();
        assertThat(work2.latest().gate3()).isTrue();
        assertThat(work2.latest().gate3Latched()).as("and it is latched").isTrue();
        assertThat(steady.reads.get()).isEqualTo(2);
        assertThat(work.latest().gate3Latched()).as("the first work never latched: its fresh read said no").isFalse();
    }

    @Test
    void onceLatched_theLiveGate3CanDropWithoutTheLatchGoing_andNoPinnedRenameBlocksAnotherSample() throws Exception {
        var reader = new Scripted(true, true, false, false, true);
        var work = work(reader);
        work.publish();   // read 1 + the re-read under the lock: latched
        assertThat(work.gate3Latched().holds()).isTrue();

        // a rename is open: after the latch no sample waits for it (every rename is one-row or refused now)
        ExecutorService pool = Executors.newSingleThreadExecutor();
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        try {
            Future<?> rename = pool.submit(() -> work.renameReadiness().pinned(() -> {
                inside.countDown();
                try {
                    release.await(30, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return null;
            }));
            assertThat(inside.await(10, TimeUnit.SECONDS)).isTrue();
            work.renameWindowWait(Duration.ofMillis(100));

            int result = work.publish();   // the live gate 3 is now false

            assertThat(result).as("the sample went through while a rename was open: no flip to wait for").isNotEqualTo(-1);
            assertThat(work.gate3().holds()).as("the live gate 3 dropped").isFalse();
            assertThat(work.gate3Latched().holds()).as("the latch stays").isTrue();
            assertThat(work.renameReadiness().gate3Latched()).isTrue();
            release.countDown();
            rename.get(10, TimeUnit.SECONDS);
            work.publish();
            work.publish();
            assertThat(work.gate3().holds()).as("the live gate 3 is back").isTrue();
            assertThat(work.gate3Latched().holds()).isTrue();
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    @Test
    void aRenameThatHoldsTheWindowLongerThanTheWaitPublishesTheSampleWithoutGate3_andTheRetryIsScheduled() throws Exception {
        var reader = new Scripted(true);
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var work = new CompletionBootWork(registry, reader);
        work.renameWindowWait(Duration.ofMillis(150));
        work.renameWindowRetry(Duration.ofMillis(100));
        ExecutorService pool = Executors.newSingleThreadExecutor();
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        try {
            Future<?> rename = pool.submit(() -> work.renameReadiness().pinned(() -> {
                inside.countDown();
                try {
                    release.await(30, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                return null;
            }));
            assertThat(inside.await(10, TimeUnit.SECONDS)).as("a pinned rename is open").isTrue();

            // the first sample runs on the schedule's thread with the rename open: 150 ms wait, then the sample is published without gate 3
            work.start(Duration.ofSeconds(60));   // the safety poll is a minute: only the scheduled retry can latch in this test
            assertThat(Rdr229Await.until(Duration.ofSeconds(10), () -> work.latest() != null))
                .as("the sample is published although a rename holds the window (the moves, CHECKs and counts are current)").isTrue();
            assertThat(work.latest().gate1()).as("the rest of the sample is the fresh read").isTrue();
            assertThat(work.latest().gate3()).as("gate 3 reads as not holding, so the readers keep the name key").isFalse();
            assertThat(work.gate3Latched()).as("and the latch is deferred").isFalse();

            release.countDown();
            rename.get(10, TimeUnit.SECONDS);
            assertThat(Rdr229Await.until(Duration.ofSeconds(10), work::gate3Latched))
                .as("the retry the check scheduled latches it, with no event and well before the 60 s poll").isTrue();
            assertThat(work.gate3().holds()).isTrue();
        } finally {
            release.countDown();
            pool.shutdownNow();
            work.close();
        }
    }

    /** A bounded poll for the schedule's thread. */
    private static final class Rdr229Await {
        static boolean until(Duration timeout, java.util.function.BooleanSupplier condition) throws InterruptedException {
            long deadline = System.nanoTime() + timeout.toNanos();
            while (!condition.getAsBoolean()) {
                if (System.nanoTime() >= deadline) {
                    return false;
                }
                Thread.sleep(20);
            }
            return true;
        }
    }
}
