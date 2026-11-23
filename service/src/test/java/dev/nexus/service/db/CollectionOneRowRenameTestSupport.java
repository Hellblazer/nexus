// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27, folio's r3 confirmation review S4): the one way a test moves the one-row rename's registry row lock bound
 * ({@code CollectionOneRowRename}'s seam is package-private, not a public mutable static in the production class). The bound is a process-wide
 * setting, so a test restores what it replaced: {@code try (var bound = CollectionOneRowRenameTestSupport.rowLockTimeoutMs("60000")) { ... }}.
 */
public final class CollectionOneRowRenameTestSupport {

    /** Restores the bound that was in force when {@link #rowLockTimeoutMs} was called. */
    public interface Restore extends AutoCloseable {
        @Override
        void close();
    }

    private CollectionOneRowRenameTestSupport() { /* static */ }

    /** Sets the bound to {@code ms} (the GUC text, milliseconds); {@code null} means the production bound. */
    public static Restore rowLockTimeoutMs(String ms) {
        String previous = CollectionOneRowRename.setRowLockTimeoutMsForTests(ms);
        return () -> CollectionOneRowRename.setRowLockTimeoutMsForTests(previous);
    }
}
