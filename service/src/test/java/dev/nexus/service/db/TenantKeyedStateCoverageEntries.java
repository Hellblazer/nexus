// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/**
 * nexus-7fku2.1.2 (RDR-134 Step 2): the {@link TenantKeyedStateGateTest} entries for the topic-coverage cache and the
 * route helper that drops it, in a file of their own because the gate test sits at its size ceiling. The evictor name
 * is the one {@code NexusService.tenantKeyedState()} registers.
 */
final class TenantKeyedStateCoverageEntries {
    private TenantKeyedStateCoverageEntries() { }

    private static final String EVICTOR = "TaxonomyCoverage";

    static void fill(Map<String, String> keyed, Map<String, String> notKeyed) {
        keyed.put("TaxonomyCoverage.slots", EVICTOR);   // the cached counts per (tenant, collection)
        keyed.put("TaxonomyCoverage.Key.tenant", EVICTOR);   // the key of slots
        notKeyed.put("TaxonomyCoverage.tenantScope", "a TenantScope handle: it holds the DataSource wrapper only, the "
            + "tenant is a per-call argument");
        notKeyed.put("TaxonomyCoverage.afterCountForTests", "a test-only hook run between a fill's count and its store; "
            + "null in production");
        notKeyed.put("CoverageInvalidation.ops", "the route paths a handler drops coverage for: a constant set, no state");
        notKeyed.put("CoverageInvalidation.named", "the collection names of the requests in flight, by exchange "
            + "identity; each entry is removed when its handler returns (finish), so none outlives a request");
        notKeyed.put("CoverageInvalidation.dropped", "the exchanges of in-flight requests whose coverage drop has run; "
            + "removed when the handler returns (finish), so none outlives a request");
    }
}
