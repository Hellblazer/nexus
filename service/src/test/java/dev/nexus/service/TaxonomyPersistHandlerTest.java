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

/**
 * RDR-152 bead nexus-1di3r.1 — TaxonomyHandler chroma-free persist/read endpoint tests.
 *
 * <p>Proves the three new routes over the full service HTTP stack:
 * {@code GET /v1/taxonomy/rebuild/old_state}, {@code POST /v1/taxonomy/topics/persist_rebuild},
 * {@code POST /v1/taxonomy/topics/persist_discovered}. Asserts route wiring, JSON return-SHAPE
 * (snake_case keys the Python orchestrator reads), {@code JsonInclude.ALWAYS} keeping nullable
 * keys present, the REPLACE / existing-topics-guard semantics end-to-end, and auth.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers), port 0, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyPersistHandlerTest {

    private static final String TOKEN = "tax-persist-handler-test-token-abc123";
    private static final String SVC_ROLE = "svc_tax_persist_handler_test";
    private static final String SVC_PASS = "svc_tax_persist_handler_test_pass";
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
            for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
            }
            su.createStatement().execute("GRANT USAGE ON SEQUENCE nexus.topics_id_seq TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT ON nexus.catalog_documents TO " + SVC_ROLE);
            // RDR-164 P1a: topics/taxonomy_meta writes ensure-register their collection
            // (catalog_collections stub) to satisfy the new fk-003 collection FKs.
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.service_tokens, nexus.session_tokens TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'test-bound') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");

            // topic_assignments.doc_id enforces an FK to catalog_documents (nexus-b7v6i):
            // seed every doc_id this test uses as a tumbler under the default tenant.
            for (String tumbler : List.of(
                    "hd-disc-1", "hd-disc-2", "hd-rb-1", "hd-rb-2", "hd-rb-manual", "hd-os-manual")) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES ('"
                    + TENANT + "', '" + tumbler + "', 'fixture: " + tumbler + "') "
                    + "ON CONFLICT (tenant_id, tumbler) DO NOTHING");
            }
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

    @Test
    void persistDiscovered_thenOldState_roundTripsShapeAndKeepsNullableKeys() throws Exception {
        String col = "knowledge__hpd";
        var spec = Map.of(
            "label", "topic-a", "doc_count", 2, "terms", "[\"a\"]",
            "assigned_by", "hdbscan", "doc_ids", List.of("hd-disc-1", "hd-disc-2"));
        var body = mapper.writeValueAsString(Map.of("collection", col, "specs", List.of(spec)));

        var resp = post("/v1/taxonomy/topics/persist_discovered", body);
        assertThat(resp.statusCode()).isEqualTo(200);
        var ids = (List<?>) mapper.readValue(resp.body(), MAP_T).get("topic_ids");
        assertThat(ids).hasSize(1);

        // old_state read half: topic surfaces with all three keys (JsonInclude.ALWAYS).
        var os = mapper.readValue(
            get("/v1/taxonomy/rebuild/old_state?collection=" + col).body(), MAP_T);
        assertThat(os).containsOnlyKeys("old_topic_map", "manual_assignments");
        var topicMap = mapper.convertValue(os.get("old_topic_map"), LIST_T);
        assertThat(topicMap).hasSize(1);
        assertThat(topicMap.get(0)).containsOnlyKeys("id", "label", "review_status");
        assertThat(topicMap.get(0).get("label")).isEqualTo("topic-a");
        assertThat(topicMap.get(0).get("review_status")).isEqualTo("pending");
        // No manual assignments yet.
        assertThat(mapper.convertValue(os.get("manual_assignments"), LIST_T)).isEmpty();
    }

    @Test
    void persistDiscovered_existingGuard_returnsNoOp() throws Exception {
        String col = "knowledge__hpg";
        var spec = Map.of("label", "first", "doc_count", 0, "terms", "[]",
                          "assigned_by", "hdbscan", "doc_ids", List.of());
        post("/v1/taxonomy/topics/persist_discovered",
             mapper.writeValueAsString(Map.of("collection", col, "specs", List.of(spec))));

        // Second call must be a no-op (topics already exist).
        var spec2 = Map.of("label", "second", "doc_count", 0, "terms", "[]",
                           "assigned_by", "hdbscan", "doc_ids", List.of());
        var resp = post("/v1/taxonomy/topics/persist_discovered",
            mapper.writeValueAsString(Map.of("collection", col, "specs", List.of(spec2))));
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat((List<?>) mapper.readValue(resp.body(), MAP_T).get("topic_ids")).isEmpty();
    }

    @Test
    void persistRebuild_replaceSemanticsAndManualTransfer_endToEnd() throws Exception {
        String col = "knowledge__hrb";
        // Seed an "old" topic via discover.
        var old = Map.of("label", "old", "doc_count", 1, "terms", "[]",
                         "assigned_by", "hdbscan", "doc_ids", List.of("hd-rb-1"));
        post("/v1/taxonomy/topics/persist_discovered",
             mapper.writeValueAsString(Map.of("collection", col, "specs", List.of(old))));

        var specs = List.of(
            Map.of("label", "new-0", "doc_count", 2, "terms", "[\"x\"]",
                   "review_status", "pending", "assigned_by", "hdbscan",
                   "doc_ids", List.of("hd-rb-1", "hd-rb-2")),
            Map.of("label", "new-1", "doc_count", 0, "terms", "[\"y\"]",
                   "review_status", "pending", "assigned_by", "hdbscan",
                   "doc_ids", List.of()));
        var body = mapper.writeValueAsString(Map.of(
            "collection", col, "specs", specs,
            "manual_transfers", Map.of("hd-rb-manual", 1)));

        var resp = post("/v1/taxonomy/topics/persist_rebuild", body);
        assertThat(resp.statusCode()).isEqualTo(200);
        var ids = (List<?>) mapper.readValue(resp.body(), MAP_T).get("topic_ids");
        assertThat(ids).hasSize(2);
        long secondId = ((Number) ids.get(1)).longValue();

        // Old topic gone, two new present, manual transfer applied to ids[1].
        var os = mapper.readValue(
            get("/v1/taxonomy/rebuild/old_state?collection=" + col).body(), MAP_T);
        var topicMap = mapper.convertValue(os.get("old_topic_map"), LIST_T);
        assertThat(topicMap).extracting(mm -> mm.get("label"))
            .containsExactlyInAnyOrder("new-0", "new-1");
        var manual = mapper.convertValue(os.get("manual_assignments"), LIST_T);
        assertThat(manual).hasSize(1);
        assertThat(manual.get(0).get("doc_id")).isEqualTo("hd-rb-manual");
        assertThat(((Number) manual.get(0).get("topic_id")).longValue()).isEqualTo(secondId);
    }

    @Test
    void oldState_missingCollectionParam_returns400() throws Exception {
        assertThat(get("/v1/taxonomy/rebuild/old_state").statusCode()).isEqualTo(400);
    }

    @Test
    void renameCollection_acceptsOldNewFields_andRepoints() throws Exception {
        // Contract guard (RDR-162 / nexus-pqatt): the rename_collection endpoint
        // takes "old"/"new" — the convention every other rename_collection handler
        // and every nexus HTTP client use. It was the lone "old_collection"/
        // "new_collection" outlier, which 400'd the cross-model ref-remap (the
        // first real caller). A test posting the wrong fields would 400; this one
        // posts the right fields and asserts the rename took effect end-to-end.
        String oldCol = "knowledge__rn-old";
        String newCol = "knowledge__rn-new";
        var spec = Map.of(
            "label", "rn-topic", "doc_count", 1, "terms", "[\"x\"]",
            "assigned_by", "hdbscan", "doc_ids", List.of("rn-1"));
        assertThat(post("/v1/taxonomy/topics/persist_discovered",
            mapper.writeValueAsString(Map.of("collection", oldCol, "specs", List.of(spec))))
            .statusCode()).isEqualTo(200);

        var resp = post("/v1/taxonomy/rename_collection",
            mapper.writeValueAsString(Map.of("old", oldCol, "new", newCol)));
        assertThat(resp.statusCode()).isEqualTo(200);
        var counts = mapper.readValue(resp.body(), MAP_T);
        // Repo returns topics/assignments/meta counts; the topic row moved.
        assertThat(((Number) counts.get("topics")).intValue()).isEqualTo(1);

        // The topic now resolves under the NEW collection, not the old.
        var newState = mapper.convertValue(
            mapper.readValue(get("/v1/taxonomy/rebuild/old_state?collection=" + newCol).body(),
                MAP_T).get("old_topic_map"), LIST_T);
        assertThat(newState).hasSize(1);
        var oldState = mapper.convertValue(
            mapper.readValue(get("/v1/taxonomy/rebuild/old_state?collection=" + oldCol).body(),
                MAP_T).get("old_topic_map"), LIST_T);
        assertThat(oldState).isEmpty();
    }

    @Test
    void renameCollection_missingFields_returns400() throws Exception {
        // The wrong field names (the old outlier contract) must be rejected loud.
        assertThat(post("/v1/taxonomy/rename_collection",
            mapper.writeValueAsString(Map.of("old_collection", "a", "new_collection", "b")))
            .statusCode()).isEqualTo(400);
    }

    @Test
    void auth_401OnMissingToken() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort()
                + "/v1/taxonomy/rebuild/old_state?collection=knowledge__x"))
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(401);
    }

    // ── Helpers ─────────────────────────────────────────────────────────────────

    private HttpResponse<String> get(String path) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
