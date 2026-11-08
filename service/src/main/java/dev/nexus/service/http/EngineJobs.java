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
import java.util.function.BiFunction;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.LongSupplier;
import java.util.function.Predicate;

/**
 * RDR-193 Step 1 (nexus-u5m5g.2): the engine's registry of background jobs, pulled out of {@link ChunkKeysSweepJobs}
 * so each job kind (the chunk-keys sweep, the catalog reconcile, later the taxonomy discover) gets its own instance
 * with its own bounds, and a running job of one kind never blocks another kind for the same tenant.
 *
 * <p><b>Scope.</b> Every job has a scope and a request fingerprint. Within one scope at most one job runs: a start
 * with the same fingerprint as the running job is handed that job ({@link Started#created} false, the route's 202 with
 * its id), and one with a different fingerprint is a {@link Conflict} naming it (409). The scope is built here from the
 * start's tenant and, for a kind scoped finer than the tenant, the thing within it (reconcile: the owner), so two
 * repositories of one tenant indexed at once do not wait on each other, and no start can name a scope of another
 * tenant. A finished job is only ever read: a start never reuses one.
 *
 * <p><b>Tenant.</b> Every job also stores its tenant, apart from its scope key. {@link #find(String, String)} answers a
 * poll only for the job's own tenant (another tenant's poll finds nothing, the route's 404), and {@link #evictTenant}
 * matches the stored tenant whatever the scope key, so the tenant reset route reaches a tenant-plus-owner job too.
 *
 * <p><b>Memory only.</b> Jobs are lost with the engine process. A poll for an id the engine does not hold is a 404,
 * and the client resubmits: every job kind here is idempotent to resubmit. A finished job is kept for the
 * {@linkplain Bounds#keepFor keep-for} period, and at most {@linkplain Bounds#retained retained} jobs are held.
 *
 * <p><b>Bounds.</b> At most {@linkplain Bounds#running running} jobs run at once and {@linkplain Bounds#queue queue}
 * more wait; a start past that is {@link Busy} (503). A job's work asks {@link Job#stopReason()} at its own boundaries
 * and stops there once the {@linkplain Bounds#maxRuntime maximum runtime} has passed since a worker took it up (time
 * in the queue does not count), or once the tenant reset evicted it.
 *
 * @param <A> what the job kind keeps on each job beside the shared fields ({@link Job#data()}); {@code Void} for none
 */
public final class EngineJobs<A> {

    /**
     * The bounds of one instance: jobs running at once, jobs waiting, jobs held at all, how long a finished job is kept
     * for its client to read ({@code keepFor}), and the longest a job runs once a worker takes it up.
     */
    public record Bounds(int running, int queue, int retained, Duration keepFor, Duration maxRuntime) { }

    /**
     * The reconcile instance's bounds (RDR-193 Step 1): many tenants committing at once, each commit short (the
     * 20,000-row commit's accept bar is 30 s, so 15 minutes stops only a stuck one).
     */
    public static final Bounds RECONCILE = new Bounds(4, 32, 256, Duration.ofMinutes(30), Duration.ofMinutes(15));

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

    /** A start's answer: the job, and whether this call created it (false: it was handed the one already running). */
    public record Started<A>(Job<A> job, boolean created) { }

    /** What a job ended with: the HTTP status and body the synchronous call would have answered. */
    public record Outcome(int httpStatus, Map<String, Object> body) { }

    /** The work of a job, told the job it runs as and where to publish its stage. A 200 ends DONE, any other FAILED. */
    @FunctionalInterface
    public interface Work<A> {
        Outcome run(Job<A> job, Consumer<String> stage) throws Exception;
    }

    /** No thread and no queue place is free. */
    public static final class Busy extends RuntimeException {
        Busy() {
            super("the engine is already running or holding the most jobs of this kind it takes; retry shortly");
        }
    }

    /** The scope has a running job for a different request. */
    public static final class Conflict extends RuntimeException {
        private final transient Job<?> running;

        Conflict(Job<?> running) {
            super("a job of this scope is already running with a different request (job " + running.id()
                + "); poll it, do not resubmit");
            this.running = running;
        }

        public String jobId() {
            return running.id();
        }

        /** The running job, so a kind's wrapper can say what it is. */
        public Job<?> running() {
            return running;
        }
    }

    /** One job. The mutable fields are written by the worker thread and read by the polling request. */
    public static final class Job<A> {
        private final String id;
        private final String tenant;
        private final String scopeKey;
        private final String fingerprint;
        private final long startedMs;
        private final LongSupplier clockMs;
        private A data;
        /** When a worker took the job up, or -1 while it is queued: the runtime cap runs from here. */
        private volatile long runStartedMs = -1;
        private volatile long maxRuntimeMs;
        /** Set by {@link #stopFor}: a stop the work found for itself; the first one stays. */
        private volatile String stopCause;
        /** Set by the tenant reset while the job runs: it keeps its scope until it ends, but no poll finds it. */
        private volatile boolean evicted;
        private volatile String stage = "queued";
        private volatile State state = State.RUNNING;
        private volatile Outcome outcome;
        private volatile long finishedMs;

        private Job(String id, String tenant, String scopeKey, String fingerprint, LongSupplier clockMs,
                    long maxRuntimeMs) {
            this.id = id;
            this.tenant = tenant;
            this.scopeKey = scopeKey;
            this.fingerprint = fingerprint;
            this.clockMs = clockMs;
            this.maxRuntimeMs = maxRuntimeMs;
            this.startedMs = clockMs.getAsLong();
        }

        public String id() {
            return id;
        }

        /** The tenant the job belongs to: the one a poll must come from and the one the tenant reset matches. */
        public String tenant() {
            return tenant;
        }

        /** What the job kind keeps on this job, or null for a kind that keeps nothing. */
        public A data() {
            return data;
        }

        public State state() {
            return state;
        }

        public String stage() {
            return stage;
        }

        /** The status and body of the ended job; null while it runs. */
        public Outcome outcome() {
            return outcome;
        }

        /** True once the tenant reset dropped this job. */
        public boolean evicted() {
            return evicted;
        }

        /** Asks the job to stop at its next boundary for a reason the work found itself; the first reason stays. */
        public void stopFor(String reason) {
            if (stopCause == null) {
                stopCause = reason;
            }
        }

        /**
         * Why the work should stop at its next boundary, or null: {@code evicted} (the tenant reset dropped the job),
         * the reason given to {@link #stopFor}, or {@code wall_clock_cap} (the maximum runtime since a worker took the
         * job up).
         */
        public String stopReason() {
            if (evicted) {
                return "evicted";
            }
            String cause = stopCause;
            if (cause != null) {
                return cause;
            }
            long run = runStartedMs;
            return run >= 0 && clockMs.getAsLong() - run >= maxRuntimeMs ? "wall_clock_cap" : null;
        }

        /** Milliseconds from the start to now, or to the end of a finished job. */
        public long elapsedMs(long nowMs) {
            return (state == State.RUNNING ? nowMs : finishedMs) - startedMs;
        }
    }

    private final LongSupplier clockMs;
    private final Bounds bounds;
    private final BiFunction<Job<A>, Throwable, Outcome> failure;
    private volatile Duration maxRuntime;
    private final ThreadPoolExecutor pool;
    private final Map<String, Job<A>> jobs = new LinkedHashMap<>();

    /**
     * @param threadPrefix the name prefix of the worker threads (a counter follows it)
     * @param failure      the outcome of a job whose work threw: told the job and what it threw
     */
    public EngineJobs(String threadPrefix, LongSupplier clockMs, Bounds bounds,
                      BiFunction<Job<A>, Throwable, Outcome> failure) {
        this.clockMs = clockMs;
        this.bounds = bounds;
        this.failure = failure;
        this.maxRuntime = bounds.maxRuntime();
        AtomicInteger n = new AtomicInteger();
        this.pool = new ThreadPoolExecutor(bounds.running(), bounds.running(), 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(bounds.queue()), r -> {
                Thread t = new Thread(r, threadPrefix + n.incrementAndGet());
                t.setDaemon(true);
                return t;
            });
    }

    /** The reconcile instance (RDR-193): {@link #RECONCILE} bounds, the default failure outcome. */
    public static EngineJobs<Void> reconcile(LongSupplier clockMs) {
        return new EngineJobs<>("catalog-reconcile-job-", clockMs, RECONCILE, defaultFailure());
    }

    /**
     * The failure outcome of a kind with nothing of its own to add: a 500 naming the tenant and the class of what the
     * work threw, never its message (which can carry another tenant's data or a statement).
     */
    public static <A> BiFunction<Job<A>, Throwable, Outcome> defaultFailure() {
        return (job, thrown) -> {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("tenant", job.tenant());
            body.put("error", "the job failed: " + thrown.getClass().getSimpleName());
            body.put("reason", "job_failed");
            return new Outcome(500, body);
        };
    }

    /** The scope of a job of {@code tenant}: the tenant itself, or the tenant and one thing within it. */
    private static String scopeKey(String tenant, String within) {
        return within == null ? tenant : tenant + '\u0000' + within;
    }

    /** Test seam: the runtime cap of the jobs started from now on and of those held now, running or queued. */
    public synchronized void maxRuntimeForTests(Duration maxRuntime) {
        this.maxRuntime = maxRuntime;
        for (Job<A> j : jobs.values()) {
            j.maxRuntimeMs = maxRuntime.toMillis();
        }
    }

    /** The clock in use, so a status answer's elapsed time reads the same one the jobs were stamped with. */
    public long nowMs() {
        return clockMs.getAsLong();
    }

    /** As {@link #start(String, String, String, Function, Work)} for a kind that keeps nothing on its jobs. */
    public Started<A> start(String tenant, String within, String fingerprint, Work<A> work) {
        return start(tenant, within, fingerprint, job -> null, work);
    }

    /**
     * Starts {@code work} for {@code tenant}, or returns the job running in its scope when its {@code fingerprint} is
     * the same. The scope is the tenant when {@code within} is null, else the tenant and {@code within} (reconcile
     * passes the owner). A running job the tenant reset evicted still holds its scope and is never
     * handed to a start. {@code attach} makes what the kind keeps on the new job; it is not called for a joined job.
     *
     * @throws Conflict a job with a different fingerprint runs in this scope
     * @throws Busy     every thread and queue place is taken
     */
    public Started<A> start(String tenant, String within, String fingerprint, Function<Job<A>, A> attach,
                            Work<A> work) {
        return start(tenant, within, fingerprint, job -> true, attach, work);
    }

    /**
     * As above, the running job handed to the start only when {@code joins} also accepts it (the chunk-keys sweep:
     * only the caller that started it, under its starter check); a running job it rejects is a {@link Conflict}.
     */
    synchronized Started<A> start(String tenant, String within, String fingerprint, Predicate<Job<A>> joins,
                                  Function<Job<A>, A> attach, Work<A> work) {
        String scopeKey = scopeKey(tenant, within);
        evictExpired();
        for (Job<A> j : jobs.values()) {
            if (!j.scopeKey.equals(scopeKey) || j.state != State.RUNNING) {
                continue;
            }
            if (!j.evicted && j.fingerprint.equals(fingerprint) && joins.test(j)) {
                return new Started<>(j, false);
            }
            throw new Conflict(j);
        }
        Job<A> job = new Job<>(UUID.randomUUID().toString(), tenant, scopeKey, fingerprint, clockMs,
            maxRuntime.toMillis());
        job.data = attach.apply(job);
        try {
            pool.execute(() -> run(job, work));
        } catch (RejectedExecutionException e) {
            throw new Busy();
        }
        jobs.put(job.id, job);
        trim();
        return new Started<>(job, true);
    }

    private void run(Job<A> job, Work<A> work) {
        Outcome outcome;
        State end;
        job.runStartedMs = clockMs.getAsLong();
        try {
            outcome = work.run(job, s -> job.stage = s);
            end = outcome.httpStatus() == 200 ? State.DONE : State.FAILED;
        } catch (Throwable t) {
            outcome = failureOutcome(job, t);
            end = State.FAILED;
        }
        job.outcome = outcome;
        job.finishedMs = clockMs.getAsLong();
        job.stage = end == State.DONE ? "done" : "failed";
        job.state = end;   // last: a poll that reads a terminal state finds the outcome already in place
    }

    /**
     * The kind's failure outcome, or the default one when the kind's own throws: a job always reaches a terminal state,
     * or it would hold its scope for good (nothing expires a running job).
     */
    private Outcome failureOutcome(Job<A> job, Throwable thrown) {
        try {
            return failure.apply(job, thrown);
        } catch (Throwable hookFailed) {
            return EngineJobs.<A>defaultFailure().apply(job, thrown);
        }
    }

    /**
     * The job with this id when it belongs to {@code tenant}, or null: never started, expired, evicted, another
     * process, or another tenant's job (the poll answers 404 either way, so it says nothing of another tenant).
     */
    public synchronized Job<A> find(String id, String tenant) {
        Job<A> j = findAnyTenant(id);
        return j == null || !j.tenant.equals(tenant) ? null : j;
    }

    /**
     * The job with this id whoever it belongs to, or null. For a kind whose wrapper makes its own ownership check
     * (the chunk-keys sweep's starter check); a route answering a poll uses {@link #find(String, String)}, never this.
     */
    synchronized Job<A> findAnyTenant(String id) {
        evictExpired();
        Job<A> j = jobs.get(id);
        return j == null || j.evicted ? null : j;
    }

    /** Whether the tenant reset hid {@code id} while it ran (no tenant check: for a kind's own wrapper). */
    synchronized boolean evicted(String id) {
        Job<A> j = jobs.get(id);
        return j != null && j.evicted;
    }

    /**
     * Drops every job of {@code tenant}, whatever its scope key: the tenant reset route's evictor. A running job is
     * not cut off: it is marked {@link Job#evicted} (its work stops at its next boundary, told by {@link
     * Job#stopReason()}) and keeps its scope until it ends. A finished one is removed. Returns how many this dropped
     * or hid.
     */
    public synchronized int evictTenant(String tenant) {
        return evictWhere(j -> j.tenant.equals(tenant));
    }

    /** As {@link #evictTenant}, for the jobs {@code match} selects; a kind whose jobs belong to two tenants uses it. */
    synchronized int evictWhere(Predicate<Job<A>> match) {
        int n = 0;
        for (Job<A> j : new ArrayList<>(jobs.values())) {
            if (!match.test(j)) {
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
        long cutoff = clockMs.getAsLong() - bounds.keepFor().toMillis();
        jobs.values().removeIf(j -> j.state != State.RUNNING && (j.evicted || j.finishedMs < cutoff));
    }

    private void trim() {
        if (jobs.size() <= bounds.retained()) {
            return;
        }
        List<String> finished = new ArrayList<>();
        for (Map.Entry<String, Job<A>> e : jobs.entrySet()) {
            if (e.getValue().state != State.RUNNING) {
                finished.add(e.getKey());
            }
        }
        for (String id : finished) {
            if (jobs.size() <= bounds.retained()) {
                break;
            }
            jobs.remove(id);
        }
    }
}
