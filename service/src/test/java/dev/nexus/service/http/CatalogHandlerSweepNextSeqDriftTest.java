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
 * nexus-0ehwe item 5, HTTP surface: {@code POST /v1/catalog/owners/sweep_next_seq_drift}
 * floors every drifted owner's {@code next_seq} in the tenant and reports which
 * ones were actually below their high-water mark. The repository-level behavior
 * (floor semantics, tombstone counting, idempotency) is covered by
 * {@code NextSeqSweepTest}; this class only pins the route wiring and response
 * envelope, same split as {@code CatalogHandlerUpdateManyDeleteManyTest} vs its
 * repository-level sibling.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerSweepNextSeqDriftTest {

    private static final String SVC_ROLE = "svc_cat_sweep_test";
    private static final String SVC_PASS = "svc_cat_sweep_test_pass";
    private static final String TENANT   = "cat-sweep-tenant";
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

    private void driftNextSeq(String prefix, long value) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (var ps = su.prepareStatement(
                "UPDATE nexus.catalog_owners SET next_seq = ? WHERE tenant_id = ? AND tumbler_prefix = ?")) {
                ps.setLong(1, value);
                ps.setString(2, TENANT);
                ps.setString(3, prefix);
                assertThat(ps.executeUpdate()).isEqualTo(1);
            }
        }
    }

    @Test
    @SuppressWarnings("unchecked")
    void sweep_floorsDriftedOwnerAndReportsIt() throws Exception {
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", "9001", "name", "http-sweep-drifted", "owner_type", "repo"));
        repo.registerDocument(TENANT, "9001", Map.of(
            "title", "a", "content_type", "code", "source_uri", "file:///httpsweep1/a.md"));
        driftNextSeq("9001", 0);

        CapturingExchange ex = post("/v1/catalog/owners/sweep_next_seq_drift", "{}");
        handleWithTenant(ex);

        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(((Number) body.get("healed")).intValue()).isGreaterThanOrEqualTo(1);
        assertThat(((Number) body.get("checked")).intValue()).isGreaterThanOrEqualTo(1);

        var owners = (List<Map<String, Object>>) body.get("owners");
        assertThat(owners.stream().anyMatch(o -> "9001".equals(o.get("tumbler_prefix")))).isTrue();

        assertThat(((Number) repo.ownerByPrefix(TENANT, "9001").get("next_seq")).longValue()).isEqualTo(1L);
    }

    @Test
    void sweep_getIsNotAllowed() throws Exception {
        CapturingExchange ex = new CapturingExchange("GET", URI.create("/v1/catalog/owners/sweep_next_seq_drift"), "");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(405);
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
