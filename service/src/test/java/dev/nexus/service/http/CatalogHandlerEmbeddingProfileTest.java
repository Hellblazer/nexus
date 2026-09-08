// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
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
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 2 (bead nexus-ft04v.33, engine half) — {@code GET
 * /v1/catalog/embedding_profile} returns the calling tenant's {@code
 * nexus.embedding_profile} rows and nothing else.
 *
 * <p>Three contracts from the bead: one row per content type for a tenant
 * whose profile the engine wrote (the boot seed, {@link
 * EmbedderRouter#seedEmbeddingProfile}); an EMPTY list for a tenant with no
 * profile yet, never a default; and RLS isolation — a tenant sees only its
 * own rows, in both directions. Plus the method guard.
 *
 * <p>Driven directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange}, the {@code CatalogHandlerCollectionUpsertProfileSeedTest}
 * idiom. Voyage-mode router with a dummy key: this path never embeds, only
 * {@code modelToken()} reads. Hermetic: Testcontainers pgvector, requires
 * Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerEmbeddingProfileTest {

    private static final String SVC_ROLE = "svc_embedding_profile_route_test";
    private static final String SVC_PASS = "svc_embedding_profile_route_test_pass";
    private static final ObjectMapper JSON = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
    EmbedderRouter router;
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
        router = new EmbedderRouter("dummy-key", "document");
        handler = new CatalogHandler(repo, new CombinedWriteService(tenantScope, repo, router));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void bootSeededTenant_getsOneRowPerContentType_withTheRoutersModelAndItsDimension() throws Exception {
        String tenant = "profile-route-seeded-1";
        router.seedEmbeddingProfile(tenantScope, tenant);
        Map<String, String> expected = router.contentTypeModelTokens();
        // Non-vacuity: the boot seed must have produced a real vocabulary to compare against.
        assertThat(expected).as("the router's content-type vocabulary is the seed").hasSizeGreaterThanOrEqualTo(4);

        JsonNode body = get(tenant);
        assertThat(body.get("count").asInt()).isEqualTo(expected.size());
        List<String> contentTypes = new ArrayList<>();
        for (JsonNode row : body.get("profile")) {
            String ct = row.get("content_type").asText();
            contentTypes.add(ct);
            assertThat(row.get("embedding_model").asText())
                .as("row for %s carries the router's token", ct)
                .isEqualTo(expected.get(ct));
            assertThat(row.get("dimension").asInt())
                .as("dimension is the embedding_models reference row's, not a client guess")
                .isEqualTo(dimensionOf(expected.get(ct)));
        }
        assertThat(contentTypes).containsExactlyInAnyOrderElementsOf(expected.keySet());
        assertThat(contentTypes).as("deterministic wire order: by content_type").isSorted();
    }

    @Test
    void unprofiledTenant_getsEmptyList_neverADefault() throws Exception {
        JsonNode body = get("profile-route-unprofiled-1");
        assertThat(body.get("count").asInt()).isZero();
        assertThat(body.get("profile").isArray()).isTrue();
        assertThat(body.get("profile")).isEmpty();
    }

    @Test
    void rls_aTenantSeesOnlyItsOwnRows_inBothDirections() throws Exception {
        String full = "profile-route-rls-full";
        String partial = "profile-route-rls-partial";
        router.seedEmbeddingProfile(tenantScope, full);
        int fullSize = router.contentTypeModelTokens().size();
        assertThat(fullSize).isGreaterThanOrEqualTo(4);

        // Before the partial tenant has any row: the full tenant's rows do not leak to it.
        assertThat(get(partial).get("count").asInt()).isZero();

        // One row for the partial tenant; each side sees exactly its own.
        router.seedEmbeddingProfileForContentType(tenantScope, partial, "code");
        JsonNode partialBody = get(partial);
        assertThat(partialBody.get("count").asInt()).isEqualTo(1);
        assertThat(partialBody.get("profile").get(0).get("content_type").asText()).isEqualTo("code");
        assertThat(get(full).get("count").asInt()).isEqualTo(fullSize);
    }

    @Test
    void post_isRefused405() throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create("/v1/catalog/embedding_profile"), "{}");
        handleWithTenant(ex, "profile-route-method-1");
        assertThat(ex.status).isEqualTo(405);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private JsonNode get(String tenant) throws Exception {
        CapturingExchange ex = new CapturingExchange("GET", URI.create("/v1/catalog/embedding_profile"), "");
        handleWithTenant(ex, tenant);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        return JSON.readTree(ex.bodyString());
    }

    private int dimensionOf(String model) {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Integer dim = ctx.select(EMBEDDING_MODELS.DIMENSION).from(EMBEDDING_MODELS)
                    .where(EMBEDDING_MODELS.EMBEDDING_MODEL.eq(model))
                    .fetchOne(EMBEDDING_MODELS.DIMENSION);
            assertThat(dim).as("embedding_models carries %s", model).isNotNull();
            return dim;
        } catch (Exception e) {
            throw new RuntimeException(e);
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
