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

    @Test
    void plainEquality_coercesNonStringValue_asJsonNumber() {
        // Unlike appendWherePredicate's TEXT-comparison quirk (metadata->>'k' = '2020'),
        // containment is type-preserving JSON equality: the operand's own JSON type
        // (here, a number) is what gets compared, not its String.valueOf() rendering.
        WherePlan plan = PgVectorRepository.planWhere(Map.of("year", 2020));
        assertThat(plan.containment().data()).isEqualTo("{\"year\":2020}");
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
            .isEqualTo("(!exists($.\"section_type\") || $.\"section_type\" != \"references\")");
    }

    @Test
    void inOperator_orsEquality() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", Map.of("$in", List.of("a", "b"))));
        assertThat(plan.jsonPath()).isEqualTo("($.\"kind\" == \"a\" || $.\"kind\" == \"b\")");
    }

    @Test
    void ninOperator_absentKeyKeptSemantics() {
        WherePlan plan = PgVectorRepository.planWhere(Map.of("kind", Map.of("$nin", List.of("a", "b"))));
        assertThat(plan.jsonPath())
            .isEqualTo("(!exists($.\"kind\") || !($.\"kind\" == \"a\" || $.\"kind\" == \"b\"))");
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
            .isEqualTo("(!exists($.\"weird key\") || $.\"weird key\" != \"x\")");
    }
}
