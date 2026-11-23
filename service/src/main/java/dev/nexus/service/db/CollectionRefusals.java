// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

import org.jooq.DSLContext;

/**
 * PROVISIONAL stub of row 16's interface (nexus-p2m1x.27), dropped when row 16 stacks: the one method P3.7's one-row rename calls. Row 16's own
 * {@code requireSourceById} replaces this body with the same signature and contract.
 */
public final class CollectionRefusals {

    private CollectionRefusals() {
    }

    /**
     * Reads the registry row of {@code id} {@code SELECT ... FOR UPDATE} by {@code collection_id} in the caller's transaction, and refuses a request
     * that named the collection by an id whose row is not what the request meant.
     *
     * @param expectedLabel the label the route boundary resolved the id to (the one the caller will go on to rename)
     * @return the live row, locked
     * @throws UnknownCollectionIdException    the tenant has no row of that id (404 {@code unknown_collection_id})
     * @throws CollectionRetiredIdException    the row is retired (409 {@code collection_superseded}, naming the successor)
     * @throws CollectionIdMismatchException   the row now bears another label than {@code expectedLabel} (409 {@code collection_id_mismatch})
     */
    public static CollectionRow requireSourceById(DSLContext ctx, String tenant, long id, String expectedLabel) {
        var r = ctx.select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE,
                CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id)))
            .forUpdate().fetchOne();
        if (r == null) {
            throw new UnknownCollectionIdException(tenant, id);
        }
        Integer dimension = r.get(CATALOG_COLLECTIONS.DIMENSION);
        CollectionRow row = new CollectionRow(r.get(CATALOG_COLLECTIONS.CONTENT_TYPE), r.get(CATALOG_COLLECTIONS.OWNER_ID),
            r.get(CATALOG_COLLECTIONS.EMBEDDING_MODEL), dimension == null ? 0 : dimension, r.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE),
            r.get(CATALOG_COLLECTIONS.SUPERSEDED_BY), id, r.get(CATALOG_COLLECTIONS.NAME));
        if (row.isSuperseded()) {
            throw new CollectionRetiredIdException(tenant, id, row.label(), CollectionSuccessor.of(ctx, tenant, id));
        }
        if (!row.label().equals(expectedLabel)) {
            throw new CollectionIdMismatchException(id, expectedLabel, row.label());
        }
        ExpectedCollectionIds.consumed(ctx, expectedLabel);   // the request's id was read in this transaction, locked
        return row;
    }
}
