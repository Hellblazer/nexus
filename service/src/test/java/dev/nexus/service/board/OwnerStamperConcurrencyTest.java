// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import dev.nexus.service.http.IdentityTestEnv;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 3: one evaluator at a time. Two {@code --done} writes evaluated at once, a hand-off racing a
 * {@code --done}, and a {@code serves} change racing a {@code --done}, post rows consistent with commit order and
 * never two rows from one state (real PostgreSQL, the per-topic advisory lock).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerStamperConcurrencyTest {

    StamperEnv se;

    @BeforeAll
    void up() throws Exception {
        se = new StamperEnv();
    }

    @AfterAll
    void down() throws Exception {
        se.close();
    }

    private String[] redOnY() {
        String t = se.tenant();
        String l = se.env.seat(t, "lead");
        String a = se.env.seat(t, "alpha");
        String c = se.env.seat(t, "carol");
        se.form(t, l, a, c);
        se.push(t, StamperEnv.BASE, "X", "G0", "sess-alpha");
        se.push(t, StamperEnv.BASE, "Y", "X", "sess-carol");
        return new String[] {t, l, a, c};
    }

    private <T> List<T> together(List<java.util.concurrent.Callable<T>> jobs) throws Exception {
        var pool = Executors.newFixedThreadPool(jobs.size());
        try {
            CountDownLatch go = new CountDownLatch(1);
            List<Future<T>> fs = jobs.stream().map(j -> pool.submit(() -> {
                go.await();
                return j.call();
            })).toList();
            go.countDown();
            java.util.ArrayList<T> out = new java.util.ArrayList<>();
            for (Future<T> f : fs) {
                out.add(f.get());
            }
            return out;
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void twoDoneWritesEvaluatedAtOncePostOneRowForOneState() throws Exception {
        for (int round = 0; round < 5; round++) {
            String[] f = redOnY();
            se.doneRowOnly(f[0], StamperEnv.BASE, "X", "cancelled");
            se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

            together(List.of(
                () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                    Map.of("kind", "sha", "sha", "X", "outcome", "failure", "from", "github"), "{}"); return null; },
                () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                    Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}"); return null; }));

            var rows = se.ownerRows(f[0], StamperEnv.BASE);
            assertThat(rows).as("round " + round + ": one row from one state").hasSize(1);
            assertThat(rows.getFirst().sha()).isEqualTo("Y");
            assertThat(rows.getFirst().body()).containsEntry("owner", f[2]);
        }
    }

    @Test
    void aRedAndAGreenEvaluatedAtOnceLeaveTheStateOfTheNewestTip() throws Exception {
        String[] f = redOnY();
        se.done(f[0], StamperEnv.BASE, "Y", "failure");
        se.push(f[0], StamperEnv.BASE, "Z", "Y", "sess-carol");
        se.doneRowOnly(f[0], StamperEnv.BASE, "Z", "success");
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

        together(List.of(
            () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                Map.of("kind", "sha", "sha", "Z", "outcome", "success", "from", "github"), "{}"); return null; },
            () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}"); return null; }));

        var rows = se.ownerRows(f[0], StamperEnv.BASE);
        assertThat(rows).extracting(StamperEnv.Row::kind).containsExactly("owner", "clear");
    }

    @Test
    void aHandOffRacingADoneWritePostsRowsInCommitOrderAndNeverTwoFromOneState() throws Exception {
        for (int round = 0; round < 5; round++) {
            String[] f = redOnY();
            se.done(f[0], StamperEnv.BASE, "Y", "failure");
            se.env.ids.clock.advance(Duration.ofMinutes(21));
            se.env.renew(f[0], f[1]);
            se.env.renew(f[0], f[2]);
            se.env.renew(f[0], f[3]);
            se.push(f[0], StamperEnv.BASE, "Z", "Y", null);   // unstamped: it restarts no one's deadline
            se.doneRowOnly(f[0], StamperEnv.BASE, "Z", "failure");

            together(List.of(
                () -> { se.stamper.onDeadline(f[0], StamperEnv.BASE); return null; },
                () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                    Map.of("kind", "sha", "sha", "Z", "outcome", "failure", "from", "github"), "{}"); return null; }));

            // Whichever took the lock first, the deadline of a state that is already past is re-armed, and the timer
            // fires again: the hand-off that a refresh row pre-empted is the next evaluation's.
            se.stamper.onDeadline(f[0], StamperEnv.BASE);

            var rows = se.ownerRows(f[0], StamperEnv.BASE);
            assertThat(rows).as("round " + round + ": the first row, one for the refresh, one for the hand-off").hasSize(3);
            StamperEnv.Row last = rows.getLast();
            assertThat(last.sha()).isEqualTo("Z");
            assertThat(last.body()).containsEntry("owner", f[3]).containsEntry("handoffs", 1);
            assertThat(rows.stream().map(r -> r.sha() + "/" + r.get("reason") + "/" + r.get("handoffs") + "/" + r.get("refresh"))
                .distinct().count()).as("never two rows from one state").isEqualTo(3L);
            assertThat(rows.stream().filter(r -> "handoff".equals(r.get("reason"))).count()).isEqualTo(1L);
        }
    }

    @Test
    void aServesChangeRacingADoneWriteLeavesExactlyOneOwnerRow() throws Exception {
        String[] f = redOnY();
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");

        // The stamper swallows what an evaluation throws (a deadlock, a serialization failure) and logs it, so the
        // log is where a lock fault in the race shows.
        var logger = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(OwnerStamper.class);
        var logs = new ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent>();
        logs.start();
        logger.addAppender(logs);
        List<Integer> statuses;
        try {
            statuses = together(List.of(
                () -> { se.stamper.afterOut(f[0], "board/ci/nexus-develop--done",
                    Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}"); return 200; },
                () -> se.env.as(f[0], f[1], "POST", "/push-train/set",
                    Map.of("serves", List.of(StamperEnv.BASE, "nexus-main"))).status()));
        } finally {
            logger.detachAppender(logs);
            logs.stop();
        }

        assertThat(statuses.get(1)).as("the serves change").isEqualTo(200);
        assertThat(logs.list.stream().map(e -> e.getFormattedMessage()).filter(m -> m.contains("board_owner_stamper_failed")).toList())
            .as("no evaluation failed in the race").isEmpty();
        assertThat(se.ownerRows(f[0], StamperEnv.BASE)).hasSize(1);
    }

    /** A scheduler whose first {@code schedule} call blocks until released, so a test can hold one evaluation in its arm. */
    private static final class BlockingScheduler extends java.util.concurrent.ScheduledThreadPoolExecutor {
        final CountDownLatch inArm = new CountDownLatch(1);
        final CountDownLatch release = new CountDownLatch(1);
        private final java.util.concurrent.atomic.AtomicBoolean first = new java.util.concurrent.atomic.AtomicBoolean(true);

        BlockingScheduler() {
            super(1);
        }

        @Override
        public java.util.concurrent.ScheduledFuture<?> schedule(Runnable command, long delay, java.util.concurrent.TimeUnit unit) {
            if (first.compareAndSet(true, false)) {
                inArm.countDown();
                try {
                    release.await();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            return super.schedule(() -> { }, 365, java.util.concurrent.TimeUnit.DAYS);
        }
    }

    @Test
    void anOlderEvaluationCannotOverwriteANewerOnesDeadline() throws Exception {
        String[] f = redOnY();
        java.time.Instant d1 = java.time.Instant.parse("2031-01-01T00:00:00Z");
        java.time.Instant d2 = java.time.Instant.parse("2031-01-02T00:00:00Z");
        java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
        OwnerStamper stamper = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock,
            (in, trigger) -> new OwnerDecision.Decision(null, calls.incrementAndGet() == 1 ? d1 : d2));
        BlockingScheduler sched = new BlockingScheduler();
        AckDeadlineTimer timer = new AckDeadlineTimer(sched, java.time.Clock.systemUTC(), (t, b) -> { });
        stamper.bind(timer);
        var pool = Executors.newFixedThreadPool(2);
        try {
            Future<?> older = pool.submit(() -> stamper.onDeadline(f[0], StamperEnv.BASE));
            assertThat(sched.inArm.await(20, java.util.concurrent.TimeUnit.SECONDS)).as("the older evaluation reached its arm").isTrue();
            Future<?> newer = pool.submit(() -> stamper.onDeadline(f[0], StamperEnv.BASE));
            Thread.sleep(700);
            assertThat(calls.get()).as("the newer evaluation waits for the topic lock the older one still holds").isEqualTo(1);

            sched.release.countDown();
            older.get(20, java.util.concurrent.TimeUnit.SECONDS);
            newer.get(20, java.util.concurrent.TimeUnit.SECONDS);

            assertThat(calls.get()).isEqualTo(2);
            assertThat(timer.armedAt(f[0], StamperEnv.BASE)).as("the newer evaluation's deadline stands").isEqualTo(d2);
        } finally {
            sched.release.countDown();
            pool.shutdownNow();
            sched.shutdownNow();
        }
    }

    // ── a group write and an evaluation wait on one lock ─────────────────────

    /** One evaluation of an open red held inside its arm, and what a test needs to let it go. */
    private record Held(BlockingScheduler sched, java.util.concurrent.ExecutorService pool, Future<?> evaluation) {
        void release() {
            sched.release.countDown();
        }

        void close() {
            sched.release.countDown();
            pool.shutdownNow();
            sched.shutdownNow();
        }
    }

    /** A red on Y, evaluated by a stamper whose timer blocks in {@code arm}: the evaluation holds the topic lock. */
    private Held holdAnEvaluation(String[] f) throws Exception {
        se.doneRowOnly(f[0], StamperEnv.BASE, "Y", "failure");
        OwnerStamper stamper = new OwnerStamper(se.env.ids.scope, se.tuples, se.env.groups, se.env.ids.repo, se.env.ids.clock);
        BlockingScheduler sched = new BlockingScheduler();
        stamper.bind(new AckDeadlineTimer(sched, java.time.Clock.systemUTC(), (t, b) -> { }));
        var pool = Executors.newFixedThreadPool(2);
        Future<?> evaluation = pool.submit(() -> stamper.afterOut(f[0], "board/ci/nexus-develop--done",
            Map.of("kind", "sha", "sha", "Y", "outcome", "failure", "from", "github"), "{}"));
        Held held = new Held(sched, pool, evaluation);
        if (!sched.inArm.await(20, java.util.concurrent.TimeUnit.SECONDS)) {
            held.close();
            throw new AssertionError("the evaluation never reached its arm");
        }
        return held;
    }

    private static <T> void assertWaitsThenCompletes(Held held, Future<T> write, String what) throws Exception {
        Thread.sleep(700);
        assertThat(write.isDone()).as(what + " waits for the topic lock the evaluation holds").isFalse();
        held.release();
        held.evaluation().get(20, java.util.concurrent.TimeUnit.SECONDS);
        write.get(20, java.util.concurrent.TimeUnit.SECONDS);
    }

    private void endsWhileAnEvaluationIsHeld(String what, java.util.function.Consumer<String[]> beforeTheEvaluation,
                                             java.util.function.Function<String[], dev.nexus.service.http.IdentityTestEnv.Resp> end)
        throws Exception {
        String[] f = redOnY();
        beforeTheEvaluation.accept(f);
        Held held = holdAnEvaluation(f);
        try {
            Future<dev.nexus.service.http.IdentityTestEnv.Resp> ending = held.pool().submit(() -> end.apply(f));
            assertWaitsThenCompletes(held, ending, what);
            assertThat(ending.get().status()).as(what).isEqualTo(200);
            assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(StamperEnv.Row::kind)
                .as("the evaluation that held the lock posted its row, before the group ended").containsExactly("owner");

            // The group is over: a deadline that falls due now posts no hand-off and no escalation for that red.
            se.env.ids.clock.advance(Duration.ofMinutes(45));
            se.stamper.onDeadline(f[0], StamperEnv.BASE);
            se.stamper.onDeadline(f[0], StamperEnv.BASE);
            assertThat(se.ownerRows(f[0], StamperEnv.BASE)).extracting(StamperEnv.Row::kind)
                .as("nothing follows the end of the group").containsExactly("owner");
        } finally {
            held.close();
        }
    }

    @Test
    void aDisbandWaitsForAnEvaluationHoldingTheTopicLockAndNothingFollowsIt() throws Exception {
        endsWhileAnEvaluationIsHeld("the disband", f -> { },
            f -> se.env.as(f[0], f[1], "POST", "/push-train/disband", Map.of()));
    }

    @Test
    void aSetEndedAtWaitsForAnEvaluationHoldingTheTopicLockAndNothingFollowsIt() throws Exception {
        endsWhileAnEvaluationIsHeld("set ended_at", f -> { },
            f -> se.env.as(f[0], f[1], "POST", "/push-train/set", Map.of("ended_at", true)));
    }

    @Test
    void anOverrideEndingTheGroupWaitsForAnEvaluationHoldingTheTopicLockAndNothingFollowsIt() throws Exception {
        endsWhileAnEvaluationIsHeld("the override", f -> se.env.end(f[0], f[1]),   // the leader is gone: the override is open
            f -> se.env.tokenless(f[0], "POST", "/push-train/override", Map.of("ended_at", true)));
    }

    @Test
    void aServesChangeWaitsForAnEvaluationOnTheSameTopicKey() throws Exception {
        String[] f = redOnY();
        Held held = holdAnEvaluation(f);
        try {
            Future<dev.nexus.service.http.IdentityTestEnv.Resp> set = held.pool().submit(() -> se.env.as(f[0], f[1], "POST",
                "/push-train/set", Map.of("serves", List.of(StamperEnv.BASE, "nexus-main"))));
            assertWaitsThenCompletes(held, set, "the serves change");
            assertThat(set.get().status()).isEqualTo(200);
        } finally {
            held.close();
        }
    }
}
