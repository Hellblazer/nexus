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
 * every P3.5b changeset applied) a rename is refused with {@link RenameRefusedException}, naming the move, the missing list or the
 * restart. Before gate 3 it is the name path and nothing here is consulted, until gate 3 is latched: the latch is sticky
 * (P3.7), so a rename is never the name path again once gate 3 has held, even if the live gate 3 drops back. Both inputs are read on
 * each call, never cached, so the refusal follows gate 3 with no restart.
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

    /** A rename's body, told which path it takes. */
    @FunctionalInterface
    public interface OnPath<T> {
        /** @param oneRow true for the one-row update that keeps the collection's id, false for the name path */
        T run(boolean oneRow);
    }

    /**
     * Whether the one-row regime is in force: gate 3 has been LATCHED ({@link RenameReadiness#gate3Latched()}: it held once and is
     * remembered), or the name-key drop move has completed ({@link RenameReadiness#dropMoveComplete()}, a durable belt for a restart that reset the
     * process-local latch), or holds now ({@code gate3}, which the latch contains in the engine, so the second term is for a readiness with no
     * latch, a test's lambda). The live gate 3 dropping back (an id index going INVALID) never returns a rename to the name path.
     */
    private boolean oneRowRegime(boolean gate3Holds) {
        return readiness.gate3Latched() || readiness.dropMoveComplete() || gate3Holds;
    }

    /**
     * @throws RenameRefusedException when the one-row regime is in force and the one-row rename cannot run yet
     */
    public void requireRenameAllowed() {
        if (!oneRowRegime(gate3.holds())) {
            return;
        }
        RenameReadiness.Missing missing = readiness.missing();
        if (missing != RenameReadiness.Missing.NONE) {
            throw new RenameRefusedException(missing);
        }
    }

    /**
     * RDR-229 P3.7 (O-5): the whole rename, decision and commit, inside {@link RenameReadiness#pinned}, so gate 3 cannot flip to true
     * between the path the rename chose and its commit. The regime is decided ONCE here, inside the pin; the refusal and the path both
     * follow that one decision, and the path is handed to {@code rename}, so a flap of the live gate 3 between two reads can never mix the
     * refusal of one regime with the path of the other (the latch itself only ever goes from false to true, under the window).
     *
     * @throws RenameRefusedException when the one-row regime is in force and the one-row rename cannot run yet; nothing was run
     */
    public <T> T pinned(OnPath<T> rename) {
        return readiness.pinned(() -> {
            boolean oneRow = oneRowRegime(gate3.holds());
            if (oneRow) {
                RenameReadiness.Missing missing = readiness.missing();
                if (missing != RenameReadiness.Missing.NONE) {
                    throw new RenameRefusedException(missing);
                }
            }
            return rename.run(oneRow);
        });
    }

    /**
     * Whether a collection's id survives a rename on this install (R6/S-7's {@code ids_stable}, the key P3.5a's rename response
     * carries): gate 3 is latched and nothing is missing (the name-key drop move completed and every P3.5b changeset applied), which is the
     * state in which the rename is the one-row update that keeps the id. Read per call, like both inputs; false while either waits. The
     * live gate 3 dropping back does not make ids unstable: the rename stays one-row.
     */
    public boolean idsStable() {
        return oneRowRegime(gate3.holds()) && readiness.missing() == RenameReadiness.Missing.NONE;
    }
}
