// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import dev.nexus.service.db.GroupRepository;
import dev.nexus.service.db.IdentityRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TupleRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.BiConsumer;
import java.util.function.Supplier;

/**
 * Builds the owner stamper and its deadline timer and registers them (RDR-252 Step 3), kept out of
 * {@code NexusService}: {@link #hook()} goes to {@code TupleHandler} when the tuple routes are built, and
 * {@link #start} runs once the engine's scheduler exists. The timer runs on that scheduler, as the engine's other
 * sweeps do. {@link #start} schedules the rebuild of the timer after a restart: each topic with a live owner row is
 * re-evaluated, which re-arms the deadline of an open red that no counting ack, newer row or {@code escalated} row
 * has closed (a deadline already past fires at once), and posts anything a crash left owed. A census that fails in
 * part is retried, with a growing and capped delay, and only for the (tenant, topic) pairs that failed, until one
 * completes.
 */
public final class BoardOwnerWiring {

    private static final Logger log = LoggerFactory.getLogger(BoardOwnerWiring.class);
    /** The seconds after start before the rebuild runs, so it never competes with the engine's boot. */
    static final long REBUILD_DELAY_SECONDS = 5;
    /** The retry delay doubles after each census that did not complete, up to this many seconds. */
    static final long REBUILD_MAX_DELAY_SECONDS = 300;
    /** After this many attempts without a full census, and every this many after, an error-level event says so. */
    static final int REBUILD_STUCK_ATTEMPTS = 12;
    private static final String PREFIX = "board/ci/";
    private static final String OWNER = "--owner";

    /** A census item that failed: a topic's re-arm, or (topic null) a tenant whose subspaces could not be listed. */
    private record Pending(String tenant, String topic) { }

    private final TupleRepository tuples;
    private final OwnerStamper stamper;
    private final BiConsumer<String, String> rearm;
    private final Clock clock;
    private AckDeadlineTimer timer;

    public BoardOwnerWiring(TenantScope scope, TupleRepository tuples, IdentityRepository identities, Clock clock) {
        this.tuples = tuples;
        this.clock = clock;
        this.stamper = new OwnerStamper(scope, tuples, new GroupRepository(scope, clock), identities, clock);
        this.rearm = stamper::rearm;
    }

    /** A test's own stamper and its own re-arm step (one topic's failure), over the same census. */
    BoardOwnerWiring(TupleRepository tuples, OwnerStamper stamper, BiConsumer<String, String> rearm, Clock clock) {
        this.tuples = tuples;
        this.clock = clock;
        this.stamper = stamper;
        this.rearm = rearm;
    }

    /** The hook {@code TupleHandler} calls after a tuple write. */
    public OwnerOutHook hook() {
        return stamper;
    }

    public OwnerStamper stamper() {
        return stamper;
    }

    /**
     * Binds the timer to {@code scheduler} and schedules the rebuild over {@code tenants}. A census that did not
     * complete is run again after twice the delay, up to {@link #REBUILD_MAX_DELAY_SECONDS}, and again at that delay
     * until one completes, so a restart that meets a database that is not ready still loses no deadline. The retry
     * covers only what failed: the tenant list again if it could not be read, else just the tenants and topics that
     * failed. The first failure logs its stack and the later ones its message; the {@value #REBUILD_STUCK_ATTEMPTS}th
     * attempt and every {@value #REBUILD_STUCK_ATTEMPTS} after it raise the error-level event
     * {@code board_ack_deadline_rebuild_stuck}.
     */
    public void start(ScheduledExecutorService scheduler, Supplier<List<String>> tenants) {
        this.timer = new AckDeadlineTimer(scheduler, clock, stamper::onDeadline);
        stamper.bind(timer);
        scheduleRebuild(scheduler, tenants, null, REBUILD_DELAY_SECONDS, 1);
    }

    /** {@code pending} null: the tenant list has not been read yet (or could not be). */
    private void scheduleRebuild(ScheduledExecutorService scheduler, Supplier<List<String>> tenants, List<Pending> pending,
                                 long delay, int attempt) {
        scheduler.schedule(() -> {
            List<Pending> failed;
            try {
                failed = pending == null ? census(tenants.get(), attempt) : retry(pending, attempt);
            } catch (Throwable t) {
                logFailure("event=board_ack_deadline_rebuild_failed attempt={} error={}", attempt, t, attempt);
                failed = null;
            }
            if (failed != null && failed.isEmpty()) {
                return;
            }
            if (attempt % REBUILD_STUCK_ATTEMPTS == 0) {
                log.error("event=board_ack_deadline_rebuild_stuck attempt={} pending={}", attempt,
                    failed == null ? "tenant list" : failed.size());
            }
            long next = Math.min(delay * 2, REBUILD_MAX_DELAY_SECONDS);
            log.warn("event=board_ack_deadline_rebuild_retry attempt={} next_in_seconds={} pending={}", attempt, next,
                failed == null ? "tenant list" : failed.size());
            try {
                scheduleRebuild(scheduler, tenants, failed, next, attempt + 1);
            } catch (Throwable t) {
                // The scheduler refuses once the engine is stopping: nothing is left to rebuild for.
                log.warn("event=board_ack_deadline_rebuild_not_rescheduled attempt={} error={}", attempt, t.getMessage());
            }
        }, delay, TimeUnit.SECONDS);
    }

    /** The first attempt logs the stack of a failure; a retry logs its message only. */
    private static void logFailure(String message, int attempt, Throwable t, Object... args) {
        Object[] all = java.util.Arrays.copyOf(args, args.length + (attempt == 1 ? 2 : 1));
        all[args.length] = t.getMessage();
        if (attempt == 1) {
            all[args.length + 1] = t;   // slf4j takes a trailing throwable as the stack
        }
        log.warn(message, all);
    }

    public AckDeadlineTimer timer() {
        return timer;
    }

    /**
     * Re-evaluates every topic that has a live owner row, for each tenant; a failure does not stop the rest. Returns
     * true only for a full census: every tenant's subspaces were listed and every topic re-armed.
     */
    public boolean rebuild(List<String> tenants) {
        return census(tenants, 1).isEmpty();
    }

    /** One census over {@code tenants}: the pairs that failed (empty for a full census). */
    private List<Pending> census(List<String> tenants, int attempt) {
        List<Pending> failed = new ArrayList<>();
        int topics = 0;
        for (String tenant : tenants) {
            try {
                for (TupleRepository.SubspaceCensus c : tuples.subspaceList(tenant, PREFIX)) {
                    String s = c.subspace();
                    if (c.total() > 0 && s.endsWith(OWNER)) {
                        topics++;
                        String base = s.substring(PREFIX.length(), s.length() - OWNER.length());
                        if (!rearmOne(tenant, base, attempt)) {
                            failed.add(new Pending(tenant, base));
                        }
                    }
                }
            } catch (Throwable t) {
                failed.add(new Pending(tenant, null));
                logFailure("event=board_ack_deadline_rebuild_tenant_failed attempt={} tenant={} error={}", attempt, t, attempt, tenant);
            }
        }
        log.info("event=board_ack_deadline_rebuilt topics={} failures={}", topics, failed.size());
        return failed;
    }

    /** A retry: only the topics whose re-arm failed, and the tenants whose listing failed. */
    private List<Pending> retry(List<Pending> pending, int attempt) {
        List<Pending> failed = new ArrayList<>();
        for (Pending p : pending) {
            if (p.topic() == null) {
                failed.addAll(census(List.of(p.tenant()), attempt));
            } else if (!rearmOne(p.tenant(), p.topic(), attempt)) {
                failed.add(p);
            }
        }
        return failed;
    }

    private boolean rearmOne(String tenant, String base, int attempt) {
        try {
            rearm.accept(tenant, base);
            return true;
        } catch (Throwable t) {
            logFailure("event=board_ack_deadline_rearm_failed attempt={} tenant={} base={} error={}", attempt, t, attempt, tenant, base);
            return false;
        }
    }
}
