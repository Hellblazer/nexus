// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
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
 * nexus-8tnz2 fix-round-2 EXTENSION — HTTP coverage for {@code GET
 * /v1/catalog/docs/collection-counts} and the brand-new sibling {@code GET
 * /v1/catalog/docs/collection-counts-all}
 * ({@link dev.nexus.service.http.CatalogHandler#handleDocsCollectionCounts} /
 * {@link dev.nexus.service.http.CatalogHandler#handleDocsCollectionCountsAll}).
 *
 * <p>The repo-level query correctness (live-only vs. all-rows counting) is
 * exhaustively covered by {@code CatalogRepositoryTest}'s
 * {@code collectionDocCountsIncludingDeleted_*} tests. What THIS test proves
 * is the thing those cannot: the two counts are reachable ONLY via two
 * DISTINCT route paths, and the OLD route silently ignores an
 * {@code ?include_deleted=true} query param rather than honoring it — the
 * exact behavior a pre-upgrade engine exhibits, and the reason the
 * substantive-critic round-2 finding required a brand-new route (a 404 on an
 * old engine) instead of a query-param toggle on the existing one (a silent
 * 200 with stale-shaped data on an old engine).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerCollectionCountsTest {

    private static final String TOKEN    = "catalog-coll-counts-token-def012";
    private static final String SVC_ROLE = "svc_cat_coll_counts";
    private static final String SVC_PASS = "svc_cat_coll_counts_pass";
    private static final String TENANT   = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    private static final String LIVE_ONLY_COLL = "hcc__live-only__voyage__v1";
    private static final String TOMBSTONED_COLL = "hcc__tombstoned-only__voyage__v1";

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;
    CatalogRepository repo;

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
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        // Seed via the repo directly (same pattern as CatalogRepositoryTest):
        // one collection with a LIVE document, one whose only document is
        // soft-tombstoned.
        var tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", "hcc.1", "title", "Live Doc",
            "content_type", "knowledge", "physical_collection", LIVE_ONLY_COLL
        ));
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", "hcc.2", "title", "Soon Tombstoned",
            "content_type", "knowledge", "physical_collection", TOMBSTONED_COLL
        ));
        int deleted = repo.deleteDocument(TENANT, "hcc.2");
        assertThat(deleted).isEqualTo(1);

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
    void oldRoute_defaultOmitsTombstonedOnlyCollection() throws Exception {
        var resp = get("/v1/catalog/docs/collection-counts");
        assertThat(resp.statusCode()).isEqualTo(200);
        var counts = countsOf(resp);
        assertThat(counts).containsEntry(LIVE_ONLY_COLL, 1);
        assertThat(counts).doesNotContainKey(TOMBSTONED_COLL);
    }

    @Test
    void oldRoute_ignoresIncludeDeletedQueryParam() throws Exception {
        // nexus-8tnz2 fix-round-2 EXTENSION: this is the load-bearing
        // assertion the critic's round-2 finding demanded. The OLD route
        // must behave EXACTLY as it does at base (byte-identical to
        // 5abc4a4a9) regardless of any query param a caller appends —
        // proving a pre-upgrade engine would silently return live-only
        // data rather than honoring (or erroring on) include_deleted, which
        // is precisely why the all-rows count now lives on its own path
        // instead of a query-param toggle here.
        var resp = get("/v1/catalog/docs/collection-counts?include_deleted=true");
        assertThat(resp.statusCode()).isEqualTo(200);
        var counts = countsOf(resp);
        assertThat(counts).containsEntry(LIVE_ONLY_COLL, 1);
        assertThat(counts).doesNotContainKey(TOMBSTONED_COLL);
    }

    @Test
    void newRoute_includesTombstonedOnlyCollection() throws Exception {
        var resp = get("/v1/catalog/docs/collection-counts-all");
        assertThat(resp.statusCode()).isEqualTo(200);
        var counts = countsOf(resp);
        assertThat(counts).containsEntry(LIVE_ONLY_COLL, 1);
        assertThat(counts).containsEntry(TOMBSTONED_COLL, 1);
    }

    @Test
    void newRoute_post_returns405() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/catalog/docs/collection-counts-all"))
            .header("Authorization", "Bearer " + TOKEN)
            .header("X-Nexus-Tenant", TENANT)
            .POST(HttpRequest.BodyPublishers.noBody())
            .build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(405);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Number> countsOf(HttpResponse<String> resp) throws Exception {
        var body = mapper.readValue(resp.body(), MAP_T);
        return (Map<String, Number>) body.get("counts");
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
