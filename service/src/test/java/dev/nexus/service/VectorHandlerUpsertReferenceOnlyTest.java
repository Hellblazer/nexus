// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgVectorRepositoryContractTest.FakeEmbedder;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
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
 * RDR-223 Phase 3 Step 2 (nexus-z0o2p.24): {@code POST /v1/vectors/upsert-reference-only} is
 * RETIRED. It wrote a chunk with no manifest row, which is the ownerless write this phase
 * refuses on every chunk-write route, and once that refusal applied it could not write a NEW
 * chunk at all (the manifest FK wants the chunk first, the refusal wants the manifest first).
 * Sam's condition for retiring it (zero production calls in the 90-day WAF log, 2026-07-03 to
 * 2026-10-01) was met on 2026-10-01.
 *
 * <p>This class used to drive the route over HTTP (malformed-embedding 400s, the full to
 * reference-only 422, tenant isolation). Those behaviours went with the handler, and the repository
 * method under it was deleted too (nexus-z0o2p.36): the engine has no writer of reference-only
 * rows, and tests build them with {@code PgContainerHelper#insertReferenceOnlyChunk} (read path:
 * {@link ReferenceOnlyChunkReadPathTest}). What is pinned here is the route's absence: 410 Gone,
 * as {@code ChashHandler} answers its retired routes, naming the replacement routes, and nothing
 * written.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerUpsertReferenceOnlyTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN    = "tok-uro-tenant-a-0123456789abcdef000000";
    private static final String SVC_ROLE = "svc_uro";
    private static final String SVC_PASS = "svc_uro_pass";
    private static final String TENANT   = "uro-tenant-a";
    private static final String COLLECTION = "knowledge__uro-owner__voyage-context-3__v1";

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
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "uro-test-a");
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

        // Burn the per-tenant ghost sweep before registering the collection (measured
        // ordering trap, see VectorHandlerDeadlineMappingTest's identical comment).
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build(), HttpResponse.BodyHandlers.ofString());

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> send(String method, Object body) throws Exception {
        var builder = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/upsert-reference-only")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json");
        builder = "GET".equals(method)
            ? builder.GET()
            : builder.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)));
        return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void thePostRouteIsGone_410_namingTheReplacementRoutes() throws Exception {
        String chash = Chash.ofText("uro-retired-route").toHex();
        float[] vec = FakeEmbedder.unitVector(1024, 1.0f, 0.0f);
        List<Double> embedding = new java.util.ArrayList<>(vec.length);
        for (float f : vec) embedding.add((double) f);

        var resp = send("POST", Map.of("collection", COLLECTION, "chash", chash, "embedding", embedding));

        assertThat(resp.statusCode()).as("body: %s", resp.body()).isEqualTo(410);
        @SuppressWarnings("unchecked")
        Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
        assertThat((String) body.get("error"))
            .contains("retired")
            .contains("/v1/catalog/manifest/write_many")
            .contains("/v1/catalog/manifest/append");

        // Nothing was written: a physical scan of the collection finds no such chunk.
        var scan = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/get")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(Map.of(
                "collection", COLLECTION, "include_non_live", true, "limit", 300))))
            .build(), HttpResponse.BodyHandlers.ofString());
        assertThat(scan.statusCode()).isEqualTo(200);
        assertThat(scan.body()).doesNotContain(chash);
    }

    @Test
    void aMalformedBodyGets410Too_theRouteDoesNotParseAnything() throws Exception {
        // The retired route answers before it reads the body, so a request that used to be a 400
        // (no embedding) is a 410 now: a caller learns the route is gone, not that it was wrong.
        var resp = send("POST", Map.of("collection", COLLECTION));
        assertThat(resp.statusCode()).isEqualTo(410);
    }

    @Test
    void aGetGets410Too() throws Exception {
        assertThat(send("GET", null).statusCode()).isEqualTo(410);
    }
}
