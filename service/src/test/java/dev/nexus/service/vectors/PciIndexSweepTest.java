// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.db.PgSession.PciSettings;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-227 Step 2 (nexus-43ulx.12): the read half's holder, with no database. Every {@link PciCatalog.Snapshot}
 * here is built in memory, which is itself the contract test that the router's question needs no JDBC.
 */
class PciIndexSweepTest {

    static final String M = "minilm-l6-v2-384";
    static final String T = "sweep-tenant";
    static final String HASH_A = "pci_" + "a".repeat(24);
    static final String HASH_B = "pci_" + "b".repeat(24);
    static final PciSettings SETTINGS = new PciSettings(true, 20_000, 600, 16);

    private ListAppender<ILoggingEvent> logs;
    private Logger sweepLog;
    private PciIndexSweep sweep;

    @BeforeEach
    void captureLogs() {
        sweepLog = (Logger) LoggerFactory.getLogger(PciIndexSweep.class);
        logs = new ListAppender<>();
        // The scheduler thread appends while the test thread streams the list; the default ArrayList throws CME.
        logs.list = new java.util.concurrent.CopyOnWriteArrayList<>();
        logs.start();
        sweepLog.addAppender(logs);
    }

    @AfterEach
    void cleanUp() throws InterruptedException {
        sweepLog.detachAppender(logs);
        logs.stop();
        if (sweep != null) {
            sweep.stop();
        }
        // stop() does not wait for its thread; the next test counts live scheduler threads, so this one's must be gone.
        await(() -> Thread.getAllStackTraces().keySet().stream()
            .noneMatch(t -> t.getName().equals("pci-sweep-read") && t.isAlive()), "the scheduler thread to end");
    }

    private List<String> lines(String event) {
        return logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.contains("event=" + event + " ") || m.endsWith("event=" + event)).toList();
    }

    private static PciCatalog.Index index(String name, boolean valid, String collection) {
        return new PciCatalog.Index(name, valid, collection);
    }

    private static PciCatalog.Snapshot snapshot(PciCatalog.Index... indexes) {
        return new PciCatalog.Snapshot(List.of(
            new PciCatalog.Leaf("nexus", "chunks_leaf", M, T, List.of(indexes)),
            new PciCatalog.Leaf("nexus", "chunks_other", M, "other-tenant", List.of())));
    }

    private static void await(BooleanSupplier condition, String what) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out waiting for " + what);
            }
            Thread.sleep(10);
        }
    }

    @Test
    void beforeTheFirstRead_theSetKnowsNoIndex_andStatusSaysNeverRead() {
        sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, true, "c1")), SETTINGS);

        assertThat(sweep.hasValidIndex(M, T, "c1")).as("empty until the first read").isFalse();
        PciIndexSweep.Status s = sweep.status();
        assertThat(s.everRead()).isFalse();
        assertThat(s.lastReadAt()).isNull();
        assertThat(s.lastFailureAt()).isNull();
        assertThat(s.valid()).isZero();
        assertThat(s.invalid()).isZero();
        assertThat(s.unparsed()).isZero();
    }

    @Test
    void hasValidIndex_answersFromMemory_theReaderIsNotCalled() {
        AtomicInteger reads = new AtomicInteger();
        sweep = new PciIndexSweep(() -> {
            reads.incrementAndGet();
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS);
        assertThat(sweep.refresh()).isTrue();
        assertThat(reads).hasValue(1);

        for (int i = 0; i < 1_000; i++) {
            assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
            assertThat(sweep.hasValidIndex(M, T, "other")).isFalse();
        }
        assertThat(reads).as("1000 questions, no read").hasValue(1);
    }

    @Test
    void aSuccessfulRead_replacesTheSet_keepsTheCountsAndTheReadTime_andLogsPciSweep() {
        Instant now = Instant.parse("2026-10-09T12:00:00Z");
        Supplier<PciCatalog.Snapshot> reader = () -> snapshot(
            index(HASH_A, true, "c1"), index(HASH_B, false, "c2"), index("pci_foo", true, null));
        sweep = new PciIndexSweep(reader, SETTINGS, Clock.fixed(now, ZoneOffset.UTC));

        assertThat(sweep.refresh()).isTrue();

        assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
        assertThat(sweep.hasValidIndex(M, "other-tenant", "c1")).as("keyed on the tenant").isFalse();
        assertThat(sweep.hasValidIndex("bge", T, "c1")).as("keyed on the model").isFalse();
        PciIndexSweep.Status s = sweep.status();
        assertThat(s.everRead()).isTrue();
        assertThat(s.valid()).isEqualTo(1);
        assertThat(s.invalid()).isEqualTo(1);
        assertThat(s.unparsed()).isEqualTo(1);
        assertThat(s.lastReadAt()).isEqualTo(now);
        assertThat(lines("pci_sweep")).containsExactly("event=pci_sweep valid=1 invalid=1 unparsed=1");
    }

    @Test
    void anInvalidIndex_isNotInTheSet() {
        sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, false, "c1")), SETTINGS);
        sweep.refresh();
        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        assertThat(sweep.status().invalid()).isEqualTo(1);
    }

    @Test
    void anUnparsedIndex_isNotInTheSet() {
        sweep = new PciIndexSweep(() -> snapshot(index("pci_foo", true, null)), SETTINGS);
        sweep.refresh();
        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        assertThat(sweep.status().unparsed()).isEqualTo(1);
    }

    @Test
    void aLaterRead_replacesTheWholeSet_aDroppedIndexLeavesIt() {
        List<PciCatalog.Snapshot> reads = List.of(
            snapshot(index(HASH_A, true, "c1")), snapshot(index(HASH_B, true, "c2")));
        AtomicInteger next = new AtomicInteger();
        sweep = new PciIndexSweep(() -> reads.get(next.getAndIncrement()), SETTINGS);

        sweep.refresh();
        assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
        sweep.refresh();
        assertThat(sweep.hasValidIndex(M, T, "c1")).as("dropped").isFalse();
        assertThat(sweep.hasValidIndex(M, T, "c2")).isTrue();
    }

    @Test
    void aFailedRead_keepsThePreviousSet_logsTheEvent_andMarksTheFailure() {
        Instant t0 = Instant.parse("2026-10-09T12:00:00Z");
        var clock = new MutableClock(t0);
        AtomicInteger n = new AtomicInteger();
        sweep = new PciIndexSweep(() -> {
            if (n.getAndIncrement() == 0) {
                return snapshot(index(HASH_A, true, "c1"));
            }
            throw new IllegalStateException("connection refused");
        }, SETTINGS, clock);
        sweep.refresh();
        clock.set(t0.plusSeconds(600));

        assertThat(sweep.refresh()).as("a failed read replaces nothing").isFalse();

        assertThat(sweep.hasValidIndex(M, T, "c1")).as("the previous set stays").isTrue();
        PciIndexSweep.Status s = sweep.status();
        assertThat(s.valid()).isEqualTo(1);
        assertThat(s.lastReadAt()).as("last SUCCESSFUL read").isEqualTo(t0);
        assertThat(s.lastFailureAt()).isEqualTo(t0.plusSeconds(600));
        List<String> failed = lines("pci_sweep_read_failed");
        assertThat(failed).hasSize(1);
        assertThat(failed.get(0)).contains("connection refused");
    }

    @Test
    void aFailedFirstRead_leavesTheSetEmpty_andNeverRead() {
        sweep = new PciIndexSweep(() -> {
            throw new IllegalStateException("boom");
        }, SETTINGS);

        assertThat(sweep.refresh()).isFalse();

        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        assertThat(sweep.status().everRead()).isFalse();
        assertThat(sweep.status().lastFailureAt()).isNotNull();
        assertThat(lines("pci_sweep_read_failed")).hasSize(1);
        assertThat(lines("pci_sweep")).isEmpty();
    }

    @Test
    void aSnapshotWithNoLeaves_isAReadFailure_neverEvidenceOfNoIndexes() {
        AtomicInteger n = new AtomicInteger();
        sweep = new PciIndexSweep(() -> n.getAndIncrement() == 0
            ? snapshot(index(HASH_A, true, "c1"))
            : new PciCatalog.Snapshot(List.of()), SETTINGS);
        sweep.refresh();

        assertThat(sweep.refresh()).isFalse();

        assertThat(sweep.hasValidIndex(M, T, "c1")).as("an empty read does not clear the set").isTrue();
        assertThat(sweep.status().valid()).isEqualTo(1);
        assertThat(lines("pci_sweep_read_failed")).hasSize(1);
        assertThat(lines("pci_sweep")).as("only the good read logged a sweep").hasSize(1);
    }

    /**
     * refresh() is public for the builder and runs while the scheduled read may be mid-statement. A read that
     * started EARLIER must not land AFTER a read that started later: the builder's refresh, called once its
     * index is committed, would be overwritten by a statement that began before that commit.
     */
    @Test
    void refreshConcurrentWithAnotherRead_neverLeavesTheOlderSnapshot() throws Exception {
        CountDownLatch firstInside = new CountDownLatch(1);
        CountDownLatch releaseFirst = new CountDownLatch(1);
        AtomicInteger calls = new AtomicInteger();
        AtomicInteger inside = new AtomicInteger();
        AtomicInteger maxInside = new AtomicInteger();
        sweep = new PciIndexSweep(() -> {
            int call = calls.incrementAndGet();
            int now = inside.incrementAndGet();
            maxInside.accumulateAndGet(now, Math::max);
            try {
                if (call == 1) {
                    firstInside.countDown();
                    releaseFirst.await(30, TimeUnit.SECONDS);
                    return snapshot(index(HASH_A, true, "old"));   // read before the index was committed
                }
                return snapshot(index(HASH_B, true, "new"));         // read after
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(e);
            } finally {
                inside.decrementAndGet();
            }
        }, SETTINGS);

        Thread scheduledRead = new Thread(sweep::refresh, "test-scheduled-read");
        scheduledRead.start();
        assertThat(firstInside.await(15, TimeUnit.SECONDS)).isTrue();
        Thread builderRefresh = new Thread(sweep::refresh, "test-builder-refresh");
        builderRefresh.start();
        // The second caller is parked on the read lock (tryLock with a timeout) when its thread reports
        // TIMED_WAITING; it calls the reader only after it holds the lock, so no other wait can put it there.
        await(() -> builderRefresh.getState() == Thread.State.TIMED_WAITING, "the second caller to wait for the lock");

        // The search path answers while a read is in flight and another caller waits: it never takes the lock.
        assertThat(CompletableFuture.supplyAsync(() -> sweep.hasValidIndex(M, T, "old")).get(5, TimeUnit.SECONDS))
            .as("no set has been installed yet").isFalse();

        releaseFirst.countDown();
        scheduledRead.join(15_000);
        builderRefresh.join(15_000);

        assertThat(calls).hasValue(2);
        assertThat(maxInside).as("reads are serialised").hasValue(1);
        assertThat(sweep.hasValidIndex(M, T, "new")).as("the later read wins").isTrue();
        assertThat(sweep.hasValidIndex(M, T, "old")).isFalse();
    }

    /**
     * A caller that cannot get the read lock within the read bound gives up instead of stalling: the builder calls
     * refresh() while holding its advisory lock, and a read ahead of it can be stuck.
     */
    @Test
    void refreshThatCannotGetTheLockWithinTheReadBound_returnsFalse_andLeavesTheSetAlone() throws Exception {
        CountDownLatch firstInside = new CountDownLatch(1);
        CountDownLatch releaseFirst = new CountDownLatch(1);
        AtomicInteger calls = new AtomicInteger();
        sweep = new PciIndexSweep(() -> {
            if (calls.incrementAndGet() == 1) {
                firstInside.countDown();
                try {
                    releaseFirst.await(60, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS, Clock.systemUTC(), Duration.ofMillis(200));

        Thread stuck = new Thread(sweep::refresh, "test-stuck-read");
        stuck.start();
        assertThat(firstInside.await(15, TimeUnit.SECONDS)).isTrue();

        try {
            boolean replaced = CompletableFuture.supplyAsync(sweep::refresh).get(10, TimeUnit.SECONDS);

            assertThat(replaced).as("no turn within the bound, so no read").isFalse();
            assertThat(calls).as("the skipped caller never reached the reader").hasValue(1);
            assertThat(lines("pci_sweep_refresh_skipped")).hasSize(1);
            assertThat(sweep.status().consecutiveFailures()).as("a skipped refresh is not a failed read").isZero();
        } finally {
            releaseFirst.countDown();
            stuck.join(15_000);
        }
        assertThat(sweep.refresh()).as("the lock is free again once the stuck read ends").isTrue();
    }

    @Test
    void start_readsOnceAtOnce_thenEveryPeriod() throws Exception {
        AtomicInteger reads = new AtomicInteger();
        sweep = PciIndexSweep.withPeriod(() -> {
            reads.incrementAndGet();
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS, Duration.ofMillis(50));

        assertThat(sweep.isRunning()).isFalse();
        sweep.start();
        assertThat(sweep.isRunning()).isTrue();

        await(() -> reads.get() >= 4, "four scheduled reads");
        assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
    }

    @Test
    void start_isIdempotent_oneTaskOneThread() throws Exception {
        sweep = PciIndexSweep.withPeriod(() -> snapshot(index(HASH_A, true, "c1")), SETTINGS, Duration.ofMillis(50));
        sweep.start();
        sweep.start();
        await(() -> sweep.status().everRead(), "first read");
        assertThat(Thread.getAllStackTraces().keySet().stream()
            .filter(t -> t.getName().equals("pci-sweep-read") && t.isAlive()).count())
            .as("one scheduler thread").isEqualTo(1);
    }

    @Test
    void stop_endsTheTask_noReadAfterIt_andTheThreadIsGone() throws Exception {
        AtomicInteger reads = new AtomicInteger();
        sweep = PciIndexSweep.withPeriod(() -> {
            reads.incrementAndGet();
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS, Duration.ofMillis(20));
        sweep.start();
        await(() -> reads.get() >= 3, "three reads");

        sweep.stop();

        assertThat(sweep.isRunning()).isFalse();
        // stop() does not wait, so the thread ends shortly after it returns.
        await(() -> Thread.getAllStackTraces().keySet().stream()
            .noneMatch(t -> t.getName().equals("pci-sweep-read") && t.isAlive()), "the scheduler thread to end");
        int afterStop = reads.get();
        Thread.sleep(300);
        assertThat(reads).as("no read after stop").hasValue(afterStop);
        // The set it learned stays readable: stop ends the reads, not the answers.
        assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
    }

    @Test
    void stop_doesNotWaitForAReadThatIgnoresTheInterrupt() throws Exception {
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        sweep = PciIndexSweep.withPeriod(() -> {
            inside.countDown();
            // A JDBC read on a silent socket: the interrupt does not reach it.
            while (true) {
                try {
                    release.await();
                    break;
                } catch (InterruptedException ignored) {
                    // keep waiting
                }
            }
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS, Duration.ofMillis(20));
        sweep.start();
        assertThat(inside.await(15, TimeUnit.SECONDS)).isTrue();

        long began = System.nanoTime();
        sweep.stop();
        long tookMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - began);
        release.countDown();

        assertThat(tookMs).as("stop() returns at once, whatever the read is doing").isLessThan(1_000);
    }

    /** The interrupt the shutdown hook delivers is not an incident: DEBUG, never WARN. */
    @Test
    void aReadInterruptedByStop_isLoggedAtDebug_notWarn() throws Exception {
        var previous = sweepLog.getLevel();
        sweepLog.setLevel(Level.DEBUG);
        try {
            CountDownLatch inside = new CountDownLatch(1);
            sweep = PciIndexSweep.withPeriod(() -> {
                inside.countDown();
                try {
                    new CountDownLatch(1).await();
                } catch (InterruptedException e) {
                    throw new IllegalStateException("interrupted", e);
                }
                return snapshot();
            }, SETTINGS, Duration.ofMillis(20));
            sweep.start();
            assertThat(inside.await(15, TimeUnit.SECONDS)).isTrue();

            sweep.stop();

            await(() -> lines("pci_sweep_read_failed").size() == 1, "the interrupted read to be logged");
            ILoggingEvent failed = logs.list.stream()
                .filter(e -> e.getFormattedMessage().contains("event=pci_sweep_read_failed")).findFirst().orElseThrow();
            assertThat(failed.getLevel()).isEqualTo(Level.DEBUG);
            assertThat(failed.getFormattedMessage()).contains("during=shutdown");
            assertThat(logs.list.stream().filter(e -> e.getLevel().isGreaterOrEqual(Level.WARN))).isEmpty();
        } finally {
            sweepLog.setLevel(previous);
        }
    }

    @Test
    void start_afterStop_throws_andDoesNotRestart() {
        sweep = new PciIndexSweep(() -> snapshot(), SETTINGS);
        sweep.stop();

        assertThatThrownBy(sweep::start).isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("does not restart");
        assertThat(sweep.isRunning()).isFalse();
    }

    @Test
    void start_afterAStartedSweepWasStopped_throws() throws Exception {
        sweep = PciIndexSweep.withPeriod(() -> snapshot(index(HASH_A, true, "c1")), SETTINGS, Duration.ofMillis(20));
        sweep.start();
        await(() -> sweep.status().everRead(), "first read");
        sweep.stop();

        assertThatThrownBy(sweep::start).isInstanceOf(IllegalStateException.class);
        assertThat(sweep.isRunning()).isFalse();
    }

    @Test
    void stop_beforeStart_isHarmless() {
        sweep = new PciIndexSweep(() -> snapshot(), SETTINGS);
        sweep.stop();
        assertThat(sweep.isRunning()).isFalse();
    }

    /**
     * refresh() catches RuntimeException itself, so only an Error from the reader reaches scheduledRead's catch. The
     * executor never reschedules a task that threw, so that catch is what keeps the schedule alive.
     */
    @Test
    void aReadThatThrowsAnError_doesNotEndTheSchedule() throws Exception {
        AtomicInteger n = new AtomicInteger();
        sweep = PciIndexSweep.withPeriod(() -> {
            if (n.incrementAndGet() <= 2) {
                throw new AssertionError("an Error, not an Exception");
            }
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS, Duration.ofMillis(20));
        sweep.start();

        await(() -> sweep.hasValidIndex(M, T, "c1"), "the read after two Errors");
        assertThat(lines("pci_sweep_read_task_error")).hasSizeGreaterThanOrEqualTo(2);
        assertThat(sweep.refresh()).as("the lock was released on the Error path").isTrue();
    }

    @Test
    void aReadThatThrowsAnException_doesNotEndTheSchedule() throws Exception {
        AtomicInteger n = new AtomicInteger();
        sweep = PciIndexSweep.withPeriod(() -> {
            if (n.incrementAndGet() <= 2) {
                throw new IllegalStateException("transient");
            }
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS, Duration.ofMillis(20));
        sweep.start();

        await(() -> sweep.hasValidIndex(M, T, "c1"), "the read after two failures");
        assertThat(lines("pci_sweep_read_failed")).hasSizeGreaterThanOrEqualTo(2);
        assertThat(sweep.status().consecutiveFailures()).as("a success resets the run").isZero();
    }

    /**
     * NX_SEARCH_PCI=0 switches the DDL half off; the read half still serves existing indexes. Built through the
     * production constructor (period 600 s from the settings): the first read is immediate, so no period is waited out.
     */
    @Test
    void aSweepBuiltFromDisabledSettings_stillStartsAndReads() throws Exception {
        sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, true, "c1")), new PciSettings(false, 20_000, 600, 16));

        sweep.start();

        await(() -> sweep.hasValidIndex(M, T, "c1"), "a read with NX_SEARCH_PCI=0");
        assertThat(sweep.status().everRead()).isTrue();
        assertThat(sweep.period()).isEqualTo(Duration.ofSeconds(600));
    }

    @Test
    void consecutiveFailures_countsTheRun_andASuccessResetsIt_butKeepsWhenTheLastFailureWas() {
        Instant t0 = Instant.parse("2026-10-09T12:00:00Z");
        var clock = new MutableClock(t0);
        AtomicInteger n = new AtomicInteger();
        sweep = new PciIndexSweep(() -> {
            if (n.incrementAndGet() == 3) {
                return snapshot(index(HASH_A, true, "c1"));
            }
            throw new IllegalStateException("down");
        }, SETTINGS, clock);

        sweep.refresh();
        assertThat(sweep.status().consecutiveFailures()).isEqualTo(1);
        clock.set(t0.plusSeconds(10));
        sweep.refresh();
        assertThat(sweep.status().consecutiveFailures()).isEqualTo(2);
        assertThat(sweep.status().lastFailureAt()).isEqualTo(t0.plusSeconds(10));
        clock.set(t0.plusSeconds(20));
        sweep.refresh();
        assertThat(sweep.status().consecutiveFailures()).isZero();
        assertThat(sweep.status().lastReadAt()).isEqualTo(t0.plusSeconds(20));
        assertThat(sweep.status().lastFailureAt())
            .as("a success resets the run, not the record of when the last failure happened")
            .isEqualTo(t0.plusSeconds(10));
    }

    /**
     * An interrupt while waiting for the read lock returns false without a read, logs it, and leaves the thread's
     * interrupt flag set for whoever owns the thread (the builder's, or the scheduler's, which shutdownNow set).
     */
    @Test
    void refreshInterruptedWhileWaitingForTheLock_returnsFalse_logsWarn_andRestoresTheInterruptFlag() {
        AtomicInteger reads = new AtomicInteger();
        sweep = new PciIndexSweep(() -> {
            reads.incrementAndGet();
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS);

        Thread.currentThread().interrupt();
        boolean replaced;
        boolean flagRestored;
        try {
            replaced = sweep.refresh();
        } finally {
            flagRestored = Thread.interrupted();   // also clears it, so this test leaves the thread clean
        }

        assertThat(replaced).isFalse();
        assertThat(flagRestored).as("refresh() puts the interrupt flag back").isTrue();
        assertThat(reads).as("an interrupted caller never reached the reader").hasValue(0);
        List<ILoggingEvent> skipped = logs.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=pci_sweep_refresh_skipped")).toList();
        assertThat(skipped).hasSize(1);
        assertThat(skipped.get(0).getLevel()).isEqualTo(Level.WARN);
        assertThat(skipped.get(0).getFormattedMessage()).contains("reason=interrupted");
    }

    @Test
    void refreshInterruptedDuringShutdown_isLoggedAtDebug_andRestoresTheInterruptFlag() {
        var previous = sweepLog.getLevel();
        sweepLog.setLevel(Level.DEBUG);
        try {
            sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, true, "c1")), SETTINGS);
            sweep.stop();

            Thread.currentThread().interrupt();
            boolean replaced;
            boolean flagRestored;
            try {
                replaced = sweep.refresh();
            } finally {
                flagRestored = Thread.interrupted();
            }

            assertThat(replaced).isFalse();
            assertThat(flagRestored).isTrue();
            List<ILoggingEvent> skipped = logs.list.stream()
                .filter(e -> e.getFormattedMessage().contains("event=pci_sweep_refresh_skipped")).toList();
            assertThat(skipped).hasSize(1);
            assertThat(skipped.get(0).getLevel()).isEqualTo(Level.DEBUG);
            assertThat(skipped.get(0).getFormattedMessage()).contains("during=shutdown");
        } finally {
            sweepLog.setLevel(previous);
        }
    }

    // ---- the set expires -------------------------------------------------------------------------

    @Test
    void theSetExpiresAfterThreePeriodsPlusTheReadBound_logsOnce_andRecoversOnTheNextRead() {
        Instant t0 = Instant.parse("2026-10-09T12:00:00Z");
        var clock = new MutableClock(t0);
        AtomicInteger n = new AtomicInteger();
        sweep = new PciIndexSweep(() -> {
            if (n.incrementAndGet() == 1 || n.get() == 4) {
                return snapshot(index(HASH_A, true, "c1"));
            }
            throw new IllegalStateException("down");
        }, SETTINGS, clock, Duration.ofSeconds(30));          // period 600 s: expires past 3 * 600 + 30 = 1830 s
        sweep.refresh();

        clock.set(t0.plusSeconds(1830));
        assertThat(sweep.hasValidIndex(M, T, "c1")).as("at the bound the set still answers").isTrue();
        assertThat(sweep.status().expired()).isFalse();

        clock.set(t0.plusSeconds(1831));
        assertThat(sweep.hasValidIndex(M, T, "c1")).as("past the bound the set answers as empty").isFalse();
        for (int i = 0; i < 100; i++) {
            assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        }
        assertThat(sweep.status().expired()).isTrue();
        assertThat(sweep.status().valid()).as("the counts are the last read's").isEqualTo(1);
        assertThat(lines("pci_sweep_set_expired")).as("logged once, not per question").hasSize(1);

        assertThat(sweep.refresh()).isFalse();                  // still failing, still expired
        assertThat(sweep.refresh()).isFalse();
        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        assertThat(sweep.status().consecutiveFailures()).isEqualTo(2);
        assertThat(lines("pci_sweep_set_expired")).as("a failed read does not re-log it").hasSize(1);
        assertThat(lines("pci_sweep_set_recovered")).isEmpty();

        assertThat(sweep.refresh()).isTrue();                   // the read succeeds again
        assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
        assertThat(sweep.status().expired()).isFalse();
        assertThat(sweep.status().consecutiveFailures()).isZero();
        assertThat(lines("pci_sweep_set_recovered")).hasSize(1);
    }

    @Test
    void aSecondExpiryLogsAgain() {
        Instant t0 = Instant.parse("2026-10-09T12:00:00Z");
        var clock = new MutableClock(t0);
        sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, true, "c1")), SETTINGS, clock, Duration.ofSeconds(30));
        sweep.refresh();
        clock.set(t0.plusSeconds(5_000));
        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        sweep.refresh();
        assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
        clock.set(t0.plusSeconds(10_000));
        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();

        assertThat(lines("pci_sweep_set_expired")).hasSize(2);
    }

    @Test
    void aSetThatWasNeverRead_isNotExpired() {
        sweep = new PciIndexSweep(() -> snapshot(), SETTINGS, new MutableClock(Instant.parse("2030-01-01T00:00:00Z")),
            Duration.ofSeconds(30));
        assertThat(sweep.status().expired()).isFalse();
        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        assertThat(lines("pci_sweep_set_expired")).isEmpty();
    }

    // ---- unparsed-only reads ---------------------------------------------------------------------

    @Test
    void aReadWithUnparsedIndexesAndNoValidOne_logsAtWarn() {
        sweep = new PciIndexSweep(() -> snapshot(index("pci_foo", true, null), index("pci_bar_ccnew", false, null)),
            SETTINGS);

        assertThat(sweep.refresh()).isTrue();

        ILoggingEvent event = logs.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=pci_sweep ")).findFirst().orElseThrow();
        assertThat(event.getLevel()).isEqualTo(Level.WARN);
        assertThat(event.getFormattedMessage()).contains("valid=0", "unparsed=2");
    }

    @Test
    void aReadWithAValidIndex_logsAtInfo_evenWithUnparsedOnes() {
        sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, true, "c1"), index("pci_foo", true, null)), SETTINGS);

        sweep.refresh();

        ILoggingEvent event = logs.list.stream()
            .filter(e -> e.getFormattedMessage().contains("event=pci_sweep ")).findFirst().orElseThrow();
        assertThat(event.getLevel()).isEqualTo(Level.INFO);
    }

    // ---- construction ------------------------------------------------------------------------------

    @Test
    void withPeriod_validatesItsArguments() {
        assertThatThrownBy(() -> PciIndexSweep.withPeriod(null, SETTINGS, Duration.ofMillis(5)))
            .isInstanceOf(NullPointerException.class);
        assertThatThrownBy(() -> PciIndexSweep.withPeriod(() -> snapshot(), null, Duration.ofMillis(5)))
            .isInstanceOf(NullPointerException.class);
        assertThatThrownBy(() -> PciIndexSweep.withPeriod(() -> snapshot(), SETTINGS, null))
            .isInstanceOf(NullPointerException.class);
        assertThatThrownBy(() -> PciIndexSweep.withPeriod(() -> snapshot(), SETTINGS, Duration.ZERO))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciIndexSweep.withPeriod(() -> snapshot(), SETTINGS, Duration.ofMillis(-1)))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void theReadBoundMustBePositive() {
        Instant now = Instant.parse("2026-10-09T12:00:00Z");
        assertThatThrownBy(() -> new PciIndexSweep(() -> snapshot(), SETTINGS, Clock.fixed(now, ZoneOffset.UTC),
            Duration.ZERO)).isInstanceOf(IllegalArgumentException.class).hasMessageContaining("readBound");
        assertThatThrownBy(() -> new PciIndexSweep(() -> snapshot(), SETTINGS, Clock.fixed(now, ZoneOffset.UTC),
            Duration.ofMillis(-1))).isInstanceOf(IllegalArgumentException.class).hasMessageContaining("readBound");
        assertThatThrownBy(() -> new PciIndexSweep(() -> snapshot(), SETTINGS, Clock.fixed(now, ZoneOffset.UTC),
            null)).isInstanceOf(NullPointerException.class);
    }

    @Test
    void thePeriodComesFromTheSettings() {
        assertThat(new PciIndexSweep(() -> snapshot(), new PciSettings(false, 1, 90, 0)).period())
            .as("the sweep runs whatever NX_SEARCH_PCI says")
            .isEqualTo(Duration.ofSeconds(90));
    }

    @Test
    void sweepLoggerKeepsInfoVisible() {
        // The events above are INFO/WARN; a logger configured above them would make these tests vacuous.
        assertThat(sweepLog.isInfoEnabled()).isTrue();
        assertThat(sweepLog.getEffectiveLevel().isGreaterOrEqual(Level.WARN)).isFalse();
    }

    // ── nexus-43ulx.25 fix round: the index as the set holds it, and a line when the set changes ─────────────────

    @Test
    void validIndex_returnsTheNameTheReadSaw_notAComputedOne() {
        // HASH_A is not PciCatalog.indexName(M, T, "c1"): a pci_ index the sweep admitted under another hash.
        sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, true, "c1")), SETTINGS);
        assertThat(sweep.validIndex(M, T, "c1")).as("nothing before the first read").isEmpty();
        assertThat(sweep.refresh()).isTrue();

        assertThat(sweep.validIndex(M, T, "c1")).get().extracting(PciIndexSet.ValidIndex::name).isEqualTo(HASH_A);
        assertThat(HASH_A).isNotEqualTo(PciCatalog.indexName(M, T, "c1"));
        assertThat(sweep.validIndex(M, T, "other")).isEmpty();
        assertThat(sweep.validIndex(M, "other-tenant", "c1")).isEmpty();
    }

    @Test
    void validIndex_isSinceTheFirstReadOfAnUnbrokenRun_andRestartsWhenTheIndexLeavesAndComesBack() {
        Instant t0 = Instant.parse("2026-10-10T08:00:00Z");
        var clock = new MutableClock(t0);
        var withIndex = new java.util.concurrent.atomic.AtomicBoolean(true);
        sweep = new PciIndexSweep(() -> withIndex.get() ? snapshot(index(HASH_A, true, "c1")) : snapshot(),
            SETTINGS, clock);

        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.validIndex(M, T, "c1")).get().extracting(PciIndexSet.ValidIndex::since).isEqualTo(t0);

        clock.set(t0.plusSeconds(60));
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.validIndex(M, T, "c1")).get().extracting(PciIndexSet.ValidIndex::since)
            .as("still listed: the date of the first read of the run").isEqualTo(t0);

        withIndex.set(false);
        clock.set(t0.plusSeconds(120));
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.validIndex(M, T, "c1")).isEmpty();

        withIndex.set(true);
        clock.set(t0.plusSeconds(180));
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.validIndex(M, T, "c1")).get().extracting(PciIndexSet.ValidIndex::since)
            .as("dropped and rebuilt: a new run").isEqualTo(t0.plusSeconds(180));
    }

    @Test
    void validIndex_answersEmptyOnceTheSetHasExpired_likeHasValidIndex() {
        Instant t0 = Instant.parse("2026-10-10T08:00:00Z");
        var clock = new MutableClock(t0);
        sweep = new PciIndexSweep(() -> snapshot(index(HASH_A, true, "c1")), SETTINGS, clock);
        assertThat(sweep.refresh()).isTrue();
        clock.set(t0.plus(Duration.ofSeconds(3 * 600).plus(PciCatalog.DEFAULT_READ_BOUND)).plusSeconds(1));

        assertThat(sweep.hasValidIndex(M, T, "c1")).isFalse();
        assertThat(sweep.validIndex(M, T, "c1")).isEmpty();
    }

    @Test
    void aChangeOfTheSet_logsOneLineNamingWhatWasAddedAndRemoved_andAnUnchangedSetLogsNothing() {
        var state = new java.util.concurrent.atomic.AtomicReference<>(snapshot(index(HASH_A, true, "c1")));
        sweep = new PciIndexSweep(state::get, SETTINGS);

        assertThat(sweep.refresh()).isTrue();
        assertThat(lines("pci_sweep_set_changed")).singleElement().satisfies(l -> assertThat(l)
            .contains("added_count=1").contains("removed_count=0")
            .contains("added=chunks_leaf:c1:" + HASH_A).contains("removed=-"));

        assertThat(sweep.refresh()).isTrue();
        assertThat(lines("pci_sweep_set_changed")).as("same set, no line").hasSize(1);

        // c1 is dropped, c2 appears, and an invalid index is not part of the router's set at all.
        state.set(snapshot(index(HASH_B, true, "c2"), index("pci_" + "c".repeat(24), false, "c3")));
        assertThat(sweep.refresh()).isTrue();
        assertThat(lines("pci_sweep_set_changed")).hasSize(2).last().satisfies(l -> assertThat(l)
            .contains("added_count=1").contains("removed_count=1")
            .contains("added=chunks_leaf:c2:" + HASH_B).contains("removed=chunks_leaf:c1:" + HASH_A)
            .doesNotContain("c3"));
    }

    @Test
    void aLargeChange_isCutAtTheBound_butTheCountsAreExact() {
        int n = PciIndexSweep.MAX_LOGGED_ENTRIES + 15;
        PciCatalog.Index[] many = new PciCatalog.Index[n];
        for (int i = 0; i < n; i++) {
            many[i] = index(String.format("pci_%024x", i), true, "c" + i);
        }
        sweep = new PciIndexSweep(() -> snapshot(many), SETTINGS);

        assertThat(sweep.refresh()).isTrue();

        assertThat(lines("pci_sweep_set_changed")).singleElement().satisfies(l -> {
            assertThat(l).contains("added_count=" + n).contains(",...+15");
            assertThat(l.split("chunks_leaf:", -1).length - 1).as("entries named").isEqualTo(PciIndexSweep.MAX_LOGGED_ENTRIES);
        });
    }

    @Test
    void aFailedRead_keepsTheSinceDates_andLogsNoChange() {
        Instant t0 = Instant.parse("2026-10-10T08:00:00Z");
        var clock = new MutableClock(t0);
        var fail = new java.util.concurrent.atomic.AtomicBoolean();
        sweep = new PciIndexSweep(() -> {
            if (fail.get()) {
                throw new IllegalStateException("db down");
            }
            return snapshot(index(HASH_A, true, "c1"));
        }, SETTINGS, clock);
        assertThat(sweep.refresh()).isTrue();

        fail.set(true);
        clock.set(t0.plusSeconds(100));
        assertThat(sweep.refresh()).isFalse();
        fail.set(false);
        clock.set(t0.plusSeconds(200));
        assertThat(sweep.refresh()).isTrue();

        assertThat(sweep.validIndex(M, T, "c1")).get().extracting(PciIndexSet.ValidIndex::since).isEqualTo(t0);
        assertThat(lines("pci_sweep_set_changed")).as("the failed read changed nothing").hasSize(1);
    }

    private static final class MutableClock extends Clock {
        private volatile Instant now;

        MutableClock(Instant now) {
            this.now = now;
        }

        void set(Instant t) {
            now = t;
        }

        @Override public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override public Instant instant() {
            return now;
        }
    }
}
