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
 * less, so route (a) compiles only the POSITIVE operators ({@link ManifestPushdown#of}); everything else is left to the
 * stage alone. Route (b) has no stage, so {@link ManifestPushdown#ofExact} compiles every operator exactly
 * (nexus-qpt52); {@code ManifestRouteBDifferentialTest} holds that form to the stage over a matrix.
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
    void aStringOperandOnAPageRange_comparesLexicallyAStringLabelThatIsNotBlank_asTheStageDoes() {
        // The stage compares a string operand lexically against a label that is not a number and not unlabelled, and
        // treats a numeric label as not comparable; only a NUMERIC operand reads text as a number.
        assertThat(ManifestPushdown.of(predicates(where("page_number", op("$gte", "3")))).jsonPath())
            .isEqualTo("exists($.\"page_number\" ? (@.type() != \"number\" && @.string() != \"\" && @.string() >= \"3\"))");
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
    void aRangeOverAStringLabel_isNotPushedOnRouteA() {
        // Java orders strings by UTF-16 code unit, jsonpath by code point; they differ only between a supplementary
        // character and U+E000..U+FFFF, where an in-SQL range could admit less than the stage keeps, which route (a)'s
        // superset rule forbids. So route (a) leaves a string range to the stage; route (b) compiles it (ofExact).
        assertThat(ManifestPushdown.of(predicates(where("section_type", op("$gte", "a"))))).isNull();
    }

    @Test
    void everyNumericKeyOfTheStage_isPushedOnRouteAInTheExactNumericForm() {
        // EXACT_KEYS is a literal (the tenant-keyed-state gate exempts only literal constants), so it must be held to
        // the stage's own list of numeric keys: a numeric key added there and not here would fall back to the planner's
        // two literal forms, which miss a stored "7.0" or an operand " 7 ".
        assertThat(ManifestLabelStage.NUMERIC_KEYS).hasSize(5);
        for (String key : ManifestLabelStage.NUMERIC_KEYS) {
            assertThat(ManifestPushdown.of(predicates(where(key, 7))).jsonPath()).as(key)
                .isEqualTo("exists($.\"" + key + "\" ? (@.number() == 7))");
        }
    }

    @Test
    void theKeysOfQpt52_arePushedOnRouteAInTheirExactForm() {
        assertThat(ManifestPushdown.of(predicates(where("indexed_at", op("$gte", "2026-01-01")))).jsonPath())
            .isEqualTo("exists($.\"indexed_at\" ? (@.type() != \"number\" && @.string() != \"\" && @.string() >= \"2026-01-01\"))");
        assertThat(ManifestPushdown.of(predicates(where("bib_year", op("$gte", 2020)))).jsonPath())
            .isEqualTo("exists($.\"bib_year\" ? (@.number() >= 2020))");
        assertThat(ManifestPushdown.of(predicates(where("bib_venue", "NeurIPS"))).jsonPath())
            .isEqualTo("exists($.\"bib_venue\" ? (@.string() == \"NeurIPS\"))");
        assertThat(ManifestPushdown.of(predicates(where("bib_citation_count", 0))).jsonPath())
            .isEqualTo("exists($.\"bib_citation_count\" ? (@.number() == 0))");
        assertThat(ManifestPushdown.of(predicates(where("bib_authors", op("$in", List.of("A", "B"))))).jsonPath())
            .isEqualTo("(exists($.\"bib_authors\" ? (@.string() == \"A\")) || exists($.\"bib_authors\" ? (@.string() == \"B\")))");
        // a negative operator is route (b)'s alone
        assertThat(ManifestPushdown.of(predicates(where("bib_year", op("$ne", 2020))))).isNull();
        assertThat(ManifestPushdown.of(predicates(where("indexed_at", op("$nin", List.of("x")))))).isNull();
    }

    @Test
    void onlyThePositivePartIsCompiled_theRestStaysWithTheStage() {
        var plan = ManifestPushdown.of(predicates(where(
            "section_type", "appendix-x", "bib_year", op("$gte", 2020), "section_title", op("$ne", "x"))));

        assertThat(plan.containment().data()).isEqualTo("{\"section_type\":\"appendix-x\"}");
        assertThat(plan.jsonPath()).isEqualTo("exists($.\"bib_year\" ? (@.number() >= 2020))");
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
        // nexus-qpt52: a numeric key compares as numbers whichever form is stored ("7", 7.0), which the planner's two
        // literal forms (7 or "7") missed.
        assertThat(ManifestPushdown.of(predicates(where("ttl_days", 7))).jsonPath()).isEqualTo(
            "exists($.\"ttl_days\" ? (@.number() == 7))");
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

    // ── nexus-qpt52: the exact form route (b) runs ──────────────────────────

    private static String exact(Map<String, Object> where) {
        return ManifestPushdown.ofExact(predicates(where)).jsonPath();
    }

    @Test
    void theExactForm_compilesEveryOperatorOnEveryKind_withNoContainment() {
        // string key
        assertThat(exact(where("section_type", "body"))).isEqualTo("exists($.\"section_type\" ? (@.string() == \"body\"))");
        assertThat(exact(where("section_type", op("$ne", "body")))).isEqualTo("!exists($.\"section_type\" ? (@.string() == \"body\"))");
        assertThat(exact(where("section_type", op("$nin", List.of("a", "b"))))).isEqualTo(
            "!(exists($.\"section_type\" ? (@.string() == \"a\")) || exists($.\"section_type\" ? (@.string() == \"b\")))");
        assertThat(exact(where("section_type", op("$in", List.of())))).isEqualTo("(1 == 2)");
        assertThat(exact(where("section_type", op("$nin", List.of())))).isEqualTo("!(1 == 2)");
        // numeric key: numbers compare as numbers, an empty operand names the unlabelled value, the rest compares as text
        assertThat(exact(where("bib_year", 2020))).isEqualTo("exists($.\"bib_year\" ? (@.number() == 2020))");
        assertThat(exact(where("bib_year", "2020.0"))).isEqualTo("exists($.\"bib_year\" ? (@.number() == 2020.0))");
        assertThat(exact(where("bib_year", ""))).isEqualTo("exists($.\"bib_year\" ? (@ == \"\"))");
        assertThat(exact(where("bib_year", "abc"))).isEqualTo("exists($.\"bib_year\" ? (@.string() == \"abc\"))");
        assertThat(exact(where("bib_year", op("$ne", 5)))).isEqualTo("!exists($.\"bib_year\" ? (@.number() == 5))");
        // set keys: membership of the array
        assertThat(exact(where("tags", op("$ne", "arch")))).isEqualTo("!exists($.\"tags\"[*] ? (@ == \"arch\"))");
        assertThat(exact(where("content_type", op("$nin", List.of("pdf"))))).isEqualTo(
            "!(exists($.\"content_type\"[*] ? (@ == \"pdf\")))");
        // predicates are ANDed, none is skipped, and no containment is produced
        var plan = ManifestPushdown.ofExact(predicates(where("section_type", "body", "bib_venue", op("$ne", "x"))));
        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath()).isEqualTo(
            "exists($.\"section_type\" ? (@.string() == \"body\")) && !exists($.\"bib_venue\" ? (@.string() == \"x\"))");
        assertThat(ManifestPushdown.ofExact(List.of())).isNull();
    }

    @Test
    void theExactForm_rangesFollowTheStagesComparableRule() {
        // a numeric operand compares on a numeric key only; on a string key or a set it never matches
        assertThat(exact(where("page_number", op("$gt", 3)))).isEqualTo("exists($.\"page_number\" ? (@.number() > 3))");
        assertThat(exact(where("section_type", op("$gt", 3)))).isEqualTo("(1 == 2)");
        assertThat(exact(where("tags", op("$gt", "a")))).isEqualTo("(1 == 2)");
        assertThat(exact(where("tags", op("$gt", 3)))).isEqualTo("(1 == 2)");
        // a string operand compares lexically, and not against an unlabelled value or a number
        assertThat(exact(where("indexed_at", op("$lt", "2026-01-01")))).isEqualTo(
            "exists($.\"indexed_at\" ? (@.type() != \"number\" && @.string() != \"\" && @.string() < \"2026-01-01\"))");
        // content_type reads the text the row would show, a separate object key
        assertThat(exact(where("content_type", op("$gte", "m")))).isEqualTo(
            "exists($.\"content_type_shown\" ? (@.type() != \"number\" && @.string() != \"\" && @.string() >= \"m\"))");
    }

    @Test
    void theExactForm_refusesANumberTooLongToRender() {
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> exact(where("bib_year", new java.math.BigDecimal("1E+999999999"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("out of range");
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> exact(where("bib_year", op("$gte", new java.math.BigDecimal("1E+5000")))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("out of range");
    }

    @Test
    void aNegativeOperatorOnContentType_isTheOnlyOneThatDisablesTheBoundedCount() {
        assertThat(ManifestPushdown.negatesContentType(predicates(where("content_type", op("$ne", "pdf"))))).isTrue();
        assertThat(ManifestPushdown.negatesContentType(predicates(where("content_type", op("$nin", List.of("pdf")))))).isTrue();
        assertThat(ManifestPushdown.negatesContentType(predicates(where("content_type", "pdf")))).isFalse();
        assertThat(ManifestPushdown.negatesContentType(predicates(where("section_type", op("$ne", "x"))))).isFalse();
    }
}
