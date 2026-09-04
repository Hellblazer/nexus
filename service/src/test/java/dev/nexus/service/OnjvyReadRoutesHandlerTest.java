package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.testcontainers.containers.PostgreSQLContainer;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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
 * nexus-onjvy — the four new read routes are actually ROUTED and serve over HTTP.
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
            PgContainerHelper.applyProductSchema(su);
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
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

        // nexus-tk070.p3c: doc_id is bytea now; the wire value must be genuine
        // 64-lowercase-hex, not the old free-text "route-detail-doc" placeholder.
        String docId = hexChash("route-detail-doc");
        // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now requires
        // a matching nexus.chunks row for this assign to succeed at all.
        seedChunk(TENANT, "code__route_src", docId, 384);
        var assign = post("/v1/taxonomy/assignments/assign",
            "{\"doc_id\":\"" + docId + "\",\"topic_id\":" + topicId
            + ",\"assigned_by\":\"projection\",\"similarity\":0.8712345,"
            + "\"source_collection\":\"code__route_src\","
            + "\"assigned_at\":\"2026-04-14T10:00:00Z\"}");
        assertThat(assign.statusCode()).isEqualTo(200);

        var resp = post("/v1/taxonomy/assignments/details",
            "{\"doc_ids\":[\"" + docId + "\"]}");

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
        // nexus-tk070.p3c: doc_id is bytea now; the wire value must be genuine
        // 64-lowercase-hex, not the old free-text "hub-doc-<src>" placeholder.
        for (String src : List.of("code__route_a", "code__route_b")) {
            // RDR-194 P3d (nexus-tk070.p3d): topic_assignments_chunk_fk now
            // requires a matching nexus.chunks row for each assign to succeed.
            seedChunk(TENANT, src, hexChash("hub-doc-" + src), 384);
            post("/v1/taxonomy/assignments/assign",
                "{\"doc_id\":\"" + hexChash("hub-doc-" + src) + "\",\"topic_id\":" + topicId
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

    // ── gap 4: tier_writes.target_title ───────────────────────────────────────

    @Test
    void tierWritesList_isRoutedAndCarriesTargetTitle() throws Exception {
        var rec = post("/v1/telemetry/tier_writes/record",
            "{\"session_id\":\"route-tw-sess\",\"tool\":\"memory_put\",\"tier\":\"T2\","
            + "\"agent\":\"developer\",\"project\":\"route-proj\",\"target_title\":\"route-target.md\"}");
        assertThat(rec.statusCode()).isEqualTo(200);

        var resp = get("/v1/telemetry/tier_writes/list?session_id=route-tw-sess");

        assertThat(resp.statusCode())
            .as("a 404 here means the case label is not wired, which no repository "
                + "test can see")
            .isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body)
            .as("capped-page-plus-exact-total envelope, same discipline as "
                + "hook_failures/list")
            .containsKeys("rows", "total");
        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) body.get("rows");
        assertThat(rows).isNotEmpty();
        var row = rows.stream()
            .filter(r -> "route-tw-sess".equals(r.get("session_id"))).findFirst().orElseThrow();
        assertThat(row.get("target_title")).isEqualTo("route-target.md");
        assertThat(row.get("tool")).isEqualTo("memory_put");
        assertThat(row.get("agent")).isEqualTo("developer");
    }

    @Test
    void tierWritesList_limitIsRoutedAndCapsThePage() throws Exception {
        // Review finding (reviewer [21898] == critic [21897]): the route must
        // actually honor ?limit= over HTTP, not just in the repository layer —
        // same routing-seam rationale as every other test in this class.
        for (int i = 0; i < 5; i++) {
            var rec = post("/v1/telemetry/tier_writes/record",
                "{\"session_id\":\"route-tw-cap\",\"tool\":\"memory_put\",\"tier\":\"T2\","
                + "\"target_title\":\"cap-" + i + "\"}");
            assertThat(rec.statusCode()).isEqualTo(200);
        }

        var resp = get("/v1/telemetry/tier_writes/list?session_id=route-tw-cap&limit=2");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) body.get("rows");
        assertThat(rows).as("page must be capped at limit=2").hasSize(2);
        assertThat(((Number) body.get("total")).intValue())
            .as("total must be the full 5, not the capped page size")
            .isEqualTo(5);
    }

    @Test
    void tierWritesList_rejectsWrongMethod() throws Exception {
        var resp = post("/v1/telemetry/tier_writes/list", "{}");
        assertThat(resp.statusCode()).isNotEqualTo(200);
    }

    // ── search_telemetry trim dry-run preview ─────────────────────────────────
    //
    // Routing-seam counterpart to TelemetryRepositoryTest's
    // trimSearchTelemetry_dryRun_* tests: proves dry_run is actually parsed
    // off the wire and threaded to the repository, not just implemented at
    // the Java-method level a route could still fail to reach.

    @Test
    void searchTrim_dryRun_isRoutedAndPreviewMatchesRealTrim() throws Exception {
        var batch = post("/v1/telemetry/search/batch",
            "{\"rows\":[[\"2024-01-15T10:30:00Z\",\"route-trim-old\",\"code__routes\",5,2,0.3,0.5]]}");
        assertThat(batch.statusCode()).isEqualTo(200);

        var preview = post("/v1/telemetry/search/trim", "{\"days\":30,\"dry_run\":true}");
        assertThat(preview.statusCode())
            .as("a non-200 here means dry_run is not wired through the route")
            .isEqualTo(200);
        var previewBody = mapper.readValue(preview.body(), MAP_T);
        assertThat(previewBody).containsKeys("deleted", "dry_run");
        assertThat(previewBody.get("dry_run")).isEqualTo(true);
        int previewCount = ((Number) previewBody.get("deleted")).intValue();
        assertThat(previewCount)
            .as("must count at least the seeded aged row").isGreaterThanOrEqualTo(1);

        var real = post("/v1/telemetry/search/trim", "{\"days\":30,\"dry_run\":false}");
        assertThat(real.statusCode()).isEqualTo(200);
        var realBody = mapper.readValue(real.body(), MAP_T);
        assertThat(realBody.get("dry_run")).isEqualTo(false);
        assertThat(((Number) realBody.get("deleted")).intValue())
            .as("real trim over HTTP must delete exactly what the preview reported")
            .isEqualTo(previewCount);
    }

    @Test
    void searchTrim_omittedDryRun_defaultsToRealDelete() throws Exception {
        // Backward compatibility: a caller that never sends dry_run (every
        // pre-existing client call) must still get the OLD real-delete
        // behavior, not an accidental no-op.
        var batch = post("/v1/telemetry/search/batch",
            "{\"rows\":[[\"2024-01-15T10:30:00Z\",\"route-trim-compat\",\"code__routes\",1,1,0.1,0.5]]}");
        assertThat(batch.statusCode()).isEqualTo(200);

        var resp = post("/v1/telemetry/search/trim", "{\"days\":30}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("dry_run"))
            .as("omitted dry_run must default to false").isEqualTo(false);
    }

    /**
     * Insert a minimal nexus.chunks row (RDR-194 P3d, nexus-tk070.p3d): every
     * topic_assignments row now requires a matching (tenant_id, source_collection,
     * doc_id) -> chunks(tenant_id, collection, chash) parent via
     * topic_assignments_chunk_fk. Also registers the collection (ON CONFLICT DO
     * NOTHING), and both statements are idempotent for reuse across tests.
     */
    private void seedChunk(String tenant, String collection, String chashHex, int dim) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + tenant + "', '"
                + collection + "') ON CONFLICT (tenant_id, name) DO NOTHING");
            String embeddingCol = "embedding_" + dim;
            su.createStatement().execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, " + embeddingCol + ") VALUES " +
                "('" + tenant + "', '" + collection + "', decode('" + chashHex + "', 'hex'), 'routes-test chunk', " +
                "('[" + "0.1,".repeat(dim - 1) + "0.1]')::vector) " +
                "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
        }
    }

    /** Genuine 64-lowercase-hex sha256 chash — required for topic_assignments.doc_id
     *  (bytea since nexus-tk070.p3c). */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
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
