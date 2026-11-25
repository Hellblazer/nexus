// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Delayed;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: the acknowledgement deadline. The decision half runs on a fixed clock; the timer half runs on a
 * scheduler that records what is scheduled and runs it only when the test says, so nothing here sleeps.
 */
class AckDeadlineTimerTest {

    private static final Instant EPISODE = OwnerFixture.T0.plus(Duration.ofHours(1));

    private OwnerFixture openRed() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.recordNow(f.decide());
        return f;
    }

    @Test
    void theDeadlineIsTheGroupDefaultCountedFromEpisodeAt() {
        OwnerFixture f = openRed();

        assertThat(f.decide().deadline()).isEqualTo(EPISODE.plus(Duration.ofMinutes(20)));
    }

    @Test
    void noAckByTheDeadlineNamesNextUp() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(20)));

        OwnerDecision.Post post = f.onDeadline().post();

        assertThat(post).isNotNull();
        assertThat(post.sha()).isEqualTo("Y");
        assertThat(OwnerFixture.bodyOf(post)).containsEntry("owner", "C").containsEntry("reason", "handoff")
            .containsEntry("handoffs", 1).containsEntry("refresh", false).containsEntry("next", null)
            .containsEntry("episode_at", EPISODE.plus(Duration.ofMinutes(20)).toString());
    }

    @Test
    void beforeTheDeadlineNothingIsPosted() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(19)));

        OwnerDecision.Post post = f.onDeadline().post();

        // Past half the deadline next up is nudged once (OwnerDecisionAuditTest); no hand-off or escalation row yet.
        assertThat(post == null || "nudge".equals(post.kind())).as("no owner row before the deadline").isTrue();
    }

    @Test
    void theTopicOverrideReplacesTheGroupDefault() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.byTopic.put(OwnerFixture.BASE, 45);
        f.push("X", "G0", "A").push("Y", "X", "C").done("Y", "failure");
        f.recordNow(f.decide());

        assertThat(f.decide().deadline()).isEqualTo(EPISODE.plus(Duration.ofMinutes(45)));
        f.now(EPISODE.plus(Duration.ofMinutes(30)));
        OwnerDecision.Post before = f.onDeadline().post();
        assertThat(before == null || "nudge".equals(before.kind())).as("past the default, before the override").isTrue();
        f.now(EPISODE.plus(Duration.ofMinutes(45)));
        assertThat(f.onDeadline().post()).isNotNull();
    }

    @Test
    void anAckOnTheFirstRedTipStillCountsAfterARefresh() {
        OwnerFixture f = openRed();
        f.ack("A", "Y");
        f.push("Z", "Y", "A").done("Z", "failure");
        OwnerDecision.Decision refresh = f.decide();
        assertThat(OwnerFixture.bodyOf(refresh.post())).containsEntry("refresh", true);
        f.recordNow(refresh);
        f.now(EPISODE.plus(Duration.ofMinutes(50)));

        assertThat(f.onDeadline().post()).as("A acknowledged Y, a red tip since the last green").isNull();
        assertThat(f.decide().deadline())
            .as("the hand-off deadline gives way to the stall deadline, counted from the owner's latest sign: its push Z")
            .isEqualTo(f.pushes.getLast().createdAt().plus(Duration.ofMinutes(60)));
    }

    @Test
    void anAckFromAnIdentityTheRedWasHandedAwayFromDoesNotCount() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(21)));
        f.recordNow(f.onDeadline());           // handed to C
        f.ack("A", "Y");                       // A answers late
        f.now(EPISODE.plus(Duration.ofMinutes(50)));

        OwnerDecision.Post post = f.onDeadline().post();

        assertThat(post).as("C never acknowledged, and A's ack is not C's").isNotNull();
        assertThat(OwnerFixture.bodyOf(post)).containsEntry("reason", "escalated");
    }

    @Test
    void anAckByTheNewOwnerCounts() {
        OwnerFixture f = openRed();
        f.now(EPISODE.plus(Duration.ofMinutes(21)));
        f.recordNow(f.onDeadline());
        f.ack("C", "Y");
        f.now(EPISODE.plus(Duration.ofMinutes(60)));

        assertThat(f.onDeadline().post()).isNull();
    }

    @Test
    void aPushBeforeAndAnAckAfterClearMapToOneIdentity() {
        OwnerFixture f = new OwnerFixture().seat("A").seat("C");
        f.stamps.put("s-before-clear", "A");   // /clear changes the session id; the identity stays
        f.stamps.put("s-after-clear", "A");
        f.pushStamped("X", "G0", "s-before-clear").push("Y", "X", "C").done("Y", "failure");
        f.recordNow(f.decide());
        f.ack("A", "Y");
        f.stamps.put("s-after-clear", "A");
        // The ack's own stamp is the new session id.
        f.owners.set(f.owners.size() - 1, new BoardRow("a-new", f.clock.plusSeconds(1), f.owners.getLast().dims(), "{}", "s-after-clear"));
        f.now(EPISODE.plus(Duration.ofMinutes(50)));

        assertThat(f.onDeadline().post()).as("the owner A acknowledged under its new session id").isNull();
    }

    // ── the timer ────────────────────────────────────────────────────────────

    /** A scheduler that records tasks and runs them only on demand. */
    private static final class ManualScheduler extends ScheduledThreadPoolExecutor {
        record Task(Runnable task, long delayMs, boolean[] cancelled) { }

        final List<Task> tasks = new ArrayList<>();

        ManualScheduler() {
            super(1);
        }

        @Override
        public ScheduledFuture<?> schedule(Runnable command, long delay, TimeUnit unit) {
            boolean[] cancelled = new boolean[1];
            tasks.add(new Task(command, unit.toMillis(delay), cancelled));
            return new ScheduledFuture<Object>() {
                @Override public long getDelay(TimeUnit u) { return 0; }
                @Override public int compareTo(Delayed o) { return 0; }
                @Override public boolean cancel(boolean b) { cancelled[0] = true; return true; }
                @Override public boolean isCancelled() { return cancelled[0]; }
                @Override public boolean isDone() { return cancelled[0]; }
                @Override public Object get() { return null; }
                @Override public Object get(long t, TimeUnit u) { return null; }
            };
        }
    }

    @Test
    void theTimerArmsForTheDelayToTheDeadlineAndFiresTheHandoffOnce() {
        ManualScheduler sched = new ManualScheduler();
        List<String> fired = new ArrayList<>();
        Clock clock = Clock.fixed(EPISODE, ZoneOffset.UTC);
        AckDeadlineTimer timer = new AckDeadlineTimer(sched, clock, (t, b) -> fired.add(t + "/" + b));

        timer.arm("t1", "nexus-develop", EPISODE.plus(Duration.ofMinutes(20)));

        assertThat(sched.tasks).hasSize(1);
        assertThat(sched.tasks.getFirst().delayMs()).isEqualTo(Duration.ofMinutes(20).toMillis());
        assertThat(timer.armedAt("t1", "nexus-develop")).isEqualTo(EPISODE.plus(Duration.ofMinutes(20)));
        sched.tasks.getFirst().task().run();
        assertThat(fired).containsExactly("t1/nexus-develop");
        assertThat(timer.armedAt("t1", "nexus-develop")).as("the hand-off re-arms from what it leaves").isNull();
    }

    @Test
    void aNewDeadlineReplacesTheOldAndNullCancels() {
        ManualScheduler sched = new ManualScheduler();
        AckDeadlineTimer timer = new AckDeadlineTimer(sched, Clock.fixed(EPISODE, ZoneOffset.UTC), (t, b) -> { });

        timer.arm("t1", "b", EPISODE.plus(Duration.ofMinutes(20)));
        timer.arm("t1", "b", EPISODE.plus(Duration.ofMinutes(20)));
        assertThat(sched.tasks).as("the same deadline is not scheduled twice").hasSize(1);
        timer.arm("t1", "b", EPISODE.plus(Duration.ofMinutes(40)));
        assertThat(sched.tasks).hasSize(2);
        assertThat(sched.tasks.getFirst().cancelled()[0]).as("the first was cancelled").isTrue();
        timer.arm("t1", "b", null);
        assertThat(sched.tasks.get(1).cancelled()[0]).isTrue();
        assertThat(timer.armedAt("t1", "b")).isNull();
    }

    @Test
    void aPastDeadlineFiresAtOnceAndAThrowingHandoffNeverEscapes() {
        ManualScheduler sched = new ManualScheduler();
        AckDeadlineTimer timer = new AckDeadlineTimer(sched, Clock.fixed(EPISODE, ZoneOffset.UTC), (t, b) -> {
            throw new IllegalStateException("boom");
        });

        timer.arm("t1", "b", EPISODE.minus(Duration.ofHours(1)));

        assertThat(sched.tasks.getFirst().delayMs()).isZero();
        sched.tasks.getFirst().task().run();   // must not throw
    }

    @Test
    void aFailedHandoffIsRetriedShortlyAndALaterArmReplacesTheRetry() {
        ManualScheduler sched = new ManualScheduler();
        AckDeadlineTimer timer = new AckDeadlineTimer(sched, Clock.fixed(EPISODE, ZoneOffset.UTC), (t, b) -> {
            throw new IllegalStateException("boom");
        });
        timer.arm("t1", "b", EPISODE.plus(Duration.ofMinutes(20)));

        sched.tasks.getFirst().task().run();

        assertThat(sched.tasks).as("a retry was scheduled").hasSize(2);
        assertThat(sched.tasks.get(1).delayMs()).isEqualTo(AckDeadlineTimer.RETRY.toMillis());
        assertThat(timer.armedAt("t1", "b")).isEqualTo(EPISODE.plus(AckDeadlineTimer.RETRY));

        timer.arm("t1", "b", EPISODE.plus(Duration.ofMinutes(40)));

        assertThat(sched.tasks.get(1).cancelled()[0]).as("the retry is replaced").isTrue();
        assertThat(timer.armedAt("t1", "b")).isEqualTo(EPISODE.plus(Duration.ofMinutes(40)));
    }

    // ── the stuck event and the failure count ────────────────────────────────

    /** The timer's log events for one test, and the retries it schedules run a round at a time. */
    private static final class Failing implements AutoCloseable {
        final ManualScheduler sched = new ManualScheduler();
        final boolean[] failing = {true};
        final AckDeadlineTimer timer = new AckDeadlineTimer(sched, Clock.fixed(EPISODE, ZoneOffset.UTC), (t, b) -> {
            if (failing[0]) {
                throw new IllegalStateException("boom " + t);
            }
        });
        final ch.qos.logback.classic.Logger logger =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(AckDeadlineTimer.class);
        final ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        private final java.util.Set<ManualScheduler.Task> ran = new java.util.HashSet<>();

        Failing() {
            logs.start();
            logger.addAppender(logs);
        }

        /** Runs every scheduled task that is not cancelled and has not run, once: a round over all armed topics. */
        void round() {
            for (ManualScheduler.Task t : List.copyOf(sched.tasks)) {
                if (!t.cancelled()[0] && ran.add(t)) {
                    t.task().run();
                }
            }
        }

        void rounds(int n) {
            for (int i = 0; i < n; i++) {
                round();
            }
        }

        List<ch.qos.logback.classic.spi.ILoggingEvent> errors() {
            return logs.list.stream().filter(e -> e.getLevel() == ch.qos.logback.classic.Level.ERROR).toList();
        }

        @Override
        public void close() {
            logger.detachAppender(logs);
        }
    }

    private static Instant due() {
        return EPISODE.plus(Duration.ofMinutes(20));
    }

    @Test
    void aHandoffThatKeepsFailingRaisesAnErrorEventEveryTwelfthAttemptAndKeepsRetrying() {
        try (Failing f = new Failing()) {
            f.timer.arm("t1", "b", due());

            f.rounds(24);

            assertThat(f.errors()).as("attempts 12 and 24").hasSize(2);
            assertThat(f.errors().getFirst().getFormattedMessage()).contains("board_ack_deadline_stuck", "attempt=12");
            assertThat(f.errors().getLast().getFormattedMessage()).contains("attempt=24");
            assertThat(f.timer.armedAt("t1", "b")).as("the retry is never abandoned").isNotNull();
        }
    }

    @Test
    void aSuccessBetweenFailuresResetsTheCount() {
        try (Failing f = new Failing()) {
            f.timer.arm("t1", "b", due());
            f.rounds(6);
            f.failing[0] = false;
            f.round();   // the hand-off succeeds
            f.failing[0] = true;
            f.timer.arm("t1", "b", due());

            f.rounds(6);   // 12 failures in all, but only 6 in a row

            assertThat(f.errors()).as("without the reset the count reaches 12 and raises the event").isEmpty();
        }
    }

    @Test
    void anArmThatCancelsTheTopicResetsItsCount() {
        try (Failing f = new Failing()) {
            f.timer.arm("t1", "b", due());
            f.rounds(11);
            f.timer.arm("t1", "b", null);   // nothing is owed any more
            f.timer.arm("t1", "b", due());

            f.round();   // the 12th failure overall, the first since the cancel

            assertThat(f.errors()).as("without the reset this is the 12th in a row").isEmpty();
        }
    }

    @Test
    void evictingATenantClearsItsFailureCountAndLeavesAnotherTenantsCount() {
        try (Failing f = new Failing()) {
            f.timer.arm("t1", "b", due());
            f.timer.arm("t2", "b", due());
            f.rounds(11);   // 11 failures of each

            f.timer.evictTenant("t1");
            f.timer.arm("t1", "b", due());
            f.round();   // t1: its first failure since the eviction; t2: its 12th

            assertThat(f.errors()).as("only t2 reaches 12: a cleared map would hide it, an uncleared t1 would add one")
                .hasSize(1);
            assertThat(f.errors().getFirst().getFormattedMessage()).contains("tenant=t2", "attempt=12");
        }
    }

    @Test
    void onlyTheFirstFailedAttemptOfARunLogsItsStack() {
        try (Failing f = new Failing()) {
            f.timer.arm("t1", "b", due());

            f.rounds(3);

            var warns = f.logs.list.stream().filter(e -> e.getFormattedMessage().contains("board_ack_deadline_failed")).toList();
            assertThat(warns).hasSize(3);
            assertThat(warns.get(0).getThrowableProxy()).as("the first attempt carries the stack").isNotNull();
            assertThat(warns.get(1).getThrowableProxy()).as("a repeat carries the message only").isNull();
            assertThat(warns.get(2).getThrowableProxy()).isNull();
        }
    }

    @Test
    void evictingATenantCancelsItsDeadlinesAndLeavesOthers() {
        ManualScheduler sched = new ManualScheduler();
        List<String> fired = new ArrayList<>();
        AckDeadlineTimer timer = new AckDeadlineTimer(sched, Clock.fixed(EPISODE, ZoneOffset.UTC), (t, b) -> fired.add(t + "/" + b));
        timer.arm("t1", "a", EPISODE.plus(Duration.ofMinutes(20)));
        timer.arm("t1", "b", EPISODE.plus(Duration.ofMinutes(20)));
        timer.arm("t2", "a", EPISODE.plus(Duration.ofMinutes(20)));

        assertThat(timer.evictTenant("t1")).as("the two deadlines of t1").isEqualTo(2);

        assertThat(timer.armedAt("t1", "a")).isNull();
        assertThat(timer.armedAt("t1", "b")).isNull();
        assertThat(timer.armedAt("t2", "a")).as("another tenant's deadline stands").isNotNull();
        assertThat(sched.tasks.get(0).cancelled()[0]).as("a cancelled task never fires").isTrue();
        assertThat(sched.tasks.get(1).cancelled()[0]).isTrue();
        assertThat(sched.tasks.get(2).cancelled()[0]).isFalse();
        assertThat(timer.evictTenant("t1")).as("nothing left to evict").isZero();
    }
}
