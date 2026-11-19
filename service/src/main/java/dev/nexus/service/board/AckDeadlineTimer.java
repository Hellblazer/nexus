// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * The acknowledgement deadline of an open red (RDR-252): one scheduled task per (tenant, base topic), set to the
 * instant {@link OwnerDecision} says the state's deadline falls due. It decides nothing. When the time comes it calls
 * the hand-off the stamper gave it, which takes the topic lock and re-decides from fresh rows; a deadline that
 * moved, passed or was superseded is the decision's to know, so a task that fires early or late is harmless.
 * {@link #arm} replaces the task of a topic and a null instant cancels it, so the stamper arms after every
 * evaluation and a superseded episode keeps no timer.
 *
 * <p>A task never ends its own schedule: nothing here throws out of the executor's thread, and a hand-off that
 * fails is retried after {@link #RETRY}.
 */
public final class AckDeadlineTimer {

    private static final Logger log = LoggerFactory.getLogger(AckDeadlineTimer.class);

    /** What a due deadline does: the stamper's hand-off, under the topic lock. */
    @FunctionalInterface
    public interface Handoff {
        void fire(String tenant, String baseTopic);
    }

    /** How long after a failed hand-off the same topic is tried again. */
    static final Duration RETRY = Duration.ofSeconds(30);

    private record Key(String tenant, String base) { }

    private final ScheduledExecutorService scheduler;
    private final Clock clock;
    private final Handoff handoff;
    private final Map<Key, Armed> armed = new ConcurrentHashMap<>();

    private record Armed(Instant due, ScheduledFuture<?> future) { }

    public AckDeadlineTimer(ScheduledExecutorService scheduler, Clock clock, Handoff handoff) {
        this.scheduler = scheduler;
        this.clock = clock;
        this.handoff = handoff;
    }

    /** Arms the topic's deadline at {@code due}, replacing any earlier one; a null {@code due} cancels it. */
    public void arm(String tenant, String base, Instant due) {
        Key key = new Key(tenant, base);
        if (due == null) {
            cancel(key);
            return;
        }
        Armed existing = armed.get(key);
        if (existing != null && existing.due().equals(due) && !existing.future().isDone()) {
            return;
        }
        long delayMs = Math.max(0, Duration.between(clock.instant(), due).toMillis());
        cancel(key);
        ScheduledFuture<?> f = scheduler.schedule(() -> run(key), delayMs, TimeUnit.MILLISECONDS);
        armed.put(key, new Armed(due, f));
    }

    private void cancel(Key key) {
        Armed old = armed.remove(key);
        if (old != null && old.future() != null) {
            old.future().cancel(false);
        }
    }

    private void run(Key key) {
        armed.remove(key);   // the hand-off re-arms from the state it leaves
        try {
            handoff.fire(key.tenant(), key.base());
        } catch (Throwable t) {
            log.warn("event=board_ack_deadline_failed tenant={} base={} error={}", key.tenant(), key.base(), t.getMessage(), t);
            // A transient failure must not drop the deadline: retry shortly, unless the hand-off armed something itself.
            if (!armed.containsKey(key)) {
                arm(key.tenant(), key.base(), clock.instant().plus(RETRY));
            }
        }
    }

    /** The instant a topic's deadline is armed for, or null; for tests and the status of the timer. */
    public Instant armedAt(String tenant, String base) {
        Armed a = armed.get(new Key(tenant, base));
        return a == null ? null : a.due();
    }
}
