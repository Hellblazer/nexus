// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
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
 * <p><b>The lock order of a delete by id</b> (B1 of the row 14 and row 16 merge). {@link #lockByIdFirst} takes, in this order: (1) the per-tenant
 * start guard, {@code pg_advisory_xact_lock_shared}, FIRST, before any registry row (a start holds it exclusive and then wants the origin's row
 * {@code FOR KEY SHARE}: a delete that held the row first and then waited for the guard would close a cycle with it, 40P01); (2) the origin's
 * row {@code FOR UPDATE} and its quarantine siblings' rows {@code FOR KEY SHARE}, in {@code collection_id} order and with no lock bound (linda:
 * the delete's lock is unbounded, a bound would be a new wire answer); (3) {@link CollectionRefusals#requireSourceById}, which only checks (404, 409
 * retired, 409 mismatch) and consumes the id the request named, a no-wait re-lock of the row (2) holds; (4) the {@code collection_busy} check.
 * One statement cannot lock some rows {@code FOR UPDATE} and others {@code FOR KEY SHARE}, so (2) is up to three statements that keep the id
 * order across both modes: the siblings below the origin's id, the origin, the siblings above it. The origin is never key-shared and then
 * upgraded: two concurrent deletes of one id would each hold a key share and wait on the other's upgrade (40P01). The collection's aspect task rows
 * are taken after all of it, by the purge's own {@code AspectTaskRefs.deleteNaming}: the registry row comes before the tuple rows, as in the
 * claim-fenced aspect write ({@code AspectRepository.upsertAspect}: registry key share, then the claim row), the one-row rename and the re-home,
 * so the delete cannot cycle with them. A delete by label keeps its order: the fence key-shares the origin and the siblings in one statement
 * ({@link #lockForDelete}), after the purge's task-row delete (a pre-existing inversion against those verbs, absorbed by the deadlock retry).
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
     * The delete by label's call: after the tuple locks and before its first child-row statement. A delete by id is not locked here: its
     * rows, its guard and its checks were taken first, by {@link #lockByIdFirst}, before the scope was resolved.
     *
     * @param scope           the delete's resolved scope; {@code onlyId} the id the route named, or null
     * @param refuseWhenBusy  true for a delete that came in through the route: refused while a re-embed job holds the collection
     * @throws CatalogRepository.CollectionHeldByReembedJob naming the job
     */
    static void lockForDelete(DSLContext ctx, String tenant, String name, CollectionPurgeScope scope, boolean keepQuarantine, Long onlyId,
                              boolean refuseWhenBusy, Gate3 gate3) {
        if (scope.matchesNothing() || onlyId != null) {
            return;
        }
        if (refuseWhenBusy) {
            takeGuardShared(ctx, tenant);
        }
        Long originId = scope.scopedById() ? scope.id() : null;
        List<CollectionRow> locked;
        if (!keepQuarantine && !QuarantineOrigin.isQuarantine(ctx, tenant, name)) {
            locked = lockOriginAndSiblings(ctx, tenant, name, originId, gate3);
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

    /**
     * The delete by id, first: steps (1) to (4) of the lock order in the class comment. Returns the origin's row, locked {@code FOR UPDATE}
     * and checked, for the delete to scope its purge by.
     *
     * @param originId       the id the route named
     * @param name           the label the route boundary resolved {@code originId} to, which the live row must still bear
     * @param refuseWhenBusy true for a delete that came in through the route (always true by id today)
     * @throws UnknownCollectionIdException   no row of {@code originId} (404)
     * @throws CollectionRetiredIdException   the row is retired (409)
     * @throws CollectionIdMismatchException  the row bears another label than {@code name} (409)
     * @throws CatalogRepository.CollectionHeldByReembedJob naming the job
     */
    static CollectionRow lockByIdFirst(DSLContext ctx, String tenant, String name, long originId, boolean keepQuarantine,
                                       boolean refuseWhenBusy, Gate3 gate3) {
        if (refuseWhenBusy) {
            takeGuardShared(ctx, tenant);   // (1)
        }
        // the seam of a request's by-id read, just before the transaction's first one (a no-op in production), as CollectionTombstones.lockRows has it
        ExpectedCollectionIds.beforeTransactionRead();
        lockInIdOrder(ctx, tenant, name, originId, !keepQuarantine && !QuarantineOrigin.isQuarantine(ctx, tenant, name), gate3);   // (2)
        CollectionRow row = CollectionRefusals.requireSourceById(ctx, tenant, originId, name);   // (3): the row is held, so this waits for nothing
        if (refuseWhenBusy) {
            refuseWhileAJobHolds(ctx, tenant, name, originId, List.of(row));   // (4)
        }
        return row;
    }

    /** The per-tenant re-embed start guard, shared: a start takes it exclusive, a rename or a re-home shared. Taken before any registry row. */
    private static void takeGuardShared(DSLContext ctx, String tenant) {
        // advisory locks come before registry rows (the lock order of WriteRegistryMemo): the guard first, shared, as a rename takes it
        WriteRegistryMemo.assertNoneHeld(ctx, "the re-embed start guard");
        ctx.select(DSL.function("pg_advisory_xact_lock_shared", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val(CatalogRepository.reembedGuardKey(tenant)))))
            .fetch();
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
     * The origin's row {@code FOR UPDATE} and the rows of the quarantine collections that hold rows of it {@code FOR KEY SHARE}, in
     * {@code collection_id} order across both modes: the siblings whose id is below the origin's, the origin, the siblings above it. The
     * siblings are found by name and locked by the id they bear now (so a sibling a verb renames while the delete waits is still the row
     * locked); the set is read again after the origin is locked, always, and a sibling that is new is key-shared on the next pass, up to
     * {@link #PASSES} (a sibling that keeps appearing past that is left to the delete's own row delete, which waits on it as before). A sibling
     * found on a later pass is locked after the origin, out of id order, so it can cycle with a verb that holds it and wants the origin: a 40P01
     * the deadlock retry absorbs. No lock bound is set.
     */
    private static void lockInIdOrder(DSLContext ctx, String tenant, String origin, long originId, boolean withSiblings, Gate3 gate3) {
        Set<Long> handled = new HashSet<>();
        handled.add(originId);
        boolean first = true;
        for (int pass = 0; pass < PASSES; pass++) {
            List<Long> todo = new ArrayList<>();
            List<String> names = withSiblings ? QuarantineOrigin.siblingsHolding(ctx, tenant, origin, gate3) : List.of();
            if (!names.isEmpty()) {
                for (Long id : ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                        .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(names))
                        .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID).fetch(CATALOG_COLLECTIONS.COLLECTION_ID)) {
                    if (handled.add(id)) todo.add(id);
                }
            }
            if (!first && todo.isEmpty()) {
                return;
            }
            if (first) {
                RegistryRowLocks.keyShareIfPresent(ctx, tenant, todo.stream().filter(id -> id < originId).toList(), List.of());
                ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(originId))
                    .forUpdate().of(CATALOG_COLLECTIONS).fetch();
                RegistryRowLocks.keyShareIfPresent(ctx, tenant, todo.stream().filter(id -> id > originId).toList(), List.of());
            } else {
                RegistryRowLocks.keyShareIfPresent(ctx, tenant, todo, List.of());
            }
            first = false;
            // the set is read once more after the origin is locked, whatever the first read found: a sibling that appeared while the delete waited
            // for the origin's row is key-shared by the next pass. That one is taken after the origin's FOR UPDATE and so out of id order (below the
            // origin's id too): a verb holding it FOR UPDATE and wanting the origin would cycle with the delete, a 40P01 the deadlock retry absorbs
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
    static List<CollectionRow> lockOriginAndSiblings(DSLContext ctx, String tenant, String origin, Long originId, Gate3 gate3) {
        Set<String> handled = new HashSet<>();
        List<CollectionRow> locked = new ArrayList<>();
        boolean first = true;
        for (int pass = 0; pass < PASSES; pass++) {
            List<String> siblings = QuarantineOrigin.siblingsHolding(ctx, tenant, origin, gate3);
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
            // read once more after the first lock, whatever the first read found (as lockInIdOrder): a late sibling is key-shared by the next pass
        }
        return locked;
    }
}
