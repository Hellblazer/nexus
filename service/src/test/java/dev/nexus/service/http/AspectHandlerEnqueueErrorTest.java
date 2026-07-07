package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.AspectRepository;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
import java.util.List;

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
 *   <li>Integration (Testcontainers): a real {@link AspectRepository} enqueue with a
 *       non-blank UNREGISTERED {@code doc_id} hits the {@code aspect_extraction_queue.doc_id}
 *       FK (bug nexus-ov0sw) and the handler responds 409 with the sqlstate — NOT a bare
 *       500 and NOT a silent 200. A blank {@code doc_id} (nullIfBlank → NULL → no FK) still
 *       returns 200 (unchanged behaviour).</li>
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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String table : List.of("document_aspects", "document_highlights",
                                        "aspect_extraction_queue", "aspect_promotion_log")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + table + " TO " + SVC_ROLE);
                su.createStatement().execute(
                    "GRANT USAGE ON SEQUENCE nexus." + table + "_id_seq TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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
    void enqueue_unregisteredDocId_returns409_withSqlstate() throws Exception {
        CapturingExchange ex = post("/v1/aspects/queue/enqueue",
            "{\"collection\":\"coll-enq-fk\",\"source_path\":\"fk-src.pdf\","
            + "\"doc_id\":\"unregistered-tumbler-zzz\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("a non-blank UNREGISTERED doc_id violates the queue FK → typed 409, not 500/200")
            .isEqualTo(409);
        assertThat(ex.bodyString()).contains("\"sqlstate\":\"23503\"");
        // Lock the info-disclosure fix: the client body is the fixed message, never
        // the raw jOOQ SQL (which names the table/constraint). A revert would fail here.
        assertThat(ex.bodyString())
            .contains("\"error\":\"integrity constraint violation\"")
            .doesNotContain("aspect_extraction_queue");
    }

    @Test
    void enqueue_blankDocId_returns200() throws Exception {
        CapturingExchange ex = post("/v1/aspects/queue/enqueue",
            "{\"collection\":\"coll-enq-ok\",\"source_path\":\"ok-src.pdf\",\"doc_id\":\"\"}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("blank doc_id → nullIfBlank → NULL → no FK violation → unchanged 200")
            .isEqualTo(200);
        // Non-vacuity: the row must carry doc_id IS NULL (not ""). An empty string
        // would also dodge the FK, so a status-only assertion can't tell nullIfBlank
        // apart from a broken coercion. Superuser connection bypasses RLS.
        try (Connection su = pg.createConnection("");
             var st = su.createStatement();
             var rs = st.executeQuery(
                 "SELECT doc_id FROM nexus.aspect_extraction_queue "
                 + "WHERE tenant_id='" + TENANT + "' AND collection='coll-enq-ok' "
                 + "AND source_path='ok-src.pdf'")) {
            assertThat(rs.next()).as("the blank-doc_id enqueue must have landed a row").isTrue();
            rs.getString("doc_id");
            assertThat(rs.wasNull()).as("doc_id must be SQL NULL, not empty string").isTrue();
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
