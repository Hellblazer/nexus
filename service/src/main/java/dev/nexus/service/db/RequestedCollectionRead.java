// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the by-id read for the writes that work by label and never needed the registry row (the aspect
 * delete and salient-sentences write, the pipeline delete and the remap leg clear). When the
 * request named {@code collection} by {@code collection_id} ({@link ExpectedCollectionIds}), this reads that id in the write's own
 * transaction {@code FOR KEY SHARE}, as the first collection-keyed statement: no row is 404, a retired row 409, so the id cannot
 * be freed and its label taken again under the write. A request by label is untouched: no read, the write's own behaviour
 * (an unregistered label removes nothing and answers 200, as it always did).
 */
final class RequestedCollectionRead {

    private RequestedCollectionRead() {
    }

    /** Reads the id the request named for {@code collection}, when it named one; the row of a retired collection is refused. */
    static void require(DSLContext ctx, String tenant, String collection) {
        if (ExpectedCollectionIds.of(collection).isPresent()) {
            CollectionRegistry.require(ctx, tenant, collection);
        }
    }
}
