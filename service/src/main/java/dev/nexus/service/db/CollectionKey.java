// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.util.Collection;
import java.util.Objects;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25): the one way a Java reader names a collection by its name column or, once gate 3 has
 * latched on the install, by its id column. A reader builds the predicate, the grouping key or the output label here
 * instead of writing {@code T.COLLECTION.eq(name)}; before gate 3 each method returns exactly the name expression the
 * reader had, so the name path is unchanged.
 *
 * <p><b>One reading of gate 3 per instance.</b> {@link #of(Gate3)} reads {@link Gate3#holds()} once and the instance keeps
 * that answer, so a query that uses {@link #key} in a GROUP BY and {@link #label} in its select list cannot see the gate
 * flip between the two (a flip would leave a selected label ungrouped). A reader takes a fresh instance per call, so the
 * gate is never cached past one call (Gate3's contract).
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

    /** {@code name IN (values)} before gate 3; {@code id IN (registry ids of values)} after. */
    public Condition in(String tenant, Field<String> name, Field<Long> id, Collection<String> values) {
        if (!byId) {
            return name.in(values);
        }
        var c = CATALOG_COLLECTIONS.as("ckin");
        return id.in(DSL.select(c.COLLECTION_ID).from(c).where(c.TENANT_ID.eq(tenant)).and(c.NAME.in(values)));
    }

    /** {@code name LIKE prefix%} before gate 3; the ids of the registry labels that start with {@code prefix} after. */
    public Condition startsWith(String tenant, Field<String> name, Field<Long> id, String prefix) {
        if (!byId) {
            return name.startsWith(prefix);
        }
        var c = CATALOG_COLLECTIONS.as("ckpre");
        return id.in(DSL.select(c.COLLECTION_ID).from(c).where(c.TENANT_ID.eq(tenant)).and(c.NAME.startsWith(prefix)));
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
        if (!byId) {
            return name;
        }
        var c = CATALOG_COLLECTIONS.as("cklab");
        return DSL.field(DSL.select(c.NAME).from(c).where(c.TENANT_ID.eq(DSL.val(tenant))).and(c.COLLECTION_ID.eq(id)));
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
}
