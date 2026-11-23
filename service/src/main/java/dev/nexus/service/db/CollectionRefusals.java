// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;

/**
 * The refusals a retiring verb owes a request that named its source by {@code collection_id} (RDR-229 P3.5a, row 16): the verb
 * (supersede, rename or delete) reads and locks the source row of the id it was asked about, in its own transaction, and refuses
 * there. The route boundary ({@code CollectionSelectorFilter}) resolved that id to a label in an earlier, shorter read; the label
 * can have been freed and taken by another row since, and a verb that went on by the label would retire or remove the other
 * collection.
 */
public final class CollectionRefusals {

    private CollectionRefusals() {
    }

    /**
     * The registry row of {@code id}, read {@code SELECT ... FOR UPDATE} by {@code collection_id} in {@code ctx}'s transaction. A plain
     * lock, so it is safe to call on a row this transaction already holds. Supersede and rename call it SECOND: their own ordered,
     * bounded lock ({@link CollectionTombstones#lockRows(DSLContext, String, long, String...)}) took the row first, and the second
     * {@code FOR UPDATE} of a row one holds does not wait. A supersede names its successor by id the same way (the ordered statement
     * took the successor's row too). The delete calls it FIRST and alone: it holds no registry row yet, takes this one before any
     * child row, and, being the delete's only registry lock, waits unbounded (see the comment on the statement). The delete takes no
     * sweep gate before it, and supersede takes none at all.
     *
     * <p>The id the request named counts as consumed ({@link ExpectedCollectionIds}) once the row is read, whatever refusal follows, so
     * a handler that stops passing the id turns the route's census red instead of silent.
     *
     * @param expectedLabel the label the route boundary resolved {@code id} to, which the live row must still bear
     * @return the live row, locked
     * @throws UnknownCollectionIdException    the tenant has no row of that id (404 {@code unknown_collection_id})
     * @throws CollectionRetiredIdException    the row is retired (409 {@code collection_superseded}, naming its successor)
     * @throws CollectionIdMismatchException   the live row bears another label than {@code expectedLabel} (409
     *                                         {@code collection_id_mismatch}, naming the id and both labels)
     */
    public static CollectionRow requireSourceById(DSLContext ctx, String tenant, long id, String expectedLabel) {
        WriteRegistryMemo.exempt(ctx, "a retiring verb that locks its source row FOR UPDATE by id");
        ExpectedCollectionIds.beforeTransactionRead();   // test seam, once per request, before the first by-id read; a no-op in production
        var modelDimension = EMBEDDING_MODELS.DIMENSION.as("model_dimension");
        Record r = ctx.select(
                CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.COLLECTION_ID,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE, modelDimension,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .leftJoin(EMBEDDING_MODELS).on(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(CATALOG_COLLECTIONS.EMBEDDING_MODEL))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))
            // the registry row alone: PostgreSQL refuses a row lock on the nullable side of the join. Unbounded here (linda and tamsin,
            // 2026-11-23): rename and supersede call this after their own bounded ordered lock has taken the row, and delete waits as
            // the final DELETE it replaces always did. A bound on delete would be a new wire answer, the label verbs' 503 rename_busy
            // with a ledger line, not row 14's collection_busy (that 409 means a re-embed job holds the collection).
            .forUpdate().of(CATALOG_COLLECTIONS)
            .fetchOne();
        if (r == null) {
            throw new UnknownCollectionIdException(tenant, id);
        }
        CollectionRow row = CollectionRegistry.rowOf(r, modelDimension, tenant);
        // the request's named id is read, in this transaction, whatever refusal follows (ExpectedCollectionIds, the census)
        ExpectedCollectionIds.consumed(ctx, expectedLabel);
        String label = row.label();
        if (row.isSuperseded()) {
            throw new CollectionRetiredIdException(tenant, id, label, CollectionSuccessor.of(ctx, tenant, id));
        }
        if (!label.equals(expectedLabel)) {
            throw new CollectionIdMismatchException(id, expectedLabel, label);
        }
        return row;
    }
}
