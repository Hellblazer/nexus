// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.impl.DSL;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;

/**
 * RDR-229 P3.1 stage 2b (nexus-p2m1x.17), catalog family: the id half of the dual write that the catalog writers
 * share. {@link CollectionIds} resolves {@code (tenant, name)} to the registry's id for a statement that names a
 * collection; this class covers the two shapes it does not.
 *
 * <p><b>An UPDATE that names no collection column.</b> A NOT VALID CHECK is evaluated on every updated row version,
 * so a row the stamp move has not reached (name set, id NULL) fails any UPDATE of it that leaves the id alone. Every
 * such UPDATE adds {@code .set(T.<ID>, CatalogIdStamps.keepXxxId())}: {@code COALESCE(<id>, <registry id of the
 * row's own name>)}. A stamped row keeps its id (the COALESCE never reaches the subselect), an unstamped row is
 * stamped by the write that touched it, and a row with no collection ({@code ''}) stays NULL. The expression
 * names the target table's own columns, so it carries no bind parameter and is safe in a JDBC batch template.
 *
 * <p><b>Rows a foreign key moved.</b> {@code nexus.chunks.collection} is the parent of three ON UPDATE CASCADE keys
 * (the manifest, the topic assignments and {@code chunk_orphaned_at}), so a rename or a re-home that updates the chunk
 * rows renames the children's {@code collection} without touching their id columns.
 * {@link #repairCascadedIds} gives two of them the target's id afterwards. The manifest's UPDATE is written inline in
 * the three gated, retried methods that need it ({@code renameCollectionTxn}, {@code rehomeCollection},
 * {@code ChashRepository.renameCollection}): {@code ManifestInsertGateTest} and {@code ManifestWriteRetryGateTest} pin
 * every writer of that table to a method that takes the sweep gate and runs under the deadlock retry.
 */
final class CatalogIdStamps {

    private CatalogIdStamps() {}

    /** {@code COALESCE(id, <registry id of (tenant, name)>)}, for the target row's own columns. */
    static Field<Long> keep(Field<Long> id, Field<String> tenant, Field<String> name) {
        var c = CATALOG_COLLECTIONS.as("kid");
        Field<Long> own = DSL.field(DSL.select(c.COLLECTION_ID).from(c)
            .where(c.TENANT_ID.eq(tenant)).and(c.NAME.eq(DSL.nullif(name, DSL.inline(""))))
            .orderBy(CollectionIdReads.liveFirst(c)).limit(DSL.inline(1)));  // live first, else the newest tombstone
        return DSL.coalesce(id, own);
    }

    /**
     * True when {@code tumbler} is a live (not tombstoned) document of {@code tenant}. An update resolves the target
     * collection's id only after this answers true: an update of an unknown or tombstoned tumbler stays a no-op (0 rows)
     * whatever collection it names, as it was before the dual write, never the typed 422 for an unregistered one.
     */
    static boolean liveDocumentExists(DSLContext ctx, String tenant, String tumbler) {
        return ctx.fetchExists(ctx.selectOne().from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(tenant)).and(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler))
            .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()));
    }

    /**
     * {@link CollectionIds#require} with a per-call memo, for a batch that names the same collections many times. The
     * memo lives in the caller's transaction, so no id outlives it.
     */
    static Long requireMemo(DSLContext ctx, String tenant, String name, java.util.Map<String, Long> memo) {
        if (name == null || name.isEmpty()) return null;
        Long id = memo.get(name);
        if (id == null) {
            id = CollectionIds.require(ctx, tenant, name);
            memo.put(name, id);
        }
        return id;
    }

    /** {@code catalog_documents.physical_collection_id} for an UPDATE that does not name {@code physical_collection}. */
    static Field<Long> keepDocumentId() {
        return keep(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, CATALOG_DOCUMENTS.TENANT_ID,
            CATALOG_DOCUMENTS.PHYSICAL_COLLECTION);
    }

    /** {@code document_aspects.collection_id} for an UPDATE that does not name {@code collection}. */
    static Field<Long> keepAspectId() {
        return keep(DOCUMENT_ASPECTS.COLLECTION_ID, DOCUMENT_ASPECTS.TENANT_ID, DOCUMENT_ASPECTS.COLLECTION);
    }

    /** {@code document_highlights.collection_id} for an UPDATE that does not name {@code collection}. */
    static Field<Long> keepHighlightId() {
        return keep(DOCUMENT_HIGHLIGHTS.COLLECTION_ID, DOCUMENT_HIGHLIGHTS.TENANT_ID, DOCUMENT_HIGHLIGHTS.COLLECTION);
    }

    /**
     * {@code catalog_collections.superseded_by_id} for an UPDATE that does not name {@code superseded_by}: NULL for a
     * live row; for a tombstone its id as it stands, else the successor's registry id, else the row's own
     * {@code collection_id} (residual R7: a tombstone whose successor is gone reads "retired, no successor").
     */
    static Field<Long> keepSupersededId() {
        var s = CATALOG_COLLECTIONS.as("sid");
        Field<Long> successor = DSL.field(DSL.select(s.COLLECTION_ID).from(s)
            .where(s.TENANT_ID.eq(CATALOG_COLLECTIONS.TENANT_ID)).and(s.NAME.eq(CATALOG_COLLECTIONS.SUPERSEDED_BY))
            .orderBy(CollectionIdReads.liveFirst(s)).limit(DSL.inline(1)));   // a label may name a live row and tombstones: live first, else the newest
        return DSL.when(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(DSL.inline("")), DSL.castNull(Long.class))
            .otherwise(DSL.coalesce(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, successor, CATALOG_COLLECTIONS.COLLECTION_ID));
    }

    /**
     * After the chunk rows of a collection were re-filed under {@code collection} (id {@code id}): the rows the
     * chunk foreign keys carried (the topic assignments and {@code chunk_orphaned_at}; the manifest is the caller's, see
     * the class comment) already name it and still carry the id they had. Gives them {@code id}; a row that already has
     * it is not written.
     *
     * <p>Run for the whole of {@code collection} on purpose. A rename's target held nothing before, and a re-home's
     * target rows already carry its id, so the {@code IS DISTINCT FROM} predicate selects exactly the rows that moved.
     */
    static void repairCascadedIds(DSLContext ctx, String tenant, String collection, Long id) {
        ctx.update(TOPIC_ASSIGNMENTS).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, id)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.eq(collection))
            .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.isDistinctFrom(id)).execute();
        ctx.update(CHUNK_ORPHANED_AT).set(CHUNK_ORPHANED_AT.COLLECTION_ID, id)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant)).and(CHUNK_ORPHANED_AT.COLLECTION.eq(collection))
            .and(CHUNK_ORPHANED_AT.COLLECTION_ID.isDistinctFrom(id)).execute();
    }

    /**
     * {@link #repairCascadedIds} for a re-home from gate 3, keyed by id: the topic assignments and {@code chunk_orphaned_at}
     * rows of the source's id whose chunk now carries {@code targetId} (the chunk update moved it, and the foreign key carried
     * the row with it), given {@code targetId}. The name arm of the form above is wrong after a one-row rename (the rows can
     * hold a stored name no label has), and the id alone is too wide: the rows of a chunk the re-home left behind (a collision)
     * also hold the source's id and must keep it, so the chunk's own id is the test.
     */
    static void repairCascadedIdsFrom(DSLContext ctx, String tenant, Long sourceId, Long targetId, String targetLabel) {
        // The rows whose chunk MOVED (it is at the target and no longer at the source), given the target's label and id: the foreign keys that
        // cascade the label (topic_assignments_chunk_fk) stay until P4.1, and a row they did not carry keeps the source's label.
        var c = CHUNKS.as("rcc");
        var gone = CHUNKS.as("rcg");
        ctx.update(TOPIC_ASSIGNMENTS).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, targetId).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, targetLabel)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(tenant)).and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID.eq(sourceId))
            .and(DSL.exists(DSL.selectOne().from(c).where(c.TENANT_ID.eq(tenant))
                .and(c.CHASH.eq(TOPIC_ASSIGNMENTS.DOC_ID)).and(c.COLLECTION_ID.eq(targetId))))
            .and(DSL.notExists(DSL.selectOne().from(gone).where(gone.TENANT_ID.eq(tenant))
                .and(gone.CHASH.eq(TOPIC_ASSIGNMENTS.DOC_ID)).and(gone.COLLECTION_ID.eq(sourceId))))
            .execute();
        var o = CHUNKS.as("rco");
        var ogone = CHUNKS.as("rcq");
        ctx.update(CHUNK_ORPHANED_AT).set(CHUNK_ORPHANED_AT.COLLECTION_ID, targetId).set(CHUNK_ORPHANED_AT.COLLECTION, targetLabel)
            .where(CHUNK_ORPHANED_AT.TENANT_ID.eq(tenant)).and(CHUNK_ORPHANED_AT.COLLECTION_ID.eq(sourceId))
            .and(DSL.exists(DSL.selectOne().from(o).where(o.TENANT_ID.eq(tenant))
                .and(o.CHASH.eq(CHUNK_ORPHANED_AT.CHASH)).and(o.COLLECTION_ID.eq(targetId))))
            .and(DSL.notExists(DSL.selectOne().from(ogone).where(ogone.TENANT_ID.eq(tenant))
                .and(ogone.CHASH.eq(CHUNK_ORPHANED_AT.CHASH)).and(ogone.COLLECTION_ID.eq(sourceId))))
            .execute();
    }
}
