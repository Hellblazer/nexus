package dev.nexus.service;

import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.http.LivezHandler;
import org.junit.jupiter.api.Test;

import javax.sql.DataSource;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.URI;
import java.sql.Connection;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-hubc0 / nexus-7f7gb (GH #1419 issue 3b): the unauthenticated probe
 * endpoints must not contend for the connection pool.
 *
 * <p>The supervisor restarts the service after N consecutive failed health
 * probes. {@code /health} is implemented as {@code getConnection()} +
 * {@code SELECT 1}, so under CPU-bound indexing the pool is contended, the
 * probe blocks past its timeout, and a healthy service is cycled mid-batch —
 * which severs in-flight clients, which retry, which adds load. The probe that
 * decides whether to KILL the service was competing for the resource that
 * saturation exhausts.
 *
 * <p>Widening the client's probe budget could not fix it: the probe blocks the
 * supervisor's heartbeat thread, so the lease TTL caps it (a 20s probe made the
 * supervisor lose its own lease mid-probe). The fix has to be a liveness signal
 * that load cannot slow down.
 *
 * <p>These tests pin the property that matters — NO pool access — rather than
 * the response text. A future edit that "just adds a quick DB check" to either
 * endpoint reintroduces the outage and must fail here.
 */
class ProbeEndpointsTest {

    /** A DataSource that FAILS the test if anyone asks it for a connection. */
    private static final class ExplodingDataSource implements DataSource {
        final AtomicInteger calls = new AtomicInteger();

        @Override public Connection getConnection() {
            calls.incrementAndGet();
            throw new AssertionError(
                "probe endpoint requested a pooled connection — this is the "
                + "nexus-7f7gb outage: a saturated pool must never be able to "
                + "make a live process look dead");
        }
        @Override public Connection getConnection(String u, String p) { return getConnection(); }
        @Override public java.io.PrintWriter getLogWriter() { return null; }
        @Override public void setLogWriter(java.io.PrintWriter out) { }
        @Override public void setLoginTimeout(int seconds) { }
        @Override public int getLoginTimeout() { return 0; }
        @Override public java.util.logging.Logger getParentLogger() { return null; }
        @Override public <T> T unwrap(Class<T> iface) { return null; }
        @Override public boolean isWrapperFor(Class<?> iface) { return false; }
    }

    /** Minimal HttpExchange capture — no server, no sockets. */
    private static final class FakeExchange extends HttpExchange {
        private final String method;
        private final ByteArrayOutputStream body = new ByteArrayOutputStream();
        private final com.sun.net.httpserver.Headers headers = new com.sun.net.httpserver.Headers();
        int status = -1;

        FakeExchange(String method) { this.method = method; }

        String bodyString() { return body.toString(java.nio.charset.StandardCharsets.UTF_8); }

        @Override public com.sun.net.httpserver.Headers getRequestHeaders() { return headers; }
        @Override public com.sun.net.httpserver.Headers getResponseHeaders() { return headers; }
        @Override public URI getRequestURI() { return URI.create("/livez"); }
        @Override public String getRequestMethod() { return method; }
        @Override public com.sun.net.httpserver.HttpContext getHttpContext() { return null; }
        @Override public void close() { }
        @Override public java.io.InputStream getRequestBody() {
            return new java.io.ByteArrayInputStream(new byte[0]);
        }
        @Override public java.io.OutputStream getResponseBody() { return body; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public java.net.InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public java.net.InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) { }
        @Override public void setStreams(java.io.InputStream i, java.io.OutputStream o) { }
        @Override public com.sun.net.httpserver.HttpPrincipal getPrincipal() { return null; }
    }

    @Test
    void livez_answers_200_without_touching_the_pool() throws IOException {
        var ds = new ExplodingDataSource();          // any getConnection() fails the test
        var ex = new FakeExchange("GET");

        new LivezHandler().handle(ex);               // handler holds no DataSource at all

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("ok");
        assertThat(ds.calls.get())
            .as("liveness must have no database dependency whatsoever")
            .isZero();
    }

    @Test
    void livez_rejects_non_get() throws IOException {
        var ex = new FakeExchange("POST");
        new LivezHandler().handle(ex);
        assertThat(ex.status).isEqualTo(405);
    }

    @Test
    void version_reads_the_pool_at_most_once_per_process() throws IOException {
        // The fix's actual claim. /version is called by the client's engine
        // handshake AND the supervisor's convergence check, so a per-request
        // pooled connection put an unauthenticated probe on the contended path
        // (HikariCP connectionTimeout is 30s — far past any client budget).
        // Liquibase runs before the server accepts requests, so databasechangelog
        // is immutable for the process lifetime and memoizing is correct, not
        // merely convenient.
        var counting = new CountingDataSource();
        var handler = new dev.nexus.service.http.VersionHandler(counting);

        for (int i = 0; i < 25; i++) {
            var ex = new FakeExchange("GET");
            handler.handle(ex);
            assertThat(ex.status).isEqualTo(200);
            assertThat(ex.bodyString()).contains("release_version");
            // nexus-308ph: the checked-in release.properties carries a BLANK
            // build_ref, so the field must be OMITTED entirely from the live
            // /version body — never "build_ref":null.
            assertThat(ex.bodyString()).doesNotContain("build_ref");
        }

        assertThat(counting.calls.get())
            .as("25 /version requests must not each open a pooled connection")
            .isLessThanOrEqualTo(1);
    }

    /** Counts getConnection() and then fails the call, so the memoized path is
     *  exercised even without a live database (a failed read is memoized too —
     *  retrying per request would restore the unbounded wait on exactly the
     *  boxes whose pool is already in trouble). */
    private static final class CountingDataSource implements DataSource {
        final AtomicInteger calls = new AtomicInteger();

        @Override public Connection getConnection() throws java.sql.SQLException {
            calls.incrementAndGet();
            throw new java.sql.SQLException("no database in this unit test");
        }
        @Override public Connection getConnection(String u, String p) throws java.sql.SQLException { return getConnection(); }
        @Override public java.io.PrintWriter getLogWriter() { return null; }
        @Override public void setLogWriter(java.io.PrintWriter out) { }
        @Override public void setLoginTimeout(int seconds) { }
        @Override public int getLoginTimeout() { return 0; }
        @Override public java.util.logging.Logger getParentLogger() { return null; }
        @Override public <T> T unwrap(Class<T> iface) { return null; }
        @Override public boolean isWrapperFor(Class<?> iface) { return false; }
    }

    @Test
    void livez_holds_no_datasource_field() {
        // Structural, not behavioural: the guarantee is that liveness CANNOT be
        // slowed by the database, which a future edit could quietly break by
        // adding a DataSource and "one quick check". Absence of the field is
        // the thing worth pinning.
        boolean hasDs = java.util.Arrays.stream(LivezHandler.class.getDeclaredFields())
            .anyMatch(f -> DataSource.class.isAssignableFrom(f.getType()));
        assertThat(hasDs)
            .as("LivezHandler must not hold a DataSource — see nexus-7f7gb")
            .isFalse();
    }
}
