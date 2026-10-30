// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.Field;
import org.jooq.Table;
import org.jooq.impl.DSL;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * How the engine labels a chunk for a human (RDR-236 Phase 3, bead nexus-6pfgb.39): the first
 * {@value #TEXT_PREFIX_CHARS} characters of its text, and the catalog document its MANIFEST names as its owner,
 * never the chunk row's own {@code title}, {@code source_path}, {@code catalog_doc_id} or {@code doc_id}. A row is
 * shared by every document holding the same text, so those keys are last-writer-wins, and the Phase 4 sweep
 * removes them.
 *
 * <p>The owner is one {@code nexus.catalog_document_chunks} row for the chunk's tenant, chash and model joined to
 * its {@code nexus.catalog_documents} row. A chunk with an own-collection manifest row is not reapable, so for the
 * chunks the reaper lists the owner is usually a manifest row of another collection, or none: then {@code
 * owner_doc_id}, {@code owner_title} and {@code owner_file_path} are all null and the text prefix is the label. When
 * several documents name the chunk the own-collection row wins, then the lowest {@code doc_id}, so the pick does not
 * vary between calls. The SQL form of the same rule is in {@code vectors-040-gc-audit-labels-by-text-and-manifest.xml}.
 */
final class ChunkOwnerLabel {

    /** Characters of chunk text shown as the label. */
    static final int TEXT_PREFIX_CHARS = 80;

    static final String ALIAS = "owner";

    static final Field<String> DOC_ID = DSL.field(DSL.name(ALIAS, "doc_id"), String.class);
    static final Field<String> TITLE = DSL.field(DSL.name(ALIAS, "title"), String.class);
    static final Field<String> FILE_PATH = DSL.field(DSL.name(ALIAS, "file_path"), String.class);

    private ChunkOwnerLabel() {}

    /** {@code left(chunk_text, 80)}. */
    static Field<String> textPrefix() {
        return DSL.left(CHUNKS.CHUNK_TEXT, TEXT_PREFIX_CHARS);
    }

    /**
     * A lateral subquery, aliased {@value #ALIAS}, of at most one row per {@code nexus.chunks} row of the enclosing
     * query. Join it with {@code leftJoin(ownerLateral()).on(DSL.trueCondition())}.
     */
    static Table<?> ownerLateral() {
        var m = CATALOG_DOCUMENT_CHUNKS;
        var d = CATALOG_DOCUMENTS;
        return DSL.lateral(
            DSL.select(m.DOC_ID.as("doc_id"), d.TITLE.as("title"), d.FILE_PATH.as("file_path"))
               .from(m)
               .join(d).on(d.TENANT_ID.eq(m.TENANT_ID).and(d.TUMBLER.eq(m.DOC_ID)))
               .where(m.TENANT_ID.eq(CHUNKS.TENANT_ID))
               .and(m.CHASH.eq(CHUNKS.CHASH))
               .and(m.EMBEDDING_MODEL.eq(CHUNKS.EMBEDDING_MODEL))
               .orderBy(DSL.case_().when(m.COLLECTION.eq(CHUNKS.COLLECTION), 0).otherwise(1), m.DOC_ID)
               .limit(1)).as(ALIAS);
    }

    /** An empty string is no value: the catalog's {@code file_path} is '' for a note. */
    static String blankToNull(String s) {
        return s == null || s.isEmpty() ? null : s;
    }
}
