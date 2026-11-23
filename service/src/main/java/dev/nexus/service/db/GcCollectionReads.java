// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23, linda's Q2 ruling): the in-order prologue of the four {@code /v1/vectors/gc} writes, which move,
 * restore and expire chunks through SQL functions that take both collections by LABEL. The order of every writer is the canonical one:
 * the sweep gate in JAVA first (the exclusive gate the function takes as its own first statement; advisory locks are re-entrant within
 * a transaction, so the function's take is a no-op), then the registry rows by id {@code FOR KEY SHARE}, then the function, called with
 * the rows' CURRENT labels. A request by {@code collection_id} therefore answers its typed 404 (no row) or 409 (retired) before the
 * function runs, the id cannot be freed and its label taken again under the move, and the function never sees the label of a row that
 * was renamed since the route resolved it (it would file or move the rows of whichever collection now holds that label).
 *
 * <p>The gate is keyed by label, so the gate of a label the row has NOW is known only after the row is read. When the row's current label
 * is not the one whose gate this attempt took, the attempt rolls back and runs again from the top with that label's gate first
 * ({@link RenameFollow}); the second gate is never taken after a registry read.
 *
 * <p>Which gate: {@code gc_quarantine_orphans*} gate the ORIGIN; {@code gc_expire_quarantine_bounded} gates the SIBLING;
 * {@code quarantine_restore_chunks} gates the origin; {@code gc_restore_rereferenced*} and {@code gc_expire_quarantine} take none ({@link Gate#NONE}).
 *
 * <p>The origin is a registered collection on every route (the dimension is derived from its row before the write), so it is always
 * read. The quarantine sibling is read only when the request named it by id: a sibling named by label may not be registered yet
 * ({@code gc_quarantine_orphans} registers it when there is something to move), and a label that no row bears is not a refusal there.
 */
public final class GcCollectionReads {

    private GcCollectionReads() {
    }

    private static volatile Runnable beforeMoveForTests;

    /** Test-only: runs in each gc write's transaction just before its SQL function; clear with null. Never set by production code. */
    public static void setBeforeMoveForTests(Runnable hook) {
        beforeMoveForTests = hook;
    }

    /** The seam above, called by each gc write in its transaction before the function; a no-op in production. */
    public static void beforeMove() {
        Runnable hook = beforeMoveForTests;
        if (hook != null) {
            hook.run();
        }
    }

    /** Which collection's sweep gate the function takes as its first statement, if any. */
    public enum Gate { NONE, ORIGIN, SIBLING }

    /** The labels to call the function with: the locked rows' current labels (a sibling named by label is as given). */
    public record Labels(String origin, String sibling) {
    }

    /** Runs {@code attempt} (one transaction, taking the gate of the label it is given) and again when the row turned out to be renamed. */
    public static <T> T attempt(Gate gate, String origin, String sibling, java.util.function.Function<String, T> attempt) {
        return RenameFollow.run(gate == Gate.SIBLING && sibling != null ? sibling : origin, attempt);
    }

    /**
     * The prologue, called first in each gc write's transaction (after any bounds it sets and the test seam {@link #beforeMove}):
     * the exclusive sweep gate of {@code gateLabel} when {@code gate} is not NONE, then the origin's row, and the sibling's when the
     * request named it by id, {@code FOR KEY SHARE} in id order.
     *
     * @throws RenameFollow.Moved when the row whose gate was taken is now named another label ({@link #attempt} runs again)
     */
    public static Labels enter(DSLContext ctx, String tenant, Gate gate, String gateLabel, String origin, String sibling) {
        if (gate != Gate.NONE) {
            CatalogRepository.acquireSweepGateExclusiveHeldByGc(ctx, tenant, gateLabel);
        }
        boolean siblingById = sibling != null && ExpectedCollectionIds.of(sibling).isPresent();
        CollectionRegistry.requireAllForWrite(ctx, tenant, siblingById ? java.util.List.of(origin, sibling) : java.util.List.of(origin));
        CollectionRow originRow = CollectionRegistry.requireForWrite(ctx, tenant, origin);
        CollectionRow siblingRow = siblingById ? CollectionRegistry.requireForWrite(ctx, tenant, sibling) : null;
        if (gate == Gate.ORIGIN) {
            RenameFollow.check(gateLabel, originRow);
        } else if (gate == Gate.SIBLING && siblingRow != null) {
            RenameFollow.check(gateLabel, siblingRow);
        }
        return new Labels(originRow.label(), siblingRow != null ? siblingRow.label() : sibling);
    }

    /** {@link #enter} with no gate and no sibling: a transaction that runs no gc function (the keyed restore's ledger-only transaction). */
    public static Labels enter(DSLContext ctx, String tenant, String origin) {
        return enter(ctx, tenant, Gate.NONE, origin, origin, null);
    }
}
