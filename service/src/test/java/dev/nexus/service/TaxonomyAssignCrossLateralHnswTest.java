// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.Map;
import java.util.Random;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_ANN_QUERY_1024;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

/**
 * Bead nexus-f3yxx (indexing-brittleness P0.2) — proof that {@code
 * assign_from_chashes_<dim>}'s cross_collection branch, rewritten as a
 * per-chunk {@code LATERAL} nearest-centroid over the per-dim HNSW index
 * (taxonomy-018-assign-cross-lateral-hnsw.xml), (a) computes the SAME answer
 * the retired exact join computed — including with the source collection
 * carrying a dense set of its OWN centroids (&gt;= 40, the benchmark's own
 * no-row-hazard shape, T2 nexus/p02-assign-lateral-benchmark-2026-09-25
 * [26864]) that the rewritten `WHERE ct.collection &lt;&gt; p_collection`
 * predicate must still correctly exclude — and (b) actually binds to the
 * per-dim HNSW index rather than a sequential scan, at the cardinality
 * where the planner's default cost model naturally prefers it (mirroring
 * {@link TaxonomyCentroidAnnPlanShapeTest}'s own {@code CENTROIDS_PER_DIM}
 * methodology).
 *
 * <p><strong>Two roles, two purposes — same split {@link TaxonomyCentroidAnnPlanShapeTest}
 * uses and explains in its own {@code startAll()} javadoc.</strong> The
 * recall proof (a) runs through a DEDICATED role with
 * {@code enable_indexscan}/{@code enable_bitmapscan} forced off, so the
 * cross pass's LATERAL subquery always resolves via an exact Seq Scan +
 * Sort regardless of pgvector's own internal HNSW graph-build randomness —
 * proving the REWRITE'S SQL LOGIC (the LATERAL shape, the tie-break, the
 * `&lt;&gt;` collection filter, the upsert semantics) matches the retired
 * exact join, independent of ANN approximation quality. (Measured directly
 * in this suite's development: at real HNSW cardinality, even
 * {@code ef_search=400} with {@code strict_order} missed a distance-0
 * global-best outlier entirely, because the true centroid was inserted
 * into a SPARSE 60-node graph and never got reconnected into the
 * subsequently-added 9000-row filler population — a graph-connectivity
 * artifact of this synthetic fixture's insertion order, not a defect in
 * the rewrite; recall AT PRODUCTION SCALE is what the benchmark already
 * measured, "equal to exact at ef 400", and is not this suite's job to
 * reproduce.) The plan-shape proof (b) runs through the UNMODIFIED role
 * (HNSW enabled) and asserts only the EXPLAIN text, never a real answer.
 *
 * <p>The plan-shape proof EXPLAINs a call to {@code taxonomy_ann_query_1024}
 * (vectors-013) rather than a hand-mirrored raw query — that function's
 * {@code (embedding, collection, cross_collection=true, n=1)} shape is
 * EXACTLY the per-chunk nearest-centroid computation the new LATERAL
 * subquery performs inline inside {@code assign_from_chashes_1024}, which is
 * itself LANGUAGE plpgsql and therefore opaque to a direct EXPLAIN of the
 * outer call (Postgres never inlines a plpgsql function body) — same
 * methodology every other plan-shape suite in this package uses ("EXPLAINs
 * a call to that SAME function rather than a hand-mirrored query",
 * {@code PgVectorRepositoryRawSqlPlanShapeTest}). The
 * {@code hnsw.iterative_scan=strict_order} / {@code hnsw.ef_search=400}
 * session settings this test sets by hand via {@link PgSession#setLocal}
 * mirror EXACTLY the function-level {@code SET} clauses
 * {@code assign_from_chashes_1024} itself now carries.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyAssignCrossLateralHnswTest {

    private static final String SVC_ROLE = "svc_afc_lateral_test";
    /** Dedicated exact-scan role for the recall (real-call) proof, forced away from
     *  any index scan -- same fix as {@link TaxonomyCentroidAnnPlanShapeTest}'s own
     *  {@code SVC_ROLE_REALCALL}; see that class's {@code startAll()} javadoc for the
     *  full rationale (HNSW's own internal graph-build randomness makes ANN recall
     *  for a borderline/sparse-graph row non-reproducible run to run). */
    private static final String SVC_ROLE_REALCALL = "svc_afc_lateral_realcall";
    private static final String SVC_PASS = "svc_afc_lateral_pass";
    private static final String TENANT = "afc-lateral-tenant";

    /** The collection under test: dense with its OWN centroids (the no-row hazard). */
    private static final String COL_DENSE = "code__afc_lateral_dense__voyage-code-3__v1";
    /** A single foreign collection carrying the ONE true nearest centroid. */
    private static final String COL_TRUE = "code__afc_lateral_true__voyage-code-3__v1";
    /** A large, unrelated foreign collection of filler centroids, purely to build up
     *  total table cardinality so the planner's default cost model prefers the HNSW
     *  index over Seq Scan + Sort for the PLAN-SHAPE proof (same role
     *  {@code CENTROIDS_PER_DIM} plays in {@link TaxonomyCentroidAnnPlanShapeTest}). */
    private static final String COL_FILLER = "knowledge__afc_lateral_filler";

    private static final int DIM = 1024;
    /** >= 40: the benchmark's own no-row-hazard threshold at default ef_search. */
    private static final int OWN_CENTROID_COUNT = 60;
    /** Total table cardinality (this + OWN_CENTROID_COUNT + 1) must clear roughly
     *  {@link TaxonomyCentroidAnnPlanShapeTest}'s own ~9000-row threshold for the
     *  planner's default cost model to prefer HNSW over Seq Scan + Sort -- unlike
     *  that suite's per-dim `collection = p_collection` filter (highly selective
     *  against ITS ~9005-row whole table), this test's `collection &lt;&gt; p_collection`
     *  filter only excludes COL_DENSE's 60 rows, so Seq Scan cost is driven by the
     *  WHOLE table's row count, not just the matched rows -- a smaller filler count
     *  (measured: 3000, total 3061) left Seq Scan cheaper in absolute terms. */
    private static final int FILLER_CENTROID_COUNT = 9_000;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TenantScope realCallScope;
    HikariDataSource svcDs;
    HikariDataSource realCallDs;
    TaxonomyRepository repo;

    private String c1;
    private long tTrue;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.assign_from_chashes_" + DIM
                + "(text, text[], boolean) TO " + SVC_ROLE);
        }
        // Second role, dedicated to the real-call recall proof -- see class javadoc.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                + SVC_ROLE_REALCALL + "') THEN CREATE ROLE " + SVC_ROLE_REALCALL
                + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE_REALCALL);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO "
                + SVC_ROLE_REALCALL);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE_REALCALL);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.assign_from_chashes_" + DIM
                + "(text, text[], boolean) TO " + SVC_ROLE_REALCALL);
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
        realCallCfg.setMaximumPoolSize(5);
        realCallCfg.setAutoCommit(true);
        realCallDs = new HikariDataSource(realCallCfg);
        realCallScope = new TenantScope(realCallDs);
        repo = new TaxonomyRepository(realCallScope);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (realCallDs != null) realCallDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * Unit vector along axis 0 for {@code c1}'s chunk embedding, and the SAME unit
     * vector for {@code COL_TRUE}'s single centroid — hand-computable cosine
     * similarity of EXACTLY 1.0, the parity idiom {@link TaxonomyAssignFromChashesRepositoryTest}
     * and {@link TaxonomyCentroidAnnPlanShapeTest} both use. Every filler and every
     * OWN-collection decoy centroid is an independently-random point in
     * {@code [-1, 1)^DIM} (seeded, {@code CLAUDE.md}: deterministic randomness).
     */
    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext bootstrapCtx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(bootstrapCtx, TENANT, COL_DENSE);
            PgContainerHelper.insertCollection(bootstrapCtx, TENANT, COL_TRUE);
            PgContainerHelper.insertCollection(bootstrapCtx, TENANT, COL_FILLER);
        }

        // The ONE true topic -- real topics.id row, referenced by the eventual
        // topic_assignments FK. The OWN-collection decoy topics ALSO need real
        // topics.id rows (fk_topic_assignments_topic_tenant, taxonomy-014):
        // taxonomy_centroids.topic_id carries no FK of its own, but the own pass's
        // argmax WILL pick one of these 60 decoys (whichever is least-far from the
        // unit vector) and persist it into topic_assignments, which does enforce it.
        tTrue = repo.insertTopic(TENANT, "afc-lateral-true-topic", null, COL_TRUE, 0,
            "2026-01-01T00:00:00Z", null);
        long[] ownTopicIds = new long[OWN_CENTROID_COUNT];
        for (int i = 0; i < OWN_CENTROID_COUNT; i++) {
            ownTopicIds[i] = repo.insertTopic(TENANT, "afc-lateral-own-decoy-" + i, null,
                COL_DENSE, 0, "2026-01-01T00:00:00Z", null);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Random rnd = new Random(2026_09_25L);

            // The chunk under test: one chash in COL_DENSE, unit vector along axis 0.
            c1 = hexChash("afc-lateral-c1");
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024)"
                    + " VALUES (?, ?, decode(?, 'hex'), ?, ?::nexus.vector)")) {
                ps.setString(1, TENANT);
                ps.setString(2, COL_DENSE);
                ps.setString(3, c1);
                ps.setString(4, "lateral test chunk");
                ps.setString(5, vectorLiteral(unitVector(DIM)));
                ps.executeUpdate();
            }

            // >= 40 OWN centroids in COL_DENSE itself (the no-row hazard fixture) --
            // random decoys, each backed by a real topics row (ownTopicIds above).
            var insertOwn = ctx.insertInto(TAXONOMY_CENTROIDS,
                TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_1024,
                TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT);
            for (long ownTopicId : ownTopicIds) {
                insertOwn = insertOwn.values(TENANT, COL_DENSE, ownTopicId,
                    fillerVector(rnd, DIM), "own-decoy", 1);
            }
            insertOwn.execute();

            // The true foreign centroid -- exact unit-vector match, real topics.id row.
            ctx.insertInto(TAXONOMY_CENTROIDS,
                    TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                    TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_1024,
                    TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT)
                .values(TENANT, COL_TRUE, tTrue, Vector.of(unitVector(DIM)),
                    "lateral-true-centroid", 1)
                .execute();

            // A large unrelated foreign collection of filler centroids (topic_id
            // values with no topics row -- never a winning argmax under the FORCED
            // EXACT scan the recall proof runs through, so the FK is never exercised
            // for these), so the planner's default cost model prefers the HNSW index
            // for the SEPARATE plan-shape proof (matching TaxonomyCentroidAnnPlanShapeTest's
            // CENTROIDS_PER_DIM density).
            var insertFiller = ctx.insertInto(TAXONOMY_CENTROIDS,
                TAXONOMY_CENTROIDS.TENANT_ID, TAXONOMY_CENTROIDS.COLLECTION,
                TAXONOMY_CENTROIDS.TOPIC_ID, TAXONOMY_CENTROIDS.EMBEDDING_1024,
                TAXONOMY_CENTROIDS.LABEL, TAXONOMY_CENTROIDS.DOC_COUNT);
            for (int i = 1; i <= FILLER_CENTROID_COUNT; i++) {
                insertFiller = insertFiller.values(TENANT, COL_FILLER, (long) (1000 + i),
                    fillerVector(rnd, DIM), "filler", 1);
            }
            insertFiller.execute();

            PgContainerHelper.analyzeTable(su, TAXONOMY_CENTROIDS);
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // (a) Recall: the LATERAL cross pass's SQL logic matches the exact join,
    // including correctly excluding a dense set of the source's OWN centroids
    // (>= 40, the benchmark's own no-row hazard shape). Runs through the
    // forced-exact-scan role -- see class javadoc for why.
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void crossLateral_findsHandComputedNearestTopic_withDenseOwnCentroids() throws Exception {
        Map<String, Object> out = repo.assignFromChashes(TENANT, COL_DENSE, List.of(c1), true);
        assertThat(out.get("cross_assigned"))
            .as("the cross pass must return exactly one row for c1").isEqualTo(1);

        // repo.assignFromChashes ALWAYS runs the own pass too (unconditionally),
        // so c1 carries BOTH an own-collection row (assigned_by='centroid', one of
        // the 60 random own decoys -- unrelated to this assertion) and the
        // cross-collection ('projection') row this test is actually about.
        List<Map<String, Object>> details = repo.getAssignmentDetails(TENANT, List.of(c1));
        assertThat(details).as("own pass + cross pass, one row each").hasSize(2);
        assertThat(details).anySatisfy(r -> {
            assertThat(r.get("assigned_by")).isEqualTo("projection");
            assertThat(r.get("topic_id")).as("must match the hand-computed nearest"
                + " topic (cosine similarity 1.0 to the true centroid), not one of"
                + " the 60 random own-collection decoys or 9000 unrelated filler"
                + " centroids").isEqualTo(tTrue);
            assertThat((Double) r.get("similarity")).as("unit vector <-> unit"
                + " vector cosine similarity is exactly 1.0").isCloseTo(1.0, within(1e-3));
            assertThat(r.get("source_collection")).isEqualTo(COL_DENSE);
        });
    }

    // ════════════════════════════════════════════════════════════════════════
    // (b) Plan shape: the cross pass's nearest-centroid computation binds to
    // idx_taxonomy_centroids_embedding_1024 (HNSW), not a sequential scan.
    // Runs through the UNMODIFIED role (HNSW enabled) -- EXPLAIN text only,
    // never a real answer (see class javadoc).
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void crossLateral_planShape_bindsHnswIndex_notSeqScan() {
        Table<?> fn = TAXONOMY_ANN_QUERY_1024.call(Vector.of(unitVector(DIM)), COL_DENSE, true, 1);
        String plan = tenantScope.withTenant(TENANT, ctx -> {
            // Mirrors EXACTLY the function-level SET clauses
            // assign_from_chashes_1024 itself now carries (taxonomy-018-1).
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "strict_order");
            PgSession.setLocal(ctx, "hnsw.ef_search", "400");
            return ctx.explain(ctx.selectFrom(fn)).plan();
        });
        assertThat(plan)
            .as("the cross-collection nearest-centroid computation (same shape as"
                + " assign_from_chashes_1024's rewritten LATERAL subquery) must bind"
                + " to the FULL idx_taxonomy_centroids_embedding_1024 HNSW index."
                + " Plan was:%n%s", plan)
            .contains("idx_taxonomy_centroids_embedding_1024");
        assertThat(plan)
            .as("must not degrade to a sequential scan at this cardinality (60 own"
                + " decoys + 9000 filler + 1 true centroid). Plan was:%n%s", plan)
            .doesNotContain("Seq Scan");
    }

    // ── helpers ─────────────────────────────────────────────────────────────────

    private static float[] unitVector(int dim) {
        float[] v = new float[dim];
        v[0] = 1.0f;
        return v;
    }

    /** Vector-typed filler for the typed jOOQ bulk inserts below (own decoys + filler). */
    private static Vector fillerVector(Random rnd, int dim) {
        float[] v = new float[dim];
        for (int i = 0; i < dim; i++) {
            v[i] = rnd.nextFloat() * 2 - 1;
        }
        return Vector.of(v);
    }

    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }
}
