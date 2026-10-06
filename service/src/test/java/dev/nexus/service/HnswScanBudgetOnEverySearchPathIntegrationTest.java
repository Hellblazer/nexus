/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.CentroidRecord;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.47 -- every engine vector-search path runs its HNSW scan with the serving
 * scan budget IN EFFECT when the search statement runs: {@code hnsw.max_scan_tuples =
 * 200000} and {@code hnsw.scan_mem_multiplier = 16 MB / work_mem} (pgvector defaults are
 * 20000 and 1; measured recall collapse past 95% dead, T2
 * nexus/rdr-192-livec-recall-extended-2026-09-30).
 *
 * <p>The probe ({@link ScanBudgetProbe}) reads the GUCs back from the search's own
 * pooled connection at the moment the search statement is prepared, so it proves the
 * value the scan saw AND that it was set before the fetch: a site that called the
 * budget helper after its fetch would read the defaults here. A helper-level check
 * would pass even if a call site forgot the helper.
 *
 * <p>A path counts only if it ran an HNSW-scan statement ({@code hnsw.iterative_scan =
 * relaxed_order}); each path is asserted to have run at least one, so a path that
 * silently stops reaching HNSW fails here instead of vacuously passing.
 *
 * <p>The multiplier depends on the container's {@code work_mem}, which is the stock 4 MB
 * (pinned below), so 16 MB / 4 MB = 4. The last test proves nothing leaks: the next
 * transaction on the SAME physical pooled connection reads pgvector's defaults.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HnswScanBudgetOnEverySearchPathIntegrationTest {

    private static final String SVC_ROLE = "svc_scanbudget_test";
    private static final String SVC_PASS = "svc_scanbudget_test_pass";
    private static final String TENANT = "tenant-a";
    private static final String COL = "knowledge__scanbudget__minilm-l6-v2-384__v1";
    private static final String TOKEN = "scanbudgetprobetoken";

    private final ScanBudgetProbe probe = new ScanBudgetProbe();

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    PgVectorRepository repo;
    TaxonomyCentroidRepository centroids;

    @BeforeAll
    void startAll() throws Exception {
        PgSession.resetScanBudgetForTests();
        // nexus-tu8wp.6: the six-row fixture is under the cardinality router's default threshold, so
        // searchWithTokens would run exact and never walk HNSW; the probe only sees the GUCs set, so
        // the non-vacuity claim below would be false. Router off: the routed path is
        // PgVectorCardinalityRouterIntegrationTest's.
        PgSession.overrideSearchExactMaxRowsForTests(0);
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        svcDs = pool(1 + 4);
        scope = new TenantScope(probe.wrap(svcDs));
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);
        centroids = new TaxonomyCentroidRepository(scope);

        // Seed with the probe disarmed.
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, COL);
            return null;
        });
        List<String> chashes = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < 6; i++) {
            chashes.add(String.format("%064x", 0xabc000L + i));
            texts.add(TOKEN + " chunk " + i);
            metas.add(Map.of());
        }
        repo.upsertChunks(TENANT, COL, chashes, texts, metas);
        // The six chunks are the gate hybridSearch_hnswFirstBranch counts. They need a live owner:
        // text_gate_probe counts live(c) chunks since vectors-023 (nexus-wbfpw.35), so unowned
        // chunks are a zero-row gate, which takes the selective branch and never reaches HNSW.
        scope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, COL, chashes.toArray(new String[0]));
            return null;
        });
        centroids.upsertCentroids(TENANT, List.of(
            new CentroidRecord("knowledge__scanbudget", 1L, unit(384), "c", 1)));
    }

    private HikariDataSource pool(int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(size);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        PgSession.resetScanBudgetForTests();
        PgSession.resetSearchExactMaxRowsForTests();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static float[] unit(int dim) {
        float[] v = new float[dim];
        v[0] = 1.0f;
        return v;
    }

    @Test
    void containerWorkMemIsTheStock4MbSoTheDerivedMultiplierIs4() {
        String workMem = scope.withTenant(TENANT, ctx -> ctx.select(
            DSL.function("current_setting", String.class, DSL.val("work_mem"))).fetchSingle().value1());
        assertThat(workMem).as("the multiplier assertions below assume stock work_mem").isEqualTo("4MB");
    }

    @Test
    void searchWithTokens() {
        run("search", () -> repo.search(TENANT, TOKEN, List.of(COL), 5, null));
        assertBudgetOnEveryHnswStatement("search");
    }

    @Test
    void hybridSearch_hnswFirstBranch() {
        // selectiveGateMax=1 with a 6-row gate forces the HNSW-first branch.
        run("hybrid", () -> repo.hybridSearch(TENANT, TOKEN, List.of(COL), 5, null, 1));
        assertBudgetOnEveryHnswStatement("hybrid");
    }

    @Test
    void searchMetadataScoped() {
        run("metadata", () -> repo.searchMetadataScoped(
            TENANT, TOKEN, List.of(COL), null, null, null, null, 5));
        assertBudgetOnEveryHnswStatement("metadata");
    }

    @Test
    void searchAspectScoped() {
        run("aspect", () -> repo.searchAspectScopedWithTokens(
            TENANT, TOKEN, List.of(COL), null, null, null, null, 5));
        assertBudgetOnEveryHnswStatement("aspect");
    }

    @Test
    void searchTopicScoped() {
        run("topic", () -> repo.searchTopicScoped(TENANT, TOKEN, "no-such-topic", COL, 5));
        assertBudgetOnEveryHnswStatement("topic");
    }

    @Test
    void searchGraphHop() {
        run("graphhop", () -> repo.searchGraphHop(
            TENANT, TOKEN, List.of("1.1.1"), List.of(COL), null, 1, "both", 5));
        assertBudgetOnEveryHnswStatement("graphhop");
    }

    @Test
    void taxonomyCentroidAnnQuery() {
        run("centroid", () -> centroids.annQuery(
            TENANT, unit(384), "knowledge__scanbudget", false, 3));
        assertBudgetOnEveryHnswStatement("centroid");
    }

    /**
     * SET LOCAL must not outlive the search transaction. A pool of ONE connection makes
     * "the next transaction on the same pooled connection" literal: after a search, a plain
     * transaction on that connection must read pgvector's defaults (20000 tuples, 1x), not
     * the serving budget, or every later statement on it would inherit the raised budget.
     */
    @Test
    void theBudgetDoesNotLeakToTheNextTransactionOnTheSamePooledConnection() {
        try (HikariDataSource one = pool(1)) {
            var oneScope = new TenantScope(one);
            var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
            var oneRepo = new PgVectorRepository(oneScope, embedder, embedder);
            oneRepo.search(TENANT, TOKEN, List.of(COL), 5, null);

            String[] after = oneScope.withTenant(TENANT, ctx -> {
                var row = ctx.select(
                        DSL.function("current_setting", String.class, DSL.val("hnsw.max_scan_tuples"),
                            DSL.inline(true)),
                        DSL.function("current_setting", String.class, DSL.val("hnsw.scan_mem_multiplier"),
                            DSL.inline(true)))
                    .fetchSingle();
                return new String[] {row.value1(), row.value2()};
            });
            assertThat(one.getMaximumPoolSize()).isEqualTo(1);
            assertThat(after[0]).as("max_scan_tuples on the connection after the search txn").isEqualTo("20000");
            assertThat(after[1]).as("scan_mem_multiplier on the connection after the search txn").isEqualTo("1");
        }
    }

    // -----------------------------------------------------------------------------

    private void run(String name, Runnable search) {
        probe.arm(name);
        try {
            search.run();
        } finally {
            probe.disarm();
        }
    }

    private void assertBudgetOnEveryHnswStatement(String name) {
        List<ScanBudgetProbe.Seen> hnsw = probe.hnswStatements(name);
        assertThat(hnsw)
            .as("path '%s' must run at least one HNSW-scan statement (non-vacuity); saw %s",
                name, probe.seen().stream().filter(s -> s.label().equals(name)).toList())
            .isNotEmpty();
        for (ScanBudgetProbe.Seen s : hnsw) {
            assertThat(s.maxScanTuples())
                .as("path '%s': hnsw.max_scan_tuples when the search statement ran", name)
                .isEqualTo("200000");
            assertThat(s.multiplier())
                .as("path '%s': hnsw.scan_mem_multiplier when the search statement ran (16 MB / 4 MB)", name)
                .isEqualTo("4");
        }
    }
}
