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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-l3dg2 (du6d0 residual, engine half) — HTTP layer test for
 * {@code POST /v1/taxonomy/meta/last_discover_batch}.
 *
 * <p>Route/JSON-contract coverage: returns stamps for known collections,
 * omits unknown ones, tenant isolation, and the size-cap 400. The
 * "absent means never discovered" and GREATEST-conflict semantics are
 * pinned at the repository layer by
 * {@code TaxonomyRepositoryTest#getLastDiscoverStamps_*}. Hermetic
 * Testcontainers PG, driven directly via {@link TaxonomyHandler#handle}
 * with a capturing {@link HttpExchange} (same idiom as
 * {@code TaxonomyHandlerAssignFromChashesTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyHandlerLastDiscoverBatchTest {

    private static final String SVC_ROLE = "svc_ldb_http_test";
    private static final String SVC_PASS = "svc_ldb_http_test_pass";
    private static final String TENANT_A = "ldb-http-tenant-a";
    private static final String TENANT_B = "ldb-http-tenant-b";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TaxonomyRepository repo;
    TaxonomyHandler handler;
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
        repo = new TaxonomyRepository(tenantScope);
        handler = new TaxonomyHandler(repo, null);  // centroid repo unused by this route
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void returnsStampsForKnownCollections_omitsUnknown() throws Exception {
        String known = "knowledge__ldb-known-" + System.nanoTime();
        String unknown = "knowledge__ldb-unknown-" + System.nanoTime();
        registerCollection(TENANT_A, known);
        registerCollection(TENANT_A, unknown);  // registered, never discovered
        repo.recordDiscoverCount(TENANT_A, known, 9, "2026-09-10T00:00:00Z");

        CapturingExchange ex = post("/v1/taxonomy/meta/last_discover_batch",
            "{\"collections\":[\"" + known + "\",\"" + unknown + "\"]}");
        handleWithTenant(ex, TENANT_A);

        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"collection\":\"" + known + "\"");
        assertThat(body).contains("\"last_discover_at\":\"2026-09-10T00:00:00Z\"");
        assertThat(body).contains("\"last_discover_doc_count\":9");
        assertThat(body).doesNotContain(unknown);
    }

    @Test
    void tenantIsolation_doesNotLeakOtherTenantsStamp() throws Exception {
        String col = "knowledge__ldb-iso-" + System.nanoTime();
        registerCollection(TENANT_A, col);
        registerCollection(TENANT_B, col);
        repo.recordDiscoverCount(TENANT_A, col, 1, "2026-01-01T00:00:00Z");
        repo.recordDiscoverCount(TENANT_B, col, 2, "2026-02-02T00:00:00Z");

        CapturingExchange ex = post("/v1/taxonomy/meta/last_discover_batch",
            "{\"collections\":[\"" + col + "\"]}");
        handleWithTenant(ex, TENANT_A);

        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"last_discover_doc_count\":1");
        assertThat(body).doesNotContain("\"last_discover_doc_count\":2");
    }

    @Test
    void missingCollections_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/meta/last_discover_batch", "{}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void emptyCollectionsArray_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/meta/last_discover_batch", "{\"collections\":[]}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void tooManyCollections_returns400() throws Exception {
        StringBuilder json = new StringBuilder("[");
        for (int i = 0; i <= TaxonomyRepository.MAX_LAST_DISCOVER_BATCH; i++) {
            if (i > 0) json.append(',');
            json.append("\"knowledge__ldb-cap-").append(i).append('"');
        }
        json.append(']');

        CapturingExchange ex = post("/v1/taxonomy/meta/last_discover_batch",
            "{\"collections\":" + json + "}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status)
            .as("MAX_LAST_DISCOVER_BATCH + 1 collections must be rejected 400 at the HTTP layer")
            .isEqualTo(400);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────

    private void registerCollection(String tenant, String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), tenant, collection);
        }
    }

    private void handleWithTenant(CapturingExchange ex, String tenant) throws Exception {
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
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
