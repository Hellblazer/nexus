// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/**
 * What a catalog collection rename committed (RDR-229 P3.5a, bead nexus-p2m1x.23): the per-table counts the route has always
 * answered, plus the renamed row the client rebinds from (residual S-7, marlowe's acceptance line).
 *
 * @param counts       the per-table re-home counts
 * @param collectionId the id of the row now holding the new label; {@code 0} when the rename retired nothing (no source row)
 * @param catalogEpoch baton's {@link CatalogEpoch}, read inside the rename's tenant scope (it is empty outside it); {@code null}
 *                     when the engine role cannot read the cluster, and then the wire carries no {@code catalog_epoch} key
 * @param idsStable    whether the id survives a rename on this install ({@link RenameGuard#idsStable()})
 */
public record RenameOutcome(Map<String, Integer> counts, long collectionId, String catalogEpoch, boolean idsStable) {
}
