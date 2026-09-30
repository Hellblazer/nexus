/* SPDX-License-Identifier: AGPL-3.0-or-later */
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository;
import dev.nexus.service.vectors.TaxonomyCentroidRepository.CentroidRecord;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import javax.sql.DataSource;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-wbfpw.47 — every engine vector-search path runs its HNSW scan with
 * {@code hnsw.max_scan_tuples = 200000} and {@code hnsw.scan_mem_multiplier = 4}
 * IN EFFECT inside the search transaction (defaults are 20000 and 1; measured
 * recall collapse past 95% dead, T2 nexus/rdr-192-livec-recall-extended-2026-09-30).
 *
 * <p>The probe is a {@link DataSource} wrapper: just before a pooled connection
 * commits, it reads the three GUCs back from that same connection, still inside
 * the transaction that ran the search. That is the value the scan saw, not a
 * value some helper claims to have set. A helper-level check would pass even if
 * a call site forgot to call the helper; this one cannot.
 *
 * <p>A path counts only if it opened an HNSW-scan transaction (one that set
 * {@code hnsw.iterative_scan = relaxed_order}); each path is asserted to have
 * opened at least one, so a path that silently stops reaching HNSW fails here
 * instead of vacuously passing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HnswScanBudgetOnEverySearchPathIntegrationTest {

    private static final String SVC_ROLE = "svc_scanbudget_test";
    private static final String SVC_PASS = "svc_scanbudget_test_pass";
    private static final String TENANT = "tenant-a";
    private static final String COL = "knowledge__scanbudget__minilm-l6-v2-384__v1";
    private static final String TOKEN = "scanbudgetprobetoken";

    /** One read-back per committed transaction: iterative_scan, max_scan_tuples, multiplier. */
    private record Seen(String label, String iterativeScan, String maxScanTuples, String multiplier) {}

    private final List<Seen> seen = new CopyOnWriteArrayList<>();
    private volatile String label = null;

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    PgVectorRepository repo;
    TaxonomyCentroidRepository centroids;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        var scope = new TenantScope(probing(svcDs));
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);
        centroids = new TaxonomyCentroidRepository(scope);

        // Seed with the probe disarmed (label == null).
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
        centroids.upsertCentroids(TENANT, List.of(
            new CentroidRecord("knowledge__scanbudget", 1L, unit(384), "c", 1)));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static float[] unit(int dim) {
        float[] v = new float[dim];
        v[0] = 1.0f;
        return v;
    }

    @Test
    void searchWithTokens() {
        run("search", () -> repo.search(TENANT, TOKEN, List.of(COL), 5, null));
        assertBudgetOnEveryHnswTxn("search");
    }

    @Test
    void hybridSearch_hnswFirstBranch() {
        // selectiveGateMax=1 with a 6-row gate forces the HNSW-first branch.
        run("hybrid", () -> repo.hybridSearch(TENANT, TOKEN, List.of(COL), 5, null, 1));
        assertBudgetOnEveryHnswTxn("hybrid");
    }

    @Test
    void searchMetadataScoped() {
        run("metadata", () -> repo.searchMetadataScoped(
            TENANT, TOKEN, List.of(COL), null, null, null, null, 5));
        assertBudgetOnEveryHnswTxn("metadata");
    }

    @Test
    void searchAspectScoped() {
        run("aspect", () -> repo.searchAspectScopedWithTokens(
            TENANT, TOKEN, List.of(COL), null, null, null, null, 5));
        assertBudgetOnEveryHnswTxn("aspect");
    }

    @Test
    void searchTopicScoped() {
        run("topic", () -> repo.searchTopicScoped(TENANT, TOKEN, "no-such-topic", COL, 5));
        assertBudgetOnEveryHnswTxn("topic");
    }

    @Test
    void searchGraphHop() {
        run("graphhop", () -> repo.searchGraphHop(
            TENANT, TOKEN, List.of("1.1.1"), List.of(COL), null, 1, "both", 5));
        assertBudgetOnEveryHnswTxn("graphhop");
    }

    @Test
    void taxonomyCentroidAnnQuery() {
        run("centroid", () -> centroids.annQuery(
            TENANT, unit(384), "knowledge__scanbudget", false, 3));
        assertBudgetOnEveryHnswTxn("centroid");
    }

    // -----------------------------------------------------------------------------

    private void run(String name, Runnable search) {
        label = name;
        try {
            search.run();
        } finally {
            label = null;
        }
    }

    private void assertBudgetOnEveryHnswTxn(String name) {
        List<Seen> hnsw = seen.stream()
            .filter(s -> s.label().equals(name) && "relaxed_order".equals(s.iterativeScan()))
            .toList();
        assertThat(hnsw)
            .as("path '%s' must open at least one HNSW-scan transaction (non-vacuity); saw %s",
                name, seen.stream().filter(s -> s.label().equals(name)).toList())
            .isNotEmpty();
        for (Seen s : hnsw) {
            assertThat(s.maxScanTuples())
                .as("path '%s': hnsw.max_scan_tuples inside the search transaction", name)
                .isEqualTo("200000");
            assertThat(s.multiplier())
                .as("path '%s': hnsw.scan_mem_multiplier inside the search transaction", name)
                .isEqualTo("4");
        }
    }

    /**
     * Wrap {@code delegate} so that, while a label is armed, every commit first reads the
     * three GUCs back from the committing connection (still inside its transaction).
     */
    private DataSource probing(DataSource delegate) {
        return (DataSource) Proxy.newProxyInstance(
            DataSource.class.getClassLoader(), new Class<?>[] {DataSource.class},
            (proxy, method, args) -> {
                Object result;
                try {
                    result = method.invoke(delegate, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
                if (method.getName().equals("getConnection") && result instanceof Connection c) {
                    return probingConnection(c);
                }
                return result;
            });
    }

    private static org.jooq.Field<String> setting(String guc) {
        return DSL.function("current_setting", String.class, DSL.val(guc), DSL.inline(true));
    }

    private Connection probingConnection(Connection real) {
        return (Connection) Proxy.newProxyInstance(
            Connection.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> {
                String armed = label;
                if (armed != null && method.getName().equals("commit")) {
                    var db = DSL.using(real, SQLDialect.POSTGRES);
                    var row = db.select(
                            setting("hnsw.iterative_scan"),
                            setting("hnsw.max_scan_tuples"),
                            setting("hnsw.scan_mem_multiplier"))
                        .fetchSingle();
                    seen.add(new Seen(armed, row.value1(), row.value2(), row.value3()));
                }
                try {
                    return method.invoke(real, args);
                } catch (InvocationTargetException e) {
                    throw e.getCause();
                }
            });
    }
}
