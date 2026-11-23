// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * The registry answered a read of one {@code collection_id} with a row of another id (RDR-229 P3.5a, bead nexus-p2m1x.23): the
 * registry itself is inconsistent, an engine defect and never a caller mistake. The HTTP layer answers it 500
 * {@code collection_id_mismatch}. It is raised so that the request fails instead of going on with a row it did not name.
 * No request reaches it today: a read by id selects on the key.
 */
public final class CollectionIdMismatchException extends RuntimeException {

    private final long requested;
    private final long resolved;

    public CollectionIdMismatchException(long requested, long resolved) {
        super("collection_id " + requested + " resolved to a registry row of collection_id " + resolved);
        this.requested = requested;
        this.resolved = resolved;
    }

    /** The row of the id carries another label than the one the request was resolved to. */
    public CollectionIdMismatchException(long collectionId, String expectedLabel, String rowLabel) {
        super("collection_id " + collectionId + " names '" + rowLabel + "', not '" + expectedLabel + "'");
        this.requested = collectionId;
        this.resolved = collectionId;
    }

    public long requested() { return requested; }
    public long resolved() { return resolved; }
}
