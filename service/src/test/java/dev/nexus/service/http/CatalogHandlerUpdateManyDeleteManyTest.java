// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-xedhp stacked-review follow-up (code-review-expert Important #1):
 * {@code POST /v1/catalog/update_many}/{@code /delete_many} must 400 on a
 * malformed BODY SHAPE (non-list, non-object/non-string element) rather than
 * silently filtering the offending entries and returning a false 200 with a
 * shrunken response array — mirrors the existing
 * {@code handleManifestWriteMany} review #2 fix. This is exactly the
 * handler-layer validation gap the review found: the repository-layer tests
 * in {@code CatalogRepositoryTest} call {@code updateDocumentsMany}/
 * {@code deleteDocumentsMany} directly and never exercise the HTTP body
 * parsing this test drives.
 *
 * <p>Hermetic: Testcontainers PG (real {@link CatalogRepository}); drives the
 * handler directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange} (same pattern as {@code CatalogHandlerManifestFkTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerUpdateManyDeleteManyTest {

    private static final String SVC_ROLE = "svc_cat_umdm_test";
    private static final String SVC_PASS = "svc_cat_umdm_test_pass";
    private static final String TENANT   = "cat-umdm-tenant";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String table : java.util.List.of(
                    "catalog_owners", "catalog_documents", "catalog_document_chunks", "catalog_collections")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
            }
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new CatalogRepository(tenantScope);
        handler = new CatalogHandler(repo);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void updateMany_nonListBody_returns400() throws Exception {
        CapturingExchange ex = post("/v1/catalog/update_many", "{\"updates\":\"not-a-list\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("must be a list");
    }

    @Test
    void updateMany_nonObjectElement_returns400() throws Exception {
        CapturingExchange ex = post("/v1/catalog/update_many", "{\"updates\":[\"not-an-object\"]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("must be an object");
    }

    @Test
    void updateMany_mixedValidAndInvalidElement_400sWithoutSilentlyDropping() throws Exception {
        // The whole-batch shape validation must reject BEFORE any row is
        // processed — a client bug (wrong element type) must not silently
        // shrink the response to just the valid entries.
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "6.1", "title", "Valid", "content_type", "code", "corpus", "code"));
        CapturingExchange ex = post("/v1/catalog/update_many",
            "{\"updates\":[{\"tumbler\":\"6.1\",\"head_hash\":\"abc\"}, \"not-an-object\"]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        // Confirm no partial write happened despite the valid-looking first entry.
        assertThat(repo.getDocument(TENANT, "6.1").get("head_hash")).isNotEqualTo("abc");
    }

    @Test
    void updateMany_validBody_returns200WithAlignedCounts() throws Exception {
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "6.2", "title", "Valid2", "content_type", "code", "corpus", "code"));
        CapturingExchange ex = post("/v1/catalog/update_many",
            "{\"updates\":[{\"tumbler\":\"6.2\",\"head_hash\":\"xyz\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"updated\":[1]");
        assertThat(repo.getDocument(TENANT, "6.2").get("head_hash")).isEqualTo("xyz");
    }

    @Test
    void deleteMany_nonListBody_returns400() throws Exception {
        CapturingExchange ex = post("/v1/catalog/delete_many", "{\"tumblers\":\"not-a-list\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("must be a list");
    }

    @Test
    void deleteMany_nonStringElement_returns400() throws Exception {
        CapturingExchange ex = post("/v1/catalog/delete_many", "{\"tumblers\":[123]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("must be a string");
    }

    @Test
    void deleteMany_validBody_returns200WithTombstonedTumblers() throws Exception {
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "6.3", "title", "ToDelete", "content_type", "code", "corpus", "code"));
        CapturingExchange ex = post("/v1/catalog/delete_many", "{\"tumblers\":[\"6.3\"]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("6.3");
        assertThat(repo.getDocument(TENANT, "6.3")).isNull();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void handleWithTenant(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange post(String path, String jsonBody) {
        return new CapturingExchange("POST", URI.create(path), jsonBody);
    }

    /** Minimal {@link HttpExchange} that captures the response status + body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
