package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Clock;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-868dq Gate-A critique — the HANDLER-LEVEL scope guard, proven
 * independently of {@link AuthFilter}.
 *
 * <p>On every deployed route the filter's mint-surface gate fires first, so the
 * handler's own {@code mint}-branch is unreachable over HTTP — which is exactly
 * why it needs a direct-dispatch unit test: if the filter's path check ever
 * regresses (a future admin route registered under {@code /v1/data-tokens/...}),
 * this layer must still hold. The {@code data}-branch IS the primary enforcement
 * (the filter deliberately passes data tokens through) and is additionally
 * covered end-to-end in {@code TokenAdminHandlerTest}.
 *
 * <p>The guard runs before any store access, so the handler is constructed with
 * null collaborators — a store call would NPE and fail the test loudly.
 */
class TokenAdminHandlerScopeGuardTest {

    private static final String[] ROUTES = {
        "/v1/tenants/create", "/v1/service-tokens/issue", "/v1/service-tokens/rotate",
        "/v1/service-tokens/revoke", "/v1/service-tokens/list",
    };

    @Test
    void mintScope_directDispatch_403OnEveryRoute() throws Exception {
        assertScopeRejected("mint");
    }

    @Test
    void dataScope_directDispatch_403OnEveryRoute() throws Exception {
        assertScopeRejected("data");
    }

    private void assertScopeRejected(String scope) throws Exception {
        var handler = new TokenAdminHandler(null, null, Clock.systemUTC());
        for (String route : ROUTES) {
            CapturingExchange ex = new CapturingExchange("POST", URI.create(route), "{}");
            RequestContext.set(new RequestContext.Principal(
                "some-tenant", null, false, false, scope, "test-credential-hash"));
            try {
                handler.handle(ex);
            } finally {
                RequestContext.clear();
            }
            assertThat(ex.status)
                .as("'%s'-scoped bearer direct-dispatched to %s", scope, route)
                .isEqualTo(403);
            assertThat(ex.bodyString()).contains("may not use the token admin surface");
        }
    }

    /** Minimal {@link HttpExchange} capturing status + body (per-file fake). */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return requestBody; }
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
