// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Random;

import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_ALLOWED_384;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-jypmj (RDR-232): what a {@code chash_allow} search of 3000 hashes costs on a collection of realistic
 * size, at the planner's default settings (nothing penalised, the router at its default), and what plan it
 * takes. A measurement, not a gate: it prints the latencies and the plans, and asserts only that the plan reads
 * the allowed rows through an index on the chunk key and never through the HNSW index.
 *
 * <p>Row count from {@code NX_CHASH_ALLOW_PERF_ROWS} (default 20000, the cardinality router's default
 * threshold); the committed default keeps the suite fast, a 50000-row measurement is one environment variable.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorChashAllowPerfIntegrationTest {

    private static final String TENANT = "jypmj-chash-perf";
    private static final String COLL = "knowledge__jypmj-perf__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";
    private static final String QUERY = "jypmj perf query";
    private static final String GATE = "zebracorn";
    private static final int ALLOW = 3000;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepository repo;
    final List<String> allChashes = new ArrayList<>();
    final List<String> gateChashes = new ArrayList<>();
    int rows;

    @BeforeAll
    void startAll() throws Exception {
        String env = System.getenv("NX_CHASH_ALLOW_PERF_ROWS");
        rows = env == null || env.isBlank() ? 20_000 : Integer.parseInt(env.trim());
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLL);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);
        embedder.register(QUERY, 1f, 0f);
        embedder.register(GATE, 1f, 0f);
        Random rnd = new Random(20261030L);
        List<String> ids = new ArrayList<>(rows);
        List<String> texts = new ArrayList<>(rows);
        for (int i = 0; i < rows; i++) {
            double theta = rnd.nextDouble() * Math.PI;
            String text = (i % 10 == 0 ? "perf " + GATE + " " : "perf plain ") + i;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            String id = dev.nexus.service.db.Chash.ofText(text).toHex();
            ids.add(id);
            texts.add(text);
            if (i % 10 == 0) gateChashes.add(id);
        }
        allChashes.addAll(ids);
        for (int from = 0; from < rows; from += 500) {
            int to = Math.min(rows, from + 500);
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = from; i < to; i++) metas.add(Map.of());
            repo.upsertChunks(TENANT, COLL, ids.subList(from, to), texts.subList(from, to), metas);
        }
        scope.withTenant(TENANT, ctx -> {
            for (int from = 0; from < rows; from += 2000) {
                int to = Math.min(rows, from + 2000);
                PgContainerHelper.ownChunks(ctx, TENANT, COLL, ids.subList(from, to).toArray(new String[0]));
            }
            // The state a production leaf is in after autovacuum: statistics and visibility map current.
            return null;
        });
        // A production leaf is vacuumed and analyzed by autovacuum; a freshly loaded one is not until it is.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String table : List.of("chunks", "catalog_document_chunks", "catalog_documents")) {
                PgContainerHelper.vacuumTable(su, "nexus", table);
            }
        }
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private byte[][] sample(List<String> from, int n, long seed) {
        List<String> copy = new ArrayList<>(from);
        Collections.shuffle(copy, new Random(seed));
        byte[][] out = new byte[Math.min(n, copy.size())][];
        for (int i = 0; i < out.length; i++) out[i] = dev.nexus.service.db.Chash.fromHex(copy.get(i)).toBytes();
        return out;
    }

    private static double medianMs(List<Long> nanos) {
        List<Long> s = new ArrayList<>(nanos);
        Collections.sort(s);
        return s.get(s.size() / 2) / 1e6;
    }

    @Test
    void measure_3000AllowedHashes_onTheDefaultPlanner() {
        byte[][] allow = sample(allChashes, ALLOW, 1L);
        byte[][] gateAllow = sample(gateChashes, ALLOW, 2L);

        List<Long> plain = new ArrayList<>();
        List<Long> hybrid = new ArrayList<>();
        List<Long> unrestricted = new ArrayList<>();
        int plainRows = 0;
        int hybridRows = 0;
        for (int i = 0; i < 28; i++) {
            long t0 = System.nanoTime();
            plainRows = repo.searchWithTokens(TENANT, QUERY, List.of(COLL), 10, null, false, allow).value().size();
            long t1 = System.nanoTime();
            hybridRows = repo.hybridSearchWithTokens(TENANT, GATE, List.of(COLL), 10, null, false, gateAllow)
                .value().size();
            long t2 = System.nanoTime();
            try {
                repo.searchWithTokens(TENANT, QUERY, List.of(COLL), 10, null, false);
            } catch (RuntimeException e) {
                // The unrestricted HNSW search is only the yardstick; it is not what this test asserts.
                System.out.println("CHASH_ALLOW_PERF unrestricted search failed: " + e.getClass().getSimpleName());
            }
            long t3 = System.nanoTime();
            if (i >= 3) {
                plain.add(t1 - t0);
                hybrid.add(t2 - t1);
                unrestricted.add(t3 - t2);
            }
        }
        System.out.printf("CHASH_ALLOW_PERF rows=%d allow=%d plain_median_ms=%.1f plain_max_ms=%.1f "
                + "hybrid_median_ms=%.1f hybrid_max_ms=%.1f unrestricted_median_ms=%.1f%n",
            rows, ALLOW, medianMs(plain), Collections.max(plain) / 1e6, medianMs(hybrid),
            Collections.max(hybrid) / 1e6, medianMs(unrestricted));
        assertThat(plainRows).isEqualTo(10);
        assertThat(hybridRows).isEqualTo(10);

        Vector q = Vector.of(unit());
        Table<?> byChash = TEXT_GATED_SEARCH_BY_CHASH_384.call(q, allow, new String[] {COLL}, null, null, 10, MODEL, TENANT);
        Table<?> allowed = TEXT_GATED_SEARCH_ALLOWED_384.call(q, GATE, gateAllow, new String[] {COLL}, null, null, 10,
                                                              MODEL, TENANT);
        String planPlain = scope.withTenant(TENANT, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            return PgContainerHelper.explainPlan(ctx, ctx.select(byChash.field("id")).from(byChash));
        });
        String planHybrid = scope.withTenant(TENANT, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            PgSession.setLocal(ctx, "pg_trgm.word_similarity_threshold", "0.6");
            return PgContainerHelper.explainPlan(ctx, ctx.select(allowed.field("id")).from(allowed));
        });
        System.out.println("CHASH_ALLOW_PLAN plain:\n" + planPlain);
        System.out.println("CHASH_ALLOW_PLAN hybrid:\n" + planHybrid);
        for (String plan : List.of(planPlain, planHybrid)) {
            assertThat(plan).as("the HNSW index is not a candidate on the exact route").doesNotContain("embedding_384_idx");
            assertThat(plan).doesNotContain("Function Scan");
            assertThat(plan).as("the allowed rows are read through the (tenant_id, chash) index. Plan was:%n%s", plan)
                .containsPattern("Index Scan on \\S*chash\\S*");
        }
    }

    private static float[] unit() {
        float[] v = new float[384];
        v[0] = 1f;
        return v;
    }
}
