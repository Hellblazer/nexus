// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.Bge768Embedder;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-160 P4.3 / bead nexus-kkgnr — LIVE end-to-end bge-768 embed smoke.
 *
 * <p>Boots the real {@link NexusService} (Testcontainers Postgres + Liquibase +
 * a service token) wired with a REAL {@link Bge768Embedder}, then POSTs the
 * committed parity fixture's texts to {@code POST /v1/vectors/embed} and asserts
 * the live HTTP path yields 768-dim vectors that match the Python fastembed
 * reference (min cosine ≥ 0.9999). This closes the composition the unit tests
 * could not: fetch-provisioned model → router dispatch → service → HTTP → 768-dim.
 * (Load+embed+parity is unit-covered by {@code Bge768ParityTest}; routing by
 * {@code EmbedderRouterBge768Test}; the embed route is model-agnostic per
 * {@code VectorHandlerEmbeddingModeTest} — this is their live composition.)
 *
 * <p>{@code @Tag("integration")}: needs Docker (Testcontainers) AND the ~416MB
 * standard bge ONNX. SKIPPED (loud) when the model is absent; CI primes it
 * (service-ci.yml) and a local box provisions it via {@code nx init --service}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Bge768ServiceEmbedIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN  = "kkgnr-bge-embed-token-0123456789abcdef";
    private static final String TENANT = "kkgnr-tenant";
    private static final String BGE_COLLECTION = "knowledge__kkgnr__bge-base-en-v15-768__v1";
    private static final double PARITY_MIN_COSINE = 0.9999;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    Bge768Embedder bge;
    NexusService service;
    HttpClient http;

    List<String> texts;
    List<float[]> referenceVectors;

    @BeforeAll
    void startAll() throws Exception {
        // Resolve the bge model BEFORE spinning up Postgres: skip loud if absent
        // so we don't pay the container cost just to skip.
        String modelPath = System.getProperty("nexus.bge.modelPath", Bge768Embedder.DEFAULT_MODEL_PATH);
        String tokPath   = System.getProperty("nexus.bge.tokenizerPath", Bge768Embedder.DEFAULT_TOKENIZER_PATH);
        Assumptions.assumeTrue(
                Files.isRegularFile(Path.of(modelPath)) && Files.isRegularFile(Path.of(tokPath)),
                "bge-768 model absent — provision via `nx init --service` (RDR-160 P3) or set "
                + "-Dnexus.bge.modelPath / -Dnexus.bge.tokenizerPath");

        loadFixture();

        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "  END IF; "
                + "END $$");
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
            su.createStatement().execute("ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.setString(3, "kkgnr-bge-embed");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        // Production-shaped local wiring: a real bge-768 router (Main.java's
        // no-NX_VOYAGE_API_KEY branch wires exactly this).
        bge = new Bge768Embedder(modelPath, tokPath);
        var docRouter = new EmbedderRouter(bge, "document");
        var qryRouter = new EmbedderRouter(bge, "query");
        var pgRepo = new PgVectorRepository(new TenantScope(svcDs), docRouter, qryRouter);

        service = new NexusService(0, TOKEN, svcDs, null, docRouter, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs   != null) svcDs.close();
        if (bge     != null) bge.close();
        if (pg      != null) pg.stop();
    }

    private void loadFixture() throws Exception {
        try (InputStream in = getClass().getResourceAsStream("/bge768/parity_reference.json")) {
            assertThat(in).as("committed parity fixture on classpath").isNotNull();
            JsonNode root = MAPPER.readTree(in);
            texts = new ArrayList<>();
            for (JsonNode t : root.get("texts")) texts.add(t.asText());
            referenceVectors = new ArrayList<>();
            for (JsonNode vec : root.get("vectors")) {
                float[] v = new float[vec.size()];
                for (int d = 0; d < vec.size(); d++) v[d] = (float) vec.get(d).asDouble();
                referenceVectors.add(v);
            }
        }
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
    void liveEmbed_yields768DimVectorsMatchingFastembed() throws Exception {
        var resp = post("/v1/vectors/embed", Map.of("collection", BGE_COLLECTION, "texts", texts));
        assertThat(resp.statusCode()).as("body=%s", resp.body()).isEqualTo(200);

        JsonNode embeddings = MAPPER.readTree(resp.body()).get("embeddings");
        assertThat(embeddings).isNotNull();
        assertThat(embeddings.size()).isEqualTo(texts.size());

        double minCosine = 1.0;
        for (int i = 0; i < texts.size(); i++) {
            JsonNode row = embeddings.get(i);
            assertThat(row.size()).as("embedding[%d] dim", i).isEqualTo(768);

            float[] ref = referenceVectors.get(i);
            double dot = 0.0, nG = 0.0, nR = 0.0;
            for (int d = 0; d < 768; d++) {
                double g = row.get(d).asDouble();
                dot += g * ref[d];
                nG  += g * g;
                nR  += (double) ref[d] * ref[d];
            }
            double cosine = dot / (Math.sqrt(nG) * Math.sqrt(nR));
            assertThat(cosine)
                    .as("live /v1/vectors/embed bge-768 parity vs fastembed for text[%d]", i)
                    .isGreaterThanOrEqualTo(PARITY_MIN_COSINE);
            minCosine = Math.min(minCosine, cosine);
        }
        assertThat(minCosine)
                .as("min cosine across fixture through the live service")
                .isGreaterThanOrEqualTo(PARITY_MIN_COSINE);
    }

    @Test
    void minilmCollection_refused_inServiceEmbed() throws Exception {
        // No silent fallback at the live HTTP boundary either: a non-bge model
        // segment on the bge-only service must refuse, not degrade.
        var resp = post("/v1/vectors/embed",
                Map.of("collection", "knowledge__kkgnr__minilm-l6-v2-384__v1", "texts", List.of("x")));
        assertThat(resp.statusCode()).as("body=%s", resp.body()).isEqualTo(422);
    }
}
