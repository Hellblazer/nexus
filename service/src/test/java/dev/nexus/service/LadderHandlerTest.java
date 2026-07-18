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
 * RDR-186 bead nexus-146xx.12 (engine half) — LadderHandler endpoint tests.
 *
 * <p>The PG write/read surface for upgrade-ladder rung completion
 * bookkeeping ({@code nexus.ladder_completions}, ladder-001-baseline.xml).
 * The client's {@code HttpLadderStore} (the {@code CompletionLedger}
 * implementation replacing ladder.db) flushes verified facts here once the
 * engine is up and reads {@code verified_rungs()}/{@code completions()} back.
 *
 * <p>Contract notes (RDR-186 D3 / RF-186-2):
 * <ul>
 *   <li>record is an UPSERT on (tenant, rung_name) — overwrite-on-reverify,
 *       mirroring the SQLite ON CONFLICT(rung_name) DO UPDATE semantics;
 *       lossy audit metadata is accepted.</li>
 *   <li>verified_at is stamped SERVER-side (now()) — the client's own clock
 *       is observability-only and may lag the flush.</li>
 *   <li>NO position surface: the endpoint serves raw completion facts; the
 *       client derives position via derive_ladder_position (Gap-4
 *       mechanism 1). No rung ordering, no position field, ever.</li>
 * </ul>
 *
 * <p>Coverage: record→completions round trip; upsert overwrite; RLS
 * isolation through HTTP; missing-field 400; auth 401.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class LadderHandlerTest {

    private static final String TOKEN = "ladder-handler-test-token-abc123";
    private static final String OTHER_TOKEN = "ladder-handler-test-token-def456";
    private static final String SVC_ROLE = "svc_ladder_handler_test";
    private static final String SVC_PASS = "svc_ladder_handler_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String OTHER_TENANT = "ladder-other-tenant";

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
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.ladder_completions TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.service_tokens, nexus.session_tokens TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'test-bound') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(OTHER_TOKEN)
                + "', '" + OTHER_TENANT + "', 'test-bound-other') ON CONFLICT (token_hash) DO NOTHING");
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

    // ── Test 1: record → completions round trip ──────────────────────────────

    @Test
    void record_thenCompletionsRoundTrip() throws Exception {
        var resp = post("/v1/ladder/record", TOKEN, TENANT,
            "{\"rung_name\":\"rt-engine-install\",\"package_version\":\"6.12.0\",\"detail\":\"ok\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("recorded")).isEqualTo(true);

        var completions = completions(TOKEN, TENANT);
        var mine = completions.stream()
            .filter(c -> "rt-engine-install".equals(c.get("rung_name")))
            .toList();
        assertThat(mine).hasSize(1);
        var record = mine.get(0);
        assertThat(record.get("package_version")).isEqualTo("6.12.0");
        assertThat(record.get("detail")).isEqualTo("ok");
        assertThat((String) record.get("verified_at"))
            .as("verified_at is stamped server-side and returned ISO-8601")
            .isNotBlank();
    }

    // ── Test 2: upsert — re-recording overwrites (SQLite parity) ─────────────

    @Test
    void record_reRecordingSameRung_overwrites() throws Exception {
        post("/v1/ladder/record", TOKEN, TENANT,
            "{\"rung_name\":\"up-rung\",\"package_version\":\"6.11.0\",\"detail\":\"first\"}");
        post("/v1/ladder/record", TOKEN, TENANT,
            "{\"rung_name\":\"up-rung\",\"package_version\":\"6.12.0\",\"detail\":\"reverify\"}");

        var mine = completions(TOKEN, TENANT).stream()
            .filter(c -> "up-rung".equals(c.get("rung_name")))
            .toList();
        assertThat(mine)
            .as("upsert on (tenant, rung_name): one row, not two — the SQLite " +
                "ON CONFLICT(rung_name) DO UPDATE overwrite-on-reverify semantics")
            .hasSize(1);
        assertThat(mine.get(0).get("package_version")).isEqualTo("6.12.0");
        assertThat(mine.get(0).get("detail")).isEqualTo("reverify");
    }

    // ── Test 3: detail optional, defaults to '' ──────────────────────────────

    @Test
    void record_withoutDetail_defaultsEmpty() throws Exception {
        var resp = post("/v1/ladder/record", TOKEN, TENANT,
            "{\"rung_name\":\"nd-rung\",\"package_version\":\"6.12.0\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var mine = completions(TOKEN, TENANT).stream()
            .filter(c -> "nd-rung".equals(c.get("rung_name")))
            .toList();
        assertThat(mine.get(0).get("detail")).isEqualTo("");
    }

    // ── Test 4: RLS isolation through HTTP ───────────────────────────────────

    @Test
    void rls_otherTenantsCompletionsInvisible() throws Exception {
        post("/v1/ladder/record", OTHER_TOKEN, OTHER_TENANT,
            "{\"rung_name\":\"other-rung\",\"package_version\":\"6.12.0\"}");

        var visible = completions(TOKEN, TENANT).stream()
            .map(c -> c.get("rung_name"))
            .toList();
        assertThat(visible)
            .as("RLS: the default tenant must not see the other tenant's completions")
            .doesNotContain("other-rung");
    }

    // ── Test 5: validation — missing fields 400 ──────────────────────────────

    @Test
    void record_missingRungName_rejected400() throws Exception {
        var resp = post("/v1/ladder/record", TOKEN, TENANT,
            "{\"package_version\":\"6.12.0\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("rung_name");
    }

    @Test
    void record_missingPackageVersion_rejected400() throws Exception {
        var resp = post("/v1/ladder/record", TOKEN, TENANT,
            "{\"rung_name\":\"x\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("package_version");
    }

    // ── Test 6: auth — 401 without bearer ────────────────────────────────────

    @Test
    void noAuth_rejected401() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/ladder/completions"))
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> completions(String token, String tenant) throws Exception {
        var resp = get("/v1/ladder/completions", token, tenant);
        assertThat(resp.statusCode()).isEqualTo(200);
        return (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("completions");
    }

    private HttpResponse<String> post(String path, String token, String tenant, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path, String token, String tenant) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
