// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-196 .p1c (nexus-nyry9.9) — {@code TelemetryHandler}'s handler-side JSON
 * shape validation for the OPTIONAL {@code steps} array on
 * {@code POST /v1/telemetry/nx_answer_runs/record}.
 *
 * <p>Mirrors {@code TelemetryHandlerJsonValidationTest}'s scaffold exactly:
 * a {@code CapturingExchange} fake {@link HttpExchange} + a {@link
 * TelemetryRepository} backed by a {@code null} {@link javax.sql.DataSource}.
 * {@code handler.handle(...)} never propagates an exception, so any
 * malformed-shape rejection happens BEFORE the repository is ever reached
 * (proven by its status being exactly 400 with the naming
 * {@code IllegalArgumentException} message, not the 500 a null-DataSource
 * {@code NullPointerException} would otherwise produce) — a status other
 * than 400 proves the request cleared shape validation and is failing
 * downstream against the null DataSource instead, which is expected and
 * irrelevant here.
 */
class TelemetryHandlerNxAnswerStepsJsonValidationTest {

    private static final String TENANT = "nyry9-telemetry-nx-answer-steps-validation-tenant";

    private final TelemetryHandler handler = new TelemetryHandler(
        new TelemetryRepository(new TenantScope(null)));

    @Test
    void nxAnswerRunRecord_absentSteps_clearsShapeGate() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("absent steps must not 400 — it is optional").isNotEqualTo(400);
    }

    @Test
    void nxAnswerRunRecord_emptySteps_clearsShapeGate() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("empty steps array must not 400").isNotEqualTo(400);
    }

    @Test
    void nxAnswerRunRecord_wellFormedStep_clearsShapeGate() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[{\"step_index\":0,\"operator\":\"op\","
            + "\"source\":\"sql\",\"elapsed_ms\":5,\"ok\":true,\"bundled_steps\":[0,1]}]}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a well-formed step must clear shape validation (fails downstream on null DataSource)")
            .isNotEqualTo(400);
    }

    @Test
    void nxAnswerRunRecord_stepsNotAnArray_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":\"not-an-array\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("steps").contains("JSON array");
    }

    @Test
    void nxAnswerRunRecord_stepElementNotAnObject_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[\"not-an-object\"]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("steps").contains("JSON object");
    }

    @Test
    void nxAnswerRunRecord_stepMissingStepIndex_returns400() throws Exception {
        // step_index feeds the (run_id, step_index) composite PK — omitting it
        // must 400 naming the field, never silently default to 0 (which would
        // collide two omitted-step_index rows on the same PK; code-review
        // finding, 2026-08-20).
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[{\"operator\":\"op\",\"source\":\"sql\",\"ok\":true}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("step_index");
    }

    @Test
    void nxAnswerRunRecord_stepMissingOperator_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[{\"step_index\":0,\"source\":\"sql\",\"ok\":true}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("operator");
    }

    @Test
    void nxAnswerRunRecord_stepMissingSource_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[{\"step_index\":0,\"operator\":\"op\",\"ok\":true}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("source");
    }

    @Test
    void nxAnswerRunRecord_stepMissingOk_returns400() throws Exception {
        // 'ok' is boolean NOT NULL with no silent default (this bead's own
        // no-silent-fallback-for-correctness reasoning) — a step omitting it
        // must 400, never coerce to a default true/false.
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[{\"step_index\":0,\"operator\":\"op\",\"source\":\"sql\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("boolean");
    }

    @Test
    void nxAnswerRunRecord_bundledStepsNotAnArray_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[{\"step_index\":0,\"operator\":\"op\",\"source\":\"bundle\",\"ok\":true,"
            + "\"bundled_steps\":\"not-an-array\"}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("bundled_steps").contains("JSON array");
    }

    @Test
    void nxAnswerRunRecord_bundledStepsElementNotAnInteger_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\",\"steps\":[{\"step_index\":0,\"operator\":\"op\",\"source\":\"bundle\",\"ok\":true,"
            + "\"bundled_steps\":[\"not-an-int\"]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("bundled_steps").contains("integer");
    }

    // ── Helpers (mirrors TelemetryHandlerJsonValidationTest's CapturingExchange) ─

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

        CapturingExchange(String method, URI uri, String jsonBody) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(jsonBody.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() {
            return responseBody.toString(StandardCharsets.UTF_8);
        }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() { }
        @Override public InputStream getRequestBody() { return requestBody; }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int code, long contentLength) { this.status = code; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) { }
        @Override public void setStreams(InputStream i, OutputStream o) { }
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
