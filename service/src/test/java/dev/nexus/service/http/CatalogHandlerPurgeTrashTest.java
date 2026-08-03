// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
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
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-3ck2g code-review Minor, fixed as part of nexus-8j1zx's fix round:
 * {@code CatalogHandler#handlePurgeTrash} previously silently fell back to the
 * default for a PRESENT-but-wrong-typed body field (e.g. {@code "older_than_days":
 * "abc"}) instead of 400ing, unlike its own range-validation ({@code < 1}) which
 * already 400s correctly. Repository-level behavior ({@code purgeTrashPreview}/
 * {@code purgeTrash} semantics, age-independent chunk sweep) is covered by {@code
 * CatalogPurgeTrashTest}; this class pins the handler's own body-validation
 * branching only, same split as {@code CatalogHandlerSweepNextSeqDriftTest} vs its
 * repository-level sibling.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerPurgeTrashTest {

    private static final String SVC_ROLE = "svc_cat_purge_http";
    private static final String SVC_PASS = "svc_cat_purge_http_pass";
    private static final String TENANT   = "cat-purge-http-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

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
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
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
    void malformedOlderThanDaysType_returns400NotSilentDefault() throws Exception {
        CapturingExchange ex = post("{\"older_than_days\": \"abc\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        @SuppressWarnings("unchecked") Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat((String) body.get("error")).contains("older_than_days").contains("integer");
    }

    @Test
    void malformedDryRunType_returns400NotSilentDefault() throws Exception {
        CapturingExchange ex = post("{\"dry_run\": \"yes\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        @SuppressWarnings("unchecked") Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat((String) body.get("error")).contains("dry_run").contains("boolean");
    }

    @Test
    void absentFields_defaultAndReturns200() throws Exception {
        // The ABSENT-field default path must still work — only PRESENT-but-wrong-typed
        // values are rejected.
        CapturingExchange ex = post("{}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        @SuppressWarnings("unchecked") Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat((Boolean) body.get("dry_run")).isTrue();
        assertThat(body).containsKey("documents_purged");
    }

    @Test
    void outOfRangeOlderThanDays_stillReturns400() throws Exception {
        // Pre-existing range validation (< 1) must be unaffected by the type-check addition.
        CapturingExchange ex = post("{\"older_than_days\": 0}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
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

    private static CapturingExchange post(String jsonBody) {
        return new CapturingExchange("POST", URI.create("/v1/catalog/purge-trash"), jsonBody);
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
