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
    void documentKeysLeaveTheWhere_withTheLabelKeys_theRestStaysForTheSearchFunction() {
        var split = ManifestLabelStage.split(w("lang", "en", "tags", "arch", "bib_year", w("$gte", 2020),
            "section_type", "body", "quality_gate_overridden", true, "source_author", w("$ne", "x")));

        assertThat(split.active()).isTrue();
        // tags and quality_gate_overridden have no catalog writer yet: they stay predicates on the chunk row.
        assertThat(split.rest()).containsOnlyKeys("lang", "tags", "quality_gate_overridden");
        assertThat(split.labels()).extracting(ManifestLabelStage.Predicate::key)
            .containsExactly("bib_year", "section_type", "source_author");
    }

    @Test
    void exactlyTheKeysWithACatalogAnswerAreAnswered_theMetaKeysTitleAndContentTypeAreNot() {
        for (String key : List.of("source_author", "indexed_at", "bib_year", "bib_authors", "bib_venue",
                "bib_citation_count", "bib_semantic_scholar_id", "bib_openalex_id", "bib_doi",
                "section_title", "section_type", "page_number")) {
            assertThat(ManifestLabelStage.split(w(key, "v")).active()).as(key).isTrue();
        }
        // The catalog meta keys have no writer (epic nexus-6pfgb wave 1), so answering them would only put
        // those filters under the overfetch cut. nexus-6pfgb.25 owns title= and content_type=.
        for (String key : List.of("tags", "category", "ttl_days", "source_agent", "extraction_method",
                "quality_gate_overridden", "extraction_source", "frecency_score",
                "title", "content_type", "chunk_text_hash")) {
            assertThat(ManifestLabelStage.split(w(key, "v")).active()).as(key).isFalse();
        }
    }

    @Test
    void aMalformedDocumentKeyOperator_isRefusedLikeALabelOne() {
        assertThatThrownBy(() -> ManifestLabelStage.split(w("bib_year", w("$regex", "x"))))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("unsupported where operator");
        assertThatThrownBy(() -> ManifestLabelStage.split(w("source_author", List.of("a"))))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void anUnenrichedDocumentHasNoBib_anEnrichedOneHasItsValues_zeroAndEmptyAreNotRecorded() {
        var keys = java.util.Set.of("bib_year", "bib_citation_count", "bib_doi", "bib_venue");

        // The columns are NOT NULL DEFAULT 0 / '': all defaults and no bib_enriched_at is unenriched.
        Map<String, Object> none = new HashMap<>();
        ManifestLabelStage.bibValues(keys, 0, "", "", 0, "", "", "", false, none);
        assertThat(none).containsOnlyKeys(keys).containsEntry("bib_year", null)
            .containsEntry("bib_citation_count", null).containsEntry("bib_doi", null);

        // Enriched (a year): the year and doi are values; the empty venue is not recorded.
        Map<String, Object> some = new HashMap<>();
        ManifestLabelStage.bibValues(keys, 2021, "", "", 0, "", "", "10.1/a", false, some);
        assertThat(some).containsEntry("bib_year", 2021).containsEntry("bib_doi", "10.1/a")
            .containsEntry("bib_citation_count", 0).containsEntry("bib_venue", null);

        // bib_enriched_at alone marks an enriched document whose backend returned nothing: year 0 stays unrecorded.
        Map<String, Object> stamped = new HashMap<>();
        ManifestLabelStage.bibValues(keys, 0, "", "", 0, "", "", "", true, stamped);
        assertThat(stamped).containsEntry("bib_year", null).containsEntry("bib_citation_count", 0);

        // Only the keys the predicates name are produced.
        Map<String, Object> one = new HashMap<>();
        ManifestLabelStage.bibValues(java.util.Set.of("bib_doi"), 2021, "a", "v", 3, "s", "o", "d", true, one);
        assertThat(one).containsOnlyKeys("bib_doi");
    }

    @Test
    void indexedAt_isRenderedAsTheChunkRowsOwnUtcText() {
        assertThat(ManifestLabelStage.indexedAtText(java.time.OffsetDateTime.parse("2026-03-01T00:00:00Z")))
            .isEqualTo("2026-03-01T00:00:00.000000+00:00");
        assertThat(ManifestLabelStage.indexedAtText(java.time.OffsetDateTime.parse("2026-03-01T02:30:05.123456+02:00")))
            .isEqualTo("2026-03-01T00:30:05.123456+00:00");
    }

    @Test
    void aNumericDocumentValue_passesTheSameOperatorsAsALabel() {
        var recent = ManifestLabelStage.split(w("bib_year", w("$gte", 2020))).labels();
        assertThat(ManifestLabelStage.passes(recent, w("bib_year", 2021))).isTrue();
        assertThat(ManifestLabelStage.passes(recent, w("bib_year", null))).isFalse();
        var notThatYear = ManifestLabelStage.split(w("bib_year", w("$ne", 2021))).labels();
        assertThat(ManifestLabelStage.passes(notThatYear, w("bib_year", null))).isTrue();
    }

    @Test
    void anExplicitEmptyStringOperand_isComparedWithAnUnlabelledValueAsTheEmptyString() {
        for (Object unlabelled : java.util.Arrays.asList(null, "")) {
            Map<String, Object> value = w("source_author", unlabelled);
            assertThat(ManifestLabelStage.passes(
                ManifestLabelStage.split(w("source_author", "")).labels(), value)).as("$eq '' on %s", unlabelled).isTrue();
            assertThat(ManifestLabelStage.passes(
                ManifestLabelStage.split(w("source_author", w("$in", List.of("x", "")))).labels(), value))
                .as("$in [''] on %s", unlabelled).isTrue();
            assertThat(ManifestLabelStage.passes(
                ManifestLabelStage.split(w("source_author", w("$ne", ""))).labels(), value))
                .as("$ne '' on %s", unlabelled).isFalse();
            assertThat(ManifestLabelStage.passes(
                ManifestLabelStage.split(w("source_author", w("$nin", List.of("", "x")))).labels(), value))
                .as("$nin [''] on %s", unlabelled).isFalse();
            // Any other operand keeps the unlabelled rule: negative operators keep it, the rest drop it.
            assertThat(ManifestLabelStage.passes(
                ManifestLabelStage.split(w("source_author", "x")).labels(), value)).isFalse();
            assertThat(ManifestLabelStage.passes(
                ManifestLabelStage.split(w("source_author", w("$ne", "x"))).labels(), value)).isTrue();
            assertThat(ManifestLabelStage.passes(
                ManifestLabelStage.split(w("source_author", w("$gte", ""))).labels(), value)).isFalse();
        }
        // A labelled value is not the empty string.
        Map<String, Object> alice = w("source_author", "Alice");
        assertThat(ManifestLabelStage.passes(ManifestLabelStage.split(w("source_author", "")).labels(), alice)).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("source_author", w("$ne", ""))).labels(), alice)).isTrue();
        // The section labels run the same code.
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("section_type", "")).labels(), labels("", "", null))).isTrue();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("section_type", w("$ne", ""))).labels(), labels("", "", null))).isFalse();
    }

    @Test
    void aStringKeyComparesAsAnExactString_aNumericKeyKeepsTheNumberTextTolerance() {
        // bib_semantic_scholar_id and bib_doi are strings: "0042" is not "42", "1e3" is not "1000".
        Map<String, Object> s2 = w("bib_semantic_scholar_id", "0042");
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_semantic_scholar_id", "0042")).labels(), s2)).isTrue();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_semantic_scholar_id", "42")).labels(), s2)).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_semantic_scholar_id", 42)).labels(), s2)).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_semantic_scholar_id", w("$ne", "42"))).labels(), s2)).isTrue();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_semantic_scholar_id", w("$in", List.of("42", "7")))).labels(), s2)).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_doi", "1000")).labels(), w("bib_doi", "1e3"))).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_doi", "1e3")).labels(), w("bib_doi", "1e3"))).isTrue();
        // A numeric operand never ranges over a string key; a string operand ranges lexically.
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_semantic_scholar_id", w("$gte", 10))).labels(), s2)).isFalse();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_semantic_scholar_id", w("$gte", "0040"))).labels(), s2)).isTrue();

        // bib_year, bib_citation_count and page_number are numbers: a text 2021 is the number 2021.
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_year", 2021)).labels(), w("bib_year", "2021"))).isTrue();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_year", "2021")).labels(), w("bib_year", 2021))).isTrue();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("bib_citation_count", w("$gte", 10))).labels(), w("bib_citation_count", "40")))
            .isTrue();
        assertThat(ManifestLabelStage.passes(
            ManifestLabelStage.split(w("page_number", "5")).labels(), labels("body", "T", 5))).isTrue();
    }

    @Test
    void widen_isFourTimesTheLimit_cappedAtTheCeilingUnlessTheLimitIsAlreadyAbove() {
        assertThat(ManifestLabelStage.widen(10)).isEqualTo(40);
        assertThat(ManifestLabelStage.widen(300)).isEqualTo(1200);
        assertThat(ManifestLabelStage.widen(1000)).isEqualTo(1200);
        assertThat(ManifestLabelStage.widen(5000)).isEqualTo(5000);
    }
}
