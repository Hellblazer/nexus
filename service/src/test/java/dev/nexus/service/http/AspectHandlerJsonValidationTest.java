// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.db.AspectRepository;
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
 * nexus-cefa1.4 — AspectHandler's handler-side JSON validation for
 * {@code extras}/{@code salient_sentences} (aspects-003-type-hygiene.xml made
 * both jsonb).
 *
 * <p>DECISION recorded on the bead: malformed JSON in either field 400s HERE,
 * at the handler ({@code Jackson#readTree}), NOT via the class-22 SQLSTATE
 * branch a malformed value would otherwise hit at the repository. The
 * SQLSTATE branch ({@code HttpUtil.sendTypedDbError}, 422) stays wired as a
 * backstop for any write path that bypasses this handler method — see
 * {@code AspectHandler.serializeAspectBody}'s own comment.
 *
 * <p>No database needed: the validation runs in {@code serializeAspectBody}
 * BEFORE the repository is ever touched, so a repository backed by a null
 * {@link javax.sql.DataSource} is safe here — {@link TenantScope}'s
 * constructor only inspects the DataSource type (a HikariDataSource-specific
 * pool-size read), it never dereferences it, and {@code handler.handle(...)}
 * catches every exception internally (never propagates), so the repository
 * is simply never reached on the failure paths this test covers.
 */
class AspectHandlerJsonValidationTest {

    private static final String TENANT = "cefa1-json-validation-tenant";

    private final AspectHandler handler = new AspectHandler(new AspectRepository(new TenantScope(null)));

    @Test
    void upsert_malformedExtras_returns400() throws Exception {
        CapturingExchange ex = post("/v1/aspects/upsert",
            "{\"collection\":\"c\",\"source_path\":\"p\",\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"confidence\":0.9,"
            + "\"extras\":\"not-json-at-all\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed extras JSON must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("extras").contains("valid JSON");
    }

    @Test
    void upsert_malformedSalientSentences_returns400() throws Exception {
        CapturingExchange ex = post("/v1/aspects/upsert",
            "{\"collection\":\"c\",\"source_path\":\"p\",\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"confidence\":0.9,"
            + "\"salient_sentences\":\"not-json-either\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed salient_sentences JSON must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("salient_sentences").contains("valid JSON");
    }

    @Test
    void upsert_validExtrasString_doesNotFailValidation() throws Exception {
        // A pre-serialized valid JSON string must NOT 400 here. handle() never
        // propagates, so any status OTHER than 400 proves the request cleared the
        // JSON-validity gate (it then fails downstream against the null
        // DataSource, which is expected and irrelevant to this test).
        CapturingExchange ex = post("/v1/aspects/upsert",
            "{\"collection\":\"c\",\"source_path\":\"p\",\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"confidence\":0.9,"
            + "\"extras\":\"{\\\"venue\\\":\\\"VLDB\\\"}\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("valid JSON extras must not 400 the validation gate").isNotEqualTo(400);
    }

    @Test
    void upsert_blankExtras_doesNotFailValidation() throws Exception {
        CapturingExchange ex = post("/v1/aspects/upsert",
            "{\"collection\":\"c\",\"source_path\":\"p\",\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"confidence\":0.9,\"extras\":\"\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("blank extras must not 400 (NULLIF semantics allow it)").isNotEqualTo(400);
    }

    @Test
    void importAspect_malformedExtras_returns400() throws Exception {
        // handleImportAspect shares serializeAspectBody with handleUpsert, so the
        // gate applies to the import path too (the "one consistent behaviour"
        // decision -- see AspectHandler.serializeAspectBody's own comment).
        CapturingExchange ex = post("/v1/aspects/import",
            "{\"collection\":\"c\",\"source_path\":\"p\",\"extracted_at\":\"2026-01-01T00:00:00Z\","
            + "\"model_version\":\"v1\",\"extractor_name\":\"ex\",\"confidence\":0.9,"
            + "\"extras\":\"not-json-at-all\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed extras JSON must 400 on /import too").isEqualTo(400);
    }

    // ── Helpers (mirrors AspectHandlerEnqueueErrorTest's CapturingExchange) ──────

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
