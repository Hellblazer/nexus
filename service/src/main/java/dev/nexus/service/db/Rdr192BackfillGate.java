// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * RDR-192 (bead nexus-wbfpw.41) — the precondition the state-derived reaper
 * checks before it deletes a manifest-less chunk.
 *
 * <p>{@code reapable(c)} treats a chunk with no manifest row as garbage once
 * it ages past the grace window. A legacy note (stored before nexus-b6enc: it
 * has a catalog document but never got a manifest row) is manifest-less too,
 * and RDR-192 Phase 2 already hides it from search and get. Deleting it is
 * irreversible, so the reaper must not run on a tenant whose legacy-
 * unmanifested backfill has not completed.
 *
 * <p>Completion is recorded by the client's upgrade-ladder rung
 * {@value #RUNG_NAME} (Python {@code
 * nexus.upgrade_ladder.rungs.rdr192_manifest_backfill}) in {@code
 * nexus.ladder_completions}, and ONLY after a fresh census over the tenant
 * reads zero legacy-unmanifested and zero unclassified chunks (RDR-142
 * verify-before-record). This class reads that one fact. It does not run the
 * census itself and never writes.
 *
 * <p><b>Fails closed.</b> No record, a different rung's record, another
 * tenant's record, and a ledger that cannot be read all refuse. A tenant no
 * client has upgraded since the rung shipped has no record; that refusal is
 * the intended safe outcome, not a defect to route around.
 *
 * <p>The reaper (nexus-2x9xa) calls {@link #requireComplete} at the top of each
 * pass, per tenant, and skips the tenant on {@link BackfillIncompleteException}.
 * Nothing calls it yet: the reaper is a later bead and this one deliberately
 * does not wire or implement it.
 */
public final class Rdr192BackfillGate {

    private static final Logger log = LoggerFactory.getLogger(Rdr192BackfillGate.class);

    /**
     * The ladder rung name. The Python side ({@code RUNG_RDR192_MANIFEST_BACKFILL}
     * in {@code nexus.upgrade_ladder.registry}) is the same literal;
     * {@code tests/upgrade/test_rdr192_manifest_backfill_rung.py} pins the two equal.
     */
    public static final String RUNG_NAME = "rdr192-manifest-backfill";

    private final LadderRepository ladder;

    public Rdr192BackfillGate(LadderRepository ladder) {
        this.ladder = ladder;
    }

    /** True only when this tenant has a verified completion record for the rung. */
    public boolean isComplete(String tenant) {
        try {
            return ladder.isRungVerified(tenant, RUNG_NAME);
        } catch (RuntimeException e) {
            log.warn("event=rdr192_backfill_gate_unreadable tenant={} error={}", tenant, e.getMessage());
            return false;
        }
    }

    /**
     * Returns normally only when {@link #isComplete} is true.
     *
     * @throws BackfillIncompleteException when the rung has no record for the tenant, or
     *         the ledger could not be read
     */
    public void requireComplete(String tenant) {
        boolean complete;
        try {
            complete = ladder.isRungVerified(tenant, RUNG_NAME);
        } catch (RuntimeException e) {
            log.warn("event=rdr192_backfill_gate_unreadable tenant={} error={}", tenant, e.getMessage());
            throw new BackfillIncompleteException(
                    "cannot read the completion ledger for tenant '" + tenant + "' ("
                            + e.getMessage() + "); refusing to reap manifest-less chunks", e);
        }
        if (!complete) {
            log.info("event=rdr192_backfill_gate_refused tenant={} rung={}", tenant, RUNG_NAME);
            throw new BackfillIncompleteException(
                    "RDR-192 manifest backfill has not completed for tenant '" + tenant
                            + "' (no verified '" + RUNG_NAME + "' completion in nexus.ladder_completions);"
                            + " refusing to reap manifest-less chunks. A client must run `nx upgrade`"
                            + " against this tenant so the census and backfill can run.");
        }
    }

    /** The reaper must not run for this tenant yet. */
    public static final class BackfillIncompleteException extends IllegalStateException {
        private static final long serialVersionUID = 1L;

        BackfillIncompleteException(String message) {
            super(message);
        }

        BackfillIncompleteException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
