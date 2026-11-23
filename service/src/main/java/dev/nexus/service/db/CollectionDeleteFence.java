// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.impl.DSL;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.REEMBED_JOBS;

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
 *
 * <p><b>The re-embed jobs.</b> A delete that arrives through the route ({@code refuseWhenBusy}) is refused with
 * {@link CatalogRepository.CollectionHeldByReembedJob} (the route answers 409 {@code collection_busy}) while a job in a lock state (planned,
 * running, paused, cutover or failed: every non-terminal state) holds the collection as its source or its target. The check runs under the
 * per-tenant start guard, taken SHARED first (a start takes it exclusive, a rename or re-home shared) and then under the origin's registry
 * row, read {@code FOR KEY SHARE}, so a job that starts while the delete runs either committed before the delete's read (the delete sees it
 * and refuses) or waits for the delete to commit and then finds the collection gone (a typed 422 from the start's own registry read); no
 * start can slip between the check and the row delete. The job's ends are matched by the locked row's id, with the label for an end that
 * stored no id ({@link ReembedJobHold#endIs}). The re-embed runner's own delete of its source at cutover is not routed here: it is the
 * job's step, not a request.
 */
final class CollectionDeleteFence {

    /** Passes of read-lock-reread: a sibling that appears while the delete waits for a lock is locked on the next pass. */
    private static final int PASSES = 3;

    private CollectionDeleteFence() {}

    /**
     * The delete's call: after the tuple locks and before its first child-row statement.
     *
     * @param scope           the delete's resolved scope; {@code onlyId} the id the route named, or null
     * @param refuseWhenBusy  true for a delete that came in through the route: refused while a re-embed job holds the collection
     * @throws CatalogRepository.CollectionHeldByReembedJob naming the job
     */
    static void lockForDelete(DSLContext ctx, String tenant, String name, CollectionPurgeScope scope, boolean keepQuarantine, Long onlyId,
                              boolean refuseWhenBusy) {
        if (scope.matchesNothing()) {
            return;
        }
        if (refuseWhenBusy) {
            // advisory locks come before registry rows (the lock order of WriteRegistryMemo): the guard first, shared, as a rename takes it
            ctx.select(DSL.function("pg_advisory_xact_lock_shared", Object.class,
                    DSL.function("hashtext", Integer.class, DSL.val(CatalogRepository.reembedGuardKey(tenant)))))
                .fetch();
        }
        Long originId = scope.scopedById() ? scope.id() : onlyId;
        List<CollectionRow> locked;
        if (!keepQuarantine && !QuarantineOrigin.isQuarantine(ctx, tenant, name)) {
            locked = lockOriginAndSiblings(ctx, tenant, name, originId);
        } else if (refuseWhenBusy) {
            locked = RegistryRowLocks.keyShareIfPresent(ctx, tenant, originId == null ? List.of() : List.of(originId),
                originId == null ? List.of(name) : List.of());
        } else {
            return;
        }
        if (refuseWhenBusy) {
            refuseWhileAJobHolds(ctx, tenant, name, originId, locked);
        }
    }

    /** The job in a lock state that holds the origin as its source or target, else nothing: the delete refuses on it. */
    private static void refuseWhileAJobHolds(DSLContext ctx, String tenant, String name, Long originId, List<CollectionRow> locked) {
        Condition holds = DSL.falseCondition();
        boolean haveRow = false;
        for (CollectionRow row : locked) {
            if (originId != null ? row.collectionId() == originId : row.label().equals(name)) {
                haveRow = true;
                holds = holds
                    .or(ReembedJobHold.endIs(REEMBED_JOBS.SOURCE_COLLECTION_ID, REEMBED_JOBS.SOURCE_COLLECTION, tenant, row.collectionId(), row.label()))
                    .or(ReembedJobHold.endIs(REEMBED_JOBS.TARGET_COLLECTION_ID, REEMBED_JOBS.TARGET_COLLECTION, tenant, row.collectionId(), row.label()));
            }
        }
        if (!haveRow) {
            // no registry row to take an id from (a label with no row): a job that recorded the label still holds it
            holds = REEMBED_JOBS.SOURCE_COLLECTION.eq(name).or(REEMBED_JOBS.TARGET_COLLECTION.eq(name));
        }
        var held = ctx.select(REEMBED_JOBS.JOB_ID, REEMBED_JOBS.STATE, REEMBED_JOBS.SOURCE_COLLECTION, REEMBED_JOBS.TARGET_COLLECTION)
            .from(REEMBED_JOBS)
            .where(REEMBED_JOBS.TENANT_ID.eq(tenant))
            .and(REEMBED_JOBS.STATE.in(ReembedJobRepository.LOCK_STATES))
            .and(holds)
            .orderBy(REEMBED_JOBS.STARTED_AT, REEMBED_JOBS.JOB_ID)
            .limit(1)
            .fetchOne();
        if (held != null) {
            throw new CatalogRepository.CollectionHeldByReembedJob("cannot delete " + name + ": re-embed job " + held.value1() + " ("
                + held.value2() + ") is moving documents from " + held.value3() + " to " + held.value4()
                + ". Abort or finish the job first.", held.value1());
        }
    }

    /**
     * Locks, {@code FOR KEY SHARE} in id order, the registry row of the collection being deleted and the rows of the quarantine collections
     * that hold rows of it (the siblings {@code deleteRowsOf} will reach). The siblings are found by name and locked by the id they bear
     * now, so a sibling that a verb renames while the delete waits is still the row locked; the sibling set is read again after the lock
     * and any that is new is locked on the next pass, up to {@link #PASSES} (a sibling that keeps appearing past that is left to the
     * delete's own row delete, which waits on it as before).
     *
     * @param originId the id of the row the delete was scoped to, or {@code null} for a delete by label (the origin's rows by that label are locked)
     * @return the rows locked (the origin's, and the siblings')
     */
    static List<CollectionRow> lockOriginAndSiblings(DSLContext ctx, String tenant, String origin, Long originId) {
        Set<String> handled = new HashSet<>();
        List<CollectionRow> locked = new ArrayList<>();
        boolean first = true;
        for (int pass = 0; pass < PASSES; pass++) {
            List<String> siblings = QuarantineOrigin.siblingsHolding(ctx, tenant, origin);
            List<String> todo = new ArrayList<>();
            for (String sibling : siblings) {
                if (!handled.contains(sibling)) todo.add(sibling);
            }
            if (!first && todo.isEmpty()) {
                return locked;
            }
            List<Long> ids = new ArrayList<>(todo.isEmpty() ? List.of() : ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(todo))
                .fetch(CATALOG_COLLECTIONS.COLLECTION_ID));
            if (first && originId != null) {
                ids.add(originId);
            }
            locked.addAll(RegistryRowLocks.keyShareIfPresent(ctx, tenant, ids, first && originId == null ? List.of(origin) : List.of()));
            handled.addAll(todo);
            first = false;
            if (todo.isEmpty()) {
                return locked;
            }
        }
        return locked;
    }
}
