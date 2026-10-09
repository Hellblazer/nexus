// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedResult;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tu8wp.1 -- the HTTP contract of {@code POST /v1/vectors/search-per-collection}: request
 * validation, the response envelope and its echo fields, the skipped-collections header, the
 * rerank stage on the merged rows, the isolation of a permanent per-collection error, and the 503
 * a transient failure in any collection turns the whole request into.
 *
 * <p>The repository under the service sits on an {@link ArmProbeDataSource}, so a transient arm
 * failure can be injected deterministically; the service's own auth and routing use the plain pool.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerSearchPerCollectionTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-tu8wp-percol-0123456789abcdef0000";
    private static final String TENANT = "tu8wp-http";
    private static final String QUERY = "tu8wp http query";

    private static final String DENSE = "knowledge__tu8wp-http-dense__minilm-l6-v2-384__v1";
    private static final String SMALL = "knowledge__tu8wp-http-small__minilm-l6-v2-384__v1";
    private static final String OTHER_MODEL = "knowledge__tu8wp-http-other__bge-base-en-v15-768__v1";
    private static final String ORPHAN = "knowledge__tu8wp-http-orphan__minilm-l6-v2-384__v1";
    private static final String GHOST = "knowledge__tu8wp-http-ghost__minilm-l6-v2-384__v1";
    private static final List<String> FAN = List.of(
        "knowledge__tu8wp-http-fan0__minilm-l6-v2-384__v1", "knowledge__tu8wp-http-fan1__minilm-l6-v2-384__v1",
        "knowledge__tu8wp-http-fan2__minilm-l6-v2-384__v1", "knowledge__tu8wp-http-fan3__minilm-l6-v2-384__v1");

    /** A query embedder that reports a fixed token count per embed call, to prove the count is emitted once. */
    private static final class TokenReportingEmbedder implements Embedder {
        private final Embedder delegate;
        private final long tokens;

        TokenReportingEmbedder(Embedder delegate, long tokens) {
            this.delegate = delegate;
            this.tokens = tokens;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            return delegate.embed(texts);
        }

        @Override
        public EmbedResult embedWithUsage(List<String> texts) {
            return new EmbedResult(delegate.embed(texts), tokens);
        }
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    ArmProbeDataSource probe;
    TenantScope probeScope;
    PgVectorRepository serviceRepo;
    NexusService service;
    HttpClient http;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "tu8wp-http");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        probe = new ArmProbeDataSource(svcDs);

        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        var seedScope = new TenantScope(svcDs);
        var seedRepo = new PgVectorRepository(seedScope, embedder, embedder);
        probeScope = new TenantScope(probe.dataSource());
        serviceRepo = new PgVectorRepository(probeScope, embedder, new TokenReportingEmbedder(embedder, 7L));
        var repo = serviceRepo;

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(DENSE, SMALL, OTHER_MODEL, ORPHAN)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
            for (String c : FAN) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }
        seed(seedScope, seedRepo, DENSE, "dense", 30, 0.0, 0.001);
        seed(seedScope, seedRepo, SMALL, "small", 3, 1.0, 0.01);
        for (String c : FAN) {
            seed(seedScope, seedRepo, c, "fan", 2, 0.2, 0.01);
        }
        // Every collection needs a chunk of its own: the engine sweeps registered-but-empty
        // "ghost" collections on a tenant's first authenticated request.
        seed(seedScope, seedRepo, ORPHAN, "orphan", 1, 0.5, 0.01);
        var embedder768 = new PgVectorRepositoryContractTest.FakeEmbedder(768);
        seed(seedScope, new PgVectorRepository(seedScope, embedder768, embedder768), OTHER_MODEL, "other", 1,
             0.5, 0.01, embedder768);
        // The orphan's registered dimension (768) now disagrees with the 384-dim vector the group's
        // model produces: the nexus-9tsdf stale-dimension class.
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.DIMENSION, 768)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT).and(CATALOG_COLLECTIONS.NAME.eq(ORPHAN)))
               .execute();
        }
        // The seeding upsert cached the row with its old dimension: drop it so the next lookup re-reads.
        dev.nexus.service.db.CollectionRegistry.evict(TENANT, ORPHAN);

        service = new NexusService(0, TOKEN, svcDs, null, repo);
        service.start();
        http = TestHttp.client();
    }

    private void seed(TenantScope scope, PgVectorRepository repo, String collection, String prefix, int count,
                      double angle0, double step) {
        seed(scope, repo, collection, prefix, count, angle0, step, embedder);
    }

    private void seed(TenantScope scope, PgVectorRepository repo, String collection, String prefix, int count,
                      double angle0, double step, PgVectorRepositoryContractTest.FakeEmbedder embedder) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
            String text = collection + "|" + prefix + "-" + i;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of());
        }
        repo.upsertChunks(TENANT, collection, ids, texts, metas);
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, ids.toArray(new String[0]));
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<String> post(Map<String, Object> body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/search-per-collection")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static Map<String, Object> request(Object collections, Object k, Object limit) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("query", QUERY);
        m.put("collections", collections);
        if (k != null) m.put("per_collection_k", k);
        if (limit != null) m.put("limit", limit);
        return m;
    }

    private static Map<String, Object> ok() {
        return request(List.of(DENSE, SMALL), 5, 100);
    }

    private JsonNode json(HttpResponse<String> r) throws Exception {
        return MAPPER.readTree(r.body());
    }

    // ── envelope and echo ─────────────────────────────────────────────────────

    @Test
    void servesTheEnvelope_withRowsPerCollectionStatsAndTheEcho() throws Exception {
        var r = post(ok());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.fieldNames()).toIterable()
            .containsExactly("results", "per_collection", "per_collection_k", "limit");
        assertThat(body.get("per_collection_k").asInt()).isEqualTo(5);
        assertThat(body.get("limit").asInt()).isEqualTo(100);

        // Per-collection top-5: DENSE would crowd SMALL out of a flat top-5, but SMALL's 3 are here.
        assertThat(body.get("results")).hasSize(8);
        long small = 0;
        for (JsonNode row : body.get("results")) {
            assertThat(row.fieldNames()).toIterable().contains("id", "content", "distance", "collection",
                                                               "retention", "chash", "span");
            assertThat(row.has("source_uri")).as("source_uri is opt-in").isFalse();
            if (SMALL.equals(row.get("collection").asText())) small++;
        }
        assertThat(small).isEqualTo(3);

        JsonNode pc = body.get("per_collection");
        assertThat(pc).hasSize(2);
        assertThat(pc.get(0).fieldNames()).toIterable().containsExactly(
            "collection", "raw_count", "dropped", "min_raw_distance", "min_dropped_distance", "error",
            "error_kind");
        assertThat(pc.get(0).get("collection").asText()).isEqualTo(DENSE);
        assertThat(pc.get(0).get("raw_count").asInt()).isEqualTo(5);
        assertThat(pc.get(0).get("dropped").asInt()).isZero();
        assertThat(pc.get(0).get("error").isNull()).isTrue();
        assertThat(pc.get(0).get("error_kind").isNull()).isTrue();
        assertThat(pc.get(1).get("collection").asText()).isEqualTo(SMALL);
        assertThat(pc.get(1).get("raw_count").asInt()).isEqualTo(3);
        assertThat(r.headers().firstValue("X-Nexus-Skipped-Collections")).isEmpty();
    }

    @Test
    void thresholdsAndTheLimitShapeTheMerge_andIncludeSourceUriIsOptIn() throws Exception {
        Map<String, Object> req = ok();
        Map<String, Object> thresholds = new LinkedHashMap<>();
        thresholds.put(SMALL, 0.0001);      // SMALL's rows are ~0.46 away: all dropped
        thresholds.put(DENSE, null);
        req.put("thresholds", thresholds);
        req.put("limit", 4);
        req.put("include_source_uri", true);
        var r = post(req);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.get("results")).hasSize(4);
        assertThat(body.get("limit").asInt()).isEqualTo(4);
        for (JsonNode row : body.get("results")) {
            assertThat(row.get("collection").asText()).isEqualTo(DENSE);
            assertThat(row.has("source_uri")).isTrue();
        }
        JsonNode small = body.get("per_collection").get(1);
        assertThat(small.get("raw_count").asInt()).isEqualTo(3);
        assertThat(small.get("dropped").asInt()).isEqualTo(3);
        assertThat(small.get("min_dropped_distance").isNumber()).isTrue();
    }

    @Test
    void anUnregisteredNameIsReportedInTheSkippedHeader_notTheBody() throws Exception {
        var r = post(request(List.of(DENSE, GHOST, SMALL), 5, 100));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(r.headers().firstValue("X-Nexus-Skipped-Collections")).hasValue(GHOST);
        assertThat(json(r).get("per_collection")).hasSize(2);
    }

    @Test
    void rerankRunsOnceOverTheMergedRows_andDegradesLoudWithoutAReranker() throws Exception {
        Map<String, Object> req = ok();
        req.put("rerank", true);
        var r = post(req);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.get("rerank_degraded").asBoolean()).isTrue();
        assertThat(body.get("rerank_error").asText()).contains("no reranker configured");
        assertThat(body.get("results")).hasSize(8);
        assertThat(body.get("per_collection")).hasSize(2);
        assertThat(body.get("per_collection_k").asInt()).isEqualTo(5);
    }

    @Test
    void aPermanentPerCollectionErrorIsIsolated_theOthersAreServed() throws Exception {
        var r = post(request(List.of(DENSE, ORPHAN, SMALL), 5, 100));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.get("results")).hasSize(8);
        JsonNode orphan = body.get("per_collection").get(1);
        assertThat(orphan.get("collection").asText()).isEqualTo(ORPHAN);
        assertThat(orphan.get("error").asText())
            .isEqualTo("query embedder produced a 384-dim vector but the collection dispatches to embedding_768");
        assertThat(orphan.get("error_kind").asText()).isEqualTo("dimension_mismatch");
        assertThat(orphan.has("error_class")).as("the Java class name is no longer on the wire").isFalse();
        assertThat(body.get("per_collection").get(0).get("error").isNull()).isTrue();
    }

    // ── include_embeddings (nexus-92q1p) ──────────────────────────────────────

    private static float[] decodeEmbedding(String b64) {
        byte[] raw = java.util.Base64.getDecoder().decode(b64);
        float[] out = new float[raw.length / 4];
        java.nio.ByteBuffer.wrap(raw).order(java.nio.ByteOrder.LITTLE_ENDIAN).asFloatBuffer().get(out);
        return out;
    }

    @Test
    void includeEmbeddings_putsEachSurvivorsStoredVectorOnItsRow_andStatesTheEncoding() throws Exception {
        Map<String, Object> req = ok();
        req.put("include_embeddings", true);
        req.put("limit", 6);   // cut after the merge: only the six survivors are read
        var r = post(req);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.get("embedding_encoding").asText()).isEqualTo("f32-le-b64");
        assertThat(body.get("embedding_dim").asInt()).isEqualTo(384);
        assertThat(body.get("results")).hasSize(6);
        for (JsonNode row : body.get("results")) {
            assertThat(row.has("embedding_b64")).as("row %s", row.get("id")).isTrue();
            float[] got = decodeEmbedding(row.get("embedding_b64").asText());
            assertThat(got).as("384 components, 4 bytes each").hasSize(384);
            float[] stored = embedder.embed(List.of(row.get("content").asText())).get(0);
            assertThat(got).as("the stored vector of %s, not another row's", row.get("content").asText())
                .containsExactly(stored);
        }
    }

    /**
     * nexus-92q1p review M2: the by-id vector fill is an enrichment of finished results. A DB error in it must
     * not discard them: the rows come back without embedding_b64 (the client fetches those by id) and the
     * request is a 200, not the whole-request 503 an unwrapped failure becomes.
     */
    @Test
    void includeEmbeddings_aFailedFill_keepsTheFinishedRows_withoutVectors() throws Exception {
        probe.reset();
        probe.failEmbeddingFill = true;
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        try {
            Map<String, Object> req = ok();
            req.put("include_embeddings", true);
            req.put("limit", 6);
            var r = post(req);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            JsonNode body = json(r);
            assertThat(body.get("results")).as("the search results survive the failed fill").hasSize(6);
            for (JsonNode row : body.get("results")) {
                assertThat(row.has("embedding_b64")).as("no vector on %s", row.get("id")).isFalse();
            }
            assertThat(body.get("embedding_encoding").asText()).isEqualTo("f32-le-b64");
        } finally {
            probe.reset();
        }
    }

    /**
     * nexus-92q1p review L6: {@code embeddings_limit} keeps the vector on only the first N rows of the final
     * order, so a client that keeps a pool smaller than {@code limit} (1000 under rerank) does not transfer
     * vectors for rows it drops. The rows past N stay in the result, without {@code embedding_b64}.
     */
    @Test
    void includeEmbeddings_embeddingsLimit_keepsTheVectorOnTheFirstNRowsOnly() throws Exception {
        Map<String, Object> req = ok();
        req.put("include_embeddings", true);
        req.put("limit", 6);
        req.put("embeddings_limit", 2);
        var r = post(req);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode results = json(r).get("results");
        assertThat(results).as("the rows are all still there").hasSize(6);
        for (int i = 0; i < results.size(); i++) {
            assertThat(results.get(i).has("embedding_b64")).as("row %d of 6, embeddings_limit 2", i).isEqualTo(i < 2);
        }
        assertThat(json(r).get("embedding_dim").asInt()).isEqualTo(384);

        Map<String, Object> bad = ok();
        bad.put("include_embeddings", true);
        bad.put("embeddings_limit", 0);
        assertThat(post(bad).statusCode()).as("embeddings_limit must be positive").isEqualTo(400);
    }

    /**
     * nexus-tao37: {@code content_chars} caps each row's text. A default MCP search shows 200-300 characters of
     * a 10-row page from a 300-row pool, and the full text was about half of every 100-160 KB gzipped
     * response (measured 2026-10-09). The rows, their order and every other field are unchanged, and the
     * envelope echoes the cap so a client can tell an engine that applied it from one that predates it.
     */
    @Test
    void contentChars_capsEachRowsText_echoesTheCap_andKeepsTheRows() throws Exception {
        JsonNode full = json(post(ok())).get("results");
        Map<String, Object> req = ok();
        req.put("content_chars", 5);
        var r = post(req);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.get("content_chars").asInt()).isEqualTo(5);
        JsonNode capped = body.get("results");
        assertThat(capped).hasSize(full.size());
        for (int i = 0; i < full.size(); i++) {
            assertThat(capped.get(i).get("id").asText()).isEqualTo(full.get(i).get("id").asText());
            String whole = full.get(i).get("content").asText();
            assertThat(capped.get(i).get("content").asText())
                .isEqualTo(whole.substring(0, Math.min(5, whole.length())));
            assertThat(capped.get(i).get("chash").asText()).isEqualTo(full.get(i).get("chash").asText());
        }
    }

    @Test
    void contentChars_mustBePositive() throws Exception {
        for (Object bad : new Object[] {0, -3}) {
            Map<String, Object> req = ok();
            req.put("content_chars", bad);
            assertThat(post(req).statusCode()).as("content_chars %s", bad).isEqualTo(400);
        }
    }

    @Test
    void contentChars_appliesAfterRerank() throws Exception {
        Map<String, Object> req = ok();
        req.put("rerank", true);
        req.put("content_chars", 4);
        var r = post(req);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = json(r);
        assertThat(body.get("content_chars").asInt()).isEqualTo(4);
        for (JsonNode row : body.get("results")) {
            assertThat(row.get("content").asText().length()).isLessThanOrEqualTo(4);
        }
    }

    @Test
    void capCodePoints_neverSplitsASurrogatePair() {
        String s = "a\uD83D\uDE00bc"; // a, grinning face (two UTF-16 units), b, c
        assertThat(dev.nexus.service.http.VectorHandler.capCodePoints(s, 2)).isEqualTo("a\uD83D\uDE00");
        assertThat(dev.nexus.service.http.VectorHandler.capCodePoints(s, 1)).isEqualTo("a");
        assertThat(dev.nexus.service.http.VectorHandler.capCodePoints(s, 10)).isSameAs(s);
        assertThat(dev.nexus.service.http.VectorHandler.capCodePoints(null, 3)).isNull();
    }

    @Test
    void includeEmbeddings_isOptIn_theDefaultResponseIsUnchanged() throws Exception {
        for (Object flag : new Object[] {null, false}) {
            Map<String, Object> req = ok();
            if (flag != null) req.put("include_embeddings", flag);
            var r = post(req);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            JsonNode body = json(r);
            assertThat(body.fieldNames()).toIterable()
                .containsExactly("results", "per_collection", "per_collection_k", "limit");
            for (JsonNode row : body.get("results")) {
                assertThat(row.has("embedding_b64")).isFalse();
            }
        }
    }

    @Test
    void includeEmbeddings_survivesRerank_andStatesTheEncodingForAnEmptyResult() throws Exception {
        Map<String, Object> req = ok();
        req.put("include_embeddings", true);
        req.put("rerank", true);
        JsonNode body = json(post(req));
        assertThat(body.get("rerank_degraded").asBoolean()).isTrue();
        for (JsonNode row : body.get("results")) {
            assertThat(row.has("embedding_b64")).isTrue();
        }

        Map<String, Object> none = ok();
        none.put("include_embeddings", true);
        none.put("thresholds", Map.of(DENSE, -0.001, SMALL, -0.001));   // every row dropped by its threshold
        JsonNode empty = json(post(none));
        assertThat(empty.get("results")).isEmpty();
        assertThat(empty.get("embedding_encoding").asText()).isEqualTo("f32-le-b64");
        assertThat(empty.get("embedding_dim").asInt()).isEqualTo(384);
    }

    /**
     * The request pattern before and after, over loopback against this engine, printed (bounds nothing).
     * Before: the search, then one {@code get-embeddings} request per collection in the pool (the client's
     * {@code _fetch_embeddings_for_results}, 8 in flight). After: the search with {@code include_embeddings}.
     * Loopback has no round-trip time, so this shows the engine's work and the bytes, and the request count
     * is what the round-trip time multiplies on the managed service (about 1.2 s per call there, 14 to 22 calls).
     */
    @Test
    void includeEmbeddings_versusTheGetEmbeddingsFanout_printed() throws Exception {
        int collections = Integer.getInteger("spc.bench.collections", 14);
        int rows = Integer.getInteger("spc.bench.rows", 40);
        var scope = new TenantScope(svcDs);
        var repo = new PgVectorRepository(scope, embedder, embedder);
        List<String> names = new ArrayList<>();
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (int c = 0; c < collections; c++) {
                String name = "knowledge__tu8wp-bench-" + c + "__minilm-l6-v2-384__v1";
                names.add(name);
                PgContainerHelper.insertCollection(dsl, TENANT, name);
            }
        }
        for (String name : names) {
            seed(scope, repo, name, "bench", rows, 0.1, 0.002);
        }
        Map<String, Object> req = request(names, rows, collections * rows);
        // Warm both paths once.
        post(req);
        long t0 = System.nanoTime();
        var search = post(req);
        JsonNode body = json(search);
        Map<String, List<String>> idsByCollection = new LinkedHashMap<>();
        for (JsonNode row : body.get("results")) {
            idsByCollection.computeIfAbsent(row.get("collection").asText(), k -> new ArrayList<>())
                .add(row.get("id").asText());
        }
        long fetchedBytes = 0;
        try (var pool = java.util.concurrent.Executors.newFixedThreadPool(8)) {
            List<java.util.concurrent.Future<Integer>> futures = new ArrayList<>();
            for (var e : idsByCollection.entrySet()) {
                futures.add(pool.submit(() -> {
                    var r = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/get-embeddings")
                        .header("Authorization", "Bearer " + TOKEN)
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(
                            Map.of("collection", e.getKey(), "ids", e.getValue()))))
                        .build();
                    return http.send(r, HttpResponse.BodyHandlers.ofString()).body().length();
                }));
            }
            for (var f : futures) fetchedBytes += f.get();
        }
        long beforeMs = (System.nanoTime() - t0) / 1_000_000L;
        long beforeBytes = fetchedBytes + search.body().length();

        Map<String, Object> withVectors = new LinkedHashMap<>(req);
        withVectors.put("include_embeddings", true);
        post(withVectors);
        long t1 = System.nanoTime();
        var one = post(withVectors);
        long afterMs = (System.nanoTime() - t1) / 1_000_000L;
        System.out.println("search+embeddings collections=" + collections + " rows=" + body.get("results").size()
            + " BEFORE requests=" + (1 + idsByCollection.size()) + " wall_ms=" + beforeMs + " bytes=" + beforeBytes
            + " | AFTER requests=1 wall_ms=" + afterMs + " bytes=" + one.body().length());
        assertThat(json(one).get("results")).hasSize(body.get("results").size());
    }

    // ── gzip (nexus-tjyzn) ────────────────────────────────────────────────────

    @Test
    void aGzipAcceptingClientGetsACompressedBodyThatDecodesToTheSameJson() throws Exception {
        String payload = MAPPER.writeValueAsString(ok());
        HttpRequest.Builder b = TestHttp.request("http://127.0.0.1:" + service.getPort()
                + "/v1/vectors/search-per-collection")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(payload));
        var identity = http.send(b.build(), HttpResponse.BodyHandlers.ofByteArray());
        assertThat(identity.headers().firstValue("Content-Encoding")).as("no Accept-Encoding, no encoding").isEmpty();

        var gz = http.send(b.header("Accept-Encoding", "gzip").build(), HttpResponse.BodyHandlers.ofByteArray());
        assertThat(gz.statusCode()).isEqualTo(200);
        assertThat(gz.headers().firstValue("Content-Encoding")).hasValue("gzip");
        assertThat(gz.headers().firstValue("Vary").orElse("")).contains("Accept-Encoding");
        assertThat(gz.body().length).as("compressed is smaller").isLessThan(identity.body().length);
        byte[] plain;
        try (var in = new java.util.zip.GZIPInputStream(new java.io.ByteArrayInputStream(gz.body()))) {
            plain = in.readAllBytes();
        }
        assertThat(MAPPER.readTree(plain)).isEqualTo(MAPPER.readTree(identity.body()));
    }

    // ── a transient failure is a whole-request 503 ────────────────────────────

    @Test
    void poolOrAdmissionExhaustionInAnyArmIs503ForTheWholeRequest() throws Exception {
        post(ok());   // warm the registry cache so only arms borrow below
        probe.reset();
        probe.failFromArm = 2;
        probe.failure = () -> new SQLTransientConnectionException("pool exhausted (probe)");
        try {
            var r = post(ok());
            assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
            assertThat(json(r).has("results")).as("never a partial result").isFalse();
        } finally {
            probe.reset();
        }
    }

    @Test
    void aStatementTimeoutInOneArmIsIsolatedWithItsKind_theOtherCollectionIsServed() throws Exception {
        post(ok());
        probe.reset();
        probe.failFromArm = 2;     // exactly one of the two arms times out
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        try {
            var r = post(ok());
            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            JsonNode body = json(r);
            long timedOut = 0;
            for (JsonNode pc : body.get("per_collection")) {
                if (!pc.get("error").isNull()) {
                    timedOut++;
                    assertThat(pc.get("error_kind").asText()).isEqualTo("statement_timeout");
                    assertThat(pc.get("error").asText()).contains("statement timeout");
                    assertThat(pc.get("raw_count").asInt()).isZero();
                }
            }
            assertThat(timedOut).isEqualTo(1);
            assertThat(body.get("results")).as("the other collection was served").isNotEmpty();
        } finally {
            probe.reset();
        }
    }

    @Test
    void everyArmTimingOutStillAnswers200_withEveryCollectionNamedAsFailed() throws Exception {
        post(ok());
        probe.reset();
        probe.failFromArm = 1;
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        try {
            var r = post(ok());
            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            JsonNode body = json(r);
            assertThat(body.get("results")).isEmpty();
            assertThat(body.get("per_collection")).hasSize(2)
                .allSatisfy(pc -> assertThat(pc.get("error_kind").asText()).isEqualTo("statement_timeout"));
        } finally {
            probe.reset();
        }
    }

    @Test
    void anOtherTransientFailureIs503WithRetryAfterForTheWholeRequest() throws Exception {
        post(ok());
        probe.reset();
        probe.failFromArm = 2;
        probe.failure = () -> new SQLException("out of memory (probe)", "53200");
        try {
            var r = post(ok());
            assertThat(r.statusCode()).as(r.body()).isEqualTo(503);
            assertThat(r.headers().firstValue("Retry-After")).isPresent();
            JsonNode body = json(r);
            assertThat(body.get("error").asText()).contains("no partial results");
            assertThat(body.get("retry_after_seconds").asLong()).isPositive();
            assertThat(body.has("results")).isFalse();
        } finally {
            probe.reset();
        }
    }

    @Test
    void theFanoutBudgetIsReportedPerCollectionWithItsKind() throws Exception {
        post(ok());
        probe.reset();
        probe.borrowDelayMs = 400;
        serviceRepo.overrideFanoutBudgetMsForTests(300);
        try {
            Map<String, Object> req = request(FAN, 2, 100);
            var r = post(req);
            assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
            JsonNode body = json(r);
            assertThat(body.get("per_collection")).hasSize(4)
                .allSatisfy(pc -> {
                    assertThat(pc.get("error_kind").asText()).isEqualTo("fanout_budget_exhausted");
                    assertThat(pc.get("error").asText()).contains("300 ms");
                });
            assertThat(body.get("results")).isEmpty();
        } finally {
            serviceRepo.overrideFanoutBudgetMsForTests(0);
            probe.reset();
        }
    }

    @Test
    void theErrorKindsHaveStableWireNames() {
        assertThat(java.util.Arrays.stream(PgVectorRepository.ArmErrorKind.values())
                .map(PgVectorRepository.ArmErrorKind::wire).toList())
            .containsExactly("dimension_mismatch", "unsupported_dimension", "statement_timeout",
                             "fanout_budget_exhausted");
    }

    @Test
    void theUsageTokenHeaderCountsTheQueryEmbedOnce_notOncePerArm() throws Exception {
        var r = post(request(FAN, 2, 100));      // four arms, one embed
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(r.headers().firstValue("X-Nexus-Usage-Tokens")).hasValue("7");
    }

    // ── cross-request concurrency: plain /search is not starved (code review I2) ──

    @Test
    void aPlainSearchCompletesWhileSlowFanoutsHoldTheirArmSlots() throws Exception {
        post(request(FAN, 2, 100));     // warm the registry cache so only arms borrow below
        probeScope.replaceFanoutArmGateForTests(3);
        try {
            probe.reset();
            probe.holdMs = 2_000;       // each arm keeps its connection two seconds after it finishes
            // Two fan-out requests of four arms each: eight arm workers against a pool of eight, unless
            // a gate shared across requests holds them to three.
            var f1 = http.sendAsync(perCollectionRequest(request(FAN, 2, 100)), HttpResponse.BodyHandlers.ofString());
            var f2 = http.sendAsync(perCollectionRequest(request(FAN, 2, 100)), HttpResponse.BodyHandlers.ofString());
            Thread.sleep(500);          // both fan-outs are running and have taken what they will take
            long t0 = System.nanoTime();
            var search = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/search")
                .header("Authorization", "Bearer " + TOKEN).header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(
                    Map.of("query", QUERY, "collections", List.of(DENSE), "n_results", 3)))).build(),
                HttpResponse.BodyHandlers.ofString());
            long elapsedMs = (System.nanoTime() - t0) / 1_000_000L;
            assertThat(search.statusCode()).as(search.body()).isEqualTo(200);
            assertThat(elapsedMs)
                .as("a plain search needs one connection; the fan-outs may hold three of the eight, not all of them")
                .isLessThan(1_000);
            assertThat(f1.get().statusCode()).isEqualTo(200);
            assertThat(f2.get().statusCode()).isEqualTo(200);
            assertThat(probe.peak.get()).as("arm connections in flight across both requests").isLessThanOrEqualTo(3);
        } finally {
            probeScope.replaceFanoutArmGateForTests(5);
            probe.reset();
        }
    }

    private HttpRequest perCollectionRequest(Map<String, Object> body) throws Exception {
        return TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/search-per-collection")
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
    }

    @Test
    void aLongThatWouldWrapIntoARangeIsA400_notASmallValidNumber() throws Exception {
        // 4294967306 = 2^32 + 10: intValue() wraps it to 10, which the range check would accept.
        Map<String, Object> req = ok();
        req.put("per_collection_k", 4294967306L);
        assertBadRequest(req, "32-bit");
        Map<String, Object> req2 = ok();
        req2.put("limit", 4294967396L);
        assertBadRequest(req2, "32-bit");
    }

    // ── validation: 400 with the engine's JSON ────────────────────────────────

    private void assertBadRequest(Map<String, Object> req, String fragment) throws Exception {
        var r = post(req);
        assertThat(r.statusCode()).as("%s -> %s", fragment, r.body()).isEqualTo(400);
        assertThat(json(r).get("error").asText()).contains(fragment);
    }

    @Test
    void validation_rejectsMalformedRequestsWith400() throws Exception {
        assertBadRequest(request(List.of(DENSE), null, 10), "missing required field: per_collection_k");
        assertBadRequest(request(List.of(DENSE), 10, null), "missing required field: limit");
        assertBadRequest(request(List.of(DENSE), 0, 10), "per_collection_k");
        assertBadRequest(request(List.of(DENSE), 301, 10), "per_collection_k");
        assertBadRequest(request(List.of(DENSE), "abc", 10), "per_collection_k");
        assertBadRequest(request(List.of(DENSE), 10, 0), "limit");
        assertBadRequest(request(List.of(DENSE), 10, 1201), "limit");
        assertBadRequest(request("not-an-array", 10, 10), "must be an array");
        assertBadRequest(request(List.of(), 10, 10), "at least one collection");
        assertBadRequest(request(List.of(DENSE, " "), 10, 10), "blank");

        List<String> tooMany = new ArrayList<>();
        for (int i = 0; i <= 256; i++) tooMany.add("knowledge__tu8wp-n" + i + "__minilm-l6-v2-384__v1");
        assertBadRequest(request(tooMany, 10, 10), "at most 256");

        Map<String, Object> noQuery = ok();
        noQuery.remove("query");
        assertBadRequest(noQuery, "missing required field: query");

        Map<String, Object> badThresholds = ok();
        badThresholds.put("thresholds", List.of(1, 2));
        assertBadRequest(badThresholds, "thresholds");
        badThresholds.put("thresholds", Map.of(DENSE, "high"));
        assertBadRequest(badThresholds, "must be a number or null");
        badThresholds.put("thresholds", Map.of("knowledge__elsewhere__minilm-l6-v2-384__v1", 0.5));
        assertBadRequest(badThresholds, "not in collections");

        Map<String, Object> topKOnly = ok();
        topKOnly.put("rerank_top_k", 5);
        assertBadRequest(topKOnly, "rerank_top_k requires");

        Map<String, Object> maxCandidatesOnly = ok();
        maxCandidatesOnly.put("rerank_max_candidates", 5);
        assertBadRequest(maxCandidatesOnly, "rerank_max_candidates requires");
        Map<String, Object> maxCandidatesZero = ok();
        maxCandidatesZero.put("rerank", true);
        maxCandidatesZero.put("rerank_max_candidates", 0);
        assertBadRequest(maxCandidatesZero, "rerank_max_candidates");

        Map<String, Object> rerankTooWide = request(List.of(DENSE, SMALL), 300, 1001);
        rerankTooWide.put("rerank", true);
        assertBadRequest(rerankTooWide, "rerank scores at most 1000");

        assertBadRequest(request(List.of(DENSE, OTHER_MODEL), 10, 10), "mixed embedding models");
    }

    @Test
    void theRouteIsPostOnly_andNeedsAuth() throws Exception {
        var get = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/search-per-collection")
            .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(get.statusCode()).isEqualTo(405);
        var anon = http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/vectors/search-per-collection")
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(ok()))).build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(anon.statusCode()).isEqualTo(401);
    }
}
