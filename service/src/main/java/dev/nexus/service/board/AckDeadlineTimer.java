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
 * fails is retried after {@link #RETRY} for as long as the cause lasts. What fails and is retried is a red that may
 * already be acknowledged, or a fault in the database; an unacknowledged red is escalated by the stamper whether or
 * not the identity read works. The {@value #STUCK_ATTEMPTS}th failure of a topic since its last success, cancel or
 * eviction, and every {@value #STUCK_ATTEMPTS} after it, raises the error-level log event {@code board_ack_deadline_stuck}; it posts no
 * row. The failed attempt that starts a run logs its stack, the later ones their message.
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

    /** After this many failures of one topic's hand-off since its last success, cancel or eviction, and every this many after, an error event says so. */
    static final int STUCK_ATTEMPTS = 12;

    private record Key(String tenant, String base) { }

    private final ScheduledExecutorService scheduler;
    private final Clock clock;
    private final Handoff handoff;
    private final Map<Key, Armed> armed = new ConcurrentHashMap<>();
    private final Map<Key, Integer> failures = new ConcurrentHashMap<>();

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
            failures.remove(key);   // nothing is owed any more, so a later failure counts from one
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
            failures.remove(key);
        } catch (Throwable t) {
            int attempt = failures.merge(key, 1, Integer::sum);
            if (attempt == 1) {
                log.warn("event=board_ack_deadline_failed tenant={} base={} attempt=1 error={}", key.tenant(), key.base(),
                    t.getMessage(), t);
            } else {
                log.warn("event=board_ack_deadline_failed tenant={} base={} attempt={} error={}", key.tenant(), key.base(),
                    attempt, t.getMessage());
            }
            if (attempt % STUCK_ATTEMPTS == 0) {
                log.error("event=board_ack_deadline_stuck tenant={} base={} attempt={}", key.tenant(), key.base(), attempt);
            }
            // A transient failure must not drop the deadline: retry shortly, unless the hand-off armed something itself.
            if (!armed.containsKey(key)) {
                arm(key.tenant(), key.base(), clock.instant().plus(RETRY));
            }
        }
    }

    /** Cancels and removes every deadline armed for {@code tenant} (the test tenant reset); returns how many. */
    public int evictTenant(String tenant) {
        int n = 0;
        for (Key key : java.util.List.copyOf(armed.keySet())) {
            if (key.tenant().equals(tenant)) {
                cancel(key);
                n++;
            }
        }
        failures.keySet().removeIf(key -> key.tenant().equals(tenant));
        return n;
    }

    /** The instant a topic's deadline is armed for, or null; for tests and the status of the timer. */
    public Instant armedAt(String tenant, String base) {
        Armed a = armed.get(new Key(tenant, base));
        return a == null ? null : a.due();
    }
}
