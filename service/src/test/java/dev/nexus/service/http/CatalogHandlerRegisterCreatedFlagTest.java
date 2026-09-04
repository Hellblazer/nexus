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
 * nexus-vfef0: {@code POST /v1/catalog/doc/register} and {@code
 * /doc/register_many} must carry an ADDITIVE {@code created} boolean (per
 * tumbler) so a caller can tell "this call minted a brand-new row" from
 * "this call handed back a pre-existing (idempotency-hit or race-loser)
 * row" — pre-fix, the wire response carried no such signal at all, so a
 * caller's compensating rollback on a later failure (the client-side
 * {@code rollback_minted_catalog_entry}) could not distinguish a genuine
 * mint from a race loser and could delete a WINNER's live, already-
 * populated document (see {@code CatalogRepositoryTest}'s and {@code
 * Catalog016SourceUriUniqueTest}'s repo-level coverage of the same
 * contract via {@link CatalogRepository#registerDocumentWithOutcome} /
 * {@link CatalogRepository#registerDocumentManyWithOutcome}).
 *
 * <p>Hermetic: Testcontainers PG (real {@link CatalogRepository}); drives
 * the handler directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange} (same pattern as {@code
 * CatalogHandlerUpdateManyDeleteManyTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerRegisterCreatedFlagTest {

    private static final String SVC_ROLE = "svc_cat_reg_created_test";
    private static final String SVC_PASS = "svc_cat_reg_created_test_pass";
    private static final String TENANT   = "cat-reg-created-tenant";
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
    void docRegister_freshDoc_envelopeCarriesCreatedTrue() throws Exception {
        CapturingExchange ex = post("/v1/catalog/doc/register",
            "{\"owner_prefix\":\"regcf.1\",\"title\":\"fresh\",\"content_type\":\"code\","
            + "\"corpus\":\"code\",\"file_path\":\"fresh.py\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body).containsKey("tumbler");
        assertThat(body.get("tumbler")).isEqualTo("regcf.1.1");
        assertThat(body.get("created")).as("additive created field, fresh insert").isEqualTo(true);
    }

    @Test
    void docRegister_sourceUriIdempotencyHit_envelopeCarriesCreatedFalse() throws Exception {
        final String uri = "file:///regcf/srcuri-hit.md";
        CapturingExchange first = post("/v1/catalog/doc/register",
            "{\"owner_prefix\":\"regcf.2\",\"title\":\"orig\",\"content_type\":\"rdr\","
            + "\"corpus\":\"rdr\",\"file_path\":\"orig.md\",\"source_uri\":\"" + uri + "\"}");
        handleWithTenant(first);
        assertThat(first.status).isEqualTo(200);
        Map<String, Object> firstBody = MAPPER.readValue(first.bodyString(), Map.class);
        assertThat(firstBody.get("created")).isEqualTo(true);

        // Same source_uri, different file_path -> idempotency-leg hit, not a mint.
        CapturingExchange second = post("/v1/catalog/doc/register",
            "{\"owner_prefix\":\"regcf.2\",\"title\":\"renamed\",\"content_type\":\"rdr\","
            + "\"corpus\":\"rdr\",\"file_path\":\"renamed.md\",\"source_uri\":\"" + uri + "\"}");
        handleWithTenant(second);
        assertThat(second.status).isEqualTo(200);
        Map<String, Object> secondBody = MAPPER.readValue(second.bodyString(), Map.class);
        assertThat(secondBody.get("tumbler")).isEqualTo(firstBody.get("tumbler"));
        assertThat(secondBody.get("created"))
            .as("idempotency-leg hit must NOT report created=true — the row predates this call")
            .isEqualTo(false);
    }

    @Test
    void registerMany_mixedNewAndExisting_createdArrayAlignsWithTumblers() throws Exception {
        // Pre-register one doc directly through the repo so the batch call
        // below resolves it via the pre-batch idempotency lookup.
        var pre = repo.registerDocumentWithOutcome(TENANT, "regcf.3", Map.of(
            "title", "keep", "content_type", "code", "corpus", "code", "file_path", "keep.py"));
        assertThat(pre.created()).isTrue();

        CapturingExchange ex = post("/v1/catalog/doc/register_many",
            "{\"owner_prefix\":\"regcf.3\",\"docs\":["
            + "{\"title\":\"keep-again\",\"content_type\":\"code\",\"corpus\":\"code\",\"file_path\":\"keep.py\"},"
            + "{\"title\":\"new1\",\"content_type\":\"code\",\"corpus\":\"code\",\"file_path\":\"new1.py\"}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        List<?> tumblers = (List<?>) body.get("tumblers");
        List<?> created = (List<?>) body.get("created");
        assertThat(tumblers).hasSize(2);
        assertThat(created).as("additive created array, positionally aligned with tumblers").hasSize(2);
        assertThat(tumblers.get(0)).isEqualTo(pre.tumbler());
        assertThat(created.get(0)).as("pre-existing doc, resolved via batch idempotency lookup").isEqualTo(false);
        assertThat(created.get(1)).as("genuinely new doc in this batch").isEqualTo(true);
        assertThat(tumblers.get(1)).isNotEqualTo(pre.tumbler());
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
