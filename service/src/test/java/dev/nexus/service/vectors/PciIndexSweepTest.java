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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;
import java.util.function.Supplier;

import static org.assertj.core.api.Assertions.assertThat;

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
        logs.start();
        sweepLog.addAppender(logs);
    }

    @AfterEach
    void cleanUp() {
        sweepLog.detachAppender(logs);
        logs.stop();
        if (sweep != null) {
            sweep.stop();
        }
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
        Thread.sleep(200);                       // let the second caller reach the holder
        releaseFirst.countDown();
        scheduledRead.join(15_000);
        builderRefresh.join(15_000);

        assertThat(calls).hasValue(2);
        assertThat(maxInside).as("reads are serialised").hasValue(1);
        assertThat(sweep.hasValidIndex(M, T, "new")).as("the later read wins").isTrue();
        assertThat(sweep.hasValidIndex(M, T, "old")).isFalse();
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
        int afterStop = reads.get();
        Thread.sleep(300);
        assertThat(reads).as("no read after stop").hasValue(afterStop);
        assertThat(Thread.getAllStackTraces().keySet().stream()
            .anyMatch(t -> t.getName().equals("pci-sweep-read") && t.isAlive()))
            .as("the scheduler thread has ended").isFalse();
        // The set it learned stays readable: stop ends the reads, not the answers.
        assertThat(sweep.hasValidIndex(M, T, "c1")).isTrue();
    }

    @Test
    void stop_beforeStart_isHarmless() {
        sweep = new PciIndexSweep(() -> snapshot(), SETTINGS);
        sweep.stop();
        assertThat(sweep.isRunning()).isFalse();
    }

    @Test
    void aReadThatThrows_doesNotEndTheSchedule() throws Exception {
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
