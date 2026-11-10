// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Select;
import org.jooq.Record1;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;

/**
 * nexus-f3yqd: the strict, bounded count behind {@code GET /v1/catalog/manifest/fillable?kind=labels|spans&limit=N}:
 * how many manifest rows the two upgrade rungs (manifest section labels, manifest spans) would fill, in one tenant-scoped
 * read-only statement in place of each rung's client-side walk and by-id chunk reads (T2
 * {@code nexus/rung-detect-route-spec-2026-11-10}, sections 2 to 4, route R1).
 *
 * <p>A row counts when it passes the guards both rungs share ({@link ManifestFillablePredicates}: a live, non-alias,
 * non-orphan-minted document; the one live holder of the chash with no tombstoned holder; a chunk row at the manifest
 * row's own collection and embedding model; the chunk's {@code indexed_at} READ and within five minutes of the
 * document's; the chunk's title names the document) AND the guard of its kind:
 * <ul>
 *   <li>{@link Kind#LABELS}: {@code labels_written} is false, the chunk carries a label key, and every label value it
 *       carries is one the client sends as a plain string or integer (see {@link #labelValuesAreScalar});</li>
 *   <li>{@link Kind#SPANS}: the chunk RECORDS a value (the Python {@code manifest_span} rule, by value) for at least
 *       one span column the manifest row holds NULL, and contradicts none of the columns the row holds
 *       (see {@link #spansFillable}).</li>
 * </ul>
 *
 * <p>BEHAVIOUR POINT, against {@link CatalogRepository#manifestSectionLabelFillable}: this count is STRICT about the
 * chunk stamp. The older count counts a stamp it cannot read, which is right for a count that only decides whether to
 * run a scan; a count that must equal what a fill writes must not count a row the fill would refuse. The rows where
 * the two differ are stamps Python's {@code fromisoformat} reads and the engine's patterns do not (the basic format,
 * an hour-only stamp); producers write {@code isoformat()}, so the expected number of them is about zero.
 *
 * <p>Where SQL cannot say what Python says, the row is left out and not guessed at (the spec's section 6): a label
 * value that is not a string, null or integer (the client sends it as found and the engine coerces it), and a span
 * or page value of more than nine digits (it would not fit the column's int4; the client would send it and the write
 * would fail).
 *
 * <p>BOUNDS: as {@link CatalogSensitiveReads}, the same {@code statement_timeout} and the same test hook; a count that
 * outlasts it is SQLSTATE {@link CatalogRepository#QUERY_CANCELED} and the route answers 503. The count is
 * {@code SELECT count(*) FROM (SELECT 1 ... LIMIT :limit)}, so the converged case (zero) scans every remaining
 * candidate and is still one statement.
 */
public final class ManifestFillableCount {

    /** The most rows one call counts; the route answers 400 above it. Same bound as the section-label count. */
    public static final int MAX_LIMIT = CatalogRepository.FILLABLE_COUNT_MAX_LIMIT;

    /** The limit a call without one uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /** The title rule the count applies ({@code _title_names_document}, nexus-l8o33), echoed so a client can tell it from an older rule. */
    public static final String RULE = "title rule 2";

    /** The chunk-stamp form the count applies: unreadable is not counted. */
    public static final String STAMP = "strict";

    /** Which rung's rows are counted. */
    public enum Kind {
        LABELS("labels"), SPANS("spans");

        private final String wire;

        Kind(String wire) {
            this.wire = wire;
        }

        public String wire() {
            return wire;
        }

        /** The kind named on the wire (exact, lower case), or null. */
        public static Kind parse(String wire) {
            for (Kind k : values()) if (k.wire.equals(wire)) return k;
            return null;
        }
    }

    /** The answer: {@code capped} means {@code fillable} stopped at {@code limit}, so it is "at least". */
    public record Result(Kind kind, int fillable, int limit, boolean capped) {}

    private final CatalogRepository repo;

    public ManifestFillableCount(CatalogRepository repo) {
        this.repo = repo;
    }

    // ── the predicates of one kind ──────────────────────────────────────────

    private static Field<String> metaText(String key) {
        return DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, key);
    }

    private static Field<String> metaType(String key) {
        return DSL.function("jsonb_typeof", SQLDataType.VARCHAR, DSL.jsonbGetAttribute(CHUNKS.METADATA, key));
    }

    /** The key holds a JSON integer of at most nine digits (a number written without fraction or exponent; not a string, bool or null). */
    private static Condition smallInt(String key, String regex) {
        // A NULL type (an absent key) makes the AND NULL, which no WHERE admits.
        return metaType(key).eq("number").and(metaText(key).likeRegex(regex));
    }

    /** A JSON integer above zero ({@code manifest_span} records it), at most nine digits: it fits int4. */
    private static Condition positiveInt(String key) {
        return smallInt(key, "^[1-9][0-9]{0,8}$");
    }

    /** A JSON integer of either sign, at most nine digits. */
    private static Condition anyInt(String key) {
        return smallInt(key, "^-?[0-9]{1,9}$");
    }

    private static Field<Integer> asInt(String key) {
        return DSL.cast(metaText(key), SQLDataType.INTEGER);
    }

    /**
     * Labels: the value of each label key the chunk carries is one the client sends as a plain scalar. A string or a JSON
     * null is a title or type (null is the empty label); an absent key is the empty label; {@code page_number} is a
     * JSON integer, a JSON null (no page) or absent. Anything else (a number or object where a title goes, a string or
     * fraction where a page goes) is left alone: the client sends it as found and the engine coerces it, which SQL does
     * not guess at (bucket {@code label_value_not_scalar}).
     */
    private static Condition labelValuesAreScalar() {
        Condition title = metaType("section_title").isNull().or(metaType("section_title").in("string", "null"));
        Condition type = metaType("section_type").isNull().or(metaType("section_type").in("string", "null"));
        Condition page = metaType("page_number").isNull().or(metaType("page_number").eq("null"))
            .or(anyInt("page_number"));
        return title.and(type).and(page);
    }

    private static Condition labelsFillable(dev.nexus.service.jooq.nexus.tables.CatalogDocumentChunks row) {
        return row.LABELS_WRITTEN.isFalse()
            .and(ManifestFillablePredicates.hasLabelKey())
            .and(labelValuesAreScalar());
    }

    /**
     * Spans: the value the chunk RECORDS for each span column ({@code manifest_span}'s rule, by value: a line is recorded
     * when it is a positive integer, the char end likewise, and the char start when it is a positive integer or an
     * integer beside a recorded end; 0 is "not recorded" otherwise). The row is fillable when it holds NULL in at
     * least one column the chunk records, and no column holds a value the chunk contradicts (a chunk row that
     * describes another position is not trusted for any column).
     */
    private static Condition spansFillable(dev.nexus.service.jooq.nexus.tables.CatalogDocumentChunks row) {
        Field<Integer> recLineStart = DSL.when(positiveInt("line_start"), asInt("line_start"));
        Field<Integer> recLineEnd = DSL.when(positiveInt("line_end"), asInt("line_end"));
        Field<Integer> recCharEnd = DSL.when(positiveInt("chunk_end_char"), asInt("chunk_end_char"));
        Field<Integer> recCharStart = DSL.when(
            positiveInt("chunk_start_char").or(anyInt("chunk_start_char").and(positiveInt("chunk_end_char"))),
            asInt("chunk_start_char"));
        Condition fills = row.LINE_START.isNull().and(recLineStart.isNotNull())
            .or(row.LINE_END.isNull().and(recLineEnd.isNotNull()))
            .or(row.CHAR_START.isNull().and(recCharStart.isNotNull()))
            .or(row.CHAR_END.isNull().and(recCharEnd.isNotNull()));
        // Each term is a conjunction with both sides NOT NULL, so it is TRUE or FALSE, never NULL, and the NOT is safe.
        Condition disagrees = row.LINE_START.isNotNull().and(recLineStart.isNotNull()).and(row.LINE_START.ne(recLineStart))
            .or(row.LINE_END.isNotNull().and(recLineEnd.isNotNull()).and(row.LINE_END.ne(recLineEnd)))
            .or(row.CHAR_START.isNotNull().and(recCharStart.isNotNull()).and(row.CHAR_START.ne(recCharStart)))
            .or(row.CHAR_END.isNotNull().and(recCharEnd.isNotNull()).and(row.CHAR_END.ne(recCharEnd)));
        return fills.and(DSL.not(disagrees));
    }

    /**
     * The statement: one row per fillable manifest row of {@code tenant}, at most {@code limit}. Public so a plan-shape
     * test can explain the exact statement the route runs.
     */
    public static Select<Record1<Integer>> query(DSLContext ctx, String tenant, Kind kind, int limit) {
        var row = CATALOG_DOCUMENT_CHUNKS;
        var doc = CATALOG_DOCUMENTS;
        return ctx.selectOne()
            .from(row)
            .join(doc).on(doc.TENANT_ID.eq(row.TENANT_ID).and(doc.TUMBLER.eq(row.DOC_ID)))
            .join(CHUNKS).on(CHUNKS.TENANT_ID.eq(row.TENANT_ID)
                .and(CHUNKS.EMBEDDING_MODEL.eq(row.EMBEDDING_MODEL))
                .and(CHUNKS.COLLECTION.eq(row.COLLECTION)).and(CHUNKS.CHASH.eq(row.CHASH)))
            .where(row.TENANT_ID.eq(tenant)
                .and(CHUNKS.TENANT_ID.eq(tenant))
                .and(row.CHASH.isNotNull())
                .and(ManifestFillablePredicates.liveOwnDocument(doc))
                .and(ManifestFillablePredicates.attributableStrict(doc))
                .and(ManifestFillablePredicates.namesDocument(doc))
                .and(kind == Kind.LABELS ? labelsFillable(row) : spansFillable(row))
                .and(ManifestFillablePredicates.soleLiveOwner(ctx, tenant, row)))
            .limit(limit);
    }

    /**
     * @param limit stops the scan at this many matches (1 to {@link #MAX_LIMIT}); a result equal to {@code limit} means
     *              "at least that many"
     * @throws IllegalArgumentException when {@code limit} is outside that range
     */
    public Result count(String tenant, Kind kind, int limit) {
        if (limit < 1 || limit > MAX_LIMIT) {
            throw new IllegalArgumentException("limit must be 1.." + MAX_LIMIT);
        }
        int fillable = repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(Integer.getInteger(
                CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS)));
            return ctx.fetchCount(query(ctx, tenant, kind, limit));
        });
        return new Result(kind, fillable, limit, fillable >= limit);
    }
}
