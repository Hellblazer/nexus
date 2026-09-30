// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CombinedWriteService;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
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
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-223 P1.1 (bead nexus-z0o2p.2) -- the HTTP face of {@code POST
 * /v1/catalog/manifest/append} with an inline {@code chunks} array: request
 * validation, the {@code X-Nexus-Usage-Tokens} header, the response fields a
 * client uses to detect an old engine that ignores {@code chunks}, and the
 * unchanged no-chunks path. Drives {@link CatalogHandler#handle} directly with a
 * capturing exchange, like {@code CatalogHandlerManifestFkTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerAppendChunksTest {

    private static final String SVC_ROLE = "svc_append_http_test";
    private static final String SVC_PASS = "svc_append_http_test_pass";
    private static final String TENANT   = "append-http-tenant";
    private static final String COLLECTION = "code__aphttp__minilm-l6-v2-384__v1";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private TenantScope tenantScope;
    private CatalogRepository repo;
    private CatalogHandler handler;
    private CatalogHandler handlerWithoutService;
    private final AtomicInteger embeds = new AtomicInteger();

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
        Embedder fake = new Embedder() {
            @Override public List<float[]> embed(List<String> texts) {
                embeds.addAndGet(texts.size());
                List<float[]> out = new ArrayList<>();
                for (String t : texts) {
                    float[] v = new float[384];
                    v[Math.floorMod(t.hashCode(), 384)] = 1.0f;
                    out.add(v);
                }
                return out;
            }
            @Override public EmbedResult embedWithUsage(List<String> texts) {
                return new EmbedResult(embed(texts), 7L * texts.size());
            }
            @Override public String modelToken() { return "minilm-l6-v2-384"; }
        };
        handler = new CatalogHandler(repo,
            new CombinedWriteService(tenantScope, repo, new EmbedderRouter(fake, "document")));
        handlerWithoutService = new CatalogHandler(repo);
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void registerDoc(String tumbler) {
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "append-http-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", COLLECTION, "chunk_count", 0));
    }

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    // ── the no-chunks path is unchanged ─────────────────────────────────────────

    @Test
    void append_withoutChunks_isByteForByteTheOldResponse_andWritesTheRows() throws Exception {
        registerDoc("aph.1");
        // A chunk the row can reference, written through the combined route first.
        String c = ch("aph1-c");
        CapturingExchange seed = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + COLLECTION + "\",\"docs\":[{\"doc_id\":\"aph.1\",\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}]}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"aph1 c\"}]}");
        handle(handler, seed);
        assertThat(seed.status).isEqualTo(200);

        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.1\",\"collection\":\"" + COLLECTION + "\",\"rows\":[{\"position\":1,\"chash\":\"" + c + "\"}]}");
        handle(handler, ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).isEqualTo("{\"ok\":true,\"count\":1}");
        assertThat(ex.responseHeaders.containsKey(VectorHandler.USAGE_TOKENS_HEADER)).isFalse();
        assertThat(repo.getManifest(TENANT, "aph.1")).hasSize(2);
    }

    // ── append with chunks ──────────────────────────────────────────────────────

    @Test
    void append_withChunks_landsChunkAndRowAndReportsCounts() throws Exception {
        registerDoc("aph.2");
        String c = ch("aph2-c");
        int before = embeds.get();

        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.2\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"aph2 c\",\"metadata\":{\"k\":\"v\"}}]}");
        handle(handler, ex);

        assertThat(ex.status).isEqualTo(200);
        String body = ex.bodyString();
        assertThat(body).contains("\"ok\":true").contains("\"count\":1")
            .contains("\"chunks_written\":1").contains("\"chunks_deduped\":1")
            .contains("\"embed_skipped\":0").contains("\"embed_embedded\":1");
        assertThat(ex.responseHeaders.getFirst(VectorHandler.USAGE_TOKENS_HEADER))
            .as("embed token usage rides the response header, as write_many does").isEqualTo("7");
        assertThat(embeds.get() - before).isEqualTo(1);
        assertThat(repo.getManifest(TENANT, "aph.2")).hasSize(1);
    }

    // ── validation ──────────────────────────────────────────────────────────────

    @Test
    void append_chunksNotAList_400() throws Exception {
        registerDoc("aph.3");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.3\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"chunks\":\"nope\"}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("'chunks' must be a list");
    }

    @Test
    void append_chunkMissingText_400() throws Exception {
        registerDoc("aph.4");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.4\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"chunks\":[{\"chash\":\"" + ch("aph4") + "\"}]}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunks[0]").contains("'text' required");
    }

    @Test
    void append_chunkWithLegacy32CharChash_400() throws Exception {
        registerDoc("aph.5");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.5\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],"
            + "\"chunks\":[{\"chash\":\"" + "a".repeat(32) + "\",\"text\":\"t\"}]}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("chunks[0]").contains("legacy 32-hex");
    }

    @Test
    void append_chunksWithNoCombinedWriteService_503() throws Exception {
        registerDoc("aph.6");
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.6\",\"collection\":\"" + COLLECTION + "\",\"rows\":[],\"chunks\":[]}");
        handle(handlerWithoutService, ex);
        assertThat(ex.status).isEqualTo(503);
    }

    @Test
    void append_chunksForMissingDocument_409_andNothingInserted() throws Exception {
        String c = ch("aph7-c");
        int before = embeds.get();
        CapturingExchange ex = post("/v1/catalog/manifest/append",
            "{\"doc_id\":\"aph.no-such\",\"collection\":\"" + COLLECTION + "\","
            + "\"rows\":[{\"position\":0,\"chash\":\"" + c + "\"}],"
            + "\"chunks\":[{\"chash\":\"" + c + "\",\"text\":\"orphan\"}]}");
        handle(handler, ex);
        assertThat(ex.status).isEqualTo(409);
        assertThat(ex.bodyString()).contains("document not registered: aph.no-such");
        assertThat(embeds.get() - before).as("no embed for a document that does not exist").isZero();
    }

    // ── helpers ──────────────────────────────────────────────────────────────────

    private void handle(CatalogHandler h, CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            h.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private static CapturingExchange post(String path, String jsonBody) {
        return new CapturingExchange("POST", URI.create(path), jsonBody);
    }

    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final InputStream requestBody;
        final Headers responseHeaders = new Headers();
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
