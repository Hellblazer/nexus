// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-u9dc5 (review r1, F3): the superset driver of the live get reads. A positive {@code $eq} or {@code $in} on
 * {@code title} or {@code source_author} adds {@code chash IN (<live owners whose own value is an operand or is
 * blank>)} so PostgreSQL starts from the catalog; anything else adds nothing. That the driver never drops a chunk the
 * function keeps is held by {@code ManifestRouteBDifferentialTest}'s route (c) leg (the 5,778-predicate matrix, with
 * blank and non-blank titles and authors); this test holds the SHAPE: when it applies, when it does not, and that the
 * blank owners are kept.
 */
class ManifestDriverConditionTest {

    private static final DimTables.ChunkTable CH = DimTables.CHUNKS.get(384);

    private static Map<String, Object> where(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private static String sql(Map<String, Object> where) {
        return DSL.using(SQLDialect.POSTGRES).renderInlined(
            PgVectorRepository.manifestDriverCondition(CH, "t1", "coll1", where));
    }

    private static int occurrences(String haystack, String needle) {
        int n = 0;
        for (int i = haystack.indexOf(needle); i >= 0; i = haystack.indexOf(needle, i + 1)) {
            n++;
        }
        return n;
    }

    @Test
    void aPositiveEqualityOnTitleDrivesFromTheLiveOwnersWhoseTitleIsTheOperandOrBlank() {
        String sql = sql(where("title", "Doc 057"));
        assertThat(sql).contains("catalog_document_chunks").contains("catalog_documents")
            .contains("\"deleted_at\" is null").contains("'t1'").contains("'coll1'")
            .contains("in ('Doc 057')")
            .as("the blank owners are kept: the function answers them from the chunk row")
            .contains("or not ((coalesce(\"nexus\".\"catalog_documents\".\"title\", '') ~ '\\S'))");
    }

    @Test
    void theOperatorFormOfEqualityAndInDriveToo() {
        assertThat(sql(where("title", where("$eq", "A")))).contains("in ('A')");
        assertThat(sql(where("title", where("$in", List.of("A", "B"))))).contains("in ('A', 'B')");
        assertThat(sql(where("source_author", where("$in", List.of("X"))))).contains("\"author\"").contains("in ('X')")
            .as("author is nullable, the blank test reads it through coalesce").contains("coalesce(\"nexus\".\"catalog_documents\".\"author\"");
    }

    @Test
    void anOperandThatIsNotAStringComparesByItsText_asTheFunctionDoes() {
        assertThat(sql(where("title", 3))).contains("in ('3')");
    }

    @Test
    void bothKeysAddOneDriverEach() {
        String sql = sql(where("title", "A", "source_author", "B"));
        assertThat(occurrences(sql, "in (select")).isEqualTo(2);
    }

    @Test
    void whatIsNotAPositiveTitleOrAuthorTestAddsNothing() {
        for (Map<String, Object> w : List.of(
                where("title", where("$ne", "A")),
                where("title", where("$nin", List.of("A"))),
                where("title", where("$gte", "A")),
                where("source_author", where("$ne", "A")),
                where("section_type", "body"),
                where("tags", "arch"),
                where("source_key", "k"),
                where("title", where("$in", List.of())),
                where("title", "a\u0000b"))) {
            assertThat(sql(w)).as("%s", w).doesNotContain("in (select");
        }
        assertThat(sql(null)).doesNotContain("in (select");
        assertThat(sql(Map.of())).doesNotContain("in (select");
    }

    @Test
    void aTitleBesideAKeyItCannotDriveStillDrives() {
        assertThat(sql(where("title", "A", "section_type", "body"))).contains("in ('A')");
    }
}
