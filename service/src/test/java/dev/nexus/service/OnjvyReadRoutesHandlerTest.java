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
 * nexus-onjvy — the three new read routes are actually ROUTED and serve over HTTP.
 *
 * <p>WHY THIS EXISTS SEPARATELY from TelemetryRepositoryTest / TaxonomyRepositoryTest.
 * Those prove the SQL: given a repository method, the right rows come back. They say
 * nothing about whether the method is reachable. Routing here is a {@code switch} on a
 * path string, so a typo'd case label — or a handler wired to the wrong method, or a
 * {@code requireMethod} mismatch — produces a 404/405 that every repository test in the
 * suite still passes through. The only other place engine routing is exercised is
 * {@code native-smoke.sh}, which runs in the RELEASE workflow, i.e. after the tag.
 * Discovering a dead route there is exactly the post-tag burn that cost v0.1.53 -> .54.
 *
 * <p>So this asserts the seam the repository tests cannot: path reaches handler, handler
 * reaches repository, and the JSON shape the Python client destructures comes back.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OnjvyReadRoutesHandlerTest {

    private static final String TOKEN    = "onjvy-routes-test-token-abc123";
    private static final String SVC_ROLE = "svc_onjvy_routes_test";
    private static final String SVC_PASS = "svc_onjvy_routes_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;

    private static final TypeReference<Map<String, Object>> MAP_T  = new TypeReference<>() {};
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
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
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
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
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

    // ── gap 2: hook_failures ──────────────────────────────────────────────────

    @Test
    void hookFailuresList_isRoutedAndReturnsRowsPlusAggregates() throws Exception {
        var rec = post("/v1/telemetry/hook_failures/record",
            "{\"doc_id\":\"route-doc\",\"collection\":\"code__routes\","
            + "\"hook_name\":\"route_hook\",\"error\":\"boom\",\"chain\":\"single\"}");
        assertThat(rec.statusCode()).isEqualTo(200);

        var resp = get("/v1/telemetry/hook_failures/list?days=0&limit=50");

        assertThat(resp.statusCode())
            .as("a 404 here means the case label is not wired, which no repository "
                + "test can see")
            .isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKeys("rows", "total", "oldest_occurred_at");
        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) body.get("rows");
        assertThat(rows).isNotEmpty();
        var row = rows.stream()
            .filter(r -> "route-doc".equals(r.get("doc_id"))).findFirst().orElseThrow();
        assertThat(row.get("hook_name")).isEqualTo("route_hook");
        assertThat(row.get("error")).isEqualTo("boom");
        assertThat((String) row.get("occurred_at"))
            .as("timestamps cross as UTC ISO-8601 with explicit seconds, not a "
                + "locale-dependent offset")
            .endsWith("Z").hasSize(20);
    }

    @Test
    void hookFailuresList_rejectsWrongMethod() throws Exception {
        // requireMethod mismatches are the other half of a mis-wired route.
        var resp = post("/v1/telemetry/hook_failures/list", "{}");
        assertThat(resp.statusCode()).isNotEqualTo(200);
    }

    // ── gap 1: assignment quality columns ─────────────────────────────────────

    @Test
    void assignmentDetails_isRoutedAndCarriesTheQualityColumns() throws Exception {
        var topic = post("/v1/taxonomy/topics/insert",
            "{\"label\":\"route-topic\",\"collection\":\"code__routes\",\"doc_count\":0}");
        assertThat(topic.statusCode()).as("topic seed must succeed").isEqualTo(200);
        long topicId = ((Number) mapper.readValue(topic.body(), MAP_T).get("id")).longValue();

        var assign = post("/v1/taxonomy/assignments/assign",
            "{\"doc_id\":\"route-detail-doc\",\"topic_id\":" + topicId
            + ",\"assigned_by\":\"projection\",\"similarity\":0.8712345,"
            + "\"source_collection\":\"code__route_src\","
            + "\"assigned_at\":\"2026-04-14T10:00:00Z\"}");
        assertThat(assign.statusCode()).isEqualTo(200);

        var resp = post("/v1/taxonomy/assignments/details",
            "{\"doc_ids\":[\"route-detail-doc\"]}");

        assertThat(resp.statusCode()).isEqualTo(200);
        var rows = mapper.readValue(resp.body(), LIST_T);
        assertThat(rows).hasSize(1);
        var row = rows.get(0);
        assertThat(((Number) row.get("similarity")).doubleValue()).isEqualTo(0.8712345);
        assertThat(row.get("source_collection")).isEqualTo("code__route_src");
        assertThat(row.get("assigned_at")).isEqualTo("2026-04-14T10:00:00Z");
    }

    // ── gap 3: hub staleness ──────────────────────────────────────────────────

    @Test
    void hubs_isRoutedAndCarriesTheStalenessFields() throws Exception {
        var topic = post("/v1/taxonomy/topics/insert",
            "{\"label\":\"route-hub\",\"collection\":\"code__routes\",\"doc_count\":0}");
        long topicId = ((Number) mapper.readValue(topic.body(), MAP_T).get("id")).longValue();
        for (String src : List.of("code__route_a", "code__route_b")) {
            post("/v1/taxonomy/assignments/assign",
                "{\"doc_id\":\"hub-doc-" + src + "\",\"topic_id\":" + topicId
                + ",\"assigned_by\":\"projection\",\"similarity\":0.5,"
                + "\"source_collection\":\"" + src + "\","
                + "\"assigned_at\":\"2026-04-10T13:04:00Z\"}");
        }

        var resp = get("/v1/taxonomy/hubs?min_collections=2");

        assertThat(resp.statusCode()).isEqualTo(200);
        var hubs = mapper.readValue(resp.body(), LIST_T);
        var hub = hubs.stream()
            .filter(h -> ((Number) h.get("topic_id")).longValue() == topicId)
            .findFirst()
            .orElseThrow(() -> new AssertionError(
                "the seeded 2-collection topic did not surface as a hub, so the "
                + "staleness assertions below would be vacuous: " + resp.body()));

        assertThat(hub)
            .as("the staleness fields must cross the wire, not just exist in the repo")
            .containsKeys("max_last_discover_at", "never_discovered_count", "is_stale");
        assertThat(hub.get("never_discovered_count"))
            .as("neither source collection was ever discovered").isEqualTo(2);
        assertThat(hub.get("is_stale"))
            .as("a never-discovered contributor forces stale").isEqualTo(true);
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

    private HttpResponse<String> get(String path) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
