// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the registry check for the writes that work by label and never needed the registry row (the aspect
 * delete and salient-sentences write, the pipeline delete and the remap leg clear). It reads the collection's row in the write's own
 * transaction {@code FOR KEY SHARE}, as the first collection-keyed statement, through {@link CollectionRegistry#requireForWriteIfPresent}:
 * a request by {@code collection_id} that names no row is 404 and a retired row 409, so the id cannot be freed and its label taken
 * again under the write; a request by label locks the row it resolves to, so a verb that retires it waits for the write (linda's
 * option (b): the same read for a label as for an id).
 *
 * <p>A label no row has removes nothing and answers 200, as it always did: only these writes never refused an unregistered label,
 * and that stays.
 */
final class RequestedCollectionRead {

    private RequestedCollectionRead() {
    }

    /**
     * Reads the row of {@code collection} FOR KEY SHARE in {@code ctx}'s transaction; a retired id is refused, a label no row has is not.
     * Returns the row (empty for a blank collection or a label no row has) so the write can match the collection's rows by its id
     * ({@link CollectionOwnedRows}) and stamp its current label.
     */
    static java.util.Optional<CollectionRow> require(DSLContext ctx, String tenant, String collection) {
        if (collection != null && !collection.isEmpty()) {
            return CollectionRegistry.requireForWriteIfPresent(ctx, tenant, collection);
        }
        return java.util.Optional.empty();
    }
}
