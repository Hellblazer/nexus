// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TEXT_GATED_SEARCH_BY_CHASH_1024;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * The selective branch of {@code hybridRank} (no manifest predicate) ranks a fixed chash set, at most
 * {@code SELECTIVE_GATE_MAX} rows, with {@code text_gated_search_by_chash_<dim>}. That set is already
 * materialised, so the rank must be exact. Left to the planner, a set that is a large share of the leaf makes
 * {@code ORDER BY embedding <=> q LIMIT n} cheapest through the leaf's HNSW index. This branch sets neither
 * {@code hnsw.iterative_scan} nor a wide {@code ef_search}, so that walk yields {@code ef_search} candidates
 * nearest the query; when the gated rows are far from the query, the {@code chash = ANY(...)} filter rejects
 * every one and the statement returns fewer rows than it should, or none. The manifest sibling turned index
 * scans off for this reason (nexus-p89h4); this branch did not (RDR-247 leaf-graph consumer inventory, T2
 * {@code nexus/rdr247-leaf-graph-consumers}, item 6).
 *
 * <p>{@link #theFixtureReachesTheHazard_theRankPlansThroughTheLeafHnswUnderDefaultSettings} is the non-vacuity
 * guard: under default settings the planner does pick the leaf HNSW for this rank, so the behavioural test is
 * exercising the hazard and not a fixture the planner already ranks exactly.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HybridSelectiveRankIndexScanTest {

    private static final String TENANT = "selrank-tenant";
    private static final String SVC_ROLE = "svc_selrank_test";
    private static final String SVC_PASS = "svc_selrank_pass";
    private static final String COLL = "knowledge__selrank__voyage-context-3__v1";   // 1024

    private static final String TOKEN = "zqrankxyz";
    private static final String QUERY = TOKEN;
    /** Near the query vector, no token: what an HNSW walk from the query reaches first. */
    private static final int FILLER = 2000;
    /** Farthest from the query, carry the token: the whole gate, under SELECTIVE_GATE_MAX (5000). */
    private static final int TARGETS = 2000;
    private static final int N = 10;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository repo;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    final List<String> targetChashes = new ArrayList<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        repo = new PgVectorRepository(tenantScope, embedder, embedder);
        seed();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void seed() throws Exception {
        embedder.register(QUERY, 1.0f, 0.0f);
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < FILLER; i++) {
            String text = "common filler document number " + i + " alpha bravo charlie";
            embedder.register(text, 1.0f, 0.0f);
            ids.add(chash("rankfill", i));
            texts.add(text);
            metas.add(Map.of());
        }
        for (int i = 0; i < TARGETS; i++) {
            String text = TOKEN + " selective rank target row " + i;
            embedder.register(text, -1.0f, 0.0f);
            String c = chash("ranktarget", i);
            targetChashes.add(c);
            ids.add(c);
            texts.add(text);
            metas.add(Map.of());
        }
        try (Connection su0 = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su0, SQLDialect.POSTGRES), TENANT, COLL);
        }
        repo.upsertChunks(TENANT, COLL, ids, texts, metas);
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, COLL, ids.toArray(new String[0]));
            return null;
        });
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    @Test
    void theFixtureReachesTheHazard_theRankPlansThroughTheLeafHnswUnderDefaultSettings() {
        byte[][] chashes = targetChashes.stream().map(c -> Chash.fromHex(c).toBytes()).toArray(byte[][]::new);
        Table<?> rank = TEXT_GATED_SEARCH_BY_CHASH_1024.call(
            queryVector(), chashes, new String[] {COLL}, null, null, N, "voyage-context-3", TENANT, false);
        String plan = tenantScope.withTenant(TENANT, ctx -> {
            PgSession.setSearchPlanCacheMode(ctx);   // the plan mode hybridRank sets before this rank
            return PgContainerHelper.explainPlan(ctx, ctx.selectFrom(rank));
        });
        assertThat(plan)
            .as("non-vacuity: with index scans allowed the planner walks the leaf HNSW for this fixed-set rank, "
                + "so the test below exercises the hazard. Plan was:%n%s", plan)
            .containsPattern("Index Scan using \"?\\S*embedding_1024_idx");
    }

    @Test
    void theSelectiveRank_returnsTheNearestGatedRows_notAnHnswWalkFilteredToNothing() {
        List<Map<String, Object>> rows = repo.hybridSearch(TENANT, QUERY, List.of(COLL), N, null);
        assertThat(rows)
            .as("the selective branch ranks its %d gated chashes exactly: %d rows, every one gated", TARGETS, N)
            .hasSize(N)
            .allSatisfy(r -> assertThat(targetChashes).contains((String) r.get("id")));
    }

    private static String chash(String prefix, int i) {
        return Chash.ofText(prefix + i).toHex();
    }

    private static Vector queryVector() {
        float[] v = new float[1024];
        v[0] = 1.0f;
        return Vector.of(v);
    }
}
