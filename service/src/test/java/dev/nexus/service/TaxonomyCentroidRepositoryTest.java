package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.AnnHit;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.CentroidRecord;
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
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

/**
 * RDR-156 bead nexus-t1hnc.2 — TaxonomyCentroidRepository pgvector contract suite.
 *
 * <p>The centroid store mirrors the {@code taxonomy__centroids} ChromaDB collection the
 * oracle ({@code catalog_taxonomy.py}) assumed: cosine ANN over precomputed centroid
 * vectors, returning {@code topic_id + similarity = 1 - cosine_distance}. It is the
 * service-backed replacement for the {@code chroma_client} in service-mode taxonomy
 * compute (assign_single / compute_assignments / discover_topics centroid upsert).
 *
 * <p>Contract pinned here:
 * <ul>
 *   <li><strong>Embedding-length routing.</strong> upsert/annQuery dispatch to
 *       {@code taxonomy_centroids_384/768/1024} by the vector's length (the vector is
 *       ground truth); taxonomy collection names are NOT four-segment conformant
 *       (RDR-075 uses {@code <content_type>__<owner>} two-segment names), so
 *       {@code dimForCollection} cannot be the router.
 *   <li><strong>Cosine similarity.</strong> annQuery returns {@code 1 - (embedding <=> q)}.
 *   <li><strong>cross_collection filter.</strong> {@code WHERE collection = ?} (false) vs
 *       {@code WHERE collection <> ?} (true) — parity with assign_single's where-filter.
 *   <li><strong>Narrow-collection exact recall (RDR-156).</strong> A collection with N
 *       centroids returns exactly {@code min(N, nResults)} hits — never silently fewer
 *       (the {@code hnsw.iterative_scan='relaxed_order'} guard).
 *   <li><strong>Tenant RLS.</strong> every op takes a tenant; a foreign tenant sees/affects 0.
 *   <li><strong>Collection-keyed ops span all three tables.</strong> count/getByCollection/
 *       delete/purge operate without a vector, so they union/delete across all per-dim
 *       tables (a deployment is single-dim per RDR-075/077; only one has rows).
 * </ul>
 *
 * <p>Plain-LOGIN NOSUPERUSER NOBYPASSRLS service role (nexus-5j7pb class) so the RLS
 * assertions are non-vacuous. Hermetic Testcontainers pgvector, PER_CLASS lifecycle.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyCentroidRepositoryTest {

    private static final String SVC_ROLE = "svc_centroids_test";
    private static final String SVC_PASS = "svc_centroids_test_pass";

    private static final String TENANT_A = "tenant-a";
    private static final String TENANT_B = "tenant-b";

    // Taxonomy collection names are deliberately NON-conformant (two-segment), proving
    // the repo routes by embedding length, not by parsing a model segment.
    private static final String COL_A = "knowledge__alpha";
    private static final String COL_B = "docs__beta";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;
    TaxonomyCentroidRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
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
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.taxonomy_centroids_" + dim
                    + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyCentroidRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    /** Unit vector ({@code x}, {@code y}, 0, ..., 0) — exact, assertable cosine distances. */
    private static float[] unit(int dim, float x, float y) {
        float[] v = new float[dim];
        v[0] = x;
        v[1] = y;
        return v;
    }

    // ── upsert + count + RLS ────────────────────────────────────────────────────

    @Test
    void upsert_landsInPerDimTableByEmbeddingLength_andRlsScopes() throws Exception {
        // Unique collection: PER_CLASS shares the DB, so this name is owned by this test
        // (cross_collection scans pull every foreign collection — see the cross test).
        String col = "knowledge__landrls";
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord(col, 10L, unit(384, 1.0f, 0.0f), "alpha", 5),
            new CentroidRecord(col, 20L, unit(384, 0.6f, 0.8f), "beta",  3),
            new CentroidRecord(col, 30L, unit(384, 0.0f, 1.0f), "gamma", 7)));

        // 384-length vectors land ONLY in taxonomy_centroids_384.
        assertThat(superuserCount(384, col)).as("all three in centroids_384").isEqualTo(3L);
        assertThat(superuserCount(768, col)).as("none in centroids_768").isEqualTo(0L);
        assertThat(superuserCount(1024, col)).as("none in centroids_1024").isEqualTo(0L);

        assertThat(repo.count(TENANT_A, col)).as("tenant-A sees 3").isEqualTo(3);
        assertThat(repo.count(TENANT_B, col)).as("tenant-B sees 0 under RLS").isEqualTo(0);
    }

    @Test
    void upsert_768vector_landsInCentroids768Only() throws Exception {
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__sevensix", 1L, unit(768, 1.0f, 0.0f), "x", 1)));
        assertThat(superuserCount(768, "knowledge__sevensix")).isEqualTo(1L);
        assertThat(superuserCount(384, "knowledge__sevensix")).isEqualTo(0L);
        assertThat(superuserCount(1024, "knowledge__sevensix")).isEqualTo(0L);
    }

    @Test
    void upsert_isUpsertNotInsert_updatesInPlace() {
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__upd", 1L, unit(384, 1.0f, 0.0f), "old", 1)));
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__upd", 1L, unit(384, 0.0f, 1.0f), "new", 9)));

        assertThat(repo.count(TENANT_A, "knowledge__upd")).isEqualTo(1);
        List<CentroidRecord> rows = repo.getByCollection(TENANT_A, "knowledge__upd");
        assertThat(rows).singleElement().satisfies(r -> {
            assertThat(r.label()).isEqualTo("new");
            assertThat(r.docCount()).isEqualTo(9);
        });
    }

    @Test
    void upsert_emptyIsNoOp_unknownDimFailsLoud() {
        repo.upsertCentroids(TENANT_A, List.of());  // no throw, no rows

        assertThatThrownBy(() -> repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__baddim", 1L, new float[512], "x", 1))))
            .as("a 512-dim centroid has no per-dim table — fail loud, never silent")
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ── annQuery ────────────────────────────────────────────────────────────────

    @Test
    void annQuery_returnsNearestTopicWithCosineSimilarity() {
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__ann", 100L, unit(384, 1.0f, 0.0f), "near", 1),
            new CentroidRecord("knowledge__ann", 200L, unit(384, 0.6f, 0.8f), "mid",  1),
            new CentroidRecord("knowledge__ann", 300L, unit(384, 0.0f, 1.0f), "far",  1)));

        List<AnnHit> hits = repo.annQuery(
            TENANT_A, unit(384, 1.0f, 0.0f), "knowledge__ann", false, 1);

        assertThat(hits).singleElement().satisfies(h -> {
            assertThat(h.topicId()).as("nearest centroid is topic 100").isEqualTo(100L);
            assertThat(h.similarity()).as("similarity = 1 - distance = 1.0").isCloseTo(1.0, within(1e-5));
        });
    }

    @Test
    void annQuery_narrowCollection_returnsExactlyN_noSilentUnderReturn() {
        // RDR-156: filtered HNSW silently under-returns at hnsw.max_scan_tuples without
        // iterative_scan=relaxed_order. A 3-centroid collection MUST return exactly 3.
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__narrow", 1L, unit(384, 1.0f, 0.0f), "a", 1),
            new CentroidRecord("knowledge__narrow", 2L, unit(384, 0.6f, 0.8f), "b", 1),
            new CentroidRecord("knowledge__narrow", 3L, unit(384, 0.0f, 1.0f), "c", 1)));

        List<AnnHit> hits = repo.annQuery(
            TENANT_A, unit(384, 1.0f, 0.0f), "knowledge__narrow", false, 10);
        assertThat(hits).as("exact recall: all 3 narrow-collection centroids returned").hasSize(3);
        // Distance-ascending: 100% match first, orthogonal last.
        assertThat(hits.get(0).topicId()).isEqualTo(1L);
        assertThat(hits.get(0).similarity()).isCloseTo(1.0, within(1e-5));
        assertThat(hits.get(2).topicId()).isEqualTo(3L);
        assertThat(hits.get(2).similarity()).isCloseTo(0.0, within(1e-5));
    }

    @Test
    void annQuery_crossCollection_excludesSameCollection() {
        // Dedicated tenant: cross_collection returns EVERY foreign collection (oracle
        // `where collection != name`), so RLS-isolate from sibling tests' centroids.
        String tenant = "tenant-xcoll";
        repo.upsertCentroids(tenant, List.of(
            new CentroidRecord(COL_A, 11L, unit(384, 1.0f, 0.0f), "a-near", 1)));
        repo.upsertCentroids(tenant, List.of(
            new CentroidRecord(COL_B, 22L, unit(384, 1.0f, 0.0f), "b-near", 1)));

        // cross_collection=true on COL_A must skip COL_A's own centroids, return COL_B's.
        List<AnnHit> hits = repo.annQuery(
            tenant, unit(384, 1.0f, 0.0f), COL_A, true, 5);
        assertThat(hits).as("cross-collection excludes the source collection")
            .extracting(AnnHit::topicId).containsExactly(22L);
    }

    // ── dimensionProbe / getByCollection / delete / purge ───────────────────────

    @Test
    void dimensionProbe_returnsDimWithRows_minusOneWhenEmpty() {
        assertThat(repo.dimensionProbe(TENANT_B)).as("tenant-B has no centroids").isEqualTo(-1);

        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__probe", 1L, unit(384, 1.0f, 0.0f), "x", 1)));
        // tenant-A already has 384-dim centroids from other tests too; probe is 384.
        assertThat(repo.dimensionProbe(TENANT_A)).isEqualTo(384);
    }

    @Test
    void getByCollection_roundTripsEmbeddingExactly() {
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__rt", 7L, unit(384, 0.6f, 0.8f), "rt", 4)));

        List<CentroidRecord> rows = repo.getByCollection(TENANT_A, "knowledge__rt");
        assertThat(rows).singleElement().satisfies(r -> {
            assertThat(r.topicId()).isEqualTo(7L);
            assertThat(r.label()).isEqualTo("rt");
            assertThat(r.docCount()).isEqualTo(4);
            assertThat(r.collection()).isEqualTo("knowledge__rt");
            assertThat(r.embedding().length).isEqualTo(384);
            assertThat(r.embedding()[0]).isCloseTo(0.6f, within(1e-5f));
            assertThat(r.embedding()[1]).isCloseTo(0.8f, within(1e-5f));
        });
    }

    @Test
    void deleteByIds_thenPurge() {
        repo.upsertCentroids(TENANT_A, List.of(
            new CentroidRecord("knowledge__del", 1L, unit(384, 1.0f, 0.0f), "a", 1),
            new CentroidRecord("knowledge__del", 2L, unit(384, 0.6f, 0.8f), "b", 1),
            new CentroidRecord("knowledge__del", 3L, unit(384, 0.0f, 1.0f), "c", 1)));

        assertThat(repo.deleteByIds(TENANT_A, "knowledge__del", List.of(2L)))
            .as("one row deleted").isEqualTo(1);
        assertThat(repo.count(TENANT_A, "knowledge__del")).isEqualTo(2);
        assertThat(repo.getByCollection(TENANT_A, "knowledge__del"))
            .extracting(CentroidRecord::topicId).containsExactlyInAnyOrder(1L, 3L);

        assertThat(repo.purgeByCollection(TENANT_A, "knowledge__del"))
            .as("purge removes the remaining two").isEqualTo(2);
        assertThat(repo.count(TENANT_A, "knowledge__del")).isZero();
    }

    // ── Helpers ─────────────────────────────────────────────────────────────────

    /** Superuser row count for one collection in a centroid table (bypasses RLS). */
    private long superuserCount(int dim, String collection) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.taxonomy_centroids_" + dim + " WHERE collection = ?")) {
            ps.setString(1, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }
}
