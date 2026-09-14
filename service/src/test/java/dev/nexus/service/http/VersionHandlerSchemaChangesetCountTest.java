// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.HttpExchange;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-jl08t sibling sweep (critic finding, T2 [25635] SIGNIFICANT (a)):
 * {@code VersionHandler#schemaIdentity()}'s {@code schema_changeset_count}
 * must count DISTINCT {@code (id, author, filename)} identities, not
 * physical {@code databasechangelog} rows -- the same over-counting class
 * {@code SchemaMigrator.MigrationOutcome}'s javadoc documents for
 * {@code reexecuted_changesets}, and confirmed live on production
 * (conexus-9a, 2026-09-14): {@code public.databasechangelog} carries no
 * uniqueness constraint on that triple, so a duplicated identity inflates a
 * bare {@code COUNT(*)} forever.
 *
 * <p>Builds a minimal {@code public.databasechangelog}-shaped table directly
 * (no real Liquibase walk needed -- {@code VersionHandler} only ever reads
 * {@code id}/{@code author}/{@code filename}/{@code orderexecuted}) with one
 * duplicated identity, then asserts the live {@code /version} body reports
 * the DISTINCT count.
 */
class VersionHandlerSchemaChangesetCountTest {

    private static PostgreSQLContainer<?> pg;

    @BeforeAll
    static void startContainer() {
        pg = PgContainerHelper.startDedicated();
    }

    @AfterAll
    static void stopContainer() {
        if (pg != null) {
            pg.stop();
        }
    }

    private static final Table<?> DATABASECHANGELOG =
        DSL.table(DSL.name("public", "databasechangelog"));

    /** Minimal shape of the real Liquibase table -- only the columns
     * VersionHandler's schemaIdentity() reads. Field-based column() overload
     * matches this repo's established ad-hoc-table convention (e.g.
     * StagingPromoteOpsIntegrationTest's census_canary table), never a bare
     * string name. */
    private static void createMinimalDatabaseChangeLog(Connection conn) {
        Field<String> idField = DSL.field(DSL.name("id"), String.class);
        Field<String> authorField = DSL.field(DSL.name("author"), String.class);
        Field<String> filenameField = DSL.field(DSL.name("filename"), String.class);
        Field<Integer> orderField = DSL.field(DSL.name("orderexecuted"), Integer.class);
        DSL.using(conn, SQLDialect.POSTGRES)
            .createTable(DATABASECHANGELOG)
            .column(idField, org.jooq.impl.SQLDataType.CLOB)
            .column(authorField, org.jooq.impl.SQLDataType.CLOB)
            .column(filenameField, org.jooq.impl.SQLDataType.CLOB)
            .column(orderField, org.jooq.impl.SQLDataType.INTEGER)
            .execute();
    }

    private static void insertChangesetRow(
            DSLContext ctx, String id, String author, String filename, int orderExecuted) {
        Field<String> idField = DSL.field(DSL.name("id"), String.class);
        Field<String> authorField = DSL.field(DSL.name("author"), String.class);
        Field<String> filenameField = DSL.field(DSL.name("filename"), String.class);
        Field<Integer> orderField = DSL.field(DSL.name("orderexecuted"), Integer.class);
        ctx.insertInto(DATABASECHANGELOG, idField, authorField, filenameField, orderField)
            .values(id, author, filename, orderExecuted)
            .execute();
    }

    /** Minimal HttpExchange capture -- no server, no sockets (matches
     * ProbeEndpointsTest's own FakeExchange pattern). */
    private static final class FakeExchange extends HttpExchange {
        private final ByteArrayOutputStream body = new ByteArrayOutputStream();
        private final com.sun.net.httpserver.Headers headers = new com.sun.net.httpserver.Headers();
        int status = -1;

        String bodyString() { return body.toString(StandardCharsets.UTF_8); }

        @Override public com.sun.net.httpserver.Headers getRequestHeaders() { return headers; }
        @Override public com.sun.net.httpserver.Headers getResponseHeaders() { return headers; }
        @Override public URI getRequestURI() { return URI.create("/version"); }
        @Override public String getRequestMethod() { return "GET"; }
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
    void schemaChangesetCountCountsDistinctIdentitiesNotDuplicateRows() throws Exception {
        try (HikariDataSource ds = PgContainerHelper.superuserDataSource(pg)) {
            try (Connection conn = ds.getConnection()) {
                createMinimalDatabaseChangeLog(conn);
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                // 3 distinct identities; "dup-id" carries 4 EXTRA physical
                // copies (5 rows total for that one identity) -- reproducing
                // the production shape (public.databasechangelog has no
                // uniqueness constraint on (id, author, filename)).
                insertChangesetRow(ctx, "unique-1", "authorA", "file-a.xml", 1);
                insertChangesetRow(ctx, "unique-2", "authorB", "file-b.xml", 2);
                for (int i = 0; i < 5; i++) {
                    insertChangesetRow(ctx, "dup-id", "authorC", "file-c.xml", 3 + i);
                }
            }

            VersionHandler handler = new VersionHandler(ds);
            FakeExchange exchange = new FakeExchange();
            handler.handle(exchange);

            assertThat(exchange.status).isEqualTo(200);
            String body = exchange.bodyString();
            assertThat(body).contains("\"schema_changeset_count\":");

            Matcher m = Pattern.compile("\"schema_changeset_count\":(\\d+)").matcher(body);
            assertThat(m.find()).as("schema_changeset_count must be a bare number: %s", body).isTrue();
            long reported = Long.parseLong(m.group(1));

            assertThat(reported)
                .as("3 distinct changeset identities (unique-1, unique-2, dup-id) despite "
                    + "7 physical rows (2 + 5 duplicate copies of dup-id) -- a bare row "
                    + "COUNT(*) would report 7")
                .isEqualTo(3L);
        }
    }
}
