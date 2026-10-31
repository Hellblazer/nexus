// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
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
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-jypmj (RDR-232): the optional {@code chash_allow} array on the vector search routes. The request names
 * the chunk hashes a search may return; the engine restricts the candidates to them, within the requested
 * collections and the tenant's RLS, and ranks them EXACTLY.
 *
 * <p><strong>The fixture is built so that an HNSW search cannot pass.</strong> One collection holds
 * {@link #NEAR_ROWS} rows in a tight cluster around the query and {@link #FAR_ROWS} rows on the far side; the
 * HNSW walk is capped at 16 tuples with an ef_search of 200 and every non-HNSW plan is penalised at connection
 * level (as {@code PgVectorCardinalityRouterIntegrationTest} does). A statement that walked the index to find
 * the far rows would see the near cluster first and starve, returning nothing or the wrong rows. The exact
 * route re-enables the plans it needs itself, so the penalties cannot mask it. Every expectation is brute
 * force over the angles the rows were seeded at, so the assertion is "the same rows in the same order as the
 * exact rank", never "some rows came back".
 *
 * <p>Runs as {@code nexus_svc} (NOBYPASSRLS), over the real HTTP handler.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorChashAllowIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String TOKEN = "tok-jypmj-chash-allow-0123456789abcdef";
    private static final String TOKEN2 = "tok-jypmj-chash-allow-other-0123456789ab";
    private static final String TENANT = "jypmj-chash-allow";
    private static final String TENANT2 = "jypmj-chash-allow-2";
    private static final String BIG = "knowledge__jypmj-big__minilm-l6-v2-384__v1";
    private static final String SIDE = "knowledge__jypmj-side__minilm-l6-v2-384__v1";
    private static final String QUERY = "jypmj plain query";
    /** The hybrid-search query: also the gate word, so only the rows whose text carries it pass the gate. */
    private static final String GATE = "zebracorn";
    private static final int NEAR_ROWS = 3000;
    private static final int FAR_ROWS = 40;
    private static final int SCAN_CAP = 16;
    private static final int EF_FLOOR = 200;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepository repo;
    NexusService service;
    HttpClient http;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;

    /** chash hex to the angle it was seeded at: cosine distance to the query (1, 0) is 1 - cos(theta). */
    final Map<String, Double> angleOf = new LinkedHashMap<>();
    /** chash hex to the text it was seeded with. */
    final Map<String, String> textOf = new LinkedHashMap<>();
    final List<String> farBig = new ArrayList<>();
    /** Near rows spaced far enough apart that float4 distances cannot tie, so the exact order is unambiguous. */
    final List<String> spacedNear = new ArrayList<>();
    final List<String> spacedNearGate = new ArrayList<>();
    String sideRow;
    String sideOnlyRow;
    String tenant2OnlyRow;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, TOKEN, TENANT, "jypmj-chash-allow");
            PgContainerHelper.seedServiceToken(dsl, TOKEN2, TENANT2, "jypmj-chash-allow-2");
            PgContainerHelper.insertCollection(dsl, TENANT, BIG);
            PgContainerHelper.insertCollection(dsl, TENANT, SIDE);
            PgContainerHelper.insertCollection(dsl, TENANT2, BIG);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        // Penalise every non-HNSW plan so an index-ordered statement is what the planner takes. The exact route
        // re-enables bitmap, seq and sort itself (PgSession.disableIndexScanForExactFallback).
        cfg.addDataSourceProperty("options",
            "-c enable_seqscan=off -c enable_bitmapscan=off -c enable_sort=off");
        ds = new HikariDataSource(cfg);
        PgSession.overrideScanBudgetForTests(SCAN_CAP, 1);
        PgSession.overrideEfSearchFloorForTests(EF_FLOOR);
        // The cardinality router sends a collection this small down the exact route by itself, which would let a
        // search that never forced it pass. Off, the plain search takes the HNSW route here, as a large
        // collection's does in production.
        PgSession.overrideSearchExactMaxRowsForTests(0);
        scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);
        embedder.register(QUERY, 1f, 0f);
        embedder.register(GATE, 1f, 0f);

        // The near cluster: angles 0.00 .. 0.60, half the rows carry the gate word.
        List<String> nearIds = new ArrayList<>();
        List<String> nearTexts = new ArrayList<>();
        for (int i = 0; i < NEAR_ROWS; i++) {
            double theta = i * (0.6 / (NEAR_ROWS - 1));
            String text = (i % 2 == 0 ? "near " + GATE + " " : "near plain ") + i;
            nearIds.add(register(TENANT, BIG, text, theta));
            nearTexts.add(text);
            // Rows 100, 200, ... (they carry the gate word) and 151, 251, ... (they do not): at least 0.01 apart
            // in angle, so their float4 distances differ by 3e-4 or more and the exact order is unambiguous.
            if (i >= 100 && i % 100 == 0) {
                spacedNear.add(nearIds.get(i));
                spacedNearGate.add(nearIds.get(i));
            } else if (i >= 151 && i % 100 == 51) {
                spacedNear.add(nearIds.get(i));
            }
        }
        upsertAndOwn(TENANT, BIG, nearIds, nearTexts);
        // The far side: angles 2.50 .. 2.89, every row carries the gate word.
        List<String> farIds = new ArrayList<>();
        List<String> farTexts = new ArrayList<>();
        for (int j = 0; j < FAR_ROWS; j++) {
            String text = "far " + GATE + " " + j;
            String id = register(TENANT, BIG, text, 2.5 + j * 0.01);
            farIds.add(id);
            farTexts.add(text);
        }
        upsertAndOwn(TENANT, BIG, farIds, farTexts);
        farBig.addAll(farIds);

        // A second collection of the same tenant: a row only it holds, and a row of the same text as a BIG row.
        sideOnlyRow = register(TENANT, SIDE, "side only " + GATE, 1.0);
        sideRow = register(TENANT, SIDE, "side shared " + GATE, 1.5);
        upsertAndOwn(TENANT, SIDE, List.of(sideOnlyRow, sideRow), List.of("side only " + GATE, "side shared " + GATE));
        // Another tenant's collection of the same NAME, holding a row of its own and the same text as a BIG row.
        tenant2OnlyRow = register(TENANT2, BIG, "tenant two only " + GATE, 0.9);
        String sharedText = nearTexts.get(100);
        embedder.register(sharedText, (float) Math.cos(100 * (0.6 / (NEAR_ROWS - 1))),
                          (float) Math.sin(100 * (0.6 / (NEAR_ROWS - 1))));
        upsertAndOwn(TENANT2, BIG, List.of(tenant2OnlyRow, nearIds.get(100)),
                     List.of("tenant two only " + GATE, sharedText));

        service = new NexusService(0, TOKEN, ds, null, repo);
        service.start();
        http = TestHttp.client();
    }

    /** Register the text's vector at {@code theta}; remember it; return the chash hex. */
    private String register(String tenant, String collection, String text, double theta) {
        embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
        String chash = dev.nexus.service.db.Chash.ofText(text).toHex();
        angleOf.put(chash, theta);
        textOf.put(chash, text);
        return chash;
    }

    private void upsertAndOwn(String tenant, String collection, List<String> ids, List<String> texts) {
        for (int from = 0; from < ids.size(); from += 300) {
            int to = Math.min(ids.size(), from + 300);
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int i = from; i < to; i++) metas.add(Map.of());
            repo.upsertChunks(tenant, collection, ids.subList(from, to), texts.subList(from, to), metas);
        }
        scope.withTenant(tenant, ctx -> {
            for (int from = 0; from < ids.size(); from += 1000) {
                int to = Math.min(ids.size(), from + 1000);
                PgContainerHelper.ownChunks(ctx, tenant, collection,
                                            ids.subList(from, to).toArray(new String[0]));
            }
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        PgSession.resetScanBudgetForTests();
        PgSession.resetEfSearchFloorForTests();
        PgSession.resetSearchExactMaxRowsForTests();
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<String> post(String path, String token, Map<String, Object> body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> post(String path, Map<String, Object> body) throws Exception {
        return post(path, TOKEN, body);
    }

    private Map<String, Object> searchBody(String query, List<String> collections, int n, Object allow) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("query", query);
        m.put("collections", collections);
        m.put("n_results", n);
        if (allow != null) m.put("chash_allow", allow);
        return m;
    }

    private List<String> ids(HttpResponse<String> r) throws Exception {
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode root = MAPPER.readTree(r.body());
        JsonNode rows = root.isArray() ? root : root.get("results");
        List<String> out = new ArrayList<>();
        for (JsonNode row : rows) out.add(row.get("id").asText());
        return out;
    }

    /** The exact rank over {@code allowed}: ascending distance to the query (1, 0), ties by chash hex. */
    private List<String> exactTopK(List<String> allowed, int k) {
        return allowed.stream()
            .sorted(Comparator.<String>comparingDouble(c -> 1.0 - Math.cos(angleOf.get(c))).thenComparing(c -> c))
            .limit(k)
            .collect(Collectors.toList());
    }

    // ── the exact rank ────────────────────────────────────────────────────────

    @Test
    void search_returnsOnlyTheAllowedRows_rankedExactly_includingRowsAnHnswWalkNeverReaches() throws Exception {
        // The allowed set is the FAR rows alone: 3000 near rows sit between the query and every one of them.
        var r = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 10, farBig));
        assertThat(ids(r)).isEqualTo(exactTopK(farBig, 10));

        // Mixed: the far rows and a spaced sample of the near cluster, ranked as one set.
        List<String> mixed = new ArrayList<>(farBig);
        mixed.addAll(spacedNear);
        var r2 = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 200, mixed));
        assertThat(ids(r2)).isEqualTo(exactTopK(mixed, 200));
        assertThat(ids(r2)).as("the far rows are in the answer, after the near ones").containsAll(farBig);
        assertThat(ids(r2)).hasSize(mixed.size());
    }

    @Test
    void search_nResultsCutsTheExactRank_andTheRowShapeIsTheUnrestrictedShape() throws Exception {
        var restricted = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 3, farBig));
        var plain = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 3, null));
        JsonNode a = MAPPER.readTree(restricted.body()).get(0);
        JsonNode b = MAPPER.readTree(plain.body()).get(0);
        assertThat(ids(restricted)).hasSize(3);
        List<String> aFields = new ArrayList<>();
        a.fieldNames().forEachRemaining(aFields::add);
        List<String> bFields = new ArrayList<>();
        b.fieldNames().forEachRemaining(bFields::add);
        assertThat(aFields).as("the same row keys, in the same order").isEqualTo(bFields);
    }

    @Test
    void query_isTheSameRouteAsSearch() throws Exception {
        var r = post("/v1/vectors/query", searchBody(QUERY, List.of(BIG), 10, farBig));
        assertThat(ids(r)).isEqualTo(exactTopK(farBig, 10));
    }

    @Test
    void search_anAllowedHashThatDoesNotExistIsSimplyAbsent() throws Exception {
        List<String> allow = new ArrayList<>(farBig.subList(0, 3));
        allow.add(dev.nexus.service.db.Chash.ofText("no such chunk anywhere").toHex());
        var r = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 10, allow));
        assertThat(ids(r)).isEqualTo(exactTopK(farBig.subList(0, 3), 10));
    }

    // ── scoping: collection, tenant ───────────────────────────────────────────

    @Test
    void anAllowedHashInAnotherCollectionOfTheTenantIsNotReturned() throws Exception {
        var notRequested = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 10, List.of(sideOnlyRow)));
        assertThat(ids(notRequested)).isEmpty();
        var requested = post("/v1/vectors/search", searchBody(QUERY, List.of(SIDE), 10, List.of(sideOnlyRow)));
        assertThat(ids(requested)).containsExactly(sideOnlyRow);
    }

    @Test
    void anAllowedHashOfAnotherTenantsCollectionOfTheSameNameIsNotReturned() throws Exception {
        // tenant two holds tenant2OnlyRow in a collection named BIG; tenant one has no such row in BIG.
        var mine = post("/v1/vectors/search", TOKEN, searchBody(QUERY, List.of(BIG), 10, List.of(tenant2OnlyRow)));
        assertThat(ids(mine)).as("RLS: another tenant's chunk is not mine").isEmpty();
        var theirs = post("/v1/vectors/search", TOKEN2, searchBody(QUERY, List.of(BIG), 10, List.of(tenant2OnlyRow)));
        assertThat(ids(theirs)).containsExactly(tenant2OnlyRow);
    }

    // ── hybrid-search: both sides ─────────────────────────────────────────────

    @Test
    void hybridSearch_requiresTheGateAndTheAllowList_rankedExactly() throws Exception {
        // Allowed: the far rows (all carry the gate word), the spaced near rows with the word, and the spaced
        // near rows WITHOUT it. The gate must drop the last group; the allow-list must drop every other near row.
        List<String> allow = new ArrayList<>(farBig);
        allow.addAll(spacedNear);
        List<String> expected = exactTopK(
            allow.stream().filter(c -> textOf.get(c).contains(GATE)).collect(Collectors.toList()), 100);
        assertThat(spacedNear).as("the fixture has allowed rows the gate must drop")
            .anyMatch(c -> !textOf.get(c).contains(GATE));
        var r = post("/v1/vectors/hybrid-search", searchBody(GATE, List.of(BIG), 100, allow));
        assertThat(ids(r)).isEqualTo(expected);

        // The far rows alone, through the gate: exact, though 1500 gate-passing near rows are nearer.
        var far = post("/v1/vectors/hybrid-search", searchBody(GATE, List.of(BIG), 5, farBig));
        assertThat(ids(far)).isEqualTo(exactTopK(farBig, 5));

        // The gate side is restricted too: a gate-passing row that is not allowed does not come back.
        assertThat(ids(r)).doesNotContain(nearGateRowNotIn(allow));
    }

    @Test
    void hybridSearch_collectionAndTenantScopingStillApply() throws Exception {
        // Both rows carry the gate word and are allowed; neither is in a collection this tenant asked for.
        var otherCollection = post("/v1/vectors/hybrid-search", searchBody(GATE, List.of(BIG), 10, List.of(sideOnlyRow)));
        assertThat(ids(otherCollection)).isEmpty();
        var otherTenant = post("/v1/vectors/hybrid-search", TOKEN,
                               searchBody(GATE, List.of(BIG), 10, List.of(tenant2OnlyRow)));
        assertThat(ids(otherTenant)).isEmpty();
        var mine = post("/v1/vectors/hybrid-search", searchBody(GATE, List.of(SIDE), 10, List.of(sideOnlyRow)));
        assertThat(ids(mine)).containsExactly(sideOnlyRow);
        var theirs = post("/v1/vectors/hybrid-search", TOKEN2,
                          searchBody(GATE, List.of(BIG), 10, List.of(tenant2OnlyRow)));
        assertThat(ids(theirs)).containsExactly(tenant2OnlyRow);
    }

    private String nearGateRowNotIn(List<String> allow) {
        return textOf.entrySet().stream()
            .filter(e -> e.getValue().startsWith("near " + GATE) && !allow.contains(e.getKey()))
            .map(Map.Entry::getKey).findFirst().orElseThrow();
    }

    // ── search-per-collection: vector arm and lexical arm ─────────────────────

    @Test
    void searchPerCollection_confinesBothArmsToTheAllowList() throws Exception {
        // The far rows alone would be rescued by the empty-result exact re-run an HNSW arm has; a sample of the
        // near cluster beside them makes the HNSW arm answer something, and miss the far rows.
        List<String> allow = new ArrayList<>(farBig.subList(0, 6));
        allow.addAll(spacedNear);
        allow.add(sideRow);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", GATE);
        body.put("collections", List.of(BIG, SIDE));
        body.put("per_collection_k", 100);
        body.put("limit", 200);
        body.put("lexical", true);
        body.put("chash_allow", allow);
        var r = post("/v1/vectors/search-per-collection", body);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode root = MAPPER.readTree(r.body());
        List<String> got = new ArrayList<>();
        for (JsonNode row : root.get("results")) got.add(row.get("id").asText());
        assertThat(got).as("the vector arm's rows and the lexical arm's extras").containsExactlyInAnyOrderElementsOf(allow);
        // The merged rows are in exact distance order across collections.
        List<String> vectorOrder = got.subList(0, allow.size());
        assertThat(vectorOrder).isEqualTo(exactTopK(allow, allow.size()));
        for (JsonNode pc : root.get("per_collection")) {
            assertThat(pc.get("error").isNull()).as(pc.toString()).isTrue();
            assertThat(pc.get("lexical_error").isNull()).as(pc.toString()).isTrue();
            for (JsonNode lid : pc.get("lexical_ids")) assertThat(allow).contains(lid.asText());
        }
        assertThat(root.get("per_collection").get(0).get("raw_count").asInt()).isEqualTo(6 + spacedNear.size());
        assertThat(root.get("per_collection").get(1).get("raw_count").asInt()).isEqualTo(1);
    }

    // ── absent field, null, empty ─────────────────────────────────────────────

    @Test
    void anAbsentFieldIsTheUnrestrictedSearch_andANullFieldIsTheSameRequest() throws Exception {
        Map<String, Object> absent = searchBody(QUERY, List.of(BIG), 7, null);
        Map<String, Object> explicitNull = searchBody(QUERY, List.of(BIG), 7, null);
        explicitNull.put("chash_allow", null);
        var a = post("/v1/vectors/search", absent);
        var b = post("/v1/vectors/search", explicitNull);
        assertThat(b.body()).isEqualTo(a.body());
        // The unrestricted answer is the near cluster, not a far row: the field changed nothing it was not sent.
        assertThat(ids(a)).doesNotContainAnyElementsOf(farBig);
        assertThat(ids(a)).hasSize(7);
        // Same through hybrid-search and the fan-out.
        var ha = post("/v1/vectors/hybrid-search", searchBody(GATE, List.of(BIG), 7, null));
        Map<String, Object> hn = searchBody(GATE, List.of(BIG), 7, null);
        hn.put("chash_allow", null);
        assertThat(post("/v1/vectors/hybrid-search", hn).body()).isEqualTo(ha.body());
    }

    @Test
    void anEmptyArrayReturnsNoResultsOnEveryRoute() throws Exception {
        assertThat(ids(post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 10, List.of())))).isEmpty();
        assertThat(ids(post("/v1/vectors/query", searchBody(QUERY, List.of(BIG), 10, List.of())))).isEmpty();
        assertThat(ids(post("/v1/vectors/hybrid-search", searchBody(GATE, List.of(BIG), 10, List.of())))).isEmpty();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", QUERY);
        body.put("collections", List.of(BIG));
        body.put("per_collection_k", 10);
        body.put("limit", 10);
        body.put("lexical", true);
        body.put("chash_allow", List.of());
        var r = post("/v1/vectors/search-per-collection", body);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode root = MAPPER.readTree(r.body());
        assertThat(root.get("results")).isEmpty();
        assertThat(root.get("per_collection")).hasSize(1);
    }

    // ── validation ────────────────────────────────────────────────────────────

    private void assertRefusedNamingTheField(Object allow) throws Exception {
        var r = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 10, allow));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(400);
        assertThat(MAPPER.readTree(r.body()).get("error").asText()).contains("chash_allow");
    }

    @Test
    void malformedValuesAre400_namingTheField() throws Exception {
        String good = farBig.get(0);
        assertRefusedNamingTheField("not-an-array");
        assertRefusedNamingTheField(Map.of("a", "b"));
        assertRefusedNamingTheField(List.of(good, 7));
        assertRefusedNamingTheField(java.util.Arrays.asList(good, null));
        assertRefusedNamingTheField(List.of(good.toUpperCase()));
        assertRefusedNamingTheField(List.of(good.substring(0, 32)));
        assertRefusedNamingTheField(List.of(good + "0"));
        assertRefusedNamingTheField(List.of("g".repeat(64)));
        // The same refusal on the other three routes that take the field.
        for (String path : List.of("/v1/vectors/hybrid-search", "/v1/vectors/query")) {
            var r = post(path, searchBody(QUERY, List.of(BIG), 10, List.of("zz")));
            assertThat(r.statusCode()).as(path + " " + r.body()).isEqualTo(400);
            assertThat(r.body()).contains("chash_allow");
        }
        Map<String, Object> per = new LinkedHashMap<>();
        per.put("query", QUERY);
        per.put("collections", List.of(BIG));
        per.put("per_collection_k", 5);
        per.put("limit", 5);
        per.put("chash_allow", List.of("zz"));
        var r = post("/v1/vectors/search-per-collection", per);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(400);
        assertThat(r.body()).contains("chash_allow");
    }

    private static List<String> distinctHashes(int n) {
        List<String> out = new ArrayList<>(n);
        for (int i = 0; i < n; i++) out.add(dev.nexus.service.db.Chash.ofText("filler " + i).toHex());
        return out;
    }

    @Test
    void moreThan3000DistinctValuesIs400_and3000DistinctIsServed_duplicatesCountOnce() throws Exception {
        assertRefusedNamingTheField(distinctHashes(3001));
        var ok = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 10, distinctHashes(3000)));
        assertThat(ok.statusCode()).as(ok.body()).isEqualTo(200);
        // 3001 entries that collapse to 3000 distinct values are within the limit.
        List<String> dup = new ArrayList<>(distinctHashes(3000));
        dup.add(dup.get(0));
        var dupOk = post("/v1/vectors/search", searchBody(QUERY, List.of(BIG), 10, dup));
        assertThat(dupOk.statusCode()).as(dupOk.body()).isEqualTo(200);
        // The limit is on the engine's side of every route, not only /search.
        var h = post("/v1/vectors/hybrid-search", searchBody(GATE, List.of(BIG), 10, distinctHashes(3001)));
        assertThat(h.statusCode()).isEqualTo(400);
    }

    @Test
    void theRoutesThatDoNotServeTheFieldRefuseItRatherThanIgnoreIt() throws Exception {
        Map<String, Object> meta = searchBody(QUERY, List.of(BIG), 5, farBig);
        assertThat(post("/v1/vectors/search-metadata-scoped", meta).statusCode()).isEqualTo(400);
        Map<String, Object> topic = new LinkedHashMap<>();
        topic.put("query", QUERY);
        topic.put("topic", "t");
        topic.put("collection", BIG);
        topic.put("chash_allow", farBig);
        var t = post("/v1/vectors/search-topic-scoped", topic);
        assertThat(t.statusCode()).isEqualTo(400);
        assertThat(t.body()).contains("chash_allow");
        Map<String, Object> hop = searchBody(QUERY, List.of(BIG), 5, farBig);
        hop.put("seeds", List.of("1.1"));
        assertThat(post("/v1/vectors/search-graph-hop", hop).statusCode()).isEqualTo(400);
        Map<String, Object> aspect = searchBody(QUERY, List.of(BIG), 5, farBig);
        assertThat(post("/v1/vectors/search-aspect-scoped", aspect).statusCode()).isEqualTo(400);
    }

    // ── /version ──────────────────────────────────────────────────────────────

    @Test
    void version_advertisesTheCapability() throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + "/version").GET().build();
        var r = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        assertThat(MAPPER.readTree(r.body()).get("vector_search_chash_allow_supported").asBoolean()).isTrue();
    }
}
