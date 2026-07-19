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
 * service here is wired with the MiniLM {@link OnnxEmbedder} to pin the
 * 422-refusal + {@code /version} handshake MECHANISM, which is model-agnostic.
 * NOTE: production local mode now wires bge-768 (still {@code modeName
 * "onnx-local"}, but {@code availableModels ["bge-base-en-v15-768"]}) per
 * RDR-160 — that production shape is asserted in {@code EmbedderRouterBge768Test};
 * this harness stays MiniLM-wired only to avoid loading the 416MB bge ONNX in
 * the PG container.
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

        // MiniLM-wired harness (mechanism only; production local mode wires
        // bge-768 per RDR-160 — see EmbedderRouterBge768Test). The 422-refusal
        // and /version handshake under test are model-agnostic.
        onnx = new OnnxEmbedder();
        var docRouter = new EmbedderRouter(onnx, "document");
        var qryRouter = new EmbedderRouter(onnx, "query");
        var pgRepo = new PgVectorRepository(new TenantScope(svcDs), docRouter, qryRouter);

        // Pass the doc router so /version reports the embedding mode
        // (nexus-pebfx.5) — same wiring shape as Main.java.
        service = new NexusService(0, TOKEN, svcDs, null, docRouter, pgRepo);
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

    // ── /v1/chash boundary cases (nexus-e0hd2) ───────────────────────────────
    // Housed here pragmatically: this is the one lightweight harness that
    // boots the FULL NexusService (all handlers registered), and the chash
    // routes need exactly that.

    // POLARITY NOTE (RDR-180, nexus-jxizy.7/.8): pre-flip the migration/upsert
    // routes NORMALIZED an incoming 64-char row to its [:32] key (the SQLite-era
    // full-hash shape). Post-flip the FULL 64-hex digest IS the canonical chash —
    // never truncated — and a bare 32-hex value is a legacy reference that must
    // resolve through nexus.chash_alias, never accepted fresh at these seams.
    // These tests REPLACE the pre-flip truncation-contract tests (inverted, not
    // deleted, mirroring ChashTypeTest / PgVectorServingContractTest).

    @Test
    void chashImport_full64CharRow_storedAsIs() throws Exception {
        String full = "e".repeat(64);
        var resp = post("/v1/chash/import", Map.of(
            "rows", List.of(Map.of(
                "chash", full,
                "collection", "code__legacy64",
                "created_at", "2025-01-01T00:00:00Z"))));
        assertThat(resp.statusCode()).isEqualTo(200);
        // The row must be findable under its FULL 64-hex id (GET /v1/chash/lookup).
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort()
                + "/v1/chash/lookup?chash=" + full))
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build();
        var lookup = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(lookup.statusCode()).isEqualTo(200);
        assertThat(lookup.body()).contains("code__legacy64");
    }

    @Test
    void chashImport_legacy32CharRow_rejected400() throws Exception {
        var resp = post("/v1/chash/import", Map.of(
            "rows", List.of(Map.of(
                "chash", "e".repeat(32),
                "collection", "code__legacy32",
                "created_at", "2025-01-01T00:00:00Z"))));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("chash").contains("legacy 32-hex");
    }

    @Test
    void chashUpsertMany_nonStringElement_400WithIndex() throws Exception {
        // nexus-e0hd2: the old loop silently DROPPED non-string elements
        // (the castRows disease) — now a loud 400 naming the index. Index 0
        // must be a VALID canonical chash so the assertion actually reaches
        // index 1's type violation (a non-canonical index 0 would 400 first
        // on its own, masking this test's intent).
        var resp = post("/v1/chash/upsert_many", Map.of(
            "chashes", java.util.Arrays.asList("a".repeat(64), 42),
            "collection", "code__strict"));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("chashes[1]").contains("must be a string");
    }

    @Test
    void chashUpsert_full64CharRow_storedAsIs() throws Exception {
        var resp = post("/v1/chash/upsert", Map.of(
            "chash", "b".repeat(64), "collection", "code__norm64"));
        assertThat(resp.statusCode()).isEqualTo(200);
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort()
                + "/v1/chash/lookup?chash=" + "b".repeat(64)))
            .header("Authorization", "Bearer " + TOKEN)
            .GET().build();
        var lookup = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(lookup.statusCode()).isEqualTo(200);
        assertThat(lookup.body()).contains("code__norm64");
    }

    @Test
    void chashUpsert_legacy32CharRow_rejected400() throws Exception {
        var resp = post("/v1/chash/upsert", Map.of(
            "chash", "b".repeat(32), "collection", "code__legacy32"));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("legacy 32-hex").contains("chash_alias");
    }

    @Test
    void chashUpsert_otherBadLength_still400() throws Exception {
        // Any non-32, non-64 length is genuinely malformed and 400s with the
        // offending length.
        var resp = post("/v1/chash/upsert", Map.of(
            "chash", "c".repeat(40), "collection", "code__strict"));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("got 40 chars");
    }

    @Test
    void upsertChunks_voyageCollectionInOnnxMode_is422_withActionableBody() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "knowledge__nexus__voyage-context-3__v1",
            "ids",        List.of(dev.nexus.service.db.Chash.ofText("pebfx2-c1").toHex()),
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
    void upsertChunks_withEmbeddings_passthroughStoresVerbatim_tokensZero() throws Exception {
        // nexus-hxry2 HTTP-boundary contract: a Python client posting the
        // `embeddings` field is the same-model passthrough — the service parses
        // the JSON number vectors, stores them verbatim, and skips the embedder
        // (tokens:0). Proves the Python→JSON→Java parse seam end-to-end. minilm-384
        // wired here → a 384-dim supplied vector.
        var vec = new java.util.ArrayList<Float>(384);
        for (int i = 0; i < 384; i++) vec.add(0.0f);
        vec.set(0, 0.25f);
        vec.set(1, 0.75f);
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "knowledge__pebfx2__minilm-l6-v2-384__v1",
            "ids",        List.of(dev.nexus.service.db.Chash.ofText("pthttp384").toHex()),
            "documents",  List.of("passthrough over http"),
            "metadatas",  List.of(Map.of()),
            "embeddings", List.of(vec)));
        assertThat(resp.statusCode())
            .as("passthrough upsert must succeed (body: %s)", resp.body())
            .isEqualTo(200);
        assertThat(resp.body()).contains("\"tokens\":0");
    }

    @Test
    void upsertChunks_withEmbeddings_wrongDim_failsLoud() throws Exception {
        // A supplied vector whose dim != the dispatched table (384) must fail
        // loud at the HTTP boundary — never silently stored or re-embedded.
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "knowledge__pebfx2__minilm-l6-v2-384__v1",
            "ids",        List.of(dev.nexus.service.db.Chash.ofText("ptbaddimhttp").toHex()),
            "documents",  List.of("bad dim over http"),
            "metadatas",  List.of(Map.of()),
            "embeddings", List.of(List.of(1.0f, 0.0f))));  // 2-dim, not 384
        assertThat(resp.statusCode())
            .as("dim mismatch must be a 4xx, not a silent 200 (body: %s)", resp.body())
            .isIn(400, 422);
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
            "ids",        List.of(dev.nexus.service.db.Chash.ofText("x").toHex()),
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
        // nexus-pebfx.5: the embedding-mode handshake — this harness is wired
        // key-less with MiniLM, so the mode reads onnx-local with the MiniLM
        // model. (Production local mode reports the same mode but bge-768 in
        // availableModels per RDR-160; asserted in EmbedderRouterBge768Test.)
        assertThat(body.get("embedding_mode")).isEqualTo("onnx-local");
        assertThat((List<String>) body.get("embedding_models"))
            .containsExactly("minilm-l6-v2-384");
        assertThat(body.get("schema_error")).isNull();
        assertThat((String) body.get("schema_latest_id")).isNotBlank();
        assertThat(((Number) body.get("schema_changeset_count")).longValue())
            .isGreaterThan(0);
    }

    @Test
    void getEmbeddings_returnsStoredVectors_inRequestOrder() throws Exception {
        // nexus-pebfx.7: the search engine fetches result vectors post-search
        // (contradiction check + Ward clustering); the endpoint returns stored
        // pgvector rows, request order, missing ids omitted (Chroma parity).
        String embA = dev.nexus.service.db.Chash.ofText("emb-a").toHex();
        String embB = dev.nexus.service.db.Chash.ofText("emb-b").toHex();
        String embMissing = dev.nexus.service.db.Chash.ofText("emb-missing").toHex();
        var up = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "knowledge__pebfx7__minilm-l6-v2-384__v1",
            "ids",        List.of(embB, embA),
            "documents",  List.of("second text", "first text"),
            "metadatas",  List.of(Map.of(), Map.of())));
        assertThat(up.statusCode()).isEqualTo(200);

        var resp = post("/v1/vectors/get-embeddings", Map.of(
            "collection", "knowledge__pebfx7__minilm-l6-v2-384__v1",
            "ids",        List.of(embA, embB, embMissing)));
        assertThat(resp.statusCode()).isEqualTo(200);
        @SuppressWarnings("unchecked")
        Map<String, Object> body = MAPPER.readValue(resp.body(), Map.class);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) body.get("ids");
        @SuppressWarnings("unchecked")
        List<List<Number>> embeddings = (List<List<Number>>) body.get("embeddings");
        assertThat(ids).containsExactly(embA, embB);   // request order, missing omitted
        assertThat(embeddings).hasSize(2);
        assertThat(embeddings.get(0)).hasSize(384);
        assertThat(embeddings.get(1)).hasSize(384);
        // The two texts differ, so the stored vectors must differ.
        assertThat(embeddings.get(0)).isNotEqualTo(embeddings.get(1));
    }

    @Test
    void minilmCollectionInOnnxMode_stillServes200() throws Exception {
        var resp = post("/v1/vectors/upsert-chunks", Map.of(
            "collection", "knowledge__pebfx2__minilm-l6-v2-384__v1",
            "ids",        List.of(dev.nexus.service.db.Chash.ofText("pebfx2-ok1").toHex()),
            "documents",  List.of("a servable chunk"),
            "metadatas",  List.of(Map.of())));
        assertThat(resp.statusCode())
            .as("refusal must not over-trigger: minilm is servable in onnx-local "
                + "mode (got body: %s)", resp.body())
            .isEqualTo(200);
    }
}
