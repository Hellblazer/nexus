// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import dev.nexus.service.datamove.Gate3;
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
import java.util.Locale;
import java.util.Map;
import java.util.regex.Pattern;

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
 * <p><b>Order.</b> Rows come in the engine's text order of {@code tumbler} (the database's collation of a text column), not
 * in the integer order of a tumbler's segments: {@code 5.1.12} sorts before {@code 5.1.2}.
 *
 * <p><b>Author</b> ({@link #authorWhere}): {@code fold(author) LIKE '%' || fold(:text) || '%' ESCAPE '\'}, where
 * {@code fold(x)} is {@code replace(replace(lower(x COLLATE "pg_c_utf8"), 'ς', 'σ'), U+0307, '')}. Case folds with the
 * {@code pg_c_utf8} collation's Unicode SIMPLE case mapping (PostgreSQL 17, a builtin, so it needs neither ICU nor a locale),
 * NOT with the database's own ctype: the local bundle is {@code initdb --no-locale} (C ctype), where a bare {@code lower()}
 * folds ASCII only and a capital O with diaeresis would not match the small one. The oracle is Python's
 * {@code needle.lower() in author.lower()} (the client's final filter, {@code _keep}), which uses the FULL mapping. Simple and
 * full lowercase differ only for U+0130 (Python "i" plus U+0307, the engine "i") and for a capital sigma at the end of a word
 * (Python's final sigma, the engine's medial one); mapping the final sigma to the medial one and dropping U+0307 on both
 * sides makes the two folds agree after that mapping, so the route returns every row Python keeps plus, rarely, rows the
 * client's filter drops: a superset whose {@code count} is an upper bound (Sam 2026-11-19, T2
 * nexus/sam-ruling-rdr241-author-fold-2026-11-19). {@code CatalogDocsByAuthorFoldBase} lists the extra pairs. No
 * normalization on either side, and {@code \}, {@code %} and {@code _} in the text are literal.
 *
 * <p><b>Metadata</b> ({@link #metaWhere}): the reading rules of {@code catalog/meta_filter.py}, the client's oracle.
 *
 * <p><b>Scheme</b> ({@link #schemeWhere}, nexus-uwggq.1.5): a live row matches iff CPython 3.13's
 * {@code urlparse(source_uri).scheme} equals the requested scheme lowercased, which is what the client keeps today
 * ({@code doctor_references.is_reference_only}). The statement mirrors {@code urllib.parse.urlsplit} as shipped in the
 * repo's uv CPython 3.13.5, {@code Lib/urllib/parse.py}: line 494 {@code url = url.lstrip(_WHATWG_C0_CONTROL_OR_SPACE)}
 * (the set is U+0000 to U+0020, line 89; only the LEFT end is stripped, line 492-493), lines 497-498 remove
 * {@code _UNSAFE_URL_BYTES_TO_REMOVE} (tab, CR, LF, line 92) from anywhere in what is left, line 503
 * {@code i = url.find(':')} and line 504 {@code if i > 0 and url[0].isascii() and url[0].isalpha()} with lines 505-509: the
 * text before the first colon is the scheme when every character is in {@code scheme_chars} (ASCII letters, digits and
 * {@code + - .}, line 82), lowercased. In SQL: {@code ltrim} of U+0001 to U+0020 (a NUL cannot be stored in PostgreSQL text,
 * so the NUL of the set cannot occur), {@code translate} to delete tab, CR and LF, then the regex
 * {@code ^([A-Za-z][A-Za-z0-9+.-]*):}, which is the same test (the class excludes the colon, so the colon it meets is the
 * first). NOT mirrored: {@code urlsplit} also raises {@code ValueError} for an invalid IPv6 netloc (lines 447-451, 514) or a
 * netloc that is not valid under NFKC ({@code _checknetloc}, line 521), and the client's {@code urlparse} call has no
 * handler, so on such a row the client fails where this route answers the scheme; that is a defect of the client's call, and
 * the route does not reproduce an exception.
 */
public final class CatalogDocReads {

    /** The most documents one page lists; the routes answer 400 above it. */
    public static final int MAX_LIMIT = 1_000;

    /** The page size a call without a limit uses. */
    public static final int DEFAULT_LIMIT = 1_000;

    /**
     * The most characters a {@code meta_min} bound or a stored integer text may have: PostgreSQL's {@code numeric} holds up to
     * 131,072 digits before the decimal point, so that is the bound (the oracle, {@code meta_at_least}, takes any length
     * through Python's Decimal). A request that is longer is limited by the HTTP server's own request-line limit first.
     * A stored text longer than this is read as no integer, so the cast can never raise numeric overflow and fail the
     * read: the one place the engine departs from the oracle, for a value nothing writes.
     */
    public static final int MAX_BOUND_DIGITS = 131_072;

    /** The answer: {@code count} is every live match; {@code documents} is the page at {@code offset}, at most {@code limit}. */
    public record Page(int count, int limit, int offset, List<Map<String, Object>> documents) {}

    /** {@code meta.<key>=<value>}, with the {@code meta_default.<key>} text when the client sent one (else null). */
    public record MetaEq(String key, String value, String dflt) {}

    /** {@code meta_min.<key>=<n>}. */
    public record MetaMin(String key, BigInteger min) {}

    /**
     * The by-meta filter: an exact content type (or null), equalities, integer lower bounds, and {@code sensitiveOnly},
     * which keeps only the live documents whose {@code sensitive} flag is set. At least one of equalities, bounds or
     * {@code sensitiveOnly} is required, so a filter is never an unfiltered listing.
     */
    public record MetaFilter(String contentType, List<MetaEq> equalities, List<MetaMin> bounds, boolean sensitiveOnly) {
        public MetaFilter {
            if (equalities.isEmpty() && bounds.isEmpty() && !sensitiveOnly) {
                throw new IllegalArgumentException("a metadata filter needs at least one meta., meta_min. or sensitive=true clause");
            }
        }
    }

    /** The builtin collation whose simple Unicode case mapping folds author text on every cluster, C ctype included (PostgreSQL 17). */
    public static final String FOLD_COLLATION = "pg_c_utf8";

    private static final String INTEGER_TEXT = "^-?[0-9]+$";

    /** RFC 3986 scheme, which is also urlsplit's scheme_chars with a letter first (parse.py lines 82 and 504). */
    public static final Pattern SCHEME_GRAMMAR = Pattern.compile("[A-Za-z][A-Za-z0-9+.-]*");

    /** The urlsplit lstrip set without its NUL (parse.py line 89): U+0001 to U+0020. */
    private static final String C0_OR_SPACE = c0OrSpace();

    private static final String UNSAFE_URL_BYTES = "\t\r\n";

    private static final String SCHEME_PREFIX = "^([A-Za-z][A-Za-z0-9+.-]*):";

    private static String c0OrSpace() {
        StringBuilder sb = new StringBuilder();
        for (char c = 1; c <= 0x20; c++) sb.append(c);
        return sb.toString();
    }
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
        Field<String> author = CATALOG_DOCUMENTS.AUTHOR.collate(FOLD_COLLATION);
        // the bound pattern is cast to a collatable type first: a bare ? carries no type for COLLATE to apply to
        Field<String> pattern = DSL.val(containsPattern(text)).cast(SQLDataType.VARCHAR).collate(FOLD_COLLATION);
        return liveOfTenant(tenant).and(fold(author).like(fold(pattern), '\\'));
    }

    /** The author fold: simple lowercase, final sigma read as the medial one, combining dot above (U+0307) dropped. */
    private static Field<String> fold(Field<String> text) {
        return DSL.replace(DSL.replace(DSL.lower(text), "\u03c2", "\u03c3"), "\u0307", "");
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
        Field<Boolean> guarded = DSL.when(t.likeRegex(INTEGER_TEXT).and(DSL.length(t).le(MAX_BOUND_DIGITS)),
                DSL.field(t.cast(SQLDataType.NUMERIC).ge(new BigDecimal(m.min()))))
            .otherwise(DSL.inline(false));
        return DSL.condition(guarded);
    }

    /**
     * Live documents of the tenant whose {@code urlparse(source_uri).scheme} is {@code scheme} lowercased; see the class
     * javadoc for the lines of urlsplit it mirrors. {@code scheme} must match {@link #SCHEME_GRAMMAR}. Public for plan tests.
     */
    public static Condition schemeWhere(String tenant, String scheme) {
        Field<String> stripped = DSL.translate(DSL.ltrim(CATALOG_DOCUMENTS.SOURCE_URI, C0_OR_SPACE), UNSAFE_URL_BYTES, "");
        Field<String> found = DSL.function("substring", String.class, stripped, DSL.val(SCHEME_PREFIX));
        // ASCII only (the regex admits nothing else), so the collation C lowercase is exact on every cluster
        return liveOfTenant(tenant).and(DSL.lower(found.collate("C")).eq(scheme.toLowerCase(Locale.ROOT)));
    }

    /** Live documents of the tenant that satisfy every clause of {@code filter}. Public for plan tests. */
    public static Condition metaWhere(String tenant, MetaFilter filter) {
        Condition c = liveOfTenant(tenant);
        if (filter.contentType() != null) c = c.and(CATALOG_DOCUMENTS.CONTENT_TYPE.eq(filter.contentType()));
        if (filter.sensitiveOnly()) c = c.and(DSL.condition(CATALOG_DOCUMENTS.SENSITIVE));
        for (MetaEq e : filter.equalities()) c = c.and(metaEq(e));
        for (MetaMin m : filter.bounds()) c = c.and(metaMin(m));
        return c;
    }

    // ── statements ──────────────────────────────────────────────────────────

    /** The page statement: the document fields plus the window total, in tumbler order. Public so a plan test explains the product's own. */
    public static ResultQuery<Record> pageQuery(DSLContext ctx, Condition where, int limit, int offset, Gate3 gate3) {
        List<SelectField<?>> fields = new ArrayList<>(List.of(CatalogRepository.documentFields(gate3)));
        fields.add(TOTAL);
        return ctx.select(fields).from(CATALOG_DOCUMENTS).where(where)
            .orderBy(CATALOG_DOCUMENTS.TUMBLER).limit(limit).offset(offset);
    }

    /** The page without the window total: the document fields in tumbler order. Public so a measurement can compare the two forms. */
    public static ResultQuery<Record> pageRowsQuery(DSLContext ctx, Condition where, int limit, int offset, Gate3 gate3) {
        return ctx.select(CatalogRepository.documentFields(gate3)).from(CATALOG_DOCUMENTS).where(where)
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
            var rows = pageQuery(ctx, where, limit, offset, repo.gate3()).fetch();
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

    /** The by-scheme page (nexus-uwggq.1.5); the bounds are {@link #byAuthor}'s. @throws IllegalArgumentException for a scheme outside {@link #SCHEME_GRAMMAR} */
    public Page byScheme(String tenant, String scheme, int limit, int offset) {
        if (scheme == null || !SCHEME_GRAMMAR.matcher(scheme).matches()) throw new IllegalArgumentException("scheme must be an RFC 3986 scheme");
        return page(tenant, schemeWhere(tenant, scheme), limit, offset);
    }

    /** The by-meta page; see {@link #byAuthor} for the bounds. */
    public Page byMeta(String tenant, MetaFilter filter, int limit, int offset) {
        return page(tenant, metaWhere(tenant, filter), limit, offset);
    }
}
