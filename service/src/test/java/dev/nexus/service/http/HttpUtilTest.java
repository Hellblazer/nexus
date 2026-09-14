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

    // ── docCountPostureTripwireMessage: P0001 + "topics_doc_count_recount_" prefix ──

    @Test
    void docCountPostureTripwireMessage_directSqlException() {
        String msg = "topics_doc_count_recount_del: nexus.tenant GUC (unset) does not cover "
            + "tenant_id(s) {t1}";
        assertThat(HttpUtil.docCountPostureTripwireMessage(new SQLException(msg, "P0001")))
            .isEqualTo(msg);
    }

    @Test
    void docCountPostureTripwireMessage_wrappedCause() {
        String msg = "topics_doc_count_recount_ins: nexus.tenant GUC (t2) does not cover "
            + "tenant_id(s) {t1}";
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException(msg, "P0001"));
        assertThat(HttpUtil.docCountPostureTripwireMessage(wrapped)).isEqualTo(msg);
    }

    @Test
    void docCountPostureTripwireMessage_unrelatedP0001_returnsNull() {
        // Same SQLSTATE (every plain RAISE EXCEPTION uses P0001 by default), but not
        // this trigger's message -- must NOT be reclassified as the tripwire.
        assertThat(HttpUtil.docCountPostureTripwireMessage(
            new SQLException("some other RAISE EXCEPTION entirely", "P0001")))
            .isNull();
    }

    @Test
    void docCountPostureTripwireMessage_matchingPrefixWrongSqlState_returnsNull() {
        assertThat(HttpUtil.docCountPostureTripwireMessage(
            new SQLException("topics_doc_count_recount_ins: nexus.tenant GUC ...", "23505")))
            .isNull();
    }

    // ── sendTypedDbError: the tripwire → 409 with detail, unaffected elsewhere ──

    @Test
    void sendTypedDbError_docCountPostureTripwire_maps409WithDetail() throws Exception {
        CapturingExchange ex = new CapturingExchange();
        String msg = "topics_doc_count_recount_del: nexus.tenant GUC (unset) does not cover "
            + "tenant_id(s) {wpath-tenant} present in this DELETE on topic_assignments -- "
            + "under FORCE ROW LEVEL SECURITY on nexus.topics this trigger's own UPDATE "
            + "would be silently filtered to zero rows for those tenants, leaving doc_count "
            + "stale (nexus-4a8pn case (f)). Remedy: set nexus.tenant for the writing "
            + "session before this statement.";
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException(msg, "P0001"));

        boolean handled = HttpUtil.sendTypedDbError(ex, wrapped, log, "test_handler", "op=/x");

        assertThat(handled)
            .as("the posture tripwire must be claimed here, not fall through to the caller's 500")
            .isTrue();
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"P0001\"");
        assertThat(ex.bodyString())
            .as("the PG message (which already names the tenant and the remedy) must reach"
                + " the client as the body's detail, not only the server log")
            .contains("nexus.tenant");
    }

    @Test
    void sendTypedDbError_unrelatedP0001_fallsThroughFalse() throws Exception {
        CapturingExchange ex = new CapturingExchange();
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException("some unrelated business-logic RAISE EXCEPTION", "P0001"));

        boolean handled = HttpUtil.sendTypedDbError(ex, wrapped, log, "test_handler", "op=/x");

        assertThat(handled)
            .as("an unrelated P0001 must keep the generic-500 policy -- only the"
                + " doc_count posture tripwire's own message prefix is mapped")
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
