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
import dev.nexus.service.vectors.UpstreamRateLimitedException;
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
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-99r7y — HTTP-boundary contract for {@link UpstreamRateLimitedException}:
 * sustained Voyage 429s past the embedder's bounded budget must surface as an
 * HONEST {@code 429} with a {@code Retry-After} header, never launder into the
 * generic 500 arm (the 2026-08-15 incident shape: the old fixed retries burned
 * inside the public edge's 30s bound and bulk {@code write_many} 5xx'd while
 * the engine was idle). The header is what the client-side rate brake
 * (conexus-cy9u7) keys on.
 *
 * <p>Mirrors {@link VectorHandlerVoyageTooManyTokensTest}'s harness exactly
 * (Testcontainers PG, full Liquibase changelog, a stub {@link Embedder} that
 * throws the typed exception, port 0, {@code PER_CLASS}) for the live-HTTP
 * proof at the {@code VectorHandler} boundary. {@code CatalogHandler}'s
 * mirror arm (the incident's actual surface) is pinned by a static source
 * scan instead — the {@code CatalogHandlerEnvelopeConformanceGateTest}
 * family's own proportionality argument: a live combined-write fixture
 * (CombinedWriteService + seeded docs) is out of proportion to proving the
 * catch arm exists and carries the same status + header.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerUpstreamRateLimitedTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN  = "tok-url429-test-0123456789abcdef0000000";
    private static final String TENANT = "url429-tenant";
    private static final String COLLECTION = "code__url429__voyage-code-3__v1";

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
            ps.setString(3, "url429-test");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        var embedder = new RateLimitedEmbedder();
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
    void upsertChunks_sustainedRateLimit_maps429WithRetryAfter_neverOpaque500() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", COLLECTION,
            "ids",        List.of(Chash.ofText("url429-c1").toHex()),
            "documents",  List.of("chunk that will hit the simulated RPM ceiling"),
            "metadatas",  List.of(Map.of())));

        assertThat(resp.statusCode())
            .as("must be the honest 429 — never the opaque 500 the 2026-08-15"
                + " incident produced, and never a gateway-retry code (got body: %s)",
                resp.body())
            .isEqualTo(429);
        assertThat(resp.headers().firstValue("Retry-After"))
            .as("the Retry-After header is what the client rate brake keys on")
            .contains("7");

        @SuppressWarnings("unchecked")
        Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
        assertThat((String) body.get("error")).contains("rate limiting");
        assertThat(((Number) body.get("retry_after_seconds")).longValue()).isEqualTo(7L);
    }

    @Test
    void catalogHandlerCarriesTheMirrorArm() throws Exception {
        // Static source pin (see the class javadoc for why not live HTTP):
        // the write_many surface — the measured incident's own path — must
        // catch the typed exception and map the same 429 + Retry-After.
        String src = Files.readString(Path.of(
            "src", "main", "java", "dev", "nexus", "service", "http", "CatalogHandler.java"));
        assertThat(src)
            .as("CatalogHandler must catch UpstreamRateLimitedException")
            .contains("catch (dev.nexus.service.vectors.UpstreamRateLimitedException");
        int armIdx = src.indexOf("catch (dev.nexus.service.vectors.UpstreamRateLimitedException");
        String arm = src.substring(armIdx, Math.min(src.length(), armIdx + 1600));
        assertThat(arm).contains("Retry-After");
        assertThat(arm).contains("429");
        // The arm must sit BEFORE the generic Exception ladder so it cannot
        // be shadowed back into the opaque 500.
        int genericIdx = src.indexOf("catch (Exception e)", armIdx);
        assertThat(genericIdx).isGreaterThan(armIdx);
    }

    /** Always throws the typed exception, simulating a budget-exhausted sustained 429. */
    private static final class RateLimitedEmbedder implements Embedder {
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

        private static UpstreamRateLimitedException simulated() {
            return new UpstreamRateLimitedException(
                "Voyage AI is rate limiting (HTTP 429 x9 within the 20000ms budget);"
                + " failing fast before the edge timeout. Retry after ~7s.", 7L);
        }
    }
}
