// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
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
    static int supersede(DSLContext ctx, String tenant, String name, String supersededBy, Field<OffsetDateTime> stampedAt) {
        return supersede(ctx, tenant, name, supersededBy, stampedAt, Gate3.never());
    }

    /** {@link #supersede(DSLContext, String, String, String, Field)} reading the id from gate 3 (RDR-229 P3.5c). */
    static int supersede(DSLContext ctx, String tenant, String name, String supersededBy, Field<OffsetDateTime> stampedAt,
                         Gate3 gate3) {
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
    static int supersede(DSLContext ctx, String tenant, String fromName, String toName, long toId) {
        return supersede(ctx, tenant, fromName, toName, toId, Gate3.never());
    }

    /** {@link #supersede(DSLContext, String, String, String, long)} reading the id from gate 3 (RDR-229 P3.5c). */
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
            .where(s.TENANT_ID.eq(tenant)).and(s.NAME.eq(supersededBy)));
        ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, supersededBy)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, DSL.coalesce(successor, CATALOG_COLLECTIONS.COLLECTION_ID))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .execute();
    }

    /**
     * A registration of {@code names} in {@code tenant}: every tombstone whose {@code superseded_by} names one of them
     * takes that row's id, replacing the self id ("retired, no successor") a delete of the earlier row gave it (or a
     * stale one). Run in the registration's own transaction, after the insert, so the invariant (a row of the named
     * successor exists, so its id) holds at commit. A tombstone that already has the right id is not written.
     *
     * @return the number of tombstones re-pointed
     */
    static int repointTombstonesNaming(DSLContext ctx, String tenant, java.util.Collection<String> names) {
        if (names.isEmpty()) return 0;
        var s = CATALOG_COLLECTIONS.as("reg");
        Field<Long> registered = DSL.field(DSL.select(s.COLLECTION_ID).from(s)
            .where(s.TENANT_ID.eq(CATALOG_COLLECTIONS.TENANT_ID)).and(s.NAME.eq(CATALOG_COLLECTIONS.SUPERSEDED_BY))
            .orderBy(s.SUPERSEDED_BY_ID.asc().nullsFirst()).limit(1));  // the live row of a label (a tombstone may share it, G15)
        return ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, registered)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.SUPERSEDED_BY.in(names))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isDistinctFrom(registered))
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
            .orderBy(s.SUPERSEDED_BY_ID.asc().nullsFirst()).limit(1));
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
    static int deleteRegistryRows(DSLContext ctx, String tenant, List<String> names) {
        return deleteRegistryRows(ctx, tenant, names, Gate3.never());
    }

    /** {@link #deleteRegistryRows(DSLContext, String, List)}; from gate 3 a survivor is found by the successor's id alone (RDR-229 P3.5c). */
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
    static int deleteRegistryRowById(DSLContext ctx, String tenant, String name, long id, List<String> predecessors) {
        deleteRegistryRows(ctx, tenant, predecessors);
        ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.COLLECTION_ID.ne(id))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.eq(id).or(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(name)))
            .execute();
        return ctx.deleteFrom(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(id))
            .execute();
    }
}
