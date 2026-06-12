package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
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

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

/**
 * RDR-156 bead nexus-t1hnc.3 — TaxonomyHandler centroid endpoint tests.
 *
 * <p>Proves the seven {@code /v1/taxonomy/centroids/*} routes over the full service HTTP
 * stack: route wiring, JSON return-SHAPE (snake_case keys the Python centroid-port reads),
 * {@code JsonInclude.ALWAYS} keeping nullable label/doc_count present, auth, and RLS.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyCentroidHandlerTest {

    private static final String TOKEN = "centroid-handler-test-token-abc123";
    private static final String SVC_ROLE = "svc_centroid_handler_test";
    private static final String SVC_PASS = "svc_centroid_handler_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};
    private static final TypeReference<List<Map<String, Object>>> LIST_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE
                + "') THEN CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.taxonomy_centroids_" + dim
                    + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT SELECT ON nexus.service_tokens, nexus.session_tokens TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'test-bound') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null)   svcDs.close();
        if (pg != null)      pg.stop();
    }

    /** 384-dim unit vector ({@code x}, {@code y}, 0, ...). */
    private static List<Float> unit(float x, float y) {
        Float[] v = new Float[384];
        java.util.Arrays.fill(v, 0.0f);
        v[0] = x;
        v[1] = y;
        return List.of(v);
    }

    private void upsert(String collection, long topicId, float x, float y,
                        String label, Integer docCount) throws Exception {
        var rec = new java.util.LinkedHashMap<String, Object>();
        rec.put("collection", collection);
        rec.put("topic_id", topicId);
        rec.put("embedding", unit(x, y));
        rec.put("label", label);
        rec.put("doc_count", docCount);
        var body = mapper.writeValueAsString(Map.of("records", List.of(rec)));
        var resp = post("/v1/taxonomy/centroids/upsert", TENANT, body);
        assertThat(resp.statusCode()).as("upsert 200").isEqualTo(200);
    }

    @Test
    void upsertThenQuery_returnsNearestTopicWithSimilarity() throws Exception {
        upsert("knowledge__hq", 100L, 1.0f, 0.0f, "near", 5);
        upsert("knowledge__hq", 200L, 0.6f, 0.8f, "mid",  3);

        var body = mapper.writeValueAsString(Map.of(
            "embedding", unit(1.0f, 0.0f),
            "collection", "knowledge__hq",
            "cross_collection", false,
            "n_results", 2));
        var resp = post("/v1/taxonomy/centroids/query", TENANT, body);
        assertThat(resp.statusCode()).isEqualTo(200);
        var hits = mapper.readValue(resp.body(), LIST_T);
        assertThat(hits).hasSize(2);
        assertThat(((Number) hits.get(0).get("topic_id")).longValue()).isEqualTo(100L);
        assertThat(((Number) hits.get(0).get("similarity")).doubleValue()).isCloseTo(1.0, within(1e-4));
        // snake_case keys present (Python centroid-port contract)
        assertThat(hits.get(0)).containsKeys("topic_id", "similarity");
    }

    @Test
    void count_and_dimension() throws Exception {
        upsert("knowledge__cnt", 1L, 1.0f, 0.0f, "a", 1);
        upsert("knowledge__cnt", 2L, 0.0f, 1.0f, "b", 1);

        var c = mapper.readValue(
            get("/v1/taxonomy/centroids/count?collection=knowledge__cnt", TENANT).body(), MAP_T);
        assertThat(((Number) c.get("count")).intValue()).isEqualTo(2);

        var d = mapper.readValue(
            get("/v1/taxonomy/centroids/dimension", TENANT).body(), MAP_T);
        assertThat(((Number) d.get("dimension")).intValue()).isEqualTo(384);
    }

    @Test
    void byCollection_roundTripsEmbedding_andKeepsNullableKeys() throws Exception {
        // No label / no doc_count → JsonInclude.ALWAYS must keep the keys as null.
        upsert("knowledge__bc", 7L, 0.6f, 0.8f, null, null);

        var resp = get("/v1/taxonomy/centroids/by_collection?collection=knowledge__bc", TENANT);
        assertThat(resp.statusCode()).isEqualTo(200);
        var rows = mapper.readValue(resp.body(), LIST_T);
        assertThat(rows).hasSize(1);
        var row = rows.get(0);
        assertThat(((Number) row.get("topic_id")).longValue()).isEqualTo(7L);
        assertThat(row.get("collection")).isEqualTo("knowledge__bc");
        assertThat(row).as("nullable keys present (JsonInclude.ALWAYS)")
            .containsKeys("label", "doc_count", "embedding");
        assertThat(row.get("label")).isNull();
        assertThat(row.get("doc_count")).isNull();
        @SuppressWarnings("unchecked")
        var emb = (List<Number>) row.get("embedding");
        assertThat(emb).hasSize(384);
        assertThat(emb.get(0).floatValue()).isCloseTo(0.6f, within(1e-4f));
        assertThat(emb.get(1).floatValue()).isCloseTo(0.8f, within(1e-4f));
    }

    @Test
    void delete_thenPurge() throws Exception {
        upsert("knowledge__del", 1L, 1.0f, 0.0f, "a", 1);
        upsert("knowledge__del", 2L, 0.0f, 1.0f, "b", 1);

        var delBody = mapper.writeValueAsString(Map.of(
            "collection", "knowledge__del", "topic_ids", List.of(1L)));
        var delResp = mapper.readValue(post("/v1/taxonomy/centroids/delete", TENANT, delBody).body(), MAP_T);
        assertThat(((Number) delResp.get("deleted")).intValue()).isEqualTo(1);

        var purgeBody = mapper.writeValueAsString(Map.of("collection", "knowledge__del"));
        var purgeResp = mapper.readValue(post("/v1/taxonomy/centroids/purge", TENANT, purgeBody).body(), MAP_T);
        assertThat(((Number) purgeResp.get("deleted")).intValue()).isEqualTo(1);

        var c = mapper.readValue(
            get("/v1/taxonomy/centroids/count?collection=knowledge__del", TENANT).body(), MAP_T);
        assertThat(((Number) c.get("count")).intValue()).isZero();
    }

    @Test
    void crossCollection_excludesSourceCollection() throws Exception {
        upsert("knowledge__xa", 11L, 1.0f, 0.0f, "a", 1);
        upsert("docs__xb",      22L, 1.0f, 0.0f, "b", 1);

        var body = mapper.writeValueAsString(Map.of(
            "embedding", unit(1.0f, 0.0f),
            "collection", "knowledge__xa",
            "cross_collection", true,
            "n_results", 10));
        var hits = mapper.readValue(post("/v1/taxonomy/centroids/query", TENANT, body).body(), LIST_T);
        // cross_collection=true on xa must NOT return topic 11 (its own collection).
        assertThat(hits).extracting(h -> ((Number) h.get("topic_id")).longValue())
            .contains(22L).doesNotContain(11L);
    }

    @Test
    void foreign_returnsAllCollectionsExceptGiven() throws Exception {
        upsert("knowledge__fga", 1L, 1.0f, 0.0f, "a", 1);
        upsert("docs__fgb",      2L, 0.6f, 0.8f, "b", 1);

        var resp = get("/v1/taxonomy/centroids/foreign?collection=knowledge__fga", TENANT);
        assertThat(resp.statusCode()).isEqualTo(200);
        var rows = mapper.readValue(resp.body(), LIST_T);
        assertThat(rows).extracting(r -> ((Number) r.get("topic_id")).longValue())
            .contains(2L).doesNotContain(1L);
        assertThat(rows).allSatisfy(r -> assertThat(r).containsKeys("embedding", "label", "doc_count", "collection"));
    }

    @Test
    void malformedInput_returns400Not500() throws Exception {
        // M1: non-object element in records
        var r1 = post("/v1/taxonomy/centroids/upsert", TENANT, "{\"records\":[123]}");
        assertThat(r1.statusCode()).as("non-object record element -> 400").isEqualTo(400);

        // M3: non-finite embedding value (1e999 parses to Infinity server-side)
        var r2 = post("/v1/taxonomy/centroids/query", TENANT,
            "{\"embedding\":[1e999,0.0],\"collection\":\"knowledge__nan\","
            + "\"cross_collection\":false,\"n_results\":1}");
        assertThat(r2.statusCode()).as("non-finite embedding -> 400").isEqualTo(400);

        // M2: non-numeric topic_ids element
        var r3 = post("/v1/taxonomy/centroids/delete", TENANT,
            "{\"collection\":\"knowledge__x\",\"topic_ids\":[\"foo\"]}");
        assertThat(r3.statusCode()).as("non-numeric topic_id -> 400").isEqualTo(400);
    }

    @Test
    void auth_401OnMissingToken() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/taxonomy/centroids/dimension"))
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
    }

    // ── Helpers ─────────────────────────────────────────────────────────────────

    private HttpResponse<String> get(String path, String tenant) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", tenant)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> post(String path, String tenant, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
