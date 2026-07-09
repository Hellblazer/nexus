// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.EmbedResult;
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
 * nexus-9y5om — HTTP-boundary contract for {@code requireHomogeneousModel}: a
 * mixed-embedding-model {@code collections} list on the combined-query endpoints must
 * 400, not silently mis-embed (the nexus-3l6gz class). Mirrors the lightweight
 * {@link VectorHandlerTokenUsageTest} harness (Testcontainers PG, full Liquibase
 * changelog, a stub {@link Embedder}, {@code PgVectorRepository} injected via the
 * 5-arg {@link NexusService} overload, port 0, {@code PER_CLASS}).
 *
 * <p>The guard throws before any embed call or DB touch (verified in
 * {@code PgVectorRepository.requireHomogeneousModel} — it is a pure string check over
 * the {@code collections} list), so no chunk seeding is required for these tests: the
 * stub embedder never needs to be exercised for the 400 path.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerCombinedQueryModelGuardTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN  = "tok-model-guard-test-0123456789abcdef00";
    private static final String TENANT = "model-guard-tenant";

    // Both 1024-dim (dim guard passes) but DIFFERENT models (model guard must catch it).
    private static final String COLL_A = "knowledge__mg-a__voyage-context-3__v1";
    private static final String COLL_B = "code__mg-b__voyage-code-3__v1";

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
            ps.setString(3, "model-guard-test");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Stub embedder: the guard throws before this is ever invoked for the 400 tests
        // below, so any 1024-dim stub suffices.
        var embedder = new StubEmbedder(1024);

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
    void searchMetadataScoped_mixedModels_returns400NamingBothModels() throws Exception {
        var resp = post("/v1/vectors/search-metadata-scoped", Map.of(
            "query",       "probe",
            "collections", List.of(COLL_A, COLL_B),
            "n_results",   5));

        assertThat(resp.statusCode())
            .as("mixed-model collections must 400, not silently mis-embed: %s", resp.body())
            .isEqualTo(400);
        assertThat(resp.body())
            .as("teaching message must name both models found")
            .contains("voyage-context-3")
            .contains("voyage-code-3");
    }

    @Test
    void searchGraphHop_mixedModels_returns400NamingBothModels() throws Exception {
        var resp = post("/v1/vectors/search-graph-hop", Map.of(
            "query",       "probe",
            "seeds",       List.of("1.1"),
            "collections", List.of(COLL_A, COLL_B),
            "n_results",   5));

        assertThat(resp.statusCode())
            .as("mixed-model collections must 400, not silently mis-embed: %s", resp.body())
            .isEqualTo(400);
        assertThat(resp.body())
            .as("teaching message must name both models found")
            .contains("voyage-context-3")
            .contains("voyage-code-3");
    }

    /** Minimal fixed-vector stub embedder — the model guard never reaches embed() here. */
    private static final class StubEmbedder implements Embedder {
        private final int dim;

        StubEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public String modelToken() {
            return "voyage-context-3";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new java.util.ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[dim];
                v[0] = 1.0f;
                out.add(v);
            }
            return out;
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), 0L);
        }
    }
}
