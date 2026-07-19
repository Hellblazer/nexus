// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.resolver;

/**
 * Functional interface for single-row chunk text lookup used by
 * {@link ChromaSchemeHandler}.
 *
 * <p>Decouples {@link ChromaSchemeHandler} from the concrete
 * {@link dev.nexus.service.vectors.PgVectorRepository} so unit tests can pass
 * a lambda/stub without subclassing the (final) repository.  In production,
 * wire as:
 *
 * <pre>  new ChromaSchemeHandler(pgVectorRepository::fetchChunkText)</pre>
 *
 * <p>RDR-169 G3: Phase A (bead nexus-064jj).
 */
@FunctionalInterface
public interface ChunkTextFetcher {

    /**
     * Fetch the stored {@code chunk_text} for a single
     * {@code (tenant, collection, chash)} triple.
     *
     * @param tenant     requesting tenant (stamps RLS GUC via TenantScope)
     * @param collection four-segment conformant collection name
     * @param chash      chunk natural ID (the full sha256 hexdigest, RDR-180)
     * @return stored chunk text, or {@code null} if the row is absent or
     *         {@code chunk_text} is NULL (reference-only retention, RDR-169 G1)
     */
    String fetch(String tenant, String collection, String chash);
}
