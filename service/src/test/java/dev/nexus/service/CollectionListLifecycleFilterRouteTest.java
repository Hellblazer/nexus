// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.jooq.binding.Vector;
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

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-bc7ps, the wire pin: BOTH collection-list routes serve the full inventory by
 * default, honour {@code lifecycle_state=<state>} as an exact match (so a routing
 * consumer that sends {@code live} never receives a {@code quarantine-<name>} row),
 * treat {@code all} as absent, and refuse an unknown filter with a 400 that names the
 * accepted set instead of returning a silently empty list.
 *
 * <ul>
 *   <li>{@code GET /v1/catalog/collections/list} — the registry.</li>
 *   <li>{@code GET /v1/vectors/stats} — the physical inventory taxonomy discovery
 *       enumerates.</li>
 * </ul>
 *
 * <p>Why a route test and not only the repository pins: the defect this closes was a
 * quarantine-<name> row reaching a client parser through these exact routes, so the
 * contract that matters is what the wire serves for each filter value.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS, real {@link NexusService}
 * on port 0, plain NOSUPERUSER svc role (RLS-subject).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CollectionListLifecycleFilterRouteTest {

    private static final String TOKEN    = "bc7ps-list-route-test-token";
    private static final String SVC_ROLE = "svc_bc7ps_list_test";
    private static final String SVC_PASS = "svc_bc7ps_list_test_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;

    private static final String LIVE       = "code__bc7ps-owner__voyage-code-3__v1";
    private static final String QUARANTINE = "quarantine-code__bc7ps-owner__voyage-code-3__v1";

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};
    private static final TypeReference<List<Map<String, Object>>> LIST_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper = new ObjectMapper();

    @BeforeAll
    void startAll() throws Exception {
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
        // The stats route needs the pgvector repository; collectionStats never
        // touches an embedder, so null routers suffice (VectorStatsCatalogJoinTest).
        var pgRepo = new dev.nexus.service.vectors.PgVectorRepository(
            new dev.nexus.service.db.TenantScope(svcDs), null, null);
        service = new NexusService(0, TOKEN, svcDs, null, null, pgRepo);
        service.start();
        http = TestHttp.client();

        // Both rows carry a chunk, so the first-request ghost sweep (RDR-204 P1) has
        // nothing to delete and both have a collection_vector_stats row to list or hide.
        seedChunk(LIVE, "a1");
        seedChunk(QUARANTINE, "b2");
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null)   svcDs.close();
        if (pg != null)      pg.stop();
    }

    // ── /v1/catalog/collections/list ───────────────────────────────────────────

    @Test
    void catalogList_defaultIsEverything_liveIsExact_unknownIs400() throws Exception {
        var dflt = get("/v1/catalog/collections/list");
        assertThat(dflt.statusCode()).isEqualTo(200);
        assertThat(catalogNames(dflt)).as("default = full inventory").contains(LIVE, QUARANTINE);

        var all = get("/v1/catalog/collections/list?lifecycle_state=all");
        assertThat(catalogNames(all)).as("'all' is absent").isEqualTo(catalogNames(dflt));
        var blank = get("/v1/catalog/collections/list?lifecycle_state=");
        assertThat(catalogNames(blank)).as("blank is absent").isEqualTo(catalogNames(dflt));

        var live = get("/v1/catalog/collections/list?lifecycle_state=live");
        assertThat(catalogNames(live)).contains(LIVE).doesNotContain(QUARANTINE);
        var q = get("/v1/catalog/collections/list?lifecycle_state=quarantine");
        assertThat(catalogNames(q)).containsExactly(QUARANTINE);

        var bad = get("/v1/catalog/collections/list?lifecycle_state=quarantined");
        assertThat(bad.statusCode()).isEqualTo(400);
        assertThat(bad.body()).contains("quarantined").contains("live").contains("all");
    }

    // ── /v1/vectors/stats ──────────────────────────────────────────────────────

    @Test
    void vectorStats_defaultIsEverything_liveIsExact_unknownIs400() throws Exception {
        var dflt = get("/v1/vectors/stats");
        assertThat(dflt.statusCode()).isEqualTo(200);
        assertThat(statsNames(dflt)).as("default = full inventory").contains(LIVE, QUARANTINE);

        var all = get("/v1/vectors/stats?lifecycle_state=all");
        assertThat(statsNames(all)).as("'all' is absent").isEqualTo(statsNames(dflt));

        var live = get("/v1/vectors/stats?lifecycle_state=live");
        assertThat(statsNames(live)).contains(LIVE).doesNotContain(QUARANTINE);
        var q = get("/v1/vectors/stats?lifecycle_state=quarantine");
        assertThat(statsNames(q)).containsExactly(QUARANTINE);

        var bad = get("/v1/vectors/stats?lifecycle_state=Live");
        assertThat(bad.statusCode()).isEqualTo(400);
        assertThat(bad.body()).contains("Live").contains("quarantine").contains("all");
    }

    // ── helpers ────────────────────────────────────────────────────────────────

    private List<String> catalogNames(HttpResponse<String> resp) throws Exception {
        @SuppressWarnings("unchecked")
        var rows = (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("collections");
        return rows.stream().map(r -> (String) r.get("name")).toList();
    }

    private List<String> statsNames(HttpResponse<String> resp) throws Exception {
        return mapper.readValue(resp.body(), LIST_T).stream().map(r -> (String) r.get("name")).toList();
    }

    /** Register {@code collection} and give it one 1024-dim chunk, through typed jOOQ DSL
     *  (the raw-SQL ratchet in RawSqlGateTest admits no new raw execute() in test sources). */
    private void seedChunk(String collection, String seed) throws Exception {
        byte[] chash = java.security.MessageDigest.getInstance("SHA-256")
            .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        float[] v = new float[1024];
        java.util.Arrays.fill(v, 0.1f);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, collection);
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                           CHUNKS.EMBEDDING_1024)
               .values(TENANT, collection, chash, "bc7ps chunk", Vector.of(v))
               .onConflictDoNothing()
               .execute();
        }
    }

    private HttpResponse<String> get(String path) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
