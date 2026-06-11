// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.OnnxEmbedder;
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
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-pebfx.2 — HTTP-boundary contract for the embedding-mode refusal.
 *
 * <p>{@code EmbeddingModeFailLoudTest} pins the router-level dispatch; this
 * suite pins the piece visible to Python clients: an unservable model segment
 * surfaces as <strong>HTTP 422</strong> (well-formed request, unservable in
 * this embedding mode), DISTINGUISHABLE from a malformed request's 400. The
 * service here is wired exactly like a key-less production start: onnx-local
 * {@link EmbedderRouter}s through the ROUTER constructor of
 * {@link PgVectorRepository}.
 *
 * <p>Hermetic: Testcontainers PG (auth needs the token table; the refusal
 * itself throws before any SQL), real ONNX embedder, port 0, PER_CLASS.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerEmbeddingModeTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN  = "pebfx2-mode-token-0123456789abcdef00";
    private static final String TENANT = "pebfx2-tenant";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    OnnxEmbedder onnx;
    NexusService service;
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                liquibase.update(new Contexts());
            }
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.setString(3, "pebfx2-mode-test");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Production-shaped key-less wiring: onnx-local routers through the
        // ROUTER constructor (Main.java's no-NX_VOYAGE_API_KEY branch).
        onnx = new OnnxEmbedder();
        var docRouter = new EmbedderRouter(onnx, "document");
        var qryRouter = new EmbedderRouter(onnx, "query");
        var pgRepo = new PgVectorRepository(new TenantScope(svcDs), docRouter, qryRouter);

        service = new NexusService(0, TOKEN, svcDs, null, null, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (onnx    != null) onnx.close();
        if (pg      != null) pg.stop();
    }

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void upsertChunks_voyageCollectionInOnnxMode_is422_withActionableBody() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "knowledge__nexus__voyage-context-3__v1",
            "ids",        List.of("pebfx2-c1"),
            "documents",  List.of("some text"),
            "metadatas",  List.of(Map.of())));
        assertThat(resp.statusCode())
            .as("unservable model must be 422, not 400/500 (got body: %s)", resp.body())
            .isEqualTo(422);
        assertThat(resp.body())
            .contains("error")
            .contains("voyage-context-3")
            .contains("NX_VOYAGE_API_KEY");
    }

    @Test
    void search_voyageCollectionInOnnxMode_is422() throws Exception {
        var resp = post("/v1/vectors/search", Map.of(
            "query",       "anything",
            "collections", List.of("code__nexus__voyage-code-3__v1"),
            "n_results",   3));
        assertThat(resp.statusCode())
            .as("query-embed refusal must also be 422 (got body: %s)", resp.body())
            .isEqualTo(422);
        assertThat(resp.body()).contains("voyage-code-3");
    }

    @Test
    void malformedCollection_staysA400_distinguishableFrom422() throws Exception {
        // Non-conformant name → dimForCollection IllegalArgumentException → 400.
        // The contrast pin: 422 means "configure the service", 400 means "fix
        // the request" — collapsing them re-opens the silent-misconfig trap.
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "not-a-conformant-name",
            "ids",        List.of("x"),
            "documents",  List.of("y"),
            "metadatas",  List.of(Map.of())));
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void versionEndpoint_reportsAppAndSchemaVersions() throws Exception {
        // nexus-pebfx.4 handshake surface. Liquibase ran in @BeforeAll, so the
        // applied journal is populated and grants-002 lets nexus_svc read it.
        // app_version is "unknown" under surefire (pom.properties is a fat-JAR
        // resource, absent from the classes dir) — assert field presence and
        // pin the schema fields strictly.
        var req = java.net.http.HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/version"))
            .GET()
            .build();
        var resp = http.send(req, java.net.http.HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
        assertThat((String) body.get("app_version")).isNotBlank();
        assertThat(body.get("schema_error")).isNull();
        assertThat((String) body.get("schema_latest_id")).isNotBlank();
        assertThat(((Number) body.get("schema_changeset_count")).longValue())
            .isGreaterThan(0);
    }

    @Test
    void minilmCollectionInOnnxMode_stillServes200() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "knowledge__pebfx2__minilm-l6-v2-384__v1",
            "ids",        List.of("pebfx2-ok1"),
            "documents",  List.of("a servable chunk"),
            "metadatas",  List.of(Map.of())));
        assertThat(resp.statusCode())
            .as("refusal must not over-trigger: minilm is servable in onnx-local "
                + "mode (got body: %s)", resp.body())
            .isEqualTo(200);
    }
}
