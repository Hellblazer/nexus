// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.ReembedJobRepository.BatchResult;
import dev.nexus.service.db.ReembedJobRepository.Job;
import dev.nexus.service.db.ReembedJobRepository.JobLockHeld;
import dev.nexus.service.db.ReembedJobRepository.LockContention;
import dev.nexus.service.db.ReembedJobRepository.Skip;
import dev.nexus.service.db.ReembedJobRepository.Transition;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static dev.nexus.service.db.ReembedJobRepository.ABORTED;
import static dev.nexus.service.db.ReembedJobRepository.CUTOVER;
import static dev.nexus.service.db.ReembedJobRepository.DONE;
import static dev.nexus.service.db.ReembedJobRepository.DONE_WITH_SKIPS;
import static dev.nexus.service.db.ReembedJobRepository.FAILED;
import static dev.nexus.service.db.ReembedJobRepository.LOCK_STATES;
import static dev.nexus.service.db.ReembedJobRepository.PAUSED;
import static dev.nexus.service.db.ReembedJobRepository.PLANNED;
import static dev.nexus.service.db.ReembedJobRepository.RUNNING;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOB_MOVES;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): {@link ReembedJobRepository} on a real Postgres (Testcontainers,
 * the product schema, the service role, so FORCE RLS applies to every read and write). Same setup as
 * {@code DocumentMoveTest}. Each test names its own tenant, so the tests share one database without
 * sharing a source lock.
 *
 * <p>Run: {@code -Dtest=ReembedJobRepositoryTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReembedJobRepositoryTest {

    private static final String SVC_ROLE = "svc_reembed_repo_test";
    private static final String SVC_PASS = "svc_reembed_repo_test_pass";
    private static final String SRC = "docs__owner__bge-base-en-v15-768__v1";
    private static final String TGT = "docs__owner__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    ReembedJobRepository repo;
    final AtomicInteger seq = new AtomicInteger();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setConnectionTimeout(10_000);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        repo = new ReembedJobRepository(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private String tenant() {
        return "reembed-repo-" + seq.incrementAndGet();
    }

    private Job apply(String tenant, Job job, String to) {
        return repo.applyTransition(tenant, job.jobId(), job.state(), job.cutoverStep(), to, Transition.NONE)
            .orElseThrow(() -> new AssertionError("transition " + job.state() + " -> " + to + " matched no row"));
    }

    // ── start, get, list ─────────────────────────────────────────────────────

    @Test
    void start_insertsAPlannedJobWithItsFlags_andGetAndListFindIt() {
        String t = tenant();
        Job job = repo.start(t, SRC, TGT, 5_000L, true, false);

        assertThat(job.state()).isEqualTo(PLANNED);
        assertThat(job.sourceCollection()).isEqualTo(SRC);
        assertThat(job.targetCollection()).isEqualTo(TGT);
        assertThat(job.tokenCap()).isEqualTo(5_000L);
        assertThat(job.keepSource()).isTrue();
        assertThat(job.forceDelete()).isFalse();
        assertThat(job.docCursor()).isEmpty();
        assertThat(job.passNumber()).isEqualTo(1);
        assertThat(job.documentsMoved()).isZero();
        assertThat(job.disposition()).isNull();
        assertThat(job.cutoverStep()).isNull();
        assertThat(job.finishedAt()).isNull();
        assertThat(job.startedAt()).isNotNull();

        assertThat(repo.get(t, job.jobId())).contains(job);
        assertThat(repo.listByStates(t, List.of(PLANNED))).containsExactly(job);
        assertThat(repo.listByStates(t, List.of(RUNNING, PAUSED))).isEmpty();
        assertThat(repo.listByStates(t, List.of())).isEmpty();
        assertThat(repo.get(t, UUID.randomUUID())).isEmpty();
    }

    @Test
    void aJobIsInvisibleToAnotherTenant() {
        String a = tenant();
        String b = tenant();
        Job job = repo.start(a, SRC, TGT, null, false, false);

        assertThat(repo.get(b, job.jobId())).isEmpty();
        assertThat(repo.listByStates(b, LOCK_STATES)).isEmpty();
        assertThat(repo.applyTransition(b, job.jobId(), PLANNED, null, RUNNING, Transition.NONE)).isEmpty();
        // and the same source in the other tenant is its own lock
        assertThat(repo.start(b, SRC, TGT, null, false, false).state()).isEqualTo(PLANNED);
    }

    // ── the source lock ──────────────────────────────────────────────────────

    @Test
    void secondStart_onASourceHeldInAnyLockState_isRefusedNamingTheHolder() {
        for (String lockState : LOCK_STATES) {
            String t = tenant();
            Job holder = inState(t, lockState);

            assertThatThrownBy(() -> repo.start(t, SRC, TGT, null, false, false))
                .as("a job in " + lockState + " holds its source")
                .isInstanceOfSatisfying(JobLockHeld.class, e -> {
                    assertThat(e.existingJobId()).isEqualTo(holder.jobId());
                    assertThat(e.existingState()).isEqualTo(lockState);
                    assertThat(e.getMessage()).contains(holder.jobId().toString()).contains(SRC);
                });
            assertThat(repo.listByStates(t, List.of(PLANNED, RUNNING, PAUSED, CUTOVER, FAILED)))
                .as("the refused start wrote nothing").containsExactly(holder);
        }
    }

    @Test
    void secondStart_whileTheFirstJobIsFailed_isRefusedNamingIt() {
        String t = tenant();
        Job job = repo.start(t, SRC, TGT, null, false, false);
        job = apply(t, job, RUNNING);
        Job failed = repo.applyTransition(t, job.jobId(), RUNNING, null, FAILED,
            Transition.failed("batch_failed", "embedder said no")).orElseThrow();
        assertThat(failed.failureReason()).isEqualTo("batch_failed");
        assertThat(failed.lastError()).isEqualTo("embedder said no");
        assertThat(failed.finishedAt()).as("a failed job is resumable, not finished").isNull();

        assertThatThrownBy(() -> repo.start(t, SRC, TGT, null, false, false))
            .isInstanceOfSatisfying(JobLockHeld.class, e -> {
                assertThat(e.existingJobId()).isEqualTo(failed.jobId());
                assertThat(e.existingState()).isEqualTo(FAILED);
            });
    }

    @Test
    void lockIsReleasedWhenTheJobReachesATerminalState() {
        for (String terminal : List.of(ABORTED, DONE, DONE_WITH_SKIPS)) {
            String t = tenant();
            Job first = repo.start(t, SRC, TGT, null, false, false);
            Job finished = repo.applyTransition(t, first.jobId(), PLANNED, null, terminal, Transition.NONE)
                .orElseThrow();
            assertThat(finished.finishedAt()).as(terminal + " stamps finished_at").isNotNull();

            Job second = repo.start(t, SRC, TGT, null, false, false);
            assertThat(second.jobId()).isNotEqualTo(first.jobId());
            assertThat(second.state()).as("a start after " + terminal).isEqualTo(PLANNED);
            assertThat(repo.get(t, first.jobId()).orElseThrow().state()).isEqualTo(terminal);
        }
    }

    @Test
    void reEnteringALockStateFromATerminalOne_whileAnotherJobHoldsTheSource_isRefused() {
        String t = tenant();
        Job first = repo.start(t, SRC, TGT, null, false, false);
        Job done = repo.applyTransition(t, first.jobId(), PLANNED, null, DONE, Transition.disposition("kept_tombstoned"))
            .orElseThrow();
        assertThat(done.disposition()).isEqualTo("kept_tombstoned");
        Job holder = repo.start(t, SRC, TGT, null, false, false);

        assertThatThrownBy(() -> repo.applyTransition(t, done.jobId(), DONE, null, RUNNING, Transition.NONE))
            .isInstanceOfSatisfying(JobLockHeld.class, e -> assertThat(e.existingJobId()).isEqualTo(holder.jobId()));
        assertThat(repo.get(t, done.jobId()).orElseThrow().state()).as("the refused re-entry changed nothing")
            .isEqualTo(DONE);

        // once the holder is out of the way, the same re-entry succeeds and clears finished_at
        repo.applyTransition(t, holder.jobId(), PLANNED, null, ABORTED, Transition.NONE).orElseThrow();
        Job back = repo.applyTransition(t, done.jobId(), DONE, null, RUNNING, Transition.NONE).orElseThrow();
        assertThat(back.state()).isEqualTo(RUNNING);
        assertThat(back.finishedAt()).isNull();
    }

    @Test
    void twoStartsOnOneSourceAtOnce_oneInserts_theOtherIsRefusedNamingTheWinner() throws Exception {
        String t = tenant();
        var ready = new CountDownLatch(2);
        var go = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            List<Future<Object>> futures = List.of(0, 1).stream().<Future<Object>>map(i -> pool.submit(() -> {
                ready.countDown();
                go.await();
                try {
                    return repo.start(t, SRC, TGT, null, false, false);
                } catch (JobLockHeld e) {
                    return e;
                }
            })).toList();
            ready.await();
            go.countDown();
            Object a = futures.get(0).get();
            Object b = futures.get(1).get();

            List<Job> winners = java.util.stream.Stream.of(a, b).filter(Job.class::isInstance).map(Job.class::cast)
                .toList();
            List<JobLockHeld> losers = java.util.stream.Stream.of(a, b).filter(JobLockHeld.class::isInstance)
                .map(JobLockHeld.class::cast).toList();
            assertThat(winners).as("exactly one start inserted").hasSize(1);
            assertThat(losers).as("the other was refused with the typed refusal").hasSize(1);
            assertThat(losers.get(0).existingJobId()).isEqualTo(winners.get(0).jobId());
            assertThat(losers.get(0).existingState()).isEqualTo(PLANNED);
            assertThat(repo.listByStates(t, LOCK_STATES)).containsExactly(winners.get(0));
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void start_whenTheHolderFinishesBetweenTheRefusalAndTheLookup_retriesOnceAndInserts() {
        String t = tenant();
        Job holder = repo.start(t, SRC, TGT, null, false, false);
        var hookCalls = new AtomicInteger();
        repo.setLockRetryHookForTests((phase, attempt) -> {
            hookCalls.incrementAndGet();
            if ("before_lookup".equals(phase) && attempt == 0) {
                repo.applyTransition(t, holder.jobId(), PLANNED, null, ABORTED, Transition.NONE).orElseThrow();
            }
        });
        try {
            Job second = repo.start(t, SRC, TGT, null, false, false);
            assertThat(second.state()).isEqualTo(PLANNED);
            assertThat(second.jobId()).isNotEqualTo(holder.jobId());
            assertThat(hookCalls.get()).as("the seam fired: the retry path ran").isEqualTo(2);
        } finally {
            repo.setLockRetryHookForTests(null);
        }
    }

    @Test
    void start_whenTheHolderIsGoneTwice_failsWithATypedRefusal_notTheRawUniqueViolation() {
        String t = tenant();
        Job first = repo.start(t, SRC, TGT, null, false, false);
        var second = new AtomicReference<Job>();
        repo.setLockRetryHookForTests((phase, attempt) -> {
            var other = new ReembedJobRepository(scope);
            if ("before_lookup".equals(phase) && attempt == 0) {
                other.applyTransition(t, first.jobId(), PLANNED, null, ABORTED, Transition.NONE).orElseThrow();
            } else if ("holder_gone".equals(phase)) {
                // a third job takes the source after the lookup found it free, so the retry is refused again
                second.set(other.start(t, SRC, TGT, null, false, false));
            } else if ("before_lookup".equals(phase)) {
                other.applyTransition(t, second.get().jobId(), PLANNED, null, ABORTED, Transition.NONE)
                    .orElseThrow();
            }
        });
        try {
            assertThatThrownBy(() -> repo.start(t, SRC, TGT, null, false, false))
                .isInstanceOf(LockContention.class)
                .hasMessageContaining(SRC);
        } finally {
            repo.setLockRetryHookForTests(null);
        }
    }

    // ── the CAS ──────────────────────────────────────────────────────────────

    @Test
    void applyTransition_refusesAStaleFromState_andChangesNothing() {
        String t = tenant();
        Job job = repo.start(t, SRC, TGT, null, false, false);
        Job running = repo.applyTransition(t, job.jobId(), PLANNED, null, RUNNING, Transition.NONE).orElseThrow();

        // a second actor decided from the same planned snapshot
        assertThat(repo.applyTransition(t, job.jobId(), PLANNED, null, ABORTED, Transition.NONE))
            .as("the job is no longer planned").isEmpty();
        assertThat(repo.get(t, job.jobId()).orElseThrow())
            .as("the stale decision left the row as the winner wrote it").isEqualTo(running);
        assertThat(repo.applyTransition(t, UUID.randomUUID(), PLANNED, null, RUNNING, Transition.NONE))
            .as("an absent job matches nothing").isEmpty();
    }

    @Test
    void applyTransition_carriesTheChange_resumeClearsTheReasonAndKeepsTheError_aResumeMayRaiseTheCap() {
        String t = tenant();
        Job job = repo.start(t, SRC, TGT, 100L, false, false);
        job = apply(t, job, RUNNING);
        Job failed = repo.applyTransition(t, job.jobId(), RUNNING, null, FAILED,
            Transition.failed("token_cap", "cap reached")).orElseThrow();
        assertThat(failed.failureReason()).isEqualTo("token_cap");

        Job resumed = repo.applyTransition(t, failed.jobId(), FAILED, null, RUNNING,
            new Transition(null, null, null, 900L)).orElseThrow();
        assertThat(resumed.failureReason()).as("resume clears the reason").isNull();
        assertThat(resumed.lastError()).as("and keeps the last error for diagnosis").isEqualTo("cap reached");
        assertThat(resumed.tokenCap()).isEqualTo(900L);
        assertThat(resumed.updatedAt()).isAfterOrEqualTo(failed.updatedAt());
    }

    @Test
    void applyTransition_keepsTheFailureReasonOnAbort_andOnlyASetReasonReplacesIt() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);
        Job failed = repo.applyTransition(t, job.jobId(), RUNNING, null, FAILED,
            Transition.failed("parity_mismatch", "counts differ")).orElseThrow();

        Job aborted = repo.applyTransition(t, failed.jobId(), FAILED, null, ABORTED, Transition.NONE).orElseThrow();
        assertThat(aborted.failureReason()).as("a failed job that is aborted still says why").isEqualTo("parity_mismatch");
        assertThat(aborted.lastError()).isEqualTo("counts differ");
        assertThat(aborted.finishedAt()).isNotNull();

        String t2 = tenant();
        Job failed2 = repo.applyTransition(t2, apply(t2, repo.start(t2, SRC, TGT, null, false, false), RUNNING).jobId(),
            RUNNING, null, FAILED, Transition.failed("batch_failed", "x")).orElseThrow();
        Job refailed = repo.applyTransition(t2, failed2.jobId(), FAILED, null, FAILED,
            Transition.failed("token_cap", "cap")).orElseThrow();
        assertThat(refailed.failureReason()).as("a set reason replaces the stored one").isEqualTo("token_cap");
        Job abortedWithReason = repo.applyTransition(t2, refailed.jobId(), FAILED, null, ABORTED,
            Transition.failed("profile_changed", "profile")).orElseThrow();
        assertThat(abortedWithReason.failureReason()).isEqualTo("profile_changed");
    }

    @Test
    void applyTransition_matchesOnTheCutoverMarker_soAnAbortDecidedBeforeTheDeleteStartedAppliesToNothing() {
        String t = tenant();
        Job job = repo.start(t, SRC, TGT, null, false, false);
        job = apply(t, job, RUNNING);
        Job cutover = apply(t, job, CUTOVER);
        assertThat(cutover.cutoverStep()).isNull();

        // the abort was decided while the marker was still null; the delete's marker commits first
        assertThat(repo.setCutoverStep(t, cutover.jobId(), "delete_started")).isTrue();
        assertThat(repo.applyTransition(t, cutover.jobId(), CUTOVER, null, ABORTED, Transition.NONE))
            .as("the marker moved under the abort's snapshot").isEmpty();
        assertThat(repo.get(t, cutover.jobId()).orElseThrow().state()).isEqualTo(CUTOVER);

        // the same state with the marker the caller now sees does apply
        assertThat(repo.applyTransition(t, cutover.jobId(), CUTOVER, "delete_started", DONE,
            Transition.disposition("deleted"))).isPresent();
    }

    @Test
    void setCutoverStep_setsOnlyWhileTheJobIsInCutover() {
        String t = tenant();
        Job job = repo.start(t, SRC, TGT, null, false, false);
        job = apply(t, job, RUNNING);
        assertThat(repo.setCutoverStep(t, job.jobId(), "superseded")).as("running is not cutover").isFalse();
        job = apply(t, job, CUTOVER);
        assertThat(repo.setCutoverStep(t, job.jobId(), "superseded")).isTrue();
        assertThat(repo.get(t, job.jobId()).orElseThrow().cutoverStep()).isEqualTo("superseded");
        Job aborted = repo.applyTransition(t, job.jobId(), CUTOVER, "superseded", ABORTED, Transition.NONE)
            .orElseThrow();
        assertThat(repo.setCutoverStep(t, aborted.jobId(), "delete_started"))
            .as("an abort that won the race stops the delete's marker").isFalse();
        assertThat(repo.get(t, job.jobId()).orElseThrow().cutoverStep()).isEqualTo("superseded");
    }

    @Test
    void setCutoverStep_isMonotone_aRestartedCutoverCannotMoveItBackAndReEnableAbort() {
        String t = tenant();
        Job job = apply(t, apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING), CUTOVER);

        assertThat(repo.setCutoverStep(t, job.jobId(), "superseded")).isTrue();
        assertThat(repo.setCutoverStep(t, job.jobId(), "superseded")).as("the same value is a no-op").isTrue();
        assertThat(repo.setCutoverStep(t, job.jobId(), "delete_started")).isTrue();
        Job atDelete = repo.get(t, job.jobId()).orElseThrow();
        assertThat(atDelete.cutoverStep()).isEqualTo("delete_started");

        // the restarted cutover re-runs from its first step
        assertThat(repo.setCutoverStep(t, job.jobId(), "superseded"))
            .as("the marker is already past superseded").isFalse();
        Job after = repo.get(t, job.jobId()).orElseThrow();
        assertThat(after.cutoverStep()).as("not moved backwards").isEqualTo("delete_started");
        assertThat(after.updatedAt()).as("and not touched").isEqualTo(atDelete.updatedAt());
        assertThat(repo.setCutoverStep(t, job.jobId(), "delete_started")).as("same value, still a no-op").isTrue();

        // an abort decided from a snapshot that predates the delete still refuses, and so does one decided
        // from the superseded marker the backward write would have restored
        assertThat(repo.applyTransition(t, job.jobId(), CUTOVER, "superseded", ABORTED, Transition.NONE)).isEmpty();
        assertThat(repo.applyTransition(t, job.jobId(), CUTOVER, null, ABORTED, Transition.NONE)).isEmpty();
        assertThat(repo.get(t, job.jobId()).orElseThrow().state()).isEqualTo(CUTOVER);

        assertThatThrownBy(() -> repo.setCutoverStep(t, job.jobId(), "bogus"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── batch and move records ───────────────────────────────────────────────

    @Test
    void recordMoveInTxn_countsEachDocumentOnce_andKeepsTheCursorMonotone() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);

        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.2"); return 0; });
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.9"); return 0; });
        // an earlier document moved later (a write-back drift picked up by a later pass) keeps the cursor
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.4"); return 0; });
        // the same document again: still one
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.9"); return 0; });

        Job after = repo.get(t, job.jobId()).orElseThrow();
        assertThat(after.documentsMoved()).isEqualTo(3);
        assertThat(after.docCursor()).isEqualTo("1.1.9");
        assertThat(movesOf(t, job.jobId(), "moved")).containsExactlyInAnyOrder("1.1.2", "1.1.4", "1.1.9");
    }

    @Test
    void recordMoveInTxn_rollsBackWithItsTransaction_andRefusesAnAbsentJob() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);

        assertThatThrownBy(() -> scope.withTenant(t, ctx -> {
            ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.1");
            throw new IllegalStateException("the move failed after its record");
        })).hasMessageContaining("the move failed");
        assertThat(repo.get(t, job.jobId()).orElseThrow().documentsMoved()).isZero();
        assertThat(movesOf(t, job.jobId(), "moved")).isEmpty();

        assertThatThrownBy(() -> scope.withTenant(t, ctx -> {
            ReembedJobRepository.recordMoveInTxn(ctx, t, UUID.randomUUID(), "1.1.1");
            return 0;
        })).hasMessageContaining("does not exist");
    }

    @Test
    void advanceAfterBatch_foldsSkipsCountersAndCursor_countsASkipOnce_andFoldsIntoAPausedJob() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);

        Job one = repo.advanceAfterBatch(t, job.jobId(), new BatchResult(
            List.of(new Skip("1.1.3", "move_precondition_failed")), 10, 4, 700, "1.1.3")).orElseThrow();
        assertThat(one.documentsSkipped()).isEqualTo(1);
        assertThat(one.chunksEmbedded()).isEqualTo(10);
        assertThat(one.chunksReused()).isEqualTo(4);
        assertThat(one.tokensBilled()).isEqualTo(700);
        assertThat(one.docCursor()).as("the cursor passes the skipped document").isEqualTo("1.1.3");

        Job two = repo.advanceAfterBatch(t, job.jobId(), new BatchResult(
            List.of(new Skip("1.1.3", "move_precondition_failed")), 5, 0, 300, "1.1.1")).orElseThrow();
        assertThat(two.documentsSkipped()).as("a document skipped twice is counted once").isEqualTo(1);
        assertThat(two.chunksEmbedded()).isEqualTo(15);
        assertThat(two.tokensBilled()).isEqualTo(1000);
        assertThat(two.docCursor()).as("the cursor never goes back").isEqualTo("1.1.3");
        assertThat(movesOf(t, job.jobId(), "skipped")).containsExactly("1.1.3");

        // a skipped document that is later moved changes sides
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.3"); return 0; });
        Job moved = repo.get(t, job.jobId()).orElseThrow();
        assertThat(moved.documentsMoved()).isEqualTo(1);
        assertThat(moved.documentsSkipped()).isZero();

        // a pause landing mid-batch: the batch happened, so its counters, skips and cursor still fold, and
        // the state stays as the pause left it
        Job paused = apply(t, moved, PAUSED);
        Job foldedWhilePaused = repo.advanceAfterBatch(t, paused.jobId(),
            new BatchResult(List.of(new Skip("1.1.7", "x")), 2, 1, 50, "1.1.9")).orElseThrow();
        assertThat(foldedWhilePaused.state()).isEqualTo(PAUSED);
        assertThat(foldedWhilePaused.chunksEmbedded()).isEqualTo(17);
        assertThat(foldedWhilePaused.chunksReused()).isEqualTo(5);
        assertThat(foldedWhilePaused.tokensBilled()).isEqualTo(1050);
        assertThat(foldedWhilePaused.documentsSkipped()).isEqualTo(1);
        assertThat(foldedWhilePaused.docCursor()).isEqualTo("1.1.9");
        assertThat(movesOf(t, job.jobId(), "skipped")).containsExactly("1.1.7");
    }

    @Test
    void advanceAfterBatch_foldsIntoAnAbortedJob_andRefusesAFinishedOrAbsentOne() {
        String t = tenant();
        Job running = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);
        Job aborted = apply(t, running, ABORTED);

        Job folded = repo.advanceAfterBatch(t, aborted.jobId(), new BatchResult(List.of(), 3, 2, 400, "1.1.5"))
            .orElseThrow();
        assertThat(folded.state()).isEqualTo(ABORTED);
        assertThat(folded.tokensBilled()).as("tokens billed by the in-flight batch are counted").isEqualTo(400);
        assertThat(folded.chunksEmbedded()).isEqualTo(3);
        assertThat(folded.finishedAt()).isEqualTo(aborted.finishedAt());

        for (String finished : List.of(DONE, DONE_WITH_SKIPS)) {
            String t2 = tenant();
            Job done = apply(t2, apply(t2, repo.start(t2, SRC, TGT, null, false, false), RUNNING), finished);
            assertThat(repo.advanceAfterBatch(t2, done.jobId(), new BatchResult(List.of(), 1, 1, 1, "9")))
                .as("a " + finished + " job takes no batch").isEmpty();
            assertThat(repo.get(t2, done.jobId()).orElseThrow().tokensBilled()).isZero();
        }
        assertThat(repo.advanceAfterBatch(t, UUID.randomUUID(), new BatchResult(List.of(), 1, 1, 1, "9"))).isEmpty();
    }

    // ── revived ──────────────────────────────────────────────────────────────

    @Test
    void markRevived_marksTheMostRecentFinishedJobTheOtherWay() {
        String t = tenant();
        // an earlier job went SRC -> TGT and finished kept_by_flag; a later one goes TGT -> SRC
        Job earlier = repo.start(t, SRC, TGT, null, true, false);
        repo.applyTransition(t, earlier.jobId(), PLANNED, null, DONE, Transition.disposition("kept_by_flag"))
            .orElseThrow();

        Optional<Job> revived = repo.markRevived(t, SRC, TGT);
        assertThat(revived).isPresent();
        assertThat(revived.get().jobId()).isEqualTo(earlier.jobId());
        assertThat(revived.get().disposition()).isEqualTo("revived");
        assertThat(revived.get().state()).as("state is left as it stands").isEqualTo(DONE);

        assertThat(repo.markRevived(t, TGT, SRC)).as("no job went the other way").isEmpty();
        Job live = repo.start(t, "docs__owner__x__v1", TGT, null, false, false);
        assertThat(repo.markRevived(t, "docs__owner__x__v1", TGT))
            .as("a job still in a lock state is not a finished one").isEmpty();
        assertThat(repo.get(t, live.jobId()).orElseThrow().disposition()).isNull();
    }

    @Test
    void markRevived_marksAnAbortedJob_andLeavesItAborted() {
        String t = tenant();
        Job earlier = repo.start(t, SRC, TGT, null, false, false);
        repo.applyTransition(t, earlier.jobId(), PLANNED, null, ABORTED, Transition.NONE).orElseThrow();

        Job revived = repo.markRevived(t, SRC, TGT).orElseThrow();
        assertThat(revived.jobId()).isEqualTo(earlier.jobId());
        assertThat(revived.disposition()).isEqualTo("revived");
        assertThat(revived.state()).isEqualTo(ABORTED);
    }

    @Test
    void markRevived_doesNotMarkAFailedJob_itHoldsTheLockSoTheStartRefusesTheReviveFirst() {
        String t = tenant();
        Job failed = inState(t, FAILED);

        assertThat(repo.markRevived(t, SRC, TGT)).isEmpty();
        Job after = repo.get(t, failed.jobId()).orElseThrow();
        assertThat(after.disposition()).isNull();
        assertThat(after.state()).isEqualTo(FAILED);
        // the lock that makes the case unreachable: a job going the other way cannot start while it holds SRC
        assertThatThrownBy(() -> repo.start(t, SRC, TGT, null, false, false)).isInstanceOf(JobLockHeld.class);
    }

    @Test
    void markRevived_doesNotMarkADoneWithSkipsJob_itNeverSupersededItsSource() {
        String t = tenant();
        Job job = apply(t, apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING), DONE_WITH_SKIPS);

        assertThat(repo.markRevived(t, SRC, TGT)).isEmpty();
        assertThat(repo.get(t, job.jobId()).orElseThrow().disposition()).isNull();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** A job on {@link #SRC} in {@code state}, reached by transitions the repository accepts. */
    private Job inState(String t, String state) {
        Job job = repo.start(t, SRC, TGT, null, false, false);
        return switch (state) {
            case PLANNED -> job;
            case RUNNING -> apply(t, job, RUNNING);
            case PAUSED -> apply(t, apply(t, job, RUNNING), PAUSED);
            case CUTOVER -> apply(t, apply(t, job, RUNNING), CUTOVER);
            case FAILED -> apply(t, apply(t, job, RUNNING), FAILED);
            default -> throw new IllegalArgumentException(state);
        };
    }

    private List<String> movesOf(String t, UUID jobId, String outcome) {
        return scope.withTenant(t, ctx -> ctx.select(REEMBED_JOB_MOVES.DOC_ID).from(REEMBED_JOB_MOVES)
            .where(REEMBED_JOB_MOVES.TENANT_ID.eq(t))
            .and(REEMBED_JOB_MOVES.JOB_ID.eq(jobId))
            .and(REEMBED_JOB_MOVES.OUTCOME.eq(outcome))
            .fetch(REEMBED_JOB_MOVES.DOC_ID));
    }
}
