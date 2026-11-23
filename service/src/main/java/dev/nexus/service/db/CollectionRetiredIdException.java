// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Optional;

/**
 * A request named a collection by {@code collection_id} and the id is the id of a retired row (a rename's or supersede's
 * tombstone), found inside the handler's transaction (RDR-229 P3.5a, bead nexus-p2m1x.23; linda's D3). The HTTP layer answers it
 * 409 {@code collection_superseded}, the same body the route boundary sends for a retired id it finds first
 * ({@code CollectionSupersededBody}). The successor is found by {@code superseded_by_id} ({@link CollectionSuccessor}); a row
 * that names none carries none.
 */
public final class CollectionRetiredIdException extends RuntimeException {

    private final String tenant;
    private final long collectionId;
    private final String label;
    private final transient Optional<CollectionSuccessor.Successor> successor;

    public CollectionRetiredIdException(String tenant, long collectionId, String label,
                                        Optional<CollectionSuccessor.Successor> successor) {
        super("collection_id " + collectionId + " ('" + label + "') is retired, so a request that names it is refused");
        this.tenant = tenant;
        this.collectionId = collectionId;
        this.label = label;
        this.successor = successor;
    }

    public String tenant() { return tenant; }
    public long collectionId() { return collectionId; }
    public String label() { return label; }
    public Optional<CollectionSuccessor.Successor> successor() { return successor; }
}
