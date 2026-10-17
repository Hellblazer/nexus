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
 * nexus-3t0jv: {@code POST /v1/vectors/resolve} and its https fetcher are removed in every
 * mode (Sam, 2026-10-17). The route fetched caller-supplied https URLs. What is pinned here is the route's absence:
 * with a valid token the path falls through to {@code VectorHandler}'s default arm and answers
 * 404, for both request forms the deleted handler took and for a loopback URL (the case the
 * fetcher would have served from inside the engine host).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerResolveRouteGoneTest {

    private static final String TOKEN    = "tok-rrg-tenant-a-0123456789abcdef000000";
    private static final String SVC_ROLE = "svc_rrg";
    private static final String SVC_PASS = "svc_rrg_pass";
    private static final String TENANT   = "rrg-tenant-a";

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
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "rrg-test-a");
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
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/resolve")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build(), HttpResponse.BodyHandlers.ofString());
    }

    private static final String DEFAULT_ARM_BODY = "{\"error\":\"not found\"}";

    @Test
    void everyRequestForm_answers404() throws Exception {
        // The body is pinned too: VectorHandler's default arm answers {"error":"not found"}.
        // The deleted handler's 404s carried its own text ("no chunk found for collection=..."),
        // so a restored handler fails the (collection, chash) form here, not only the URI form.
        var byUri = post("{\"source_uri\":\"https://example.invalid/doc\"}");
        assertThat(byUri.statusCode()).as("body: %s", byUri.body()).isEqualTo(404);
        assertThat(byUri.body()).isEqualTo(DEFAULT_ARM_BODY);

        var byChash = post("{\"collection\":\"knowledge__rrg__voyage-context-3__v1\","
            + "\"chash\":\"" + "a".repeat(64) + "\"}");
        assertThat(byChash.statusCode()).as("body: %s", byChash.body()).isEqualTo(404);
        assertThat(byChash.body()).isEqualTo(DEFAULT_ARM_BODY);

        var loopback = post("{\"source_uri\":\"https://127.0.0.1:" + service.getPort() + "/v1/status\"}");
        assertThat(loopback.statusCode()).as("body: %s", loopback.body()).isEqualTo(404);
        assertThat(loopback.body()).isEqualTo(DEFAULT_ARM_BODY);
    }
}
