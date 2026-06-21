// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * Unit coverage for {@link PgVectorRepository#appendWherePredicate} — the shared
 * where-translator backing the three vector bridge routes (nexus-05bfd). Pure (no
 * PG container): asserts the exact SQL fragment and bind ORDER for every operator,
 * with focus on the {@code $nin} double-key pattern and the empty-list branches the
 * Testcontainers suite does not exercise.
 */
class PgVectorRepositoryWherePredicateTest {

    private static List<Object> binds() {
        return new ArrayList<>();
    }

    @Test
    void plainEquality_scalarValue() {
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "kind", "a");
        assertThat(sql).hasToString(" AND metadata->>? = ?");
        assertThat(binds).containsExactly("kind", "a");
    }

    @Test
    void plainEquality_coercesNonStringValue() {
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "year", 2020);
        assertThat(sql).hasToString(" AND metadata->>? = ?");
        assertThat(binds).containsExactly("year", "2020");
    }

    @Test
    void eqOperator_sameAsPlainEquality() {
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "kind", Map.of("$eq", "a"));
        assertThat(sql).hasToString(" AND metadata->>? = ?");
        assertThat(binds).containsExactly("kind", "a");
    }

    @Test
    void neOperator_isDistinctFrom() {
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "section_type", Map.of("$ne", "references"));
        assertThat(sql).hasToString(" AND metadata->>? IS DISTINCT FROM ?");
        assertThat(binds).containsExactly("section_type", "references");
    }

    @Test
    void inOperator_bindsKeyThenItems() {
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "kind", Map.of("$in", List.of("a", "b")));
        assertThat(sql).hasToString(" AND metadata->>? IN (?,?)");
        assertThat(binds).containsExactly("kind", "a", "b");
    }

    @Test
    void ninOperator_bindsKeyTwiceThenItems() {
        // The subtle case: two metadata->>? placeholders (IS NULL OR NOT IN) → key bound twice.
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "kind", Map.of("$nin", List.of("a", "b")));
        assertThat(sql).hasToString(" AND (metadata->>? IS NULL OR metadata->>? NOT IN (?,?))");
        assertThat(binds).containsExactly("kind", "kind", "a", "b");
    }

    @Test
    void inEmptyList_matchesNothing() {
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "kind", Map.of("$in", List.of()));
        assertThat(sql).hasToString(" AND FALSE");
        assertThat(binds).isEmpty();
    }

    @Test
    void ninEmptyList_excludesNothing() {
        var sql = new StringBuilder();
        var binds = binds();
        PgVectorRepository.appendWherePredicate(sql, binds, "kind", Map.of("$nin", List.of()));
        assertThat(sql).hasToString(" AND TRUE");
        assertThat(binds).isEmpty();
    }

    @Test
    void compoundOperatorKey_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.appendWherePredicate(
                new StringBuilder(), binds(), "$or", List.of(Map.of("kind", "a"))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("$or");
    }

    @Test
    void unknownOperator_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.appendWherePredicate(
                new StringBuilder(), binds(), "kind", Map.of("$regex", "a.*")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("$regex");
    }

    @Test
    void multiOperatorMap_failsLoud() {
        var ops = new java.util.LinkedHashMap<String, Object>();
        ops.put("$ne", "a");
        ops.put("$in", List.of("b"));
        assertThatThrownBy(() -> PgVectorRepository.appendWherePredicate(
                new StringBuilder(), binds(), "kind", ops))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("exactly one operator");
    }

    @Test
    void inOperator_nonListOperand_failsLoud() {
        assertThatThrownBy(() -> PgVectorRepository.appendWherePredicate(
                new StringBuilder(), binds(), "kind", Map.of("$in", "a")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("list operand");
    }
}
