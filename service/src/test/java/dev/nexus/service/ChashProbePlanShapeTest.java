package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.TenantScope;
import org.testcontainers.containers.PostgreSQLContainer;
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

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.3 — the SURVIVOR at-scale plan-shape pin for the
 * chash lookup (the .2 review's S2 carry-forward).
 *
 * <p>{@code ChashProbePerfSpikeTest} (the router comparison) and
 * {@code ChashRerouteConformanceTest} both reference {@code chash_index} and
 * retire with it at nexus-piwya.9. This class does NOT: it seeds the chunks
 * tables alone at production cardinality (255k rows across the three dim
 * tables — the cloud store's magnitude) and pins, permanently:
 * <ol>
 *   <li>EXPLAIN of the SHIPPED probe SQL ({@link ChashRepository#PROBE_SQL},
 *       the exact statement {@code lookup} executes) through the real
 *       {@code nexus_svc}/FORCE-RLS path chooses
 *       {@code idx_chunks_<dim>_tenant_chash} on all three legs with no
 *       sequential scan — on real statistics, no planner coercion. Without
 *       the router (which would have masked a slow lookup), this is the only
 *       guard against the reroute silently degrading to a 255k-row scan.</li>
 *   <li>{@code lookup} answers correctly at that scale (multi-collection
 *       membership sample).</li>
 * </ol>
 *
 * <p>Method mirrors the spike class: server-side {@code generate_series}
 * seeding; HNSW / tsv-GIN / trgm-GIN indexes dropped first (superuser,
 * discarded container) since only the btree probe path is under test and
 * vector-index maintenance would dominate seeding time.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashProbePlanShapeTest {

    private static final String TENANT = "planshape-tenant";
    private static final int CHUNKS_PER_DIM = 85_000; // 255k total ~ cloud cardinality

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    ChashRepository repo;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        seedAtCardinality();

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new ChashRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    private void seedAtCardinality() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            Statement st = su.createStatement();

            for (int dim : new int[] {384, 768, 1024}) {
                st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_" + dim + "_embedding");
                st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_" + dim + "_tsv");
                st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_" + dim + "_trgm");
            }

            for (int dim : new int[] {384, 768, 1024}) {
                st.execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                    "VALUES ('" + TENANT + "', 'plan-" + dim + "') ON CONFLICT DO NOTHING");
                st.execute(
                    "INSERT INTO nexus.chunks_" + dim +
                    " (tenant_id, collection, chash, chunk_text, embedding) " +
                    "SELECT '" + TENANT + "', 'plan-" + dim + "', " +
                    "       decode(md5('p" + dim + "-' || i) || md5('q" + dim + "-' || i), 'hex'), " +
                    "       'plan chunk ' || i, v.vec " +
                    "FROM generate_series(1, " + CHUNKS_PER_DIM + ") i " +
                    "CROSS JOIN (SELECT ('[1' || repeat(',0', " + (dim - 1) + ") || ']')::vector AS vec) v");
                st.execute("ANALYZE nexus.chunks_" + dim);
            }

            // A multi-collection sample: one 768-derived chash also lands in
            // the 384-dim collection (chunk text identity, different model).
            st.execute(
                "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) " +
                "SELECT '" + TENANT + "', 'plan-384', " +
                "       decode('" + liveChash(768, 42) + "', 'hex'), 'cross-model copy', " +
                "       ('[1' || repeat(',0', 383) || ']')::vector");
            st.execute("ANALYZE nexus.chunks_384");
        }
    }

    @Test
    void shippedProbeSqlKeepsAllThreeIndexesUnderRlsAtScale() {
        byte[] bytes = Chash.fromHex(liveChash(768, 42)).toBytes();
        String plan = tenantScope.withTenant(TENANT, ctx -> {
            StringBuilder sb = new StringBuilder();
            for (var r : ctx.resultQuery(
                    "EXPLAIN " + ChashRepository.PROBE_SQL, bytes, bytes, bytes).fetch()) {
                sb.append(r.get(0, String.class)).append('\n');
            }
            return sb.toString();
        });
        for (int dim : new int[] {384, 768, 1024}) {
            assertThat(plan)
                .as("lookup leg on chunks_%d must use its (tenant_id, chash) index at 255k-row scale", dim)
                .contains("idx_chunks_" + dim + "_tenant_chash");
        }
        assertThat(plan)
            .as("no lookup leg may degrade to a sequential scan")
            .doesNotContain("Seq Scan");
    }

    @Test
    void lookupAnswersMultiCollectionMembershipAtScale() {
        var rows = repo.lookup(TENANT, Chash.fromHex(liveChash(768, 42)));
        assertThat(rows).hasSize(2);
        assertThat(rows).extracting(r -> r.get("collection"))
            .containsExactlyInAnyOrder("plan-384", "plan-768");
        assertThat(repo.lookup(TENANT, Chash.fromHex(md5x2("plan-miss-a", "plan-miss-b"))))
            .isEmpty();
    }

    @Test
    void seededCardinalityIsReal() throws Exception {
        try (Connection su = pg.createConnection("");
             ResultSet rs = su.createStatement().executeQuery(
                "SELECT (SELECT count(*) FROM nexus.chunks_384) + " +
                "       (SELECT count(*) FROM nexus.chunks_768) + " +
                "       (SELECT count(*) FROM nexus.chunks_1024)")) {
            rs.next();
            assertThat(rs.getLong(1))
                .as("the plan-shape claim is only meaningful at cardinality")
                .isEqualTo(3L * CHUNKS_PER_DIM + 1);
        }
    }

    /** The seeding formula's chash for row i of chunks_<dim>, computed Java-side. */
    private static String liveChash(int dim, int i) {
        return md5x2("p" + dim + "-" + i, "q" + dim + "-" + i);
    }

    private static String md5x2(String a, String b) {
        return md5Hex(a) + md5Hex(b);
    }

    private static String md5Hex(String s) {
        try {
            var md = java.security.MessageDigest.getInstance("MD5");
            StringBuilder sb = new StringBuilder();
            for (byte x : md.digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8))) {
                sb.append(String.format("%02x", x));
            }
            return sb.toString();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

}
