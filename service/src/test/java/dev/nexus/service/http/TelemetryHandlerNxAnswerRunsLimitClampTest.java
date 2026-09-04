// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TelemetryRepository;
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
 * RDR-196 .p1c-b review fix (nexus-lme1s, T2 [23099] finding 2): {@code GET
 * /v1/telemetry/nx_answer_runs/query}'s {@code limit} had no upper clamp, so
 * {@code include_steps=true} could return N runs x M steps entirely
 * unbounded by a caller-supplied {@code limit}. {@code
 * TelemetryHandler.MAX_QUERY_RUNS_LIMIT} (300, matching the project's {@code
 * MAX_QUERY_RESULTS} paging convention) now clamps server-side rather than
 * rejecting with a 400 — an over-large request degrades to the capped page.
 *
 * <p>Model: {@link CatalogHandlerListPaginationTest} (hermetic Testcontainers
 * PG, drives {@link TelemetryHandler#handle} directly via a capturing {@link
 * HttpExchange}). Proving the clamp needs MORE than 300 real rows in the DB
 * — with fewer rows, an unclamped huge {@code limit} and a clamped-to-300
 * {@code limit} return an identical page, so the clamp would be
 * unobservable.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TelemetryHandlerNxAnswerRunsLimitClampTest {

    private static final String SVC_ROLE = "svc_tel_clamp_test";
    private static final String SVC_PASS = "svc_tel_clamp_test_pass";
    private static final String TENANT   = "tel-limit-clamp-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TelemetryRepository repo;
    TelemetryHandler handler;
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
        repo = new TelemetryRepository(tenantScope);
        handler = new TelemetryHandler(repo);

        // Seed 305 rows — 5 MORE than MAX_QUERY_RUNS_LIMIT (300) — the
        // minimum needed to make the clamp observable (see class javadoc).
        for (int i = 0; i < 305; i++) {
            repo.recordNxAnswerRun(TENANT, "clamp-seed-" + i, null, null, 0, "",
                0.0, 1_000, "2024-01-15T10:30:00Z");
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void limitAboveCap_isClampedTo300() throws Exception {
        var body = query("limit=99999");
        assertThat(rows(body))
            .as("a limit far above MAX_QUERY_RUNS_LIMIT must clamp to exactly 300 rows, not 305")
            .hasSize(300);
    }

    @Test
    void limitAtCap_isUnaffected() throws Exception {
        var body = query("limit=300");
        assertThat(rows(body)).hasSize(300);
    }

    @Test
    void limitBelowCap_isHonoredUnclamped() throws Exception {
        var body = query("limit=50");
        assertThat(rows(body))
            .as("a limit under the cap must be honored exactly, not silently raised or lowered")
            .hasSize(50);
    }

    @Test
    void clampAppliesRegardlessOfIncludeSteps() throws Exception {
        var body = query("limit=99999&include_steps=true");
        assertThat(rows(body))
            .as("include_steps=true must not bypass the same clamp — this is the exact "
                + "unbounded-N-runs-x-M-steps growth the clamp exists to prevent")
            .hasSize(300);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> rows(Map<String, Object> body) {
        return (List<Map<String, Object>>) body.get("rows");
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> query(String qs) throws Exception {
        CapturingExchange ex = get("/v1/telemetry/nx_answer_runs/query?" + qs);
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        return MAPPER.readValue(ex.bodyString(), Map.class);
    }

    private static CapturingExchange get(String uri) {
        return new CapturingExchange("GET", URI.create(uri));
    }

    /** Minimal {@link HttpExchange} that captures the response status + body (GET, no body). */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri) {
            this.method = method;
            this.uri = uri;
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return new ByteArrayInputStream(new byte[0]); }
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
