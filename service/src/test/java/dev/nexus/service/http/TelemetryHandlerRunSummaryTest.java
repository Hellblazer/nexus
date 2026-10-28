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
 * RDR-232 Phase 3 (nexus-jypmj.3.4) — the {@code run_summary} field on
 * {@code nx_answer_runs}: accepted, validated and persisted by {@code /record},
 * {@code /complete}, {@code /import} and {@code /import_batch}, and returned by
 * {@code /query}. Drives {@link TelemetryHandler#handle} against a real
 * Postgres (the validation alone would pass against a handler that dropped the
 * field before the repository, which is exactly the defect this guards).
 *
 * <p>Model: {@link TelemetryHandlerNxAnswerRunsLimitClampTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TelemetryHandlerRunSummaryTest {

    private static final String SVC_ROLE = "svc_tel_run_summary_test";
    private static final String SVC_PASS = "svc_tel_run_summary_test_pass";
    private static final String TENANT   = "tel-run-summary-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String CREATED = "2026-10-27T12:00:00+00:00";
    private static final String SUMMARY_JSON =
        "{\"version\":1,\"coverage\":{\"requested\":[\"f1\",\"f2\"],\"covered\":[\"f1\"],"
        + "\"primary_source\":\"found\",\"repair_attempts\":0.5}}";

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
    void recordPersistsRunSummaryAndQueryReturnsItAsAnObject() throws Exception {
        String q = "record-summary-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"run_summary\":" + SUMMARY_JSON + "}").status).isEqualTo(200);

        Map<String, Object> row = rowFor(q);
        assertThat(row).containsKey("run_summary");
        assertThat(row.get("run_summary"))
            .as("the summary comes back as a JSON object with its own version and content, not a string")
            .isEqualTo(MAPPER.readValue(SUMMARY_JSON, Map.class));
    }

    @Test
    void recordWithoutRunSummaryStoresNull() throws Exception {
        // The old-client case: no field on the wire. The column stays NULL and the
        // readback says so with an explicit null, never an empty object.
        String q = "record-no-summary-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED + "\"}").status)
            .isEqualTo(200);
        Map<String, Object> row = rowFor(q);
        assertThat(row).containsKey("run_summary");
        assertThat(row.get("run_summary")).isNull();
    }

    @Test
    void recordWithExplicitJsonNullRunSummaryStoresNull() throws Exception {
        String q = "record-null-summary-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"run_summary\":null}").status).isEqualTo(200);
        assertThat(rowFor(q).get("run_summary")).isNull();
    }

    @Test
    void recordRejectsANonObjectRunSummary() throws Exception {
        for (String bad : List.of("\"a string\"", "[1,2]", "7", "true")) {
            String q = "record-bad-summary-" + System.nanoTime();
            Reply r = post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
                + "\",\"run_summary\":" + bad + "}");
            assertThat(r.status).as("run_summary=%s", bad).isEqualTo(400);
            assertThat(r.body).contains("run_summary").contains("JSON object");
            assertThat(findRow(q)).as("a rejected body writes nothing").isNull();
        }
    }

    // ── U+0000 ───────────────────────────────────────────────────────────────

    @Test
    void aNulCharacterInTheSummaryIsA400NamingTheFieldNotA500() throws Exception {
        // jsonb cannot hold U+0000. The Python client strips it, so only another client sends it; it must be refused
        // as a bad request (naming the field) rather than reaching the INSERT and losing the run row to a 500.
        // Each body spells the NUL as the JSON escape backslash-u0000: in a value, in a nested array, in a key.
        List<String> bad = List.of(
            "{\"p\":\"a\\u0000b\"}",
            "{\"p\":{\"q\":[1,\"x\\u0000\"]}}",
            "{\"k\\u0000\":1}");
        for (String summary : bad) {
            String q = "nul-" + System.nanoTime();
            Reply r = post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
                + "\",\"run_summary\":" + summary + "}");
            assertThat(r.status).as("run_summary=%s: %s", summary, r.body).isEqualTo(400);
            assertThat(r.body).contains("run_summary").contains("U+0000");
            assertThat(findRow(q)).as("a rejected body writes nothing").isNull();
        }
        String q = "nul-complete-" + System.nanoTime();
        Reply c = post("/complete", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"success\",\"run_summary\":" + bad.get(0) + "}");
        assertThat(c.status).isEqualTo(400);
        assertThat(findRow(q)).isNull();
        Reply batch = postPath("/import_batch", "{\"table\":\"nx_answer_runs\",\"rows\":["
            + "{\"question\":\"nul-batch-" + System.nanoTime() + "\",\"created_at\":\"" + CREATED + "\",\"run_summary\":" + bad.get(0) + "}]}");
        assertThat(batch.status).isEqualTo(400);
        assertThat(batch.body).contains("rows[0]").contains("run_summary");
    }

    @Test
    void aSummaryWithAnEscapedBackslashBeforeU0000IsNotANulAndIsStored() throws Exception {
        // The text backslash-backslash-u0000 is a backslash followed by the letters u0000, not a NUL.
        String q = "not-nul-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"run_summary\":{\"p\":\"a\\\\u0000b\"}}").status).isEqualTo(200);
        assertThat(((Map<?, ?>) rowFor(q).get("run_summary")).get("p")).isEqualTo("a\\u0000b");
    }

    // ── size cap ─────────────────────────────────────────────────────────────

    @Test
    void aSummaryOfExactlyTheCapIsAcceptedAndOneByteOverIsRefused() throws Exception {
        int cap = TelemetryHandler.MAX_RUN_SUMMARY_BYTES;
        // {"p":""} is 8 bytes; pad the value to land on the cap exactly.
        String atCap = "{\"p\":\"" + "x".repeat(cap - 8) + "\"}";
        String overCap = "{\"p\":\"" + "x".repeat(cap - 7) + "\"}";
        assertThat(atCap.getBytes(StandardCharsets.UTF_8)).hasSize(cap);

        String q1 = "cap-at-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q1 + "\",\"created_at\":\"" + CREATED
            + "\",\"run_summary\":" + atCap + "}").status).isEqualTo(200);
        assertThat(rowFor(q1).get("run_summary")).isNotNull();

        String q2 = "cap-over-" + System.nanoTime();
        Reply r = post("/record", "{\"question\":\"" + q2 + "\",\"created_at\":\"" + CREATED
            + "\",\"run_summary\":" + overCap + "}");
        assertThat(r.status).isEqualTo(400);
        assertThat(r.body).contains("run_summary").contains(String.valueOf(cap));
        assertThat(findRow(q2)).isNull();
    }

    @Test
    void theCapCountsUtf8BytesNotCharacters() throws Exception {
        int cap = TelemetryHandler.MAX_RUN_SUMMARY_BYTES;
        // Two-byte characters: fewer than cap characters, more than cap bytes.
        String multi = "{\"p\":\"" + "é".repeat(cap / 2) + "\"}";
        assertThat(multi.length()).isLessThan(cap);
        assertThat(multi.getBytes(StandardCharsets.UTF_8).length).isGreaterThan(cap);

        String q = "cap-bytes-" + System.nanoTime();
        assertThat(post("/record", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"run_summary\":" + multi + "}").status).isEqualTo(400);
        assertThat(findRow(q)).isNull();
    }

    // ── /complete ────────────────────────────────────────────────────────────

    @Test
    void completePersistsRunSummaryWithItsStepsInOneCall() throws Exception {
        String q = "complete-summary-" + System.nanoTime();
        Reply r = post("/complete", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"success\",\"step_count\":1,"
            + "\"steps\":[{\"step_index\":0,\"operator\":\"op\",\"source\":\"sql\",\"elapsed_ms\":5,\"ok\":true}],"
            + "\"run_summary\":" + SUMMARY_JSON + "}");
        assertThat(r.status).as(r.body).isEqualTo(200);

        Map<String, Object> row = rowFor(q, true);
        assertThat(row.get("run_summary")).isEqualTo(MAPPER.readValue(SUMMARY_JSON, Map.class));
        assertThat((List<?>) row.get("steps")).as("the step child rides the same write").hasSize(1);
    }

    @Test
    void completeWithoutRunSummaryStoresNull() throws Exception {
        String q = "complete-no-summary-" + System.nanoTime();
        assertThat(post("/complete", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"failure\"}").status).isEqualTo(200);
        assertThat(rowFor(q).get("run_summary")).isNull();
    }

    @Test
    void completeRejectsNonObjectAndOverCapRunSummary() throws Exception {
        String q = "complete-bad-summary-" + System.nanoTime();
        Reply bad = post("/complete", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"success\",\"run_summary\":[1]}");
        assertThat(bad.status).isEqualTo(400);
        assertThat(bad.body).contains("run_summary");

        String over = "{\"p\":\"" + "x".repeat(TelemetryHandler.MAX_RUN_SUMMARY_BYTES) + "\"}";
        Reply big = post("/complete", "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED
            + "\",\"outcome\":\"success\",\"run_summary\":" + over + "}");
        assertThat(big.status).isEqualTo(400);
        assertThat(findRow(q)).as("nothing written on either rejection").isNull();
    }

    // ── import (single and batch) ────────────────────────────────────────────

    @Test
    void singleImportCarriesRunSummary() throws Exception {
        String q = "import-summary-" + System.nanoTime();
        assertThat(postPath("/import", "{\"table\":\"nx_answer_runs\",\"question\":\"" + q
            + "\",\"created_at\":\"" + CREATED + "\",\"run_summary\":" + SUMMARY_JSON + "}").status)
            .isEqualTo(200);
        assertThat(rowFor(q).get("run_summary")).isEqualTo(MAPPER.readValue(SUMMARY_JSON, Map.class));

        String q2 = "import-no-summary-" + System.nanoTime();
        assertThat(postPath("/import", "{\"table\":\"nx_answer_runs\",\"question\":\"" + q2
            + "\",\"created_at\":\"" + CREATED + "\"}").status).isEqualTo(200);
        assertThat(rowFor(q2).get("run_summary")).isNull();
    }

    @Test
    void batchImportCarriesRunSummaryRowByRow() throws Exception {
        String withQ = "batch-with-" + System.nanoTime();
        String withoutQ = "batch-without-" + System.nanoTime();
        Reply r = postPath("/import_batch", "{\"table\":\"nx_answer_runs\",\"rows\":["
            + "{\"question\":\"" + withQ + "\",\"created_at\":\"" + CREATED + "\",\"run_summary\":" + SUMMARY_JSON + "},"
            + "{\"question\":\"" + withoutQ + "\",\"created_at\":\"" + CREATED + "\"}]}");
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(rowFor(withQ).get("run_summary")).isEqualTo(MAPPER.readValue(SUMMARY_JSON, Map.class));
        assertThat(rowFor(withoutQ).get("run_summary")).isNull();
    }

    @Test
    void batchImportAcceptsARunSummaryExportedAsJsonText() throws Exception {
        // An ETL source that serialises JSON columns as text (hook_failures.batch_doc_ids does)
        // sends the object as a string; it must land as the same object.
        String q = "batch-text-" + System.nanoTime();
        String asText = MAPPER.writeValueAsString(SUMMARY_JSON);
        assertThat(postPath("/import_batch", "{\"table\":\"nx_answer_runs\",\"rows\":["
            + "{\"question\":\"" + q + "\",\"created_at\":\"" + CREATED + "\",\"run_summary\":" + asText + "}]}").status)
            .isEqualTo(200);
        assertThat(rowFor(q).get("run_summary")).isEqualTo(MAPPER.readValue(SUMMARY_JSON, Map.class));
    }

    @Test
    void batchImportNamesTheRowWithABadRunSummary() throws Exception {
        String q = "batch-bad-" + System.nanoTime();
        Reply r = postPath("/import_batch", "{\"table\":\"nx_answer_runs\",\"rows\":["
            + "{\"question\":\"" + q + "-ok\",\"created_at\":\"" + CREATED + "\"},"
            + "{\"question\":\"" + q + "-bad\",\"created_at\":\"" + CREATED + "\",\"run_summary\":[1]}]}");
        assertThat(r.status).isEqualTo(400);
        assertThat(r.body).contains("rows[1]").contains("run_summary");
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
