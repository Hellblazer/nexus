// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.TenantScope;

import org.jooq.Field;

import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;

/**
 * nexus-tko2w (RDR-108): the search {@code where} filter on the three per-position section labels,
 * {@code section_title}, {@code section_type} and {@code page_number}, answered from the catalog
 * manifest instead of from the shared T3 chunk row.
 *
 * <p><b>Why a stage after the search, not a predicate inside it.</b> A T3 chunk row is one per
 * (tenant, collection, chash, model), so when two documents hold the same chunk text the row's
 * metadata labels are whichever document wrote last. The labels live on {@code
 * catalog_document_chunks}, one row per (doc_id, position). The vector search runs inside the
 * {@code plain_search_<dim>} / {@code text_gated_*} table functions, which take a metadata containment
 * and a jsonpath and no manifest argument; pushing the manifest into them would mean recreating every
 * body of the family in every dimension. So the three keys are taken out of the {@code where} the
 * function receives ({@link Split#rest()}), the search runs with a wider cut ({@link #widen}), and this
 * stage answers the label predicates per manifest occurrence and cuts to the caller's limit.
 *
 * <p><b>Semantics (design settled with the RDR-232 owner, T2 nexus/tko2w-key-split-2026-10-25).</b>
 * <ul>
 *   <li>The predicate is evaluated per (doc_id, position) occurrence of the hit's chash in the hit's
 *       collection, over live (not tombstoned) documents. A hit passes through once for every
 *       <em>document</em> that owns an occurrence passing the predicate (the document's lowest passing
 *       position), carrying that document's {@code doc_id}, {@code position} and own labels. A shared
 *       chunk can therefore come back twice, once per owner, each with its own label.</li>
 *   <li>Unlabelled: a negative operator ({@code $ne}, {@code $nin}) keeps an unlabelled occurrence (SQL
 *       {@code IS DISTINCT FROM}); every other operator drops it. An empty string counts as unlabelled
 *       exactly as NULL does: a current client writes {@code ""} for a chunk with no heading. The one
 *       exception is an operand that is itself the empty string: an unlabelled value is compared with
 *       {@code ""} first, so {@code $eq ""} and {@code $in [""]} match it and {@code $ne ""} and {@code $nin
 *       [""]} drop it. The chunk-row filter matched only a stored {@code ""} on these operands; a value
 *       that is missing now matches {@code ""} too ({@code $eq ""}, {@code $in [""]}) and is dropped by
 *       {@code $ne ""} and {@code $nin [""]}.</li>
 *   <li>{@code labels_written} (catalog-046): a manifest row says whether its labels are its document's
 *       own answer. A WRITTEN row is authoritative, its empty or NULL label included: the chunk row is
 *       never consulted, because the chunk row belongs to the last writer and a document that indexed a
 *       chunk with no label must not inherit another document's. A LEGACY row (written before
 *       catalog-046, by a client that sends no labels, or by the backfill) has NULL labels that mean
 *       "never recorded": reading them as "no label" would make {@code section_type!=references} stop
 *       filtering every existing document until it is re-indexed, so a legacy occurrence takes the chunk
 *       row's own label instead, exactly the value the filter read before this change. A re-index writes
 *       the manifest and the fallback stops being consulted for that row.</li>
 *   <li>A hit with no live manifest row (an orphan) is kept or dropped on its chunk row's own label
 *       alone, with no {@code doc_id}.</li>
 *   <li>The caller's limit applies after expansion and de-duplication, never before.</li>
 * </ul>
 *
 * <p><b>Document-level keys (nexus-6pfgb.24, epic nexus-6pfgb).</b> The same stage answers the keys that
 * describe a whole document, not a position, per owning document, because the shared chunk row holds the
 * last writer's value of each. The catalog is the authority:
 * <ul>
 *   <li>{@code source_author} reads {@code catalog_documents.author}; {@code indexed_at} reads {@code
 *       catalog_documents.indexed_at} (rendered as the chunk rows' own text shape, UTC, microseconds). An
 *       empty author, or no {@code indexed_at}, falls back to the chunk row: markdown authors predate their
 *       catalog home.</li>
 *   <li>{@code bib_*} read the {@code bib_*} columns and are AUTHORITATIVE: those columns are NOT NULL with
 *       defaults (0 / ''), so a document counts as enriched when {@code bib_enriched_at} is set or any bib
 *       column holds a value, and an unenriched document has no bib at all. The chunk row is never
 *       consulted, because a shared row carries the bib of whichever owner was enriched. An enriched
 *       document's year 0 or empty string is "not recorded" (unlabelled); its citation count 0 is a
 *       count.</li>
 * </ul>
 * The catalog document's {@code metadata} keys ({@code tags}, {@code category}, {@code ttl_days} and the
 * rest) are NOT answered here: no writer puts them on a catalog document yet (epic nexus-6pfgb, wave 1), so
 * answering them would only move those filters under the {@link #OVERFETCH} cut for the same answers. They
 * stay predicates on the chunk row, inside the search.
 * Operators, the unlabelled rules, the per-document lowest-passing-position
 * expansion and the cut are the section labels'. An expanded row speaks for its document on the document
 * keys the filter names: the owner's value replaces the chunk row's, and an authoritative absence (an
 * unenriched document's bib) removes the key.
 *
 * <p><b>Known cost.</b> The search is cut at {@link #OVERFETCH} times the limit before this stage
 * filters, so a filter that rejects most of the neighbourhood (a positive filter on a rare label) can
 * return fewer rows than the limit where the old in-index {@code hnsw.iterative_scan} would have kept
 * walking. The filter that matters in practice, a negative one ({@code section_type!=references}),
 * rejects a small fraction.
 */
final class ManifestLabelStage {

    /** The per-position keys answered from the manifest rows. */
    static final Set<String> LABEL_KEYS = Set.of("section_title", "section_type", "page_number");

    /** {@code source_author}: answered from {@code catalog_documents.author}, the chunk row when that is empty. */
    static final String AUTHOR_KEY = "source_author";

    /** {@code indexed_at}: answered from {@code catalog_documents.indexed_at}, the chunk row when that is NULL. */
    static final String INDEXED_AT_KEY = "indexed_at";

    /** The bibliographic keys: answered from the {@code bib_*} columns, authoritative with no row fallback. */
    static final Set<String> BIB_KEYS = Set.of("bib_year", "bib_authors", "bib_venue", "bib_citation_count",
        "bib_semantic_scholar_id", "bib_openalex_id", "bib_doi");

    /** Every document-level key this stage answers. */
    static final Set<String> DOC_KEYS = Set.copyOf(union(Set.of(AUTHOR_KEY, INDEXED_AT_KEY), BIB_KEYS));

    /**
     * The answered keys whose values are numbers. Only these compare numerically across a number and its text
     * ({@code 5} and {@code "5"}); every other key is a string and compares as an exact string, so an id such
     * as {@code "0042"} is not {@code "42"} and {@code "1e3"} is not {@code "1000"}.
     */
    static final Set<String> NUMERIC_KEYS = Set.of("page_number", "bib_year", "bib_citation_count");

    /** Every key this stage answers: the per-position labels and the document-level keys. */
    static final Set<String> ANSWERED_KEYS = Set.copyOf(union(LABEL_KEYS, DOC_KEYS));

    private static Set<String> union(Set<String> first, Set<String> second) {
        Set<String> all = new LinkedHashSet<>(first);
        all.addAll(second);
        return all;
    }

    /** The chunk rows' own {@code indexed_at} text: UTC, microseconds, an explicit offset (catalog-031's shape). */
    private static final DateTimeFormatter INDEXED_AT_TEXT =
        DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSSSSS'+00:00'");

    /** How many times the caller's limit the underlying search is asked for when labels filter. */
    static final int OVERFETCH = 4;

    /** The widened cut never exceeds this unless the caller's own limit already does. */
    static final int OVERFETCH_CEILING = 1200;

    /** Chashes per manifest lookup (a bind-parameter budget well under PostgreSQL's 32767). */
    private static final int LOOKUP_BATCH = 300;

    private final TenantScope tenantScope;

    ManifestLabelStage(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    // ── where split ─────────────────────────────────────────────────────────

    /** One label predicate: a key, an operator ({@code $eq} for the scalar shorthand) and its operand. */
    record Predicate(String key, String operator, Object operand) {}

    /**
     * @param rest   the {@code where} without the answered keys: what the search itself receives. The same
     *               instance as the input when no answered key is present.
     * @param labels the label and document-key predicates, ANDed
     */
    record Split(Map<String, Object> rest, List<Predicate> labels) {
        boolean active() {
            return !labels.isEmpty();
        }
    }

    /**
     * Separate the label and document-key predicates from a Chroma-style {@code where} map. Operator shapes the search's
     * own planner accepts are accepted here; anything else fails with the same kind of
     * {@link IllegalArgumentException} (the handler's 400 ladder).
     */
    static Split split(Map<String, Object> where) {
        if (where == null || where.isEmpty()) {
            return new Split(where, List.of());
        }
        boolean any = false;
        for (String key : where.keySet()) {
            if (ANSWERED_KEYS.contains(key)) {
                any = true;
                break;
            }
        }
        if (!any) {
            return new Split(where, List.of());
        }
        Map<String, Object> rest = new LinkedHashMap<>();
        List<Predicate> labels = new ArrayList<>();
        for (Map.Entry<String, Object> e : where.entrySet()) {
            String key = e.getKey();
            if (!ANSWERED_KEYS.contains(key)) {
                rest.put(key, e.getValue());
                continue;
            }
            Object value = e.getValue();
            if (!(value instanceof Map<?, ?> ops)) {
                requireScalar("$eq", key, value);
                labels.add(new Predicate(key, "$eq", value));
                continue;
            }
            if (ops.size() != 1) {
                throw new IllegalArgumentException(
                    "where operator map for field '" + key + "' must hold exactly one operator, got "
                    + ops.keySet());
            }
            Map.Entry<?, ?> op = ops.entrySet().iterator().next();
            String operator = String.valueOf(op.getKey());
            Object operand = op.getValue();
            switch (operator) {
                case "$eq", "$ne" -> requireScalar(operator, key, operand);
                case "$in", "$nin" -> {
                    if (!(operand instanceof List<?> items)) {
                        throw new IllegalArgumentException(operator + " for field '" + key
                            + "' expects a list operand, got "
                            + (operand == null ? "null" : operand.getClass().getSimpleName()));
                    }
                    for (Object item : items) {
                        requireScalar(operator, key, item);
                    }
                }
                case "$gte", "$lte", "$gt", "$lt" -> {
                    if (!(operand instanceof Number) && !(operand instanceof String)) {
                        throw new IllegalArgumentException(operator + " for field '" + key
                            + "' expects a numeric or string operand, got "
                            + (operand == null ? "null" : operand.getClass().getSimpleName()));
                    }
                }
                default -> throw new IllegalArgumentException(
                    "unsupported where operator '" + operator + "' for field '" + key
                    + "'; supported: $eq, $ne, $in, $nin, $gte, $lte, $gt, $lt");
            }
            labels.add(new Predicate(key, operator, operand));
        }
        return new Split(rest.isEmpty() ? null : rest, List.copyOf(labels));
    }

    private static void requireScalar(String operator, String key, Object operand) {
        if (operand instanceof Map<?, ?> || operand instanceof List<?>) {
            throw new IllegalArgumentException(
                operator + " for field '" + key + "' expects a scalar operand, got "
                + operand.getClass().getSimpleName());
        }
    }

    /** The cut the underlying search is asked for when label predicates filter its output. */
    static int widen(int limit) {
        long wide = (long) limit * OVERFETCH;
        long ceiling = Math.max(limit, OVERFETCH_CEILING);
        return (int) Math.min(wide, ceiling);
    }

    // ── predicate evaluation ────────────────────────────────────────────────

    /** Whether one occurrence's effective label value passes every predicate (they are ANDed). */
    static boolean passes(List<Predicate> predicates, Map<String, Object> labelsByKey) {
        for (Predicate p : predicates) {
            if (!test(p, labelsByKey.get(p.key()))) {
                return false;
            }
        }
        return true;
    }

    private static boolean test(Predicate p, Object label) {
        if (label == null || (label instanceof String text && text.isEmpty())) {
            // An explicit empty-string operand names exactly this value: compare it before the unlabelled rule.
            // A missing value (null) counts as empty here; the chunk-row filter matched only a stored "".
            return switch (p.operator()) {
                case "$eq" -> isEmptyString(p.operand());
                case "$ne" -> !isEmptyString(p.operand());
                case "$in" -> ((List<?>) p.operand()).stream().anyMatch(ManifestLabelStage::isEmptyString);
                case "$nin" -> ((List<?>) p.operand()).stream().noneMatch(ManifestLabelStage::isEmptyString);
                // IS DISTINCT FROM: a negative operator keeps an unlabelled occurrence, any other drops it.
                default -> false;
            };
        }
        boolean numeric = NUMERIC_KEYS.contains(p.key());
        return switch (p.operator()) {
            case "$eq" -> sameValue(label, p.operand(), numeric);
            case "$ne" -> !sameValue(label, p.operand(), numeric);
            case "$in" -> ((List<?>) p.operand()).stream().anyMatch(o -> sameValue(label, o, numeric));
            case "$nin" -> ((List<?>) p.operand()).stream().noneMatch(o -> sameValue(label, o, numeric));
            case "$gte" -> comparable(label, p.operand(), numeric) && compare(label, p.operand()) >= 0;
            case "$lte" -> comparable(label, p.operand(), numeric) && compare(label, p.operand()) <= 0;
            case "$gt" -> comparable(label, p.operand(), numeric) && compare(label, p.operand()) > 0;
            case "$lt" -> comparable(label, p.operand(), numeric) && compare(label, p.operand()) < 0;
            default -> throw new IllegalStateException("operator validated in split: " + p.operator());
        };
    }

    private static boolean isEmptyString(Object operand) {
        return operand instanceof String s && s.isEmpty();
    }

    /**
     * Equality with the search planner's tolerant typing for a numeric key: a stored {@code 5} equals the
     * operand {@code "5"} and the reverse, so {@code page_number=5} and {@code page_number="5"} both match.
     * A string key compares as an exact string.
     */
    private static boolean sameValue(Object label, Object operand, boolean numericKey) {
        if (numericKey) {
            BigDecimal a = asNumber(label);
            BigDecimal b = asNumber(operand);
            if (a != null && b != null) {
                return a.compareTo(b) == 0;
            }
        }
        return String.valueOf(label).equals(String.valueOf(operand));
    }

    /**
     * A numeric operand compares numerically, a string operand lexically; across the two nothing matches. A
     * string key has no number to compare with, so only a string operand applies to it.
     */
    private static boolean comparable(Object label, Object operand, boolean numericKey) {
        if (operand instanceof Number) {
            return numericKey && asNumber(label) != null;
        }
        return !(label instanceof Number);
    }

    private static int compare(Object label, Object operand) {
        if (operand instanceof Number n) {
            BigDecimal l = asNumber(label);
            return l == null ? 0 : l.compareTo(new BigDecimal(n.toString()));
        }
        return String.valueOf(label).compareTo(String.valueOf(operand));
    }

    private static BigDecimal asNumber(Object o) {
        if (o instanceof Number n) {
            try {
                return new BigDecimal(n.toString());
            } catch (NumberFormatException e) {
                return null;
            }
        }
        if (o instanceof String s && !s.isBlank()) {
            try {
                return new BigDecimal(s.trim());
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return null;
    }

    // ── manifest lookup ─────────────────────────────────────────────────────

    /**
     * One live manifest occurrence of a chash: the owning document, the position and its own labels, the
     * document's {@code source_uri} (a row that carries one is re-pointed at the document it speaks for), and
     * the document's own answers on the document-level keys the filter names.
     *
     * @param docValues the document's AUTHORITATIVE value for each document-level key the predicates name:
     *                  a key that is present answers for the document (a null value is an authoritative
     *                  absence, an unenriched document's bib); a key that is absent falls back to the chunk
     *                  row (a legacy document). Empty when the filter names no document-level key.
     */
    record Occurrence(String docId, int position, String sectionTitle, String sectionType, Integer pageNumber,
                      boolean labelsWritten, String sourceUri, Map<String, Object> docValues) {}

    /** The document-level keys the predicates name. */
    static Set<String> docKeysOf(List<Predicate> predicates) {
        Set<String> keys = new LinkedHashSet<>();
        for (Predicate p : predicates) {
            if (DOC_KEYS.contains(p.key())) {
                keys.add(p.key());
            }
        }
        return keys;
    }

    /** The document columns one lookup reads for {@code docKeys}, and how to read them back. */
    private record DocFields(Set<String> keys, Field<String> author, Field<OffsetDateTime> indexedAt,
                             Field<Integer> bibYear, Field<String> bibAuthors, Field<String> bibVenue,
                             Field<Integer> bibCitations, Field<String> bibS2, Field<String> bibOpenAlex,
                             Field<String> bibDoi, Field<OffsetDateTime> bibEnrichedAt) {

        static DocFields of(Set<String> keys) {
            boolean bib = keys.stream().anyMatch(BIB_KEYS::contains);
            return new DocFields(keys,
                keys.contains(AUTHOR_KEY) ? CATALOG_DOCUMENTS.AUTHOR : null,
                keys.contains(INDEXED_AT_KEY) ? CATALOG_DOCUMENTS.INDEXED_AT : null,
                bib ? CATALOG_DOCUMENTS.BIB_YEAR : null, bib ? CATALOG_DOCUMENTS.BIB_AUTHORS : null,
                bib ? CATALOG_DOCUMENTS.BIB_VENUE : null, bib ? CATALOG_DOCUMENTS.BIB_CITATION_COUNT : null,
                bib ? CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID : null, bib ? CATALOG_DOCUMENTS.BIB_OPENALEX_ID : null,
                bib ? CATALOG_DOCUMENTS.BIB_DOI : null, bib ? CATALOG_DOCUMENTS.BIB_ENRICHED_AT : null);
        }

        void addTo(List<Field<?>> select) {
            for (Field<?> f : java.util.Arrays.asList(author, indexedAt, bibYear, bibAuthors, bibVenue, bibCitations,
                                                      bibS2, bibOpenAlex, bibDoi, bibEnrichedAt)) {
                if (f != null) {
                    select.add(f);
                }
            }
        }

        Map<String, Object> read(org.jooq.Record r) {
            Map<String, Object> out = new HashMap<>();
            if (author != null) {
                String a = r.get(author);
                if (a != null && !a.isBlank()) {
                    out.put(AUTHOR_KEY, a);
                }
            }
            if (indexedAt != null) {
                OffsetDateTime at = r.get(indexedAt);
                if (at != null) {
                    out.put(INDEXED_AT_KEY, indexedAtText(at));
                }
            }
            if (bibYear != null) {
                bibValues(keys, r.get(bibYear), r.get(bibAuthors), r.get(bibVenue), r.get(bibCitations),
                    r.get(bibS2), r.get(bibOpenAlex), r.get(bibDoi), r.get(bibEnrichedAt) != null, out);
            }
            return out;
        }
    }

    /** A catalog timestamp as the chunk rows' own {@code indexed_at} text, so range operands compare alike. */
    static String indexedAtText(OffsetDateTime at) {
        return at.withOffsetSameInstant(ZoneOffset.UTC).format(INDEXED_AT_TEXT);
    }

    /**
     * The document's bib values for the named bib keys, into {@code out}. The columns are NOT NULL with
     * defaults, so "enriched" is read off them: {@code bib_enriched_at} set, or any column holding a value. An
     * unenriched document answers an authoritative absence (null) for every bib key; an enriched one answers
     * its values, a year of 0 and an empty string being "not recorded" (null) and a citation count of 0 a count.
     */
    static void bibValues(Set<String> keys, Integer yearColumn, String authors, String venue, Integer citationColumn,
                          String s2, String openAlex, String doi, boolean enrichedAtSet, Map<String, Object> out) {
        int year = yearColumn == null ? 0 : yearColumn;
        int citations = citationColumn == null ? 0 : citationColumn;
        boolean enriched = enrichedAtSet || year != 0 || citations != 0 || notEmpty(authors) || notEmpty(venue)
            || notEmpty(s2) || notEmpty(openAlex) || notEmpty(doi);
        Map<String, Object> all = new HashMap<>();
        all.put("bib_year", enriched && year != 0 ? (Object) year : null);
        all.put("bib_authors", enriched && notEmpty(authors) ? authors : null);
        all.put("bib_venue", enriched && notEmpty(venue) ? venue : null);
        all.put("bib_citation_count", enriched ? (Object) citations : null);
        all.put("bib_semantic_scholar_id", enriched && notEmpty(s2) ? s2 : null);
        all.put("bib_openalex_id", enriched && notEmpty(openAlex) ? openAlex : null);
        all.put("bib_doi", enriched && notEmpty(doi) ? doi : null);
        for (String key : keys) {
            if (BIB_KEYS.contains(key)) {
                out.put(key, all.get(key));
            }
        }
    }

    private static boolean notEmpty(String s) {
        return s != null && !s.isEmpty();
    }

    /**
     * The live manifest occurrences of each (collection, chash) the rows name, in (doc_id, position)
     * order. One bounded statement per collection and batch, under the tenant's row-level security, joined
     * to the document so a tombstoned document owns nothing. The same statement reads the document-level
     * values the predicates name ({@code docKeys}); nothing is read for a filter that names none.
     */
    private Map<String, List<Occurrence>> occurrences(String tenant, List<Map<String, Object>> rows,
                                                      Set<String> docKeys) {
        Map<String, Set<String>> chashesByCollection = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            Object id = row.get("id");
            Object coll = row.get("collection");
            if (id instanceof String chash && chash.length() == 64 && coll instanceof String c) {
                chashesByCollection.computeIfAbsent(c, k -> new LinkedHashSet<>()).add(chash);
            }
        }
        Map<String, List<Occurrence>> out = new HashMap<>();
        if (chashesByCollection.isEmpty()) {
            return out;
        }
        Field<String> chashHex = ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH);
        DocFields docFields = DocFields.of(docKeys);
        List<Field<?>> select = new ArrayList<>(List.of(chashHex,
            CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
            CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE,
            CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN,
            CATALOG_DOCUMENTS.SOURCE_URI));
        docFields.addTo(select);
        tenantScope.withTenant(tenant, ctx -> {
            for (Map.Entry<String, Set<String>> e : chashesByCollection.entrySet()) {
                List<String> hexes = new ArrayList<>(e.getValue());
                for (int start = 0; start < hexes.size(); start += LOOKUP_BATCH) {
                    List<String> batch = hexes.subList(start, Math.min(start + LOOKUP_BATCH, hexes.size()));
                    List<byte[]> keys = new ArrayList<>(batch.size());
                    for (String hex : batch) {
                        keys.add(Chash.fromHex(hex).toBytes());
                    }
                    var fetched = ctx.select(select)
                        .from(CATALOG_DOCUMENT_CHUNKS)
                        .join(CATALOG_DOCUMENTS)
                            // A tombstoned document owns nothing (nexus-mqd6t): its occurrence is not a hit.
                            .on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                                .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID))
                                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
                        .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                            .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(e.getKey()))
                            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(keys)))
                        .orderBy(CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION)
                        .fetch();
                    for (var r : fetched) {
                        out.computeIfAbsent(key(e.getKey(), r.get(chashHex)), k -> new ArrayList<>())
                           .add(new Occurrence(r.get(CATALOG_DOCUMENT_CHUNKS.DOC_ID),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.POSITION),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER),
                                               Boolean.TRUE.equals(r.get(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)),
                                               r.get(CATALOG_DOCUMENTS.SOURCE_URI),
                                               docFields.read(r)));
                    }
                }
            }
            return null;
        });
        return out;
    }

    private static String key(String collection, String chash) {
        return collection + "\u0000" + chash;
    }

    // ── the stage ───────────────────────────────────────────────────────────

    /**
     * Expand each row to its passing occurrences and cut to {@code limit} (best-first order is the input's).
     *
     * @param limit the caller's limit, or {@link Integer#MAX_VALUE} for no cut (a list that is not cut here)
     */
    List<Map<String, Object>> apply(String tenant, List<Map<String, Object>> rows, List<Predicate> predicates,
                                    int limit) {
        if (rows.isEmpty()) {
            return rows;
        }
        Set<String> docKeys = docKeysOf(predicates);
        Map<String, List<Occurrence>> owners = occurrences(tenant, rows, docKeys);
        List<Map<String, Object>> out = new ArrayList<>(Math.min(rows.size(), Math.max(limit, 0)));
        for (Map<String, Object> row : rows) {
            Object id = row.get("id");
            Object coll = row.get("collection");
            List<Occurrence> occ = id instanceof String ch && coll instanceof String c
                ? owners.getOrDefault(key(c, ch), List.of()) : List.of();
            if (occ.isEmpty()) {
                // No live manifest row: judged on the chunk row's own values, no document to name.
                if (passes(predicates, rowLabels(row, null, docKeys))) {
                    out.add(row);
                }
            } else {
                Set<String> seenDocs = new LinkedHashSet<>();
                for (Occurrence o : occ) {
                    if (seenDocs.contains(o.docId())) {
                        continue;
                    }
                    Map<String, Object> labels = rowLabels(row, o, docKeys);
                    if (passes(predicates, labels)) {
                        seenDocs.add(o.docId());
                        out.add(expanded(row, o));
                    }
                }
            }
            if (out.size() >= limit) {
                return new ArrayList<>(out.subList(0, limit));
            }
        }
        return out;
    }

    /**
     * The effective value of one occurrence, per key. A WRITTEN manifest row (see {@link Occurrence})
     * answers the section labels for itself, an empty or NULL label included. A legacy row, or a hit with no
     * live manifest row ({@code o == null}), takes the chunk row's own label (flattened into the row map),
     * else is absent. A document-level key the document answers (see {@link Occurrence#docValues}) is its
     * value, a null one an authoritative absence; any other falls back to the chunk row.
     */
    private static Map<String, Object> rowLabels(Map<String, Object> row, Occurrence o, Set<String> docKeys) {
        Map<String, Object> m = new HashMap<>(4 + docKeys.size());
        if (o != null && o.labelsWritten()) {
            m.put("section_title", o.sectionTitle());
            m.put("section_type", o.sectionType());
            m.put("page_number", o.pageNumber());
        } else {
            m.put("section_title", row.get("section_title"));
            m.put("section_type", row.get("section_type"));
            m.put("page_number", row.get("page_number"));
        }
        for (String key : docKeys) {
            m.put(key, o != null && o.docValues().containsKey(key) ? o.docValues().get(key) : row.get(key));
        }
        return m;
    }

    /**
     * A copy of {@code row} that speaks for one owning document: its id, position and own labels. A written
     * occurrence replaces the chunk row's (foreign) labels outright: an empty title or type is sent as
     * {@code ""} (the chunkers' "none here"), and an unset page removes the key instead of leaving another
     * document's page behind. The document-level keys the filter named, where the document answers them,
     * are replaced the same way: the owner's value, or no key for an authoritative absence.
     */
    private static Map<String, Object> expanded(Map<String, Object> row, Occurrence o) {
        Map<String, Object> copy = new LinkedHashMap<>(row);
        copy.put("doc_id", o.docId());
        copy.put("position", o.position());
        if (copy.containsKey("source_uri") && o.sourceUri() != null) {
            // include_source_uri resolved one owner's URI for the shared chunk; this row speaks for o's document.
            copy.put("source_uri", o.sourceUri());
        }
        if (o.labelsWritten()) {
            copy.put("section_title", o.sectionTitle() == null ? "" : o.sectionTitle());
            copy.put("section_type", o.sectionType() == null ? "" : o.sectionType());
            if (o.pageNumber() == null) {
                copy.remove("page_number");
            } else {
                copy.put("page_number", o.pageNumber());
            }
        }
        for (Map.Entry<String, Object> d : o.docValues().entrySet()) {
            if (d.getValue() == null) {
                copy.remove(d.getKey());
            } else {
                copy.put(d.getKey(), d.getValue());
            }
        }
        return copy;
    }
}
