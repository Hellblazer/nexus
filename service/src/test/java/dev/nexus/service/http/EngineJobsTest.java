// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.http.EngineJobs.Bounds;
import dev.nexus.service.http.EngineJobs.Busy;
import dev.nexus.service.http.EngineJobs.Conflict;
import dev.nexus.service.http.EngineJobs.Job;
import dev.nexus.service.http.EngineJobs.Outcome;
import dev.nexus.service.http.EngineJobs.Started;
import dev.nexus.service.http.EngineJobs.State;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.BooleanSupplier;

/**
 * RDR-193 Step 1 (nexus-u5m5g.2): the shared engine job registry. Pinned: the lifecycle (running, done, failed), the
 * start rules within one scope (the same request joins the running job, a different one is a 409 naming it, a full
 * pool and queue is a 503), an unknown id finds nothing (the 404 that tells a client to resubmit), retention, the
 * tenant reset, the reconcile instance's tenant-plus-owner scope, and (residual 3 of the RDR-193 accept) that a job is
 * found only by its own tenant and evicted by its tenant whatever its scope key.
 */
class EngineJobsTest {

    private final AtomicLong now = new AtomicLong(1_000_000L);
    private EngineJobs<Void> jobs = new EngineJobs<>("engine-jobs-test-", now::get,
        new Bounds(2, 2, 64, Duration.ofMinutes(30), Duration.ofMinutes(15)), EngineJobs.defaultFailure());

    @AfterEach
    void stop() {
        jobs.shutdown();
    }

    private static Outcome ok(String tenant) {
        return new Outcome(200, Map.of("tenant", tenant));
    }

    private static void await(BooleanSupplier condition) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("condition not met within 20 s");
            }
            Thread.sleep(5);
        }
    }

    private Started<Void> hold(String tenant, String owner, String fp, CountDownLatch release) {
        return jobs.start(tenant, owner, fp, (job, stage) -> {
            release.await(20, TimeUnit.SECONDS);
            return ok(tenant);
        });
    }

    @Test
    void aJobRunsThenEndsDone_andThePollReadsItsStageAndOutcome() throws Exception {
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        Started<Void> s = jobs.start("t", null, "fp", (job, stage) -> {
            stage.accept("staging");
            inside.countDown();
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        });

        assertThat(s.created()).isTrue();
        assertThat(inside.await(20, TimeUnit.SECONDS)).isTrue();
        Job<Void> running = jobs.find(s.job().id(), "t");
        assertThat(running.state()).isEqualTo(State.RUNNING);
        assertThat(running.stage()).isEqualTo("staging");
        assertThat(running.outcome()).isNull();

        release.countDown();
        await(() -> jobs.find(s.job().id(), "t").state() != State.RUNNING);
        Job<Void> done = jobs.find(s.job().id(), "t");
        assertThat(done.state()).isEqualTo(State.DONE);
        assertThat(done.stage()).isEqualTo("done");
        assertThat(done.outcome().httpStatus()).isEqualTo(200);
    }

    @Test
    void aWorkThatThrowsEndsFailedWith500_namingTheClassNeverTheMessage() throws Exception {
        Started<Void> s = jobs.start("t", null, "fp", (job, stage) -> {
            throw new IllegalStateException("secret detail");
        });
        await(() -> jobs.find(s.job().id(), "t").state() != State.RUNNING);

        Job<Void> failed = jobs.find(s.job().id(), "t");
        assertThat(failed.state()).isEqualTo(State.FAILED);
        assertThat(failed.stage()).isEqualTo("failed");
        assertThat(failed.outcome().httpStatus()).isEqualTo(500);
        assertThat(failed.outcome().body()).containsEntry("reason", "job_failed").containsEntry("tenant", "t");
        assertThat(failed.outcome().body().toString()).contains("IllegalStateException").doesNotContain("secret");
    }

    @Test
    void aWorkThatAnswersNon200EndsFailedWithItsOwnOutcome() throws Exception {
        Started<Void> s = jobs.start("t", null, "fp", (job, stage) -> new Outcome(409, Map.of("reason", "stale")));
        await(() -> jobs.find(s.job().id(), "t").state() != State.RUNNING);

        assertThat(jobs.find(s.job().id(), "t").state()).isEqualTo(State.FAILED);
        assertThat(jobs.find(s.job().id(), "t").outcome().body()).containsEntry("reason", "stale");
    }

    @Test
    void theSameRequestIsHandedTheRunningJob_aDifferentOneIsAConflictNamingIt() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        Started<Void> first = hold("t", null, "fp-a", release);

        Started<Void> again = jobs.start("t", null, "fp-a", (job, stage) -> ok("never"));
        assertThat(again.created()).isFalse();
        assertThat(again.job().id()).isEqualTo(first.job().id());

        assertThatThrownBy(() -> jobs.start("t", null, "fp-b", (job, stage) -> ok("never")))
            .isInstanceOfSatisfying(Conflict.class, c -> assertThat(c.jobId()).isEqualTo(first.job().id()))
            .hasMessageContaining(first.job().id());

        release.countDown();
        await(() -> jobs.find(first.job().id(), "t").state() == State.DONE);
        Started<Void> after = jobs.start("t", null, "fp-a", (job, stage) -> ok("t"));
        assertThat(after.created()).as("a finished job is never handed to a new start").isTrue();
        assertThat(after.job().id()).isNotEqualTo(first.job().id());
    }

    @Test
    void aFullPoolAndQueueIsBusy() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        try {
            for (int i = 0; i < 4; i++) {   // 2 running + 2 queued
                hold("t" + i, null, "fp", release);
            }
            assertThatThrownBy(() -> hold("t9", null, "fp", release)).isInstanceOf(Busy.class);
            assertThat(jobs.size()).as("a refused start holds nothing").isEqualTo(4);
        } finally {
            release.countDown();
        }
    }

    @Test
    void anUnknownIdFindsNothing_whileAnotherJobIsHeld() throws Exception {
        Started<Void> s = jobs.start("t", null, "fp", (job, stage) -> ok("t"));
        await(() -> s.job().state() == State.DONE);
        assertThat(jobs.find(s.job().id(), "t")).isNotNull();
        assertThat(jobs.find("no-such-job", "t")).isNull();
    }

    @Test
    void aFailureHookThatThrowsStillEndsTheJob_withTheDefaultOutcome() throws Exception {
        jobs.shutdown();
        jobs = new EngineJobs<>("engine-jobs-test-", now::get,
            new Bounds(2, 2, 64, Duration.ofMinutes(30), Duration.ofMinutes(15)),
            (job, thrown) -> { throw new IllegalStateException("the hook itself failed"); });
        Started<Void> s = jobs.start("t", null, "fp", (job, stage) -> {
            throw new IllegalArgumentException("work failed");
        });
        await(() -> s.job().state() != State.RUNNING);
        assertThat(s.job().state()).isEqualTo(State.FAILED);
        assertThat(s.job().outcome().body()).containsEntry("reason", "job_failed");
        assertThat(jobs.start("t", null, "fp-next", (job, stage) -> ok("t")).created())
            .as("the scope is free again").isTrue();
    }

    @Test
    void aFinishedJobIsGoneAfterTheRetentionPeriod() throws Exception {
        Started<Void> s = jobs.start("t", null, "fp", (job, stage) -> ok("t"));
        await(() -> jobs.find(s.job().id(), "t").state() == State.DONE);

        now.addAndGet(Duration.ofMinutes(29).toMillis());
        assertThat(jobs.find(s.job().id(), "t")).isNotNull();
        now.addAndGet(Duration.ofMinutes(2).toMillis());
        assertThat(jobs.find(s.job().id(), "t")).isNull();
        assertThat(jobs.size()).isZero();
    }

    @Test
    void aJobStopsByItselfAtTheMaximumRuntime() throws Exception {
        CountDownLatch inside = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        Started<Void> s = jobs.start("t", null, "fp", (job, stage) -> {
            inside.countDown();
            release.await(20, TimeUnit.SECONDS);
            return new Outcome(job.stopReason() == null ? 200 : 500, Map.of("stopped", String.valueOf(job.stopReason())));
        });
        assertThat(inside.await(20, TimeUnit.SECONDS)).isTrue();
        assertThat(s.job().stopReason()).isNull();
        now.addAndGet(Duration.ofMinutes(15).toMillis());
        assertThat(s.job().stopReason()).isEqualTo("wall_clock_cap");
        release.countDown();
        await(() -> s.job().state() != State.RUNNING);
        assertThat(s.job().outcome().body()).containsEntry("stopped", "wall_clock_cap");
    }

    @Test
    void evictTenantHidesItsRunningJobsAndRemovesItsFinishedOnes_otherTenantsKeepTheirs() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        Started<Void> done = jobs.start("t", null, "fp-done", (job, stage) -> ok("t"));
        await(() -> jobs.find(done.job().id(), "t").state() == State.DONE);
        Started<Void> running = hold("t", null, "fp-run", release);
        Started<Void> other = jobs.start("u", null, "fp", (job, stage) -> ok("u"));
        await(() -> jobs.find(other.job().id(), "u").state() == State.DONE);

        assertThat(jobs.evictTenant("t")).isEqualTo(2);
        assertThat(jobs.find(done.job().id(), "t")).isNull();
        assertThat(jobs.find(running.job().id(), "t")).as("a running job is hidden").isNull();
        assertThat(jobs.evicted(running.job().id())).isTrue();
        assertThat(running.job().stopReason()).isEqualTo("evicted");
        assertThat(jobs.find(other.job().id(), "u")).isNotNull();

        assertThatThrownBy(() -> jobs.start("t", null, "fp-run", (job, stage) -> ok("never")))
            .as("an evicted running job still holds its scope until it ends, and is never joined")
            .isInstanceOf(Conflict.class);
        release.countDown();
        await(() -> running.job().state() != State.RUNNING);
        assertThat(jobs.start("t", null, "fp-run", (job, stage) -> ok("t")).created()).isTrue();
    }

    @Test
    void theReconcileInstanceRunsTwoOwnersOfOneTenantAtOnce() throws Exception {
        jobs.shutdown();
        jobs = EngineJobs.reconcile(now::get);
        CountDownLatch bothInside = new CountDownLatch(2);
        CountDownLatch release = new CountDownLatch(1);
        EngineJobs.Work<Void> work = (job, stage) -> {
            bothInside.countDown();
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        };
        try {
            Started<Void> a = jobs.start("t", "1.1", "fp-a", work);
            Started<Void> b = jobs.start("t", "1.2", "fp-b", work);
            assertThat(a.created()).isTrue();
            assertThat(b.created()).as("another owner of the same tenant is not a conflict").isTrue();
            assertThat(bothInside.await(20, TimeUnit.SECONDS)).as("both run at once").isTrue();
            assertThatThrownBy(() -> jobs.start("t", "1.1", "fp-c", work))
                .as("the same owner with a different request is a conflict")
                .isInstanceOf(Conflict.class);
        } finally {
            release.countDown();
        }
    }

    @Test
    void theReconcileBoundsAreTheOnesTheRdrSets() {
        assertThat(EngineJobs.RECONCILE).isEqualTo(
            new Bounds(4, 32, 256, Duration.ofMinutes(30), Duration.ofMinutes(15)));
    }

    @Test
    void theReconcileInstanceRunsFourAtOnce_queuesThirtyTwo_refusesTheNext_andCapsAJobAtFifteenMinutes()
            throws Exception {
        jobs.shutdown();
        jobs = EngineJobs.reconcile(now::get);
        CountDownLatch release = new CountDownLatch(1);
        java.util.concurrent.atomic.AtomicInteger inside = new java.util.concurrent.atomic.AtomicInteger();
        EngineJobs.Work<Void> work = (job, stage) -> {
            inside.incrementAndGet();
            release.await(20, TimeUnit.SECONDS);
            return ok("t");
        };
        try {
            Started<Void> first = jobs.start("t", "owner-0", "fp", work);
            for (int i = 1; i < 36; i++) {
                jobs.start("t", "owner-" + i, "fp", work);
            }
            await(() -> inside.get() == 4);
            assertThatThrownBy(() -> jobs.start("t", "owner-36", "fp", work))
                .as("4 running and 32 queued: the 37th start is refused")
                .isInstanceOf(Busy.class);
            assertThat(inside.get()).as("no fifth worker").isEqualTo(4);

            assertThat(first.job().stopReason()).isNull();
            now.addAndGet(Duration.ofMinutes(15).toMillis() - 1);
            assertThat(first.job().stopReason()).isNull();
            now.addAndGet(1);
            assertThat(first.job().stopReason()).isEqualTo("wall_clock_cap");
        } finally {
            release.countDown();
        }
    }

    @Test
    void twoTenantsWithTheSameOwnerNameNeverShareAScope() throws Exception {
        jobs.shutdown();
        jobs = EngineJobs.reconcile(now::get);
        CountDownLatch release = new CountDownLatch(1);
        try {
            Started<Void> a = hold("t", "1.1", "fp", release);
            Started<Void> b = hold("u", "1.1", "fp", release);
            assertThat(b.created()).as("tenant u is never handed tenant t's job").isTrue();
            assertThat(b.job().id()).isNotEqualTo(a.job().id());
            assertThatThrownBy(() -> jobs.start("u", "1.1", "fp-other", (job, stage) -> ok("never")))
                .as("a different request in u's scope names u's job, never t's")
                .isInstanceOfSatisfying(Conflict.class, c -> assertThat(c.jobId()).isEqualTo(b.job().id()));
        } finally {
            release.countDown();
        }
    }

    @Test
    void residual3_anotherTenantsPollFindsNothing() throws Exception {
        Started<Void> s = jobs.start("t", "1.1", "fp", (job, stage) -> ok("t"));
        await(() -> s.job().state() == State.DONE);

        assertThat(jobs.find(s.job().id(), "t")).isNotNull();
        assertThat(jobs.find(s.job().id(), "u")).as("another tenant's poll answers 404").isNull();
    }

    @Test
    void residual3_evictTenantDropsAJobKeyedByTenantPlusOwner() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        Started<Void> done = jobs.start("t", "1.1", "fp", (job, stage) -> ok("t"));
        await(() -> done.job().state() == State.DONE);
        Started<Void> running = hold("t", "1.2", "fp", release);
        try {
            assertThat(jobs.evictTenant("t")).isEqualTo(2);
            assertThat(jobs.find(done.job().id(), "t")).isNull();
            assertThat(jobs.find(running.job().id(), "t")).isNull();
            assertThat(jobs.evicted(running.job().id())).isTrue();
        } finally {
            release.countDown();
        }
    }
}
