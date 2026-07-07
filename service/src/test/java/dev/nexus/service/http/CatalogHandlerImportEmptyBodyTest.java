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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-zbci5 (GH conexus-tsye) — {@code POST /v1/catalog/import/{owner,document,
 * link,collection}} return a clean 400 on an EMPTY request body instead of a
 * generic 500.
 *
 * <p>Root cause: on an empty body, {@code readBody} returns {@code Map.of()};
 * since it has no {@code "rows"} key, the handler fell through to
 * {@code rows = List.of(body)} — a ONE-element list containing an EMPTY map —
 * which then failed deep inside the repo batch-import (uncaught, surfaced as a
 * bare 500). {@code requireNonEmptyImportBody} now fails loud before the repo
 * is ever called.
 *
 * <p>Hermetic: Testcontainers PG (real {@link CatalogRepository}); drives each
 * handler directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange} — no HTTP server or auth layer needed (the guard is
 * handler-internal, matching {@code AspectHandlerEnqueueErrorTest}'s pattern).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerImportEmptyBodyTest {

    private static final String SVC_ROLE = "svc_cat_import_empty_test";
    private static final String SVC_PASS = "svc_cat_import_empty_test_pass";
    private static final String TENANT   = "cat-import-empty-tenant";

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
            for (String table : List.of("catalog_owners", "catalog_documents", "catalog_links", "catalog_collections")) {
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
    void importOwner_emptyBody_returns400() throws Exception {
        assertEmptyBody400("/v1/catalog/import/owner");
    }

    @Test
    void importDocument_emptyBody_returns400() throws Exception {
        assertEmptyBody400("/v1/catalog/import/document");
    }

    @Test
    void importLink_emptyBody_returns400() throws Exception {
        assertEmptyBody400("/v1/catalog/import/link");
    }

    @Test
    void importCollection_emptyBody_returns400() throws Exception {
        assertEmptyBody400("/v1/catalog/import/collection");
    }

    @Test
    void importOwner_validBody_stillReturns200() throws Exception {
        // Non-regression: the empty-body guard must not disturb the happy path.
        CapturingExchange ex = post("/v1/catalog/import/owner",
            "{\"tumbler_prefix\":\"9\",\"name\":\"zbci5-owner\",\"owner_type\":\"curator\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("valid single-row body still imports").isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"imported\":1");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void assertEmptyBody400(String path) throws Exception {
        CapturingExchange ex = post(path, "");
        handleWithTenant(ex);
        assertThat(ex.status).as(path + " on empty body").isEqualTo(400);
        assertThat(ex.bodyString()).contains("request body required");
    }

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
