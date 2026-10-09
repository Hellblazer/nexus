// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.PgSession.PciSettings;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-227 Step 2 (nexus-43ulx.25): the planner's choice, the sampled plan check, and the recall the per-collection
 * index exists for. Runs through {@link PgVectorRepository#searchWithTokens}, the arm's own statement, as
 * {@code nexus_svc} (NOBYPASSRLS), so the settings batch, the bound {@code p_collections}, the router probe and the
 * custom plan are the production ones.
 *
 * <p><b>Fixture.</b> One leaf holds a target collection and two larger sibling collections. Every row is
 * {@code rho * q0 + sqrt(1 - rho^2) * w}, where {@code q0} is the topic direction, {@code rho} is the row's cosine to
 * it and {@code w} is a unit vector orthogonal to {@code q0} drawn from {@link #CLUSTERS} clusters. Siblings reach
 * {@code rho} 0.95 and the target stops at 0.75, so the siblings crowd the target out of the first rows the leaf walk
 * returns. The twelve queries are small perturbations of {@code q0}: own-topic queries for the target. The seed is
 * fixed; the data is the same on every run, and the leaf's incremental HNSW insert order is the one thing a run
 * does not repeat, which is why the crowd-out is asserted with a wide margin and measured over several runs.
 *
 * <p><b>Order matters.</b> The crowd-out is measured before the target's index exists, and the index tests build it.
 * Tests are ordered for that reason ({@code @Order}), and the first one asserts the leaf holds no
 * {@code pci_} index so the order cannot silently flip.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class PciPlanAndRecallIntegrationTest {

    static final String TENANT = "43ulx25-pci";
    static final String MODEL = "minilm-l6-v2-384";
    static final String TARGET = "knowledge__43ulx25-target__minilm-l6-v2-384__v1";
    static final String SIB_A = "knowledge__43ulx25-siba__minilm-l6-v2-384__v1";
    static final String SIB_B = "knowledge__43ulx25-sibb__minilm-l6-v2-384__v1";
    /** A second, small collection for the degraded and cadence cases; it never needs recall. */
    static final String SMALL = "knowledge__43ulx25-small__minilm-l6-v2-384__v1";

    static final int DIM = 384;
    static final long SEED = 2527L;
    static final int N_SIBLING = 8000;
    static final int N_TARGET = 3000;
    static final int N_SMALL = 1500;
    static final int CLUSTERS = 20;
    static final int QUERIES = 12;
    static final int[] KS = {40, 60, 80, 100, 120};
    /** The router threshold for this class: above it for every collection here, so every arm walks HNSW. */
    static final int ROUTER_T = 1000;
    static final double TARGET_RECALL = 0.99;
    static final Duration BOUND = Duration.ofSeconds(120);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    QueryEmbedder embedder;
    PciIndexSweep sweep;
    String leaf;
    String targetIndex;
    String smallIndex;

    /** chash hex to vector, target collection only: the exact baseline. */
    final Map<String, float[]> targetVectors = new LinkedHashMap<>();
    final List<float[]> queries = new ArrayList<>();

    ListAppender<ILoggingEvent> checkLogs;
    Logger checkLogger;

    /** Query text to vector; the repository embeds a query through this. */
    static final class QueryEmbedder implements Embedder {
        final Map<String, float[]> byText = new LinkedHashMap<>();

        @Override
        public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> byText.get(t).clone()).toList();
        }
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        embedder = new QueryEmbedder();
        sweep = PciIndexSweep.create(svcDs, new PciSettings(true, 20_000, 600, 16));
        PgSession.overrideSearchExactMaxRowsForTests(ROUTER_T);
        targetIndex = PciCatalog.indexName(MODEL, TENANT, TARGET);
        smallIndex = PciCatalog.indexName(MODEL, TENANT, SMALL);
        seed();
    }

    @AfterAll
    void stopAll() {
        PgSession.resetSearchExactMaxRowsForTests();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    @BeforeEach
    void captureCheckLogs() {
        checkLogger = (Logger) LoggerFactory.getLogger(PciPlanCheck.class);
        checkLogs = new ListAppender<>();
        checkLogs.start();
        checkLogger.addAppender(checkLogs);
    }

    @AfterEach
    void releaseCheckLogs() {
        checkLogger.detachAppender(checkLogs);
        checkLogs.stop();
        PgSession.overrideSearchExactMaxRowsForTests(ROUTER_T);
    }

    // -- fixture ---------------------------------------------------------------------------------------------

    private static double dot(double[] a, double[] b) {
        double s = 0;
        for (int i = 0; i < a.length; i++) s += a[i] * b[i];
        return s;
    }

    private static double dot(float[] a, float[] b) {
        double s = 0;
        for (int i = 0; i < a.length; i++) s += (double) a[i] * b[i];
        return s;
    }

    private static double[] gauss(Random r) {
        double[] g = new double[DIM];
        for (int i = 0; i < DIM; i++) g[i] = r.nextGaussian();
        return g;
    }

    private static double[] orthTo(double[] v, double[] q) {
        double d = dot(v, q);
        double[] o = new double[v.length];
        for (int i = 0; i < v.length; i++) o[i] = v[i] - d * q[i];
        return o;
    }

    private static double[] normalize(double[] v) {
        double n = Math.sqrt(dot(v, v));
        double[] o = new double[v.length];
        for (int i = 0; i < v.length; i++) o[i] = v[i] / n;
        return o;
    }

    private static float[] toFloats(double[] v) {
        float[] f = new float[v.length];
        for (int i = 0; i < v.length; i++) f[i] = (float) v[i];
        return f;
    }

    private static byte[] sha256(String s) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Seed the three collections of the crowd-out and the small one, then ANALYZE the leaf. */
    private void seed() throws Exception {
        Random rnd = new Random(SEED);
        double[] q0 = normalize(gauss(rnd));
        double[][] centres = new double[CLUSTERS][];
        for (int j = 0; j < CLUSTERS; j++) centres[j] = normalize(orthTo(gauss(rnd), q0));

        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (String c : List.of(TARGET, SIB_A, SIB_B, SMALL)) {
                PgContainerHelper.insertCollection(ctx, TENANT, c);
            }
            String model = PgContainerHelper.collectionModel(ctx, TENANT, TARGET);
            assertThat(model).isEqualTo(MODEL);
            leaf = PciCatalogIntegrationTest.leafName(MODEL, TENANT);

            insert(ctx, TARGET, N_TARGET, 0.45, 0.75, rnd, q0, centres, targetVectors);
            insert(ctx, SIB_A, N_SIBLING, 0.45, 0.95, rnd, q0, centres, null);
            insert(ctx, SIB_B, N_SIBLING, 0.45, 0.95, rnd, q0, centres, null);
            insert(ctx, SMALL, N_SMALL, 0.45, 0.75, rnd, q0, centres, null);

            PgContainerHelper.installTestObjects(su);
            PgContainerHelper.analyzeTable(su, DSL.table(DSL.name("nexus", leaf)));
        }
        for (int i = 0; i < QUERIES; i++) {
            double[] pert = orthTo(gauss(rnd), q0);
            double[] q = new double[DIM];
            for (int d = 0; d < DIM; d++) q[d] = q0[d] + 0.025 * pert[d];
            float[] qf = toFloats(normalize(q));
            queries.add(qf);
            embedder.byText.put("q" + i, qf);
        }
    }

    private void insert(DSLContext ctx, String collection, int n, double rhoLo, double rhoHi, Random rnd,
                        double[] q0, double[][] centres, Map<String, float[]> keep) {
        String[] hex = new String[n];
        for (int from = 0; from < n; from += 500) {
            int to = Math.min(n, from + 500);
            var step = ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384);
            for (int i = from; i < to; i++) {
                double rho = rhoLo + (rhoHi - rhoLo) * rnd.nextDouble();
                double[] m = centres[rnd.nextInt(CLUSTERS)];
                double[] g = orthTo(gauss(rnd), q0);
                double[] w = new double[DIM];
                for (int d = 0; d < DIM; d++) w[d] = m[d] + 0.5 * g[d] / Math.sqrt(DIM);
                w = normalize(orthTo(w, q0));
                double s = Math.sqrt(1 - rho * rho);
                double[] v = new double[DIM];
                for (int d = 0; d < DIM; d++) v[d] = rho * q0[d] + s * w[d];
                float[] fv = toFloats(normalize(v));
                byte[] chash = sha256(TENANT + "|" + collection + "|" + i);
                hex[i] = HexFormat.of().formatHex(chash);
                if (keep != null) keep.put(hex[i], fv);
                step = step.values(TENANT, collection, chash, MODEL, "t", Vector.of(fv));
            }
            step.execute();
        }
        for (int from = 0; from < n; from += 4000) {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, Arrays.copyOfRange(hex, from, Math.min(n, from + 4000)));
        }
    }

    // -- helpers ---------------------------------------------------------------------------------------------

    /** The exact top-k of the target for query {@code i}: cosine distance is 1 - dot on unit vectors. */
    private Set<String> exactTopK(int i, int k) {
        float[] q = queries.get(i);
        return targetVectors.entrySet().stream()
            .sorted((a, b) -> Double.compare(1 - dot(a.getValue(), q), 1 - dot(b.getValue(), q)))
            .limit(k).map(Map.Entry::getKey).collect(Collectors.toSet());
    }

    private List<Map<String, Object>> search(PgVectorRepository repo, String collection, int i, int k) {
        return repo.searchWithTokens(TENANT, "q" + i, List.of(collection), k, null, false).value();
    }

    private double recall(PgVectorRepository repo, int i, int k) {
        Set<String> got = search(repo, TARGET, i, k).stream().map(r -> (String) r.get("id"))
            .collect(Collectors.toSet());
        Set<String> exact = exactTopK(i, k);
        return (double) got.stream().filter(exact::contains).count() / k;
    }

    /** Recall per query at k. */
    private double[] recalls(PgVectorRepository repo, int k) {
        double[] r = new double[QUERIES];
        for (int i = 0; i < QUERIES; i++) r[i] = recall(repo, i, k);
        return r;
    }

    private static double mean(double[] v) {
        return Arrays.stream(v).average().orElseThrow();
    }

    private static double min(double[] v) {
        return Arrays.stream(v).min().orElseThrow();
    }

    /** A repository over the real sweep: its router set is what the catalog says. */
    private PgVectorRepository realRepo(PciPlanCheck check) {
        return new PgVectorRepository(scope, embedder, embedder, sweep, check);
    }

    /** A repository whose router set claims a valid index for every collection: serving ef, whatever exists. */
    private PgVectorRepository claimingRepo(PciPlanCheck check) {
        return new PgVectorRepository(scope, embedder, embedder, (m, t, c) -> true, check);
    }

    private static PciPlanCheck neverSample() {
        return new PciPlanCheck(Integer.MAX_VALUE, (ctx, q) -> {
            throw new AssertionError("the plan check must not sample here");
        });
    }

    /** Plans captured from the arm's own EXPLAIN. */
    static final class CapturingPlans implements PciPlanCheck.PlanSource {
        final List<String> plans = new CopyOnWriteArrayList<>();

        @Override
        public String plan(DSLContext ctx, org.jooq.Select<?> statement) {
            String plan = ctx.explain(statement).plan();
            plans.add(plan);
            return plan;
        }
    }

    private void ddl(String statement) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdl(su, statement);
        }
    }

    private void buildIndex(String collection) throws Exception {
        String name = PciCatalog.indexName(MODEL, TENANT, collection);
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.runSuperuserDdlOutsideTransaction(su,
                PciCatalogIntegrationTest.createIndexDdl("CONCURRENTLY ", name, MODEL, TENANT,
                    "collection = " + PciCatalogIntegrationTest.lit(collection)));
        }
    }

    private void ensureTargetIndex() throws Exception {
        if (!sweep.refresh() || !sweep.hasValidIndex(MODEL, TENANT, TARGET)) {
            buildIndex(TARGET);
            assertThat(sweep.refresh()).isTrue();
        }
        assertThat(sweep.hasValidIndex(MODEL, TENANT, TARGET)).as("the router's set sees the built index").isTrue();
    }

    private Boolean indexIsValid(String name) {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            return ctx.select(DSL.field(DSL.name("i", "indisvalid"), Boolean.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_index")).as("i"))
                .join(DSL.table(DSL.name("pg_catalog", "pg_class")).as("c"))
                .on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
                .where(DSL.field(DSL.name("c", "relname"), String.class).eq(name))
                .fetchOne(0, Boolean.class);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The leaf's own (unconditional) HNSW index on the 384 column. */
    private String leafHnswIndex() {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES)
                .select(DSL.field(DSL.name("indexname"), String.class))
                .from(DSL.table(DSL.name("pg_catalog", "pg_indexes")))
                .where(DSL.field(DSL.name("tablename"), String.class).eq(leaf))
                .and(DSL.field(DSL.name("indexdef"), String.class).likeIgnoreCase("%using hnsw%embedding_384%"))
                .and(DSL.field(DSL.name("indexdef"), String.class).notLike("% WHERE %"))
                .fetchOne(0, String.class);
        } catch (java.sql.SQLException e) {
            throw new IllegalStateException(e);
        }
    }

    private static void await(String what, BooleanSupplier condition) throws InterruptedException {
        long deadline = System.nanoTime() + BOUND.toNanos();
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                throw new AssertionError("timed out after " + BOUND + " waiting for " + what);
            }
            Thread.sleep(25);
        }
    }

    private List<String> checkMessages() {
        return checkLogs.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    private void report(String label, PgVectorRepository repo) {
        for (int k : KS) {
            double[] r = recalls(repo, k);
            System.out.printf("PCI-RECALL %s k=%d mean=%.4f min=%.4f%n", label, k, mean(r), min(r));
        }
    }

    // -- 1. the crowd-out, before any index -----------------------------------------------------------------

    @Test
    @Order(1)
    void theLeafWalkAtServingSettingsLosesRecall_whenSiblingsCrowdTheTargetOut() throws Exception {
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.hasValidIndex(MODEL, TENANT, TARGET))
            .as("no per-collection index yet: the crowd-out is measured on the leaf walk alone").isFalse();
        assertThat(indexIsValid(targetIndex)).as("the target's index does not exist yet").isNull();
        assertThat(leafHnswIndex()).as("the leaf's own HNSW index is what the walk uses").isNotNull();

        long hnswBefore = PgVectorRepository.routedHnswCount();
        long exactBefore = PgVectorRepository.routedExactCount();
        PgVectorRepository leafWalk = claimingRepo(neverSample());
        List<String> table = new ArrayList<>();
        for (int k : KS) {
            double[] r = recalls(leafWalk, k);
            table.add(String.format("k=%d mean=%.4f min=%.4f", k, mean(r), min(r)));
            assertThat(mean(r))
                .as("crowd-out at k=%d: the filtered leaf walk at serving ef must lose recall (seed %d); a seed that "
                    + "does not reproduce it is a failed test", k, SEED)
                .isLessThan(TARGET_RECALL);
        }
        System.out.println("PCI-CROWDOUT seed=" + SEED + " leaf-walk-serving-ef " + String.join(" | ", table));

        assertThat(PgVectorRepository.routedHnswCount() - hnswBefore)
            .as("every search walked HNSW (an exact route would be recall 1.0 and prove nothing)")
            .isEqualTo((long) KS.length * QUERIES);
        assertThat(PgVectorRepository.routedExactCount() - exactBefore).isZero();
    }

    @Test
    @Order(2)
    void noIndex_theWidestWalkReturnsRows() {
        PgVectorRepository none = new PgVectorRepository(scope, embedder, embedder, PciIndexSet.NONE);
        for (int k : KS) {
            assertThat(search(none, TARGET, 0, k)).as("no index, ef 1000 walk, k=%d", k).hasSize(k);
        }
        assertThat(search(none, SMALL, 0, 40)).hasSize(40);
    }

    // -- 2. the planner pin, the plan check, and recall with the index --------------------------------------

    @Test
    @Order(4)
    void explainPin_aValidIndexIsPlanned_andThePlanCheckLogsUsedTrue() throws Exception {
        ensureTargetIndex();
        assertThat(indexIsValid(targetIndex)).as("pg_index.indisvalid").isTrue();
        CapturingPlans plans = new CapturingPlans();
        PgVectorRepository repo = realRepo(new PciPlanCheck(1, plans));

        assertThat(search(repo, TARGET, 0, 60)).hasSize(60);

        assertThat(plans.plans).as("the arm explained its own statement once").hasSize(1);
        String plan = plans.plans.get(0);
        System.out.println("PCI-PLAN valid-index plan:\n" + plan);
        assertThat(plan).as("the planner scans the collection's own index").contains(targetIndex);
        assertThat(plan).as("the SQL function is inlined: no Function Scan stands between the arm and the index")
            .doesNotContain("Function Scan");
        assertThat(checkMessages()).anySatisfy(m -> assertThat(m)
            .contains("event=pci_plan_check").contains("used=true")
            .contains("index=" + targetIndex).contains("collection=" + TARGET));
    }

    @Test
    @Order(5)
    void recallWithTheIndex_meetsTheTargetAtEveryK() throws Exception {
        ensureTargetIndex();
        long hnswBefore = PgVectorRepository.routedHnswCount();
        PgVectorRepository repo = realRepo(neverSample());
        List<String> table = new ArrayList<>();
        for (int k : KS) {
            double[] r = recalls(repo, k);
            table.add(String.format("k=%d mean=%.4f min=%.4f", k, mean(r), min(r)));
            assertThat(mean(r)).as("mean recall at k=%d with the index", k).isGreaterThanOrEqualTo(TARGET_RECALL);
            assertThat(min(r)).as("worst own-topic query at k=%d with the index", k)
                .isGreaterThanOrEqualTo(TARGET_RECALL);
        }
        System.out.println("PCI-INDEXED seed=" + SEED + " own-index " + String.join(" | ", table));
        assertThat(PgVectorRepository.routedHnswCount() - hnswBefore)
            .as("every search walked HNSW, not the exact route").isEqualTo((long) KS.length * QUERIES);
    }

    // -- 3. degraded paths: an index being built, an invalid index ------------------------------------------

    @Test
    @Order(3)
    void anIndexBeingBuilt_andAnInvalidOne_degradeToRows_andThePlanCheckReadsUsedFalse() throws Exception {
        String appName = "pciplan-build";
        var activityApp = DSL.field(DSL.name("application_name"), String.class);
        var activityPid = DSL.field(DSL.name("pid"), Integer.class);
        var activityWait = DSL.field(DSL.name("wait_event_type"), String.class);
        var activity = DSL.table(DSL.name("pg_catalog", "pg_stat_activity"));
        String hnswIndex = leafHnswIndex();

        try (Connection monitor = pg.createConnection(""); Connection blocker = pg.createConnection("")) {
            monitor.setAutoCommit(true);
            DSLContext asSuperuser = DSL.using(monitor, SQLDialect.POSTGRES);
            blocker.setAutoCommit(false);
            // A DELETE that matches nothing still holds ROW EXCLUSIVE on the leaf to the end of the transaction,
            // which a concurrent build waits out AFTER it has committed its (invalid) catalog entry.
            DSL.using(blocker, SQLDialect.POSTGRES).deleteFrom(DSL.table(DSL.name("nexus", leaf)))
                .where(DSL.falseCondition()).execute();
            CompletableFuture<Void> build = CompletableFuture.runAsync(() -> {
                try (Connection c = pg.createConnection("?ApplicationName=" + appName)) {
                    PgContainerHelper.runSuperuserDdlOutsideTransaction(c,
                        PciCatalogIntegrationTest.createIndexDdl("CONCURRENTLY ", targetIndex, MODEL, TENANT,
                            "collection = " + PciCatalogIntegrationTest.lit(TARGET)));
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });
            try {
                java.util.function.Supplier<Integer> buildPid = () -> asSuperuser.select(activityPid).from(activity)
                    .where(activityApp.eq(appName)).and(activityWait.eq("Lock")).limit(1).fetchOne(0, Integer.class);
                await("the concurrent build to wait on the blocker's lock", () -> buildPid.get() != null);
                Integer pid = buildPid.get();

                // In flight: the catalog lists the index, and it is not valid.
                assertThat(indexIsValid(targetIndex)).as("being built").isFalse();
                assertThat(sweep.refresh()).isTrue();
                assertThat(sweep.hasValidIndex(MODEL, TENANT, TARGET)).as("never routed to while building").isFalse();
                degradedArmsReturnRows("being built", hnswIndex);

                Boolean terminated = asSuperuser
                    .select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.val(pid)))
                    .fetchOne(0, Boolean.class);
                assertThat(terminated).isTrue();
                await("the build statement to end", build::isDone);
                assertThat(build).isCompletedExceptionally();
                try (Connection su = pg.createConnection("")) {
                    PgContainerHelper.clearSuperuserDdlOutsideTransactionLock(su);
                }
            } finally {
                blocker.rollback();
            }
        }

        assertThat(indexIsValid(targetIndex)).as("the terminated build left an invalid index").isFalse();
        assertThat(sweep.refresh()).isTrue();
        assertThat(sweep.hasValidIndex(MODEL, TENANT, TARGET)).isFalse();
        degradedArmsReturnRows("invalid", hnswIndex);

        ddl("DROP INDEX nexus." + targetIndex);
    }

    /**
     * With the TARGET collection's index not valid: the real router set sends the arm down the widest walk, a stale
     * set that still claims the index sends it down the serving walk, and both return rows. The second also
     * samples, and the planner skips an index that is not valid, so the check reads used=false and the plan names
     * the leaf's own HNSW index.
     */
    private void degradedArmsReturnRows(String state, String hnswIndex) {
        int k = 40;
        assertThat(search(realRepo(neverSample()), TARGET, 0, k)).as("%s: real set, ef 1000 walk", state).hasSize(k);

        int before = checkMessages().size();
        CapturingPlans plans = new CapturingPlans();
        PgVectorRepository stale = claimingRepo(new PciPlanCheck(1, plans));
        assertThat(search(stale, TARGET, 0, k)).as("%s: stale set, serving walk", state).hasSize(k);
        assertThat(plans.plans).hasSize(1);
        assertThat(plans.plans.get(0)).as("%s: the planner skips the index that is not valid", state)
            .doesNotContain(targetIndex).contains(hnswIndex);
        assertThat(checkMessages().subList(before, checkMessages().size())).anySatisfy(m -> assertThat(m)
            .contains("event=pci_plan_check").contains("used=false")
            .contains("index=" + targetIndex).contains("collection=" + TARGET));
    }

    // -- 4. the sampling cadence ----------------------------------------------------------------------------

    @Test
    @Order(6)
    void theCheckSamplesOneIndexedHnswArmInN_andNothingElse() {
        AtomicInteger explains = new AtomicInteger();
        PciPlanCheck check = new PciPlanCheck(3, (ctx, q) -> {
            explains.incrementAndGet();
            return "stub plan";
        });
        PgVectorRepository claiming = claimingRepo(check);

        for (int i = 0; i < 7; i++) {
            assertThat(search(claiming, SMALL, i % QUERIES, 20)).hasSize(20);
        }
        assertThat(explains.get()).as("arms 3 and 6 of 7 are sampled, a counter and not a coin").isEqualTo(2);

        // Not counted: a collection with no valid index (the real set has none for SMALL), a statement over
        // several collections, and an arm the router sends exact.
        PgVectorRepository real = new PgVectorRepository(scope, embedder, embedder, PciIndexSet.NONE, check);
        for (int i = 0; i < 6; i++) {
            assertThat(search(real, SMALL, 0, 20)).hasSize(20);
        }
        for (int i = 0; i < 6; i++) {
            assertThat(claiming.searchWithTokens(TENANT, "q0", List.of(SMALL, TARGET), 20, null, false).value())
                .hasSize(20);
        }
        PgSession.overrideSearchExactMaxRowsForTests(100_000);
        for (int i = 0; i < 6; i++) {
            assertThat(search(claiming, SMALL, 0, 20)).hasSize(20);
        }
        assertThat(explains.get()).as("no sample from an unindexed, multi-collection or exact-routed arm").isEqualTo(2);

        PgSession.overrideSearchExactMaxRowsForTests(ROUTER_T);
        assertThat(search(claiming, SMALL, 0, 20)).hasSize(20);
        assertThat(search(claiming, SMALL, 0, 20)).hasSize(20);
        assertThat(explains.get()).as("the exact-routed arms did not advance the counter: the 9th sampled arm is sampled")
            .isEqualTo(3);
    }

    // -- 5. a failed EXPLAIN never fails the arm ------------------------------------------------------------

    @Test
    @Order(7)
    void aFailedExplainAbortsItsSavepointOnly_theArmStillReturnsRows() {
        AtomicInteger calls = new AtomicInteger();
        // A real error on the arm's own connection: after it the transaction is aborted (25P02 on the next
        // statement) unless the failure was rolled back to a savepoint.
        PciPlanCheck check = new PciPlanCheck(1, (ctx, q) -> {
            calls.incrementAndGet();
            ctx.select(DSL.field("1/0", Integer.class)).fetch();
            return "unreachable";
        });
        PgVectorRepository repo = claimingRepo(check);

        List<Map<String, Object>> rows = search(repo, SMALL, 0, 40);

        assertThat(calls.get()).as("the EXPLAIN ran and failed").isEqualTo(1);
        assertThat(rows).as("the arm's statement ran after the failed EXPLAIN").hasSize(40);
        assertThat(checkMessages()).anySatisfy(m -> assertThat(m)
            .contains("event=pci_plan_check_failed").contains("index=" + smallIndex).contains("collection=" + SMALL));
        assertThat(checkMessages()).noneMatch(m -> m.contains("event=pci_plan_check ") && m.contains("used="));

        // The next arm is sampled again and returns rows: a failure is not sticky.
        assertThat(search(repo, SMALL, 1, 40)).hasSize(40);
        assertThat(calls.get()).isEqualTo(2);
    }

    @Test
    @Order(8)
    void aPlanSourceThatThrowsOutsideSqlAlsoLeavesTheArmIntact() {
        PciPlanCheck check = new PciPlanCheck(1, (ctx, q) -> {
            throw new IllegalStateException("plan source broke");
        });
        assertThat(search(claimingRepo(check), SMALL, 0, 40)).hasSize(40);
        assertThat(checkMessages()).anySatisfy(m -> assertThat(m).contains("event=pci_plan_check_failed"));
    }

    @Test
    @Order(9)
    void theSamplingIntervalMustBePositive() {
        assertThatThrownBy(() -> new PciPlanCheck(0, (ctx, q) -> "")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new PciPlanCheck(-1, (ctx, q) -> "")).isInstanceOf(IllegalArgumentException.class);
    }
}
