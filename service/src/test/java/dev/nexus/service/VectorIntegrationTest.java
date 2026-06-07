// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.vectors.ChromaQuotaValidator;
import dev.nexus.service.vectors.ChromaRestClient;
import dev.nexus.service.vectors.LocalChromaServer;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.VectorRepository;
import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-152 bead nexus-gmiaf.20 — hermetic Seam B integration tests.
 *
 * <p>All tests use:
 * <ul>
 *   <li>Embedded Postgres (io.zonky:embedded-postgres)</li>
 *   <li>Local ChromaDB child process ({@code chroma run}) on a free port</li>
 *   <li>Local ONNX embedder (all-MiniLM-L6-v2 from chromadb cache)</li>
 *   <li>Port 0 for the NexusService HTTP server</li>
 * </ul>
 *
 * <p>Requires:
 * <ul>
 *   <li>{@code chroma} CLI on PATH (or NX_CHROMA_BINARY set)</li>
 *   <li>ONNX model files at {@code ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/}</li>
 * </ul>
 *
 * <p>Marked {@code @Tag("integration")} — run via {@code mvn -P integration test} or
 * {@code mvn test -Dgroups=integration}.
 *
 * <p>Test order is independent; the shared class-lifecycle starts/stops Chroma once per class.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorIntegrationTest {

    private static final String TOKEN       = "vector-integration-token-secret";
    private static final String TENANT      = "test-tenant";
    private static final String COLLECTION  = "knowledge__nexus-test__all-minilm-l6-v2__v1";

    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};
    private static final ObjectMapper MAPPER = new ObjectMapper();

    EmbeddedPostgres pg;
    LocalChromaServer localChroma;
    OnnxEmbedder      onnxEmbedder;
    VectorRepository  vectorRepo;
    NexusService      service;
    HttpClient        http;

    @BeforeAll
    void startAll() throws Exception {
        // Embedded Postgres — needed only for the service's DB-backed endpoints
        pg = EmbeddedPostgres.builder().start();
        var pgDs = pg.getPostgresDatabase();

        // Bootstrap minimal schema + tables the service expects
        bootstrapSchema(pgDs);

        var svcDs = buildSvcDs(pg);

        // Local Chroma: find binary, create temp data dir, start child
        String chromaBinary = LocalChromaServer.findChromaBinary();
        Path chromaData = Files.createTempDirectory("vector-it-chroma-");
        int chromaPort = LocalChromaServer.findFreePort();

        localChroma = new LocalChromaServer(chromaBinary, chromaData.toString(), chromaPort);
        localChroma.start();

        // Embedder + Chroma client → repository
        onnxEmbedder = new OnnxEmbedder();
        ChromaRestClient chromaClient = ChromaRestClient.local("127.0.0.1", chromaPort);
        vectorRepo = new VectorRepository(onnxEmbedder, onnxEmbedder, chromaClient);

        // NexusService on ephemeral port with vector backend
        service = new NexusService(0, TOKEN, svcDs, vectorRepo);
        service.start();

        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service     != null) service.stop();
        if (onnxEmbedder != null) onnxEmbedder.close();
        if (localChroma != null) localChroma.stop();
        if (pg          != null) pg.close();
    }

    // ── Test 1: upsert-chunks → search returns them ──────────────────────────

    @Test
    void upsertChunks_thenSearch_returnsResults() throws Exception {
        // Three chunks about different topics
        var body = Map.of(
                "collection", COLLECTION,
                "ids",        List.of("chunk-aa", "chunk-bb", "chunk-cc"),
                "documents",  List.of(
                        "The sky is blue and birds fly through it.",
                        "Deep neural networks learn representations from data.",
                        "Postgres is a relational database with MVCC."),
                "metadatas",  List.of(
                        Map.of("topic", "nature"),
                        Map.of("topic", "ml"),
                        Map.of("topic", "database"))
        );

        var upsertResp = post("/v1/vectors/upsert-chunks", body);
        assertThat(upsertResp.statusCode()).as("upsert 200").isEqualTo(200);
        var upsertJson = MAPPER.readValue(upsertResp.body(), MAP_TYPE);
        assertThat(((Number) upsertJson.get("upserted")).intValue()).isEqualTo(3);

        // Search for the ML chunk
        var searchBody = Map.of(
                "query",       "neural network machine learning",
                "collections", List.of(COLLECTION),
                "n_results",   3
        );
        var searchResp = post("/v1/vectors/search", searchBody);
        assertThat(searchResp.statusCode()).as("search 200").isEqualTo(200);

        @SuppressWarnings("unchecked")
        var results = MAPPER.readValue(searchResp.body(), List.class);
        assertThat(results).isNotEmpty();
        // The ML chunk should be the closest result
        @SuppressWarnings("unchecked")
        Map<String, Object> top = (Map<String, Object>) results.get(0);
        assertThat(top.get("id")).isEqualTo("chunk-bb");
    }

    // ── Test 2: ONNX embed determinism (bit-exact) + Chroma round-trip parity ──
    //
    // Part A — BIT-EXACT VECTOR DETERMINISM (S0.2 self-consistency check):
    //   Embed the same text twice directly via onnxEmbedder; assert Arrays.equals.
    //   This is the genuine "no-tolerance" proof: the local ONNX pipeline is
    //   deterministic for the same input. The .21 harness extends this to
    //   Java-vs-Python cross-language exact-cosine-1.0 parity.
    //
    // Part B — CHROMA ROUND-TRIP DISTANCE:
    //   Upsert one chunk, search with identical text; assert abs(dist) < 1e-6.
    //   Chroma computes cosine distance in float32 internally; an identical stored
    //   vector yields ~1e-7 (NOT bit-exact 0.0) due to float32 arithmetic.
    //   A real embedding drift (e.g. the Voyage truncation=false bug gives
    //   sim ≈ 0.99995 → dist ≈ 5e-5) is still caught: 5e-5 >> 1e-6.

    @Test
    void onnxEmbedParity_bitExactDeterminism_andChromaRoundTrip() throws Exception {
        String text = "Semantic search connects questions to answers through meaning.";
        String chunkId = "parity-chunk-01";

        // ── Part A: bit-exact vector determinism ───────────────────────────────
        float[] v1 = onnxEmbedder.embedOne(text);
        float[] v2 = onnxEmbedder.embedOne(text);
        assertThat(Arrays.equals(v1, v2))
                .as("OnnxEmbedder must be bit-exactly deterministic for the same input text")
                .isTrue();

        // ── Part B: Chroma round-trip distance < 1e-6 ─────────────────────────
        // Upsert one chunk via service endpoint (server-side ONNX embed)
        var upsertBody = Map.of(
                "collection", COLLECTION + "-parity",
                "ids",        List.of(chunkId),
                "documents",  List.of(text),
                "metadatas",  List.of(Map.of("test", "parity"))
        );
        var upsertResp = post("/v1/vectors/upsert-chunks", upsertBody);
        assertThat(upsertResp.statusCode()).isEqualTo(200);

        // Search with the exact same text
        var searchBody = Map.of(
                "query",       text,
                "collections", List.of(COLLECTION + "-parity"),
                "n_results",   1
        );
        var searchResp = post("/v1/vectors/search", searchBody);
        assertThat(searchResp.statusCode()).isEqualTo(200);

        @SuppressWarnings("unchecked")
        var results = MAPPER.readValue(searchResp.body(), List.class);
        assertThat(results).hasSize(1);

        @SuppressWarnings("unchecked")
        Map<String, Object> result = (Map<String, Object>) results.get(0);
        assertThat(result.get("id")).isEqualTo(chunkId);

        // Chroma computes cosine distance in float32; an identical vector yields
        // ~1e-7 (not bit-exact 0.0). Threshold 1e-6 is tight enough to catch
        // real embedding drift (Voyage truncation bug: dist ≈ 5e-5 >> 1e-6).
        double dist = ((Number) result.get("distance")).doubleValue();
        assertThat(Math.abs(dist))
                .as("Chroma cosine distance for identical text must be < 1e-6 (float32 epsilon is ~1e-7; real drift is ≥ 5e-5)")
                .isLessThan(1e-6);
    }

    // ── Test 3: quota enforcement (oversized document rejected) ──────────────

    @Test
    void quotaViolation_oversizedDocument_returns413() throws Exception {
        // Document exceeding MAX_DOCUMENT_BYTES (16384 bytes)
        String oversized = "x".repeat(ChromaQuotaValidator.MAX_DOCUMENT_BYTES + 1);

        var body = Map.of(
                "collection", COLLECTION,
                "ids",        List.of("oversized-id"),
                "documents",  List.of(oversized),
                "metadatas",  List.of(Map.of())
        );

        var resp = post("/v1/vectors/upsert-chunks", body);
        assertThat(resp.statusCode()).as("oversized document → 413").isEqualTo(413);

        var json = MAPPER.readValue(resp.body(), MAP_TYPE);
        assertThat(json.get("error")).isEqualTo("quota_violation");
        assertThat(json.get("field").toString()).contains("document");
    }

    // ── Test 4: store-put → store-get round trip ──────────────────────────────

    @Test
    void storePut_thenStoreGet_roundTrips() throws Exception {
        String docId = "store-put-doc-01";
        String content = "Storing content in the knowledge base for later retrieval.";

        var putBody = Map.of(
                "collection", COLLECTION + "-store",
                "doc_id",     docId,
                "content",    content,
                "metadata",   Map.of("source", "test", "version", "1")
        );
        var putResp = post("/v1/vectors/store-put", putBody);
        assertThat(putResp.statusCode()).as("store-put 200").isEqualTo(200);

        var putJson = MAPPER.readValue(putResp.body(), MAP_TYPE);
        assertThat(putJson.get("id")).isEqualTo(docId);

        // Retrieve it back
        var getBody = Map.of(
                "collection", COLLECTION + "-store",
                "ids",        List.of(docId)
        );
        var getResp = post("/v1/vectors/store-get", getBody);
        assertThat(getResp.statusCode()).as("store-get 200").isEqualTo(200);

        @SuppressWarnings("unchecked")
        Map<String, Object> getJson = MAPPER.readValue(getResp.body(), MAP_TYPE);
        @SuppressWarnings("unchecked")
        List<String> returnedIds = (List<String>) getJson.get("ids");
        assertThat(returnedIds).contains(docId);
    }

    // ── Test 5: clean shutdown kills Chroma child (no orphan) ─────────────────
    //
    // This test is implicitly verified by the @AfterAll stopAll() method.
    // After stopAll(), localChroma.isAlive() must be false.
    // We explicitly test it here with a separate server instance.

    @Test
    void localChromaShutdown_noOrphan() throws Exception {
        // Create a second LocalChromaServer on a separate port and data dir
        String chromaBinary = LocalChromaServer.findChromaBinary();
        Path tempData = Files.createTempDirectory("chroma-orphan-test-");
        int tempPort = LocalChromaServer.findFreePort();

        LocalChromaServer temp = new LocalChromaServer(chromaBinary, tempData.toString(), tempPort);
        temp.start();
        assertThat(temp.isAlive()).as("chroma process alive after start").isTrue();

        temp.stop();
        assertThat(temp.isAlive()).as("chroma process dead after stop").isFalse();

        // Cleanup temp dir
        try {
            Files.walk(tempData)
                    .sorted(java.util.Comparator.reverseOrder())
                    .map(Path::toFile)
                    .forEach(java.io.File::delete);
        } catch (IOException ignored) {}
    }

    // ── Test 6: /v1/vectors/collections endpoint ──────────────────────────────

    @Test
    void listCollections_afterUpsert_containsCollection() throws Exception {
        String colName = COLLECTION + "-listtest";
        var upsertBody = Map.of(
                "collection", colName,
                "ids",        List.of("list-test-id"),
                "documents",  List.of("Testing collection listing."),
                "metadatas",  List.of(Map.of())
        );
        var upsertResp = post("/v1/vectors/upsert-chunks", upsertBody);
        assertThat(upsertResp.statusCode()).isEqualTo(200);

        var colsResp = http.send(
                HttpRequest.newBuilder()
                        .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/vectors/collections"))
                        .header("Authorization", "Bearer " + TOKEN)
                        .header("X-Nexus-Tenant", TENANT)
                        .GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(colsResp.statusCode()).isEqualTo(200);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> collections = MAPPER.readValue(colsResp.body(),
                new TypeReference<List<Map<String, Object>>>() {});
        assertThat(collections).anyMatch(c -> colName.equals(c.get("name")));
    }

    // ── Test 7: store-delete removes the chunk ───────────────────────────────

    @Test
    void storeDelete_removesChunk() throws Exception {
        String colName = COLLECTION + "-delete";
        String docId   = "delete-me-01";

        // Insert
        post("/v1/vectors/upsert-chunks", Map.of(
                "collection", colName,
                "ids",        List.of(docId),
                "documents",  List.of("To be deleted."),
                "metadatas",  List.of(Map.of())
        ));

        // Delete
        var delResp = post("/v1/vectors/store-delete", Map.of(
                "collection", colName,
                "ids",        List.of(docId)
        ));
        assertThat(delResp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        Map<String, Object> delJson = MAPPER.readValue(delResp.body(), MAP_TYPE);
        assertThat(((Number) delJson.get("deleted")).intValue()).isGreaterThanOrEqualTo(0);

        // Search should return nothing for that ID
        var countResp = http.send(
                HttpRequest.newBuilder()
                        .uri(URI.create("http://127.0.0.1:" + service.getPort()
                                + "/v1/vectors/count?collection=" + colName))
                        .header("Authorization", "Bearer " + TOKEN)
                        .header("X-Nexus-Tenant", TENANT)
                        .GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertThat(countResp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        Map<String, Object> countJson = MAPPER.readValue(countResp.body(), MAP_TYPE);
        assertThat(((Number) countJson.get("count")).intValue()).isEqualTo(0);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String path, Object bodyObj) throws Exception {
        String json = MAPPER.writeValueAsString(bodyObj);
        var req = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    // Minimal schema for the service (just what NexusService/TenantScope needs)
    private void bootstrapSchema(javax.sql.DataSource superDs) throws Exception {
        try (var conn = superDs.getConnection()) {
            conn.setAutoCommit(true);

            // Service role: plain LOGIN
            conn.createStatement().execute(
                    "DO $$ BEGIN " +
                    "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='svc_test') THEN " +
                    "    CREATE ROLE svc_test LOGIN PASSWORD 'svc_test_pass'; " +
                    "  END IF; " +
                    "END $$");

            // Minimal nexus schema for TenantScope to work
            conn.createStatement().execute("CREATE SCHEMA IF NOT EXISTS nexus");

            // Grant usage
            conn.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO svc_test");
            conn.createStatement().execute("GRANT USAGE ON SCHEMA public TO svc_test");
        }
    }

    private com.zaxxer.hikari.HikariDataSource buildSvcDs(EmbeddedPostgres pg) {
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl("postgres", "postgres"));
        cfg.setUsername("postgres");
        cfg.setPassword("");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        return new com.zaxxer.hikari.HikariDataSource(cfg);
    }
}
