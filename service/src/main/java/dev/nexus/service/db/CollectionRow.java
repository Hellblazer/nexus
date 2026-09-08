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
 * database column is nullable (a dormant or disputed-model row has no single
 * agreed dimension — {@code hygiene-002-collection-attributes-walk.xml}), and
 * a {@code NULL} value is normalized to {@code 0} at the point this record is
 * constructed ({@link CollectionRegistry#require}). {@code 0} is not a valid
 * embedding dimension, so it is unambiguous as "unknown/disputed" to any
 * caller that inspects the field.
 *
 * @param contentType    {@code catalog_collections.content_type}
 * @param ownerId        {@code catalog_collections.owner_id}
 * @param embeddingModel {@code catalog_collections.embedding_model}
 * @param dimension      {@code catalog_collections.dimension}, or {@code 0} when the column is {@code NULL}
 * @param lifecycleState {@code catalog_collections.lifecycle_state}
 */
public record CollectionRow(
        String contentType,
        String ownerId,
        String embeddingModel,
        int dimension,
        String lifecycleState) {
}
