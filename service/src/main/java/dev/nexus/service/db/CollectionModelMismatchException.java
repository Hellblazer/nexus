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
        this(sourceCollection, sourceModel, targetCollection, targetModel,
            "refusing to re-home chunks of collection '" + sourceCollection + "' (embedding model '"
            + sourceModel + "') into collection '" + targetCollection + "' (embedding model '"
            + targetModel + "'): a chunk keeps its collection's model, and moving between models is a "
            + "cross-model migration into a new collection, not a rename");
    }

    private CollectionModelMismatchException(String sourceCollection, String sourceModel,
                                             String targetCollection, String targetModel,
                                             String message) {
        super(message);
        this.sourceCollection = sourceCollection;
        this.sourceModel = sourceModel;
        this.targetCollection = targetCollection;
        this.targetModel = targetModel;
    }

    /**
     * A rename whose new name carries a model token that differs from the source row's
     * {@code embedding_model} (nexus-ucagi). A rename keeps the source row's model, so the vectors stay in
     * that model's space whatever the new name says; the engine does not parse names (RDR-204), so
     * {@code nameModel} is the token the client derived and sent as {@code embedding_model}. Carries the same
     * wire fields as the re-home refusal: {@code target_collection} is the new name and {@code target_model}
     * the token it claims.
     */
    public static CollectionModelMismatchException forRename(String oldName, String sourceModel,
                                                             String newName, String nameModel) {
        return new CollectionModelMismatchException(oldName, sourceModel, newName, nameModel,
            "refusing to rename collection '" + oldName + "' (embedding model '" + sourceModel
            + "') to '" + newName + "', whose name carries the model token '" + nameModel
            + "': a rename keeps the source row's embedding model and never re-embeds, so the new name would "
            + "claim a model the vectors are not in. Rename to a name carrying '" + sourceModel
            + "', or migrate across models into a new collection");
    }

    public String sourceCollection() { return sourceCollection; }
    public String sourceModel() { return sourceModel; }
    public String targetCollection() { return targetCollection; }
    public String targetModel() { return targetModel; }
}
