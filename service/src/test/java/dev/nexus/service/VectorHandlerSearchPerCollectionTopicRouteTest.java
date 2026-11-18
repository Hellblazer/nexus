// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-7fku2.1.3 (RDR-134 Step 3) -- the HTTP contract of {@code topic_route} on
 * {@code POST /v1/vectors/search-per-collection}: request validation (400), the absent and present-but-empty
 * {@code collections} byte-identity, the status on each {@code per_collection} entry, the mixed group, and the
 * request-wide skip. The routed searches themselves are {@code PgVectorSearchPerCollectionTopicRouteIntegrationTest}'s.
 * DENSE and SMALL have no taxonomy, so a routed one reads {@code no_topics}. TAXO has one (two topics, every chunk
 * assigned), so a request that routes it reads {@code routed} at the HTTP layer: the coverage lookup the handler wires
 * in is the one that finds it (a handler that wired an empty lookup would read {@code no_topics}), and the additive
 * {@code topic_route_coverage} and {@code topic_route_topics} keys appear only for it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerSearchPerCollectionTopicRouteTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-7fku213-http-0123456789abcdef0000";
    private static final String TENANT = "7fku213-http";
    private static final String QUERY = "7fku213 http query";
    private static final String DENSE = "knowledge__7fku213-http-dense__minilm-l6-v2-384__v1";
    private static final String SMALL = "knowledge__7fku213-http-small__minilm-l6-v2-384__v1";
    private static final String TAXO = "knowledge__7fku213-http-taxo__minilm-l6-v2-384__v1";
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "7fku213-http");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        var scope = new TenantScope(svcDs);
        var repo = new PgVectorRepository(scope, embedder, embedder);
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, DENSE);
            PgContainerHelper.insertCollection(dsl, TENANT, SMALL);
            PgContainerHelper.insertCollection(dsl, TENANT, TAXO);
        }
        seed(scope, repo, DENSE, "dense", 30, 0.0, 0.001);
        seed(scope, repo, SMALL, "small", 3, 1.0, 0.01);
        // TAXO: topic TA's centroid is the nearest the query (0.10 rad) and its chunks the farthest (0.30 rad and up);
        // TB's centroid is far (1.0 rad) and its chunks the nearest (0.05 rad and up): routed, the rows are TA's
        List<String> ta = seed(scope, repo, TAXO, "ta", 6, 0.30, 0.01);
        List<String> tb = seed(scope, repo, TAXO, "tb", 6, 0.05, 0.01);
        taxonomy(TAXO, "TA", 0.10, ta);
        taxonomy(TAXO, "TB", 1.00, tb);
        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
    }

    private List<String> seed(TenantScope scope, PgVectorRepository repo, String collection, String prefix, int count,
                              double angle0, double step) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
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
        return ids;
    }

    private void taxonomy(String collection, String label, double centroidAngle, List<String> assigned) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, collection);
            long id = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, label + " of " + collection)
                .set(TOPICS.COLLECTION, collection).set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
            dsl.insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, TENANT).set(TAXONOMY_CENTROIDS.COLLECTION, collection)
                .set(TAXONOMY_CENTROIDS.TOPIC_ID, id).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, model)
                .set(TAXONOMY_CENTROIDS.LABEL, label)
                .set(VectorSeeds.centroidEmbedding(384),
                     VectorSeeds.vecXY(384, (float) Math.cos(centroidAngle), (float) Math.sin(centroidAngle)))
                .execute();
            for (String hex : assigned) {
                dsl.insertInto(TOPIC_ASSIGNMENTS)
                    .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT)
                    .set(TOPIC_ASSIGNMENTS.DOC_ID, HexFormat.of().parseHex(hex))
                    .set(TOPIC_ASSIGNMENTS.TOPIC_ID, id).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, collection)
                    .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
                    .execute();
            }
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
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

    private HttpResponse<String> post(Map<String, Object> body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/search-per-collection")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode json(HttpResponse<String> r) throws Exception {
        return MAPPER.readTree(r.body());
    }

    private static Map<String, Object> ok() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("query", QUERY);
        m.put("collections", List.of(DENSE, SMALL));
        m.put("per_collection_k", 5);
        m.put("limit", 100);
        return m;
    }

    private void assertBadRequest(Map<String, Object> req, String fragment) throws Exception {
        var r = post(req);
        assertThat(r.statusCode()).as("%s -> %s", fragment, r.body()).isEqualTo(400);
        assertThat(json(r).get("error").asText()).contains(fragment);
    }

    private static Map<String, Object> topicRoute(Object mode, Object width, Object collections) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (mode != null) m.put("mode", mode);
        if (width != null) m.put("width", width);
        if (collections != null) m.put("collections", collections);
        return m;
    }

    private Map<String, Object> routed(Map<String, Object> topicRoute) {
        Map<String, Object> req = ok();
        req.put("topic_route", topicRoute);
        return req;
    }

    @Test
    void validation_rejectsMalformedRoutesWith400() throws Exception {
        assertBadRequest(routed(topicRoute("nearest", null, List.of(DENSE))), "mode");
        assertBadRequest(routed(topicRoute(null, null, List.of(DENSE))), "mode");
        assertBadRequest(routed(topicRoute(7, null, List.of(DENSE))), "mode");
        assertBadRequest(routed(topicRoute("band", null, List.of(DENSE))), "width");
        assertBadRequest(routed(topicRoute("band", -0.1, List.of(DENSE))), "width");
        assertBadRequest(routed(topicRoute("band", "wide", List.of(DENSE))), "width");
        assertBadRequest(routed(topicRoute("top1", null,
            List.of("knowledge__7fku213-http-elsewhere__minilm-l6-v2-384__v1"))), "not in collections");
        assertBadRequest(routed(topicRoute("top1", null, List.of(DENSE, "knowledge__x__minilm-l6-v2-384__v1"))),
            "not in collections");
        assertBadRequest(routed(topicRoute("top1", null, "not-an-array")), "collections");
        Map<String, Object> notAnObject = ok();
        notAnObject.put("topic_route", "top1");
        assertBadRequest(notAnObject, "topic_route");

        // a present field is checked whether or not the list names anything
        assertBadRequest(routed(topicRoute("nearest", null, List.of())), "mode");
        assertBadRequest(routed(topicRoute("band", null, List.of())), "width");
    }

    @Test
    void aGoodRouteIsAccepted_bandWidthZeroIncluded() throws Exception {
        assertThat(post(routed(topicRoute("top1", null, List.of(DENSE)))).statusCode()).isEqualTo(200);
        assertThat(post(routed(topicRoute("top1", 0.5, List.of(DENSE)))).statusCode())
            .as("width is read for band only").isEqualTo(200);
        assertThat(post(routed(topicRoute("band", 0, List.of(DENSE)))).statusCode()).isEqualTo(200);
        assertThat(post(routed(topicRoute("band", 0.25, null))).statusCode()).as("no list: every collection").isEqualTo(200);
        Map<String, Object> jsonNull = ok();
        jsonNull.put("topic_route", null);
        assertThat(post(jsonNull).statusCode()).as("a JSON null is an absent field").isEqualTo(200);
    }

    @Test
    void absentAndPresentButEmptyCollections_areByteIdentical() throws Exception {
        var absent = post(ok());
        var empty = post(routed(topicRoute("top1", null, List.of())));
        var emptyBand = post(routed(topicRoute("band", 0.2, List.of())));
        assertThat(absent.statusCode()).isEqualTo(200);
        assertThat(empty.statusCode()).as(empty.body()).isEqualTo(200);
        assertThat(empty.body()).as("an empty collections list adds no envelope key and no per_collection key")
            .isEqualTo(absent.body());
        assertThat(emptyBand.body()).isEqualTo(absent.body());
        assertThat(absent.body()).doesNotContain("topic_route");
    }

    @Test
    void theStatusIsOnEachPerCollectionEntry_andTheEnvelopeIsUnchanged() throws Exception {
        var r = post(routed(topicRoute("top1", null, List.of(DENSE))));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.fieldNames()).toIterable().containsExactly("results", "per_collection", "per_collection_k", "limit");
        JsonNode pc = body.get("per_collection");
        assertThat(pc.get(0).fieldNames()).toIterable().containsExactly(
            "collection", "raw_count", "dropped", "min_raw_distance", "min_dropped_distance", "error", "error_kind",
            "topic_route_status");
        assertThat(pc.get(0).get("collection").asText()).isEqualTo(DENSE);
        assertThat(pc.get(0).get("topic_route_status").asText())
            .as("DENSE has no taxonomy: searched unscoped, no_topics").isEqualTo("no_topics");
        assertThat(pc.get(1).get("collection").asText()).isEqualTo(SMALL);
        assertThat(pc.get(1).get("topic_route_status").asText())
            .as("SMALL is not in topic_route.collections: searched unscoped, skipped").isEqualTo("skipped");
    }

    @Test
    void aMixedGroup_theCollectionNotListedReturnsWhatAnUnroutedRequestReturns() throws Exception {
        JsonNode plain = json(post(ok()));
        JsonNode mixed = json(post(routed(topicRoute("top1", null, List.of(DENSE)))));
        assertThat(mixed.get("results")).as("neither collection has a taxonomy, so every row is the unscoped one")
            .isEqualTo(plain.get("results"));
        for (int i = 0; i < 2; i++) {
            ((ObjectNode) mixed.get("per_collection").get(i)).remove("topic_route_status");
        }
        assertThat(mixed.get("per_collection")).isEqualTo(plain.get("per_collection"));
    }

    @Test
    void withWhereOrChashAllow_everyCollectionIsSkipped() throws Exception {
        Map<String, Object> withWhere = routed(topicRoute("top1", null, null));
        withWhere.put("where", Map.of("nonexistent", "key"));
        JsonNode w = json(post(withWhere));
        assertThat(w.get("per_collection")).extracting(n -> n.get("topic_route_status").asText())
            .containsExactly("skipped", "skipped");

        Map<String, Object> withAllow = routed(topicRoute("top1", null, null));
        withAllow.put("chash_allow", List.of(chash(DENSE + "|dense-0")));
        JsonNode a = json(post(withAllow));
        assertThat(a.get("per_collection")).extracting(n -> n.get("topic_route_status").asText())
            .containsExactly("skipped", "skipped");
        assertThat(a.get("results")).as("non-vacuity: the allowed chunk still comes back").hasSize(1);
    }

    @Test
    void everyCollectionIsRoutedByDefault_whenNoListIsGiven() throws Exception {
        JsonNode r = json(post(routed(topicRoute("band", 0.3, null))));
        assertThat(r.get("per_collection")).extracting(n -> n.get("topic_route_status").asText())
            .containsExactly("no_topics", "no_topics");
    }

    private Map<String, Object> taxoRequest(Map<String, Object> topicRoute) {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("query", QUERY);
        req.put("collections", List.of(TAXO));
        req.put("per_collection_k", 5);
        req.put("limit", 100);
        if (topicRoute != null) {
            req.put("topic_route", topicRoute);
        }
        return req;
    }

    private static List<String> idsOf(JsonNode results) {
        List<String> ids = new ArrayList<>();
        results.forEach(n -> ids.add(n.get("id").asText()));
        return ids;
    }

    private static List<String> hashes(String prefix, int from, int to) {
        List<String> out = new ArrayList<>();
        for (int i = from; i < to; i++) out.add(chash(TAXO + "|" + prefix + "-" + i));
        return out;
    }

    @Test
    void aCollectionWithATaxonomyIsRoutedAtTheHttpLayer_andCarriesCoverageAndKeptTopics() throws Exception {
        var r = post(taxoRequest(topicRoute("top1", null, List.of(TAXO))));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        JsonNode entry = body.get("per_collection").get(0);
        assertThat(entry.get("topic_route_status").asText()).as("the handler's coverage wiring finds TAXO's taxonomy")
            .isEqualTo("routed");
        assertThat(entry.fieldNames()).toIterable().containsExactly(
            "collection", "raw_count", "dropped", "min_raw_distance", "min_dropped_distance", "error", "error_kind",
            "topic_route_status", "topic_route_coverage", "topic_route_topics");
        assertThat(entry.get("topic_route_coverage").asDouble()).isEqualTo(1.0);
        assertThat(entry.get("topic_route_topics").asInt()).isEqualTo(1);
        assertThat(idsOf(body.get("results"))).as("TA's chunks, though TB's are nearer the query")
            .containsExactlyElementsOf(hashes("ta", 0, 5));

        var unrouted = json(post(taxoRequest(null)));
        assertThat(idsOf(unrouted.get("results"))).as("non-vacuity: unrouted, the same query returns TB's chunks")
            .containsExactlyElementsOf(hashes("tb", 0, 5));
    }

    @Test
    void theNewRouteKeysAreAbsentWithoutARoute_andWithAnEmptyCollectionsList() throws Exception {
        var absent = post(taxoRequest(null));
        var empty = post(taxoRequest(topicRoute("top1", null, List.of())));
        assertThat(empty.statusCode()).isEqualTo(200);
        assertThat(empty.body()).as("an empty collections list changes no byte, though TAXO could be routed")
            .isEqualTo(absent.body());
        assertThat(absent.body()).doesNotContain("topic_route_status").doesNotContain("topic_route_error")
            .doesNotContain("topic_route_coverage").doesNotContain("topic_route_topics");
    }
}
