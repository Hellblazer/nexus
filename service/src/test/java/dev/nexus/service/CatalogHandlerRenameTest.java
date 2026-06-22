// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-164 P3 — HTTP coverage for {@code POST /v1/catalog/collections/rename}
 * ({@link dev.nexus.service.http.CatalogHandler#handleCollectionRename}). The repo-level
 * coherent re-home is exhaustively covered by {@link CatalogRenameCollectionTest}; this
 * exercises the HTTP glue the repo test cannot: the {@code old_name/new_name} canonical
 * keys, the {@code old/new} compat alias, the 400 missing-key guard, the 405 method guard,
 * and the {@code {"renamed": {...}}} response shape.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerRenameTest {

    private static final String TOKEN = "catalog-rename-handler-token-def456";
    private static final String SVC_ROLE = "svc_cat_ren_handler";
    private static final String SVC_PASS = "svc_cat_ren_handler_pass";
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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)));
            lb.update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label) VALUES ('"
                + dev.nexus.service.db.TokenHashing.sha256Hex(TOKEN)
                + "', '" + TENANT + "', 'test-bound') ON CONFLICT (token_hash) DO NOTHING");
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
            // Seed two registry rows to rename (one per route-shape test).
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', 'hren__old')");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" + TENANT + "', 'hren__old-alias')");
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
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void post_canonicalKeys_returns200WithRenamedCounts() throws Exception {
        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"hren__old\",\"new_name\":\"hren__new\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKey("renamed");
        @SuppressWarnings("unchecked")
        Map<String, Object> renamed = (Map<String, Object>) body.get("renamed");
        assertThat(((Number) renamed.get("catalog_collections_inserted")).intValue())
            .as("registry Y inserted via HTTP").isEqualTo(1);
        assertThat(((Number) renamed.get("catalog_collections_deleted")).intValue())
            .as("registry X deleted via HTTP").isEqualTo(1);
    }

    @Test
    void post_oldNewAlias_returns200() throws Exception {
        // The handler accepts old/new as a compat alias for old_name/new_name.
        var resp = post("/v1/catalog/collections/rename",
            "{\"old\":\"hren__old-alias\",\"new\":\"hren__new-alias\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        @SuppressWarnings("unchecked")
        Map<String, Object> renamed = (Map<String, Object>) body.get("renamed");
        assertThat(((Number) renamed.get("catalog_collections_inserted")).intValue())
            .as("alias old/new resolved").isEqualTo(1);
    }

    @Test
    void post_missingKeys_returns400() throws Exception {
        var resp = post("/v1/catalog/collections/rename", "{}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void post_missingNewName_returns400() throws Exception {
        var resp = post("/v1/catalog/collections/rename", "{\"old_name\":\"hren__whatever\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void get_returns405() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/rename"))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(405);
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
