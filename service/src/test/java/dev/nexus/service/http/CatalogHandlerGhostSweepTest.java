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
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
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

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * POST /v1/catalog/ghost-sweep (nexus-29drn): the operator-facing HTTP route
 * backing {@code nx catalog sweep-ghosts}. Body validation is pinned here,
 * same split as {@code CatalogHandlerPurgeTrashTest} vs its repository-level
 * sibling ({@code GhostSweepDormantMarkingTest} pins the classification
 * predicate itself). Also pins the one thing only the HTTP layer can prove:
 * the wire response shape (snake_case keys, {@code ghost_names}/{@code
 * dormant_names} arrays) and that dry-run truly leaves the row untouched
 * end to end through the handler, not just through the repository method.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerGhostSweepTest {

    private static final String SVC_ROLE = "svc_cat_ghost_http";
    private static final String SVC_PASS = "svc_cat_ghost_http_pass";
    private static final String TENANT   = "cat-ghost-http-tenant";
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
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
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
    void wrongMethod_returns405() throws Exception {
        CapturingExchange ex = new CapturingExchange("GET", URI.create("/v1/catalog/ghost-sweep"), "");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(405);
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
    void absentBody_defaultsToDryRunTrue_andReturns200WithFullShape() throws Exception {
        CapturingExchange ex = post("{}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        @SuppressWarnings("unchecked") Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat((Boolean) body.get("dry_run")).isTrue();
        assertThat(body).containsKeys(
            "scanned", "ghosts_deleted", "marked_dormant", "quarantine_held",
            "ghost_names", "dormant_names", "dry_run");
    }

    @Test
    void dryRunTrue_reportsGhost_butLeavesRowInPlace() throws Exception {
        // nexus-29drn review fixup: a PER-TEST tenant, not the shared class-level
        // TENANT -- the sweep scans the WHOLE tenant, so a dry-run row left behind
        // by this test (correctly, since dry-run must not delete it) would still
        // be there for a LATER test sharing the same tenant to trip over.
        String tenant = "cat-ghost-http-dryrun-tenant";
        String coll = "knowledge__gs-http-dryrun__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, coll);
        }

        CapturingExchange ex = post("{\"dry_run\": true}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        @SuppressWarnings("unchecked") Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat((Integer) body.get("ghosts_deleted")).isEqualTo(1);
        @SuppressWarnings("unchecked") List<String> ghostNames = (List<String>) body.get("ghost_names");
        assertThat(ghostNames).contains(coll);

        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))))
                .as("dry-run over HTTP must not delete the row").isTrue();
        }
    }

    @Test
    void dryRunFalse_actuallyReclaims() throws Exception {
        String tenant = "cat-ghost-http-apply-tenant";
        String coll = "knowledge__gs-http-apply__minilm-l6-v2-384__v1";
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, coll);
        }

        CapturingExchange ex = post("{\"dry_run\": false}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        @SuppressWarnings("unchecked") Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat((Boolean) body.get("dry_run")).isFalse();
        assertThat((Integer) body.get("ghosts_deleted")).isEqualTo(1);

        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(ctx.fetchExists(ctx.selectOne().from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(coll))))
                .as("dry_run=false over HTTP must actually delete the row").isFalse();
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void handleWithTenant(CapturingExchange ex) throws Exception {
        handleWithTenant(ex, TENANT);
    }

    private void handleWithTenant(CapturingExchange ex, String tenant) throws Exception {
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange post(String jsonBody) {
        return new CapturingExchange("POST", URI.create("/v1/catalog/ghost-sweep"), jsonBody);
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
