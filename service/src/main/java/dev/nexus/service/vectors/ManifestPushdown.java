// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.ObjectMapper;

import org.jooq.JSONB;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * nexus-p89h4 and nexus-6pfgb.25 (RDR-236 Phase 3): the part of a label or document-key {@code where} that can run
 * INSIDE the vector search, as an EXISTS on the catalog manifest ({@code nexus.manifest_search_<dim>}, vectors-040).
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
 * and, on {@code page_number}, the numeric ranges. A negative operator keeps what it does not reject, so it
 * prunes little; it, {@code indexed_at}, {@code bib_year}, {@code bib_venue}, {@code bib_authors} and {@code
 * bib_citation_count} stay with the stage alone (the RDR's own split).
 *
 * <p>{@code content_type} is the one key whose answer depends on the operand, so the object holds an ARRAY of every
 * word the document answers to (its catalog type, the format the catalog derives, and the chunk row's value where
 * those leave it open), and the compiled test is "the array has the operand". The array is a superset of what
 * {@link ManifestLabelStage#matches} accepts for any operand.
 *
 * <p>The compiled form reuses {@link PgVectorRepository#planWhere}, so the operators, the tolerant number-or-text
 * equality and the three-valued jsonpath rules are the search's own.
 */
final class ManifestPushdown {

    /** The compiled predicate: a containment and a jsonpath over the per-occurrence object. Either may be null. */
    record Plan(JSONB containment, String jsonPath) {}

    /** Keys whose effective value is one string and whose equality and {@code $in} compile. */
    private static final Set<String> STRING_KEYS = Set.of(
        "section_title", "section_type", "title", "source_author",
        "bib_doi", "bib_openalex_id", "bib_semantic_scholar_id");

    /** {@code page_number} is the one numeric key whose ranges compile. */
    private static final String PAGE_NUMBER = "page_number";

    private static final Set<String> RANGE_OPERATORS = Set.of("$gte", "$gt", "$lte", "$lt");

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ManifestPushdown() {}

    /** Whether the stage predicate {@code p} has an in-SQL form. */
    static boolean pushable(ManifestLabelStage.Predicate p) {
        String op = p.operator();
        boolean positive = "$eq".equals(op) || "$in".equals(op);
        if (STRING_KEYS.contains(p.key()) || ManifestLabelStage.CONTENT_TYPE_KEY.equals(p.key())) {
            return positive;
        }
        if (PAGE_NUMBER.equals(p.key())) {
            return positive || RANGE_OPERATORS.contains(op);
        }
        return false;
    }

    /**
     * The in-SQL form of {@code predicates} (ANDed, as the stage ANDs them over one occurrence), or {@code null}
     * when none of them can run in SQL.
     */
    static Plan of(List<ManifestLabelStage.Predicate> predicates) {
        Map<String, Object> where = new LinkedHashMap<>();
        List<String> contentType = new ArrayList<>();
        for (ManifestLabelStage.Predicate p : predicates) {
            if (!pushable(p)) {
                continue;
            }
            if (ManifestLabelStage.CONTENT_TYPE_KEY.equals(p.key())) {
                contentType.add(contentTypePath(p));
                continue;
            }
            Map<String, Object> op = new LinkedHashMap<>();
            op.put(p.operator(), p.operand());
            where.put(p.key(), op);
        }
        PgVectorRepository.WherePlan plan = PgVectorRepository.planWhere(where.isEmpty() ? null : where);
        List<String> paths = new ArrayList<>(contentType);
        if (plan.jsonPath() != null) {
            paths.add(0, plan.jsonPath());
        }
        if (plan.containment() == null && paths.isEmpty()) {
            return null;
        }
        return new Plan(plan.containment(), paths.isEmpty() ? null : String.join(" && ", paths));
    }

    /** {@code $eq} / {@code $in} on {@code content_type}: some word in the document's array equals the operand. */
    private static String contentTypePath(ManifestLabelStage.Predicate p) {
        List<?> items = "$in".equals(p.operator()) ? (List<?>) p.operand() : List.of(String.valueOf(p.operand()));
        if (items.isEmpty()) {
            return "(1 == 2)";
        }
        List<String> any = new ArrayList<>(items.size());
        for (Object item : items) {
            any.add("exists($.\"content_type\"[*] ? (@ == " + stringLiteral(String.valueOf(item)) + "))");
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
