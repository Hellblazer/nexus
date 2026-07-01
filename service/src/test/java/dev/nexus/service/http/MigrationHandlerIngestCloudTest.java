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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-176 Phase 4 (bead nexus-t9rmg.23 P4.T / .24 P4) — cloud→cloud server-side
 * ingest contract.
 *
 * <p>Assertions:
 * <ol>
 *   <li><b>Server-side copy, zero client vectors + parity signal.</b> {@code POST
 *       /v1/migration/ingest-cloud} pulls pre-embedded chunks from a
 *       {@link MigrationHandler.CloudSource} (fake — no ChromaCloud/network) and
 *       lands them in pgvector; the response echoes both {@code copied} (source
 *       reads) and {@code dest_counts} (pgvector rows).</li>
 *   <li><b>Ephemeral credentials (Pillar 1b).</b> The api key appears in NO log
 *       line, NOT the response body, and NOT any persisted chunk metadata — on
 *       BOTH the happy and the mid-copy-failure paths.</li>
 *   <li><b>Pagination.</b> A &gt;PAGE collection is copied across multiple pages
 *       with a correctly advancing offset.</li>
 *   <li><b>Fail loud.</b> Missing api key or an unknown source collection → 400;
 *       a mid-copy failure → 500 carrying the partial progress.</li>
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

    // 384-dim collections (minilm token) so the fake vectors stay small.
    private static final String COLL_A = "knowledge__migr__minilm-l6-v2-384__v1";
    private static final String COLL_B = "docs__migr__minilm-l6-v2-384__v1";
    private static final String COLL_BIG = "knowledge__migrbig__minilm-l6-v2-384__v1";

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

    @org.junit.jupiter.api.BeforeEach
    void clearChunks() throws SQLException {
        // PER_CLASS shares one DB across tests; isolate each test's parity/count
        // assertions by truncating the chunk tables (superuser bypasses RLS).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute("TRUNCATE nexus.chunks_" + dim);
            }
        }
    }

    @Test
    void ingestCloud_copiesServerSide_reportsParity_andNeverLeaksCredentials() throws Exception {
        var openedWith = new AtomicReference<String[]>();
        FakeCloud fake = new FakeCloud(openedWith);
        fake.put(COLL_A, chash("a", 1), "alpha one");
        fake.put(COLL_A, chash("a", 2), "alpha two");
        fake.put(COLL_B, chash("b", 1), "bravo one");

        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of(COLL_A, COLL_B)));
            handleWithTenant(new MigrationHandler(pgVectors, fake), ex);

            assertThat(ex.status).as("ingest-cloud returns 200").isEqualTo(200);
            @SuppressWarnings("unchecked")
            Map<String, Object> resp = MAPPER.readValue(ex.bodyString(), Map.class);
            assertThat(resp.get("total")).isEqualTo(3);
            @SuppressWarnings("unchecked")
            Map<String, Object> copied = (Map<String, Object>) resp.get("copied");
            assertThat(copied).containsEntry(COLL_A, 2).containsEntry(COLL_B, 1);
            @SuppressWarnings("unchecked")
            Map<String, Object> destCounts = (Map<String, Object>) resp.get("dest_counts");
            assertThat(destCounts).as("parity: dest_counts == copied")
                .containsEntry(COLL_A, 2).containsEntry(COLL_B, 1);

            // Vectors actually landed server-side (client sent none).
            assertThat(superuserCount(384, COLL_A)).isEqualTo(2L);
            assertThat(superuserCount(384, COLL_B)).isEqualTo(1L);

            // The handler used the client's ephemeral creds (in-memory only).
            assertThat(openedWith.get()).containsExactly("src-tenant", "src-db", SECRET_API_KEY);

            // The api key leaked NOWHERE: response, logs, persisted metadata.
            assertNoKeyLeak(ex, logs);
        } finally {
            root.detachAppender(logs);
        }
    }

    @Test
    void ingestCloud_paginatesLargeCollection_withAdvancingOffset() throws Exception {
        int count = MigrationHandler.PAGE + 1;  // 301 → two pages (300 then 1)
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        for (int i = 0; i < count; i++) fake.put(COLL_BIG, chash("big", i), "doc " + i);

        CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of(COLL_BIG)));
        handleWithTenant(new MigrationHandler(pgVectors, fake), ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(superuserCount(384, COLL_BIG))
            .as("all 301 chunks landed across two pages").isEqualTo((long) count);
        assertThat(fake.offsetsFor(COLL_BIG))
            .as("offset advanced by PAGE across pages").containsExactly(0, MigrationHandler.PAGE);
    }

    @Test
    void ingestCloud_midCopyFailure_returns500_withPartialProgress_noLeak() throws Exception {
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        fake.put(COLL_A, chash("fa", 1), "ok one");
        fake.put(COLL_B, chash("fb", 1), "never read");  // present in source ...
        fake.failReadOn(COLL_B);                          // ... but its read throws mid-copy

        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of(COLL_A, COLL_B)));
            handleWithTenant(new MigrationHandler(pgVectors, fake), ex);

            assertThat(ex.status).as("mid-copy failure → 500").isEqualTo(500);
            @SuppressWarnings("unchecked")
            Map<String, Object> resp = MAPPER.readValue(ex.bodyString(), Map.class);
            @SuppressWarnings("unchecked")
            Map<String, Object> partial = (Map<String, Object>) resp.get("copied_before_failure");
            assertThat(partial).as("only the collection that landed before the failure is reported")
                .containsExactly(Map.entry(COLL_A, 1));
            // Even on the failure path the api key must not leak.
            assertNoKeyLeak(ex, logs);
        } finally {
            root.detachAppender(logs);
        }
    }

    @Test
    void ingestCloud_unknownCollection_returns400() throws Exception {
        FakeCloud fake = new FakeCloud(new AtomicReference<>());
        fake.put(COLL_A, chash("u", 1), "present");
        CapturingExchange ex = post(reqBody(SECRET_API_KEY, List.of("knowledge__typo__minilm-l6-v2-384__v1")));
        handleWithTenant(new MigrationHandler(pgVectors, fake), ex);
        assertThat(ex.status).as("a requested collection absent from source → 400").isEqualTo(400);
        assertThat(ex.bodyString()).contains("not present in source");
    }

    @Test
    void ingestCloud_missingApiKey_returns400() throws Exception {
        MigrationHandler handler = new MigrationHandler(pgVectors,
            (t, d, k) -> { throw new AssertionError("source must not be opened on a bad request"); });
        String body = MAPPER.writeValueAsString(Map.of(
            "source_tenant", "src-tenant", "source_database", "src-db"));  // no api key
        CapturingExchange ex = post(body);
        handleWithTenant(handler, ex);
        assertThat(ex.status).isEqualTo(400);
        assertThat(ex.bodyString()).contains("source_api_key");
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /** Collision-free 32-char chash id: 4-char tag field + 28-digit index. */
    private static String chash(String tag, int i) {
        String t = (tag + "xxxx").substring(0, 4);
        return t + String.format("%028d", i);  // exactly 32 chars, unique per i
    }

    private static String reqBody(String apiKey, List<String> collections) throws Exception {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("source_tenant", "src-tenant");
        m.put("source_database", "src-db");
        m.put("source_api_key", apiKey);
        m.put("collections", collections);
        return MAPPER.writeValueAsString(m);
    }

    private static CapturingExchange post(String body) {
        return new CapturingExchange("POST", URI.create("/v1/migration/ingest-cloud"), body);
    }

    private void handleWithTenant(MigrationHandler handler, CapturingExchange ex) throws Exception {
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
    }

    private void assertNoKeyLeak(CapturingExchange ex, ListAppender<ILoggingEvent> logs) throws SQLException {
        assertThat(ex.bodyString()).as("api key not in response body").doesNotContain(SECRET_API_KEY);
        for (ILoggingEvent e : logs.list) {
            assertThat(e.getFormattedMessage()).as("api key not in any log line").doesNotContain(SECRET_API_KEY);
        }
        assertThat(allChunkMetadataText(384))
            .as("api key not persisted in any chunk metadata").doesNotContain(SECRET_API_KEY);
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
             PreparedStatement ps = su.prepareStatement("SELECT metadata::text FROM nexus.chunks_" + dim);
             ResultSet rs = ps.executeQuery()) {
            while (rs.next()) sb.append(rs.getString(1)).append('\n');
        }
        return sb.toString();
    }

    /** Fake in-memory cloud source: records opened creds + read offsets; can fail. */
    private static final class FakeCloud implements MigrationHandler.CloudSourceFactory {
        private final Map<String, List<String>> ids = new LinkedHashMap<>();
        private final Map<String, List<String>> docs = new LinkedHashMap<>();
        private final Map<String, List<float[]>> vecs = new LinkedHashMap<>();
        private final Map<String, List<Integer>> offsets = new LinkedHashMap<>();
        private String failReadOn;
        private final AtomicReference<String[]> openedWith;

        FakeCloud(AtomicReference<String[]> openedWith) { this.openedWith = openedWith; }

        void put(String coll, String id, String doc) {
            float[] v = new float[384];
            v[0] = 1.0f;
            ids.computeIfAbsent(coll, k -> new ArrayList<>()).add(id);
            docs.computeIfAbsent(coll, k -> new ArrayList<>()).add(doc);
            vecs.computeIfAbsent(coll, k -> new ArrayList<>()).add(v);
        }

        void failReadOn(String coll) { this.failReadOn = coll; }

        List<Integer> offsetsFor(String coll) { return offsets.getOrDefault(coll, List.of()); }

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
                    offsets.computeIfAbsent(coll, k -> new ArrayList<>()).add(offset);
                    if (coll.equals(failReadOn)) {
                        // Message intentionally embeds a fake secret-shaped token to
                        // prove the handler does not echo exception messages.
                        throw new RuntimeException("chroma read failed body={token:" + SECRET_API_KEY + "}");
                    }
                    List<String> cids = ids.getOrDefault(coll, List.of());
                    if (offset >= cids.size()) return MigrationHandler.ChunkPage.empty();
                    int end = Math.min(offset + limit, cids.size());
                    List<Map<String, Object>> metas = new ArrayList<>();
                    for (int i = offset; i < end; i++) metas.add(Map.of("k", "v"));
                    return new MigrationHandler.ChunkPage(
                        cids.subList(offset, end), docs.get(coll).subList(offset, end),
                        vecs.get(coll).subList(offset, end), metas);
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
