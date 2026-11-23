// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * A snapshot of one {@code nexus.catalog_collections} row's registration
 * attributes (RDR-204 Phase 2, bead nexus-ft04v.14).
 *
 * <p>{@link CollectionRegistry} used to cache only PRESENCE — a boolean fact
 * that a {@code (tenant, collection)} pair has a row, nothing about what the
 * row says. This record is what the cache holds instead: the five columns a
 * parse site (dimension routing, model-homogeneity checks, embedder
 * selection) actually needs, read once and reused for the life of the
 * process rather than re-queried on every write.
 *
 * <p>{@code dimension} is a primitive {@code int}, not {@code Integer}: the
 * database column {@code catalog_collections.dimension} is nullable (a
 * walked zero-chunk row, or a dormant/disputed row has no single agreed
 * dimension — {@code hygiene-002-collection-attributes-walk.xml}), but a
 * {@code NULL} column is never handed to a caller as a fabricated {@code 0}.
 * {@link CollectionRegistry#require} instead COALESCEs a {@code NULL}
 * {@code catalog_collections.dimension} with the row's own {@code
 * embedding_model}'s dimension in {@code nexus.embedding_models} (NOT NULL,
 * FK-backed since {@code hygiene-002-1}) — a real, known-correct dimension
 * for the model the row is registered under, not a guess. If somehow
 * neither resolves (unreachable given the FK, but never silently trusted),
 * {@code require} throws {@link IllegalStateException} rather than caching
 * a sentinel a caller could misread as a real dimension (RDR-204 Phase 2
 * follow-up, bead nexus-ft04v.16 — the {@code 0}-sentinel this javadoc used
 * to describe was landed by nexus-ft04v.14 with no consumer reading it yet,
 * and is retired here before one could).
 *
 * @param contentType    {@code catalog_collections.content_type}
 * @param ownerId        {@code catalog_collections.owner_id}
 * @param embeddingModel {@code catalog_collections.embedding_model}
 * @param dimension      the row's resolved dimension — {@code
 *                        catalog_collections.dimension} when non-NULL, else
 *                        {@code embedding_models.dimension} for {@code
 *                        embeddingModel}
 * @param lifecycleState {@code catalog_collections.lifecycle_state}
 * @param supersededBy   {@code catalog_collections.superseded_by}, normalised: {@code ""} when the
 *                        row is not superseded (the column's own not-superseded value, as NULL is).
 *                        A superseded row is the product of a rename or supersede, and a write to
 *                        it is refused (nexus-mp8ys, {@link CollectionSupersededException}); such a
 *                        row is never cached, see {@link CollectionRegistry#markKnown}
 * @param collectionId   {@code catalog_collections.collection_id} (RDR-229 P3.5a, nexus-p2m1x.23), the registry's own id;
 *                        {@code 0} for a row built without one (the short constructors below, which tests and seeding
 *                        callers use: the identity starts at 1, so 0 names no row)
 * @param label          {@code catalog_collections.name}, the label the row is registered under; {@code ""} for a row
 *                        built without one
 */
public record CollectionRow(
        String contentType,
        String ownerId,
        String embeddingModel,
        int dimension,
        String lifecycleState,
        String supersededBy,
        long collectionId,
        String label) {

    public CollectionRow {
        supersededBy = supersededBy == null ? "" : supersededBy;
        label = label == null ? "" : label;
    }

    /** A row without an id or a label: the shape every caller before RDR-229 P3.5a built. */
    public CollectionRow(String contentType, String ownerId, String embeddingModel, int dimension,
                         String lifecycleState, String supersededBy) {
        this(contentType, ownerId, embeddingModel, dimension, lifecycleState, supersededBy, 0L, "");
    }

    /** A row that is not superseded; the shape every caller before nexus-mp8ys built. */
    public CollectionRow(String contentType, String ownerId, String embeddingModel, int dimension,
                         String lifecycleState) {
        this(contentType, ownerId, embeddingModel, dimension, lifecycleState, "");
    }

    /** True when the row carries its registry id and label, as every row the registry reads does. */
    public boolean hasIdentity() {
        return collectionId > 0 && !label.isEmpty();
    }

    /**
     * The label a write STORES for this collection (RDR-229 P3.5c, I8): the row's CURRENT label, read under the write's own lock,
     * never the label the request carried from the route boundary. {@code requested} when the row carries no label of its own (a row
     * built without identity).
     */
    public String storedLabel(String requested) {
        return hasIdentity() ? label : requested;
    }

    /** True when the row names a successor: it was renamed away or superseded. */
    public boolean isSuperseded() {
        return !supersededBy.isEmpty();
    }
}
