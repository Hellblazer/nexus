// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnregisteredCollectionException;
import dev.nexus.service.http.RequestContext;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionResult;
import dev.nexus.service.vectors.PgVectorRepository.PerCollectionStat;
import dev.nexus.service.vectors.RequestDeadlineExceededException;
import dev.nexus.service.vectors.SearchFanoutTransientException;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-tu8wp.1 -- {@link PgVectorRepository#searchPerCollection}: per-collection top-K over one
 * embedding model group, run as bounded parallel single-collection {@code plain_search_<dim>} arms
 * and merged server-side.
 *
 * <p>Runs as {@code nexus_svc} (NOSUPERUSER NOBYPASSRLS), the production role, so row-level security
 * is live for every arm. The crowd-out pin is the one the feature exists for: a flat
 * {@code plain_search_<dim>(q, [A, B], k)} hands every slot to a dense collection A and returns ZERO
 * rows of a small collection B; the per-collection route returns B's rows. Every other test pins a
 * property of the design of record (T2 nexus/design-tu8wp-engine-per-collection-topk-2026-10-04):
 * per-collection equivalence with the single-collection call, threshold and global cut, isolation
 * of a permanent per-collection error, whole-request failure on a transient one, tenant isolation
 * with the arms on other threads, and the simultaneous-holder cap.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorSearchPerCollectionIntegrationTest {

    static final String QUERY = "tu8wp per collection query";
    static final int K = 20;

    static final String CROWD_TENANT = "tu8wp-crowd";
    static final String DENSE = "knowledge__tu8wp-dense__minilm-l6-v2-384__v1";
    static final String SMALL = "knowledge__tu8wp-small__minilm-l6-v2-384__v1";
    static final int DENSE_ROWS = 200;
    static final int SMALL_ROWS = 5;

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepositoryContractTest.FakeEmbedder embedder;
    PgVectorRepository repo;

    ArmProbeDataSource probe;
    PgVectorRepository probeRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(12);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        embedder.register(QUERY, 1f, 0f);
        repo = new PgVectorRepository(scope, embedder, embedder);

        probe = new ArmProbeDataSource(ds);
        probeRepo = new PgVectorRepository(new TenantScope(probe.dataSource()), embedder, embedder);

        // Crowd-out fixture. DENSE: 200 chunks in a narrow cone round the query. SMALL: 5 chunks far
        // from it. Every DENSE chunk is nearer than every SMALL chunk, so a flat top-20 over both
        // collections is all DENSE.
        register(CROWD_TENANT, DENSE, SMALL);
        seed(CROWD_TENANT, DENSE, "dense", DENSE_ROWS, 0.0, 0.001, true);
        seed(CROWD_TENANT, SMALL, "small", SMALL_ROWS, 1.0, 0.01, false);
    }

    @AfterAll
    void stopAll() {
        if (ds != null) {
            ds.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ── fixture helpers ──────────────────────────────────────────────────────

    private void register(String tenant, String... collections) throws Exception {
        try (Connection su = pg.createConnection("")) {
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : collections) {
                PgContainerHelper.insertCollection(dsl, tenant, c);
            }
        }
    }

    /**
     * Seed {@code count} chunks of {@code collection} on a ray from the query at
     * {@code angle0 + i * step} radians, each carrying {@code metadata.kind}: alternating when
     * {@code alternateKind}, else always {@code odd}. Returns the chashes in insertion order.
     */
    private List<String> seed(String tenant, String collection, String prefix, int count,
                              double angle0, double step, boolean alternateKind) {
        List<String> ids = new ArrayList<>(count);
        List<String> texts = new ArrayList<>(count);
        List<Map<String, Object>> metas = new ArrayList<>(count);
        for (int i = 0; i < count; i++) {
            double theta = angle0 + i * step;
            String text = tenant + "|" + collection + "|" + prefix + "-" + i;
            embedder.register(text, (float) Math.cos(theta), (float) Math.sin(theta));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of("kind", (alternateKind && i % 2 == 0) ? "even" : "odd"));
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
        return ids;
    }

    private static String chash(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static List<Map<String, Object>> rowsOf(List<Map<String, Object>> rows, String collection) {
        return rows.stream().filter(r -> collection.equals(r.get("collection"))).toList();
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    private static PerCollectionStat stat(PerCollectionResult r, String collection) {
        return r.perCollection().stream().filter(s -> s.collection().equals(collection)).findFirst().orElseThrow();
    }

    // ── crowd-out ─────────────────────────────────────────────────────────────

    @Test
    void crowdOut_aFlatSearchStarvesTheSmallCollection_theRouteServesIt() {
        // The old path: ONE flat top-20 over [DENSE, SMALL] is all DENSE, zero SMALL.
        var flat = repo.searchWithTokens(CROWD_TENANT, QUERY, List.of(DENSE, SMALL), K, null, false).value();
        assertThat(flat).hasSize(K);
        assertThat(rowsOf(flat, SMALL))
            .as("the flat top-K hands every slot to the dense collection (the starvation this route fixes)")
            .isEmpty();

        // The route: each collection returns its own top-K, so SMALL's 5 rows are present.
        PerCollectionResult r = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, SMALL),
                                                         K, 1200, null, null, false);
        assertThat(rowsOf(r.rows(), SMALL)).hasSize(SMALL_ROWS);
        assertThat(rowsOf(r.rows(), DENSE)).hasSize(K);
        assertThat(stat(r, DENSE).rawCount()).isEqualTo(K);
        assertThat(stat(r, SMALL).rawCount()).isEqualTo(SMALL_ROWS);
        // The merge is globally ordered by distance, so SMALL's rows sort after DENSE's.
        List<Double> d = r.rows().stream().map(x -> (Double) x.get("distance")).toList();
        assertThat(d).isSorted();
        assertThat(r.rows()).hasSize(K + SMALL_ROWS);
    }

    // ── equivalence ───────────────────────────────────────────────────────────

    @Test
    void equivalence_eachCollectionsRowsAreTheSingleCollectionPlainSearchRows() {
        PerCollectionResult r = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, SMALL),
                                                         K, 1200, null, null, false);
        for (String c : List.of(DENSE, SMALL)) {
            var single = repo.searchWithTokens(CROWD_TENANT, QUERY, List.of(c), K, null, false).value();
            var viaRoute = rowsOf(r.rows(), c);
            assertThat(ids(viaRoute)).as("same ids, same order, for %s", c).isEqualTo(ids(single));
            assertThat(viaRoute.stream().map(x -> x.get("distance")).toList())
                .as("same distances for %s", c)
                .isEqualTo(single.stream().map(x -> x.get("distance")).toList());
            // Same row shape: every key the single-collection row carries, the route row carries.
            for (int i = 0; i < single.size(); i++) {
                assertThat(viaRoute.get(i)).isEqualTo(single.get(i));
            }
        }
    }

    @Test
    void equivalence_holdsUnderAWhereFilterAppliedInEveryArm() {
        Map<String, Object> where = Map.of("kind", "odd");
        PerCollectionResult r = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, SMALL),
                                                         K, 1200, null, where, false);
        for (String c : List.of(DENSE, SMALL)) {
            var single = repo.searchWithTokens(CROWD_TENANT, QUERY, List.of(c), K, where, false).value();
            assertThat(ids(rowsOf(r.rows(), c))).isEqualTo(ids(single));
            assertThat(single).isNotEmpty();
        }
        assertThat(r.rows()).allSatisfy(row -> assertThat(row.get("kind")).isEqualTo("odd"));
    }

    // ── threshold and global cut ──────────────────────────────────────────────

    @Test
    void thresholds_dropPerCollection_andTheCutKeepsTheGlobalBestLimit() {
        PerCollectionResult full = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, SMALL),
                                                            K, 1200, null, null, false);
        var denseRows = rowsOf(full.rows(), DENSE);
        double cut = (Double) denseRows.get(9).get("distance");   // keep ranks 0..9 of DENSE

        Map<String, Double> thresholds = new LinkedHashMap<>();
        thresholds.put(DENSE, cut);
        thresholds.put(SMALL, null);                               // null / absent = no threshold
        PerCollectionResult r = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, SMALL),
                                                         K, 1200, thresholds, null, false);
        assertThat(rowsOf(r.rows(), DENSE)).hasSize(10);
        assertThat(rowsOf(r.rows(), SMALL)).hasSize(SMALL_ROWS);
        PerCollectionStat d = stat(r, DENSE);
        assertThat(d.rawCount()).isEqualTo(K);
        assertThat(d.dropped()).isEqualTo(K - 10);
        assertThat(d.minRawDistance()).isEqualTo(denseRows.get(0).get("distance"));
        assertThat(d.minDroppedDistance()).isEqualTo(denseRows.get(10).get("distance"));
        PerCollectionStat s = stat(r, SMALL);
        assertThat(s.dropped()).isZero();
        assertThat(s.minDroppedDistance()).isNull();

        // limit: the global top 7 by (distance, id), all from DENSE.
        PerCollectionResult cutRun = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, SMALL),
                                                              K, 7, null, null, false);
        assertThat(ids(cutRun.rows())).isEqualTo(ids(full.rows()).subList(0, 7));
        assertThat(stat(cutRun, SMALL).rawCount()).as("raw_count is before the global cut").isEqualTo(SMALL_ROWS);
    }

    // ── registration, homogeneity, validation ─────────────────────────────────

    @Test
    void anUnregisteredNameIsSkipped_andAllUnregisteredFailsLoud() {
        PerCollectionResult r = repo.searchPerCollection(CROWD_TENANT, QUERY,
            List.of(DENSE, "knowledge__tu8wp-ghost__minilm-l6-v2-384__v1", SMALL), K, 1200, null, null, false);
        assertThat(r.skippedCollections()).containsExactly("knowledge__tu8wp-ghost__minilm-l6-v2-384__v1");
        assertThat(r.perCollection()).extracting(PerCollectionStat::collection).containsExactly(DENSE, SMALL);
        assertThat(r.rows()).isNotEmpty();

        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY,
            List.of("knowledge__tu8wp-ghost__minilm-l6-v2-384__v1"), K, 1200, null, null, false))
            .isInstanceOf(UnregisteredCollectionException.class);
    }

    @Test
    void mixedEmbeddingModelsAreRefused() throws Exception {
        String other = "knowledge__tu8wp-other__bge-base-en-v15-768__v1";
        register(CROWD_TENANT, other);
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, other),
                                                          K, 1200, null, null, false))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("mixed embedding models");
    }

    @Test
    void limitsAreEnforced() {
        List<String> two = List.of(DENSE, SMALL);
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, two, 0, 10, null, null, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("per_collection_k");
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, two, 301, 10, null, null, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("per_collection_k");
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, two, 10, 0, null, null, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("limit");
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, two, 10, 1201, null, null, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("limit");
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(), 10, 10, null, null, false))
            .isInstanceOf(IllegalArgumentException.class);
        List<String> tooMany = new ArrayList<>();
        for (int i = 0; i <= PgVectorRepository.MAX_FANOUT_COLLECTIONS; i++) {
            tooMany.add("knowledge__tu8wp-many-" + i + "__minilm-l6-v2-384__v1");
        }
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, tooMany, 10, 10, null, null, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("at most 256");
        assertThatThrownBy(() -> repo.searchPerCollection(CROWD_TENANT, QUERY, two, 10, 10,
                                                          Map.of("knowledge__not-requested__minilm-l6-v2-384__v1", 0.5),
                                                          null, false))
            .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("not in collections");
    }

    // ── permanent per-collection error is isolated ────────────────────────────

    @Test
    void aDimensionMismatchedCollectionIsIsolated_theOthersAreServed() throws Exception {
        // The nexus-9tsdf class: registered under the 384-dim model, but its row says 768.
        String orphan = "knowledge__tu8wp-orphan__minilm-l6-v2-384__v1";
        register(CROWD_TENANT, orphan);
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.DIMENSION, 768)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(CROWD_TENANT).and(CATALOG_COLLECTIONS.NAME.eq(orphan)))
               .execute();
        }
        PerCollectionResult r = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(DENSE, orphan, SMALL),
                                                         K, 1200, null, null, false);
        PerCollectionStat bad = stat(r, orphan);
        assertThat(bad.error())
            .isEqualTo("query embedder produced a 384-dim vector but the collection dispatches to embedding_768");
        assertThat(bad.errorClass()).isEqualTo("IllegalArgumentException");
        assertThat(bad.rawCount()).isZero();
        // The healthy collections are served, in full.
        assertThat(rowsOf(r.rows(), DENSE)).hasSize(K);
        assertThat(rowsOf(r.rows(), SMALL)).hasSize(SMALL_ROWS);
        assertThat(stat(r, DENSE).error()).isNull();

        // When EVERY collection is the orphan the request still answers; the errors carry the news.
        PerCollectionResult all = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(orphan),
                                                           K, 1200, null, null, false);
        assertThat(all.rows()).isEmpty();
        assertThat(all.perCollection()).singleElement().extracting(PerCollectionStat::error).isNotNull();
    }

    // ── tenant isolation, arms on other threads ───────────────────────────────

    @Test
    void tenantIsolation_noCrossTenantRowsThroughTheRoute_withArmsOnOtherThreads() throws Exception {
        List<String> cols = new ArrayList<>();
        for (int i = 0; i < 6; i++) {
            cols.add("knowledge__tu8wp-iso" + i + "__minilm-l6-v2-384__v1");
        }
        String ta = "tu8wp-iso-a";
        String tb = "tu8wp-iso-b";
        String tc = "tu8wp-iso-empty";
        for (String t : List.of(ta, tb, tc)) {
            register(t, cols.toArray(new String[0]));
        }
        Map<String, List<String>> own = new LinkedHashMap<>();
        for (String t : List.of(ta, tb)) {
            List<String> mine = new ArrayList<>();
            for (String c : cols) {
                // Same collection names in both tenants, near-identical geometry, tenant-specific text.
                mine.addAll(seed(t, c, "iso", 8, 0.0, 0.01, false));
            }
            own.put(t, mine);
        }
        // Four workers over six collections: arms run on several virtual threads, none of which
        // carries the request's tenant except through the argument the arm was handed.
        for (String t : List.of(ta, tb)) {
            PerCollectionResult r = repo.searchPerCollection(t, QUERY, cols, 8, 300, null, null, false, 4);
            assertThat(r.rows()).as("tenant %s sees all of its own rows", t).hasSize(cols.size() * 8);
            assertThat(ids(r.rows())).containsExactlyInAnyOrderElementsOf(own.get(t));
            assertThat(r.rows()).allSatisfy(row -> assertThat((String) row.get("content")).startsWith(t + "|"));
        }
        // Positive control that RLS is the thing separating them: a third tenant, same collection
        // names, no chunks of its own, sees nothing.
        PerCollectionResult empty = repo.searchPerCollection(tc, QUERY, cols, 8, 300, null, null, false, 4);
        assertThat(empty.rows()).isEmpty();
        assertThat(empty.perCollection()).allSatisfy(s -> assertThat(s.rawCount()).isZero());
    }

    // ── concurrency cap: SIMULTANEOUS holders, not exit codes ─────────────────

    private List<String> tinyCollections(String tenant, int n) throws Exception {
        List<String> cols = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            cols.add("knowledge__tu8wp-cc" + i + "__minilm-l6-v2-384__v1");
        }
        register(tenant, cols.toArray(new String[0]));
        for (String c : cols) {
            seed(tenant, c, "cc", 3, 0.0, 0.05, false);
        }
        return cols;
    }

    @Test
    void concurrency_simultaneousArmHoldersNeverExceedTheCap() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 12);
        // Warm the registry cache so the measured run's request-thread lookups borrow nothing.
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);

        for (int cap : new int[] {1, 3, 5}) {
            probe.reset();
            probe.holdMs = 60;     // hold each arm connection open so arms that CAN overlap DO overlap
            PerCollectionResult r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null,
                                                                  false, cap);
            assertThat(r.rows()).hasSize(12 * 3);
            assertThat(probe.armBorrows.get()).as("one connection per arm").isEqualTo(12);
            assertThat(probe.peak.get()).as("simultaneous arm holders, cap=%d", cap).isLessThanOrEqualTo(cap);
            assertThat(probe.peak.get())
                .as("non-vacuity: the arms really did run %d at a time", cap)
                .isEqualTo(cap);
            assertThat(probe.open.get()).as("every arm connection was returned").isZero();
        }
    }

    @Test
    void concurrency_theDefaultIsHalfThePool_andOneRequestStaysUnderTheAdmissionLimit() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 12);
        probe.reset();
        probe.holdMs = 60;
        // A non-Hikari DataSource is sized at the TenantScope default pool of 10: default is 5.
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false);
        assertThat(probe.peak.get()).isEqualTo(5);
        // An absurd explicit cap is clamped under the admission limit (2 x pool = 20) and, here,
        // by the 12 arms and the 12-connection pool; it never starves the request into a deadlock.
        probe.reset();
        probe.holdMs = 20;
        PerCollectionResult r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null,
                                                              false, 10_000);
        assertThat(r.rows()).hasSize(36);
        assertThat(probe.peak.get()).isLessThanOrEqualTo(20);
    }

    // ── a transient failure fails the WHOLE request ───────────────────────────

    @Test
    void aTransientArmFailureFailsTheWholeRequest_andStopsTheArmsNotYetStarted() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 12);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm

        // Pool or admission exhaustion: arm 1 succeeds, arm 2 cannot get a connection.
        probe.reset();
        probe.failFromArm = 2;
        probe.failure = () -> new SQLTransientConnectionException("pool exhausted (probe)");
        assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1))
            .as("never a partial result")
            .satisfies(t -> assertThat(causeChain(t)).anyMatch(c -> c instanceof SQLTransientConnectionException));
        assertThat(probe.armBorrows.get()).as("arms 3..12 were never started").isEqualTo(2);

        // A statement timeout (57014) becomes the typed whole-request transient failure.
        probe.reset();
        probe.failFromArm = 3;
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1))
            .isInstanceOf(SearchFanoutTransientException.class)
            .satisfies(t -> assertThat(((SearchFanoutTransientException) t).sqlState()).isEqualTo("57014"));

        // A SQL failure that is not transient is a request-level error and propagates untyped.
        probe.reset();
        probe.failFromArm = 1;
        probe.failure = () -> new SQLException("relation does not exist", "42P01");
        assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 2))
            .isInstanceOf(RuntimeException.class)
            .isNotInstanceOf(SearchFanoutTransientException.class)
            .isNotInstanceOf(IllegalArgumentException.class);

        // And the repository is healthy afterwards: nothing was left holding a connection.
        probe.reset();
        PerCollectionResult ok = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 4);
        assertThat(ok.rows()).hasSize(36);
        assertThat(probe.open.get()).isZero();
    }

    @Test
    void anAlreadySpentRequestBudgetRefusesBeforeAnyArmStarts() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 4);
        probe.reset();
        RequestContext.setDeadlineNanos(System.nanoTime() - 1_000_000L);
        try {
            assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false))
                .isInstanceOf(RequestDeadlineExceededException.class);
        } finally {
            RequestContext.clearDeadline();
        }
        assertThat(probe.armBorrows.get()).as("no arm borrowed a connection").isZero();

        // A deadline with budget left is captured on the request thread and does not disturb the
        // arms, which never read the thread-local themselves.
        RequestContext.setDeadlineNanos(System.nanoTime() + 60_000_000_000L);
        try {
            assertThat(probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 4).rows())
                .hasSize(12);
        } finally {
            RequestContext.clearDeadline();
        }
    }

    private static List<Throwable> causeChain(Throwable t) {
        List<Throwable> out = new ArrayList<>();
        for (Throwable c = t; c != null && out.size() < 32; c = c.getCause()) {
            out.add(c);
        }
        return out;
    }

    @Test
    void searchPerCollectionReturnsTheSameRowShapeAsSearch() {
        var single = repo.searchWithTokens(CROWD_TENANT, QUERY, List.of(SMALL), 1, null, true).value();
        PerCollectionResult r = repo.searchPerCollection(CROWD_TENANT, QUERY, List.of(SMALL), 1, 10, null, null, true);
        assertThat(r.rows()).hasSize(1);
        assertThat(r.rows().get(0).keySet()).containsExactlyInAnyOrderElementsOf(single.get(0).keySet());
        assertThat(r.rows().get(0)).containsKeys("id", "content", "distance", "collection", "retention",
                                                 "chash", "span", "source_uri");
        assertThat(r.rows().stream().map(x -> x.get("collection")).collect(Collectors.toSet()))
            .containsExactly(SMALL);
    }
}
