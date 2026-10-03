// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantConstants;
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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.71 at the wire: the four routes that would move or delete a {@code quarantine-} collection's rows
 * with no audit row answer 400 and name the sanctioned verbs, and the collection-delete route takes the origin's
 * quarantine rows with it and reports them. The repository-level behaviour is
 * {@link QuarantineCollectionLifecycleTest}; this is the HTTP mapping that test cannot reach (a refusal that
 * reached the generic catch would read as a 500 and discard the message naming the remedy).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class QuarantineCollectionRouteTest {

    private static final String TOKEN = "quarantine-route-token-0123456789abcdef";
    private static final String SVC_ROLE = "svc_quarantine_route";
    private static final String SVC_PASS = "svc_quarantine_route_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    private static final String ORIGIN = "knowledge__qroute-origin__minilm-l6-v2-384__v1";
    private static final String OTHER = "knowledge__qroute-other__minilm-l6-v2-384__v1";
    private static final String SIBLING = "quarantine-" + ORIGIN;

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;
    String quarantinedHex;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        var tenantScope = new dev.nexus.service.db.TenantScope(svcDs);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        var vecRepo = new dev.nexus.service.vectors.PgVectorRepository(tenantScope, embedder, embedder);
        service = new NexusService(0, TOKEN, svcDs, null, null, vecRepo);
        service.start();
        http = TestHttp.client();

        // Burn the once-per-process ghost sweep before registering anything (it deletes registered but chunkless
        // collections), as CatalogHandlerRehomeTest does and for the same reason.
        var warmup = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN).GET().build();
        http.send(warmup, HttpResponse.BodyHandlers.ofString());

        quarantinedHex = Chash.ofText(SIBLING + "/route").toHex();
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, ORIGIN);
            PgContainerHelper.insertCollection(dsl, TENANT, OTHER);
            PgContainerHelper.insertCollection(dsl, TENANT, SIBLING);
            PgContainerHelper.insertChunks(dsl, TENANT, SIBLING, List.of(quarantinedHex), List.of("route text"),
                List.of(new float[384]),
                List.of(Map.<String, Object>of("origin_collection", ORIGIN, "quarantined_at", "2026-09-01T00:00:00Z")));
        }
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void assertRefusal(HttpResponse<String> resp) {
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(400);
        assertThat(resp.body()).contains(SIBLING).contains("nx t3 quarantine restore").contains("nx t3 gc");
    }

    @Test
    void storeDeleteOnAQuarantineCollection_is400_namingTheVerbs() throws Exception {
        assertRefusal(post("/v1/vectors/store-delete",
            "{\"collection\":\"" + SIBLING + "\",\"ids\":[\"" + quarantinedHex + "\"]}"));
    }

    @Test
    void renameOfAQuarantineCollection_is400_asSourceAndAsTarget() throws Exception {
        assertRefusal(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"" + SIBLING + "\",\"new_name\":\"knowledge__qroute-new__minilm-l6-v2-384__v1\"}"));
        assertRefusal(post("/v1/catalog/collections/rename",
            "{\"old_name\":\"" + ORIGIN + "\",\"new_name\":\"" + SIBLING + "\"}"));
    }

    @Test
    void chashRenameOfAQuarantineCollection_is400() throws Exception {
        assertRefusal(post("/v1/chash/rename_collection",
            "{\"old\":\"" + SIBLING + "\",\"new\":\"" + OTHER + "\"}"));
        assertRefusal(post("/v1/chash/rename_collection",
            "{\"old\":\"" + OTHER + "\",\"new\":\"" + SIBLING + "\"}"));
    }

    @Test
    void rehomeOfAQuarantineCollection_is400_asSourceAndAsTarget() throws Exception {
        assertRefusal(post("/v1/catalog/collections/rehome",
            "{\"source\":\"" + SIBLING + "\",\"target\":\"" + OTHER + "\"}"));
        assertRefusal(post("/v1/catalog/collections/rehome",
            "{\"source\":\"" + OTHER + "\",\"target\":\"" + SIBLING + "\"}"));
    }

    @Test
    void theRefusedRoutesLeftTheRowInPlace() throws Exception {
        // Order-independent of the other tests: they all refuse, so the row is still there whenever this runs.
        var resp = post("/v1/catalog/collections/delete", "{\"name\":\"knowledge__qroute-absent__minilm-l6-v2-384__v1\"}");
        assertThat(resp.statusCode()).isEqualTo(200);
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES)
                .fetchCount(dev.nexus.service.jooq.nexus.Tables.CHUNKS,
                    dev.nexus.service.jooq.nexus.Tables.CHUNKS.COLLECTION.eq(SIBLING))).isEqualTo(1);
        }
    }

    @Test
    void collectionDeleteOfTheOrigin_takesItsQuarantineRows_andReportsThem() throws Exception {
        String origin = "knowledge__qroute-del__minilm-l6-v2-384__v1";
        String sibling = "quarantine-" + origin;
        String hex = Chash.ofText(sibling + "/del").toHex();
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, origin);
            PgContainerHelper.insertCollection(dsl, TENANT, sibling);
            PgContainerHelper.insertChunks(dsl, TENANT, sibling, List.of(hex), List.of("del text"),
                List.of(new float[384]), List.of(Map.<String, Object>of("origin_collection", origin)));
        }

        var resp = post("/v1/catalog/collections/delete", "{\"name\":\"" + origin + "\"}");

        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        var deleted = (Map<String, Object>) mapper.readValue(resp.body(), MAP_T).get("deleted");
        assertThat(((Number) deleted.get("quarantine_chunks")).intValue()).isEqualTo(1);
        var audit = get("/v1/catalog/gc_audit/list?collection=" + sibling);
        assertThat(audit.statusCode()).as(audit.body()).isEqualTo(200);
        assertThat(audit.body()).contains("collection_delete_quarantine").contains(hex);
    }

    private HttpResponse<String> get(String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
