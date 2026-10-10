// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.vectors.PciCatalog;
import dev.nexus.service.vectors.PciIndexSet;
import dev.nexus.service.vectors.PciIndexSweep;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.FanoutSettings;
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

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
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
    // RDR-227 Step 1: a tenant of its own for the fan-out telemetry test (one exact arm, two HNSW arms).
    static final String TENANT_X = "43ulx1-ef";
    static final String X_EXACT = "knowledge__43ulx1-exact__minilm-l6-v2-384__v1";
    static final String X_HNSW1 = "knowledge__43ulx1-h1__minilm-l6-v2-384__v1";
    static final String X_HNSW2 = "knowledge__43ulx1-h2__minilm-l6-v2-384__v1";
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
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
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
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);
        embedder.register(QUERY, 1f, 0f);

        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(BIG, S1, S2, C1, C2, ISO)) {
                PgContainerHelper.insertCollection(dsl, TENANT, c);
            }
            for (String c : List.of(X_EXACT, X_HNSW1, X_HNSW2)) {
                PgContainerHelper.insertCollection(dsl, TENANT_X, c);
            }
            PgContainerHelper.insertCollection(dsl, ISO_A, ISO);
            PgContainerHelper.insertCollection(dsl, ISO_B, ISO);
        }
        seed(scope, embedder, TENANT, BIG, "big", BIG_ROWS, -0.3, 0.6 / (BIG_ROWS - 1));
        seed(scope, embedder, TENANT, S1, "s1", SMALL_ROWS, 3.0, 0.01);
        seed(scope, embedder, TENANT, S2, "s2", SMALL_ROWS, 3.1, 0.01);
        seed(scope, embedder, TENANT, C1, "c1", PAIR_ROWS, 2.0, 0.02);
        seed(scope, embedder, TENANT, C2, "c2", PAIR_ROWS, 2.5, 0.02);
        seed(scope, embedder, TENANT_X, X_EXACT, "xe", 4, 2.0, 0.02);
        seed(scope, embedder, TENANT_X, X_HNSW1, "x1", 30, 0.5, 0.02);
        seed(scope, embedder, TENANT_X, X_HNSW2, "x2", 40, 1.0, 0.02);
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

    /**
     * A repository whose index set claims a valid per-collection index for every collection, so a single
     * collection walks at the SERVING ef_search (RDR-227 Step 1). A single collection with no index walks at
     * 1000, which covers this fixture's whole ~320-row leaf, so that walk does not starve; the starved walk
     * the fallback cases below need is the serving one. The default repository (no index set) is the
     * production Step 1 shape and is witnessed separately, in
     * {@link #aSmallCollectionAboveT_onTheDefaultRepoWalksAtWidestAndNeedsNoFallback_theServingWalkStillFallsBack}.
     */
    private PgVectorRepository servingRepo() {
        return new PgVectorRepository(new TenantScope(ds), embedder, embedder, (model, tenant, collection) -> true);
    }

    /**
     * Witnesses the empty-result fallback on a statement that walks at the SERVING ef_search (an indexed or
     * multi-collection statement), not on the default repository's ef 1000 walk.
     */
    @Test
    void aSmallCollectionAboveTOnTheServingEfWalkStillGetsTheEmptyResultFallback() {
        PgSession.overrideSearchExactMaxRowsForTests(SMALL_ROWS - 1);
        Counters before = Counters.now();
        assertThat(servingRepo().searchWithTokens(TENANT, QUERY, List.of(S1), K, null, false).value())
            .as("repaired by the re-run").hasSize(SMALL_ROWS);
        assertThat(Counters.now().since(before).fallback())
            .as("the serving-ef walk starves above T and the re-run repairs it").isEqualTo(1);
    }

    /**
     * The production Step 1 shape (the default repository, no per-collection index): a small collection above
     * T walks the leaf at 1000, which covers the fixture's whole leaf, so it returns every row with no
     * fallback. The same statement over an indexed repository (the serving ef) starves and is repaired by the
     * re-run, so the two are the two sides of the ef choice.
     */
    @Test
    void aSmallCollectionAboveT_onTheDefaultRepoWalksAtWidestAndNeedsNoFallback_theServingWalkStillFallsBack() {
        PgSession.overrideSearchExactMaxRowsForTests(SMALL_ROWS - 1);
        Counters before = Counters.now();
        List<Map<String, Object>> wide = flat(TENANT, List.of(S1), K);
        Counters d = Counters.now().since(before);
        assertThat(wide).as("every row of the collection, from the first walk").hasSize(SMALL_ROWS);
        assertThat(d.hnsw()).as("above T: the HNSW route").isEqualTo(1);
        assertThat(d.exact()).isZero();
        assertThat(d.fallback()).as("ef 1000 reaches the collection: nothing starved, no re-run").isZero();

        before = Counters.now();
        List<Map<String, Object>> serving = servingRepo().searchWithTokens(TENANT, QUERY, List.of(S1), K, null, false)
            .value();
        d = Counters.now().since(before);
        assertThat(d.fallback()).as("the same statement at the serving ef starves and is repaired").isEqualTo(1);
        assertThat(serving.stream().map(r -> r.get("id")).toList())
            .as("both routes end with the same rows").containsExactlyInAnyOrderElementsOf(
                wide.stream().map(r -> r.get("id")).toList());
    }

    // ── 3. T = 0 disables ─────────────────────────────────────────────────────

    /** T = 0 on the SERVING ef walk (see {@link #servingRepo}): never exact, starved, repaired by the re-run. */
    @Test
    void zeroNeverRoutesExact_onTheServingEfWalk() {
        PgSession.overrideSearchExactMaxRowsForTests(0);
        Counters before = Counters.now();
        List<Map<String, Object>> rows =
            servingRepo().searchWithTokens(TENANT, QUERY, List.of(S1), K, null, false).value();
        Counters d = Counters.now().since(before);

        assertThat(rows).as("still repaired, by the empty-result re-run").hasSize(SMALL_ROWS);
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
        logs.list = new java.util.concurrent.CopyOnWriteArrayList<>();
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
        assertThat(PgVectorRepository.slowRouteLabel(10, true, 4, 1000))
            .as("exact ignores ef_search, so the label does not name it").isEqualTo("exact");
        assertThat(PgVectorRepository.slowRouteLabel(10, false, 11, 600)).isEqualTo("hnsw@600");
        assertThat(PgVectorRepository.slowRouteLabel(10, false, 11, 1000)).isEqualTo("hnsw@1000");
        assertThat(PgVectorRepository.slowRouteLabel(0, false, -1, 1000)).as("router off").isEqualTo("hnsw@1000");
        assertThat(PgVectorRepository.slowRouteLabel(10, false, -1, 1000))
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

    // ── RDR-227 Step 1: hnsw.ef_search 1000 for a single collection with no per-collection index ──────────

    private static final int WIDEST = 1000;

    /** A repository over a probed copy of the pool, so a test reads the ef_search each statement ran with. */
    private PgVectorRepository probedRepo(EfSearchProbe probe, PciIndexSet indexes) {
        return new PgVectorRepository(new TenantScope(probe.wrap(ds)), embedder, embedder, indexes);
    }

    /** Records every question the router asked, and answers from a fixed set of collection names. */
    private static final class StubIndexes implements PciIndexSet {
        final List<String> asked = new ArrayList<>();
        final java.util.Set<String> valid;

        StubIndexes(String... valid) {
            this.valid = java.util.Set.of(valid);
        }

        @Override
        public boolean hasValidIndex(String model, String tenant, String collection) {
            asked.add(model + "|" + tenant + "|" + collection);
            return valid.contains(collection);
        }
    }

    /** The ef_search each plain-search statement of {@code search} ran with, read in its own transaction. */
    private static List<String> efFor(EfSearchProbe probe, PgVectorRepository r,
                                      java.util.function.Consumer<PgVectorRepository> search) {
        probe.clear();
        search.accept(r);
        return List.copyOf(probe.efSearchPerStatement());
    }

    /** Routing row 1: the probe counts at or below T. The route is exact; the batch had already set 1000. */
    @Test
    void rowProbeAtOrBelowT_routesExact_andTheBatchHadAlreadySetTheWideValue() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var probe = new EfSearchProbe();
        var r = probedRepo(probe, PciIndexSet.NONE);
        Counters before = Counters.now();
        List<String> ef = efFor(probe, r, x -> x.searchWithTokens(TENANT, QUERY, List.of(S1), K, null, false));
        Counters d = Counters.now().since(before);
        assertThat(d.exact()).as("S1 holds 4 rows, T is 10: exact").isEqualTo(1);
        assertThat(d.hnsw()).isZero();
        assertThat(ef).as("the choice is made in the settings batch, before the probe; exact ignores it")
            .isNotEmpty().containsOnly(Integer.toString(WIDEST));
    }

    /** Routing row 2, searchWithTokens: above T, one collection, no index: HNSW at 1000. */
    @Test
    void rowAboveT_oneCollection_noIndex_searchWithTokensWalksAtWidest() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var probe = new EfSearchProbe();
        var stub = new StubIndexes();
        var r = probedRepo(probe, stub);
        Counters before = Counters.now();
        List<String> ef = efFor(probe, r, x -> x.searchWithTokens(TENANT, QUERY, List.of(BIG), K, null, false));
        Counters d = Counters.now().since(before);
        assertThat(d.hnsw()).as("BIG holds 300 rows, T is 10: HNSW").isEqualTo(1);
        assertThat(ef).as("hnsw.ef_search the statement ran with, read in its transaction")
            .isNotEmpty().containsOnly(Integer.toString(WIDEST));
        assertThat(stub.asked).as("asked once, with the statement's tenant and collection").hasSize(1);
        String[] q = stub.asked.get(0).split("\\|");
        assertThat(q).hasSize(3);
        assertThat(q[0]).as("the model of the leaf the collection is registered under")
            .isEqualTo(registeredModel(TENANT, BIG));
        assertThat(q[1]).isEqualTo(TENANT);
        assertThat(q[2]).isEqualTo(BIG);
    }

    /** Routing row 3: a statement over several collections keeps the serving value. */
    @Test
    void rowSeveralCollections_keepTheServingValue() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var probe = new EfSearchProbe();
        var stub = new StubIndexes();
        var r = probedRepo(probe, stub);
        Counters before = Counters.now();
        List<String> ef = efFor(probe, r, x -> x.searchWithTokens(TENANT, QUERY, List.of(C1, C2), 20, null, false));
        assertThat(Counters.now().since(before).hnsw()).as("6 + 6 rows > T").isEqualTo(1);
        assertThat(ef).as("serving: max(floor %d, k 20)", EF_FLOOR).isNotEmpty().containsOnly(Integer.toString(EF_FLOOR));
        assertThat(stub.asked).as("a multi-collection statement cannot use a partial index: not asked").isEmpty();
    }

    /** Routing row 4: the router off (T = 0), one collection: 1000. */
    @Test
    void rowRouterOff_oneCollection_walksAtWidest() {
        PgSession.overrideSearchExactMaxRowsForTests(0);
        var probe = new EfSearchProbe();
        var r = probedRepo(probe, PciIndexSet.NONE);
        Counters before = Counters.now();
        List<String> ef = efFor(probe, r, x -> x.searchWithTokens(TENANT, QUERY, List.of(S1), K, null, false));
        Counters d = Counters.now().since(before);
        assertThat(d.exact()).as("no probe at T = 0").isZero();
        assertThat(d.hnsw()).isEqualTo(1);
        assertThat(ef.get(0)).isEqualTo(Integer.toString(WIDEST));
    }

    /** Routing row 5: an index set that has a valid index for the collection: the serving value. */
    @Test
    void rowValidIndex_keepsTheServingValue_andOnlyForThatCollection() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var probe = new EfSearchProbe();
        var r = probedRepo(probe, new StubIndexes(BIG));
        Counters before = Counters.now();
        List<String> ef = efFor(probe, r, x -> x.searchWithTokens(TENANT, QUERY, List.of(BIG), K, null, false));
        assertThat(Counters.now().since(before).hnsw()).isEqualTo(1);
        assertThat(ef).as("BIG has a valid index: serving").isNotEmpty().containsOnly(Integer.toString(EF_FLOOR));

        // The set is keyed on the collection: an index for another collection does not help BIG.
        ef = efFor(probe, probedRepo(probe, new StubIndexes(S1)),
                   x -> x.searchWithTokens(TENANT, QUERY, List.of(BIG), K, null, false));
        assertThat(ef).as("only S1 has an index, BIG walks the leaf").isNotEmpty().containsOnly(Integer.toString(WIDEST));
    }

    /** The fan-out arm, rows 2 and 5: the same rule, read from the arm-phases line. */
    @Test
    void fanOutArm_noIndex_walksAtWidest_andAValidIndexKeepsServing() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var probe = new EfSearchProbe();

        probe.clear();
        List<String> lines = phaseLines(() -> probedRepo(probe, PciIndexSet.NONE)
            .searchPerCollection(TENANT, QUERY, List.of(BIG), K, 100, null, null, false));
        assertThat(probe.efSearchPerStatement()).as("hnsw.ef_search Postgres ran the arm with")
            .containsExactly(Integer.toString(WIDEST));
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0)).contains("ef1000_arms=1 ", "top_statements=" + BIG + ":hnsw:1000:");

        probe.clear();
        lines = phaseLines(() -> probedRepo(probe, new StubIndexes(BIG))
            .searchPerCollection(TENANT, QUERY, List.of(BIG), K, 100, null, null, false));
        assertThat(probe.efSearchPerStatement()).containsExactly(Integer.toString(EF_FLOOR));
        assertThat(lines).hasSize(1);
        assertThat(lines.get(0)).contains("ef1000_arms=0 ", "top_statements=" + BIG + ":hnsw:" + EF_FLOOR + ":");
    }

    /**
     * The field the Phase 2a measurement reads. One exact arm and two single-collection HNSW arms: the line
     * counts the arms at 1000, and names the three arms with route, ef_search and statement time, largest
     * first. The exact arm's batch also set 1000 (the choice precedes the probe), but it is not an arm that
     * ran HNSW at 1000, so it is listed with its route and left out of the count.
     */
    @Test
    void theArmPhasesLineNamesTheArmsByStatementTimeAndCountsTheEf1000Arms() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var probe = new EfSearchProbe();
        List<String> lines = phaseLines(() -> probedRepo(probe, PciIndexSet.NONE).searchPerCollection(
            TENANT_X, QUERY, List.of(X_EXACT, X_HNSW1, X_HNSW2), K, 100, null, null, false));
        assertThat(probe.efSearchPerStatement()).as("Postgres ran all three arms at 1000, the exact one included")
            .hasSize(3).containsOnly(Integer.toString(WIDEST));
        assertThat(lines).as("one arm-phases line per request").hasSize(1);
        String line = lines.get(0);
        assertThat(line).contains("arms=3 ", "exact_arms=1 hnsw_arms=2", "ef1000_arms=2 ");
        assertThat(line).as("the new fields come after the existing ones, so existing readers still match")
            .matches(".* ef1000_arms=2 top_statements=\\S+ sum_hnsw_statement_ms=\\d+ max_hnsw_statement_ms=\\d+$");

        var m = java.util.regex.Pattern.compile(" top_statements=(\\S*)").matcher(line);
        assertThat(m.find()).as(line).isTrue();
        String[] entries = m.group(1).split(",");
        assertThat(entries).as("the three largest statement times").hasSize(3);
        java.util.Map<String, String[]> byCollection = new java.util.HashMap<>();
        long previous = Long.MAX_VALUE;
        for (String e : entries) {
            String[] f = e.split(":");
            assertThat(f).as(e).hasSize(4);
            byCollection.put(f[0], f);
            long ms = Long.parseLong(f[3]);
            assertThat(ms).as("largest first: " + m.group(1)).isLessThanOrEqualTo(previous);
            previous = ms;
        }
        assertThat(byCollection.keySet()).containsExactlyInAnyOrder(X_EXACT, X_HNSW1, X_HNSW2);
        // The HNSW route's own statement time: the sum and max cover the two HNSW arms and not the exact one.
        long hnswMs = Long.parseLong(byCollection.get(X_HNSW1)[3]) + Long.parseLong(byCollection.get(X_HNSW2)[3]);
        long hnswMaxMs = Math.max(Long.parseLong(byCollection.get(X_HNSW1)[3]),
                                  Long.parseLong(byCollection.get(X_HNSW2)[3]));
        var hm = java.util.regex.Pattern.compile("sum_hnsw_statement_ms=(\\d+) max_hnsw_statement_ms=(\\d+)")
            .matcher(line);
        assertThat(hm.find()).as(line).isTrue();
        assertThat(Long.parseLong(hm.group(1))).as("sum over the HNSW arms (ms truncation tolerated)")
            .isBetween(hnswMs, hnswMs + 2);
        assertThat(Long.parseLong(hm.group(2))).isBetween(hnswMaxMs, hnswMaxMs + 1);
        assertThat(byCollection.get(X_EXACT)).as("route and ef_search of the exact arm")
            .containsSubsequence(X_EXACT, "exact", Integer.toString(WIDEST));
        assertThat(byCollection.get(X_HNSW1)).containsSubsequence(X_HNSW1, "hnsw", Integer.toString(WIDEST));
        assertThat(byCollection.get(X_HNSW2)).containsSubsequence(X_HNSW2, "hnsw", Integer.toString(WIDEST));
    }

    /** The field's ordering, limit and format, with the times fixed. */
    @Test
    void topStatementsIsLargestFirst_limited_marksStatementsThatDidNotReturn_andSkipsOnesNeverStarted() {
        var arms = List.of(
            new PgVectorRepository.ArmStatement("a", false, 1000, 5_000_000L, true),
            new PgVectorRepository.ArmStatement("b", true, 1000, 90_000_000L, true),
            new PgVectorRepository.ArmStatement("c", false, 600, 40_000_000L, true),
            new PgVectorRepository.ArmStatement("d", false, 1000, 70_000_000L, true),
            new PgVectorRepository.ArmStatement("t", false, 1000, 30_001_000_000L, false),
            new PgVectorRepository.ArmStatement("e", false, 1000, 0L, false));
        assertThat(PgVectorRepository.topStatements(arms, 3))
            .as("the arm that timed out ran longest, so it leads, marked")
            .isEqualTo("t:hnsw:1000:30001!,b:exact:1000:90,d:hnsw:1000:70");
        assertThat(PgVectorRepository.topStatements(arms, 10))
            .as("an arm that never started a statement has no statement time to list")
            .isEqualTo("t:hnsw:1000:30001!,b:exact:1000:90,d:hnsw:1000:70,c:hnsw:600:40,a:hnsw:1000:5");
        assertThat(PgVectorRepository.topStatements(List.of(), 3)).isEmpty();
    }

    /** The slow-statement line carries the route with its ef_search. */
    @Test
    void theSlowLineCarriesTheEfSearchOfAnHnswStatement() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        PgVectorRepository.overrideSlowStatementMsForTests(0);
        List<String> slow = slowLines(() -> flat(TENANT, List.of(BIG), K));
        assertThat(slow).hasSize(1);
        assertThat(slow.get(0)).contains("route=hnsw@" + WIDEST + " ");
        slow = slowLines(() -> flat(TENANT, List.of(C1, C2), 20));
        assertThat(slow).hasSize(1);
        assertThat(slow.get(0)).contains("route=hnsw@" + EF_FLOOR + " ");
    }

    /** The DEBUG per-arm line (not emitted in the cloud, but the local read of an arm) names ef_search. */
    @Test
    void theDebugPerArmLineCarriesEfSearch() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(PgVectorRepository.class);
        var was = logger.getLevel();
        logger.setLevel(ch.qos.logback.classic.Level.DEBUG);
        try {
            List<String> arms = capture(() -> repo.searchPerCollection(
                TENANT, QUERY, List.of(BIG, S1), K, 100, null, null, false), "event=search_per_collection_arm ");
            assertThat(arms).hasSize(2);
            assertThat(arms).allSatisfy(l -> assertThat(l).contains("ef_search=" + WIDEST + " "));
        } finally {
            logger.setLevel(was);
        }
    }

    // ── RDR-227 Step 2 (nexus-43ulx.12): the read half supplies the real valid-index set ──────────

    private static final PciSettings SWEEP_SETTINGS = new PciSettings(true, 20_000, 600, 16);
    private static final String MODEL_384 = "minilm-l6-v2-384";

    private static String sha256Hex(String s) {
        try {
            return HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256")
                .digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** {@code nexus.partition_name('chunks', model, tenant)}: the tenant leaf an index is made on. */
    private static String leafName(String model, String tenant) {
        return "chunks_m" + sha256Hex(model).substring(0, 8) + "_t_" + sha256Hex(tenant).substring(0, 16);
    }

    /** Build the per-collection index the way the builder will, as the superuser who owns the leaf. */
    private void createPciIndex(String tenant, String collection) throws Exception {
        String model = registeredModel(tenant, collection);
        assertThat(model).isEqualTo(MODEL_384);
        String ddl = "CREATE INDEX " + PciCatalog.indexName(model, tenant, collection) + " ON nexus."
            + leafName(model, tenant) + " USING hnsw (embedding_384 nexus.vector_cosine_ops) WHERE collection = '"
            + collection + "'";
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, ddl);
        }
    }

    private void dropPciIndex(String tenant, String collection) throws Exception {
        String model = registeredModel(tenant, collection);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su,
                "DROP INDEX IF EXISTS nexus." + PciCatalog.indexName(model, tenant, collection));
        }
    }

    private java.util.function.Supplier<PciCatalog.Snapshot> catalogReader() {
        var catalog = new PciCatalog(ds);
        return () -> catalog.read();
    }

    /** A sweep that reads the real catalog through the engine's pool, counting its reads. */
    private PciIndexSweep catalogSweep(java.util.concurrent.atomic.AtomicInteger reads) {
        var read = catalogReader();
        return new PciIndexSweep(() -> {
            reads.incrementAndGet();
            return read.get();
        }, SWEEP_SETTINGS);
    }

    private static void awaitTrue(java.util.function.BooleanSupplier condition, String what) throws Exception {
        long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(30);
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out waiting for " + what);
            }
            Thread.sleep(20);
        }
    }

    /** A swept valid index serves its collection at the serving value; any other walks the leaf at 1000. */
    @Test
    void sweptValidIndex_servesItsCollection_anUnindexedOneWalksWidest() throws Exception {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var sweep = catalogSweep(new java.util.concurrent.atomic.AtomicInteger());
        var probe = new EfSearchProbe();
        var r = probedRepo(probe, sweep);
        createPciIndex(TENANT_X, X_HNSW1);
        try {
            List<String> beforeRead = efFor(probe, r,
                x -> x.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW1), K, null, false));
            assertThat(beforeRead).as("the set is empty until the first read, whatever the catalog holds")
                .isNotEmpty().containsOnly(Integer.toString(WIDEST));

            assertThat(sweep.refresh()).isTrue();
            assertThat(sweep.status().valid()).as("exactly the one index built here").isEqualTo(1);

            assertThat(efFor(probe, r, x -> x.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW1), K, null, false)))
                .as("X_HNSW1 has a valid index: serving").isNotEmpty().containsOnly(Integer.toString(EF_FLOOR));
            assertThat(efFor(probe, r, x -> x.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW2), K, null, false)))
                .as("X_HNSW2 has none: leaf at 1000").isNotEmpty().containsOnly(Integer.toString(WIDEST));
        } finally {
            dropPciIndex(TENANT_X, X_HNSW1);
        }
    }

    /**
     * The production factory, not the test constructors: {@code create(ds, settings)} builds the catalog reader over
     * the engine's pool, so a wiring slip in it (a reader over the wrong source, a throwing factory) shows here.
     */
    @Test
    void theProductionFactory_readsTheRealCatalog() throws Exception {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var sweep = PciIndexSweep.create(ds, SWEEP_SETTINGS);
        createPciIndex(TENANT_X, X_HNSW1);
        try {
            assertThat(sweep.refresh()).as("create(ds, settings) reads the catalog").isTrue();
            assertThat(sweep.status().valid()).as("exactly the one index built here").isEqualTo(1);
            assertThat(sweep.hasValidIndex(registeredModel(TENANT_X, X_HNSW1), TENANT_X, X_HNSW1)).isTrue();
            assertThat(sweep.hasValidIndex(registeredModel(TENANT_X, X_HNSW2), TENANT_X, X_HNSW2)).isFalse();
        } finally {
            dropPciIndex(TENANT_X, X_HNSW1);
        }
    }

    /** Router off (NX_SEARCH_EXACT_MAX_ROWS=0): the indexed collection still serves, the rest walk at 1000. */
    @Test
    void routerOff_sweptIndexedCollectionServes_theRestWalkWidest() throws Exception {
        PgSession.overrideSearchExactMaxRowsForTests(0);
        var sweep = catalogSweep(new java.util.concurrent.atomic.AtomicInteger());
        var probe = new EfSearchProbe();
        var r = probedRepo(probe, sweep);
        createPciIndex(TENANT_X, X_HNSW1);
        try {
            sweep.refresh();
            Counters before = Counters.now();
            assertThat(efFor(probe, r, x -> x.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW1), K, null, false)))
                .isNotEmpty().containsOnly(Integer.toString(EF_FLOOR));
            assertThat(efFor(probe, r, x -> x.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW2), K, null, false)))
                .isNotEmpty().containsOnly(Integer.toString(WIDEST));
            assertThat(Counters.now().since(before).exact()).as("no probe at T = 0").isZero();
        } finally {
            dropPciIndex(TENANT_X, X_HNSW1);
        }
    }

    /**
     * Router refresh: two engines (two sweeps over one database) hold the same set after their next scheduled
     * read, for a build and again for a drop.
     */
    @Test
    void twoEnginesAgainstOneDatabase_holdTheSameSetAfterTheirNextRead() throws Exception {
        var a = PciIndexSweep.withPeriod(catalogReader(), SWEEP_SETTINGS, java.time.Duration.ofMillis(100));
        var b = PciIndexSweep.withPeriod(catalogReader(), SWEEP_SETTINGS, java.time.Duration.ofMillis(100));
        String model = registeredModel(TENANT_X, X_HNSW1);
        a.start();
        b.start();
        try {
            awaitTrue(() -> a.status().everRead() && b.status().everRead(), "both engines' first read");
            assertThat(a.hasValidIndex(model, TENANT_X, X_HNSW1)).isFalse();
            assertThat(b.hasValidIndex(model, TENANT_X, X_HNSW1)).isFalse();

            createPciIndex(TENANT_X, X_HNSW1);
            try {
                awaitTrue(() -> a.hasValidIndex(model, TENANT_X, X_HNSW1) && b.hasValidIndex(model, TENANT_X, X_HNSW1),
                    "both engines to see the new index");
            } finally {
                dropPciIndex(TENANT_X, X_HNSW1);
            }
            awaitTrue(() -> !a.hasValidIndex(model, TENANT_X, X_HNSW1) && !b.hasValidIndex(model, TENANT_X, X_HNSW1),
                "both engines to see the drop");
        } finally {
            a.stop();
            b.stop();
        }
    }

    /** No catalog round trip per statement or per arm: the reader runs only when refresh() does. */
    @Test
    void theSearchPathNeverCallsTheCatalog() throws Exception {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var reads = new java.util.concurrent.atomic.AtomicInteger();
        var sweep = catalogSweep(reads);
        var probe = new EfSearchProbe();
        var r = probedRepo(probe, sweep);
        createPciIndex(TENANT_X, X_HNSW1);
        try {
            sweep.refresh();
            assertThat(reads).hasValue(1);

            probe.clear();
            r.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW1), K, null, false);
            r.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW2), K, null, false);
            r.searchWithTokens(TENANT_X, QUERY, List.of(X_HNSW1, X_HNSW2), K, null, false);
            r.searchPerCollection(TENANT_X, QUERY, List.of(X_EXACT, X_HNSW1, X_HNSW2), K, 100, null, null, false);

            assertThat(probe.efSearchPerStatement()).as("the statements ran, and consulted the set")
                .contains(Integer.toString(EF_FLOOR), Integer.toString(WIDEST));
            assertThat(reads).as("and none of them read the catalog").hasValue(1);
        } finally {
            dropPciIndex(TENANT_X, X_HNSW1);
        }
    }

    /** The embedding model the catalog registered {@code collection} under: the leaf the statement reads. */
    private String registeredModel(String tenant, String collection) {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_COLLECTIONS.EMBEDDING_MODEL)
                .from(CATALOG_COLLECTIONS)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(collection))
                .fetchSingle().value1();
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * An arm whose statement runs into the search bound (SQLSTATE 57014) never returns, but it is the slowest
     * statement of the request and the one the telemetry exists to name. It is listed in top_statements with
     * the time it ran and a trailing {@code !}, counted in the HNSW statement time, and counted as an arm that
     * walked at 1000. The router is off (T = 0) so the lock blocks the statement, not the probe.
     */
    @Test
    void aStatementThatTimesOutIsNamedInTopStatementsWithAMarker_andCountsInTheHnswStatementTime()
            throws Exception {
        PgSession.overrideSearchExactMaxRowsForTests(0);
        PerCollectionResult[] result = new PerCollectionResult[1];
        List<String> lines;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            try {
                // An uncommitted rename holds the chunks table's exclusive lock: the arm's statement waits on it
                // until its 400 ms statement_timeout cancels it.
                DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).renameTo("chunks_43ulx_lock").execute();
                lines = phaseLines(() -> result[0] = repo.searchPerCollection(
                    TENANT, QUERY, List.of(BIG), K, 100, null, null, false, new FanoutSettings(1, 60_000, 400)));
            } finally {
                su.rollback();
            }
        }
        assertThat(result[0].perCollection()).hasSize(1);
        assertThat(result[0].perCollection().get(0).errorKind())
            .as("the arm timed out").isEqualTo(PgVectorRepository.ArmErrorKind.STATEMENT_TIMEOUT);
        assertThat(lines).hasSize(1);
        String line = lines.get(0);
        var m = java.util.regex.Pattern.compile(" top_statements=" + BIG + ":hnsw:" + WIDEST + ":(\\d+)! ")
            .matcher(line);
        assertThat(m.find()).as("the timed-out arm is listed, marked: " + line).isTrue();
        long ranMs = Long.parseLong(m.group(1));
        assertThat(ranMs).as("the time it ran before the 400 ms bound cancelled it").isGreaterThanOrEqualTo(300L);
        assertThat(line).contains("ef1000_arms=1 ");
        var t = java.util.regex.Pattern.compile(
            "sum_statement_ms=(\\d+) .*max_statement_ms=(\\d+) .*sum_hnsw_statement_ms=(\\d+) max_hnsw_statement_ms=(\\d+)")
            .matcher(line);
        assertThat(t.find()).as(line).isTrue();
        for (int g = 1; g <= 4; g++) {
            assertThat(Long.parseLong(t.group(g))).as("field %d counts the arm's running time: %s", g, line)
                .isGreaterThanOrEqualTo(300L);
        }
    }

    /**
     * A probe that runs into the search bound (57014) never reaches its statement: the arm is not an arm that
     * walked at 1000, it has no statement time so top_statements does not name it, and the time it ran is
     * the probe's. The router is on (T &gt; 0) and the same uncommitted rename blocks the probe's count.
     */
    @Test
    void aProbeThatTimesOutCountsInTheProbeTimeAndNotInTheStatementTimeOrTheEf1000Arms() throws Exception {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        PerCollectionResult[] result = new PerCollectionResult[1];
        List<String> lines;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            try {
                DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).renameTo("chunks_43ulx_lock").execute();
                lines = phaseLines(() -> result[0] = repo.searchPerCollection(
                    TENANT, QUERY, List.of(BIG), K, 100, null, null, false, new FanoutSettings(1, 60_000, 400)));
            } finally {
                su.rollback();
            }
        }
        assertThat(result[0].perCollection()).hasSize(1);
        assertThat(result[0].perCollection().get(0).errorKind())
            .as("the probe timed out").isEqualTo(PgVectorRepository.ArmErrorKind.STATEMENT_TIMEOUT);
        assertThat(lines).hasSize(1);
        String line = lines.get(0);
        assertThat(line).as("a probe that failed has no statement to name").contains(" top_statements= ");
        assertThat(line).contains("ef1000_arms=0 ");
        var t = java.util.regex.Pattern.compile(
            "sum_probe_ms=(\\d+) sum_statement_ms=(\\d+) .*sum_hnsw_statement_ms=(\\d+) ").matcher(line);
        assertThat(t.find()).as(line).isTrue();
        // A lower bound only: the 400 ms bound cancelled the probe, so it ran for most of that.
        assertThat(Long.parseLong(t.group(1))).as("the probe's time is the time it ran: %s", line)
            .isGreaterThanOrEqualTo(300L);
        assertThat(Long.parseLong(t.group(2))).as("no statement ran: %s", line).isZero();
        assertThat(Long.parseLong(t.group(3))).as("no HNSW statement ran: %s", line).isZero();
    }

    /**
     * ef1000_arms records the decision, not the value: an arm served from a valid index whose serving
     * ef_search happens to be 1000 (here a floor of 1000) is not an arm that walked wide.
     */
    @Test
    void anArmServedAtAnEfOf1000ByTheServingFloorIsNotCountedAsAWideWalk() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        PgSession.overrideEfSearchFloorForTests(WIDEST);
        try {
            var probe = new EfSearchProbe();
            List<String> lines = phaseLines(() -> probedRepo(probe, new StubIndexes(BIG))
                .searchPerCollection(TENANT, QUERY, List.of(BIG), K, 100, null, null, false));
            assertThat(probe.efSearchPerStatement()).as("Postgres ran the arm at 1000, from the serving floor")
                .containsExactly(Integer.toString(WIDEST));
            assertThat(lines).hasSize(1);
            assertThat(lines.get(0)).contains("hnsw_arms=1", "ef1000_arms=0 ",
                                              "top_statements=" + BIG + ":hnsw:" + WIDEST + ":");
        } finally {
            PgSession.overrideEfSearchFloorForTests(EF_FLOOR);
        }
    }

    /** The same name twice is one collection: the single-collection rule applies. */
    @Test
    void aDuplicatedSingleCollectionIsOneCollection_andWalksAtWidest() {
        PgSession.overrideSearchExactMaxRowsForTests(10);
        var probe = new EfSearchProbe();
        var stub = new StubIndexes();
        var r = probedRepo(probe, stub);
        List<Map<String, Object>>[] rows = new List[1];
        List<String> ef = efFor(probe, r, x -> rows[0] =
            x.searchWithTokens(TENANT, QUERY, List.of(BIG, BIG), K, null, false).value());
        assertThat(ef).as("[A, A] is the single collection A: no index, so 1000")
            .isNotEmpty().containsOnly(Integer.toString(WIDEST));
        assertThat(stub.asked).as("asked once, about A").hasSize(1);
        assertThat(stub.asked.get(0)).endsWith("|" + TENANT + "|" + BIG);
        assertThat(rows[0]).hasSize(K);
        assertThat(rows[0].stream().map(x -> x.get("id")).distinct().count()).as("no duplicated rows").isEqualTo(K);
    }

    private static List<String> phaseLines(Runnable body) {
        return capture(body, "event=search_per_collection_arm_phases ");
    }

    /** Formatted messages beginning with {@code prefix} that were logged while {@code body} ran. */
    private static List<String> capture(Runnable body, String prefix) {
        var root = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.list = new java.util.concurrent.CopyOnWriteArrayList<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.run();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
        return logs.list.stream().map(ILoggingEvent::getFormattedMessage).filter(m -> m.startsWith(prefix)).toList();
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
