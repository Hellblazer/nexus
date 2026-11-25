// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.TaxonomyCoverage;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.http.RequestContext;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.ArmErrorKind;
import dev.nexus.service.vectors.PgVectorRepository.FanoutSettings;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionStat;
import dev.nexus.service.vectors.RequestDeadlineExceededException;
import dev.nexus.service.vectors.SearchFanoutTransientException;
import dev.nexus.service.vectors.TopicRoute;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-7fku2.1.3 (RDR-134 Step 3): {@code topic_route} on {@link PgVectorRepository#searchPerCollection}, against a real
 * Postgres as {@code nexus_svc}.
 *
 * <p>The fixture is built so that routing changes the answer. In a prose collection the topic whose centroid is nearest
 * the query (TA, 0.10 rad) holds chunks that are FARTHER from the query (0.30 rad and up) than the chunks of the other
 * topic (TB, centroid 1.0 rad, chunks from 0.05 rad). The unscoped top-5 is therefore all TB; a routed top-1 search is
 * all TA, so a test sees from the rows alone whether the route ran.
 *
 * <p>What the class pins: every status ({@code routed}, {@code no_topics}, {@code fallback}, {@code skipped}); band
 * mode; the request-wide skip ({@code chash_allow}, {@code where}); the mixed group, where a collection the request does
 * not list is searched unscoped with status skipped and returns exactly what an unrouted request returns; a present
 * but empty {@code collections} list, which changes nothing; routed rows that carry the fields an unscoped row carries
 * (the topic function returns no metadata, so the route reads it); the threshold; one embedding per request; and the
 * two failure modes the RDR omits, an erroring topic search and a failed coverage lookup (plus a failed centroid
 * ranking), each of which serves the collection unscoped and fails nothing.
 *
 * <p>A stage's error is induced through {@link ArmProbeDataSource#failSqlWhen}, which fails the arm's connection at one
 * stage's statement with a SQLSTATE of the test's choosing, so jOOQ wraps it as it wraps a real one: an undefined
 * function (42883, not transient and not a timeout) for the failures a route degrades, 57014 under each of the three
 * bounds, and the failures it must NOT degrade (the request deadline, a transient connection or SQLSTATE) through the
 * coverage seam too. The routing reads' clamp to the arm's budget is read off the {@code statement_timeout} each arm
 * connection was given. {@code TopicRouteArmPropagationTest} pins the same split with no database.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorSearchPerCollectionTopicRouteIntegrationTest {

    private static final String TENANT = "7fku213-route";
    private static final String QUERY = "7fku213 route query";
    private static final int K = 5;
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    private static final String PROSE = "knowledge__7fku213-prose__minilm-l6-v2-384__v1";
    private static final String CODE = "knowledge__7fku213-code__minilm-l6-v2-384__v1";
    private static final String NOCENT = "knowledge__7fku213-nocent__minilm-l6-v2-384__v1";
    private static final String LOWCOV = "knowledge__7fku213-lowcov__minilm-l6-v2-384__v1";
    private static final String EMPTY = "knowledge__7fku213-empty__minilm-l6-v2-384__v1";
    private static final String FALLBACK = "knowledge__7fku213-fallback__minilm-l6-v2-384__v1";
    /** A REAL code collection (the prefix the engine's guard reads), with PROSE's taxonomy and full coverage. */
    private static final String CODEC = "code__7fku213-codec__minilm-l6-v2-384__v1";
    /** Coverage 0.95: 19 of 20 chunks assigned. Routed at 0.9, not at 1.0. */
    private static final String ALMOST = "knowledge__7fku213-almost__minilm-l6-v2-384__v1";

    /** Counts every embed call, to prove the request embeds its query once. */
    private static final class CountingEmbedder implements Embedder {
        private final Embedder delegate;
        final AtomicInteger calls = new AtomicInteger();

        CountingEmbedder(Embedder delegate) {
            this.delegate = delegate;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            calls.incrementAndGet();
            return delegate.embed(texts);
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            calls.incrementAndGet();
            return new EmbedResult(delegate.embed(texts), 0L);
        }
    }

    /** A topic of the fixture: its centroid's angle from the query, and the chunk keys assigned to it. */
    private record Topic(String label, double centroidAngle, List<String> assigned) {}

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    CountingEmbedder counting;
    PgVectorRepository repo;
    ArmProbeDataSource probe;
    TenantScope probeScope;
    PgVectorRepository probeRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(12);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        counting = new CountingEmbedder(embedder);
        repo = new PgVectorRepository(scope, embedder, counting);
        probe = new ArmProbeDataSource(ds);
        probeScope = new TenantScope(probe.dataSource());
        probeRepo = new PgVectorRepository(probeScope, embedder, counting);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(PROSE, CODE, NOCENT, LOWCOV, EMPTY, FALLBACK, CODEC, ALMOST)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }

        // PROSE and CODE: two topics each. TA's centroid is the nearest to the query and its chunks the farthest.
        for (String c : List.of(PROSE, CODE, CODEC)) {
            List<String> ta = seed(c, "ta", 6, 0.30, 0.01);
            List<String> tb = seed(c, "tb", 6, 0.05, 0.01);
            taxonomy(c, List.of(new Topic("TA", 0.10, ta), new Topic("TB", 1.00, tb)));
        }
        // NOCENT: chunks, no topic and no centroid.
        seed(NOCENT, "n", 6, 0.20, 0.01);
        // LOWCOV: a topic and a centroid; only 5 of 10 chunks are assigned (coverage 0.5, below 0.9).
        List<String> low = seed(LOWCOV, "l", 10, 0.20, 0.01);
        taxonomy(LOWCOV, List.of(new Topic("TL", 0.10, low.subList(0, 5))));
        // ALMOST: one topic, 19 of 20 chunks assigned (coverage 0.95).
        List<String> almost = seed(ALMOST, "a", 20, 0.20, 0.01);
        taxonomy(ALMOST, List.of(new Topic("TAL", 0.10, almost.subList(0, 19))));
        // EMPTY: an orphaned taxonomy, a topic and a centroid and no chunks.
        taxonomy(EMPTY, List.of(new Topic("TO", 0.10, List.of())));
        // FALLBACK: full coverage, but the nearest centroid (TE) has no chunk assigned; every chunk is TF's.
        List<String> fb = seed(FALLBACK, "f", 8, 0.40, 0.01);
        taxonomy(FALLBACK, List.of(new Topic("TE", 0.05, List.of()), new Topic("TF", 1.20, fb)));
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String textOf(String collection, String key) {
        return collection + "|" + key;
    }

    /** Chunks {@code prefix0 ..} on a ray at {@code angle0 + i * step} radians from the query; returns their chashes. */
    private List<String> seed(String collection, String prefix, int count, double angle0, double step) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
            String text = textOf(collection, prefix + i);
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of("kind", prefix, "title", "title of " + prefix + i));
        }
        repo.upsertChunks(TENANT, collection, ids, texts, metas);
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, ids.toArray(new String[0]));
            return null;
        });
        return ids;
    }

    private void taxonomy(String collection, List<Topic> topics) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, collection);
            for (Topic t : topics) {
                long id = dsl.insertInto(TOPICS)
                    .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, t.label() + " of " + collection)
                    .set(TOPICS.COLLECTION, collection)
                    .set(TOPICS.COLLECTION_ID, CollectionIds.idOf(TENANT, collection)).set(TOPICS.CREATED_AT, SEED_TS)
                    .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
                dsl.insertInto(TAXONOMY_CENTROIDS)
                    .set(TAXONOMY_CENTROIDS.TENANT_ID, TENANT).set(TAXONOMY_CENTROIDS.COLLECTION, collection)
                    .set(TAXONOMY_CENTROIDS.COLLECTION_ID, CollectionIds.idOf(TENANT, collection))
                    .set(TAXONOMY_CENTROIDS.TOPIC_ID, id).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, model)
                    .set(TAXONOMY_CENTROIDS.LABEL, t.label())
                    .set(VectorSeeds.centroidEmbedding(384),
                         VectorSeeds.vecXY(384, (float) Math.cos(t.centroidAngle()), (float) Math.sin(t.centroidAngle())))
                    .execute();
                for (String hex : t.assigned()) {
                    dsl.insertInto(TOPIC_ASSIGNMENTS)
                        .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT)
                        .set(TOPIC_ASSIGNMENTS.DOC_ID, HexFormat.of().parseHex(hex))
                        .set(TOPIC_ASSIGNMENTS.TOPIC_ID, id).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, collection)
                        .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, CollectionIds.idOf(TENANT, collection))
                        .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
                        .execute();
                }
            }
        }
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    /** A route over {@code collections} (null: every collection of the request) with a cache of its own. */
    private TopicRoute route(TopicRoute.Mode mode, double width, Set<String> collections) {
        TaxonomyCoverage coverage = new TaxonomyCoverage(probeScope);
        return new TopicRoute(mode, width, collections, coverage::lookup, false);
    }

    private TopicRoute top1(String... collections) {
        return route(TopicRoute.Mode.TOP1, 0.0, Set.of(collections));
    }

    private PerCollectionResult run(List<String> collections, TopicRoute route) {
        return run(collections, route, null, null, null);
    }

    private PerCollectionResult run(List<String> collections, TopicRoute route, Map<String, Double> thresholds,
                                    Map<String, Object> where, byte[][] chashAllow) {
        return probeRepo.searchPerCollection(TENANT, QUERY, collections, K, 1200, thresholds, where, false, false, false,
                                        chashAllow, false, route);
    }

    private static PerCollectionStat stat(PerCollectionResult r, String collection) {
        return r.perCollection().stream().filter(s -> s.collection().equals(collection)).findFirst().orElseThrow();
    }

    private static List<Map<String, Object>> rowsOf(PerCollectionResult r, String collection) {
        return r.rows().stream().filter(x -> collection.equals(x.get("collection"))).toList();
    }

    private static List<String> idsOf(List<Map<String, Object>> rows) {
        return rows.stream().map(x -> (String) x.get("id")).toList();
    }

    private static List<String> hashes(String collection, String prefix, int from, int to) {
        List<String> out = new ArrayList<>();
        for (int i = from; i < to; i++) out.add(chash(textOf(collection, prefix + i)));
        return out;
    }

    /**
     * Run {@code body} with every arm statement that calls {@code nexus.<function>} failing as an undefined function
     * (SQLSTATE 42883: not transient, not a timeout), as the database would answer it. The statement is failed at the
     * arm's connection ({@link ArmProbeDataSource#failSqlWhen}), so jOOQ wraps the error as it wraps a real one; the
     * route's own scopes are the probe's, which is why {@link #run} and {@link #route} use {@code probeRepo} and
     * {@code probeScope}.
     */
    private void withFunctionMissing(String function, Runnable body) {
        probe.reset();
        probe.failSqlWhen = sql -> sql.contains(function);
        probe.failure = () -> new SQLException("function nexus." + function + " does not exist (test)", "42883");
        try {
            body.run();
        } finally {
            probe.reset();
        }
    }

    @BeforeEach
    @AfterEach
    void resetTheProbe() {
        probe.reset();
    }

    // ── routed ────────────────────────────────────────────────────────────────

    @Test
    void routedTop1_searchesOnlyTheTopicWhoseCentroidIsNearest() {
        PerCollectionResult r = run(List.of(PROSE), top1(PROSE));
        assertThat(stat(r, PROSE).routeStatus()).isEqualTo("routed");
        assertThat(idsOf(r.rows())).as("TA's chunks, though TB's are nearer the query")
            .containsExactlyElementsOf(hashes(PROSE, "ta", 0, 5));
        assertThat(stat(r, PROSE).rawCount()).isEqualTo(K);

        PerCollectionResult plain = run(List.of(PROSE), null);
        assertThat(stat(plain, PROSE).routeStatus()).isNull();
        assertThat(idsOf(plain.rows())).as("non-vacuity: unscoped, the same query returns TB's chunks")
            .containsExactlyElementsOf(hashes(PROSE, "tb", 0, 5));
    }

    @Test
    void aRoutedRowCarriesWhatAnUnscopedRowCarries() {
        PerCollectionResult routed = run(List.of(PROSE), top1(PROSE));
        // every chunk of the collection, unscoped: k large enough to return TA's chunks too
        PerCollectionResult all = repo.searchPerCollection(TENANT, QUERY, List.of(PROSE), 12, 1200, null, null, false,
                                                           false, false, null, false, null);
        String id = chash(textOf(PROSE, "ta0"));
        Map<String, Object> routedRow = routed.rows().stream().filter(x -> id.equals(x.get("id"))).findFirst().orElseThrow();
        Map<String, Object> plainRow = all.rows().stream().filter(x -> id.equals(x.get("id"))).findFirst().orElseThrow();
        assertThat(plainRow).as("non-vacuity: the unscoped row has the stored metadata and the address fields")
            .containsKeys("kind", "title", "chash", "span", "retention");
        assertThat(routedRow).as("the routed row is the unscoped row, key for key and value for value")
            .isEqualTo(plainRow);
        assertThat(new ArrayList<>(routedRow.keySet())).as("in the same key order")
            .isEqualTo(new ArrayList<>(plainRow.keySet()));
    }

    @Test
    void band_keepsTheTopicsWithinTheWidthOfTheNearestOne() {
        // sim(TA) = cos 0.10 = 0.995, sim(TB) = cos 1.00 = 0.540: they differ by 0.455
        PerCollectionResult narrow = run(List.of(PROSE), route(TopicRoute.Mode.BAND, 0.10, Set.of(PROSE)));
        assertThat(stat(narrow, PROSE).routeStatus()).isEqualTo("routed");
        assertThat(idsOf(narrow.rows())).as("a band of 0.10 keeps only TA")
            .containsExactlyElementsOf(hashes(PROSE, "ta", 0, 5));

        PerCollectionResult wide = run(List.of(PROSE), route(TopicRoute.Mode.BAND, 0.60, Set.of(PROSE)));
        assertThat(stat(wide, PROSE).routeStatus()).isEqualTo("routed");
        assertThat(idsOf(wide.rows())).as("a band of 0.60 keeps TA and TB: TB's chunks are nearer")
            .containsExactlyElementsOf(hashes(PROSE, "tb", 0, 5));

        PerCollectionResult edge = run(List.of(PROSE), route(TopicRoute.Mode.BAND, 0.45, Set.of(PROSE)));
        assertThat(idsOf(edge.rows())).as("0.455 is outside a band of 0.45")
            .containsExactlyElementsOf(hashes(PROSE, "ta", 0, 5));
    }

    @Test
    void theThresholdAppliesToRoutedRowsAsToUnscopedOnes() {
        // TA's chunks sit at 0.30, 0.31, ... rad: distances 1 - cos(angle) = 0.0447, 0.0475, ...
        PerCollectionResult r = run(List.of(PROSE), top1(PROSE), Map.of(PROSE, 0.046), null, null);
        PerCollectionStat s = stat(r, PROSE);
        assertThat(s.routeStatus()).isEqualTo("routed");
        assertThat(s.rawCount()).isEqualTo(K);
        assertThat(s.dropped()).as("ta0 is under 0.046, ta1..ta4 are over").isEqualTo(4);
        assertThat(idsOf(r.rows())).containsExactly(chash(textOf(PROSE, "ta0")));
    }

    @Test
    void aThresholdThatDropsEveryRoutedRow_leavesTheCollectionRouted_withNoRows_andDroppedEqualsRawCount() {
        // critique S2, kept: the zero-row fallback is judged on the arm's top-k BEFORE the client threshold. TA's rows
        // sit at distances 0.0447 and up; TB's, the unscoped top-5, at 0.00125 and up. A threshold of 0.002 drops
        // every routed row and passes TB's first two.
        Map<String, Double> thresholds = Map.of(PROSE, 0.002);
        PerCollectionResult r = run(List.of(PROSE), top1(PROSE), thresholds, null, null);
        PerCollectionStat s = stat(r, PROSE);
        assertThat(s.routeStatus()).as("rows came back before the threshold, so not a fallback").isEqualTo("routed");
        assertThat(s.rawCount()).isEqualTo(K);
        assertThat(s.dropped()).as("every routed row is over the threshold").isEqualTo(s.rawCount());
        assertThat(s.minDroppedDistance()).isEqualTo(s.minRawDistance());
        assertThat(r.rows()).as("the collection contributes nothing").isEmpty();

        PerCollectionResult unrouted = run(List.of(PROSE), null, thresholds, null, null);
        assertThat(rowsOf(unrouted, PROSE)).as("non-vacuity: the unscoped search WOULD have passed rows at this threshold")
            .isNotEmpty();
    }

    // ── min_coverage (critique C1) ────────────────────────────────────────────

    private TopicRoute routeMin(double minCoverage, String... collections) {
        TaxonomyCoverage coverage = new TaxonomyCoverage(probeScope);
        return new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(collections), coverage::lookup, false, minCoverage);
    }

    @Test
    void minCoverageZero_routesALowCoverageCollection_theDefaultDoesNot() {
        // LOWCOV: 5 of 10 chunks assigned, coverage 0.5
        assertThat(stat(run(List.of(LOWCOV), top1(LOWCOV)), LOWCOV).routeStatus())
            .as("the default 0.9 refuses it").isEqualTo("no_topics");
        PerCollectionResult zero = run(List.of(LOWCOV), routeMin(0.0, LOWCOV));
        assertThat(stat(zero, LOWCOV).routeStatus()).as("0 routes any collection that has centroids and chunks")
            .isEqualTo("routed");
        assertThat(stat(zero, LOWCOV).routeCoverage()).isEqualTo(0.5);
        assertThat(idsOf(rowsOf(zero, LOWCOV))).as("TL's assigned chunks, which are also the five nearest")
            .containsExactlyElementsOf(hashes(LOWCOV, "l", 0, 5));
        // the boundary is inclusive and is the value, not a constant
        assertThat(stat(run(List.of(LOWCOV), routeMin(0.5, LOWCOV)), LOWCOV).routeStatus()).isEqualTo("routed");
        assertThat(stat(run(List.of(LOWCOV), routeMin(0.51, LOWCOV)), LOWCOV).routeStatus()).isEqualTo("no_topics");
        // zero does not make an empty or centroid-less collection routable
        PerCollectionResult unroutable = run(List.of(NOCENT, EMPTY), routeMin(0.0, NOCENT, EMPTY));
        assertThat(stat(unroutable, NOCENT).routeStatus()).isEqualTo("no_topics");
        assertThat(stat(unroutable, EMPTY).routeStatus()).isEqualTo("no_topics");
    }

    @Test
    void minCoverageNinetyPercent_isTheDefault_andOneNeedsFullCoverage() {
        // ALMOST: 19 of 20 assigned, coverage 0.95
        PerCollectionResult dflt = run(List.of(ALMOST), top1(ALMOST));
        assertThat(stat(dflt, ALMOST).routeStatus()).as("0.95 clears the default 0.9").isEqualTo("routed");
        assertThat(stat(run(List.of(ALMOST), routeMin(0.9, ALMOST)), ALMOST).routeStatus()).isEqualTo("routed");
        assertThat(stat(run(List.of(ALMOST), routeMin(1.0, ALMOST)), ALMOST).routeStatus())
            .as("1.0 needs every chunk assigned: 0.95 is refused").isEqualTo("no_topics");
        assertThat(stat(run(List.of(PROSE), routeMin(1.0, PROSE)), PROSE).routeStatus())
            .as("a fully covered collection clears 1.0").isEqualTo("routed");
        assertThat(TopicRoute.DEFAULT_MIN_COVERAGE).isEqualTo(0.9);
        assertThat(top1(PROSE).minCoverage()).as("a route built without the knob carries the default").isEqualTo(0.9);
    }

    @Test
    void minCoverage_outsideZeroToOne_isRefusedWhenTheRouteIsBuilt() {
        for (double bad : new double[] {-0.001, 1.001, Double.NaN, Double.POSITIVE_INFINITY}) {
            assertThatThrownBy(() -> routeMin(bad, PROSE)).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("min_coverage");
        }
    }

    // ── code collections are never routed (code review I1) ────────────────────

    @Test
    void aCodeCollection_isNeverRouted_evenWhenItHasCentroidsAndFullCoverage_andNoListIsGiven() {
        // non-vacuity: the coverage reading of CODEC says it is routable, and a knowledge__ collection of the same shape
        // IS routed by the same route (withNoCollectionsListed_everyCollectionOfTheRequestIsRouted)
        var counts = new TaxonomyCoverage(probeScope).lookup(TENANT, CODEC).orElseThrow();
        assertThat(counts.routable()).as("CODEC has centroids and full coverage").isTrue();

        List<String> cols = List.of(PROSE, CODEC);
        PerCollectionResult r = run(cols, route(TopicRoute.Mode.TOP1, 0.0, null));
        PerCollectionResult plain = run(cols, null);
        assertThat(stat(r, PROSE).routeStatus()).isEqualTo("routed");
        assertThat(stat(r, CODEC).routeStatus()).isEqualTo("skipped");
        assertThat(stat(r, CODEC).routeCoverage()).as("a skipped collection reads nothing").isNull();
        assertThat(stat(r, CODEC).routeTopics()).isNull();
        assertThat(idsOf(rowsOf(plain, CODEC))).as("non-vacuity: unscoped, TB's chunks are the nearest")
            .containsExactlyElementsOf(hashes(CODEC, "tb", 0, 5));
        assertThat(idsOf(rowsOf(r, CODEC))).as("CODEC is searched unscoped: TB's chunks, not the routed TA's")
            .containsExactlyElementsOf(hashes(CODEC, "tb", 0, 5));
        assertThat(rowsOf(r, CODEC)).isEqualTo(rowsOf(plain, CODEC));
    }

    @Test
    void aCodeCollectionNamedInTheRoutesList_isSkipped_notRouted_andNot400() {
        // Decision (code review I1): a code collection named in topic_route.collections is SKIPPED, not a 400. The engine
        // knows a collection is code from the registry's content type; parsing the request has no registry to ask.
        List<String> cols = List.of(PROSE, CODEC);
        PerCollectionResult r = run(cols, top1(PROSE, CODEC));
        PerCollectionResult plain = run(cols, null);
        assertThat(stat(r, PROSE).routeStatus()).isEqualTo("routed");
        assertThat(stat(r, CODEC).routeStatus()).as("named, full coverage, centroids, and still skipped").isEqualTo("skipped");
        assertThat(rowsOf(r, CODEC)).isEqualTo(rowsOf(plain, CODEC));
        assertThat(idsOf(rowsOf(r, CODEC))).containsExactlyElementsOf(hashes(CODEC, "tb", 0, 5));

        // the registry decides, not the name: a knowledge__ collection whose name says "code" is routed
        PerCollectionResult byName = run(List.of(CODE), top1(CODE));
        assertThat(stat(byName, CODE).routeStatus()).isEqualTo("routed");
    }

    @Test
    void parse_stillTakesACodeLookingName_theGuardIsTheRepositorys() {
        TopicRoute.CoverageLookup none = (t, c, ms) -> Optional.empty();
        TopicRoute parsed = TopicRoute.parse(Map.of("mode", "top1", "collections", List.of(CODEC)), List.of(PROSE, CODEC), none);
        assertThat(parsed.collections()).containsExactly(CODEC);
    }

    @Test
    void parse_minCoverage_defaultsTo90Percent_andRefusesWhatIsNotANumberFromZeroToOne() {
        List<String> request = List.of(PROSE);
        TopicRoute.CoverageLookup none = (t, c, ms) -> Optional.empty();
        assertThat(TopicRoute.parse(Map.of("mode", "top1"), request, none).minCoverage()).isEqualTo(0.9);
        assertThat(TopicRoute.parse(Map.of("mode", "top1", "min_coverage", 0), request, none).minCoverage()).isEqualTo(0.0);
        assertThat(TopicRoute.parse(Map.of("mode", "top1", "min_coverage", 1), request, none).minCoverage()).isEqualTo(1.0);
        assertThat(TopicRoute.parse(Map.of("mode", "top1", "min_coverage", 0.25), request, none).minCoverage())
            .isEqualTo(0.25);
        for (Object bad : new Object[] {-0.1, 1.1, 2, "0.5", true, List.of(), Map.of()}) {
            assertThatThrownBy(() -> TopicRoute.parse(Map.of("mode", "top1", "min_coverage", bad), request, none))
                .as("min_coverage %s", bad).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("min_coverage");
        }
        assertThat(TopicRoute.parse(Map.of("mode", "top1", "min_coverage", 0.5), request, none).skipRequest().minCoverage())
            .as("skipRequest keeps it").isEqualTo(0.5);
    }

    // ── no_topics ─────────────────────────────────────────────────────────────

    @Test
    void noTopics_forACollectionWithNoCentroids_aLowCoverageOne_andAnEmptyOne() {
        List<String> cols = List.of(NOCENT, LOWCOV, EMPTY);
        PerCollectionResult routed = run(cols, top1(NOCENT, LOWCOV, EMPTY));
        PerCollectionResult plain = run(cols, null);
        for (String c : cols) {
            assertThat(stat(routed, c).routeStatus()).as(c).isEqualTo("no_topics");
        }
        assertThat(rowsOf(plain, NOCENT)).as("non-vacuity: NOCENT has rows").hasSize(K);
        assertThat(rowsOf(plain, LOWCOV)).as("non-vacuity: LOWCOV has rows").hasSize(K);
        assertThat(rowsOf(plain, EMPTY)).as("EMPTY has none").isEmpty();
        assertThat(routed.rows()).as("each is searched unscoped: the rows of an unrouted request")
            .isEqualTo(plain.rows());
    }

    // ── fallback ──────────────────────────────────────────────────────────────

    @Test
    void fallback_whenTheKeptTopicsReturnNothing_theCollectionIsSearchedUnscoped() {
        PerCollectionResult routed = run(List.of(FALLBACK), top1(FALLBACK));
        PerCollectionResult plain = run(List.of(FALLBACK), null);
        assertThat(stat(routed, FALLBACK).routeStatus()).isEqualTo("fallback");
        assertThat(rowsOf(plain, FALLBACK)).as("non-vacuity").hasSize(K);
        assertThat(routed.rows()).isEqualTo(plain.rows());
        assertThat(stat(routed, FALLBACK).rawCount()).isEqualTo(K);
    }

    // ── skipped ───────────────────────────────────────────────────────────────

    @Test
    void aChashAllowRequestIsNotRouted_everyCollectionIsSkipped() {
        byte[][] allow = hashes(PROSE, "ta", 0, 3).stream().map(h -> HexFormat.of().parseHex(h)).toArray(byte[][]::new);
        List<String> cols = List.of(PROSE, CODE);
        PerCollectionResult routed = run(cols, top1(PROSE, CODE), null, null, allow);
        PerCollectionResult plain = run(cols, null, null, null, allow);
        assertThat(routed.perCollection()).extracting(PerCollectionStat::routeStatus).containsExactly("skipped", "skipped");
        assertThat(plain.rows()).as("non-vacuity: the allowed chunks come back").isNotEmpty();
        assertThat(routed.rows()).isEqualTo(plain.rows());
    }

    @Test
    void aMetadataWhereIsNotRouted_everyCollectionIsSkipped() {
        List<String> cols = List.of(PROSE, CODE);
        Map<String, Object> where = Map.of("kind", "ta");
        PerCollectionResult routed = run(cols, top1(PROSE, CODE), null, where, null);
        PerCollectionResult plain = run(cols, null, null, where, null);
        assertThat(routed.perCollection()).extracting(PerCollectionStat::routeStatus).containsExactly("skipped", "skipped");
        assertThat(plain.rows()).as("non-vacuity").isNotEmpty();
        assertThat(routed.rows()).isEqualTo(plain.rows());
    }

    @Test
    void aMixedGroup_aCollectionNotListedIsSearchedUnscopedWithStatusSkipped() {
        List<String> cols = List.of(PROSE, CODE);
        PerCollectionResult mixed = run(cols, top1(PROSE));
        PerCollectionResult plain = run(cols, null);
        assertThat(stat(mixed, PROSE).routeStatus()).isEqualTo("routed");
        assertThat(stat(mixed, CODE).routeStatus()).isEqualTo("skipped");
        assertThat(idsOf(rowsOf(mixed, PROSE))).as("PROSE is routed").containsExactlyElementsOf(hashes(PROSE, "ta", 0, 5));
        assertThat(rowsOf(plain, CODE)).as("non-vacuity: CODE has a taxonomy it could have been routed on").hasSize(K);
        assertThat(rowsOf(mixed, CODE)).as("CODE's rows are exactly those of an unrouted request")
            .isEqualTo(rowsOf(plain, CODE));
        PerCollectionStat code = stat(mixed, CODE);
        PerCollectionStat codePlain = stat(plain, CODE);
        assertThat(code.rawCount()).isEqualTo(codePlain.rawCount());
        assertThat(code.minRawDistance()).isEqualTo(codePlain.minRawDistance());
    }

    @Test
    void withNoCollectionsListed_everyCollectionOfTheRequestIsRouted() {
        PerCollectionResult r = run(List.of(PROSE, CODE), route(TopicRoute.Mode.TOP1, 0.0, null));
        assertThat(r.perCollection()).extracting(PerCollectionStat::routeStatus).containsExactly("routed", "routed");
        assertThat(idsOf(rowsOf(r, PROSE))).containsExactlyElementsOf(hashes(PROSE, "ta", 0, 5));
        assertThat(idsOf(rowsOf(r, CODE))).containsExactlyElementsOf(hashes(CODE, "ta", 0, 5));
    }

    @Test
    void aPresentButEmptyCollectionsList_changesNothingAndReportsNoStatus() {
        List<String> cols = List.of(PROSE, CODE);
        PerCollectionResult empty = run(cols, route(TopicRoute.Mode.TOP1, 0.0, Set.of()));
        PerCollectionResult plain = run(cols, null);
        assertThat(empty.perCollection()).as("no status on any collection, so no key on the wire")
            .extracting(PerCollectionStat::routeStatus).containsOnlyNulls();
        assertThat(empty.rows()).isEqualTo(plain.rows());
        assertThat(empty.perCollection()).isEqualTo(plain.perCollection());
    }

    // ── one embedding ─────────────────────────────────────────────────────────

    @Test
    void theQueryIsEmbeddedOnce_whateverTheRoutesDo() {
        // routed, fallback, no_topics (twice) and an unscoped fallback re-run in one request
        List<String> cols = List.of(PROSE, FALLBACK, NOCENT, LOWCOV);
        counting.calls.set(0);
        PerCollectionResult r = run(cols, top1(PROSE, FALLBACK, NOCENT, LOWCOV));
        assertThat(r.perCollection()).extracting(PerCollectionStat::routeStatus)
            .containsExactly("routed", "fallback", "no_topics", "no_topics");
        assertThat(counting.calls.get()).as("the request embeds its query once, and the route reuses the vector")
            .isEqualTo(1);
    }

    // ── failure modes the RDR omits ───────────────────────────────────────────

    @Test
    void anErroringTopicSearch_servesTheCollectionUnscoped_asFallback_andFailsNothing() throws Exception {
        PerCollectionResult plain = run(List.of(PROSE, CODE), null);
        PerCollectionResult[] routed = new PerCollectionResult[1];
        withFunctionMissing("search_topic_set_384", () -> routed[0] = run(List.of(PROSE, CODE), top1(PROSE)));
        assertThat(stat(routed[0], PROSE).routeStatus()).as("a topic search that erred is a fallback").isEqualTo("fallback");
        assertThat(stat(routed[0], PROSE).error()).as("the collection is served, not failed").isNull();
        assertThat(routed[0].rows()).as("every collection is served as an unrouted request serves it")
            .isEqualTo(plain.rows());
    }

    @Test
    void aFailedCoverageLookup_servesTheCollectionUnscoped_asNoTopics_andFailsNothing() throws Exception {
        PerCollectionResult plain = run(List.of(PROSE, CODE), null);
        PerCollectionResult[] routed = new PerCollectionResult[1];
        withFunctionMissing("taxonomy_coverage_counts_384", () -> routed[0] = run(List.of(PROSE, CODE), top1(PROSE)));
        assertThat(stat(routed[0], PROSE).routeStatus()).as("no coverage reading, so no topics to route on")
            .isEqualTo("no_topics");
        assertThat(stat(routed[0], PROSE).error()).isNull();
        assertThat(routed[0].rows()).isEqualTo(plain.rows());
    }

    @Test
    void aFailedCentroidRanking_servesTheCollectionUnscoped_asNoTopics_andFailsNothing() throws Exception {
        PerCollectionResult plain = run(List.of(PROSE, CODE), null);
        PerCollectionResult[] routed = new PerCollectionResult[1];
        withFunctionMissing("taxonomy_ann_query_384", () -> routed[0] = run(List.of(PROSE, CODE), top1(PROSE)));
        assertThat(stat(routed[0], PROSE).routeStatus()).isEqualTo("no_topics");
        assertThat(stat(routed[0], PROSE).error()).isNull();
        assertThat(routed[0].rows()).isEqualTo(plain.rows());
    }

    @Test
    void aCoverageLookupThatAnswersNothing_isNoTopics() {
        // the seam a caller wires in: a lookup that has no reading (TaxonomyCoverage answers empty on any failure)
        TopicRoute none = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                         (tenant, collection, ms) -> Optional.empty(), false);
        PerCollectionResult r = run(List.of(PROSE), none);
        assertThat(stat(r, PROSE).routeStatus()).isEqualTo("no_topics");
        assertThat(idsOf(r.rows())).containsExactlyElementsOf(hashes(PROSE, "tb", 0, 5));
    }

    @Test
    void aCoverageLookupThatThrows_isNoTopics_notARequestFailure() {
        TopicRoute boom = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                         (tenant, collection, ms) -> { throw new IllegalStateException("boom"); }, false);
        PerCollectionResult r = run(List.of(PROSE), boom);
        assertThat(stat(r, PROSE).routeStatus()).isEqualTo("no_topics");
        assertThat(stat(r, PROSE).error()).isNull();
    }

    // ── what the route must not degrade (SB2) ─────────────────────────────────

    private TopicRoute seam(TopicRoute.CoverageLookup lookup) {
        return new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE), lookup, false);
    }

    @Test
    void aCoverageLookupThatFailsWithTheRequestDeadline_failsTheRequest_itIsNotDegraded() {
        TopicRoute deadline = seam((t, c, ms) -> {
            throw new RequestDeadlineExceededException("request budget spent (test)", 1);
        });
        assertThatThrownBy(() -> run(List.of(PROSE, CODE), deadline)).isInstanceOf(RequestDeadlineExceededException.class);
    }

    @Test
    void aCoverageLookupThatFailsWithPoolExhaustion_failsTheRequest_itIsNotDegraded() {
        TopicRoute pool = seam((t, c, ms) -> {
            throw new RuntimeException(new SQLTransientConnectionException("pool exhausted (test)"));
        });
        assertThatThrownBy(() -> run(List.of(PROSE, CODE), pool))
            .hasRootCauseInstanceOf(SQLTransientConnectionException.class);
    }

    @Test
    void aCoverageLookupThatFailsWithATransientSqlState_failsTheRequest_itIsNotDegraded() {
        TopicRoute serialization = seam((t, c, ms) -> {
            throw new RuntimeException(new SQLException("could not serialize access (test)", "40001"));
        });
        assertThatThrownBy(() -> run(List.of(PROSE, CODE), serialization))
            .isInstanceOf(SearchFanoutTransientException.class)
            .extracting(e -> ((SearchFanoutTransientException) e).sqlState()).isEqualTo("40001");
    }

    @Test
    void aFanoutBudgetSpentBetweenStages_isIsolatedToTheCollection_notDegradedAndNotARequestFailure() {
        // stage 1 returns a routable reading but outlasts the 300 ms fan-out budget; stage 2's budget check then fires
        var routable = Optional.of(new TaxonomyCoverage.Counts(true, 12, 0));
        TopicRoute slow = seam((t, c, ms) -> {
            try {
                Thread.sleep(450);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return routable;
        });
        PerCollectionResult r = probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE, CODE), K, 1200, null, null,
            false, false, false, new FanoutSettings(1, 300, 30_000), null, false, slow);
        PerCollectionStat prose = stat(r, PROSE);
        assertThat(prose.errorKind()).as("isolated as the fan-out budget, as an unrouted arm's would be")
            .isEqualTo(ArmErrorKind.FANOUT_BUDGET_EXHAUSTED);
        assertThat(prose.routeStatus()).as("an arm that ended with an error is never reported skipped").isNull();
        assertThat(rowsOf(r, PROSE)).isEmpty();
    }

    @Test
    void aTopicSearchCutByTheSearchBound_isAFallback_servedUnscoped_withAShortError() {
        PerCollectionResult plain = run(List.of(PROSE, CODE), null);
        probe.reset();
        probe.failSqlWhen = sql -> sql.contains("search_topic_set");
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                          new TaxonomyCoverage(probeScope)::lookup, false);
        PerCollectionResult r = probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE, CODE), K, 1200, null, null,
            false, false, false, new FanoutSettings(1, 20_000, 10_000), null, false, route);
        PerCollectionStat prose = stat(r, PROSE);
        assertThat(prose.routeStatus()).isEqualTo("fallback");
        assertThat(prose.routeError()).startsWith("topic search failed").contains("SQLSTATE 57014").doesNotContain("select");
        assertThat(prose.error()).as("served, not failed").isNull();
        assertThat(r.rows()).as("served as an unrouted request serves it").isEqualTo(plain.rows());
    }

    @Test
    void aTopicSearchCutByTheRequestBound_failsTheRequest() {
        probe.reset();
        probe.failSqlWhen = sql -> sql.contains("search_topic_set");
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                          new TaxonomyCoverage(probeScope)::lookup, false);
        RequestContext.setDeadlineNanos(System.nanoTime() + 5_000_000_000L);
        try {
            assertThatThrownBy(() -> probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE, CODE), K, 1200, null,
                null, false, false, false, new FanoutSettings(1, 20_000, 30_000), null, false, route))
                .as("a 57014 under the request's bound is the request's deadline")
                .isInstanceOf(RequestDeadlineExceededException.class);
        } finally {
            RequestContext.clearDeadline();
        }
    }

    @Test
    void aTopicSearchCutByTheFanoutBound_isIsolatedToTheCollection_andReportsNoStatus() {
        probe.reset();
        probe.failSqlWhen = sql -> sql.contains("search_topic_set");
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                          new TaxonomyCoverage(probeScope)::lookup, false);
        // the fan-out budget (4 s) is nearer than the search bound (30 s): the statement's limiter is the fan-out
        PerCollectionResult r = probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE, CODE), K, 1200, null, null,
            false, false, false, new FanoutSettings(1, 4_000, 30_000), null, false, route);
        PerCollectionStat prose = stat(r, PROSE);
        assertThat(prose.errorKind()).isEqualTo(ArmErrorKind.FANOUT_BUDGET_EXHAUSTED);
        assertThat(prose.routeStatus()).as("a listed collection whose arm errored is never reported skipped").isNull();
        assertThat(rowsOf(r, PROSE)).as("isolated, not retried unscoped").isEmpty();
        assertThat(stat(r, CODE).error()).isNull();
        assertThat(stat(r, CODE).routeStatus()).as("the other collection is served and reads skipped").isEqualTo("skipped");
        assertThat(rowsOf(r, CODE)).hasSize(K);
    }

    // ── the routing reads are bounded by the arm's budget, and degrade (S3, I1) ──

    @Test
    void theRoutingReadsAndTheMetadataReadAreBounded_notRunAtTheirOwnDefaults() {
        probe.reset();
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                          new TaxonomyCoverage(probeScope)::lookup, false);
        PerCollectionResult r = probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE), K, 1200, null, null, false,
            false, false, new FanoutSettings(1, 4_000, 7_000), null, false, route);
        assertThat(stat(r, PROSE).routeStatus()).isEqualTo("routed");
        // in order: the cold coverage count, the centroid ranking, the topic search, the metadata read
        assertThat(probe.statementTimeouts).as("one bound per statement of the attempt and the metadata read").hasSize(4);
        assertThat(probe.statementTimeouts.get(0)).as("coverage count: at most half the 4 s fan-out budget")
            .isLessThanOrEqualTo(2_000);
        assertThat(probe.statementTimeouts.get(1)).as("centroid ranking: at most half of what was left")
            .isLessThanOrEqualTo(2_000);
        assertThat(probe.statementTimeouts.get(2)).as("topic search: the arm's whole bound, the fan-out's 4 s")
            .isBetween(1, 4_000);
        assertThat(probe.statementTimeouts.get(3)).as("metadata read: the search bound, not unbounded").isEqualTo(7_000);
    }

    @Test
    void theMetadataReadIsBoundedByTheRequestBudgetToo() {
        probe.reset();
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                          new TaxonomyCoverage(probeScope)::lookup, false);
        RequestContext.setDeadlineNanos(System.nanoTime() + 5_000_000_000L);
        try {
            probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE), K, 1200, null, null, false, false, false,
                new FanoutSettings(1, 20_000, 30_000), null, false, route);
        } finally {
            RequestContext.clearDeadline();
        }
        assertThat(probe.statementTimeouts).hasSize(4);
        assertThat(probe.statementTimeouts.get(3)).isLessThanOrEqualTo(5_000);
    }

    @Test
    void aCoverageCountCutByItsBound_degradesToNoTopics_itDoesNotBecomeABudgetError() {
        PerCollectionResult plain = run(List.of(PROSE, CODE), null);
        probe.reset();
        probe.failSqlWhen = sql -> sql.contains("taxonomy_coverage_counts");
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                          new TaxonomyCoverage(probeScope)::lookup, false);
        // a 4 s fan-out budget makes the arm's limiter the fan-out: an unclamped read cut there would be a budget error
        PerCollectionResult r = probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE, CODE), K, 1200, null, null,
            false, false, false, new FanoutSettings(1, 4_000, 30_000), null, false, route);
        PerCollectionStat prose = stat(r, PROSE);
        assertThat(prose.routeStatus()).isEqualTo("no_topics");
        assertThat(prose.routeError()).isEqualTo("coverage unavailable");
        assertThat(prose.error()).isNull();
        assertThat(r.rows()).as("served as an unrouted request serves it").isEqualTo(plain.rows());
    }

    @Test
    void aCentroidRankingCutByItsBound_degradesToNoTopics_itDoesNotBecomeABudgetError() {
        PerCollectionResult plain = run(List.of(PROSE, CODE), null);
        probe.reset();
        probe.failSqlWhen = sql -> sql.contains("taxonomy_ann_query");
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE),
                                          new TaxonomyCoverage(probeScope)::lookup, false);
        PerCollectionResult r = probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE, CODE), K, 1200, null, null,
            false, false, false, new FanoutSettings(1, 4_000, 30_000), null, false, route);
        PerCollectionStat prose = stat(r, PROSE);
        assertThat(prose.routeStatus()).isEqualTo("no_topics");
        assertThat(prose.routeError()).startsWith("centroid ranking failed").contains("SQLSTATE 57014");
        assertThat(prose.routeCoverage()).as("the coverage was read before the ranking failed").isEqualTo(1.0);
        assertThat(prose.error()).isNull();
        assertThat(r.rows()).isEqualTo(plain.rows());
    }

    // ── the degrade paths carry a short error; coverage and kept topics are reported (S2, S4) ──

    @Test
    void theDegradePathsCarryAShortRouteError_theOtherPathsCarryNone() throws Exception {
        PerCollectionResult[] hidden = new PerCollectionResult[3];
        withFunctionMissing("search_topic_set_384", () -> hidden[0] = run(List.of(PROSE, CODE), top1(PROSE)));
        withFunctionMissing("taxonomy_coverage_counts_384", () -> hidden[1] = run(List.of(PROSE, CODE), top1(PROSE)));
        withFunctionMissing("taxonomy_ann_query_384", () -> hidden[2] = run(List.of(PROSE, CODE), top1(PROSE)));
        assertThat(stat(hidden[0], PROSE).routeError()).startsWith("topic search failed").contains("SQLSTATE 42883");
        assertThat(stat(hidden[1], PROSE).routeError()).isEqualTo("coverage unavailable");
        assertThat(stat(hidden[2], PROSE).routeError()).startsWith("centroid ranking failed").contains("SQLSTATE 42883");
        for (PerCollectionResult r : hidden) {
            assertThat(stat(r, CODE).routeError()).as("a skipped collection has no route error").isNull();
        }
        assertThat(stat(run(List.of(PROSE), top1(PROSE)), PROSE).routeError()).as("routed: none").isNull();
        assertThat(stat(run(List.of(NOCENT), top1(NOCENT)), NOCENT).routeError()).as("no_topics by data: none").isNull();
        assertThat(stat(run(List.of(FALLBACK), top1(FALLBACK)), FALLBACK).routeError()).as("zero rows: none").isNull();
    }

    @Test
    void theCoverageRatioAndTheKeptTopicsAreReportedWhereTheyWereRead() {
        PerCollectionResult r = run(List.of(PROSE, LOWCOV, NOCENT, EMPTY, FALLBACK),
                                    top1(PROSE, LOWCOV, NOCENT, EMPTY, FALLBACK));
        assertThat(stat(r, PROSE).routeCoverage()).isEqualTo(1.0);
        assertThat(stat(r, PROSE).routeTopics()).isEqualTo(1);
        assertThat(stat(r, FALLBACK).routeCoverage()).isEqualTo(1.0);
        assertThat(stat(r, FALLBACK).routeTopics()).isEqualTo(1);
        assertThat(stat(r, LOWCOV).routeCoverage()).as("5 of 10 chunks assigned").isEqualTo(0.5);
        assertThat(stat(r, LOWCOV).routeTopics()).as("not ranked: below the coverage floor").isNull();
        assertThat(stat(r, NOCENT).routeCoverage()).as("no centroids: not measured").isNull();
        assertThat(stat(r, EMPTY).routeCoverage()).as("no chunks: no coverage").isNull();

        PerCollectionResult wide = run(List.of(PROSE), route(TopicRoute.Mode.BAND, 0.60, Set.of(PROSE)));
        assertThat(stat(wide, PROSE).routeTopics()).as("a band of 0.60 keeps TA and TB").isEqualTo(2);

        PerCollectionResult plain = run(List.of(PROSE), null);
        assertThat(stat(plain, PROSE).routeCoverage()).as("no topic_route: no key").isNull();
        assertThat(stat(plain, PROSE).routeTopics()).isNull();
        PerCollectionResult empty = run(List.of(PROSE), route(TopicRoute.Mode.TOP1, 0.0, Set.of()));
        assertThat(stat(empty, PROSE).routeCoverage()).as("an empty collections list: no key").isNull();
        assertThat(stat(empty, PROSE).routeTopics()).isNull();
    }

    // ── the recall canary's ring, and the arm permits (I3, I5) ────────────────

    @Test
    void aRoutedArmDoesNotChooseTheRingArm_itsNearestRowIsOverItsTopicSubsetOnly() {
        // PROSE routed: its nearest row is TA's ta0 (distance 0.0447); FALLBACK unrouted: its nearest is 0.0789.
        // Counted, PROSE would win the ring; left out, the one arm that searched everything does.
        PerCollectionResult r = run(List.of(PROSE, FALLBACK), top1(PROSE));
        assertThat(stat(r, PROSE).routeStatus()).isEqualTo("routed");
        assertThat(stat(r, PROSE).minRawDistance()).isLessThan(stat(r, FALLBACK).minRawDistance());
        assertThat(r.queryVector().collection()).isEqualTo(FALLBACK);

        PerCollectionResult plain = run(List.of(PROSE, FALLBACK), null);
        assertThat(plain.queryVector().collection()).as("non-vacuity: unrouted, PROSE's tb0 is the nearest row")
            .isEqualTo(PROSE);
    }

    @Test
    void routedArmsDrawOnTheSharedArmGate_theirConnectionsNeverExceedIt() {
        try {
            for (int permits : new int[] {2, 1}) {
                probeScope.replaceFanoutArmGateForTests(permits);
                probe.reset();
                probe.holdMs = 60;     // hold each connection open so arms that CAN overlap DO overlap
                TopicRoute route = new TopicRoute(TopicRoute.Mode.TOP1, 0.0, Set.of(PROSE, CODE),
                                                  new TaxonomyCoverage(probeScope)::lookup, false);
                PerCollectionResult r = probeRepo.searchPerCollection(TENANT, QUERY, List.of(PROSE, CODE), K, 1200,
                    null, null, false, false, false, new FanoutSettings(2, 30_000, 30_000), null, false, route);
                assertThat(r.perCollection()).extracting(PerCollectionStat::routeStatus)
                    .containsExactly("routed", "routed");
                assertThat(probe.armBorrows.get()).as("the routed attempts' connections are counted as arm connections")
                    .isGreaterThanOrEqualTo(6);
                assertThat(probe.peak.get()).as("two workers, %d permit(s): the permits bound the simultaneous holders",
                                                permits).isEqualTo(permits);
                assertThat(probe.open.get()).as("every arm connection was returned").isZero();
            }
        } finally {
            probeScope.replaceFanoutArmGateForTests(5);
            probe.reset();
        }
    }
}
