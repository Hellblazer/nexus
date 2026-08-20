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
 * nexus-tk070.p6b (RDR-194 § D5, § P6b) — boundary rejection of
 * {@code ttl_days=0} at {@code POST /v1/telemetry/frecency/upsert}, the
 * live single-row write path. Mirrors {@code MemoryHandlerTest}'s
 * {@code put_ttlZeroIsRejectedWith400}-shaped tests and reuses
 * {@link TelemetryHandlerChunkIdValidationTest}'s exact no-DB harness: the
 * boundary check ({@code TelemetryHandler.requirePositiveOrNullTtlDays})
 * throws {@code IllegalArgumentException} BEFORE the repository is ever
 * reached, so a {@link TelemetryRepository} backed by a null
 * {@link javax.sql.DataSource} is safe to construct here.
 *
 * <p>Falsification check (recorded, not automated): deleting
 * {@code requirePositiveOrNullTtlDays}'s call site in
 * {@code TelemetryHandler.handleFrecencyUpsert} (reverting to the retired
 * {@code optInt(body, "ttl_days", 0)} shape) makes
 * {@link #frecencyUpsert_ttlDaysZero_returns400} fail — the request would
 * clear validation and 200, never reaching the repository at all in this
 * no-DB harness (it would then NPE/fail differently, never 400). This is
 * the SAME falsification shape as
 * {@code Tk070P6aTtlDaysCountedDeleteTest}'s and
 * {@code MemoryRepositoryTest}'s CHECK-layer tests, one layer up (the
 * boundary, not the CHECK).
 */
class TelemetryHandlerFrecencyTtlBoundaryTest {

    private static final String TENANT = "tk070-p6b-frecency-ttl-boundary-tenant";
    private static final String VALID_64_HEX =
        "ec8019fb0b9188069cc0ed3fa423e7c307d27cccc93324ca020fdb191679fd16";

    private final TelemetryHandler handler = new TelemetryHandler(
        new TelemetryRepository(new TenantScope(null)));

    @Test
    void frecencyUpsert_ttlDaysZero_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/frecency/upsert",
            "{\"chunk_id\":\"" + VALID_64_HEX + "\",\"ttl_days\":0}");
        handleWithTenant(ex);
        assertThat(ex.status).as("ttl_days=0 must be rejected loud, not silently reinterpreted").isEqualTo(400);
        assertThat(ex.bodyString())
            .as("the 400 must name the fix, matching MemoryHandler's requirePositiveOrNullTtl shape")
            .contains("ttl_days").contains("permanent").contains("NULL");
    }

    @Test
    void frecencyUpsert_ttlDaysNegative_returns400() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/frecency/upsert",
            "{\"chunk_id\":\"" + VALID_64_HEX + "\",\"ttl_days\":-5}");
        handleWithTenant(ex);
        assertThat(ex.status).as("a negative ttl_days must also be rejected").isEqualTo(400);
    }

    @Test
    void frecencyUpsert_ttlDaysOmitted_doesNotFailValidation() throws Exception {
        // Omitting ttl_days must mean permanent (null), not the retired 0 —
        // any status OTHER than 400 proves the boundary gate cleared (it
        // then fails downstream against the null DataSource, expected and
        // irrelevant to this test).
        CapturingExchange ex = post("/v1/telemetry/frecency/upsert",
            "{\"chunk_id\":\"" + VALID_64_HEX + "\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("an omitted ttl_days must not 400 the boundary gate").isNotEqualTo(400);
    }

    @Test
    void frecencyUpsert_ttlDaysNull_doesNotFailValidation() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/frecency/upsert",
            "{\"chunk_id\":\"" + VALID_64_HEX + "\",\"ttl_days\":null}");
        handleWithTenant(ex);
        assertThat(ex.status).as("an explicit null ttl_days must not 400 the boundary gate").isNotEqualTo(400);
    }

    @Test
    void frecencyUpsert_ttlDaysPositive_doesNotFailValidation() throws Exception {
        CapturingExchange ex = post("/v1/telemetry/frecency/upsert",
            "{\"chunk_id\":\"" + VALID_64_HEX + "\",\"ttl_days\":30}");
        handleWithTenant(ex);
        assertThat(ex.status).as("a positive ttl_days must not 400 the boundary gate").isNotEqualTo(400);
    }

    // ── Helpers (verbatim copy of TelemetryHandlerChunkIdValidationTest's
    //    CapturingExchange — duplicated per this project's established
    //    per-test-class convention rather than a shared utility class) ────

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
