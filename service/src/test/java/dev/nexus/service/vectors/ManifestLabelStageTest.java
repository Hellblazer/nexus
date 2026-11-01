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
        // nexus-6pfgb.29: tags and quality_gate_overridden are answered per document now; only lang stays a chunk-row predicate.
        assertThat(split.rest()).containsOnlyKeys("lang");
        assertThat(split.labels()).extracting(ManifestLabelStage.Predicate::key)
            .containsExactly("tags", "bib_year", "section_type", "quality_gate_overridden", "source_author");
    }

    @Test
    void exactlyTheKeysWithACatalogAnswerAreAnswered_theMetaKeysAreNot() {
        // title and content_type joined the answered keys with nexus-6pfgb.25 (Sam's rulings nexus-6pfgb.2 and
        // nexus-6pfgb.3); this pin used to list them as unanswered, changed on purpose.
        for (String key : List.of("source_author", "indexed_at", "bib_year", "bib_authors", "bib_venue",
                "bib_citation_count", "bib_semantic_scholar_id", "bib_openalex_id", "bib_doi",
                "section_title", "section_type", "page_number", "title", "content_type",
                // nexus-6pfgb.29: the catalog-meta keys, answered per owning document with a row fallback.
                "tags", "category", "ttl_days", "source_agent", "extraction_method",
                "quality_gate_overridden", "extraction_source", "frecency_score")) {
            assertThat(ManifestLabelStage.split(w(key, "v")).active()).as(key).isTrue();
        }
        // Everything else stays a predicate on the chunk row, inside the search.
        for (String key : List.of("chunk_text_hash", "lang", "frecency_computed_at", "put_at", "bib_enriched_by")) {
            assertThat(ManifestLabelStage.split(w(key, "v")).active()).as(key).isFalse();
        }
        assertThat(ManifestLabelStage.META_KEYS).containsExactlyInAnyOrder("tags", "category", "ttl_days",
            "source_agent", "extraction_method", "quality_gate_overridden", "extraction_source", "frecency_score");
    }

    // ── nexus-6pfgb.29: the catalog-meta keys ───────────────────────────────

    private static boolean passes(Map<String, Object> where, Map<String, Object> value) {
        return ManifestLabelStage.passes(ManifestLabelStage.split(where).labels(), value);
    }

    @Test
    void tagsAreASet_aPositiveOperatorMatchesAnyMember_aNegativeOneNone() {
        Map<String, Object> note = w("tags", List.of("arch", "decision"));
        assertThat(passes(w("tags", "arch"), note)).isTrue();
        assertThat(passes(w("tags", "decision"), note)).isTrue();
        assertThat(passes(w("tags", "arc"), note)).as("a prefix is not a tag").isFalse();
        assertThat(passes(w("tags", w("$in", List.of("zzz", "decision"))), note)).isTrue();
        assertThat(passes(w("tags", w("$in", List.of("zzz"))), note)).isFalse();
        assertThat(passes(w("tags", w("$ne", "arch")), note)).as("$ne: none of the tags is arch").isFalse();
        assertThat(passes(w("tags", w("$ne", "other")), note)).isTrue();
        assertThat(passes(w("tags", w("$nin", List.of("x", "decision"))), note)).isFalse();
        assertThat(passes(w("tags", w("$nin", List.of("x", "y"))), note)).isTrue();
        assertThat(passes(w("tags", w("$gte", "a")), note)).as("a range over a set has no meaning").isFalse();
    }

    @Test
    void aDocumentWithNoTag_answersTheEmptyString_likeAnyUnlabelledValue() {
        for (Object none : java.util.Arrays.asList(null, List.of())) {
            Map<String, Object> value = w("tags", none);
            assertThat(passes(w("tags", "arch"), value)).as("tags=arch on %s", none).isFalse();
            assertThat(passes(w("tags", ""), value)).as("tags='' on %s", none).isTrue();
            assertThat(passes(w("tags", w("$ne", "arch")), value)).as("$ne arch on %s", none).isTrue();
            assertThat(passes(w("tags", w("$ne", "")), value)).as("$ne '' on %s", none).isFalse();
        }
        assertThat(passes(w("tags", ""), w("tags", List.of("arch")))).as("a tagged document is not untagged").isFalse();
    }

    @Test
    void tagSet_splitsAStoredValueTheWayTheClientDoes() {
        assertThat(ManifestLabelStage.tagSet(" arch , decision,,arch ,")).containsExactly("arch", "decision");
        assertThat(ManifestLabelStage.tagSet(List.of(" a ", "b", "", "a"))).containsExactly("a", "b");
        assertThat(ManifestLabelStage.tagSet("")).isEmpty();
        assertThat(ManifestLabelStage.tagSet(null)).isEmpty();
        assertThat(ManifestLabelStage.tagSet(7)).as("a number holds no tag").isEmpty();
    }

    @Test
    void ttlDaysAndFrecencyScoreAreNumeric_aNumberAndItsTextCompareEqual_rangesCompareAsNumbers() {
        assertThat(passes(w("ttl_days", 7), w("ttl_days", 7))).isTrue();
        assertThat(passes(w("ttl_days", "7"), w("ttl_days", 7))).isTrue();
        assertThat(passes(w("ttl_days", 7), w("ttl_days", "7"))).as("a stored text 7 (a legacy chunk row)").isTrue();
        assertThat(passes(w("ttl_days", 7), w("ttl_days", 70))).isFalse();
        assertThat(passes(w("ttl_days", w("$gte", 7)), w("ttl_days", 30))).isTrue();
        assertThat(passes(w("ttl_days", w("$gt", 7)), w("ttl_days", 7))).isFalse();
        assertThat(passes(w("frecency_score", w("$gte", 0.5)), w("frecency_score", 0.75))).isTrue();
        assertThat(passes(w("frecency_score", w("$lt", 0.5)), w("frecency_score", "0.25"))).isTrue();
        assertThat(passes(w("frecency_score", w("$gte", 0.5)), w("frecency_score", "abc"))).as("not a number").isFalse();
        // Zero is a value (ttl_days 0 is permanent), not an absence.
        assertThat(passes(w("ttl_days", 0), w("ttl_days", 0))).isTrue();
    }

    @Test
    void theStringMetaKeysCompareAsExactStrings() {
        for (String key : List.of("category", "source_agent", "extraction_method", "extraction_source")) {
            assertThat(passes(w(key, "x"), w(key, "x"))).as(key).isTrue();
            assertThat(passes(w(key, "x"), w(key, "X"))).as(key + " is case-exact").isFalse();
            assertThat(passes(w(key, w("$in", List.of("y", "x"))), w(key, "x"))).as(key).isTrue();
            assertThat(passes(w(key, w("$ne", "x")), w(key, "x"))).as(key).isFalse();
            assertThat(passes(w(key, "x"), w(key, null))).as(key + " absent").isFalse();
        }
        // An id-shaped value is a string: "0042" is not "42" on a string key.
        assertThat(passes(w("category", "42"), w("category", "0042"))).isFalse();
    }

    @Test
    void qualityGateOverridden_theJsonBooleanAndItsLowerCaseTextCompareEqual_nothingElseDoes() {
        assertThat(passes(w("quality_gate_overridden", true), w("quality_gate_overridden", true))).isTrue();
        assertThat(passes(w("quality_gate_overridden", "true"), w("quality_gate_overridden", true))).isTrue();
        assertThat(passes(w("quality_gate_overridden", true), w("quality_gate_overridden", "true"))).isTrue();
        assertThat(passes(w("quality_gate_overridden", false), w("quality_gate_overridden", false))).isTrue();
        assertThat(passes(w("quality_gate_overridden", true), w("quality_gate_overridden", false))).isFalse();
        // The planner's two literal forms are true/"true"; a capitalised text is neither.
        assertThat(passes(w("quality_gate_overridden", true), w("quality_gate_overridden", "True"))).isFalse();
        assertThat(passes(w("quality_gate_overridden", "True"), w("quality_gate_overridden", true))).isFalse();
    }

    @Test
    void readMeta_aKeyTheMetaHasAnswersForTheDocument_aMissingOrNullOneFallsBackToTheRow() {
        Map<String, Object> out = new HashMap<>();
        ManifestLabelStage.readMeta(org.jooq.JSONB.valueOf(
            "{\"tags\":[\"arch\",\" b \"],\"category\":\"\",\"ttl_days\":0,\"frecency_score\":0.75,"
            + "\"quality_gate_overridden\":false,\"source_agent\":null}"),
            ManifestLabelStage.META_KEYS, out);

        assertThat(out).containsEntry("tags", List.of("arch", "b"))
            .containsEntry("category", "").containsEntry("ttl_days", 0)
            .containsEntry("frecency_score", 0.75).containsEntry("quality_gate_overridden", false);
        // Absent (extraction_method) and JSON null (source_agent): no entry, so the chunk row answers.
        assertThat(out).doesNotContainKeys("source_agent", "extraction_method", "extraction_source");

        Map<String, Object> onlyNamed = new HashMap<>();
        ManifestLabelStage.readMeta(org.jooq.JSONB.valueOf("{\"category\":\"c\",\"ttl_days\":3}"),
            java.util.Set.of("category"), onlyNamed);
        assertThat(onlyNamed).containsOnlyKeys("category");

        Map<String, Object> none = new HashMap<>();
        ManifestLabelStage.readMeta(null, ManifestLabelStage.META_KEYS, none);
        assertThat(none).isEmpty();
    }

    @Test
    void derivedFormat_readsTheExtensionFirst_thenTheCatalogType_asPythonsPathSuffixDoes() {
        // The extension wins: a markdown file registered as prose, rdr or paper is markdown, a .pdf is pdf.
        assertThat(ManifestLabelStage.derivedFormat("prose", "notes/d.md")).isEqualTo("markdown");
        assertThat(ManifestLabelStage.derivedFormat("rdr", "docs/rdr/x.MARKDOWN")).isEqualTo("markdown");
        assertThat(ManifestLabelStage.derivedFormat("paper", "papers/c.pdf")).isEqualTo("pdf");
        assertThat(ManifestLabelStage.derivedFormat("", "papers/C.PDF")).isEqualTo("pdf");
        assertThat(ManifestLabelStage.derivedFormat("paper", "")).isEqualTo("pdf");
        assertThat(ManifestLabelStage.derivedFormat("pdf", "")).isEqualTo("pdf");
        // Then the catalog type.
        assertThat(ManifestLabelStage.derivedFormat("code", "src/alpha.py")).isEqualTo("code");
        assertThat(ManifestLabelStage.derivedFormat("prose", "notes/readme.txt")).isEqualTo("prose");
        assertThat(ManifestLabelStage.derivedFormat("rdr", "")).isEqualTo("prose");
        // None: a note, a type outside the rules, nothing at all.
        assertThat(ManifestLabelStage.derivedFormat("knowledge", "")).isNull();
        assertThat(ManifestLabelStage.derivedFormat("docs", "")).isNull();
        assertThat(ManifestLabelStage.derivedFormat("", "")).isNull();
        assertThat(ManifestLabelStage.derivedFormat(null, null)).isNull();
        // Suffix edges: a dotfile has none, a dot in a directory does not count, a double extension is its last.
        assertThat(ManifestLabelStage.derivedFormat("", ".gitignore")).isNull();
        assertThat(ManifestLabelStage.derivedFormat("", "dir.pdf/file")).isNull();
        assertThat(ManifestLabelStage.derivedFormat("", "a.tar.pdf")).isEqualTo("pdf");
        assertThat(ManifestLabelStage.derivedFormat("", "a.pdf.gz")).isNull();
    }

    @Test
    void contentType_pdfAndMarkdownAreFormats_everyOtherWordIsTheCatalogType() {
        var pdf = new ManifestLabelStage.ContentType("paper", "pdf", "markdown");
        assertThat(ManifestLabelStage.matches(pdf, "pdf")).isTrue();
        assertThat(ManifestLabelStage.matches(pdf, "markdown")).isFalse();
        assertThat(ManifestLabelStage.matches(pdf, "paper")).isTrue();
        assertThat(ManifestLabelStage.matches(pdf, "prose")).isFalse();

        // A markdown file the catalog calls prose: prose includes it.
        var md = new ManifestLabelStage.ContentType("prose", "markdown", "pdf");
        assertThat(ManifestLabelStage.matches(md, "markdown")).isTrue();
        assertThat(ManifestLabelStage.matches(md, "prose")).isTrue();
        assertThat(ManifestLabelStage.matches(md, "pdf")).isFalse();

        // No derived format: pdf and markdown fall back to the chunk row's value.
        var note = new ManifestLabelStage.ContentType("knowledge", null, "pdf");
        assertThat(ManifestLabelStage.matches(note, "pdf")).isTrue();
        assertThat(ManifestLabelStage.matches(note, "knowledge")).isTrue();
        // No catalog type: any other word falls back to the row's too.
        var bare = new ManifestLabelStage.ContentType(null, "pdf", "code");
        assertThat(ManifestLabelStage.matches(bare, "code")).isTrue();
        assertThat(ManifestLabelStage.matches(bare, "pdf")).isTrue();
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
        // null is a MISSING value and "" a stored empty one; both are the empty string here. The chunk-row
        // filter matched only a stored "" on $eq "" and kept a missing key on $ne "".
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

    // ── nexus-6pfgb.26: an owner's span replaces the chunk row's, field by field ──

    private static ManifestLabelStage.Occurrence owner(Integer ls, Integer le, Integer cs, Integer ce) {
        return new ManifestLabelStage.Occurrence("doc", 0, null, null, null, true, "file:///doc.md",
            ls, le, cs, ce, Map.of());
    }

    @Test
    void anOwnersSpan_replacesTheChunkRowsFields() {
        Map<String, Object> row = w("line_start", 30, "line_end", 35, "chunk_start_char", 300, "chunk_end_char", 350);

        ManifestLabelStage.putSpan(row, owner(10, 20, 100, 200));

        assertThat(row).containsEntry("line_start", 10).containsEntry("line_end", 20)
            .containsEntry("chunk_start_char", 100).containsEntry("chunk_end_char", 200);
    }

    @Test
    void anOwnerWithNoSpan_leavesTheChunkRowsValuesAlone() {
        Map<String, Object> row = w("line_start", 30, "line_end", 35, "chunk_start_char", 300, "chunk_end_char", 350);

        ManifestLabelStage.putSpan(row, owner(null, null, null, null));

        assertThat(row).containsOnly(org.assertj.core.api.Assertions.entry("line_start", 30),
            org.assertj.core.api.Assertions.entry("line_end", 35),
            org.assertj.core.api.Assertions.entry("chunk_start_char", 300),
            org.assertj.core.api.Assertions.entry("chunk_end_char", 350));
    }

    @Test
    void anOwnerWithOnlyLines_writesOnlyTheTwoLineFields_andNoCharField() {
        Map<String, Object> row = w();

        ManifestLabelStage.putSpan(row, owner(4, 9, null, null));

        assertThat(row).containsOnlyKeys("line_start", "line_end");
    }
}
