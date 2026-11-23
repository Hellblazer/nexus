// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * The registry answered a read of one {@code collection_id} with a row of another id (RDR-229 P3.5a, bead nexus-p2m1x.23): the
 * registry itself is inconsistent, an engine defect and never a caller mistake. The HTTP layer answers it 500
 * {@code collection_id_mismatch}. (The other form, an id and a label the request itself sent that no longer agree, is the client's
 * statement contradicting itself: 409, same reason, with the labels.) It is raised so that the request fails instead of going on with a row it did not name.
 * No request reaches it today: a read by id selects on the key.
 */
public final class CollectionIdMismatchException extends RuntimeException {

    private final long requested;
    private final long resolved;
    private final String requestedLabel;
    private final String currentLabel;

    public CollectionIdMismatchException(long requested, long resolved) {
        super("collection_id " + requested + " resolved to a registry row of collection_id " + resolved);
        this.requested = requested;
        this.resolved = resolved;
        this.requestedLabel = null;
        this.currentLabel = null;
    }

    /**
     * A request carried BOTH a {@code collection_id} and a label of its own, and the row of that id no longer bears the label (read
     * in the write's own transaction, {@code FOR KEY SHARE}): the two named different collections by the time of the write. The
     * client's own statement disagrees with itself, so the HTTP layer answers 409 {@code collection_id_mismatch} naming the id, the
     * label sent and the row's current label, and nothing is written. (A request by id ONLY follows a rename: the id is the
     * identity.)
     */
    public CollectionIdMismatchException(long collectionId, String requestedLabel, String currentLabel) {
        super("collection_id " + collectionId + " names '" + currentLabel + "', not '" + requestedLabel + "'");
        this.requested = collectionId;
        this.resolved = collectionId;
        this.requestedLabel = requestedLabel;
        this.currentLabel = currentLabel;
    }

    /** True for the 409 case: the request's own id and label disagree. False for a registry inconsistency (500). */
    public boolean labelDisagreement() { return requestedLabel != null; }
    public String requestedLabel() { return requestedLabel; }
    public String currentLabel() { return currentLabel; }

    public long requested() { return requested; }
    public long resolved() { return resolved; }
}
