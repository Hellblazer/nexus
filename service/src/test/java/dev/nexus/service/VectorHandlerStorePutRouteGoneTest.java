// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-nt76j: {@code POST /v1/vectors/store-put}, the single-chunk put, is retired. No released client
 * from v7.68.0 on sends it, and the engine already refused its ownerless writes. What is pinned here is the
 * route's answer: with a valid token it is 410 Gone with an actionable body (name the route as retired,
 * upgrade conexus, restart long-lived sessions), as the retired upsert-reference-only arm answers, for a
 * well-formed body that would once have been accepted and for an empty one. It is not the default arm's 404.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerStorePutRouteGoneTest {

    private static final String TOKEN    = "tok-spg-tenant-a-0123456789abcdef000000";
    private static final String SVC_ROLE = "svc_spg";
    private static final String SVC_PASS = "svc_spg_pass";
    private static final String TENANT   = "spg-tenant-a";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "spg-test-a");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        FakeEmbedder embedder = new FakeEmbedder(1024);
        PgVectorRepository repo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> post(String json) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/store-put")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build(), HttpResponse.BodyHandlers.ofString());
    }

    private static void assertRetiredBody(HttpResponse<String> r) {
        assertThat(r.statusCode()).as("body: %s", r.body()).isEqualTo(410);
        // A restored handler answers 200 {"id":..} or its own 4xx text; the default arm answers
        // {"error":"not found"} with 404. Neither carries the route name and the upgrade instruction.
        assertThat(r.body())
            .contains("POST /v1/vectors/store-put is retired")
            .contains("Upgrade conexus")
            .contains("restart")
            .contains("nx-mcp");
    }

    @Test
    void storePut_answers410Gone_withAnActionableBody() throws Exception {
        assertRetiredBody(post("{\"collection\":\"knowledge__spg__voyage-context-3__v1\","
            + "\"doc_id\":\"" + "a".repeat(64) + "\",\"content\":\"text\",\"metadata\":{}}"));
        assertRetiredBody(post("{}"));
    }
}
