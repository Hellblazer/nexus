// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;

import dev.nexus.service.vectors.PgVectorRepository.WherePlan;

/**
 * Unit coverage for {@link PgVectorRepository#planWhere} — the where-translator backing
 * {@code nexus.plain_search_<dim>}/{@code nexus.text_gated_search_<dim>} (nexus-zrcj7,
 * retiring {@code appendWherePredicate}'s raw-SQL-string translation; see the deleted
 * test class {@code PgVectorRepositoryWherePredicateTest} for the operator coverage this
 * class carries forward against the new (containment, jsonpath) contract). Pure (no PG
 * container): asserts the exact {@link WherePlan} shape for every operator.
 */
class PgVectorRepositoryWherePlanTest {

    @Test
    void nullOrEmptyWhere_producesNoPredicate() {
        assertThat(PgVectorRepository.planWhere(null)).isEqualTo(new WherePlan(null, null));
        assertThat(PgVectorRepository.planWhere(Map.of())).isEqualTo(new WherePlan(null, null));
    }

    @Test
    void plainEquality_scalarValue_becomesContainment() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", "a"));
        assertThat(plan.containment().data()).isEqualTo("{\"kind\":\"a\"}");
        assertThat(plan.jsonPath()).isNull();
    }

    // ── tolerant-typing rule (T2 [24219] critique finding A) ────────────────────────
    // The retired appendWherePredicate always compared via metadata->>'k' (TEXT), so a
    // stored JSON number 5 matched an operand "5" and vice versa, likewise booleans.
    // Containment/jsonpath native == are both type-strict, so a Number/Boolean/
    // numeric-looking-string/boolean-looking-string operand must route through a
    // tolerant jsonpath OR of both literal forms to reproduce that cross-type match.

    @Test
    void plainEquality_numericOperand_toleratesCrossTypeMatch() {
        // Containment alone would only match a stored JSON number 2020, never a stored
        // JSON string "2020" -- both matched under the retired TEXT comparison.
        WherePlan plan = PgVectorRepository.planWhere(Map.of("year", 2020));
        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath())
            .isEqualTo("(exists($.\"year\" ? (@ == 2020)) || exists($.\"year\" ? (@ == \"2020\")))");
    }

    @Test
    void plainEquality_numericLookingStringOperand_toleratesCrossTypeMatch() {
        // Symmetric case: a String operand that LOOKS numeric must also match a stored
        // JSON number, exactly as the retired code's blind String.valueOf(...) did.
        WherePlan plan = PgVectorRepository.planWhere(Map.of("year", "2020"));
        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath())
            .isEqualTo("(exists($.\"year\" ? (@ == 2020)) || exists($.\"year\" ? (@ == \"2020\")))");
    }

    @Test
    void plainEquality_booleanOperand_toleratesCrossTypeMatch() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("flag", true));
        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath())
            .isEqualTo("(exists($.\"flag\" ? (@ == true)) || exists($.\"flag\" ? (@ == \"true\")))");
    }

    @Test
    void plainEquality_booleanLookingStringOperand_toleratesCrossTypeMatch() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("flag", "false"));
        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath())
            .isEqualTo("(exists($.\"flag\" ? (@ == false)) || exists($.\"flag\" ? (@ == \"false\")))");
    }

    @Test
    void plainEquality_nullOperand_matchesLiteralFourCharacterString() {
        // Retired code compared against String.valueOf(null) = "null" (a plain 4-char
        // string), never the JSON null literal -- containment.put(key, null) would mean
        // something else entirely ("stored value IS json null").
        var where = new LinkedHashMap<String, Object>();
        where.put("k", null);
        WherePlan plan = PgVectorRepository.planWhere(where);
        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath()).isEqualTo("(exists($.\"k\" ? (@ == \"null\")))");
    }

    @Test
    void neOperator_numericOperand_toleratesCrossTypeMatch() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("year", Map.of("$ne", 2020)));
        assertThat(plan.jsonPath())
            .isEqualTo("(!exists($.\"year\") || !(exists($.\"year\" ? (@ == 2020)) "
                + "|| exists($.\"year\" ? (@ == \"2020\"))))");
    }

    @Test
    void inOperator_numericAndBooleanItems_toleratesCrossTypeMatch() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("k", Map.of("$in", List.of(5, true))));
        assertThat(plan.jsonPath())
            .isEqualTo("(exists($.\"k\" ? (@ == 5)) || exists($.\"k\" ? (@ == \"5\")) "
                + "|| exists($.\"k\" ? (@ == true)) || exists($.\"k\" ? (@ == \"true\")))");
    }

    @Test
    void ninOperator_numericItem_toleratesCrossTypeMatch() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("k", Map.of("$nin", List.of(3))));
        assertThat(plan.jsonPath())
            .isEqualTo("(!exists($.\"k\") || !(exists($.\"k\" ? (@ == 3)) "
                + "|| exists($.\"k\" ? (@ == \"3\"))))");
    }

    // ── non-scalar operand rejection (T2 [24220] review finding) ────────────────────
    // A Map/List operand would otherwise serialize as raw, unquoted JSON object/array
    // text straight into the jsonpath predicate string -- invalid jsonpath syntax,
    // surfacing as an opaque Postgres syntax-error 500 instead of a 400.

    @Test
    void plainEquality_nonScalarValue_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("k", List.of("a", "b"))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scalar operand");
    }

    @Test
    void eqOperator_nonScalarOperand_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("k", Map.of("$eq", Map.of("nested", 1)))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scalar operand");
    }

    @Test
    void neOperator_nonScalarOperand_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("k", Map.of("$ne", List.of("a", "b")))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scalar operand");
    }

    @Test
    void inOperator_nestedMapItem_failsLoud() {
        assertThatThrownBy(
                () -> PgVectorRepository.planWhere(Map.of("k", Map.of("$in", List.of(Map.of("nested", 1))))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scalar operand");
    }

    @Test
    void ninOperator_nestedListItem_failsLoud() {
        assertThatThrownBy(
                () -> PgVectorRepository.planWhere(Map.of("k", Map.of("$nin", List.of(List.of(1, 2))))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("scalar operand");
    }

    @Test
    void eqOperator_sameAsPlainEquality() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", Map.of("$eq", "a")));
        assertThat(plan.containment().data()).isEqualTo("{\"kind\":\"a\"}");
        assertThat(plan.jsonPath()).isNull();
    }

    @Test
    void neOperator_absentKeyKeptSemantics() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("section_type", Map.of("$ne", "references")));
        assertThat(plan.containment()).isNull();
        assertThat(plan.jsonPath())
            .isEqualTo("(!exists($.\"section_type\") || !(exists($.\"section_type\" ? (@ == \"references\"))))");
    }

    @Test
    void inOperator_orsEquality() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", Map.of("$in", List.of("a", "b"))));
        assertThat(plan.jsonPath())
            .isEqualTo("(exists($.\"kind\" ? (@ == \"a\")) || exists($.\"kind\" ? (@ == \"b\")))");
    }

    @Test
    void ninOperator_absentKeyKeptSemantics() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", Map.of("$nin", List.of("a", "b"))));
        assertThat(plan.jsonPath())
            .isEqualTo("(!exists($.\"kind\") || !(exists($.\"kind\" ? (@ == \"a\")) "
                + "|| exists($.\"kind\" ? (@ == \"b\"))))");
    }

    @Test
    void inEmptyList_matchesNothing() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", Map.of("$in", List.of())));
        assertThat(plan.jsonPath()).isEqualTo("(1 == 2)");
    }

    @Test
    void ninEmptyList_excludesNothing() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", Map.of("$nin", List.of())));
        assertThat(plan.jsonPath()).isEqualTo("(1 == 1)");
    }

    @Test
    void compoundOperatorKey_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("$or", List.of(Map.of("kind", "a")))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("$or");
    }

    @Test
    void unknownOperator_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("kind", Map.of("$regex", "a.*"))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("$regex");
    }

    @Test
    void multiOperatorMap_failsLoud() {
        var ops = new LinkedHashMap<String, Object>();
        ops.put("$ne", "a");
        ops.put("$in", List.of("b"));
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("kind", ops)))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("exactly one operator");
    }

    @Test
    void inOperator_nonListOperand_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("kind", Map.of("$in", "a"))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("list operand");
    }

    // ── range operators: native jsonpath comparison, no jsonb_typeof hack ──────────

    @Test
    void gteNumericOperand_nativeJsonpathCompare() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("bib_year", Map.of("$gte", 2020)));
        assertThat(plan.jsonPath()).isEqualTo("$.\"bib_year\" >= 2020");
    }

    @Test
    void ltStringOperand_nativeJsonpathCompare() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("rank", Map.of("$lt", "m")));
        assertThat(plan.jsonPath()).isEqualTo("$.\"rank\" < \"m\"");
    }

    @Test
    void rangeOperator_rejectsNonScalarOperand_loud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("k", Map.of("$gt", List.of(1)))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("numeric or string operand");
    }

    @Test
    void unsupportedOperator_errorListsRangeOperators() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("k", Map.of("$regex", "x"))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("$gte, $lte, $gt, $lt");
    }

    @Test
    void rangeOperator_nonFiniteNumericOperand_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.planWhere(Map.of("k", Map.of("$gte", Double.NaN))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("non-finite");
    }

    // ── multi-field: all fields ANDed together, containment and jsonpath split ─────

    @Test
    void multiField_scalarAndOperator_splitsContainmentAndJsonPath() {
        var where = new LinkedHashMap<String, Object>();
        where.put("kind", "note");
        where.put("year", Map.of("$gte", 2020));
        WherePlan plan = PgVectorRepository.planWhere(where);
        assertThat(plan.containment().data()).isEqualTo("{\"kind\":\"note\"}");
        assertThat(plan.jsonPath()).isEqualTo("$.\"year\" >= 2020");
    }

    @Test
    void multiField_twoOperators_andedTogether() {
        var where = new LinkedHashMap<String, Object>();
        where.put("a", Map.of("$gte", 1));
        where.put("b", Map.of("$lte", 9));
        WherePlan plan = PgVectorRepository.planWhere(where);
        assertThat(plan.jsonPath()).isEqualTo("$.\"a\" >= 1 && $.\"b\" <= 9");
    }

    @Test
    void keyRequiringQuoting_rendersAsQuotedJsonPathMember() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("weird key", Map.of("$ne", "x")));
        assertThat(plan.jsonPath())
            .isEqualTo("(!exists($.\"weird key\") || !(exists($.\"weird key\" ? (@ == \"x\"))))");
    }
}
