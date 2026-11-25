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
 * RDR-229 P2.8 (nexus-p2m1x.13), the engine half of item 11: {@code GET
 * /v1/catalog/collections/for_tuple} answers a {@code legacy_grandfathered} row, so a
 * knowledge collection registered under a label that is not of the conformant shape is found
 * by its tuple. Through the real route, over a real Postgres: the repository-level
 * ordering cases live in {@link CatalogRepositoryTest}.
 *
 * <p>The rows are registered the way {@code HttpCatalogClient.register_collection} registers a
 * non-conformant name: {@code POST /collections/upsert} with {@code legacy_grandfathered} true
 * (the client derives the flag from the name until P3.8 stops writing it).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerForTupleTest {

    private static final String TOKEN = "catalog-for-tuple-handler-token-p28";
    private static final String SVC_ROLE = "svc_cat_fortuple";
    private static final String SVC_PASS = "svc_cat_fortuple_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String KNOWLEDGE_MODEL = "voyage-context-3";
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

        // AuthFilter runs the per-tenant, once-per-process ghost sweep on the tenant's first
        // request and deletes any collection registered but chunkless at that moment
        // (CatalogHandlerRenameTest, nexus-ft04v Phase 1). Burn it before registering.
        var warmup = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build();
        http.send(warmup, HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * The acceptance of P2.8 against the real route: before the filter drop this tuple answered
     * 404, because the only row on it is flagged {@code legacy_grandfathered}.
     */
    @Test
    void forTuple_answersAGrandfatheredKnowledgeRowUnderANonConformantLabel() throws Exception {
        var up = post("/v1/catalog/collections/upsert",
            "{\"name\":\"notes-from-2024\",\"content_type\":\"knowledge\",\"owner_id\":\"field-notes\","
                + "\"embedding_model\":\"" + KNOWLEDGE_MODEL + "\",\"model_version\":\"v1\","
                + "\"display_name\":\"\",\"legacy_grandfathered\":true}");
        assertThat(up.statusCode()).as("guard: the row registers").isEqualTo(200);
        var got = get("/v1/catalog/collections/get?name=notes-from-2024");
        assertThat(mapper.readValue(got.body(), MAP_T).get("legacy_grandfathered"))
            .as("guard: the row is flagged legacy_grandfathered").isEqualTo(true);

        var resp = get(forTuple("knowledge", "field-notes", KNOWLEDGE_MODEL));

        assertThat(resp.statusCode()).as("a grandfathered row is a winner of its tuple").isEqualTo(200);
        var row = mapper.readValue(resp.body(), MAP_T);
        assertThat(row.get("name")).isEqualTo("notes-from-2024");
        assertThat(row.get("model_version")).isEqualTo("v1");
        assertThat(row.get("owner_id")).isEqualTo("field-notes");
        assertThat(row.get("content_type")).isEqualTo("knowledge");
    }

    /** A tuple no row backs still answers 404 (the client's "no row" outcome). */
    @Test
    void forTuple_answers404ForAnUnregisteredTuple() throws Exception {
        var resp = get(forTuple("knowledge", "no-such-subject-p28", KNOWLEDGE_MODEL));
        assertThat(resp.statusCode()).isEqualTo(404);
    }

    /**
     * A superseded grandfathered row is still not a candidate: dropping the grandfathered filter
     * leaves the supersession and quarantine filters in place.
     */
    @Test
    void forTuple_stillSkipsASupersededGrandfatheredRow() throws Exception {
        post("/v1/catalog/collections/upsert",
            "{\"name\":\"old-notes-p28\",\"content_type\":\"knowledge\",\"owner_id\":\"retired-notes\","
                + "\"embedding_model\":\"" + KNOWLEDGE_MODEL + "\",\"model_version\":\"v1\","
                + "\"display_name\":\"\",\"legacy_grandfathered\":true}");
        // the successor is v2: a second live row on the old row's tuple is refused by the registration (RDR-229 P3.8)
        post("/v1/catalog/collections/upsert",
            "{\"name\":\"new-notes-p28\",\"content_type\":\"knowledge\",\"owner_id\":\"retired-notes\","
                + "\"embedding_model\":\"" + KNOWLEDGE_MODEL + "\",\"model_version\":\"v2\","
                + "\"display_name\":\"\",\"legacy_grandfathered\":true}");
        var sup = post("/v1/catalog/collections/supersede",
            "{\"name\":\"old-notes-p28\",\"superseded_by\":\"new-notes-p28\"}");
        assertThat(sup.statusCode()).as("guard: the supersession applies").isEqualTo(200);

        var resp = get(forTuple("knowledge", "retired-notes", KNOWLEDGE_MODEL));

        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("name"))
            .as("the live row answers, not the superseded one").isEqualTo("new-notes-p28");
    }

    private static String forTuple(String contentType, String ownerId, String model) {
        return "/v1/catalog/collections/for_tuple?content_type=" + contentType
            + "&owner_id=" + ownerId + "&embedding_model=" + model;
    }

    private HttpResponse<String> get(String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
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
