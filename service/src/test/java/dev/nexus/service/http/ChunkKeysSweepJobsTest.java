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
}
