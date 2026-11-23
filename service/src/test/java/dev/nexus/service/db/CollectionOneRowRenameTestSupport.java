// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-229 P3.7 (nexus-p2m1x.27, folio's r3 confirmation review S4): the one way a one-row rename test moves the registry row lock bound it shares with every
 * retiring verb ({@link CollectionTombstones#lockRows}). The bound is a process-wide setting, so a test restores what it replaced:
 * {@code try (var bound = CollectionOneRowRenameTestSupport.rowLockTimeoutMs("60000")) { ... }}.
 */
public final class CollectionOneRowRenameTestSupport {

    /** Restores the bound that was in force when {@link #rowLockTimeoutMs} was called. */
    public interface Restore extends AutoCloseable {
        @Override
        void close();
    }

    /** What this class last set, as the GUC text; null while the production bound is in force. Tests of one class run one at a time. */
    private static String current;

    private CollectionOneRowRenameTestSupport() { /* static */ }

    /** Sets the bound to {@code ms} (the GUC text, milliseconds); {@code null} means the production bound. */
    public static synchronized Restore rowLockTimeoutMs(String ms) {
        String previous = current;
        CollectionTombstones.setRowLockTimeoutMsForTests(ms);
        current = ms;
        return () -> {
            synchronized (CollectionOneRowRenameTestSupport.class) {
                CollectionTombstones.setRowLockTimeoutMsForTests(previous);
                current = previous;
            }
        };
    }
}
