// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
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
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-176 Phase 4 (bead nexus-t9rmg.23 P4.T / .24 P4) — cloud→cloud server-side
 * ingest contract.
 *
 * <p>Two load-bearing assertions:
 * <ol>
 *   <li><b>Server-side copy, zero client vectors.</b> {@code POST
 *       /v1/migration/ingest-cloud} pulls pre-embedded chunks from a
 *       {@link MigrationHandler.CloudSource} (here a fake — no ChromaCloud/network)
 *       and lands them in pgvector. The client body carries only the trigger +
 *       source credentials; it never sends a vector, and the response echoes only
 *       counts.</li>
 *   <li><b>Ephemeral credentials (Pillar 1b).</b> The client-supplied
 *       ChromaCloud api key is used solely to open the source, then discarded: it
 *       appears in NO log line, NOT in the response body, and NOT in any persisted
 *       chunk metadata.</li>
 * </ol>
 *
 * <p>Integration over mocks: real Testcontainers pgvector + real
 * {@link PgVectorRepository} upsert; only the external ChromaCloud boundary is
 * faked (via the {@link MigrationHandler.CloudSourceFactory} seam).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MigrationHandlerIngestCloudTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String SVC_ROLE = "svc_migr_ingest_test";
    private static final String SVC_PASS = "svc_migr_ingest_test_pass";
    private static final String TENANT   = "migr-ingest-tenant";

    // 384-dim collection (minilm token) so the fake vectors stay small.
    private static final String COLL_A = "knowledge__migr__minilm-l6-v2-384__v1";
    private static final String COLL_B = "docs__migr__minilm-l6-v2-384__v1";

    // The sentinel credential we scan for across logs / response / persisted rows.
    private static final String SECRET_API_KEY = "ck-SENTINEL-super-secret-cloud-key-DO-NOT-LEAK";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    PgVectorRepository pgVectors;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
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
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_" + dim + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT SELECT ON nexus.catalog_documents, nexus.catalog_document_chunks TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        Embedder embedder = new NoopEmbedder(384);  // upsertChunksWithVectors skips it
        pgVectors = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void ingestCloud_copiesServerSide_andNeverLeaksCredentials() throws Exception {
        // Fake source: 2 collections, pre-embedded chunks. Records the creds it was
        // opened with so we can prove the handler USED them (in-memory) but leaked
        // them nowhere.
        var openedWith = new AtomicReference<String[]>();
        FakeCloud fake = new FakeCloud(openedWith);
        // chash ids are the 32-char chunk_text_hash (chunks_<dim>_chash_len_check).
        fake.put(COLL_A, "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1", "alpha one", 384);
        fake.put(COLL_A, "a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2a2", "alpha two", 384);
        fake.put(COLL_B, "b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1", "bravo one", 384);

        MigrationHandler handler = new MigrationHandler(pgVectors, fake);

        ListAppender<ILoggingEvent> logs = attachLogCapture();

        String reqBody = MAPPER.writeValueAsString(Map.of(
            "source_tenant", "src-tenant",
            "source_database", "src-db",
            "source_api_key", SECRET_API_KEY,
            "collections", List.of(COLL_A, COLL_B)));
        CapturingExchange ex = new CapturingExchange("POST",
            URI.create("/v1/migration/ingest-cloud"), reqBody);
        handleWithTenant(handler, ex);

        // (1) Server-side copy succeeded; response is counts only.
        assertThat(ex.status).as("ingest-cloud returns 200").isEqualTo(200);
        @SuppressWarnings("unchecked")
        Map<String, Object> resp = MAPPER.readValue(ex.bodyString(), Map.class);
        assertThat(resp.get("total")).isEqualTo(3);
        @SuppressWarnings("unchecked")
        Map<String, Object> copied = (Map<String, Object>) resp.get("copied");
        assertThat(copied).containsEntry(COLL_A, 2).containsEntry(COLL_B, 1);

        // (2) The vectors actually landed in pgvector server-side (client sent none).
        assertThat(superuserCount(384, COLL_A)).as("COLL_A chunks in chunks_384").isEqualTo(2L);
        assertThat(superuserCount(384, COLL_B)).as("COLL_B chunks in chunks_384").isEqualTo(1L);

        // (3) The handler DID use the client-supplied ephemeral creds (in-memory).
        assertThat(openedWith.get())
            .as("handler opened the source with the client's ephemeral creds")
            .containsExactly("src-tenant", "src-db", SECRET_API_KEY);

        // (4) The api key leaked NOWHERE: not the response body ...
        assertThat(ex.bodyString()).doesNotContain(SECRET_API_KEY);
        //     ... not any log line ...
        for (ILoggingEvent e : logs.list) {
            assertThat(e.getFormattedMessage())
                .as("no log line may contain the ephemeral api key").doesNotContain(SECRET_API_KEY);
        }
        //     ... not any persisted chunk metadata.
        assertThat(allChunkMetadataText(384))
            .as("the api key must not be persisted in any chunk metadata")
            .doesNotContain(SECRET_API_KEY);
    }

    @Test
    void ingestCloud_missingApiKey_returns400() throws Exception {
        MigrationHandler handler = new MigrationHandler(pgVectors,
            (t, d, k) -> { throw new AssertionError("source must not be opened on a bad request"); });
        String reqBody = MAPPER.writeValueAsString(Map.of(
            "source_tenant", "src-tenant", "source_database", "src-db"));  // no api key
        CapturingExchange ex = new CapturingExchange("POST",
            URI.create("/v1/migration/ingest-cloud"), reqBody);
        handleWithTenant(handler, ex);
        assertThat(ex.status).as("missing source_api_key → 400").isEqualTo(400);
        assertThat(ex.bodyString()).contains("source_api_key");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void handleWithTenant(MigrationHandler handler, CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private ListAppender<ILoggingEvent> attachLogCapture() {
        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> appender = new ListAppender<>();
        appender.start();
        root.addAppender(appender);
        return appender;
    }

    private long superuserCount(int dim, String collection) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.chunks_" + dim + " WHERE collection = ?")) {
            ps.setString(1, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    private String allChunkMetadataText(int dim) throws SQLException {
        StringBuilder sb = new StringBuilder();
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT metadata::text FROM nexus.chunks_" + dim);
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) sb.append(rs.getString(1)).append('\n');
        }
        return sb.toString();
    }

    /** Fake in-memory cloud source — records the creds it was opened with. */
    private static final class FakeCloud implements MigrationHandler.CloudSourceFactory {
        private final Map<String, List<String>> ids = new java.util.LinkedHashMap<>();
        private final Map<String, List<String>> docs = new java.util.LinkedHashMap<>();
        private final Map<String, List<float[]>> vecs = new java.util.LinkedHashMap<>();
        private final AtomicReference<String[]> openedWith;

        FakeCloud(AtomicReference<String[]> openedWith) {
            this.openedWith = openedWith;
        }

        void put(String coll, String id, String doc, int dim) {
            float[] v = new float[dim];
            v[0] = 1.0f;  // arbitrary unit-ish vector; value irrelevant to this test
            ids.computeIfAbsent(coll, k -> new ArrayList<>()).add(id);
            docs.computeIfAbsent(coll, k -> new ArrayList<>()).add(doc);
            vecs.computeIfAbsent(coll, k -> new ArrayList<>()).add(v);
        }

        @Override
        public MigrationHandler.CloudSource open(String tenant, String database, String apiKey) {
            openedWith.set(new String[] {tenant, database, apiKey});
            return new MigrationHandler.CloudSource() {
                @Override
                public List<String> collections() {
                    return new ArrayList<>(ids.keySet());
                }

                @Override
                public MigrationHandler.ChunkPage read(String coll, int limit, int offset) {
                    List<String> cids = ids.getOrDefault(coll, List.of());
                    if (offset >= cids.size()) return MigrationHandler.ChunkPage.empty();
                    int end = Math.min(offset + limit, cids.size());
                    List<Map<String, Object>> metas = new ArrayList<>();
                    for (int i = offset; i < end; i++) metas.add(Map.of("k", "v"));
                    return new MigrationHandler.ChunkPage(
                        cids.subList(offset, end),
                        docs.get(coll).subList(offset, end),
                        vecs.get(coll).subList(offset, end),
                        metas);
                }
            };
        }
    }

    /** Never actually invoked (vectors are supplied); satisfies the ctor. */
    private static final class NoopEmbedder implements Embedder {
        private final int dim;
        NoopEmbedder(int dim) { this.dim = dim; }
        @Override public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>();
            for (int i = 0; i < texts.size(); i++) out.add(new float[dim]);
            return out;
        }
        @Override public void close() {}
    }

    /** Minimal {@link HttpExchange} capturing response status + body. */
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
