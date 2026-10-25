// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-tko2w: the pure half of {@link ManifestLabelStage}: how a {@code where} is split into the part the
 * search function receives and the label predicates the stage answers, and how one occurrence's label is
 * judged. The manifest lookup and the expansion are pinned in {@code PgVectorManifestLabelFilterTest}.
 */
class ManifestLabelStageTest {

    private static Map<String, Object> w(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static Map<String, Object> labels(String type, String title, Object page) {
        Map<String, Object> m = new HashMap<>();
        m.put("section_type", type);
        m.put("section_title", title);
        m.put("page_number", page);
        return m;
    }

    @Test
    void aWhereWithoutLabelKeys_isReturnedAsTheSameInstance_andIsNotActive() {
        Map<String, Object> where = w("lang", "en");
        var split = ManifestLabelStage.split(where);

        assertThat(split.active()).isFalse();
        assertThat(split.rest()).isSameAs(where);
        assertThat(ManifestLabelStage.split(null).active()).isFalse();
        assertThat(ManifestLabelStage.split(Map.of()).active()).isFalse();
    }

    @Test
    void labelKeysLeaveTheWhere_theRestStaysForTheSearchFunction() {
        var split = ManifestLabelStage.split(w("lang", "en", "section_type", w("$ne", "references"), "page_number", 3));

        assertThat(split.active()).isTrue();
        assertThat(split.rest()).containsOnlyKeys("lang");
        assertThat(split.labels()).extracting(ManifestLabelStage.Predicate::key, ManifestLabelStage.Predicate::operator)
            .containsExactly(org.assertj.core.groups.Tuple.tuple("section_type", "$ne"),
                             org.assertj.core.groups.Tuple.tuple("page_number", "$eq"));
    }

    @Test
    void aWhereOfLabelKeysOnly_leavesNoRest() {
        assertThat(ManifestLabelStage.split(w("section_type", "body")).rest()).isNull();
    }

    @Test
    void malformedOperatorsAreRefusedLikeTheSearchPlannerRefusesThem() {
        assertThatThrownBy(() -> ManifestLabelStage.split(w("section_type", w("$regex", "x"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("unsupported where operator");
        assertThatThrownBy(() -> ManifestLabelStage.split(w("section_type", w("$ne", "a", "$eq", "b"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("exactly one operator");
        assertThatThrownBy(() -> ManifestLabelStage.split(w("section_type", w("$in", "references"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("list operand");
        assertThatThrownBy(() -> ManifestLabelStage.split(w("section_type", List.of("a"))))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void aNullLabel_isKeptByNegativeOperators_andDroppedByEveryOther() {
        var nul = labels(null, null, null);
        for (String neg : List.of("$ne", "$nin")) {
            Object operand = neg.equals("$ne") ? "references" : List.of("references");
            var p = ManifestLabelStage.split(w("section_type", w(neg, operand))).labels();
            assertThat(ManifestLabelStage.passes(p, nul)).as(neg).isTrue();
        }
        for (var positive : List.of(w("section_type", "references"),
                                    w("section_type", w("$in", List.of("references"))),
                                    w("section_type", w("$gte", "a")),
                                    w("page_number", w("$lt", 5)))) {
            assertThat(ManifestLabelStage.passes(ManifestLabelStage.split(positive).labels(), nul))
                .as(positive.toString()).isFalse();
        }
    }

    @Test
    void anEmptyLabel_isUnlabelled_exactlyAsNullIs() {
        // A current client writes "" for a chunk with no heading (labels_written): same verdicts as NULL.
        var empty = labels("", "", null);
        for (String neg : List.of("$ne", "$nin")) {
            Object operand = neg.equals("$ne") ? "references" : List.of("references");
            var p = ManifestLabelStage.split(w("section_type", w(neg, operand))).labels();
            assertThat(ManifestLabelStage.passes(p, empty)).as(neg).isTrue();
        }
        for (var positive : List.of(w("section_type", "references"),
                                    w("section_type", w("$in", List.of("references"))),
                                    w("section_title", w("$gte", "a")),
                                    w("section_title", w("$lt", "z")))) {
            assertThat(ManifestLabelStage.passes(ManifestLabelStage.split(positive).labels(), empty))
                .as(positive.toString()).isFalse();
        }
    }

    @Test
    void equalityIsTolerantAboutNumbersAndStrings_asTheSearchPlannerIs() {
        var nine = labels("body", "T", 9);
        assertThat(ManifestLabelStage.passes(ManifestLabelStage.split(w("page_number", 9)).labels(), nine)).isTrue();
        assertThat(ManifestLabelStage.passes(ManifestLabelStage.split(w("page_number", "9")).labels(), nine)).isTrue();
        assertThat(ManifestLabelStage.passes(ManifestLabelStage.split(w("page_number", 9.0)).labels(), nine)).isTrue();
        assertThat(ManifestLabelStage.passes(ManifestLabelStage.split(w("page_number", 10)).labels(), nine)).isFalse();
    }

    @Test
    void rangeOperators_compareNumbersNumerically_andStringsLexically_andNeverAcrossTheTwo() {
        var nine = labels("body", "T", 9);
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("page_number", w("$gte", 10))).labels(), nine)).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("page_number", w("$gte", 2))).labels(), nine)).isTrue();
        // '9' > '10' lexically, but a numeric stored value against a string operand matches nothing.
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("page_number", w("$gt", "10"))).labels(), nine)).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("section_title", w("$gt", "A"))).labels(), labels("body", "B", null))).isTrue();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("section_title", w("$lt", "A"))).labels(), labels("body", "B", null))).isFalse();
    }

    @Test
    void predicatesAreAnded() {
        var split = ManifestLabelStage.split(w("section_type", "body", "page_number", w("$gte", 3)));

        assertThat(ManifestLabelStage.passes(split.labels(), labels("body", null, 4))).isTrue();
        assertThat(ManifestLabelStage.passes(split.labels(), labels("body", null, 2))).isFalse();
        assertThat(ManifestLabelStage.passes(split.labels(), labels("references", null, 4))).isFalse();
    }

    @Test
    void widen_isFourTimesTheLimit_cappedAtTheCeilingUnlessTheLimitIsAlreadyAbove() {
        assertThat(ManifestLabelStage.widen(10)).isEqualTo(40);
        assertThat(ManifestLabelStage.widen(300)).isEqualTo(1200);
        assertThat(ManifestLabelStage.widen(1000)).isEqualTo(1200);
        assertThat(ManifestLabelStage.widen(5000)).isEqualTo(5000);
    }
}
