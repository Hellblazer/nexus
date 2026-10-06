package dev.nexus.service;

import dev.nexus.service.db.Chash;
import dev.nexus.service.db.ChashRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.testcontainers.containers.PostgreSQLContainer;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.3 — the SURVIVOR at-scale plan-shape pin for the
 * chash lookup (the .2 review's S2 carry-forward).
 *
 * <p>{@code ChashProbePerfSpikeTest} (the router comparison) and
 * {@code ChashRerouteConformanceTest} both reference {@code chash_index} and
 * retire with it at nexus-piwya.9. This class does NOT: it seeds the unified
 * {@code nexus.chunks} table at production cardinality (255k rows across the
 * three embedding-column populations — the cloud store's magnitude) and
 * pins, permanently:
 * <ol>
 *   <li>EXPLAIN of the SHIPPED probe SQL ({@link ChashRepository#PROBE_SQL},
 *       the exact statement {@code lookup} executes) through the real
 *       {@code nexus_svc}/FORCE-RLS path chooses {@code
 *       idx_chunks_tenant_chash} with no sequential scan — on real
 *       statistics, no planner coercion. Without the router (which would
 *       have masked a slow lookup), this is the only guard against the
 *       reroute silently degrading to a 255k-row scan.</li>
 *   <li>{@code lookup} answers correctly at that scale (multi-collection
 *       membership sample).</li>
 * </ol>
 *
 * <p>RDR-191 Phase 4 (repoint-batch lane D5, bead nexus-o8dil.48 part 2):
 * retargeted from three per-dim tables ({@code nexus.chunks_384/768/1024},
 * each with its own {@code idx_chunks_<dim>_tenant_chash}) to the ONE
 * unified {@code nexus.chunks} table (vectors-004-unify-chunks.xml) with a
 * SINGLE {@code idx_chunks_tenant_chash} index — seeding now writes all
 * three embedding populations into the same table (one row per generated
 * id, embedding landing in its dim-typed column), and {@code PROBE_SQL} is
 * a single-leg SELECT rather than a three-leg {@code UNION ALL} (see that
 * constant's own javadoc for why a union of the identical predicate against
 * the identical post-unification table would triple-return every row).
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
            PgContainerHelper.applyProductSchema(su);
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

            // RDR-191 Phase 4 (lane D5): one unified nexus.chunks table now
            // carries all three embedding columns and ONE (tenant_id, chash)
            // index (idx_chunks_tenant_chash), not one per dim.
            for (int dim : new int[] {384, 768, 1024}) {
                st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_embedding_" + dim);
            }
            st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_tsv");
            st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_trgm");

            for (int dim : new int[] {384, 768, 1024}) {
                // RDR-204 nexus-ft04v.4/.5: routed through PgContainerHelper.insertCollection.
                // RDR-225: each collection carries the one model whose dimension matches its vectors.
                PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "plan-" + dim,
                    modelFor(dim));
                st.execute(
                    "INSERT INTO nexus.chunks" +
                    " (tenant_id, collection, embedding_model, chash, chunk_text, embedding_" + dim + ") " +
                    "SELECT '" + TENANT + "', 'plan-" + dim + "', " +
                    "       '" + modelFor(dim) + "', " +
                    "       decode(md5('p" + dim + "-' || i) || md5('q" + dim + "-' || i), 'hex'), " +
                    "       'plan chunk ' || i, v.vec " +
                    "FROM generate_series(1, " + CHUNKS_PER_DIM + ") i " +
                    "CROSS JOIN (SELECT ('[1' || repeat(',0', " + (dim - 1) + ") || ']')::nexus.vector AS vec) v");
            }

            // A multi-collection sample: one 768-derived chash also lands in
            // the 384-dim collection (chunk text identity, different model).
            // Different collection ('plan-384' vs 'plan-768') means no PK
            // collision on (tenant_id, collection, chash).
            st.execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, embedding_model, chash, chunk_text, embedding_384) " +
                "SELECT '" + TENANT + "', 'plan-384', '" + modelFor(384) + "', " +
                "       decode('" + liveChash(768, 42) + "', 'hex'), 'cross-model copy', " +
                "       ('[1' || repeat(',0', 383) || ']')::nexus.vector");
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    @Test
    void shippedProbeSqlKeepsTenantChashIndexUnderRlsAtScale() {
        byte[] bytes = Chash.fromHex(liveChash(768, 42)).toBytes();
        String plan = tenantScope.withTenant(TENANT, ctx -> {
            StringBuilder sb = new StringBuilder();
            for (var r : ctx.resultQuery(
                    "EXPLAIN " + ChashRepository.PROBE_SQL, bytes).fetch()) {
                sb.append(r.get(0, String.class)).append('\n');
            }
            return sb.toString();
        });
        // RDR-225: nexus.chunks is LIST-partitioned by model then tenant, so the plan names the tenant's
        // LEAF under each model partition (and each leaf's own copy of idx_chunks_tenant_chash, named
        // <leaf>_tenant_id_chash_idx by Postgres), not the parent index. The three leaves that hold the seeded rows must each be
        // probed through an index and never scanned sequentially; the leaves of models with no rows are
        // empty, and the planner is free to seq-scan an empty relation.
        for (int dim : new int[] {384, 768, 1024}) {
            String leaf = leafName(modelFor(dim), TENANT);
            assertThat(plan)
                .as("lookup must use an index on the seeded leaf %s at 255k-row scale", leaf)
                .contains(leaf + "_tenant_id_chash_idx");
            assertThat(plan)
                .as("lookup may not degrade to a sequential scan of the seeded leaf %s", leaf)
                .doesNotContainPattern("Seq Scan on (nexus\\.)?" + leaf);
        }
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
                "SELECT count(*) FROM nexus.chunks")) {
            rs.next();
            assertThat(rs.getLong(1))
                .as("the plan-shape claim is only meaningful at cardinality")
                .isEqualTo(3L * CHUNKS_PER_DIM + 1);
        }
    }

    /** The embedding model whose dimension is {@code dim} (RDR-225: a collection has exactly one model). */
    private static String modelFor(int dim) {
        return switch (dim) {
            case 384 -> "minilm-l6-v2-384";
            case 768 -> "bge-base-en-v15-768";
            case 1024 -> "voyage-code-3";
            default -> throw new IllegalArgumentException("no model of dimension " + dim);
        };
    }

    /** The seeding formula's chash for row i of chunks_<dim>, computed Java-side. */
    private static String liveChash(int dim, int i) {
        return md5x2("p" + dim + "-" + i, "q" + dim + "-" + i);
    }

    /** {@code nexus.partition_name('chunks', model, tenant)}: the tenant's leaf under the model partition. */
    private static String leafName(String model, String tenant) {
        return "chunks_m" + sha256Hex(model).substring(0, 8) + "_t_" + sha256Hex(tenant).substring(0, 16);
    }

    private static String sha256Hex(String s) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            StringBuilder sb = new StringBuilder();
            for (byte x : md.digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8))) {
                sb.append(String.format("%02x", x));
            }
            return sb.toString();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
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
