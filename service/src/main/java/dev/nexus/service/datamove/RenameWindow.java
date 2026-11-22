// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import java.time.Duration;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantReadWriteLock;
import java.util.function.Supplier;

/**
 * RDR-229 P3.7 (O-5): the window in which a collection rename chooses its path and commits, and the one place gate 3's latch takes effect
 * for renames. A rename runs inside {@link #pinned} (read side). The latch row is written through {@link #latchWriter()} (write side):
 * the writer waits, bounded, for every pinned rename to return, writes the durable row, then flips {@link #oneRowRegime()} with one volatile
 * write and nothing that can throw in between, all inside the lock. So the durable latch and the in-process one-row regime begin together, and
 * no rename commits a name-path rewrite after the regime switched.
 *
 * <p>The regime read is {@link #oneRowRegime()} OR the durable latch ({@link CompletionBootWork#gate3Latched()}), both read on every check
 * ({@link CompletionBootWork#renameReadiness()}), so a flip that was lost (the latch's own write succeeded and something after it failed, which
 * P3.4 does not retry) still reads as latched from the row. After a restart the regime is the row alone.
 *
 * <p>The writer that cannot get the lock within {@link #wait} throws: P3.4's check then leaves the latch unwritten and unreported, and
 * the next evaluation tries again; {@link #onBusy} lets the completion check ask for that evaluation soon rather than at the safety poll.
 */
public final class RenameWindow {

    /** How long the latch waits for the pinned renames before it defers to the next evaluation. */
    public static final Duration WAIT = Duration.ofSeconds(30);

    private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger(RenameWindow.class);

    private final ReentrantReadWriteLock lock = new ReentrantReadWriteLock(true);
    private volatile boolean regime;
    private volatile Duration wait = WAIT;
    private volatile Runnable onBusy = () -> { };

    /** Whether the one-row regime has been switched on in this process (by the latch writer). Not the durable latch; see the class comment. */
    public boolean oneRowRegime() {
        return regime;
    }

    /** Runs {@code rename} inside the window: the latch cannot be written, and the regime cannot switch, until it returns. */
    public <T> T pinned(Supplier<T> rename) {
        lock.readLock().lock();
        try {
            return rename.get();
        } finally {
            lock.readLock().unlock();
        }
    }

    /**
     * The writer to hand to {@link CompletionBootWork} ({@code atBoot} or the constructor): takes the write lock (bounded by {@link #wait}), runs
     * the row write, then switches the regime, in that order, and releases the lock in {@code finally}. A timeout, or an interrupt while waiting,
     * throws {@link IllegalStateException}; {@code writeRow} was not run, so nothing is latched and the next evaluation retries.
     */
    public CompletionCheck.Gate3LatchWriter latchWriter() {
        return writeRow -> {
            boolean locked;
            try {
                locked = lock.writeLock().tryLock(wait.toMillis(), TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                locked = false;
            }
            if (!locked) {
                log.warn("event=rdr229_rename_window_busy waited_ms={} cause=\"a pinned rename is still open; the gate 3 latch is deferred\"",
                    wait.toMillis());
                onBusy.run();
                throw new IllegalStateException("a collection rename is still open: the gate 3 latch is deferred");
            }
            try {
                writeRow.run();
                regime = true;   // one volatile write straight after the row: nothing that can throw sits between them
            } finally {
                lock.writeLock().unlock();
            }
        };
    }

    /** Test seam: how long the latch writer waits for a pinned rename. */
    void wait(Duration wait) {
        this.wait = Objects.requireNonNull(wait, "wait");
    }

    /** What to run when the latch writer gave up waiting; the completion check sets it to ask for a read soon. */
    void onBusy(Runnable onBusy) {
        this.onBusy = Objects.requireNonNull(onBusy, "onBusy");
    }
}
