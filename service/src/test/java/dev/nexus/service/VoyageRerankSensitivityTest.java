// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.Reranker;
import dev.nexus.service.vectors.VoyageReranker;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
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
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CopyOnWriteArrayList;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-rfvyy.8.4 (RDR-243 Phase 3 Step 3.3, Q4): the engine keeps sensitive candidates out of the Voyage
 * reranker; they keep their vector order in the merged result.
 *
 * <p>Real pgvector rows under RLS, a scripted fake Voyage upstream that records every request body, the real
 * HTTP surface. The fixture's candidates, nearest the query first (S = owned by a sensitive document):
 * {@code A(S) B C D(S) E}. The fake scores are scripted so that reranking visibly reorders the non-sensitive
 * rows, so a skipped rerank cannot pass.
 *
 * <p>Merge order under test (slot-preserving, see {@code RerankStage}): a sensitive row keeps the index it had in
 * the distance-ordered input; the other slots are filled, in index order, by the scored rows (score descending),
 * then the unscored ones in input order; {@code top_k} cuts after the merge.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VoyageRerankSensitivityTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {};

    private static final String TOKEN = "rfvyy84-rerank-sensitivity-token-0123456789";
    private static final String TENANT = "rfvyy84-tenant";
    private static final String Q = "zebracorn";

    private static final String MIXED = "knowledge__rfvyy84mixed__voyage-context-3__v1";
    private static final String PLAIN = "knowledge__rfvyy84plain__voyage-context-3__v1";
    private static final String ALL_S = "knowledge__rfvyy84alls__voyage-context-3__v1";
    private static final String UNION = "knowledge__rfvyy84union__voyage-context-3__v1";

    /** Marker carried by every sensitive chunk's text; it must never appear in a request to the fake Voyage. */
    private static final String SECRET = "ZZSECRETZZ";

    private static final String TA = "chunk a " + SECRET;
    private static final String TB = "chunk b plain";
    private static final String TC = "chunk c plain";
    private static final String TD = "chunk d " + SECRET;
    private static final String TE = "chunk e plain";

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    HttpServer fakeVoyage;
    final ConcurrentLinkedQueue<String> voyageResponses = new ConcurrentLinkedQueue<>();
    final List<String> voyageRequests = new CopyOnWriteArrayList<>();

    NexusService svcVoyage;
    NexusService svcLocal;
    final List<List<String>> localDocs = new CopyOnWriteArrayList<>();
    HttpClient http;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "rfvyy84");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        embedder.register(Q, 1f, 0f);
        var repo = new PgVectorRepository(scope, embedder, embedder);

        fakeVoyage = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fakeVoyage.createContext("/v1/rerank", exchange -> {
            voyageRequests.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            String scripted = voyageResponses.poll();
            byte[] body = (scripted == null ? "{\"data\": []}" : scripted).getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, body.length);
            try (OutputStream os = exchange.getResponseBody()) { os.write(body); }
        });
        fakeVoyage.start();
        String fakeUrl = "http://127.0.0.1:" + fakeVoyage.getAddress().getPort() + "/v1/rerank";

        svcVoyage = new NexusService(0, TOKEN, ds, null, repo,
                new VoyageReranker("test-key", "rerank-2.5", fakeUrl, 10L, Optional.empty()));
        svcVoyage.start();
        // A reranker that sends nothing off the machine (the local cross-encoder's posture): records what it is
        // handed and scores the last document highest.
        Reranker local = new Reranker() {
            @Override public String modelToken() { return "local-fake"; }
            @Override public List<Scored> rerank(String query, List<String> documents, Integer topK) {
                localDocs.add(List.copyOf(documents));
                List<Scored> out = new ArrayList<>();
                for (int i = documents.size() - 1; i >= 0; i--) out.add(new Scored(i, 1.0 - 0.1 * (documents.size() - i)));
                return out;
            }
        };
        svcLocal = new NexusService(0, TOKEN, ds, null, repo, local);
        svcLocal.start();
        http = HttpClient.newHttpClient();

        // Burn the per-tenant ghost sweep before collections exist (see RerankStageIntegrationTest).
        http.send(HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + svcVoyage.getPort() + "/v1/catalog/collections/list"))
                .header("Authorization", "Bearer " + TOKEN).GET().build(), HttpResponse.BodyHandlers.ofString());

        // Vector order: a(0.0) b(0.3) c(0.6) d(0.9) e(1.2).
        seed(MIXED, linked(TA, 0.0, TB, 0.3, TC, 0.6, TD, 0.9, TE, 1.2), List.of(TA, TD));
        seed(PLAIN, linked(TB, 0.3, TC, 0.6, TE, 1.2), List.of());
        seed(ALL_S, linked(TA, 0.0, TD, 0.9), List.of(TA, TD));
        // Union: three plain vector rows, then two token-carrying rows only the lexical arm reaches.
        seed(UNION, linked("filler 0", 0.0, "filler 1", 0.05, "filler 2", 0.1,
                "token " + Q + " " + SECRET, 1.2, "token " + Q + " open", 1.3),
                List.of("token " + Q + " " + SECRET));
    }

    @AfterAll
    void stopAll() {
        if (svcVoyage != null) svcVoyage.stop();
        if (svcLocal != null) svcLocal.stop();
        if (fakeVoyage != null) fakeVoyage.stop(0);
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void reset() {
        voyageResponses.clear();
        voyageRequests.clear();
        localDocs.clear();
    }

    // ── fixture ───────────────────────────────────────────────────────────────

    private static Map<String, Double> linked(Object... kv) {
        Map<String, Double> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], (Double) kv[i + 1]);
        return m;
    }

    private static String chash(String text) {
        return Chash.ofText(text).toHex();
    }

    /** Seed {@code angles} (text to angle) into {@code collection}; the texts in {@code sensitive} belong to a sensitive document. */
    private void seed(String collection, Map<String, Double> angles, List<String> sensitive) throws Exception {
        List<String> hexes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        angles.forEach((text, theta) -> {
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            hexes.add(chash(text));
            texts.add(text);
            metas.add(Map.of());
        });
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(dsl, TENANT, collection);
            PgContainerHelper.insertChunks(dsl, TENANT, collection, hexes, texts, metas, embedder);
            String model = PgContainerHelper.collectionModel(dsl, TENANT, collection);
            for (boolean isSensitive : new boolean[] {false, true}) {
                String doc = (isSensitive ? "sens-" : "plain-") + collection;
                int position = 0;
                boolean any = false;
                for (String text : texts) {
                    if (sensitive.contains(text) != isSensitive) continue;
                    if (!any) {
                        dsl.insertInto(CATALOG_DOCUMENTS)
                            .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT).set(CATALOG_DOCUMENTS.TUMBLER, doc)
                            .set(CATALOG_DOCUMENTS.TITLE, doc).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection)
                            .set(CATALOG_DOCUMENTS.SENSITIVE, isSensitive)
                            .execute();
                        any = true;
                    }
                    dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                        .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, TENANT).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                        .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position++)
                        .set(CATALOG_DOCUMENT_CHUNKS.CHASH, Chash.ofText(text).toBytes())
                        .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection)
                        .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
                        .execute();
                }
            }
        }
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private Map<String, Object> postOk(NexusService svc, String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + svc.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as("%s (body: %s)", path, resp.body()).isEqualTo(200);
        return MAPPER.readValue(resp.body(), MAP_TYPE);
    }

    private void scriptVoyage(String entries) {
        voyageResponses.add("{\"object\": \"list\", \"data\": [" + entries + "], "
                + "\"model\": \"rerank-2.5\", \"usage\": {\"total_tokens\": 7}}");
    }

    private static String score(int index, double s) {
        return "{\"index\": " + index + ", \"relevance_score\": " + s + "}";
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> results(Map<String, Object> envelope) {
        return (List<Map<String, Object>>) envelope.get("results");
    }

    @SuppressWarnings("unchecked")
    private List<String> voyageDocs(int request) throws Exception {
        return (List<String>) MAPPER.readValue(voyageRequests.get(request), MAP_TYPE).get("documents");
    }

    private static List<String> ids(Map<String, Object> envelope, String... order) {
        return results(envelope).stream().map(r -> (String) r.get("id")).toList();
    }

    private static List<String> hexes(String... texts) {
        List<String> out = new ArrayList<>();
        for (String t : texts) out.add(chash(t));
        return out;
    }

    private static Map<String, Object> searchBody(String collection, Object... extra) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", Q);
        body.put("collections", List.of(collection));
        body.put("n_results", 5);
        for (int i = 0; i < extra.length; i += 2) body.put((String) extra[i], extra[i + 1]);
        return body;
    }

    private void assertNoSecretSentToVoyage() {
        assertThat(voyageRequests).as("a request reached the fake Voyage").isNotEmpty();
        for (String raw : voyageRequests) {
            assertThat(raw).as("raw Voyage request body").doesNotContain(SECRET);
        }
    }

    // ── tests ─────────────────────────────────────────────────────────────────

    @Test
    void sensitiveCandidatesNeverReachVoyage_andKeepTheirVectorSlots() throws Exception {
        // Voyage sees the three non-sensitive documents B, C, E (indexes 0, 1, 2). E scores highest.
        scriptVoyage(score(2, 0.9) + ", " + score(0, 0.5) + ", " + score(1, 0.1));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search", searchBody(MIXED, "rerank", true));

        assertNoSecretSentToVoyage();
        assertThat(voyageRequests).hasSize(1);
        assertThat(voyageDocs(0)).containsExactly(TB, TC, TE);
        assertThat(env.get("rerank_degraded")).isEqualTo(false);
        // Slots 0 (A) and 3 (D) are the sensitive rows' vector positions; the others follow the scores E, B, C.
        assertThat(ids(env)).containsExactly(chash(TA), chash(TE), chash(TB), chash(TD), chash(TC));
        var rows = results(env);
        assertThat(rows.get(0)).doesNotContainKey("rerank_score");
        assertThat(rows.get(3)).doesNotContainKey("rerank_score");
        assertThat(rows.get(0).get("sensitive")).isEqualTo(true);
        assertThat(rows.get(1).get("rerank_score")).isEqualTo(0.9);
        assertThat(rows.get(2).get("rerank_score")).isEqualTo(0.5);
        assertThat(rows.get(4).get("rerank_score")).isEqualTo(0.1);
    }

    @Test
    void topKCutsAfterTheMerge_soASensitiveTopHitIsNeverLost_andIsForwardedUnchanged() throws Exception {
        scriptVoyage(score(2, 0.9) + ", " + score(0, 0.5) + ", " + score(1, 0.1));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search",
                searchBody(MIXED, "rerank", true, "rerank_top_k", 3));

        assertNoSecretSentToVoyage();
        assertThat(MAPPER.readValue(voyageRequests.get(0), MAP_TYPE).get("top_k")).isEqualTo(3);
        assertThat(ids(env)).containsExactly(chash(TA), chash(TE), chash(TB));
    }

    @Test
    void sensitiveRowsUseNoMaxCandidatesSlot() throws Exception {
        // Cap 2: the two nearest NON-sensitive rows (B, C) are scored; E stays unscored; A and D keep their slots.
        scriptVoyage(score(1, 0.9) + ", " + score(0, 0.2));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search",
                searchBody(MIXED, "rerank", true, "rerank_max_candidates", 2));

        assertNoSecretSentToVoyage();
        assertThat(voyageDocs(0)).containsExactly(TB, TC);
        assertThat(ids(env)).containsExactly(chash(TA), chash(TC), chash(TB), chash(TD), chash(TE));
        assertThat(results(env).get(4)).doesNotContainKey("rerank_score");
    }

    @Test
    void controlWithNoSensitiveRows_sendsEveryDocument_andOrdersByScore() throws Exception {
        scriptVoyage(score(2, 0.9) + ", " + score(0, 0.5) + ", " + score(1, 0.1));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search", searchBody(PLAIN, "rerank", true));

        assertThat(voyageDocs(0)).containsExactly(TB, TC, TE);
        assertThat(ids(env)).containsExactly(chash(TE), chash(TB), chash(TC));
        assertThat(env.get("rerank_degraded")).isEqualTo(false);
        assertThat(MAPPER.readValue(voyageRequests.get(0), MAP_TYPE)).doesNotContainKey("top_k");
    }

    @Test
    void whenEveryRowIsSensitive_noVoyageCallIsMade_andTheDegradeNamesTheTrueReason() throws Exception {
        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search", searchBody(ALL_S, "rerank", true));

        assertThat(voyageRequests).as("no request may leave for Voyage").isEmpty();
        assertThat(env.get("rerank_degraded")).isEqualTo(true);
        assertThat((String) env.get("rerank_error")).contains("sensitive").doesNotContain("textless");
        assertThat(ids(env)).containsExactly(chash(TA), chash(TD));
    }

    @Test
    void aReranker_thatSendsNothingOffTheMachine_stillScoresSensitiveRows() throws Exception {
        Map<String, Object> env = postOk(svcLocal, "/v1/vectors/search", searchBody(MIXED, "rerank", true));

        assertThat(localDocs).hasSize(1);
        assertThat(localDocs.get(0)).containsExactly(TA, TB, TC, TD, TE);
        assertThat(results(env)).allSatisfy(r -> assertThat(r).containsKey("rerank_score"));
        assertThat(ids(env)).containsExactly(chash(TE), chash(TD), chash(TC), chash(TB), chash(TA));
    }

    @Test
    void lexicalUnion_doesNotSendASensitiveLexicalExtraToVoyage() throws Exception {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", Q);
        body.put("collections", List.of(UNION));
        body.put("per_collection_k", 3);
        body.put("limit", 3);
        body.put("lexical", true);
        body.put("rerank", true);
        // Call 1 scores the vector rows (filler 0, 1, 2); call 2 scores the one non-sensitive lexical extra.
        scriptVoyage(score(2, 0.9) + ", " + score(1, 0.5) + ", " + score(0, 0.1));
        scriptVoyage(score(0, 0.7));

        Map<String, Object> env = postOk(svcVoyage, "/v1/vectors/search-per-collection", body);

        assertNoSecretSentToVoyage();
        assertThat(voyageRequests).hasSize(2);
        assertThat(voyageDocs(1)).containsExactly("token " + Q + " open");
        // Input order: filler 0, 1, 2, then the lexical extras (secret, open). The secret row keeps slot 3.
        String secret = chash("token " + Q + " " + SECRET);
        assertThat(ids(env)).containsExactly(chash("filler 2"), chash("token " + Q + " open"), chash("filler 1"),
                secret, chash("filler 0"));
        assertThat(results(env).get(3)).doesNotContainKey("rerank_score");
    }
}
