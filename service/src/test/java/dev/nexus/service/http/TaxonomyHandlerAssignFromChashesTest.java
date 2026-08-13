// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
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
import java.sql.PreparedStatement;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-lns3o (engine half) — HTTP layer test for
 * {@code POST /v1/taxonomy/assignments/assign_from_chashes}.
 *
 * <p>Route/JSON-contract coverage only; the compute+persist parity contract is
 * pinned at the repository layer by {@code TaxonomyAssignFromChashesRepositoryTest}
 * (which documents the client-semantics derivation this route replaces).
 * Hermetic Testcontainers PG, driven directly via {@link TaxonomyHandler#handle}
 * with a capturing {@link HttpExchange} (same idiom as
 * {@code TaxonomyHandlerAssignFkTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TaxonomyHandlerAssignFromChashesTest {

    private static final String SVC_ROLE = "svc_afc_http_test";
    private static final String SVC_PASS = "svc_afc_http_test_pass";
    private static final String TENANT   = "afc-http-tenant";
    private static final String COL      = "code__afchttp__voyage-code-3__v1";
    private static final int DIM = 1024;

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    TaxonomyRepository repo;
    TaxonomyHandler handler;
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
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            // RDR-191 unify (vectors-004/taxonomy-007): chunks_<dim> and
            // taxonomy_centroids_<dim> collapse into ONE unified table each --
            // one grant apiece, not a dim loop. assign_from_chashes_<dim> stays
            // THREE separate typed functions (DECISION 2, T2 [22445]), so its
            // EXECUTE grant keeps the loop.
            su.createStatement().execute("GRANT SELECT ON nexus.chunks TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT ON nexus.taxonomy_centroids TO " + SVC_ROLE);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT EXECUTE ON FUNCTION nexus.assign_from_chashes_" + dim
                    + "(text, text[], boolean) TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.topics TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT, UPDATE ON SEQUENCE nexus.topics_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.topic_assignments TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);
        handler = new TaxonomyHandler(repo, null);  // centroid repo unused by this route
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @Test
    void assignFromChashes_defaultCrossCollectionTrue_returnsCountsAndUnmatched() throws Exception {
        String matched   = hexChash("afc-http-matched");
        String unmatched = hexChash("afc-http-unmatched");
        seedChunk(matched, unit(1.0f, 0.0f));
        seedCentroid(COL, seedTopic(COL, "http-topic-1"), unit(1.0f, 0.0f));

        CapturingExchange ex = post("/v1/taxonomy/assignments/assign_from_chashes",
            "{\"collection\":\"" + COL + "\",\"chashes\":[\"" + matched + "\",\"" + unmatched + "\"]}");
        handleWithTenant(ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"assigned\":1");
        assertThat(ex.bodyString()).contains("\"unmatched_chashes\":[\"" + unmatched + "\"]");
        // cross_collection defaulted to true — the field must be present (JsonInclude.ALWAYS)
        // even though there is no foreign centroid seeded (cross_assigned:0).
        assertThat(ex.bodyString()).contains("\"cross_assigned\":0");
    }

    @Test
    void assignFromChashes_crossCollectionFalse_skipsCrossPass() throws Exception {
        String chash = hexChash("afc-http-owncross-off");
        seedChunk(chash, unit(0.0f, 1.0f));
        seedCentroid(COL, seedTopic(COL, "http-topic-2"), unit(0.0f, 1.0f));

        CapturingExchange ex = post("/v1/taxonomy/assignments/assign_from_chashes",
            "{\"collection\":\"" + COL + "\",\"chashes\":[\"" + chash + "\"],\"cross_collection\":false}");
        handleWithTenant(ex);

        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.bodyString()).contains("\"assigned\":1");
        assertThat(ex.bodyString()).contains("\"cross_assigned\":0");
    }

    @Test
    void assignFromChashes_missingCollection_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign_from_chashes",
            "{\"chashes\":[\"x\"]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void assignFromChashes_emptyChashesArray_returns400() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign_from_chashes",
            "{\"collection\":\"" + COL + "\",\"chashes\":[]}");
        handleWithTenant(ex);
        assertThat(ex.status).isEqualTo(400);
    }

    @Test
    void assignFromChashes_nonConformantCollection_returns400NotServerError() throws Exception {
        CapturingExchange ex = post("/v1/taxonomy/assignments/assign_from_chashes",
            "{\"collection\":\"not-conformant\",\"chashes\":[\"x\"]}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("dimForCollection's IllegalArgumentException maps to 400 via the handler's"
                + " generic IllegalArgumentException catch, not a 500")
            .isEqualTo(400);
    }

    @Test
    void assignFromChashes_tooManyChashes_returns400() throws Exception {
        // substantive-critic SIGNIFICANT: MAX_ASSIGN_FROM_CHASHES had zero HTTP-layer
        // coverage — the handler's own size check (mirroring the repository's) must
        // reject one past the limit as a 400, not a 500 or a silently-truncated 200.
        StringBuilder chashesJson = new StringBuilder("[");
        for (int i = 0; i <= TaxonomyRepository.MAX_ASSIGN_FROM_CHASHES; i++) {
            if (i > 0) chashesJson.append(',');
            chashesJson.append('"').append(hexChash("afc-http-cap-" + i)).append('"');
        }
        chashesJson.append(']');

        CapturingExchange ex = post("/v1/taxonomy/assignments/assign_from_chashes",
            "{\"collection\":\"" + COL + "\",\"chashes\":" + chashesJson + "}");
        handleWithTenant(ex);
        assertThat(ex.status)
            .as("MAX_ASSIGN_FROM_CHASHES + 1 chashes must be rejected 400 at the HTTP layer")
            .isEqualTo(400);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────

    private static float[] unit(float x, float y) {
        float[] v = new float[DIM];
        v[0] = x;
        v[1] = y;
        return v;
    }

    /** Deterministic 64-lowercase-hex chash from a readable seed — chunks_&lt;dim&gt;.chash is
     * bytea (RDR-180); every chash value used here, including deliberately-never-upserted
     * ones, must be syntactically valid hex. */
    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private void seedChunk(String hexChashValue, float[] emb) throws Exception {
        registerCollection(COL);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.chunks"
                    + " (tenant_id, collection, chash, chunk_text, embedding_" + DIM + ")"
                    + " VALUES (?, ?, decode(?, 'hex'), ?, ?::vector)")) {
                ps.setString(1, TENANT);
                ps.setString(2, COL);
                ps.setString(3, hexChashValue);
                ps.setString(4, "seed text " + hexChashValue);
                ps.setString(5, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
    }

    /** Pre-register the fixture collection before seeding chunk rows (idempotent).
     * The unified nexus.chunks ships with ZERO collection-FK enforcement until
     * RDR-191 Phase 5 (vectors-004-unify-chunks.xml header) — belt-and-suspenders,
     * not FK-required, kept for parity with the SVC_ROLE catalog_collections grant. */
    private void registerCollection(String collection) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?)"
                    + " ON CONFLICT (tenant_id, name) DO NOTHING")) {
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.executeUpdate();
            }
        }
    }

    /** topic_assignments.topic_id has a real FK to topics(id) — a centroid's topic_id must
     * name an actual row in nexus.topics. Returns the generated id. */
    private long seedTopic(String collection, String label) {
        return repo.insertTopic(TENANT, label, null, collection, 0, "2026-01-01T00:00:00Z", null);
    }

    private void seedCentroid(String collection, long topicId, float[] emb) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            try (PreparedStatement ps = su.prepareStatement(
                    "INSERT INTO nexus.taxonomy_centroids"
                    + " (tenant_id, collection, topic_id, embedding_" + DIM + ") VALUES (?, ?, ?, ?::vector)")) {
                ps.setString(1, TENANT);
                ps.setString(2, collection);
                ps.setLong(3, topicId);
                ps.setString(4, vectorLiteral(emb));
                ps.executeUpdate();
            }
        }
    }

    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }

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
