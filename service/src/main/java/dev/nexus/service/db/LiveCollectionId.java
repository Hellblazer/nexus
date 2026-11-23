// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.OptionalLong;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * The id of the live row a label names (RDR-229 P3.5a, bead nexus-p2m1x.23): what the route boundary reads to name the successor
 * of a retired row in its 409 {@code collection_superseded}. "Live" is {@link CollectionRegistry#notSuperseded()}; a label that
 * several live rows hold (a state P4.1 forbids) answers the newest. Reads the table, not the registry cache, and fills no cache:
 * it answers one refusal and keeps no state.
 */
public final class LiveCollectionId {

    private LiveCollectionId() {
    }

    /** The id of the live row of {@code label} in {@code tenant}, or empty when no live row has that label. */
    public static OptionalLong of(TenantScope scope, String tenant, String label) {
        Long id = scope.withTenant(tenant, ctx -> ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(label))
            .and(CollectionRegistry.notSuperseded())
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID.desc())
            .limit(1)
            .fetchOne(CATALOG_COLLECTIONS.COLLECTION_ID));
        return id == null ? OptionalLong.empty() : OptionalLong.of(id);
    }
}
