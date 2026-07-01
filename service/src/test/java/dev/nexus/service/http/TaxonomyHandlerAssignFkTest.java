// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TaxonomyRepository;
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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-172 follow-up (nexus-7e057) — {@code POST /v1/taxonomy/assignments/assign}
 * maps a class-23 integrity violation to a typed 409, ahead of the generic 500.
 *
 * <p>{@code TaxonomyRepository.assignTopic} inserts into
 * {@code topic_assignments}, whose {@code topic_id} column has a real FK to
 * {@code topics(id)} (taxonomy-001-baseline). A client-supplied {@code topic_id}
 * that does not exist — the same bug class fixed for AspectHandler in
 * nexus-gfl3y (bug nexus-ov0sw) — previously hit the generic
 * {@code catch (Exception)} → bare 500. {@link HttpUtil#sqlState23} now catches
 * it first.
 *
 * <p>Hermetic: Testcontainers PG (real {@link TaxonomyRepository}); drives the
 * handler directly via {@link TaxonomyHandler#handle} with a capturing
 * {@link HttpExchange}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyHandlerAssignFkTest {

    private static final String SVC_ROLE = "svc_tax_assign_fk_test";
    private static final String SVC_PASS = "svc_tax_assign_fk_test_pass";
    private static final String TENANT   = "tax-assign-fk-tenant";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TaxonomyRepository repo;
    TaxonomyHandler handler;
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
            for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
            }
            su.createStatement().execute("GRANT USAGE ON SEQUENCE nexus.topics_id_seq TO " + SVC_ROLE);
            // insertTopic auto-stubs catalog_collections (RDR-156 P0.2).
            su.createStatement().execute("GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
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
        repo = new TaxonomyRepository(tenantScope);
        handler = new TaxonomyHandler(repo, null);  // centroid repo unused by /assignments/assign
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void assign_nonexistentTopicId_returns409_withSqlstate() throws Exception {
        // assigned_by != "projection" takes the plain-insert branch (no
        // collection auto-stub needed) — isolates the topic_id FK exactly.
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign",
            "{\"doc_id\":\"some-doc\",\"topic_id\":999999,\"assigned_by\":\"manual\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a nonexistent topic_id violates topics(id) FK → typed 409, not 500")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23503\"");
        assertThat(ex.bodyString())
            .contains("\"error\":\"integrity constraint violation\"")
            .doesNotContain("topic_assignments");
    }

    @Test
    void assign_existingTopicId_stillReturns200() throws Exception {
        // Non-regression: register a real topic first, then a valid assignment
        // must still succeed (the guard doesn't over-fire).
        long topicId = repo.insertTopic(TENANT, "fk-test-topic", null, "coll", 0, "2026-07-01T00:00:00Z", null);
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign",
            "{\"doc_id\":\"some-doc\",\"topic_id\":" + topicId + ",\"assigned_by\":\"manual\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("existing topic_id: assignment succeeds").isEqualTo(200);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void handleWithTenant(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false));
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
