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
 * RDR-193 Part A4 (nexus-u5m5g.4): {@code POST /v1/catalog/links/upsert_many} and {@code POST
 * /v1/catalog/links/same_content} over the handler, on PostgreSQL as the NOBYPASSRLS service role: the 1000-edge cap
 * (1000 accepted, 1001 a 400 that writes nothing), the response shapes, and the 400s for a request of the wrong shape.
 * The SQL itself is pinned by {@code CatalogLinksUpsertManyTest} and {@code CatalogLinkSameContentTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerLinksBatchRoutesTest {

    private static final String SVC_ROLE = "svc_lb193_routes";
    private static final String SVC_PASS = "svc_lb193_routes_pass";
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

    private String tenantWithDocs() {
        String t = "lb193http-" + (++tenantSeq);
        repo.upsertOwner(t, Map.of("tumbler_prefix", "1.1", "name", "own-1.1", "owner_type", "repo",
            "repo_hash", "h-" + t, "repo_root", "/r"));
        for (String p : List.of("a.md", "b.md")) {
            repo.registerDocument(t, "1.1", new LinkedHashMap<>(Map.of("title", p, "file_path", p, "content_type", "prose",
                "physical_collection", "docs__seed", "head_hash", "H0")));
        }
        return t;   // 1.1.1 = a.md, 1.1.2 = b.md
    }

    private static Map<String, Object> edge(String from, String to, String type) {
        var m = new LinkedHashMap<String, Object>();
        m.put("from", Map.of("tumbler", from));
        m.put("to", Map.of("tumbler", to));
        m.put("link_type", type);
        m.put("created_by", "gen");
        return m;
    }

    private Resp upsert(String t, Object edges) throws Exception {
        return call(t, "POST", "/v1/catalog/links/upsert_many", MAPPER.writeValueAsString(Map.of("edges", edges)));
    }

    private int linkCount(String t) {
        return tenantScope.withTenant(t, ctx -> ctx.fetchCount(dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS,
            dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS.TENANT_ID.eq(t)));
    }

    // ── upsert_many ─────────────────────────────────────────────────────

    @Test
    void upsertMany_aThousandEdgesAreAccepted_andAThousandAndOneIsA400ThatWritesNothing() throws Exception {
        String t = tenantWithDocs();
        List<Map<String, Object>> edges = new ArrayList<>();
        for (int i = 0; i < 1000; i++) edges.add(edge("1.1.1", "1.1.2", "t" + i));

        Resp over = upsert(t, concat(edges, edge("1.1.1", "1.1.2", "t1000")));
        assertThat(over.status).as(over.body).isEqualTo(400);
        assertThat(over.json().get("error").asText()).contains("too many edges").contains("1000");
        assertThat(linkCount(t)).as("a refused call wrote nothing").isZero();

        Resp ok = upsert(t, edges);
        assertThat(ok.status).as(ok.body).isEqualTo(200);
        assertThat(ok.json().get("created").asInt()).isEqualTo(1000);
        assertThat(linkCount(t)).isEqualTo(1000);
    }

    private static List<Map<String, Object>> concat(List<Map<String, Object>> a, Map<String, Object> b) {
        var out = new ArrayList<>(a);
        out.add(b);
        return out;
    }

    @Test
    void upsertMany_answersCreatedSkippedAndUnresolved() throws Exception {
        String t = tenantWithDocs();
        var byPath = edge("1.1.1", "1.1.2", "cites");
        byPath.put("to", Map.of("owner", "1.1", "file_path", "b.md"));
        var noPath = edge("1.1.1", "1.1.2", "cites");
        noPath.put("to", Map.of("owner", "1.1", "file_path", "missing.md"));

        Resp r = upsert(t, List.of(edge("1.1.1", "1.1.2", "cites"), byPath, edge("1.1.1", "1.1.9", "cites"), noPath));

        assertThat(r.status).as(r.body).isEqualTo(200);
        JsonNode j = r.json();
        assertThat(j.get("created").asInt()).isEqualTo(1);
        assertThat(j.get("skipped").asInt()).as("the path edge resolves to the same pair").isEqualTo(1);
        assertThat(j.get("unresolved")).hasSize(2);
        assertThat(j.get("unresolved").get(0)).isEqualTo(MAPPER.readTree("{\"index\":2,\"side\":\"to\",\"reason\":\"dangling\"}"));
        assertThat(j.get("unresolved").get(1)).isEqualTo(MAPPER.readTree("{\"index\":3,\"side\":\"to\",\"reason\":\"no_path\"}"));
    }

    @Test
    void upsertMany_anEmptyListIsZero_noEdgesKeyIsA400() throws Exception {
        String t = tenantWithDocs();
        Resp empty = upsert(t, List.of());
        assertThat(empty.status).as(empty.body).isEqualTo(200);
        assertThat(empty.json().toString()).isEqualTo("{\"created\":0,\"skipped\":0,\"unresolved\":[]}");

        assertThat(call(t, "POST", "/v1/catalog/links/upsert_many", "{}").status).isEqualTo(400);
        assertThat(call(t, "POST", "/v1/catalog/links/upsert_many", "{\"edges\":\"x\"}").status).isEqualTo(400);
    }

    @Test
    void upsertMany_aMalformedEdgeIs400NamingItsIndex_andWritesNothing() throws Exception {
        String t = tenantWithDocs();
        var good = edge("1.1.1", "1.1.2", "cites");
        var noType = edge("1.1.1", "1.1.2", "cites");
        noType.remove("link_type");
        var blankType = edge("1.1.1", "1.1.2", " ");
        var badFrom = edge("1.1.1", "1.1.2", "cites");
        badFrom.put("from", "1.1.1");
        var halfPath = edge("1.1.1", "1.1.2", "cites");
        halfPath.put("to", Map.of("owner", "1.1"));
        var numericTumbler = edge("1.1.1", "1.1.2", "cites");
        numericTumbler.put("to", Map.of("tumbler", 5));
        var badSpan = edge("1.1.1", "1.1.2", "cites");
        badSpan.put("from_span", 7);

        for (var bad : List.of(noType, blankType, badFrom, halfPath, numericTumbler, badSpan)) {
            Resp r = upsert(t, List.of(good, bad));
            assertThat(r.status).as(r.body).isEqualTo(400);
            assertThat(r.json().get("error").asText()).startsWith("edge 1: ");
        }
        assertThat(upsert(t, List.of("x")).status).isEqualTo(400);
        assertThat(linkCount(t)).as("the good edge beside a bad one was never written").isZero();
    }

    @Test
    void upsertMany_getIs405() throws Exception {
        assertThat(call(tenantWithDocs(), "GET", "/v1/catalog/links/upsert_many", "").status).isEqualTo(405);
    }

    // ── same_content ────────────────────────────────────────────────────

    private void pdf(String t, String path, String key) {
        repo.registerDocument(t, "1.1", new LinkedHashMap<>(Map.of("title", path, "file_path", path, "content_type", "pdf",
            "physical_collection", "docs__seed", "head_hash", "H0", "meta", Map.of("content_hash", key))));
    }

    @Test
    void sameContent_answersCreatedAndRetracted() throws Exception {
        String t = tenantWithDocs();
        pdf(t, "p1.pdf", "K");   // 1.1.3
        pdf(t, "p2.pdf", "K");   // 1.1.4

        Resp r = call(t, "POST", "/v1/catalog/links/same_content", "{\"tumblers\":[\"1.1.4\"],\"prune\":true}");

        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(r.json().toString()).isEqualTo("{\"created\":1,\"retracted\":0}");
        assertThat(linkCount(t)).isEqualTo(1);

        // prune defaults to false
        Resp noPrune = call(t, "POST", "/v1/catalog/links/same_content", "{\"tumblers\":[\"1.1.4\"]}");
        assertThat(noPrune.status).as(noPrune.body).isEqualTo(200);
        assertThat(noPrune.json().toString()).isEqualTo("{\"created\":0,\"retracted\":0}");
    }

    @Test
    void sameContent_refusesAnOversizedOrMalformedRequest() throws Exception {
        String t = tenantWithDocs();
        List<String> many = new ArrayList<>();
        for (int i = 0; i < 1001; i++) many.add("1.1." + (i + 1));

        Resp over = call(t, "POST", "/v1/catalog/links/same_content", MAPPER.writeValueAsString(Map.of("tumblers", many)));
        assertThat(over.status).as(over.body).isEqualTo(400);
        assertThat(over.json().get("error").asText()).contains("too many tumblers");

        assertThat(call(t, "POST", "/v1/catalog/links/same_content", "{}").status).isEqualTo(400);
        assertThat(call(t, "POST", "/v1/catalog/links/same_content", "{\"tumblers\":\"1.1.1\"}").status).isEqualTo(400);
        assertThat(call(t, "POST", "/v1/catalog/links/same_content", "{\"tumblers\":[\"\"]}").status).isEqualTo(400);
        assertThat(call(t, "POST", "/v1/catalog/links/same_content", "{\"tumblers\":[1]}").status).isEqualTo(400);
        assertThat(call(t, "POST", "/v1/catalog/links/same_content", "{\"tumblers\":[\"1.1.1\"],\"prune\":\"yes\"}").status).isEqualTo(400);
        assertThat(call(t, "GET", "/v1/catalog/links/same_content", "").status).isEqualTo(405);

        Resp thousand = call(t, "POST", "/v1/catalog/links/same_content",
            MAPPER.writeValueAsString(Map.of("tumblers", many.subList(0, 1000))));
        assertThat(thousand.status).as("1000 tumblers are accepted").isEqualTo(200);
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
