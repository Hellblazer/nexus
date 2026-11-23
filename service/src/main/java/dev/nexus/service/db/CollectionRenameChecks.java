// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

import org.jooq.DSLContext;

/**
 * The refusals every collection rename checks before it writes (RDR-229 P3.7): the name path ({@code CatalogRepository#renameCollectionTxn}'s name
 * body) and the one-row path share them, so the one-row rename cannot skip a refusal the name path has. Moved out of {@link CatalogRepository} to keep
 * that file under its size ceiling ({@code scripts/file-size-exceptions.txt}); the bodies are unchanged.
 */
final class CollectionRenameChecks {

    private CollectionRenameChecks() { /* static */ }

    /**
     * What every rename checks before it writes, on either path (RDR-229 P3.7: the name path and the one-row path share it, so the one-row rename
     * cannot skip a refusal the name path has): the sweep gates (SHARED, sorted), the re-embed job, the model token, the live-target refusal and the
     * emptiness of a retired target. Runs in the rename's own transaction, after the rename lock.
     *
     * @return whether a row of the new label exists (a tombstone the caller established is empty)
     */
    static boolean shared(CatalogRepository repo, DSLContext ctx, String tenant, String oldName, String newName,
                                       String expectedTargetSupersededBy, String newNameEmbeddingModelOrNull) {
        // nexus-11gh6 rev 2 §3.2 (Hal Q1: gate the Java collection-move
        // paths in this bead). The rename below bulk-repoints
        // catalog_document_chunks.collection (and chunks_<dim>.collection via
        // COLLECTION_SCOPED_TABLES) from oldName to newName -- the SAME
        // hazard class as a manifest INSERT: it can make a chash appear
        // referenced in newName's scope (or vanish from oldName's) while
        // a concurrent sweep of either collection is mid-guard. Gate
        // BOTH endpoints SHARED, sorted alphabetically so two concurrent
        // renames sharing an endpoint acquire in one consistent global
        // order (no lock-order deadlock between them).
        for (String c : java.util.stream.Stream.of(oldName, newName).sorted().distinct().toList()) {
            CatalogRepository.acquireSweepGateShared(ctx, tenant, c);
        }
        // RDR-210 Phase 6 Step 2 (bead nexus-f98bg, ruling 4): a name a re-embed job in a lock state is moving
        // documents out of or into is not renamed underneath it. Read under the sweep gates above, in the
        // rename's own transaction.
        CatalogRepository.refuseUnderReembedJob(ctx, tenant, "rename", oldName, newName);
        // nexus-ucagi: step 1 below copies the SOURCE row's embedding_model onto the new row, and
        // step 2 re-files the chunks under it, so the vectors stay in that model's space whatever
        // the new name says. A new name whose token (the caller's, RDR-204: the engine does not
        // parse names) disagrees would register a name claiming a model the vectors are not in;
        // readers that trust the token over the row then disagree with what the engine embeds by.
        // Refused here, before step 1, so nothing is written. Skipped when the caller sent no
        // token (an older client, or a legacy new name) and for a `disputed` source, whose row
        // model is itself in doubt (hygiene-002) -- the two cases the client verbs
        // (corpus.rename_model_conflict and their disputed warning) already exempt.
        if (newNameEmbeddingModelOrNull != null) {
            var src = ctx.select(CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(oldName))
                .fetchOne();
            if (src != null) {
                String srcModel = src.value1();
                boolean disputed = "disputed".equals(src.value2());
                if (!disputed && srcModel != null && !srcModel.equals(newNameEmbeddingModelOrNull)) {
                    throw CollectionModelMismatchException.forRename(
                        oldName, srcModel, newName, newNameEmbeddingModelOrNull);
                }
            }
        }
        return target(repo, ctx, tenant, newName, expectedTargetSupersededBy);
    }

    /**
     * The target half of {@link #shared}: the live-target refusal, the emptiness of a retired target and the identity belt. The one-row
     * rename runs it again under its registry row locks (folio's r3 critique F1, linda): what it read before the lock may have changed while it waited.
     *
     * @return whether a row of the new label exists (a retired, empty one)
     */
    static boolean target(CatalogRepository repo, DSLContext ctx, String tenant, String newName, String expectedTargetSupersededBy) {
        // nexus-cecqy: LIVE target, not merely "a row exists". Since step 3 retires X
        // as a superseded tombstone instead of deleting it, a round-trip rename
        // (X->Y then Y->X) finds X's tombstone sitting at the target. A tombstone is
        // a retired name, not an occupied one, so it takes the canonical branch, where
        // step 1's upsert REVIVES it. A LIVE target is refused below.
        // THIS IS THE AUTHORITATIVE BRANCH SELECTOR, and it now MEASURES the hazard
        // instead of inferring it. Two prior fixes inferred, and both were wrong:
        //   nexus-u4e20  guarded on "a row exists"  — could not see that a tombstone
        //                is a retired name, not an occupied one. Undo unreachable.
        //   nexus-v6za0  guarded on LIVENESS, then on IDENTITY (superseded_by ==
        //                oldName). Both are proxies for EMPTINESS and both leak:
        //                supersedeCollection is a pure UPDATE, so a supersede
        //                tombstone is non-live AND populated, and nothing stops its
        //                superseded_by naming the rename source. step 3 and
        //                supersedeCollection write the column identically, so no
        //                predicate over it can recover PROVENANCE.
        // The canonical branch's actual precondition is that the target holds NO DATA,
        // because step 1 revives the row and step 2 re-homes the source's children ON
        // TOP of whatever is already there. So ask that question, in this transaction's
        // own snapshot. A populated non-live target is refused: the canonical branch would
        // merge it.
        boolean targetRowExists = ctx.fetchExists(
            ctx.selectOne().from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(newName)));
        boolean liveTargetExists = ctx.fetchExists(
            ctx.selectOne().from(CATALOG_COLLECTIONS)
               .where(CATALOG_COLLECTIONS.NAME.eq(newName)
                   .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""))));
        // WHY THIS RE-CHECKS EMPTINESS BUT NOT IDENTITY, which looks asymmetric.
        // The handler checks both; this transaction re-checks only the first. That is
        // deliberate, and the reason lives in a DIFFERENT method, so state it here or
        // the next reader re-derives it (both reviewers raised this; one withdrew it
        // after tracing the mechanism — nexus-2sovp carries the analysis).
        //
        //   EMPTINESS is a fact about ROWS IN OTHER TABLES. Nothing serialises those
        //   against this rename, so a concurrent write can land between the handler's
        //   look and this transaction. It must be re-measured HERE, in the snapshot
        //   that is about to act on it.
        //
        //   IDENTITY is a fact about superseded_by on THIS row, and
        //   supersedeCollection carries its own precondition IN THE WHERE CLAUSE
        //   (SUPERSEDED_BY = '' OR SUPERSEDED_BY = :target — a DB-level
        //   compare-and-swap added for nexus-cecqy). So the column cannot be moved to
        //   a THIRD value underneath us: a concurrent supersede either matches zero
        //   rows, or writes the value it already had. The handler's read stays true.
        //
        // If that CAS is ever weakened, this asymmetry becomes a hole and the identity
        // check has to be re-checked here too. The guard below is not self-defending;
        // it is defended by a precondition in another statement.
        //
        // nexus-2sovp, ADDITIVE BELT (2026-08-01, Hal-adjudicated): the paragraph above
        // is what makes the asymmetry SAFE TODAY — there is exactly one caller
        // (CatalogHandler.handleCollectionRename), and its identity gate cannot go stale
        // because of the CAS. That is a fact about the CURRENT caller graph, not about
        // this method. A future non-HTTP caller (a CLI path, a migration step, a
        // scheduled repair) that calls renameCollection without replicating the
        // handler's identity pre-check would inherit the emptiness protection below and
        // NONE of the identity protection — silently. The belt below is optional
        // (opt-in via a non-null expectedTargetSupersededBy) precisely so it can be
        // additive: it does not change behavior for any caller that passes null, and it
        // re-verifies — it does not replace — the emptiness check above.
        if (targetRowExists && !liveTargetExists) {
            var blocker = repo.blockingTable(ctx, tenant, newName);
            if (blocker.isPresent()) {
                // nexus-34wrg option (c): name WHICH table blocked so an operator can
                // tell an audit breadcrumb from real data instead of purging on faith.
                throw new CatalogRepository.CollectionMergeRefused(
                    "target collection " + newName + " is retired but still holds "
                    + blocker.get().describe() + "; renaming onto it would merge two "
                    + "collections. Purge or restore it first.");
            }
        }
        if (targetRowExists && !liveTargetExists && expectedTargetSupersededBy != null) {
            String actualTargetSupersededBy = ctx.select(CATALOG_COLLECTIONS.SUPERSEDED_BY)
                .from(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.NAME.eq(newName))
                .fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY);
            if (!expectedTargetSupersededBy.equals(actualTargetSupersededBy)) {
                throw new CatalogRepository.CollectionMergeRefused(
                    "target collection " + newName + " is retired by " + actualTargetSupersededBy
                    + ", not " + expectedTargetSupersededBy + " as observed at the caller's own read; "
                    + "refusing to revive a tombstone whose identity changed underneath this rename.");
            }
        }

        if (liveTargetExists) {
            // nexus-avjrb: a live target is an occupied name. The handler 409s it first; this
            // refusal holds for any direct caller. (The RDR-162 cross-model COPY branch that
            // repointed documents only onto a live target is gone: no released client reaches
            // it on a production path.)
            throw new CatalogRepository.CollectionMergeRefused("target collection already exists: " + newName);
        }
        return targetRowExists;
    }
}
