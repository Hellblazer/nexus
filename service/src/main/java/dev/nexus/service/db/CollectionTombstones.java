// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;

import java.time.OffsetDateTime;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.1 stage 2b (nexus-p2m1x.17), residual R7: the writers that retire a registry row, and the delete that
 * can leave one pointing at nothing. {@code superseded_by_id = collection_id} means "retired, successor gone"; only
 * two writers create it, the stamp move and {@link #deleteRegistryRows}.
 *
 * <p>Moved out of {@code CatalogRepository} (over its size cap) as the area this bead rewrites: the repository's
 * {@code supersedeCollection} is a delegate of {@link #supersede}.
 */
public final class CollectionTombstones {

    private CollectionTombstones() {}

    /**
     * The registry row lock bound, in milliseconds as the GUC text (linda's Q3): a verb that cannot lock its rows in this time answers
     * {@link RenameBusyException} (503 {@code rename_busy}) and changes nothing. A new {@code FOR KEY SHARE} locker joins a row while
     * a {@code FOR UPDATE} waits for the earlier holder (pinned by
     * {@code RetiringVerbsLiveAtCommitTest#aWriterArrivingWhileTheVerbIsQueuedBehindAHolderJoinsIt_andTheVerbWaitsForBoth}), so the
     * verb waits for every holder, the late ones included, and steady overlapping writers could keep the row from it. A single writer
     * held past the bound ends the wait the same way ({@code aVerbWhoseRowIsHeldPastItsBoundAnswersRenameBusy_andChangesNothing}).
     * Either way the bound turns the wait into an answer. Writers get no timeout.
     */
    private static volatile String rowLockTimeoutMs = "10000";

    /** Test hook: a shorter bound, so a starvation test answers in a second; pass {@code null} to restore the default. */
    public static void setRowLockTimeoutMsForTests(String ms) {
        rowLockTimeoutMs = ms == null ? "10000" : ms;
    }

    /**
     * Step 3 of the canonical lock order ({@link RenameGuard#runRename}): the registry rows of {@code names} that exist,
     * {@code SELECT ... FOR UPDATE} in {@code collection_id} order, taken by a verb that retires or revives a row (the catalog rename
     * and the supersede; the delete is not one, linda's Q2) before any chunk, manifest or child-row work. The order itself, and why it
     * cannot close a cycle, is written once in {@code runRename}'s javadoc. The wait is bounded ({@link #rowLockTimeoutMs}, set local to
     * this statement and restored after it): on a timeout nothing has been written, the transaction rolls back, and
     * {@link RenameBusyException} says so. A wait over {@link RegistryLockWait#SLOW_MS} is logged with its time.
     */
    static void lockRows(DSLContext ctx, String tenant, String... names) {
        String previous = ctx.select(DSL.function("current_setting", String.class, DSL.val("lock_timeout"))).fetchOne(0, String.class);
        ctx.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val(rowLockTimeoutMs), DSL.val(true))).fetch();
        long started = System.nanoTime();
        try {
            ctx.select(CATALOG_COLLECTIONS.COLLECTION_ID).from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(names))
                .orderBy(CATALOG_COLLECTIONS.COLLECTION_ID)
                .forUpdate().fetch();
        } catch (DataAccessException e) {
            if ("55P03".equals(e.sqlState())) {
                RegistryLockWait.logIfSlow("verb", tenant, String.join(",", names), (System.nanoTime() - started) / 1_000_000, "timed_out");
                throw new RenameBusyException(rowLockTimeoutMs + " ms");
            }
            throw e;
        }
        RegistryLockWait.logIfSlow("verb", tenant, String.join(",", names), (System.nanoTime() - started) / 1_000_000, "locked");
        ctx.select(DSL.function("set_config", String.class, DSL.val("lock_timeout"), DSL.val(previous), DSL.val(true))).fetch();
    }

    /**
     * Supersede {@code name} by {@code supersededBy}: the CAS UPDATE of {@code CatalogRepository.supersedeCollection}
     * with the successor's id set in the same statement.
     *
     * <p>The target must be registered, and the refusal is typed ({@link UnregisteredCollectionException}) rather
     * than the 23514 a NULL subselect would give, and not the "already superseded" that
     * {@code ReembedRunner} reads from a zero row count. The check reads the target in this transaction; the id the
     * statement writes is its own subselect, so name and id come from one registry row.
     *
     * @return the number of rows updated (0 means no {@code (tenant, name)} row, or a different target already set)
     */
    static int supersede(DSLContext ctx, String tenant, String name, String supersededBy, Field<OffsetDateTime> stampedAt,
                         Gate3 gate3) {
        // The canonical lock order (RenameGuard#runRename), steps 1 and 3: this verb takes no sweep gate. The successor's row too, so a
        // delete of the successor between the target check and the UPDATE cannot leave the CHECK a successor that is gone (23514).
        RenameLatchLock.shared(ctx);
        lockRows(ctx, tenant, name, supersededBy);
        CollectionIds.require(ctx, tenant, supersededBy);
        return casSupersede(ctx, tenant, name, supersededBy, CollectionIds.idOf(tenant, supersededBy), stampedAt, gate3);
    }

    /**
     * Supersede {@code fromName} by {@code toName} with the successor's id given: {@code superseded_by = toName} and
     * {@code superseded_by_id = toId} in the one CAS statement of {@link #supersede(DSLContext, String, String, String, Field)},
     * the same load-bearing WHERE conjunct, and no registry lookup of the target.
     *
     * <p>For a rename, which draws the new row's id first, retires the source with it, and only then inserts the new row
     * (the source holds the live tuple the new row takes, and P3.1's CHECK ties {@code superseded_by_id} to
     * {@code superseded_by}, so the source cannot be retired by name alone). At the retire the successor has no registry
     * row for the other overload's subselect to read, and that overload refuses an unregistered target.
     *
     * <p><b>The caller vouches</b> that {@code toId} comes from the column's own sequence ({@code CollectionMint.drawId}) or is
     * the id of {@code toName}'s own row, and that the row {@code toName} with that id is inserted (or already exists) in
     * this transaction. Nothing here checks either, so a caller that cannot say so uses the other overload.
     *
     * @return the number of rows updated (0 means no {@code (tenant, fromName)} row, or a different target already set)
     */
    static int supersede(DSLContext ctx, String tenant, String fromName, String toName, long toId, Gate3 gate3) {
        return casSupersede(ctx, tenant, fromName, toName, DSL.val(toId), DSL.currentOffsetDateTime(), gate3);
    }

    private static int casSupersede(DSLContext ctx, String tenant, String name, String supersededBy, Field<Long> successorId,
                                    Field<OffsetDateTime> stampedAt, Gate3 gate3) {
        // RDR-229 P3.5c: the row is the caller's label (the registry name stays); "live" and "already this target" read the
        // id from gate 3. The name arm of a write (the SET) is the dual write's, not a read.
        Condition live = CollectionIdReads.live(gate3, CATALOG_COLLECTIONS.SUPERSEDED_BY, CATALOG_COLLECTIONS.SUPERSEDED_BY_ID);
        Condition alreadyThis = gate3.holds() ? CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.eq(successorId)
            : CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(supersededBy);
        return ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successorId)
            // nexus-0svvu (a): stamp only on the transition OUT OF '' (a genuine first supersede); a row that
            // already carries THIS target keeps whatever instant it already has.
            .set(CATALOG_COLLECTIONS.SUPERSEDED_AT,
                DSL.when(live, stampedAt)
                   .otherwise(CATALOG_COLLECTIONS.SUPERSEDED_AT))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)
                .and(CATALOG_COLLECTIONS.NAME.eq(name))
                // nexus-cecqy / nexus-0svvu / nexus-2sovp: LOAD-BEARING, do not weaken or remove this conjunct.
                // It is the one source of the DIFFERENT-target refusal: the loser of a concurrent supersede to
                // another target matches zero rows, and renameCollectionTxn's emptiness/identity asymmetry
                // (nexus-2sovp) depends on this CAS staying exactly this strict.
                .and(live.or(alreadyThis)))
            .execute();
    }

    /**
     * A row {@code name} just inserted with {@code superseded_by} left empty becomes the tombstone of
     * {@code supersededBy}: the name, and the successor's registry id, or its own {@code collection_id} when the
     * successor has no row (a backup or an ETL import can name one that never existed, R7). Two statements (insert
     * live, then this) because the successor may be inserted in the same batch, after this row, and a statement
     * cannot see its own sibling rows.
     */
    static void stampTombstone(DSLContext ctx, String tenant, String name, String supersededBy) {
        if (supersededBy == null || supersededBy.isEmpty()) return;
        var s = CATALOG_COLLECTIONS.as("succ");
        Field<Long> successor = DSL.field(DSL.select(s.COLLECTION_ID).from(s)
            .where(s.TENANT_ID.eq(tenant)).and(s.NAME.eq(supersededBy))
            .orderBy(CollectionIdReads.liveFirst(s)).limit(1));
        ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, DSL.coalesce(successor, CATALOG_COLLECTIONS.COLLECTION_ID))
            // the row just written (live); a tombstone that shares the label is not this one
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull())
            .execute();
    }

    /**
     * A registration of {@code names} in {@code tenant}: every tombstone whose {@code superseded_by} names one of them
     * takes that row's id, replacing the self id ("retired, no successor") a delete of the earlier row gave it (or a
     * stale one). Run in the registration's own transaction, after the insert, so the invariant (a row of the named
     * successor exists, so its id) holds at commit. A tombstone that already has the right id is not written.
     *
     * <p>Only a tombstone whose {@code superseded_by_id} is NULL or dangling (no LIVE registry row holds it) is re-pointed (linda
     * 2026-11-23): one whose id still names a live collection already has a true successor, whatever its text says, so a label that
     * passes to a new collection never steals the tombstones of the one it left.
     *
     * @return the number of tombstones re-pointed
     */
    static int repointTombstonesNaming(DSLContext ctx, String tenant, java.util.Collection<String> names) {
        if (names.isEmpty()) return 0;
        var s = CATALOG_COLLECTIONS.as("reg");
        Field<Long> registered = DSL.field(DSL.select(s.COLLECTION_ID).from(s)
            .where(s.TENANT_ID.eq(CATALOG_COLLECTIONS.TENANT_ID)).and(s.NAME.eq(CATALOG_COLLECTIONS.SUPERSEDED_BY))
            .orderBy(CollectionIdReads.liveFirst(s)).limit(1));  // live first, else the newest tombstone
        var held = CATALOG_COLLECTIONS.as("heldsucc");
        return ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, registered)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.in(names))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isDistinctFrom(registered))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull().or(DSL.notExists(DSL.selectOne().from(held)
                .where(held.TENANT_ID.eq(tenant)).and(held.COLLECTION_ID.eq(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID))
                .and(held.SUPERSEDED_BY.eq("")).and(held.SUPERSEDED_BY_ID.isNull()))))
            .execute();
    }

    /**
     * Re-points every tombstone of {@code tenant} that names {@code fromId} onto {@code toId}: {@code superseded_by_id} becomes
     * {@code toId} and {@code superseded_by} the registry's label of {@code toId}, in one statement that touches no other column or
     * row. For P3.7's one-row rename (which replaces {@link #repointTombstonesNaming}, kept until it goes). The column has no foreign
     * key, only the CHECK {@code (superseded_by = '') = (superseded_by_id IS NULL)}, which the statement keeps true, so the registry
     * row of {@code fromId} can be deleted before or after the call in one transaction.
     *
     * @return the number of tombstones re-pointed
     */
    static int repointTombstones(DSLContext ctx, String tenant, long fromId, long toId) {
        var s = CATALOG_COLLECTIONS.as("rpt");
        Field<String> label = DSL.field(DSL.select(s.NAME).from(s).where(s.TENANT_ID.eq(tenant)).and(s.COLLECTION_ID.eq(toId)));
        return ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, toId)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, label)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.eq(fromId))
            .execute();
    }

    /**
     * The parity check of the superseded_by invariant, for one tenant: the number of {@code catalog_collections} rows
     * that break it. The invariant: a live row has no {@code superseded_by_id}; a tombstone's {@code superseded_by_id}
     * is the id of the row its {@code superseded_by} names whenever such a row exists in the tenant, and its own
     * {@code collection_id} (retired, no successor) only when none does. Run in the tenant's scope. The stamp move's
     * verification reuses it; zero is the answer a stamped tenant gives.
     */
    public static int invariantViolations(DSLContext ctx, String tenant) {
        var s = CATALOG_COLLECTIONS.as("inv");
        Field<Long> successor = DSL.field(DSL.select(s.COLLECTION_ID).from(s)
            .where(s.TENANT_ID.eq(CATALOG_COLLECTIONS.TENANT_ID)).and(s.NAME.eq(CATALOG_COLLECTIONS.SUPERSEDED_BY))
            .orderBy(CollectionIdReads.liveFirst(s)).limit(1));
        return ctx.fetchCount(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID.eq(tenant).and(
            CATALOG_COLLECTIONS.SUPERSEDED_BY.eq("").and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNotNull())
            .or(CATALOG_COLLECTIONS.SUPERSEDED_BY.ne("").and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isDistinctFrom(
                DSL.coalesce(successor, CATALOG_COLLECTIONS.COLLECTION_ID))))));
    }

    /**
     * Delete registry rows, the successor-delete repair first, in the caller's transaction. Every surviving row that
     * names one of {@code names} as its successor, by id (post-stamp) or by name (pre-stamp), becomes "retired, no
     * successor": its {@code superseded_by_id} is its own {@code collection_id}. A row left naming a deleted id would
     * read as live once the name column is gone, and any foreign key on {@code superseded_by_id} (ON DELETE NO
     * ACTION, never SET NULL) would refuse the delete.
     *
     * <p>The rows of {@code names} are deleted in one statement, so a chain of tombstones that all go (A names B names
     * C) needs no leaf-first order. A tombstone that holds nothing is the caller's to list in {@code names}
     * (nexus-s80u9); one that holds content stays and is repaired here.
     *
     * @return the number of registry rows deleted
     */
    static int deleteRegistryRows(DSLContext ctx, String tenant, List<String> names, Gate3 gate3) {
        if (names.isEmpty()) return 0;
        var d = CATALOG_COLLECTIONS.as("gone");
        Field<Long> goneId = d.COLLECTION_ID;
        Condition byId = CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.in(
            DSL.select(goneId).from(d).where(d.TENANT_ID.eq(tenant)).and(d.NAME.in(names)));
        ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.NAME.notIn(names))
            .and(gate3.holds() ? byId : CATALOG_COLLECTIONS.SUPERSEDED_BY.in(names).or(byId))
            .execute();
        return ctx.deleteFrom(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.in(names))
            .execute();
    }

    /**
     * {@link #deleteRegistryRows} for one row named by id (RDR-229 P3.5a, delete by {@code collection_id}): the row with
     * {@code id} goes and no other row that happens to share its label, then the retired predecessors in {@code predecessors}
     * (empty tombstones of it, listed by name) go through {@link #deleteRegistryRows} first. A survivor that names the deleted
     * row, by its id or by its label, becomes "retired, no successor", as there.
     *
     * @return the number of registry rows deleted for {@code id}: 0 or 1, the predecessors not counted
     */
    static int deleteRegistryRowById(DSLContext ctx, String tenant, String name, long id, List<String> predecessors, Gate3 gate3) {
        deleteRegistryRows(ctx, tenant, predecessors, gate3);
        ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.COLLECTION_ID.ne(id))
            // from gate 3 a survivor names the deleted row by its id alone: a label can name two registry rows (linda 2026-11-23)
            .and(gate3.holds() ? CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.eq(id)
                : CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.eq(id).or(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(name)))
            .execute();
        return ctx.deleteFrom(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))
            .execute();
    }
}
