// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PciIndexSet;
import dev.nexus.service.vectors.PciQueryRing;
import dev.nexus.service.vectors.PciQueryRingReader;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import javax.sql.DataSource;
import java.lang.reflect.Proxy;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4lnn1.1.3 (RDR-230 P1.2), admission: which searches put their query vector in which collection's ring, driven
 * through the real HTTP routes ({@code /search} and {@code /search-per-collection}) on a real Postgres. The valid-index
 * set is a test double that the tests edit; the writer is flushed by the test, so the assertions read the table.
 *
 * <p>The rule under test: a vector enters X's ring when the request searched X alone, or when X's arm returned the
 * nearest row among the request's arms; and only for a collection with a valid per-collection index. Nothing is
 * embedded for the ring: the vector written is the one the search used.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PciQueryRingAdmissionIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-ring-adm-0123456789abcdef000000";
    private static final String TENANT = "ring-adm";
    /** Registered at (1, 0): nearest to {@link #NEAR_A}. */
    private static final String NEAR_A = "ring adm query near a";
    /** Registered at angle 0.5: nearest to {@link #B}. */
    private static final String NEAR_B = "ring adm query near b";
    /** Every seeded chunk carries both words, so the hybrid route's text gate passes all of them. */
    private static final String HYB_NEAR_A = "ringprobe";
    private static final String HYB_NEAR_B = "ringprobeb";
    private static final String TOPIC_LABEL = "ring-adm-topic";
    private static final String A = "knowledge__ring-adm-a__minilm-l6-v2-384__v1";
    private static final String B = "knowledge__ring-adm-b__minilm-l6-v2-384__v1";
    private static final String ORPHAN = "knowledge__ring-adm-orphan__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    PgVectorRepository repo;
    NexusService service;
    HttpClient http;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    final Set<String> indexed = ConcurrentHashMap.newKeySet();
    PciQueryRing ring;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "ring-adm");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);

        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(NEAR_A, 1f, 0f);
        embedder.register(NEAR_B, (float) Math.cos(0.5), (float) Math.sin(0.5));
        embedder.register(HYB_NEAR_A, 1f, 0f);
        embedder.register(HYB_NEAR_B, (float) Math.cos(0.5), (float) Math.sin(0.5));
        repo = new PgVectorRepository(scope, embedder, embedder);
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(A, B, ORPHAN)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }
        List<String> chashesA = seed(A, "a", 4, 0.0);
        seed(B, "b", 4, 0.5);
        seed(ORPHAN, "orphan", 1, 0.3);
        seedTopic(A, chashesA);
        // The aspect route joins document_aspects: one row for each collection's owning document.
        AspectRepository aspects = new AspectRepository(scope);
        for (String c : List.of(A, B)) {
            long id = aspects.upsertAspect(TENANT, Map.of(
                "collection", c, "source_path", c + ".md", "extracted_at", "2026-01-01T00:00:00+00:00",
                "model_version", "v1", "extractor_name", "test", "source_uri", "file:///" + c + ".md",
                "confidence", 0.9, "doc_id", "own-" + c, "problem_formulation", "ring probe"));
            assertThat(id).as("aspect seed for %s", c).isPositive();
        }
        // The orphan's registered dimension (768) disagrees with the 384-wide vector its model produces: its arm
        // fails with dimension_mismatch, and a vector from a failed arm must not enter a ring.
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.DIMENSION, 768)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(ORPHAN)))
               .execute();
        }
        dev.nexus.service.db.CollectionRegistry.evict(TENANT, ORPHAN);

        service = new NexusService(0, TOKEN, svcDs, null, repo);
        ring = new PciQueryRing(scope, (model, tenant, collection) -> indexed.contains(collection));
        service.queryRing(ring);
        service.start();
        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() {
        if (ring != null) ring.stop();
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void emptyRings() throws Exception {
        ring.flushNow();   // anything an earlier test left queued
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING)
               .where(dev.nexus.service.jooq.nexus.Tables.CANARY_QUERY_RING.TENANT_ID.eq(TENANT)).execute();
        }
        indexed.clear();
        service.queryRing(ring);
    }

    private List<String> seed(String collection, String prefix, int count, double angle0) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * 0.01;
            String text = "ringprobe ringprobeb " + collection + "|" + prefix + "-" + i;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of());
        }
        repo.upsertChunks(TENANT, collection, ids, texts, metas);
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, ids.toArray(new String[0]));
            return null;
        });
        return ids;
    }

    /** One topic over {@code collection}, assigned to each of its chunks (the topic-scoped route joins on it). */
    private void seedTopic(String collection, List<String> chashes) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var at = java.time.OffsetDateTime.parse("2026-01-01T00:00:00+00:00");
            String model = PgContainerHelper.collectionModel(ctx, TENANT, collection);
            long topic = ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.CREATED_AT)
                .values(TENANT, TOPIC_LABEL, collection, at).returning(TOPICS.ID).fetchOne().getId();
            for (String chash : chashes) {
                ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                        TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT,
                        TOPIC_ASSIGNMENTS.EMBEDDING_MODEL)
                    .values(TENANT, HexFormat.of().parseHex(chash), topic, collection, at, model)
                    .onConflict(TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID, TOPIC_ASSIGNMENTS.TOPIC_ID)
                    .doNothing().execute();
            }
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

    /**
     * The handler admits AFTER it has sent the response, so a test that reads the queue right after the call could
     * read it early (and pass a "nothing was admitted" assertion vacuously). It waits until the ring has finished one
     * more admit call, whatever that call decided.
     */
    private HttpResponse<String> post(String route, Map<String, Object> body) throws Exception {
        long before = ring.admissionsSeen();
        var response = postOnly(route, body);
        if (response.statusCode() == 200) {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (ring.admissionsSeen() == before && System.nanoTime() < deadline) {
                Thread.sleep(5);
            }
            assertThat(ring.admissionsSeen()).as("the handler called admit once").isEqualTo(before + 1);
        }
        return response;
    }

    private HttpResponse<String> postOnly(String route, Map<String, Object> body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors" + route)
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static Map<String, Object> searchBody(String query, String... collections) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("query", query);
        m.put("collections", List.of(collections));
        m.put("n_results", 5);
        return m;
    }

    private static Map<String, Object> fanOutBody(String query, String... collections) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("query", query);
        m.put("collections", List.of(collections));
        m.put("per_collection_k", 5);
        m.put("limit", 20);
        return m;
    }

    private HttpResponse<String> search(String query, String... collections) throws Exception {
        return post("/search", searchBody(query, collections));
    }

    private HttpResponse<String> fanOut(String query, String... collections) throws Exception {
        return post("/search-per-collection", fanOutBody(query, collections));
    }

    private int ringSize(String collection) {
        ring.flushNow();
        return new PciQueryRingReader(scope).load(TENANT, collection).size();
    }

    private float[] ringVector(String collection) {
        ring.flushNow();
        return new PciQueryRingReader(scope).load(TENANT, collection).get(0).vector();
    }

    // -- test 1 -----------------------------------------------------------------------------------------------

    @Test
    void aSingleCollectionSearchAdmitsItsVector_theOneTheSearchUsed() throws Exception {
        indexed.addAll(List.of(A, B));

        assertThat(search(NEAR_A, A).statusCode()).isEqualTo(200);

        assertThat(ringSize(A)).isEqualTo(1);
        assertThat(ringSize(B)).isZero();
        assertThat(ringVector(A)).as("the embedding the search computed, not a second one")
            .containsExactly(embedder.embedOne(NEAR_A));
    }

    @Test
    void aSingleCollectionFanOutRequestAdmitsToItsOnlyArm() throws Exception {
        indexed.addAll(List.of(A, B));

        assertThat(fanOut(NEAR_B, B).statusCode()).isEqualTo(200);

        assertThat(ringSize(B)).isEqualTo(1);
        assertThat(ringSize(A)).isZero();
    }

    @Test
    void aSearchOfACollectionWithoutAValidIndexAdmitsNothing() throws Exception {
        indexed.add(B);

        search(NEAR_A, A);
        fanOut(NEAR_A, A);

        assertThat(ring.queued()).isZero();
        assertThat(ringSize(A)).isZero();
    }

    // -- test 2 -----------------------------------------------------------------------------------------------

    @Test
    void aFanOutAdmitsOnlyTheNearestArm_perCollectionRoute() throws Exception {
        indexed.addAll(List.of(A, B));

        assertThat(fanOut(NEAR_A, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).as("A's arm returned the nearest row").isEqualTo(1);
        assertThat(ringSize(B)).as("B's arm was farther: not admitted").isZero();

        assertThat(fanOut(NEAR_B, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(B)).as("now B is nearest").isEqualTo(1);
        assertThat(ringSize(A)).as("A keeps its one earlier row, gains none").isEqualTo(1);
    }

    @Test
    void aFanOutAdmitsOnlyTheNearestArm_searchRoute() throws Exception {
        indexed.addAll(List.of(A, B));

        assertThat(search(NEAR_A, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).isEqualTo(1);
        assertThat(ringSize(B)).isZero();

        assertThat(search(NEAR_B, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(B)).isEqualTo(1);
        assertThat(ringSize(A)).isEqualTo(1);
    }

    @Test
    void theNearestArmWithoutAValidIndexIsNotReplacedByAFartherOne() throws Exception {
        indexed.add(B);                      // A is nearest to NEAR_A but has no index

        fanOut(NEAR_A, A, B);
        search(NEAR_A, A, B);

        assertThat(ringSize(A)).isZero();
        assertThat(ringSize(B)).as("a farther arm does not inherit the admission").isZero();
    }

    @Test
    void anArmThatFailedAdmitsNothing() throws Exception {
        indexed.addAll(List.of(A, ORPHAN));

        var r = fanOut(NEAR_A, ORPHAN);

        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).contains("dimension_mismatch");
        assertThat(ring.queued()).as("the only arm failed: its vector is not the right width").isZero();
    }

    // -- nexus-4lnn1.1.7: /hybrid-search and the four scoped routes admit by the same rule ----------------------------

    private HttpResponse<String> hybrid(String query, String... collections) throws Exception {
        return post("/hybrid-search", searchBody(query, collections));
    }

    private HttpResponse<String> metadataScoped(String query, String... collections) throws Exception {
        return post("/search-metadata-scoped", searchBody(query, collections));
    }

    private HttpResponse<String> graphHop(String query, String... collections) throws Exception {
        Map<String, Object> body = searchBody(query, collections);
        List<String> seeds = new ArrayList<>();
        for (String c : collections) seeds.add("own-" + c);
        body.put("seeds", seeds);
        return post("/search-graph-hop", body);
    }

    private HttpResponse<String> aspectScoped(String query, String... collections) throws Exception {
        return post("/search-aspect-scoped", searchBody(query, collections));
    }

    private HttpResponse<String> topicScoped(String query, String collection) throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", query);
        body.put("topic", TOPIC_LABEL);
        body.put("collection", collection);
        body.put("n_results", 5);
        return post("/search-topic-scoped", body);
    }

    @Test
    void hybridSearch_aSingleCollectionAdmitsItsVector_aFanOutOnlyTheNearestArm() throws Exception {
        indexed.addAll(List.of(A, B));

        var one = hybrid(HYB_NEAR_B, A);
        assertThat(one.statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).as("a single collection: admitted to it, whatever the rows").isEqualTo(1);
        assertThat(ringVector(A)).as("the embedding the search computed")
            .containsExactly(embedder.embedOne(HYB_NEAR_B));
        assertThat(ringSize(B)).isZero();

        assertThat(hybrid(HYB_NEAR_B, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(B)).as("B's arm returned the nearest gated row").isEqualTo(1);
        assertThat(ringSize(A)).as("A gains nothing from the fan-out").isEqualTo(1);

        assertThat(hybrid(HYB_NEAR_A, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).isEqualTo(2);
        assertThat(ringSize(B)).isEqualTo(1);
    }

    @Test
    void hybridSearch_aCollectionWithoutAValidIndexAdmitsNothing() throws Exception {
        indexed.add(B);

        hybrid(HYB_NEAR_A, A);
        hybrid(HYB_NEAR_A, A, B);

        assertThat(ring.queued()).isZero();
        assertThat(ringSize(A)).isZero();
        assertThat(ringSize(B)).as("a farther arm does not inherit the admission").isZero();
    }

    // The filtered scoped routes (metadata, graph-hop, aspect) admit only a request that targets exactly one collection
    // (fix round 2): their rows are those passing a predicate, so the nearest filtered row can be in a collection the
    // query is far from, and admitting there makes a false red (RDR-227: a far query scores 0.975 to 0.983).

    @Test
    void metadataScopedSearch_admitsASingleCollection_andNoFanOut() throws Exception {
        indexed.addAll(List.of(A, B));

        assertThat(metadataScoped(NEAR_B, A).statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).as("a single collection").isEqualTo(1);
        assertThat(ringVector(A)).containsExactly(embedder.embedOne(NEAR_B));

        assertThat(metadataScoped(NEAR_B, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(B)).as("a fan-out admits to no arm, not even the nearest").isZero();
        assertThat(ringSize(A)).as("A gains nothing from the fan-out").isEqualTo(1);
    }

    @Test
    void graphHopSearch_admitsASingleCollection_andNoFanOut() throws Exception {
        indexed.addAll(List.of(A, B));

        assertThat(graphHop(NEAR_B, A).statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).as("a single collection").isEqualTo(1);
        assertThat(ringVector(A)).containsExactly(embedder.embedOne(NEAR_B));

        assertThat(graphHop(NEAR_B, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(B)).as("a fan-out admits to no arm, not even the nearest").isZero();
        assertThat(ringSize(A)).as("A gains nothing from the fan-out").isEqualTo(1);
    }

    @Test
    void aspectScopedSearch_admitsASingleCollection_andNoFanOut() throws Exception {
        indexed.addAll(List.of(A, B));

        assertThat(aspectScoped(NEAR_B, A).statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).as("a single collection").isEqualTo(1);
        assertThat(ringVector(A)).containsExactly(embedder.embedOne(NEAR_B));

        assertThat(aspectScoped(NEAR_B, A, B).statusCode()).isEqualTo(200);
        assertThat(ringSize(B)).as("a fan-out admits to no arm, not even the nearest").isZero();
        assertThat(ringSize(A)).as("A gains nothing from the fan-out").isEqualTo(1);
    }

    @Test
    void topicScopedSearch_admitsToItsOneCollection_onlyWithAValidIndex() throws Exception {
        indexed.add(B);
        assertThat(topicScoped(NEAR_A, A).statusCode()).isEqualTo(200);
        assertThat(ring.queued()).as("A has no valid index").isZero();
        assertThat(ringSize(A)).isZero();

        indexed.add(A);
        assertThat(topicScoped(NEAR_A, A).statusCode()).isEqualTo(200);
        assertThat(ringSize(A)).isEqualTo(1);
        assertThat(ringVector(A)).containsExactly(embedder.embedOne(NEAR_A));
        assertThat(ringSize(B)).isZero();
    }

    @Test
    void aScopedRouteThatReturnsNoRow_onAFanOut_admitsNothing() throws Exception {
        indexed.addAll(List.of(A, B));
        Map<String, Object> body = searchBody(NEAR_A, A, B);
        body.put("seeds", List.of("no-such-document"));

        var r = post("/search-graph-hop", body);

        assertThat(r.statusCode()).isEqualTo(200);
        assertThat(r.body()).isEqualTo("[]");
        assertThat(ring.queued()).as("no row, no nearest arm").isZero();
    }

    // -- test 7 -----------------------------------------------------------------------------------------------

    @Test
    void aWriterWhoseDatabaseRefusesLeavesTheSearchResponseUnchanged() throws Exception {
        indexed.addAll(List.of(A, B));
        service.queryRing(null);
        // Every route that admits to the ring, each in a shape that reaches admission: the unfiltered fan-outs over
        // two collections, the filtered scoped routes and the topic route over one. (Fix round 2: the pin once
        // covered /search and /search-per-collection only.)
        Map<String, Map<String, Object>> requests = new LinkedHashMap<>();
        requests.put("/search", searchBody(NEAR_A, A, B));
        requests.put("/search-per-collection", fanOutBody(NEAR_A, A, B));
        requests.put("/hybrid-search", searchBody(HYB_NEAR_A, A, B));
        requests.put("/search-metadata-scoped", searchBody(NEAR_A, A));
        requests.put("/search-aspect-scoped", searchBody(NEAR_A, A));
        Map<String, Object> hop = searchBody(NEAR_A, A);
        hop.put("seeds", List.of("own-" + A));
        requests.put("/search-graph-hop", hop);
        Map<String, Object> topic = new LinkedHashMap<>();
        topic.put("query", NEAR_A);
        topic.put("topic", TOPIC_LABEL);
        topic.put("collection", A);
        topic.put("n_results", 5);
        requests.put("/search-topic-scoped", topic);
        Map<String, String> baselines = new LinkedHashMap<>();
        requests.forEach((route, body) -> {
            try {
                var r = postOnly(route, body);
                assertThat(r.statusCode()).as(route + " baseline").isEqualTo(200);
                baselines.put(route, r.body());
            } catch (Exception e) {
                throw new AssertionError(route, e);
            }
        });

        CountDownLatch released = new CountDownLatch(1);
        CountDownLatch writerArrived = new CountDownLatch(1);
        DataSource hung = (DataSource) Proxy.newProxyInstance(DataSource.class.getClassLoader(),
            new Class<?>[] {DataSource.class}, (proxy, method, args) -> {
                if (method.getName().equals("getConnection")) {
                    writerArrived.countDown();
                    released.await(60, TimeUnit.SECONDS);
                    throw new SQLException("refused");
                }
                if (method.getName().equals("hashCode")) return System.identityHashCode(proxy);
                if (method.getName().equals("equals")) return proxy == args[0];
                return "hung-datasource";
            });
        PciQueryRing hungRing = new PciQueryRing(new TenantScope(hung),
            (model, tenant, collection) -> indexed.contains(collection), 1024, 1, Duration.ofMinutes(10));
        hungRing.start();
        service.queryRing(hungRing);
        try {
            var first = postOnly("/search", requests.get("/search"));
            assertThat(writerArrived.await(20, TimeUnit.SECONDS)).as("the writer is stuck in the database").isTrue();
            assertThat(first.statusCode()).isEqualTo(200);
            assertThat(first.body()).as("/search body identical with a stuck writer").isEqualTo(baselines.get("/search"));

            for (var e : requests.entrySet()) {
                if (e.getKey().equals("/search")) continue;
                var r = postOnly(e.getKey(), e.getValue());
                assertThat(r.statusCode()).as(e.getKey()).isEqualTo(200);
                assertThat(r.body()).as(e.getKey() + " body identical with a stuck writer")
                    .isEqualTo(baselines.get(e.getKey()));
            }
        } finally {
            released.countDown();
            hungRing.stop();
            service.queryRing(ring);
        }
    }
}
