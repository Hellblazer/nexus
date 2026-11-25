// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executor;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), item (B): {@link CompletionBootWork} re-reads on a worker's move completion and at the end of
 * each reconciler pass, and on a slow 60 s safety poll in case an event is missed. Hermetic: a fixed clock the test moves, a
 * direct executor (or a queue the test drains), no sleeps and no database.
 */
class CompletionBootWorkRefreshTest {

    /** A clock the test moves. */
    private static final class Moving extends Clock {
        private Instant now = Instant.parse("2026-11-21T12:00:00Z");

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override
        public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }

    private static final class Reads implements CompletionBootWork.Reader {
        final AtomicInteger reads = new AtomicInteger();
        volatile boolean gate1;

        @Override
        public CompletionCheck.Result read() {
            reads.incrementAndGet();
            return new CompletionCheck.Result(gate1, false, false, List.of(), List.of(), true, 0L, List.of(), List.of(), "no reader");
        }

        @Override
        public void abort() { }

        @Override
        public void close() { }
    }

    private static String summary(CompletionBootWork work) {
        try {
            return new com.fasterxml.jackson.databind.ObjectMapper().readTree(work.statusJson()).get("summary").asText();
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    private static final Executor DIRECT = Runnable::run;

    @Test
    void aMoveCompletionReadsAgainAtOnce_andTheEntryShowsWhatTheReadFound() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var reader = new Reads();
        var work = new CompletionBootWork(registry, reader, new Moving(), DIRECT);
        work.publish();
        assertThat(reader.reads.get()).isEqualTo(1);
        assertThat(summary(work)).contains("gate 1 waits");

        reader.gate1 = true;   // the stamp the worker just completed lets gate 1 hold
        work.onMoveCompleted();

        assertThat(reader.reads.get()).as("one more read, caused by the event alone").isEqualTo(2);
        assertThat(summary(work)).contains("gate 1 holds");
    }

    @Test
    void aReconcilerPassEndReadsAgainAtOnce() {
        var registry = new BootWorkRegistry(Clock.systemUTC());
        var reader = new Reads();
        var work = new CompletionBootWork(registry, reader, new Moving(), DIRECT);
        work.publish();

        work.onReconcilerPassEnded();

        assertThat(reader.reads.get()).isEqualTo(2);
    }

    @Test
    void aBurstOfEventsQueuedBehindOneReadIsOneRead() {
        var queued = new ArrayList<Runnable>();
        var reader = new Reads();
        var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, new Moving(), queued::add);

        work.onMoveCompleted();
        work.onMoveCompleted();
        work.onReconcilerPassEnded();
        assertThat(queued).as("each event asked for a read").hasSize(3);
        assertThat(reader.reads.get()).as("none ran yet: the caller was not made to wait").isZero();
        queued.forEach(Runnable::run);

        assertThat(reader.reads.get()).as("the first one read for all three").isEqualTo(1);
    }

    @Test
    void anEventBeforeStartReadsNothing_theFirstSampleCoversIt() {
        var reader = new Reads();
        var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, new Moving(), null);

        work.onMoveCompleted();
        work.onReconcilerPassEnded();

        assertThat(reader.reads.get()).isZero();
    }

    @Test
    void theSafetyPollReadsOnlyWhenSixtySecondsHavePassedSinceTheLastRead_whatever_causedIt() {
        var clock = new Moving();
        var reader = new Reads();
        var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, clock, DIRECT);
        assertThat(CompletionBootWork.SAFETY_POLL).as("slow: the events do the work").isEqualTo(Duration.ofSeconds(60));

        work.pollIfDue();
        assertThat(reader.reads.get()).as("the first poll reads: there is no sample yet").isEqualTo(1);

        clock.advance(Duration.ofSeconds(59));
        work.pollIfDue();
        assertThat(reader.reads.get()).as("59 s later: not due").isEqualTo(1);

        clock.advance(Duration.ofSeconds(1));
        work.pollIfDue();
        assertThat(reader.reads.get()).as("60 s after the last read: due").isEqualTo(2);

        clock.advance(Duration.ofSeconds(59));
        work.onMoveCompleted();   // an event's read restarts the interval
        assertThat(reader.reads.get()).isEqualTo(3);
        clock.advance(Duration.ofSeconds(59));
        work.pollIfDue();
        assertThat(reader.reads.get()).as("59 s after the event's read: not due").isEqualTo(3);
        clock.advance(Duration.ofSeconds(1));
        work.pollIfDue();
        assertThat(reader.reads.get()).as("60 s after it: due").isEqualTo(4);
    }

    @Test
    void anEventAfterCloseReadsNothing() {
        var reader = new Reads();
        var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader, new Moving(), DIRECT);
        work.close();

        work.onMoveCompleted();

        assertThat(reader.reads.get()).isZero();
    }

    @Test
    void aTableWithTwoCheckedColumnsIsOneValidateRow_withTheSumOfItsOrphans_andEveryConstraintNamed() {
        var merged = CompletionBootWork.byTable(List.of(
            new CompletionCheck.Violators("chash_remap", "chash_remap_source_collection_id_check", 0L, 1L, true),
            new CompletionCheck.Violators("chash_remap", "chash_remap_target_collection_id_check", 1L, 0L, true),
            new CompletionCheck.Violators("catalog_documents", "catalog_documents_physical_collection_id_check", 2L, 0L, true)));

        assertThat(merged).hasSize(2);
        assertThat(merged.get(0).table()).isEqualTo("chash_remap");
        assertThat(merged.get(0).orphans()).isEqualTo(1L);
        assertThat(merged.get(0).unstamped()).isEqualTo(1L);
        assertThat(merged.get(0).constraints()).containsExactly("chash_remap_source_collection_id_check",
            "chash_remap_target_collection_id_check");
        assertThat(merged.get(1).orphans()).isEqualTo(2L);
    }
}
