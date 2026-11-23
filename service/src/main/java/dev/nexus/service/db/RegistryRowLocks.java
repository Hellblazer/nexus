// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * The tolerant registry read of a maintenance or ledger write (RDR-229 P3.5a row 14, nexus-p2m1x.23): the rows of the collections
 * a write names BY ID, {@code FOR KEY SHARE} in {@code collection_id} order, with no refusal. {@link CollectionRegistry#requireIdsAndLabelsForWrite}
 * refuses a row that is gone (404) or retired (409), which is right for a client write and wrong where the id is only a record: a
 * job's stored end, a chunk's stored collection id. Here a row that is gone is simply not returned, and the caller decides (skip
 * the write, refuse its own way); a retired row is returned and carries {@link CollectionRow#isSuperseded}.
 *
 * <p>Each row read is memoised in the transaction's {@link WriteRegistryMemo} under a key no label or id lookup uses
 * ({@code ~held#<id>}), so the census sees a registry row held, and {@code lockForWrite} / {@code lockIdForWrite} never answer a
 * request from a row this class read (they would hand back a retired one).
 */
final class RegistryRowLocks {

    private RegistryRowLocks() {}

    /**
     * @param ids    registry ids to lock; null entries are ignored
     * @param labels labels whose rows (live or retired) are locked as well, for a record that never carried an id
     * @return the rows found, in {@code collection_id} order
     */
    static List<CollectionRow> keyShareIfPresent(DSLContext ctx, String tenant, Collection<Long> ids, Collection<String> labels) {
        List<Long> idList = new ArrayList<>();
        for (Long id : ids) {
            if (id != null && !idList.contains(id)) idList.add(id);
        }
        List<String> labelList = new ArrayList<>();
        for (String label : labels) {
            if (label != null && !label.isEmpty() && !labelList.contains(label)) labelList.add(label);
        }
        if (idList.isEmpty() && labelList.isEmpty()) {
            return List.of();
        }
        Condition match = DSL.falseCondition();
        if (!idList.isEmpty()) match = match.or(CATALOG_COLLECTIONS.COLLECTION_ID.in(idList));
        if (!labelList.isEmpty()) match = match.or(CATALOG_COLLECTIONS.NAME.in(labelList));
        List<CollectionRow> rows = ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(match))
            .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID)
            .forKeyShare().of(CATALOG_COLLECTIONS)
            .fetch(r -> new CollectionRow(r.get(CATALOG_COLLECTIONS.CONTENT_TYPE), r.get(CATALOG_COLLECTIONS.OWNER_ID),
                r.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL),
                // the dimension is not read by anything that holds one of these rows; a NULL is 0, never a guess
                r.get(CATALOG_COLLECTIONS.DIMENSION) == null ? 0 : r.get(CATALOG_COLLECTIONS.DIMENSION),
                r.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE), r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY),
                r.get(CATALOG_COLLECTIONS.COLLECTION_ID), r.get(CATALOG_COLLECTIONS.NAME)));
        for (CollectionRow row : rows) {
            WriteRegistryMemo.put(ctx, tenant, memoKey(row.collectionId()), row);
        }
        return rows;
    }

    /** Drops the memo entries of {@code rows}: the savepoint that took their locks rolled back. */
    static void forget(DSLContext ctx, String tenant, Collection<CollectionRow> rows) {
        for (CollectionRow row : rows) {
            WriteRegistryMemo.remove(ctx, tenant, memoKey(row.collectionId()));
        }
    }

    private static String memoKey(long collectionId) {
        return "~held#" + collectionId;
    }
}
