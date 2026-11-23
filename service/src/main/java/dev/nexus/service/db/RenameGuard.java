// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.CompletionCheck;
import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness;
import org.jooq.DSLContext;

/**
 * The rename refusal of RDR-229 P3.5a (bead nexus-p2m1x.23), the one object every collection-rename primitive takes by
 * constructor: {@code CatalogRepository.renameCollection} and the rename of each per-store repository (chash, taxonomy,
 * telemetry, aspects, highlights). Linda's ruling 3 (2026-11-22): the refusal lives in the shared primitive, so each entry
 * point that rewrites a collection name has it, an internal caller included.
 *
 * <p>From gate 3 ({@link Gate3}) until the one-row rename can run ({@link RenameReadiness}: the name-key drop move complete and
 * every P3.5b changeset applied) a rename is refused with {@link RenameRefusedException}, naming the move or the restart.
 * Before gate 3 it is the name path and nothing here is consulted. Both inputs are read on each call, never cached, so the
 * refusal follows gate 3 with no restart.
 *
 * <p>Two checks, because the first cannot be the last (linda's ruling D2): {@link #requireRenameAllowed()} runs at method entry,
 * outside the rename's transaction, so a rename that passed it could commit after the latch row was written.
 * {@link #requireRenameAllowed(DSLContext)} runs inside the rename's transaction under {@link RenameLatchLock}, which the latch write
 * takes exclusive: a rename either committed before the row was written or reads the row and is refused the same way. Every rename
 * primitive calls both; the first is the early answer, the second the authority.
 *
 * <p>{@link #open()} never refuses: what a repository built without a guard gets, so the callers and tests that predate P3.5a
 * are unchanged.
 */
public final class RenameGuard {

    private static final RenameGuard OPEN = new RenameGuard(Gate3.never(), RenameReadiness.ready(), false);

    private final Gate3 gate3;
    private final RenameReadiness readiness;
    /** False for {@link #open()}: it never refuses, so its transactions take no lock and read no latch row. */
    private final boolean recheckInTransaction;

    public RenameGuard(Gate3 gate3, RenameReadiness readiness) {
        this(gate3, readiness, true);
    }

    private RenameGuard(Gate3 gate3, RenameReadiness readiness, boolean recheckInTransaction) {
        this.gate3 = java.util.Objects.requireNonNull(gate3, "gate3");
        this.readiness = java.util.Objects.requireNonNull(readiness, "readiness");
        this.recheckInTransaction = recheckInTransaction;
    }

    /** A guard that never refuses. */
    public static RenameGuard open() {
        return OPEN;
    }

    /**
     * @throws RenameRefusedException when gate 3 holds and the one-row rename cannot run yet
     */
    public void requireRenameAllowed() {
        if (!gate3.holds()) {
            return;
        }
        RenameReadiness.Missing missing = readiness.missing();
        if (missing != RenameReadiness.Missing.NONE) {
            throw new RenameRefusedException(missing);
        }
    }

    /**
     * The check inside the rename's own transaction ({@code ctx} is that transaction's context): call it FIRST there, before any other
     * lock or write. It takes {@link RenameLatchLock} shared, then reads the latch row of gate 3, so the answer is the committed
     * state after any latch write that was in flight, never a read from before it. Refused when gate 3 holds (the in-process latch or
     * the row) and the one-row rename cannot run yet, with the same {@link RenameRefusedException} as the entry check; the transaction
     * rolls back with nothing written.
     *
     * @throws RenameRefusedException when gate 3 holds and the one-row rename cannot run yet
     */
    public void requireRenameAllowed(DSLContext ctx) {
        if (!recheckInTransaction) {
            return;
        }
        RenameLatchLock.shared(ctx);
        if (!gate3.holds() && CompletionCheck.readLatch(ctx, CompletionCheck.GATE3).isEmpty()) {
            return;
        }
        RenameReadiness.Missing missing = readiness.missing();
        if (missing != RenameReadiness.Missing.NONE) {
            throw new RenameRefusedException(missing);
        }
    }

    /**
     * Whether a collection's id survives a rename on this install (R6's {@code ids_stable}, the key P3.5a's rename response
     * carries). Always false in this tree: the rename it runs is the name path, which gives the collection a new id, whatever
     * gate 3 says. P3.7's one-row rename keeps the id and is what makes this true (gate 3 holding, the drop move complete, every
     * P3.5b changeset applied, {@link #requireRenameAllowed()} passing); it changes this one method.
     */
    public boolean idsStable() {
        return false;
    }
}
