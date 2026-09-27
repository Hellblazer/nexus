// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.Query;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 4 (bead nexus-wbfpw.9): the EXPLAIN + recall + latency evidence
 * the bead's acceptance criteria requires for {@code nexus.chunk_is_live}, on a
 * moderate-scale (default 500-chunk, one-collection) fixture in the same style
 * as {@code HybridSearchFunctionParityIntegrationTest}'s own corpus (a sibling
 * fixture, per the bead's own instruction, rather than reusing that class's
 * private setup methods directly).
 *
 * <p><b>Fixture design.</b> {@link #LIVE_COUNT} live chunks (own-collection
 * manifest row, live document) plus one manifest-less "noise" chunk PER QUERY
 * (no manifest row anywhere -- R1's shape), each noise chunk's text set to the
 * EXACT query string it targets so its embedding is (near-)identical to the
 * query vector and it wins rank 1 in a plain nearest-neighbor search --
 * exactly the class of chunk {@code nexus.chunk_is_live} exists to exclude.
 *
 * <p><b>Recall methodology (avoids needing an independent raw-SQL oracle
 * query, so this file's raw-SQL footprint stays at 2 call sites, the
 * chunk_is_live-filtered KNN and its EXPLAIN twin):</b> {@code
 * nexus.plain_search_384} (today's shipped predicate) at {@code LIMIT
 * K+1=11} on the FULL corpus (live + noise) gives the noise chunk plus the
 * true top-10 live chunks in one call -- the noise chunk is guaranteed
 * present (plain_search's own anti-join has no manifest-less guard, Gap 1
 * item 1) and guaranteed closest (exact text match), so removing it from the
 * 11 leaves exactly the live-only oracle top-10. {@code plain_search_384} at
 * {@code LIMIT K=10} (today's actual production shape) is "before"; the raw
 * {@code nexus.chunk_is_live}-filtered KNN at {@code LIMIT K=10} is "after".
 * Recall@10 is measured against that oracle for both.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkIsLiveHnswExplainRecallIntegrationTest {

    private static final String TENANT = "chunkislive-explain";
    private static final String COLLECTION = "knowledge__chunkislive__minilm-l6-v2-384__v1";
    private static final int DIM = 384;

    private static final int LIVE_COUNT =
        Integer.getInteger("nx.chunkislive.explain.size", 500);
    private static final int QUERY_COUNT =
        Integer.getInteger("nx.chunkislive.explain.queries", 10);
    private static final int K = 10;
    private static final int LATENCY_ROUNDS =
        Integer.getInteger("nx.chunkislive.explain.rounds", 5);

    /** Separate, larger, random-vector fixture used ONLY by {@link
     *  #explain_chunkIsLivePredicate_usesHnswIndexScan}. At {@link #LIVE_COUNT}'s modest
     *  scale, PostgreSQL correctly prefers an Index Scan on {@code chunks_pk} (tenant_id +
     *  collection equality) followed by an in-memory Top-N sort over probing the HNSW
     *  graph -- sorting a few hundred already-materialized rows is cheaper than an ANN
     *  traversal, the SAME reason {@code HybridSearchFunctionParityIntegrationTest}'s own
     *  {@code explain_hybridSearchInlines_selectiveGateExactPlanNoHnsw} (amended 2026-08-18)
     *  no longer requires HNSW reachability at ITS fixture scale either. Seeded via {@link
     *  PgVectorRepository#upsertChunksWithVectors} (precomputed random unit vectors, no
     *  ONNX call) so a realistic row count is affordable in CI. */
    private static final int SCALE_COUNT =
        Integer.getInteger("nx.chunkislive.explain.scale", 5_000);
    private static final String COLLECTION_SCALE =
        "knowledge__chunkislive-scale__minilm-l6-v2-384__v1";

    private static final List<String> WORD_BANK = List.of(
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
        "yankee", "zulu", "cobalt", "quartz", "falcon", "harbor", "lantern",
        "marble", "nickel", "orchid", "pylon", "quiver", "raven", "saddle",
        "timber", "vortex", "willow");

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    OnnxEmbedder onnx;
    EmbedderRouter docRouter;
    EmbedderRouter queryRouter;
    PgVectorRepository pgRepo;

    final List<String> queries = new ArrayList<>();
    /** query index -> that query's own noise chunk's chash hex. */
    final List<String> noiseChashHex = new ArrayList<>();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        onnx = new OnnxEmbedder();
        docRouter = new EmbedderRouter(onnx, "document");
        queryRouter = new EmbedderRouter(onnx, "query");
        pgRepo = new PgVectorRepository(tenantScope, docRouter, queryRouter);

        seedCorpus();
        seedScaleFixture();
    }

    @AfterAll
    void stopAll() {
        if (onnx != null) onnx.close();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private void seedCorpus() throws Exception {
        try (Connection reg = pg.createConnection("")) {
            reg.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(reg, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }

        Random rnd = new Random(20260927L);
        Map<String, String> corpus = new LinkedHashMap<>();
        for (int d = 0; d < LIVE_COUNT; d++) {
            int len = 8 + rnd.nextInt(5);
            Set<String> words = new LinkedHashSet<>();
            while (words.size() < len) words.add(WORD_BANK.get(rnd.nextInt(WORD_BANK.size())));
            corpus.put(String.format("cil-doc-%05d", d), String.join(" ", words) + " doc" + d);
        }
        List<String> docTexts = new ArrayList<>(corpus.values());
        for (int q = 0; q < QUERY_COUNT; q++) {
            String[] w = docTexts.get((q * 7) % docTexts.size()).split(" ");
            queries.add(w[0] + " " + w[1] + " " + w[2]);
        }

        List<String> liveIds = new ArrayList<>(corpus.keySet());
        List<String> liveTexts = new ArrayList<>(corpus.values());
        List<String> liveChashes = liveIds.stream().map(id -> Chash.ofText(id).toHex()).toList();
        List<Map<String, Object>> liveMetas = new ArrayList<>();
        for (int i = 0; i < liveIds.size(); i++) liveMetas.add(Map.of());
        pgRepo.upsertChunks(TENANT, COLLECTION, liveChashes, liveTexts, liveMetas);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int i = 0; i < liveIds.size(); i++) {
                String id = liveIds.get(i);
                ctx.insertInto(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                    .set(CATALOG_DOCUMENTS.TUMBLER, id)
                    .set(CATALOG_DOCUMENTS.TITLE, "Doc")
                    .set(CATALOG_DOCUMENTS.CONTENT_TYPE, "prose")
                    .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION)
                    .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
                    .doNothing()
                    .execute();
                ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                        CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(TENANT, id, 0, HexFormat.of().parseHex(liveChashes.get(i)), COLLECTION)
                    .onConflict(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION)
                    .doNothing()
                    .execute();
            }
        }

        // One manifest-less "noise" chunk per query, text == the query text exactly
        // (near-zero embedding distance -- guaranteed rank 1 in an unfiltered KNN).
        // No catalog_documents / catalog_document_chunks row at all -- R1's shape.
        List<String> noiseChashes = new ArrayList<>();
        List<String> noiseTexts = new ArrayList<>();
        List<Map<String, Object>> noiseMetas = new ArrayList<>();
        for (String q : queries) {
            String chash = Chash.ofText("cil-noise-" + q).toHex();
            noiseChashHex.add(chash);
            noiseChashes.add(chash);
            noiseTexts.add(q);
            noiseMetas.add(Map.of());
        }
        pgRepo.upsertChunks(TENANT, COLLECTION, noiseChashes, noiseTexts, noiseMetas);
    }

    /** {@link #SCALE_COUNT} chunks with precomputed random unit vectors (no ONNX call),
     *  every one live (own-collection manifest row, live document) -- purely a plan-shape
     *  fixture, so semantic content of the text/vectors is irrelevant. */
    private void seedScaleFixture() throws Exception {
        try (Connection reg = pg.createConnection("")) {
            reg.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(reg, SQLDialect.POSTGRES), TENANT, COLLECTION_SCALE);
        }

        Random rnd = new Random(20260927002L);
        List<String> ids = new ArrayList<>(SCALE_COUNT);
        List<String> texts = new ArrayList<>(SCALE_COUNT);
        List<float[]> vectors = new ArrayList<>(SCALE_COUNT);
        List<Map<String, Object>> metas = new ArrayList<>(SCALE_COUNT);
        List<String> chashes = new ArrayList<>(SCALE_COUNT);
        for (int i = 0; i < SCALE_COUNT; i++) {
            String id = "cil-scale-" + i;
            ids.add(id);
            texts.add("scale fixture chunk " + i);
            vectors.add(randomUnitVector(rnd, DIM));
            metas.add(Map.of());
            chashes.add(Chash.ofText(id).toHex());
        }

        int batch = 500;
        for (int start = 0; start < SCALE_COUNT; start += batch) {
            int end = Math.min(start + batch, SCALE_COUNT);
            pgRepo.upsertChunksWithVectors(TENANT, COLLECTION_SCALE,
                chashes.subList(start, end), texts.subList(start, end),
                vectors.subList(start, end), metas.subList(start, end));
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<Query> docQueries = new ArrayList<>(SCALE_COUNT);
            List<Query> chunkQueries = new ArrayList<>(SCALE_COUNT);
            for (int i = 0; i < SCALE_COUNT; i++) {
                docQueries.add(ctx.insertInto(CATALOG_DOCUMENTS,
                        CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                        CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                    .values(TENANT, ids.get(i), "Doc", "prose", COLLECTION_SCALE));
                chunkQueries.add(ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                        CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                        CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                        CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                    .values(TENANT, ids.get(i), 0, HexFormat.of().parseHex(chashes.get(i)), COLLECTION_SCALE));
            }
            ctx.batch(docQueries).execute();
            ctx.batch(chunkQueries).execute();
            su.commit();
        }

        // The planner's own row-count estimate for the (tenant_id, collection) equality
        // on nexus.chunks is stale until ANALYZE runs (default/pre-insert statistics,
        // NOT this fixture's real cardinality) -- without this, the cost-based choice
        // between the chunks_pk-then-sort plan and the HNSW plan is uninformed and
        // never reflects true row count regardless of how large SCALE_COUNT is.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
    }

    private static float[] randomUnitVector(Random rnd, int dim) {
        float[] v = new float[dim];
        double sumSq = 0;
        for (int i = 0; i < dim; i++) {
            v[i] = (float) rnd.nextGaussian();
            sumSq += v[i] * v[i];
        }
        float norm = (float) Math.sqrt(sumSq);
        for (int i = 0; i < dim; i++) v[i] /= norm;
        return v;
    }

    private float[] embedQuery(String text) {
        return queryRouter.embedOneForCollection(tenantScope, TENANT, COLLECTION, text);
    }

    private static String vectorLiteral(float[] v) {
        StringBuilder sb = new StringBuilder(v.length * 8 + 2).append('[');
        for (int i = 0; i < v.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(v[i]);
        }
        return sb.append(']').toString();
    }

    /** "before": today's shipped predicate, {@code nexus.plain_search_384}, via the
     *  generated typed table function -- not raw SQL. */
    private List<String> plainSearch384(float[] vec, int n) {
        Table<?> fn = PLAIN_SEARCH_384.call(Vector.of(vec),
            new String[] {COLLECTION}, null, null, n);
        return tenantScope.withTenant(TENANT, ctx -> ctx.selectFrom(fn)
            .fetch(r -> r.get("id", String.class)));
    }

    /**
     * "after": a raw KNN query with {@code nexus.chunk_is_live(tenant, collection,
     * chash)} in the WHERE clause -- Step 5's not-yet-wired shape, gathered here
     * purely as evidence (this bead does not migrate any production call site).
     * SANCTIONED RAW (nexus-wbfpw.9, TEST-TREE RATCHET): no typed-DSL vector KNN
     * form exists in this codebase's test conventions (every sibling KNN plan-
     * shape test -- GraphHopParityTest, TaxonomyAssignCrossLateralHnswTest --
     * uses the identical raw-literal-SQL idiom for the same reason: the {@code
     * OPERATOR(nexus.<=>)} vector-distance ORDER BY has no jOOQ DSL operator).
     */
    private List<String> chunkIsLiveFilteredKnn(String collection, float[] vec, int n) {
        Result<Record> rows = tenantScope.withTenant(TENANT, ctx -> ctx.fetch(
            "SELECT encode(c.chash, 'hex') FROM nexus.chunks c"
            + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
            + " AND nexus.chunk_is_live(c.tenant_id, c.collection, c.chash)"
            + " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector"
            + " LIMIT ?",
            collection, vectorLiteral(vec), n));
        List<String> ids = new ArrayList<>(rows.size());
        for (var rec : rows) ids.add(rec.get(0, String.class));
        return ids;
    }

    /** EXPLAIN (ANALYZE, BUFFERS) twin of {@link #chunkIsLiveFilteredKnn} -- same
     *  statement body, ANALYZE-prefixed, {@code enable_seqscan=off} so index access
     *  paths are chosen at this fixture's scale (HybridSearchFunctionParityIntegrationTest's
     *  {@code explain()} precedent). SANCTIONED RAW (nexus-wbfpw.9, TEST-TREE RATCHET). */
    private String explainChunkIsLiveFilteredKnn(String collection, float[] vec, int n) {
        return tenantScope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "enable_seqscan", "off");
            Result<Record> rows = ctx.fetch(
                "EXPLAIN (ANALYZE, BUFFERS) SELECT encode(c.chash, 'hex') FROM nexus.chunks c"
                + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
                + " AND nexus.chunk_is_live(c.tenant_id, c.collection, c.chash)"
                + " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector"
                + " LIMIT ?",
                collection, vectorLiteral(vec), n);
            List<String> lines = new ArrayList<>();
            for (var rec : rows) lines.add(rec.get(0, String.class));
            return String.join("\n", lines);
        });
    }

    /** The live-only oracle top-{@link #K}: {@code plain_search_384} at LIMIT K + the
     *  full noise population's headroom, minus EVERY known noise chash (not only the
     *  query's own -- a query's text can be semantically close enough to ANOTHER
     *  query's noise chunk, drawn from the same shared word bank, to also intrude on
     *  its top-K window; excluding only "its own" noise chash undercounts recall_after
     *  by treating a correctly-excluded foreign noise chunk as a missed oracle member),
     *  truncated back to K. */
    private List<String> oracleTop10(float[] vec) {
        List<String> candidates = plainSearch384(vec, K + noiseChashHex.size());
        List<String> oracle = new ArrayList<>(candidates);
        oracle.removeAll(noiseChashHex);
        return oracle.size() > K ? oracle.subList(0, K) : oracle;
    }

    private static double recallAt10(List<String> topK, List<String> oracle) {
        long hits = topK.stream().filter(oracle::contains).count();
        return (double) hits / oracle.size();
    }

    private static long p50(List<Long> samplesMs) {
        List<Long> sorted = samplesMs.stream().sorted().toList();
        return sorted.get((int) Math.ceil(sorted.size() * 0.5) - 1);
    }

    // ── guard ───────────────────────────────────────────────────────────────

    @Test
    void guard_fixtureLoadedCorrectly() throws Exception {
        assertThat(pgRepo.count(TENANT, COLLECTION)).isEqualTo(LIVE_COUNT + QUERY_COUNT);
        assertThat(queries).hasSize(QUERY_COUNT);
        assertThat(noiseChashHex).hasSize(QUERY_COUNT);
    }

    // ── EXPLAIN: on the small, realistic-shape fixture, the predicate inlines ────
    // ── on the large fixture, the HNSW index itself is reached ───────────────

    /**
     * At {@link #LIVE_COUNT}'s modest, realistic-collection scale (500 chunks),
     * {@code chunk_is_live} inlines as a plain {@code Filter} on the SAME
     * {@code chunks_pk} index-scan node PostgreSQL already chooses for the
     * {@code tenant_id}/{@code collection} equality -- exactly the "not a view
     * join" structural claim the RDR's Technical Design worries about ("Risk:
     * live(c) as a view join breaks HNSW binds"): a view join would show as a
     * SEPARATE join node forcing a materialization boundary between {@code
     * nexus.chunks} and the predicate; here the predicate rides along on the
     * base table's own scan. PostgreSQL correctly prefers this Top-N-sort shape
     * over an HNSW probe at this row count -- sorting ~500 already-selected
     * rows is cheaper than an ANN traversal, the identical reasoning behind
     * {@code HybridSearchFunctionParityIntegrationTest}'s own {@code
     * explain_hybridSearchInlines_selectiveGateExactPlanNoHnsw} (amended
     * 2026-08-18) no longer requiring HNSW reachability at ITS fixture scale.
     */
    @Test
    void explain_chunkIsLivePredicate_inlinesAsAFilter_onTheRealisticFixture() throws Exception {
        float[] vec = embedQuery(queries.get(0));
        String plan = explainChunkIsLiveFilteredKnn(COLLECTION, vec, K);

        System.out.println("[nexus-wbfpw.9 EXPLAIN, " + (LIVE_COUNT + QUERY_COUNT)
            + " chunks] chunk_is_live-filtered KNN plan:\n" + plan);

        assertThat(plan)
            .as("chunk_is_live must INLINE as a Filter on the base table's own scan node --"
                + " NOT a separate join/materialization boundary (the view-join risk RDR-192's"
                + " Technical Design names). Plan was:%n%s", plan)
            .contains("Filter:")
            .contains("chunk_is_live")
            .doesNotContain("Function Scan");
        assertThat(plan)
            .as("the base table scan must be an index scan, not an unqualified sequential"
                + " scan of nexus.chunks. Plan was:%n%s", plan)
            .contains("Index Scan");
    }

    /**
     * At {@link #SCALE_COUNT}'s larger scale (default 5,000 chunks, one
     * collection), the SAME query -- {@code enable_seqscan=off} still set, per
     * this suite's established convention -- is expensive enough for
     * PostgreSQL's own cost model to choose the {@code idx_chunks_embedding_384}
     * HNSW index directly, satisfying the bead's acceptance criterion literally:
     * the predicate does not defeat the HNSW index bind at the scale where HNSW
     * actually matters.
     */
    @Test
    void explain_chunkIsLivePredicate_usesHnswIndexScan_onTheLargeFixture() throws Exception {
        float[] vec = randomUnitVector(new Random(20260927003L), DIM);
        String plan = explainChunkIsLiveFilteredKnn(COLLECTION_SCALE, vec, K);

        System.out.println("[nexus-wbfpw.9 EXPLAIN, " + SCALE_COUNT
            + " chunks] chunk_is_live-filtered KNN plan:\n" + plan);

        assertThat(plan)
            .as("HNSW index scan must survive the chunk_is_live predicate at a scale where"
                + " HNSW actually matters (RDR-192 Technical Design 'Risk: live(c) as a view"
                + " join breaks HNSW binds' -- this is a function, not a view, precisely to"
                + " avoid that). Plan was:%n%s", plan)
            .contains("idx_chunks_embedding_384");
        assertThat(plan)
            .as("no sequential scan of nexus.chunks (enable_seqscan=off forces index access"
                + " paths). Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks")
            .doesNotContain("Seq Scan on nexus.chunks");
        assertThat(plan)
            .as("chunk_is_live must INLINE -- no opaque Function Scan node boundary"
                + " (vectors-009's own precedent: 'no Function Scan, HNSW index survives')."
                + " Plan was:%n%s", plan)
            .doesNotContain("Function Scan");
    }

    // ── recall@10: before (plain_search_384, today's shipped shape) vs after (chunk_is_live) ──

    @Test
    void recall_chunkIsLiveExcludesManifestLessNoise_plainSearchDoesNot() throws Exception {
        List<Double> recallBefore = new ArrayList<>();
        List<Double> recallAfter = new ArrayList<>();

        for (int q = 0; q < queries.size(); q++) {
            float[] vec = embedQuery(queries.get(q));
            List<String> oracle = oracleTop10(vec);
            List<String> before = plainSearch384(vec, K);
            List<String> after = chunkIsLiveFilteredKnn(COLLECTION, vec, K);

            assertThat(before)
                .as("query %d: plain_search_384 (today's shipped predicate) has no"
                    + " manifest-less guard (Gap 1 item 1) -- its own noise chunk must be"
                    + " present", q)
                .contains(noiseChashHex.get(q));
            assertThat(after)
                .as("query %d: nexus.chunk_is_live must exclude the manifest-less noise chunk", q)
                .doesNotContain(noiseChashHex.get(q));

            recallBefore.add(recallAt10(before, oracle));
            recallAfter.add(recallAt10(after, oracle));
        }

        double avgBefore = recallBefore.stream().mapToDouble(Double::doubleValue).average().orElseThrow();
        double avgAfter = recallAfter.stream().mapToDouble(Double::doubleValue).average().orElseThrow();

        System.out.println("[nexus-wbfpw.9 RECALL@10] before(plain_search_384)=" + avgBefore
            + " after(chunk_is_live)=" + avgAfter + " over " + queries.size() + " queries");

        assertThat(avgBefore)
            .as("sanity: the noise fixture must actually degrade plain_search_384's recall"
                + " against the live-only oracle, or this test proves nothing")
            .isLessThan(1.0);
        assertThat(avgAfter)
            .as("nexus.chunk_is_live must recover full recall@10 against the live-only oracle")
            .isEqualTo(1.0);
    }

    // ── p50 latency: before vs after ─────────────────────────────────────────

    @Test
    void latency_p50_beforeAndAfter_reported() throws Exception {
        // Warm-up, excluded from measurement.
        for (String q : queries) {
            float[] vec = embedQuery(q);
            plainSearch384(vec, K);
            chunkIsLiveFilteredKnn(COLLECTION, vec, K);
        }

        List<Long> beforeMs = new ArrayList<>();
        List<Long> afterMs = new ArrayList<>();
        for (int round = 0; round < LATENCY_ROUNDS; round++) {
            for (String q : queries) {
                float[] vec = embedQuery(q);

                long t0 = System.nanoTime();
                plainSearch384(vec, K);
                beforeMs.add((System.nanoTime() - t0) / 1_000_000L);

                long t1 = System.nanoTime();
                chunkIsLiveFilteredKnn(COLLECTION, vec, K);
                afterMs.add((System.nanoTime() - t1) / 1_000_000L);
            }
        }

        long p50Before = p50(beforeMs);
        long p50After = p50(afterMs);

        System.out.println("[nexus-wbfpw.9 LATENCY] samples=" + beforeMs.size()
            + " plain_search_384.p50=" + p50Before + "ms chunk_is_live_knn.p50=" + p50After + "ms"
            + " (fixture: " + (LIVE_COUNT + QUERY_COUNT) + " chunks, one collection)");

        // No hard bound asserted here -- this is evidence for the close note (any recall
        // drop or latency increase is reported to Sam before S5/nexus-wbfpw.10 merges,
        // per the bead's acceptance criteria), not a regression gate.
        assertThat(p50Before).isGreaterThanOrEqualTo(0);
        assertThat(p50After).isGreaterThanOrEqualTo(0);
    }
}
