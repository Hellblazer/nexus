// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;

import java.time.OffsetDateTime;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.TreeSet;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, row 14): the registry read of {@code purgeTrash}. {@code nexus.purge_trash} is one SQL call that hard-deletes
 * the aged tombstones (the fk-001 cascade takes their manifest rows) and sweeps the chunks those manifest rows were the only holders of,
 * across every collection the tenant has: a write of collection-scoped rows in collections the request never names. Like a manifest purge,
 * it reads the registry rows of those collections {@code FOR KEY SHARE} first, in {@code collection_id} order, in ANY state (a retired
 * collection's tombstones are reclaimed too), so it takes its place in the canonical order with the verbs and never deletes ahead of one that
 * holds a row.
 *
 * <p>The set is the collections of the aged tombstones: their own {@code physical_collection}, and the collections of their manifest rows
 * (a chunk is swept only where such a manifest row is the sole holder, so every collection the sweep reaches is among them). The threshold is
 * the function's own server-side expression, and {@code now()} is the transaction's start, so the read and the routine see one population.
 */
final class TrashPurgeRegistryReads {

    private TrashPurgeRegistryReads() {}

    /** Reads, {@code FOR KEY SHARE} in id order, the rows of every collection the purge of tombstones aged past {@code threshold} reaches. */
    static void lockCollectionsOfAgedTombstones(DSLContext ctx, String tenant, Field<OffsetDateTime> threshold) {
        Set<Long> ids = new TreeSet<>();
        Set<String> labels = new LinkedHashSet<>();
        // TOMBSTONE-EXEMPT in purpose: the aged tombstones are the population being purged
        for (var r : ctx.selectDistinct(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                .from(CATALOG_DOCUMENTS)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())
                    .and(CATALOG_DOCUMENTS.DELETED_AT.le(threshold)))
                .fetch()) {
            add(ids, labels, r.value1(), r.value2());
        }
        for (var r : ctx.selectDistinct(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                .from(CATALOG_DOCUMENT_CHUNKS)
                .join(CATALOG_DOCUMENTS)
                    .on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                        .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID)))
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())
                    .and(CATALOG_DOCUMENTS.DELETED_AT.le(threshold)))
                .fetch()) {
            add(ids, labels, r.value1(), r.value2());
        }
        if (!ids.isEmpty() || !labels.isEmpty()) {
            CollectionRegistry.lockForCleanup(ctx, tenant, ids, labels);
        }
    }

    /** A stamped row by its id; a row the P3.1 stamp has not reached, by its label. */
    private static void add(Set<Long> ids, Set<String> labels, Long id, String label) {
        if (id != null) {
            ids.add(id);
        } else if (label != null && !label.isEmpty()) {
            labels.add(label);
        }
    }
}
