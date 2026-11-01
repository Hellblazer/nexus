// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.TenantScope;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.Record;
import org.jooq.ResultQuery;

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
import java.util.regex.Matcher;
import java.util.regex.Pattern;

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
 * <ul>
 *   <li>{@code title} (nexus-6pfgb.25) reads {@code catalog_documents.title}; an empty catalog title falls back to
 *       the chunk row's, so a legacy document keeps matching its row title.</li>
 *   <li>{@code content_type} (nexus-6pfgb.25) is answered by value, see {@link #CONTENT_TYPE_KEY} and {@link
 *       ContentType}. {@code source_format} is not a column yet (nexus-6pfgb.23), so the format is derived here
 *       from the catalog's content type and file extension by the rule that column will carry, and the engine's
 *       in-SQL pre-filter ({@code manifest_search_<dim>}, vectors-041) derives it by the same expression.</li>
 * </ul>
 * <p><b>Catalog-meta keys (nexus-6pfgb.29).</b> The eight keys the catalog document's {@code metadata} holds,
 * {@link #META_KEYS}, are answered per owning document too. nexus-6pfgb.24 had taken them out because no writer
 * stamped them yet and answering them then only moved those filters under the {@link #OVERFETCH} cut for the same
 * answers; the wave-1 writers (store_put, import, the meta backfill) now do, and a rare value is found by the same
 * in-SQL EXISTS as a rare label ({@link ManifestPushdown}), so they are answered here. The rule per key is the
 * presence fallback: a document whose meta HAS the key (a JSON value that is not null) answers for itself, an empty
 * value included (the catalog has no way to retract a key, so an empty value is the writer's answer); a document
 * whose meta lacks it takes the chunk row's value, exactly what the filter read before this change, so documents
 * stamped before the writers existed behave as they did. Typing per key: {@code category}, {@code source_agent},
 * {@code extraction_method} and {@code extraction_source} compare as exact strings; {@code ttl_days} and {@code
 * frecency_score} are numeric ({@link #NUMERIC_KEYS}: {@code 7} equals {@code "7"}, ranges compare as numbers);
 * {@code quality_gate_overridden} is a boolean whose JSON form {@code true} and text form {@code "true"} compare
 * equal, any other spelling ({@code "True"}) does not; {@code tags} is a SET (RDR-236 RF-17): the meta holds a JSON
 * array (a legacy comma string is split on commas, each tag trimmed, empties dropped, the chunk row's comma string
 * the same), a positive operator matches when ANY tag matches, a negative one when NONE does, and a document with
 * no tag answers the empty string like any unlabelled value. A row this stage returns for a filter that names
 * {@code tags} shows the owner's tags as the chunk row's own comma-separated text.
 *
 * <p><b>In-SQL pre-filter (nexus-p89h4, nexus-6pfgb.25).</b> The positive form of the predicates {@link
 * ManifestPushdown} can express (an equality or {@code $in} on a label, {@code title}, {@code source_author} or an
 * id-valued bib key, {@code content_type} by value, a range on {@code page_number}) is ALSO sent into the search as
 * an EXISTS on the manifest ({@code manifest_search_<dim>}), so the scan keeps walking until the page is full and a
 * rare label is found. That EXISTS only admits a chunk; this stage still names the qualifying owner per document,
 * and it is the exact judge of every operator, so the in-SQL form is built to admit a superset of what the stage
 * keeps and never less.
 *
 * Operators, the unlabelled rules, the per-document lowest-passing-position
 * expansion and the cut are the section labels'. An expanded row speaks for its document on the document
 * keys the filter names: the owner's value replaces the chunk row's, and an authoritative absence (an
 * unenriched document's bib) removes the key.
 *
 * <p><b>Known cost.</b> The search is cut at {@link #OVERFETCH} times the limit before this stage
 * filters. A positive predicate {@link ManifestPushdown} carries into the search no longer starves on it (the
 * nexus-p89h4 fix). A predicate it does not carry (a negative operator, {@code indexed_at}, {@code bib_year},
 * {@code bib_venue}, {@code bib_authors}, {@code bib_citation_count}) still depends on the cut: a filter that
 * rejects most of the neighbourhood returns fewer rows than the limit. The negative filter that matters in
 * practice ({@code section_type!=references}) rejects a small fraction.
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

    /**
     * {@code title}: the catalog title (the filename for code and prose), the chunk row's only while the catalog
     * title is empty (nexus-6pfgb.25, Sam's ruling nexus-6pfgb.2). The per-position label is {@code section_title}.
     */
    static final String TITLE_KEY = "title";

    /**
     * {@code content_type}: answered by value (nexus-6pfgb.25, Sam's ruling nexus-6pfgb.3). {@code pdf} and
     * {@code markdown} read the format the catalog derives for the document ({@link #derivedFormat}), the chunk
     * row's value when it derives none; every other value ({@code code}, {@code prose}, {@code paper}, {@code
     * rdr}, {@code knowledge}) reads {@code catalog_documents.content_type}, so {@code prose} includes a markdown
     * file.
     */
    static final String CONTENT_TYPE_KEY = "content_type";

    /** {@code tags}: a set, see the class comment. */
    static final String TAGS_KEY = "tags";

    /**
     * The keys the catalog document's {@code metadata} holds (nexus-6pfgb.29): answered from the document's meta when
     * it HAS the key, from the chunk row when it lacks it.
     */
    static final Set<String> META_KEYS = Set.of(TAGS_KEY, "category", "ttl_days", "source_agent",
        "extraction_method", "quality_gate_overridden", "extraction_source", "frecency_score");

    /** Every document-level key this stage answers. */
    static final Set<String> DOC_KEYS = Set.copyOf(union(
        Set.of(AUTHOR_KEY, INDEXED_AT_KEY, TITLE_KEY, CONTENT_TYPE_KEY), union(BIB_KEYS, META_KEYS)));

    /**
     * The answered keys whose values are numbers. Only these compare numerically across a number and its text
     * ({@code 5} and {@code "5"}); every other key is a string and compares as an exact string, so an id such
     * as {@code "0042"} is not {@code "42"} and {@code "1e3"} is not {@code "1000"}.
     */
    static final Set<String> NUMERIC_KEYS = Set.of("page_number", "bib_year", "bib_citation_count",
        "ttl_days", "frecency_score");

    /** Every key this stage answers: the per-position labels and the document-level keys. */
    static final Set<String> ANSWERED_KEYS = Set.copyOf(union(LABEL_KEYS, DOC_KEYS));

    /**
     * A document's answer on {@code content_type}: the catalog's own content type ({@code null} when empty) and the
     * format {@link #derivedFormat} gives it ({@code null} when it derives none), with the chunk row's value as
     * the fallback each of them leaves open. The operand picks which one speaks: see {@link #matches}.
     *
     * @param rowType the chunk row's {@code content_type}, filled in per hit; {@code null} in the lookup
     */
    record ContentType(String catalogType, String format, Object rowType) {
        ContentType withRow(Object row) {
            return new ContentType(catalogType, format, row);
        }

        /** The value an expanded row shows for this document: its format when it has one, else its catalog type. */
        String display() {
            return format != null ? format : catalogType;
        }
    }

    /** The file extension the way Python's {@code Path.suffix} reads it: no dotfile, no dot in a directory. */
    private static final Pattern EXTENSION = Pattern.compile("[^/.]\\.([^./]+)$");

    /**
     * The format the catalog derives for a document (the rule nexus-6pfgb.23's {@code source_format} column will
     * carry, computed here until it ships): the extension is read first; {@code pdf} (or catalog type {@code pdf}
     * or {@code paper}) is {@code pdf}, {@code md} and {@code markdown} are {@code markdown} whatever the catalog
     * type, then catalog {@code code} is {@code code} and {@code prose} or {@code rdr} is {@code prose}. Anything
     * else (a note, a file with no extension, a type outside the rules) derives none.
     */
    static String derivedFormat(String contentType, String filePath) {
        String ext = null;
        if (filePath != null) {
            Matcher m = EXTENSION.matcher(filePath);
            if (m.find()) {
                ext = m.group(1).toLowerCase(java.util.Locale.ROOT);
            }
        }
        String type = contentType == null ? "" : contentType;
        if ("pdf".equals(ext) || "pdf".equals(type) || "paper".equals(type)) {
            return "pdf";
        }
        if ("md".equals(ext) || "markdown".equals(ext)) {
            return "markdown";
        }
        if ("code".equals(type)) {
            return "code";
        }
        if ("prose".equals(type) || "rdr".equals(type)) {
            return "prose";
        }
        return null;
    }

    /**
     * Whether a document answers to {@code operand} on {@code content_type}. {@code pdf} and {@code markdown}
     * are formats: the derived one when the catalog has one, else the chunk row's value. Every other word is a
     * catalog content type: the catalog's when it has one, else the chunk row's.
     */
    static boolean matches(ContentType ct, Object operand) {
        String wanted = String.valueOf(operand);
        if ("pdf".equals(wanted) || "markdown".equals(wanted)) {
            return ct.format() != null ? ct.format().equals(wanted) : wanted.equals(textOf(ct.rowType()));
        }
        return ct.catalogType() != null ? ct.catalogType().equals(wanted) : wanted.equals(textOf(ct.rowType()));
    }

    private static String textOf(Object o) {
        return o == null ? null : String.valueOf(o);
    }

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
    private static final int LOOKUP_BATCH = PgVectorRepository.SOURCE_URI_JOIN_BATCH;

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
        // A document with no tag answers the empty string, as an unlabelled value does.
        return testValue(p, label instanceof List<?> set && set.isEmpty() ? null : label);
    }

    private static boolean testValue(Predicate p, Object label) {
        if (label instanceof List<?> tags) {
            // A SET (tags): a positive operator matches when any member does, a negative one when none does.
            return switch (p.operator()) {
                case "$eq" -> tags.stream().anyMatch(t -> sameText(t, p.operand()));
                case "$ne" -> tags.stream().noneMatch(t -> sameText(t, p.operand()));
                case "$in" -> ((List<?>) p.operand()).stream().anyMatch(o -> tags.stream().anyMatch(t -> sameText(t, o)));
                case "$nin" -> ((List<?>) p.operand()).stream().noneMatch(o -> tags.stream().anyMatch(t -> sameText(t, o)));
                default -> false;  // a range over a set of names has no meaning
            };
        }
        if (label instanceof ContentType ct) {
            return switch (p.operator()) {
                case "$eq" -> matches(ct, p.operand());
                case "$ne" -> !matches(ct, p.operand());
                case "$in" -> ((List<?>) p.operand()).stream().anyMatch(o -> matches(ct, o));
                case "$nin" -> ((List<?>) p.operand()).stream().noneMatch(o -> matches(ct, o));
                // A range over content types has no meaning; it reads the value the row would show.
                default -> testValue(p, ct.display());
            };
        }
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

    private static boolean sameText(Object member, Object operand) {
        return String.valueOf(member).equals(String.valueOf(operand));
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
     * document's {@code source_uri} (a row that carries one is re-pointed at the document it speaks for), the
     * position's own span ({@code lineStart}, {@code lineEnd}, {@code charStart}, {@code charEnd}, each NULL
     * for a row written before the manifest carried spans: nexus-6pfgb.26), and the document's own answers on
     * the document-level keys the filter names.
     *
     * @param docValues the document's AUTHORITATIVE value for each document-level key the predicates name:
     *                  a key that is present answers for the document (a null value is an authoritative
     *                  absence, an unenriched document's bib); a key that is absent falls back to the chunk
     *                  row (a legacy document). Empty when the filter names no document-level key.
     */
    record Occurrence(String docId, int position, String sectionTitle, String sectionType, Integer pageNumber,
                      boolean labelsWritten, String sourceUri, Integer lineStart, Integer lineEnd,
                      Integer charStart, Integer charEnd, Map<String, Object> docValues) {}

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
                             Field<String> bibDoi, Field<OffsetDateTime> bibEnrichedAt,
                             Field<String> title, Field<String> contentType, Field<String> filePath,
                             Field<JSONB> metadata) {

        static DocFields of(Set<String> keys) {
            boolean bib = keys.stream().anyMatch(BIB_KEYS::contains);
            boolean type = keys.contains(CONTENT_TYPE_KEY);
            return new DocFields(keys,
                keys.contains(AUTHOR_KEY) ? CATALOG_DOCUMENTS.AUTHOR : null,
                keys.contains(INDEXED_AT_KEY) ? CATALOG_DOCUMENTS.INDEXED_AT : null,
                bib ? CATALOG_DOCUMENTS.BIB_YEAR : null, bib ? CATALOG_DOCUMENTS.BIB_AUTHORS : null,
                bib ? CATALOG_DOCUMENTS.BIB_VENUE : null, bib ? CATALOG_DOCUMENTS.BIB_CITATION_COUNT : null,
                bib ? CATALOG_DOCUMENTS.BIB_SEMANTIC_SCHOLAR_ID : null, bib ? CATALOG_DOCUMENTS.BIB_OPENALEX_ID : null,
                bib ? CATALOG_DOCUMENTS.BIB_DOI : null, bib ? CATALOG_DOCUMENTS.BIB_ENRICHED_AT : null,
                keys.contains(TITLE_KEY) ? CATALOG_DOCUMENTS.TITLE : null,
                type ? CATALOG_DOCUMENTS.CONTENT_TYPE : null, type ? CATALOG_DOCUMENTS.FILE_PATH : null,
                keys.stream().anyMatch(META_KEYS::contains) ? CATALOG_DOCUMENTS.METADATA : null);
        }

        void addTo(List<Field<?>> select) {
            for (Field<?> f : java.util.Arrays.asList(author, indexedAt, bibYear, bibAuthors, bibVenue, bibCitations,
                                                      bibS2, bibOpenAlex, bibDoi, bibEnrichedAt, title, contentType,
                                                      filePath, metadata)) {
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
            if (title != null) {
                String t = r.get(title);
                if (t != null && !t.isBlank()) {
                    out.put(TITLE_KEY, t);
                }
            }
            if (contentType != null) {
                String catalogType = r.get(contentType);
                String format = derivedFormat(catalogType, r.get(filePath));
                String type = catalogType == null || catalogType.isBlank() ? null : catalogType;
                if (type != null || format != null) {
                    // The row's value, the fallback each of the two leaves open, is filled in per hit.
                    out.put(CONTENT_TYPE_KEY, new ContentType(type, format, null));
                }
            }
            if (metadata != null) {
                readMeta(r.get(metadata), keys, out);
            }
            return out;
        }
    }

    private static final ObjectMapper META_MAPPER = new ObjectMapper();

    /**
     * The document's answer for each catalog-meta key the filter names, into {@code out}: a key the meta HAS (a JSON
     * value that is not null) answers for the document, whatever it holds; a key it lacks is left out so the chunk
     * row answers (the presence fallback, see the class comment).
     */
    static void readMeta(JSONB metadata, Set<String> keys, Map<String, Object> out) {
        if (metadata == null) {
            return;
        }
        JsonNode meta;
        try {
            meta = META_MAPPER.readTree(metadata.data());
        } catch (java.io.IOException e) {
            throw new IllegalStateException("catalog_documents.metadata is not JSON: " + e.getMessage(), e);
        }
        for (String key : keys) {
            JsonNode v = META_KEYS.contains(key) ? meta.get(key) : null;
            if (v == null || v.isNull()) {
                continue;
            }
            out.put(key, TAGS_KEY.equals(key) ? tagSet(v) : metaScalar(v));
        }
    }

    /** A meta value as the Java value the predicates compare: a number, a boolean, or the text of anything else. */
    private static Object metaScalar(JsonNode v) {
        if (v.isNumber()) {
            return v.numberValue();
        }
        if (v.isBoolean()) {
            return v.booleanValue();
        }
        return v.isTextual() ? v.textValue() : v.toString();
    }

    /**
     * The tags a stored value holds, as a list: a JSON array (each element's text), or a string (the legacy comma
     * form) split on commas; every tag trimmed of ASCII whitespace and empties dropped, duplicates removed,
     * first-seen order kept. Any other value holds no tag.
     *
     * <p>The trim is the ASCII set {@code nexus.catalog_tag_set} (vectors-044) trims with, NOT {@link String#strip}
     * (Unicode): the in-search EXISTS tests membership of the SQL set and the stage trims the rest with this one,
     * so a wider trim here would pass a stored {@code "<U+2003>arch"} through the stage that the EXISTS had already
     * dropped. ASCII was chosen over widening the SQL to {@code \s}: the function stays a plain IMMUTABLE
     * {@code btrim}, no regex per tag, and vectors-044 needs no edit. The client's {@code split_note_tags} strips
     * Unicode whitespace too, which only matters one way: a tag the client writes never carries surrounding
     * Unicode whitespace, so nothing it stored is cut differently here; a tag written by another path with such
     * whitespace is kept whole on both server sides and matches only an operand that carries it.
     */
    static List<String> tagSet(Object stored) {
        List<String> raw = new ArrayList<>();
        if (stored instanceof JsonNode node) {
            if (node.isArray()) {
                node.forEach(e -> {
                    if (!e.isNull()) {  // jsonb_array_elements_text gives SQL NULL for a JSON null, which is no tag
                        raw.add(e.isTextual() ? e.textValue() : e.toString());
                    }
                });
            } else if (node.isTextual()) {
                raw.addAll(java.util.Arrays.asList(node.textValue().split(",", -1)));
            }
        } else if (stored instanceof List<?> items) {
            items.forEach(e -> raw.add(String.valueOf(e)));
        } else if (stored instanceof String text) {
            raw.addAll(java.util.Arrays.asList(text.split(",", -1)));
        }
        Set<String> tags = new LinkedHashSet<>();
        for (String t : raw) {
            String trimmed = trimAscii(t);
            if (!trimmed.isEmpty()) {
                tags.add(trimmed);
            }
        }
        return List.copyOf(tags);
    }

    /** The ASCII whitespace {@code nexus.catalog_tag_set} trims: space, tab, CR, LF, form feed, vertical tab. */
    private static String trimAscii(String t) {
        int from = 0;
        int to = t.length();
        while (from < to && isTagSpace(t.charAt(from))) {
            from++;
        }
        while (to > from && isTagSpace(t.charAt(to - 1))) {
            to--;
        }
        return t.substring(from, to);
    }

    private static boolean isTagSpace(char c) {
        return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' || c == '\u000b';
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
            if (id instanceof String chash && PgVectorRepository.HEX64.matcher(chash).matches()
                    && coll instanceof String c) {
                chashesByCollection.computeIfAbsent(c, k -> new LinkedHashSet<>()).add(chash);
            }
        }
        return fetchOccurrences(tenant, chashesByCollection, docKeys, false);
    }

    /**
     * The FIRST live owner of each (collection, chash): the occurrence of the lowest {@code doc_id} (in the
     * database's text collation, so {@code 1.10} sorts before {@code 1.2}), then the lowest position, among
     * documents that are not tombstoned (nexus-6pfgb.26). This is the one owner a chunk-level search row's
     * {@code doc_id}, {@code position}, {@code source_uri} and span speak for when no label filter has expanded
     * the row per document; keyed by {@link #ownerKey}. A chash with no live manifest row has no entry.
     *
     * <p>Its own {@code SELECT DISTINCT ON (chash) ... ORDER BY chash, doc_id, position}, so the statement
     * returns one row per chash where {@link #apply}'s lookup returns every occurrence: a boilerplate chunk
     * held by N documents costs a search window one row, not N.
     */
    Map<String, Occurrence> firstOwners(String tenant, Map<String, Set<String>> chashesByCollection) {
        Map<String, Occurrence> first = new HashMap<>();
        for (Map.Entry<String, List<Occurrence>> e : fetchOccurrences(tenant, chashesByCollection, Set.of(), true)
                .entrySet()) {
            first.put(e.getKey(), e.getValue().get(0));
        }
        return first;
    }

    /** The key {@link #firstOwners} answers under. */
    static String ownerKey(String collection, String chash) {
        return key(collection, chash);
    }

    /**
     * @param firstOnly one occurrence per chash (the first, by {@code doc_id} then position) instead of all
     *                  of them; both orders are the database's own, so the two shapes agree on that row
     */
    private Map<String, List<Occurrence>> fetchOccurrences(String tenant,
                                                           Map<String, Set<String>> chashesByCollection,
                                                           Set<String> docKeys, boolean firstOnly) {
        Map<String, List<Occurrence>> out = new HashMap<>();
        if (chashesByCollection.isEmpty()) {
            return out;
        }
        Field<String> chashHex = ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH);
        DocFields docFields = DocFields.of(docKeys);
        List<Field<?>> select = occurrenceFields(chashHex);
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
                    for (var r : occurrenceQuery(ctx, tenant, e.getKey(), keys, select, firstOnly).fetch()) {
                        out.computeIfAbsent(key(e.getKey(), r.get(chashHex)), k -> new ArrayList<>())
                           .add(new Occurrence(r.get(CATALOG_DOCUMENT_CHUNKS.DOC_ID),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.POSITION),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER),
                                               Boolean.TRUE.equals(r.get(CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN)),
                                               r.get(CATALOG_DOCUMENTS.SOURCE_URI),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.LINE_START),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.LINE_END),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_START),
                                               r.get(CATALOG_DOCUMENT_CHUNKS.CHAR_END),
                                               docFields.read(r)));
                    }
                }
            }
            return null;
        });
        return out;
    }

    /** The columns every occurrence statement selects, before the document-level fields the filter names. */
    private static List<Field<?>> occurrenceFields(Field<String> chashHex) {
        return new ArrayList<>(List.of(chashHex,
            CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
            CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE, CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE,
            CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER, CATALOG_DOCUMENT_CHUNKS.LABELS_WRITTEN,
            CATALOG_DOCUMENTS.SOURCE_URI,
            CATALOG_DOCUMENT_CHUNKS.LINE_START, CATALOG_DOCUMENT_CHUNKS.LINE_END,
            CATALOG_DOCUMENT_CHUNKS.CHAR_START, CATALOG_DOCUMENT_CHUNKS.CHAR_END));
    }

    /**
     * The one statement behind both lookups, for ONE collection and one batch of chashes. Package-private so
     * a plan test can EXPLAIN the statement production runs, not a copy of it. {@code firstOnly} is {@code
     * SELECT DISTINCT ON (chash) ... ORDER BY chash, doc_id, position}; otherwise every occurrence, ordered
     * by {@code doc_id}, {@code position}.
     */
    static ResultQuery<Record> occurrenceQuery(DSLContext ctx, String tenant, String collection,
                                               List<byte[]> keys, List<Field<?>> select, boolean firstOnly) {
        var head = firstOnly
            ? ctx.select(select).distinctOn(CATALOG_DOCUMENT_CHUNKS.CHASH)
            : ctx.select(select);
        List<org.jooq.OrderField<?>> order = new ArrayList<>(3);
        if (firstOnly) {
            order.add(CATALOG_DOCUMENT_CHUNKS.CHASH);  // DISTINCT ON (chash) needs it as the leading key
        }
        order.add(CATALOG_DOCUMENT_CHUNKS.DOC_ID);
        order.add(CATALOG_DOCUMENT_CHUNKS.POSITION);
        return head
            .from(CATALOG_DOCUMENT_CHUNKS)
            .join(CATALOG_DOCUMENTS)
                // A tombstoned document owns nothing (nexus-mqd6t): its occurrence is not a hit.
                .on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                    .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID))
                    .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))
            .where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(tenant)
                .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(collection))
                .and(CATALOG_DOCUMENT_CHUNKS.CHASH.in(keys)))
            .orderBy(order);
    }

    /** The first and the full lookup, for a test that compares them. */
    Map<String, List<Occurrence>> allOccurrences(String tenant, Map<String, Set<String>> chashesByCollection) {
        return fetchOccurrences(tenant, chashesByCollection, Set.of(), false);
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
            Object answer = o != null && o.docValues().containsKey(key) ? o.docValues().get(key) : row.get(key);
            if (TAGS_KEY.equals(key) && answer != null) {
                answer = tagSet(answer);  // the chunk row's comma string, or a list, as a set (the meta's is one already)
            }
            m.put(key, answer instanceof ContentType ct ? ct.withRow(row.get(key)) : answer);
        }
        return m;
    }

    /**
     * Writes the owner's manifest span onto {@code row} (nexus-6pfgb.26): {@code line_start}, {@code line_end},
     * {@code chunk_start_char} and {@code chunk_end_char} are the position's own, where the shared chunk row's
     * are the last writer's. It writes each field the manifest HAS a value for and nothing else, so a key
     * the row lacked appears when the owner's manifest holds it; a NULL manifest value leaves that field as
     * the row has it, which is the row's own value ONLY if the row has not already been given another
     * owner's (see {@code PgVectorRepository#enrichSearchRows}, {@code labelStageFollows}). NULL is also what
     * a note, an orphan and a prose or PDF chunk carry for good, not only a row written before the manifest
     * had spans, so such an owner shows the chunk row's span beside its {@code source_uri}.
     */
    static void putSpan(Map<String, Object> row, Occurrence o) {
        if (o.lineStart() != null) {
            row.put("line_start", o.lineStart());
        }
        if (o.lineEnd() != null) {
            row.put("line_end", o.lineEnd());
        }
        if (o.charStart() != null) {
            row.put("chunk_start_char", o.charStart());
        }
        if (o.charEnd() != null) {
            row.put("chunk_end_char", o.charEnd());
        }
    }

    /**
     * A copy of {@code row} that speaks for one owning document: its id, position, span and own labels. A written
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
        putSpan(copy, o);
        if (copy.containsKey("span")) {
            // The line-range fallback of a row with no chunk_text_hash names this owner's lines.
            copy.put("span", PgVectorRepository.spanFromMeta(copy));
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
            Object value = d.getValue() instanceof ContentType ct ? ct.display() : d.getValue();
            if (TAGS_KEY.equals(d.getKey()) && value instanceof List<?> tags) {
                value = tags.stream().map(String::valueOf).collect(java.util.stream.Collectors.joining(","));
            }
            if (value == null) {
                copy.remove(d.getKey());
            } else {
                copy.put(d.getKey(), value);
            }
        }
        return copy;
    }
}
