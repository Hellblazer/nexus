// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.JSONB;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * nexus-p89h4 (RDR-236 Phase 3): the part of a label or document-key {@code where} that can run
 * INSIDE the vector search, as an EXISTS on the catalog manifest ({@code nexus.manifest_search_<dim>}, vectors-040).
 *
 * <p>Why. {@link ManifestLabelStage} judges these keys per owning document after the search, over a cut of four
 * times the limit (at most 1200). A positive filter on a rare label is held by few chunks of a large collection and none of them is likely to be in the nearest 40, so the page came
 * back short or empty where the old in-index predicate kept walking under {@code hnsw.iterative_scan}. With the
 * predicate inside the search the scan keeps going until the page is full.
 *
 * <p>The contract with the stage. The function evaluates the compiled predicate against one JSON object built per
 * manifest occurrence, the owning document's EFFECTIVE value of each key (the same fallbacks the stage applies:
 * a written manifest row's own label, else the chunk row's; the catalog author when it is not blank, else
 * the chunk row's). The function ADMITS A CHUNK when any live occurrence passes; the stage still names the
 * qualifying owner and is the exact judge of every operator. So what is compiled here has one rule: it may admit
 * more than the stage keeps, never less. That is why a missing value is the empty string in the object (the stage
 * counts a missing value as {@code ""}), and why only the POSITIVE operators are compiled: {@code $eq}, {@code $in}
 * and, on {@code page_number}, the numeric ranges. A negative operator keeps what it does not reject, so it
 * prunes little; it, {@code title}, {@code content_type}, {@code indexed_at}, {@code bib_year}, {@code bib_venue},
 * {@code bib_authors} and {@code bib_citation_count} stay with the stage alone (the RDR's own split).
 *
 * <p>The compiled form reuses {@link PgVectorRepository#planWhere}, so the operators, the tolerant number-or-text
 * equality and the three-valued jsonpath rules are the search's own.
 */
final class ManifestPushdown {

    /** The compiled predicate: a containment and a jsonpath over the per-occurrence object. Either may be null. */
    record Plan(JSONB containment, String jsonPath) {}

    /** Keys whose effective value is one string and whose equality and {@code $in} compile. */
    private static final Set<String> STRING_KEYS = Set.of(
        "section_title", "section_type", "source_author",
        "bib_doi", "bib_openalex_id", "bib_semantic_scholar_id");

    /** {@code page_number} is the one numeric key whose ranges compile. */
    private static final String PAGE_NUMBER = "page_number";

    private static final Set<String> RANGE_OPERATORS = Set.of("$gte", "$gt", "$lte", "$lt");

    private ManifestPushdown() {}

    /** Whether the stage predicate {@code p} has an in-SQL form. */
    static boolean pushable(ManifestLabelStage.Predicate p) {
        String op = p.operator();
        boolean positive = "$eq".equals(op) || "$in".equals(op);
        if (STRING_KEYS.contains(p.key())) {
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
        for (ManifestLabelStage.Predicate p : predicates) {
            if (!pushable(p)) {
                continue;
            }
            Map<String, Object> op = new LinkedHashMap<>();
            op.put(p.operator(), p.operand());
            where.put(p.key(), op);
        }
        PgVectorRepository.WherePlan plan = PgVectorRepository.planWhere(where.isEmpty() ? null : where);
        if (plan.containment() == null && plan.jsonPath() == null) {
            return null;
        }
        return new Plan(plan.containment(), plan.jsonPath());
    }
}
