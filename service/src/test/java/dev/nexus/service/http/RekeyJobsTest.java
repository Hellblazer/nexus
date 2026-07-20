package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-b878d — {@link RekeyJobs}, the async job registry behind
 * {@code POST /v1/remap/rekey}.
 *
 * <p>Why this class exists at all: the rekey ran ~90s+ at production scale and
 * the tls sidecar's nginx {@code proxy_read_timeout} is ~120s, so gate-xr789
 * took a 504 at 120.3s while the transaction COMMITTED 88s later. The operator
 * saw a failure; the store had changed. Async submission plus a fast poll means
 * no single request is ever held open long enough for a proxy to guess wrong.
 *
 * <p>The runner is constructor-injected so the concurrency here is driven by
 * latches rather than timing — no sleeps, no flake.
 */
class RekeyJobsTest {

    private static final Map<String, Object> ENVELOPE =
        Map.of("disposition", "rekeyed", "chunks_1024", 12);

    /** A runner that blocks until released, so RUNNING is observable. */
    private static final class GatedRunner implements RekeyJobs.Runner {
        final CountDownLatch entered = new CountDownLatch(1);
        final CountDownLatch release = new CountDownLatch(1);

        @Override
        public Map<String, Object> run(String tenant, boolean synthesizeOrphans) {
            entered.countDown();
            try {
                release.await(10, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return ENVELOPE;
        }
    }

    private static RekeyJobs.Job awaitTerminal(RekeyJobs jobs, String id) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline) {
            RekeyJobs.Job j = ((RekeyJobs.Lookup.Found) jobs.lookup(id)).job();
            if (j.state() != RekeyJobs.State.RUNNING) return j;
            Thread.sleep(5);
        }
        throw new AssertionError("job " + id + " never reached a terminal state");
    }

    @Test
    void jobIdCarriesTheEngineEpoch() throws Exception {
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> ENVELOPE)) {
            String id = jobs.submit("tenant-a", false);
            assertThat(id).startsWith(jobs.epoch() + "-");
            awaitTerminal(jobs, id);
        }
    }

    @Test
    void runningJobIsObservable_thenSucceedsCarryingTheEnvelope() throws Exception {
        GatedRunner runner = new GatedRunner();
        try (RekeyJobs jobs = new RekeyJobs(runner)) {
            String id = jobs.submit("tenant-a", false);
            assertThat(runner.entered.await(10, TimeUnit.SECONDS)).isTrue();

            RekeyJobs.Job mid = ((RekeyJobs.Lookup.Found) jobs.lookup(id)).job();
            assertThat(mid.state()).isEqualTo(RekeyJobs.State.RUNNING);
            assertThat(mid.envelope()).isNull();

            runner.release.countDown();
            RekeyJobs.Job done = awaitTerminal(jobs, id);
            assertThat(done.state()).isEqualTo(RekeyJobs.State.SUCCEEDED);
            assertThat(done.envelope()).isEqualTo(ENVELOPE);
            assertThat(done.failure()).isNull();
        }
    }

    @Test
    void failedJobKeepsTheThrowable_soTheHandlerCanTypeTheStatus() throws Exception {
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> {
            throw new IllegalStateException("boom from the runner");
        })) {
            RekeyJobs.Job done = awaitTerminal(jobs, jobs.submit("tenant-a", false));
            assertThat(done.state()).isEqualTo(RekeyJobs.State.FAILED);
            assertThat(done.envelope()).isNull();
            assertThat(done.failure())
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("boom from the runner");
        }
    }

    @Test
    void theOrphanPolicyReachesTheRunner() throws Exception {
        Map<String, Boolean> seen = new LinkedHashMap<>();
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> {
            seen.put(t, s);
            return ENVELOPE;
        })) {
            awaitTerminal(jobs, jobs.submit("tenant-synth", true));
            awaitTerminal(jobs, jobs.submit("tenant-drop", false));
        }
        assertThat(seen).containsEntry("tenant-synth", true).containsEntry("tenant-drop", false);
    }

    @Test
    void secondSubmitWhileOneIsRunning_isRejectedAndNamesTheRunningJob() throws Exception {
        GatedRunner runner = new GatedRunner();
        try (RekeyJobs jobs = new RekeyJobs(runner)) {
            String first = jobs.submit("tenant-a", false);
            assertThat(runner.entered.await(10, TimeUnit.SECONDS)).isTrue();

            assertThatThrownBy(() -> jobs.submit("tenant-a", false))
                .isInstanceOf(RekeyJobs.AlreadyRunningException.class)
                .hasMessageContaining(first);

            runner.release.countDown();
            awaitTerminal(jobs, first);
        }
    }

    /**
     * The inverse of the rejection above, and the one that matters more: a
     * finished rekey must not wedge its tenant out of ever rekeying again.
     */
    @Test
    void submitAfterATerminalJob_isAllowed() throws Exception {
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> ENVELOPE)) {
            awaitTerminal(jobs, jobs.submit("tenant-a", false));
            String second = jobs.submit("tenant-a", false);
            assertThat(awaitTerminal(jobs, second).state()).isEqualTo(RekeyJobs.State.SUCCEEDED);
        }
    }

    /** A FAILED job must release the tenant too, not just a succeeding one. */
    @Test
    void submitAfterAFailedJob_isAllowed() throws Exception {
        java.util.concurrent.atomic.AtomicBoolean firstCall =
            new java.util.concurrent.atomic.AtomicBoolean(true);
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> {
            if (firstCall.getAndSet(false)) throw new IllegalStateException("first fails");
            return ENVELOPE;
        })) {
            assertThat(awaitTerminal(jobs, jobs.submit("tenant-a", false)).state())
                .isEqualTo(RekeyJobs.State.FAILED);
            assertThat(awaitTerminal(jobs, jobs.submit("tenant-a", false)).state())
                .isEqualTo(RekeyJobs.State.SUCCEEDED);
        }
    }

    /** The exclusion is per-tenant, not a global lock. */
    @Test
    void distinctTenantsRunConcurrently() throws Exception {
        CountDownLatch bothIn = new CountDownLatch(2);
        CountDownLatch release = new CountDownLatch(1);
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> {
            bothIn.countDown();
            try {
                release.await(10, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return ENVELOPE;
        })) {
            String a = jobs.submit("tenant-a", false);
            String b = jobs.submit("tenant-b", false);
            assertThat(bothIn.await(10, TimeUnit.SECONDS))
                .as("both tenants' rekeys must be in flight at once")
                .isTrue();
            release.countDown();
            awaitTerminal(jobs, a);
            awaitTerminal(jobs, b);
        }
    }

    /**
     * The load-bearing honesty case. After an engine restart the in-memory
     * registry is empty, but a job id minted by the PREVIOUS instance is still
     * in the operator's hand. That must not read as "never existed" — the
     * derivable truth is that its transaction rolled back and the store is
     * unchanged, and only the epoch tells us so.
     */
    @Test
    void aJobIdFromAPriorEngineInstanceIsForeignEpoch_notUnknown() throws Exception {
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> ENVELOPE)) {
            RekeyJobs.Lookup foreign = jobs.lookup("0000dead-" + java.util.UUID.randomUUID());
            assertThat(foreign).isInstanceOf(RekeyJobs.Lookup.ForeignEpoch.class);
            assertThat(((RekeyJobs.Lookup.ForeignEpoch) foreign).jobEpoch()).isEqualTo("0000dead");

            RekeyJobs.Lookup unknown = jobs.lookup(jobs.epoch() + "-" + java.util.UUID.randomUUID());
            assertThat(unknown).isInstanceOf(RekeyJobs.Lookup.Unknown.class);
        }
    }

    @Test
    void anIdWithNoEpochSeparatorIsMalformed() throws Exception {
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> ENVELOPE)) {
            assertThat(jobs.lookup("")).isInstanceOf(RekeyJobs.Lookup.Malformed.class);
            assertThat(jobs.lookup("nodash")).isInstanceOf(RekeyJobs.Lookup.Malformed.class);
            // A separator IS present, so this parses; the epoch simply is not
            // ours, which is the ForeignEpoch answer rather than Malformed.
            assertThat(jobs.lookup("someone-elses-id"))
                .isInstanceOf(RekeyJobs.Lookup.ForeignEpoch.class);
        }
    }

    @Test
    void terminalJobsAreEvictedOldestFirstOnceRetentionIsExceeded() throws Exception {
        try (RekeyJobs jobs = new RekeyJobs((t, s) -> ENVELOPE, 3, "cafebabe")) {
            String oldest = jobs.submit("tenant-0", false);
            awaitTerminal(jobs, oldest);
            for (int i = 1; i <= 3; i++) {
                awaitTerminal(jobs, jobs.submit("tenant-" + i, false));
            }
            assertThat(jobs.lookup(oldest)).isInstanceOf(RekeyJobs.Lookup.Unknown.class);
        }
    }

    /**
     * Retention must never reclaim a job that is still running — evicting it
     * would strand the operator holding its id with no way to learn the
     * outcome, which is the exact failure b878d exists to remove.
     *
     * <p>AND retention must keep applying to everything else while that job
     * runs. Both halves are asserted here because the obvious implementation
     * (a LinkedHashMap whose removeEldestEntry declines to evict a RUNNING
     * eldest) satisfies only the first: removeEldestEntry is offered the HEAD
     * alone, so a wedged job at the head suspends eviction registry-wide and
     * the map grows without bound. A test that checked only "the running job
     * survived" would pass against that leak — this one fails against it.
     */
    @Test
    void aRunningJobIsNeverEvicted_butRetentionStillAppliesToTheRest() throws Exception {
        GatedRunner runner = new GatedRunner();
        try (RekeyJobs jobs = new RekeyJobs(
                (t, s) -> "tenant-pinned".equals(t) ? runner.run(t, s) : ENVELOPE, 2, "cafebabe")) {
            // Submitted FIRST, so it is the eldest — the position that would
            // wedge a head-only eviction check.
            String pinned = jobs.submit("tenant-pinned", false);
            assertThat(runner.entered.await(10, TimeUnit.SECONDS)).isTrue();

            List<String> fillers = new ArrayList<>();
            for (int i = 0; i < 5; i++) {
                String id = jobs.submit("tenant-filler-" + i, false);
                awaitTerminal(jobs, id);
                fillers.add(id);
            }

            assertThat(jobs.lookup(pinned))
                .as("the running job survived 5 terminal submissions at retention 2")
                .isInstanceOf(RekeyJobs.Lookup.Found.class);

            // The load-bearing half: with retention 2, only the last two
            // terminal jobs may remain. If the running job had suspended
            // eviction, all five would still be here.
            List<String> retained = fillers.stream()
                .filter(id -> jobs.lookup(id) instanceof RekeyJobs.Lookup.Found)
                .toList();
            assertThat(retained)
                .as("a wedged running job must not suspend retention for everyone else")
                .containsExactly(fillers.get(3), fillers.get(4));

            runner.release.countDown();
            awaitTerminal(jobs, pinned);
        }
    }
}
