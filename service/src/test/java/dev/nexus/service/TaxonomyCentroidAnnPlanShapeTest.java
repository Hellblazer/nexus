// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.tables.records.TaxonomyCentroidsRecord;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.AnnHit;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.TableField;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Random;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_1024;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_384;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_768;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

/**
 * RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5) — EXPLAIN-based
 * plan-shape proof for {@link TaxonomyCentroidRepository#annQuery}'s ANN query,
 * mirroring {@code PgVectorRepositoryRawSqlPlanShapeTest}'s methodology for the sibling
 * {@code nexus.chunks} unification.
 *
 * <p>nexus-zrcj7 step 4 (Sam's no-SQL-strings-in-Java directive): {@code annQuery}'s
 * former string-concatenated raw SQL is retired onto {@code nexus.taxonomy_ann_query_
 * <dim>} (vectors-013), an inlinable schema function — the EXPLAIN targets below now
 * call the function directly rather than reproducing its retired inline query text, and
 * carry a NEW inlining assertion (no {@code Function Scan}) alongside the pre-existing
 * HNSW-index-binding proof.
 *
 * <p><strong>What this proves and why it matters.</strong> {@code annQuery}'s raw ANN
 * query was found hand-rolling {@code "nexus.taxonomy_centroids_" + dim} for the table
 * name and a bare {@code embedding} column — NEITHER resolves against the unified {@code
 * nexus.taxonomy_centroids} table (three nullable {@code embedding_384}/{@code
 * embedding_768}/{@code embedding_1024} columns, one non-null per row): the table name
 * would fail LOUD (relation does not exist) but the column name is plain string
 * interpolation, so a stale reference would have been a SILENT RUNTIME failure invisible
 * to the compile-time census that scoped the rest of this repoint batch. Fixed to consult
 * {@link DimTables#CENTROIDS_TABLE_NAME} / {@link DimTables#embeddingColumn(int)} and to
 * add an explicit {@code embedding_<dim> IS NOT NULL} predicate.
 *
 * <p>The {@code IS NOT NULL} predicate is not cosmetic: unlike {@code
 * PgVectorRepository}'s per-collection dim homogeneity (D2's hazard analysis — a
 * collection's rows are all one dim by construction), a taxonomy collection CAN
 * legitimately hold centroids at two dims at once mid-migration (this repository's own
 * {@code dimensionProbe} javadoc). {@link
 * #annQuery_mixedDimCollection_onlyMatchesQueriedDim} seeds exactly that scenario and
 * proves the query does not silently rank against, or get confused by, the foreign-dim
 * rows sharing the same physical table.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyCentroidAnnPlanShapeTest {

    private static final String SVC_ROLE = "svc_centroid_planshape_test";
    /** Dedicated ef_search-boosted role for the real-call companions — see startAll(). */
    private static final String SVC_ROLE_REALCALL = "svc_centroid_planshape_realcall";
    private static final String SVC_PASS = "svc_centroid_planshape_pass";
    private static final String TENANT = "centroid-planshape-tenant";

    private static final String COL_1024 = "knowledge__planshape1024";
    private static final String COL_768  = "docs__planshape768";
    private static final String COL_384  = "knowledge__planshape384";
    private static final String COL_MIXED = "knowledge__planshapemixed";

    // Modest but non-trivial per-dim cardinality: large enough that the planner's default
    // cost model naturally prefers the HNSW index over Seq Scan + Sort, small enough to
    // build fast under Testcontainers.
    private static final int CENTROIDS_PER_DIM = 3_000;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;
    HikariDataSource realCallDs;
    TaxonomyCentroidRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        // Second role + pool, DEDICATED to the real-call companions (found during Step G
        // cluster-B triage, nexus-o8dil.16/.48 — same fix as
        // PgVectorRepositoryRawSqlPlanShapeTest, see that class's startAll() javadoc for
        // the full two-revision history and why the final fix is "force Seq Scan for
        // this role" rather than "raise ef_search": ef_search on the shared SVC_ROLE
        // flips the EXPLAIN shape tests' planner choice, and even an isolated boosted-
        // ef_search role stayed flaky (~1 in 3) when run in the same mvn invocation
        // alongside other EXPLAIN-heavy classes, because pgvector's HNSW graph build
        // uses its OWN internal randomness independent of this suite's seeded SQL-level
        // random() filler vectors - no ef_search value makes that graph's shape (and
        // therefore recall for a borderline row) reproducible run to run. repo (used by
        // both annQuery_realCall_... and annQuery_mixedDimCollection_...) connects via
        // this role, forced away from any index scan (Seq Scan + exact Sort is
        // deterministic); explain() keeps using the unmodified SVC_ROLE/tenantScope
        // above for the actual HNSW-bind proof.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // KEPT RAW (all five statements): no typed jOOQ DDL form for a conditional
            // CREATE ROLE via DO $$, GRANT USAGE ON SCHEMA, a multi-privilege GRANT ON
            // TABLE, or ALTER ROLE ... SET -- none covered by a nexus_test bootstrap
            // function this round (candidates for one, per the task brief).
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                + SVC_ROLE_REALCALL + "') THEN CREATE ROLE " + SVC_ROLE_REALCALL
                + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE_REALCALL);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.taxonomy_centroids TO "
                + SVC_ROLE_REALCALL);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE_REALCALL + " SET enable_indexscan = off");
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE_REALCALL + " SET enable_bitmapscan = off");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var realCallCfg = new HikariConfig();
        realCallCfg.setJdbcUrl(pg.getJdbcUrl());
        realCallCfg.setUsername(SVC_ROLE_REALCALL);
        realCallCfg.setPassword(SVC_PASS);
        realCallCfg.setMaximumPoolSize(2);
        realCallCfg.setAutoCommit(true);
        realCallDs = new HikariDataSource(realCallCfg);
        repo = new TaxonomyCentroidRepository(new TenantScope(realCallDs));

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (realCallDs != null) realCallDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * Bulk-seeds all three dims directly into {@code nexus.taxonomy_centroids}
     * (superuser, bypasses RLS — same fast generate_series pattern as {@code
     * PgVectorRepositoryRawSqlPlanShapeTest}) so the per-dim HNSW indexes carry enough
     * rows for the planner's default cost model to prefer them, plus a MIXED-dim
     * collection ({@code COL_MIXED}) holding centroids at both 384 and 768 under
     * disjoint topic_ids — the fixture {@link #annQuery_mixedDimCollection_onlyMatchesQueriedDim}
     * needs.
     *
     * <p><strong>Filler vectors are randomized, not a single repeated literal</strong>
     * (found during Step G cluster-B triage, nexus-o8dil.16/.48 — same defect class as
     * {@code PgVectorRepositoryRawSqlPlanShapeTest}'s seeding, see that method's javadoc
     * for the full explanation). {@link #CENTROIDS_PER_DIM} byte-identical filler points
     * is an adversarial fixture for HNSW — one dense clique the graph search can get stuck
     * inside — and {@link #annQuery_realCall_findsNearestTopicByCorrectDimColumn_1024}
     * measurably missed the true (distance-0) nearest topic in favor of a filler
     * duplicate. {@code CROSS JOIN LATERAL} over a fresh {@code random()} draw forces
     * genuine per-row evaluation (a plain, non-lateral subquery is computed once and
     * reused for every outer row), giving each filler row an independent, well-spread
     * position.
     */
    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // Deterministic filler positions (CLAUDE.md: seeded randomness) -- a
            // client-side seeded Random replaces the retired setseed(0.42)/random()
            // SQL-side generation (nexus-cbo4a: the raw generate_series/LATERAL/
            // array_agg bulk insert this used to build has no typed jOOQ DSL form,
            // so the filler vectors are now generated in Java and bound directly).
            // The exact values don't matter, only that they are NOT all identical.
            Random rnd = new Random(42);

            for (int dim : new int[] {1024, 768, 384}) {
                String coll = dim == 1024 ? COL_1024 : dim == 768 ? COL_768 : COL_384;
                TableField<TaxonomyCentroidsRecord, Vector> embCol = embeddingField(dim);
                // Filler: CENTROIDS_PER_DIM independently-random vectors (never collides
                // with the single "nearest" row seeded below — see the method javadoc).
                var insert = ctx.insertInto(TAXONOMY_CENTROIDS,
                    TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                    TAXONOMY_CENTROIDS.TOPIC_ID, embCol,
                    TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT);
                for (int i = 1; i <= CENTROIDS_PER_DIM; i++) {
                    insert = insert.values(TENANT, coll, (long) i, fillerVector(rnd, dim), "filler", 1);
                }
                insert.execute();
                // The single nearest row: unit vector along the first axis.
                ctx.insertInto(TAXONOMY_CENTROIDS,
                        TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                        TAXONOMY_CENTROIDS.TOPIC_ID, embCol,
                        TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT)
                    .values(TENANT, coll, (long) (CENTROIDS_PER_DIM + dim), queryVec(dim), "nearest", 1)
                    .execute();
                PgContainerHelper.analyzeTable(su, TAXONOMY_CENTROIDS);
            }

            // Mixed-dim collection: topic 1 at 384-dim (near), topic 2 at 768-dim (near
            // in ITS own space) — disjoint topic_ids, same (tenant, collection), two
            // different populated embedding columns on two different physical rows.
            ctx.insertInto(TAXONOMY_CENTROIDS,
                    TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                    TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_384,
                    TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT)
                .values(TENANT, COL_MIXED, 1L, queryVec(384), "mixed-384", 1)
                .execute();
            ctx.insertInto(TAXONOMY_CENTROIDS,
                    TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                    TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_768,
                    TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT)
                .values(TENANT, COL_MIXED, 2L, queryVec(768), "mixed-768", 1)
                .execute();
            PgContainerHelper.analyzeTable(su, TAXONOMY_CENTROIDS);
        }
    }

    /** EXPLAIN a typed jOOQ table-function call over {@code taxonomy_ann_query_<dim>}
     *  (nexus-cbo4a batch 3) -- a single generated table-function call has a typed
     *  DSL form, so {@code ctx.explain(Query)} replaces the raw {@code "EXPLAIN " +
     *  sql} JDBC text this used to build by hand. */
    private String explain(Table<?> fn) {
        return tenantScope.withTenant(TENANT, ctx -> {
            dev.nexus.service.db.PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            return ctx.explain(ctx.selectFrom(fn)).plan();
        });
    }

    /** A length-{@code dim} typed pgvector value: first component 1.0, rest zero --
     *  the SAME shape as the retired {@code "[1" + ",0".repeat(dim - 1) + "]"} literal,
     *  for the {@code taxonomy_ann_query_<dim>.call(...)} conversions above. */
    private static Vector queryVec(int dim) {
        float[] v = new float[dim];
        v[0] = 1.0f;
        return Vector.of(v);
    }

    /** The generated {@code embedding_<dim>} column for {@code dim}, replacing
     *  {@link DimTables#embeddingColumn(int)}'s string column name with a typed field. */
    private static TableField<TaxonomyCentroidsRecord, Vector> embeddingField(int dim) {
        return switch (dim) {
            case 384 -> TAXONOMY_CENTROIDS.EMBEDDING_384;
            case 768 -> TAXONOMY_CENTROIDS.EMBEDDING_768;
            case 1024 -> TAXONOMY_CENTROIDS.EMBEDDING_1024;
            default -> throw new IllegalArgumentException("unsupported dim: " + dim);
        };
    }

    /** A length-{@code dim} vector of independently-random components in [-1, 1),
     *  replacing the retired {@code generate_series/LATERAL/array_agg(random())}
     *  SQL-side generation -- see {@link #seedFixtures}'s own javadoc. */
    private static Vector fillerVector(Random rnd, int dim) {
        float[] v = new float[dim];
        for (int i = 0; i < dim; i++) {
            v[i] = rnd.nextFloat() * 2 - 1;
        }
        return Vector.of(v);
    }

    // ════════════════════════════════════════════════════════════════════════
    // annQuery's ANN query -- nexus.taxonomy_ann_query_<dim> (vectors-013, nexus-zrcj7
    // step 4), retiring the former string-concatenated raw SQL onto an inlinable
    // schema function (embedding_<dim> <=> p_embedding, FROM nexus.taxonomy_centroids,
    // WHERE embedding_<dim> IS NOT NULL AND collection = / <> p_collection). EXPLAIN
    // over a direct call to the FUNCTION rather than the retired inline query text --
    // both the HNSW-index-binding proof (unchanged from before) AND a NEW inlining
    // proof (no Function Scan node — mirrors CombinedQueryParityTest's own inlining
    // group for the sibling combined-query functions).
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void annQuery_shape_usesFullHnswIndex_1024() {
        Table<?> fn = TAXONOMY_ANN_QUERY_1024.call(queryVec(1024), COL_1024, false, 10);
        String plan = explain(fn);
        assertThat(plan)
            .as("annQuery's distance projection (1024-dim) must bind to the FULL"
                + " idx_taxonomy_centroids_embedding_1024 HNSW index. Plan was:%n%s", plan)
            .contains("idx_taxonomy_centroids_embedding_1024");
        assertThat(plan)
            .as("must not degrade to a sequential scan of the unified (mixed-dim) table."
                + " Plan was:%n%s", plan)
            .doesNotContain("Seq Scan");
        assertThat(plan)
            .as("a Function Scan node means taxonomy_ann_query_1024 is not inlinable — "
                + "it must stay LANGUAGE sql/STABLE/SECURITY INVOKER with no SET clause. "
                + "Plan was:%n%s", plan)
            .doesNotContain("Function Scan");
    }

    @Test
    void annQuery_shape_usesFullHnswIndex_768() {
        Table<?> fn = TAXONOMY_ANN_QUERY_768.call(queryVec(768), COL_768, false, 10);
        String plan = explain(fn);
        assertThat(plan)
            .as("annQuery's distance projection (768-dim) must bind to the FULL"
                + " idx_taxonomy_centroids_embedding_768 HNSW index. Plan was:%n%s", plan)
            .contains("idx_taxonomy_centroids_embedding_768");
        assertThat(plan).as("no Seq Scan. Plan was:%n%s", plan).doesNotContain("Seq Scan");
        assertThat(plan)
            .as("no Function Scan (inlining proof). Plan was:%n%s", plan)
            .doesNotContain("Function Scan");
    }

    @Test
    void annQuery_shape_usesFullHnswIndex_384() {
        Table<?> fn = TAXONOMY_ANN_QUERY_384.call(queryVec(384), COL_384, false, 10);
        String plan = explain(fn);
        assertThat(plan)
            .as("annQuery's distance projection (384-dim) must bind to the FULL"
                + " idx_taxonomy_centroids_embedding_384 HNSW index. Plan was:%n%s", plan)
            .contains("idx_taxonomy_centroids_embedding_384");
        assertThat(plan).as("no Seq Scan. Plan was:%n%s", plan).doesNotContain("Seq Scan");
        assertThat(plan)
            .as("no Function Scan (inlining proof). Plan was:%n%s", plan)
            .doesNotContain("Function Scan");
    }

    // ════════════════════════════════════════════════════════════════════════
    // Behavioral companions: the REAL annQuery() call, end to end.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void annQuery_realCall_findsNearestTopicByCorrectDimColumn_1024() {
        float[] q = unit(1024, 1.0f);
        List<AnnHit> hits = repo.annQuery(TENANT, q, COL_1024, false, 1);
        assertThat(hits).singleElement().satisfies(h -> {
            assertThat(h.topicId()).isEqualTo(CENTROIDS_PER_DIM + 1024L);
            assertThat(h.similarity()).isCloseTo(1.0, within(1e-5));
        });
    }

    /**
     * The regression proof for the {@code embedding_<dim> IS NOT NULL} fix: a collection
     * whose centroids straddle two dims (topic 1 at 384, topic 2 at 768 — same physical
     * table, same {@code collection}, disjoint {@code topic_id}) must have each dim's
     * query see ONLY its own dim's row. Before the fix, ordering by a bare {@code
     * embedding <=> ?} (or, post-repoint without the guard, an unfiltered {@code
     * embedding_<dim> <=> ?}) risked ranking against — or silently including — the
     * foreign-dim row (a NULL-distance row Postgres sorts last under default NULLS LAST,
     * masking the defect at small scale but not the underlying wrong-population query
     * shape this test pins).
     */
    @Test
    void annQuery_mixedDimCollection_onlyMatchesQueriedDim() {
        List<AnnHit> at384 = repo.annQuery(TENANT, unit(384, 1.0f), COL_MIXED, false, 10);
        assertThat(at384).as("384-dim query in a mixed-dim collection sees only topic 1")
            .extracting(AnnHit::topicId).containsExactly(1L);

        List<AnnHit> at768 = repo.annQuery(TENANT, unit(768, 1.0f), COL_MIXED, false, 10);
        assertThat(at768).as("768-dim query in a mixed-dim collection sees only topic 2")
            .extracting(AnnHit::topicId).containsExactly(2L);
    }

    @Test
    void seededCardinalityIsReal() throws Exception {
        try (Connection su = pg.createConnection("")) {
            int count = DSL.using(su, SQLDialect.POSTGRES)
                .fetchCount(TAXONOMY_CENTROIDS, TAXONOMY_CENTROIDS.TENANT_ID.eq(TENANT));
            assertThat((long) count)
                .as("the plan-shape claim is only meaningful at cardinality")
                .isEqualTo(3L * (CENTROIDS_PER_DIM + 1) + 2);
        }
    }

    /** Unit vector (x, 0, 0, ..., 0) of length dim. */
    private static float[] unit(int dim, float x) {
        float[] v = new float[dim];
        v[0] = x;
        return v;
    }
}
