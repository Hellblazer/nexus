// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.JsonNode;
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
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-193 Part A4 item 2 (nexus-u5m5g.4): the combined {@code GET /v1/catalog/list} branch. {@code owner=<prefix>} with
 * one or more {@code content_type=} values returns that owner's documents of those types, and {@code
 * source_uri_prefix=} returns the documents whose URI starts with a prefix, so the dependency linker's two scoped
 * reads (the current owner's rdr and prose rows; the rows under {@code rdr_source_prefix}) are one request each. Every
 * filter sent is applied; a limit that is not sent leaves the branch unbounded (nexus-xoimv). The branches a caller
 * that sends none of the new parameters reaches are pinned by {@code CatalogHandlerListPaginationTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerListOwnerContentTypeTest {

    private static final String SVC_ROLE = "svc_lo193_list";
    private static final String SVC_PASS = "svc_lo193_list_pass";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;
    int tenantSeq = 0;

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
     * Owner 1.1 (root /r) and owner 1.2 (root /s) with, in registration order: 1.1.1 prose docs/a.md, 1.1.2 rdr
     * docs/rdr/rdr-001.md, 1.1.3 code src/m.py, 1.1.4 pdf docs/p.pdf, 1.1.5 prose docs/gone.md (tombstoned),
     * 1.1.6 prose d_cs/x.md, 1.1.7 prose dxcs/y.md, and 1.2.1 prose docs/b.md, 1.2.2 rdr docs/rdr/rdr-002.md.
     */
    private String tenantWithCatalog() {
        String t = "lo193http-" + (++tenantSeq);
        for (String[] o : new String[][] {{"1.1", "/r"}, {"1.2", "/s"}}) {
            repo.upsertOwner(t, Map.of("tumbler_prefix", o[0], "name", "own-" + o[0], "owner_type", "repo",
                "repo_hash", "h-" + t + o[0], "repo_root", o[1]));
        }
        reg(t, "1.1", "docs/a.md", "prose");
        reg(t, "1.1", "docs/rdr/rdr-001.md", "rdr");
        reg(t, "1.1", "src/m.py", "code");
        reg(t, "1.1", "docs/p.pdf", "pdf");
        String gone = reg(t, "1.1", "docs/gone.md", "prose");
        repo.deleteDocument(t, gone);
        reg(t, "1.1", "d_cs/x.md", "prose");
        reg(t, "1.1", "dxcs/y.md", "prose");
        reg(t, "1.2", "docs/b.md", "prose");
        reg(t, "1.2", "docs/rdr/rdr-002.md", "rdr");
        return t;
    }

    private String reg(String t, String owner, String path, String type) {
        return repo.registerDocument(t, owner, new LinkedHashMap<>(Map.of("title", path, "file_path", path,
            "content_type", type, "physical_collection", "docs__seed", "head_hash", "H0")));
    }

    private List<String> list(String t, String query) throws Exception {
        Resp r = call(t, "GET", "/v1/catalog/list?" + query, "");
        assertThat(r.status).as(r.body).isEqualTo(200);
        JsonNode j = r.json();
        List<String> out = new ArrayList<>();
        for (JsonNode d : j.get("documents")) out.add(d.get("tumbler").asText());
        assertThat(j.get("count").asInt()).as("count equals the rows returned").isEqualTo(out.size());
        return out;
    }

    // ── owner plus content types ────────────────────────────────────────

    @Test
    void ownerPlusTwoContentTypes_returnsOnlyThatOwnersRowsOfThoseTypes() throws Exception {
        String t = tenantWithCatalog();

        assertThat(list(t, "owner=1.1&content_type=rdr&content_type=prose"))
            .containsExactly("1.1.1", "1.1.2", "1.1.6", "1.1.7");
        assertThat(list(t, "owner=1.2&content_type=prose&content_type=rdr")).containsExactly("1.2.1", "1.2.2");
    }

    @Test
    void ownerPlusOneContentType_filtersByTheOwnerToo() throws Exception {
        String t = tenantWithCatalog();

        assertThat(list(t, "owner=1.1&content_type=rdr")).containsExactly("1.1.2");
        // the content_type branch alone, which a caller sending no owner reaches, is unchanged: every owner's rows
        assertThat(list(t, "content_type=rdr")).containsExactly("1.1.2", "1.2.2");
    }

    @Test
    void anOwnerPrefixIsMatchedAsAPrefixOfTheSegment() throws Exception {
        String t = tenantWithCatalog();
        // owner 1.1 must not match an owner 1.10 (the prefix carries its dot)
        repo.upsertOwner(t, Map.of("tumbler_prefix", "1.10", "name", "own-1.10", "owner_type", "repo",
            "repo_hash", "h-" + t + "1.10", "repo_root", "/ten"));
        reg(t, "1.10", "docs/z.md", "prose");

        assertThat(list(t, "owner=1.1&content_type=prose")).doesNotContain("1.10.1");
        assertThat(list(t, "owner=1.10&content_type=prose")).containsExactly("1.10.1");
    }

    // ── source_uri_prefix ───────────────────────────────────────────────

    @Test
    void sourceUriPrefix_returnsOnlyRowsUnderThePrefix() throws Exception {
        String t = tenantWithCatalog();

        assertThat(list(t, "source_uri_prefix=" + enc("file:///r/docs/")))
            .as("any owner's rows whose URI starts with it; the tombstone is not live")
            .containsExactly("1.1.1", "1.1.2", "1.1.4");
        assertThat(list(t, "source_uri_prefix=" + enc("file:///s/"))).containsExactly("1.2.1", "1.2.2");
        assertThat(list(t, "source_uri_prefix=" + enc("file:///nowhere/"))).isEmpty();
    }

    @Test
    void sourceUriPrefix_combinesWithOwnerAndContentTypes() throws Exception {
        String t = tenantWithCatalog();

        assertThat(list(t, "source_uri_prefix=" + enc("file:///r/docs/") + "&content_type=rdr&content_type=pdf"))
            .containsExactly("1.1.2", "1.1.4");
        assertThat(list(t, "owner=1.1&source_uri_prefix=" + enc("file:///r/docs/rdr/") + "&content_type=rdr"))
            .containsExactly("1.1.2");
        assertThat(list(t, "owner=1.2&source_uri_prefix=" + enc("file:///r/docs/")))
            .as("the owner and the prefix both apply").isEmpty();
    }

    @Test
    void sourceUriPrefix_isALiteralPrefix_likeMetacharactersDoNotMatch() throws Exception {
        String t = tenantWithCatalog();

        assertThat(list(t, "source_uri_prefix=" + enc("file:///r/d_cs/"))).containsExactly("1.1.6");
        assertThat(list(t, "source_uri_prefix=" + enc("file:///r/d%"))).isEmpty();
    }

    // ── limit, offset ───────────────────────────────────────────────────

    @Test
    void anAbsentLimitIsUnbounded_anExplicitOneIsHonored_andOffsetPages() throws Exception {
        String t = "lo193http-big-" + (++tenantSeq);
        repo.upsertOwner(t, Map.of("tumbler_prefix", "1.1", "name", "own", "owner_type", "repo",
            "repo_hash", "h-" + t, "repo_root", "/r"));
        for (int i = 0; i < 230; i++) reg(t, "1.1", "docs/f" + i + ".md", "rdr");

        assertThat(list(t, "owner=1.1&content_type=rdr")).as("no limit sent: more than the unfiltered 200").hasSize(230);
        assertThat(list(t, "source_uri_prefix=" + enc("file:///r/docs/"))).hasSize(230);
        List<String> page1 = list(t, "owner=1.1&content_type=rdr&limit=100");
        List<String> page2 = list(t, "owner=1.1&content_type=rdr&limit=100&offset=100");
        List<String> page3 = list(t, "owner=1.1&content_type=rdr&limit=100&offset=200");
        assertThat(page1).hasSize(100);
        assertThat(page2).hasSize(100);
        assertThat(page3).hasSize(30);
        List<String> all = new ArrayList<>(page1);
        all.addAll(page2);
        all.addAll(page3);
        assertThat(all).as("pages walk the full set in tumbler order without overlap").isEqualTo(list(t, "owner=1.1&content_type=rdr"));
    }

    // ── the other branches are untouched ────────────────────────────────

    @Test
    void theExistingBranchesStillAnswerAsBefore() throws Exception {
        String t = tenantWithCatalog();

        assertThat(list(t, "owner=1.2")).containsExactly("1.2.1", "1.2.2");
        assertThat(list(t, "content_type=pdf")).containsExactly("1.1.4");
        assertThat(list(t, "owner=1.1&file_path=" + enc("docs/a.md"))).containsExactly("1.1.1");
        assertThat(list(t, "source_uri=" + enc("file:///r/docs/a.md"))).containsExactly("1.1.1");
        assertThat(list(t, "collection=docs__seed&limit=3")).hasSize(3);
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    // ── the wire ────────────────────────────────────────────────────────

    private record Resp(int status, String body) {
        JsonNode json() throws Exception { return MAPPER.readTree(body); }
    }

    private Resp call(String tenant, String method, String path, String body) throws Exception {
        CapturingExchange ex = new CapturingExchange(method, URI.create(path), body);
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString());
    }

    /** Minimal {@link HttpExchange} that captures the response status and body. */
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
