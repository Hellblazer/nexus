// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.jooq.nexus.tables.CatalogDocumentChunks;
import dev.nexus.service.jooq.nexus.tables.CatalogDocuments;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.DatePart;
import org.jooq.Field;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.time.OffsetDateTime;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * The engine forms of the guards the two manifest upgrade rungs share
 * ({@code nexus.upgrade_ladder.rungs.manifest_section_labels} and {@code manifest_span_fill}; the first module's
 * docstring is the rule of record), split out of {@link CatalogRepository} (which is at its file-size cap) so that
 * {@link CatalogRepository#manifestSectionLabelFillable} (the live count, which counts a chunk stamp it cannot read)
 * and {@link ManifestFillableCount} (the strict count) read one definition of each. Every method builds a typed jOOQ
 * condition over {@code CATALOG_DOCUMENT_CHUNKS}, {@code CATALOG_DOCUMENTS} and {@code CHUNKS}; the caller joins them.
 */
final class ManifestFillablePredicates {

    private ManifestFillablePredicates() {}

    /**
     * A chunk row's {@code indexed_at} text with an explicit offset ({@code Z}, {@code +05:30}, {@code -0800}).
     * PYTHON TWIN: {@code nexus.catalog.note_ttl.parse_iso} ({@code datetime.fromisoformat}), which the rung's scan
     * reads the stamp with; this is the subset of its forms that PostgreSQL reads the same way.
     */
    private static final String STAMP_WITH_OFFSET =
        "^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$";

    /**
     * A chunk row's {@code indexed_at} text with no offset: read as UTC, as {@code nexus.catalog.note_ttl.parse_iso} does.
     * PYTHON TWIN: the same function, see {@link #STAMP_WITH_OFFSET}.
     */
    private static final String STAMP_NAIVE =
        "^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?$";

    /**
     * The per-chunk suffix an indexer appends to a chunk row's title ({@code :12-30}, {@code :chunk-3}, {@code :page-2}).
     * PYTHON TWIN: {@code _CHUNK_TITLE_SUFFIX} in {@code nexus.upgrade_ladder.rungs.manifest_section_labels}.
     */
    private static final String CHUNK_TITLE_SUFFIX = ":(chunk-[0-9]+|[0-9]+-[0-9]+|page-[0-9]+)$";

    /**
     * Everything up to the last slash: what {@code os.path.basename} drops.
     * PYTHON TWIN: {@code os.path.basename} in {@code _title_names_document} of
     * {@code nexus.upgrade_ladder.rungs.manifest_section_labels}.
     */
    private static final String FILE_NAME_PREFIX = "^.*/";

    /**
     * How long after a document a chunk row may be stamped and still be its own.
     * PYTHON TWIN: {@code nexus.catalog.note_ttl.PUT_SKEW} (five minutes), applied in {@code _labels_from_chunk_row}
     * of {@code nexus.upgrade_ladder.rungs.manifest_section_labels}.
     */
    private static final int SECTION_LABEL_SKEW_SECONDS = 300;

    /**
     * The {@code metadata.backfill_from} value of a document the orphan backfill minted.
     * PYTHON TWIN: {@code ORPHAN_BACKFILL_MARKER} in {@code nexus.upgrade_ladder.rungs.manifest_section_labels}.
     */
    private static final String ORPHAN_BACKFILL_VALUE = "t3_orphan";

    /** {@code pg_input_is_valid(text, 'timestamptz')} as a typed condition: true when the cast would not raise. */
    private static Condition validTimestamptz(Field<String> text) {
        return DSL.condition(DSL.function(DSL.name("pg_input_is_valid"), SQLDataType.BOOLEAN, text, DSL.val("timestamptz")));
    }

    /** The chunk row's {@code metadata.indexed_at}, as text. */
    private static Field<String> chunkStamp() {
        return DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "indexed_at");
    }

    /**
     * The chunk stamp as an instant, NULL when it is not in a shape this reads. A stamp with no offset is read as UTC,
     * as {@code parse_iso} does. {@code pg_input_is_valid} (PostgreSQL 16+; the floor is 17) says whether the cast would
     * raise, so month 13 is 'cannot read' (NULL) and never an error for the whole tenant; CASE evaluates in order, so
     * the cast of an invalid stamp is not reached.
     */
    private static Field<OffsetDateTime> stampAt() {
        Field<String> stamp = chunkStamp();
        Field<String> naiveAsUtc = DSL.concat(stamp, DSL.inline("+00"));
        return DSL.when(stamp.likeRegex(STAMP_WITH_OFFSET).and(validTimestamptz(stamp)),
                DSL.cast(stamp, SQLDataType.TIMESTAMPWITHTIMEZONE))
            .when(stamp.likeRegex(STAMP_NAIVE).and(validTimestamptz(naiveAsUtc)),
                DSL.cast(naiveAsUtc, SQLDataType.TIMESTAMPWITHTIMEZONE));
    }

    /** chunk - document <= 5 minutes ({@code note_ttl.PUT_SKEW}), compared as epoch seconds. */
    private static Condition withinSkew(CatalogDocuments doc) {
        return DSL.extract(stampAt(), DatePart.EPOCH)
            .minus(DSL.extract(doc.INDEXED_AT, DatePart.EPOCH)).le(DSL.val(SECTION_LABEL_SKEW_SECONDS));
    }

    /**
     * The attribution guard, LENIENT form (the live count): both stamps exist and the chunk's is no more than five
     * minutes after the document's, where a chunk stamp this cannot read counts as passing. A count above what the
     * rung's scan would fill makes the rung run its scan once and record the completion; a count below it would hide a
     * fill, so the error there is never that way.
     */
    static Condition attributableLenient(CatalogDocuments doc) {
        Field<String> stamp = chunkStamp();
        return stamp.isNotNull().and(stamp.ne(""))
            .and(doc.INDEXED_AT.isNotNull())
            .and(stampAt().isNull().or(withinSkew(doc)));
    }

    /**
     * The attribution guard, STRICT form (the fill's count): the chunk stamp must be read AND within the skew. The live
     * count's lenient form is safe for a count and unsafe for a fill, so a count that must equal what a fill would
     * write uses this one: a stamp this cannot read is not counted.
     */
    static Condition attributableStrict(CatalogDocuments doc) {
        Field<String> stamp = chunkStamp();
        return stamp.isNotNull().and(stamp.ne(""))
            .and(doc.INDEXED_AT.isNotNull())
            .and(stampAt().isNotNull())
            .and(withinSkew(doc));
    }

    /**
     * {@code _title_names_document}: no title is not contradicted; else the title, the title without its per-chunk
     * suffix, or that one's file name, equals the document's title (with or without ITS per-chunk suffix: an ETL
     * import can title a document after one of its chunks, nexus-l8o33), file path or file name. Empty names do
     * not count.
     */
    static Condition namesDocument(CatalogDocuments doc) {
        Field<String> chunkTitle = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "title");
        Field<String> unsuffixed = DSL.regexpReplaceAll(chunkTitle, CHUNK_TITLE_SUFFIX, "");
        Field<String> unsuffixedName = DSL.regexpReplaceAll(unsuffixed, FILE_NAME_PREFIX, "");
        Field<String> ownTitle = DSL.nullif(doc.TITLE, "");
        Field<String> ownTitleUnsuffixed = DSL.nullif(DSL.regexpReplaceAll(doc.TITLE, CHUNK_TITLE_SUFFIX, ""), "");
        Field<String> ownPath = DSL.nullif(doc.FILE_PATH, "");
        Field<String> ownName = DSL.nullif(DSL.regexpReplaceAll(doc.FILE_PATH, FILE_NAME_PREFIX, ""), "");
        Condition names = chunkTitle.isNull().or(chunkTitle.eq(""));
        for (Field<String> candidate : List.of(chunkTitle, unsuffixed, unsuffixedName)) {
            names = names.or(candidate.eq(ownTitle)).or(candidate.eq(ownTitleUnsuffixed))
                .or(candidate.eq(ownPath)).or(candidate.eq(ownName));
        }
        return names;
    }

    /** The chunk row carries at least one of {@code section_title}, {@code section_type}, {@code page_number} (by key presence). */
    static Condition hasLabelKey() {
        // "metadata -> key IS NOT NULL" is key presence: a key whose value is JSON null reads as the jsonb value null, not SQL NULL.
        return DSL.jsonbGetAttribute(CHUNKS.METADATA, "section_title").isNotNull()
            .or(DSL.jsonbGetAttribute(CHUNKS.METADATA, "section_type").isNotNull())
            .or(DSL.jsonbGetAttribute(CHUNKS.METADATA, "page_number").isNotNull());
    }

    /** The document is live, not an alias, and not minted by the orphan backfill. */
    static Condition liveOwnDocument(CatalogDocuments doc) {
        return doc.DELETED_AT.isNull()
            .and(doc.ALIAS_OF.eq(""))
            .and(DSL.jsonbGetAttributeAsText(doc.METADATA, "backfill_from").isDistinctFrom(ORPHAN_BACKFILL_VALUE));
    }

    /**
     * Exactly one LIVE manifest row of the tenant holds this row's chash (this row), and no manifest row of a
     * TOMBSTONED document holds it. One arm names TOMBSTONED holders by design (a NOT EXISTS whose join requires
     * {@code DELETED_AT.isNotNull()}); the row's own document and the other-holder join are live only.
     */
    static Condition soleLiveOwner(DSLContext ctx, String tenant, CatalogDocumentChunks row) {
        var other = CATALOG_DOCUMENT_CHUNKS.as("other_holder");
        var otherDoc = CATALOG_DOCUMENTS.as("other_holder_doc");
        var gone = CATALOG_DOCUMENT_CHUNKS.as("deleted_holder");
        var goneDoc = CATALOG_DOCUMENTS.as("deleted_holder_doc");
        return DSL.notExists(ctx.selectOne().from(other)
                .join(otherDoc).on(otherDoc.TENANT_ID.eq(other.TENANT_ID)
                    .and(otherDoc.TUMBLER.eq(other.DOC_ID)).and(otherDoc.DELETED_AT.isNull()))
                .where(other.TENANT_ID.eq(tenant).and(other.CHASH.eq(row.CHASH))
                    .and(other.DOC_ID.ne(row.DOC_ID).or(other.POSITION.ne(row.POSITION)))))
            .and(DSL.notExists(ctx.selectOne().from(gone)
                .join(goneDoc).on(goneDoc.TENANT_ID.eq(gone.TENANT_ID)
                    .and(goneDoc.TUMBLER.eq(gone.DOC_ID)).and(goneDoc.DELETED_AT.isNotNull()))
                .where(gone.TENANT_ID.eq(tenant).and(gone.CHASH.eq(row.CHASH)))));
    }
}
