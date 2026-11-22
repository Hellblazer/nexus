// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A route or the registry was given a {@code collection_id} that no row of the tenant's {@code nexus.catalog_collections}
 * has (RDR-229 P3.5a, bead nexus-p2m1x.23). The id-input twin of {@link UnregisteredCollectionException}, which a LABEL with no
 * row still raises (linda's ruling 2, 2026-11-22): the two are unrelated types so a caller cannot take one for the other, and
 * the HTTP layer answers this one with a typed 404 {@code unknown_collection_id} naming the id, the other with the typed 422.
 *
 * <p>The message carries the id as a plain decimal, with no digit grouping.
 */
public final class UnknownCollectionIdException extends RuntimeException {

    private final String tenant;
    private final long collectionId;

    public UnknownCollectionIdException(String tenant, long collectionId) {
        super("collection_id " + collectionId + " is not a collection of tenant '" + tenant + "'");
        this.tenant = tenant;
        this.collectionId = collectionId;
    }

    public String tenant() {
        return tenant;
    }

    public long collectionId() {
        return collectionId;
    }
}
