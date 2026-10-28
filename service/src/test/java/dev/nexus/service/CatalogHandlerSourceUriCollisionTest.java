// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
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
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-231 (nexus-bivz3.7, nexus-bivz3.8): a move whose note {@code source_uri} rewrite would land on a URI a live
 * document already holds answers 409 over HTTP, and the body names the holder, on every route that moves a
 * collection: {@code /update}, {@code /update_many}, {@code /collections/rehome} and {@code /collections/rename}.
 * The repository tests pin the exception; these pin the {@code CatalogHandler} dispatch that maps it, which a
 * removed {@code catch} would turn into an opaque 500.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerSourceUriCollisionTest {

    private static final String TOKEN = "catalog-uri-collision-handler-token-ghi789";
    private static final String SVC_ROLE = "svc_cat_uri_coll";
    private static final String SVC_PASS = "svc_cat_uri_coll_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;
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
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        repo = new CatalogRepository(new TenantScope(svcDs));
        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
        // The tenant's first request runs the once-per-process ghost sweep, which deletes registered-but-chunkless
        // collections; burn it before any collection is registered (see CatalogHandlerRenameTest).
        http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/catalog/collections/list")
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void update_aCollectionMoveOntoATakenUri_is409NamingTheHolder() throws Exception {
        String a = coll("hu-a");
        String b = coll("hu-b");
        register(a, b);
        seedNote("hu-mover", a, "nx-note://" + a + "/t");
        seedNote("hu-holder", b, "nx-note://" + b + "/t");

        var resp = post("/v1/catalog/update", "{\"tumbler\":\"hu-mover\",\"physical_collection\":\"" + b + "\"}");

        assertRefusal(resp, "hu-holder");
        assertThat(uri("hu-mover")).isEqualTo("nx-note://" + a + "/t");
    }

    @Test
    void updateMany_aCollectionMoveOntoATakenUri_is409NamingTheHolder() throws Exception {
        String a = coll("hm-a");
        String b = coll("hm-b");
        register(a, b);
        seedNote("hm-free", a, "nx-note://" + a + "/free");
        seedNote("hm-mover", a, "nx-note://" + a + "/t");
        seedNote("hm-holder", b, "nx-note://" + b + "/t");

        var resp = post("/v1/catalog/update_many", "{\"updates\":["
            + "{\"tumbler\":\"hm-free\",\"physical_collection\":\"" + b + "\"},"
            + "{\"tumbler\":\"hm-mover\",\"physical_collection\":\"" + b + "\"}]}");

        assertRefusal(resp, "hm-holder");
        assertThat(uri("hm-free")).as("the whole request is refused").isEqualTo("nx-note://" + a + "/free");
    }

    @Test
    void rehome_ontoATakenUri_is409NamingTheHolder() throws Exception {
        String src = coll("hr-src");
        String dst = coll("hr-dst");
        register(src, dst);
        seedNote("hr-mover", src, "nx-note://" + src + "/t");
        seedNote("hr-holder", dst, "nx-note://" + dst + "/t");

        var resp = post("/v1/catalog/collections/rehome", "{\"source\":\"" + src + "\",\"target\":\"" + dst + "\"}");

        assertRefusal(resp, "hr-holder");
        assertThat(uri("hr-mover")).isEqualTo("nx-note://" + src + "/t");
    }

    @Test
    void rename_ontoATakenUri_is409NamingTheHolder() throws Exception {
        String oldName = coll("hn-old");
        String newName = coll("hn-new");
        String elsewhere = coll("hn-else");
        register(oldName, elsewhere);
        seedNote("hn-mover", oldName, "nx-note://" + oldName + "/t");
        seedNote("hn-holder", elsewhere, "nx-note://" + newName + "/t");

        var resp = post("/v1/catalog/collections/rename",
            "{\"old_name\":\"" + oldName + "\",\"new_name\":\"" + newName + "\"}");

        assertRefusal(resp, "hn-holder");
        assertThat(uri("hn-mover")).isEqualTo("nx-note://" + oldName + "/t");
    }

    private void assertRefusal(HttpResponse<String> resp, String holder) throws Exception {
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(409);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat((String) body.get("error")).contains("would rewrite a document's source_uri")
            .contains("live document " + holder + " ");
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json)).build(), HttpResponse.BodyHandlers.ofString());
    }

    private static String coll(String slug) {
        return "knowledge__" + slug + "__minilm-l6-v2-384__v1";
    }

    private void register(String... names) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String n : names) PgContainerHelper.insertCollection(dsl, TENANT, n);
        }
    }

    private void seedNote(String tumbler, String collection, String uri) {
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "knowledge",
            "corpus", "knowledge", "physical_collection", collection, "source_uri", uri));
    }

    private String uri(String tumbler) {
        return new TenantScope(svcDs).withTenant(TENANT, ctx -> ctx.select(CATALOG_DOCUMENTS.SOURCE_URI)
            .from(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq(tumbler)).fetchOne(0, String.class));
    }
}
