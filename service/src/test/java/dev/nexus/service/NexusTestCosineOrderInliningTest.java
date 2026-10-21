// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.Routines;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record1;
import org.jooq.SQLDialect;
import org.jooq.Select;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.HexFormat;
import java.util.List;
import java.util.Random;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-cbo4a batch D, step 1: the falsification test for {@code nexus_test.cosine_order}.
 *
 * <p>Hypothesis: {@code nexus_test.cosine_order(a, b)} is a LANGUAGE sql, IMMUTABLE, non-STRICT function with no SET
 * clause whose body is {@code a OPERATOR(nexus.<=>) b}; PostgreSQL substitutes the body before it matches the ORDER BY
 * against the index's pathkeys, so an HNSW index serves {@code ORDER BY nexus_test.cosine_order(embedding_384, $v)
 * LIMIT k} exactly as it serves the bare operator. If that holds, the batch-D KNN tests (GraphHop 659,
 * ChunkLiveOwnersInlineRecall 277/304, Msz9i 478/528/540/556) can be written as typed jOOQ; if it fails, they fall back
 * to per-shape plpgsql functions.
 *
 * <p><b>How the test can tell inlined from not inlined.</b> An inlined call leaves no trace of the function's name:
 * the plan carries the operator, in an {@code Order By:} line under an {@code Index Scan using ...embedding_384...}
 * node. A call that was not inlined is an opaque expression: the plan names the function in a {@code Sort Key:} line
 * above a scan, and has no {@code Order By:} line. {@code nexus_test.cosine_order_blocked}, the same body with a SET
 * clause (the documented blocker), is the control that proves those assertions can fail: its plan is asserted to show
 * exactly the not-inlined signature. {@code Routines.cosineDistance1}, pgvector's C function, is the second control:
 * an index never serves it.
 *
 * <p><b>Two plan modes.</b> The NATURAL plan is the planner's own cost choice, which is what a converted test runs;
 * the fixture is sized like PgVectorRepositoryRawSqlPlanShapeTest's (4000 rows in one leaf) where the planner picks
 * the index for the plain operator. The REACHABLE plan penalizes every other access path (sequential and bitmap
 * scans, explicit sorts), so the assertion no longer depends on the cost model: with the alternatives off, the index
 * is used if and only if the ORDER BY can be matched to it. Each is explained in two forms: bound (jOOQ's prepared
 * statement, how a test executes it) and inlined (every bind rendered as a literal through
 * {@link PgContainerHelper#explainPlanWith}, which also runs the statement under ANALYZE).
 *
 * <p><b>The HNSW index.</b> No index is created here. nexus.chunks is partitioned by embedding model then tenant, and
 * the product's HNSW index on the parent is attached to every leaf, so registering a collection and inserting its
 * chunks gives the (model, tenant) leaf its {@code ...embedding_384...} HNSW index, the same fixture path as
 * PgVectorRepositoryRawSqlPlanShapeTest. {@link #fixtureIndexServesThePlainOperator} proves that index serves the
 * operator before any verdict is read from cosine_order.
 *
 * <p><b>Recall.</b> HNSW is approximate. The executed comparison uses a fixture whose vectors live in the first
 * {@link #LIVE_DIMS} dimensions of the 384 (the rest are zero), where a graph search is reliable, a small k, and
 * {@code hnsw.ef_search} {@value #EF_SEARCH} (10% of the rows), so the index's top k equals the exact top k with
 * effectively no failure probability; the reachable plan is asserted inside the same transaction so the rows came
 * from the index.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NexusTestCosineOrderInliningTest {

    private static final String SVC_ROLE = "svc_cosine_order_test";
    private static final String SVC_PASS = "svc_cosine_order_pass";
    private static final String TENANT = "cosine-order-tenant";
    private static final String COLLECTION = "knowledge__cosineorder__minilm-l6-v2-384__v1";
    private static final String MODEL = "minilm-l6-v2-384";

    private static final int DIM = 384;
    /** The number of leading components that carry signal; the other {@code DIM - LIVE_DIMS} are zero. */
    private static final int LIVE_DIMS = 12;
    private static final int ROWS = 4_000;
    /** Rows per INSERT statement (6 binds each, well under the 65535-bind limit). */
    private static final int BATCH = 250;
    private static final int K_PLAN = 10;
    private static final int K_EXEC = 5;
    private static final int EF_SEARCH = 400;

    private static final String INDEX_SCAN = "Index Scan using \"?\\S*embedding_384";
    private static final String ORDER_BY_OPERATOR = "Order By:.*<=>";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    Vector queryVector;

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
        cfg.setMaximumPoolSize(3);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        queryVector = liveVector(new Random(20261020L));
        seed();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** A 384-wide vector whose first {@link #LIVE_DIMS} components are uniform in [-1, 1) and the rest zero. */
    private static Vector liveVector(Random rnd) {
        float[] f = new float[DIM];
        for (int k = 0; k < LIVE_DIMS; k++) {
            f[k] = rnd.nextFloat() * 2f - 1f;
        }
        return Vector.of(f);
    }

    private void seed() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION, MODEL);
            String model = PgContainerHelper.collectionModel(ctx, TENANT, COLLECTION);
            Random rnd = new Random(42L);
            for (int first = 0; first < ROWS; first += BATCH) {
                var ins = ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                    CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384);
                for (int i = first; i < first + BATCH && i < ROWS; i++) {
                    ins = ins.values(TENANT, COLLECTION, Chash.ofText("cosine-order-row-" + i).toBytes(), model,
                        "cosine order row " + i, liveVector(rnd));
                }
                ins.execute();
            }
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    // ── query builders ───────────────────────────────────────────────────────

    /** {@code nexus_test.<function>(embedding_384, <v bound through the generated VectorBinding>)}. */
    private static Field<Double> viaTestFunction(String function, Vector v) {
        return DSL.function(DSL.name("nexus_test", function), SQLDataType.DOUBLE, CHUNKS.EMBEDDING_384,
            DSL.val(v, CHUNKS.EMBEDDING_384.getDataType()));
    }

    /** pgvector's C function {@code nexus.cosine_distance}: the same values as the operator, never index-served. */
    private static Field<Double> viaCosineDistanceFunction(Vector v) {
        return Routines.cosineDistance1(CHUNKS.EMBEDDING_384, DSL.val(v, CHUNKS.EMBEDDING_384.getDataType()));
    }

    /** The KNN shape the converted tests share: one collection's chunks with an embedding, nearest {@code limit}. */
    private static Select<Record1<byte[]>> knn(DSLContext ctx, Field<Double> orderBy, int limit) {
        return ctx.select(CHUNKS.CHASH)
            .from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(TENANT))
            .and(CHUNKS.COLLECTION.eq(COLLECTION))
            .and(CHUNKS.EMBEDDING_MODEL.eq(MODEL))
            .and(CHUNKS.EMBEDDING_384.isNotNull())
            .orderBy(orderBy.asc())
            .limit(limit);
    }

    // ── plans ────────────────────────────────────────────────────────────────

    /** Turns off every access path that is not the index scan, so the plan no longer depends on the cost model. */
    private static void onlyTheIndexScan(DSLContext ctx) {
        for (String guc : List.of("enable_seqscan", "enable_bitmapscan", "enable_sort")) {
            PgSession.setLocal(ctx, guc, "off");
        }
    }

    private String boundPlan(Field<Double> orderBy, boolean reachable) {
        return tenantScope.withTenant(TENANT, ctx -> {
            if (reachable) onlyTheIndexScan(ctx);
            return PgContainerHelper.explainPlan(ctx, knn(ctx, orderBy, K_PLAN));
        });
    }

    private String inlinedAnalyzedPlan(Field<Double> orderBy, boolean reachable) {
        return tenantScope.withTenant(TENANT, ctx -> {
            if (reachable) onlyTheIndexScan(ctx);
            return PgContainerHelper.explainPlanWith(ctx, knn(ctx, orderBy, K_PLAN),
                PgContainerHelper.ExplainOptions.ANALYZE_VERBOSE_COSTS_OFF);
        });
    }

    // ── assertions shared by the plans ───────────────────────────────────────

    /** What an inlined, index-served ORDER BY looks like. */
    private static void assertInlinedAndIndexServed(String plan, String what) {
        assertThat(plan)
            .as("%s: the function name must be gone from the plan (inline_function substituted its body)."
                + " Plan was:%n%s", what, plan)
            .doesNotContain("cosine_order");
        assertThat(plan)
            .as("%s: the HNSW index on embedding_384 must serve the scan. Plan was:%n%s", what, plan)
            .containsPattern(INDEX_SCAN);
        assertThat(plan)
            .as("%s: the index scan must carry the ORDER BY as an operator Order By line. Plan was:%n%s", what, plan)
            .containsPattern(ORDER_BY_OPERATOR);
    }

    /** What an ORDER BY the planner cannot match to the index looks like: the expression is sorted, never indexed. */
    private static void assertSortedNotIndexServed(String plan, String what) {
        assertThat(plan)
            .as("%s: the ORDER BY must be an explicit sort. Plan was:%n%s", what, plan)
            .contains("Sort Key:");
        assertThat(plan)
            .as("%s: no index scan may carry an Order By line. Plan was:%n%s", what, plan)
            .doesNotContainPattern(ORDER_BY_OPERATOR);
        assertThat(plan)
            .as("%s: no HNSW index scan on embedding_384. Plan was:%n%s", what, plan)
            .doesNotContainPattern(INDEX_SCAN);
    }

    // ── the fixture ──────────────────────────────────────────────────────────

    /**
     * Inconclusive-guard: before any verdict on cosine_order, the bare operator (inside the product's
     * plain_search_384, the function production calls) must be served by the fixture's HNSW index at the planner's
     * own choice. A failure here is a fixture problem, not a cosine_order result.
     */
    @Test
    void fixtureIndexServesThePlainOperator() {
        Table<?> fn = PLAIN_SEARCH_384.call(queryVector, new String[] {COLLECTION}, null, null, K_PLAN, MODEL, TENANT);
        String plan = tenantScope.withTenant(TENANT,
            ctx -> PgContainerHelper.explainPlan(ctx, ctx.select(fn.field("id")).from(fn)));
        assertThat(plan)
            .as("the fixture's leaf HNSW index must serve the operator-ordered KNN at the planner's own choice;"
                + " if this fails the fixture is wrong and no cosine_order verdict below means anything."
                + " Plan was:%n%s", plan)
            .containsPattern(INDEX_SCAN);
    }

    // ── the hypothesis ───────────────────────────────────────────────────────

    @Test
    void cosineOrder_naturalPlan_boundForm_isInlinedAndServedByHnsw() {
        assertInlinedAndIndexServed(boundPlan(viaTestFunction("cosine_order", queryVector), false),
            "cosine_order, natural plan, bound");
    }

    @Test
    void cosineOrder_naturalPlan_inlinedLiterals_analyzed_isInlinedAndServedByHnsw() {
        String plan = inlinedAnalyzedPlan(viaTestFunction("cosine_order", queryVector), false);
        assertInlinedAndIndexServed(plan, "cosine_order, natural plan, literals, ANALYZE");
        assertThat(plan)
            .as("ANALYZE must have run the index scan, not only planned it. Plan was:%n%s", plan)
            .containsPattern("Index Scan using.*actual time=");
    }

    @Test
    void cosineOrder_reachablePlan_boundForm_isInlinedAndServedByHnsw() {
        assertInlinedAndIndexServed(boundPlan(viaTestFunction("cosine_order", queryVector), true),
            "cosine_order, alternatives off, bound");
    }

    @Test
    void cosineOrder_reachablePlan_inlinedLiterals_analyzed_isInlinedAndServedByHnsw() {
        assertInlinedAndIndexServed(inlinedAnalyzedPlan(viaTestFunction("cosine_order", queryVector), true),
            "cosine_order, alternatives off, literals, ANALYZE");
    }

    // ── the controls: the assertions above must be able to fail ──────────────

    /** pgvector's C function is never matched to an HNSW index, even with every alternative off. */
    @Test
    void cosineDistanceFunction_isSortedNeverIndexServed() {
        Field<Double> order = viaCosineDistanceFunction(queryVector);
        assertSortedNotIndexServed(boundPlan(order, false), "cosine_distance, natural plan, bound");
        assertSortedNotIndexServed(boundPlan(order, true), "cosine_distance, alternatives off, bound");
        assertSortedNotIndexServed(inlinedAnalyzedPlan(order, true), "cosine_distance, alternatives off, literals");
    }

    /**
     * The detector's own control: the same body behind a SET clause is not inlined, and its plan shows exactly the
     * signature {@link #assertInlinedAndIndexServed} refuses: the function's name, a sort, no Order By line.
     */
    @Test
    void blockedTwin_isNotInlined_andTheAssertionsSeeIt() {
        Field<Double> order = viaTestFunction("cosine_order_blocked", queryVector);
        for (boolean reachable : new boolean[] {false, true}) {
            String bound = boundPlan(order, reachable);
            assertThat(bound)
                .as("a function with a SET clause is opaque to the planner and its name stays in the plan."
                    + " Plan was:%n%s", bound)
                .contains("cosine_order_blocked");
            assertSortedNotIndexServed(bound, "cosine_order_blocked, alternatives off=" + reachable);
        }
    }

    // ── the result: index-served cosine_order returns the exact top-k ────────

    @Test
    void cosineOrder_indexServedTopK_equalsTheExactTopK() {
        Field<Double> viaIndex = viaTestFunction("cosine_order", queryVector);

        List<String> indexed = tenantScope.withTenant(TENANT, ctx -> {
            onlyTheIndexScan(ctx);
            PgSession.setLocal(ctx, "hnsw.ef_search", Integer.toString(EF_SEARCH));
            // The rows below must come from the index: the same transaction, the same settings, the same query.
            String plan = PgContainerHelper.explainPlan(ctx, knn(ctx, viaIndex, K_EXEC));
            assertInlinedAndIndexServed(plan, "cosine_order top-k execution");
            return hex(knn(ctx, viaIndex, K_EXEC).fetch(CHUNKS.CHASH));
        });

        // The oracle: pgvector's C function sorts every row, so this is the exact order, unaffected by the graph.
        List<String> exact = tenantScope.withTenant(TENANT,
            ctx -> hex(knn(ctx, viaCosineDistanceFunction(queryVector), K_EXEC).fetch(CHUNKS.CHASH)));

        assertThat(exact).as("the oracle returns k distinct rows").hasSize(K_EXEC).doesNotHaveDuplicates();
        assertThat(indexed)
            .as("the HNSW-served ORDER BY cosine_order(...) must return the exact nearest %d, nearest first", K_EXEC)
            .isEqualTo(exact);
    }

    private static List<String> hex(List<byte[]> chashes) {
        HexFormat hex = HexFormat.of();
        return chashes.stream().map(hex::formatHex).toList();
    }
}
