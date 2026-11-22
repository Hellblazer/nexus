// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import dev.nexus.service.datamove.RenameReadiness;

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
 * <p>{@link #open()} never refuses: what a repository built without a guard gets, so the callers and tests that predate P3.5a
 * are unchanged.
 */
public final class RenameGuard {

    private static final RenameGuard OPEN = new RenameGuard(Gate3.never(), RenameReadiness.ready());

    private final Gate3 gate3;
    private final RenameReadiness readiness;

    public RenameGuard(Gate3 gate3, RenameReadiness readiness) {
        this.gate3 = java.util.Objects.requireNonNull(gate3, "gate3");
        this.readiness = java.util.Objects.requireNonNull(readiness, "readiness");
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
     * RDR-229 P3.7 (O-5): runs a whole rename, decision and commit, inside {@link RenameReadiness#pinned}, so gate 3 cannot flip to
     * true between the path the rename chose and its commit. A rename takes {@link #requireRenameAllowed} and {@link #oneRowRename}
     * inside {@code rename}, never before it.
     */
    public <T> T pinned(java.util.function.Supplier<T> rename) {
        return readiness.pinned(rename);
    }

    /**
     * The rename path after {@link #requireRenameAllowed} passed: true when gate 3 holds (then nothing is missing and the rename is the
     * one-row update that keeps the collection's id), false for the name path. Read inside {@link #pinned}, where gate 3 cannot change.
     */
    public boolean oneRowRename() {
        return gate3.holds();
    }

    /**
     * Whether a collection's id survives a rename on this install (R6's {@code ids_stable}, the key P3.5a's rename response
     * carries). Always false in this tree: the rename it runs is the name path, which gives the collection a new id, whatever
     * gate 3 says. P3.7's one-row rename keeps the id and is what makes this true (gate 3 holding, the drop move complete, every
     * P3.5b changeset applied, {@link #requireRenameAllowed} passing); it changes this one method.
     */
    public boolean idsStable() {
        return false;
    }
}
