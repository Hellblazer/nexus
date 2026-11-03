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
 * counts a missing value as {@code ""}), and why route (a) compiles only the POSITIVE operators: {@code $eq}, {@code $in}
 * and the ranges the keys have. A negative operator keeps what it does not reject, so it prunes little, and over the
 * {@code content_type} array below, which lists more words than the stage accepts for one operand, a negation would
 * admit LESS than the stage keeps. So on route (a) a negative operator stays with the stage alone (the RDR's own split).
 *
 * <p><b>Route (b) (nexus-qpt52).</b> Route (b) has no stage after the search, so the SQL is the answer and {@link
 * #ofExact} compiles EVERY predicate, negative operators and ranges included, to the jsonpath that is true on the
 * object exactly when {@link ManifestLabelStage#testValue} is. {@code ManifestRouteBDifferentialTest} runs a matrix of
 * predicates through the stage and the SQL on one fixture and requires identical sets. The numeric keys and the keys
 * nexus-qpt52 added ({@link #EXACT_KEYS}) use that exact form on route (a) as well for their positive operators and
 * ranges, which keeps the superset rule (an exact test admits exactly what the stage keeps).
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
 * returns fewer rows than asked is re-run once exact, unless a bounded manifest count ({@code
 * manifest_qualifying_hits}, vectors-047, nexus-8ptg4) shows the page already holds every chunk the predicate admits.
 * The EXISTS is a correlated SubPlan per HNSW candidate (pinned in PlainSearchTextGatedSearchExplainTest), about 17
 * microseconds a candidate measured against 3 for the chunk-row filter it replaced. The cost is the per-occurrence
 * object, not the plan shape: building it in the WHERE, which does let PostgreSQL convert the EXISTS to a semi-join,
 * did not change the time and doubled the plan time of a search (the engine inlines the function body on every call).
 * vectors-045 builds only the keys the predicate names, which cuts the cost by about a third; a predicate written as a
 * column test the planner has statistics for would let it drive from the manifest and find a rare label in
 * milliseconds, and is a larger change to the function interface.
 *
 * <p>The other keys' positive forms reuse {@link PgVectorRepository#planWhere}, so their operators, tolerant
 * number-or-text equality and three-valued jsonpath rules are the search's own. The exact form (see {@link
 * #exactPath}) reads a numeric key as a number whichever JSON form it has: {@code exists($."page_number" ? (@.number()
 * >= 3))}. {@code .number()} turns a JSON number or a numeric string into an exact {@code numeric}; a value that is not
 * numeric (the empty string a missing page is counted as, text such as {@code "abc"}) is an error inside the filter,
 * which jsonpath turns into "unknown" and so into "no match", the stage's {@code comparable == false}. It needs
 * PostgreSQL 17 (the engine's floor). Residuals, none reached by the writers: the stage parses with {@link
 * java.math.BigDecimal}, which also reads non-ASCII digits, and PostgreSQL does not; a string range compares in UTF-8
 * byte order here and in UTF-16 order in Java, which differ only between supplementary characters and U+E000 to U+FFFF;
 * a catalog meta number stored in exponent form prints differently in Jackson and PostgreSQL, which matters only to a
 * string comparison of it.
 */
final class ManifestPushdown {

    /** The compiled predicate: a containment and a jsonpath over the per-occurrence object. Either may be null. */
    record Plan(JSONB containment, String jsonPath) {}

    /**
     * Keys whose effective value is one scalar and whose equality and {@code $in} compile. The catalog-meta ones
     * (nexus-6pfgb.29) are scalars too: {@code quality_gate_overridden} is a boolean, so {@link PgVectorRepository#planWhere}'s
     * tolerant form gives its equality both the JSON literal and the text one, as the stage's text compare does;
     * the numeric ones ({@code ttl_days}, {@code frecency_score}) are in {@link #EXACT_KEYS}.
     */
    private static final Set<String> STRING_KEYS = Set.of(
        "section_title", "section_type", "title", "source_author",
        "bib_doi", "bib_openalex_id", "bib_semantic_scholar_id",
        "category", "source_agent", "extraction_method", "extraction_source", "quality_gate_overridden");

    /**
     * Keys whose effective value is a SET of words, held in the object as an array: {@code content_type} (every word
     * the document answers to) and {@code tags}. A positive operator compiles to "some member equals the operand".
     */
    private static final Set<String> ARRAY_KEYS = Set.of(ManifestLabelStage.CONTENT_TYPE_KEY, ManifestLabelStage.TAGS_KEY);

    private static final Set<String> RANGE_OPERATORS = Set.of("$gte", "$gt", "$lte", "$lt");

    /**
     * The keys compiled by {@link #exactPath} on route (a) too: the numeric keys ({@link
     * ManifestLabelStage#NUMERIC_KEYS}: {@code page_number}, {@code bib_year}, {@code bib_citation_count}, {@code
     * ttl_days}, {@code frecency_score}), which compare as numbers across a number and its text where {@link
     * PgVectorRepository#planWhere}'s two literal forms miss a stored {@code "5.0"} or an operand {@code " 5 "}, and
     * the string keys nexus-qpt52 added ({@code bib_authors}, {@code bib_venue}, {@code indexed_at}). Equality,
     * {@code $in} and ranges; a negative operator is route (b)'s alone ({@link #ofExact}).
     */
    private static final Set<String> EXACT_KEYS = Set.of(
        "page_number", "bib_year", "bib_citation_count", "ttl_days", "frecency_score",
        "bib_authors", "bib_venue", "indexed_at");

    /** The object key that holds the text a {@code content_type} range reads, see {@link #exactRange}. */
    static final String CONTENT_TYPE_SHOWN_KEY = "content_type_shown";

    /** Refuse a numeric operand whose plain rendering would be absurdly long (an exponent of a million). */
    private static final int MAX_NUMERIC_DIGITS = 1000;

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ManifestPushdown() {}

    /** Whether the stage predicate {@code p} has an in-SQL form. */
    static boolean pushable(ManifestLabelStage.Predicate p) {
        String op = p.operator();
        boolean positive = "$eq".equals(op) || "$in".equals(op);
        if (EXACT_KEYS.contains(p.key())) {
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
            if (EXACT_KEYS.contains(p.key())) {
                own.add(exactPath(p));
                continue;
            }
            if (ARRAY_KEYS.contains(p.key())) {
                own.add(memberPath(p));
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

    /**
     * The route (b) form of {@code predicates} (nexus-qpt52): EVERY predicate compiled, positive and negative, each to
     * the jsonpath that is true on a per-occurrence object exactly when {@link ManifestLabelStage#testValue} is true on
     * the same value. Route (b) has no stage after the search, so this is the answer, not a pre-filter: it may neither
     * admit more nor less than the stage keeps. No containment is produced (a containment is type-strict, the stage
     * is not), and the result is never {@code null} for a non-empty list.
     *
     * <p>Only route (b) uses it for the keys route (a) already pushes in the superset form ({@link #of}), because a
     * NEGATIVE test over route (a)'s {@code content_type} array, which lists more words than the stage accepts for any
     * one operand, would admit less than the stage keeps. Route (b)'s own array is exact (vectors-048, vectors-049).
     */
    static Plan ofExact(List<ManifestLabelStage.Predicate> predicates) {
        if (predicates.isEmpty()) {
            return null;
        }
        List<String> paths = new ArrayList<>(predicates.size());
        for (ManifestLabelStage.Predicate p : predicates) {
            paths.add(exactPath(p));
        }
        return new Plan(null, String.join(" && ", paths));
    }

    /** Whether any of {@code predicates} is a negative operator on {@code content_type}, see {@link #ofExact}. */
    static boolean negatesContentType(List<ManifestLabelStage.Predicate> predicates) {
        for (ManifestLabelStage.Predicate p : predicates) {
            if (ManifestLabelStage.CONTENT_TYPE_KEY.equals(p.key()) && ("$ne".equals(p.operator()) || "$nin".equals(p.operator()))) {
                return true;
            }
        }
        return false;
    }

    private enum Kind { STRING, NUMERIC, SET }

    private static Kind kindOf(String key) {
        if (ARRAY_KEYS.contains(key)) {
            return Kind.SET;
        }
        return ManifestLabelStage.NUMERIC_KEYS.contains(key) ? Kind.NUMERIC : Kind.STRING;
    }

    /**
     * The jsonpath predicate that is true on an occurrence's object exactly when the stage's {@code testValue} is,
     * for one predicate. Every piece is built from {@code exists(...)}, which is true or false and never unknown, so a
     * negative operator is plain {@code !}: {@code $ne} and {@code $nin} are the stage's {@code !equals} and {@code
     * noneMatch}, the negation of {@code $eq} and {@code $in} in every branch of {@code testValue} (an unlabelled
     * value included: the object holds {@code ""} for a missing value, and {@code "" == ""} is the stage's "an
     * explicit empty operand names the unlabelled value").
     *
     * <p>The three kinds. A STRING key (the labels but {@code page_number}, {@code title}, {@code source_author}, the
     * id and text {@code bib_*} keys, {@code indexed_at}, the string meta keys) compares {@code String.valueOf} of
     * both sides: {@code @.string()} gives a number or a boolean its text, as Java does. A NUMERIC key
     * ({@link ManifestLabelStage#NUMERIC_KEYS}) compares as numbers when both sides read as numbers
     * ({@code @.number()} reads a JSON number or a numeric string, and is an error, hence "no match", for anything
     * else), and as text otherwise; which of the two applies is known from the operand when the path is built.
     * A SET key ({@code tags}, {@code content_type}) is membership of the array the object holds.
     */
    static String exactPath(ManifestLabelStage.Predicate p) {
        String key = p.key();
        return switch (p.operator()) {
            case "$eq" -> exactEquals(key, p.operand());
            case "$ne" -> "!" + exactEquals(key, p.operand());
            case "$in" -> exactAny(key, (List<?>) p.operand());
            case "$nin" -> "!" + exactAny(key, (List<?>) p.operand());
            case "$gte", "$gt", "$lte", "$lt" -> exactRange(p);
            default -> throw new IllegalArgumentException("unsupported where operator '" + p.operator() + "' for field '" + key + "'");
        };
    }

    private static String exactAny(String key, List<?> operands) {
        if (operands.isEmpty()) {
            return "(1 == 2)";
        }
        List<String> any = new ArrayList<>(operands.size());
        for (Object operand : operands) {
            any.add(exactEquals(key, operand));
        }
        return "(" + String.join(" || ", any) + ")";
    }

    private static String exactEquals(String key, Object operand) {
        String text = String.valueOf(operand);
        return switch (kindOf(key)) {
            case SET -> "exists($." + stringLiteral(key) + "[*] ? (@ == " + stringLiteral(text) + "))";
            case STRING -> "exists($." + stringLiteral(key) + " ? (@.string() == " + stringLiteral(text) + "))";
            case NUMERIC -> {
                if (operand instanceof String s && s.isEmpty()) {
                    // the unlabelled value, which the object holds as ""
                    yield "exists($." + stringLiteral(key) + " ? (@ == \"\"))";
                }
                BigDecimal n = ManifestLabelStage.asNumber(operand);
                yield n != null
                    ? "exists($." + stringLiteral(key) + " ? (@.number() == " + plainNumber(key, operand, n) + "))"
                    : "exists($." + stringLiteral(key) + " ? (@.string() == " + stringLiteral(text) + "))";
            }
        };
    }

    private static String exactRange(ManifestLabelStage.Predicate p) {
        // content_type has no range meaning of its own: the stage reads the value the row would show.
        String key = ManifestLabelStage.CONTENT_TYPE_KEY.equals(p.key()) ? CONTENT_TYPE_SHOWN_KEY : p.key();
        Kind kind = kindOf(p.key());
        if (kind == Kind.SET && !ManifestLabelStage.CONTENT_TYPE_KEY.equals(p.key())) {
            return "(1 == 2)";  // a range over a set of names has no meaning
        }
        String cmp = switch (p.operator()) {
            case "$gte" -> ">=";
            case "$gt" -> ">";
            case "$lte" -> "<=";
            default -> "<";
        };
        if (p.operand() instanceof Number) {
            if (kind != Kind.NUMERIC) {
                return "(1 == 2)";  // a numeric operand compares only on a numeric key
            }
            BigDecimal n = ManifestLabelStage.asNumber(p.operand());
            if (n == null) {
                throw new IllegalArgumentException(
                    p.operator() + " for field '" + p.key() + "' expects a finite numeric operand, got " + p.operand());
            }
            return "exists($." + stringLiteral(key) + " ? (@.number() " + cmp + " " + plainNumber(p.key(), p.operand(), n) + "))";
        }
        // A string operand compares lexically against a label that is not a number and not unlabelled.
        String text = String.valueOf(p.operand());
        return "exists($." + stringLiteral(key) + " ? (@.type() != \"number\" && @.string() != \"\" && @.string() "
            + cmp + " " + stringLiteral(text) + "))";
    }

    private static String plainNumber(String key, Object operand, BigDecimal n) {
        if (Math.abs((long) n.scale()) > MAX_NUMERIC_DIGITS || (long) n.precision() - n.scale() > MAX_NUMERIC_DIGITS) {
            throw new IllegalArgumentException(
                "where operand for field '" + key + "' is a number out of range: " + operand);
        }
        return n.toPlainString();
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
