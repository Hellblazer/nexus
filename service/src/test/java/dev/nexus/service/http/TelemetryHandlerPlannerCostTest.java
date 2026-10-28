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
 * nexus-jypmj.5 — the {@code planner_cost_usd} field on {@code nx_answer_runs}: the inline planner's own
 * dispatch cost, accepted, validated and persisted by {@code /record}, {@code /complete}, {@code /import}
 * and {@code /import_batch} and returned by {@code /query}. It is nullable end to end: an absent value is
 * SQL NULL (a plan-match hit has no planner dispatch, an old client sends none), never a fabricated 0.0.
 * Drives {@link TelemetryHandler#handle} against a real Postgres, because a handler that read the field and
 * dropped it before the repository would pass any validation-only test.
 *
 * <p>Model: {@link TelemetryHandlerRunSummaryTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TelemetryHandlerPlannerCostTest {

    private static final String SVC_ROLE = "svc_tel_planner_cost_test";
    private static final String SVC_PASS = "svc_tel_planner_cost_test_pass";
    private static final String TENANT   = "tel-planner-cost-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String CREATED = "2026-10-27T12:00:00+00:00";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TelemetryHandler handler;

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
        handler = new TelemetryHandler(new TelemetryRepository(new TenantScope(svcDs)));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── /record ──────────────────────────────────────────────────────────────

    @Test
    void recordPersistsPlannerCostAndQueryReturnsItBesideTheRunCost() throws Exception {
        String q = "record-planner-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"plan_id\":0,\"cost_usd\":0.0125,\"planner_cost_usd\":0.004}").status).isEqualTo(200);

        Map<String, Object> row = rowFor(q);
        assertThat(((Number) row.get("planner_cost_usd")).doubleValue()).isEqualTo(0.004);
        assertThat(((Number) row.get("cost_usd")).doubleValue()).isEqualTo(0.0125);
    }

    @Test
    void recordWithoutPlannerCostStoresNullNotZero() throws Exception {
        // The old-client case, and a plan-match hit (no planner dispatch): no field on the wire.
        String q = "record-no-planner-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED + "\"}").status)
            .isEqualTo(200);
        Map<String, Object> row = rowFor(q);
        assertThat(row).as("the readback always carries the key").containsKey("planner_cost_usd");
        assertThat(row.get("planner_cost_usd")).isNull();
    }

    @Test
    void anExplicitNullAndAZeroAreDistinct() throws Exception {
        String qNull = "record-null-planner-" + System.nanoTime();
        String qZero = "record-zero-planner-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + qNull + "\",\"created_at\":\"" + CREATED
            + "\",\"planner_cost_usd\":null}").status).isEqualTo(200);
        assertThat(post("/record", "{\"question\":\"" + qZero + "\",\"created_at\":\"" + CREATED
            + "\",\"planner_cost_usd\":0.0}").status).isEqualTo(200);
        assertThat(rowFor(qNull).get("planner_cost_usd")).as("unknown stays unknown").isNull();
        assertThat(((Number) rowFor(qZero).get("planner_cost_usd")).doubleValue()).as("a known free planner").isEqualTo(0.0);
    }

    @Test
    void recordRejectsANegativeNonNumericOrNonFinitePlannerCost() throws Exception {
        for (String bad : List.of("-0.01", "\"abc\"", "true", "[1]", "{\"a\":1}")) {
            String q = "record-bad-planner-" + System.nanoTime();
            Reply r = post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
                + "\",\"planner_cost_usd\":" + bad + "}");
            assertThat(r.status).as("planner_cost_usd=%s: %s", bad, r.body).isEqualTo(400);
            assertThat(r.body).contains("planner_cost_usd");
            assertThat(findRow(q)).as("a rejected body writes nothing").isNull();
        }
    }

    // ── /complete ────────────────────────────────────────────────────────────

    @Test
    void completePersistsPlannerCostWithItsStepsInOneCall() throws Exception {
        String q = "complete-planner-" + System.nanoTime();
        Reply r = post("/complete", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"success\",\"step_count\":1,\"plan_id\":0,\"cost_usd\":0.03,\"planner_cost_usd\":0.011,"
            + "\"steps\":[{\"step_index\":0,\"operator\":\"op\",\"source\":\"sql\",\"elapsed_ms\":5,\"ok\":true,\"cost_usd\":0.019}]}");
        assertThat(r.status).as(r.body).isEqualTo(200);

        Map<String, Object> row = rowFor(q, true);
        assertThat(((Number) row.get("planner_cost_usd")).doubleValue()).isEqualTo(0.011);
        assertThat((List<?>) row.get("steps")).as("the step child rides the same write").hasSize(1);
    }

    @Test
    void completeWithoutPlannerCostStoresNullAndRejectsABadOne() throws Exception {
        String q = "complete-no-planner-" + System.nanoTime();
        assertThat(post("/complete", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"failure\"}").status).isEqualTo(200);
        assertThat(rowFor(q).get("planner_cost_usd")).isNull();

        String bad = "complete-bad-planner-" + System.nanoTime();
        Reply r = post("/complete", "{\"question\":\"" + bad + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"success\",\"planner_cost_usd\":-1}");
        assertThat(r.status).isEqualTo(400);
        assertThat(r.body).contains("planner_cost_usd");
        assertThat(findRow(bad)).as("nothing written on the rejection").isNull();
    }

    // ── import (single and batch) ────────────────────────────────────────────

    @Test
    void singleImportCarriesPlannerCost() throws Exception {
        String q = "import-planner-" + System.nanoTime();
        assertThat(postPath("/import", "{\"table\":\"nx_answer_runs\",\"question\":\"" + q
            + "\",\"created_at\":\"" + CREATED + "\",\"planner_cost_usd\":0.007}").status).isEqualTo(200);
        assertThat(((Number) rowFor(q).get("planner_cost_usd")).doubleValue()).isEqualTo(0.007);

        String q2 = "import-no-planner-" + System.nanoTime();
        assertThat(postPath("/import", "{\"table\":\"nx_answer_runs\",\"question\":\"" + q2
            + "\",\"created_at\":\"" + CREATED + "\"}").status).isEqualTo(200);
        assertThat(rowFor(q2).get("planner_cost_usd")).isNull();
    }

    @Test
    void batchImportCarriesPlannerCostRowByRowAndNamesABadRow() throws Exception {
        String withQ = "batch-with-" + System.nanoTime();
        String withoutQ = "batch-without-" + System.nanoTime();
        Reply r = postPath("/import_batch", "{\"table\":\"nx_answer_runs\",\"rows\":["
            + "{\"question\":\"" + withQ + "\",\"created_at\":\"" + CREATED + "\",\"planner_cost_usd\":0.002},"
            + "{\"question\":\"" + withoutQ + "\",\"created_at\":\"" + CREATED + "\"}]}");
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(((Number) rowFor(withQ).get("planner_cost_usd")).doubleValue()).isEqualTo(0.002);
        assertThat(rowFor(withoutQ).get("planner_cost_usd")).isNull();

        String q = "batch-bad-" + System.nanoTime();
        Reply bad = postPath("/import_batch", "{\"table\":\"nx_answer_runs\",\"rows\":["
            + "{\"question\":\"" + q + "-ok\",\"created_at\":\"" + CREATED + "\"},"
            + "{\"question\":\"" + q + "-bad\",\"created_at\":\"" + CREATED + "\",\"planner_cost_usd\":-5}]}");
        assertThat(bad.status).isEqualTo(400);
        assertThat(bad.body).contains("rows[1]").contains("planner_cost_usd");
        assertThat(findRow(q + "-ok")).as("the batch is refused whole").isNull();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private record Reply(int status, String body) {}

    private Reply post(String route, String json) throws Exception {
        return postPath("/nx_answer_runs" + route, json);
    }

    private Reply postPath(String path, String json) throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create("/v1/telemetry" + path), json);
        run(ex);
        return new Reply(ex.status, ex.bodyString());
    }

    private Map<String, Object> rowFor(String question) throws Exception {
        return rowFor(question, false);
    }

    private Map<String, Object> rowFor(String question, boolean steps) throws Exception {
        Map<String, Object> row = find(question, steps);
        assertThat(row).as("a row for %s", question).isNotNull();
        return row;
    }

    private Map<String, Object> findRow(String question) throws Exception {
        return find(question, false);
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> find(String question, boolean steps) throws Exception {
        CapturingExchange ex = new CapturingExchange("GET", URI.create(
            "/v1/telemetry/nx_answer_runs/query?limit=300" + (steps ? "&include_steps=true" : "")), "");
        run(ex);
        assertThat(ex.status).as(ex.bodyString()).isEqualTo(200);
        for (Map<String, Object> row : (List<Map<String, Object>>) MAPPER.readValue(ex.bodyString(), Map.class).get("rows")) {
            if (question.equals(row.get("question"))) return row;
        }
        return null;
    }

    private void run(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    /** Minimal {@link HttpExchange} that captures the response status and body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final byte[] requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = body.getBytes(StandardCharsets.UTF_8);
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return new ByteArrayInputStream(requestBody); }
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
