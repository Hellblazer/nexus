// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the registry reads of the four {@code /v1/vectors/gc} writes, which move, restore and expire chunks
 * through SQL functions that take both collections by LABEL. Each write calls this in its own transaction, so a request that
 * named a collection by {@code collection_id} reads that id there ({@code FOR KEY SHARE}, through {@link CollectionIds#require}): an id
 * with no row is 404, a retired one 409, and the id cannot be freed and its label taken again by another row under the move.
 *
 * <p><b>The read follows the function.</b> Every one of those functions takes the collection's exclusive sweep gate
 * ({@code pg_advisory_xact_lock('sweepgate:...')}) as its first statement, and the lock order is the sweep gate, then the registry row
 * ({@code RenameGuard.runRename}): a read before the call would hold the row {@code FOR KEY SHARE} while the function waits for a gate
 * that a delete holds shared while it waits for that row. The read after the call is still in the write's own transaction, so an id
 * with no row, or a retired one, throws and the transaction rolls back with everything the function did: a label another row took
 * lands nothing. A move that took a chunk of the row of the id has already held that row key-share through the foreign key.
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

    /** Reads {@code origin}, and {@code sibling} when the request named it by id; {@code sibling} may be null (the restore finds its own). */
    public static void read(DSLContext ctx, String tenant, String origin, String sibling) {
        CollectionIds.require(ctx, tenant, origin);
        if (sibling != null && ExpectedCollectionIds.of(sibling).isPresent()) {
            CollectionIds.require(ctx, tenant, sibling);
        }
    }

    /**
     * Reads {@code origin} only when the request named it by id: for a function that refused the origin and so left no read to answer a
     * freed id. A request by label reads nothing here, so the function's own refusal stands.
     */
    public static void readIfNamedById(DSLContext ctx, String tenant, String origin) {
        if (ExpectedCollectionIds.of(origin).isPresent()) {
            CollectionIds.require(ctx, tenant, origin);
        }
    }

    /** {@link #read} of the origin alone. */
    public static void read(DSLContext ctx, String tenant, String origin) {
        read(ctx, tenant, origin, null);
    }
}
