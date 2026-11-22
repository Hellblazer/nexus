// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/**
 * RDR-229 (nexus-p2m1x.17 P3.1, nexus-p2m1x.31 P3.11): a backup's {@code collection_id} to the id the restore gave that
 * registry row, source id to target id. Unused in P3.1, which passes {@link #empty()}: a restore never trusts a file's ids and
 * refills every id by name ({@link RestoreIdFill}). It is the seam for a P3.11 that maps ids instead.
 */
record CollectionIdMap(Map<Long, Long> sourceToTarget) {

    CollectionIdMap {
        sourceToTarget = Map.copyOf(sourceToTarget);
    }

    static CollectionIdMap empty() {
        return new CollectionIdMap(Map.of());
    }

    boolean isEmpty() {
        return sourceToTarget.isEmpty();
    }
}
