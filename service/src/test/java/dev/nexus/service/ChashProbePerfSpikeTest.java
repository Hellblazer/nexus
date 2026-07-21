package dev.nexus.service;

import dev.nexus.service.db.Chash;
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
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.2 — the perf spike behind research finding 4
 * (ASSUMED until this measures it): lookup-by-chash via the 3-table UNION
 * probe with the nexus-piwya.1 {@code (tenant_id, chash)} indexes performs at
 * parity or better than the router ({@code chash_index}) lookup.
 *
 * <p><b>Venue decision</b> (recorded on the bead): the bead named the
 * migration-rehearsal {@code --guided} container as the venue, but its
 * {@code seed_legacy.py} data is fixture-scale (tens of rows per collection) —
 * EXPLAIN ANALYZE there would prove nothing beyond what fixture tests already
 * prove. This test seeds Testcontainers PG at PRODUCTION cardinality instead
 * (255k chunk rows across the three dim tables, 300k router rows including a
 * 45k orphan population; the cloud store carries ~255k chunks and a
 * ~292k-orphan router), which measures the actual question — index depth at
 * real scale — deterministically and on every suite run.
 *
 * <p>Method: server-side {@code generate_series} seeding; the HNSW / tsv-GIN /
 * trgm-GIN indexes are dropped first (superuser, discarded container) because
 * only the btree probe path is under measurement and vector-index maintenance
 * would dominate seeding time. ANALYZE everything, then compare median
 * EXPLAIN ANALYZE execution times over repeated runs. Both shapes run as
 * superuser with explicit tenant predicates — symmetric, so RLS overhead is
 * excluded from BOTH sides equally.
 *
 * <p>Plan-shape assertions run WITHOUT {@code enable_seqscan=off}: at this
 * cardinality the planner must choose the {@code idx_chunks_<dim>_tenant_chash}
 * indexes on real statistics (the at-scale proof the fixture-scale
 * VectorsChashIndexLiquibaseTest deliberately defers to here).
 *
 * <p>References {@code chash_index} (the comparison baseline), so this class
 * retires with the router at nexus-piwya.9, like ChashRerouteConformanceTest.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashProbePerfSpikeTest {

    private static final String TENANT = "perf-tenant";
    private static final int CHUNKS_PER_DIM = 85_000;   // 255k total ~ cloud chunk cardinality
    private static final int ORPHAN_ROWS   = 45_000;    // router = 255k live + 45k orphan = 300k

    private static final int WARMUP_RUNS  = 3;
    private static final int MEASURE_RUNS = 15;

    /** Parity-or-better, with a noise floor: both shapes are sub-ms in practice. */
    private static final double PARITY_FACTOR = 3.0;
    private static final double NOISE_FLOOR_MS = 1.0;
    /**
     * Catastrophe tripwire only, deliberately generous: real regression
     * detection lives in the plan-shape assertions (a seq-scan leg fails
     * those directly) and the relative parity bound (contention-robust —
     * both shapes run under the same load). A tight wall-clock bound on a
     * Testcontainers host would flake on scheduler stalls unrelated to any
     * regression (P1 review, 2026-07-20).
     */
    private static final double ABSOLUTE_BOUND_MS = 100.0;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
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

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        seedAtCardinality();
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

            // Vector/FTS index maintenance is irrelevant to the btree probe
            // and would dominate bulk-load time; drop them (container is
            // discarded, nothing rebuilds them).
            for (int dim : new int[] {384, 768, 1024}) {
                st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_" + dim + "_embedding");
                st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_" + dim + "_tsv");
                st.execute("DROP INDEX IF EXISTS nexus.idx_chunks_" + dim + "_trgm");
            }

            for (int dim : new int[] {384, 768, 1024}) {
                st.execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                    "VALUES ('" + TENANT + "', 'perf-" + dim + "') ON CONFLICT DO NOTHING");
                // 32-byte chash = md5(a)||md5(b): deterministic, unique per (dim, i).
                st.execute(
                    "INSERT INTO nexus.chunks_" + dim +
                    " (tenant_id, collection, chash, chunk_text, embedding) " +
                    "SELECT '" + TENANT + "', 'perf-" + dim + "', " +
                    "       decode(md5('c" + dim + "-' || i) || md5('d" + dim + "-' || i), 'hex'), " +
                    "       'perf chunk ' || i, v.vec " +
                    "FROM generate_series(1, " + CHUNKS_PER_DIM + ") i " +
                    "CROSS JOIN (SELECT ('[1' || repeat(',0', " + (dim - 1) + ") || ']')::vector AS vec) v");
            }

            // Router: one live row per chunk row (the dual-write era shape)...
            st.execute(
                "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) " +
                "SELECT tenant_id, chash, collection, created_at FROM nexus.chunks_384 " +
                "UNION ALL SELECT tenant_id, chash, collection, created_at FROM nexus.chunks_768 " +
                "UNION ALL SELECT tenant_id, chash, collection, created_at FROM nexus.chunks_1024");
            // ...plus the orphan population (rows whose chunks are long gone).
            st.execute(
                "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) " +
                "SELECT '" + TENANT + "', decode(md5('orph-' || i) || md5('anx-' || i), 'hex'), " +
                "       'perf-384', now() " +
                "FROM generate_series(1, " + ORPHAN_ROWS + ") i");

            st.execute("ANALYZE nexus.chunks_384");
            st.execute("ANALYZE nexus.chunks_768");
            st.execute("ANALYZE nexus.chunks_1024");
            st.execute("ANALYZE nexus.chash_index");

            try (ResultSet rs = st.executeQuery(
                    "SELECT (SELECT count(*) FROM nexus.chunks_384) + " +
                    "       (SELECT count(*) FROM nexus.chunks_768) + " +
                    "       (SELECT count(*) FROM nexus.chunks_1024), " +
                    "       (SELECT count(*) FROM nexus.chash_index)")) {
                rs.next();
                assertThat(rs.getLong(1)).isEqualTo(3L * CHUNKS_PER_DIM);
                assertThat(rs.getLong(2)).isEqualTo(3L * CHUNKS_PER_DIM + ORPHAN_ROWS);
            }
        }
    }

    // ── plan shape at scale, real statistics, NO planner coercion ───────────

    @Test
    void probePlanUsesAllThreeTenantChashIndexesAtScale() throws Exception {
        try (Connection su = pg.createConnection("")) {
            String plan = explain(su, probeSql(liveChash(768, 42)), false);
            for (int dim : new int[] {384, 768, 1024}) {
                assertThat(plan)
                    .as("probe leg on chunks_%d must use its (tenant_id, chash) index at 255k-row scale", dim)
                    .contains("idx_chunks_" + dim + "_tenant_chash");
            }
            assertThat(plan)
                .as("no probe leg may fall back to a sequential scan")
                .doesNotContain("Seq Scan");
        }
    }

    @Test
    void probePlanSurvivesRealRlsAtScale() {
        // Critic S1 (P1 review): the medians above are measured
        // superuser-symmetric, which BYPASSES row security on both sides.
        // This test closes the asserted-vs-proven gap: plan the probe through
        // TenantScope under nexus_svc (NOSUPERUSER NOBYPASSRLS, subject to
        // the FORCE RLS policies) against the 255k-row store and confirm the
        // planner still chooses all three (tenant_id, chash) indexes once the
        // RLS quals are ANDed in — the plan shape nexus-piwya.3 actually ships.
        byte[] bytes = Chash.fromHex(liveChash(768, 42)).toBytes();
        String probeWithRls =
            "EXPLAIN SELECT collection, created_at FROM nexus.chunks_384 " +
            " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ? " +
            "UNION ALL " +
            "SELECT collection, created_at FROM nexus.chunks_768 " +
            " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ? " +
            "UNION ALL " +
            "SELECT collection, created_at FROM nexus.chunks_1024 " +
            " WHERE tenant_id = current_setting('nexus.tenant', true) AND chash = ?";
        String plan = tenantScope.withTenant(TENANT, ctx -> {
            StringBuilder sb = new StringBuilder();
            for (var r : ctx.resultQuery(probeWithRls, bytes, bytes, bytes).fetch()) {
                sb.append(r.get(0, String.class)).append('\n');
            }
            return sb.toString();
        });
        for (int dim : new int[] {384, 768, 1024}) {
            assertThat(plan)
                .as("probe leg on chunks_%d must keep its (tenant_id, chash) index under real RLS", dim)
                .contains("idx_chunks_" + dim + "_tenant_chash");
        }
        assertThat(plan)
            .as("no probe leg may seq-scan under RLS")
            .doesNotContain("Seq Scan");
    }

    @Test
    void routerPlanUsesAnIndex() throws Exception {
        try (Connection su = pg.createConnection("")) {
            String plan = explain(su, routerSql(liveChash(768, 42)), false);
            assertThat(plan)
                .as("router lookup must be an index seek (fair baseline)")
                .containsAnyOf("chash_index_pkey", "idx_chash_index_chash", "chash_index_pk");
            assertThat(plan).doesNotContain("Seq Scan");
        }
    }

    // ── the measurement: parity or better ───────────────────────────────────

    @Test
    void probeLatencyIsParityOrBetterThanRouterAtScale() throws Exception {
        // Live targets spread across all three dim tables + one guaranteed
        // miss (the miss path is the census/alias probe's common case).
        List<String> targets = List.of(
            liveChash(384, 7), liveChash(768, 4242), liveChash(1024, 80_000),
            md5x2("miss-a", "miss-b"));

        try (Connection su = pg.createConnection("")) {
            for (String t : targets) {
                for (int i = 0; i < WARMUP_RUNS; i++) {
                    explain(su, probeSql(t), true);
                    explain(su, routerSql(t), true);
                }
            }
            List<Double> probeTimes = new ArrayList<>();
            List<Double> routerTimes = new ArrayList<>();
            for (int i = 0; i < MEASURE_RUNS; i++) {
                for (String t : targets) {
                    probeTimes.add(executionTimeMs(explain(su, probeSql(t), true)));
                    routerTimes.add(executionTimeMs(explain(su, routerSql(t), true)));
                }
            }
            double probeMedian = median(probeTimes);
            double routerMedian = median(routerTimes);

            // The measured numbers ARE the spike's deliverable — surface them
            // in the test output for the bead record.
            System.out.printf(
                "RDR-187 perf spike (255k chunks / 300k router rows, %d runs x %d targets):%n" +
                "  3-table probe median: %.3f ms%n" +
                "  router lookup median: %.3f ms%n",
                MEASURE_RUNS, targets.size(), probeMedian, routerMedian);

            assertThat(probeMedian)
                .as("3-table probe (%.3f ms) must be parity-or-better vs router (%.3f ms), " +
                    "within noise (x%.1f + %.1f ms)",
                    probeMedian, routerMedian, PARITY_FACTOR, NOISE_FLOOR_MS)
                .isLessThanOrEqualTo(routerMedian * PARITY_FACTOR + NOISE_FLOOR_MS);
            assertThat(probeMedian)
                .as("3-table probe must be fast in absolute terms")
                .isLessThan(ABSOLUTE_BOUND_MS);
        }
    }

    // ── SQL shapes ──────────────────────────────────────────────────────────

    /** The reroute probe shape (ChashRerouteConformanceTest.PROBE_SQL with explicit tenant). */
    private static String probeSql(String chashHex) {
        String pred = " WHERE tenant_id = '" + TENANT + "' AND chash = decode('" + chashHex + "', 'hex')";
        return "SELECT collection, created_at FROM nexus.chunks_384" + pred +
               " UNION ALL SELECT collection, created_at FROM nexus.chunks_768" + pred +
               " UNION ALL SELECT collection, created_at FROM nexus.chunks_1024" + pred;
    }

    /** The router lookup shape (ChashRepository.lookup's query with explicit tenant). */
    private static String routerSql(String chashHex) {
        return "SELECT physical_collection, created_at FROM nexus.chash_index " +
               "WHERE tenant_id = '" + TENANT + "' AND chash = decode('" + chashHex + "', 'hex')";
    }

    /** The seeding formula's chash for row i of chunks_<dim>, computed Java-side. */
    private static String liveChash(int dim, int i) {
        return md5x2("c" + dim + "-" + i, "d" + dim + "-" + i);
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

    // ── EXPLAIN plumbing ────────────────────────────────────────────────────

    private static String explain(Connection c, String sql, boolean analyze) throws Exception {
        String prefix = analyze ? "EXPLAIN (ANALYZE, TIMING OFF) " : "EXPLAIN ";
        List<String> lines = new ArrayList<>();
        try (Statement st = c.createStatement();
             ResultSet rs = st.executeQuery(prefix + sql)) {
            while (rs.next()) {
                lines.add(rs.getString(1));
            }
        }
        return String.join("\n", lines);
    }

    private static double executionTimeMs(String plan) {
        for (String line : plan.split("\n")) {
            if (line.startsWith("Execution Time:")) {
                return Double.parseDouble(line.replace("Execution Time:", "").replace("ms", "").trim());
            }
        }
        throw new IllegalStateException("no Execution Time in plan:\n" + plan);
    }

    private static double median(List<Double> xs) {
        List<Double> sorted = new ArrayList<>(xs);
        sorted.sort(Double::compareTo);
        int n = sorted.size();
        return n % 2 == 1 ? sorted.get(n / 2)
                          : (sorted.get(n / 2 - 1) + sorted.get(n / 2)) / 2.0;
    }
}
