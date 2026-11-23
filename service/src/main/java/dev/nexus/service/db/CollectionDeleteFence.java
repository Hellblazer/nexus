// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.DSLContext;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * The registry reads of a collection delete (RDR-229 P3.5a row 14, nexus-p2m1x.23). {@code CatalogRepository#deleteCollectionTxn} stays the
 * delete; what it must read FOR KEY SHARE before it writes a child row lives here, so the delete grows by calls and not by logic.
 *
 * <p><b>The quarantine siblings.</b> The delete removes the origin's rows from its quarantine siblings ({@code QuarantineOrigin.deleteRowsOf}),
 * and unregisters a sibling it empties. Those are writes under collections whose registry rows the transaction never read: a verb that
 * holds a sibling's row {@code FOR UPDATE} (a restore, a rename) and a writer of the sibling that holds it {@code FOR KEY SHARE} could both
 * be mid-flight against rows the delete is about to remove. {@link #lockOriginAndSiblings} reads the origin's row and every sibling's row
 * {@code FOR KEY SHARE} in one statement in {@code collection_id} order (the canonical order, so a verb that locks the origin and a sibling
 * {@code FOR UPDATE} in id order meets the delete in the same order, and neither waits on the other while holding what it wants), before the
 * first child-row statement of the delete.
 */
final class CollectionDeleteFence {

    /** Passes of read-lock-reread: a sibling that appears while the delete waits for a lock is locked on the next pass. */
    private static final int PASSES = 3;

    private CollectionDeleteFence() {}

    /**
     * The delete's call: after the tuple locks and before its first child-row statement, for a delete that will remove quarantine rows
     * (not a delete of a quarantine collection itself, and not one that keeps the quarantine rows).
     *
     * @param scope    the delete's resolved scope; {@code onlyId} the id the route named, or null
     */
    static void lockForDelete(DSLContext ctx, String tenant, String name, CollectionPurgeScope scope, boolean keepQuarantine, Long onlyId,
                              Gate3 gate3) {
        if (keepQuarantine || scope.matchesNothing() || QuarantineOrigin.isQuarantine(ctx, tenant, name)) {
            return;
        }
        lockOriginAndSiblings(ctx, tenant, name, scope.scopedById() ? scope.id() : onlyId, gate3);
    }

    /**
     * Locks, {@code FOR KEY SHARE} in id order, the registry row of the collection being deleted and the rows of the quarantine collections
     * that hold rows of it (the siblings {@code deleteRowsOf} will reach). The siblings are found by name and locked by the id they bear
     * now, so a sibling that a verb renames while the delete waits is still the row locked; the sibling set is read again after the lock
     * and any that is new is locked on the next pass, up to {@link #PASSES} (a sibling that keeps appearing past that is left to the
     * delete's own row delete, which waits on it as before).
     *
     * @param originId the id of the row the delete was scoped to, or {@code null} for a delete by label (the origin's rows by that label are locked)
     */
    static void lockOriginAndSiblings(DSLContext ctx, String tenant, String origin, Long originId, Gate3 gate3) {
        Set<String> handled = new HashSet<>();
        boolean first = true;
        for (int pass = 0; pass < PASSES; pass++) {
            List<String> siblings = QuarantineOrigin.siblingsHolding(ctx, tenant, origin, gate3);
            List<String> todo = new ArrayList<>();
            for (String sibling : siblings) {
                if (!handled.contains(sibling)) todo.add(sibling);
            }
            if (!first && todo.isEmpty()) {
                return;
            }
            List<Long> ids = new ArrayList<>(todo.isEmpty() ? List.of() : ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(todo))
                .fetch(CATALOG_COLLECTIONS.COLLECTION_ID));
            if (first && originId != null) {
                ids.add(originId);
            }
            RegistryRowLocks.keyShareIfPresent(ctx, tenant, ids, first && originId == null ? List.of(origin) : List.of());
            handled.addAll(todo);
            first = false;
            if (todo.isEmpty()) {
                return;
            }
        }
    }
}
