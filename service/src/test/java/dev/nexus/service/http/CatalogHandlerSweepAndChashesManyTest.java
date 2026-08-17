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
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
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
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-eslkl / T2 nexus/design-eslkl-hook-lock-narrowing §8.1 (engine half) —
 * HTTP layer coverage for {@code POST /v1/catalog/manifest/chashes_many} and
 * the {@code "sweep"} flag on {@code POST /v1/catalog/manifest/write_many}.
 *
 * <p>Route/JSON-envelope coverage only; the guard's compute+persist
 * correctness is pinned at the repository layer by
 * {@code CatalogManifestSweepRepositoryTest}. Hermetic Testcontainers PG,
 * driven directly via {@link CatalogHandler#handle} with a capturing
 * {@link HttpExchange} (same idiom as {@code CatalogHandlerManifestFkTest} /
 * {@code TaxonomyHandlerAssignFromChashesTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CatalogHandlerSweepAndChashesManyTest {

    private static final String SVC_ROLE = "svc_sweep_http_test";
    private static final String SVC_PASS = "svc_sweep_http_test_pass";
    private static final String TENANT   = "sweep-http-tenant";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository repo;
    CatalogHandler handler;
    com.zaxxer.hikari.HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "'; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db).update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.grantServiceSchemaAccess(su, SVC_ROLE);
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

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    private void registerDoc(String tumbler, String collection) {
        repo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", "http-sweep-" + tumbler,
            "content_type", "code", "corpus", "code",
            "physical_collection", collection, "chunk_count", 0));
    }

    private void seedChunk384(String collection, String hexChash) throws Exception {
        repo.upsertCollection(TENANT, Map.of(
            "name", collection, "content_type", "code", "owner_id", "http-sweep-owner",
            "embedding_model", "minilm-l6-v2-384", "model_version", "v1"));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("SET nexus.tenant = '" + TENANT + "'");
            String zeroVec = "[" + "0,".repeat(383) + "0]";
            var ps = su.prepareStatement(
                "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(384) + ")"
                + " VALUES (?, ?, ?, ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
            ps.setString(1, TENANT);
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(hexChash));
            ps.setString(4, "seed text " + hexChash);
            ps.setString(5, zeroVec);
            ps.executeUpdate();
        }
    }

    // ── /v1/catalog/manifest/chashes_many ─────────────────────────────────────

    @Test
    void chashesMany_multiDoc_returnsChashesAndCount() throws Exception {
        String col = "code__httpcm1__minilm-l6-v2-384__v1";
        registerDoc("hcm.1", col);
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for this manifest write to succeed.
        seedChunk384(col, ch("hcm1a"));
        CapturingExchange w = post("/v1/catalog/manifest/write",
            "{\"doc_id\":\"hcm.1\",\"collection\":\"" + col + "\",\"rows\":[{\"position\":0,\"chash\":\"" + ch("hcm1a") + "\"}]}");
        handleWithTenant(w);
        assertThat(w.status).isEqualTo(200);

        CapturingExchange ex = post("/v1/catalog/manifest/chashes_many",
            "{\"doc_ids\":[\"hcm.1\",\"hcm.never-registered\"]}");
        handleWithTenant(ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString())
            .contains("\"chashes\":{\"hcm.1\":[\"" + ch("hcm1a") + "\"]}")
            .contains("\"count\":1");
    }

    @Test
    void chashesMany_emptyDocIds_returnsEmptyEnvelope() throws Exception {
        CapturingExchange ex = post("/v1/catalog/manifest/chashes_many", "{\"doc_ids\":[]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).isEqualTo("{\"chashes\":{},\"count\":0}");
    }

    @Test
    void chashesMany_tooManyDocIds_returns400() throws Exception {
        StringBuilder ids = new StringBuilder("[");
        for (int i = 0; i <= 1000; i++) {
            if (i > 0) ids.append(',');
            ids.append("\"hcm-cap-").append(i).append('"');
        }
        ids.append(']');
        CapturingExchange ex = post("/v1/catalog/manifest/chashes_many",
            "{\"doc_ids\":" + ids + "}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("too many doc_ids");
    }

    @Test
    void chashesMany_getMethod_returns405() throws Exception {
        CapturingExchange ex = new CapturingExchange("GET",
            URI.create("/v1/catalog/manifest/chashes_many"), "");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(405);
    }

    // ── /v1/catalog/manifest/write_many "sweep" flag ──────────────────────────

    @Test
    void writeMany_sweepAbsent_backwardCompatible_zeroSweepFields() throws Exception {
        String col = "code__httpwm1__minilm-l6-v2-384__v1";
        String x = ch("hwm1-x");
        seedChunk384(col, x);
        registerDoc("hwm.1", col);
        CapturingExchange seed = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + col + "\",\"docs\":[{\"doc_id\":\"hwm.1\",\"rows\":[{\"position\":0,\"chash\":\"" + x + "\"}]}]}");
        handleWithTenant(seed);
        assertThat(seed.status).isEqualTo(200);

        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the REPLACE write below too.
        seedChunk384(col, ch("hwm1-y"));
        // No "sweep" key at all — must behave exactly as before this feature.
        CapturingExchange ex = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + col + "\",\"docs\":[{\"doc_id\":\"hwm.1\",\"rows\":[{\"position\":0,\"chash\":\"" + ch("hwm1-y") + "\"}]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString())
            .contains("\"swept\":0")
            .contains("\"sweep_skipped\":0")
            .contains("\"sweep_detail\":[]");
    }

    @Test
    void writeMany_sweepTrue_dropsUnreferencedChash_returnsSweptCount() throws Exception {
        String col = "code__httpwm2__minilm-l6-v2-384__v1";
        String x = ch("hwm2-x");
        seedChunk384(col, x);
        registerDoc("hwm.2", col);
        CapturingExchange seed = post("/v1/catalog/manifest/write_many",
            "{\"collection\":\"" + col + "\",\"docs\":[{\"doc_id\":\"hwm.2\",\"rows\":[{\"position\":0,\"chash\":\"" + x + "\"}]}]}");
        handleWithTenant(seed);
        assertThat(seed.status).isEqualTo(200);

        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for the REPLACE write below too.
        seedChunk384(col, ch("hwm2-y"));
        CapturingExchange ex = post("/v1/catalog/manifest/write_many",
            "{\"sweep\":true,\"collection\":\"" + col + "\",\"docs\":[{\"doc_id\":\"hwm.2\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + ch("hwm2-y") + "\"}]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString())
            .contains("\"swept\":1")
            .contains("\"sweep_skipped\":0");
        assertThat(ex.bodyString()).contains("\"doc_id\":\"hwm.2\"").contains("\"dropped\":1");
    }

    @Test
    void writeMany_sweepNonBooleanValue_treatedAsFalse() throws Exception {
        String col = "code__httpwm3__minilm-l6-v2-384__v1";
        registerDoc("hwm.3", col);
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires a
        // matching nexus.chunks row for this manifest write to succeed.
        seedChunk384(col, ch("hwm3-a"));
        // "sweep":"true" (a STRING, not a JSON boolean) must NOT be truthy —
        // same "explicit true only" idiom as handleAssignMany's cross_collection.
        CapturingExchange ex = post("/v1/catalog/manifest/write_many",
            "{\"sweep\":\"true\",\"collection\":\"" + col + "\",\"docs\":[{\"doc_id\":\"hwm.3\",\"rows\":[{\"position\":0,"
            + "\"chash\":\"" + ch("hwm3-a") + "\"}]}]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"swept\":0").contains("\"sweep_detail\":[]");
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
