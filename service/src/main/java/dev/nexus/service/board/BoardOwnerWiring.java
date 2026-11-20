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
import java.util.List;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

/**
 * Builds the owner stamper and its deadline timer and registers them (RDR-252 Step 3), kept out of
 * {@code NexusService}: {@link #hook()} goes to {@code TupleHandler} when the tuple routes are built, and
 * {@link #start} runs once the engine's scheduler exists. The timer runs on that scheduler, as the engine's other
 * sweeps do. {@link #start} schedules the rebuild of the timer after a restart: each topic with a live owner row is
 * re-evaluated, which re-arms the deadline of an open red that no counting ack, newer row or {@code escalated} row
 * has closed (a deadline already past fires at once), and posts anything a crash left owed. A census that fails in
 * part is retried, with a growing and capped delay, until one completes.
 */
public final class BoardOwnerWiring {

    private static final Logger log = LoggerFactory.getLogger(BoardOwnerWiring.class);
    /** The seconds after start before the rebuild runs, so it never competes with the engine's boot. */
    static final long REBUILD_DELAY_SECONDS = 5;
    /** The retry delay doubles after each census that did not complete, up to this many seconds. */
    static final long REBUILD_MAX_DELAY_SECONDS = 300;
    private static final String PREFIX = "board/ci/";
    private static final String OWNER = "--owner";

    private final TupleRepository tuples;
    private final OwnerStamper stamper;
    private final Clock clock;
    private AckDeadlineTimer timer;

    public BoardOwnerWiring(TenantScope scope, TupleRepository tuples, IdentityRepository identities, Clock clock) {
        this.tuples = tuples;
        this.clock = clock;
        this.stamper = new OwnerStamper(scope, tuples, new GroupRepository(scope, clock), identities, clock);
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
     * complete (the tenant list could not be read, a tenant's subspaces could not be listed, a topic could not be
     * re-armed) is run again after twice the delay, up to {@link #REBUILD_MAX_DELAY_SECONDS}, and again at that
     * delay until one census completes: a restart that meets a database that is not ready still loses no deadline.
     */
    public void start(ScheduledExecutorService scheduler, Supplier<List<String>> tenants) {
        this.timer = new AckDeadlineTimer(scheduler, clock, stamper::onDeadline);
        stamper.bind(timer);
        scheduleRebuild(scheduler, tenants, REBUILD_DELAY_SECONDS, 1);
    }

    private void scheduleRebuild(ScheduledExecutorService scheduler, Supplier<List<String>> tenants, long delay, int attempt) {
        scheduler.schedule(() -> {
            boolean full = false;
            try {
                full = rebuild(tenants.get());
            } catch (Throwable t) {
                log.warn("event=board_ack_deadline_rebuild_failed attempt={} error={}", attempt, t.getMessage(), t);
            }
            if (!full) {
                long next = Math.min(delay * 2, REBUILD_MAX_DELAY_SECONDS);
                log.warn("event=board_ack_deadline_rebuild_retry attempt={} next_in_seconds={}", attempt, next);
                try {
                    scheduleRebuild(scheduler, tenants, next, attempt + 1);
                } catch (Throwable t) {
                    // The scheduler refuses once the engine is stopping: nothing is left to rebuild for.
                    log.warn("event=board_ack_deadline_rebuild_not_rescheduled attempt={} error={}", attempt, t.getMessage());
                }
            }
        }, delay, TimeUnit.SECONDS);
    }

    public AckDeadlineTimer timer() {
        return timer;
    }

    /**
     * Re-evaluates every topic that has a live owner row, for each tenant; a failure does not stop the rest. Returns
     * true only for a full census: every tenant's subspaces were listed and every topic re-armed.
     */
    public boolean rebuild(List<String> tenants) {
        int topics = 0;
        int failures = 0;
        for (String tenant : tenants) {
            try {
                for (TupleRepository.SubspaceCensus c : tuples.subspaceList(tenant, PREFIX)) {
                    String s = c.subspace();
                    if (c.total() > 0 && s.endsWith(OWNER)) {
                        topics++;
                        try {
                            stamper.rearm(tenant, s.substring(PREFIX.length(), s.length() - OWNER.length()));
                        } catch (Throwable t) {
                            failures++;
                            log.warn("event=board_ack_deadline_rearm_failed tenant={} subspace={} error={}", tenant, s, t.getMessage(), t);
                        }
                    }
                }
            } catch (Throwable t) {
                failures++;
                log.warn("event=board_ack_deadline_rebuild_tenant_failed tenant={} error={}", tenant, t.getMessage(), t);
            }
        }
        log.info("event=board_ack_deadline_rebuilt topics={} failures={}", topics, failures);
        return failures == 0;
    }
}
