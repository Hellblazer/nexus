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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-cw262 HTTP surface: {@code POST /v1/catalog/owners/deactivate} and
 * {@code POST /v1/catalog/owners/reactivate} — the engine route the 7kl32
 * dead-owner GC mutation arm had nothing to call before this bead. The
 * repository-level behavior (default-exclusion semantics, idempotency, RLS
 * isolation, the auto-reactivate-on-upsert self-heal) is covered by
 * {@code CatalogRepositoryTest}'s {@code nexus-cw262} block; this class pins
 * the route wiring, response envelope, and the {@code include_deactivated}
 * query/body param on {@code /owners/list} and {@code /owners/by_type} — the
 * same split as {@code CatalogHandlerSweepNextSeqDriftTest} vs {@code
 * NextSeqSweepTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerOwnerDeactivateTest {

    private static final String SVC_ROLE = "svc_cat_deact_test";
    private static final String SVC_PASS = "svc_cat_deact_test_pass";
    private static final String TENANT   = "cat-deact-tenant";
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
            for (String table : List.of(
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
    @SuppressWarnings("unchecked")
    void deactivate_thenReactivate_roundTripThroughListAndByType() throws Exception {
        repo.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", "9101", "name", "http-deact-repo", "owner_type", "repo"));

        // Visible before deactivation on both default read routes.
        assertThat(prefixesOf(get("/v1/catalog/owners/list"))).contains("9101");
        assertThat(prefixesOf(postByType("repo", false))).contains("9101");

        CapturingExchange deact = post("/v1/catalog/owners/deactivate",
            "{\"tumbler_prefix\":\"9101\"}");
        handleWithTenant(deact);
        assertThat(deact.status).as("response body: %s", deact.bodyString()).isEqualTo(200);
        Map<String, Object> deactBody = MAPPER.readValue(deact.bodyString(), Map.class);
        assertThat(((Number) deactBody.get("deactivated")).intValue()).isEqualTo(1);

        // Default routes now exclude it.
        assertThat(prefixesOf(get("/v1/catalog/owners/list"))).doesNotContain("9101");
        assertThat(prefixesOf(postByType("repo", false))).doesNotContain("9101");

        // include_deactivated=true still surfaces it on both routes.
        assertThat(prefixesOf(get("/v1/catalog/owners/list?include_deactivated=true"))).contains("9101");
        assertThat(prefixesOf(postByType("repo", true))).contains("9101");

        // Idempotent: a second deactivate is a no-op.
        CapturingExchange deactAgain = post("/v1/catalog/owners/deactivate",
            "{\"tumbler_prefix\":\"9101\"}");
        handleWithTenant(deactAgain);
        Map<String, Object> deactAgainBody = MAPPER.readValue(deactAgain.bodyString(), Map.class);
        assertThat(((Number) deactAgainBody.get("deactivated")).intValue()).isEqualTo(0);

        CapturingExchange react = post("/v1/catalog/owners/reactivate",
            "{\"tumbler_prefix\":\"9101\"}");
        handleWithTenant(react);
        assertThat(react.status).as("response body: %s", react.bodyString()).isEqualTo(200);
        Map<String, Object> reactBody = MAPPER.readValue(react.bodyString(), Map.class);
        assertThat(((Number) reactBody.get("reactivated")).intValue()).isEqualTo(1);

        // Back on the default routes.
        assertThat(prefixesOf(get("/v1/catalog/owners/list"))).contains("9101");
        assertThat(prefixesOf(postByType("repo", false))).contains("9101");
    }

    @Test
    void deactivate_missingTumblerPrefix_is400() throws Exception {
        CapturingExchange ex = post("/v1/catalog/owners/deactivate", "{}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void deactivate_unknownPrefix_returnsZeroNot404() throws Exception {
        // Mirrors /delete's idempotent-zero contract, not a 404 -- an already-gone
        // (or never-existed) owner is not an error for a soft-delete route.
        CapturingExchange ex = post("/v1/catalog/owners/deactivate",
            "{\"tumbler_prefix\":\"no-such-owner\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(((Number) body.get("deactivated")).intValue()).isEqualTo(0);
    }

    @Test
    void deactivate_getIsNotAllowed() throws Exception {
        CapturingExchange ex = new CapturingExchange("GET", URI.create("/v1/catalog/owners/deactivate"), "");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(405);
    }

    @Test
    void reactivate_getIsNotAllowed() throws Exception {
        CapturingExchange ex = new CapturingExchange("GET", URI.create("/v1/catalog/owners/reactivate"), "");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(405);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private List<String> prefixesOf(CapturingExchange ex) throws Exception {
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        var owners = (List<Map<String, Object>>) body.get("owners");
        return owners.stream().map(o -> (String) o.get("tumbler_prefix")).toList();
    }

    private CapturingExchange get(String pathAndQuery) throws Exception {
        CapturingExchange ex = new CapturingExchange("GET", URI.create(pathAndQuery), "");
        handleWithTenant(ex);
        return ex;
    }

    private CapturingExchange postByType(String ownerType, boolean includeDeactivated) throws Exception {
        CapturingExchange ex = post("/v1/catalog/owners/by_type",
            "{\"owner_type\":\"" + ownerType + "\",\"include_deactivated\":" + includeDeactivated + "}");
        handleWithTenant(ex);
        return ex;
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
