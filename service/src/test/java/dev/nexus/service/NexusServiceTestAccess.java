// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.tuples.TemplateRegistry;

import javax.sql.DataSource;
import java.io.IOException;
import java.util.concurrent.ScheduledExecutorService;

/**
 * Lets a test in another package build a {@link NexusService} on a scheduler of its own: the constructor with the
 * sweep scheduler seam is package-private, and a test that must see what {@code NexusService} registers on the
 * scheduler (the owner stamper's timer, RDR-252 Step 3) cannot be in {@code dev.nexus.service}'s package and
 * {@code dev.nexus.service.board}'s fixtures at once.
 */
public final class NexusServiceTestAccess {

    private NexusServiceTestAccess() {
    }

    public static NexusService withSweepScheduler(int port, String token, DataSource ds, TemplateRegistry registry,
                                                  ScheduledExecutorService sweepScheduler) throws IOException {
        return new NexusService(port, token, ds, null, null, null, null, registry, sweepScheduler);
    }

    /** The evictors the tenant reset route runs, as the service built them. */
    public static java.util.List<dev.nexus.service.db.TenantKeyedState> tenantKeyedState(NexusService service) {
        return service.tenantKeyedState();
    }
}
