// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
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

    /** One job. The mutable fields are written by the worker thread and read by the polling request. */
    public static final class Job {
        private final String id;
        private final String tenant;
        private final String callerTenant;
        private final boolean root;
        private final String fingerprint;
        private final Kind kind;
        private final long startedMs;
        /** When a worker thread took the job up, or -1 while it is queued: the wall-clock cap runs from here. */
        private volatile long runStartedMs = -1;
        private final LongSupplier clockMs;
        private volatile long maxRuntimeMs;
        /** Set by {@link #requestCancel}: the apply stops at its next batch boundary. */
        private volatile boolean cancelRequested;
        /** Set by {@link #stopFor}: a stop the work found for itself (a revoked credential); the first one stays. */
        private volatile String stopCause;
        /** The apply's progress so far, written by its worker and read by a poll; -1 remaining means not known yet. */
        private volatile int batches;
        private volatile long rowsSwept;
        private volatile long remaining = -1;
        /** Set by the tenant reset while the job runs: it stays (the tenant keeps its one running job) but is unseen. */
        private volatile boolean evicted;
        private volatile String stage = "queued";
        private volatile State state = State.RUNNING;
        private volatile Outcome outcome;
        private volatile long finishedMs;

        private Job(String id, String tenant, String callerTenant, boolean root, String fingerprint, Kind kind,
                    LongSupplier clockMs, long maxRuntimeMs) {
            this.id = id;
            this.tenant = tenant;
            this.callerTenant = callerTenant;
            this.root = root;
            this.fingerprint = fingerprint;
            this.kind = kind;
            this.clockMs = clockMs;
            this.maxRuntimeMs = maxRuntimeMs;
            this.startedMs = clockMs.getAsLong();
        }

        public Kind kind() {
            return kind;
        }

        /**
         * Asks a running apply to stop at its next batch boundary. False (and nothing changes) for a dry run, which
         * writes nothing and has no batch to stop at, and for a job that has ended.
         */
        public boolean requestCancel() {
            if (kind != Kind.APPLY || state != State.RUNNING) {
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
            if (stopCause == null) {
                stopCause = reason;
            }
        }

        /**
         * Why the work should stop at its next boundary, or null: {@code evicted} (the tenant reset dropped the job),
         * {@code cancelled} (its starter asked), the reason the work gave {@link #stopFor} (such as {@code
         * credential_revoked}), or {@code wall_clock_cap} ({@link #MAX_RUNTIME} since a worker took the job up: a job
         * that waits in the queue has not started its cap clock).
         */
        public String stopReason() {
            if (evicted) {
                return "evicted";
            }
            if (cancelRequested) {
                return "cancelled";
            }
            String cause = stopCause;
            if (cause != null) {
                return cause;
            }
            long run = runStartedMs;
            return run >= 0 && clockMs.getAsLong() - run >= maxRuntimeMs ? "wall_clock_cap" : null;
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
            return id;
        }

        /** The tenant the job counts (a dry run) or sweeps (an apply). */
        public String tenant() {
            return tenant;
        }

        public State state() {
            return state;
        }

        /** True once the tenant reset dropped this job: its outcome belongs to no tenant any more and is not recorded. */
        public boolean evicted() {
            return evicted;
        }

        public String stage() {
            return stage;
        }

        /** The status and body the synchronous call would have answered; null while the job runs. */
        public Outcome outcome() {
            return outcome;
        }

        /** Whether {@code callerTenant}, a root credential or not, is the one that started this job. */
        public boolean startedBy(String caller, boolean rootCredential) {
            return callerTenant.equals(caller) && (!root || rootCredential);
        }

        /** Milliseconds from the start to now, or to the end of a finished job. */
        public long elapsedMs(long nowMs) {
            return (state == State.RUNNING ? nowMs : finishedMs) - startedMs;
        }
    }

    private final LongSupplier clockMs;
    private final Duration retention;
    private final int maxRetained;
    private volatile Duration maxRuntime;
    private final ThreadPoolExecutor pool;
    private final Map<String, Job> jobs = new LinkedHashMap<>();

    public ChunkKeysSweepJobs() {
        this(System::currentTimeMillis, RETENTION, MAX_RETAINED, MAX_RUNNING, QUEUE);
    }

    ChunkKeysSweepJobs(LongSupplier clockMs, Duration retention, int maxRetained, int running, int queue) {
        this(clockMs, retention, maxRetained, running, queue, MAX_RUNTIME);
    }

    ChunkKeysSweepJobs(LongSupplier clockMs, Duration retention, int maxRetained, int running, int queue,
                       Duration maxRuntime) {
        this.clockMs = clockMs;
        this.retention = retention;
        this.maxRetained = maxRetained;
        this.maxRuntime = maxRuntime;
        AtomicInteger n = new AtomicInteger();
        this.pool = new ThreadPoolExecutor(running, running, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(queue), r -> {
                Thread t = new Thread(r, "chunk-keys-sweep-job-" + n.incrementAndGet());
                t.setDaemon(true);
                return t;
            });
    }

    /** Test seam (nexus-v75nw): the wall-clock cap of the jobs started from now on and of those held now, running or queued. */
    public synchronized void maxRuntimeForTests(Duration maxRuntime) {
        this.maxRuntime = maxRuntime;
        for (Job j : jobs.values()) {
            j.maxRuntimeMs = maxRuntime.toMillis();
        }
    }

    /** The clock in use, so a status answer's {@code elapsed_ms} reads the same one the jobs were stamped with. */
    public long nowMs() {
        return clockMs.getAsLong();
    }

    /**
     * Starts the dry run {@code work} for {@code tenant}, or returns the job already running for that tenant when its
     * request is the same ({@code fingerprint}; {@link Started#created} is then false). A work that throws ends the job
     * {@code FAILED} with a 500.
     *
     * @throws Conflict a different request of this tenant is running
     * @throws Busy     every thread and queue place is taken
     */
    public synchronized Started start(String tenant, String callerTenant, boolean root, String fingerprint, Work work) {
        return start(tenant, callerTenant, root, Kind.DRY_RUN, fingerprint, (job, stage) -> work.run(stage));
    }

    /**
     * As above for a job of {@code kind}, the work told its job. Only a RUNNING job is joined; a finished one is never
     * handed to a new start, which always runs (a rerun after a refusal or a sweep must count again, not replay the
     * outcome). Any other running job of the tenant, dry run or apply, is a {@link Conflict}.
     */
    public synchronized Started start(String tenant, String callerTenant, boolean root, Kind kind, String fingerprint,
                                      JobWork work) {
        evictExpired();
        for (Job j : jobs.values()) {
            if (!j.tenant.equals(tenant) || j.state != State.RUNNING) {
                continue;
            }
            if (!j.evicted && j.kind == kind && j.fingerprint.equals(fingerprint) && j.startedBy(callerTenant, root)) {
                return new Started(j, false);
            }
            throw new Conflict(j.id, j.kind);
        }
        Job job = new Job(UUID.randomUUID().toString(), tenant, callerTenant, root, fingerprint, kind, clockMs,
            maxRuntime.toMillis());
        try {
            pool.execute(() -> run(job, work));
        } catch (RejectedExecutionException e) {
            throw new Busy();
        }
        jobs.put(job.id, job);
        trim();
        return new Started(job, true);
    }

    private void run(Job job, JobWork work) {
        Outcome outcome;
        State end;
        job.runStartedMs = clockMs.getAsLong();
        try {
            outcome = work.run(job, s -> job.stage = s);
            end = outcome.httpStatus() == 200 ? State.DONE : State.FAILED;
        } catch (Throwable t) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("tenant", job.tenant);
            boolean apply = job.kind == Kind.APPLY;
            body.put("error", "the " + (apply ? "apply" : "dry run") + " failed: " + t.getClass().getSimpleName());
            body.put("reason", "sweep_failed");
            body.put("dry_run", !apply);
            if (apply) {
                body.put("rows_swept", job.rowsSwept);
                body.put("rows_changed", job.rowsSwept > 0);
            }
            outcome = new Outcome(500, body);
            end = State.FAILED;
        }
        job.outcome = outcome;
        job.finishedMs = clockMs.getAsLong();
        job.stage = end == State.DONE ? "done" : "failed";
        job.state = end;   // last: a poll that reads a terminal state finds the outcome already in place
    }

    /** The job with this id, or null when the engine holds none (never started, expired, evicted, or another process). */
    public synchronized Job find(String id) {
        evictExpired();
        Job j = jobs.get(id);
        return j == null || j.evicted ? null : j;
    }

    /** Whether the tenant reset hid {@code id} while it ran (the worker then writes no outcome row). */
    public synchronized boolean evicted(String id) {
        Job j = jobs.get(id);
        return j != null && j.evicted;
    }

    /**
     * Drops every job of {@code tenant} (counted or started by it): the tenant reset route's evictor. A job that is
     * still running is not cut off (a dry run ends at its next stage, an apply at its next batch boundary, both told
     * by {@link Job#stopReason()}), and the tenant keeps its one-running-job guard until it ends, so it stays
     * in the table marked {@link Job#evicted}: no poll finds it, a start does not
     * join it, and its worker writes no outcome row into the reset tenant. Returns how many jobs this dropped or hid.
     */
    public synchronized int evictTenant(String tenant) {
        int n = 0;
        for (Job j : new ArrayList<>(jobs.values())) {
            if (!j.tenant.equals(tenant) && !j.callerTenant.equals(tenant)) {
                continue;
            }
            if (j.state == State.RUNNING) {
                if (!j.evicted) {
                    j.evicted = true;
                    n++;
                }
            } else {
                jobs.remove(j.id);
                n++;
            }
        }
        return n;
    }

    /** Jobs held now. */
    public synchronized int size() {
        return jobs.size();
    }

    /** Stops accepting work and interrupts what runs; the engine's shutdown. */
    public void shutdown() {
        pool.shutdownNow();
    }

    private void evictExpired() {
        long cutoff = clockMs.getAsLong() - retention.toMillis();
        jobs.values().removeIf(j -> j.state != State.RUNNING && (j.evicted || j.finishedMs < cutoff));
    }

    private void trim() {
        if (jobs.size() <= maxRetained) {
            return;
        }
        List<String> finished = new ArrayList<>();
        for (Map.Entry<String, Job> e : jobs.entrySet()) {
            if (e.getValue().state != State.RUNNING) {
                finished.add(e.getKey());
            }
        }
        for (String id : finished) {
            if (jobs.size() <= maxRetained) {
                break;
            }
            jobs.remove(id);
        }
    }
}
