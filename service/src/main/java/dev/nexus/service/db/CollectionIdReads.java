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
 * tombstone that holds it. After P3.6's live-label index a label may be held by a tombstone beside a live row; the reader of that
 * label means the live collection.
 */
final class CollectionIdReads {

    private CollectionIdReads() {
    }

    /**
     * A scalar subselect giving the registry id of {@code label} in {@code tenant}: the live row's, else a tombstone's; NULL for
     * {@code ''}, {@code null} or a label with no row. Uncorrelated, so the planner runs it once.
     */
    static Field<Long> idOfLabel(String tenant, String label) {
        if (label == null || label.isEmpty()) {
            return DSL.castNull(Long.class);
        }
        var c = CATALOG_COLLECTIONS.as("cir");
        return DSL.field(DSL.select(c.COLLECTION_ID).from(c)
            .where(c.TENANT_ID.eq(tenant).and(c.NAME.eq(label)))
            .orderBy(c.SUPERSEDED_BY_ID.asc().nullsFirst())
            .limit(1));
    }

    /** {@code nameCol = label} before gate 3; {@code idCol = <the registry id of label>} from gate 3. */
    static Condition is(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol, String label) {
        return gate3.holds() ? idCol.eq(idOfLabel(tenant, label)) : nameCol.eq(label);
    }

    /**
     * {@code nameCol IN labels} before gate 3; {@code idCol IN (the registry ids of labels)} from gate 3 (every row holding one of
     * the labels, live or tombstone). An empty list matches nothing, either way.
     */
    static Condition in(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol, Collection<String> labels) {
        if (!gate3.holds()) {
            return nameCol.in(labels);
        }
        var c = CATALOG_COLLECTIONS.as("cir");
        return idCol.in(DSL.select(c.COLLECTION_ID).from(c)
            .where(c.TENANT_ID.eq(tenant).and(c.NAME.in(labels))));
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

    /** A registry row is live: {@code superseded_by = ''} before gate 3; {@code superseded_by_id IS NULL} from gate 3. */
    static Condition live(Gate3 gate3, Field<String> supersededBy, Field<Long> supersededById) {
        return gate3.holds() ? supersededById.isNull() : supersededBy.eq("");
    }

    /** A registry row is a tombstone: the negation of {@link #live}. */
    static Condition tombstoned(Gate3 gate3, Field<String> supersededBy, Field<Long> supersededById) {
        return gate3.holds() ? supersededById.isNotNull() : supersededBy.ne("");
    }

    /**
     * The successor a tombstone names: its {@code superseded_by} before gate 3; the registry's current name of its
     * {@code superseded_by_id} from gate 3 ({@code ''} for a live row).
     */
    static Field<String> successorLabel(Gate3 gate3, Field<String> tenantCol, Field<String> supersededBy, Field<Long> supersededById) {
        return labelOf(gate3, tenantCol, supersededBy, supersededById);
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
     * name column; from gate 3 the row's id, or, for a row with no id, its name column. An empty or null label matches nothing.
     */
    static Condition historyIs(Gate3 gate3, String tenant, Field<String> nameCol, Field<Long> idCol, String label) {
        if (!gate3.holds()) {
            return nameCol.eq(label);
        }
        if (label == null || label.isEmpty()) {
            return DSL.falseCondition();
        }
        return idCol.eq(idOfLabel(tenant, label)).or(idCol.isNull().and(nameCol.eq(label)));
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
