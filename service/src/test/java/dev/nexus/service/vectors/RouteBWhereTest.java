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
 * nexus-6pfgb.41: how a route (b) {@code where} is split between the chunk-row containment ({@code p_where}) and the
 * manifest predicate ({@code p_manifest_where} / {@code p_manifest_path}) of the nine route (b) functions.
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
        assertThat(rb.manifestContainment().data()).isEqualTo("{\"source_author\":\"Alice\"}");
        assertThat(rb.manifestPath()).isNull();
    }

    @Test
    void aMixedWhere_splitsByKey_keepingTheChunkRowKeysInTheContainment() {
        var rb = PgVectorRepository.routeBWhere(where("lang", "en", "section_type", "body", "title", "T"));

        assertThat(rb.containment().data()).isEqualTo("{\"lang\":\"en\"}");
        assertThat(rb.manifestContainment().data()).contains("\"section_type\":\"body\"").contains("\"title\":\"T\"");
    }

    @Test
    void aNumericRange_becomesTheManifestJsonPath() {
        var rb = PgVectorRepository.routeBWhere(where("page_number", where("$gte", 9000)));

        assertThat(rb.containment()).isNull();
        assertThat(rb.manifestContainment()).isNull();
        assertThat(rb.manifestPath()).isEqualTo("exists($.\"page_number\" ? (@.number() >= 9000))");
    }

    @Test
    void aKeyThatHasNoSqlForm_staysInTheContainment_neverDropped() {
        // $ne is not compiled (negative operators stay with the stage, and route (b) has no stage), bib_year is not
        // a compiled key at all. Both keep the retired meaning: a containment.
        var rb = PgVectorRepository.routeBWhere(where("section_type", where("$ne", "body")));
        assertThat(rb.hasManifest()).isFalse();
        assertThat(rb.containment().data()).isEqualTo("{\"section_type\":{\"$ne\":\"body\"}}");

        var mixed = PgVectorRepository.routeBWhere(where("bib_year", 2020, "source_author", "Alice"));
        assertThat(mixed.containment().data()).isEqualTo("{\"bib_year\":2020}");
        assertThat(mixed.manifestContainment().data()).isEqualTo("{\"source_author\":\"Alice\"}");
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
