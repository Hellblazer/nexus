// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TenantConstants;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-ndwzk (2): handler-level pins for the machine-readable 400 both
 * {@code POST /v1/catalog/link} and {@code POST /v1/catalog/import/link}
 * return for a dangling endpoint — {@code {error, code:"dangling_endpoint",
 * missing:[...]}} — driven over real HTTP against a live {@code NexusService}
 * (previously covered only at the repository-exception level).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogLinksDanglingEndpointHttpTest {
    private static final String TOKEN = "catalog-links-dangling-http-token-1";
    private static final String SVC_ROLE = "svc_links_http_test";
    private static final String SVC_PASS = "svc_links_http_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String COLLECTION = "knowledge__links-http__minilm-l6-v2-384__v1";
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository catalogRepo;
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
            // bootstrapServiceRole's Liquibase run leaves su's autoCommit disabled
            // (Liquibase manages its own changeset-boundary commits); re-enable it
            // so this INSERT via seedServiceToken actually commits before su closes.
            su.setAutoCommit(true);
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
        catalogRepo = new CatalogRepository(new TenantScope(svcDs));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "http-src", "title", "http-src", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLLECTION));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "http-dst", "title", "http-dst", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLLECTION));
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
    void link_danglingToTumbler_returns400WithMachineReadableBody() throws Exception {
        var resp = post("/v1/catalog/link",
            "{\"from_tumbler\":\"http-src\",\"to_tumbler\":\"http-missing\",\"link_type\":\"cites\",\"created_by\":\"test\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("code")).isEqualTo("dangling_endpoint");
        assertThat(body.get("missing")).isEqualTo(List.of("to_tumbler"));
        assertThat(String.valueOf(body.get("error"))).isNotBlank();
    }

    @Test
    void importLink_danglingFromTumbler_returns400WithMachineReadableBody() throws Exception {
        var resp = post("/v1/catalog/import/link",
            "{\"rows\":[{\"from_tumbler\":\"http-missing\",\"to_tumbler\":\"http-dst\",\"link_type\":\"cites\",\"created_by\":\"test\"}]}");
        assertThat(resp.statusCode()).isEqualTo(400);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.get("code")).isEqualTo("dangling_endpoint");
        assertThat(body.get("missing")).isEqualTo(List.of("from_tumbler"));
        assertThat(String.valueOf(body.get("error"))).isNotBlank();
    }

    @Test
    void link_bothEndpointsPresent_returns200() throws Exception {
        var resp = post("/v1/catalog/link",
            "{\"from_tumbler\":\"http-src\",\"to_tumbler\":\"http-dst\",\"link_type\":\"cites\",\"created_by\":\"test\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(resp.body()).contains("\"ok\":true");
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
