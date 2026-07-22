package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.Chash;
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
import java.sql.ResultSet;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.3 — /v1/chash/* wire-format coverage for the
 * reroute (the first end-to-end HTTP test of this surface; the .2 review
 * flagged that only repository-level coverage existed).
 *
 * <p>Pins the RDR-187 compatibility contract through the full stack
 * (HTTP → ChashHandler → ChashRepository → chunks tables):
 * <ol>
 *   <li>READ shapes are byte-compatible with the router era: lookup returns
 *       {@code {"rows":[{"collection","created_at"}],"chash"}} with
 *       second-precision UTC timestamps; registered_chashes / distinct
 *       collections / count / is_empty keep their keys</li>
 *   <li>DEPRECATED writes (upsert, upsert_many, import, delete_collection,
 *       delete_stale) keep their old success shapes, add
 *       {@code "deprecated":true}, perform ZERO database work — and
 *       delete_collection specifically does NOT destroy chunk content</li>
 *   <li>validation still fails loud during the window: malformed chash 400,
 *       missing params 400, wrong method 405</li>
 *   <li>rename_collection stays REAL (re-homes chunks; Q3)</li>
 * </ol>
 *
 * <p>Survives nexus-piwya.9 (no {@code chash_index} references — the no-op
 * assertions check chunk-table state only); the write-endpoint tests are
 * updated at nexus-piwya.11 when the 410 flip lands.
 *
 * <p>Hermetic: Testcontainers pgvector, port 0, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashHandlerRerouteTest {

    private static final String TOKEN = "chash-reroute-test-token-abc123";
    private static final String TENANT = dev.nexus.service.db.TenantConstants.DEFAULT_TENANT;

    private static final String COLL_384 = "wire-coll-384";
    private static final String COLL_768 = "wire-coll-768";

    private static final Chash MULTI = Chash.ofText("wire-multi");
    private static final Chash REF_ONLY = Chash.ofText("wire-ref-only");
    private static final Chash ABSENT = Chash.ofText("wire-absent");

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

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
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'chash-reroute-test') ON CONFLICT (token_hash) DO NOTHING");

            for (String coll : new String[] {COLL_384, COLL_768}) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                    "VALUES ('" + TENANT + "', '" + coll + "') " +
                    "ON CONFLICT (tenant_id, name) DO NOTHING");
            }
            chunk(su, 384, COLL_384, MULTI,    "2026-07-02 08:00:01+00");
            chunk(su, 768, COLL_768, MULTI,    "2026-07-02 08:00:02+00");
            chunk(su, 768, COLL_768, REF_ONLY, "2026-07-02 08:00:03+00");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
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
        if (svcDs  != null) svcDs.close();
        if (pg     != null) pg.stop();
    }

    // ── READ shapes ──────────────────────────────────────────────────────────

    @Test
    @SuppressWarnings("unchecked")
    void lookup_wireShape_unchangedFromRouterEra() throws Exception {
        var resp = get("/v1/chash/lookup?chash=" + MULTI.toHex());
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsOnlyKeys("rows", "chash");
        assertThat(body.get("chash")).isEqualTo(MULTI.toHex());
        List<Map<String, String>> rows = (List<Map<String, String>>) body.get("rows");
        assertThat(rows).hasSize(2);
        for (Map<String, String> row : rows) {
            assertThat(row).containsOnlyKeys("collection", "created_at");
            assertThat(row.get("created_at")).matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z");
        }
        assertThat(rows).extracting(r -> r.get("collection"))
            .containsExactlyInAnyOrder(COLL_384, COLL_768);
    }

    @Test
    @SuppressWarnings("unchecked")
    void lookup_unknownChash_emptyRows() throws Exception {
        var resp = get("/v1/chash/lookup?chash=" + ABSENT.toHex());
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat((List<Object>) body.get("rows")).isEmpty();
    }

    @Test
    @SuppressWarnings("unchecked")
    void readEndpoints_keysUnchanged() throws Exception {
        Map<String, Object> reg = mapper.readValue(
            get("/v1/chash/registered_chashes?collection=" + COLL_768).body(), MAP_T);
        assertThat(reg).containsOnlyKeys("chashes");
        assertThat((List<String>) reg.get("chashes"))
            .containsExactlyInAnyOrder(MULTI.toHex(), REF_ONLY.toHex());

        Map<String, Object> distinct = mapper.readValue(
            get("/v1/chash/distinct_collections").body(), MAP_T);
        assertThat(distinct).containsOnlyKeys("collections");
        assertThat((List<String>) distinct.get("collections"))
            .contains(COLL_384, COLL_768);

        Map<String, Object> count = mapper.readValue(
            get("/v1/chash/count_for_collection?collection=" + COLL_768).body(), MAP_T);
        assertThat(count).containsOnlyKeys("count");
        assertThat(count.get("count")).isEqualTo(2);

        Map<String, Object> empty = mapper.readValue(get("/v1/chash/is_empty").body(), MAP_T);
        assertThat(empty).containsOnlyKeys("empty");
        assertThat(empty.get("empty")).isEqualTo(false);
    }

    // ── DEPRECATED writes: old shape + marker + zero DB work ─────────────────

    @Test
    void upsert_acceptsAndNoOps_withDeprecationMarker() throws Exception {
        long before = chunkRowCount();
        var resp = post("/v1/chash/upsert",
            "{\"chash\":\"" + ABSENT.toHex() + "\",\"collection\":\"" + COLL_384 + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsOnlyKeys("ok", "deprecated");
        assertThat(body.get("ok")).isEqualTo(true);
        assertThat(body.get("deprecated")).isEqualTo(true);
        assertThat(chunkRowCount()).as("no database work").isEqualTo(before);
        // The no-op'd chash stays unresolvable — nothing was registered anywhere.
        Map<String, Object> lookup = mapper.readValue(
            get("/v1/chash/lookup?chash=" + ABSENT.toHex()).body(), MAP_T);
        assertThat((List<?>) lookup.get("rows")).isEmpty();
    }

    @Test
    void upsertMany_acceptsAndNoOps_countIsAcceptedCount() throws Exception {
        long before = chunkRowCount();
        var resp = post("/v1/chash/upsert_many",
            "{\"chashes\":[\"" + ABSENT.toHex() + "\",\"" + MULTI.toHex() + "\"]," +
            "\"collection\":\"" + COLL_384 + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsOnlyKeys("ok", "count", "deprecated");
        assertThat(body.get("count")).isEqualTo(2);
        assertThat(body.get("deprecated")).isEqualTo(true);
        assertThat(chunkRowCount()).isEqualTo(before);
    }

    @Test
    void import_returnsHonestZero_withDeprecationMarker() throws Exception {
        long before = chunkRowCount();
        var resp = post("/v1/chash/import",
            "{\"rows\":[{\"chash\":\"" + ABSENT.toHex() + "\",\"collection\":\"" + COLL_384 +
            "\",\"created_at\":\"2025-06-01T10:30:00Z\"}]}");
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsOnlyKeys("imported", "deprecated");
        assertThat(body.get("imported"))
            .as("honest: nothing was persisted — never a fabricated success count")
            .isEqualTo(0);
        assertThat(body.get("deprecated")).isEqualTo(true);
        assertThat(chunkRowCount()).isEqualTo(before);
    }

    @Test
    void deleteCollection_noOps_andDoesNotDestroyChunkContent() throws Exception {
        long before = chunkRowCount();
        var resp = post("/v1/chash/delete_collection",
            "{\"collection\":\"" + COLL_768 + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsOnlyKeys("deleted", "deprecated");
        assertThat(body.get("deleted")).isEqualTo(0);
        assertThat(body.get("deprecated")).isEqualTo(true);
        assertThat(chunkRowCount())
            .as("the chash surface must NEVER escalate 'drop routing rows' into 'drop content'")
            .isEqualTo(before);
        // Content still resolves.
        Map<String, Object> lookup = mapper.readValue(
            get("/v1/chash/lookup?chash=" + REF_ONLY.toHex()).body(), MAP_T);
        assertThat((List<?>) lookup.get("rows")).hasSize(1);
    }

    @Test
    void deleteStale_noOps_withDeprecationMarker() throws Exception {
        long before = chunkRowCount();
        var resp = post("/v1/chash/delete_stale",
            "{\"chash\":\"" + MULTI.toHex() + "\",\"collection\":\"" + COLL_384 + "\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsOnlyKeys("deleted", "deprecated");
        assertThat(body.get("deleted")).isEqualTo(0);
        assertThat(body.get("deprecated")).isEqualTo(true);
        assertThat(chunkRowCount()).isEqualTo(before);
    }

    // ── rename_collection stays REAL (Q3) ────────────────────────────────────

    @Test
    @SuppressWarnings("unchecked")
    void renameCollection_isRealAndRehomesChunks() throws Exception {
        // Own fixture collection so read tests elsewhere are unaffected.
        Chash renamed = Chash.ofText("wire-rename");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                "VALUES ('" + TENANT + "', 'wire-ren-src') ON CONFLICT DO NOTHING");
            chunk(su, 384, "wire-ren-src", renamed, "2026-07-02 08:00:04+00");
        }
        var resp = post("/v1/chash/rename_collection",
            "{\"old\":\"wire-ren-src\",\"new\":\"wire-ren-dst\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsOnlyKeys("updated");
        assertThat(body.get("updated")).isEqualTo(1);

        Map<String, Object> lookup = mapper.readValue(
            get("/v1/chash/lookup?chash=" + renamed.toHex()).body(), MAP_T);
        List<Map<String, String>> rows = (List<Map<String, String>>) lookup.get("rows");
        assertThat(rows).extracting(r -> r.get("collection")).containsExactly("wire-ren-dst");
    }

    // ── validation still fails loud during the window ────────────────────────

    @Test
    void validation_survivesTheDeprecationWindow() throws Exception {
        // Malformed chash on a deprecated write: still 400, not a silent ok.
        assertThat(post("/v1/chash/upsert",
            "{\"chash\":\"not-hex\",\"collection\":\"c\"}").statusCode()).isEqualTo(400);
        assertThat(post("/v1/chash/upsert_many",
            "{\"chashes\":[\"zz\"],\"collection\":\"c\"}").statusCode()).isEqualTo(400);
        assertThat(post("/v1/chash/import",
            "{\"rows\":[{\"chash\":\"beef\",\"collection\":\"c\"}]}").statusCode()).isEqualTo(400);
        // Missing required fields.
        assertThat(post("/v1/chash/upsert",
            "{\"chash\":\"" + MULTI.toHex() + "\"}").statusCode()).isEqualTo(400);
        assertThat(post("/v1/chash/delete_collection", "{}").statusCode()).isEqualTo(400);
        assertThat(get("/v1/chash/lookup").statusCode()).isEqualTo(400);
        // Wrong method.
        assertThat(get("/v1/chash/upsert").statusCode()).isEqualTo(405);
        assertThat(post("/v1/chash/lookup", "{}").statusCode()).isEqualTo(405);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void chunk(Connection su, int dim, String collection, Chash chash,
                       String createdAt) throws Exception {
        StringBuilder vec = new StringBuilder("'[1");
        for (int i = 1; i < dim; i++) vec.append(",0");
        vec.append("]'");
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_" + dim +
            " (tenant_id, collection, chash, chunk_text, embedding, created_at) VALUES " +
            "('" + TENANT + "', '" + collection + "', decode('" + chash.toHex() + "', 'hex'), " +
            "'wire chunk " + chash.toHex().substring(0, 8) + "', " + vec + "::vector, " +
            "TIMESTAMPTZ '" + createdAt + "')");
    }

    /** Total chunk rows across the three dim tables — the no-op invariant. */
    private long chunkRowCount() throws Exception {
        try (Connection su = pg.createConnection("");
             ResultSet rs = su.createStatement().executeQuery(
                "SELECT (SELECT count(*) FROM nexus.chunks_384) + " +
                "       (SELECT count(*) FROM nexus.chunks_768) + " +
                "       (SELECT count(*) FROM nexus.chunks_1024)")) {
            rs.next();
            return rs.getLong(1);
        }
    }

    private HttpResponse<String> get(String path) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET()
            .build();
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
