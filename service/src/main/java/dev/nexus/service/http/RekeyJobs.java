package dev.nexus.service.http;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayDeque;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * nexus-b878d — async job registry for the RDR-180 per-tenant rekey.
 *
 * <p><strong>Why.</strong> {@code POST /v1/remap/rekey} ran ~90s+ at production
 * scale. conexus-engine-tls shares the engine netns and its nginx
 * {@code proxy_read_timeout} is ~120s, so gate-xr789 took a 504 at 120.3s while
 * the transaction COMMITTED 88s later: the operator saw a failure and the store
 * had silently changed — the GH #1390 hazard class arriving through the proxy.
 * As shipped, the synchronous endpoint was unreachable at production scale
 * through any proxied path. Submission now returns immediately and the outcome
 * is fetched by poll, so no single request is held open long enough for a proxy
 * to guess wrong about it.
 *
 * <p><strong>Why this state is in memory and not in Postgres.</strong> A rekey
 * is ONE transaction, so in the overwhelming majority of engine deaths the
 * outcome is rollback-and-store-unchanged, and a job table would be persisting
 * a fact that is already derivable. Job ids are instead fenced with an
 * {@link #epoch()} minted per engine instance, so a poll carrying a previous
 * instance's epoch is recognisable as such rather than reading like a typo.
 *
 * <p><strong>The window this does NOT close, stated plainly.</strong> The
 * commit happens inside {@code TenantScope}, and this class marks the job
 * SUCCEEDED only after {@code runner.run(...)} returns. Those are two steps,
 * so a JVM death in between (SIGKILL, OOM-kill, node eviction) leaves a store
 * that HAS changed and a job that never reached SUCCEEDED. A foreign-epoch
 * poll therefore must NOT claim the store is unchanged — it cannot know that,
 * and claiming it would re-commit the exact sin this bead exists to remove
 * (telling an operator nothing happened when something did). It answers
 * "outcome unknown to this instance" and points at the two things that do
 * settle it: the server-side {@code event=rekey_complete} log, which is the
 * authoritative record of what any completed rekey did (it is what recovered
 * the envelope the 504 appeared to lose), and the rekey's idempotence — a
 * re-run over an already-rekeyed store reports all-zero counts, so re-running
 * is both safe and self-answering.
 *
 * <p>A Postgres job table would not close that window either: a row written
 * after the rekey commits has the identical gap. Only writing job state INSIDE
 * the rekey transaction would be atomic, which is a materially larger design
 * for a case that an idempotent re-run already resolves correctly.
 *
 * <p><strong>Concurrency.</strong> One in-flight rekey per tenant, enforced
 * here so callers get a clean {@link AlreadyRunningException} naming the job
 * already running instead of piling threads up behind the per-tenant
 * {@code pg_advisory_xact_lock} that {@code RekeyOps} takes anyway. The pool is
 * cached and therefore self-bounding: the per-tenant gate caps live tasks at
 * the number of distinct tenants.
 *
 * <p>The runner is constructor-injected, which keeps this class free of any
 * database dependency and lets its tests drive concurrency with latches.
 */
public final class RekeyJobs implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(RekeyJobs.class);

    /** Terminal jobs kept for polling after completion. */
    private static final int DEFAULT_RETENTION = 64;

    /** The work a submitted job performs: {@code (tenant, synthesizeOrphans) -> envelope}. */
    @FunctionalInterface
    public interface Runner {
        Map<String, Object> run(String tenant, boolean synthesizeOrphans);
    }

    public enum State { RUNNING, SUCCEEDED, FAILED }

    /**
     * A submitted rekey. {@code envelope} is populated only on SUCCEEDED and
     * {@code failure} only on FAILED; the handler types the HTTP status off the
     * throwable rather than off a stringified kind.
     */
    public record Job(String id,
                      String tenant,
                      State state,
                      Map<String, Object> envelope,
                      Throwable failure) {}

    /** The four honest answers to a poll. */
    public sealed interface Lookup {
        record Found(Job job) implements Lookup {}
        /** Our epoch, but no such job — never submitted, or aged out of retention. */
        record Unknown() implements Lookup {}
        /**
         * Minted by a previous engine instance. Almost always means the
         * transaction rolled back — but NOT provably, because the engine can
         * die between the commit and this registry recording it, so callers
         * must report the outcome as unknown rather than as unchanged.
         */
        record ForeignEpoch(String jobEpoch) implements Lookup {}
        record Malformed() implements Lookup {}
    }

    /** One rekey per tenant at a time; carries the id of the one already running. */
    public static final class AlreadyRunningException extends RuntimeException {
        private final String runningJobId;

        AlreadyRunningException(String tenant, String runningJobId) {
            super("a rekey is already running for tenant '" + tenant
                  + "' (job " + runningJobId + ") — poll it rather than starting another");
            this.runningJobId = runningJobId;
        }

        public String runningJobId() {
            return runningJobId;
        }
    }

    private final Runner runner;
    private final String epoch;
    private final int retention;
    private final ExecutorService pool;

    /** tenant -> id of its in-flight job. */
    private final ConcurrentHashMap<String, String> inFlight = new ConcurrentHashMap<>();

    /** Every known job, running and terminal. Lookup is lock-free. */
    private final ConcurrentHashMap<String, Job> jobs = new ConcurrentHashMap<>();

    /**
     * Terminal job ids in completion order — the eviction queue, and the ONLY
     * thing eviction consults.
     *
     * <p>This is deliberately separate from {@link #jobs} rather than a
     * {@code LinkedHashMap} with a {@code removeEldestEntry} override. That
     * shape looked equivalent and is not: {@code removeEldestEntry} is only
     * ever offered the map's HEAD, so declining to evict a running head means
     * declining to evict anything at all. One wedged job sitting at the head
     * would silently suspend retention for the whole registry, and the map
     * would grow without bound — a leak wearing the disguise of a safety
     * check. Enqueueing only terminal jobs makes "never evict a running job"
     * true by construction and keeps the bound applying to everything else.
     */
    private final ArrayDeque<String> terminalOrder = new ArrayDeque<>();

    public RekeyJobs(Runner runner) {
        this(runner, DEFAULT_RETENTION, UUID.randomUUID().toString().substring(0, 8));
    }

    RekeyJobs(Runner runner, int retention, String epoch) {
        this.runner = runner;
        this.retention = retention;
        this.epoch = epoch;
        AtomicInteger n = new AtomicInteger();
        ThreadFactory tf = r -> {
            Thread t = new Thread(r, "rekey-job-" + n.incrementAndGet());
            t.setDaemon(true);
            return t;
        };
        this.pool = Executors.newCachedThreadPool(tf);
    }

    /**
     * Record a job's terminal state and reclaim the oldest terminal jobs past
     * {@link #retention}. Running jobs are never enqueued here, so they cannot
     * be reclaimed and cannot block reclamation of anything else.
     */
    private void recordTerminal(String id, Job job) {
        jobs.put(id, job);
        synchronized (terminalOrder) {
            terminalOrder.addLast(id);
            while (terminalOrder.size() > retention) {
                jobs.remove(terminalOrder.removeFirst());
            }
        }
    }

    /** The engine-instance epoch every job id minted here is prefixed with. */
    public String epoch() {
        return epoch;
    }

    /**
     * Start a rekey for *tenant* and return its job id immediately.
     *
     * @throws AlreadyRunningException if that tenant already has one in flight
     */
    public String submit(String tenant, boolean synthesizeOrphans) {
        String id = epoch + "-" + UUID.randomUUID();
        String existing = inFlight.putIfAbsent(tenant, id);
        if (existing != null) {
            throw new AlreadyRunningException(tenant, existing);
        }
        jobs.put(id, new Job(id, tenant, State.RUNNING, null, null));
        log.info("event=rekey_job_submitted tenant={} job_id={}", tenant, id);
        try {
            pool.execute(() -> run(id, tenant, synthesizeOrphans));
        } catch (RuntimeException e) {
            // Never leave a tenant gated behind a job that was never scheduled.
            inFlight.remove(tenant, id);
            recordTerminal(id, new Job(id, tenant, State.FAILED, null, e));
            throw e;
        }
        return id;
    }

    private void run(String id, String tenant, boolean synthesizeOrphans) {
        try {
            Map<String, Object> envelope = runner.run(tenant, synthesizeOrphans);
            recordTerminal(id, new Job(id, tenant, State.SUCCEEDED, envelope, null));
            log.info("event=rekey_job_succeeded tenant={} job_id={}", tenant, id);
        } catch (Throwable t) {
            recordTerminal(id, new Job(id, tenant, State.FAILED, null, t));
            log.error("event=rekey_job_failed tenant={} job_id={} error={}",
                    tenant, id, t.getMessage(), t);
        } finally {
            inFlight.remove(tenant, id);
        }
    }

    /**
     * Resolve a job id to one of the four {@link Lookup} answers.
     *
     * <p>NOT tenant-scoped: a {@link Lookup.Found} carries whatever job owns
     * that id. Callers serving a request MUST compare {@link Job#tenant()}
     * against the caller's tenant before returning anything from it — see
     * {@code RemapHandler.handleRekeyStatus}, which answers 404 on a mismatch.
     */
    public Lookup lookup(String jobId) {
        if (jobId == null) return new Lookup.Malformed();
        int dash = jobId.indexOf('-');
        if (dash <= 0 || dash == jobId.length() - 1) return new Lookup.Malformed();
        String jobEpoch = jobId.substring(0, dash);
        if (!epoch.equals(jobEpoch)) return new Lookup.ForeignEpoch(jobEpoch);
        Job job = jobs.get(jobId);
        return job == null ? new Lookup.Unknown() : new Lookup.Found(job);
    }

    @Override
    public void close() {
        pool.shutdownNow();
    }
}
