// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.records.ReembedJobsRecord;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.time.OffsetDateTime;
import java.util.Collection;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.function.BiConsumer;
import java.util.function.Supplier;

import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOB_MOVES;

/**
 * RDR-210 Phase 6 Step 2 (bead nexus-f98bg): the durable record of a cross-model re-embed job,
 * {@code nexus.reembed_jobs} and {@code nexus.reembed_job_moves} ({@code vectors-037-reembed-jobs.xml}).
 *
 * <p>This class is storage only. It does not know which transitions the RDR allows (the state machine
 * decides that) and it never embeds or moves a document. Every read and write goes through
 * {@link TenantScope#withTenant}, and every UPDATE of a job is a compare-and-set on the state the caller
 * decided from, so two actors (the runner and a control route) cannot both apply a decision made from the
 * same snapshot.
 *
 * <p><b>The source lock.</b> A partial unique index on {@code (tenant_id, source_collection)} over the five
 * {@linkplain #LOCK_STATES lock states} means at most one job per source is planned, running, paused, in
 * cutover or failed. {@link #start} and a transition that takes a job back into a lock state both meet it
 * and answer {@link JobLockHeld} naming the job that holds the source. A job in a terminal state holds
 * nothing, so the lock is released by the CAS that moves the job there.
 *
 * <p><b>Time.</b> Every stamp is {@code clock_timestamp()}, the wall clock at the statement.
 */
public final class ReembedJobRepository {

    public static final String PLANNED = "planned";
    public static final String RUNNING = "running";
    public static final String PAUSED = "paused";
    public static final String FAILED = "failed";
    public static final String ABORTED = "aborted";
    public static final String CUTOVER = "cutover";
    public static final String DONE = "done";
    public static final String DONE_WITH_SKIPS = "done_with_skips";

    /** The states in which a job holds its source's lock (the partial unique index's predicate). */
    public static final Set<String> LOCK_STATES = Set.of(PLANNED, RUNNING, PAUSED, CUTOVER, FAILED);

    /** The states that release the lock and carry a {@code finished_at}. */
    public static final Set<String> TERMINAL_STATES = Set.of(DONE, DONE_WITH_SKIPS, ABORTED);

    /** The states whose job may still fold a batch: a lock state, or aborted (a batch that was in flight). */
    private static final Set<String> BATCH_FOLD_STATES = Set.of(PLANNED, RUNNING, PAUSED, CUTOVER, FAILED, ABORTED);

    public static final String CUTOVER_SUPERSEDED = "superseded";
    public static final String CUTOVER_DELETE_STARTED = "delete_started";

    /** The cutover markers in the order a cutover reaches them; the marker only ever moves rightwards. */
    private static final List<String> CUTOVER_STEPS = List.of(CUTOVER_SUPERSEDED, CUTOVER_DELETE_STARTED);

    public static final String OUTCOME_MOVED = "moved";
    public static final String OUTCOME_SKIPPED = "skipped";

    /** The source-lock index; the constraint name PostgreSQL reports for a second lock-state job. */
    static final String SOURCE_LOCK_INDEX = "reembed_jobs_source_lock";

    /** One job row. {@code tokenCap} null means no cap; {@code disposition} and {@code cutoverStep} may be null. */
    public record Job(UUID jobId, String sourceCollection, String targetCollection, String state,
                      String disposition, String docCursor, int passNumber, String cutoverStep,
                      long documentsMoved, long documentsSkipped, long chunksEmbedded, long chunksReused,
                      long tokensBilled, Long tokenCap, boolean keepSource, boolean forceDelete,
                      OffsetDateTime startedAt, OffsetDateTime updatedAt, OffsetDateTime finishedAt,
                      String failureReason, String lastError) {

        static Job from(ReembedJobsRecord r) {
            return new Job(r.getJobId(), r.getSourceCollection(), r.getTargetCollection(), r.getState(),
                r.getDisposition(), r.getDocCursor(), r.getPassNumber(), r.getCutoverStep(),
                r.getDocumentsMoved(), r.getDocumentsSkipped(), r.getChunksEmbedded(), r.getChunksReused(),
                r.getTokensBilled(), r.getTokenCap(), r.getKeepSource(), r.getForceDelete(),
                r.getStartedAt(), r.getUpdatedAt(), r.getFinishedAt(), r.getFailureReason(), r.getLastError());
        }
    }

    /**
     * Another job holds the source's lock. {@code existingJobId} and {@code existingState} name it, so a
     * handler can answer 409 naming the job rather than a bare conflict.
     */
    public static final class JobLockHeld extends RuntimeException {
        private final UUID existingJobId;
        private final String existingState;

        public JobLockHeld(String sourceCollection, UUID existingJobId, String existingState, Throwable cause) {
            super("re-embed job " + existingJobId + " (" + existingState + ") holds source " + sourceCollection,
                cause);
            this.existingJobId = existingJobId;
            this.existingState = existingState;
        }

        private JobLockHeld(String message, UUID existingJobId, String existingState) {
            super(message);
            this.existingJobId = existingJobId;
            this.existingState = existingState;
        }

        /** A job in a lock state is moving documents INTO {@code collection}, which a new job would move out of. */
        static JobLockHeld targetOf(String collection, UUID existingJobId, String existingState) {
            return new JobLockHeld("re-embed job " + existingJobId + " (" + existingState
                + ") holds " + collection + " as its target; it cannot also be a new job's source",
                existingJobId, existingState);
        }

        /** A job in a lock state is moving documents OUT of {@code collection}, which a new job would fill. */
        static JobLockHeld sourceOf(String collection, UUID existingJobId, String existingState) {
            return new JobLockHeld("re-embed job " + existingJobId + " (" + existingState
                + ") holds " + collection + " as its source; it cannot also be a new job's target",
                existingJobId, existingState);
        }

        public UUID existingJobId() {
            return existingJobId;
        }

        public String existingState() {
            return existingState;
        }
    }

    /**
     * What a transition changes besides the state. {@code failureReason}, {@code lastError},
     * {@code disposition} and {@code tokenCap} are written only when non-null. A null {@code failureReason}
     * keeps the stored one when the job moves to a terminal state or to {@code failed} (a failed job that
     * is aborted still says why it failed) and clears it when the job moves to any other state (a resume
     * has left the failure behind). {@code finished_at} is stamped when the new state is terminal and
     * cleared otherwise.
     */
    public record Transition(String failureReason, String lastError, String disposition, Long tokenCap) {
        public static final Transition NONE = new Transition(null, null, null, null);

        public static Transition failed(String failureReason, String lastError) {
            return new Transition(failureReason, lastError, null, null);
        }

        public static Transition disposition(String disposition) {
            return new Transition(null, null, disposition, null);
        }
    }

    /** What one batch changed besides the documents it moved (those are {@link #recordMoveInTxn}'s). */
    public record BatchResult(List<Skip> skips, long chunksEmbedded, long chunksReused, long tokensBilled,
                              String cursor) {
    }

    /** A document the runner left in the source this pass, with the reason. */
    public record Skip(String docId, String reason) {
    }

    /**
     * The lock was contended twice in a row by jobs that were gone by the time they were looked up. A caller
     * answers a retryable conflict; there is no holder to name.
     */
    public static final class LockContention extends RuntimeException {
        public LockContention(String sourceCollection, Throwable cause) {
            super("the lock on source " + sourceCollection + " changed hands twice while it was being read",
                cause);
        }

        private LockContention(Throwable cause, String message) {
            super(message, cause);
        }

        /**
         * {@link #start} waited {@link #GUARD_LOCK_TIMEOUT_MS} for the per-tenant guard and did not get it: a
         * name-rewriting verb holds it shared for its whole transaction. Nothing was written; retry.
         */
        static LockContention guardBusy(String sourceCollection, Throwable cause) {
            return new LockContention(cause, "a collection rename or rehome is in progress for this tenant; the "
                + "re-embed job on " + sourceCollection + " was not started. Retry shortly");
        }
    }

    /**
     * How long {@link #start} waits for the per-tenant guard before answering {@link LockContention}. A rename
     * holds the guard shared until it commits, so an unbounded wait would pin the caller's pool connection for
     * as long as a large rename runs. Same bound as the other short lock waits ({@code
     * PgSession.DEFAULT_GC_EXPIRE_BOUNDED_LOCK_TIMEOUT_MS}).
     */
    static final int GUARD_LOCK_TIMEOUT_MS = 2_000;

    private final TenantScope scope;

    /**
     * Test-only seam: called with a phase ({@code "before_lookup"}: the source-lock index refused and the
     * holder is about to be looked up; {@code "holder_gone"}: no holder was found and the operation is about
     * to be retried; both outside any transaction) and the zero-based attempt. {@code "after_start_reads"} is
     * called INSIDE {@link #start}'s transaction, with the per-tenant start lock held, between its cross-job reads
     * and its insert. Null (a no-op) in production.
     */
    private volatile BiConsumer<String, Integer> lockRetryHookForTests;

    /** Test-only: install (or clear with {@code null}) the lock-retry hook. */
    public void setLockRetryHookForTests(BiConsumer<String, Integer> hook) {
        this.lockRetryHookForTests = hook;
    }

    public ReembedJobRepository(TenantScope scope) {
        this.scope = scope;
    }

    /**
     * Insert a {@code planned} job. {@code tokenCap} null means no cap.
     *
     * <p><b>The per-tenant start lock.</b> The two cross-job reads below (this job's source against the lock-state
     * jobs' targets, this job's target against their sources) and the insert are one decision. The source-lock
     * index covers only two jobs on the SAME source, so two starts that meet on different names (A to B and B to C)
     * would each read the other's absence and both insert. The transaction therefore takes
     * {@code pg_advisory_xact_lock(hashtext('reembedguard:' || tenant))} first, serialising the tenant's starts, and
     * the name-rewriting verbs ({@link CatalogRepository#refuseUnderReembedJob}) take the same key shared, so a
     * start cannot slip between a rename's read of the jobs and its commit. Lock order: this lock first, then
     * nothing else (a start takes no job-row lock; it only reads and inserts), so it cannot be a link in a cycle
     * with the runner's sweep-gate, index-run and job-row locks, none of which waits on it.
     *
     * @throws JobLockHeld     when a job in a lock state already holds {@code source}, naming it (a failed job
     *                         included: it keeps the lock until it is resumed or aborted), or holds {@code source}
     *                         as its target, or {@code target} as its source
     * @throws LockContention  when the index refused twice and the holder was gone each time it was looked up, or
     *                         when a rename or rehome held the per-tenant guard for longer than
     *                         {@link #GUARD_LOCK_TIMEOUT_MS}
     */
    public Job start(String tenant, String source, String target, Long tokenCap, boolean keepSource,
                     boolean forceDelete) {
        UUID jobId = UUID.randomUUID();
        return retryingOnLock(tenant, jobId, () -> source, () ->
            scope.withTenant(tenant, ctx -> {
                takeStartGuard(ctx, tenant, source);
                // A source that is the TARGET of a job still in a lock state is being filled by that job;
                // moving documents out of it would race that job. The unique index covers only same-source jobs,
                // so this is the refusal that names the job holding the collection from the other side.
                var intoSource = ctx.select(REEMBED_JOBS.JOB_ID, REEMBED_JOBS.STATE).from(REEMBED_JOBS)
                    .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                    .and(REEMBED_JOBS.TARGET_COLLECTION.eq(source))
                    .and(REEMBED_JOBS.STATE.in(LOCK_STATES))
                    .orderBy(REEMBED_JOBS.STARTED_AT, REEMBED_JOBS.JOB_ID)
                    .limit(1)
                    .fetchOne();
                if (intoSource != null) {
                    throw JobLockHeld.targetOf(source, intoSource.value1(), intoSource.value2());
                }
                // The reverse: a target that is the SOURCE of a job still in a lock state is being drained by
                // that job; filling it from a new job would race the drain.
                var outOfTarget = ctx.select(REEMBED_JOBS.JOB_ID, REEMBED_JOBS.STATE).from(REEMBED_JOBS)
                    .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                    .and(REEMBED_JOBS.SOURCE_COLLECTION.eq(target))
                    .and(REEMBED_JOBS.STATE.in(LOCK_STATES))
                    .orderBy(REEMBED_JOBS.STARTED_AT, REEMBED_JOBS.JOB_ID)
                    .limit(1)
                    .fetchOne();
                if (outOfTarget != null) {
                    throw JobLockHeld.sourceOf(target, outOfTarget.value1(), outOfTarget.value2());
                }
                hook("after_start_reads", 0);
                return Job.from(ctx.insertInto(REEMBED_JOBS)
                .set(REEMBED_JOBS.TENANT_ID, tenant)
                .set(REEMBED_JOBS.JOB_ID, jobId)
                .set(REEMBED_JOBS.SOURCE_COLLECTION, source)
                .set(REEMBED_JOBS.TARGET_COLLECTION, target)
                .set(REEMBED_JOBS.STATE, PLANNED)
                .set(REEMBED_JOBS.TOKEN_CAP, tokenCap)
                .set(REEMBED_JOBS.KEEP_SOURCE, keepSource)
                .set(REEMBED_JOBS.FORCE_DELETE, forceDelete)
                .set(REEMBED_JOBS.STARTED_AT, clock())
                .set(REEMBED_JOBS.UPDATED_AT, clock())
                .returning()
                .fetchOne());
            }));
    }

    /**
     * Take the per-tenant guard exclusive, waiting at most {@link #GUARD_LOCK_TIMEOUT_MS}. The bound is
     * transaction-local and is put back as it was once the guard is held, so the reads and the insert that follow
     * run under the setting they had before. A timeout (SQLSTATE 55P03) is {@link LockContention}, which the route
     * answers as a retryable 409; the transaction ends with the throw, so nothing was written.
     */
    static void takeStartGuard(DSLContext ctx, String tenant, String source) {
        String before = ctx.select(DSL.function("current_setting", String.class, DSL.val("lock_timeout")))
            .fetchOne(0, String.class);
        PgSession.setLocal(ctx, "lock_timeout", Integer.toString(GUARD_LOCK_TIMEOUT_MS));
        try {
            ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                    DSL.function("hashtext", Integer.class, DSL.val(CatalogRepository.reembedGuardKey(tenant)))))
                .fetch();
        } catch (RuntimeException e) {
            if (SqlConstraints.isLockNotAvailable(e)) {
                throw LockContention.guardBusy(source, e);
            }
            throw e;
        }
        PgSession.setLocal(ctx, "lock_timeout", before);
    }

    public Optional<Job> get(String tenant, UUID jobId) {
        return scope.withTenant(tenant, ctx -> ctx.selectFrom(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .and(REEMBED_JOBS.JOB_ID.eq(jobId))
            .fetchOptional()
            .map(Job::from));
    }

    /** Every job row of the tenant, newest first (the status route; the table is a job log and stays small). */
    public List<Job> listAll(String tenant) {
        return scope.withTenant(tenant, ctx -> ctx.selectFrom(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .orderBy(REEMBED_JOBS.STARTED_AT.desc(), REEMBED_JOBS.JOB_ID.desc())
            .fetch(Job::from));
    }

    /** The tenant's jobs in any of {@code states}, oldest first. */
    public List<Job> listByStates(String tenant, Collection<String> states) {
        if (states.isEmpty()) {
            return List.of();
        }
        return scope.withTenant(tenant, ctx -> ctx.selectFrom(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .and(REEMBED_JOBS.STATE.in(states))
            .orderBy(REEMBED_JOBS.STARTED_AT, REEMBED_JOBS.JOB_ID)
            .fetch(Job::from));
    }

    /**
     * Compare-and-set a job from {@code from} to {@code to}. The UPDATE matches only while the row is still
     * in {@code from} and its cutover marker is still {@code expectedCutoverStep} (null: no marker yet), the
     * two things the caller's decision was made from, so a decision taken from a stale snapshot applies to
     * nothing. Abort relies on the marker: a job whose delete has started no longer matches an abort
     * decided before it.
     *
     * @return the updated row, or empty when the job is absent or no longer in {@code from} with that marker
     * @throws JobLockHeld    when {@code to} is a lock state and another job holds the source (a re-entry
     *                        from a terminal state)
     * @throws LockContention when the index refused twice and the holder was gone each time it was looked up
     */
    public Optional<Job> applyTransition(String tenant, UUID jobId, String from, String expectedCutoverStep,
                                         String to, Transition change) {
        boolean terminal = TERMINAL_STATES.contains(to);
        boolean keepsReason = terminal || FAILED.equals(to);
        return retryingOnLock(tenant, jobId, () -> sourceOf(tenant, jobId), () ->
            scope.withTenant(tenant, ctx -> {
                var update = ctx.update(REEMBED_JOBS)
                    .set(REEMBED_JOBS.STATE, to)
                    .set(REEMBED_JOBS.UPDATED_AT, clock());
                if (change.failureReason() != null) {
                    update = update.set(REEMBED_JOBS.FAILURE_REASON, change.failureReason());
                } else if (!keepsReason) {
                    update = update.setNull(REEMBED_JOBS.FAILURE_REASON);
                }
                update = terminal
                    ? update.set(REEMBED_JOBS.FINISHED_AT, clock())
                    : update.setNull(REEMBED_JOBS.FINISHED_AT);
                if (change.lastError() != null) {
                    update = update.set(REEMBED_JOBS.LAST_ERROR, change.lastError());
                }
                if (change.disposition() != null) {
                    update = update.set(REEMBED_JOBS.DISPOSITION, change.disposition());
                }
                if (change.tokenCap() != null) {
                    update = update.set(REEMBED_JOBS.TOKEN_CAP, change.tokenCap());
                }
                return update
                    .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                    .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                    .and(REEMBED_JOBS.STATE.eq(from))
                    .and(REEMBED_JOBS.CUTOVER_STEP.isNotDistinctFrom(expectedCutoverStep))
                    .returning()
                    .fetchOptional()
                    .map(Job::from);
            }));
    }

    /**
     * Take a {@code done} job whose disposition is {@code kept_tombstoned} back into a lock state (RDR-210 Cutover,
     * "Tombstoned documents"): {@code running} when a document is live in the source again, {@code cutover} when
     * the tombstones are gone. A compare-and-set on {@code done} and the disposition, so two re-entries, or a
     * re-entry racing a start, cannot both win; the entry meets the source-lock index like any other.
     *
     * <p>The job starts a fresh walk: pass 1, cursor at the start, no failure, no old message, no disposition (the
     * new end sets one), {@code finished_at} cleared. The cutover marker stays: the source is still superseded,
     * and the marker only moves forward. The counters keep what the job has done, and so does its move record, which
     * the parity check reads.
     *
     * @return the updated row, or empty when the job is absent, no longer {@code done}, or no longer
     *         {@code kept_tombstoned}
     * @throws JobLockHeld    when another job holds the source
     * @throws LockContention when the index refused twice and the holder was gone each time it was looked up
     * @throws IllegalArgumentException when {@code to} is neither {@code running} nor {@code cutover}
     */
    public Optional<Job> reenter(String tenant, UUID jobId, String to) {
        if (!RUNNING.equals(to) && !CUTOVER.equals(to)) {
            throw new IllegalArgumentException("a kept_tombstoned job re-enters running or cutover, not " + to);
        }
        return retryingOnLock(tenant, jobId, () -> sourceOf(tenant, jobId), () ->
            scope.withTenant(tenant, ctx -> ctx.update(REEMBED_JOBS)
                .set(REEMBED_JOBS.STATE, to)
                .set(REEMBED_JOBS.PASS_NUMBER, (short) 1)
                .set(REEMBED_JOBS.DOC_CURSOR, "")
                .setNull(REEMBED_JOBS.DISPOSITION)
                .setNull(REEMBED_JOBS.FAILURE_REASON)
                .setNull(REEMBED_JOBS.LAST_ERROR)
                .setNull(REEMBED_JOBS.FINISHED_AT)
                .set(REEMBED_JOBS.UPDATED_AT, clock())
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                .and(REEMBED_JOBS.STATE.eq(DONE))
                .and(REEMBED_JOBS.DISPOSITION.eq("kept_tombstoned"))
                .returning()
                .fetchOptional()
                .map(Job::from)));
    }

    /**
     * Fold one batch into a job, in one transaction: the batch's skips (a document skipped once in this job is
     * counted once; one that had moved and is in the source again becomes a skip, and stops counting as moved),
     * its chunk and token counters, and the cursor, kept
     * monotone ({@code greatest}), so the cursor passes skipped documents the moves never reached.
     *
     * <p>The batch has already happened (its moves are committed and counted by {@link #recordMoveInTxn},
     * its tokens are billed), so it folds into a job in any lock state or in {@code aborted}: a pause or an
     * abort landing mid-batch must not drop the batch's chunks, tokens and skips, or {@code tokens_billed}
     * undercounts against the token cap. The state is never changed here, and the cursor still advances,
     * since the moves it passes are committed and a resume restarts from the cursor. Only a job that has
     * finished ({@code done}, {@code done_with_skips}) or is absent takes nothing.
     *
     * @return the updated row, or empty when the job is absent or finished
     */
    public Optional<Job> advanceAfterBatch(String tenant, UUID jobId, BatchResult batch) {
        return advanceAfterBatch(tenant, jobId, batch, null);
    }

    /**
     * {@link #advanceAfterBatch(String, UUID, BatchResult)} for a batch with a failed document: instead of moving
     * the cursor forward, set it to {@code least(cursor, retryAfter)}. The moves that committed in the batch (the
     * documents after the failed one) have already advanced the cursor past the failed document, and a cursor
     * left there would step the resume past it, to be found only by the next pass and, on the last pass, recorded
     * as a {@code pass_limit} skip with the real failure lost. {@code retryAfter} is the document just before the
     * first failed one (or the cursor the batch started from), so the resume reads the failed document first.
     * The documents after it that committed are no longer candidates; ones that were skipped are skipped again
     * without being counted twice.
     *
     * <p>The one lowering of the cursor besides {@link #startNextPass}, and only ever to a point at or after the
     * start of the batch, so it never re-walks documents an earlier batch finished.
     *
     * @param retryAfter the cursor to resume after, or null for the ordinary monotone advance;
     *                   {@code batch.cursor()} must then be null
     * @throws IllegalArgumentException when both {@code retryAfter} and {@code batch.cursor()} are set
     */
    public Optional<Job> advanceAfterBatch(String tenant, UUID jobId, BatchResult batch, String retryAfter) {
        if (retryAfter != null && batch.cursor() != null) {
            throw new IllegalArgumentException("a batch with a failed document does not also advance the cursor");
        }
        return scope.withTenant(tenant, ctx -> {
            Optional<Short> pass = ctx.select(REEMBED_JOBS.PASS_NUMBER).from(REEMBED_JOBS)
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                .and(REEMBED_JOBS.STATE.in(BATCH_FOLD_STATES))
                .forUpdate()
                .fetchOptional(REEMBED_JOBS.PASS_NUMBER);
            if (pass.isEmpty()) {
                return Optional.<Job>empty();
            }
            long newlySkipped = 0;
            long noLongerMoved = 0;
            for (Skip skip : batch.skips()) {
                String previous = ctx.select(REEMBED_JOB_MOVES.OUTCOME).from(REEMBED_JOB_MOVES)
                    .where(REEMBED_JOB_MOVES.TENANT_ID.eq(tenant))
                    .and(REEMBED_JOB_MOVES.JOB_ID.eq(jobId))
                    .and(REEMBED_JOB_MOVES.DOC_ID.eq(skip.docId()))
                    .forUpdate()
                    .fetchOne(REEMBED_JOB_MOVES.OUTCOME);
                if (OUTCOME_SKIPPED.equals(previous)) {
                    continue;
                }
                if (previous == null) {
                    ctx.insertInto(REEMBED_JOB_MOVES)
                        .set(REEMBED_JOB_MOVES.TENANT_ID, tenant)
                        .set(REEMBED_JOB_MOVES.JOB_ID, jobId)
                        .set(REEMBED_JOB_MOVES.DOC_ID, skip.docId())
                        .set(REEMBED_JOB_MOVES.OUTCOME, OUTCOME_SKIPPED)
                        .set(REEMBED_JOB_MOVES.REASON, skip.reason())
                        .set(REEMBED_JOB_MOVES.PASS_NUMBER, pass.get())
                        .set(REEMBED_JOB_MOVES.AT, clock())
                        .execute();
                } else {
                    // A document that moved and is in the source again (a client wrote back after its move)
                    // and could not be moved again: it is a skip now, the mirror of recordMoveInTxn's
                    // skipped-to-moved.
                    ctx.update(REEMBED_JOB_MOVES)
                        .set(REEMBED_JOB_MOVES.OUTCOME, OUTCOME_SKIPPED)
                        .set(REEMBED_JOB_MOVES.REASON, skip.reason())
                        .set(REEMBED_JOB_MOVES.PASS_NUMBER, pass.get())
                        .set(REEMBED_JOB_MOVES.AT, clock())
                        .where(REEMBED_JOB_MOVES.TENANT_ID.eq(tenant))
                        .and(REEMBED_JOB_MOVES.JOB_ID.eq(jobId))
                        .and(REEMBED_JOB_MOVES.DOC_ID.eq(skip.docId()))
                        .execute();
                    noLongerMoved++;
                }
                newlySkipped++;
            }
            var update = ctx.update(REEMBED_JOBS)
                .set(REEMBED_JOBS.DOCUMENTS_SKIPPED, REEMBED_JOBS.DOCUMENTS_SKIPPED.plus(newlySkipped))
                .set(REEMBED_JOBS.DOCUMENTS_MOVED, REEMBED_JOBS.DOCUMENTS_MOVED.minus(noLongerMoved))
                .set(REEMBED_JOBS.CHUNKS_EMBEDDED, REEMBED_JOBS.CHUNKS_EMBEDDED.plus(batch.chunksEmbedded()))
                .set(REEMBED_JOBS.CHUNKS_REUSED, REEMBED_JOBS.CHUNKS_REUSED.plus(batch.chunksReused()))
                .set(REEMBED_JOBS.TOKENS_BILLED, REEMBED_JOBS.TOKENS_BILLED.plus(batch.tokensBilled()))
                .set(REEMBED_JOBS.UPDATED_AT, clock());
            if (batch.cursor() != null) {
                update = update.set(REEMBED_JOBS.DOC_CURSOR,
                    DSL.greatest(REEMBED_JOBS.DOC_CURSOR, DSL.val(batch.cursor())));
            } else if (retryAfter != null) {
                update = update.set(REEMBED_JOBS.DOC_CURSOR,
                    DSL.least(REEMBED_JOBS.DOC_CURSOR, DSL.val(retryAfter)));
            }
            return update
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                .returning()
                .fetchOptional()
                .map(Job::from);
        });
    }

    /**
     * Begin the next pass over the source: {@code pass_number + 1} and the cursor back to the start. The one
     * place the cursor moves backwards (everywhere else it is {@code greatest}), because a new pass walks the
     * source from the beginning to find what the last pass left or a client wrote back.
     *
     * <p>The UPDATE matches only while the job is {@code running} and still on {@code fromPass}, so a runner
     * that decided from a stale read, a pause or an abort that landed first, or a second runner, applies
     * nothing and the pass number is never counted twice.
     *
     * @return the updated row, or empty when the job is absent, not {@code running}, or no longer on
     *         {@code fromPass}
     */
    public Optional<Job> startNextPass(String tenant, UUID jobId, int fromPass) {
        return scope.withTenant(tenant, ctx -> ctx.update(REEMBED_JOBS)
            .set(REEMBED_JOBS.PASS_NUMBER, (short) (fromPass + 1))
            .set(REEMBED_JOBS.DOC_CURSOR, "")
            .set(REEMBED_JOBS.UPDATED_AT, clock())
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .and(REEMBED_JOBS.JOB_ID.eq(jobId))
            .and(REEMBED_JOBS.STATE.eq(RUNNING))
            .and(REEMBED_JOBS.PASS_NUMBER.eq((short) fromPass))
            .returning()
            .fetchOptional()
            .map(Job::from));
    }

    /**
     * Advance the cutover marker to {@code step}, only while the job is still in {@code cutover}. The
     * commit before an irreversible step: a concurrent abort moved the job out of {@code cutover} first
     * (this matches nothing, and the step must not run) or this wins and the abort is then refused by the
     * marker.
     *
     * <p>The marker is monotone: {@code superseded} then {@code delete_started}, never backwards. A
     * cutover restarted after the delete began re-runs from its first step, and must not write
     * {@code superseded} over {@code delete_started}, which would re-enable an abort after the delete.
     * Setting the value the marker already holds is a no-op.
     *
     * @return true when the marker now holds {@code step} (set here, or already there); false when the job
     *         is not in {@code cutover}, or the marker is already past {@code step}
     * @throws IllegalArgumentException for a step that is not a cutover marker
     */
    public boolean setCutoverStep(String tenant, UUID jobId, String step) {
        int rank = CUTOVER_STEPS.indexOf(step);
        if (rank < 0) {
            throw new IllegalArgumentException("not a cutover step: " + step);
        }
        List<String> behind = CUTOVER_STEPS.subList(0, rank);
        return scope.withTenant(tenant, ctx -> {
            Condition markerBehind = REEMBED_JOBS.CUTOVER_STEP.isNull();
            if (!behind.isEmpty()) {
                markerBehind = markerBehind.or(REEMBED_JOBS.CUTOVER_STEP.in(behind));
            }
            int set = ctx.update(REEMBED_JOBS)
                .set(REEMBED_JOBS.CUTOVER_STEP, step)
                .set(REEMBED_JOBS.UPDATED_AT, clock())
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                .and(REEMBED_JOBS.STATE.eq(CUTOVER))
                .and(markerBehind)
                .execute();
            return set == 1 || ctx.fetchExists(ctx.selectFrom(REEMBED_JOBS)
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                .and(REEMBED_JOBS.STATE.eq(CUTOVER))
                .and(REEMBED_JOBS.CUTOVER_STEP.eq(step)));
        });
    }

    /**
     * Record why a job in {@code state} is not making progress, without changing its state: set
     * {@code last_error} to {@code message}. The write is a no-op (and leaves {@code updated_at} alone) when the
     * column already holds the message, so a blocked cutover that repeats on every tick records itself once.
     * {@code last_error} is the last error seen; a null {@code message} clears it (the runner clears a blocked
     * cutover's message when the cutover finishes).
     *
     * @return true when the job is in {@code state} afterwards (the message is stored), false when the job is
     *         absent or has left {@code state}
     */
    public boolean setLastError(String tenant, UUID jobId, String state, String message) {
        return scope.withTenant(tenant, ctx -> {
            int set = ctx.update(REEMBED_JOBS)
                .set(REEMBED_JOBS.LAST_ERROR, message)
                .set(REEMBED_JOBS.UPDATED_AT, clock())
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                .and(REEMBED_JOBS.STATE.eq(state))
                .and(message == null ? REEMBED_JOBS.LAST_ERROR.isNotNull()
                    : REEMBED_JOBS.LAST_ERROR.isNull().or(REEMBED_JOBS.LAST_ERROR.ne(message)))
                .execute();
            return set == 1 || ctx.fetchExists(ctx.selectFrom(REEMBED_JOBS)
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(jobId))
                .and(REEMBED_JOBS.STATE.eq(state)));
        });
    }

    /**
     * A later job took {@code earlierTarget} back as its target: mark the most recent finished job that
     * went {@code earlierSource -> earlierTarget} (state {@code done} or {@code aborted}) with disposition
     * {@code revived}, so a later {@code --all} never deletes it. State is left as it stands.
     *
     * <p>The match is {@code done} and {@code aborted} on purpose. A {@code failed} earlier job is in a
     * lock state, so its source is the revive's target and the start's lock check refuses the revive
     * before this runs (TargetSelection encodes it); and a {@code done_with_skips} job never superseded
     * its source, so it cannot be the job the revive is taking back.
     *
     * <p>Idempotent: when that most recent finished job is already {@code revived} nothing is written and the
     * result is empty, so a retry of a start whose first attempt revived the target but lost the start or the mark
     * heals the mark and a repeat changes nothing. An older finished job is never reached past a revived one.
     *
     * @return the updated row, or empty when there is no such job or it is already marked
     */
    public Optional<Job> markRevived(String tenant, String earlierSource, String earlierTarget) {
        return scope.withTenant(tenant, ctx -> {
            var latest = ctx.select(REEMBED_JOBS.JOB_ID, REEMBED_JOBS.DISPOSITION).from(REEMBED_JOBS)
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.SOURCE_COLLECTION.eq(earlierSource))
                .and(REEMBED_JOBS.TARGET_COLLECTION.eq(earlierTarget))
                .and(REEMBED_JOBS.STATE.in(DONE, ABORTED))
                .orderBy(REEMBED_JOBS.FINISHED_AT.desc().nullsLast(), REEMBED_JOBS.STARTED_AT.desc())
                .limit(1)
                .forUpdate()
                .fetchOptional();
            if (latest.isEmpty() || "revived".equals(latest.get().get(REEMBED_JOBS.DISPOSITION))) {
                return Optional.<Job>empty();
            }
            return ctx.update(REEMBED_JOBS)
                .set(REEMBED_JOBS.DISPOSITION, "revived")
                .set(REEMBED_JOBS.UPDATED_AT, clock())
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.JOB_ID.eq(latest.get().get(REEMBED_JOBS.JOB_ID)))
                .returning()
                .fetchOptional()
                .map(Job::from);
        });
    }

    /**
     * Record that a document moved, inside the document's own move transaction, so the record and the move
     * commit or roll back together. Locks the job row (the last lock the move takes), upserts the document's
     * moves row as {@code moved}, folds the counters (a document moved twice, or moved after a skip, is
     * counted once), and keeps the cursor monotone.
     *
     * @throws IllegalStateException when the job row does not exist, which rolls the move back
     */
    public static void recordMoveInTxn(DSLContext ctx, String tenant, UUID jobId, String docId) {
        Short pass = ctx.select(REEMBED_JOBS.PASS_NUMBER).from(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .and(REEMBED_JOBS.JOB_ID.eq(jobId))
            .forUpdate()
            .fetchOne(REEMBED_JOBS.PASS_NUMBER);
        if (pass == null) {
            throw new IllegalStateException("re-embed job " + jobId + " does not exist for tenant " + tenant);
        }
        String previous = ctx.select(REEMBED_JOB_MOVES.OUTCOME).from(REEMBED_JOB_MOVES)
            .where(REEMBED_JOB_MOVES.TENANT_ID.eq(tenant))
            .and(REEMBED_JOB_MOVES.JOB_ID.eq(jobId))
            .and(REEMBED_JOB_MOVES.DOC_ID.eq(docId))
            .forUpdate()
            .fetchOne(REEMBED_JOB_MOVES.OUTCOME);
        long movedDelta = 0;
        long skippedDelta = 0;
        if (previous == null) {
            ctx.insertInto(REEMBED_JOB_MOVES)
                .set(REEMBED_JOB_MOVES.TENANT_ID, tenant)
                .set(REEMBED_JOB_MOVES.JOB_ID, jobId)
                .set(REEMBED_JOB_MOVES.DOC_ID, docId)
                .set(REEMBED_JOB_MOVES.OUTCOME, OUTCOME_MOVED)
                .set(REEMBED_JOB_MOVES.PASS_NUMBER, pass)
                .set(REEMBED_JOB_MOVES.AT, clock())
                .execute();
            movedDelta = 1;
        } else {
            ctx.update(REEMBED_JOB_MOVES)
                .set(REEMBED_JOB_MOVES.OUTCOME, OUTCOME_MOVED)
                .setNull(REEMBED_JOB_MOVES.REASON)
                .set(REEMBED_JOB_MOVES.PASS_NUMBER, pass)
                .set(REEMBED_JOB_MOVES.AT, clock())
                .where(REEMBED_JOB_MOVES.TENANT_ID.eq(tenant))
                .and(REEMBED_JOB_MOVES.JOB_ID.eq(jobId))
                .and(REEMBED_JOB_MOVES.DOC_ID.eq(docId))
                .execute();
            if (OUTCOME_SKIPPED.equals(previous)) {
                movedDelta = 1;
                skippedDelta = -1;
            }
        }
        ctx.update(REEMBED_JOBS)
            .set(REEMBED_JOBS.DOCUMENTS_MOVED, REEMBED_JOBS.DOCUMENTS_MOVED.plus(movedDelta))
            .set(REEMBED_JOBS.DOCUMENTS_SKIPPED, REEMBED_JOBS.DOCUMENTS_SKIPPED.plus(skippedDelta))
            .set(REEMBED_JOBS.DOC_CURSOR, DSL.greatest(REEMBED_JOBS.DOC_CURSOR, DSL.val(docId)))
            .set(REEMBED_JOBS.UPDATED_AT, clock())
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .and(REEMBED_JOBS.JOB_ID.eq(jobId))
            .execute();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static Field<OffsetDateTime> clock() {
        return DSL.function("clock_timestamp", SQLDataType.TIMESTAMPWITHTIMEZONE);
    }

    /** The job's source, or null when the job row is absent. */
    private String sourceOf(String tenant, UUID jobId) {
        return scope.withTenant(tenant, ctx -> ctx.select(REEMBED_JOBS.SOURCE_COLLECTION).from(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .and(REEMBED_JOBS.JOB_ID.eq(jobId))
            .fetchOne(REEMBED_JOBS.SOURCE_COLLECTION));
    }

    /**
     * Run {@code op}; when the source-lock index refuses it, name the lock-state job that holds the source
     * ({@link JobLockHeld}). The holder may finish between the refusal and the lookup, in which case there
     * is nothing to name and the operation is retried once; a second such refusal is {@link LockContention},
     * never the raw unique violation. Any other failure, and a refusal with no source to look up (the job is
     * absent), passes through unchanged.
     */
    private <T> T retryingOnLock(String tenant, UUID self, Supplier<String> sourceOf, Supplier<T> op) {
        for (int attempt = 0; ; attempt++) {
            try {
                return op.get();
            } catch (RuntimeException e) {
                if (!SqlConstraints.violatesAny(e, SOURCE_LOCK_INDEX)) {
                    throw e;
                }
                String source = sourceOf.get();
                if (source == null) {
                    throw e;
                }
                hook("before_lookup", attempt);
                Optional<JobLockHeld> held = holderOf(tenant, source, self, e);
                if (held.isPresent()) {
                    throw held.get();
                }
                if (attempt == 1) {
                    throw new LockContention(source, e);
                }
                hook("holder_gone", attempt);
            }
        }
    }

    private void hook(String phase, int attempt) {
        BiConsumer<String, Integer> h = lockRetryHookForTests;
        if (h != null) {
            h.accept(phase, attempt);
        }
    }

    /** The lock-state job other than {@code self} that holds {@code source} (read in a new transaction). */
    private Optional<JobLockHeld> holderOf(String tenant, String source, UUID self, RuntimeException cause) {
        return scope.withTenant(tenant, ctx -> ctx.select(REEMBED_JOBS.JOB_ID, REEMBED_JOBS.STATE)
                .from(REEMBED_JOBS)
                .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
                .and(REEMBED_JOBS.SOURCE_COLLECTION.eq(source))
                .and(REEMBED_JOBS.STATE.in(LOCK_STATES))
                .and(REEMBED_JOBS.JOB_ID.ne(self))
                .fetchOptional())
            .map(holder -> new JobLockHeld(source, holder.value1(), holder.value2(), cause));
    }
}
