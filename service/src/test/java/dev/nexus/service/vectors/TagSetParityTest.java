// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Routines.catalogTagSet;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.29: the tag-set split has ONE SQL definition, {@code nexus.catalog_tag_set(jsonb)} (vectors-044), and
 * the Java copy the stage keeps ({@link ManifestLabelStage#tagSet}) must give the same SET of tags on every stored
 * value. The in-search EXISTS tests membership of the SQL set, the stage trims the rest with the Java one; a
 * difference makes the EXISTS stricter than the stage and silently drops a row (the .62 parity class).
 *
 * <p>The SQL function answers {@code [""]} for no tag (so an untagged document answers the empty string); the Java
 * one answers an empty list, and the comparison reads the sentinel as empty. Order and duplicates are not
 * compared: a test of membership cannot see them. Both server copies trim ASCII whitespace only (space, tab, CR,
 * LF, FF, VT); a tag with Unicode whitespace at its ends (U+2003, U+3000, U+00A0) is kept whole by both, and the
 * matrix holds such rows. The client's {@code split_note_tags} strips Unicode whitespace too: one way only, since a
 * tag the client writes never carries it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TagSetParityTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final List<String> MATRIX = List.of(
        "[\"arch\",\"decision\"]",
        "[\" a \",\"b\",\"\",\"a\"]",
        "[\"\\tt\\t\",\"a\\nb\",\"\\r\\f\\u000bq\"]",
        "\"arch, decision,,arch ,\"",
        "\"single\"",
        "\" spaced out ,\\ttabbed\\t\"",
        "\"\"",
        "\"  ,  \"",
        "[]",
        "[\"\"]",
        "null",
        "7",
        "true",
        "{\"a\":1}",
        "[\"x\",5,true]",
        "[\"a\",null,\"b\"]",
        "[\"dup\",\"dup\",\"Dup\"]",
        "[\"\u2003arch\",\"arch\",\"\u3000\",\"\u00a0x\u00a0\"]",
        "\"\u2003arch, \u3000decision\u2003,arch\"");

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
    }

    @AfterAll
    void stop() {
        if (pg != null) pg.stop();
    }

    private Set<String> sql(DSLContext ctx, JSONB stored) throws Exception {
        JSONB out = ctx.select(catalogTagSet(DSL.val(stored, SQLDataType.JSONB))).fetchOne(0, JSONB.class);
        Set<String> tags = new HashSet<>();
        for (JsonNode n : MAPPER.readTree(out.data())) {
            tags.add(n.textValue());
        }
        // The sentinel for "no tag" is the array holding the empty string.
        return tags.equals(Set.of("")) ? Set.of() : tags;
    }

    @Test
    void javaAndSqlSplitEveryStoredValueIntoTheSameSetOfTags_andTheMatrixReachesBothEmptyAndNonEmpty() throws Exception {
        List<String> disagreements = new ArrayList<>();
        int empty = 0;
        int nonEmpty = 0;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String json : MATRIX) {
                Set<String> db = sql(ctx, JSONB.valueOf(json));
                Set<String> java = new HashSet<>(ManifestLabelStage.tagSet(MAPPER.readTree(json)));
                if (!db.equals(java)) {
                    disagreements.add(json + ": java=" + java + " sql=" + db);
                }
                if (java.isEmpty()) empty++; else nonEmpty++;
            }
        }
        assertThat(disagreements).as("stored values where the Java split and nexus.catalog_tag_set differ").isEmpty();
        assertThat(empty).as("matrix rows with no tag").isGreaterThanOrEqualTo(5);
        assertThat(nonEmpty).as("matrix rows with tags").isGreaterThanOrEqualTo(8);
    }

    @Test
    void anSqlNullHoldsNoTag() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(sql(ctx, null)).isEmpty();
        }
    }
}
