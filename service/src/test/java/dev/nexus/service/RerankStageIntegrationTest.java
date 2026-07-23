// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.CrossEncoderReranker;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.VoyageReranker;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentLinkedQueue;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-188 bead nexus-9o6y2.4 — Testcontainers integration suite for the fused
 * rerank stage: real pgvector rows under RLS, the REAL {@link VoyageReranker}
 * against a scripted fake Voyage upstream, the real HTTP surface end to end.
 *
 * <p>The contract locked here for the P2 client repoint:
 * <ul>
 *   <li><b>Success</b>: {@code {"results": [...], "rerank_degraded": false,
 *       "rerank_model": ...}}, rows REORDERED by relevance (the fixture's fake
 *       scores invert distance order, so a silently-skipped rerank cannot
 *       pass), each row carrying {@code rerank_score}.</li>
 *   <li><b>Degrade is LOUD and structured</b>: upstream 5xx-exhaustion and
 *       auth rejection both serve 200 with distance-order rows PLUS
 *       {@code rerank_degraded=true} + {@code rerank_error} — never a silent
 *       input-order fallback (the retired client anti-pattern), never a
 *       rerank-failure-poisons-search 5xx.</li>
 *   <li><b>Cross-encoder path</b>: with no Voyage anywhere server-side, the
 *       local scorer serves the same envelope (gated on the ~91MB artifact
 *       being provisioned; skips on CI).</li>
 *   <li><b>Legacy shape untouched</b>: no {@code rerank} field → bare array.</li>
 * </ul>
 *
 * <p>Hermetic + deterministic: Testcontainers pgvector, FakeEmbedder unit
 * vectors (exact distances), scripted upstream, port 0 everywhere, retry base
 * 10ms.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RerankStageIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN  = "rdr188-stage-token-0123456789abcdef";
    private static final String TENANT = "rdr188-tenant";

    private static final String COL = "knowledge__rdr188stage__voyage-context-3__v1";
    private static final String Q   = "tenant isolation policy";

    private static final String DOC_NEAR = "the tenant isolation policy guards every row";
    private static final String DOC_MID  = "tenant isolation policy enforcement in postgres";
    private static final String DOC_FAR  = "a tenant isolation policy for vector chunks";

    private static final String C1 = "10" + "0".repeat(62);   // distance 0.0 (nearest)
    private static final String C2 = "20" + "0".repeat(62);   // distance 0.2
    private static final String C3 = "30" + "0".repeat(62);   // distance 0.4 (farthest)

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository pgRepo;

    HttpServer fakeVoyage;
    final ConcurrentLinkedQueue<Object[]> voyageResponses = new ConcurrentLinkedQueue<>();

    NexusService svcVoyage;   // fused stage → real VoyageReranker → fake upstream
    NexusService svcNone;     // no reranker wired
    NexusService svcCross;    // local cross-encoder (test gated on artifact presence)
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
            ps.setString(3, "rdr188-stage-test");
            ps.executeUpdate();
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);

        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        embedder.register(Q,        1.0f, 0.0f);
        embedder.register(DOC_NEAR, 1.0f, 0.0f);
        embedder.register(DOC_MID,  0.8f, 0.6f);
        embedder.register(DOC_FAR,  0.6f, 0.8f);
        pgRepo = new PgVectorRepository(new TenantScope(svcDs), embedder, embedder);

        // Scripted fake Voyage /v1/rerank upstream (same idiom as VoyageRerankerTest).
        fakeVoyage = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fakeVoyage.createContext("/v1/rerank", exchange -> {
            exchange.getRequestBody().readAllBytes();
            Object[] scripted = voyageResponses.poll();
            int status = scripted == null ? 200 : (Integer) scripted[0];
            byte[] body = (scripted == null ? "{\"data\": []}" : (String) scripted[1])
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, body.length);
            try (OutputStream os = exchange.getResponseBody()) { os.write(body); }
        });
        fakeVoyage.start();
        String fakeUrl = "http://127.0.0.1:" + fakeVoyage.getAddress().getPort() + "/v1/rerank";

        svcVoyage = new NexusService(0, TOKEN, svcDs, null, pgRepo,
                new VoyageReranker("test-key", "rerank-2.5", fakeUrl, 10L, Optional.empty()));
        svcVoyage.start();
        svcNone = new NexusService(0, TOKEN, svcDs, null, pgRepo);
        svcNone.start();
        svcCross = new NexusService(0, TOKEN, svcDs, null, pgRepo, new CrossEncoderReranker());
        svcCross.start();
        http = HttpClient.newHttpClient();

        // Fixture rows: distances 0.0 / 0.2 / 0.4 → distance order C1, C2, C3.
        Map<String, Object> up = postOk(svcVoyage, "/v1/vectors/upsert-chunks", Map.of(
            "collection", COL,
            "ids",        List.of(C1, C2, C3),
            "documents",  List.of(DOC_NEAR, DOC_MID, DOC_FAR),
            "metadatas",  List.of(Map.of(), Map.of(), Map.of())));
        assertThat(((Number) up.get("upserted")).intValue()).isEqualTo(3);
    }

    @AfterAll
    void stopAll() {
        if (svcVoyage  != null) svcVoyage.stop();
        if (svcNone    != null) svcNone.stop();
        if (svcCross   != null) svcCross.stop();
        if (fakeVoyage != null) fakeVoyage.stop(0);
        if (svcDs      != null) svcDs.close();
        if (pg         != null) pg.stop();
    }

    @BeforeEach
    void clearScript() {
        voyageResponses.clear();
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private HttpResponse<String> post(NexusService svc, String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + svc.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private Map<String, Object> postOk(NexusService svc, String path, Object body) throws Exception {
        var resp = post(svc, path, body);
        assertThat(resp.statusCode()).as("%s (body: %s)", path, resp.body()).isEqualTo(200);
        return MAPPER.readValue(resp.body(), MAP_TYPE);
    }

    private void scriptVoyage(int status, String body) {
        voyageResponses.add(new Object[] {status, body});
    }

    private static String voyageScores(String entries) {
        return "{\"object\": \"list\", \"data\": [" + entries + "], "
                + "\"model\": \"rerank-2.5\", \"usage\": {\"total_tokens\": 7}}";
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> results(Map<String, Object> envelope) {
        return (List<Map<String, Object>>) envelope.get("results");
    }

    private static Map<String, Object> searchBody(Object... extra) {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("query", Q);
        body.put("collections", List.of(COL));
        body.put("n_results", 3);
        for (int i = 0; i < extra.length; i += 2) body.put((String) extra[i], extra[i + 1]);
        return body;
    }

    // ── Success: reorder is real, not a passthrough ──────────────────────────

    @Test
    void voyageRerankReordersRowsAgainstDistanceOrder() throws Exception {
        // Fake scores INVERT distance order (farthest doc scored highest): a
        // stage that silently skipped reranking would return C1,C2,C3 and fail.
        scriptVoyage(200, voyageScores(
                "{\"index\": 2, \"relevance_score\": 0.95}, "
                + "{\"index\": 1, \"relevance_score\": 0.5}, "
                + "{\"index\": 0, \"relevance_score\": 0.05}"));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search",
                searchBody("rerank", true));

        assertThat(env.get("rerank_degraded")).isEqualTo(false);
        assertThat(env.get("rerank_model")).isEqualTo("rerank-2.5");
        List<Map<String, Object>> rows = results(env);
        assertThat(rows).extracting(r -> r.get("id")).containsExactly(C3, C2, C1);
        assertThat(rows).extracting(r -> r.get("rerank_score"))
                .containsExactly(0.95, 0.5, 0.05);
        // Distance survives on the rows (the client shows both signals).
        assertThat((Double) rows.get(2).get("distance")).isLessThan(1e-6);
    }

    @Test
    void rerankTopKTruncatesToBestScored() throws Exception {
        scriptVoyage(200, voyageScores("{\"index\": 2, \"relevance_score\": 0.9}"));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search",
                searchBody("rerank", true, "rerank_top_k", 1));

        assertThat(results(env)).extracting(r -> r.get("id")).containsExactly(C3);
    }

    @Test
    void hybridSearchCarriesTheSameRerankEnvelope() throws Exception {
        scriptVoyage(200, voyageScores(
                "{\"index\": 2, \"relevance_score\": 0.9}, "
                + "{\"index\": 0, \"relevance_score\": 0.2}, "
                + "{\"index\": 1, \"relevance_score\": 0.1}"));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/hybrid-search",
                searchBody("rerank", true));

        assertThat(env.get("rerank_degraded")).isEqualTo(false);
        assertThat(results(env).get(0).get("id")).isEqualTo(C3);
    }

    // ── Degrade: LOUD + structured, never silent, never a 5xx ────────────────

    @Test
    void upstreamExhaustionDegradesLoudWithDistanceOrderRows() throws Exception {
        scriptVoyage(500, "{}");
        scriptVoyage(500, "{}");
        scriptVoyage(500, "{}");

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search",
                searchBody("rerank", true));

        // Falsify-by-deletion pin: these two keys ARE the loud-degrade contract —
        // remove either from the envelope and this test fails.
        assertThat(env).containsKey("rerank_degraded");
        assertThat(env).containsKey("rerank_error");
        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("500");

        List<Map<String, Object>> rows = results(env);
        assertThat(rows).extracting(r -> r.get("id")).containsExactly(C1, C2, C3);
        assertThat(rows.get(0)).doesNotContainKey("rerank_score");
    }

    @Test
    void upstreamAuthRejectionDegradesLoudNotA502() throws Exception {
        // The embed path maps UpstreamAuthException → 502 (no results exist
        // without embedding). The rerank stage has rows in hand: the same auth
        // failure must degrade the STAGE loudly, not poison the whole search.
        scriptVoyage(401, "{\"detail\": \"bad key\"}");

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search",
                searchBody("rerank", true));

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("401");
        assertThat(results(env)).hasSize(3);
    }

    @Test
    void noRerankerWiredDegradesLoud() throws Exception {
        Map<String, Object> env = postOk(svcNone, "/v1/vectors/search",
                searchBody("rerank", true));

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("no reranker configured");
        assertThat(results(env)).extracting(r -> r.get("id")).containsExactly(C1, C2, C3);
    }

    // ── Local cross-encoder path (no Voyage anywhere server-side) ────────────

    @Test
    void crossEncoderPathReranksWithoutAnyVoyageKey() throws Exception {
        Assumptions.assumeTrue(
                Files.isRegularFile(Path.of(CrossEncoderReranker.DEFAULT_MODEL_PATH))
                && Files.isRegularFile(Path.of(CrossEncoderReranker.DEFAULT_TOKENIZER_PATH)),
                "cross-encoder ONNX not provisioned — run `nx init` (skips on CI)");

        Map<String, Object> env = postOk(svcCross, "/v1/vectors/search",
                searchBody("rerank", true));

        assertThat(env.get("rerank_degraded")).isEqualTo(false);
        assertThat(env.get("rerank_model")).isEqualTo("ms-marco-minilm-l6-v2");
        List<Map<String, Object>> rows = results(env);
        assertThat(rows).hasSize(3);
        assertThat(rows).allSatisfy(r -> assertThat(r).containsKey("rerank_score"));
        // Scores must come back sorted descending (real inference order).
        for (int i = 1; i < rows.size(); i++) {
            assertThat(((Number) rows.get(i - 1).get("rerank_score")).doubleValue())
                    .isGreaterThanOrEqualTo(((Number) rows.get(i).get("rerank_score")).doubleValue());
        }
    }

    @Test
    void crossEncoderAbsentModelDegradesLoud() throws Exception {
        // Only meaningful when the artifact is NOT provisioned (the CI posture):
        // the lazy init must degrade the stage loud, not 500 the search.
        Assumptions.assumeTrue(
                !Files.isRegularFile(Path.of(CrossEncoderReranker.DEFAULT_MODEL_PATH)),
                "model provisioned locally — the absent-model posture is covered on CI");

        Map<String, Object> env = postOk(svcCross, "/v1/vectors/search",
                searchBody("rerank", true));

        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("not found");
        assertThat(results(env)).hasSize(3);
    }

    // ── Legacy shape + caller errors ─────────────────────────────────────────

    @Test
    void withoutRerankFieldTheBareArrayShapeIsUnchanged() throws Exception {
        var resp = post(svcVoyage, "/v1/vectors/search", searchBody());
        assertThat(resp.statusCode()).isEqualTo(200);
        List<?> rows = MAPPER.readValue(resp.body(), List.class);
        assertThat(rows).hasSize(3);
    }

    @Test
    void rerankTopKWithoutRerankIsA400() throws Exception {
        var resp = post(svcVoyage, "/v1/vectors/search", searchBody("rerank_top_k", 2));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("rerank_top_k");
    }
}
