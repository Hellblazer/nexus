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
 * RDR-203 P2 — {@code TelemetryHandler}'s handler-side shape validation for
 * {@code POST /v1/telemetry/nx_answer_runs/complete}, plus the proof that
 * {@code /record} keeps its lenient {@code created_at} handling untouched.
 *
 * <p>Mirrors {@code TelemetryHandlerNxAnswerStepsJsonValidationTest}'s
 * scaffold exactly: a {@code CapturingExchange} fake {@link HttpExchange} +
 * a {@link TelemetryRepository} backed by a {@code null} {@link
 * javax.sql.DataSource}. A shape-validation rejection happens BEFORE the
 * repository is ever reached, so it is exactly 400 with an
 * {@code IllegalArgumentException} message naming the field; a request that
 * clears validation instead fails downstream against the null
 * {@code DataSource} (a status other than 400, typically 500) — expected and
 * irrelevant here, since these tests only assert whether the 400 shape gate
 * fired.
 */
class TelemetryHandlerNxAnswerCompleteTest {

    private static final String TENANT = "rdr203-telemetry-nx-answer-complete-tenant";

    private final TelemetryHandler handler = new TelemetryHandler(
        new TelemetryRepository(new TenantScope(null)));

    // ── outcome: closed vocabulary, no silent default (D1) ──────────────────────

    @Test
    void missingOutcomeIs400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/complete",
            "{\"question\":\"q\",\"created_at\":\"2026-09-05T18:22:31.481920+00:00\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("outcome");
    }

    @Test
    void unknownOutcomeValueIs400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/complete",
            "{\"question\":\"q\",\"created_at\":\"2026-09-05T18:22:31.481920+00:00\","
            + "\"outcome\":\"maybe\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("outcome");
    }

    // ── created_at: required on /complete, D1's dedup key ────────────────────────

    @Test
    void missingCreatedAtOnCompleteIs400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/complete",
            "{\"question\":\"q\",\"outcome\":\"success\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("created_at");
    }

    @Test
    void malformedCreatedAtOnCompleteIs400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/complete",
            "{\"question\":\"q\",\"outcome\":\"success\",\"created_at\":\"not-a-timestamp\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("created_at");
    }

    @Test
    void blankCreatedAtOnCompleteIs400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/complete",
            "{\"question\":\"q\",\"outcome\":\"success\",\"created_at\":\"\"}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("created_at");
    }

    /**
     * The other half of the pair (D1): {@code /record} must keep its lenient
     * handling — absent {@code created_at} stamps {@code now()} inside
     * {@code TelemetryRepository.recordNxAnswerRun}, never a 400. The ETL
     * path, the RDR-203 D6 survivors and {@code nx_answer_report} all rely on
     * that leniency; scoping the new requirement to {@code /complete} alone
     * is the point of {@code handleNxAnswerRunComplete} being its OWN
     * handler method rather than a branch inside {@code
     * handleNxAnswerRunRecord}. Falsifier: make the requirement global (e.g.
     * a shared helper both routes call) and this reds.
     */
    @Test
    void missingCreatedAtOnRecordStillStampsNow() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/record",
            "{\"question\":\"q\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("/record must stay lenient about absent created_at")
            .isNotEqualTo(400);
    }

    // ── degradation contract: steps absent writes parent only, same as /record ──

    @Test
    void stepsAbsentWritesParentOnly() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/complete",
            "{\"question\":\"q\",\"outcome\":\"success\","
            + "\"created_at\":\"2026-09-05T18:22:31.481920+00:00\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a well-formed /complete body with no steps must clear shape validation")
            .isNotEqualTo(400);
    }

    @Test
    void wellFormedCompleteBodyClearsShapeGate() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/nx_answer_runs/complete",
            "{\"question\":\"q\",\"plan_id\":42,\"matched_confidence\":0.71,"
            + "\"step_count\":1,\"final_text\":\"answer\",\"cost_usd\":0.0123,"
            + "\"duration_ms\":81422,"
            + "\"created_at\":\"2026-09-05T18:22:31.481920+00:00\","
            + "\"steps\":[{\"step_index\":0,\"operator\":\"op\",\"source\":\"sql\","
            + "\"elapsed_ms\":5,\"ok\":true}],"
            + "\"outcome\":\"success\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a fully-formed composite payload must clear shape validation")
            .isNotEqualTo(400);
    }

    // ── Helpers (mirrors TelemetryHandlerNxAnswerStepsJsonValidationTest) ────────

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
