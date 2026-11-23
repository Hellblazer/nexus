// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

/**
 * The seam for P3.7's one-row catalog rename (RDR-229 P3.5a, linda's R-A ruling): what {@link RenameGuard#runRename} runs for the
 * catalog rename when the latch row exists and the rename readiness is complete. It runs in the rename's own transaction ({@code ctx}),
 * after the rename lock and the latch read, and answers the committed outcome the route reports.
 *
 * <p>{@link #failClosed()} is the default and the only implementation on this base: the one-row regime has no catalog rename here,
 * so a rename that reaches it is refused ({@link RenamePathMismatchException}, 409 {@code rename_path_mismatch}) with nothing written.
 * P3.7 supplies its own at wiring time ({@code NexusService}, where the one {@link RenameGuard} is built), so adopting it changes the
 * wiring, not {@code CatalogRepository}.
 */
@FunctionalInterface
public interface OneRowRename {

    /** Renames {@code oldLabel} to {@code newLabel} by moving the registry row only; the content keeps its name columns. */
    RenameOutcome rename(DSLContext ctx, String tenant, String oldLabel, String newLabel);

    /** The default: refuses, writing nothing. */
    static OneRowRename failClosed() {
        return (ctx, tenant, oldLabel, newLabel) -> {
            throw new RenamePathMismatchException(oldLabel, newLabel);
        };
    }
}
