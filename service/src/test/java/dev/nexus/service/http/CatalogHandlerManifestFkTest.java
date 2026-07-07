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
 * RDR-172 follow-up (nexus-7e057) — {@code POST /v1/catalog/manifest/write} and
 * {@code /manifest/append} map a class-23 integrity violation to a typed 409,
 * ahead of the generic 500.
 *
 * <p>{@code writeManifest}/{@code appendManifestChunks} insert into
 * {@code catalog_document_chunks}, whose {@code doc_id} column has a real FK to
 * {@code catalog_documents(tenant_id, tumbler)} (fk-001). A non-blank but
 * UNREGISTERED {@code doc_id} — the exact bug class fixed for AspectHandler in
 * nexus-gfl3y (bug nexus-ov0sw) — previously hit the generic
 * {@code catch (Exception)} → bare 500. {@link HttpUtil#sqlState23} (extracted
 * from {@code AspectHandler}) now catches it first.
 *
 * <p>Hermetic: Testcontainers PG (real {@link CatalogRepository}); drives the
 * handler directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange} (same pattern as {@code AspectHandlerEnqueueErrorTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerManifestFkTest {

    private static final String SVC_ROLE = "svc_cat_manifest_fk_test";
    private static final String SVC_PASS = "svc_cat_manifest_fk_test_pass";
    private static final String TENANT   = "cat-manifest-fk-tenant";

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
    void manifestWrite_unregisteredDocId_returns409_withSqlstate() throws Exception {
        CapturingExchange ex = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"unregistered-tumbler-zzz\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a non-blank UNREGISTERED doc_id violates the chunks FK → typed 409, not 500")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23503\"");
        assertThat(ex.bodyString())
            .contains("\"error\":\"integrity constraint violation\"")
            .doesNotContain("catalog_document_chunks");
    }

    @Test
    void manifestAppend_unregisteredDocId_returns409_withSqlstate() throws Exception {
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"unregistered-tumbler-yyy\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23503\"");
    }

    @Test
    void manifestWrite_registeredDocId_stillReturns200() throws Exception {
        // Non-regression: register the owner + document first, then a valid
        // manifest write must still succeed (the guard doesn't over-fire).
        repo.upsertDocument(TENANT, java.util.Map.of(
            "tumbler", "5.1", "title", "FK test doc", "content_type", "paper",
            "corpus", "knowledge", "physical_collection", "knowledge__fk__v1"));
        CapturingExchange ex = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"5.1\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"cccccccccccccccccccccccccccccccc\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("registered doc_id: manifest write succeeds").isEqualTo(200);
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
