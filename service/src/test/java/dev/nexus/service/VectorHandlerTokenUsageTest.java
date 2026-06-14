// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.http.VectorHandler;
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
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-ehc4q — HTTP-boundary contract for {@code X-Nexus-Usage-Tokens} header.
 *
 * <p>Pins the following invariants:
 * <ol>
 *   <li>An embedding endpoint (e.g. {@code /v1/vectors/search}) sets
 *       {@code X-Nexus-Usage-Tokens: 73} when the stub embedder reports 73 tokens.</li>
 *   <li>A non-embedding endpoint ({@code /v1/vectors/store-list}) NEVER sets the header
 *       — it does not embed and must not emit a stale value.</li>
 *   <li>When the embedder returns {@code tokens=0} (no usage data), the header is ABSENT
 *       (a zero is not emitted — it is meaningless to the edge proxy).</li>
 * </ol>
 *
 * <p>Wiring: {@link TokenReportingEmbedder} overrides
 * {@link Embedder#embedWithUsage} to return the configured token count.
 * The rest of the setup mirrors {@link VectorHybridHttpTest}: Testcontainers
 * pgvector, Liquibase schema, port 0, PER_CLASS.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerTokenUsageTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN  = "tok-usage-test-0123456789abcdef0000";
    private static final String TENANT = "tok-usage-tenant";

    /** Collection with ONNX (minilm) model — no voyage credentials needed. */
    private static final String COL = "knowledge__usage__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;

    /** Fixed token count the stub embedder reports — chosen to be distinctive. */
    static final long STUB_TOKENS = 73L;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // nexus_svc role (grants changeset is fail-loud without it).
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
        // Register the service token used throughout this suite.
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.setString(3, "token-usage-test");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Stub embedder that returns STUB_TOKENS from embedWithUsage.
        // 384-dim to match the minilm collection; also registered as the query embedder.
        var embedder = new TokenReportingEmbedder(384, STUB_TOKENS);
        embedder.register("hello world");

        var pgRepo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        // Seed one chunk so /search returns a non-empty result (header is set regardless
        // of result count, but seeding gives a deterministic happy-path).
        pgRepo.upsertChunks(TENANT, COL,
            List.of("tokc1000000000000000000000000000"),
            List.of("hello world"),
            List.of(Map.of()));

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

    // ── Helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .GET()
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    // ── Tests ─────────────────────────────────────────────────────────────────

    /**
     * (a) Header present and correct on an embedding endpoint.
     * (e) Header NAME matches {@link VectorHandler#USAGE_TOKENS_HEADER}.
     *
     * <p>/search embeds the query; the stub reports STUB_TOKENS = 73.
     * Pins: header name is the constant, value is a decimal integer string = "73".
     */
    @Test
    void search_emitsTokenUsageHeader_whenEmbedderReportsTokens() throws Exception {
        var resp = post("/v1/vectors/search", Map.of(
            "query",       "hello world",
            "collections", List.of(COL),
            "n_results",   5));

        assertThat(resp.statusCode())
            .as("search should return 200 (got: %s)", resp.body())
            .isEqualTo(200);

        // (e) header name matches the constant — a typo in the constant would fail here
        assertThat(resp.headers().firstValue(VectorHandler.USAGE_TOKENS_HEADER))
            .as("header name must match VectorHandler.USAGE_TOKENS_HEADER = '%s'",
                VectorHandler.USAGE_TOKENS_HEADER)
            .isPresent();

        // value is a decimal integer string (not "73.0", not "73tokens", not blank)
        String headerValue = resp.headers().firstValue(VectorHandler.USAGE_TOKENS_HEADER).get();
        assertThat(headerValue)
            .as("header value must be a decimal integer string")
            .matches("\\d+");
        assertThat(Long.parseLong(headerValue))
            .as("header value must equal the stub token count")
            .isEqualTo(STUB_TOKENS);
    }

    /**
     * (b) Header ABSENT on a non-embedding endpoint.
     *
     * <p>/store-list does NOT call embedQuery — there is no embedding path to set
     * a token count. The non-embedding handler structurally never calls emitTokenUsage.
     * With return-value threading there is no ThreadLocal side-channel to stale-read.
     */
    @Test
    void storeList_doesNotEmitTokenUsageHeader() throws Exception {
        var resp = post("/v1/vectors/store-list", Map.of(
            "collection", COL,
            "limit",      5,
            "offset",     0));

        assertThat(resp.statusCode())
            .as("store-list should return 200 (got: %s)", resp.body())
            .isEqualTo(200);
        assertThat(resp.headers().firstValue(VectorHandler.USAGE_TOKENS_HEADER))
            .as("non-embedding endpoint must NEVER emit %s", VectorHandler.USAGE_TOKENS_HEADER)
            .isEmpty();
    }

    /**
     * (c) Header ABSENT when the embedder reports 0 tokens.
     *
     * <p>Covers the ONNX local-mode case: ONNX embedders return {@code tokens=0}
     * because they have no API cost. Emitting "X-Nexus-Usage-Tokens: 0" is meaningless
     * and must be suppressed (the proxy treats absent == no billing event to record).
     */
    @Test
    void search_withZeroTokens_headerIsAbsent() throws Exception {
        // Wire a fresh service with a zero-token embedder (models ONNX local-mode behaviour).
        var zeroEmbedder = new TokenReportingEmbedder(384, 0L);
        zeroEmbedder.register("hello world");
        var zeroRepo = new PgVectorRepository(new TenantScope(svcDs), zeroEmbedder, zeroEmbedder);
        zeroRepo.upsertChunks(TENANT, COL,
            List.of("tokz1000000000000000000000000000"),
            List.of("hello world"),
            List.of(Map.of()));

        NexusService zeroService = new NexusService(0, TOKEN, svcDs, null, zeroRepo);
        zeroService.start();
        try {
            var req = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + zeroService.getPort() + "/v1/vectors/search"))
                .header("Authorization", "Bearer " + TOKEN)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(Map.of(
                    "query",       "hello world",
                    "collections", List.of(COL),
                    "n_results",   1))))
                .build();
            var resp = http.send(req, HttpResponse.BodyHandlers.ofString());

            assertThat(resp.statusCode()).isEqualTo(200);
            assertThat(resp.headers().firstValue(VectorHandler.USAGE_TOKENS_HEADER))
                .as("zero-token embedder (ONNX local-mode): header must be absent, not '0'")
                .isEmpty();
        } finally {
            zeroService.stop();
        }
    }

    /**
     * (d) Header ABSENT on error response.
     *
     * <p>A /store-list request missing 'collection' triggers IllegalArgumentException
     * → 400. The error path never reaches the embedding call so no token count is
     * available to emit.
     */
    @Test
    void errorResponse_doesNotEmitTokenUsageHeader() throws Exception {
        // Missing 'collection' triggers IllegalArgumentException → 400.
        var resp = post("/v1/vectors/store-list", Map.of(
            "limit",  5,
            "offset", 0));
        assertThat(resp.headers().firstValue(VectorHandler.USAGE_TOKENS_HEADER))
            .as("error response must not carry %s", VectorHandler.USAGE_TOKENS_HEADER)
            .isEmpty();
    }

    // ── Stub embedder ─────────────────────────────────────────────────────────

    /**
     * Deterministic embedder that returns a configurable token count from
     * {@link #embedWithUsage}, enabling unit-level assertion of the header value
     * without Voyage API credentials.
     *
     * <p>Produces fixed unit vectors (identical to {@link PgVectorRepositoryContractTest.FakeEmbedder}
     * for known texts, 1-hot for unknown). The model token is {@code "minilm-l6-v2-384"}
     * so it routes to the {@code chunks_384} table without Voyage credentials.
     */
    static final class TokenReportingEmbedder implements Embedder {

        private final int dim;
        private final long tokenCount;
        private final Map<String, float[]> registered = new HashMap<>();

        TokenReportingEmbedder(int dim, long tokenCount) {
            this.dim        = dim;
            this.tokenCount = tokenCount;
        }

        void register(String text) {
            float[] v = new float[dim];
            v[0] = 1.0f;  // unit vector along first axis
            registered.put(text, v);
        }

        @Override
        public String modelToken() {
            // minilm-l6-v2-384: dispatches to chunks_384; no Voyage credentials needed.
            return "minilm-l6-v2-384";
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = registered.getOrDefault(t, defaultVec());
                out.add(java.util.Arrays.copyOf(v, v.length));
            }
            return out;
        }

        /**
         * Returns the configured {@link #tokenCount} so the handler can emit
         * {@code X-Nexus-Usage-Tokens}.
         */
        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(embed(texts), tokenCount);
        }

        private float[] defaultVec() {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return v;
        }
    }
}
