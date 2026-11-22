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
 * nexus-xoimv: {@code GET /v1/catalog/list} must thread {@code limit}/{@code
 * offset} through every filter branch (collection/content_type/corpus/owner/
 * file_path/owner+file_path), not just the terminal (unfiltered) branch.
 * Model: {@link CatalogHandlerUpdateManyDeleteManyTest} (hermetic
 * Testcontainers PG, drives {@link CatalogHandler#handle} directly via a
 * capturing {@link HttpExchange}).
 *
 * <p>Each branch is checked for all four properties nexus-xoimv calls for:
 * <ul>
 *   <li>an explicit {@code limit} is honored (fewer rows than the full set);</li>
 *   <li>{@code limit}+{@code offset} pages without overlap or gap — walking
 *       every page reconstructs the exact full ordered set;</li>
 *   <li>an ABSENT {@code limit} stays unbounded (the pre-xoimv contract for
 *       these seven branches — see {@link CatalogHandler#handleList});</li>
 *   <li>ordering is stable ({@code ORDER BY tumbler}) across pages.</li>
 * </ul>
 *
 * <p>{@code documentsBySourceUri} is NOT exercised here: {@code
 * catalog-016-source-uri-unique.xml} enforces a partial unique index on
 * {@code (tenant_id, source_uri)} for any non-empty value, so no HTTP-
 * reachable seed can produce more than one live row per matched
 * {@code source_uri} — and an empty value never routes into that branch at
 * all ({@code handleList} treats a blank {@code source_uri} query param as
 * absent). Its pagination is covered at the repository level in {@code
 * CatalogRepositoryTest} instead, which can call {@code
 * documentsBySourceUri(tenant, "", limit, offset)} directly.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerListPaginationTest {

    private static final String SVC_ROLE = "svc_cat_lp_test";
    private static final String SVC_PASS = "svc_cat_lp_test_pass";
    private static final String TENANT   = "cat-list-page-tenant";
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

    private static final int N = 7;

    /** Seeds N docs sharing the given filter value(s), tumblers {@code prefix.1..prefix.N}. */
    private List<String> seed(String tumblerPrefix, Map<String, Object> sharedFields) {
        List<String> tumblers = new java.util.ArrayList<>();
        for (int i = 1; i <= N; i++) {
            String t = tumblerPrefix + "." + i;
            var doc = new java.util.LinkedHashMap<String, Object>(sharedFields);
            doc.put("tumbler", t);
            doc.put("title", "Page Doc " + i);
            repo.upsertDocument(TENANT, doc);
            tumblers.add(t);
        }
        return tumblers; // already in tumbler (lexicographic) order: prefix.1 .. prefix.N
    }

    // ── collection branch ───────────────────────────────────────────────────

    @Test
    void collection_absentLimit_isUnbounded() throws Exception {
        var expected = seed("lpcoll1", Map.of(
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "list-page-coll-1"));
        var docs = listDocs("collection=list-page-coll-1");
        assertThat(tumblers(docs)).containsExactlyElementsOf(expected);
    }

    @Test
    void collection_explicitLimit_isHonored() throws Exception {
        seed("lpcoll2", Map.of(
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "list-page-coll-2"));
        var docs = listDocs("collection=list-page-coll-2&limit=3");
        assertThat(docs).hasSize(3);
    }

    @Test
    void collection_offsetPages_noOverlapNoGap_stableOrder() throws Exception {
        var expected = seed("lpcoll3", Map.of(
            "content_type", "paper", "corpus", "knowledge",
            "physical_collection", "list-page-coll-3"));
        assertPagesReconstructFullSet("collection=list-page-coll-3", expected, 3);
    }

    // ── content_type branch ─────────────────────────────────────────────────

    @Test
    void contentType_absentLimit_isUnbounded() throws Exception {
        var expected = seed("lpctype1", Map.of("content_type", "lp-ctype-1", "corpus", "knowledge"));
        var docs = listDocs("content_type=lp-ctype-1");
        assertThat(tumblers(docs)).containsExactlyElementsOf(expected);
    }

    @Test
    void contentType_explicitLimit_isHonored() throws Exception {
        seed("lpctype2", Map.of("content_type", "lp-ctype-2", "corpus", "knowledge"));
        var docs = listDocs("content_type=lp-ctype-2&limit=2");
        assertThat(docs).hasSize(2);
    }

    @Test
    void contentType_offsetPages_noOverlapNoGap_stableOrder() throws Exception {
        var expected = seed("lpctype3", Map.of("content_type", "lp-ctype-3", "corpus", "knowledge"));
        assertPagesReconstructFullSet("content_type=lp-ctype-3", expected, 3);
    }

    // ── corpus branch ────────────────────────────────────────────────────────

    @Test
    void corpus_absentLimit_isUnbounded() throws Exception {
        var expected = seed("lpcorpus1", Map.of("content_type", "code", "corpus", "lp-corpus-1"));
        var docs = listDocs("corpus=lp-corpus-1");
        assertThat(tumblers(docs)).containsExactlyElementsOf(expected);
    }

    @Test
    void corpus_explicitLimit_isHonored() throws Exception {
        seed("lpcorpus2", Map.of("content_type", "code", "corpus", "lp-corpus-2"));
        var docs = listDocs("corpus=lp-corpus-2&limit=4");
        assertThat(docs).hasSize(4);
    }

    @Test
    void corpus_offsetPages_noOverlapNoGap_stableOrder() throws Exception {
        var expected = seed("lpcorpus3", Map.of("content_type", "code", "corpus", "lp-corpus-3"));
        assertPagesReconstructFullSet("corpus=lp-corpus-3", expected, 2);
    }

    // ── owner branch (tumbler prefix) ───────────────────────────────────────

    @Test
    void owner_absentLimit_isUnbounded() throws Exception {
        var expected = seed("lpowner1", Map.of("content_type", "code", "corpus", "code"));
        var docs = listDocs("owner=lpowner1");
        assertThat(tumblers(docs)).containsExactlyElementsOf(expected);
    }

    @Test
    void owner_explicitLimit_isHonored() throws Exception {
        seed("lpowner2", Map.of("content_type", "code", "corpus", "code"));
        var docs = listDocs("owner=lpowner2&limit=5");
        assertThat(docs).hasSize(5);
    }

    @Test
    void owner_offsetPages_noOverlapNoGap_stableOrder() throws Exception {
        var expected = seed("lpowner3", Map.of("content_type", "code", "corpus", "code"));
        assertPagesReconstructFullSet("owner=lpowner3", expected, 3);
    }

    // ── file_path branch (owner-agnostic; file_path carries no unique index) ─

    @Test
    void filePath_absentLimit_isUnbounded() throws Exception {
        var expected = seed("lpfpath1", Map.of(
            "content_type", "code", "corpus", "code", "file_path", "shared/lp-fpath-1.py"));
        var docs = listDocs("file_path=shared/lp-fpath-1.py");
        assertThat(tumblers(docs)).containsExactlyElementsOf(expected);
    }

    @Test
    void filePath_explicitLimit_isHonored() throws Exception {
        seed("lpfpath2", Map.of(
            "content_type", "code", "corpus", "code", "file_path", "shared/lp-fpath-2.py"));
        var docs = listDocs("file_path=shared/lp-fpath-2.py&limit=3");
        assertThat(docs).hasSize(3);
    }

    @Test
    void filePath_offsetPages_noOverlapNoGap_stableOrder() throws Exception {
        var expected = seed("lpfpath3", Map.of(
            "content_type", "code", "corpus", "code", "file_path", "shared/lp-fpath-3.py"));
        assertPagesReconstructFullSet("file_path=shared/lp-fpath-3.py", expected, 3);
    }

    // ── owner + file_path branch (GH #1350 Fix B combined predicate) ────────

    @Test
    void ownerAndFilePath_absentLimit_isUnbounded() throws Exception {
        var expected = seed("lpofp1", Map.of(
            "content_type", "code", "corpus", "code", "file_path", "shared/lp-ofp-1.py"));
        var docs = listDocs("owner=lpofp1&file_path=shared/lp-ofp-1.py");
        assertThat(tumblers(docs)).containsExactlyElementsOf(expected);
    }

    @Test
    void ownerAndFilePath_explicitLimit_isHonored() throws Exception {
        seed("lpofp2", Map.of(
            "content_type", "code", "corpus", "code", "file_path", "shared/lp-ofp-2.py"));
        var docs = listDocs("owner=lpofp2&file_path=shared/lp-ofp-2.py&limit=2");
        assertThat(docs).hasSize(2);
    }

    @Test
    void ownerAndFilePath_offsetPages_noOverlapNoGap_stableOrder() throws Exception {
        var expected = seed("lpofp3", Map.of(
            "content_type", "code", "corpus", "code", "file_path", "shared/lp-ofp-3.py"));
        assertPagesReconstructFullSet("owner=lpofp3&file_path=shared/lp-ofp-3.py", expected, 3);
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /**
     * Walks every page at the given page size (via limit+offset) and asserts
     * the concatenation reconstructs *expected* exactly — no dupes, no drops,
     * no reordering across page boundaries.
     */
    private void assertPagesReconstructFullSet(String baseQuery, List<String> expected, int pageSize) throws Exception {
        List<String> reconstructed = new java.util.ArrayList<>();
        int offset = 0;
        while (true) {
            var page = listDocs(baseQuery + "&limit=" + pageSize + "&offset=" + offset);
            if (page.isEmpty()) break;
            reconstructed.addAll(tumblers(page));
            offset += pageSize;
            if (offset > expected.size() + pageSize) break; // safety valve against infinite loop on a bug
        }
        assertThat(reconstructed)
            .as("paging at size %d must reconstruct the full ordered set with no overlap/gap", pageSize)
            .containsExactlyElementsOf(expected);
    }

    @SuppressWarnings("unchecked")
    private List<String> tumblers(List<Map<String, Object>> docs) {
        return docs.stream().map(d -> (String) d.get("tumbler")).toList();
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> listDocs(String query) throws Exception {
        CapturingExchange ex = get("/v1/catalog/list?" + query);
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        assertThat(ex.status).as("response body: %s", ex.bodyString()).isEqualTo(200);
        Map<String, Object> body = MAPPER.readValue(ex.bodyString(), Map.class);
        return (List<Map<String, Object>>) body.get("documents");
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
