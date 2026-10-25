// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashHex;
import dev.nexus.service.db.TenantScope;

import java.math.BigDecimal;
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
 *   <li>NULL: a negative operator ({@code $ne}, {@code $nin}) keeps an unlabelled occurrence (SQL
 *       {@code IS DISTINCT FROM}); every other operator drops it. An unlabelled occurrence is one whose
 *       manifest label is NULL <em>and</em> whose chunk row carries no label of its own (see the next
 *       item).</li>
 *   <li>Legacy fallback: a manifest row written before catalog-046 has NULL labels, and nothing
 *       backfills them. Reading NULL as "no label" would make {@code section_type!=references} stop
 *       filtering every existing document until it is re-indexed, so an occurrence whose manifest label
 *       is NULL takes the chunk row's own (last-writer, possibly another document's) label instead,
 *       exactly the value the filter read before this change. A re-index fills the manifest and the
 *       fallback stops being consulted for that row.</li>
 *   <li>A hit with no live manifest row (an orphan) is kept or dropped on its chunk row's own label
 *       alone, with no {@code doc_id}.</li>
 *   <li>The caller's limit applies after expansion and de-duplication, never before.</li>
 * </ul>
 *
 * <p><b>Known cost.</b> The search is cut at {@link #OVERFETCH} times the limit before this stage
 * filters, so a filter that rejects most of the neighbourhood (a positive filter on a rare label) can
 * return fewer rows than the limit where the old in-index {@code hnsw.iterative_scan} would have kept
 * walking. The filter that matters in practice, a negative one ({@code section_type!=references}),
 * rejects a small fraction.
 */
final class ManifestLabelStage {

    /** The metadata keys answered from the manifest. */
    static final Set<String> LABEL_KEYS = Set.of("section_title", "section_type", "page_number");

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
     * @param rest   the {@code where} without the label keys: what the search itself receives. The same
     *               instance as the input when no label key is present.
     * @param labels the label predicates, ANDed
     */
    record Split(Map<String, Object> rest, List<Predicate> labels) {
        boolean active() {
            return !labels.isEmpty();
        }
    }

    /**
     * Separate the label predicates from a Chroma-style {@code where} map. Operator shapes the search's
     * own planner accepts are accepted here; anything else fails with the same kind of
     * {@link IllegalArgumentException} (the handler's 400 ladder).
     */
    static Split split(Map<String, Object> where) {
        if (where == null || where.isEmpty()) {
            return new Split(where, List.of());
        }
        boolean any = false;
        for (String key : where.keySet()) {
            if (LABEL_KEYS.contains(key)) {
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
            if (!LABEL_KEYS.contains(key)) {
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
        if (label == null) {
            // IS DISTINCT FROM: a negative operator keeps an unlabelled occurrence, any other drops it.
            return p.operator().equals("$ne") || p.operator().equals("$nin");
        }
        return switch (p.operator()) {
            case "$eq" -> sameValue(label, p.operand());
            case "$ne" -> !sameValue(label, p.operand());
            case "$in" -> ((List<?>) p.operand()).stream().anyMatch(o -> sameValue(label, o));
            case "$nin" -> ((List<?>) p.operand()).stream().noneMatch(o -> sameValue(label, o));
            case "$gte" -> compare(label, p.operand()) >= 0 && comparable(label, p.operand());
            case "$lte" -> compare(label, p.operand()) <= 0 && comparable(label, p.operand());
            case "$gt" -> compare(label, p.operand()) > 0 && comparable(label, p.operand());
            case "$lt" -> compare(label, p.operand()) < 0 && comparable(label, p.operand());
            default -> throw new IllegalStateException("operator validated in split: " + p.operator());
        };
    }

    /**
     * Equality with the search planner's tolerant typing: a stored {@code 5} equals the operand
     * {@code "5"} and the reverse, so {@code page_number=5} and {@code page_number="5"} both match.
     */
    private static boolean sameValue(Object label, Object operand) {
        BigDecimal a = asNumber(label);
        BigDecimal b = asNumber(operand);
        if (a != null && b != null) {
            return a.compareTo(b) == 0;
        }
        return String.valueOf(label).equals(String.valueOf(operand));
    }

    /** A numeric operand compares numerically, a string operand lexically; across the two nothing matches. */
    private static boolean comparable(Object label, Object operand) {
        if (operand instanceof Number) {
            return asNumber(label) != null;
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
     * One live manifest occurrence of a chash: the owning document, the position and its own labels, and
     * the document's {@code source_uri} (a row that carries one is re-pointed at the document it speaks for).
     */
    record Occurrence(String docId, int position, String sectionTitle, String sectionType, Integer pageNumber,
                      String sourceUri) {}

    /**
     * The live manifest occurrences of each (collection, chash) the rows name, in (doc_id, position)
     * order. One bounded statement per collection and batch, under the tenant's row-level security, joined
     * to the document so a tombstoned document owns nothing.
     */
    private Map<String, List<Occurrence>> occurrences(String tenant, List<Map<String, Object>> rows) {
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
        tenantScope.withTenant(tenant, ctx -> {
            for (Map.Entry<String, Set<String>> e : chashesByCollection.entrySet()) {
                List<String> hexes = new ArrayList<>(e.getValue());
                for (int start = 0; start < hexes.size(); start += LOOKUP_BATCH) {
                    List<String> batch = hexes.subList(start, Math.min(start + LOOKUP_BATCH, hexes.size()));
                    List<byte[]> keys = new ArrayList<>(batch.size());
                    for (String hex : batch) {
                        keys.add(Chash.fromHex(hex).toBytes());
                    }
                    var fetched = ctx.select(ChashHex.hex(CATALOG_DOCUMENT_CHUNKS.CHASH),
                                             CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                                             CATALOG_DOCUMENT_CHUNKS.SECTION_TITLE,
                                             CATALOG_DOCUMENT_CHUNKS.SECTION_TYPE,
                                             CATALOG_DOCUMENT_CHUNKS.PAGE_NUMBER,
                                             CATALOG_DOCUMENTS.SOURCE_URI)
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
                        out.computeIfAbsent(key(e.getKey(), r.value1()), k -> new ArrayList<>())
                           .add(new Occurrence(r.value2(), r.value3(), r.value4(), r.value5(), r.value6(),
                                               r.value7()));
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
        Map<String, List<Occurrence>> owners = occurrences(tenant, rows);
        List<Map<String, Object>> out = new ArrayList<>(Math.min(rows.size(), Math.max(limit, 0)));
        for (Map<String, Object> row : rows) {
            Object id = row.get("id");
            Object coll = row.get("collection");
            List<Occurrence> occ = id instanceof String ch && coll instanceof String c
                ? owners.getOrDefault(key(c, ch), List.of()) : List.of();
            if (occ.isEmpty()) {
                // No live manifest row: judged on the chunk row's own label, no document to name.
                if (passes(predicates, rowLabels(row, null))) {
                    out.add(row);
                }
            } else {
                Set<String> seenDocs = new LinkedHashSet<>();
                for (Occurrence o : occ) {
                    if (seenDocs.contains(o.docId())) {
                        continue;
                    }
                    Map<String, Object> labels = rowLabels(row, o);
                    if (passes(predicates, labels)) {
                        seenDocs.add(o.docId());
                        out.add(expanded(row, o, labels));
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
     * The effective label of one occurrence, per key: the manifest's own, else (a row written before
     * catalog-046) the chunk row's, else absent. The chunk row's labels are flattened into the row map.
     */
    private static Map<String, Object> rowLabels(Map<String, Object> row, Occurrence o) {
        Map<String, Object> m = new HashMap<>(4);
        m.put("section_title", o != null && o.sectionTitle() != null ? o.sectionTitle() : row.get("section_title"));
        m.put("section_type", o != null && o.sectionType() != null ? o.sectionType() : row.get("section_type"));
        m.put("page_number", o != null && o.pageNumber() != null ? o.pageNumber() : row.get("page_number"));
        return m;
    }

    /** A copy of {@code row} that speaks for one owning document: its id, position and own labels. */
    private static Map<String, Object> expanded(Map<String, Object> row, Occurrence o, Map<String, Object> labels) {
        Map<String, Object> copy = new LinkedHashMap<>(row);
        copy.put("doc_id", o.docId());
        copy.put("position", o.position());
        if (copy.containsKey("source_uri") && o.sourceUri() != null) {
            // include_source_uri resolved one owner's URI for the shared chunk; this row speaks for o's document.
            copy.put("source_uri", o.sourceUri());
        }
        for (Map.Entry<String, Object> e : labels.entrySet()) {
            if (e.getValue() != null) {
                copy.put(e.getKey(), e.getValue());
            }
        }
        return copy;
    }
}
