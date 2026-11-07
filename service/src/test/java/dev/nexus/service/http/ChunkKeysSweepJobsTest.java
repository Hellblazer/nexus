// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.http.ChunkKeysSweepJobs.Busy;
import dev.nexus.service.http.ChunkKeysSweepJobs.Conflict;
import dev.nexus.service.http.ChunkKeysSweepJobs.Job;
import dev.nexus.service.http.ChunkKeysSweepJobs.Outcome;
import dev.nexus.service.http.ChunkKeysSweepJobs.Started;
import dev.nexus.service.http.ChunkKeysSweepJobs.State;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/**
 * nexus-v75nw: the registry the background retire-keys dry runs live in. What is pinned: a start answers at once
 * however long the work takes (the whole point against an edge that cuts a request at about 30 s), a poll reads the
 * stage while the job runs and the status and body the synchronous call would have answered once it ended, a work that
 * throws ends as a 500 and leaves the pool usable, one tenant has one running job, the pool and queue are bounded, a
 * finished job expires, and a tenant reset drops a tenant's jobs.
 */
class ChunkKeysSweepJobsTest {

    private final AtomicLong now = new AtomicLong(1_000_000L);
    private ChunkKeysSweepJobs jobs = new ChunkKeysSweepJobs(now::get, Duration.ofMinutes(30), 64, 2, 2);

    @AfterEach
    void stop() {
        jobs.shutdown();
    }

    private static Outcome ok(String tenant) {
        return new Outcome(200, Map.of("tenant", tenant, "totals", Map.of("sweepable_rows", 7)));
    }

    private static void await(java.util.function.BooleanSupplier condition) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("condition not met within 20 s");
            }
            Thread.sleep(5);
        }
    }

    @Test
    void aStartAnswersWhileTheWorkIsStillRunning_andThePollReadsTheStageThenTheOutcome() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        CountDownLatch inside = new CountDownLatch(1);

        Started s = jobs.start("t", "t", false, "fp", stage -> {
            stage.accept("census 1/1: c");
            inside.countDown();
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        });

        assertThat(s.created()).isTrue();
        assertThat(inside.await(20, TimeUnit.SECONDS)).isTrue();
        Job running = jobs.find(s.job().id());
        assertThat(running.state()).as("the start returned with the work still held open").isEqualTo(State.RUNNING);
        assertThat(running.stage()).isEqualTo("census 1/1: c");
        assertThat(running.outcome()).isNull();

        now.addAndGet(45_000);   // longer than the edge's origin timeout: the job does not care
        assertThat(running.elapsedMs(now.get())).isEqualTo(45_000);
        release.countDown();
        await(() -> jobs.find(s.job().id()).state() != State.RUNNING);

        Job done = jobs.find(s.job().id());
        assertThat(done.state()).isEqualTo(State.DONE);
        assertThat(done.stage()).isEqualTo("done");
        assertThat(done.outcome().httpStatus()).isEqualTo(200);
        assertThat(done.outcome().body()).containsEntry("tenant", "t");
    }

    @Test
    void aWorkThatThrowsEndsAs500SweepFailed_andTheNextJobStillRuns() throws Exception {
        Started bad = jobs.start("t", "t", false, "fp", stage -> {
            throw new IllegalStateException("statement timeout");
        });
        await(() -> jobs.find(bad.job().id()).state() != State.RUNNING);

        Job failed = jobs.find(bad.job().id());
        assertThat(failed.state()).isEqualTo(State.FAILED);
        assertThat(failed.outcome().httpStatus()).isEqualTo(500);
        assertThat(failed.outcome().body()).containsEntry("reason", "sweep_failed").containsEntry("dry_run", true);
        assertThat(failed.outcome().body().toString()).as("the class, never the message").doesNotContain("timeout");

        Started good = jobs.start("t", "t", false, "fp", stage -> ok("t"));
        assertThat(good.created()).as("a failed job does not hold the tenant").isTrue();
        await(() -> jobs.find(good.job().id()).state() == State.DONE);
    }

    @Test
    void aWorkThatAnswersNon200EndsFailedWithItsOwnStatusAndBody() throws Exception {
        Started s = jobs.start("t", "t", false, "fp", stage -> new Outcome(500, Map.of("reason", "audit_write_failed")));
        await(() -> jobs.find(s.job().id()).state() != State.RUNNING);

        assertThat(jobs.find(s.job().id()).state()).isEqualTo(State.FAILED);
        assertThat(jobs.find(s.job().id()).outcome().body()).containsEntry("reason", "audit_write_failed");
    }

    @Test
    void oneTenantHasOneRunningJob_theSameRequestJoinsIt_aDifferentOneIsRefused() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        Started first = jobs.start("t", "t", false, "fp-a", stage -> {
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        });

        Started again = jobs.start("t", "t", false, "fp-a", stage -> ok("never"));
        assertThat(again.created()).isFalse();
        assertThat(again.job().id()).isEqualTo(first.job().id());

        assertThatThrownBy(() -> jobs.start("t", "t", false, "fp-b", stage -> ok("never")))
            .isInstanceOf(Conflict.class)
            .satisfies(e -> assertThat(((Conflict) e).jobId()).isEqualTo(first.job().id()));
        assertThatThrownBy(() -> jobs.start("t", "other-caller", false, "fp-a", stage -> ok("never")))
            .as("another caller does not join a job it did not start")
            .isInstanceOf(Conflict.class);

        Started otherTenant = jobs.start("u", "u", false, "fp-a", stage -> ok("u"));
        assertThat(otherTenant.created()).as("tenants do not block each other").isTrue();
        release.countDown();
    }

    @Test
    void theThreadsAndTheQueueAreBounded_aStartPastThemIsBusy() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        // 2 threads + 2 queue places = 4 jobs; each on its own tenant so the per-tenant rule does not answer first.
        for (int i = 0; i < 4; i++) {
            jobs.start("t" + i, "t" + i, false, "fp", stage -> {
                release.await(20, TimeUnit.SECONDS);
                return ok("x");
            });
        }

        assertThatThrownBy(() -> jobs.start("t4", "t4", false, "fp", stage -> ok("x"))).isInstanceOf(Busy.class);
        assertThat(jobs.find("nope")).isNull();
        assertThat(jobs.size()).as("a refused start leaves no job behind").isEqualTo(4);
        release.countDown();
    }

    @Test
    void aFinishedJobExpiresAfterTheRetention_aRunningOneNeverDoes() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        Started running = jobs.start("slow", "slow", false, "fp", stage -> {
            release.await(20, TimeUnit.SECONDS);
            return ok("slow");
        });
        Started quick = jobs.start("quick", "quick", false, "fp", stage -> ok("quick"));
        await(() -> jobs.find(quick.job().id()).state() == State.DONE);

        now.addAndGet(Duration.ofMinutes(29).toMillis());
        assertThat(jobs.find(quick.job().id())).as("kept for its client to read").isNotNull();
        now.addAndGet(Duration.ofMinutes(2).toMillis());
        assertThat(jobs.find(quick.job().id())).as("gone after the retention").isNull();
        assertThat(jobs.find(running.job().id())).as("a running job is not expired").isNotNull();
        release.countDown();
    }

    @Test
    void theRegistryHoldsAtMostItsLimit_oldestFinishedFirst() throws Exception {
        jobs.shutdown();
        jobs = new ChunkKeysSweepJobs(now::get, Duration.ofMinutes(30), 3, 2, 2);
        String firstId = null;
        for (int i = 0; i < 5; i++) {
            Started s = jobs.start("t" + i, "t" + i, false, "fp", stage -> ok("x"));
            if (i == 0) firstId = s.job().id();
            await(() -> jobs.find(s.job().id()) == null || jobs.find(s.job().id()).state() != State.RUNNING);
        }

        assertThat(jobs.size()).isLessThanOrEqualTo(3);
        assertThat(jobs.find(firstId)).as("the oldest finished job went first").isNull();
    }

    @Test
    void aTenantResetDropsTheJobsOfThatTenantOnly() throws Exception {
        Started a = jobs.start("a", "a", false, "fp", stage -> ok("a"));
        Started viaRoot = jobs.start("target", "default", true, "fp", stage -> ok("target"));
        Started b = jobs.start("b", "b", false, "fp", stage -> ok("b"));
        await(() -> jobs.find(a.job().id()).state() == State.DONE && jobs.find(b.job().id()).state() == State.DONE
            && jobs.find(viaRoot.job().id()).state() == State.DONE);

        assertThat(jobs.evictTenant("a")).isEqualTo(1);
        assertThat(jobs.find(a.job().id())).isNull();
        assertThat(jobs.evictTenant("default")).as("a job a root credential started is its caller's too").isEqualTo(1);
        assertThat(jobs.find(viaRoot.job().id())).isNull();
        assertThat(jobs.find(b.job().id())).isNotNull();
    }

    @Test
    void aJobIsReadableOnlyByTheCredentialThatStartedIt() throws Exception {
        Started rootStart = jobs.start("target", "default", true, "fp", stage -> ok("target"));
        Started tenantStart = jobs.start("t", "t", false, "fp", stage -> ok("t"));

        assertThat(rootStart.job().startedBy("default", true)).isTrue();
        assertThat(rootStart.job().startedBy("default", false)).as("a tenant credential bound to the same tenant").isFalse();
        assertThat(rootStart.job().startedBy("t", true)).isFalse();
        assertThat(tenantStart.job().startedBy("t", false)).isTrue();
        assertThat(tenantStart.job().startedBy("u", false)).isFalse();
    }

    // ---- no finished-job join, eviction of a running job, cancel and the wall-clock cap -----------------------

    @Test
    void aStartNeverJoinsAFinishedJob_itRunsAgain_whateverTheRequestOrTheOutcome() throws Exception {
        java.util.concurrent.atomic.AtomicInteger runs = new java.util.concurrent.atomic.AtomicInteger();
        Started first = jobs.start("t", "t", false, "fp", stage -> {
            runs.incrementAndGet();
            return ok("t");
        });
        await(() -> first.job().state() == State.DONE);

        Started again = jobs.start("t", "t", false, "fp", stage -> {
            runs.incrementAndGet();
            return ok("t");
        });
        assertThat(again.created()).as("a finished job is read, never handed to a new start").isTrue();
        assertThat(again.job().id()).isNotEqualTo(first.job().id());
        await(() -> again.job().state() == State.DONE);
        assertThat(runs.get()).as("the second start recounted").isEqualTo(2);
        assertThat(jobs.find(first.job().id())).as("the first stays readable by id until the retention ends").isNotNull();

        Started blocked = jobs.start("f", "f", false, "fp", stage -> new Outcome(409, Map.of("reason", "sweep_preconditions_not_met")));
        await(() -> blocked.job().state() == State.FAILED);
        Started rerun = jobs.start("f", "f", false, "fp", stage -> ok("f"));
        assertThat(rerun.created()).as("a refused (blocked) outcome is not replayed to the rerun after the fix").isTrue();
    }

    @Test
    void anApplyAndADryRunOfOneTenantExcludeEachOther_theSameApplyJoinsOnly() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        ChunkKeysSweepJobs.JobWork hold = (job, stage) -> {
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        };
        Started apply = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp", hold);

        assertThatThrownBy(() -> jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.DRY_RUN, "fp", hold))
            .as("a dry run of the same request is still another job").isInstanceOf(Conflict.class);
        assertThatThrownBy(() -> jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp-other", hold))
            .isInstanceOf(Conflict.class);
        Started same = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp", hold);
        assertThat(same.created()).isFalse();
        assertThat(same.job().id()).isEqualTo(apply.job().id());
        release.countDown();
        await(() -> apply.job().state() == State.DONE);

        CountDownLatch release2 = new CountDownLatch(1);
        Started dry = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.DRY_RUN, "fp", (job, stage) -> {
            release2.await(20, TimeUnit.SECONDS);
            return ok("t");
        });
        assertThatThrownBy(() -> jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp", hold))
            .as("and an apply while a dry run counts").isInstanceOf(Conflict.class)
            .satisfies(e -> assertThat(((Conflict) e).jobId()).isEqualTo(dry.job().id()));
        release2.countDown();
    }

    @Test
    void aCancelIsAcceptedByARunningApplyOnly_andTheWorkSeesItAtItsNextBoundary() throws Exception {
        CountDownLatch atBoundary = new CountDownLatch(1);
        java.util.concurrent.atomic.AtomicReference<String> seen = new java.util.concurrent.atomic.AtomicReference<>();
        Started apply = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp", (job, stage) -> {
            assertThat(job.stopReason()).as("nothing asked yet").isNull();
            atBoundary.await(20, TimeUnit.SECONDS);
            seen.set(job.stopReason());
            return ok("t");
        });
        Started dry = jobs.start("u", "u", false, "fp", stage -> {
            Thread.sleep(50);
            return ok("u");
        });

        assertThat(dry.job().requestCancel()).as("a dry run has no batch to stop at").isFalse();
        assertThat(dry.job().cancelRequested()).isFalse();
        assertThat(apply.job().requestCancel()).isTrue();
        assertThat(apply.job().stopReason()).isEqualTo("cancelled");
        atBoundary.countDown();
        await(() -> apply.job().state() == State.DONE);

        assertThat(seen.get()).isEqualTo("cancelled");
        assertThat(apply.job().requestCancel()).as("a job that has ended cannot be cancelled").isFalse();
    }

    @Test
    void aJobPastTheWallClockCapIsToldToStop_andOneInsideItIsNot() throws Exception {
        jobs.shutdown();
        jobs = new ChunkKeysSweepJobs(now::get, Duration.ofMinutes(30), 64, 2, 2, Duration.ofHours(4));
        CountDownLatch release = new CountDownLatch(1);
        Started running = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp", (job, stage) -> {
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        });

        now.addAndGet(Duration.ofHours(4).toMillis() - 1);
        assertThat(running.job().stopReason()).as("one millisecond inside the cap").isNull();
        now.addAndGet(1);
        assertThat(running.job().stopReason()).isEqualTo("wall_clock_cap");
        running.job().requestCancel();
        assertThat(running.job().stopReason()).as("an explicit cancel is named before the cap").isEqualTo("cancelled");
        jobs.evictTenant("t");
        assertThat(running.job().stopReason()).as("a tenant reset is named first").isEqualTo("evicted");
        release.countDown();
    }

    @Test
    void anApplyPublishesItsProgress_andAFinishedOneKeepsIt() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        CountDownLatch published = new CountDownLatch(1);
        Started apply = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp", (job, stage) -> {
            assertThat(job.remaining()).as("not known before the count").isEqualTo(-1);
            job.progress(3, 2_500, 7_500);
            published.countDown();
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        });

        assertThat(published.await(20, TimeUnit.SECONDS)).isTrue();
        assertThat(apply.job().batches()).isEqualTo(3);
        assertThat(apply.job().rowsSwept()).isEqualTo(2_500);
        assertThat(apply.job().remaining()).isEqualTo(7_500);
        release.countDown();
    }

    @Test
    void anErrorInAnApplyJobEndsItFailedWithTheRowsItHadSwept() throws Exception {
        Started apply = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.APPLY, "fp", (job, stage) -> {
            job.progress(2, 2_000, 5);
            throw new AssertionError("an Error, not an Exception");
        });
        await(() -> apply.job().state() == State.FAILED);

        Map<String, Object> body = apply.job().outcome().body();
        assertThat(apply.job().outcome().httpStatus()).isEqualTo(500);
        assertThat(body).containsEntry("reason", "sweep_failed").containsEntry("dry_run", false)
            .containsEntry("rows_swept", 2_000L).containsEntry("rows_changed", true);
    }

    @Test
    void evictingARunningJobHidesItButKeepsTheTenantsOneJobGuard_untilItEnds() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        Started running = jobs.start("t", "t", false, "fp", stage -> {
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        });

        assertThat(jobs.evictTenant("t")).isEqualTo(1);

        assertThat(jobs.evicted(running.job().id())).isTrue();
        assertThat(running.job().evicted()).isTrue();
        assertThat(jobs.find(running.job().id())).as("no poll finds it").isNull();
        assertThatThrownBy(() -> jobs.start("t", "t", false, "fp", stage -> ok("t")))
            .as("the same request does not join an invisible job").isInstanceOf(Conflict.class);
        assertThat(jobs.evictTenant("t")).as("already hidden: not counted twice").isZero();
        release.countDown();
        await(() -> running.job().state() == State.DONE);
        assertThat(jobs.start("t", "t", false, "fp", stage -> ok("t")).created())
            .as("the guard ends with the worker, and the hidden job is dropped").isTrue();
    }

    @Test
    void theWorkIsToldItsJob() throws Exception {
        java.util.concurrent.atomic.AtomicReference<String> seen = new java.util.concurrent.atomic.AtomicReference<>();
        Started s = jobs.start("t", "t", false, ChunkKeysSweepJobs.Kind.DRY_RUN, "fp", (job, stage) -> {
            seen.set(job.id() + "/" + job.kind().wire());
            return ok("t");
        });
        await(() -> s.job().state() == State.DONE);
        assertThat(seen.get()).isEqualTo(s.job().id() + "/dry_run");
    }
}
