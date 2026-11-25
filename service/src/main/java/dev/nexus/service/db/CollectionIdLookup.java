// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

import java.util.Collection;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17): the tolerant batch form of {@link CollectionIds}, for the history tables
 * ({@code relevance_log}, {@code search_telemetry}, {@code hook_failures}, {@code gc_audit}). Their {@code
 * collection_id} is a bare nullable column with no CHECK, so a name the registry does not hold is not a reason to
 * refuse the write: it gets a NULL id. {@link CollectionIds#require} refuses an unregistered name, which is right for
 * a writer the CHECK guards and wrong for an event log.
 *
 * <p>One registry read for the batch's distinct names, in the writer's own transaction, never cached across
 * transactions. A multi-row INSERT carries a Long per row from this map rather than a subselect per row, which would
 * add two bind parameters to each row against the batch's bind-count budget.
 */
final class CollectionIdLookup {

    private CollectionIdLookup() {}

    /**
     * The registry id of each name that has one; a name that is {@code ''}, {@code null} or unregistered is absent
     * from the map, so {@code get} gives {@code null} for it.
     */
    static Map<String, Long> idsOf(DSLContext ctx, String tenant, Collection<String> names) {
        Objects.requireNonNull(tenant, "tenant");
        var distinct = names.stream().filter(n -> n != null && !n.isEmpty()).distinct().toList();
        var out = new HashMap<String, Long>();
        if (distinct.isEmpty()) {
            return out;
        }
        ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(distinct))
            // a label two rows hold (a tombstone beside a live row, G15): the live row, else the newest tombstone, first
            .orderBy(java.util.stream.Stream.concat(java.util.stream.Stream.of((org.jooq.OrderField<?>) CATALOG_COLLECTIONS.NAME.asc()),
                CollectionIdReads.liveFirst().stream()).toList())
            .fetch()
            .forEach(r -> out.putIfAbsent(r.value1(), r.value2()));
        return out;
    }
}
