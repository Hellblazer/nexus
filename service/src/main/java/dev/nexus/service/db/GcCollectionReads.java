// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.impl.DSL;

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

    /**
     * The registry wait of {@link #enter} ended by a bound (a verb held the row past the statement bound, or a lock timeout): carries the
     * database error, whose SQLSTATE the restore routes read ({@code asRestoreBusy}); every other gc route answers it as
     * {@link RenameBusyException} ({@link #attempt}).
     */
    public static final class RegistryWaitTimedOut extends RuntimeException {
        RegistryWaitTimedOut(Throwable cause) {
            super("the collection's registry row is held by a retiring verb: " + cause.getMessage(), cause);
        }
    }

    /**
     * The statement bound of a gc route's registry read, 25 s (the bound a restore already carries). It is a gc route's, not a writer's: an
     * operator's gc can be retried, so a verb that holds the row past 25 s ends the wait as a typed 503 {@code rename_busy}; an ingest writer
     * has no such bound (it waits for the verb, and the wait is logged as {@code registry_wait_slow} past one second), because a writer should
     * not fail behind a rare rename.
     */
    private static volatile int registryReadBoundMs = 25_000;

    /** Test-only: the statement bound of a registry read on a route that sets none, in ms; clear with 25000. Never set by production code. */
    public static void setRegistryReadBoundForTests(int ms) {
        registryReadBoundMs = ms;
    }

    /**
     * Runs {@code attempt} (one transaction, taking the gate of the label it is given) and again when the row turned out to be renamed. A
     * registry row held past the read's bound answers {@link RenameBusyException} (503 {@code rename_busy}, retryable): the typed busy of the
     * quarantine, expire and restore-rereferenced routes, keyed or not.
     */
    public static <T> T attempt(Gate gate, String origin, String sibling, java.util.function.Function<String, T> attempt) {
        try {
            return attemptRestore(gate, origin, sibling, attempt);
        } catch (RegistryWaitTimedOut busy) {
            throw RenameBusyException.registryRowHeld(busy);
        }
    }

    /**
     * {@link #attempt} for the quarantine RESTORE routes: a registry wait ended by the route's statement bound propagates with its SQLSTATE,
     * which {@code PgVectorRepository#asRestoreBusy} answers as the typed 503 {@code quarantine_restore_busy} those routes always had.
     */
    public static <T> T attemptRestore(Gate gate, String origin, String sibling, java.util.function.Function<String, T> attempt) {
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
        // The lock bound the route set (or none), put back after the registry reads: the Java take of the gate carries the function's own
        // 2 s bound, but the registry rows are read under the writers' rule, no timeout of their own (a verb's FOR UPDATE ends the wait when
        // it commits; the route's statement bound still applies), so a registry row held by a verb is not answered as a gate that timed out
        String priorLockTimeout = ctx.fetchValue(DSL.function("current_setting", String.class, DSL.val("lock_timeout")));
        if (gate != Gate.NONE) {
            CatalogRepository.acquireSweepGateExclusiveHeldByGc(ctx, tenant, gateLabel);
        }
        ctx.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val("0"), DSL.val(true))).fetch();
        // A route that sets no statement bound of its own (the unbounded forms) gets one for the registry reads, so a row a verb holds ends the
        // wait as a typed busy answer instead of hanging the request; a route with a bound keeps it (restore: the 25 s, 503 quarantine_restore_busy)
        boolean noStatementBound = "0".equals(ctx.fetchValue(DSL.function("current_setting", String.class, DSL.val("statement_timeout"))));
        if (noStatementBound) {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(registryReadBoundMs));
        }
        boolean siblingById = sibling != null && ExpectedCollectionIds.of(sibling).isPresent();
        CollectionRow originRow;
        CollectionRow siblingRow;
        try {
            CollectionRegistry.requireAllForWrite(ctx, tenant, siblingById ? java.util.List.of(origin, sibling) : java.util.List.of(origin));
            originRow = CollectionRegistry.requireForWrite(ctx, tenant, origin);
            siblingRow = siblingById ? CollectionRegistry.requireForWrite(ctx, tenant, sibling) : null;
        } catch (org.jooq.exception.DataAccessException e) {
            if (SqlConstraints.isLockNotAvailable(e) || "57014".equals(e.sqlState())) {
                throw new RegistryWaitTimedOut(e);
            }
            throw e;
        }
        if (noStatementBound) {
            PgSession.setLocal(ctx, "statement_timeout", "0");
        }
        if (gate == Gate.ORIGIN) {
            RenameFollow.check(gateLabel, originRow);
        } else if (gate == Gate.SIBLING && siblingRow != null) {
            RenameFollow.check(gateLabel, siblingRow);
        }
        ctx.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val(priorLockTimeout), DSL.val(true))).fetch();
        return new Labels(originRow.label(), siblingRow != null ? siblingRow.label() : sibling);
    }

    /** {@link #enter} with no gate and no sibling: a transaction that runs no gc function (the keyed restore's ledger-only transaction). */
    public static Labels enter(DSLContext ctx, String tenant, String origin) {
        return enter(ctx, tenant, Gate.NONE, origin, origin, null);
    }
}
