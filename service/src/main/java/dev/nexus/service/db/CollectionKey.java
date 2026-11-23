// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;

import java.util.Collection;
import java.util.List;
import java.util.Objects;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the one way a Java reader names a collection by its name column or, once gate 3 has
 * latched on the install, by its id column. A reader builds the predicate, the grouping key or the output label here
 * instead of writing {@code T.COLLECTION.eq(name)}; before gate 3 each method returns exactly the name expression the
 * reader had, so the name path is unchanged.
 *
 * <p><b>The gate is the latched value.</b> {@link Gate3#holds()} is the persisted, monotone gate 3 of the install (linda
 * 2026-11-23): once true it stays true, so a reader never goes back to a name after it has read an id. It is read once per
 * instance by {@link #of(Gate3)}. A method that uses a key in more than one clause of one statement (a GROUP BY and the select
 * list, a count and its HAVING) takes one instance and uses it for the whole statement, so the clauses cannot disagree.
 *
 * <p><b>A NULL id is no collection.</b> The id arm compares the id column with the registry's id of the name. A name that
 * is {@code ''}, {@code null} or unregistered resolves to NULL, {@code id = NULL} matches nothing, and a row whose id is NULL
 * matches nothing: neither falls back to the {@code ''} name (linda 2026-11-20).
 *
 * <p><b>Tenant.</b> The id of a name is read with an explicit {@code tenant_id} (an uncorrelated subselect, one planner
 * InitPlan), so the id predicate stays an index condition on the table's id key. A label used twice in one SELECT DISTINCT (the
 * select list and its ORDER BY) is selected under an alias and ordered by the alias: two renderings of the subselect bind two
 * parameters, and PostgreSQL does not match them.
 */
public final class CollectionKey {

    private static final CollectionKey NAME = new CollectionKey(false);
    private static final CollectionKey ID = new CollectionKey(true);

    private final boolean byId;

    private CollectionKey(boolean byId) {
        this.byId = byId;
    }

    /** The reader's key for this call: by id when gate 3 holds now, by name otherwise. Reads the gate once, here. */
    public static CollectionKey of(Gate3 gate3) {
        return Objects.requireNonNull(gate3, "gate3").holds() ? ID : NAME;
    }

    /** Whether this instance reads the id. */
    public boolean byId() {
        return byId;
    }

    /** {@code name = value} before gate 3; {@code id = registry id of value} after (matches nothing for an unresolved value). */
    public Condition eq(String tenant, Field<String> name, Field<Long> id, String value) {
        return byId ? id.eq(CollectionIds.idOf(tenant, value)) : name.eq(value);
    }

    /**
     * A HISTORY table's rows written under {@code value} with NO id (relevance_log, search_telemetry, hook_failures, gc_audit rows a
     * stamp never reached): {@code id IS NULL AND name = value}. They are pre-id label history, never attributed to the collection
     * {@code value} names today (a label can pass to another collection), so a read reports them separately and never folds them
     * into a collection's figures. An empty or null {@code value} matches nothing.
     */
    public Condition preId(Field<String> name, Field<Long> id, String value) {
        if (value == null || value.isEmpty()) {
            return DSL.falseCondition();
        }
        return id.isNull().and(name.eq(value));
    }

    /**
     * A ledger table's read by label (no FK to the registry; a row may outlive its registry row, as a pdf_pipeline row does when the
     * client deletes the registry row first): {@code name = value} before gate 3; after, {@code id = registry id of value OR (name =
     * value AND no LIVE registry row holds the row's id)}, so a row whose collection is gone is still found by the name it recorded,
     * and a label that has passed to another live collection never reaches the old rows.
     *
     * <p>The name arm only ever sees a dangling id, never a NULL one (linda 2026-11-23): chash_remap (source and target), pdf_pipeline and
     * reembed_jobs (source and target) are all {@code NAME_ID} in {@code db/rdr229/p31-checks.tsv} (rows 28 to 32: the check is
     * {@code (NULLIF(name,'') IS NULL) = (id IS NULL)}), and gate 3 latches only with those checks validated, so from the latch no row of
     * these tables has a NULL id beside a non-empty name. A row the name arm reaches carries an id no LIVE registry row holds.
     */
    public Condition eqLedger(String tenant, Field<String> name, Field<Long> id, String value) {
        if (!byId) {
            return name.eq(value);
        }
        var c = CATALOG_COLLECTIONS.as("ckled");
        return id.eq(CollectionIds.idOf(tenant, value)).or(name.eq(value).andNotExists(DSL.selectOne().from(c)
            .where(c.TENANT_ID.eq(tenant)).and(c.COLLECTION_ID.eq(id)).and(CollectionIds.live(c))));
    }

    /** {@code name IN (values)} before gate 3; {@code id IN (registry ids of values)} after. */
    public Condition in(String tenant, Field<String> name, Field<Long> id, Collection<String> values) {
        if (!byId) {
            return name.in(values);
        }
        var c = CATALOG_COLLECTIONS.as("ckin");
        // each label names its preferred row (live first, else the newest tombstone: CollectionIds.idOf), never two
        return id.in(DSL.select(c.COLLECTION_ID).from(c).where(c.TENANT_ID.eq(tenant)).and(c.NAME.in(values))
            .and(c.COLLECTION_ID.eq(CollectionIds.idOf(c.TENANT_ID, c.NAME))));
    }

    /** {@code name LIKE prefix%} before gate 3; the ids of the registry labels that start with {@code prefix} after. */
    public Condition startsWith(String tenant, Field<String> name, Field<Long> id, String prefix) {
        if (!byId) {
            return name.startsWith(prefix);
        }
        var c = CATALOG_COLLECTIONS.as("ckpre");
        // each label names its preferred row, as in(): live first, else the newest tombstone
        return id.in(DSL.select(c.COLLECTION_ID).from(c).where(c.TENANT_ID.eq(tenant)).and(c.NAME.startsWith(prefix))
            .and(c.COLLECTION_ID.eq(CollectionIds.idOf(c.TENANT_ID, c.NAME))));
    }

    /** The column that identifies the collection in a grouping, a DISTINCT or an ordering: the id after gate 3, the name before. */
    public Field<?> key(Field<String> name, Field<Long> id) {
        return byId ? id : name;
    }

    /** The row has a collection: {@code name <> ''} before gate 3 (and not NULL), {@code id IS NOT NULL} after. */
    public Condition present(Field<String> name, Field<Long> id) {
        return byId ? id.isNotNull() : name.isNotNull().and(name.ne(""));
    }

    /**
     * The collection's label as an output column: the name column before gate 3; after, the registry's CURRENT label of the
     * row's id (a one-row rename leaves the row's own name column stale), NULL for a row with no id. The expression is not aliased: a reader selects it
     * and reads the same Field back, and aliases it ({@code .as("collection")}) only where a name read is wanted.
     */
    public Field<String> label(String tenant, Field<String> name, Field<Long> id) {
        return label(DSL.val(tenant), name, id);
    }

    /**
     * As {@link #label(String, Field, Field)} for a statement that reads rows of every tenant: the registry row is the one of the
     * row's own tenant. {@code tenant} must be qualified when the row's table is plain SQL and the subselect's own columns
     * share its name (an unqualified {@code tenant_id} would bind to the registry's).
     */
    public Field<String> label(Field<String> tenant, Field<String> name, Field<Long> id) {
        if (!byId) {
            return name;
        }
        var c = CATALOG_COLLECTIONS.as("cklab");
        return DSL.field(DSL.select(c.NAME).from(c).where(c.TENANT_ID.eq(tenant)).and(c.COLLECTION_ID.eq(id)));
    }

    /**
     * The name to write into a name column when a statement copies a row (INSERT ... SELECT): the registry's current label of the
     * row's id after gate 3, the row's own name before it, and the row's own name for a row with no id (NULL or {@code ''}), so the
     * copy satisfies the P3.1 name/id CHECK exactly as its source did.
     */
    public Field<String> copyName(String tenant, Field<String> name, Field<Long> id) {
        return byId ? DSL.when(id.isNull(), name).otherwise(label(tenant, name, id)) : name;
    }

    /** Two rows name the same collection: {@code a.name = b.name} before gate 3, {@code a.id = b.id} after (NULL ids never match). */
    public Condition same(Field<String> aName, Field<Long> aId, Field<String> bName, Field<Long> bId) {
        return byId ? aId.eq(bId) : aName.eq(bName);
    }

    /** The registry row is a tombstone: a non-empty {@code superseded_by} before gate 3, a {@code superseded_by_id} after. */
    public Condition retired(Field<String> supersededBy, Field<Long> supersededById) {
        return byId ? supersededById.isNotNull() : DSL.coalesce(supersededBy, "").ne("");
    }

    /** {@code name <> value} before gate 3; after, true also for a row whose id is NULL or that names another collection (NULL-safe, unlike {@code eq(..).not()}). */
    public Condition ne(String tenant, Field<String> name, Field<Long> id, String value) {
        return byId ? id.isNull().or(id.isDistinctFrom(CollectionIds.idOf(tenant, value))) : name.ne(value);
    }

    /** Two rows name different collections: {@code a.name <> b.name} before gate 3, a NULL-safe id difference after. */
    public Condition differ(Field<String> aName, Field<Long> aId, Field<String> bName, Field<Long> bId) {
        return byId ? aId.isDistinctFrom(bId) : aName.ne(bName);
    }

    // ---- the FK-less ledgers (chash_remap, reembed_jobs): history, linda 2026-11-23 ------------------------------------

    private static Condition noRegistryRow(String tenant, Field<Long> id) {
        var c = CATALOG_COLLECTIONS.as("ckgone");
        // a LIVE registry row: a tombstone (superseded) is a collection that no longer exists under its label (linda 2026-11-23)
        return DSL.notExists(DSL.selectOne().from(c).where(c.TENANT_ID.eq(tenant)).and(c.COLLECTION_ID.eq(id))
            .and(c.SUPERSEDED_BY.eq("")).and(c.SUPERSEDED_BY_ID.isNull()));
    }

    /**
     * A caller's value {@code v} against a ledger row that carries both a stored name and an id but no registry foreign key: {@code
     * id = idOf(v)}, or {@code name = v} when no LIVE registry row has the row's id (the collection is gone or retired to a tombstone, and its stored name is its
     * last label). The id arm finds a renamed collection under its new label; the name arm reaches only gone history, so a stale name
     * never matches a different live collection that now holds the label. Before gate 3: {@code name = v}.
     *
     * <p>The name arm only ever sees a dangling id, never a NULL one (linda 2026-11-23): chash_remap (source and target), pdf_pipeline and
     * reembed_jobs (source and target) are all {@code NAME_ID} in {@code db/rdr229/p31-checks.tsv} (rows 28 to 32: the check is
     * {@code (NULLIF(name,'') IS NULL) = (id IS NULL)}), and gate 3 latches only with those checks validated, so from the latch no row of
     * these tables has a NULL id beside a non-empty name. A row the name arm reaches carries an id no LIVE registry row holds.
     */
    public Condition eqHistory(String tenant, Field<String> name, Field<Long> id, String value) {
        return byId ? id.eq(CollectionIds.idOf(tenant, value)).or(name.eq(value).and(noRegistryRow(tenant, id))) : name.eq(value);
    }

    /** A ledger row's collection as an output: the live registry label of its id, else its stored name (its last label); the name before gate 3. */
    public Field<String> labelOrName(String tenant, Field<String> name, Field<Long> id) {
        if (!byId) {
            return name;
        }
        var c = CATALOG_COLLECTIONS.as("ckll");
        // the LIVE row's name only: a tombstone's own name is the stored last label, which the row already carries
        return DSL.coalesce(DSL.field(DSL.select(c.NAME).from(c).where(c.TENANT_ID.eq(DSL.val(tenant))).and(c.COLLECTION_ID.eq(id))
            .and(c.SUPERSEDED_BY.eq("")).and(c.SUPERSEDED_BY_ID.isNull())), name);
    }

    /** True when no LIVE registry row has the ledger row's id (the collection no longer exists, or is a tombstone); false before gate 3, where nothing is read by id. */
    public Field<Boolean> gone(String tenant, Field<Long> id) {
        return byId ? DSL.field(noRegistryRow(tenant, id)) : DSL.inline(false);
    }

    /**
     * The distinct labels of the collections a table holds rows for, in label order: {@code DISTINCT collection_id} first, then the
     * registry label of each id once (a join), never a correlated label per row. Before gate 3 it is the caller's name query, so
     * this is for the id arm only. A NULL id is no collection and is left out.
     *
     * <p>Choose this when an id no registry row holds (a dangling id) is NOT a collection to list: the join drops it. A caller that must see
     * every collection its rows name, a dangling one included (the reaper's pass, chash-reconcile's ghost detection), takes
     * {@link #distinctLabelsKeepingDangling} instead.
     */
    public static List<String> distinctLabels(DSLContext ctx, String tenant, Table<?> table, Condition tenantRows, Field<Long> id) {
        var ids = ctx.selectDistinct(id.as("cid")).from(table).where(tenantRows).and(id.isNotNull()).asTable("ckids");
        var c = CATALOG_COLLECTIONS.as("ckdl");
        return ctx.select(c.NAME).from(ids).join(c)
            .on(c.TENANT_ID.eq(tenant).and(c.COLLECTION_ID.eq(ids.field("cid", Long.class))))
            .orderBy(c.NAME).fetch(c.NAME);
    }

    /**
     * The distinct labels of every collection id a table holds rows for, in label order, a dangling id included (linda 2026-11-23): each id is
     * named by the registry row's current name (by id, no live filter), or, for an id no registry row holds, by the stored name of that id's rows
     * ({@code min(storedName)}, one per id). One registry read per id (GROUP BY), never per row. A NULL id is no collection and is left out; two ids
     * that share a label list it once.
     *
     * <p>Choose this when a collection whose registry row is gone must still be listed, so a ghost shows up to the caller that looks for it:
     * the reaper's {@code collectionsWithChunks} and {@code ChashRepository#distinctCollections}. {@link #distinctLabels} drops such an id.
     */
    public static List<String> distinctLabelsKeepingDangling(DSLContext ctx, String tenant, Table<?> table, Condition tenantRows,
                                                              Field<Long> id, Field<String> storedName) {
        var c = CATALOG_COLLECTIONS.as("ckdk");
        Field<String> label = DSL.coalesce(
            DSL.field(DSL.select(c.NAME).from(c).where(c.TENANT_ID.eq(tenant)).and(c.COLLECTION_ID.eq(id))),
            DSL.min(storedName)).as("label");
        return ctx.select(label).from(table).where(tenantRows).and(id.isNotNull()).groupBy(id).orderBy(label)
            .fetch(label).stream().distinct().toList();
    }

    /** The jOOQ fields of {@code table} named by {@code columns} (a key column list of ArbiterTwins, as TwinKeyUpsert.Pair carries it), in order. */
    public static Field<?>[] fields(Table<?> table, List<String> columns) {
        return columns.stream().map(n -> Objects.requireNonNull(table.field(n), n + " is not a column of " + table)).toArray(Field<?>[]::new);
    }
}
