// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.VectorSeeds;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.MANIFEST_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.SEARCH_METADATA_SCOPED_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-qpt52 review H2: the per-occurrence object of the route (b) functions and of {@code manifest_search_<dim>} is
 * two objects since vectors-049. The first (the keys that existed before) is built key by key behind {@code CASE WHEN
 * nexus.manifest_names_key(...)}; the second (bib_year, bib_authors, bib_venue, bib_citation_count, indexed_at and
 * content_type_shown) is built behind one more {@code CASE} that fires when ANY of its six keys is named, and each of
 * its six values behind its own. The cost of a search that names none of them (every search before nexus-qpt52, and
 * every search today that filters on a label) must not change: it relies on the planner folding those guards, because
 * {@code manifest_names_key} is IMMUTABLE and its arguments are constants when the function is inlined, so the whole
 * second object collapses to {@code '{}'} and its columns ({@code d.bib_*}, {@code d.indexed_at}, the {@code to_char})
 * are never read.
 *
 * <p>Nothing else pinned that. The timings ManifestSearchShortPageIntegrationTest prints do not fail, and a guard that
 * stopped folding (a volatile call, a column in the condition, a function that stopped inlining) would still return the
 * right rows, only slower and with the plan carrying the bib columns of every candidate. This test reads the plan
 * ({@code EXPLAIN (VERBOSE, COSTS OFF)}, the bind values inlined as the planner sees a first execution) of
 * {@code search_metadata_scoped_384} and {@code manifest_search_384} for a {@code where} that names none of the six
 * keys and asserts the second object is absent, and for one that names each of them and asserts it is present.
 *
 * <p>The plan, not the rows, is the subject, so the tables are empty: folding is decided from the constants alone.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestSecondObjectPlanTest {

    private static final String SVC_ROLE = "svc_second_obj";
    private static final String SVC_PASS = "svc_second_obj_pass";
    private static final String TENANT = "second-obj";
    private static final String COLL = "knowledge__second-obj__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    /** The six keys of the second object. */
    private static final List<String> SECOND_OBJECT_KEYS = List.of(
        "bib_year", "bib_authors", "bib_venue", "bib_citation_count", "indexed_at", "content_type_shown");

    /**
     * A pattern only the branch of each key prints, when that branch is built. A key literal ({@code 'bib_year'}) is
     * printed by the {@code jsonb_build_object} that holds the whole second object whichever branches are built, so it
     * does not tell the branches apart. The column a branch reads does, except that the citation count's enrichment test
     * reads the other bib columns too, so the patterns name the {@code to_jsonb(...)} each branch builds its value with,
     * and the citation count is told by {@code bib_enriched_at}. The planner aliases the document table ({@code d},
     * {@code d_1}) where the object sits in a subplan, hence the optional suffix.
     */
    private static final Map<String, String> BRANCH_PATTERN = Map.of(
        "bib_year", "to_jsonb\\(d(_\\d+)?\\.bib_year\\)",
        "bib_authors", "to_jsonb\\(COALESCE\\(d(_\\d+)?\\.bib_authors",
        "bib_venue", "to_jsonb\\(COALESCE\\(d(_\\d+)?\\.bib_venue",
        "bib_citation_count", "bib_enriched_at",
        "indexed_at", "to_char");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        // Registering the collection creates the (model, tenant) leaf of nexus.chunks: without it the planner prunes
        // every partition and the plan is a constant-false Result with no scan to read.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
    }

    @AfterAll
    void stop() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── the plans ───────────────────────────────────────────────────────────

    private static Vector query() {
        return VectorSeeds.vecXY(384, 1f, 0f);
    }

    private static Map<String, Object> where(String key, Object value) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(key, value);
        return m;
    }

    private static Map<String, Object> op(String operator, Object operand) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(operator, operand);
        return m;
    }

    private String explain(Table<?> fn) {
        return tenantScope.withTenant(TENANT, ctx -> PgContainerHelper.explainPlanWith(ctx,
            ctx.select(fn.field("id")).from(fn), new PgContainerHelper.ExplainOptions(false, true, false, false)));
    }

    /** The plan of {@code search_metadata_scoped_384} for {@code where}, through the repository's own split. */
    private String routeBPlan(Map<String, Object> where) {
        PgVectorRepository.RouteBWhere rb = PgVectorRepository.routeBWhere(where);
        return explain(SEARCH_METADATA_SCOPED_384.call(query(), new String[] {COLL}, null, null, null, null, null,
            rb.containment(), rb.manifestContainment(), rb.manifestPath(), 10, MODEL, TENANT));
    }

    /** The plan of {@code manifest_search_384} for {@code where}, with the predicate route (a) really pushes. */
    private String manifestSearchPlan(Map<String, Object> where) {
        ManifestPushdown.Plan plan = ManifestPushdown.of(ManifestLabelStage.split(where).labels());
        assertThat(plan).as("the predicate must be one route (a) pushes: %s", where).isNotNull();
        return explain(MANIFEST_SEARCH_384.call(query(), null, null, new String[] {COLL}, null, null,
            plan.containment(), plan.jsonPath(), 10, MODEL, TENANT));
    }

    /** {@code where} maps that name none of the six second-object keys, each one the route (a) search can push. */
    private static List<Map<String, Object>> wheresNamingNone() {
        return List.of(
            where("section_type", "body"),
            where("source_author", "Alice"),
            where("title", op("$in", List.of("Alpha", "Beta"))),
            where("page_number", op("$gte", 3)),
            where("content_type", "code"),
            where("tags", "arch"));
    }

    // ── none named: the second object is folded away ────────────────────────

    private void assertSecondObjectAbsent(String what, String plan) {
        assertThat(plan).as("%s: the function must inline (a Function Scan means no folding). Plan was:%n%s", what, plan)
            .doesNotContain("Function Scan")
            .contains("catalog_document_chunks");
        for (String key : SECOND_OBJECT_KEYS) {
            assertThat(plan).as("%s: the second object's key %s must not be in the plan. Plan was:%n%s", what, key, plan)
                .doesNotContain(key);
        }
        assertThat(plan).as("%s: no bib column and no timestamp rendering. Plan was:%n%s", what, plan)
            .doesNotContain("bib_citation_count")
            .doesNotContain("bib_enriched_at")
            .doesNotContain("to_char");
    }

    @Test
    void routeB_aWhereNamingNoSecondObjectKey_plansWithoutTheSecondObject() {
        int checked = 0;
        for (Map<String, Object> w : wheresNamingNone()) {
            assertSecondObjectAbsent("search_metadata_scoped_384 " + w, routeBPlan(w));
            checked++;
        }
        // a negative operator is route (b)'s alone; it names a first-object key and nothing of the second
        assertSecondObjectAbsent("search_metadata_scoped_384 $ne", routeBPlan(where("section_type", op("$ne", "references"))));
        checked++;
        // no where at all
        assertSecondObjectAbsent("search_metadata_scoped_384 no where", routeBPlan(null));
        checked++;
        assertThat(checked).as("route (b) shapes examined").isEqualTo(wheresNamingNone().size() + 2);
    }

    @Test
    void manifestSearch_aWhereNamingNoSecondObjectKey_plansWithoutTheSecondObject() {
        int checked = 0;
        for (Map<String, Object> w : wheresNamingNone()) {
            assertSecondObjectAbsent("manifest_search_384 " + w, manifestSearchPlan(w));
            checked++;
        }
        assertThat(checked).as("manifest_search shapes examined").isEqualTo(wheresNamingNone().size());
    }

    // ── one named: the second object is built, and only its branch ──────────

    /** A {@code where} that names exactly one second-object key. */
    private static Map<String, Object> wheresNaming(String key) {
        return switch (key) {
            case "bib_year" -> where("bib_year", op("$gte", 2020));
            case "bib_authors" -> where("bib_authors", "Smith, J.");
            case "bib_venue" -> where("bib_venue", op("$in", List.of("NeurIPS", "ICML")));
            case "bib_citation_count" -> where("bib_citation_count", op("$gt", 3));
            case "indexed_at" -> where("indexed_at", op("$gte", "2026-01-01"));
            // content_type_shown is named by a RANGE on content_type, which the stage reads off the shown text
            case "content_type_shown" -> where("content_type", op("$gte", "code"));
            default -> throw new IllegalArgumentException(key);
        };
    }

    private void assertSecondObjectBuiltFor(String what, String key, String plan) {
        assertThat(plan).as("%s: the function must inline. Plan was:%n%s", what, plan).doesNotContain("Function Scan");
        assertThat(plan).as("%s: the second object is built, so its key %s is in the plan. Plan was:%n%s", what, key, plan)
            .contains(key);
        String pattern = BRANCH_PATTERN.get(key);
        if (pattern != null) {
            assertThat(plan).as("%s: the branch of %s is built (%s). Plan was:%n%s", what, key, pattern, plan)
                .containsPattern(pattern);
        }
        // the branches of the keys NOT named stay folded away: naming one key does not pay for the others
        for (Map.Entry<String, String> other : BRANCH_PATTERN.entrySet()) {
            if (!other.getKey().equals(key)) {
                assertThat(plan).as("%s: naming %s must not build the branch of %s (%s). Plan was:%n%s", what, key,
                    other.getKey(), other.getValue(), plan).doesNotContainPattern(other.getValue());
            }
        }
    }

    @Test
    void routeB_aWhereNamingASecondObjectKey_plansWithItAndOnlyItsBranch() {
        for (String key : SECOND_OBJECT_KEYS) {
            assertSecondObjectBuiltFor("search_metadata_scoped_384 " + key, key, routeBPlan(wheresNaming(key)));
        }
    }

    @Test
    void manifestSearch_aWhereNamingASecondObjectKey_plansWithItAndOnlyItsBranch() {
        List<String> checked = new ArrayList<>();
        // content_type_shown is built only by route (b)'s exact range on content_type; route (a) does not push that
        for (String key : SECOND_OBJECT_KEYS) {
            if ("content_type_shown".equals(key)) {
                continue;
            }
            assertSecondObjectBuiltFor("manifest_search_384 " + key, key, manifestSearchPlan(wheresNaming(key)));
            checked.add(key);
        }
        assertThat(checked).as("manifest_search keys examined").hasSize(SECOND_OBJECT_KEYS.size() - 1);
    }
}
