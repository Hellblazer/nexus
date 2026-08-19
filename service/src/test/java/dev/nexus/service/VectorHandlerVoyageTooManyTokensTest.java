// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.VoyageTooManyTokensException;
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
 * RDR-195 gate remediation (nexus-kmtlp.11 fix 2, 2026-08-19) — HTTP-boundary contract for
 * {@link VoyageTooManyTokensException}: the substantive-critic (T2
 * {@code substantive-critique-rdr195-phase2-da9c61781-2026-08-19} finding 2) verified that both
 * of this exception's escape paths (single-unsplittable rethrow, adaptive-split exhaustion)
 * reached {@link dev.nexus.service.http.VectorHandler}'s generic {@code catch (Exception e)}
 * arm and came back as the hardcoded, detail-free {@code {"error": "internal server error"}}
 * 500 body — the exact opaque-500 symptom RDR-195 Gap 2 exists to close, reappearing one layer
 * up for these two rare tail paths. This pins the fix: a bespoke arm (mirroring
 * {@code UpstreamAuthException}'s 502 mapping and {@code EmbeddingModelUnavailableException}'s
 * 422 mapping) maps it to <strong>422</strong> — never 500 (that is the opaque outcome being
 * fixed) and never one of the client's {@code _GATEWAY_RETRY_CODES} (502/503/504) — since
 * retrying the identical oversize body is guaranteed to fail again and re-bills tokenization.
 *
 * <p>Mirrors the lightweight {@link VectorHandlerCombinedQueryModelGuardTest} harness
 * (Testcontainers PG, full Liquibase changelog, a stub {@link Embedder} that throws directly,
 * {@code PgVectorRepository} injected via the 5-arg {@link NexusService} overload, port 0,
 * {@code PER_CLASS}) — a fake embedder throwing the typed exception is the smallest fixture
 * that exercises the real {@code VectorHandler.handle} catch chain end-to-end over HTTP.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerVoyageTooManyTokensTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN  = "tok-vtmt-test-0123456789abcdef00000000";
    private static final String TENANT = "vtmt-tenant";
    private static final String COLLECTION = "code__vtmt__voyage-code-3__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
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
            new Liquibase("db/changelog/db.changelog-master.xml",
                          new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
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
            ps.setString(3, "vtmt-test");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Stub embedder: always throws the typed exception, mirroring the two real
        // VoyageEmbedder escape paths (single-unsplittable rethrow, exhaustion) without
        // needing a live/fake Voyage upstream — VectorHandler's catch chain is under test,
        // not VoyageEmbedder's planner (that is VoyageEmbedderBatchSplitTest's job).
        var embedder = new ThrowingEmbedder();
        var pgRepo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        service = new NexusService(0, TOKEN, svcDs, null, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
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
    void upsertChunks_voyageTooManyTokens_maps422WithStructuredBody_neverOpaque500() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", COLLECTION,
            "ids",        List.of(Chash.ofText("vtmt-c1").toHex()),
            "documents",  List.of("pretend-oversized chunk text"),
            "metadatas",  List.of(Map.of())));

        assertThat(resp.statusCode())
            .as("must be 422 (actionable, non-retryable) -- never the generic 500 the critic"
                + " found, and never one of the client's gateway-retry codes 502/503/504"
                + " (got body: %s)", resp.body())
            .isEqualTo(422);

        @SuppressWarnings("unchecked")
        Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
        assertThat(body.get("error")).isEqualTo("TOO_MANY_TOKENS_IN_BATCH");
        assertThat((String) body.get("detail"))
            .as("the upstream/typed detail must reach the caller, not just the engine log")
            .contains("vtmt-simulated-exhaustion");
        assertThat(((Number) body.get("sub_requests")).intValue()).isEqualTo(3);
        assertThat(((Number) body.get("batch_size")).intValue()).isEqualTo(1);
        assertThat(body.get("model")).isEqualTo("voyage-code-3");
    }

    /** Always throws the typed exception, simulating an exhausted adaptive-split budget. */
    private static final class ThrowingEmbedder implements Embedder {
        @Override
        public String modelToken() {
            return "voyage-code-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            throw simulated();
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            throw simulated();
        }

        private static VoyageTooManyTokensException simulated() {
            return new VoyageTooManyTokensException(
                "vtmt-simulated-exhaustion: adaptive-split sub-request budget exhausted",
                "TOO_MANY_TOKENS_IN_BATCH", "voyage-code-3", 1, 3);
        }
    }
}
