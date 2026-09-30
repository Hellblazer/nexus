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
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
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
 * manifest-row count, document count) with synthetic vectors of low intrinsic
 * dimension (see {@link #fixtureVector}; no ONNX --
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
 * EXACT (ORDER BY rewritten so HNSW cannot serve it, see {@link #exactForm}) oracle graded
 * against its OWN predicate's live population. Round-3 rework added the
 * missing CONTROLS a standalone chunk_live_owners recall number cannot
 * supply on its own: an unfiltered 0%-tombstoned baseline (isolates whether
 * this fixture's own approximate-search quality, not
 * filtering, drives a measured gap) and the SAME methodology applied to
 * today's shipped dead-set anti-join predicate (isolates whether
 * chunk_live_owners is worse than, equal to, or better than the status quo
 * on this axis), plus a full-population live-count comparison between the
 * two predicates at every fraction. That comparison is a FINDING, not a
 * confirmed assumption: the two predicates do not describe the identical
 * live population -- they differ by exactly the manifest-less chunk count,
 * constant at every fraction, because the old dead-set anti-join never
 * catches a chunk with zero manifest rows (this fixture's own manifest-less
 * slice) while chunk_live_owners correctly does. See the recall test's own
 * javadoc below for the full explanation.
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
    /** live(c) recall@K floor, every fraction and deletion mode (Sam, 2026-09-29, nexus-wbfpw.36).
     *  Measured at acceptance: 1.000 scattered, 0.995 at 60% correlated. */
    private static final double LIVE_RECALL_FLOOR = 0.95;
    /** Per-query guard beside the average (critique-nexus-wbfpw.36-recall-floor S1): one query
     *  returning nothing averages to 19/20 = 0.95 and would pass the floor alone. */
    private static final double LIVE_RECALL_QUERY_MIN = 0.7;
    private static final double LIVE_RECALL_QUERY_LOW = 0.9;
    private static final int LIVE_RECALL_MAX_LOW_QUERIES = 2;
    private static final int[] TOMBSTONE_FRACTIONS_PCT = {3, 10, 30, 60};
    private static final int LATENCY_REPS = Integer.getInteger("nx.cloMsz9i.reps", 10);
    private static final int RECALL_QUERY_COUNT = Integer.getInteger("nx.cloMsz9i.recallQueries", 20);

    /** Intrinsic dimension of the fixture's vectors (see {@link #fixtureVector}). */
    private static final int LOW_RANK = 16;

    /** Fixed DIM x LOW_RANK Gaussian projection shared by every fixture, query, probe and
     *  pin vector, so all of them are drawn from one distribution. */
    private static final float[][] PROJECTION = gaussianMatrix(new Random(20260927099L), DIM, LOW_RANK);

    /** Production search's own HNSW GUCs (PgVectorRepository#search): {@code
     *  hnsw.iterative_scan=relaxed_order}, {@code hnsw.ef_search =
     *  max(PgSession.DEFAULT_EF_SEARCH_FLOOR=200, nResults)} -- for K=10 that floor
     *  wins, so ef_search=200 here reproduces the real value production would set. */
    private static final String PROD_ITERATIVE_SCAN = "relaxed_order";
    private static final String PROD_EF_SEARCH = "200";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    /** All NUM_DOCS document tumblers, in SPATIAL order: document d owns the d-th run of
     *  manifest chunks sorted by cosine similarity to a fixed anchor vector (nearest
     *  first), so a prefix of this list is one spherical cap of the space HNSW searches.
     *  Chunk indices [NUM_MANIFEST, NUM_CHUNKS) are manifest-less. */
    final List<String> docIds = new ArrayList<>(NUM_DOCS);

    /** {@link #docIds} in a seeded shuffled order: a prefix of it is a spatially
     *  scattered set of documents. */
    final List<String> scatteredDocOrder = new ArrayList<>(NUM_DOCS);

    /** How {@link #setTombstoneFraction} picks the tombstoned documents. SCATTERED leaves
     *  deletion independent of vector position; CORRELATED kills one contiguous region,
     *  the shape of a topically-related batch superseded together (RDR-192 Background). */
    enum Tombstones { SCATTERED, CORRELATED }

    /** Owning document index per chunk, or -1 for a manifest-less chunk. */
    int[] chunkDoc;

    /** Chunk index by chash hex, to map a search result back to its owning document. */
    final Map<String, Integer> chunkIndexByHex = new HashMap<>(NUM_CHUNKS * 2);

    /** Every chunk's embedding, by chunk index. */
    final List<float[]> chunkVectors = new ArrayList<>(NUM_CHUNKS);

    /** Document indices currently tombstoned (mirrors deleted_at). */
    final Set<Integer> deadDocs = new HashSet<>();

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
        float[] anchor = fixtureVector(new Random(20260927097L));
        double[] anchorSimilarity = new double[NUM_CHUNKS];
        for (int i = 0; i < NUM_CHUNKS; i++) {
            String id = "clo-msz9i-chunk-" + i;
            chashHex.add(Chash.ofText(id).toHex());
            chunkIndexByHex.put(chashHex.get(i), i);
            float[] v = fixtureVector(rnd);
            vectors.add(v);
            chunkVectors.add(v);
            double dot = 0;
            for (int d = 0; d < DIM; d++) dot += v[d] * anchor[d];
            anchorSimilarity[i] = dot;
        }

        // Spatial document assignment: manifest chunks sorted by cosine similarity to the
        // anchor (both unit vectors, so the dot product is the cosine), nearest first, cut
        // into NUM_DOCS consecutive runs. Positions count up within each run. A first
        // attempt sorted on one raw latent coordinate; that is not a compact region in
        // the cosine metric, and the dead-neighbourhood pin below caught it (0 of 20
        // queries at 60%).
        Integer[] bySlab = new Integer[NUM_MANIFEST];
        for (int i = 0; i < NUM_MANIFEST; i++) bySlab[i] = i;
        Arrays.sort(bySlab, Comparator.comparingDouble(i -> -anchorSimilarity[i]));
        chunkDoc = new int[NUM_CHUNKS];
        Arrays.fill(chunkDoc, -1);
        int[] positionOf = new int[NUM_CHUNKS];
        int[] nextPosition = new int[NUM_DOCS];
        for (int k = 0; k < NUM_MANIFEST; k++) {
            int doc = (int) ((long) k * NUM_DOCS / NUM_MANIFEST);
            chunkDoc[bySlab[k]] = doc;
            positionOf[bySlab[k]] = nextPosition[doc]++;
        }
        scatteredDocOrder.addAll(docIds);
        Collections.shuffle(scatteredDocOrder, new Random(20260927098L));

        // Chunks, in batches (upsertChunksWithVectors -- no embedder call, precomputed
        // low-intrinsic-dimension vectors; see fixtureVector for why not uniform noise).
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

        // Manifest: first NUM_MANIFEST chunks, assigned spatially (see chunkDoc above). The
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
                    String doc = docIds.get(chunkDoc[i]);
                    int position = positionOf[i];
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

    /** {@link #setTombstoneFraction(int, Tombstones)} with SCATTERED deletion. */
    private void setTombstoneFraction(int percent) throws Exception {
        setTombstoneFraction(percent, Tombstones.SCATTERED);
    }

    /** Resets every fixture document to live, then tombstones a deterministic PREFIX of
     *  the {@code mode}'s document order, sized to {@code percent}% of {@link #NUM_DOCS}
     *  -- msz9i's own sweep methodology (mutate the SAME fixture between measurements).
     *  Re-ANALYZEs catalog_documents (small, cheap) so the planner's row estimate
     *  reflects the new tombstone fraction. */
    private void setTombstoneFraction(int percent, Tombstones mode) throws Exception {
        List<String> order = mode == Tombstones.CORRELATED ? docIds : scatteredDocOrder;
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
                        .and(CATALOG_DOCUMENTS.TUMBLER.in(order.subList(0, tombstoneCount))))
                    .execute();
            }
            deadDocs.clear();
            for (String id : order.subList(0, tombstoneCount)) deadDocs.add(docIds.indexOf(id));
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    /** A unit vector in DIM dimensions with intrinsic dimension {@link #LOW_RANK}: a
     *  Gaussian point in LOW_RANK dimensions, lifted through {@link #PROJECTION} and
     *  normalised. Round 3 replaced uniform random unit vectors, on which the UNFILTERED
     *  HNSW baseline measured recall@10 = 0.22: in 384 uniform dimensions every point is
     *  nearly equidistant from every other, so the exact top-10 is close to arbitrary and
     *  no approximate index can find it. Real embeddings have low intrinsic dimension;
     *  this fixture now does too, which is what lets a filtered-vs-unfiltered recall
     *  comparison mean anything. */
    private static float[] fixtureVector(Random rnd) {
        return lift(latent(rnd));
    }

    private static double[] latent(Random rnd) {
        double[] z = new double[LOW_RANK];
        for (int k = 0; k < LOW_RANK; k++) z[k] = rnd.nextGaussian();
        return z;
    }

    private static float[] lift(double[] z) {
        float[] v = new float[DIM];
        double sumSq = 0;
        for (int i = 0; i < DIM; i++) {
            double x = 0;
            for (int k = 0; k < LOW_RANK; k++) x += PROJECTION[i][k] * z[k];
            v[i] = (float) x;
            sumSq += x * x;
        }
        float norm = (float) Math.sqrt(sumSq);
        for (int i = 0; i < DIM; i++) v[i] /= norm;
        return v;
    }

    private static float[][] gaussianMatrix(Random rnd, int rows, int cols) {
        float[][] m = new float[rows][cols];
        for (int i = 0; i < rows; i++) {
            for (int k = 0; k < cols; k++) m[i][k] = (float) rnd.nextGaussian();
        }
        return m;
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

    /** Round-3 rework control (coordinator instruction after round-2 review): the SAME
     *  KNN shape with NO liveness predicate at all -- isolates whether a measured recall
     *  drop is caused by FILTERING, or is simply this fixture's own
     *  HNSW approximation quality on an unfiltered search. Same 3-bind-parameter shape
     *  (collection, vector, n) as {@link #BEFORE_DEAD_SET_ANTI_JOIN}/{@link
     *  #AFTER_CHUNK_LIVE_OWNERS}, so it runs through the SAME {@link #runProd}/{@link
     *  #runExact} call sites -- no new raw-SQL site for this constant. */
    private static final String NO_PREDICATE_KNN =
        "SELECT encode(c.chash, 'hex') FROM nexus.chunks c"
        + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
        + " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector"
        + " LIMIT ?";

    /** Full-population COUNT variants of the no-predicate/before/after predicates, used
     *  to confirm {@link #BEFORE_DEAD_SET_ANTI_JOIN} and {@link #AFTER_CHUNK_LIVE_OWNERS}
     *  agree on the EXACT SAME live population at every tombstone fraction (round-3
     *  rework: "confirm ... that (b) and (c) return identical live sets"), not merely the
     *  same top-K under one probe vector. Run through the ONE new {@link #countMatching}
     *  call site. */
    private static final String NO_PREDICATE_COUNT =
        "SELECT count(*) FROM nexus.chunks c"
        + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL";

    private static final String BEFORE_COUNT =
        "SELECT count(*) FROM nexus.chunks c"
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
        + "                                       AND d2.deleted_at IS NULL)))";

    private static final String AFTER_COUNT =
        "SELECT count(*) FROM nexus.chunks c"
        + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
        + " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))";

    /** Runs {@code sql} (either {@link #BEFORE_DEAD_SET_ANTI_JOIN} or {@link
     *  #AFTER_CHUNK_LIVE_OWNERS}) with production HNSW GUCs and returns the ordered
     *  chash-hex result list. */
    private List<String> runProd(String sql, float[] vec, int n) {
        return runHnsw(sql, vec, n, PROD_ITERATIVE_SCAN, PROD_EF_SEARCH);
    }

    /** {@link #runProd} with the two HNSW GUCs supplied, for the recall test's positive
     *  control (a deliberately starved search that MUST lose recall). */
    private List<String> runHnsw(String sql, float[] vec, int n, String iterativeScan, String efSearch) {
        Result<Record> rows = tenantScope.withTenant(TENANT, ctx -> {
            PgSession.setLocal(ctx, "hnsw.iterative_scan", iterativeScan);
            PgSession.setLocal(ctx, "hnsw.ef_search", efSearch);
            return ctx.fetch(sql, COLLECTION, vectorLiteral(vec), n);
        });
        List<String> ids = new ArrayList<>(rows.size());
        for (var rec : rows) ids.add(rec.get(0, String.class));
        return ids;
    }

    private static final String PROD_ORDER_BY =
        " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector";

    /** The EXACT form of a KNN statement: its ORDER BY distance becomes {@code (distance)
     *  + 0}, an expression the HNSW index cannot serve (pgvector matches only a bare
     *  {@code column <=> constant} ordering), so PostgreSQL must compute every candidate's
     *  true distance and sort. Round 3 replaced a session-wide {@code enable_indexscan=off}
     *  that also took the btree lookups away from chunk_live_owners' EXISTS, turning each
     *  oracle query into a per-row manifest scan (over 2 minutes each at msz9i scale; the
     *  40-query recall loop outran surefire's 1800s fork timeout). The recall test pins
     *  that this form really leaves HNSW. */
    private static String exactForm(String knnSql) {
        String exact = knnSql.replace(PROD_ORDER_BY,
            " ORDER BY (c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector) + 0");
        if (exact.equals(knnSql)) {
            throw new IllegalArgumentException("no production ORDER BY to rewrite in:\n" + knnSql);
        }
        return exact;
    }

    /** EXACT oracle: same statement in {@link #exactForm} -- true nearest neighbors among
     *  the population its own predicate admits, no HNSW approximation. */
    private List<String> runExact(String knnSql, float[] vec, int n) {
        String sql = exactForm(knnSql);
        Result<Record> rows = tenantScope.withTenant(TENANT, ctx ->
            ctx.fetch(sql, COLLECTION, vectorLiteral(vec), n));
        List<String> ids = new ArrayList<>(rows.size());
        for (var rec : rows) ids.add(rec.get(0, String.class));
        return ids;
    }

    /** Full-population COUNT of {@code sql} (one of the {@code *_COUNT} constants) --
     *  used to confirm two predicates describe the identical live population, not merely
     *  the same top-K under one probe vector. SANCTIONED RAW (nexus-wbfpw.9 round 3,
     *  TEST-TREE RATCHET). */
    private long countMatching(String sql) {
        return tenantScope.withTenant(TENANT, ctx -> {
            Record rec = ctx.fetch(sql, COLLECTION).get(0);
            return rec.get(0, Long.class);
        });
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

    /** The live(c) recall gate: average at or above {@link #LIVE_RECALL_FLOOR}, no query below
     *  {@link #LIVE_RECALL_QUERY_MIN}, at most {@link #LIVE_RECALL_MAX_LOW_QUERIES} below
     *  {@link #LIVE_RECALL_QUERY_LOW} (Sam, 2026-09-29, nexus-wbfpw.36). */
    private static void assertLiveRecall(String what, int pct, List<Double> recalls) {
        assertThat(avg(recalls))
            .as("live(c) recall@%d, %s at %d%%: average below the floor. Per query: %s", K, what, pct, recalls)
            .isBetween(LIVE_RECALL_FLOOR, 1.0);
        assertThat(recalls.stream().mapToDouble(Double::doubleValue).min().orElse(0.0))
            .as("live(c) recall@%d, %s at %d%%: a query fell below %.2f. Per query: %s",
                K, what, pct, LIVE_RECALL_QUERY_MIN, recalls)
            .isGreaterThanOrEqualTo(LIVE_RECALL_QUERY_MIN);
        assertThat(recalls.stream().filter(r -> r < LIVE_RECALL_QUERY_LOW).count())
            .as("live(c) recall@%d, %s at %d%%: too many queries below %.2f. Per query: %s",
                K, what, pct, LIVE_RECALL_QUERY_LOW, recalls)
            .isLessThanOrEqualTo(LIVE_RECALL_MAX_LOW_QUERIES);
    }

    private static double recallAt(List<String> approx, List<String> oracle, int k) {
        List<String> oracleTopK = oracle.size() > k ? oracle.subList(0, k) : oracle;
        long hits = approx.stream().limit(k).filter(oracleTopK::contains).count();
        return oracleTopK.isEmpty() ? 1.0 : (double) hits / oracleTopK.size();
    }

    /** Grades {@code approxSql}'s production-GUC KNN result against {@code oracleSql}'s
     *  EXACT result, over {@link #RECALL_QUERY_COUNT} random query vectors. Every call
     *  site in this file's recall test passes the SAME predicate text as both approx and
     *  oracle, so each case is graded against its OWN correct answer set (round-3
     *  rework requirement: "confirm the oracle applies the SAME liveness filter as the
     *  query it grades"). */
    private List<Double> recallSeries(String approxSql, String oracleSql, long seed) {
        Random rnd = new Random(seed);
        List<float[]> queries = new ArrayList<>(RECALL_QUERY_COUNT);
        for (int q = 0; q < RECALL_QUERY_COUNT; q++) queries.add(fixtureVector(rnd));
        return recallSeries(approxSql, oracleSql, queries);
    }

    /** Queries aimed INTO the deleted region: the embeddings of {@link #RECALL_QUERY_COUNT}
     *  seeded-random chunks owned by tombstoned documents -- a user searching for the text
     *  of a retracted note. Uniform queries rarely land in a small deleted cap. */
    private List<float[]> deadRegionQueries(long seed) {
        List<Integer> dead = new ArrayList<>();
        for (int i = 0; i < NUM_CHUNKS; i++) {
            if (chunkDoc[i] >= 0 && deadDocs.contains(chunkDoc[i])) dead.add(i);
        }
        assertThat(dead).as("no tombstone-owned chunks to aim queries at").isNotEmpty();
        Random rnd = new Random(seed);
        List<float[]> queries = new ArrayList<>(RECALL_QUERY_COUNT);
        for (int q = 0; q < RECALL_QUERY_COUNT; q++) {
            queries.add(chunkVectors.get(dead.get(rnd.nextInt(dead.size()))));
        }
        return queries;
    }

    private List<Double> recallSeries(String approxSql, String oracleSql, List<float[]> queries) {
        List<Double> recalls = new ArrayList<>(queries.size());
        for (float[] vec : queries) {
            List<String> oracle = runExact(oracleSql, vec, K);
            List<String> approx = runProd(approxSql, vec, K);
            recalls.add(recallAt(approx, oracle, K));
        }
        return recalls;
    }

    /** Over {@link #RECALL_QUERY_COUNT} fixture queries, how many have an unfiltered exact
     *  top-K whose MANIFEST-OWNED members are at least 90% tombstoned: queries whose whole
     *  neighbourhood is dead, the hard case for a filtered HNSW search. Manifest-less
     *  chunks are excluded from both counts: they are a quarter of the fixture and
     *  scattered everywhere, so counting them would cap the dead share near 0.75 even
     *  inside a fully tombstoned region. */
    private int deadNeighbourhoodQueries(long seed) {
        Random rnd = new Random(seed);
        int dead = 0;
        for (int q = 0; q < RECALL_QUERY_COUNT; q++) {
            List<String> top = runExact(NO_PREDICATE_KNN, fixtureVector(rnd), K);
            int[] owners = top.stream()
                .mapToInt(h -> chunkDoc[chunkIndexByHex.get(h)])
                .filter(doc -> doc >= 0)
                .toArray();
            long tombstoned = Arrays.stream(owners).filter(deadDocs::contains).count();
            if (owners.length > 0 && tombstoned >= 0.9 * owners.length) dead++;
        }
        return dead;
    }

    private static double avg(List<Double> values) {
        return values.stream().mapToDouble(Double::doubleValue).average().orElseThrow();
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
        float[] probeVec = fixtureVector(rnd);

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
     * Round-3 rework (coordinator instruction, after round-2 review returned the
     * standalone chunk_live_owners recall numbers -- 0.24 to 0.28 at 30%/60% -- as
     * insufficient on their own: "0.24-0.28 alone cannot say whether live(c) causes a
     * drop"). Supersedes the round-2 test above (which measured only chunk_live_owners,
     * at 30%/60% only) with three controlled measurements, EACH oracle graded against
     * its OWN predicate text (never a mismatched liveness filter):
     *
     * <ul>
     *   <li>(a) 0% tombstoned, NO liveness predicate at all -- the unfiltered HNSW
     *       baseline. It must reach 0.9: below that, the fixture's own approximate-search
     *       quality, not liveness filtering, would drive any gap in (b)/(c), so the test
     *       refuses rather than report numbers that cannot distinguish the two. (Round 3
     *       measured 0.22 here on uniform random vectors; see {@link #fixtureVector}.)</li>
     *   <li>(b) today's SHIPPED predicate ({@link #BEFORE_DEAD_SET_ANTI_JOIN}), at
     *       every fraction in {@link #TOMBSTONE_FRACTIONS_PCT}.</li>
     *   <li>(c) {@link #AFTER_CHUNK_LIVE_OWNERS} (this bead's replacement), at every
     *       fraction in {@link #TOMBSTONE_FRACTIONS_PCT}.</li>
     * </ul>
     *
     * <p>At every fraction, (b)'s and (c)'s full-population live COUNTS (not merely
     * their top-K under one probe vector) are compared -- and they are NOT identical.
     * Measured (this is a finding, not an assumption confirmed): (b), the dead-set
     * anti-join, only removes a chunk whose EVERY manifest row points at a tombstoned
     * document; a chunk with ZERO manifest rows at all (this fixture's manifest-less
     * slice, R1's own shape in the matrix test above) is never caught by that
     * anti-join and so counts as live under (b). (c), chunk_live_owners, requires an
     * actual owning row and correctly counts a manifest-less chunk as dead. The two
     * counts differ by EXACTLY {@code NUM_CHUNKS - NUM_MANIFEST}, constant across
     * every fraction -- proving the two predicates agree on tombstone handling and
     * diverge ONLY on the already-documented manifest-less gap this class's own
     * javadoc names above ("R1/R3/R4/R6-in-A/R8... P1g=true, P1s=true, LIVE=false").
     * (c) is the CORRECT {@code live(c)}; (b) was always a narrower approximation.
     * {@code hnsw.max_scan_tuples} is read via {@code current_setting(...)} and
     * printed -- production code never sets it (grepped: only referenced in comments
     * describing the failure mode it can cause), so its effective value here is
     * pgvector's own compiled-in default.
     */
    @Test
    void recall_withControls_unfilteredBaseline_beforeVsAfter_acrossFractions() throws Exception {
        String maxScanTuples = tenantScope.withTenant(TENANT, ctx ->
            ctx.fetch("SELECT current_setting('hnsw.max_scan_tuples')").get(0).get(0, String.class));

        System.out.println("[nexus-wbfpw.9 RECALL CONTROLS] hnsw.max_scan_tuples=" + maxScanTuples
            + " (never set by production code -- pgvector's own compiled-in default)"
            + " hnsw.ef_search=" + PROD_EF_SEARCH + " hnsw.iterative_scan=" + PROD_ITERATIVE_SCAN);
        // The oracle is only an oracle if HNSW cannot serve it. Pinned on both predicates
        // (and the unfiltered form) before any recall number is trusted.
        float[] pinVec = fixtureVector(new Random(20260927100L));
        for (String knn : List.of(NO_PREDICATE_KNN, BEFORE_DEAD_SET_ANTI_JOIN, AFTER_CHUNK_LIVE_OWNERS)) {
            String exactPlan = explainProd(exactForm(knn), pinVec, K);
            assertThat(exactPlan)
                .as("the exact oracle must not use the HNSW index. Plan was:%n%s", exactPlan)
                .doesNotContain("idx_chunks_embedding_384");
        }

        System.out.println("case     | tombstone% | avg_recall@10 | per_query                     | live_count(exact)");

        // (a) unfiltered baseline: no liveness predicate at all. Fixed at 0% tombstoned
        // (deleted_at plays no role in this query at all, so the fraction is otherwise
        // moot) so the fixture is measured in its known-clean state.
        setTombstoneFraction(0);
        List<Double> baseline = recallSeries(NO_PREDICATE_KNN, NO_PREDICATE_KNN, 20260927200L);
        double baselineAvg = avg(baseline);
        long baselineLiveCount = countMatching(NO_PREDICATE_COUNT);
        System.out.printf("%-8s | %10s | %13.3f | %-30s | %d%n",
            "a-none", "0(n/a)", baselineAvg, baseline, baselineLiveCount);
        assertThat(baselineAvg)
            .as("the UNFILTERED baseline (a) must reach 0.9 recall@%d, or this fixture cannot"
                + " separate filtering loss from its own approximation quality. Per query: %s",
                K, baseline)
            .isGreaterThanOrEqualTo(0.9);

        for (int pct : TOMBSTONE_FRACTIONS_PCT) {
            setTombstoneFraction(pct);

            long beforeLiveCount = countMatching(BEFORE_COUNT);
            long afterLiveCount = countMatching(AFTER_COUNT);

            List<Double> beforeRecalls = recallSeries(
                BEFORE_DEAD_SET_ANTI_JOIN, BEFORE_DEAD_SET_ANTI_JOIN, 20260927300L + pct);
            List<Double> afterRecalls = recallSeries(
                AFTER_CHUNK_LIVE_OWNERS, AFTER_CHUNK_LIVE_OWNERS, 20260927400L + pct);
            double beforeAvg = avg(beforeRecalls);
            double afterAvg = avg(afterRecalls);

            long manifestLessGap = beforeLiveCount - afterLiveCount;

            System.out.printf("%-8s | %10d | %13.3f | %-30s | %d%n",
                "b-before", pct, beforeAvg, beforeRecalls, beforeLiveCount);
            System.out.printf("%-8s | %10d | %13.3f | %-30s | %d (delta vs before: %d)%n",
                "c-after", pct, afterAvg, afterRecalls, afterLiveCount, manifestLessGap);

            // MEASURED, not assumed identical: (b) and (c) do NOT describe the same live
            // population. The dead-set anti-join (b) only removes a chunk whose EVERY
            // manifest row points at a tombstoned document -- a chunk with ZERO manifest
            // rows at all (this fixture's [NUM_MANIFEST, NUM_CHUNKS) slice, R1's own shape
            // in the liveness matrix above) is never caught by that anti-join, so (b)
            // counts it as live. chunk_live_owners (c) requires an ACTUAL owning row
            // (an inner JOIN against catalog_document_chunks), so a manifest-less chunk
            // returns zero owners and (c) counts it as dead. This is exactly the R1/R3/R4/
            // R6-in-A/R8 divergence this class's own javadoc already documents ("P1g=true,
            // P1s=true, LIVE=false") -- (c) is the CORRECT live(c); (b) was always a
            // narrower approximation with this known gap. The two predicates therefore
            // differ by EXACTLY (NUM_CHUNKS - NUM_MANIFEST), constant across every
            // tombstone fraction (proving they agree on tombstone handling and diverge
            // ONLY on the manifest-less population, not on anything fraction-dependent).
            assertThat(manifestLessGap)
                .as("tombstone%%=%d: before(b) minus after(c) must equal EXACTLY the"
                    + " manifest-less chunk count (NUM_CHUNKS-NUM_MANIFEST=%d) -- a different"
                    + " delta would mean the two predicates disagree on tombstone handling"
                    + " itself, not merely on the already-documented manifest-less gap", pct,
                    NUM_CHUNKS - NUM_MANIFEST)
                .isEqualTo(NUM_CHUNKS - NUM_MANIFEST);

            // live(c) is gated at LIVE_RECALL_FLOOR (Sam, 2026-09-29, nexus-wbfpw.36); the
            // old dead-set predicate is reported only, since it is no longer in production.
            assertThat(beforeAvg).isBetween(0.0, 1.0);
            assertLiveRecall("scattered deletion", pct, afterRecalls);
        }

        // Correlated deletion (critique-wbfpw9-r3 Significant 1): the series above deletes
        // documents independently of vector position. Here one contiguous region dies
        // together, the shape of a topically-related batch superseded at once. live(c)
        // recall is gated at LIVE_RECALL_FLOOR here too (nexus-wbfpw.36); the old
        // predicate's is reported only. The pin that CORRELATED mode really does produce
        // dead neighbourhoods stays below.
        System.out.println("case     | tombstone% | avg_recall@10 | per_query                     | dead-neighbourhood queries (scattered/correlated)");
        for (int pct : TOMBSTONE_FRACTIONS_PCT) {
            setTombstoneFraction(pct, Tombstones.SCATTERED);
            int scatteredDead = deadNeighbourhoodQueries(20260927500L + pct);
            setTombstoneFraction(pct, Tombstones.CORRELATED);
            int correlatedDead = deadNeighbourhoodQueries(20260927500L + pct);

            List<Double> beforeCorr = recallSeries(
                BEFORE_DEAD_SET_ANTI_JOIN, BEFORE_DEAD_SET_ANTI_JOIN, 20260927500L + pct);
            List<Double> afterCorr = recallSeries(
                AFTER_CHUNK_LIVE_OWNERS, AFTER_CHUNK_LIVE_OWNERS, 20260927500L + pct);
            System.out.printf("%-8s | %10d | %13.3f | %-30s | %d/%d%n",
                "b-corr", pct, avg(beforeCorr), beforeCorr, scatteredDead, correlatedDead);
            System.out.printf("%-8s | %10d | %13.3f | %-30s | %d/%d%n",
                "c-corr", pct, avg(afterCorr), afterCorr, scatteredDead, correlatedDead);
            assertThat(avg(beforeCorr)).isBetween(0.0, 1.0);
            assertLiveRecall("correlated deletion", pct, afterCorr);

            // Same deletion, every query aimed at the deleted region (critique-wbfpw9-r3
            // round-4 Observation: uniform queries put 0 of 20 in the cap at 3% and 10%).
            List<float[]> inRegion = deadRegionQueries(20260927600L + pct);
            List<Double> beforeIn = recallSeries(BEFORE_DEAD_SET_ANTI_JOIN, BEFORE_DEAD_SET_ANTI_JOIN, inRegion);
            List<Double> afterIn = recallSeries(AFTER_CHUNK_LIVE_OWNERS, AFTER_CHUNK_LIVE_OWNERS, inRegion);
            System.out.printf("%-8s | %10d | %13.3f | %-30s | queries inside the deleted cap%n",
                "b-inCap", pct, avg(beforeIn), beforeIn);
            System.out.printf("%-8s | %10d | %13.3f | %-30s | queries inside the deleted cap%n",
                "c-inCap", pct, avg(afterIn), afterIn);
            assertThat(avg(beforeIn)).isBetween(0.0, 1.0);
            assertLiveRecall("correlated deletion, queries inside the deleted region", pct, afterIn);

            if (pct == 60) {
                assertThat(correlatedDead)
                    .as("CORRELATED deletion at 60% must leave some queries with a dead"
                        + " neighbourhood, or this series measures the scattered case again")
                    .isGreaterThanOrEqualTo(3);
                assertThat(scatteredDead)
                    .as("SCATTERED deletion at 60% must not produce dead neighbourhoods,"
                        + " or the two modes are not distinct")
                    .isLessThanOrEqualTo(1);
            }
        }

        // Positive control: the instrument must be able to SEE a filtering loss, or the
        // 1.0s above prove nothing. At 60% tombstoned (about 30% of chunks live under
        // chunk_live_owners), a search with iterative scan off and ef_search=K visits
        // only K candidates and filters most of them away, so it must fall short of the
        // exact oracle. Same query vectors as the 60% (c) series above.
        setTombstoneFraction(60);
        Random rnd = new Random(20260927400L + 60);
        List<Double> starved = new ArrayList<>(RECALL_QUERY_COUNT);
        for (int q = 0; q < RECALL_QUERY_COUNT; q++) {
            float[] vec = fixtureVector(rnd);
            List<String> oracle = runExact(AFTER_CHUNK_LIVE_OWNERS, vec, K);
            List<String> approx = runHnsw(AFTER_CHUNK_LIVE_OWNERS, vec, K, "off", Integer.toString(K));
            starved.add(recallAt(approx, oracle, K));
        }
        double starvedAvg = avg(starved);
        System.out.printf("%-8s | %10d | %13.3f | %-30s | (iterative_scan=off, ef_search=%d)%n",
            "d-starve", 60, starvedAvg, starved, K);
        assertThat(starvedAvg)
            .as("positive control: a starved filtered search at 60%% tombstoned must lose recall,"
                + " or this fixture cannot detect filtering loss at all. Per query: %s", starved)
            .isLessThan(0.9);
    }
}
