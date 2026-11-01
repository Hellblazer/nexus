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
        // nexus-6pfgb.64: read as a number whether the stored page is a JSON number or numeric text, as the stage does.
        assertThat(plan.jsonPath()).isEqualTo("exists($.\"page_number\" ? (@.number() >= 9000))");
    }

    @Test
    void everyNumericPageRangeOperator_readsTheStoredPageAsANumber() {
        for (var e : Map.of("$gte", ">=", "$gt", ">", "$lte", "<=", "$lt", "<").entrySet()) {
            var plan = ManifestPushdown.of(predicates(where("page_number", op(e.getKey(), 7))));
            assertThat(plan.jsonPath()).as(e.getKey())
                .isEqualTo("exists($.\"page_number\" ? (@.number() " + e.getValue() + " 7))");
        }
        // A decimal operand is rendered as plain numeric text, never in exponent form.
        assertThat(ManifestPushdown.of(predicates(where("page_number", op("$gte", 2.5)))).jsonPath())
            .isEqualTo("exists($.\"page_number\" ? (@.number() >= 2.5))");
    }

    @Test
    void aStringOperandOnAPageRange_keepsTheStrictStringComparison_asTheStageComparesItLexically() {
        // The stage treats a string operand as a lexical comparison against a string label and a number as not
        // comparable, which is what the strict jsonpath comparison does; only a NUMERIC operand reads text as a number.
        assertThat(ManifestPushdown.of(predicates(where("page_number", op("$gte", "3")))).jsonPath())
            .isEqualTo("$.\"page_number\" >= \"3\"");
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
        assertThat(plan.jsonPath()).isEqualTo("exists($.\"page_number\" ? (@.number() < 5))");
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

    @Test
    void title_isCompiled_asAnEquality() {
        var plan = ManifestPushdown.of(predicates(where("title", "alpha.py")));

        assertThat(plan.containment().data()).isEqualTo("{\"title\":\"alpha.py\"}");
        assertThat(ManifestPushdown.of(predicates(where("title", op("$ne", "alpha.py"))))).isNull();
    }

    @Test
    void contentType_isCompiled_asMembershipOfTheDocumentsArray() {
        var eq = ManifestPushdown.of(predicates(where("content_type", "pdf")));
        assertThat(eq.containment()).isNull();
        assertThat(eq.jsonPath()).isEqualTo("(exists($.\"content_type\"[*] ? (@ == \"pdf\")))");

        var in = ManifestPushdown.of(predicates(where("content_type", op("$in", List.of("pdf", "code")))));
        assertThat(in.jsonPath()).contains("== \"pdf\"").contains("== \"code\"").contains("||");

        // An empty list admits nothing, as the stage's anyMatch does.
        assertThat(ManifestPushdown.of(predicates(where("content_type", op("$in", List.of())))).jsonPath())
            .isEqualTo("(1 == 2)");
        assertThat(ManifestPushdown.of(predicates(where("content_type", op("$ne", "pdf"))))).isNull();
    }

    @Test
    void contentTypeAndALabel_areAndedInOneJsonPath() {
        var plan = ManifestPushdown.of(predicates(where("page_number", op("$gte", 3), "content_type", "pdf")));

        assertThat(plan.jsonPath()).isEqualTo(
            "exists($.\"page_number\" ? (@.number() >= 3)) && (exists($.\"content_type\"[*] ? (@ == \"pdf\")))");
    }

    // ── nexus-6pfgb.29: the catalog-meta keys ───────────────────────────────

    @Test
    void theScalarMetaKeys_compileLikeOtherScalars() {
        var plan = ManifestPushdown.of(predicates(where(
            "category", "decision", "source_agent", "claude", "extraction_method", "docling",
            "extraction_source", "pdf")));

        assertThat(plan.containment().data()).contains("\"category\":\"decision\"").contains("\"source_agent\":\"claude\"")
            .contains("\"extraction_method\":\"docling\"").contains("\"extraction_source\":\"pdf\"");
        assertThat(plan.jsonPath()).isNull();

        var in = ManifestPushdown.of(predicates(where("category", op("$in", List.of("a", "b")))));
        assertThat(in.jsonPath()).contains("== \"a\"").contains("== \"b\"").contains("||");
        assertThat(ManifestPushdown.of(predicates(where("category", op("$ne", "a"))))).isNull();
    }

    @Test
    void qualityGateOverridden_takesTheJsonAndTheTextFormOfABoolean() {
        var plan = ManifestPushdown.of(predicates(where("quality_gate_overridden", true)));

        assertThat(plan.jsonPath()).isEqualTo(
            "(exists($.\"quality_gate_overridden\" ? (@ == true)) || exists($.\"quality_gate_overridden\" ? (@ == \"true\")))");
    }

    @Test
    void ttlDaysAndFrecencyScore_takeEqualityLikeAPage_andRangesAsNumbers() {
        assertThat(ManifestPushdown.of(predicates(where("ttl_days", 7))).jsonPath()).isEqualTo(
            "(exists($.\"ttl_days\" ? (@ == 7)) || exists($.\"ttl_days\" ? (@ == \"7\")))");
        assertThat(ManifestPushdown.of(predicates(where("frecency_score", op("$gte", 0.5)))).jsonPath())
            .isEqualTo("exists($.\"frecency_score\" ? (@.number() >= 0.5))");
        assertThat(ManifestPushdown.of(predicates(where("ttl_days", op("$lt", 30)))).jsonPath())
            .isEqualTo("exists($.\"ttl_days\" ? (@.number() < 30))");
        assertThat(ManifestPushdown.of(predicates(where("ttl_days", op("$ne", 7))))).isNull();
    }

    @Test
    void tags_isCompiled_asMembershipOfTheDocumentsTagSet() {
        var eq = ManifestPushdown.of(predicates(where("tags", "arch")));
        assertThat(eq.containment()).isNull();
        assertThat(eq.jsonPath()).isEqualTo("(exists($.\"tags\"[*] ? (@ == \"arch\")))");

        var in = ManifestPushdown.of(predicates(where("tags", op("$in", List.of("a", "b")))));
        assertThat(in.jsonPath()).isEqualTo(
            "(exists($.\"tags\"[*] ? (@ == \"a\")) || exists($.\"tags\"[*] ? (@ == \"b\")))");
        assertThat(ManifestPushdown.of(predicates(where("tags", op("$in", List.of())))).jsonPath()).isEqualTo("(1 == 2)");
        assertThat(ManifestPushdown.of(predicates(where("tags", op("$ne", "arch"))))).isNull();
        assertThat(ManifestPushdown.of(predicates(where("tags", op("$nin", List.of("arch")))))).isNull();
    }
}
