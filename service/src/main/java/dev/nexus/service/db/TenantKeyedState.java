// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.function.ToIntFunction;

/**
 * One JVM structure keyed by tenant, and the call that clears a tenant's entries from it (bead nexus-0bl77, rtg20
 * phase A). The test-only {@code POST /v1/_test/tenant-reset} route runs every registered instance for the tenant
 * whose rows a test harness has just deleted, so the next test on the same tenant name starts from the state a fresh
 * tenant would have. {@code TenantKeyedStateGateTest} fails the build when a collection-typed or tenant-named field
 * in {@code src/main} is not accounted for.
 */
public interface TenantKeyedState {

    /** Stable name, reported in the route's response and listed in the gate test's table. */
    String name();

    /**
     * Removes {@code tenant}'s entries.
     *
     * @return how many entries were removed (the unit is the structure's own; zero when it held nothing)
     */
    int evictTenant(String tenant);

    static TenantKeyedState of(String name, ToIntFunction<String> evictor) {
        return new TenantKeyedState() {
            @Override
            public String name() {
                return name;
            }

            @Override
            public int evictTenant(String tenant) {
                return evictor.applyAsInt(tenant);
            }
        };
    }
}
