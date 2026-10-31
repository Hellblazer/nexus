// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.ObjectMapper;

import org.jooq.JSONB;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * nexus-p89h4 and nexus-6pfgb.25 (RDR-236 Phase 3): the part of a label or document-key {@code where} that can run
 * INSIDE the vector search, as an EXISTS on the catalog manifest ({@code nexus.manifest_search_<dim>}, vectors-041).
 *
 * <p>Why. {@link ManifestLabelStage} judges these keys per owning document after the search, over a cut of four
 * times the limit (at most 1200). A positive filter on a rare label, or on {@code title}, which names one document,
 * is held by few chunks of a large collection and none of them is likely to be in the nearest 40, so the page came
 * back short or empty where the old in-index predicate kept walking under {@code hnsw.iterative_scan}. With the
 * predicate inside the search the scan keeps going until the page is full.
 *
 * <p>The contract with the stage. The function evaluates the compiled predicate against one JSON object built per
 * manifest occurrence, the owning document's EFFECTIVE value of each key (the same fallbacks the stage applies:
 * a written manifest row's own label, else the chunk row's; the catalog title or author when it is not blank, else
 * the chunk row's). The function ADMITS A CHUNK when any live occurrence passes; the stage still names the
 * qualifying owner and is the exact judge of every operator. So what is compiled here has one rule: it may admit
 * more than the stage keeps, never less. That is why a missing value is the empty string in the object (the stage
 * counts a missing value as {@code ""}), and why only the POSITIVE operators are compiled: {@code $eq}, {@code $in}
 * and, on the numeric keys ({@code page_number}, {@code ttl_days}, {@code frecency_score}), the numeric ranges. A negative operator keeps what it does not reject, so it
 * prunes little; it, {@code indexed_at}, {@code bib_year}, {@code bib_venue}, {@code bib_authors} and {@code
 * bib_citation_count} stay with the stage alone (the RDR's own split).
 *
 * <p>{@code content_type} is the one key whose answer depends on the operand, so the object holds an ARRAY of every
 * word the document answers to (its catalog type, the format the catalog derives, and the chunk row's value where
 * those leave it open), and the compiled test is "the array has the operand". The array is a superset of what
 * {@link ManifestLabelStage#matches} accepts for any operand. {@code tags} is the other array key: the object holds
 * the document's tag set (the meta's array or comma string, else the chunk row's comma string, split by {@code
 * nexus.catalog_tag_set}; {@code [""]} for none), and the test is the same membership.
 *
 * <p>The catalog-meta keys (nexus-6pfgb.29) hold the document's meta value when the meta HAS the key and the chunk
 * row's value otherwise, the presence fallback the stage applies; {@code category}, {@code source_agent}, {@code
 * extraction_method}, {@code extraction_source} and {@code quality_gate_overridden} compile like the other scalars
 * (the boolean through the planner's tolerant JSON-or-text form), {@code ttl_days} and {@code frecency_score} like
 * {@code page_number}, ranges included.
 *
 * <p><b>Short pages (nexus-6pfgb.61, RDR-236 assumption A8).</b> Below the cardinality router's threshold (20,000
 * rows by default) the exact plan runs and the EXISTS is evaluated over every row, so the page is complete. Above it
 * the EXISTS is a filter on the HNSW scan under {@code hnsw.iterative_scan}, which stops at {@code
 * hnsw.max_scan_tuples} or its memory budget: a rare label whose chunks rank past the cap yields the ones the walk
 * reached, a SHORT page that is not empty, which the empty-page re-run (nexus-bq06h) never repaired. Measured on 21,000
 * chunks with the cap at its 1,000 minimum: 1 of 3 rows found. The repair is {@code
 * PgVectorRepository#exactOnUnderReturn} with {@code rerunWhenShort}: a statement that carries this predicate and
 * returns fewer rows than asked is re-run once exact. The EXISTS is a correlated SubPlan per HNSW candidate (pinned
 * in PlainSearchTextGatedSearchExplainTest), about 17 microseconds a candidate measured against 3 for the chunk-row
 * filter it replaced. The cost is the per-occurrence object, not the plan shape: building it in the WHERE, which does
 * let PostgreSQL convert the EXISTS to a semi-join, did not change the time and doubled the plan time of a search
 * (the engine inlines the function body on every call). vectors-045 builds only the keys the predicate names, which
 * cuts the cost by about a third; a predicate written as a column test the planner has statistics for would let it
 * drive from the manifest and find a rare label in milliseconds, and is a larger change to the function interface.
 *
 * <p>The compiled form reuses {@link PgVectorRepository#planWhere}, so the operators, the tolerant number-or-text
 * equality and the three-valued jsonpath rules are the search's own, with one exception: a NUMERIC range on
 * a numeric key ({@code page_number}, nexus-6pfgb.64; {@code ttl_days} and {@code frecency_score}, nexus-6pfgb.29) is compiled here. {@link PgVectorRepository#planWhere} keeps the retired
 * type-strict range (a numeric operand never matches a stored string), but the stage's {@code comparable} accepts a
 * numeric string on a numeric key, so a legacy chunk row that stored {@code page_number} as {@code "5"} is KEPT by
 * the stage for {@code page_number >= 3}. The pushed range therefore reads the stored value as a number whichever
 * JSON form it has: {@code exists($."page_number" ? (@.number() >= 3))}. {@code .number()} turns a JSON number or
 * a numeric string into an exact {@code numeric}; a value that is not numeric (the empty string a missing page is
 * counted as, text such as {@code "abc"}) is an error inside the filter, which jsonpath turns into "unknown" and so
 * into "no match", the stage's {@code comparable == false}. It needs PostgreSQL 17 (the engine's floor). Residual:
 * the stage parses with {@link java.math.BigDecimal}, which also reads non-ASCII digits; PostgreSQL does not, so a
 * legacy page stored as such text is kept by the stage and rejected by the EXISTS. A STRING operand on a range
 * keeps the strict string comparison on both sides (the stage compares a string label lexically and treats a
 * number as not comparable), so nothing differs there. EQUALITY on a numeric key keeps the planner's two literal
 * forms ({@code 5} or {@code "5"}), so a stored {@code "5.0"}, which the stage reads as 5, is rejected by the
 * EXISTS; the writers store numbers, and the page-number case predates this class.
 */
final class ManifestPushdown {

    /** The compiled predicate: a containment and a jsonpath over the per-occurrence object. Either may be null. */
    record Plan(JSONB containment, String jsonPath) {}

    /**
     * Keys whose effective value is one scalar and whose equality and {@code $in} compile. The catalog-meta ones
     * (nexus-6pfgb.29) are scalars too: {@code quality_gate_overridden} is a boolean, so {@link PgVectorRepository#planWhere}'s
     * tolerant form gives its equality both the JSON literal and the text one, as the stage's text compare does;
     * {@code ttl_days} and {@code frecency_score} are numbers and also take ranges ({@link #NUMERIC_RANGE_KEYS}).
     */
    private static final Set<String> STRING_KEYS = Set.of(
        "section_title", "section_type", "title", "source_author",
        "bib_doi", "bib_openalex_id", "bib_semantic_scholar_id",
        "category", "source_agent", "extraction_method", "extraction_source", "quality_gate_overridden",
        "ttl_days", "frecency_score");

    /**
     * The numeric keys whose ranges compile: the stored value is read as a number whether it is a JSON number or a
     * numeric string, see the class comment ({@code .number()}).
     */
    private static final Set<String> NUMERIC_RANGE_KEYS = Set.of("page_number", "ttl_days", "frecency_score");

    /**
     * Keys whose effective value is a SET of words, held in the object as an array: {@code content_type} (every word
     * the document answers to) and {@code tags}. A positive operator compiles to "some member equals the operand".
     */
    private static final Set<String> ARRAY_KEYS = Set.of(ManifestLabelStage.CONTENT_TYPE_KEY, ManifestLabelStage.TAGS_KEY);

    private static final Set<String> RANGE_OPERATORS = Set.of("$gte", "$gt", "$lte", "$lt");

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ManifestPushdown() {}

    /** Whether the stage predicate {@code p} has an in-SQL form. */
    static boolean pushable(ManifestLabelStage.Predicate p) {
        String op = p.operator();
        boolean positive = "$eq".equals(op) || "$in".equals(op);
        if (NUMERIC_RANGE_KEYS.contains(p.key())) {
            return positive || RANGE_OPERATORS.contains(op);
        }
        if (STRING_KEYS.contains(p.key()) || ARRAY_KEYS.contains(p.key())) {
            return positive;
        }
        return false;
    }

    /**
     * The in-SQL form of {@code predicates} (ANDed, as the stage ANDs them over one occurrence), or {@code null}
     * when none of them can run in SQL.
     */
    static Plan of(List<ManifestLabelStage.Predicate> predicates) {
        Map<String, Object> where = new LinkedHashMap<>();
        List<String> own = new ArrayList<>();
        for (ManifestLabelStage.Predicate p : predicates) {
            if (!pushable(p)) {
                continue;
            }
            if (ARRAY_KEYS.contains(p.key())) {
                own.add(memberPath(p));
                continue;
            }
            if (isNumericPageRange(p)) {
                own.add(pageNumberRangePath(p));
                continue;
            }
            Map<String, Object> op = new LinkedHashMap<>();
            op.put(p.operator(), p.operand());
            where.put(p.key(), op);
        }
        PgVectorRepository.WherePlan plan = PgVectorRepository.planWhere(where.isEmpty() ? null : where);
        List<String> paths = new ArrayList<>(own);
        if (plan.jsonPath() != null) {
            paths.add(0, plan.jsonPath());
        }
        if (plan.containment() == null && paths.isEmpty()) {
            return null;
        }
        return new Plan(plan.containment(), paths.isEmpty() ? null : String.join(" && ", paths));
    }

    /** A range on a numeric key with a numeric operand: the one range this class compiles itself. */
    private static boolean isNumericPageRange(ManifestLabelStage.Predicate p) {
        return NUMERIC_RANGE_KEYS.contains(p.key()) && RANGE_OPERATORS.contains(p.operator())
            && p.operand() instanceof Number;
    }

    /**
     * {@code exists($."page_number" ? (@.number() >= N))}: the stored page read as a number whether it is a JSON
     * number or a numeric string (see the class comment). The operand is rendered as plain numeric text, as {@link
     * PgVectorRepository#planWhere} renders a numeric operand; a value that is not finite is refused with the same
     * 400-shaped error the search's own planner gives it.
     */
    private static String pageNumberRangePath(ManifestLabelStage.Predicate p) {
        BigDecimal operand;
        try {
            operand = new BigDecimal(p.operand().toString());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                p.operator() + " for field '" + p.key() + "' expects a finite numeric operand, got " + p.operand(), e);
        }
        String cmp = switch (p.operator()) {
            case "$gte" -> ">=";
            case "$gt" -> ">";
            case "$lte" -> "<=";
            default -> "<";
        };
        return "exists($." + stringLiteral(p.key()) + " ? (@.number() " + cmp + " " + operand.toPlainString() + "))";
    }

    /** {@code $eq} / {@code $in} on a set-valued key: some word in the document's array equals the operand. */
    private static String memberPath(ManifestLabelStage.Predicate p) {
        List<?> items = "$in".equals(p.operator()) ? (List<?>) p.operand() : List.of(String.valueOf(p.operand()));
        if (items.isEmpty()) {
            return "(1 == 2)";
        }
        List<String> any = new ArrayList<>(items.size());
        for (Object item : items) {
            any.add("exists($." + stringLiteral(p.key()) + "[*] ? (@ == " + stringLiteral(String.valueOf(item)) + "))");
        }
        return "(" + String.join(" || ", any) + ")";
    }

    private static String stringLiteral(String text) {
        try {
            return MAPPER.writeValueAsString(text);
        } catch (Exception e) {
            throw new IllegalArgumentException("where operand is not JSON-serializable: " + text, e);
        }
    }
}
