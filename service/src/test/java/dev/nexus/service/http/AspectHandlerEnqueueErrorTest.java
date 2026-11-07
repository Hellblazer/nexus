package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TenantScope;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.SQLException;

import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-172 P3.1 (nexus-gfl3y) — AspectHandler maps SQLSTATE class-23 integrity
 * violations to a typed 4xx (409), ahead of the generic Exception→500.
 *
 * <p>Two layers:
 * <ol>
 *   <li>Pure-logic: {@link AspectHandler#sqlState23(Throwable)} detects a class-23
 *       SQLSTATE anywhere in the cause chain (jOOQ wraps the driver PSQLException),
 *       and returns null for non-class-23 / non-SQL / null inputs.</li>
 *   <li>Integration (Testcontainers): a real {@link AspectRepository} upsert with a
 *       non-blank UNREGISTERED {@code doc_id} hits the {@code document_aspects.doc_id}
 *       FK (bug nexus-ov0sw) and the handler responds 409 with the sqlstate — NOT a bare
 *       500 and NOT a silent 200. A blank {@code doc_id} 400s at the boundary (hygiene-001).
 *       The class once drove the same mapping through {@code /queue/enqueue}; the queue table
 *       and its routes are retired (nexus-hbnml), and the retired routes' 404 is pinned here.</li>
 * </ol>
 *
 * <p>Hermetic: embedded Postgres (Testcontainers pgvector), requires Docker. Drives the
 * real handler via {@link #handle} with a capturing {@link HttpExchange}; no HTTP server
 * or auth layer needed (the SQLSTATE mapping is handler-internal).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AspectHandlerEnqueueErrorTest {

    private static final String SVC_ROLE = "svc_aspect_enqueue_err_test";
    private static final String SVC_PASS = "svc_aspect_enqueue_err_test_pass";
    private static final String TENANT   = "aspect-enqueue-err-tenant";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    AspectRepository repo;
    AspectHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new AspectRepository(tenantScope);
        handler = new AspectHandler(repo);

        // RDR-164 P1a / RDR-204 Phase 1: document_aspects_collection_fk is a REAL,
        // always-enforced FK now -- register the collection the non-blank-doc_id upsert targets
        // (the blank-doc_id test short-circuits before ever reaching the collection FK).
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, "coll-enq-fk");
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    // ── Pure-logic: sqlState23 detection ─────────────────────────────────────────

    @Test
    void sqlState23_directSqlException() {
        assertThat(AspectHandler.sqlState23(new SQLException("fk violation", "23503")))
            .isEqualTo("23503");
    }

    @Test
    void sqlState23_wrappedCause() {
        Throwable wrapped = new RuntimeException("jOOQ DataAccessException",
            new SQLException("duplicate key", "23505"));
        assertThat(AspectHandler.sqlState23(wrapped)).isEqualTo("23505");
    }

    @Test
    void sqlState23_deeplyNestedCause() {
        Throwable e = new RuntimeException("a",
            new IllegalStateException("b", new SQLException("not-null", "23502")));
        assertThat(AspectHandler.sqlState23(e)).isEqualTo("23502");
    }

    @Test
    void sqlState23_nonClass23_returnsNull() {
        // 42601 = syntax error — a real server fault, must stay 500 (null here).
        assertThat(AspectHandler.sqlState23(new SQLException("syntax", "42601"))).isNull();
    }

    @Test
    void sqlState23_nonSqlException_returnsNull() {
        assertThat(AspectHandler.sqlState23(new RuntimeException("plain"))).isNull();
    }

    @Test
    void sqlState23_null_returnsNull() {
        assertThat(AspectHandler.sqlState23(null)).isNull();
    }

    // ── Integration: handler maps the real FK violation to 409 ───────────────────

    @Test
    void upsert_unregisteredDocId_returns409_withSqlstate() throws Exception {
        CapturingExchange ex = post("/v1/aspects/upsert",
            "{\"collection\":\"coll-enq-fk\",\"source_path\":\"fk-src.pdf\","
            + "\"doc_id\":\"unregistered-tumbler-zzz\",\"confidence\":0.9,"
            + "\"extracted_at\":\"2026-01-01T00:00:00Z\",\"model_version\":\"v1\",\"extractor_name\":\"ex\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a non-blank UNREGISTERED doc_id violates the document_aspects FK → typed 409, not 500/200")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23503\"");
        // Lock the info-disclosure fix: the client body is the fixed message, never
        // the raw jOOQ SQL (which names the table/constraint). A revert would fail here.
        assertThat(ex.bodyString())
            .contains("\"error\":\"integrity constraint violation\"")
            .doesNotContain("document_aspects");
        try (Connection su = pg.createConnection("")) {
            assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchExists(DOCUMENT_ASPECTS,
                    DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT).and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("fk-src.pdf"))))
                .as("a rejected upsert must not land a row").isFalse();
        }
    }

    @Test
    void upsert_blankDocId_returns400() throws Exception {
        CapturingExchange ex = post("/v1/aspects/upsert",
            "{\"collection\":\"coll-enq-ok\",\"source_path\":\"ok-src.pdf\",\"doc_id\":\"\","
            + "\"confidence\":0.9,\"extracted_at\":\"2026-01-01T00:00:00Z\",\"model_version\":\"v1\","
            + "\"extractor_name\":\"ex\"}");
        handleWithTenant(ex);
        assertThat(ex.status).as("blank doc_id must 400").isEqualTo(400);
        assertThat(ex.bodyString()).isEqualTo("{\"error\":\"doc_id required\"}");
    }

    /**
     * nexus-hbnml: the aspect_extraction_queue table is retired, and with it every {@code /v1/aspects/queue/*}
     * route. Each answers the 404 of any unknown aspects op, on both verbs, and reaches no repository code.
     */
    @Test
    void theRetiredQueueRoutes_answer404() throws Exception {
        for (String op : new String[]{"enqueue", "enqueue_many", "claim_next", "claim_batch", "mark_done", "mark_failed",
                "mark_retry", "reclaim_stale", "pending_count", "is_drained", "list_pending", "list_waiting",
                "list_failed", "rename_collection", "import"}) {
            for (String method : new String[]{"POST", "GET"}) {
                CapturingExchange ex = new CapturingExchange(method, URI.create("/v1/aspects/queue/" + op), "{}");
                handleWithTenant(ex);
                assertThat(ex.status).as(method + " /v1/aspects/queue/" + op).isEqualTo(404);
                assertThat(ex.bodyString()).contains("unknown aspects op");
            }
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────────────

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
