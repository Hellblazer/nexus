// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;

import java.time.Clock;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

import org.junit.jupiter.api.Test;

/**
 * RDR-229 P3.5c assembly (linda 2026-11-23): the value the repositories read, as {@code Main} builds it, never returns to false once
 * gate 3 has latched. The supplier below is {@code Main}'s own shape, line for line
 * ({@code c != null ? c.gate3Latched() : bootLatch.latched()}, pinned on {@code Main}'s text by {@code MainPciWiringTest}), built over a
 * real {@link CompletionBootWork} whose reader a test drives, so the live gate 3 can be made to go false under it.
 * {@code PciIdGate3IntegrationTest} proves the same stickiness one level down, on the reporter against a database; this is the level
 * the readers see.
 */
class Gate3ProductionSignalTest {

    private static CompletionCheck.Result sample(boolean liveGate3, boolean latched, boolean idIndexInvalid) {
        return new CompletionCheck.Result(true, true, liveGate3, List.of(), List.of(), true, 0L, List.of(), List.of(), "",
            new CompletionCheck.Dangling(0L, 0L, Map.of(), 0L, Map.of(), true), false, latched, idIndexInvalid);
    }

    /** Main's production supplier over {@code completion} and the boot latch. */
    private static Gate3 production(AtomicReference<CompletionBootWork> completionRef, Gate3Latch bootLatch) {
        return () -> { var c = completionRef.get(); return c != null ? c.gate3Latched() : bootLatch.latched(); };
    }

    private static final class Driven implements CompletionBootWork.Reader {
        volatile Supplier<CompletionCheck.Result> next;

        @Override
        public CompletionCheck.Result read() {
            return next.get();
        }

        @Override
        public void abort() { }

        @Override
        public void close() { }
    }

    @Test
    void onceLatched_theSignalStaysTrueWhileTheLiveGate3GoesFalse_andNeverReadsTheLiveValue() {
        var completionRef = new AtomicReference<CompletionBootWork>();
        Gate3 signal = production(completionRef, Gate3Latch.of(false, false));
        var reader = new Driven();
        var work = new CompletionBootWork(new BootWorkRegistry(Clock.systemUTC()), reader);

        assertThat(signal.holds()).as("before the completion check exists the boot latch answers: not latched").isFalse();
        completionRef.set(work);
        assertThat(signal.holds()).as("an install that has not latched is not latched").isFalse();

        reader.next = () -> sample(true, false, false);
        work.publish();
        assertThat(work.latest().gate3()).as("non-vacuity: the live gate 3 holds").isTrue();
        assertThat(signal.holds()).as("the signal does not follow the live gate 3: only the latch moves it").isFalse();

        reader.next = () -> sample(true, true, false);
        work.publish();
        assertThat(signal.holds()).as("the latch is read: latched").isTrue();

        reader.next = () -> sample(false, true, true);   // an id index went INVALID: the live gate 3 is false, the latch is not
        work.publish();
        assertThat(work.latest().gate3()).as("non-vacuity: the live gate 3 is now false").isFalse();
        assertThat(work.latest().idIndexInvalid()).isTrue();
        assertThat(signal.holds()).as("the signal stays latched across a live gate 3 that went false").isTrue();

        reader.next = () -> sample(false, false, true);   // even a sample that does not say latched cannot unlatch it
        work.publish();
        assertThat(signal.holds()).as("monotone: a later sample never returns it to false").isTrue();
    }

    @Test
    void anInstallThatLatchedBeforeThisBoot_answersTrueFromTheBootLatch_beforeAndAfterTheCheckExists_withNoSampleRead() {
        var boot = Gate3Latch.of(true, false);
        var completionRef = new AtomicReference<CompletionBootWork>();
        Gate3 signal = production(completionRef, boot);
        assertThat(signal.holds()).as("before the check exists: the boot latch").isTrue();

        // the production factory: the check is seeded from the boot latch, and it has read nothing from the database (the address is nowhere)
        var work = CompletionBootWork.atBoot(new BootWorkRegistry(Clock.systemUTC()),
            new dev.nexus.service.db.AdminConnection("jdbc:postgresql://127.0.0.1:1/none", "u", "p"), boot);
        completionRef.set(work);
        assertThat(work.latest()).as("non-vacuity: no sample has been read").isNull();
        assertThat(signal.holds()).as("the check answers from the boot latch it was seeded with, before its first sample").isTrue();
    }
}
