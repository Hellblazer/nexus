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
 * nexus-nydpr — TelemetryHandler's handler-side JSON validation for
 * {@code hook_failures.batch_doc_ids} (telemetry-004-type-hygiene.xml made it
 * jsonb, nexus-cefa1.3).
 *
 * <p>Mirrors {@code TaxonomyHandlerJsonValidationTest} (nexus-cefa1.6) and
 * {@code PlanHandlerJsonValidationTest} (nexus-cefa1.5) exactly: malformed
 * JSON in {@code batch_doc_ids} 400s HERE, at the handler ({@code
 * HttpUtil#rejectMalformedJson}, the shared helper all four handlers now
 * call), NOT via the class-22 SQLSTATE branch a malformed value would
 * otherwise hit at the repository. The SQLSTATE branch ({@code
 * HttpUtil.sendTypedDbError}, 422) stays wired as a backstop for any write
 * path that bypasses this handler (e.g. a direct repository call).
 *
 * <p>Unlike {@code link_types} (NOT NULL, so a missing value 400s via a
 * repository backstop) {@code batch_doc_ids} is a nullable column with no
 * default (telemetry-001-baseline.xml ships it as plain {@code
 * batch_doc_ids TEXT,}) — a missing/blank value is legitimate (the
 * changeset's {@code NULLIF(batch_doc_ids, '')::jsonb} USING clause maps it
 * to SQL NULL) and must NOT 400, covered below.
 *
 * <p>No database needed: a malformed batch_doc_ids 400s before the
 * repository ever reaches its {@link TenantScope}, and {@code
 * handler.handle(...)} catches every exception internally (never
 * propagates), so a repository backed by a null {@link javax.sql.DataSource}
 * is safe here — the same setup {@code TaxonomyHandlerJsonValidationTest}
 * uses.
 */
class TelemetryHandlerJsonValidationTest {

    private static final String TENANT = "nydpr-telemetry-batch-doc-ids-validation-tenant";

    private final TelemetryHandler handler = new TelemetryHandler(
        new TelemetryRepository(new TenantScope(null)));

    @Test
    void hookFailureRecord_malformedBatchDocIds_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/hook_failures/record",
            "{\"hook_name\":\"h\",\"batch_doc_ids\":\"not-json-at-all\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed batch_doc_ids must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("batch_doc_ids").contains("valid JSON");
    }

    @Test
    void hookFailureRecord_validBatchDocIds_doesNotFailJsonValidation() throws Exception {
        // handle() never propagates, so any status OTHER than 400 proves the
        // request cleared the JSON-validity gate (it then fails downstream
        // against the null DataSource, which is expected and irrelevant here).
        CapturingExchange ex = post("/v1/telemetry/hook_failures/record",
            "{\"hook_name\":\"h\",\"batch_doc_ids\":\"[\\\"doc1\\\",\\\"doc2\\\"]\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("valid batch_doc_ids must not 400 the JSON-validity gate").isNotEqualTo(400);
    }

    @Test
    void hookFailureRecord_missingBatchDocIds_doesNotFailJsonValidation() throws Exception {
        // No batch_doc_ids key at all: rejectMalformedJson skips a non-String
        // (null) value, and unlike link_types this column is nullable with no
        // default (telemetry-001-baseline.xml), so a missing value is legitimate
        // and must not 400 on JSON-shape grounds.
        CapturingExchange ex = post("/v1/telemetry/hook_failures/record",
            "{\"hook_name\":\"h\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("missing batch_doc_ids must not 400").isNotEqualTo(400);
    }

    @Test
    void importHookFailure_malformedBatchDocIds_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/import",
            "{\"table\":\"hook_failures\",\"hook_name\":\"h\",\"occurred_at\":\"2026-01-01T00:00:00Z\","
            + "\"batch_doc_ids\":\"not-json-at-all\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed batch_doc_ids must 400 on /import too").isEqualTo(400);
        assertThat(ex.bodyString()).contains("batch_doc_ids").contains("valid JSON");
    }

    @Test
    void importBatch_malformedBatchDocIds_namesFailingRowIndex() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/import_batch",
            "{\"table\":\"hook_failures\",\"rows\":["
            + "{\"hook_name\":\"h\",\"occurred_at\":\"2026-01-01T00:00:00Z\","
            + "\"batch_doc_ids\":\"[\\\"doc1\\\"]\"},"
            + "{\"hook_name\":\"h\",\"occurred_at\":\"2026-01-01T00:00:00Z\","
            + "\"batch_doc_ids\":\"not-json-at-all\"}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("malformed batch_doc_ids in a batch row must 400").isEqualTo(400);
        assertThat(ex.bodyString())
            .as("error must name the failing row index (row 1, the second row)")
            .contains("rows[1]");
    }

    @Test
    void importBatch_nativeArrayBatchDocIds_isCoercedAndRejectedLikeSingleRowPaths() throws Exception {
        // A client that sends batch_doc_ids as a NATIVE JSON array (not the
        // pre-stringified form the wire contract specifies) must not skip the
        // gate: the batch path coerces through optStrNull exactly like the
        // single-row /hook_failures/record and /import paths, so the List's
        // toString() form ("[doc1, doc2]", not JSON) is what gets validated and
        // 400s, naming the row.
        CapturingExchange ex = post("/v1/telemetry/import_batch",
            "{\"table\":\"hook_failures\",\"rows\":["
            + "{\"hook_name\":\"h\",\"occurred_at\":\"2026-01-01T00:00:00Z\","
            + "\"batch_doc_ids\":[\"doc1\",\"doc2\"]}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("native-array batch_doc_ids in a batch row must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("rows[0]").contains("batch_doc_ids");
    }

    @Test
    void importBatch_otherTable_doesNotValidateBatchDocIds() throws Exception {
        // The rows[i] batch_doc_ids gate is scoped to table="hook_failures"
        // only — a "relevance_log" batch's rows are untouched by this bead's
        // validation, so a garbage "batch_doc_ids"-named field on an unrelated
        // table must not spuriously 400.
        CapturingExchange ex = post("/v1/telemetry/import_batch",
            "{\"table\":\"relevance_log\",\"rows\":["
            + "{\"query\":\"q\",\"chunk_id\":\"c\",\"action\":\"a\","
            + "\"timestamp\":\"2026-01-01T00:00:00Z\",\"batch_doc_ids\":\"not-json-at-all\"}"
            + "]}");
        handleWithTenant(ex);
        assertThat(ex.status).as("non-hook_failures table must not 400 on the batch_doc_ids gate").isNotEqualTo(400);
    }

    // ── Helpers (mirrors TaxonomyHandlerJsonValidationTest's CapturingExchange) ─

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
