// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR bead nexus-l46pu (follow-up to nexus-kk4ut) — HTTP coverage for {@code
 * POST /v1/catalog/collections/set_aspects_enabled} ({@link
 * dev.nexus.service.http.CatalogHandler#handleCollectionSetAspectsEnabled}). The
 * repo-level column default / update / tenant-isolation behaviour is covered by
 * {@link CatalogRepositoryTest}'s {@code collection_aspectsEnabled_*} /
 * {@code collection_setAspectsEnabled_*} tests; this exercises the HTTP glue those
 * cannot: the request-shape guards and the {@code {"updated": N}} response shape.
 * Model: {@link CatalogHandlerRenameTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerSetAspectsEnabledTest {

    private static final String TOKEN = "catalog-set-aspects-handler-token-a1b2c3";
    private static final String SVC_ROLE = "svc_cat_asp_handler";
    private static final String SVC_PASS = "svc_cat_asp_handler_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
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
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = TestHttp.client();

        // RDR-204 Phase 1 (bead nexus-ft04v.3): AuthFilter runs the per-tenant,
        // once-per-process ghost sweep on whichever request is TENANT's first
        // against this service instance -- burn it before registering fixtures
        // (same ordering CatalogHandlerRenameTest's startAll depends on).
        var warmup = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build();
        http.send(warmup, HttpResponse.BodyHandlers.ofString());

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "hasp__target");
        }
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void post_setsTrue_returns200AndUpdatedOne() throws Exception {
        assertThat(collectionRow("hasp__target").get("aspects_enabled"))
            .as("guard: starts at the column default").isEqualTo(false);

        var resp = post("/v1/catalog/collections/set_aspects_enabled",
            "{\"name\":\"hasp__target\",\"aspects_enabled\":true}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("updated")).isEqualTo(1);
        assertThat(collectionRow("hasp__target").get("aspects_enabled")).isEqualTo(true);
    }

    @Test
    void post_setsFalse_returns200() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, "hasp__flip");
        }
        assertThat(post("/v1/catalog/collections/set_aspects_enabled",
            "{\"name\":\"hasp__flip\",\"aspects_enabled\":true}").statusCode()).isEqualTo(200);
        assertThat(collectionRow("hasp__flip").get("aspects_enabled")).isEqualTo(true);

        var resp = post("/v1/catalog/collections/set_aspects_enabled",
            "{\"name\":\"hasp__flip\",\"aspects_enabled\":false}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(collectionRow("hasp__flip").get("aspects_enabled")).isEqualTo(false);
    }

    @Test
    void post_missingName_returns400() throws Exception {
        var resp = post("/v1/catalog/collections/set_aspects_enabled", "{\"aspects_enabled\":true}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void post_missingAspectsEnabled_returns400() throws Exception {
        var resp = post("/v1/catalog/collections/set_aspects_enabled", "{\"name\":\"hasp__target\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void post_nonBooleanAspectsEnabled_returns400() throws Exception {
        var resp = post("/v1/catalog/collections/set_aspects_enabled",
            "{\"name\":\"hasp__target\",\"aspects_enabled\":\"yes\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void post_unregisteredCollection_returns404AndWritesNothing() throws Exception {
        var resp = post("/v1/catalog/collections/set_aspects_enabled",
            "{\"name\":\"hasp__never-registered\",\"aspects_enabled\":true}");
        assertThat(resp.statusCode()).isEqualTo(404);
        assertThat(resp.body()).contains("collection not found");
    }

    @Test
    void get_returns405() throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/set_aspects_enabled")
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(405);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private Map<String, Object> collectionRow(String name) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort()
                + "/v1/catalog/collections/get?name=" + name)
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var r = http.send(req, HttpResponse.BodyHandlers.ofString());
        return r.statusCode() == 200 ? mapper.readValue(r.body(), MAP_T) : null;
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
