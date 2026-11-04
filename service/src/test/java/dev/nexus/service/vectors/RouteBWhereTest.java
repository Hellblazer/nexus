// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-6pfgb.41 and nexus-qpt52: how a route (b) {@code where} is split between the chunk-row containment ({@code
 * p_where}) and the manifest predicate ({@code p_manifest_where} / {@code p_manifest_path}) of the nine route (b)
 * functions. Every key {@link ManifestLabelStage} answers leaves the containment, whatever its operator; only a key
 * the stage does not answer (a chunk-row key) stays in it.
 */
class RouteBWhereTest {

    private static Map<String, Object> where(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    @Test
    void noWhere_isNoPredicateAtAll() {
        for (Map<String, Object> w : java.util.Arrays.asList(null, Map.<String, Object>of())) {
            var rb = PgVectorRepository.routeBWhere(w);
            assertThat(rb.containment()).isNull();
            assertThat(rb.hasManifest()).isFalse();
            assertThat(rb.manifestContainment()).isNull();
            assertThat(rb.manifestPath()).isNull();
        }
    }

    @Test
    void aChunkRowKeyOnly_isTheRetiredContainment_byteForByte() {
        var rb = PgVectorRepository.routeBWhere(where("lang", "en", "n", 3));

        assertThat(rb.containment().data()).isEqualTo("{\"lang\":\"en\",\"n\":3}");
        assertThat(rb.hasManifest()).isFalse();
    }

    @Test
    void aDocumentOrLabelKey_leavesTheContainmentAndBecomesTheManifestPlan() {
        var rb = PgVectorRepository.routeBWhere(where("source_author", "Alice"));

        assertThat(rb.containment()).isNull();
        assertThat(rb.hasManifest()).isTrue();
        // nexus-qpt52: the exact form is a jsonpath only (a containment is type-strict, the stage is not)
        assertThat(rb.manifestContainment()).isNull();
        assertThat(rb.manifestPath()).isEqualTo("exists($.\"source_author\" ? (@.string() == \"Alice\"))");
    }

    @Test
    void aMixedWhere_splitsByKey_keepingTheChunkRowKeysInTheContainment() {
        var rb = PgVectorRepository.routeBWhere(where("lang", "en", "section_type", "body", "title", "T"));

        assertThat(rb.containment().data()).isEqualTo("{\"lang\":\"en\"}");
        assertThat(rb.manifestPath()).contains("\"section_type\"").contains("\"title\"");
    }

    @Test
    void aNumericRange_becomesTheManifestJsonPath() {
        var rb = PgVectorRepository.routeBWhere(where("page_number", where("$gte", 9000)));

        assertThat(rb.containment()).isNull();
        assertThat(rb.manifestContainment()).isNull();
        assertThat(rb.manifestPath()).isEqualTo("exists($.\"page_number\" ? (@.number() >= 9000))");
    }

    @Test
    void aNegativeOperatorOrAKeyRoute_bDidNotAnswerBefore_nowLeavesTheContainmentToo() {
        // nexus-qpt52. These stayed in the containment before: an operator map is never contained in a chunk row, so
        // $ne matched nothing, and bib_year matched the last writer's row.
        var ne = PgVectorRepository.routeBWhere(where("section_type", where("$ne", "body")));
        assertThat(ne.containment()).isNull();
        assertThat(ne.manifestPath()).isEqualTo("!exists($.\"section_type\" ? (@.string() == \"body\"))");

        var mixed = PgVectorRepository.routeBWhere(where("bib_year", 2020, "source_author", "Alice", "lang", "en"));
        assertThat(mixed.containment().data()).isEqualTo("{\"lang\":\"en\"}");
        assertThat(mixed.manifestPath()).contains("\"bib_year\"").contains("\"source_author\"").contains(" && ");

        for (String key : ManifestLabelStage.ANSWERED_KEYS) {
            for (String operator : List.of("$eq", "$ne", "$gte", "$lt")) {
                var rb = PgVectorRepository.routeBWhere(where(key, where(operator, "x")));
                assertThat(rb.containment()).as("%s %s", key, operator).isNull();
                assertThat(rb.hasManifest()).as("%s %s", key, operator).isTrue();
            }
        }
    }

    @Test
    void theBoundedCount_isSkippedOnlyForANegativeOperatorOnContentType() {
        // route (a)'s content_type array, which manifest_qualifying_hits builds, lists more words than route (b)'s, so a
        // negation over it admits fewer occurrences than route (b) returns and the count could fall below the page.
        assertThat(PgVectorRepository.routeBWhere(where("content_type", where("$ne", "pdf"))).countable()).isFalse();
        assertThat(PgVectorRepository.routeBWhere(where("content_type", where("$nin", List.of("pdf")))).countable()).isFalse();
        assertThat(PgVectorRepository.routeBWhere(where("content_type", "pdf")).countable()).isTrue();
        assertThat(PgVectorRepository.routeBWhere(where("section_type", where("$ne", "x"))).countable()).isTrue();
        assertThat(PgVectorRepository.routeBWhere(null).countable()).isTrue();
    }

    @Test
    void aChunkRowKey_staysInTheContainment_neverDropped() {
        // lang is not a key the stage answers: an operator on it is a containment as before.
        var rb = PgVectorRepository.routeBWhere(where("lang", where("$ne", "en")));
        assertThat(rb.hasManifest()).isFalse();
        assertThat(rb.containment().data()).isEqualTo("{\"lang\":{\"$ne\":\"en\"}}");
    }

    @Test
    void anOperatorTheStageRefuses_isRefusedHere_withTheSameError() {
        assertThatThrownBy(() -> PgVectorRepository.routeBWhere(where("section_type", where("$regex", "x"))))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("unsupported where operator");
        assertThatThrownBy(() -> PgVectorRepository.routeBWhere(where("title", List.of("a"))))
            .isInstanceOf(IllegalArgumentException.class);
    }
}
