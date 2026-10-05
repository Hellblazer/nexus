// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.http.RequestContext;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tu8wp.1 -- the exact re-run (nexus-bq06h) must work PER COLLECTION inside the fan-out.
 *
 * <p>Same pathology and same forcing as {@code HnswScanCapExactFallbackIntegrationTest}: the HNSW
 * walk is capped at 16 tuples and every non-HNSW plan is penalised, so the planner takes the
 * index-ordered scan the way it did in production. One BIG collection fills the shared index
 * around the query; two SMALL collections sit far away, so an index-ordered scan of either admits
 * nothing and returns EMPTY. Each starved arm must repair itself on its OWN statement: that is
 * what a single {@code LATERAL} statement over all collections could not do, and why the arms are
 * separate. Non-vacuity: the fallback counter moves by exactly the number of starved arms, and
 * stays put for the arm the index satisfies.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorSearchPerCollectionExactFallbackIntegrationTest {

    static final String TENANT = "tu8wp-fallback";
    static final String BIG = "knowledge__tu8wp-fb-big__minilm-l6-v2-384__v1";
    static final String SMALL1 = "knowledge__tu8wp-fb-small1__minilm-l6-v2-384__v1";
    static final String SMALL2 = "knowledge__tu8wp-fb-small2__minilm-l6-v2-384__v1";
    static final String QUERY = "tu8wp fallback query";
    static final int BIG_ROWS = 3000;
    static final int SMALL_ROWS = 4;
    static final int SCAN_CAP = 16;
    static final int K = 5;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    PgVectorRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        // Penalise seq, bitmap and sort so the planner takes the HNSW-ordered scan (as in
        // HnswScanCapExactFallbackIntegrationTest); the cap itself goes through PgSession's seam
        // because every search SET LOCALs the serving budget, which would override a startup option.
        cfg.addDataSourceProperty("options",
            "-c enable_seqscan=off -c enable_bitmapscan=off -c enable_sort=off");
        ds = new HikariDataSource(cfg);
        PgSession.overrideScanBudgetForTests(SCAN_CAP, 1);
        var scope = new TenantScope(ds);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(BIG, SMALL1, SMALL2)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }
        embedder.register(QUERY, 1f, 0f);
        seed(scope, embedder, BIG, "big", BIG_ROWS, -0.3, 0.6 / (BIG_ROWS - 1));
        seed(scope, embedder, SMALL1, "s1", SMALL_ROWS, 3.0, 0.01);   // far side of the circle
        seed(scope, embedder, SMALL2, "s2", SMALL_ROWS, 3.1, 0.01);
    }

    private void seed(TenantScope scope, PgVectorRepositoryContractTest.FakeEmbedder embedder, String collection,
                      String prefix, int count, double angle0, double step) {
        List<String> ids = new ArrayList<>(count);
        List<String> texts = new ArrayList<>(count);
        List<Map<String, Object>> metas = new ArrayList<>(count);
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
            String text = collection + "|" + prefix + "-" + i;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of());
        }
        for (int from = 0; from < count; from += 300) {
            int to = Math.min(count, from + 300);
            repo.upsertChunks(TENANT, collection, ids.subList(from, to), texts.subList(from, to),
                              metas.subList(from, to));
        }
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, ids.toArray(new String[0]));
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        PgSession.resetScanBudgetForTests();
        if (ds != null) {
            ds.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void eachStarvedArmRepairsItselfOnItsOwnStatement() {
        // The old shape: ONE flat statement over all three collections. Its capped index scan sees
        // only BIG's rows, so the small collections get nothing and nothing flags it.
        var flat = repo.searchWithTokens(TENANT, QUERY, List.of(BIG, SMALL1, SMALL2), K, null, false).value();
        assertThat(flat).as("the flat statement is satisfied by BIG alone").hasSize(K);
        assertThat(flat).allSatisfy(r -> assertThat(r.get("collection")).isEqualTo(BIG));

        long before = PgVectorRepository.exactFallbackCount();
        PerCollectionResult r = repo.searchPerCollection(TENANT, QUERY, List.of(BIG, SMALL1, SMALL2),
                                                         K, 100, null, null, false);
        long fallbacks = PgVectorRepository.exactFallbackCount() - before;

        long s1 = r.rows().stream().filter(x -> SMALL1.equals(x.get("collection"))).count();
        long s2 = r.rows().stream().filter(x -> SMALL2.equals(x.get("collection"))).count();
        long big = r.rows().stream().filter(x -> BIG.equals(x.get("collection"))).count();
        assertThat(s1).as("starved arm 1 returns its rows via its own exact re-run").isEqualTo(SMALL_ROWS);
        assertThat(s2).as("starved arm 2 returns its rows via its own exact re-run").isEqualTo(SMALL_ROWS);
        assertThat(big).isEqualTo(K);
        assertThat(fallbacks)
            .as("exactly the two starved arms fell back; the arm the index satisfied did not")
            .isEqualTo(2);
    }

    @Test
    void theExactRerunStillWorksWhenTheArmCarriesARequestBudget() {
        // With a deadline on the request thread the arm re-binds its statement bound to the
        // remaining budget just before the exact re-run; the repair must be unaffected.
        RequestContext.setDeadlineNanos(System.nanoTime() + 120_000_000_000L);
        try {
            long before = PgVectorRepository.exactFallbackCount();
            PerCollectionResult r = repo.searchPerCollection(TENANT, QUERY, List.of(SMALL1), K, 100, null, null,
                                                             false);
            assertThat(r.rows()).hasSize(SMALL_ROWS);
            assertThat(PgVectorRepository.exactFallbackCount() - before).isEqualTo(1);
        } finally {
            RequestContext.clearDeadline();
        }
    }

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
