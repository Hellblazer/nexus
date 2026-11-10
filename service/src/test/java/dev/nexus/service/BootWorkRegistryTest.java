// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.BootWorkRegistry.Entry;
import dev.nexus.service.BootWorkRegistry.Step;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** The boot-work registry (nexus-f3yqd.3): hermetic, a fixed clock, no database. */
class BootWorkRegistryTest {

    private static final Instant T0 = Instant.parse("2026-11-10T08:00:00Z");
    private final BootWorkRegistry registry = new BootWorkRegistry(Clock.fixed(T0, ZoneOffset.UTC));

    private void registerOne(String id) {
        registry.register(id, "building search indexes", 0L, 5L, "indexes", true, "kept");
    }

    @Test
    void anIdleRegistryHasAnEmptySnapshot() {
        assertThat(registry.snapshot()).isEmpty();
    }

    @Test
    void aRegisteredEntryCarriesEveryFieldAndTheClocksStartTime() {
        registry.register("pci-reconcile", "building search indexes", 0L, 3L, "indexes", true, "the index is dropped");

        assertThat(registry.snapshot()).containsExactly(new Entry("pci-reconcile", "building search indexes", T0, 0L,
            3L, "indexes", true, "the index is dropped", null));
    }

    @Test
    void progressAndStepUpdateTheEntry_andANullStepClearsIt() {
        registerOne("a");

        registry.progress("a", 2L, 5L);
        registry.step("a", new Step("c: building index", 10L, 40L, "blocks"));
        var e = registry.snapshot().get(0);
        assertThat(e.done()).isEqualTo(2L);
        assertThat(e.total()).isEqualTo(5L);
        assertThat(e.step()).isEqualTo(new Step("c: building index", 10L, 40L, "blocks"));
        assertThat(e.startedAt()).isEqualTo(T0);

        registry.step("a", null);
        assertThat(registry.snapshot().get(0).step()).isNull();
    }

    @Test
    void finishRemovesTheEntry_andALateUpdateDoesNotBringItBack() {
        registerOne("a");
        registerOne("b");

        registry.finish("a");
        registry.progress("a", 4L, 5L);
        registry.step("a", new Step("late", 1L, 2L, "blocks"));

        assertThat(registry.snapshot()).extracting(Entry::id).containsExactly("b");
        registry.finish("a");   // idempotent
        registry.finish("nobody");
        assertThat(registry.snapshot()).hasSize(1);
    }

    @Test
    void registeringAnIdAgainReplacesTheEntry() {
        registerOne("a");
        registry.progress("a", 3L, 5L);

        registry.register("a", "second", 0L, 1L, "indexes", true, "x");

        assertThat(registry.snapshot()).hasSize(1);
        assertThat(registry.snapshot().get(0).what()).isEqualTo("second");
        assertThat(registry.snapshot().get(0).done()).isEqualTo(0L);
    }

    @Test
    void entriesComeBackInRegistrationOrder() {
        registerOne("z");
        registerOne("a");
        registerOne("m");

        assertThat(registry.snapshot()).extracting(Entry::id).containsExactly("z", "a", "m");
    }

    @Test
    void aSnapshotIsImmutable_andLaterChangesDoNotReachIt() {
        registerOne("a");
        List<Entry> before = registry.snapshot();

        assertThatThrownBy(() -> before.add(before.get(0))).isInstanceOf(UnsupportedOperationException.class);
        assertThatThrownBy(before::clear).isInstanceOf(UnsupportedOperationException.class);

        registry.progress("a", 5L, 5L);
        registerOne("b");
        registry.finish("a");

        assertThat(before).hasSize(1);
        assertThat(before.get(0).done()).isEqualTo(0L);
        assertThat(registry.snapshot()).extracting(Entry::id).containsExactly("b");
    }

    /**
     * Two producers on ONE entry: progress() copies the entry and writes it back with its old step, step() copies it
     * and writes it back with its old counts, so without the registry's lock one write reverts the other's. Each
     * writer only ever counts up, so a reader that takes snapshots all the while sees a value go backward the moment
     * an update is lost, and the end state must hold both writers' last values.
     */
    @Test
    void twoProducersOnOneEntryNeverRevertEachOther_inAnySnapshot() throws Exception {
        int updates = 200_000;
        registerOne("one");
        ExecutorService pool = Executors.newFixedThreadPool(3);
        CountDownLatch go = new CountDownLatch(1);
        AtomicBoolean writersDone = new AtomicBoolean();
        try {
            Future<?> progress = pool.submit(() -> {
                go.await();
                for (long i = 1; i <= updates; i++) {
                    registry.progress("one", i, (long) updates);
                }
                return null;
            });
            Future<?> step = pool.submit(() -> {
                go.await();
                for (long i = 1; i <= updates; i++) {
                    registry.step("one", new Step("s", i, (long) updates, "blocks"));
                }
                return null;
            });
            Future<List<String>> reader = pool.submit(() -> {
                go.await();
                List<String> reverted = new ArrayList<>();
                long lastDone = 0;
                long lastStep = 0;
                long snapshots = 0;
                while (!writersDone.get() && reverted.size() < 5) {
                    Entry e = registry.snapshot().get(0);
                    snapshots++;
                    long d = e.done() == null ? 0 : e.done();
                    long st = e.step() == null ? 0 : e.step().done();
                    if (d < lastDone) {
                        reverted.add("done went " + lastDone + " -> " + d);
                    }
                    if (st < lastStep) {
                        reverted.add("step.done went " + lastStep + " -> " + st);
                    }
                    lastDone = Math.max(lastDone, d);
                    lastStep = Math.max(lastStep, st);
                }
                assertThat(snapshots).as("the reader raced the writers").isPositive();
                return reverted;
            });
            go.countDown();
            progress.get(120, TimeUnit.SECONDS);
            step.get(120, TimeUnit.SECONDS);
            writersDone.set(true);
            assertThat(reader.get(60, TimeUnit.SECONDS)).as("no update was lost under a snapshot").isEmpty();
        } finally {
            pool.shutdownNow();
        }

        Entry end = registry.snapshot().get(0);
        assertThat(end.done()).as("the progress writer's last value survived").isEqualTo((long) updates);
        assertThat(end.step()).as("and the step writer's").isEqualTo(new Step("s", (long) updates, (long) updates,
            "blocks"));
    }

    @Test
    void concurrentRegistersAndFinishesLeaveExactlyTheSurvivors() throws Exception {
        int threads = 8;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            List<Future<?>> futures = new ArrayList<>();
            for (int t = 0; t < threads; t++) {
                int n = t;
                futures.add(pool.submit(() -> {
                    for (int i = 0; i < 200; i++) {
                        registerOne("t" + n + "-" + i);
                        if (i % 2 == 0) {
                            registry.finish("t" + n + "-" + i);
                        }
                    }
                    return null;
                }));
            }
            for (Future<?> f : futures) {
                f.get(60, TimeUnit.SECONDS);
            }
        } finally {
            pool.shutdownNow();
        }

        assertThat(registry.snapshot()).hasSize(threads * 100)
            .allSatisfy(e -> assertThat(Integer.parseInt(e.id().substring(e.id().indexOf('-') + 1)) % 2)
                .isEqualTo(1));
    }
}
