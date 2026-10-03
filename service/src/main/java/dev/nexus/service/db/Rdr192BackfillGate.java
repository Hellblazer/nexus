// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.function.Predicate;

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
 * <p><b>The one exemption: an empty tenant (nexus-wbfpw.73).</b> A tenant that
 * holds nothing has nothing for the reaper to delete and nothing for a backfill
 * to heal, so the gate passes it without a completion record. "Empty" here reads
 * at least as strictly as the client rung's empty-listing branch of {@code
 * _default_census} (the Python rung), which calls a tenant converged without a
 * census when the collection listing is empty and the catalog's live document
 * chunk count is zero. The engine's test is that the tenant holds no row in {@code
 * nexus.chunks} at all, quarantine siblings included, which the client's census
 * skips; and because every manifest row references its chunk through the
 * validated catalog-029 foreign key, a tenant with no chunk has no manifest row
 * either, so the catalog cross-check the client needs against a listing that
 * failed silently has nothing left to add here. The two are not the same test and
 * the engine's is the stricter. The gate takes the test as a {@link Predicate}
 * so the database read stays in the vectors package; {@link
 * #Rdr192BackfillGate(LadderRepository)} has none, and a gate built that way
 * never exempts anything. The gate never writes a completion for an empty
 * tenant: the client rung stays the one recorder, and a tenant that later gains
 * content and has no record is refused again from that pass on. A tenant holding
 * only quarantine-* chunks is NOT empty: it holds chunks, and the quarantine
 * expiry that would follow is an irreversible delete.
 *
 * <p>The reaper (nexus-2x9xa) calls {@link #requireComplete} at the top of each
 * pass, per tenant, and skips the tenant on {@link BackfillIncompleteException}
 * ({@code ChunkReaper.passTenant}). This class only reads the fact; it neither
 * wires nor implements the reaper.
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
    private final Predicate<String> emptyTenant;

    /** A gate with no empty-tenant exemption: only a recorded completion opens it. */
    public Rdr192BackfillGate(LadderRepository ladder) {
        this(ladder, tenant -> false);
    }

    /**
     * @param emptyTenant true when the tenant holds no chunk row (read under the tenant's own
     *        RLS context). It may throw; a throw is a refusal, never a pass.
     */
    public Rdr192BackfillGate(LadderRepository ladder, Predicate<String> emptyTenant) {
        this.ladder = ladder;
        this.emptyTenant = emptyTenant;
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
     * Returns normally when the tenant has a verified completion record, or is empty (see the class comment).
     *
     * @throws BackfillIncompleteException when the rung has no record for the tenant and the tenant is not empty,
     *         or the ledger (or, with no record, the emptiness read) could not be read
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
        if (complete) return;
        boolean empty;
        try {
            empty = emptyTenant.test(tenant);
        } catch (RuntimeException e) {
            log.warn("event=rdr192_backfill_gate_empty_check_failed tenant={} error={}", tenant, e.getMessage());
            throw new BackfillIncompleteException(
                    "no verified '" + RUNG_NAME + "' completion for tenant '" + tenant
                            + "' and cannot tell whether the tenant is empty (" + e.getMessage()
                            + "); refusing to reap manifest-less chunks", e);
        }
        if (empty) {
            // DEBUG, not INFO: one line per empty tenant per hourly pass; the pass summary carries the count (tenants_empty).
            log.debug("event=rdr192_backfill_gate_passed_empty_tenant tenant={} rung={}", tenant, RUNG_NAME);
            return;
        }
        log.info("event=rdr192_backfill_gate_refused tenant={} rung={}", tenant, RUNG_NAME);
        throw new BackfillIncompleteException(
                "RDR-192 manifest backfill has not completed for tenant '" + tenant
                        + "' (no verified '" + RUNG_NAME + "' completion in nexus.ladder_completions);"
                        + " refusing to reap manifest-less chunks. A client must run `nx upgrade`"
                        + " against this tenant so the census and backfill can run.");
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
