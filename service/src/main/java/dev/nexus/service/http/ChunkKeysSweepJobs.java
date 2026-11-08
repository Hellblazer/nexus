// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Consumer;
import java.util.function.LongSupplier;

/**
 * nexus-v75nw: the dry runs and the applies of the retire-keys sweep that run in the background, so a tenant whose
 * preconditions take longer than the edge's origin timeout (about 30 s) can still be counted, and an apply that takes
 * hours is not a client loop that sends a root request every few seconds. {@code POST /v1/admin/chunk-keys/sweep}
 * with {@code "async": true} starts one here and answers at once with its id; {@code GET
 * /v1/admin/chunk-keys/sweep/jobs/{id}} reads its stage, its progress and, when it has ended, the status and body the
 * synchronous call would have answered; {@code POST /v1/admin/chunk-keys/sweep/jobs/{id}/cancel} asks an apply job to
 * stop at its next batch boundary.
 *
 * <p><b>Memory only.</b> The table is not persisted: a job is lost with the engine process, and a poll for an id the
 * engine does not hold is a 404 the client answers by starting again (a dry run writes nothing; an apply that was
 * running keeps what its committed batches swept, and the same command counts again and takes what is left). A
 * finished job is kept for {@link #RETENTION} so its client can read the outcome, at most {@link #MAX_RETAINED} are
 * held, and a finished job is only ever READ: a start never reuses one, it always runs (and so recounts).
 *
 * <p><b>Bounds.</b> At most {@link #MAX_RUNNING} jobs run at once, each holding one pooled connection at a time, and
 * {@link #QUEUE} more wait; a start past that is refused ({@link Busy}, 503) rather than queued without end. One
 * tenant has at most one running job, dry run or apply: a second start with the same request is handed the running
 * job, and one with a different request is refused ({@link Conflict}) until it ends. A job stops by itself at
 * {@link #MAX_RUNTIME} of wall clock from the moment a worker takes it up (time in the queue does not count; with
 * {@link #MAX_RUNNING} workers, a long apply can hold one for hours while queued jobs wait): the work asks {@link Job#stopReason()} at its own boundaries (a stage of a dry
 * run, a batch of an apply), so a job is never cut mid-batch.
 *
 * <p><b>Tenant state.</b> A job holds its tenant's counts, so the registry is tenant-keyed state: {@link
 * #evictTenant} is the evictor the tenant reset route runs. The threads are daemon threads and {@link #shutdown}
 * ends them with the engine.
 *
 * <p><b>Shared registry.</b> The pool, the table, the scope rule, retention and eviction live in {@link EngineJobs}
 * (RDR-193 Step 1); this class keeps what is the sweep's own: the dry-run or apply kind, cancel for an apply, an
 * apply's progress, and the starter check that decides who may join or poll a job.
 */
public final class ChunkKeysSweepJobs {

    /** Jobs running at once. A job holds one pooled connection at a time, so this is also the connections it can take. */
    public static final int MAX_RUNNING = 2;
    /** Jobs waiting for a thread; a start beyond running + this is {@link Busy}. */
    public static final int QUEUE = 8;
    /** How long a finished job is kept for its client to read. */
    public static final Duration RETENTION = Duration.ofMinutes(30);
    /** The longest a job runs (wall clock from its start); the work stops at its next boundary past this. */
    public static final Duration MAX_RUNTIME = Duration.ofHours(4);
    /** Jobs held at all (running, queued and finished); a start evicts the oldest finished ones past this. */
    public static final int MAX_RETAINED = 64;

    /** A job's state. */
    public enum State {
        RUNNING("running"), DONE("done"), FAILED("failed");

        private final String wire;

        State(String wire) {
            this.wire = wire;
        }

        public String wire() {
            return wire;
        }
    }

    /** What a job runs: the dry run (read only) or the apply (it sweeps). Only an apply can be cancelled. */
    public enum Kind {
        DRY_RUN("dry_run"), APPLY("apply");

        private final String wire;

        Kind(String wire) {
            this.wire = wire;
        }

        public String wire() {
            return wire;
        }
    }

    /** A start's answer: the job, and whether this call created it (false: it joined the one already running). */
    public record Started(Job job, boolean created) { }

    /** What a job ended with: the HTTP status and body the synchronous call would have answered. */
    public record Outcome(int httpStatus, Map<String, Object> body) { }

    /** The work of a job; it names each step to the sink it is given. */
    @FunctionalInterface
    public interface Work {
        Outcome run(Consumer<String> stage) throws Exception;
    }

    /** As {@link Work}, told the job it runs as: its id, whether it was told to stop, and where to publish its progress. */
    @FunctionalInterface
    public interface JobWork {
        Outcome run(Job job, Consumer<String> stage) throws Exception;
    }

    /**
     * Thrown by the work of a job when it was told to stop (the cap, a cancel, a revoked credential or the tenant reset)
     * at a stage boundary of a count: a dry run's, or the count an apply's first call makes. Nothing is swept by then.
     */
    public static final class Stopped extends RuntimeException {
        private final String reason;

        public Stopped(String reason) {
            super("the job was stopped: " + reason);
            this.reason = reason;
        }

        public String reason() {
            return reason;
        }
    }

    /** No thread and no queue place is free. */
    public static final class Busy extends RuntimeException {
        Busy() {
            super("the engine is already running or holding the most sweep jobs it takes; retry shortly");
        }
    }

    /** The tenant has a running job for a different request (a dry run and an apply are different requests). */
    public static final class Conflict extends RuntimeException {
        private final String jobId;
        private final Kind kind;

        Conflict(String jobId, Kind kind) {
            super("a " + (kind == Kind.APPLY ? "sweep" : "dry run") + " of this tenant is already running with a "
                + "different request (job " + jobId + "); wait for it to end, or poll it");
            this.jobId = jobId;
            this.kind = kind;
        }

        public String jobId() {
            return jobId;
        }

        /** What the running job is: a client that wanted to apply the same request may rejoin an apply. */
        public Kind kind() {
            return kind;
        }
    }


    /**
     * One job: the shared registry's job plus what is the sweep's own (its kind, its starter, an apply's cancel and
     * progress). The mutable fields are written by the worker thread and read by the polling request.
     */
    public static final class Job {
        private final EngineJobs.Job<Job> core;
        private final String callerTenant;
        private final boolean root;
        private final Kind kind;
        /** Set by {@link #requestCancel}: the apply stops at its next batch boundary. */
        private volatile boolean cancelRequested;
        /** The apply's progress so far, written by its worker and read by a poll; -1 remaining means not known yet. */
        private volatile int batches;
        private volatile long rowsSwept;
        private volatile long remaining = -1;

        private Job(EngineJobs.Job<Job> core, String callerTenant, boolean root, Kind kind) {
            this.core = core;
            this.callerTenant = callerTenant;
            this.root = root;
            this.kind = kind;
        }

        public Kind kind() {
            return kind;
        }

        /**
         * Asks a running apply to stop at its next batch boundary. False (and nothing changes) for a dry run, which
         * writes nothing and has no batch to stop at, and for a job that has ended.
         */
        public boolean requestCancel() {
            if (kind != Kind.APPLY || core.state() != EngineJobs.State.RUNNING) {
                return false;
            }
            cancelRequested = true;
            return true;
        }

        public boolean cancelRequested() {
            return cancelRequested;
        }

        /**
         * Asks the job to stop at its next boundary for a reason the work found itself (the starter's credential was
         * revoked or expired: {@code credential_revoked}). The first reason set stays; a cancel or an eviction that is
         * already asked is reported first by {@link #stopReason()}.
         */
        public void stopFor(String reason) {
            core.stopFor(reason);
        }

        /**
         * Why the work should stop at its next boundary, or null: {@code evicted} (the tenant reset dropped the job),
         * {@code cancelled} (its starter asked), the reason the work gave {@link #stopFor} (such as {@code
         * credential_revoked}), or {@code wall_clock_cap} ({@link #MAX_RUNTIME} since a worker took the job up: a job
         * that waits in the queue has not started its cap clock).
         */
        public String stopReason() {
            if (core.evicted()) {
                return "evicted";
            }
            if (cancelRequested) {
                return "cancelled";
            }
            return core.stopReason();
        }

        /** The worker's publication of an apply's progress: batches run, rows swept (both over the whole job) and rows left. */
        void progress(int batchesRun, long rows, long rowsLeft) {
            this.batches = batchesRun;
            this.rowsSwept = rows;
            this.remaining = rowsLeft;
        }

        public int batches() {
            return batches;
        }

        public long rowsSwept() {
            return rowsSwept;
        }

        /** Rows still to sweep, or -1 before the apply's count has finished. */
        public long remaining() {
            return remaining;
        }

        public String id() {
            return core.id();
        }

        /** The tenant the job counts (a dry run) or sweeps (an apply). */
        public String tenant() {
            return core.tenant();
        }

        public State state() {
            return State.valueOf(core.state().name());
        }

        /** True once the tenant reset dropped this job: its outcome belongs to no tenant any more and is not recorded. */
        public boolean evicted() {
            return core.evicted();
        }

        public String stage() {
            return core.stage();
        }

        /** The status and body the synchronous call would have answered; null while the job runs. */
        public Outcome outcome() {
            EngineJobs.Outcome o = core.outcome();
            return o == null ? null : new Outcome(o.httpStatus(), o.body());
        }

        /** Whether {@code callerTenant}, a root credential or not, is the one that started this job. */
        public boolean startedBy(String caller, boolean rootCredential) {
            return callerTenant.equals(caller) && (!root || rootCredential);
        }

        /** Milliseconds from the start to now, or to the end of a finished job. */
        public long elapsedMs(long nowMs) {
            return core.elapsedMs(nowMs);
        }
    }

    private final EngineJobs<Job> jobs;

    public ChunkKeysSweepJobs() {
        this(System::currentTimeMillis, RETENTION, MAX_RETAINED, MAX_RUNNING, QUEUE);
    }

    ChunkKeysSweepJobs(LongSupplier clockMs, Duration retention, int maxRetained, int running, int queue) {
        this(clockMs, retention, maxRetained, running, queue, MAX_RUNTIME);
    }

    ChunkKeysSweepJobs(LongSupplier clockMs, Duration retention, int maxRetained, int running, int queue,
                       Duration maxRuntime) {
        this.jobs = new EngineJobs<>("chunk-keys-sweep-job-", clockMs,
            new EngineJobs.Bounds(running, queue, maxRetained, retention, maxRuntime), ChunkKeysSweepJobs::failed);
    }

    /** Test seam (nexus-v75nw): the wall-clock cap of the jobs started from now on and of those held now, running or queued. */
    public void maxRuntimeForTests(Duration maxRuntime) {
        jobs.maxRuntimeForTests(maxRuntime);
    }

    /** The clock in use, so a status answer's {@code elapsed_ms} reads the same one the jobs were stamped with. */
    public long nowMs() {
        return jobs.nowMs();
    }

    /**
     * Starts the dry run {@code work} for {@code tenant}, or returns the job already running for that tenant when its
     * request is the same ({@code fingerprint}; {@link Started#created} is then false). A work that throws ends the job
     * {@code FAILED} with a 500.
     *
     * @throws Conflict a different request of this tenant is running
     * @throws Busy     every thread and queue place is taken
     */
    public Started start(String tenant, String callerTenant, boolean root, String fingerprint, Work work) {
        return start(tenant, callerTenant, root, Kind.DRY_RUN, fingerprint, (job, stage) -> work.run(stage));
    }

    /**
     * As above for a job of {@code kind}, the work told its job. The scope is the tenant: one running job per tenant,
     * dry run or apply. Only a RUNNING job of the same kind and request, started by this caller, is joined; a finished
     * one is never handed to a new start, which always runs (a rerun after a refusal or a sweep must count again, not
     * replay the outcome). Any other running job of the tenant is a {@link Conflict}.
     */
    public Started start(String tenant, String callerTenant, boolean root, Kind kind, String fingerprint,
                         JobWork work) {
        try {
            EngineJobs.Started<Job> s = jobs.start(tenant, null, kind.wire() + '\u0000' + fingerprint,
                j -> j.data().startedBy(callerTenant, root),
                core -> new Job(core, callerTenant, root, kind),
                (core, stage) -> {
                    Outcome o = work.run(core.data(), stage);
                    return new EngineJobs.Outcome(o.httpStatus(), o.body());
                });
            return new Started(s.job().data(), s.created());
        } catch (EngineJobs.Conflict c) {
            throw new Conflict(c.jobId(), ((Job) c.running().data()).kind);
        } catch (EngineJobs.Busy b) {
            throw new Busy();
        }
    }

    /** A throwing work's outcome: a 500 naming the class, never the message, and what an apply swept before it. */
    private static EngineJobs.Outcome failed(EngineJobs.Job<Job> core, Throwable t) {
        Job job = core.data();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("tenant", core.tenant());
        boolean apply = job.kind == Kind.APPLY;
        body.put("error", "the " + (apply ? "apply" : "dry run") + " failed: " + t.getClass().getSimpleName());
        body.put("reason", "sweep_failed");
        body.put("dry_run", !apply);
        if (apply) {
            body.put("rows_swept", job.rowsSwept);
            body.put("rows_changed", job.rowsSwept > 0);
        }
        return new EngineJobs.Outcome(500, body);
    }

    /** The job with this id, or null when the engine holds none (never started, expired, evicted, or another process). */
    public Job find(String id) {
        EngineJobs.Job<Job> j = jobs.findAnyTenant(id);
        return j == null ? null : j.data();
    }

    /** Whether the tenant reset hid {@code id} while it ran (the worker then writes no outcome row). */
    public boolean evicted(String id) {
        return jobs.evicted(id);
    }

    /**
     * Drops every job of {@code tenant} (counted or started by it): the tenant reset route's evictor. A job that is
     * still running is not cut off (a dry run ends at its next stage, an apply at its next batch boundary, both told
     * by {@link Job#stopReason()}), and the tenant keeps its one-running-job guard until it ends, so it stays
     * in the table marked {@link Job#evicted}: no poll finds it, a start does not
     * join it, and its worker writes no outcome row into the reset tenant. Returns how many jobs this dropped or hid.
     */
    public int evictTenant(String tenant) {
        return jobs.evictWhere(j -> j.tenant().equals(tenant) || j.data().callerTenant.equals(tenant));
    }

    /** Jobs held now. */
    public int size() {
        return jobs.size();
    }

    /** Stops accepting work and interrupts what runs; the engine's shutdown. */
    public void shutdown() {
        jobs.shutdown();
    }
}
