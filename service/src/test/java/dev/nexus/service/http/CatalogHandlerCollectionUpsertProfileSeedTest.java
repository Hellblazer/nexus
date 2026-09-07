// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedderRouter;
import org.jooq.DSLContext;
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
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 1 (bead nexus-ft04v.6) — proves the LAZY, per-cloud-tenant
 * {@code embedding_profile} seed fires through the REAL registration path
 * (the client's {@code register_collection()} -> {@code POST
 * /v1/catalog/collections/upsert} -> {@link
 * CatalogHandler#handleCollectionUpsert}), not merely by calling {@link
 * EmbedderRouter#seedEmbeddingProfileForContentType} directly (that
 * router-level contract is covered separately by {@code
 * EmbedderRouterEmbeddingProfileSeedTest}).
 *
 * <p>Driven directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange} — same idiom as {@code
 * CatalogHandlerSweepAndChashesManyTest} / {@code CatalogHandlerManifestFkTest}.
 * A voyage-mode {@link EmbedderRouter} (dummy key — no embed call happens on
 * this path, only {@code modelToken()} reads) wired through a real {@link
 * CombinedWriteService}, exactly as {@code NexusService} wires {@link
 * CatalogHandler} in production when an {@code EmbedderRouter} is present.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerCollectionUpsertProfileSeedTest {

    private static final String SVC_ROLE = "svc_profile_seed_http_test";
    private static final String SVC_PASS = "svc_profile_seed_http_test_pass";

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

        // Voyage-mode, no local ONNX -- the production cloud shape
        // (nexus-0n7uc). Dummy key: this path never calls embed(), only
        // modelToken() reads (construction touches no network).
        EmbedderRouter router = new EmbedderRouter("dummy-key", "document");
        var combinedWriteService = new CombinedWriteService(tenantScope, repo, router);
        handler = new CatalogHandler(repo, combinedWriteService);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private Map<String, Object[]> profileRows(String tenant) {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            var rows = ctx.select(EMBEDDING_PROFILE.CONTENT_TYPE, EMBEDDING_PROFILE.EMBEDDING_MODEL,
                                   EMBEDDING_PROFILE.DIMENSION)
                    .from(EMBEDDING_PROFILE)
                    .where(EMBEDDING_PROFILE.TENANT_ID.eq(tenant))
                    .fetch();
            Map<String, Object[]> out = new java.util.LinkedHashMap<>();
            for (var r : rows) {
                out.put(r.get(EMBEDDING_PROFILE.CONTENT_TYPE),
                        new Object[] {r.get(EMBEDDING_PROFILE.EMBEDDING_MODEL), r.get(EMBEDDING_PROFILE.DIMENSION)});
            }
            return out;
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    @Test
    void registeringACollection_lazilySeedsExactlyOneProfileRow_forThatContentType() throws Exception {
        String tenant = "cloud-shaped-tenant-http-1";
        assertThat(profileRows(tenant)).as("no profile before any registration").isEmpty();

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"code__" + tenant + "__voyage-code-3__v1\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).isEqualTo(200);

        var rows = profileRows(tenant);
        assertThat(rows.keySet())
            .as("registering a 'code' collection must seed ONLY the 'code' profile row")
            .containsExactly("code");
        assertThat(rows.get("code")[0]).isEqualTo("voyage-code-3");
        assertThat(rows.get("code")[1]).isEqualTo(1024);
    }

    @Test
    void registeringASecondContentType_addsOnlyThatRow_leavesFirstUntouched() throws Exception {
        String tenant = "cloud-shaped-tenant-http-2";

        CapturingExchange first = post("/v1/catalog/collections/upsert",
            "{\"name\":\"code__" + tenant + "__voyage-code-3__v1\",\"content_type\":\"code\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-code-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(first, tenant);
        assertThat(first.status).isEqualTo(200);

        CapturingExchange second = post("/v1/catalog/collections/upsert",
            "{\"name\":\"knowledge__" + tenant + "__voyage-context-3__v1\",\"content_type\":\"knowledge\","
            + "\"owner_id\":\"" + tenant + "\",\"embedding_model\":\"voyage-context-3\","
            + "\"model_version\":\"v1\"}");
        handleWithTenant(second, tenant);
        assertThat(second.status).isEqualTo(200);

        var rows = profileRows(tenant);
        assertThat(rows.keySet()).containsExactlyInAnyOrder("code", "knowledge");
        assertThat(rows.get("code")[0]).isEqualTo("voyage-code-3");
        assertThat(rows.get("knowledge")[0]).isEqualTo("voyage-context-3");
    }

    @Test
    void registeringWithNoContentType_seedsNoProfileRow() throws Exception {
        String tenant = "cloud-shaped-tenant-http-3";

        CapturingExchange ex = post("/v1/catalog/collections/upsert",
            "{\"name\":\"legacy-name-" + tenant + "\"}");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).isEqualTo(200);

        assertThat(profileRows(tenant))
            .as("no content_type in the request -- nothing to seed a profile row against")
            .isEmpty();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

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
        @Override public com.sun.net.httpserver.HttpPrincipal getPrincipal() { return null; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
    }
}
