// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.DSLContext;
import org.jooq.Record;
import org.jooq.Row3;
import org.jooq.Row14;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;

/**
 * The manifest REPLACE of one document as a diff (nexus-z0o2p.29, "ship now, recover cost later"). The replace
 * used to DELETE every row of the document and INSERT the new set; the orphaning-record triggers
 * (vectors-061) then stamped every chunk the DELETE dropped and cleared it again on the INSERT, so a re-index
 * that kept every chunk still wrote and deleted one record per chunk. This leaves a row alone when the new set
 * holds the same row, and writes only the difference: DELETE the positions the new set lacks, UPDATE the rows
 * whose payload changed, INSERT the new positions.
 *
 * <p>The row key is the table's primary key, {@code (tenant_id, doc_id, position)}. A row's payload is every
 * other column the replace wrote, {@link #PAYLOAD_COLUMNS}: chash, chunk_index, the four span columns, collection,
 * collection_id (RDR-229 P3.1), embedding_model, the three section labels and labels_written. An UPDATE writes every payload column from the
 * incoming row, NULLs included, exactly as the insert did, so a kept row comes out what a delete and re-insert
 * would have left. {@code ManifestReplaceDiffTest} fails when the table gains or loses a column this class does
 * not list, and changes each payload column of a row to check the diff writes it.
 *
 * <p>The orphan records end as the old path left them: the DELETE and UPDATE statements stamp what they drop (the
 * vectors-061 triggers), and then the record of every chunk key the new set lists is cleared, kept rows included,
 * which is what the old INSERT's trigger did for every row it wrote.
 *
 * <p>Locks: the document's rows are read {@code FOR UPDATE} (the DELETE of the old path locked the same rows),
 * then the chunk rows of every row about to be deleted or changed are locked in key order in ONE pass
 * ({@code FOR NO KEY UPDATE}, the lock the stamp triggers take), before any statement that fires a trigger, so
 * chunk-then-record order holds across the statements as it did across the old path's one DELETE. Nothing is
 * locked that the old path did not lock.
 */
final class ManifestReplace {

    private ManifestReplace() {
    }

    /**
     * More changed (deleted or updated) rows than this fall back to the delete-and-insert. A cost bound, not a
     * correctness one: past about this many the diff's UPDATE of every row costs what the delete-and-insert
     * does (measured: 4000 changed rows 0.6 s against 1.1 s; 5000 about equal), and the chunk pre-lock and the
     * UPDATE's VALUES list grow with the count.
     */
    static final int MAX_CHANGED_ROWS = 4000;

    /** The payload columns of a manifest row: every column of the table except its primary key. */
    static final java.util.Set<String> PAYLOAD_COLUMNS = java.util.Set.of("chash", "chunk_index", "line_start",
        "line_end", "char_start", "char_end", "collection", "collection_id", "embedding_model", "section_title",
        "section_type", "page_number", "labels_written");

    /** Test hook: when set, {@link #replace} declines every replace, so a test can run the old path in one JVM. */
    static volatile boolean declineForTests;

    /** Test hook: a positive value replaces {@link #MAX_CHANGED_ROWS}, so a small document can reach the cap. */
    static volatile int maxChangedRowsForTests;

    /** One incoming row, as {@code insertManifestChunkRows} would write it. */
    private record Incoming(int position, String chashHex, int chunkIndex, Integer lineStart, Integer lineEnd,
                            Integer charStart, Integer charEnd, String sectionTitle, String sectionType,
                            Integer pageNumber, boolean labelsWritten) {
    }

    /** One stored row of the document. */
    private record Stored(int position, byte[] chash, Integer chunkIndex, Integer lineStart, Integer lineEnd,
                          Integer charStart, Integer charEnd, String collection, Long collectionId, String embeddingModel,
                          String sectionTitle, String sectionType, Integer pageNumber, boolean labelsWritten) {
        String hex() {
            return HexFormat.of().formatHex(chash);
        }
    }

    /**
     * Replaces the document's manifest with {@code rows} by writing the difference.
     *
     * @return the rows the document held before, as the departed-writer span logic reads them, or {@code null}
     *         when the diff does not apply and nothing was written (a duplicate or missing position, a blank
     *         chash, or more changes than {@link #MAX_CHANGED_ROWS}): the caller then runs the delete and
     *         insert, which refuses a bad row exactly as it always did
     */
    static List<DepartedWriterSpans.Gone> replace(DSLContext ctx, String tenant, String docId, String collection,
                                                  List<Map<String, Object>> rows) {
        if (declineForTests) return null;
        List<Incoming> incoming = new ArrayList<>(rows.size());
        Map<Integer, Incoming> byPosition = new HashMap<>(Math.max(16, rows.size() * 2));
        for (Map<String, Object> row : rows) {
            Integer position = CatalogRepository.i(row, "position");
            String chash = CatalogRepository.s(row, "chash");
            if (position == null || chash == null || chash.isBlank()) return null;
            Incoming in = new Incoming(position, chash.toLowerCase(java.util.Locale.ROOT),
                CatalogRepository.ni(CatalogRepository.i(row, "chunk_index"), position),
                CatalogRepository.i(row, "line_start"), CatalogRepository.i(row, "line_end"),
                CatalogRepository.i(row, "char_start"), CatalogRepository.i(row, "char_end"),
                CatalogRepository.s(row, "section_title"), CatalogRepository.s(row, "section_type"),
                CatalogRepository.i(row, "page_number"), CatalogRepository.labelsWritten(row));
            if (byPosition.put(position, in) != null) return null;   // a duplicate position fails the insert's key
            incoming.add(in);
        }
        // The collection's model, and the superseded-collection refusal the insert helper makes: asked even when
        // every row is kept, so a retired collection still refuses the write.
        final String embeddingModel = rows.isEmpty() ? null
            : CollectionRegistry.requireWritable(ctx, tenant, collection).embeddingModel();
        // RDR-229 P3.1: the registry's id, read in this transaction; a stored row that lacks it (the stamp has not
        // reached it) differs from the incoming one and is rewritten with it.
        final Long collectionId = rows.isEmpty() ? null : CollectionIds.require(ctx, tenant, collection);

        List<Stored> stored = new ArrayList<>();
        for (Record r : ctx.select(CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                    CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, CATALOG_DOCUMENT_CHUNKS.LINE_START,
                    CATALOG_DOCUMENT_CHUNKS.LINE_END, CATALOG_DOCUMENT_CHUNKS.CHAR_START,
                    CATALOG_DOCUMENT_CHUNKS.CHAR_END, CATALOG_DOCUMENT_CHUNKS.COLLECTION,
                    CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE,
                    CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER,
                    CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)
                .from(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId)))
                .orderBy(CATALOG_DOCUMENT_CHUNKS.POSITION)
                .forUpdate()
                .fetch()) {
            stored.add(new Stored(r.get(CATALOG_DOCUMENT_CHUNKS.POSITION), r.get(CATALOG_DOCUMENT_CHUNKS.CHASH),
                r.get(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX), r.get(CATALOG_DOCUMENT_CHUNKS.LINE_START),
                r.get(CATALOG_DOCUMENT_CHUNKS.LINE_END), r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_START),
                r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_END), r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION),
                r.get(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID), r.get(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL), r.get(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE),
                r.get(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE), r.get(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER),
                r.get(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)));
        }

        List<Stored> toDelete = new ArrayList<>();
        List<Incoming> toUpdate = new ArrayList<>();
        List<Stored> updatedFrom = new ArrayList<>();
        Set<Integer> storedPositions = new HashSet<>();
        for (Stored old : stored) {
            storedPositions.add(old.position());
            Incoming in = byPosition.get(old.position());
            if (in == null) {
                toDelete.add(old);
            } else if (!same(old, in, collection, collectionId, embeddingModel)) {
                toUpdate.add(in);
                updatedFrom.add(old);
            }
        }
        int cap = maxChangedRowsForTests > 0 ? maxChangedRowsForTests : MAX_CHANGED_ROWS;
        if (toDelete.size() + toUpdate.size() > cap) return null;
        List<Map<String, Object>> toInsert = new ArrayList<>();
        for (int k = 0; k < rows.size(); k++) {
            if (!storedPositions.contains(incoming.get(k).position())) toInsert.add(rows.get(k));
        }

        List<DepartedWriterSpans.Gone> before = new ArrayList<>(stored.size());
        for (Stored old : stored) {
            before.add(new DepartedWriterSpans.Gone(old.collection(), old.chash(), old.lineStart(), old.lineEnd(),
                old.charStart(), old.charEnd()));
        }

        // The chunk rows of every row about to be deleted or rewritten, locked in key order in one pass, before
        // the first statement that fires a trigger (which would lock them again, already held).
        if (!toDelete.isEmpty() || !toUpdate.isEmpty()) {
            List<Row3<String, String, byte[]>> keys = new ArrayList<>();
            Set<String> seen = new HashSet<>();
            for (Stored old : concat(toDelete, updatedFrom)) {
                if (seen.add(old.collection() + "\u0000" + old.hex())) {
                    keys.add(DSL.row(tenant, old.collection(), old.chash()));
                }
            }
            ctx.selectOne().from(CHUNKS)
                .where(DSL.row(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH).in(keys))
                .orderBy(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH)
                .forNoKeyUpdate()
                .fetch();
        }

        if (!toDelete.isEmpty()) {
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS)
                .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId)))
                .and(CATALOG_DOCUMENT_CHUNKS.POSITION.in(toDelete.stream().map(Stored::position).toList()))
                .execute();
        }
        if (!toUpdate.isEmpty()) {
            update(ctx, tenant, docId, collection, collectionId, embeddingModel, toUpdate);
        }
        if (!toInsert.isEmpty()) {
            CatalogRepository.insertManifestChunkRows(ctx, tenant, docId, collection, toInsert,
                CatalogRepository.ManifestInsertMode.PLAIN);
        }

        // The record of every chunk key the new set lists, in this document's collection: what the old path's INSERT
        // trigger did for every row it wrote. A key a DELETE or UPDATE statement above stamped and the new set still
        // lists (the stamp never reads the manifest), and a record another document left on a chunk this one keeps,
        // go here. The records are locked in key order first, as the clear function does, and deleted only if any
        // exist. The record's own key is (tenant, collection, chash); the chunk's model is the collection's, so the
        // stamp's fourth column never differs from the clear's three.
        if (!incoming.isEmpty()) {
            List<Row3<String, String, byte[]>> listed = new ArrayList<>();
            Set<String> seen = new HashSet<>();
            for (Incoming in : incoming) {
                if (seen.add(in.chashHex())) listed.add(DSL.row(tenant, collection, HexFormat.of().parseHex(in.chashHex())));
            }
            clearRecords(ctx, listed);
        }
        return before;
    }

    private static boolean same(Stored old, Incoming in, String collection, Long collectionId, String embeddingModel) {
        return old.hex().equals(in.chashHex())
            && Objects.equals(old.chunkIndex(), in.chunkIndex())
            && Objects.equals(old.lineStart(), in.lineStart()) && Objects.equals(old.lineEnd(), in.lineEnd())
            && Objects.equals(old.charStart(), in.charStart()) && Objects.equals(old.charEnd(), in.charEnd())
            && Objects.equals(old.collection(), collection) && Objects.equals(old.collectionId(), collectionId)
            && Objects.equals(old.embeddingModel(), embeddingModel)
            && Objects.equals(old.sectionTitle(), in.sectionTitle())
            && Objects.equals(old.sectionType(), in.sectionType())
            && Objects.equals(old.pageNumber(), in.pageNumber())
            && old.labelsWritten() == in.labelsWritten();
    }

    private static <T> List<T> concat(List<T> a, List<T> b) {
        List<T> out = new ArrayList<>(a.size() + b.size());
        out.addAll(a);
        out.addAll(b);
        return out;
    }

    /** One UPDATE ... FROM (VALUES ...): every payload column from the incoming row, so one statement-level trigger run. */
    private static void update(DSLContext ctx, String tenant, String docId, String collection, Long collectionId,
                               String embeddingModel, List<Incoming> changed) {
        @SuppressWarnings("unchecked")
        Row14<Integer, byte[], Integer, Integer, Integer, Integer, Integer, String, Long, String, String, String,
            Integer, Boolean>[] values = changed.stream().map(in -> DSL.row(
                DSL.val(in.position(), SQLDataType.INTEGER).cast(SQLDataType.INTEGER),
                DSL.val(HexFormat.of().parseHex(in.chashHex()), SQLDataType.BLOB).cast(SQLDataType.BLOB),
                DSL.val(in.chunkIndex(), SQLDataType.INTEGER).cast(SQLDataType.INTEGER),
                DSL.val(in.lineStart(), SQLDataType.INTEGER).cast(SQLDataType.INTEGER),
                DSL.val(in.lineEnd(), SQLDataType.INTEGER).cast(SQLDataType.INTEGER),
                DSL.val(in.charStart(), SQLDataType.INTEGER).cast(SQLDataType.INTEGER),
                DSL.val(in.charEnd(), SQLDataType.INTEGER).cast(SQLDataType.INTEGER),
                DSL.val(collection, SQLDataType.VARCHAR).cast(SQLDataType.VARCHAR),
                DSL.val(collectionId, SQLDataType.BIGINT).cast(SQLDataType.BIGINT),
                DSL.val(embeddingModel, SQLDataType.VARCHAR).cast(SQLDataType.VARCHAR),
                DSL.val(in.sectionTitle(), SQLDataType.VARCHAR).cast(SQLDataType.VARCHAR),
                DSL.val(in.sectionType(), SQLDataType.VARCHAR).cast(SQLDataType.VARCHAR),
                DSL.val(in.pageNumber(), SQLDataType.INTEGER).cast(SQLDataType.INTEGER),
                DSL.val(in.labelsWritten(), SQLDataType.BOOLEAN).cast(SQLDataType.BOOLEAN)))
            .toArray(Row14[]::new);
        var v = DSL.values(values).asTable("v", "position", "chash", "chunk_index", "line_start", "line_end",
            "char_start", "char_end", "collection", "collection_id", "embedding_model", "section_title",
            "section_type", "page_number", "labels_written");
        ctx.update(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.CHASH, v.field("chash", byte[].class))
            .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, v.field("chunk_index", Integer.class))
            .set(CATALOG_DOCUMENT_CHUNKS.LINE_START, v.field("line_start", Integer.class))
            .set(CATALOG_DOCUMENT_CHUNKS.LINE_END, v.field("line_end", Integer.class))
            .set(CATALOG_DOCUMENT_CHUNKS.CHAR_START, v.field("char_start", Integer.class))
            .set(CATALOG_DOCUMENT_CHUNKS.CHAR_END, v.field("char_end", Integer.class))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, v.field("collection", String.class))
            .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, v.field("collection_id", Long.class))
            .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, v.field("embedding_model", String.class))
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, v.field("section_title", String.class))
            .set(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE, v.field("section_type", String.class))
            .set(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, v.field("page_number", Integer.class))
            .set(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN, v.field("labels_written", Boolean.class))
            .from(v)
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(docId))
                .and(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(v.field("position", Integer.class))))
            .execute();
    }

    /** Deletes the orphaning records of {@code keys}, the rows locked first in key order (the stamp upserts' order). */
    private static void clearRecords(DSLContext ctx, List<Row3<String, String, byte[]>> keys) {
        var key = DSL.row(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH);
        var locked = ctx.selectOne().from(CHUNK_ORPHANED_AT).where(key.in(keys))
            .orderBy(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH)
            .forUpdate()
            .fetch();
        if (!locked.isEmpty()) ctx.deleteFrom(CHUNK_ORPHANED_AT).where(key.in(keys)).execute();
    }
}
