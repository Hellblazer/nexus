// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.Reranker;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

/**
 * nexus-7fku2.1.4 (RDR-134 Step 4) -- the field-interaction table of RDR-134, one test per row, driven through the real
 * HTTP route with {@code topic_route} on and a stub reranker.
 *
 * <p>Rows already pinned elsewhere and not repeated here: {@code chash_allow} and a metadata {@code where} leave the
 * whole request unrouted ({@code PgVectorSearchPerCollectionTopicRouteIntegrationTest.aChashAllowRequestIsNotRouted_...}
 * and {@code aMetadataWhereIsNotRouted_...}, and {@code VectorHandlerSearchPerCollectionTopicRouteTest.
 * withWhereOrChashAllow_everyCollectionIsSkipped} at the wire). {@code thresholds} is pinned at the repository by
 * {@code theThresholdAppliesToRoutedRowsAsToUnscopedOnes}; the test here pins the same row at the wire, with the
 * per-collection counters.
 *
 * <p>The fixture has three collections of one model group, one for each status a routed request reports: ROUTED (two
 * topics; the topic whose centroid is nearest the query holds the chunks that are FARTHEST from it, so a routed search
 * returns different rows from an unscoped one), FALLBACK (its nearest centroid has no chunk, so the kept topic returns
 * nothing and the collection is searched unscoped), and PLAIN (no taxonomy: {@code no_topics}). Their rows interleave in
 * no way a routed and an unrouted request could share, so a test sees from the rows alone what routing did. A second
 * set of the same three, SENS, carries one sensitive chunk in each and is the only set the sensitivity test reads, so
 * the reranker's hold-back of sensitive rows never touches the other tests.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorHandlerSearchPerCollectionTopicRouteFieldsTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-7fku214-fields-0123456789abcdef000";
    private static final String TENANT = "7fku214-fields";
    private static final String QUERY = "7fku214 fields query";
    private static final String LEX_QUERY = "zebracorn";
    private static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    private static final String ROUTED = coll("routed");
    private static final String FALLBACK = coll("fallback");
    private static final String PLAIN = coll("plain");
    private static final String S_ROUTED = coll("s-routed");
    private static final String S_FALLBACK = coll("s-fallback");
    private static final String S_PLAIN = coll("s-plain");
    /** Read by the all-sensitive-topic test only: its nearest topic holds sensitive chunks and nothing else. */
    private static final String S_ALLSENS = coll("s-allsens");

    private static String coll(String name) {
        return "knowledge__7fku214-" + name + "__minilm-l6-v2-384__v1";
    }

    /** Scores a document by its place in {@link #preferred} (earlier is higher; absent is 0) and records what it saw. */
    private static final class StubReranker implements Reranker {
        volatile List<String> preferred = List.of();
        volatile List<String> lastDocuments;
        volatile Integer lastTopK;
        final AtomicInteger calls = new AtomicInteger();

        void reset() {
            preferred = List.of();
            lastDocuments = null;
            lastTopK = null;
            calls.set(0);
        }

        @Override
        public String modelToken() {
            return "stub-rerank";
        }

        @Override
        public boolean sendsTextOffMachine() {
            return false;
        }

        @Override
        public List<Scored> rerank(String query, List<String> documents, Integer topK) {
            calls.incrementAndGet();
            lastDocuments = List.copyOf(documents);
            lastTopK = topK;
            List<Scored> all = new ArrayList<>();
            for (int i = 0; i < documents.size(); i++) {
                int place = preferred.indexOf(documents.get(i));
                all.add(new Scored(i, place < 0 ? 0.0 : preferred.size() - place));
            }
            all.sort(Comparator.comparingDouble(Scored::relevanceScore).reversed());   // stable: ties keep input order
            return topK == null || all.size() <= topK ? all : all.subList(0, topK);
        }
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    NexusService service;
    HttpClient http;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    final StubReranker reranker = new StubReranker();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "7fku214-fields");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        embedder.register(LEX_QUERY, 1f, 0f);
        var scope = new TenantScope(svcDs);
        var repo = new PgVectorRepository(scope, embedder, embedder);
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(ROUTED, FALLBACK, PLAIN, S_ROUTED, S_FALLBACK, S_PLAIN, S_ALLSENS)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
        }
        // ROUTED: topic TA's centroid is the nearest the query (0.10 rad) and its chunks the farthest (0.30 rad and up);
        // TB's centroid is far (1.0 rad) and its chunks the nearest (0.05 rad and up). Routed top1 reads TA's rows.
        // One more chunk, the only one whose text holds the lexical token, sits in TB at 0.9 rad.
        seedSet(scope, repo, ROUTED, FALLBACK, PLAIN, true);
        seedSet(scope, repo, S_ROUTED, S_FALLBACK, S_PLAIN, false);
        // one sensitive chunk in each collection of the second set: the nearest routed one, the nearest fallback one
        // and the nearest plain one
        markSensitive(S_ROUTED, text(S_ROUTED, "ta", 0));
        markSensitive(S_FALLBACK, text(S_FALLBACK, "f", 0));
        markSensitive(S_PLAIN, text(S_PLAIN, "p", 0));
        // S_ALLSENS: topic SA's centroid is the nearest the query (0.10 rad) and holds two chunks, both sensitive and the
        // nearest of the collection (0.05 and 0.06 rad); topic SB's centroid is far (1.0 rad) and holds eight clean
        // chunks (0.30 rad and up). Routed top1 reads SA, which the exclusion empties: the status is fallback.
        List<String> sa = seed(scope, repo, S_ALLSENS, "sa", 2, 0.05, 0.01);
        List<String> sb = seed(scope, repo, S_ALLSENS, "sb", 8, 0.30, 0.01);
        taxonomy(S_ALLSENS, "SA", 0.10, sa);
        taxonomy(S_ALLSENS, "SB", 1.00, sb);
        markSensitive(S_ALLSENS, text(S_ALLSENS, "sa", 0));
        markSensitive(S_ALLSENS, text(S_ALLSENS, "sa", 1));
        // every collection of the two sets has an owner document; give each a source_uri of its own
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(ROUTED, FALLBACK, PLAIN, S_ROUTED, S_FALLBACK, S_PLAIN, S_ALLSENS)) {
                dsl.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SOURCE_URI, "file:///" + c)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq("own-" + c)))
                    .execute();
            }
        }
        service = new NexusService(0, TOKEN, svcDs, null, repo, reranker);
        service.start();
        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void resetTheReranker() {
        reranker.reset();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    private void seedSet(TenantScope scope, PgVectorRepository repo, String routed, String fallback, String plain,
                         boolean withLexicalChunk) throws Exception {
        List<String> ta = seed(scope, repo, routed, "ta", 6, 0.30, 0.01);
        List<String> tb = seed(scope, repo, routed, "tb", 6, 0.05, 0.01);
        if (withLexicalChunk) {
            String lexText = routed + " " + LEX_QUERY + " lx";
            embedder.register(lexText, (float) Math.cos(0.9), (float) Math.sin(0.9));
            repo.upsertChunks(TENANT, routed, List.of(chash(lexText)), List.of(lexText), List.of(Map.of()));
            scope.withTenant(TENANT, ctx -> {
                PgContainerHelper.ownChunks(ctx, TENANT, routed, chash(lexText));
                return null;
            });
            tb = new ArrayList<>(tb);
            tb.add(chash(lexText));
        }
        taxonomy(routed, "TA", 0.10, ta);
        taxonomy(routed, "TB", 1.00, tb);
        // FALLBACK: full coverage, but the nearest centroid (TE) has no chunk assigned; every chunk is TF's
        List<String> f = seed(scope, repo, fallback, "f", 8, 0.40, 0.01);
        taxonomy(fallback, "TE", 0.05, List.of());
        taxonomy(fallback, "TF", 1.20, f);
        // PLAIN: no taxonomy; the nearest rows of the whole fixture
        seed(scope, repo, plain, "p", 6, 0.15, 0.02);
    }

    private static String text(String collection, String prefix, int i) {
        return collection + "|" + prefix + i;
    }

    private List<String> seed(TenantScope scope, PgVectorRepository repo, String collection, String prefix, int count,
                              double angle0, double step) {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
            String text = text(collection, prefix, i);
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
        return ids;
    }

    private void taxonomy(String collection, String label, double centroidAngle, List<String> assigned) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, collection);
            long id = dsl.insertInto(TOPICS)
                .set(TOPICS.TENANT_ID, TENANT).set(TOPICS.LABEL, label + " of " + collection)
                .set(TOPICS.COLLECTION, collection).set(TOPICS.CREATED_AT, SEED_TS)
                .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
            dsl.insertInto(TAXONOMY_CENTROIDS)
                .set(TAXONOMY_CENTROIDS.TENANT_ID, TENANT).set(TAXONOMY_CENTROIDS.COLLECTION, collection)
                .set(TAXONOMY_CENTROIDS.TOPIC_ID, id).set(TAXONOMY_CENTROIDS.EMBEDDING_MODEL, model)
                .set(TAXONOMY_CENTROIDS.LABEL, label)
                .set(VectorSeeds.centroidEmbedding(384),
                     VectorSeeds.vecXY(384, (float) Math.cos(centroidAngle), (float) Math.sin(centroidAngle)))
                .execute();
            for (String hex : assigned) {
                dsl.insertInto(TOPIC_ASSIGNMENTS)
                    .set(TOPIC_ASSIGNMENTS.TENANT_ID, TENANT)
                    .set(TOPIC_ASSIGNMENTS.DOC_ID, HexFormat.of().parseHex(hex))
                    .set(TOPIC_ASSIGNMENTS.TOPIC_ID, id).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, collection)
                    .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
                    .execute();
            }
        }
    }

    /** A second, sensitive document that owns the chunk, so the chunk has a live sensitive owner. */
    private void markSensitive(String collection, String chunkText) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, collection);
            String doc = "sensitive-" + chash(chunkText).substring(0, 12);
            dsl.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, doc)
                .set(CATALOG_DOCUMENTS.TITLE, "Sensitive " + doc).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                .set(CATALOG_DOCUMENTS.SENSITIVE, true)
                .execute();
            dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0)
                .set(CATALOG_DOCUMENT_CHUNKS.CHASH, HexFormat.of().parseHex(chash(chunkText)))
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
                .execute();
        }
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

    private JsonNode ok(Map<String, Object> body) throws Exception {
        var r = post(body);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    /** The three-collection request; {@code route} adds a top1 route over every collection of the request. */
    private static Map<String, Object> request(List<String> collections, boolean route) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("query", QUERY);
        m.put("collections", collections);
        m.put("per_collection_k", 5);
        m.put("limit", 100);
        if (route) {
            m.put("topic_route", Map.of("mode", "top1"));
        }
        return m;
    }

    private static Map<String, Object> routed() {
        return request(List.of(ROUTED, FALLBACK, PLAIN), true);
    }

    private static List<String> statuses(JsonNode body) {
        List<String> out = new ArrayList<>();
        body.get("per_collection").forEach(n -> out.add(n.get("topic_route_status").asText()));
        return out;
    }

    private static List<String> contents(JsonNode results) {
        List<String> out = new ArrayList<>();
        results.forEach(n -> out.add(n.get("content").asText()));
        return out;
    }

    private static List<String> ids(JsonNode results) {
        List<String> out = new ArrayList<>();
        results.forEach(n -> out.add(n.get("id").asText()));
        return out;
    }

    private static List<String> texts(String collection, String prefix, int from, int to) {
        List<String> out = new ArrayList<>();
        for (int i = from; i < to; i++) out.add(text(collection, prefix, i));
        return out;
    }

    @SafeVarargs
    private static List<String> concat(List<String>... lists) {
        List<String> out = new ArrayList<>();
        for (List<String> l : lists) out.addAll(l);
        return out;
    }

    /**
     * The merged order of the routed request, nearest first: PLAIN's five (0.15 rad and up), ROUTED's five routed rows
     * (TA, 0.30 rad and up), FALLBACK's five (0.40 rad and up). Asserted in the baseline of every test that reads it.
     */
    private static List<String> mergedOrder() {
        return concat(texts(PLAIN, "p", 0, 5), texts(ROUTED, "ta", 0, 5), texts(FALLBACK, "f", 0, 5));
    }

    private JsonNode baseline() throws Exception {
        JsonNode b = ok(routed());
        assertThat(statuses(b)).as("non-vacuity: each status is on the wire").containsExactly("routed", "fallback", "no_topics");
        assertThat(contents(b.get("results"))).as("the routed merge: TA's rows, though TB's are nearer the query")
            .containsExactlyElementsOf(mergedOrder());
        return b;
    }

    private static float[] decodeEmbedding(String b64) {
        byte[] raw = java.util.Base64.getDecoder().decode(b64);
        float[] out = new float[raw.length / 4];
        java.nio.ByteBuffer.wrap(raw).order(java.nio.ByteOrder.LITTLE_ENDIAN).asFloatBuffer().get(out);
        return out;
    }

    // ── /version ──────────────────────────────────────────────────────────────

    @Test
    void version_advertisesTheCapability() throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/version").GET().build();
        var r = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = MAPPER.readTree(r.body());
        assertThat(body.get("vector_topic_route_supported").asBoolean()).isTrue();
        assertThat(body.get("vector_search_chash_allow_supported").asBoolean())
            .as("the flag is its own: the neighbouring one is untouched").isTrue();
    }

    // ── thresholds ────────────────────────────────────────────────────────────

    @Test
    void thresholds_dropRoutedAndFallbackRowsAsTheyDropUnscopedOnes_andTheCountersReadTheRoutedSubset() throws Exception {
        baseline();
        Map<String, Object> req = routed();
        Map<String, Object> thresholds = new LinkedHashMap<>();
        thresholds.put(ROUTED, 0.046);      // ta0 is 0.0447 away, ta1 0.0477
        thresholds.put(FALLBACK, 0.05);     // every fallback row is over 0.078 away
        thresholds.put(PLAIN, null);
        req.put("thresholds", thresholds);
        JsonNode body = ok(req);
        assertThat(statuses(body)).as("a threshold changes no status").containsExactly("routed", "fallback", "no_topics");
        assertThat(contents(body.get("results"))).containsExactlyElementsOf(
            concat(texts(PLAIN, "p", 0, 5), List.of(text(ROUTED, "ta", 0))));
        JsonNode r = body.get("per_collection").get(0);
        assertThat(r.get("raw_count").asInt()).isEqualTo(5);
        assertThat(r.get("dropped").asInt()).isEqualTo(4);
        assertThat(r.get("min_raw_distance").asDouble()).isCloseTo(1 - Math.cos(0.30), within(1e-4));
        assertThat(r.get("min_dropped_distance").asDouble()).isCloseTo(1 - Math.cos(0.31), within(1e-4));
        JsonNode f = body.get("per_collection").get(1);
        assertThat(f.get("raw_count").asInt()).isEqualTo(5);
        assertThat(f.get("dropped").asInt()).isEqualTo(5);
        assertThat(body.get("per_collection").get(2).get("dropped").asInt()).isZero();
    }

    // ── lexical ───────────────────────────────────────────────────────────────

    @Test
    void lexical_theArmIsNotRouted_itsRowsOutsideTheKeptTopicMergeAsToday() throws Exception {
        Map<String, Object> routedLex = request(List.of(ROUTED), true);
        routedLex.put("query", LEX_QUERY);
        routedLex.put("lexical", true);
        Map<String, Object> plainLex = request(List.of(ROUTED), false);
        plainLex.put("query", LEX_QUERY);
        plainLex.put("lexical", true);
        Map<String, Object> routedNoLex = request(List.of(ROUTED), true);
        routedNoLex.put("query", LEX_QUERY);

        JsonNode withRoute = ok(routedLex);
        JsonNode without = ok(plainLex);
        String lx = chash(ROUTED + " " + LEX_QUERY + " lx");
        assertThat(statuses(withRoute)).containsExactly("routed");
        assertThat(ids(ok(routedNoLex).get("results"))).as("non-vacuity: without the lexical arm the routed rows hold no lx")
            .doesNotContain(lx).containsExactlyElementsOf(textHashes(ROUTED, "ta", 0, 5));
        assertThat(ids(without.get("results"))).as("non-vacuity: unrouted, the vector rows are TB's and lx follows them")
            .containsExactlyElementsOf(concat(textHashes(ROUTED, "tb", 0, 5), List.of(lx)));

        JsonNode routedEntry = withRoute.get("per_collection").get(0);
        JsonNode plainEntry = without.get("per_collection").get(0);
        assertThat(routedEntry.get("lexical_ids")).as("the lexical arm is the unrouted one: same ids")
            .isEqualTo(plainEntry.get("lexical_ids"));
        assertThat(strings(routedEntry.get("lexical_ids"))).containsExactly(lx);
        assertThat(routedEntry.get("lexical_count").asInt()).isEqualTo(1);
        assertThat(ids(withRoute.get("results"))).as("TA's rows from the routed vector arm, then the lexical row of TB")
            .containsExactlyElementsOf(concat(textHashes(ROUTED, "ta", 0, 5), List.of(lx)));
        assertThat(withRoute.get("lexical").asBoolean()).isTrue();
    }

    private static List<String> textHashes(String collection, String prefix, int from, int to) {
        return texts(collection, prefix, from, to).stream().map(VectorHandlerSearchPerCollectionTopicRouteFieldsTest::chash).toList();
    }

    private static List<String> strings(JsonNode array) {
        List<String> out = new ArrayList<>();
        array.forEach(n -> out.add(n.asText()));
        return out;
    }

    // ── rerank ────────────────────────────────────────────────────────────────

    @Test
    void rerank_reranksTheMergedRows_routedFallbackAndUnscopedAlike() throws Exception {
        JsonNode base = baseline();
        reranker.reset();
        // an order no distance sort produces: a fallback row, a routed row, a plain row, another routed row, ...
        List<String> preferred = List.of(text(FALLBACK, "f", 1), text(ROUTED, "ta", 2), text(PLAIN, "p", 4),
                                         text(ROUTED, "ta", 0), text(FALLBACK, "f", 3));
        reranker.preferred = preferred;
        Map<String, Object> req = routed();
        req.put("rerank", true);
        JsonNode body = ok(req);

        assertThat(reranker.calls.get()).as("one rerank call over the merged rows").isEqualTo(1);
        assertThat(reranker.lastDocuments).as("the reranker is handed every merged row: routed, fallback and unscoped")
            .containsExactlyElementsOf(mergedOrder());
        assertThat(body.get("rerank_degraded").asBoolean()).isFalse();
        assertThat(body.get("rerank_model").asText()).isEqualTo("stub-rerank");
        List<String> rest = new ArrayList<>(mergedOrder());
        rest.removeAll(preferred);
        assertThat(contents(body.get("results"))).as("the stub's order, then the zero-scored rows in distance order")
            .containsExactlyElementsOf(concat(preferred, rest));
        for (JsonNode row : body.get("results")) {
            assertThat(row.has("rerank_score")).as(row.get("content").asText()).isTrue();
        }
        assertThat(statuses(body)).as("rerank touches no status").containsExactly("routed", "fallback", "no_topics");
        assertThat(contents(base.get("results"))).as("non-vacuity: the unreranked order differs from the reranked one")
            .isNotEqualTo(contents(body.get("results")));
    }

    // ── include_embeddings, content_chars, include_source_uri, embeddings_limit ──

    @Test
    void rowFields_includeEmbeddingsContentCharsAndSourceUri_applyToEveryRow_routedFallbackAndUnscoped() throws Exception {
        JsonNode base = baseline();
        Map<String, Object> req = routed();
        req.put("include_embeddings", true);
        req.put("content_chars", 6);
        req.put("include_source_uri", true);
        JsonNode body = ok(req);

        assertThat(body.get("embedding_encoding").asText()).isEqualTo("f32-le-b64");
        assertThat(body.get("embedding_dim").asInt()).isEqualTo(384);
        assertThat(body.get("content_chars").asInt()).isEqualTo(6);
        assertThat(body.get("results")).hasSize(15);
        assertThat(statuses(body)).containsExactly("routed", "fallback", "no_topics");
        for (int i = 0; i < 15; i++) {
            JsonNode row = body.get("results").get(i);
            String full = base.get("results").get(i).get("content").asText();
            String collection = row.get("collection").asText();
            assertThat(row.get("id").asText()).as("the rows and their order are the routed merge's")
                .isEqualTo(base.get("results").get(i).get("id").asText());
            assertThat(row.get("content").asText()).as("content_chars on %s", full).isEqualTo(full.substring(0, 6));
            assertThat(row.get("source_uri").asText()).as("include_source_uri on %s", full).isEqualTo("file:///" + collection);
            assertThat(row.has("embedding_b64")).as("include_embeddings on %s", full).isTrue();
            assertThat(decodeEmbedding(row.get("embedding_b64").asText())).as("the stored vector of %s", full)
                .containsExactly(embedder.embed(List.of(full)).get(0));
        }
    }

    @Test
    void embeddingsLimit_keepsTheVectorOnTheFirstNRowsOfTheMergedOrder_routedRowsIncluded() throws Exception {
        baseline();
        Map<String, Object> req = routed();
        req.put("include_embeddings", true);
        req.put("embeddings_limit", 7);     // PLAIN's five, then two of ROUTED's routed rows
        JsonNode results = ok(req).get("results");
        assertThat(results).as("the rows past the limit stay").hasSize(15);
        for (int i = 0; i < 15; i++) {
            assertThat(results.get(i).has("embedding_b64")).as("row %d of 15, embeddings_limit 7", i).isEqualTo(i < 7);
        }
        assertThat(results.get(6).get("collection").asText()).as("the last vector is a routed row's").isEqualTo(ROUTED);
    }

    // ── rerank_top_k, rerank_max_candidates, limit ────────────────────────────

    @Test
    void limit_cutsTheMergedRows_andIsEchoed() throws Exception {
        baseline();
        Map<String, Object> req = routed();
        req.put("limit", 7);
        JsonNode body = ok(req);
        assertThat(contents(body.get("results"))).as("the nearest seven of the merge: PLAIN's five, two routed rows")
            .containsExactlyElementsOf(mergedOrder().subList(0, 7));
        assertThat(body.get("limit").asInt()).isEqualTo(7);
        assertThat(body.get("per_collection_k").asInt()).isEqualTo(5);
        for (JsonNode entry : body.get("per_collection")) {
            assertThat(entry.get("raw_count").asInt()).as("the cut is after each arm, not in it").isEqualTo(5);
        }
        assertThat(statuses(body)).containsExactly("routed", "fallback", "no_topics");
    }

    @Test
    void rerankTopK_cutsTheRerankedRows() throws Exception {
        baseline();
        reranker.reset();
        reranker.preferred = List.of(text(FALLBACK, "f", 1), text(ROUTED, "ta", 2), text(PLAIN, "p", 4));
        Map<String, Object> req = routed();
        req.put("rerank", true);
        req.put("rerank_top_k", 3);
        JsonNode body = ok(req);
        assertThat(reranker.lastTopK).isEqualTo(3);
        assertThat(reranker.lastDocuments).as("top_k bounds what comes back, not what is scored").hasSize(15);
        assertThat(contents(body.get("results"))).containsExactlyElementsOf(reranker.preferred);
        assertThat(body.get("limit").asInt()).isEqualTo(100);
    }

    @Test
    void rerankMaxCandidates_scoresOnlyTheFirstNMergedRows_theRestFollowUnscored() throws Exception {
        baseline();
        reranker.reset();
        // the first seven of the merge are PLAIN's five and two routed rows of ROUTED
        reranker.preferred = List.of(text(ROUTED, "ta", 1), text(PLAIN, "p", 3));
        Map<String, Object> req = routed();
        req.put("rerank", true);
        req.put("rerank_max_candidates", 7);
        JsonNode body = ok(req);
        List<String> merged = mergedOrder();
        assertThat(reranker.lastDocuments).as("only the nearest seven reach the scorer")
            .containsExactlyElementsOf(merged.subList(0, 7));
        JsonNode results = body.get("results");
        assertThat(results).as("every row is still in the result").hasSize(15);
        List<String> expectedFirst = new ArrayList<>(reranker.preferred);
        expectedFirst.addAll(merged.subList(0, 7).stream().filter(t -> !reranker.preferred.contains(t)).toList());
        assertThat(contents(results).subList(0, 7)).containsExactlyElementsOf(expectedFirst);
        assertThat(contents(results).subList(7, 15)).as("the unscored rows, in the merge's order")
            .containsExactlyElementsOf(merged.subList(7, 15));
        for (int i = 0; i < 15; i++) {
            assertThat(results.get(i).has("rerank_score")).as("row %d", i).isEqualTo(i < 7);
        }
    }

    // ── exclude_sensitive (wire field: sensitivity) ───────────────────────────

    @Test
    void sensitivityExclude_isAppliedInsideTheRoutedSearchAndInsideItsFallback() throws Exception {
        List<String> cols = List.of(S_ROUTED, S_FALLBACK, S_PLAIN);
        JsonNode open = ok(request(cols, true));
        assertThat(statuses(open)).containsExactly("routed", "fallback", "no_topics");
        List<String> sensitive = List.of(text(S_ROUTED, "ta", 0), text(S_FALLBACK, "f", 0), text(S_PLAIN, "p", 0));
        assertThat(contents(open.get("results"))).as("non-vacuity: without the flag the three sensitive chunks come back")
            .containsAll(sensitive);
        for (JsonNode row : open.get("results")) {
            assertThat(row.get("sensitive").asBoolean()).as(row.get("content").asText())
                .isEqualTo(sensitive.contains(row.get("content").asText()));
        }

        Map<String, Object> req = request(cols, true);
        req.put("sensitivity", "exclude");
        JsonNode body = ok(req);
        assertThat(statuses(body)).as("the exclusion does not change what the route did")
            .containsExactly("routed", "fallback", "no_topics");
        assertThat(contents(body.get("results"))).doesNotContainAnyElementsOf(sensitive);
        assertThat(contents(body.get("results"))).as("the next rows of each collection, from the kept topic or unscoped")
            .containsExactlyInAnyOrderElementsOf(concat(texts(S_ROUTED, "ta", 1, 6), texts(S_FALLBACK, "f", 1, 6),
                                                       texts(S_PLAIN, "p", 1, 6)));
        for (JsonNode row : body.get("results")) {
            assertThat(row.get("sensitive").asBoolean()).isFalse();
        }
    }

    @Test
    void sensitivityExclude_onAKeptTopicOfOnlySensitiveChunks_fallsBackAndTheFallbackExcludesToo() throws Exception {
        List<String> cols = List.of(S_ALLSENS);
        List<String> sensitive = texts(S_ALLSENS, "sa", 0, 2);

        JsonNode open = ok(request(cols, true));
        assertThat(statuses(open)).as("non-vacuity: without the flag the kept topic answers").containsExactly("routed");
        assertThat(contents(open.get("results"))).as("non-vacuity: and its rows are the two sensitive chunks")
            .containsExactlyElementsOf(sensitive);

        Map<String, Object> req = request(cols, true);
        req.put("sensitivity", "exclude");
        JsonNode body = ok(req);
        assertThat(statuses(body)).as("the routed search found no row once the sensitive ones were excluded")
            .containsExactly("fallback");
        assertThat(contents(body.get("results"))).as("the unscoped fallback excludes them too")
            .doesNotContainAnyElementsOf(sensitive);
        assertThat(contents(body.get("results"))).as("and fills k from the clean chunks of the other topic")
            .containsExactlyElementsOf(texts(S_ALLSENS, "sb", 0, 5));
        assertThat(body.get("per_collection").get(0).get("raw_count").asInt()).isEqualTo(5);
        for (JsonNode row : body.get("results")) {
            assertThat(row.get("sensitive").asBoolean()).isFalse();
        }
    }
}
