// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.BootWorkRegistry;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27), the rename window (O-5) over P3.4's latch writer seam ({@link CompletionCheck.Gate3LatchWriter}): the row write and
 * the one-row regime begin together inside the window's write lock, a pinned rename defers the latch (bounded) and the retry latches by itself, and a
 * lost flip self-heals from the durable accessor. Hermetic: a scripted reader that calls the writer as {@link CompletionCheck#evaluate} does, no database.
 */
class CompletionBootWorkRenameWindowTest {

    /**
     * A reader that behaves like the real check around the latch: when its gate 3 holds and no row exists it calls the writer with a row write that
     * records into {@code events}; the result's latch is the row's existence after that call, and a writer that throws leaves it unlatched.
     */
    private static class Scripted implements CompletionBootWork.Reader {
        final CompletionCheck.Gate3LatchWriter writer;
        final List<String> events = java.util.Collections.synchronizedList(new ArrayList<>());
        final AtomicBoolean row = new AtomicBoolean();
        volatile boolean gate3 = true;

        Scripted(CompletionCheck.Gate3LatchWriter writer) {
            this.writer = writer;
        }

        @Override
        public CompletionCheck.Result read() {
            boolean latched = row.get();
            if (gate3 && !latched) {
                try {
                    writer.latch(() -> {
                        row.set(true);
                        events.add("row");
                    });
                } catch (RuntimeException e) {
                    events.add("writer threw");
                }
                latched = row.get();
            }
            return new CompletionCheck.Result(true, true, gate3, List.of(), List.of(), true, 0L, List.of(), List.of(), gate3 ? "" : "no id index",
                CompletionCheck.Dangling.UNREAD, false, latched, false, false, CompletionCheck.RenameState.NOT_READ);
        }

        @Override
        public boolean latchRowPresent() {
            return row.get();
        }

        @Override
        public void abort() { }

        @Override
        public void close() { }
    }

    private static CompletionBootWork work(Scripted reader, RenameWindow window) {
        return new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, Clock.systemUTC(), Runnable::run, window);
    }

    @Test
    void theLatchWriterWritesTheRowThenFlipsTheRegime_insideTheWriteLock() throws Exception {
        var window = new RenameWindow();
        var inside = new ArrayList<String>();
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try {
            var reader = new Scripted(writeRow -> window.latchWriter().latch(() -> {
                // inside the write lock: the regime is not on yet, and a pin from another thread cannot get in
                inside.add("regime before row: " + window.oneRowRegime());
                Future<String> blocked = pool.submit(() -> window.pinned(() -> "ran"));
                try {
                    blocked.get(200, TimeUnit.MILLISECONDS);
                    inside.add("pin ran inside the lock");
                } catch (java.util.concurrent.TimeoutException expected) {
                    inside.add("pin waits");
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
                writeRow.run();
                inside.add("regime after row, before release: " + window.oneRowRegime());
            }));
            var work = work(reader, window);

            work.publish();

            assertThat(reader.events).containsExactly("row");
            assertThat(inside).as("order inside the lock: the row is written with the regime off and a pin waiting; the flip comes after")
                .containsExactly("regime before row: false", "pin waits", "regime after row, before release: false");
            assertThat(window.oneRowRegime()).as("one volatile write after the row: the regime is on").isTrue();
            assertThat(work.gate3Latched()).as("marlowe's accessor reads the row").isTrue();
            assertThat(work.renameReadiness().gate3Latched()).isTrue();
            assertThat(work.renameReadiness().pinned(() -> work.renameReadiness().gate3Latched()))
                .as("a rename that starts after the latch is in the one-row regime").isTrue();
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void aPinnedRenameDefersTheLatchPastTheWait_theWriterThrowsNothingIsLatched_andTheRetryLatchesByItself() throws Exception {
        var window = new RenameWindow();
        window.wait(Duration.ofMillis(150));
        var reader = new Scripted(window.latchWriter());
        var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, Clock.systemUTC(), null, window);
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

            // the first sample runs on the schedule's thread with the rename open; the safety poll is a minute, so only the retry can latch
            work.start(Duration.ofSeconds(60));
            assertThat(await(Duration.ofSeconds(10), () -> work.latest() != null)).as("the sample is published").isTrue();
            assertThat(reader.events).as("the writer gave up: the row was not written").containsExactly("writer threw");
            assertThat(work.latest().gate3()).as("the rest of the sample is the fresh read: gate 3 holds live").isTrue();
            assertThat(work.latest().gate3Latched()).as("but nothing is latched").isFalse();
            assertThat(work.gate3Latched()).isFalse();
            assertThat(window.oneRowRegime()).as("and the regime is off: the rename in flight is a name-path rename").isFalse();
            assertThat(work.renameReadiness().gate3Latched()).isFalse();

            release.countDown();
            rename.get(10, TimeUnit.SECONDS);
            assertThat(await(Duration.ofSeconds(10), work::gate3Latched))
                .as("the retry the check scheduled latches it, with no event and well before the 60 s poll").isTrue();
            assertThat(reader.events).containsExactly("writer threw", "row");
            assertThat(window.oneRowRegime()).isTrue();
        } finally {
            release.countDown();
            pool.shutdownNow();
            work.close();
        }
    }

    @Test
    void aLostFlipSelfHeals_theRowIsWrittenButTheRegimeNeverFlipped_yetEveryCheckReadsLatchedFromTheRow() {
        // a writer of another origin (here: the immediate one) leaves the regime off; the row says latched, and P3.7 reads the accessor on every check
        var window = new RenameWindow();
        var reader = new Scripted(CompletionCheck.Gate3LatchWriter.IMMEDIATE);
        var work = work(reader, window);

        work.publish();

        assertThat(window.oneRowRegime()).as("non-vacuity: the flip was lost").isFalse();
        assertThat(work.gate3Latched()).isTrue();
        assertThat(work.renameReadiness().gate3Latched()).as("self-healed from the durable accessor").isTrue();
    }

    @Test
    void afterTheLatch_theLiveGate3CanDropWithoutTheLatchGoing_andNoPinnedRenameBlocksAnotherSample() throws Exception {
        var window = new RenameWindow();
        window.wait(Duration.ofMillis(100));
        var reader = new Scripted(window.latchWriter());
        var work = work(reader, window);
        work.publish();
        assertThat(work.gate3Latched()).isTrue();

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
            reader.gate3 = false;

            int result = work.publish();   // the latch exists: the writer is not called, so nothing waits for the pinned rename

            assertThat(result).isNotEqualTo(-1);
            assertThat(work.latest().gate3()).as("the live gate 3 of the sample dropped (an id index went INVALID)").isFalse();
            assertThat(work.gate3().holds()).as("the readers' Gate3 is the sticky latch: it stays true after the live dip (review SB2)").isTrue();
            assertThat(work.gate3Latched()).as("the latch stays").isTrue();
            assertThat(work.renameReadiness().gate3Latched()).isTrue();
            release.countDown();
            rename.get(10, TimeUnit.SECONDS);
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    /**
     * linda 2026-11-23, ruling 1: the rename's path comes from a fresh read of the latch row inside the window, never the regime flag or the sample.
     * The regime says "not latched" while the row is present at lock time: the rename takes the latched (one-row) path.
     */
    @Test
    void theRenamePathFollowsTheLatchRowReadUnderTheLock_theRegimeSaysNotLatchedButTheRowIsPresent() {
        var window = new RenameWindow();
        var reader = new Scripted(window.latchWriter());
        reader.gate3 = false;   // the check latches nothing: the row appears out of band
        var work = work(reader, window);
        work.publish();
        var guard = new dev.nexus.service.db.RenameGuard(Gate3.never(), work.renameReadiness());
        assertThat(guard.<Boolean>pinned(oneRow -> oneRow)).as("control: no row, not latched: the name path").isFalse();

        reader.row.set(true);   // the row is present at lock time

        assertThat(window.oneRowRegime()).as("non-vacuity: the in-process regime says not latched").isFalse();
        assertThat(work.gate3Latched()).as("non-vacuity: so does the accessor").isFalse();
        assertThat(work.latest().gate3Latched()).as("non-vacuity: and the last sample").isFalse();
        // the latched path is the one that can refuse: the scripted sample has no completed drop move, so the one-row regime refuses naming it
        assertThatThrownBy(() -> guard.pinned(oneRow -> oneRow)).as("the rename took the latched path: the row was read under the lock")
            .isInstanceOf(dev.nexus.service.db.RenameRefusedException.class);
        assertThat(work.gate3Latched()).as("and the accessor keeps what was seen").isTrue();
    }

    /** The reverse: the regime and the accessor say latched but the row is absent at lock time: the rename takes the name path. */
    @Test
    void theRenamePathFollowsTheLatchRowReadUnderTheLock_theRegimeSaysLatchedButTheRowIsAbsent() {
        var window = new RenameWindow();
        var reader = new Scripted(window.latchWriter());
        var work = work(reader, window);
        work.publish();
        assertThat(window.oneRowRegime()).as("non-vacuity: the regime says latched").isTrue();
        assertThat(work.gate3Latched()).as("non-vacuity: so does the accessor").isTrue();
        var guard = new dev.nexus.service.db.RenameGuard(Gate3.never(), work.renameReadiness());
        assertThatThrownBy(() -> guard.pinned(oneRow -> oneRow)).as("control: the row is there: the latched path (it refuses: no drop move)")
            .isInstanceOf(dev.nexus.service.db.RenameRefusedException.class);

        reader.row.set(false);   // a restore that lost the row, say

        assertThat(guard.<Boolean>pinned(oneRow -> oneRow)).as("the row is absent at lock time: the name path, whatever the flag says").isFalse();
    }

    /** A row that cannot be read is neither latched nor not latched: the rename fails, it does not guess. */
    @Test
    void aLatchRowThatCannotBeReadFailsTheRename() {
        var window = new RenameWindow();
        var reader = new Scripted(window.latchWriter()) {
            @Override
            public boolean latchRowPresent() {
                throw new IllegalStateException("the database cannot answer");
            }
        };
        var work = work(reader, window);
        var guard = new dev.nexus.service.db.RenameGuard(Gate3.never(), work.renameReadiness());
        assertThatThrownBy(() -> guard.pinned(oneRow -> oneRow)).isInstanceOf(IllegalStateException.class);
    }

    /** A bounded poll for the schedule's thread. */
    private static boolean await(Duration timeout, java.util.function.BooleanSupplier condition) throws InterruptedException {
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
