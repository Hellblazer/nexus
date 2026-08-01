// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import org.junit.jupiter.api.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.SQLException;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-dmrkm — {@link HttpUtil#sendTypedDbError} maps SQLSTATE class-22 data
 * exceptions (22021 {@code character_not_in_repertoire} — the NUL byte
 * Postgres {@code text}/{@code jsonb} cannot store, surfaced by nexus-yvzhz's
 * PDF-with-broken-ToUnicode-CMap; 22P05 {@code untranslatable_character}; and
 * siblings) to a typed 422, ahead of the generic {@code Exception}→500
 * fall-through at the bottom of every handler's catch block
 * ({@code PipelineHandler.handle} among them).
 *
 * <p>Class-wide (matches the whole {@code 22*} family), not a {@code 22021}-only
 * allowlist — mirrors how {@link HttpUtil#sqlState23} already treats the whole
 * class-23 integrity-violation family as one caller-error bucket rather than
 * enumerating {@code 23502}/{@code 23503}/{@code 23505} individually
 * (nexus-7e057). The bead explicitly names {@code 22021} and "at least
 * {@code 22P05}" as siblings that should not each need their own carve-out.
 *
 * <p>Pure-logic + a minimal capturing {@link HttpExchange} (mirrors
 * {@code AspectHandlerEnqueueErrorTest}'s {@code CapturingExchange}) — no
 * database needed, {@code sendTypedDbError} only inspects the exception's
 * cause chain.
 */
class HttpUtilTest {

    private static final Logger log = LoggerFactory.getLogger(HttpUtilTest.class);

    // ── pure-logic: sqlStateDataException detection ──────────────────────────

    @Test
    void sqlStateDataException_directSqlException() {
        assertThat(HttpUtil.sqlStateDataException(new SQLException("nul byte", "22021")))
            .isEqualTo("22021");
    }

    @Test
    void sqlStateDataException_wrappedCause() {
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException("nul byte", "22021"));
        assertThat(HttpUtil.sqlStateDataException(wrapped)).isEqualTo("22021");
    }

    @Test
    void sqlStateDataException_deeplyNestedCause() {
        Throwable e = new RuntimeException("a",
            new IllegalStateException("b", new SQLException("untranslatable", "22P05")));
        assertThat(HttpUtil.sqlStateDataException(e)).isEqualTo("22P05");
    }

    @Test
    void sqlStateDataException_nonClass22_returnsNull() {
        // 23505 = unique violation — a different, already-handled class; must
        // not be double-mapped by the class-22 walk.
        assertThat(HttpUtil.sqlStateDataException(new SQLException("dup", "23505"))).isNull();
    }

    @Test
    void sqlStateDataException_nonSqlException_returnsNull() {
        assertThat(HttpUtil.sqlStateDataException(new RuntimeException("plain"))).isNull();
    }

    @Test
    void sqlStateDataException_null_returnsNull() {
        assertThat(HttpUtil.sqlStateDataException(null)).isNull();
    }

    // ── sendTypedDbError: 22021 → 422, not 500 ────────────────────────────────

    @Test
    void sendTypedDbError_class22_sends422WithSqlstate() throws Exception {
        CapturingExchange ex = new CapturingExchange();
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException("invalid byte sequence for encoding \"UTF8\": 0x00", "22021"));

        boolean handled = HttpUtil.sendTypedDbError(ex, wrapped, log, "test_handler", "op=/x");

        assertThat(handled)
            .as("a class-22 cause must be claimed here, not fall through to the caller's 500")
            .isTrue();
        assertThat(ex.status).isEqualTo(422);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"22021\"");
        // Info-disclosure parity with the class-23 branch (AspectHandlerEnqueueErrorTest
        // pins the same discipline there): the raw driver message never reaches the
        // client body, only the server log.
        assertThat(ex.bodyString()).doesNotContain("invalid byte sequence");
    }

    @Test
    void sendTypedDbError_class22_untranslatableCharacter_alsoMapped() throws Exception {
        CapturingExchange ex = new CapturingExchange();
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException("untranslatable character", "22P05"));

        boolean handled = HttpUtil.sendTypedDbError(ex, wrapped, log, "test_handler", "op=/x");

        assertThat(handled)
            .as("class-wide match: 22P05 is mapped too, not just 22021")
            .isTrue();
        assertThat(ex.status).isEqualTo(422);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"22P05\"");
    }

    @Test
    void sendTypedDbError_class23_stillMapsTo409_unaffectedByClass22Addition() throws Exception {
        CapturingExchange ex = new CapturingExchange();
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException("not-null violation", "23502"));

        boolean handled = HttpUtil.sendTypedDbError(ex, wrapped, log, "test_handler", "op=/x");

        assertThat(handled).isTrue();
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23502\"");
    }

    @Test
    void sendTypedDbError_neitherClass_fallsThroughFalse() throws Exception {
        CapturingExchange ex = new CapturingExchange();
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException("syntax error", "42601"));

        boolean handled = HttpUtil.sendTypedDbError(ex, wrapped, log, "test_handler", "op=/x");

        assertThat(handled)
            .as("42601 is a genuine server fault — caller must still fall through to 500")
            .isFalse();
        assertThat(ex.status).isEqualTo(-1);
    }

    // ── minimal capturing HttpExchange (mirrors AspectHandlerEnqueueErrorTest) ─

    private static final class CapturingExchange extends HttpExchange {
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        String bodyString() {
            return responseBody.toString(StandardCharsets.UTF_8);
        }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return URI.create("/v1/pipeline/chunks"); }
        @Override public String getRequestMethod() { return "POST"; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() { }
        @Override public InputStream getRequestBody() { return new ByteArrayInputStream(new byte[0]); }
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
