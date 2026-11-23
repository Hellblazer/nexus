// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A request named a collection by {@code collection_id}; the route boundary resolved the id to a label, and by the time the write's
 * transaction read the row of that id (RDR-229 P3.5a, bead nexus-p2m1x.23) the row carried ANOTHER label: a one-row rename (P3.7)
 * keeps the id and changes the label, and landed in the gap. An ordinary race, never an engine defect: the id is still the live
 * collection, so the client may read it again and retry. The HTTP layer answers 409 {@code collection_renamed} naming the id, the
 * label the request resolved and the row's current label, and nothing was written. (The registry answering an id with a row of
 * another id is {@link CollectionIdMismatchException}, a 500.)
 */
public final class CollectionRenamedUnderRequestException extends RuntimeException {

    private final String tenant;
    private final long collectionId;
    private final String requestedLabel;
    private final String currentLabel;

    public CollectionRenamedUnderRequestException(String tenant, long collectionId, String requestedLabel, String currentLabel) {
        super("collection_id " + collectionId + " is now named '" + currentLabel + "', not '" + requestedLabel + "'");
        this.tenant = tenant;
        this.collectionId = collectionId;
        this.requestedLabel = requestedLabel;
        this.currentLabel = currentLabel;
    }

    public String tenant() { return tenant; }
    public long collectionId() { return collectionId; }
    public String requestedLabel() { return requestedLabel; }
    public String currentLabel() { return currentLabel; }
}
