// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
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
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-4y66t: the client's fail-closed tombstone read
 * ({@code nexus.catalog.tombstones.read_tombstones}) refuses a trash entry that
 * lacks the {@code file_path} KEY but accepts a present null or empty value (a
 * tombstoned paper or note has no file; the column is NOT NULL, so the engine
 * lists it as ""). That holds only while the handler's serializer keeps the key
 * for an empty value. This pins it at the handler, below the repository test
 * that only sees the Java map.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerTrashNullKeyTest {

    private static final String SVC_ROLE = "svc_cat_trashnull_http";
    private static final String SVC_PASS = "svc_cat_trashnull_http_pass";
    private static final String TENANT   = "cat-trashnull-http-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
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
        repo = new CatalogRepository(tenantScope);
        handler = new CatalogHandler(repo);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /**
     * nexus.catalog_documents.file_path is NOT NULL, so a document registered
     * with no path is stored (and listed) with an empty string, never JSON null.
     * What matters is that the entry carries the KEY: the client refuses an
     * entry without it and reads "" (like null) as "no path".
     */
    @Test
    void trashEntryForDocumentRegisteredWithNoFilePath_carriesTheKeyWithBlankValue() throws Exception {
        String tumbler = registerWithoutPathAndTombstone("trash-blank-path");
        var entry = trashEntry(tumbler);
        assertThat(entry.has("file_path"))
            .as("the key must be PRESENT (a client treats an absent key as an engine too old): %s", entry)
            .isTrue();
        assertThat(entry.get("file_path").isTextual() && entry.get("file_path").asText().isEmpty())
            .as("a document with no path lists as an empty string (the column is NOT NULL): %s", entry)
            .isTrue();
    }

    private String registerWithoutPathAndTombstone(String owner) {
        String tumbler = repo.registerDocument(TENANT, owner,
            Map.of("title", "Paper with no file " + owner, "content_type", "paper", "corpus", "knowledge",
                   "physical_collection", "knowledge__trash-null-key__minilm-l6-v2__v1"));
        assertThat(repo.deleteDocument(TENANT, tumbler)).isEqualTo(1);
        return tumbler;
    }

    private com.fasterxml.jackson.databind.JsonNode trashEntry(String tumbler) throws Exception {
        CapturingExchange ex = get("/v1/catalog/trash?limit=200&offset=0");
        handleWithTenant(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        for (com.fasterxml.jackson.databind.JsonNode d : MAPPER.readTree(ex.bodyString()).get("documents")) {
            if (tumbler.equals(d.get("tumbler").asText())) return d;
        }
        throw new AssertionError("tombstoned document " + tumbler + " not listed in: " + ex.bodyString());
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void handleWithTenant(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange get(String path) {
        return new CapturingExchange("GET", URI.create(path), "");
    }

    /** Minimal {@link HttpExchange} that captures the response status + body. */
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
