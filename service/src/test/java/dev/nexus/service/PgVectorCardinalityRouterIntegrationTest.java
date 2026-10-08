// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tu8wp.6 -- the engine-side cardinality router for plain search. When the collections a
 * search selects hold at most {@code NX_SEARCH_EXACT_MAX_ROWS} physical rows in the tenant, the
 * statement runs exact instead of walking the shared HNSW index; above it (or with the router off)
 * it is today's path.
 *
 * <p>The fixture reproduces the pathology the router exists for, the way
 * {@code PgVectorSearchPerCollectionExactFallbackIntegrationTest} does: one BIG collection fills
 * the shared index around the query, the small collections sit on the far side, the HNSW walk is
 * capped at 16 tuples, and every non-HNSW plan is penalised at connection level. An index-ordered
 * statement for a small collection therefore starves and returns EMPTY, which is repaired only by
 * the empty-result exact re-run. A statement the router sends exact never starves and never needs
 * the re-run, so {@code exactFallbackCount} staying put is a second, independent witness beside the
 * routed-exact counter. Runs as {@code nexus_svc} (NOBYPASSRLS), so RLS is live for the probe.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorCardinalityRouterIntegrationTest {

    static final String TENANT = "tu8wp6-router";
    static final String ISO_A = "tu8wp6-iso-a";
    static final String ISO_B = "tu8wp6-iso-b";
    static final String BIG = "knowledge__tu8wp6-big__minilm-l6-v2-384__v1";
    static final String S1 = "knowledge__tu8wp6-s1__minilm-l6-v2-384__v1";
    static final String S2 = "knowledge__tu8wp6-s2__minilm-l6-v2-384__v1";
    static final String C1 = "knowledge__tu8wp6-c1__minilm-l6-v2-384__v1";
    static final String C2 = "knowledge__tu8wp6-c2__minilm-l6-v2-384__v1";
    static final String ISO = "knowledge__tu8wp6-iso__minilm-l6-v2-384__v1";
    static final String QUERY = "tu8wp6 router query";
    static final int BIG_ROWS = 300;
    static final int SMALL_ROWS = 4;
    static final int PAIR_ROWS = 6;
    static final int SCAN_CAP = 16;
    static final int EF_FLOOR = 200;
    static final int K = 5;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    PgVectorRepository repo;
    /** chash to the angle it was seeded at, for the brute-force expectation. */
    final Map<String, Double> angleOf = new LinkedHashMap<>();
    /** chash to "tenant|collection", so a test can name the rows of a collection set. */
    final Map<String, String> ownership = new LinkedHashMap<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        // Penalise every non-HNSW plan so an index-ordered statement is what the planner takes
        // (as in production under a stale generic plan). The exact route re-enables bitmap, seq and
        // sort itself (PgSession.disableIndexScanForExactFallback), so these cannot mask it.
        cfg.addDataSourceProperty("options",
            "-c enable_seqscan=off -c enable_bitmapscan=off -c enable_sort=off");
        ds = new HikariDataSource(cfg);
        PgSession.overrideScanBudgetForTests(SCAN_CAP, 1);
        // The fallback cases need a starved walk: the first ef batch must not reach the small
        // collections. The fixture was built at the 200 floor; at the 600 default that batch
        // covers this tenant's whole ~320-row leaf (nexus-3wh8d.31).
        PgSession.overrideEfSearchFloorForTests(EF_FLOOR);
        var scope = new TenantScope(ds);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);
        embedder.register(QUERY, 1f, 0f);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(BIG, S1, S2, C1, C2, ISO)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
            PgContainerHelper.insertCollection(dsl, ISO_A, ISO);
            PgContainerHelper.insertCollection(dsl, ISO_B, ISO);
        }
        seed(scope, embedder, TENANT, BIG, "big", BIG_ROWS, -0.3, 0.6 / (BIG_ROWS - 1));
        seed(scope, embedder, TENANT, S1, "s1", SMALL_ROWS, 3.0, 0.01);
        seed(scope, embedder, TENANT, S2, "s2", SMALL_ROWS, 3.1, 0.01);
        seed(scope, embedder, TENANT, C1, "c1", PAIR_ROWS, 2.0, 0.02);
        seed(scope, embedder, TENANT, C2, "c2", PAIR_ROWS, 2.5, 0.02);
        // Same collection name in two tenants: 3 rows for A, 30 for B.
        seed(scope, embedder, ISO_A, ISO, "isoa", 3, 2.0, 0.05);
        seed(scope, embedder, ISO_B, ISO, "isob", 30, 2.0, 0.05);
    }

    private void seed(TenantScope scope, PgVectorRepositoryContractTest.FakeEmbedder embedder, String tenant,
                      String collection, String prefix, int count, double angle0, double step) {
        List<String> ids = new ArrayList<>(count);
        List<String> texts = new ArrayList<>(count);
        List<Map<String, Object>> metas = new ArrayList<>(count);
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
            String text = tenant + "|" + collection + "|" + prefix + "-" + i;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            String chash = chash(text);
            ids.add(chash);
            texts.add(text);
            metas.add(Map.of());
            angleOf.put(chash, theta);
            ownership.put(chash, tenant + "|" + collection);
        }
        for (int from = 0; from < count; from += 300) {
            int to = Math.min(count, from + 300);
            repo.upsertChunks(tenant, collection, ids.subList(from, to), texts.subList(from, to),
                              metas.subList(from, to));
        }
        scope.withTenant(tenant, ctx -> {
            PgContainerHelper.ownChunks(ctx, tenant, collection, ids.toArray(new String[0]));
            return null;
        });
    }

    @AfterEach
    void unpin() {
        PgSession.resetSearchExactMaxRowsForTests();
        PgVectorRepository.resetSlowStatementMsForTests();
    }

    @AfterAll
    void stopAll() {
        PgSession.resetScanBudgetForTests();
        PgSession.resetEfSearchFloorForTests();
        if (ds != null) {
            ds.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    /** Counter snapshot, so each test asserts on its own deltas. */
    private record Counters(long exact, long hnsw, long fallback) {
        static Counters now() {
            return new Counters(PgVectorRepository.routedExactCount(), PgVectorRepository.routedHnswCount(),
                                PgVectorRepository.exactFallbackCount());
        }

        Counters since(Counters before) {
            return new Counters(exact - before.exact, hnsw - before.hnsw, fallback - before.fallback);
        }
    }

    private List<Map<String, Object>> flat(String tenant, List<String> cols, int k) {
        return repo.searchWithTokens(tenant, QUERY, cols, k, null, false).value();
    }

    /** The exact top-k over the given chashes: cosine distance to the query (1, 0) is 1 - cos(theta). */
    private List<String> bruteForceTopK(List<String> chashes, int k) {
        return chashes.stream()
            .sorted(Comparator.comparingDouble(c -> 1.0 - Math.cos(angleOf.get(c))))
            .limit(k).toList();
    }

    private List<String> chashesOf(String tenant, String... cols) {
        List<String> out = new ArrayList<>();
        for (String c : cols) {
            for (String id : angleOf.keySet()) {
                if (ownership.getOrDefault(id, "").equals(tenant + "|" + c)) {
                    out.add(id);
                }
            }
        }
        return out;
    }

    // ── 1. a collection set at or below T routes exact, and is exact ──────────

    @Test
    void aSmallSelectedSetRoutesExact_matchesBruteForce_andNeverStarves() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        Counters before = Counters.now();
        List<Map<String, Object>> rows = flat(TENANT, List.of(S1, S2), K);
        Counters d = Counters.now().since(before);

        assertThat(d.exact()).as("routed exact").isEqualTo(1);
        assertThat(d.hnsw()).as("not routed to HNSW").isZero();
        assertThat(d.fallback())
            .as("exact is complete: the capped HNSW walk that would have starved never ran")
            .isZero();
        List<String> expected = bruteForceTopK(chashesOf(TENANT, S1, S2), K);
        assertThat(rows.stream().map(r -> (String) r.get("id")).toList())
            .as("same ids, same order as an exact computation").isEqualTo(expected);
        for (Map<String, Object> r : rows) {
            double want = 1.0 - Math.cos(angleOf.get((String) r.get("id")));
            assertThat((Double) r.get("distance")).isCloseTo(want, org.assertj.core.data.Offset.offset(1e-5));
        }
        assertThat(rows.stream().map(r -> (Double) r.get("distance")).toList()).isSorted();
    }

    @Test
    void theRouterIsInclusiveAtTheThreshold() {
        PgSession.overrideSearchExactMaxRowsForTests(SMALL_ROWS);   // exactly the collection's size
        Counters before = Counters.now();
        assertThat(flat(TENANT, List.of(S1), K)).hasSize(SMALL_ROWS);
        Counters atT = Counters.now().since(before);
        assertThat(atT.exact()).as("rows == T routes exact").isEqualTo(1);

        PgSession.overrideSearchExactMaxRowsForTests(SMALL_ROWS - 1);
        before = Counters.now();
        assertThat(flat(TENANT, List.of(S1), K)).hasSize(SMALL_ROWS);
        Counters belowT = Counters.now().since(before);
        assertThat(belowT.exact()).as("rows == T + 1 does not").isZero();
        assertThat(belowT.hnsw()).isEqualTo(1);
    }

    // ── 2. above T: HNSW, today's path ────────────────────────────────────────

    @Test
    void aSelectedSetAboveTRoutesHnsw() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        Counters before = Counters.now();
        List<Map<String, Object>> rows = flat(TENANT, List.of(BIG), K);
        Counters d = Counters.now().since(before);

        assertThat(rows).hasSize(K);
        assertThat(d.hnsw()).as("routed HNSW").isEqualTo(1);
        assertThat(d.exact()).isZero();
        assertThat(d.fallback()).as("the index satisfied it: no re-run").isZero();
    }

    @Test
    void aSmallCollectionAboveTStillGetsTodaysEmptyResultFallback() {
        PgSession.overrideSearchExactMaxRowsForTests(SMALL_ROWS - 1);
        Counters before = Counters.now();
        assertThat(flat(TENANT, List.of(S1), K)).as("repaired by the re-run").hasSize(SMALL_ROWS);
        assertThat(Counters.now().since(before).fallback()).as("today's path is unchanged above T").isEqualTo(1);
    }

    // ── 3. T = 0 disables ─────────────────────────────────────────────────────

    @Test
    void zeroNeverRoutesExact() {
        PgSession.overrideSearchExactMaxRowsForTests(0);
        Counters before = Counters.now();
        List<Map<String, Object>> rows = flat(TENANT, List.of(S1), K);
        Counters d = Counters.now().since(before);

        assertThat(rows).as("still repaired, by today's re-run").hasSize(SMALL_ROWS);
        assertThat(d.exact()).as("T = 0: never exact").isZero();
        assertThat(d.hnsw()).isEqualTo(1);
        assertThat(d.fallback()).as("the starved HNSW walk ran and was repaired").isEqualTo(1);
    }

    @Test
    void routedAndUnroutedResultsAgree() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        List<Map<String, Object>> routed = flat(TENANT, List.of(S1, S2), K);
        PgSession.overrideSearchExactMaxRowsForTests(0);
        List<Map<String, Object>> today = flat(TENANT, List.of(S1, S2), K);
        assertThat(routed.stream().map(r -> r.get("id")).toList())
            .isEqualTo(today.stream().map(r -> r.get("id")).toList());
    }

    // ── 4. tenant isolation ───────────────────────────────────────────────────

    @Test
    void anotherTenantsRowsInASameNamedCollectionDoNotCountTowardTheProbe() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        // A holds 3 rows, B holds 30 in the SAME collection name. With RLS the probe sees 3 for A
        // (exact); without it the probe would see 33 and A would route HNSW.
        Counters before = Counters.now();
        List<Map<String, Object>> a = flat(ISO_A, List.of(ISO), K);
        Counters dA = Counters.now().since(before);
        assertThat(a).hasSize(3);
        assertThat(a).allSatisfy(r -> assertThat((String) r.get("content")).startsWith(ISO_A + "|"));
        assertThat(dA.exact()).as("A's probe counted only A's 3 rows").isEqualTo(1);
        assertThat(dA.hnsw()).isZero();

        // The control: B's own 30 rows are over T, so B routes HNSW.
        before = Counters.now();
        List<Map<String, Object>> b = flat(ISO_B, List.of(ISO), K);
        Counters dB = Counters.now().since(before);
        assertThat(b).hasSize(K);
        assertThat(dB.hnsw()).isEqualTo(1);
        assertThat(dB.exact()).isZero();
    }

    // ── 5. a batched multi-collection search sums across collections ──────────

    @Test
    void aBatchedSearchSumsAcrossCollections() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        // C1 (6) and C2 (6) are each under T = 10, together 12 > 10.
        Counters before = Counters.now();
        List<Map<String, Object>> rows = flat(TENANT, List.of(C1, C2), 20);
        Counters d = Counters.now().since(before);
        assertThat(rows).hasSize(2 * PAIR_ROWS);
        assertThat(d.hnsw()).as("the sum decides, not either collection alone").isEqualTo(1);
        assertThat(d.exact()).isZero();

        // The same pair with T raised to the sum routes exact.
        PgSession.overrideSearchExactMaxRowsForTests(2 * PAIR_ROWS);
        before = Counters.now();
        assertThat(flat(TENANT, List.of(C1, C2), 20)).hasSize(2 * PAIR_ROWS);
        assertThat(Counters.now().since(before).exact()).isEqualTo(1);
    }

    // ── 7. a per-collection fan-out routes each arm on its own count ──────────

    @Test
    void eachFanOutArmRoutesOnItsOwnCollectionsCount() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        Counters before = Counters.now();
        PerCollectionResult r = repo.searchPerCollection(TENANT, QUERY, List.of(BIG, S1, S2), K, 100, null, null,
                                                         false);
        Counters d = Counters.now().since(before);
        long s1 = r.rows().stream().filter(x -> S1.equals(x.get("collection"))).count();
        long s2 = r.rows().stream().filter(x -> S2.equals(x.get("collection"))).count();
        long big = r.rows().stream().filter(x -> BIG.equals(x.get("collection"))).count();
        assertThat(s1).isEqualTo(SMALL_ROWS);
        assertThat(s2).isEqualTo(SMALL_ROWS);
        assertThat(big).isEqualTo(K);
        assertThat(d.exact()).as("S1 and S2 arms routed exact").isEqualTo(2);
        assertThat(d.hnsw()).as("the BIG arm stayed on HNSW").isEqualTo(1);
        assertThat(d.fallback()).as("no arm needed the re-run").isZero();

        // A threshold between the two small sizes and the pair size splits the arms differently:
        // each arm compares ITS OWN collection's count, never the request's total.
        PgSession.overrideSearchExactMaxRowsForTests(SMALL_ROWS);
        before = Counters.now();
        repo.searchPerCollection(TENANT, QUERY, List.of(C1, S1, S2), K, 100, null, null, false);
        d = Counters.now().since(before);
        assertThat(d.exact()).as("S1 (4) and S2 (4) at T = 4; C1 (6) is above").isEqualTo(2);
        assertThat(d.hnsw()).isEqualTo(1);
    }

    // ── slow-statement line ───────────────────────────────────────────────────

    /** The {@code vector_search_statement_slow} lines emitted while {@code body} runs. */
    private static List<String> slowLines(Runnable body) {
        var root = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.run();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
        return logs.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.contains("event=vector_search_statement_slow")).toList();
    }

    @Test
    void aSlowStatementLogsItsRouteRowsAndCollections() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        PgVectorRepository.overrideSlowStatementMsForTests(0);
        List<String> slow = slowLines(() -> flat(TENANT, List.of(S1, S2), K));
        assertThat(slow).hasSize(1);
        assertThat(slow.get(0)).contains("route=exact", "probed_rows=8", "elapsed_ms=", S1, S2);
    }

    @Test
    void aFastStatementLogsNoSlowLine() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        // A threshold of ten minutes: no statement on any box can reach it, so the absence below
        // is the logging rule and not a race against a loaded CI runner.
        PgVectorRepository.overrideSlowStatementMsForTests(600_000);
        assertThat(slowLines(() -> flat(TENANT, List.of(S1), K))).as("under the threshold: no line").isEmpty();

        // Non-vacuity: the identical statement logs once the threshold is lowered, so the empty
        // result above is the threshold at work and not a logger that cannot see the line.
        PgVectorRepository.overrideSlowStatementMsForTests(0);
        assertThat(slowLines(() -> flat(TENANT, List.of(S1), K))).as("same statement, threshold 0").hasSize(1);
    }

    // ── the probe is bounded at T + 1 ─────────────────────────────────────────

    @Test
    void theProbeStopsCountingAtTPlusOneHoweverLargeTheCollection() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        PgVectorRepository.overrideSlowStatementMsForTests(0);
        // BIG holds 300 physical rows. A probe without its LIMIT would report 300 here.
        List<String> slow = slowLines(() -> flat(TENANT, List.of(BIG), K));
        assertThat(slow).hasSize(1);
        assertThat(slow.get(0)).contains("route=hnsw", "probed_rows=11");
    }

    // ── the slow line's route label ───────────────────────────────────────────

    @Test
    void theSlowLineNamesARouteNeverDecidedAsUnrouted() {
        assertThat(PgVectorRepository.slowRouteLabel(10, true, 4)).isEqualTo("exact");
        assertThat(PgVectorRepository.slowRouteLabel(10, false, 11)).isEqualTo("hnsw");
        assertThat(PgVectorRepository.slowRouteLabel(0, false, -1)).as("router off").isEqualTo("hnsw");
        assertThat(PgVectorRepository.slowRouteLabel(10, false, -1))
            .as("router on, the probe threw before counting").isEqualTo("unrouted");
    }

    // ── the exact route's planner switches do not outlive the transaction ─────

    /**
     * enable_indexscan=off and its siblings are SET LOCAL. A pool of ONE connection makes "the next
     * transaction on the same pooled connection" literal: after an exact-routed search, a plain
     * transaction reads the connection-level values again (the fixture's options), not the exact
     * route's, or every later statement on that connection would silently lose its index scans.
     */
    @Test
    void theExactRoutesPlannerSwitchesDoNotSurviveTheTransaction() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(1);
        cfg.setAutoCommit(true);
        cfg.addDataSourceProperty("options",
            "-c enable_seqscan=off -c enable_bitmapscan=off -c enable_sort=off");
        try (HikariDataSource one = new HikariDataSource(cfg)) {
            var oneScope = new TenantScope(one);
            var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
            embedder.register(QUERY, 1f, 0f);
            var oneRepo = new PgVectorRepository(oneScope, embedder, embedder);
            Counters before = Counters.now();
            oneRepo.searchWithTokens(TENANT, QUERY, List.of(S1), K, null, false);
            assertThat(Counters.now().since(before).exact())
                .as("non-vacuity: the search took the exact route that sets the switches").isEqualTo(1);

            List<String> after = oneScope.withTenant(TENANT, ctx -> {
                List<String> out = new ArrayList<>();
                for (String guc : List.of("enable_indexscan", "enable_bitmapscan", "enable_seqscan",
                                          "enable_sort")) {
                    out.add(ctx.select(DSL.function("current_setting", String.class, DSL.val(guc)))
                               .fetchSingle().value1());
                }
                return out;
            });
            assertThat(one.getMaximumPoolSize()).isEqualTo(1);
            assertThat(after).as("indexscan, bitmapscan, seqscan, sort on the connection after the search")
                .containsExactly("on", "off", "off", "off");
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
}
