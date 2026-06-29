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
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * PlanHandler HTTP endpoint tests (nexus-82ihm).
 *
 * <p>Regression coverage for the service-mode plan-library seeding defect found
 * during the 6.0.0 local validation pass: {@code GET /v1/plans/get} with a blank
 * {@code project} query param (the valid GLOBAL-SCOPE sentinel — see the Python
 * {@code _default_project_for_scope("global") -> ""} convention and the
 * {@code nexus.plans.project TEXT NOT NULL DEFAULT ''} column) was rejected with
 * HTTP 400 "missing required query param: project". That broke
 * {@code nx catalog setup} plan seeding entirely in service mode, because every
 * global builtin template queries by-dimensions with an empty project.
 *
 * <p>Contract: an EMPTY project is a valid value (404 if the row is absent, 200
 * if present); only an ABSENT project key is a 400.
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), port 0, requires Docker.
 *
 * <p>Isolation invariant (PER_CLASS shares one DB, no per-test cleanup): each
 * test keys on a DISTINCT {@code dimensions} value (the unique index is on
 * {@code (tenant_id, project, dimensions)}), so test order is irrelevant and no
 * test poisons another. A new test MUST pick its own dimensions marker; do NOT
 * add a DELETE/reset step without revisiting this invariant.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PlanHandlerTest {

    private static final String TOKEN = "plan-handler-test-token-abc789";
    private static final String SVC_ROLE = "svc_plan_handler_test";
    private static final String SVC_PASS = "svc_plan_handler_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    /** A canonical dimensions JSON of a global-scope builtin plan. */
    private static final String GLOBAL_DIMS = "{\"scope\":\"global\",\"verb\":\"research\"}";

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
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db);
            liquibase.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.plans TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE ON SEQUENCE nexus.plans_id_seq TO " + SVC_ROLE);
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

    // ── The regression: empty project is the global-scope sentinel ─────────────

    /**
     * An ABSENT project key is still a 400 — the genuinely-missing case stays
     * rejected. (Guards against over-correcting the fix into accepting no key.)
     */
    @Test
    void getByDimensions_absentProjectKey_returns400() throws Exception {
        var resp = get("/v1/plans/get?dimensions=" + enc(GLOBAL_DIMS), TENANT);
        assertThat(resp.statusCode())
            .as("absent project key is a genuine 400")
            .isEqualTo(400);
    }

    /**
     * An EMPTY project value with no matching row is 404 (not found), NOT 400.
     * This is the defect: a blank project is the valid global-scope sentinel, so
     * the by-dimensions lookup must run and return 404 when absent.
     */
    @Test
    void getByDimensions_emptyProject_noRow_returns404() throws Exception {
        // Distinct dimensions never saved by any other test (shared PER_CLASS DB,
        // no per-test cleanup) so this asserts the genuine not-found path.
        String unsavedDims = "{\"scope\":\"global\",\"verb\":\"never-saved-marker\"}";
        var resp = get("/v1/plans/get?project=&dimensions=" + enc(unsavedDims), TENANT);
        assertThat(resp.statusCode())
            .as("blank project (global scope) with no row must be 404, not 400")
            .isEqualTo(404);
    }

    /**
     * After saving a global-scope plan (project=""), the by-dimensions GET with a
     * blank project returns 200 and the stored row.
     */
    @Test
    void getByDimensions_emptyProject_afterSave_returns200() throws Exception {
        var save = post("/v1/plans/save", TENANT,
            "{\"project\":\"\",\"query\":\"global research plan\","
            + "\"plan_json\":\"{\\\"steps\\\":[]}\",\"dimensions\":" + jsonString(GLOBAL_DIMS) + "}");
        assertThat(save.statusCode()).as("save with empty project must succeed").isEqualTo(200);

        var resp = get("/v1/plans/get?project=&dimensions=" + enc(GLOBAL_DIMS), TENANT);
        assertThat(resp.statusCode())
            .as("blank project (global scope) with a saved row must be 200")
            .isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("query")).isEqualTo("global research plan");
    }

    /**
     * A non-empty project still works (no regression for scoped plans).
     */
    @Test
    void getByDimensions_nonEmptyProject_afterSave_returns200() throws Exception {
        String dims = "{\"scope\":\"code__nexus\",\"verb\":\"research\"}";
        var save = post("/v1/plans/save", TENANT,
            "{\"project\":\"code__nexus\",\"query\":\"scoped plan\","
            + "\"plan_json\":\"{\\\"steps\\\":[]}\",\"dimensions\":" + jsonString(dims) + "}");
        assertThat(save.statusCode()).isEqualTo(200);

        var resp = get("/v1/plans/get?project=code__nexus&dimensions=" + enc(dims), TENANT);
        assertThat(resp.statusCode()).isEqualTo(200);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    /** Serialize a string as a JSON string literal (for embedding in a body). */
    private String jsonString(String s) throws Exception {
        return mapper.writeValueAsString(s);
    }

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
