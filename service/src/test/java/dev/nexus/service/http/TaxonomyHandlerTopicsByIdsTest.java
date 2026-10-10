// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
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
import java.util.HashSet;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-w032x — HTTP layer test for {@code POST /v1/taxonomy/topics/by_ids},
 * the batched counterpart of {@code GET /topics/by_id}: search's topic
 * grouping used to make about forty by_id GETs per default search.
 *
 * <p>Covers the happy path (same object shape as by_id), missing ids omitted,
 * cross-tenant isolation, bad bodies and the 300-id cap. Hermetic
 * Testcontainers PG, driven directly via {@link TaxonomyHandler#handle} with a
 * capturing {@link HttpExchange} (same idiom as
 * {@code TaxonomyHandlerLastDiscoverBatchTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyHandlerTopicsByIdsTest {

    private static final String SVC_ROLE = "svc_tbi_http_test";
    private static final String SVC_PASS = "svc_tbi_http_test_pass";
    private static final String TENANT_A = "tbi-http-tenant-a";
    private static final String TENANT_B = "tbi-http-tenant-b";
    private static final String COL = "knowledge__tbi-col";

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

        registerCollection(TENANT_A, COL);
        registerCollection(TENANT_B, COL);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void returnsTheSameObjectByIdReturns_forEveryKnownId() throws Exception {
        long a = repo.insertTopic(TENANT_A, "alpha", null, COL, 3, null, "x,y");
        long b = repo.insertTopic(TENANT_A, "beta", a, COL, 5, null, null);

        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids", "{\"ids\":[" + a + "," + b + "]}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(200);
        JsonNode rows = TaxonomyHandler.MAPPER.readTree(ex.bodyString());
        assertThat(rows.isArray()).isTrue();
        assertThat(rows).hasSize(2);

        for (JsonNode row : rows) {
            long id = row.get("id").asLong();
            CapturingExchange single = get("/v1/taxonomy/topics/by_id?id=" + id);
            handleWithTenant(single, TENANT_A);
            assertThat(single.status).isEqualTo(200);
            assertThat(row)
                .as("by_ids must return exactly the object by_id returns for id %d", id)
                .isEqualTo(TaxonomyHandler.MAPPER.readTree(single.bodyString()));
        }
        Set<String> labels = new HashSet<>();
        rows.forEach(r -> labels.add(r.get("label").asText()));
        assertThat(labels).containsExactlyInAnyOrder("alpha", "beta");
    }

    @Test
    void missingIdsAreOmitted() throws Exception {
        long a = repo.insertTopic(TENANT_A, "present", null, COL, 0, null, null);

        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids",
            "{\"ids\":[" + a + ",987654321,987654322]}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(200);
        JsonNode rows = TaxonomyHandler.MAPPER.readTree(ex.bodyString());
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get("id").asLong()).isEqualTo(a);
    }

    @Test
    void allIdsMissing_isAnEmptyArray_not404() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids", "{\"ids\":[987654323]}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString().trim()).isEqualTo("[]");
    }

    @Test
    void tenantIsolation_doesNotLeakAnotherTenantsTopic() throws Exception {
        long mine = repo.insertTopic(TENANT_A, "mine", null, COL, 0, null, null);
        long theirs = repo.insertTopic(TENANT_B, "theirs-secret", null, COL, 0, null, null);

        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids",
            "{\"ids\":[" + mine + "," + theirs + "]}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"label\":\"mine\"");
        assertThat(body).doesNotContain("theirs-secret");

        CapturingExchange asB = post("/v1/taxonomy/topics/by_ids",
            "{\"ids\":[" + mine + "," + theirs + "]}");
        handleWithTenant(asB, TENANT_B);
        assertThat(asB.bodyString()).contains("theirs-secret").doesNotContain("\"label\":\"mine\"");
    }

    @Test
    void missingIds_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids", "{}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void emptyIdsArray_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids", "{\"ids\":[]}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void nonArrayIds_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids", "{\"ids\":\"1,2\"}");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void nonIntegerElement_returns400() throws Exception {
        for (String bad : new String[] {"[\"1\"]", "[1.5]", "[null]", "[{}]", "[true]"}) {
            CapturingExchange ex = post("/v1/taxonomy/topics/by_ids", "{\"ids\":" + bad + "}");
            handleWithTenant(ex, TENANT_A);
            assertThat(ex.status).as("ids=%s", bad).isEqualTo(400);
        }
    }

    @Test
    void malformedJson_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/topics/by_ids", "{\"ids\":[1,");
        handleWithTenant(ex, TENANT_A);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void capIsAcceptedAtTheLimit_andRejectedOneOver() throws Exception {
        long real = repo.insertTopic(TENANT_A, "cap-real", null, COL, 0, null, null);

        StringBuilder atCap = new StringBuilder("[").append(real);
        for (int i = 1; i < TaxonomyRepository.MAX_TOPICS_BY_IDS; i++) atCap.append(',').append(900_000_000L + i);
        atCap.append(']');
        CapturingExchange ok = post("/v1/taxonomy/topics/by_ids", "{\"ids\":" + atCap + "}");
        handleWithTenant(ok, TENANT_A);
        assertThat(ok.status)
            .as("exactly MAX_TOPICS_BY_IDS ids is served")
            .isEqualTo(200);
        assertThat(TaxonomyHandler.MAPPER.readTree(ok.bodyString())).hasSize(1);

        StringBuilder over = new StringBuilder("[");
        for (int i = 0; i <= TaxonomyRepository.MAX_TOPICS_BY_IDS; i++) {
            if (i > 0) over.append(',');
            over.append(900_000_000L + i);
        }
        over.append(']');
        CapturingExchange tooMany = post("/v1/taxonomy/topics/by_ids", "{\"ids\":" + over + "}");
        handleWithTenant(tooMany, TENANT_A);
        assertThat(tooMany.status)
            .as("MAX_TOPICS_BY_IDS + 1 ids must be rejected 400 at the HTTP layer")
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

    private static CapturingExchange get(String path) {
        return new CapturingExchange("GET", URI.create(path), "");
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
