// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
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
        repo = new PgVectorRepository(scope, embedder, embedder);
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(A, B, ORPHAN)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }
        seed(A, "a", 4, 0.0);
        seed(B, "b", 4, 0.5);
        seed(ORPHAN, "orphan", 1, 0.3);
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

    private void seed(String collection, String prefix, int count, double angle0) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * 0.01;
            String text = collection + "|" + prefix + "-" + i;
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

    // -- test 7 -----------------------------------------------------------------------------------------------

    @Test
    void aWriterWhoseDatabaseRefusesLeavesTheSearchResponseUnchanged() throws Exception {
        indexed.addAll(List.of(A, B));
        service.queryRing(null);
        String baselineSearch = postOnly("/search", searchBody(NEAR_A, A, B)).body();
        String baselineFan = postOnly("/search-per-collection", fanOutBody(NEAR_A, A, B)).body();

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
            var first = postOnly("/search", searchBody(NEAR_A, A, B));
            assertThat(writerArrived.await(20, TimeUnit.SECONDS)).as("the writer is stuck in the database").isTrue();
            var second = postOnly("/search-per-collection", fanOutBody(NEAR_A, A, B));

            assertThat(first.statusCode()).isEqualTo(200);
            assertThat(first.body()).as("/search body identical with a stuck writer").isEqualTo(baselineSearch);
            assertThat(second.statusCode()).isEqualTo(200);
            assertThat(second.body()).as("/search-per-collection body identical").isEqualTo(baselineFan);
        } finally {
            released.countDown();
            hungRing.stop();
            service.queryRing(ring);
        }
    }
}
