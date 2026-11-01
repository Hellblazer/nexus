// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

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
import java.util.List;

import static dev.nexus.service.jooq.nexus.Routines.manifestNamesKey;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pfgb.61: {@code nexus.manifest_names_key(where, path, key)} (vectors-045) is what lets {@code
 * manifest_search_<dim>} build only the keys of its per-occurrence object that the compiled predicate names. It must
 * never say "no" for a key a predicate reads, or the object holds JSON null where the test expects the value and the
 * row is silently dropped; saying "yes" for a key nobody reads only costs time.
 *
 * <p>Every path {@link ManifestPushdown} compiles quotes its keys ({@code $."page_number"}, {@code
 * exists($."tags"[*] ? ...)}), and its containment holds keys at the top level, so those are the two forms the function
 * reads. The matrix below also feeds it the shapes the compiler really emits for each pushable key, so a key added to
 * the pushdown without the object (or the reverse) is caught here and not by a dropped row.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestNamesKeyTest {

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

    private boolean names(DSLContext ctx, String where, String path, String key) {
        return Boolean.TRUE.equals(ctx.select(manifestNamesKey(
                DSL.val(where == null ? null : JSONB.valueOf(where), SQLDataType.JSONB),
                DSL.val(path, SQLDataType.VARCHAR), DSL.val(key, SQLDataType.VARCHAR)))
            .fetchOne(0, Boolean.class));
    }

    @Test
    void truthTable() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // the containment names the keys it holds at its top level
            assertThat(names(ctx, "{\"section_type\":\"x\",\"title\":\"t\"}", null, "section_type")).isTrue();
            assertThat(names(ctx, "{\"section_type\":\"x\",\"title\":\"t\"}", null, "title")).isTrue();
            assertThat(names(ctx, "{\"section_type\":\"x\"}", null, "section_title")).isFalse();
            assertThat(names(ctx, "{\"section_type\":\"x\"}", null, "type")).as("a substring of a key").isFalse();
            // the path names the keys it quotes
            assertThat(names(ctx, null, "$.\"page_number\" >= 3", "page_number")).isTrue();
            assertThat(names(ctx, null, "$.\"section_title\" >= 3", "title")).as("title is not section_title").isFalse();
            assertThat(names(ctx, null, "(exists($.\"tags\"[*] ? (@ == \"a\")))", "tags")).isTrue();
            // both forms at once, either sufficing
            assertThat(names(ctx, "{\"title\":\"t\"}", "$.\"page_number\" >= 3", "page_number")).isTrue();
            assertThat(names(ctx, "{\"title\":\"t\"}", "$.\"page_number\" >= 3", "title")).isTrue();
            assertThat(names(ctx, "{\"title\":\"t\"}", "$.\"page_number\" >= 3", "category")).isFalse();
            // neither argument: nothing is named, and the answer is false, never NULL
            assertThat(names(ctx, null, null, "title")).isFalse();
        }
    }

    /**
     * The predicates {@link ManifestPushdown#of} compiles for every pushable key, each checked against the key it
     * reads: the function must name it. A non-vacuity floor keeps the list from shrinking silently.
     */
    @Test
    void everyKeyThePushdownCompilesIsNamed() throws Exception {
        List<String> keys = List.of("section_title", "section_type", "page_number", "title", "source_author", "bib_doi",
            "bib_openalex_id", "bib_semantic_scholar_id", "content_type", "tags", "category", "ttl_days", "source_agent",
            "extraction_method", "quality_gate_overridden", "extraction_source", "frecency_score");
        List<String> missed = new ArrayList<>();
        int checked = 0;
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String key : keys) {
                List<Object> operands = new ArrayList<>(List.of("v", 7));
                for (Object operand : operands) {
                    for (var predicate : List.of(
                            new ManifestLabelStage.Predicate(key, "$eq", operand),
                            new ManifestLabelStage.Predicate(key, "$in", List.of(operand, "w")),
                            new ManifestLabelStage.Predicate(key, "$gte", operand))) {
                        if (!ManifestPushdown.pushable(predicate)) {
                            continue;
                        }
                        var plan = ManifestPushdown.of(List.of(predicate));
                        String where = plan.containment() == null ? null : plan.containment().data();
                        checked++;
                        if (!names(ctx, where, plan.jsonPath(), key)) {
                            missed.add(key + " " + predicate.operator() + " " + operand + " -> " + where + " | " + plan.jsonPath());
                        }
                    }
                }
            }
        }
        assertThat(missed).as("pushed predicates whose key manifest_names_key does not name").isEmpty();
        assertThat(checked).as("pushed predicates examined").isGreaterThanOrEqualTo(40);
    }
}
