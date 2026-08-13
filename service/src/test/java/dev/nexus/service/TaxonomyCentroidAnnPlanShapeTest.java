// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.AnnHit;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

/**
 * RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-jv3ue item 5) — EXPLAIN-based
 * plan-shape proof for {@link TaxonomyCentroidRepository#annQuery}'s raw-SQL ANN query,
 * mirroring {@code PgVectorRepositoryRawSqlPlanShapeTest}'s methodology for the sibling
 * {@code nexus.chunks} unification.
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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE
                + "') THEN CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
                + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') "
                + "THEN CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                          new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.taxonomy_centroids TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                + SVC_ROLE_REALCALL + "') THEN CREATE ROLE " + SVC_ROLE_REALCALL
                + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE_REALCALL);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.taxonomy_centroids TO "
                + SVC_ROLE_REALCALL);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE_REALCALL + " SET search_path TO nexus, public");
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
            var st = su.createStatement();
            // Deterministic filler positions (CLAUDE.md: seeded randomness) — the exact
            // values don't matter, only that they are NOT all identical.
            st.execute("SELECT setseed(0.42)");

            for (int dim : new int[] {1024, 768, 384}) {
                String coll = dim == 1024 ? COL_1024 : dim == 768 ? COL_768 : COL_384;
                String embCol = DimTables.embeddingColumn(dim);
                // Filler: CENTROIDS_PER_DIM independently-random vectors (never collides
                // with the single "nearest" row seeded below — see the method javadoc).
                st.execute(
                    "INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, "
                    + embCol + ", label, doc_count) "
                    + "SELECT '" + TENANT + "', '" + coll + "', i, v.vec, 'filler', 1 "
                    + "FROM generate_series(1, " + CENTROIDS_PER_DIM + ") i "
                    + "CROSS JOIN LATERAL (SELECT (array_agg(random() * 2 - 1))::vector AS vec"
                    + "                    FROM generate_series(1, " + dim + ")) v");
                // The single nearest row: unit vector along the first axis.
                st.execute(
                    "INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, "
                    + embCol + ", label, doc_count) VALUES ('"
                    + TENANT + "', '" + coll + "', " + (CENTROIDS_PER_DIM + dim) + ", "
                    + "('[1' || repeat(',0', " + (dim - 1) + ") || ']')::vector, 'nearest', 1)");
                st.execute("ANALYZE nexus.taxonomy_centroids");
            }

            // Mixed-dim collection: topic 1 at 384-dim (near), topic 2 at 768-dim (near
            // in ITS own space) — disjoint topic_ids, same (tenant, collection), two
            // different populated embedding columns on two different physical rows.
            st.execute(
                "INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, embedding_384, label, doc_count) "
                + "VALUES ('" + TENANT + "', '" + COL_MIXED + "', 1, "
                + "('[1' || repeat(',0', 383) || ']')::vector, 'mixed-384', 1)");
            st.execute(
                "INSERT INTO nexus.taxonomy_centroids (tenant_id, collection, topic_id, embedding_768, label, doc_count) "
                + "VALUES ('" + TENANT + "', '" + COL_MIXED + "', 2, "
                + "('[1' || repeat(',0', 767) || ']')::vector, 'mixed-768', 1)");
            st.execute("ANALYZE nexus.taxonomy_centroids");
        }
    }

    private String explain(String sql) {
        return tenantScope.withTenant(TENANT, ctx -> {
            dev.nexus.service.db.PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            StringBuilder sb = new StringBuilder();
            for (var r : ctx.resultQuery("EXPLAIN " + sql).fetch()) {
                sb.append(r.get(0, String.class)).append('\n');
            }
            return sb.toString();
        });
    }

    // ════════════════════════════════════════════════════════════════════════
    // annQuery's raw ANN query (embedding_<dim> <=> ?::vector, FROM
    // nexus.taxonomy_centroids, WHERE embedding_<dim> IS NOT NULL AND collection = ?)
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void annQuery_shape_usesFullHnswIndex_1024() {
        String vec = "[1" + ",0".repeat(1023) + "]";
        String sql =
            "SELECT topic_id, (embedding_1024 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.taxonomy_centroids"
            + " WHERE embedding_1024 IS NOT NULL AND collection = '" + COL_1024 + "'"
            + " ORDER BY distance ASC, topic_id ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("annQuery's distance projection (1024-dim) must bind to the FULL"
                + " idx_taxonomy_centroids_embedding_1024 HNSW index. Plan was:%n%s", plan)
            .contains("idx_taxonomy_centroids_embedding_1024");
        assertThat(plan)
            .as("must not degrade to a sequential scan of the unified (mixed-dim) table."
                + " Plan was:%n%s", plan)
            .doesNotContain("Seq Scan");
    }

    @Test
    void annQuery_shape_usesFullHnswIndex_768() {
        String vec = "[1" + ",0".repeat(767) + "]";
        String sql =
            "SELECT topic_id, (embedding_768 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.taxonomy_centroids"
            + " WHERE embedding_768 IS NOT NULL AND collection = '" + COL_768 + "'"
            + " ORDER BY distance ASC, topic_id ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("annQuery's distance projection (768-dim) must bind to the FULL"
                + " idx_taxonomy_centroids_embedding_768 HNSW index. Plan was:%n%s", plan)
            .contains("idx_taxonomy_centroids_embedding_768");
        assertThat(plan).as("no Seq Scan. Plan was:%n%s", plan).doesNotContain("Seq Scan");
    }

    @Test
    void annQuery_shape_usesFullHnswIndex_384() {
        String vec = "[1" + ",0".repeat(383) + "]";
        String sql =
            "SELECT topic_id, (embedding_384 <=> '" + vec + "'::vector) AS distance"
            + " FROM nexus.taxonomy_centroids"
            + " WHERE embedding_384 IS NOT NULL AND collection = '" + COL_384 + "'"
            + " ORDER BY distance ASC, topic_id ASC LIMIT 10";
        String plan = explain(sql);
        assertThat(plan)
            .as("annQuery's distance projection (384-dim) must bind to the FULL"
                + " idx_taxonomy_centroids_embedding_384 HNSW index. Plan was:%n%s", plan)
            .contains("idx_taxonomy_centroids_embedding_384");
        assertThat(plan).as("no Seq Scan. Plan was:%n%s", plan).doesNotContain("Seq Scan");
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
        try (Connection su = pg.createConnection("");
             ResultSet rs = su.createStatement().executeQuery(
                "SELECT count(*) FROM nexus.taxonomy_centroids WHERE tenant_id = '" + TENANT + "'")) {
            rs.next();
            assertThat(rs.getLong(1))
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
