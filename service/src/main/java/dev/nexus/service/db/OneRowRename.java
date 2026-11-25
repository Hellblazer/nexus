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
 * wiring, not {@code CatalogRepository}. The wiring is {@link CollectionOneRowRename} ({@code NexusService}).
 */
@FunctionalInterface
public interface OneRowRename {

    /**
     * What the catalog rename hands the one-row body beyond the two labels (RDR-229 P3.7): what the client derived for the new label, the caller's target checks
     * (run again under the registry row locks), and where the row's tuple is reported for the caller's race recheck ({@link CollectionMint#recheckOf}).
     *
     * @param sourceId        the {@code collection_id} the request named the source by (resolved to {@code oldLabel} by the route boundary), or null for a
     *                        request by label: the body locks that row with the others and refuses, before it writes, an id that is unknown or retired or that
     *                        now bears another label ({@link CollectionRefusals#requireSourceById}), so a label freed and registered again in between is never
     *                        renamed in the id's place
     * @param newContentType  the client's content type for the new label, or null to keep the row's
     * @param newOwnerId      the client's owner, or null to keep the row's
     * @param tupleOut        receives the row's tuple after the rename (five slots)
     * @param targetUnderLock the caller's target checks (live target, emptiness, identity), run again by the body once it holds the registry row locks;
     *                        it refuses as the shared checks do and answers whether a retired row of the new label exists now (folio's r3 critique F1)
     */
    record Request(Long sourceId, String newContentType, String newOwnerId, String[] tupleOut, TargetCheck targetUnderLock) {
        /** A request by label, no client attributes, no retired target, nowhere to report a tuple: for a test of the seam. */
        public static Request bare() {
            return new Request(null, null, null, new String[5], ctx -> false);
        }
    }

    /** The target checks a one-row body runs under its registry row locks: throws a refusal, or answers whether a retired row of the new label exists. */
    @FunctionalInterface
    interface TargetCheck {
        boolean underLock(DSLContext ctx);
    }

    /**
     * Renames {@code oldLabel} to {@code newLabel} by moving the registry row only; the content keeps its name columns. The answer carries the id of
     * the renamed row (which is the same one as before), the catalog epoch read in this transaction's tenant scope, and {@code idsStable} true.
     */
    RenameOutcome rename(DSLContext ctx, String tenant, String oldLabel, String newLabel, Request request);

    /** The default: refuses, writing nothing. */
    static OneRowRename failClosed() {
        return (ctx, tenant, oldLabel, newLabel, request) -> {
            throw new RenamePathMismatchException(oldLabel, newLabel);
        };
    }
}
