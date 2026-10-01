// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.List;

/**
 * RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): {@code upsert-chunks} or {@code store-put} was asked
 * to write a chash that has no live manifest row in the collection, and the engine is in
 * {@link OwnerlessWriteMode#ENFORCE}. The whole request is refused before anything is embedded
 * or written. {@code VectorHandler} maps it to 422.
 */
public final class OwnerlessChunkWriteException extends RuntimeException {

    private final String route;
    private final String collection;
    private final int unownedCount;
    private final int requestedCount;
    private final List<String> unownedSample;

    public OwnerlessChunkWriteException(String route, String collection, int unownedCount,
                                        int requestedCount, List<String> unownedSample) {
        super("refusing an ownerless chunk write on " + route + ": " + unownedCount + " of "
            + requestedCount + " chashes have no live manifest row in collection '" + collection
            + "' (e.g. " + (unownedSample.isEmpty() ? "?" : unownedSample.get(0)) + "). Write a document's "
            + "chunks and its manifest rows in one request through POST /v1/catalog/manifest/write_many "
            + "(first batch) and POST /v1/catalog/manifest/append (later batches); this route only "
            + "rewrites chunks that a live document already owns");
        this.route = route;
        this.collection = collection;
        this.unownedCount = unownedCount;
        this.requestedCount = requestedCount;
        this.unownedSample = List.copyOf(unownedSample);
    }

    public String route() {
        return route;
    }

    public String collection() {
        return collection;
    }

    public int unownedCount() {
        return unownedCount;
    }

    public int requestedCount() {
        return requestedCount;
    }

    /** At most a handful of the offending chashes, for the error body and the log. */
    public List<String> unownedSample() {
        return unownedSample;
    }
}
