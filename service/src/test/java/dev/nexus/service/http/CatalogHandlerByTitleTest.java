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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-gacz0: {@code GET /v1/catalog/by_title} returns every LIVE document whose title
 * equals {@code title} exactly, tenant-scoped, optionally narrowed by owner prefix and
 * content_type. What the full-text {@code /search} cannot give: it matches tokens, so a title of
 * common words matches every document sharing them. Drives {@link CatalogHandler#handle}
 * directly over hermetic Testcontainers PG, as {@link CatalogHandlerListPaginationTest} does.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerByTitleTest {

    private static final String SVC_ROLE = "svc_cat_bt_test";
    private static final String SVC_PASS = "svc_cat_bt_test_pass";
    private static final String TENANT   = "cat-by-title-tenant";
    private static final String OTHER_TENANT = "cat-by-title-other-tenant";
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

    private void doc(String tenant, String tumbler, String title, String contentType) {
        var d = new java.util.LinkedHashMap<String, Object>();
        d.put("tumbler", tumbler);
        d.put("title", title);
        d.put("content_type", contentType);
        d.put("corpus", "knowledge");
        repo.upsertDocument(tenant, d);
    }

    @Test
    void titleMatchIsExact_notTokenwise() throws Exception {
        doc(TENANT, "7.1.1", "The Design of Things", "knowledge");
        doc(TENANT, "7.1.2", "The Design of Things Part Two", "knowledge");
        doc(TENANT, "7.1.3", "Design of things", "knowledge");   // case differs
        doc(TENANT, "7.1.4", "the design of things ", "knowledge"); // trailing space
        // The full-text search finds all of them, which is the defect the route exists for.
        assertThat(repo.searchDocuments(TENANT, "The Design of Things", null, 50).size()).isGreaterThan(1);
        assertThat(tumblers(byTitle("title=The%20Design%20of%20Things"))).containsExactly("7.1.1");
    }

    @Test
    void ownerPrefixScopesToDescendants_andOtherOwnersAreExcluded() throws Exception {
        doc(TENANT, "8.1.1", "Shared Title Across Owners", "knowledge");
        doc(TENANT, "8.2.1", "Shared Title Across Owners", "knowledge");
        doc(TENANT, "81.1.1", "Shared Title Across Owners", "knowledge"); // "8" must not prefix-match "81"
        String t = "title=Shared%20Title%20Across%20Owners";
        // Order is the database collation's (a locale collation sorts across the dots), so this
        // cross-prefix assertion is on membership.
        assertThat(tumblers(byTitle(t))).containsExactlyInAnyOrder("8.1.1", "8.2.1", "81.1.1");
        assertThat(tumblers(byTitle(t + "&owner=8.1"))).containsExactly("8.1.1");
        assertThat(tumblers(byTitle(t + "&owner=8"))).containsExactly("8.1.1", "8.2.1");
        assertThat(tumblers(byTitle(t + "&owner=9"))).isEmpty();
    }

    @Test
    void anOwnerWithLikeWildcards_matchesLiterally() throws Exception {
        doc(TENANT, "wild_1.1", "Wildcard Owner Title", "knowledge");
        doc(TENANT, "wildX1.1", "Wildcard Owner Title", "knowledge");
        String t = "title=Wildcard%20Owner%20Title";
        // An unescaped underscore would also match "wildX1".
        assertThat(tumblers(byTitle(t + "&owner=wild_1"))).containsExactly("wild_1.1");
        assertThat(tumblers(byTitle(t + "&owner=%25"))).isEmpty();
    }

    @Test
    void contentTypeNarrows() throws Exception {
        doc(TENANT, "9.1.1", "Typed Title", "knowledge");
        doc(TENANT, "9.1.2", "Typed Title", "paper");
        assertThat(tumblers(byTitle("title=Typed%20Title"))).containsExactly("9.1.1", "9.1.2");
        assertThat(tumblers(byTitle("title=Typed%20Title&content_type=paper"))).containsExactly("9.1.2");
    }

    @Test
    void aTombstonedDocumentIsNotReturned() throws Exception {
        doc(TENANT, "10.1.1", "Soon Deleted Title", "knowledge");
        doc(TENANT, "10.1.2", "Soon Deleted Title", "knowledge");
        assertThat(repo.deleteDocument(TENANT, "10.1.1")).isEqualTo(1);
        assertThat(tumblers(byTitle("title=Soon%20Deleted%20Title"))).containsExactly("10.1.2");
    }

    @Test
    void anotherTenantsDocumentIsNeverReturned() throws Exception {
        doc(OTHER_TENANT, "11.1.1", "Tenant Scoped Title", "knowledge");
        assertThat(byTitle("title=Tenant%20Scoped%20Title")).isEmpty();
        doc(TENANT, "11.1.2", "Tenant Scoped Title", "knowledge");
        assertThat(tumblers(byTitle("title=Tenant%20Scoped%20Title"))).containsExactly("11.1.2");
    }

    @Test
    void aTitleWithReservedCharactersRoundTripsUrlEncoded() throws Exception {
        String title = "Q&A: 100% of a/b = c+d?";
        doc(TENANT, "12.1.1", title, "knowledge");
        String enc = java.net.URLEncoder.encode(title, StandardCharsets.UTF_8);
        assertThat(tumblers(byTitle("title=" + enc))).containsExactly("12.1.1");
    }

    @Test
    void everyMatchIsReturned_noPageCap() throws Exception {
        for (int i = 1; i <= 60; i++) {
            doc(TENANT, "13.1." + i, "Many Same Titled", "knowledge");
        }
        // 60 > searchDocuments' default 50: the route must not inherit that cap.
        assertThat(byTitle("title=Many%20Same%20Titled")).hasSize(60);
    }

    @Test
    void aMissingOrEmptyTitleIs400_notMatchEverything() throws Exception {
        assertThat(status("/v1/catalog/by_title?owner=8")).isEqualTo(400);
        assertThat(status("/v1/catalog/by_title?title=")).isEqualTo(400);
    }

    @Test
    void aNonGetIs405() throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create("/v1/catalog/by_title?title=x"));
        run(ex);
        assertThat(ex.status).isEqualTo(405);
    }

    private int status(String uri) throws Exception {
        CapturingExchange ex = get(uri);
        run(ex);
        return ex.status;
    }

    private void run(CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> byTitle(String query) throws Exception {
        CapturingExchange ex = get("/v1/catalog/by_title?" + query);
        run(ex);
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(body.get("count")).isEqualTo(((List<?>) body.get("documents")).size());
        return (List<Map<String, Object>>) body.get("documents");
    }

    @SuppressWarnings("unchecked")
    private List<String> tumblers(List<Map<String, Object>> docs) {
        return docs.stream().map(d -> (String) d.get("tumbler")).toList();
    }

    private static CapturingExchange get(String uri) {
        return new CapturingExchange("GET", URI.create(uri));
    }

    /** Minimal {@link HttpExchange} that captures the response status + body (GET, no body). */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri) {
            this.method = method;
            this.uri = uri;
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return new ByteArrayInputStream(new byte[0]); }
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
