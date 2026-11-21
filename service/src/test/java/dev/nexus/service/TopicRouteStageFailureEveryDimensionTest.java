// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CollectionIds;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TaxonomyCoverage;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionStat;
import dev.nexus.service.vectors.TopicRoute;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.3 (RDR-134 pre-release test validation G4): the degrade paths of a routed arm at EVERY embedding
 * dimension. {@code PgVectorSearchPerCollectionTopicRouteIntegrationTest} injects a missing database function at 384
 * only; the three routing statements each call a per-dimension function ({@code taxonomy_coverage_counts_<dim>},
 * {@code taxonomy_ann_query_<dim>}, {@code search_topic_set_<dim>}), and a dimension whose arm name or SQL dispatch
 * were wrong would not be seen there. Each dimension has its own collection, embedder and repository over one
 * database; a routed request is first shown to route (so a failure below is the injected one, not an unrouted
 * collection), then each function is made to fail as an undefined function (SQLSTATE 42883) at the arm's connection.
 * Expected: the coverage and centroid failures degrade to {@code no_topics}, the topic-search failure to
 * {@code fallback}, each serving the rows an unrouted request serves and failing nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TopicRouteStageFailureEveryDimensionTest {

    private static final String TENANT = "7fku213-dims";
    private static final String QUERY = "7fku213 dims query";
    private static final int K = 5;
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");
    private static final int[] DIMS = {384, 768, 1024};

    /** What one dimension needs: its collection, its embedder, a repository whose arms the probe watches. */
    private static final class Dim {
        final int dim;
        final String collection;
        final PgVectorRepositoryContractTest.FakeEmbedder embedder;
        PgVectorRepository repo;
        ArmProbeDataSource probe;
        TenantScope probeScope;
        PgVectorRepository probeRepo;

        Dim(int dim, String collection) {
            this.dim = dim;
            this.collection = collection;
            this.embedder = new PgVectorRepositoryContractTest.FakeEmbedder(dim);
        }
    }

    private final Map<Integer, Dim> dims = new LinkedHashMap<>();
    PostgreSQLContainer<?> pg;
    HikariDataSource ds;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        TenantScope scope = new TenantScope(ds);
        dims.put(384, new Dim(384, "knowledge__7fku213-dims384__minilm-l6-v2-384__v1"));
        dims.put(768, new Dim(768, "knowledge__7fku213-dims768__bge-base-en-v15-768__v1"));
        dims.put(1024, new Dim(1024, "knowledge__7fku213-dims1024__voyage-context-3__v1"));
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (Dim d : dims.values()) {
                PgContainerHelper.insertCollection(dsl, TENANT, d.collection);
            }
        }
        for (Dim d : dims.values()) {
            d.embedder.register(QUERY, 1f, 0f);
            d.repo = new PgVectorRepository(scope, d.embedder, d.embedder);
            d.probe = new ArmProbeDataSource(ds);
            d.probeScope = new TenantScope(d.probe.dataSource());
            d.probeRepo = new PgVectorRepository(d.probeScope, d.embedder, d.embedder);
            // topic TA: centroid nearest the query, chunks the farther; topic TB: centroid far, chunks the nearer
            List<String> ta = seed(scope, d, "ta", 6, 0.30, 0.01);
            List<String> tb = seed(scope, d, "tb", 6, 0.05, 0.01);
            taxonomy(d, "TA", 0.10, ta);
            taxonomy(d, "TB", 1.00, tb);
        }
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private List<String> seed(TenantScope scope, Dim d, String prefix, int count, double angle0, double step) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
            String text = d.collection + "|" + prefix + i;
            d.embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of());
        }
        d.repo.upsertChunks(TENANT, d.collection, ids, texts, metas);
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, d.collection, ids.toArray(new String[0]));
            return null;
        });
        return ids;
    }

    private void taxonomy(Dim d, String label, double centroidAngle, List<String> assigned) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, d.collection);
            long id = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, label + " of " + d.collection)
                .set(TOPICS.COLLECTION, d.collection)
                .set(TOPICS.COLLECTION_ID, CollectionIds.idOf(TENANT, d.collection)).set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
            dsl.insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, TENANT).set(TAXONOMY_CENTROIDS.COLLECTION, d.collection).set(TAXONOMY_CENTROIDS.COLLECTION_ID, CollectionIds.idOf(TENANT, d.collection))
                .set(TAXONOMY_CENTROIDS.TOPIC_ID, id).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, model)
                .set(TAXONOMY_CENTROIDS.LABEL, label)
                .set(VectorSeeds.centroidEmbedding(d.dim),
                     VectorSeeds.vecXY(d.dim, (float) Math.cos(centroidAngle), (float) Math.sin(centroidAngle)))
                .execute();
            for (String hex : assigned) {
                dsl.insertInto(TOPIC_ASSIGNMENTS)
                    .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT)
                    .set(TOPIC_ASSIGNMENTS.DOC_ID, HexFormat.of().parseHex(hex))
                    .set(TOPIC_ASSIGNMENTS.TOPIC_ID, id).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, d.collection).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(TENANT, d.collection))
                    .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
                    .execute();
            }
        }
    }

    private static PerCollectionResult run(Dim d, TopicRoute route) {
        return d.probeRepo.searchPerCollection(TENANT, QUERY, List.of(d.collection), K, 1200, null, null, false, false,
                                               false, null, false, route);
    }

    private static TopicRoute routeOf(Dim d) {
        return new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(d.collection),
                              new TaxonomyCoverage(d.probeScope)::lookup, false);
    }

    private static PerCollectionStat stat(PerCollectionResult r, Dim d) {
        return r.perCollection().stream().filter(s -> s.collection().equals(d.collection)).findFirst().orElseThrow();
    }

    private static List<String> ids(PerCollectionResult r) {
        return r.rows().stream().map(x -> (String) x.get("id")).toList();
    }

    private static List<String> hashes(Dim d, String prefix) {
        List<String> out = new ArrayList<>();
        for (int i = 0; i < K; i++) out.add(chash(d.collection + "|" + prefix + i));
        return out;
    }

    /** Make every arm statement that calls {@code nexus.<function>} fail as an undefined function (SQLSTATE 42883). */
    private static PerCollectionResult withFunctionMissing(Dim d, String function) {
        d.probe.reset();
        d.probe.failSqlWhen = sql -> sql.contains(function);
        d.probe.failure = () -> new SQLException("function nexus." + function + " does not exist (test)", "42883");
        try {
            return run(d, routeOf(d));
        } finally {
            d.probe.reset();
        }
    }

    @Test
    void everyDimensionRoutes_beforeAnythingIsInjected_soTheFailuresBelowAreTheInjectedOnes() {
        for (Dim d : dims.values()) {
            PerCollectionResult routed = run(d, routeOf(d));
            assertThat(stat(routed, d).routeStatus()).as("dim %d", d.dim).isEqualTo("routed");
            assertThat(ids(routed)).as("dim %d: TA's chunks, though TB's are nearer", d.dim)
                .containsExactlyElementsOf(hashes(d, "ta"));
            assertThat(ids(run(d, null))).as("dim %d non-vacuity: unrouted, TB's", d.dim)
                .containsExactlyElementsOf(hashes(d, "tb"));
        }
    }

    @Test
    void aMissingTopicSearchFunction_isAFallback_atEveryDimension() {
        for (Dim d : dims.values()) {
            PerCollectionResult plain = run(d, null);
            PerCollectionResult r = withFunctionMissing(d, "search_topic_set_" + d.dim);
            PerCollectionStat s = stat(r, d);
            assertThat(s.routeStatus()).as("dim %d", d.dim).isEqualTo("fallback");
            assertThat(s.routeError()).as("dim %d", d.dim).startsWith("topic search failed").contains("SQLSTATE 42883");
            assertThat(s.error()).as("dim %d: served, not failed", d.dim).isNull();
            assertThat(r.rows()).as("dim %d: the rows of an unrouted request", d.dim).isEqualTo(plain.rows());
        }
    }

    @Test
    void aMissingCoverageFunction_isNoTopics_atEveryDimension() {
        for (Dim d : dims.values()) {
            PerCollectionResult plain = run(d, null);
            PerCollectionResult r = withFunctionMissing(d, "taxonomy_coverage_counts_" + d.dim);
            assertThat(stat(r, d).routeStatus()).as("dim %d", d.dim).isEqualTo("no_topics");
            assertThat(stat(r, d).error()).as("dim %d", d.dim).isNull();
            assertThat(r.rows()).as("dim %d", d.dim).isEqualTo(plain.rows());
        }
    }

    @Test
    void aMissingCentroidRankingFunction_isNoTopics_atEveryDimension() {
        for (Dim d : dims.values()) {
            PerCollectionResult plain = run(d, null);
            PerCollectionResult r = withFunctionMissing(d, "taxonomy_ann_query_" + d.dim);
            assertThat(stat(r, d).routeStatus()).as("dim %d", d.dim).isEqualTo("no_topics");
            assertThat(stat(r, d).routeError()).as("dim %d", d.dim).startsWith("centroid ranking failed")
                .contains("SQLSTATE 42883");
            assertThat(stat(r, d).error()).as("dim %d", d.dim).isNull();
            assertThat(r.rows()).as("dim %d", d.dim).isEqualTo(plain.rows());
        }
    }

    @Test
    void theDimensionsAreTheThreeTheEngineServes() {
        assertThat(dims.keySet()).containsExactly(DIMS[0], DIMS[1], DIMS[2]);
    }
}
