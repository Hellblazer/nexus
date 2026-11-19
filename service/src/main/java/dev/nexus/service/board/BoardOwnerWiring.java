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
 * has closed (a deadline already past fires at once), and posts anything a crash left owed.
 */
public final class BoardOwnerWiring {

    private static final Logger log = LoggerFactory.getLogger(BoardOwnerWiring.class);
    /** The seconds after start before the rebuild runs, so it never competes with the engine's boot. */
    static final long REBUILD_DELAY_SECONDS = 5;
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

    /** Binds the timer to {@code scheduler} and schedules the rebuild over {@code tenants}. */
    public void start(ScheduledExecutorService scheduler, Supplier<List<String>> tenants) {
        this.timer = new AckDeadlineTimer(scheduler, clock, stamper::onDeadline);
        stamper.bind(timer);
        scheduler.schedule(() -> {
            try {
                rebuild(tenants.get());
            } catch (Throwable t) {
                log.warn("event=board_ack_deadline_rebuild_failed error={}", t.getMessage(), t);
            }
        }, REBUILD_DELAY_SECONDS, TimeUnit.SECONDS);
    }

    public AckDeadlineTimer timer() {
        return timer;
    }

    /** Re-evaluates every topic that has a live owner row, for each tenant; a topic's failure does not stop the rest. */
    public void rebuild(List<String> tenants) {
        int topics = 0;
        for (String tenant : tenants) {
            try {
                for (TupleRepository.SubspaceCensus c : tuples.subspaceList(tenant, PREFIX)) {
                    String s = c.subspace();
                    if (c.total() > 0 && s.endsWith(OWNER)) {
                        topics++;
                        try {
                            stamper.rearm(tenant, s.substring(PREFIX.length(), s.length() - OWNER.length()));
                        } catch (Throwable t) {
                            log.warn("event=board_ack_deadline_rearm_failed tenant={} subspace={} error={}", tenant, s, t.getMessage(), t);
                        }
                    }
                }
            } catch (Throwable t) {
                log.warn("event=board_ack_deadline_rebuild_tenant_failed tenant={} error={}", tenant, t.getMessage(), t);
            }
        }
        log.info("event=board_ack_deadline_rebuilt topics={}", topics);
    }
}
