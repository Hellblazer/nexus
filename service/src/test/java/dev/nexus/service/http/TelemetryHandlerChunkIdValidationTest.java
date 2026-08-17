// SPDX-License-Identifier: AGPL-3.0-or-later
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
 * nexus-lgdel.l1 — canonical-form {@code chunk_id} validation at
 * TelemetryHandler's four write entry points (the "regrowth gap"): with
 * {@code nexus.chash_alias} and its coalesce-to-hex UPDATE cascades
 * (RekeyOps/StagingPromoteOps) deleted, nothing normalizes a non-canonical
 * {@code chunk_id} written directly through this handler any more, so the
 * handler itself must reject one loud rather than let the population the
 * legacy-001-drop-chash-alias.xml changesets just deleted quietly regrow.
 *
 * <p>Mirrors {@link TelemetryHandlerJsonValidationTest}'s no-DB harness
 * exactly: {@code Chash.requireCanonical} throws {@code
 * IllegalArgumentException} BEFORE the repository is ever reached, so a
 * {@link TelemetryRepository} backed by a null {@link javax.sql.DataSource}
 * is safe to construct here.
 */
class TelemetryHandlerChunkIdValidationTest {

    private static final String TENANT = "lgdel-telemetry-chunk-id-validation-tenant";
    private static final String VALID_64_HEX =
        "ec8019fb0b9188069cc0ed3fa423e7c307d27cccc93324ca020fdb191679fd16";
    private static final String LEGACY_32_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90";

    private final TelemetryHandler handler = new TelemetryHandler(
        new TelemetryRepository(new TenantScope(null)));

    @Test
    void relevanceLog_malformedChunkId_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/relevance/log",
            "{\"query\":\"q\",\"chunk_id\":\"not-a-chash\",\"action\":\"hit\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("non-canonical chunk_id must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunk_id");
    }

    @Test
    void relevanceLog_legacy32HexChunkId_returns400() throws Exception {
        // The former chash_alias regrowth path: a 32-hex legacy-shaped id
        // used to be silently normalized to 64-hex by the alias-driven
        // UPDATE cascade. That cascade is gone — this must 400, not write
        // the 32-hex value through verbatim.
        CapturingExchange ex = post("/v1/telemetry/relevance/log",
            "{\"query\":\"q\",\"chunk_id\":\"" + LEGACY_32_HEX + "\",\"action\":\"hit\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("legacy 32-hex chunk_id must 400, never write through").isEqualTo(400);
    }

    @Test
    void relevanceLog_validChunkId_doesNotFailValidation() throws Exception {
        // handle() never propagates, so any status OTHER than 400 proves the
        // request cleared the chunk_id-canonical gate (it then fails
        // downstream against the null DataSource, expected and irrelevant).
        CapturingExchange ex = post("/v1/telemetry/relevance/log",
            "{\"query\":\"q\",\"chunk_id\":\"" + VALID_64_HEX + "\",\"action\":\"hit\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("canonical chunk_id must not 400 the validation gate").isNotEqualTo(400);
    }

    @Test
    void frecencyUpsert_malformedChunkId_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/frecency/upsert",
            "{\"chunk_id\":\"not-a-chash\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("non-canonical chunk_id must 400 at the handler").isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunk_id");
    }

    @Test
    void frecencyUpsert_validChunkId_doesNotFailValidation() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/frecency/upsert",
            "{\"chunk_id\":\"" + VALID_64_HEX + "\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("canonical chunk_id must not 400 the validation gate").isNotEqualTo(400);
    }

    @Test
    void importRelevanceLog_malformedChunkId_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/import",
            "{\"table\":\"relevance_log\",\"query\":\"q\",\"chunk_id\":\"not-a-chash\","
            + "\"action\":\"hit\",\"timestamp\":\"2026-01-01T00:00:00Z\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("non-canonical chunk_id must 400 on /import too").isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunk_id");
    }

    @Test
    void importFrecency_malformedChunkId_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/import",
            "{\"table\":\"frecency\",\"chunk_id\":\"not-a-chash\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("non-canonical chunk_id must 400 on /import too").isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunk_id");
    }

    @Test
    void importFrecency_validChunkId_doesNotFailValidation() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/import",
            "{\"table\":\"frecency\",\"chunk_id\":\"" + VALID_64_HEX + "\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("canonical chunk_id must not 400 the validation gate").isNotEqualTo(400);
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
