// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.Record1;
import org.jooq.ResultQuery;
import org.jooq.SelectField;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;

/**
 * nexus-uwggq.2.3 and .4 (RDR-241 Phase 2): the paged document reads behind {@code GET /v1/catalog/docs/by-author}
 * and {@code GET /v1/catalog/docs/by-meta}, split out of {@link CatalogRepository} (at its file-size cap). Each answers
 * the tenant's live documents that satisfy a filter, in tumbler order, one page at a time, with the EXACT number of
 * live matches whatever the page.
 *
 * <p>One page statement carries {@code count(*) OVER ()} on every row, so a page and its total are one snapshot. A page
 * past the last match has no row to carry the window count, so then (and only then) a second statement counts the same
 * predicate: the count stays exact on every page, and the second statement runs only for an offset beyond the end. Both
 * run in a {@code tenantScope.withTenant} transaction (FORCE row-level security, and the tenant is an explicit equality)
 * under the section-label count's {@code statement_timeout} ({@link CatalogRepository#FILLABLE_COUNT_TIMEOUT_MS}, same
 * test hook): a timeout is a {@code DataAccessException} holding SQLSTATE {@link CatalogRepository#QUERY_CANCELED}.
 * Rows are the {@code /list} document maps ({@link CatalogRepository#documentFields()}), metadata included.
 *
 * <p><b>Author</b> ({@link #authorWhere}): {@code lower(author) LIKE '%' || lower(:text) || '%' ESCAPE '\'}, which is
 * Python's {@code needle.lower() in author.lower()} under the database's lowercase: no normalization on either side,
 * and {@code \}, {@code %} and {@code _} in the text are literal.
 *
 * <p><b>Metadata</b> ({@link #metaWhere}): the reading rules of {@code catalog/meta_filter.py}, the client's oracle.
 */
public final class CatalogDocReads {

    /** The most documents one page lists; the routes answer 400 above it. */
    public static final int MAX_LIMIT = 1_000;

    /** The page size a call without a limit uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /** A {@code meta_min} bound: an optional minus sign and digits, at most this many (numeric compares any length). */
    public static final int MAX_BOUND_DIGITS = 38;

    /** The answer: {@code count} is every live match; {@code documents} is the page at {@code offset}, at most {@code limit}. */
    public record Page(int count, int limit, int offset, List<Map<String, Object>> documents) {}

    /** {@code meta.<key>=<value>}, with the {@code meta_default.<key>} text when the client sent one (else null). */
    public record MetaEq(String key, String value, String dflt) {}

    /** {@code meta_min.<key>=<n>}. */
    public record MetaMin(String key, BigInteger min) {}

    /** The by-meta filter: an exact content type (or null), equalities, and integer lower bounds; at least one of the last two. */
    public record MetaFilter(String contentType, List<MetaEq> equalities, List<MetaMin> bounds) {
        public MetaFilter {
            if (equalities.isEmpty() && bounds.isEmpty()) {
                throw new IllegalArgumentException("a metadata filter needs at least one meta. or meta_min. clause");
            }
        }
    }

    private static final String INTEGER_TEXT = "^-?[0-9]+$";
    private static final Field<Integer> TOTAL = DSL.count().over().as("nx_total");

    private final CatalogRepository repo;

    public CatalogDocReads(CatalogRepository repo) {
        this.repo = repo;
    }

    // ── predicates ──────────────────────────────────────────────────────────

    private static Condition liveOfTenant(String tenant) {
        return CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.DELETED_AT.isNull());
    }

    /** The contains-pattern for {@code text}: backslash, {@code %} and {@code _} escaped, wrapped in {@code %}. */
    static String containsPattern(String text) {
        return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%";
    }

    /** Live documents of the tenant whose author contains {@code text}, case-insensitively and literally. Public for plan tests. */
    public static Condition authorWhere(String tenant, String text) {
        return liveOfTenant(tenant).and(DSL.lower(CATALOG_DOCUMENTS.AUTHOR)
            .like(DSL.lower(DSL.val(containsPattern(text))), '\\'));
    }

    /** {@code metadata->>key}, the key bound (user input). NULL for an absent key and for a JSON null; a boolean reads true or false. */
    private static Field<String> metaText(String key) {
        return DSL.jsonbGetAttributeAsText(CATALOG_DOCUMENTS.METADATA, key);
    }

    private static Condition metaEq(MetaEq e) {
        Field<String> t = metaText(e.key());
        // With a default the client sent: absent, JSON null and "" read as the default (meta_matches). Without one, NULL
        // (absent or null) matches nothing and "" stays "".
        Field<String> have = e.dflt() == null ? t : DSL.coalesce(DSL.nullif(t, ""), DSL.val(e.dflt()));
        return have.eq(e.value());
    }

    /**
     * The integer lower bound of {@code meta_at_least}. The CASE keeps the cast behind the regex: PostgreSQL does not
     * promise the order of an AND, and {@code ::numeric} raises on a non-integer text, which would fail the whole read on
     * one malformed value. Defaults never apply to a bound.
     */
    private static Condition metaMin(MetaMin m) {
        Field<String> t = metaText(m.key());
        Field<Boolean> guarded = DSL.when(t.likeRegex(INTEGER_TEXT),
                DSL.field(t.cast(SQLDataType.NUMERIC).ge(new BigDecimal(m.min()))))
            .otherwise(DSL.inline(false));
        return DSL.condition(guarded);
    }

    /** Live documents of the tenant that satisfy every clause of {@code filter}. Public for plan tests. */
    public static Condition metaWhere(String tenant, MetaFilter filter) {
        Condition c = liveOfTenant(tenant);
        if (filter.contentType() != null) c = c.and(CATALOG_DOCUMENTS.CONTENT_TYPE.eq(filter.contentType()));
        for (MetaEq e : filter.equalities()) c = c.and(metaEq(e));
        for (MetaMin m : filter.bounds()) c = c.and(metaMin(m));
        return c;
    }

    // ── statements ──────────────────────────────────────────────────────────

    /** The page statement: the document fields plus the window total, in tumbler order. Public so a plan test explains the product's own. */
    public static ResultQuery<Record> pageQuery(DSLContext ctx, Condition where, int limit, int offset) {
        List<SelectField<?>> fields = new ArrayList<>(List.of(CatalogRepository.documentFields()));
        fields.add(TOTAL);
        return ctx.select(fields).from(CATALOG_DOCUMENTS).where(where)
            .orderBy(CATALOG_DOCUMENTS.TUMBLER).limit(limit).offset(offset);
    }

    /** The page without the window total: the document fields in tumbler order. Public so a measurement can compare the two forms. */
    public static ResultQuery<Record> pageRowsQuery(DSLContext ctx, Condition where, int limit, int offset) {
        return ctx.select(CatalogRepository.documentFields()).from(CATALOG_DOCUMENTS).where(where)
            .orderBy(CATALOG_DOCUMENTS.TUMBLER).limit(limit).offset(offset);
    }

    /** The count statement for the same predicate. */
    public static ResultQuery<Record1<Integer>> countQuery(DSLContext ctx, Condition where) {
        return ctx.select(DSL.count()).from(CATALOG_DOCUMENTS).where(where);
    }

    private Page page(String tenant, Condition where, int limit, int offset) {
        if (limit < 1 || limit > MAX_LIMIT) throw new IllegalArgumentException("limit must be 1.." + MAX_LIMIT);
        if (offset < 0) throw new IllegalArgumentException("offset must be 0 or more");
        return repo.tenantScope().withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "statement_timeout", Integer.toString(Integer.getInteger(
                CatalogRepository.FILLABLE_COUNT_TIMEOUT_PROPERTY, CatalogRepository.FILLABLE_COUNT_TIMEOUT_MS)));
            var rows = pageQuery(ctx, where, limit, offset).fetch();
            int count;
            if (!rows.isEmpty()) {
                count = rows.get(0).get(TOTAL);
            } else if (offset == 0) {
                count = 0;   // no row at the first offset: nothing matches
            } else {
                count = countQuery(ctx, where).fetchOne(0, Integer.class);
            }
            List<Map<String, Object>> docs = new ArrayList<>(rows.size());
            for (var r : rows) docs.add(CatalogRepository.docRowFromRecord(r.intoMap()));
            return new Page(count, limit, offset, docs);
        });
    }

    /**
     * @param author non-empty text
     * @param limit  1 to {@link #MAX_LIMIT}
     * @param offset 0 or more
     */
    public Page byAuthor(String tenant, String author, int limit, int offset) {
        if (author == null || author.isEmpty()) throw new IllegalArgumentException("author must be non-empty");
        return page(tenant, authorWhere(tenant, author), limit, offset);
    }

    /** The by-meta page; see {@link #byAuthor} for the bounds. */
    public Page byMeta(String tenant, MetaFilter filter, int limit, int offset) {
        return page(tenant, metaWhere(tenant, filter), limit, offset);
    }
}
