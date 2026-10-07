// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A re-home would file chunk rows of one embedding model under a collection registered with another
 * (RDR-225, nexus-3wh8d.13).
 *
 * <p>A chunk carries its collection's model. {@code chunks} is LIST-partitioned by model, and its
 * composite foreign key to {@code catalog_collections} is on {@code (tenant_id, collection,
 * embedding_model)}, so an UPDATE of {@code collection} to a collection of another model cannot
 * succeed: PostgreSQL refuses it with SQLSTATE 23503 and a constraint name that is a per-partition
 * clone. A cross-model move is a re-embed into a new collection (cross-model migration), never an
 * in-place re-filing, so the engine refuses it up front naming both models and both collections.
 *
 * <p>Raised inside the repository's transaction before the offending UPDATE, so nothing is written;
 * {@link dev.nexus.service.http.HttpUtil#sendTypedDbError} maps it to a typed 409.
 */
public final class CollectionModelMismatchException extends RuntimeException {

    private final String sourceCollection;
    private final String sourceModel;
    private final String targetCollection;
    private final String targetModel;

    public CollectionModelMismatchException(String sourceCollection, String sourceModel,
                                            String targetCollection, String targetModel) {
        super("refusing to re-home chunks of collection '" + sourceCollection + "' (embedding model '"
            + sourceModel + "') into collection '" + targetCollection + "' (embedding model '"
            + targetModel + "'): a chunk keeps its collection's model, and moving between models is a "
            + "cross-model migration into a new collection, not a rename");
        this.sourceCollection = sourceCollection;
        this.sourceModel = sourceModel;
        this.targetCollection = targetCollection;
        this.targetModel = targetModel;
    }

    public String sourceCollection() { return sourceCollection; }
    public String sourceModel() { return sourceModel; }
    public String targetCollection() { return targetCollection; }
    public String targetModel() { return targetModel; }
}
