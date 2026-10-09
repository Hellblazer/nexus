// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

/**
 * The set of per-collection HNSW indexes the engine knows to be valid (RDR-227). A single-collection plain
 * search consults it to choose {@code hnsw.ef_search}: a collection with a valid index walks that index at the
 * serving value, any other walks the shared leaf index at the widest value pgvector allows.
 *
 * <p>Step 1 ships only {@link #NONE}, which knows no index, so every single-collection statement that the
 * router sends to HNSW walks the leaf at the widest value. Step 2's read half supplies the real set.
 *
 * <p>Called once per single-collection statement, before the statement's transaction, so an implementation
 * must answer from memory and never query the database.
 */
public interface PciIndexSet {

    /** The empty set: no collection has a valid per-collection index. */
    PciIndexSet NONE = (model, tenant, collection) -> false;

    /**
     * @param model      the embedding model of the leaf the collection lives in
     * @param tenant     the tenant of that leaf
     * @param collection the collection name
     * @return whether a valid per-collection index exists for exactly this (model, tenant, collection)
     */
    boolean hasValidIndex(String model, String tenant, String collection);
}
