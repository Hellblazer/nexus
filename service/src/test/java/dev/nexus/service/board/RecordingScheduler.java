// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * A scheduler that records each one-shot {@code schedule} call and runs none of them: the task stays in the record
 * for a test to run, and the future it returns is real (not done, cancellable), so {@link AckDeadlineTimer} keeps its
 * armed state as it does on the engine's scheduler. The periodic sweeps use other methods and are not recorded.
 */
final class RecordingScheduler extends ScheduledThreadPoolExecutor {

    record Scheduled(Runnable task, long delay, TimeUnit unit) {
        long millis() {
            return unit.toMillis(delay);
        }
    }

    private final List<Scheduled> calls = new CopyOnWriteArrayList<>();
    private final List<ScheduledFuture<?>> futures = new CopyOnWriteArrayList<>();

    RecordingScheduler() {
        super(1);
    }

    @Override
    public ScheduledFuture<?> schedule(Runnable command, long delay, TimeUnit unit) {
        calls.add(new Scheduled(command, delay, unit));
        ScheduledFuture<?> f = super.schedule(() -> { }, 365, TimeUnit.DAYS);
        futures.add(f);
        return f;
    }

    /** The future handed back for {@code call}, to see whether it was cancelled. */
    ScheduledFuture<?> futureOf(Scheduled call) {
        return futures.get(calls.indexOf(call));
    }

    /** What {@link AckDeadlineTimer#arm} scheduled: it counts in milliseconds. */
    List<Scheduled> deadlines() {
        return calls.stream().filter(s -> s.unit() == TimeUnit.MILLISECONDS).toList();
    }

    /** The restart rebuild {@link BoardOwnerWiring#start} schedules, or null. */
    Scheduled rebuild() {
        return calls.stream()
            .filter(s -> s.unit() == TimeUnit.SECONDS && s.delay() == BoardOwnerWiring.REBUILD_DELAY_SECONDS)
            .findFirst().orElse(null);
    }
}
