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
import static org.assertj.core.api.Assertions.catchThrowable;

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
    private static final String MID = "docs__owner__mid-model__v1";
    private static final String OTHER = "docs__owner__other-model__v1";

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
            if (!"after_start_reads".equals(phase)) {
                hookCalls.incrementAndGet();    // the lock-retry phases only; after_start_reads is start's own seam
            }
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
    void advanceAfterBatch_aSkipOfADocumentThatMoved_flipsItToSkipped_theMirrorOfRecordMoveInTxn() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.1"); return 0; });
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.2"); return 0; });

        // 1.1.1 moved, a client wrote it back to the source, and the last pass could not move it again
        Job flipped = repo.advanceAfterBatch(t, job.jobId(), new BatchResult(
            List.of(new Skip("1.1.1", "pass_limit")), 0, 0, 0, null)).orElseThrow();

        assertThat(flipped.documentsMoved()).as("only 1.1.2 still counts as moved").isEqualTo(1);
        assertThat(flipped.documentsSkipped()).isEqualTo(1);
        assertThat(movesOf(t, job.jobId(), "moved")).containsExactly("1.1.2");
        assertThat(movesOf(t, job.jobId(), "skipped")).containsExactly("1.1.1");
        assertThat(flipped.docCursor()).as("a null cursor leaves the cursor where the moves put it").isEqualTo("1.1.2");

        Job again = repo.advanceAfterBatch(t, job.jobId(), new BatchResult(
            List.of(new Skip("1.1.1", "pass_limit")), 0, 0, 0, null)).orElseThrow();
        assertThat(again.documentsMoved()).isEqualTo(1);
        assertThat(again.documentsSkipped()).as("the same skip again is counted once").isEqualTo(1);
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

    // ── start: a source that is another job's target (C3, the C2 critique) ───

    @Test
    void start_onASourceThatIsAnotherLockStateJobsTarget_isRefusedNamingThatJob() {
        for (String lockState : LOCK_STATES) {
            String t = tenant();
            Job filling = inState(t, lockState);

            // the new job would move documents OUT of the collection `filling` is moving them INTO
            assertThatThrownBy(() -> repo.start(t, TGT, SRC, null, false, false))
                .as("TGT is the target of a job in " + lockState)
                .isInstanceOfSatisfying(JobLockHeld.class, e -> {
                    assertThat(e.existingJobId()).isEqualTo(filling.jobId());
                    assertThat(e.existingState()).isEqualTo(lockState);
                    assertThat(e.getMessage()).contains(filling.jobId().toString()).contains(TGT).contains("target");
                });
            assertThat(repo.listByStates(t, List.of(PLANNED, RUNNING, PAUSED, CUTOVER, FAILED)))
                .as("the refused start wrote nothing").containsExactly(filling);
        }
    }

    @Test
    void start_onASourceThatWasAnotherJobsTarget_isAllowedOnceThatJobFinished() {
        for (String terminal : List.of(ABORTED, DONE, DONE_WITH_SKIPS)) {
            String t = tenant();
            Job first = repo.start(t, SRC, TGT, null, false, false);
            repo.applyTransition(t, first.jobId(), PLANNED, null, terminal, Transition.NONE).orElseThrow();

            Job back = repo.start(t, TGT, SRC, null, false, false);
            assertThat(back.state()).as("TGT was the target of a job that is " + terminal).isEqualTo(PLANNED);
        }
    }

    @Test
    void start_onATargetThatIsAnotherLockStateJobsSource_isRefusedNamingThatJob() {
        for (String lockState : LOCK_STATES) {
            String t = tenant();
            Job draining = inState(t, lockState);    // SRC -> TGT

            // the new job would move documents INTO the collection `draining` is moving them OUT of
            assertThatThrownBy(() -> repo.start(t, OTHER, SRC, null, false, false))
                .as("SRC is the source of a job in " + lockState)
                .isInstanceOfSatisfying(JobLockHeld.class, e -> {
                    assertThat(e.existingJobId()).isEqualTo(draining.jobId());
                    assertThat(e.existingState()).isEqualTo(lockState);
                    assertThat(e.getMessage()).contains(draining.jobId().toString()).contains(SRC).contains("source");
                });
            assertThat(repo.listByStates(t, List.of(PLANNED, RUNNING, PAUSED, CUTOVER, FAILED)))
                .as("the refused start wrote nothing").containsExactly(draining);
        }
    }

    @Test
    void start_onATargetThatWasAnotherJobsSource_isAllowedOnceThatJobFinished() {
        for (String terminal : List.of(ABORTED, DONE, DONE_WITH_SKIPS)) {
            String t = tenant();
            Job first = repo.start(t, SRC, TGT, null, false, false);
            repo.applyTransition(t, first.jobId(), PLANNED, null, terminal, Transition.NONE).orElseThrow();

            Job into = repo.start(t, OTHER, SRC, null, false, false);
            assertThat(into.state()).as("SRC was the source of a job that is " + terminal).isEqualTo(PLANNED);
        }
    }

    @Test
    void twoStartsThatMeetOnDifferentNames_areSerialised_oneInserts_theOtherIsRefusedNamingIt() throws Exception {
        // A moves SRC to MID and B moves MID to TGT: MID is A's target and B's source. The source-lock index
        // covers only two jobs on one source, so without the tenant's start lock each start reads the other's
        // absence and both insert.
        String t = tenant();
        var pausedInReads = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var a = new ReembedJobRepository(scope);
        a.setLockRetryHookForTests((phase, attempt) -> {
            if ("after_start_reads".equals(phase)) {
                pausedInReads.countDown();
                try {
                    release.await(30, java.util.concurrent.TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        });
        var b = new ReembedJobRepository(scope);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<Job> startA = pool.submit(() -> a.start(t, SRC, MID, null, false, false));
            assertThat(pausedInReads.await(30, java.util.concurrent.TimeUnit.SECONDS))
                .as("A reached the gap between its reads and its insert").isTrue();

            Future<Job> startB = pool.submit(() -> b.start(t, MID, TGT, null, false, false));
            // B must be held behind A's lock: given a second it has still not finished. (Without the lock it
            // reads, inserts and returns in milliseconds, and the assertions below fail.) One second, not more:
            // B's wait for the guard is bounded at GUARD_LOCK_TIMEOUT_MS, and this probe must end well inside it.
            assertThatThrownBy(() -> startB.get(1, java.util.concurrent.TimeUnit.SECONDS))
                .isInstanceOf(java.util.concurrent.TimeoutException.class);

            release.countDown();
            Job winner = startA.get(30, java.util.concurrent.TimeUnit.SECONDS);
            Throwable refused = catchThrowable(() -> startB.get(30, java.util.concurrent.TimeUnit.SECONDS));
            assertThat(refused).isInstanceOf(java.util.concurrent.ExecutionException.class);
            assertThat(refused.getCause()).isInstanceOfSatisfying(JobLockHeld.class, held -> {
                assertThat(held.existingJobId()).isEqualTo(winner.jobId());
                assertThat(held.getMessage()).contains(MID).contains("target");
            });
            assertThat(repo.listByStates(t, LOCK_STATES)).containsExactly(winner);
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    // ── the start guard: bounded wait, and the shared half held by a rename ──

    /**
     * Open a transaction that does what a rename does at its guard, {@link CatalogRepository#refuseUnderReembedJob}
     * (the guard taken SHARED, then the jobs read), and keep it open until {@code release}. Calling the guard
     * method itself, rather than a copy of its lock line, is what ties these tests to the rename path: delete the
     * {@code pg_advisory_xact_lock_shared} statement there and no start waits on anything.
     */
    private Future<?> renameHoldingTheGuard(ExecutorService pool, String t, CountDownLatch held,
                                            CountDownLatch release) {
        return pool.submit(() -> scope.withTenant(t, ctx -> {
            CatalogRepository.refuseUnderReembedJob(ctx, t, "rename", SRC, OTHER);
            held.countDown();
            try {
                release.await(60, java.util.concurrent.TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return null;
        }));
    }

    /** Sessions currently queued for this tenant's start guard (a not-granted advisory lock on its key). */
    private int guardWaiters(String t) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var key = org.jooq.impl.DSL.function("hashtext", Integer.class,
                org.jooq.impl.DSL.val(CatalogRepository.reembedGuardKey(t))).cast(Long.class).bitAnd(4294967295L);
            return org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES).fetchCount(
                org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("pg_locks")),
                org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("locktype"), String.class).eq("advisory")
                    .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("granted"), Boolean.class).isFalse())
                    .and(org.jooq.impl.DSL.field(org.jooq.impl.DSL.name("objid")).cast(Long.class).eq(key)));
        }
    }

    @Test
    void start_waitsForARenameThatHoldsTheGuardShared_andInsertsOnlyAfterItCommits() throws Exception {
        String t = tenant();
        var held = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var events = new java.util.concurrent.CopyOnWriteArrayList<String>();
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            Future<?> rename = renameHoldingTheGuard(pool, t, held, release);
            assertThat(held.await(30, java.util.concurrent.TimeUnit.SECONDS)).as("the rename holds its guard").isTrue();

            Future<Job> start = pool.submit(() -> {
                Job job = repo.start(t, SRC, TGT, null, false, false);
                events.add("start_returned");
                return job;
            });
            long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(5);
            while (guardWaiters(t) == 0 && !start.isDone() && System.nanoTime() < deadline) {
                Thread.sleep(20);
            }
            // The non-vacuity half: if the rename's shared lock line is deleted, start takes its exclusive lock at
            // once, commits in milliseconds, and this fails. Release well inside GUARD_LOCK_TIMEOUT_MS.
            assertThat(start.isDone()).as("start returned while a rename held the guard: the shared half is gone")
                .isFalse();
            assertThat(guardWaiters(t)).as("start is queued on the tenant's guard").isEqualTo(1);
            assertThat(repo.listByStates(t, LOCK_STATES)).as("nothing committed in between").isEmpty();

            events.add("rename_released");
            release.countDown();
            rename.get(30, java.util.concurrent.TimeUnit.SECONDS);
            Job job = start.get(30, java.util.concurrent.TimeUnit.SECONDS);

            assertThat(events).as("the start finished after the rename let go, never before")
                .containsExactly("rename_released", "start_returned");
            assertThat(repo.listByStates(t, LOCK_STATES)).containsExactly(job);
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    @Test
    void start_whenARenameHoldsTheGuardPastTheBound_isARetryableLockContention_andWritesNothing() throws Exception {
        String t = tenant();
        var held = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(1);
        try {
            Future<?> rename = renameHoldingTheGuard(pool, t, held, release);
            assertThat(held.await(30, java.util.concurrent.TimeUnit.SECONDS)).isTrue();

            long begun = System.nanoTime();
            Throwable refused = catchThrowable(() -> repo.start(t, SRC, TGT, null, false, false));
            long millis = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - begun);

            assertThat(refused).isInstanceOf(LockContention.class).hasMessageContaining("rename").hasMessageContaining(SRC);
            assertThat(millis).as("it waited out the guard bound, not less")
                .isGreaterThanOrEqualTo(ReembedJobRepository.GUARD_LOCK_TIMEOUT_MS - 200L);
            assertThat(millis).as("and did not wait unbounded").isLessThan(20_000L);
            assertThat(repo.listByStates(t, LOCK_STATES)).as("the refused start wrote nothing").isEmpty();

            release.countDown();
            rename.get(30, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(repo.start(t, SRC, TGT, null, false, false).state())
                .as("with the rename gone the same start goes through").isEqualTo(PLANNED);
        } finally {
            release.countDown();
            pool.shutdownNow();
        }
    }

    @Test
    void takeStartGuard_putsTheLockTimeoutBackOnceItHoldsTheGuard() {
        String t = tenant();
        List<String> seen = scope.withTenant(t, ctx -> {
            PgSession.setLocal(ctx, "lock_timeout", "7000");
            String before = ctx.select(org.jooq.impl.DSL.function("current_setting", String.class,
                org.jooq.impl.DSL.val("lock_timeout"))).fetchOne(0, String.class);
            ReembedJobRepository.takeStartGuard(ctx, t, SRC);
            String after = ctx.select(org.jooq.impl.DSL.function("current_setting", String.class,
                org.jooq.impl.DSL.val("lock_timeout"))).fetchOne(0, String.class);
            return List.of(before, after);
        });
        assertThat(seen.get(0)).as("the setting the transaction started the guard under").isEqualTo("7s");
        assertThat(seen.get(1)).as("the bound did not outlive the guard wait").isEqualTo(seen.get(0));
    }

    // ── setLastError ─────────────────────────────────────────────────────────

    @Test
    void setLastError_recordsTheReasonWithoutChangingTheState_andRepeatsAreNoOps() {
        String t = tenant();
        Job job = apply(t, apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING), CUTOVER);

        assertThat(repo.setLastError(t, job.jobId(), CUTOVER, "cutover is blocked: x")).isTrue();
        Job first = repo.get(t, job.jobId()).orElseThrow();
        assertThat(first.state()).isEqualTo(CUTOVER);
        assertThat(first.lastError()).isEqualTo("cutover is blocked: x");
        assertThat(first.failureReason()).isNull();
        assertThat(first.cutoverStep()).isEqualTo(job.cutoverStep());

        assertThat(repo.setLastError(t, job.jobId(), CUTOVER, "cutover is blocked: x")).as("the same message again").isTrue();
        assertThat(repo.get(t, job.jobId()).orElseThrow().updatedAt()).as("an equal message writes nothing")
            .isEqualTo(first.updatedAt());

        assertThat(repo.setLastError(t, job.jobId(), CUTOVER, "cutover is blocked: y")).isTrue();
        assertThat(repo.get(t, job.jobId()).orElseThrow().lastError()).isEqualTo("cutover is blocked: y");

        assertThat(repo.setLastError(t, job.jobId(), CUTOVER, null)).as("null clears it").isTrue();
        assertThat(repo.get(t, job.jobId()).orElseThrow().lastError()).isNull();
        assertThat(repo.setLastError(t, job.jobId(), CUTOVER, "cutover is blocked: y")).isTrue();

        assertThat(repo.setLastError(t, job.jobId(), RUNNING, "never")).as("the job is not running").isFalse();
        assertThat(repo.setLastError(t, UUID.randomUUID(), CUTOVER, "never")).as("an absent job").isFalse();
        assertThat(repo.get(t, job.jobId()).orElseThrow().lastError()).isEqualTo("cutover is blocked: y");
    }

    // ── advanceAfterBatch with a failed document ─────────────────────────────

    @Test
    void advanceAfterBatch_withARetryCursor_lowersTheCursorToIt_butNeverRaisesIt_andKeepsTheCounters() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);
        for (String doc : List.of("1.1.1", "1.1.2", "1.1.3")) {
            scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), doc); return 0; });
        }
        assertThat(repo.get(t, job.jobId()).orElseThrow().docCursor()).isEqualTo("1.1.3");

        Job after = repo.advanceAfterBatch(t, job.jobId(), new BatchResult(List.of(), 7, 2, 9, null), "1.1.1")
            .orElseThrow();

        assertThat(after.docCursor()).as("just before the first failed document").isEqualTo("1.1.1");
        assertThat(after.documentsMoved()).as("the committed moves still count").isEqualTo(3);
        assertThat(after.chunksEmbedded()).isEqualTo(7);
        assertThat(after.chunksReused()).isEqualTo(2);
        assertThat(after.tokensBilled()).isEqualTo(9);

        Job higher = repo.advanceAfterBatch(t, job.jobId(), new BatchResult(List.of(), 0, 0, 0, null), "1.1.9")
            .orElseThrow();
        assertThat(higher.docCursor()).as("a retry cursor above the cursor lowers nothing").isEqualTo("1.1.1");

        assertThatThrownBy(() -> repo.advanceAfterBatch(t, job.jobId(),
            new BatchResult(List.of(), 0, 0, 0, "1.1.5"), "1.1.1")).isInstanceOf(IllegalArgumentException.class);
    }

    // ── startNextPass ────────────────────────────────────────────────────────

    @Test
    void startNextPass_bumpsThePassAndRewindsTheCursor_theOnePlaceTheCursorMovesBackwards() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.9"); return 0; });
        assertThat(repo.get(t, job.jobId()).orElseThrow().docCursor()).isEqualTo("1.1.9");

        Job next = repo.startNextPass(t, job.jobId(), 1).orElseThrow();

        assertThat(next.passNumber()).isEqualTo(2);
        assertThat(next.docCursor()).as("a new pass walks the source from the beginning").isEmpty();
        assertThat(next.documentsMoved()).as("the counters are the job's, not the pass's").isEqualTo(1);
        assertThat(next.state()).isEqualTo(RUNNING);
        // the cursor is monotone again inside pass 2
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.3"); return 0; });
        scope.withTenant(t, ctx -> { ReembedJobRepository.recordMoveInTxn(ctx, t, job.jobId(), "1.1.2"); return 0; });
        assertThat(repo.get(t, job.jobId()).orElseThrow().docCursor()).isEqualTo("1.1.3");
    }

    @Test
    void startNextPass_appliesOncePerPass_andOnlyToARunningJob() {
        String t = tenant();
        Job job = apply(t, repo.start(t, SRC, TGT, null, false, false), RUNNING);

        assertThat(repo.startNextPass(t, job.jobId(), 1)).isPresent();
        assertThat(repo.startNextPass(t, job.jobId(), 1))
            .as("a second runner deciding from the same read of pass 1 applies to nothing").isEmpty();
        assertThat(repo.get(t, job.jobId()).orElseThrow().passNumber()).isEqualTo(2);

        Job paused = apply(t, repo.get(t, job.jobId()).orElseThrow(), PAUSED);
        assertThat(repo.startNextPass(t, paused.jobId(), 2)).as("a paused job").isEmpty();
        assertThat(repo.startNextPass(t, UUID.randomUUID(), 1)).as("an absent job").isEmpty();
        assertThat(repo.get(t, job.jobId()).orElseThrow().passNumber()).isEqualTo(2);
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
