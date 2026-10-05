// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.UnregisteredCollectionException;
import dev.nexus.service.http.RequestContext;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.PgVectorRepository.ArmErrorKind;
import dev.nexus.service.vectors.PgVectorRepository.FanoutSettings;
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
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
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
    TenantScope probeScope;
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
        probeScope = new TenantScope(probe.dataSource());
        probeRepo = new PgVectorRepository(probeScope, embedder, embedder);

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

    @org.junit.jupiter.api.BeforeEach
    void resetProbe() {
        probe.reset();
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
        assertThat(bad.errorKind()).isEqualTo(ArmErrorKind.DIMENSION_MISMATCH);
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
    void concurrency_theDefaultIsHalfThePool() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 12);
        probe.reset();
        probe.holdMs = 60;
        // A non-Hikari DataSource is sized at the TenantScope default pool of 10: default is 5.
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false);
        assertThat(probe.peak.get()).isEqualTo(5);
    }

    @Test
    void concurrency_anExplicitParallelismIsClampedToThePoolSize_notTheAdmissionLimit() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 24);
        // Lift the cross-request arm gate out of the way so the per-request clamp is what is measured.
        probeScope.replaceFanoutArmGateForTests(24);
        try {
            probe.reset();
            probe.holdMs = 60;
            // The probe's TenantScope sizes itself at the default pool of 10 (admission limit 20);
            // the real pool behind it holds 12 connections. 24 arms, an absurd request of 10000.
            PerCollectionResult r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null,
                                                                  false, 10_000);
            assertThat(r.rows()).hasSize(72);
            assertThat(probe.peak.get())
                .as("clamped to the pool size (10); a clamp to the admission limit (20) would reach the 12 the pool holds")
                .isEqualTo(10);
        } finally {
            probeScope.replaceFanoutArmGateForTests(5);
        }
    }

    @Test
    void concurrency_theArmGateIsSharedByEveryRequest_notPerRequest() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 12);
        probeScope.replaceFanoutArmGateForTests(3);
        try {
            probe.reset();
            probe.holdMs = 80;
            // Two requests at once, each wanting 5 arms: 10 arms against a gate of 3.
            var failures = new java.util.concurrent.CopyOnWriteArrayList<Throwable>();
            Runnable one = () -> {
                try {
                    PerCollectionResult r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null,
                                                                          false, 5);
                    assertThat(r.rows()).hasSize(36);
                } catch (Throwable t) {
                    failures.add(t);
                }
            };
            Thread a = Thread.ofVirtual().start(one);
            Thread b = Thread.ofVirtual().start(one);
            a.join();
            b.join();
            assertThat(failures).isEmpty();
            assertThat(probe.armBorrows.get()).isEqualTo(24);
            assertThat(probe.peak.get())
                .as("simultaneous arm connections across BOTH requests stay within the shared gate")
                .isLessThanOrEqualTo(3);
            assertThat(probe.peak.get()).as("non-vacuity: the gate was actually saturated").isEqualTo(3);
        } finally {
            probeScope.replaceFanoutArmGateForTests(5);
        }
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

        // Other transient states become the typed whole-request transient failure: resource
        // exhaustion (class 53), a lock timeout (55P03), a lost connection (08006), a
        // serialization failure (40001), an operator shutdown (57P01).
        for (String state : new String[] {"53100", "53200", "53300", "55P03", "08006", "40001", "40P01", "57P01"}) {
            probe.reset();
            probe.failFromArm = 3;
            probe.failure = () -> new SQLException("transient (probe)", state);
            assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1))
                .as("sqlstate %s", state)
                .isInstanceOf(SearchFanoutTransientException.class)
                .satisfies(t -> assertThat(((SearchFanoutTransientException) t).sqlState()).isEqualTo(state));
        }

        // A statement timeout (57014) is NOT a whole-request failure: it is isolated to its
        // collection (Sam, 2026-10-05). Arms 1 and 2 are served, arm 3 onward time out, and every
        // later arm is still attempted: a timeout is not a reason to stop the others.
        probe.reset();
        probe.failFromArm = 3;
        probe.failure = () -> new SQLException("canceling statement due to statement timeout", "57014");
        PerCollectionResult timedOut = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null,
                                                                     false, 1);
        assertThat(timedOut.perCollection().stream().filter(s -> s.errorKind() == ArmErrorKind.STATEMENT_TIMEOUT))
            .hasSize(10);
        assertThat(timedOut.rows()).as("the two arms that ran are served").hasSize(6);

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

    // ── a non-dimension IllegalArgumentException is NOT isolated ──────────────

    @Test
    void anIllegalArgumentExceptionThatIsNotADimensionCasePropagates_itIsNotReportedAsACollectionError()
            throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 4);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm
        probe.reset();
        probe.failFromArm = 2;
        probe.failure = () -> new IllegalArgumentException("a programming error in an arm");
        assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("a programming error in an arm");
    }

    @Test
    void anUnsupportedDispatchDimensionIsIsolatedWithItsOwnKind() throws Exception {
        // A collection whose recorded dimension has no plain_search function, searched by an
        // embedder that produces that same width: the width check passes, the dispatch cannot.
        String odd = "knowledge__tu8wp-d512__minilm-l6-v2-384__v1";
        register(CROWD_TENANT, odd);
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS)
               .set(CATALOG_COLLECTIONS.DIMENSION, 512)
               .where(CATALOG_COLLECTIONS.TENANT_ID.eq(CROWD_TENANT).and(CATALOG_COLLECTIONS.NAME.eq(odd)))
               .execute();
        }
        dev.nexus.service.db.CollectionRegistry.evict(CROWD_TENANT, odd);
        var embedder512 = new PgVectorRepositoryContractTest.FakeEmbedder(512);
        var repo512 = new PgVectorRepository(scope, embedder512, embedder512);
        PerCollectionResult r = repo512.searchPerCollection(CROWD_TENANT, QUERY, List.of(odd), 5, 10, null, null, false);
        assertThat(r.rows()).isEmpty();
        PerCollectionStat s = r.perCollection().get(0);
        assertThat(s.errorKind()).isEqualTo(ArmErrorKind.UNSUPPORTED_DIMENSION);
        assertThat(s.error()).isEqualTo("unsupported dim 512");
    }

    // ── the statement bound is computed AFTER admission (code review I1) ──────

    @Test
    void statementBound_isComputedAfterTheConnectionWait_notBefore() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 4);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm
        probe.reset();
        probe.borrowDelayMs = 1_000;     // each arm queues one second for its connection
        RequestContext.setDeadlineNanos(System.nanoTime() + 5_000_000_000L);
        try {
            PerCollectionResult r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false,
                new FanoutSettings(4, 60_000, 30_000));
            assertThat(r.rows()).hasSize(12);
        } finally {
            RequestContext.clearDeadline();
        }
        assertThat(probe.statementTimeouts).as("one statement_timeout per arm").hasSize(4);
        assertThat(probe.statementTimeouts)
            .as("the bound reflects the 5 s budget MINUS the 1 s queue wait; a bound computed before the wait is ~5000")
            .allSatisfy(ms -> assertThat(ms).isLessThanOrEqualTo(4_100).isPositive());
    }

    @Test
    void statementBound_aBudgetSpentWhileTheArmQueuedRefusesTheStatement() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 4);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm
        probe.reset();
        probe.borrowDelayMs = 1_000;
        RequestContext.setDeadlineNanos(System.nanoTime() + 500_000_000L);   // spent before the borrow returns
        try {
            assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false,
                    new FanoutSettings(1, 60_000, 30_000)))
                .isInstanceOf(RequestDeadlineExceededException.class);
        } finally {
            RequestContext.clearDeadline();
        }
        assertThat(probe.statementTimeouts).as("no statement was ever bounded and run").isEmpty();
        assertThat(probe.armBorrows.get()).as("no further arm was launched").isEqualTo(1);
    }

    // ── a REAL statement timeout (an ACCESS EXCLUSIVE lock on nexus.chunks) ───

    /**
     * A superuser connection holding ACCESS EXCLUSIVE on nexus.chunks until {@link #unlock}. The lock
     * is taken by an uncommitted {@code ALTER TABLE ... RENAME} (transactional DDL, undone by the
     * rollback), which jOOQ's typed DSL can express where {@code LOCK TABLE} would be raw SQL.
     */
    private Connection lockChunks() throws Exception {
        Connection su = pg.createConnection("");
        su.setAutoCommit(false);
        DSL.using(su, SQLDialect.POSTGRES).alterTable(CHUNKS).renameTo("chunks_tu8wp_lock").execute();
        return su;
    }

    private static void unlock(Connection su) {
        try {
            su.rollback();
            su.close();
        } catch (SQLException ignored) {
            // already released
        }
    }

    @Test
    void aRealStatementTimeoutIsIsolatedToItsCollection_theOthersAreServed() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 3);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm
        probe.reset();
        Connection su = lockChunks();
        // Release the lock the moment the SECOND arm has its connection: arm 1 has by then run into
        // its 400 ms bound, arm 2 waits out the few milliseconds left and is served.
        Thread releaser = Thread.ofVirtual().start(() -> {
            long end = System.nanoTime() + 20_000_000_000L;
            while (probe.armBorrows.get() < 2 && System.nanoTime() < end) {
                try {
                    Thread.sleep(5);
                } catch (InterruptedException e) {
                    return;
                }
            }
            unlock(su);
        });
        PerCollectionResult r;
        try {
            r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false,
                new FanoutSettings(1, 60_000, 400));
        } finally {
            releaser.join();
            unlock(su);
        }
        PerCollectionStat first = stat(r, cols.get(0));
        assertThat(first.errorKind()).isEqualTo(ArmErrorKind.STATEMENT_TIMEOUT);
        assertThat(first.error()).contains("400 ms").contains("57014");
        assertThat(first.rawCount()).isZero();
        assertThat(stat(r, cols.get(1)).error()).isNull();
        assertThat(stat(r, cols.get(2)).error()).isNull();
        assertThat(rowsOf(r.rows(), cols.get(1))).hasSize(3);
        assertThat(rowsOf(r.rows(), cols.get(2))).hasSize(3);
        assertThat(probe.statementTimeouts.get(0)).as("the search bound, since neither budget was nearer").isEqualTo(400);
    }

    @Test
    void aStatementCancelledByTheRequestBudgetsBoundIsAWholeRequestDeadline_notAnIsolatedTimeout() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 3);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm
        probe.reset();
        Connection su = lockChunks();
        RequestContext.setDeadlineNanos(System.nanoTime() + 700_000_000L);
        try {
            assertThatThrownBy(() -> probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false,
                    new FanoutSettings(1, 60_000, 30_000)))
                .as("the request ran out of time: that is the request's failure, not one collection's")
                .isInstanceOf(RequestDeadlineExceededException.class);
        } finally {
            RequestContext.clearDeadline();
            unlock(su);
        }
        assertThat(probe.armBorrows.get()).as("no further arm was launched after the whole-request failure")
            .isEqualTo(1);
    }

    @Test
    void aStatementRunningWhenTheFanoutBudgetEndsIsReportedPerCollection_andNoLaterArmStarts() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 3);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm
        probe.reset();
        Connection su = lockChunks();
        long t0 = System.nanoTime();
        PerCollectionResult r;
        try {
            r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false,
                new FanoutSettings(1, 500, 30_000));
        } finally {
            unlock(su);
        }
        long elapsedMs = (System.nanoTime() - t0) / 1_000_000L;
        assertThat(r.rows()).isEmpty();
        assertThat(r.perCollection()).hasSize(3)
            .allSatisfy(s -> assertThat(s.errorKind()).isEqualTo(ArmErrorKind.FANOUT_BUDGET_EXHAUSTED));
        assertThat(probe.armBorrows.get()).as("arms 2 and 3 were never started").isEqualTo(1);
        assertThat(probe.statementTimeouts.get(0)).as("the arm was bounded by the fan-out budget, not the 30 s bound")
            .isLessThanOrEqualTo(500);
        assertThat(elapsedMs).as("answered at the budget, not at the search bound").isLessThan(5_000);
    }

    // ── the fan-out wall budget (critique S1), without a database stall ──────

    @Test
    void fanoutBudget_armsNotStartedWhenItIsSpentAreReportedPerCollection() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 6);
        probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false, 1);   // warm
        probe.reset();
        probe.borrowDelayMs = 300;       // each arm queues 300 ms; the budget is 500 ms
        PerCollectionResult r = probeRepo.searchPerCollection(tenant, QUERY, cols, 3, 100, null, null, false,
            new FanoutSettings(1, 500, 30_000));
        // Arm 1 borrows at ~300 ms (inside the budget) and is served; arm 2's borrow returns at
        // ~600 ms, past it, and its statement is refused; arms 3..6 are never launched.
        assertThat(stat(r, cols.get(0)).error()).isNull();
        assertThat(rowsOf(r.rows(), cols.get(0))).hasSize(3);
        for (int i = 1; i < 6; i++) {
            PerCollectionStat s = stat(r, cols.get(i));
            assertThat(s.errorKind()).as("collection %d", i).isEqualTo(ArmErrorKind.FANOUT_BUDGET_EXHAUSTED);
            assertThat(s.error()).contains("500 ms");
        }
        assertThat(probe.armBorrows.get()).as("arms 3..6 were not launched once the budget was spent").isEqualTo(2);
        assertThat(r.rows()).hasSize(3);
    }

    // ── cross-collection ties (code review test gap) ──────────────────────────

    @Test
    void crossCollectionTies_areOrderedByCollection_whateverTheRequestOrder() throws Exception {
        String tenant = "tu8wp-tie";
        String t1 = "knowledge__tu8wp-tie1__minilm-l6-v2-384__v1";
        String t2 = "knowledge__tu8wp-tie2__minilm-l6-v2-384__v1";
        register(tenant, t1, t2);
        // The SAME five chunks (same text, so the same chash and the same vector) in both collections.
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < 5; i++) {
            String text = "tu8wp-tie|" + i;
            embedder.register(text, (float) Math.cos(0.05 * i), (float) Math.sin(0.05 * i));
            ids.add(chash(text));
            texts.add(text);
            metas.add(Map.of());
        }
        for (String c : List.of(t1, t2)) {
            repo.upsertChunks(tenant, c, ids, texts, metas);
            scope.withTenant(tenant, ctx -> {
                PgContainerHelper.ownChunks(ctx, tenant, c, ids.toArray(new String[0]));
                return null;
            });
        }
        // distance ascending by index, and each id appears once per collection at the same distance:
        // (id0,t1) (id0,t2) (id1,t1) (id1,t2) (id2,t1) ...  limit 5 cuts BETWEEN a tied pair.
        List<String> expected = List.of(ids.get(0) + "@" + t1, ids.get(0) + "@" + t2, ids.get(1) + "@" + t1,
                                        ids.get(1) + "@" + t2, ids.get(2) + "@" + t1);
        for (List<String> order : List.of(List.of(t1, t2), List.of(t2, t1))) {
            // parallelism 1: arrival order IS the request order, so only the tie-break can equalise them
            PerCollectionResult r = repo.searchPerCollection(tenant, QUERY, order, 5, 5, null, null, false, 1);
            assertThat(r.rows().stream().map(x -> x.get("id") + "@" + x.get("collection")).toList())
                .as("request order %s", order).isEqualTo(expected);
        }
    }

    // ── the merge holds at most `limit` rows (code review I3, critique S2) ────

    @Test
    void theMergeRetainsAtMostLimitRows_andStillReturnsTheGlobalBest() throws Exception {
        String tenant = "tu8wp-cc";
        List<String> cols = tinyCollections(tenant, 12);       // 12 collections x 3 rows = 36 survivors
        PerCollectionResult all = repo.searchPerCollection(tenant, QUERY, cols, 3, 1200, null, null, false, 1);
        assertThat(all.rows()).hasSize(36);
        assertThat(all.peakRetainedRows()).isEqualTo(36);       // limit 1200: nothing to evict
        for (int arrivals : new int[] {1, 4}) {
            PerCollectionResult cut = repo.searchPerCollection(tenant, QUERY, cols, 3, 5, null, null, false, arrivals);
            assertThat(cut.rows()).hasSize(5);
            assertThat(cut.peakRetainedRows())
                .as("36 rows were offered; the merge never held more than limit=5 of them")
                .isEqualTo(5);
            assertThat(ids(cut.rows()))
                .as("the global best 5, whatever order the arms finished in")
                .isEqualTo(ids(all.rows()).subList(0, 5));
            // per-collection stats still describe each arm's FULL row set, before the cut
            assertThat(cut.perCollection()).allSatisfy(s -> assertThat(s.rawCount()).isEqualTo(3));
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
