// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.db.PlanRepository;
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
 * nexus-cefa1.5 — PlanHandler's handler-side JSON validation for
 * {@code plan_json}/{@code default_bindings} (plans-002-jsonb.xml made both
 * jsonb).
 *
 * <p>Mirrors {@code AspectHandlerJsonValidationTest} (nexus-cefa1.4) exactly:
 * malformed JSON in either field 400s HERE, at the handler ({@code
 * HttpUtil#rejectMalformedJson}, the shared helper both handlers now call),
 * NOT via the class-22 SQLSTATE branch a malformed value would otherwise hit
 * at the repository. The SQLSTATE branch ({@code HttpUtil.sendTypedDbError},
 * 422) stays wired as a backstop for any write path that bypasses this
 * handler (e.g. a direct repository call).
 *
 * <p>{@code plan_json} carries a SECOND, pre-existing gate ({@code
 * requireString}) that already 400s on a missing/blank value — distinct from
 * this bead's new "present but not valid JSON" gate. Both are covered below.
 *
 * <p>No database needed: the validation runs in {@code handleSave} /
 * {@code parsePlanRow} BEFORE the repository is ever touched, so a repository
 * backed by a null {@link javax.sql.DataSource} is safe here — {@link
 * TenantScope}'s constructor only inspects the DataSource type, it never
 * dereferences it, and {@code handler.handle(...)} catches every exception
 * internally (never propagates), so the repository is simply never reached
 * on the failure paths this test covers.
 */
class PlanHandlerJsonValidationTest {

    private static final String TENANT = "cefa1-plan-json-validation-tenant";

    private final PlanHandler handler = new PlanHandler(new PlanRepository(new TenantScope(null)));

    @Test
    void save_malformedPlanJson_returns400() throws Exception {
        CapturingExchange ex = post("/v1/plans/save",
            "{\"query\":\"q\",\"plan_json\":\"not-json-at-all\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed plan_json must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("plan_json").contains("valid JSON");
    }

    @Test
    void save_malformedDefaultBindings_returns400() throws Exception {
        CapturingExchange ex = post("/v1/plans/save",
            "{\"query\":\"q\",\"plan_json\":\"{}\",\"default_bindings\":\"not-json-either\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed default_bindings must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("default_bindings").contains("valid JSON");
    }

    @Test
    void save_validPlanJson_doesNotFailValidation() throws Exception {
        // handle() never propagates, so any status OTHER than 400 proves the
        // request cleared the JSON-validity gate (it then fails downstream
        // against the null DataSource, which is expected and irrelevant here).
        CapturingExchange ex = post("/v1/plans/save",
            "{\"query\":\"q\",\"plan_json\":\"{\\\"steps\\\":[]}\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("valid plan_json must not 400 the validation gate").isNotEqualTo(400);
    }

    @Test
    void save_blankDefaultBindings_doesNotFailValidation() throws Exception {
        CapturingExchange ex = post("/v1/plans/save",
            "{\"query\":\"q\",\"plan_json\":\"{}\",\"default_bindings\":\"\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("blank default_bindings must not 400 (NULLIF semantics allow it)")
            .isNotEqualTo(400);
    }

    @Test
    void save_missingPlanJson_returns400() throws Exception {
        // Pre-existing requireString gate (not new to this bead): absent
        // plan_json is a 400 distinct from "present but malformed".
        CapturingExchange ex = post("/v1/plans/save", "{\"query\":\"q\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("missing plan_json must 400").isEqualTo(400);
    }

    @Test
    void importPlan_malformedPlanJson_returns400() throws Exception {
        // handleImport shares parsePlanRow with handleImportBatch, so the gate
        // applies to the import path too (the "one consistent behaviour"
        // decision — see AspectHandler.serializeAspectBody's identical framing).
        CapturingExchange ex = post("/v1/plans/import",
            "{\"project\":\"p\",\"query\":\"q\",\"plan_json\":\"not-json-at-all\","
            + "\"created_at\":\"2026-01-01T00:00:00Z\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed plan_json must 400 on /import too").isEqualTo(400);
    }

    @Test
    void importBatch_malformedPlanJson_namesFailingRowIndex() throws Exception {
        CapturingExchange ex = post("/v1/plans/import_batch",
            "{\"rows\":["
            + "{\"project\":\"p\",\"query\":\"q0\",\"plan_json\":\"{}\","
            + "\"created_at\":\"2026-01-01T00:00:00Z\"},"
            + "{\"project\":\"p\",\"query\":\"q1\",\"plan_json\":\"not-json-at-all\","
            + "\"created_at\":\"2026-01-01T00:00:00Z\"}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed plan_json in a batch row must 400").isEqualTo(400);
        assertThat(ex.bodyString())
            .as("error must name the failing row index (row 1, the second row)")
            .contains("rows[1]");
    }

    // ── Helpers (mirrors AspectHandlerJsonValidationTest's CapturingExchange) ───

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
