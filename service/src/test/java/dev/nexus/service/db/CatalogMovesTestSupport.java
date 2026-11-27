// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the way a test outside this package sets {@code CatalogMoves}'s after-writes hook, which runs inside the move's
 * transaction after its last write. The hook is process-wide; the returned handle clears it.
 */
public final class CatalogMovesTestSupport {

    private CatalogMovesTestSupport() {
    }

    /** Sets the hook; {@code close()} clears it. */
    public static AutoCloseable afterWrites(Runnable hook) {
        CatalogMoves.afterWritesHookForTests = hook;
        return () -> CatalogMoves.afterWritesHookForTests = null;
    }
}
