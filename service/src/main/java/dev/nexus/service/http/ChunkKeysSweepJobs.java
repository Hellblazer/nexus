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
 * nexus-v75nw: the dry runs of the retire-keys sweep that run in the background, so a tenant whose preconditions take
 * longer than the edge's origin timeout (about 30 s) can still be counted. {@code POST /v1/admin/chunk-keys/sweep}
 * with {@code "async": true} on a dry run starts one here and answers at once with its id; {@code GET
 * /v1/admin/chunk-keys/sweep/jobs/{id}} reads its stage and, when it has ended, the status and body the synchronous
 * call would have answered.
 *
 * <p><b>Memory only.</b> A dry run writes nothing and is cheap to repeat, so the table is not persisted: a job is
 * lost with the engine process, and a poll for an id the engine does not hold is a 404 the client answers by
 * starting again. A finished job is kept for {@link #RETENTION} and at most {@link #MAX_RETAINED} are held.
 *
 * <p><b>Bounds.</b> At most {@link #MAX_RUNNING} jobs run at once, each holding one pooled connection at a time, and
 * {@link #QUEUE} more wait; a start past that is refused ({@link Busy}, 503) rather than queued without end. One
 * tenant has at most one running job: a second start with the same request is handed the running job, and one with a
 * different request is refused ({@link Conflict}) until it ends.
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

    /** A start's answer: the job, and whether this call created it (false: it joined the one already running). */
    public record Started(Job job, boolean created) { }

    /** What a job ended with: the HTTP status and body the synchronous call would have answered. */
    public record Outcome(int httpStatus, Map<String, Object> body) { }

    /** The work of a job; it names each step to the sink it is given. */
    @FunctionalInterface
    public interface Work {
        Outcome run(Consumer<String> stage) throws Exception;
    }

    /** As {@link Work}, told the id of the job it runs as: the signed continuation of a finished dry run names it. */
    @FunctionalInterface
    public interface JobWork {
        Outcome run(String jobId, Consumer<String> stage) throws Exception;
    }

    /** No thread and no queue place is free. */
    public static final class Busy extends RuntimeException {
        Busy() {
            super("the engine is already running or holding the most dry-run jobs it takes; retry shortly");
        }
    }

    /** The tenant has a running job for a different request. */
    public static final class Conflict extends RuntimeException {
        private final String jobId;

        Conflict(String jobId) {
            super("a dry run of this tenant is already running with a different request (job " + jobId + "); wait "
                + "for it to end, or poll it");
            this.jobId = jobId;
        }

        public String jobId() {
            return jobId;
        }
    }

    /** One job. The mutable fields are written by the worker thread and read by the polling request. */
    public static final class Job {
        private final String id;
        private final String tenant;
        private final String callerTenant;
        private final boolean root;
        private final String fingerprint;
        private final long startedMs;
        /** Set by the tenant reset while the job runs: it stays (the tenant keeps its one running job) but is unseen. */
        private volatile boolean evicted;
        private volatile String stage = "queued";
        private volatile State state = State.RUNNING;
        private volatile Outcome outcome;
        private volatile long finishedMs;

        private Job(String id, String tenant, String callerTenant, boolean root, String fingerprint, long startedMs) {
            this.id = id;
            this.tenant = tenant;
            this.callerTenant = callerTenant;
            this.root = root;
            this.fingerprint = fingerprint;
            this.startedMs = startedMs;
        }

        public String id() {
            return id;
        }

        /** The tenant the dry run counts. */
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
    private final ThreadPoolExecutor pool;
    private final Map<String, Job> jobs = new LinkedHashMap<>();

    public ChunkKeysSweepJobs() {
        this(System::currentTimeMillis, RETENTION, MAX_RETAINED, MAX_RUNNING, QUEUE);
    }

    ChunkKeysSweepJobs(LongSupplier clockMs, Duration retention, int maxRetained, int running, int queue) {
        this.clockMs = clockMs;
        this.retention = retention;
        this.maxRetained = maxRetained;
        AtomicInteger n = new AtomicInteger();
        this.pool = new ThreadPoolExecutor(running, running, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(queue), r -> {
                Thread t = new Thread(r, "chunk-keys-sweep-job-" + n.incrementAndGet());
                t.setDaemon(true);
                return t;
            });
    }

    /** The clock in use, so a status answer's {@code elapsed_ms} reads the same one the jobs were stamped with. */
    public long nowMs() {
        return clockMs.getAsLong();
    }

    /**
     * Starts {@code work} for {@code tenant}, or returns the job already running for that tenant when its request is
     * the same ({@code fingerprint}; {@link Started#created} is then false). A work that throws ends the job {@code FAILED} with a 500.
     *
     * @throws Conflict a different request of this tenant is running
     * @throws Busy     every thread and queue place is taken
     */
    public synchronized Started start(String tenant, String callerTenant, boolean root, String fingerprint, Work work) {
        return start(tenant, callerTenant, root, fingerprint, (id, stage) -> work.run(stage));
    }

    /**
     * As above, the work told its job's id. A start whose request equals a job of this tenant that FINISHED
     * ({@link State#DONE}) inside the retention window is handed that job instead of a recount; a failed one is not
     * reused, and the apply drops a tenant's finished jobs ({@link #evictFinished}) because its counts are stale then.
     */
    public synchronized Started start(String tenant, String callerTenant, boolean root, String fingerprint,
                                      JobWork work) {
        evictExpired();
        Job finished = null;
        for (Job j : jobs.values()) {
            if (!j.tenant.equals(tenant)) {
                continue;
            }
            if (j.state == State.RUNNING) {
                if (!j.evicted && j.fingerprint.equals(fingerprint) && j.startedBy(callerTenant, root)) {
                    return new Started(j, false);
                }
                throw new Conflict(j.id);
            }
            if (j.state == State.DONE && j.fingerprint.equals(fingerprint) && j.startedBy(callerTenant, root)) {
                finished = j;
            }
        }
        if (finished != null) {
            return new Started(finished, false);
        }
        Job job = new Job(UUID.randomUUID().toString(), tenant, callerTenant, root, fingerprint, clockMs.getAsLong());
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
        try {
            outcome = work.run(job.id, s -> job.stage = s);
            end = outcome.httpStatus() == 200 ? State.DONE : State.FAILED;
        } catch (Throwable t) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("tenant", job.tenant);
            body.put("error", "the dry run failed: " + t.getClass().getSimpleName());
            body.put("reason", "sweep_failed");
            body.put("dry_run", true);
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

    /** Whether the engine still holds the finished job {@code id} of {@code tenant}: the life of its continuation. */
    public synchronized boolean holdsFinished(String id, String tenant) {
        evictExpired();
        Job j = id == null ? null : jobs.get(id);
        return j != null && !j.evicted && j.state == State.DONE && j.tenant.equals(tenant);
    }

    /** Whether the tenant reset hid {@code id} while it ran (the worker then writes no outcome row). */
    public synchronized boolean evicted(String id) {
        Job j = jobs.get(id);
        return j != null && j.evicted;
    }

    /**
     * Drops every job of {@code tenant} (counted or started by it): the tenant reset route's evictor. A job that is
     * still running cannot be stopped, and the tenant keeps its one-running-job guard until it ends, so it stays
     * in the table marked {@link Job#evicted}: no poll finds it, a continuation made from it lapses, a start does not
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

    /**
     * Drops the finished jobs of {@code tenant}: an apply that changed rows makes their counts stale, so neither a
     * rerun may be handed one nor its continuation applied. A running job stays. Returns how many were dropped.
     */
    public synchronized int evictFinished(String tenant) {
        int before = jobs.size();
        jobs.values().removeIf(j -> j.state != State.RUNNING && j.tenant.equals(tenant));
        return before - jobs.size();
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
