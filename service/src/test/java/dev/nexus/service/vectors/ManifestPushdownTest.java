// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-p89h4: which predicates of a label or document-key {@code where} run inside the search, and what they
 * compile to. The rule is that the in-SQL form may admit more than {@link ManifestLabelStage} keeps and never
 * less, so only the POSITIVE operators are compiled; everything else is left to the stage alone.
 */
class ManifestPushdownTest {

    private static List<ManifestLabelStage.Predicate> predicates(Map<String, Object> where) {
        return ManifestLabelStage.split(where).labels();
    }

    private static Map<String, Object> where(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        return where(operator, operand);
    }

    @Test
    void anEqualityOnALabel_compilesToContainment() {
        var plan = ManifestPushdown.of(predicates(where("section_type", "appendix-x")));

        assertThat(plan).isNotNull();
        assertThat(plan.containment().data()).isEqualTo("{\"section_type\":\"appendix-x\"}");
        assertThat(plan.jsonPath()).isNull();
    }

    @Test
    void aPageRange_compilesToAJsonPathComparison() {
        var plan = ManifestPushdown.of(predicates(where("page_number", op("$gte", 9000))));

        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath()).isEqualTo("$.\"page_number\" >= 9000");
    }

    @Test
    void anInList_compilesToAnyOfTheValues() {
        var plan = ManifestPushdown.of(predicates(where("section_type", op("$in", List.of("a", "b")))));

        assertThat(plan.jsonPath()).contains("== \"a\"").contains("== \"b\"").contains("||");
    }

    @Test
    void predicatesAreAnded_overTheSameOccurrence() {
        var plan = ManifestPushdown.of(predicates(where("section_type", "body", "page_number", op("$lt", 5))));

        assertThat(plan.containment().data()).isEqualTo("{\"section_type\":\"body\"}");
        assertThat(plan.jsonPath()).isEqualTo("$.\"page_number\" < 5");
    }

    @Test
    void aNegativeOperator_isNotCompiled_itKeepsWhatItDoesNotReject() {
        assertThat(ManifestPushdown.of(predicates(where("section_type", op("$ne", "references"))))).isNull();
        assertThat(ManifestPushdown.of(predicates(where("section_type", op("$nin", List.of("references")))))).isNull();
    }

    @Test
    void aKeyWithNoInSqlForm_isNotCompiled() {
        assertThat(ManifestPushdown.of(predicates(where("indexed_at", op("$gte", "2026-01-01"))))).isNull();
        assertThat(ManifestPushdown.of(predicates(where("bib_year", op("$gte", 2020))))).isNull();
        assertThat(ManifestPushdown.of(predicates(where("bib_venue", "NeurIPS")))).isNull();
        // A range over a string label is not compiled either: the stage compares in Java's order, not the database's.
        assertThat(ManifestPushdown.of(predicates(where("section_type", op("$gte", "a"))))).isNull();
    }

    @Test
    void onlyThePositivePartIsCompiled_theRestStaysWithTheStage() {
        var plan = ManifestPushdown.of(predicates(where(
            "section_type", "appendix-x", "bib_year", op("$gte", 2020), "section_title", op("$ne", "x"))));

        assertThat(plan.containment().data()).isEqualTo("{\"section_type\":\"appendix-x\"}");
        assertThat(plan.jsonPath()).isNull();
    }

    @Test
    void theIdValuedBibKeysAndTheAuthor_areCompiled() {
        var plan = ManifestPushdown.of(predicates(where(
            "bib_doi", "10.1/a", "bib_openalex_id", "oa-a", "bib_semantic_scholar_id", "0042", "source_author", "Alice")));

        assertThat(plan.containment().data())
            .contains("\"bib_doi\":\"10.1/a\"").contains("\"bib_openalex_id\":\"oa-a\"")
            .contains("\"source_author\":\"Alice\"");
        // "0042" reads as a number, so it takes the search's tolerant number-or-text form (a jsonpath), not containment.
        assertThat(plan.jsonPath()).contains("bib_semantic_scholar_id").contains("\"0042\"");
    }
}
