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
 * nexus-v3w9n Amendment 2: the tumbler grammar (owner prefix exactly 2
 * segments, document tumbler 3 or more) is enforced at the HTTP boundary —
 * {@link CatalogHandler}'s {@code rejectIfBadOwnerPrefix} /
 * {@code rejectIfBadDocumentTumbler}, backed by {@link TumblerGrammar} —
 * NOT by a schema CHECK (deferred to nexus-ia69x). Every validated route
 * gets one refusal case (400, {@code rule=tumbler-grammar}, naming the
 * offending field and value) and one accepted-conforming-shape case.
 *
 * <p>Validated routes: legacy {@code POST /register} (both the owner half
 * via {@code tumbler_prefix} and the document half via {@code tumbler}),
 * {@code POST /owners/upsert}, {@code POST /import/owner} (per-row {@code
 * tumbler_prefix}), {@code POST /import/document} (per-row {@code tumbler}
 * — fix round 1, substantive-critic ship-blocker: this route was missing
 * entirely), {@code POST /doc/register} and {@code /doc/register_many}
 * (the required {@code owner_prefix} unconditionally, plus an optional
 * explicit {@code tumbler} when present). See {@link TumblerGrammar}'s own
 * javadoc for the full route-by-route VALIDATED/LOOKUP-ONLY table.
 *
 * <p>Hermetic: Testcontainers PG (real {@link CatalogRepository}); drives
 * the handler directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange}, same pattern as {@code
 * CatalogHandlerRegisterCreatedFlagTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerTumblerGrammarTest {

    private static final String SVC_ROLE = "svc_cat_grammar_test";
    private static final String SVC_PASS = "svc_cat_grammar_test_pass";
    private static final String TENANT   = "cat-grammar-tenant";
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
            PgContainerHelper.grantServiceSchemaAccess(su, SVC_ROLE);
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

    // ── /owners/upsert ──────────────────────────────────────────────────────

    @Test
    void ownersUpsert_explicitOneSegmentPrefix_400sNamingRule() throws Exception {
        CapturingExchange ex = post("/v1/catalog/owners/upsert",
            "{\"tumbler_prefix\":\"7777\",\"name\":\"http-one-seg\",\"owner_type\":\"repo\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler_prefix");
        assertThat(body.get("value")).isEqualTo("7777");
    }

    @Test
    void ownersUpsert_conformingPrefix_succeeds() throws Exception {
        CapturingExchange ex = post("/v1/catalog/owners/upsert",
            "{\"tumbler_prefix\":\"1.7776\",\"name\":\"http-conforming\",\"owner_type\":\"repo\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
    }

    // ── legacy /register — owner half ───────────────────────────────────────

    @Test
    void legacyRegister_explicitOneSegmentOwnerPrefix_400sNamingRule() throws Exception {
        CapturingExchange ex = post("/v1/catalog/register",
            "{\"tumbler_prefix\":\"7778\",\"name\":\"http-legacy-one-seg\",\"owner_type\":\"repo\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler_prefix");
    }

    @Test
    void legacyRegister_conformingOwnerPrefix_succeeds() throws Exception {
        CapturingExchange ex = post("/v1/catalog/register",
            "{\"tumbler_prefix\":\"1.7781\",\"name\":\"http-legacy-conforming\",\"owner_type\":\"repo\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
    }

    // ── legacy /register — document half ────────────────────────────────────

    @Test
    void legacyRegister_explicitTwoSegmentDocumentTumbler_400sNamingRule() throws Exception {
        CapturingExchange ex = post("/v1/catalog/register",
            "{\"tumbler\":\"1.7779\",\"title\":\"illegal doc shape\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler");
        assertThat(body.get("value")).isEqualTo("1.7779");
    }

    @Test
    void legacyRegister_conformingDocumentTumbler_succeeds() throws Exception {
        CapturingExchange ex = post("/v1/catalog/register",
            "{\"tumbler\":\"1.7782.1\",\"title\":\"conforming doc shape\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
    }

    // ── /import/owner ────────────────────────────────────────────────────────

    @Test
    void importOwner_explicitOneSegmentPrefix_400sNamingRule() throws Exception {
        CapturingExchange ex = post("/v1/catalog/import/owner",
            "{\"tumbler_prefix\":\"7783\",\"name\":\"import-one-seg\",\"owner_type\":\"curator\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler_prefix");
        assertThat(body.get("value")).isEqualTo("7783");
    }

    @Test
    void importOwner_conformingPrefix_succeeds() throws Exception {
        CapturingExchange ex = post("/v1/catalog/import/owner",
            "{\"tumbler_prefix\":\"1.7784\",\"name\":\"import-conforming\",\"owner_type\":\"curator\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
    }

    // ── /import/document (fix round 1: ship-blocker gap closed) ────────────────

    @Test
    void importDocument_explicitTwoSegmentTumbler_400sNamingRule() throws Exception {
        CapturingExchange ex = post("/v1/catalog/import/document",
            "{\"tumbler\":\"1.7790\",\"title\":\"import doc bad shape\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler");
        assertThat(body.get("value")).isEqualTo("1.7790");
    }

    @Test
    void importDocument_conformingTumbler_succeeds() throws Exception {
        CapturingExchange ex = post("/v1/catalog/import/document",
            "{\"tumbler\":\"1.7791.1\",\"title\":\"import doc conforming\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
    }

    // ── /doc/register ────────────────────────────────────────────────────────

    @Test
    void docRegister_malformedOwnerPrefix_400sNamingRule() throws Exception {
        // owner_prefix is ALWAYS explicit/required on this route (never the
        // owners/upsert auto-mint path) -- validated unconditionally.
        CapturingExchange ex = post("/v1/catalog/doc/register",
            "{\"owner_prefix\":\"7785\",\"title\":\"via doc register\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler_prefix");
        assertThat(body.get("value")).isEqualTo("7785");
    }

    @Test
    void docRegister_explicitTwoSegmentTumbler_400sNamingRule() throws Exception {
        // owner_prefix conforms; the OPTIONAL explicit "tumbler" field does not.
        CapturingExchange ex = post("/v1/catalog/doc/register",
            "{\"owner_prefix\":\"1.7786\",\"tumbler\":\"1.7786\",\"title\":\"explicit bad tumbler\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler");
    }

    @Test
    void docRegister_conformingOwnerPrefix_succeeds() throws Exception {
        CapturingExchange ex = post("/v1/catalog/doc/register",
            "{\"owner_prefix\":\"1.7787\",\"title\":\"conforming\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(((String) body.get("tumbler"))).startsWith("1.7787.");
    }

    // ── /doc/register_many ──────────────────────────────────────────────────

    @Test
    void docRegisterMany_malformedOwnerPrefix_400sNamingRule() throws Exception {
        CapturingExchange ex = post("/v1/catalog/doc/register_many",
            "{\"owner_prefix\":\"7788\",\"docs\":[{\"title\":\"a\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(400);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("rule")).isEqualTo("tumbler-grammar");
        assertThat(body.get("field")).isEqualTo("tumbler_prefix");
    }

    @Test
    void docRegisterMany_conformingOwnerPrefix_succeeds() throws Exception {
        CapturingExchange ex = post("/v1/catalog/doc/register_many",
            "{\"owner_prefix\":\"1.7789\",\"docs\":[{\"title\":\"a\"},{\"title\":\"b\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat((java.util.List<?>) body.get("tumblers")).hasSize(2);
    }

    // ── helpers (mirrors CatalogHandlerRegisterCreatedFlagTest) ────────────────

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
