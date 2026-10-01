// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import java.util.Objects;

/**
 * What a chunk-write route asks of {@link PgVectorRepository}: check that every chash in the
 * request has a live manifest row in the collection, and either refuse ({@link
 * OwnerlessWriteMode#ENFORCE}) or log and count ({@link OwnerlessWriteMode#LOG_ONLY}).
 *
 * <p>The check is requested by the HANDLER, through this guard, and is not part of the
 * repository's write methods: a repository method called without a guard (the contract and
 * fixture tests, the migration ingest) writes ownerless chunks as it always did.
 *
 * @param mode  enforce or log-only
 * @param route the route name for the error and the log, e.g. {@code upsert-chunks}
 */
public record OwnershipGuard(OwnerlessWriteMode mode, String route) {

    public OwnershipGuard {
        Objects.requireNonNull(mode, "mode");
        Objects.requireNonNull(route, "route");
    }
}
