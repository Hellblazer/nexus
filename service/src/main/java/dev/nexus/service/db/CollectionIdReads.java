// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

import dev.nexus.service.datamove.Gate3;
import org.jooq.Condition;
import org.jooq.Field;
import org.jooq.impl.DSL;

import java.util.Collection;

/**
 * RDR-229 P3.5c (nexus-p2m1x.25), item 6, "Readers during the stamp": the one way the catalog readers filter, join and select on a
 * collection once gate 3 has latched on the install.
 *
 * <p><b>The rule.</b> Every method takes the {@link Gate3} and reads {@link Gate3#holds()} when it builds the clause, never caching
 * it. While gate 3 does not hold the clause is the name clause the reader always had. From gate 3 every row carries its id, so the
 * clause is the id's: a caller's collection <em>label</em> (the registry's {@code name}, which is what a route, a client or a
 * rename hands in) resolves to {@code collection_id} through the registry, and the other tables are read by that id. A rename
 * rewrites the registry row alone (P3.7), so a row whose own name column still holds the old label is found by the new one.
 *
 * <p><b>A NULL id is no collection</b> (linda 2026-11-20). An empty or null label, and a label the registry does not hold,
 * resolve to NULL, and {@code id = NULL} matches nothing; the id path never falls back to a {@code ''} name. A row whose id is NULL
 * (its name column held {@code ''}) keeps no label either: {@link #labelOf} gives {@code ''} for it, as the name column did.
 *
 * <p><b>Which registry row a label names.</b> The live one ({@code superseded_by_id IS NULL}) when the label has one, else the
 * newest tombstone that holds it ({@code ORDER BY (superseded_by_id IS NOT NULL), collection_id DESC}). After P3.6's live-label
 * index a label may be held by a tombstone beside a live row; the reader of that label means the live collection.
 * {@link #is} and {@link #in} agree on that one row per label.
 *
 * <p><b>The gate is the latched, monotone value</b> (linda 2026-11-23): once it holds on an install it never returns to false, so
 * a clause built from it does not have to survive a return to the names. It is still read on every call, never cached here, so
 * the switch needs no restart.
 */
final class CollectionIdReads {

    private CollectionIdReads() {
    }

    /**
     * A scalar subselect giving the registry id of {@code label} in {@code tenant}: the live row's, else the newest tombstone's; NULL
     * for {@code ''}, {@code null} or a label with no row. Uncorrelated, so the planner runs it once.
     */
    static Field<Long> idOfLabel(String tenant, String label) {
        if (label == null || label.isEmpty()) {
            return DSL.castNull(Long.class);
        }
        var c = CATALOG_COLLECTIONS.as("cir");
        return DSL.field(DSL.select(c.COLLECTION_ID).from(c)
            .where(c.TENANT_ID.eq(tenant).and(c.NAME.eq(label)))
            .orderBy(liveFirst(c))
            .limit(1));
    }

    /** The order that picks one registry row of a label: the live one, else the newest tombstone (use with {@code limit(1)}). */
    static java.util.List<org.jooq.OrderField<?>> liveFirst() {
        return java.util.List.copyOf(CollectionRegistry.labelResolutionOrder());   // the one definition (P3.5a); the alias form below mirrors it
    }

    /** {@link #liveFirst()} over an alias of the registry ({@link CollectionRegistry#labelResolutionOrder()} cannot take one; the two are the same order). Live means not superseded ({@code superseded_by = '' AND superseded_by_id IS NULL}), not {@code lifecycle_state}. */
    static java.util.List<org.jooq.OrderField<?>> liveFirst(dev.nexus.service.jooq.nexus.tables.CatalogCollections t) {
        return java.util.List.of(DSL.when(t.SUPERSEDED_BY.eq("").and(t.SUPERSEDED_BY_ID.isNull()), 1).otherwise(0).desc(), t.COLLECTION_ID.desc());
    }

    /** {@code nameCol = label} before gate 3; {@code idCol = <the registry id of label>} from gate 3. */
    static Condition is(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol, String label) {
        return gate3.holds() ? idCol.eq(idOfLabel(tenant, label)) : nameCol.eq(label);
    }

    /**
     * {@code nameCol IN labels} before gate 3; {@code idCol IN (the registry id of each label)} from gate 3, the one row per label
     * that {@link #is} resolves. An empty list matches nothing, either way.
     */
    static Condition in(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol, Collection<String> labels) {
        if (!gate3.holds()) {
            return nameCol.in(labels);
        }
        var c = CATALOG_COLLECTIONS.as("cir");
        var order = new java.util.ArrayList<org.jooq.OrderField<?>>();
        order.add(c.NAME.asc());
        order.addAll(liveFirst(c));
        return idCol.in(DSL.select(c.COLLECTION_ID).distinctOn(c.NAME).from(c)
            .where(c.TENANT_ID.eq(tenant).and(c.NAME.in(labels)))
            .orderBy(order));
    }

    /** The label a stored row shows: its name column before gate 3; the registry's current name of its id from gate 3 ({@code ''} for no id). */
    static Field<String> labelOf(Gate3 gate3, Field<String> tenantCol, Field<String> nameCol, Field<Long> idCol) {
        if (!gate3.holds()) {
            return nameCol;
        }
        var c = CATALOG_COLLECTIONS.as("cil");
        return DSL.coalesce(DSL.field(DSL.select(c.NAME).from(c)
            .where(c.TENANT_ID.eq(tenantCol).and(c.COLLECTION_ID.eq(idCol)))), DSL.inline(""));
    }

    /** Two tables name the same collection: {@code a.name = b.name} before gate 3; {@code a.id = b.id} from gate 3. */
    static Condition same(Gate3 gate3, Field<String> nameA, Field<Long> idA, Field<String> nameB, Field<Long> idB) {
        return gate3.holds() ? idA.eq(idB) : nameA.eq(nameB);
    }

    /**
     * A registry row is live: {@code superseded_by = ''} before gate 3; from gate 3 both columns, {@code superseded_by = '' AND
     * superseded_by_id IS NULL}, as {@link CollectionIds#live} tests them (a retired tombstone with no successor has the text of its dead
     * successor and its own id, so each column alone misses one kind of tombstone).
     */
    static Condition live(Gate3 gate3, Field<String> supersededBy, Field<Long> supersededById) {
        return gate3.holds() ? supersededBy.eq("").and(supersededById.isNull()) : supersededBy.eq("");
    }

    /** A registry row is a tombstone: the negation of {@link #live}. */
    static Condition tombstoned(Gate3 gate3, Field<String> supersededBy, Field<Long> supersededById) {
        return gate3.holds() ? supersededById.isNotNull() : supersededBy.ne("");
    }

    /**
     * The {@code superseded_by} a registry row shows on the wire (RDR-229 Q4: the Phase 3 wire stays byte-identical). Before gate 3
     * the text column. From gate 3 a tombstone whose successor still exists shows that successor's current label; a live row
     * ({@code ''}) and a retired tombstone with no successor (its {@code superseded_by_id} is its own {@code collection_id}) show the
     * stored text, exactly as before.
     */
    static Field<String> successor(Gate3 gate3) {
        var t = CATALOG_COLLECTIONS;
        if (!gate3.holds()) {
            return t.SUPERSEDED_BY;
        }
        var c = CATALOG_COLLECTIONS.as("cis");
        return DSL.when(t.SUPERSEDED_BY_ID.isNull().or(t.SUPERSEDED_BY_ID.eq(t.COLLECTION_ID)), t.SUPERSEDED_BY)
            .otherwise(DSL.coalesce(DSL.field(DSL.select(c.NAME).from(c)
                .where(c.TENANT_ID.eq(t.TENANT_ID).and(c.COLLECTION_ID.eq(t.SUPERSEDED_BY_ID)))), t.SUPERSEDED_BY));
    }

    /**
     * The conflict-target predicate of the registry's label upserts (G15, linda 2026-11-23): {@code ON CONFLICT (tenant_id, name)
     * WHERE superseded_by = '' AND superseded_by_id IS NULL} infers the live-label partial unique index (catalog-053-1, and
     * P4.1's re-creation over {@code superseded_by_id IS NULL}), so a label held only by a tombstone does not conflict: the write
     * inserts a new live row. The {@code ''} is inlined, not bound: PostgreSQL proves the index predicate from this one at parse
     * time and cannot do it through a parameter. Until P4.1 drops the name primary key, that key is an arbiter beside it, and a
     * tombstone's label still conflicts there.
     */
    static Condition liveLabelConflict() {
        return CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(DSL.inline("")).and(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID.isNull());
    }

    /**
     * A history table's rows of the collection {@code label} ({@code relevance_log}, {@code search_telemetry}, {@code hook_failures},
     * {@code gc_audit}): their id column is nullable and no stamp fills it on an old row (p31-checks kind HISTORY). Before gate 3 the
     * name column. From gate 3 (linda's history ruling): the row's id; and a row whose collection is gone (an id no live registry row
     * holds, its name the label) so a deleted collection's audit trail still lists by its name. A NULL id never resolves to a
     * collection: those rows are {@link #historyPreId}'s. An empty or null label matches nothing.
     */
    static Condition historyIs(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol, String label) {
        if (!gate3.holds()) {
            return nameCol.eq(label);
        }
        if (label == null || label.isEmpty()) {
            return DSL.falseCondition();
        }
        var c = CATALOG_COLLECTIONS.as("cihx");
        Condition noLiveHolder = DSL.notExists(DSL.selectOne().from(c)
            .where(c.TENANT_ID.eq(tenant).and(c.COLLECTION_ID.eq(idCol)).and(c.SUPERSEDED_BY_ID.isNull())));
        return idCol.eq(idOfLabel(tenant, label)).or(idCol.isNotNull().and(nameCol.eq(label)).and(noLiveHolder));
    }

    /**
     * The rows a history surface shows apart, marked: those with no id ({@code NULL}) written under {@code label}. From gate 3 only;
     * they are pre-id history, recorded under that label, which may belong to a collection since renamed (linda's history ruling).
     * Nothing attributes them to the label's current holder and nothing moves, counts or deletes by them.
     */
    static Condition historyPreId(Gate3 gate3, Field<String> nameCol, Field<Long> idCol, String label) {
        return !gate3.holds() || label == null || label.isEmpty() ? DSL.falseCondition() : idCol.isNull().and(nameCol.eq(label));
    }

    /** A history row's collection as a label: the registry's current name of its id, else its own name column. */
    static Field<String> historyLabel(Gate3 gate3, Field<String> tenantCol, Field<String> nameCol, Field<Long> idCol) {
        if (!gate3.holds()) {
            return nameCol;
        }
        var c = CATALOG_COLLECTIONS.as("cih");
        return DSL.coalesce(DSL.field(DSL.select(c.NAME).from(c).where(c.TENANT_ID.eq(tenantCol).and(c.COLLECTION_ID.eq(idCol)))), nameCol);
    }

    // ---- the three tables CatalogRepository reads most, spelled once ----------------------------------------------------

    /** {@code catalog_documents.physical_collection} as a label. */
    static Field<String> docLabel(Gate3 gate3) {
        return labelOf(gate3, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID);
    }

    /** {@code catalog_documents} rows of the collection {@code label}. */
    static Condition docIs(Gate3 gate3, String tenant, String label) {
        return is(gate3, tenant, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, label);
    }

    /** {@code catalog_documents} rows of any of the collections {@code labels}. */
    static Condition docIn(Gate3 gate3, String tenant, Collection<String> labels) {
        return in(gate3, tenant, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, labels);
    }

    /** {@code catalog_document_chunks.collection} as a label. */
    static Field<String> manifestLabel(Gate3 gate3) {
        return labelOf(gate3, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID);
    }

    /** {@code catalog_document_chunks} rows of the collection {@code label}. */
    static Condition manifestIs(Gate3 gate3, String tenant, String label) {
        return is(gate3, tenant, CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, label);
    }

    /** {@code catalog_document_chunks} rows of any of the collections {@code labels}. */
    static Condition manifestIn(Gate3 gate3, String tenant, Collection<String> labels) {
        return in(gate3, tenant, CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, labels);
    }

    /** {@code chunks} rows of the collection {@code label}. */
    static Condition chunksIs(Gate3 gate3, String tenant, String label) {
        return is(gate3, tenant, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID, label);
    }

    /** {@code chunks} rows of any of the collections {@code labels}. */
    static Condition chunksIn(Gate3 gate3, String tenant, Collection<String> labels) {
        return in(gate3, tenant, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID, labels);
    }

    /** {@code chunks.collection} as a label. */
    static Field<String> chunksLabel(Gate3 gate3) {
        return labelOf(gate3, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.COLLECTION_ID);
    }
}
