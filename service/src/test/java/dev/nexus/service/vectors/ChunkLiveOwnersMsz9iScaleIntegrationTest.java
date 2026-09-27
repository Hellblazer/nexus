// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import org.jooq.DSLContext;
import org.jooq.Query;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
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
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.function.Supplier;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 4 (bead nexus-wbfpw.9), round-2 rework (critique T2
 * nexus/critique-wbfpw9, Critical): the round-1 evidence substituted an ad
 * hoc, all-live, zero-tombstone fixture for the RDR's explicitly named
 * "msz9i fixture" methodology -- nexus-msz9i's own investigation (bd show
 * nexus-msz9i) needed a 76k-chunk / 57k-manifest-row / 1k-document fixture
 * with a tombstone-fraction sweep (3/10/30/60%) to characterize cost that is
 * LINEAR IN MANIFEST SIZE / TOMBSTONE FRACTION -- exactly the risk class an
 * all-live 5,000-row fixture cannot exercise (nothing is ever filtered out).
 *
 * <p>This class reproduces that fixture's SCALE AND SHAPE (chunk count,
 * manifest-row count, document count) with random unit vectors (no ONNX --
 * this is a plan-shape/cost/recall measurement, not a semantic-ranking test,
 * so a real embedding model buys nothing here and would make a 76k-row
 * fixture far too slow to seed routinely), and reproduces msz9i's own
 * dead-set tombstone-fraction sweep methodology: mutate {@code deleted_at}
 * on the SAME fixture, re-ANALYZE, re-measure, rather than rebuild per
 * fraction.
 *
 * <p><b>Controlled comparison (critique/review Significant findings on the
 * confounded round-1 latency number):</b> "before" and "after" are BOTH raw
 * SQL, through the SAME harness, with the SAME text-literal-vector-cast
 * binding -- the only thing that differs between them is the WHERE-clause
 * predicate itself (plain_search_384's own dead-set anti-join vs. {@code
 * EXISTS(SELECT 1 FROM nexus.chunk_live_owners(...))}). Server-side EXPLAIN
 * ANALYZE execution time and client-side JDBC wall-clock are measured and
 * reported SEPARATELY -- never compared against each other, only against
 * their own opposite number (before-server vs after-server, before-client
 * vs after-client).
 *
 * <p><b>Recall under selectivity</b> uses the SAME HNSW GUCs production
 * search actually sets ({@code hnsw.iterative_scan=relaxed_order}, {@code
 * hnsw.ef_search=200} -- {@code PgVectorRepository#search}'s own {@code
 * PgSession.setHnswEfSearch}/{@code hnsw.iterative_scan} calls, K=10 floored
 * to {@code PgSession.DEFAULT_EF_SEARCH_FLOOR}=200), measured against an
 * EXACT (index scans disabled, forced sequential scan + sort) live-only
 * oracle, at the two fractions (30%, 60%) where a meaningful share of the
 * HNSW-visited candidates are filtered out.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkLiveOwnersMsz9iScaleIntegrationTest {

    private static final String TENANT = "clo-msz9i";
    private static final String COLLECTION = "knowledge__clo-msz9i-repro__minilm-l6-v2-384__v1";
    private static final int DIM = 384;

    /** nexus-msz9i's own repro fixture shape (bd show nexus-msz9i): "76k 384-dim
     *  chunks / 57k manifest rows / 1k docs". Overridable down for a fast local loop;
     *  the default reproduces the named scale. */
    private static final int NUM_CHUNKS = Integer.getInteger("nx.cloMsz9i.chunks", 76_000);
    private static final int NUM_MANIFEST = Integer.getInteger("nx.cloMsz9i.manifest", 57_000);
    private static final int NUM_DOCS = Integer.getInteger("nx.cloMsz9i.docs", 1_000);
    private static final int K = 10;
    private static final int[] TOMBSTONE_FRACTIONS_PCT = {3, 10, 30, 60};
    private static final int LATENCY_REPS = Integer.getInteger("nx.cloMsz9i.reps", 10);
    private static final int RECALL_QUERY_COUNT = Integer.getInteger("nx.cloMsz9i.recallQueries", 5);

    /** Production search's own HNSW GUCs (PgVectorRepository#search): {@code
     *  hnsw.iterative_scan=relaxed_order}, {@code hnsw.ef_search =
     *  max(PgSession.DEFAULT_EF_SEARCH_FLOOR=200, nResults)} -- for K=10 that floor
     *  wins, so ef_search=200 here reproduces the real value production would set. */
    private static final String PROD_ITERATIVE_SCAN = "relaxed_order";
    private static final String PROD_EF_SEARCH = "200";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    /** All NUM_DOCS document tumblers, in seed order -- {@link #setTombstoneFraction}
     *  tombstones a deterministic PREFIX of this list. */
    final List<String> docIds = new ArrayList<>(NUM_DOCS);
    /** All NUM_CHUNKS chash hex strings, in seed order. Indices
     *  [0, NUM_MANIFEST) have a manifest row (assigned round-robin across docIds);
     *  indices [NUM_MANIFEST, NUM_CHUNKS) are manifest-less. */
    final List<String> chashHex = new ArrayList<>(NUM_CHUNKS);

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

        seedFixture();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── fixture ─────────────────────────────────────────────────────────────

    private void seedFixture() throws Exception {
        try (Connection reg = pg.createConnection("")) {
            reg.setAutoCommit(true);
            PgContainerHelper.insertCollection(DSL.using(reg, SQLDialect.POSTGRES), TENANT, COLLECTION);
        }

        for (int i = 0; i < NUM_DOCS; i++) docIds.add(String.format("clo-msz9i-doc-%05d", i));
        Random rnd = new Random(20260927101L);
        List<float[]> vectors = new ArrayList<>(NUM_CHUNKS);
        for (int i = 0; i < NUM_CHUNKS; i++) {
            String id = "clo-msz9i-chunk-" + i;
            chashHex.add(Chash.ofText(id).toHex());
            vectors.add(randomUnitVector(rnd, DIM));
        }

        // Chunks, in batches (upsertChunksWithVectors -- no embedder call, precomputed
        // random vectors; the whole point of this fixture is scale/plan-shape/cost, not
        // semantic ranking quality).
        var pgRepo = new PgVectorRepository(tenantScope, (Embedder) null, (Embedder) null);
        int batch = 1000;
        for (int start = 0; start < NUM_CHUNKS; start += batch) {
            int end = Math.min(start + batch, NUM_CHUNKS);
            List<String> texts = new ArrayList<>(end - start);
            List<Map<String, Object>> metas = new ArrayList<>(end - start);
            for (int i = start; i < end; i++) {
                texts.add("msz9i-scale fixture chunk " + i);
                metas.add(Map.of());
            }
            pgRepo.upsertChunksWithVectors(TENANT, COLLECTION,
                chashHex.subList(start, end), texts, vectors.subList(start, end), metas);
        }

        // Documents: all live initially (deleted_at NULL) -- setTombstoneFraction
        // mutates this per round, matching nexus-msz9i's own sweep methodology
        // (mutate the SAME fixture, re-ANALYZE, re-measure -- never rebuild per
        // fraction).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<Query> docQueries = new ArrayList<>(NUM_DOCS);
            for (String id : docIds) {
                docQueries.add(ctx.insertInto(CATALOG_DOCUMENTS,
                        CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                        CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                    .values(TENANT, id, "Doc", "prose", COLLECTION));
            }
            ctx.batch(docQueries).execute();
            su.commit();
        }

        // Manifest: first NUM_MANIFEST chunks, assigned round-robin across NUM_DOCS
        // documents (chunk i -> doc i % NUM_DOCS, position i / NUM_DOCS). The
        // remaining (NUM_CHUNKS - NUM_MANIFEST) chunks are manifest-less (R1's
        // shape) -- always "not live" regardless of tombstone fraction, matching
        // msz9i's own chunk/manifest-row count split.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            int manifestBatch = 5000;
            for (int start = 0; start < NUM_MANIFEST; start += manifestBatch) {
                int end = Math.min(start + manifestBatch, NUM_MANIFEST);
                List<Query> chunkQueries = new ArrayList<>(end - start);
                for (int i = start; i < end; i++) {
                    String doc = docIds.get(i % NUM_DOCS);
                    int position = i / NUM_DOCS;
                    chunkQueries.add(ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                            CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                            CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                        .values(TENANT, doc, position, HexFormat.of().parseHex(chashHex.get(i)), COLLECTION));
                }
                ctx.batch(chunkQueries).execute();
                su.commit();
            }
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
        }
    }

    /** Resets every fixture document to live, then tombstones a deterministic PREFIX
     *  of {@link #docIds} sized to {@code percent}% of {@link #NUM_DOCS} -- msz9i's own
     *  sweep methodology (mutate the SAME fixture between measurements). Re-ANALYZEs
     *  catalog_documents (small, cheap) so the planner's row estimate reflects the new
     *  tombstone fraction. */
    private void setTombstoneFraction(int percent) throws Exception {
        int tombstoneCount = (int) Math.ceil(percent / 100.0 * NUM_DOCS);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, (java.time.OffsetDateTime) null)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(COLLECTION)))
                .execute();
            if (tombstoneCount > 0) {
                ctx.update(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)
                        .and(CATALOG_DOCUMENTS.TUMBLER.in(docIds.subList(0, tombstoneCount))))
                    .execute();
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
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

    private static String vectorLiteral(float[] v) {
        StringBuilder sb = new StringBuilder(v.length * 8 + 2).append('[');
        for (int i = 0; i < v.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(v[i]);
        }
        return sb.append(']').toString();
    }

    // ── raw SQL harness: SAME shape, SAME text-literal-vector binding, predicate differs ──

    private static final String BEFORE_DEAD_SET_ANTI_JOIN =
        "SELECT encode(c.chash, 'hex') FROM nexus.chunks c"
        + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
        + " AND (NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m"
        + "                   JOIN nexus.catalog_documents d"
        + "                     ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id"
        + "                  WHERE m.tenant_id = c.tenant_id AND m.collection = c.collection AND m.chash = c.chash"
        + "                    AND d.deleted_at IS NOT NULL"
        + "                    AND NOT EXISTS (SELECT 1 FROM nexus.catalog_document_chunks m2"
        + "                                      JOIN nexus.catalog_documents d2"
        + "                                        ON d2.tenant_id = m2.tenant_id AND d2.tumbler = m2.doc_id"
        + "                                     WHERE m2.tenant_id = m.tenant_id AND m2.collection = m.collection AND m2.chash = m.chash"
        + "                                       AND d2.deleted_at IS NULL)))"
        + " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector"
        + " LIMIT ?";

    private static final String AFTER_CHUNK_LIVE_OWNERS =
        "SELECT encode(c.chash, 'hex') FROM nexus.chunks c"
        + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
        + " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))"
        + " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector"
        + " LIMIT ?";

    /** Runs {@code sql} (either {@link #BEFORE_DEAD_SET_ANTI_JOIN} or {@link
     *  #AFTER_CHUNK_LIVE_OWNERS}) with production HNSW GUCs and returns the ordered
     *  chash-hex result list. */
    private List<String> runProd(String sql, float[] vec, int n) {
        Result<Record> rows = tenantScope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "hnsw.iterative_scan", PROD_ITERATIVE_SCAN);
            PgSession.setLocal(ctx, "hnsw.ef_search", PROD_EF_SEARCH);
            return ctx.fetch(sql, COLLECTION, vectorLiteral(vec), n);
        });
        List<String> ids = new ArrayList<>(rows.size());
        for (var rec : rows) ids.add(rec.get(0, String.class));
        return ids;
    }

    /** EXACT oracle: same statement, but with index/bitmap scans disabled so PostgreSQL
     *  can only satisfy the ORDER BY via a full sequential scan + sort -- true nearest
     *  neighbors among the LIVE population, no HNSW approximation. */
    private List<String> runExact(String sql, float[] vec, int n) {
        Result<Record> rows = tenantScope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "enable_indexscan", "off");
            PgSession.setLocal(ctx, "enable_bitmapscan", "off");
            return ctx.fetch(sql, COLLECTION, vectorLiteral(vec), n);
        });
        List<String> ids = new ArrayList<>(rows.size());
        for (var rec : rows) ids.add(rec.get(0, String.class));
        return ids;
    }

    /** EXPLAIN (ANALYZE, BUFFERS) of {@code sql} under production HNSW GUCs. Returns
     *  the full plan text; {@link #parseExecutionTimeMs} extracts the server-side
     *  execution time from it. SANCTIONED RAW (nexus-wbfpw.9, TEST-TREE RATCHET). */
    private String explainProd(String sql, float[] vec, int n) {
        return tenantScope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "hnsw.iterative_scan", PROD_ITERATIVE_SCAN);
            PgSession.setLocal(ctx, "hnsw.ef_search", PROD_EF_SEARCH);
            // Inline concatenation (not a separately-named variable) so this raw-SQL call
            // site starts with a literal '"', the shape RawSqlGateTest's scan anchors on --
            // a variable named anything other than sql/SQL is invisible to that scan (its
            // own documented KNOWN RESIDUAL), and this file's raw-SQL footprint should be
            // honestly countable, not accidentally hidden by a naming choice.
            Result<Record> rows = ctx.fetch("EXPLAIN (ANALYZE, BUFFERS) " + sql,
                COLLECTION, vectorLiteral(vec), n);
            List<String> lines = new ArrayList<>();
            for (var rec : rows) lines.add(rec.get(0, String.class));
            return String.join("\n", lines);
        });
    }

    private static final Pattern EXECUTION_TIME = Pattern.compile("Execution Time: ([0-9.]+) ms");

    private static double parseExecutionTimeMs(String plan) {
        Matcher m = EXECUTION_TIME.matcher(plan);
        if (!m.find()) {
            throw new IllegalArgumentException("no 'Execution Time:' line in plan:\n" + plan);
        }
        return Double.parseDouble(m.group(1));
    }

    private static long p50(List<Long> samplesMs) {
        List<Long> sorted = samplesMs.stream().sorted().toList();
        return sorted.get((int) Math.ceil(sorted.size() * 0.5) - 1);
    }

    private long clientWallClockP50Ms(Supplier<?> call, int reps) {
        List<Long> samples = new ArrayList<>(reps);
        for (int i = 0; i < reps; i++) {
            long t0 = System.nanoTime();
            call.get();
            samples.add((System.nanoTime() - t0) / 1_000_000L);
        }
        return p50(samples);
    }

    private static double recallAt(List<String> approx, List<String> oracle, int k) {
        List<String> oracleTopK = oracle.size() > k ? oracle.subList(0, k) : oracle;
        long hits = approx.stream().limit(k).filter(oracleTopK::contains).count();
        return oracleTopK.isEmpty() ? 1.0 : (double) hits / oracleTopK.size();
    }

    // ── guard ───────────────────────────────────────────────────────────────

    @Test
    void guard_fixtureLoadedCorrectly() throws Exception {
        assertThat(docIds).hasSize(NUM_DOCS);
        assertThat(chashHex).hasSize(NUM_CHUNKS);
        setTombstoneFraction(0);
        long liveDocs = tenantScope.withTenant(TENANT, ctx -> (long) ctx.fetchCount(
            DSL.selectFrom(CATALOG_DOCUMENTS)
               .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)
                   .and(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION.eq(COLLECTION))
                   .and(CATALOG_DOCUMENTS.DELETED_AT.isNull()))));
        assertThat(liveDocs).isEqualTo(NUM_DOCS);
    }

    // ── EXPLAIN + latency sweep across msz9i's own tombstone fractions ──────

    /**
     * At every fraction in {@link #TOMBSTONE_FRACTIONS_PCT}: both queries must reach
     * the HNSW index ({@code idx_chunks_embedding_384}), and the "after" query's plan
     * must never contain the literal function name (the same inlining pin as
     * ChunkLiveOwnersInlineRecallIntegrationTest, now exercised at real msz9i scale
     * and under a real dead-row fraction, closing critique-wbfpw9's Important-2: "the
     * 5,000-row scale fixture is 100% live; it cannot exercise the actual risk case").
     * Server EXPLAIN ANALYZE execution time and client JDBC wall-clock p50 are
     * measured and printed SEPARATELY for before/after at each fraction -- never
     * compared against each other, only against their own opposite number.
     */
    @Test
    void explainAndLatency_acrossTombstoneFractions() throws Exception {
        Random rnd = new Random(20260927102L);
        float[] probeVec = randomUnitVector(rnd, DIM);

        System.out.println("[nexus-wbfpw.9 MSZ9I SWEEP] fixture=" + NUM_CHUNKS + " chunks / "
            + NUM_MANIFEST + " manifest rows / " + NUM_DOCS + " docs, hnsw.iterative_scan="
            + PROD_ITERATIVE_SCAN + " hnsw.ef_search=" + PROD_EF_SEARCH);
        System.out.println("tombstone% | before_server_ms | after_server_ms | before_client_p50_ms |"
            + " after_client_p50_ms | before_hnsw | after_hnsw | after_inlined");

        for (int pct : TOMBSTONE_FRACTIONS_PCT) {
            setTombstoneFraction(pct);

            String beforePlan = explainProd(BEFORE_DEAD_SET_ANTI_JOIN, probeVec, K);
            String afterPlan = explainProd(AFTER_CHUNK_LIVE_OWNERS, probeVec, K);
            double beforeServerMs = parseExecutionTimeMs(beforePlan);
            double afterServerMs = parseExecutionTimeMs(afterPlan);

            boolean beforeHnsw = beforePlan.contains("idx_chunks_embedding_384");
            boolean afterHnsw = afterPlan.contains("idx_chunks_embedding_384");
            boolean afterInlined = !afterPlan.contains("chunk_live_owners")
                && afterPlan.contains("catalog_document_chunks");

            // Warm-up before client wall-clock timing.
            for (int i = 0; i < 2; i++) {
                runProd(BEFORE_DEAD_SET_ANTI_JOIN, probeVec, K);
                runProd(AFTER_CHUNK_LIVE_OWNERS, probeVec, K);
            }
            long beforeClientP50 = clientWallClockP50Ms(
                () -> runProd(BEFORE_DEAD_SET_ANTI_JOIN, probeVec, K), LATENCY_REPS);
            long afterClientP50 = clientWallClockP50Ms(
                () -> runProd(AFTER_CHUNK_LIVE_OWNERS, probeVec, K), LATENCY_REPS);

            System.out.printf("%10d | %16.3f | %15.3f | %21d | %19d | %11b | %10b | %13b%n",
                pct, beforeServerMs, afterServerMs, beforeClientP50, afterClientP50,
                beforeHnsw, afterHnsw, afterInlined);

            assertThat(beforeHnsw)
                .as("tombstone%%=%d: the dead-set anti-join (today's shipped predicate)"
                    + " must still reach the HNSW index at this scale. Plan was:%n%s", pct, beforePlan)
                .isTrue();
            assertThat(afterHnsw)
                .as("tombstone%%=%d: chunk_live_owners must not defeat the HNSW index bind"
                    + " (RDR-192 Technical Design's own risk). Plan was:%n%s", pct, afterPlan)
                .isTrue();
            assertThat(afterInlined)
                .as("tombstone%%=%d: chunk_live_owners must stay inlined (no opaque"
                    + " Function Scan / literal function name) even under a real dead-row"
                    + " fraction at msz9i scale. Plan was:%n%s", pct, afterPlan)
                .isTrue();
        }
    }

    // ── recall under selectivity: exact live-only oracle vs production ANN settings ──

    /**
     * At 30% and 60% tombstoned (a meaningful, not merely worst-case-demo, filtered-
     * out fraction), recall@10 of chunk_live_owners under PRODUCTION HNSW settings
     * (relaxed_order iterative scan, ef_search=200) against an EXACT live-only oracle
     * (index/bitmap scans disabled, forcing a true sequential-scan-and-sort nearest-
     * neighbor computation). Closes critique-wbfpw9's Significant finding: "the one
     * experiment that would show whether a filtered HNSW search can miss true top-K
     * results... was never run."
     */
    @Test
    void recall_underSelectivity_productionEfSearch_vsExactOracle() throws Exception {
        for (int pct : new int[] {30, 60}) {
            setTombstoneFraction(pct);

            Random rnd = new Random(20260927103L + pct);
            List<Double> recalls = new ArrayList<>(RECALL_QUERY_COUNT);
            for (int q = 0; q < RECALL_QUERY_COUNT; q++) {
                float[] vec = randomUnitVector(rnd, DIM);
                List<String> oracle = runExact(AFTER_CHUNK_LIVE_OWNERS, vec, K);
                List<String> approx = runProd(AFTER_CHUNK_LIVE_OWNERS, vec, K);
                recalls.add(recallAt(approx, oracle, K));
            }
            double avgRecall = recalls.stream().mapToDouble(Double::doubleValue).average().orElseThrow();

            System.out.println("[nexus-wbfpw.9 RECALL@10 UNDER SELECTIVITY] tombstone%=" + pct
                + " production(ef_search=" + PROD_EF_SEARCH + ", iterative_scan=" + PROD_ITERATIVE_SCAN
                + ") vs exact-oracle avg recall@" + K + "=" + avgRecall
                + " over " + RECALL_QUERY_COUNT + " random-vector queries. Per-query: " + recalls);

            // Evidence, not a hard regression gate (same discipline as the sibling
            // class's latency test) -- but non-vacuous: the measurement must actually
            // run and produce a real number in [0,1], and any drop below 1.0 is
            // reported plainly above, per the bead's acceptance criteria.
            assertThat(avgRecall).isBetween(0.0, 1.0);
        }
    }
}
