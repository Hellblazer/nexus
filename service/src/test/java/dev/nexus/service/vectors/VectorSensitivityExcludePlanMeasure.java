// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;

/**
 * RDR-243 Phase 2, nexus-rfvyy.7.16, part 2: the live measurement of {@code sensitivity=exclude}. Not a regression
 * test: it asserts nothing about speed. It is run once on a lab host (scripts/spikes/rdr243_plan/run.sh) and its
 * numbers go to T2 nexus_rdr/243-plan-evidence-2026-11-06. It does nothing unless {@code NX_RDR243_PLAN_MEASURE} is set,
 * and its name does not match surefire's default includes, so a suite run never reaches it.
 *
 * <p><b>What is measured</b> (all warm, one fixed seed, serving session settings, as {@code nexus_svc} under RLS):
 * <ul>
 *   <li>collection A: ROWS_A chunks (384-d, clustered), one tenant, walking the shared leaf index at the widest
 *       ef_search as a single-collection search does with no per-collection index; collection B: ROWS_B chunks with a
 *       per-collection (PCI) index, at the serving ef_search;</li>
 *       <li>documents of 5 chunks; a document is sensitive iff {@code abs(hashtext(tumbler)) % 100 < pct}, so the
 *       fractions 0, 10, 50 and 90 percent are nested and reproducible; {@code ANALYZE} after every change;</li>
 *   <li>per cell (pct, collection, k in 10 and 100): the function called directly (the search statement alone, one
 *       transaction) with {@code exclude} false and true, and for collection A the whole repository call
 *       {@code searchWithTokens}, in which every search also runs the sensitive-flag lookup in a second transaction;
 *       that lookup's own time is the span from the repository's pre-lookup seam to the return;</li>
 *   <li>the EXPLAIN of the exclude = true statement per cell (index, SubPlan form), the number of rows returned, the
 *       count of sensitive rows in an exclude = true page (must be 0), and the exact-fallback counter;</li>
 *   <li>the cost of an empty tenant transaction (BEGIN, set_config, COMMIT), the floor of the second transaction.</li>
 * </ul>
 * Output is one CSV file and one plan file, named by {@code NX_RDR243_PLAN_OUT}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorSensitivityExcludePlanMeasure {

    private static final Logger LOG = LoggerFactory.getLogger(VectorSensitivityExcludePlanMeasure.class);

    static final String TENANT = "rfvyy716-measure";
    static final String MODEL = "minilm-l6-v2-384";
    static final String COLL_A = "knowledge__rfvyy716-a__minilm-l6-v2-384__v1";
    static final String COLL_B = "knowledge__rfvyy716-b__minilm-l6-v2-384__v1";
    /** Below the router threshold (20,000 rows): the repository sends it down the exact route, no HNSW. */
    static final String COLL_C = "knowledge__rfvyy716-c__minilm-l6-v2-384__v1";
    static final String PCI_INDEX = "pci_rfvyy716_measure_b";
    static final int DIM = 384;
    static final int CHUNKS_PER_DOC = 5;
    static final int CLUSTERS = 200;
    static final int[] PCTS = {0, 10, 50, 90};
    static final int[] KS = {10, 100};
    static final long SEED = 20261106L;

    static final int ROWS_A = intEnv("NX_RDR243_PLAN_ROWS_A", 80_000);
    static final int ROWS_B = intEnv("NX_RDR243_PLAN_ROWS_B", 30_000);
    static final int ROWS_C = intEnv("NX_RDR243_PLAN_ROWS_C", 18_000);
    static final int QUERIES = intEnv("NX_RDR243_PLAN_QUERIES", 100);
    /** No new cell starts after this many minutes; the output then says TRUNCATED. */
    static final int BUDGET_MINUTES = intEnv("NX_RDR243_PLAN_BUDGET_MIN", 24);

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    PgVectorRepository repo;
    String leaf;
    final List<float[]> queryVectors = new ArrayList<>();
    final StringBuilder csv = new StringBuilder();
    final StringBuilder plans = new StringBuilder();
    long startedNanos;
    boolean truncated;
    volatile long hookNanos;

    private static int intEnv(String name, int dflt) {
        String v = System.getenv(name);
        return v == null || v.isBlank() ? dflt : Integer.parseInt(v.trim());
    }

    /** Query text to vector, the repository's embedder. */
    static final class Queries implements Embedder {
        final Map<String, float[]> byText = new LinkedHashMap<>();

        @Override
        public List<float[]> embed(List<String> texts) {
            return texts.stream().map(t -> byText.get(t).clone()).toList();
        }
    }

    final Queries queries = new Queries();

    @BeforeAll
    void startAll() throws Exception {
        Assumptions.assumeTrue(System.getenv("NX_RDR243_PLAN_MEASURE") != null,
            "set NX_RDR243_PLAN_MEASURE to run the live measurement (scripts/spikes/rdr243_plan/run.sh)");
        startedNanos = System.nanoTime();
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        repo = new PgVectorRepository(scope, queries, queries);
        leaf = PciCatalogIntegrationTest.leafName(MODEL, TENANT);
        PgVectorRepository.setBeforeSensitiveLookupHookForTests(() -> hookNanos = System.nanoTime());
    }

    @AfterAll
    void stopAll() {
        PgVectorRepository.setBeforeSensitiveLookupHookForTests(null);
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // -- fixture ---------------------------------------------------------------------------------------

    private static byte[] sha256(String s) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static float[] unit(double[] v) {
        double n = 0;
        for (double x : v) n += x * x;
        n = Math.sqrt(n);
        float[] f = new float[v.length];
        for (int i = 0; i < v.length; i++) f[i] = (float) (v[i] / n);
        return f;
    }

    private static double[] gauss(Random r) {
        double[] g = new double[DIM];
        for (int i = 0; i < DIM; i++) g[i] = r.nextGaussian();
        return g;
    }

    /** center * 0.9 + noise * 0.44, unit length: clustered like topics, not like white noise. */
    private static float[] around(double[] center, Random r) {
        double[] noise = gauss(r);
        double[] v = new double[DIM];
        for (int i = 0; i < DIM; i++) v[i] = 0.9 * center[i] / Math.sqrt(DIM) + 0.44 * noise[i] / Math.sqrt(DIM);
        return unit(v);
    }

    private void load(DSLContext ctx, String collection, int rows, double[][] centers, Random rnd) {
        byte[][] chash = new byte[rows][];
        for (int from = 0; from < rows; from += 250) {
            int to = Math.min(rows, from + 250);
            var step = ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH,
                CHUNKS.EMBEDDING_MODEL, CHUNKS.CHUNK_TEXT, CHUNKS.EMBEDDING_384);
            for (int i = from; i < to; i++) {
                chash[i] = sha256(collection + "|" + i);
                int docCluster = (i / CHUNKS_PER_DOC) % CLUSTERS;
                step = step.values(TENANT, collection, chash[i], MODEL, "t" + i,
                    Vector.of(around(centers[docCluster], rnd)));
            }
            step.execute();
            if ((from / 250) % 40 == 0) {
                LOG.info("event=rdr243_measure_load collection={} rows={}/{} elapsed_s={}", collection, to, rows,
                    (System.nanoTime() - startedNanos) / 1_000_000_000L);
            }
        }
        int docs = (rows + CHUNKS_PER_DOC - 1) / CHUNKS_PER_DOC;
        for (int from = 0; from < docs; from += 500) {
            int to = Math.min(docs, from + 500);
            var step = ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION,
                CATALOG_DOCUMENTS.SENSITIVE);
            for (int d = from; d < to; d++) {
                step = step.values(TENANT, doc(collection, d), "doc " + d, "paper", collection, false);
            }
            step.execute();
        }
        for (int from = 0; from < rows; from += 250) {
            int to = Math.min(rows, from + 250);
            var step = ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                CATALOG_DOCUMENT_CHUNKS.COLLECTION, CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL);
            org.jooq.InsertValuesStep6<?, String, String, Integer, byte[], String, String> values = null;
            for (int i = from; i < to; i++) {
                values = (values == null ? step : values).values(TENANT, doc(collection, i / CHUNKS_PER_DOC),
                    i % CHUNKS_PER_DOC, chash[i], collection, MODEL);
            }
            values.execute();
        }
    }

    private static String doc(String collection, int d) {
        return (collection.equals(COLL_A) ? "a" : collection.equals(COLL_B) ? "b" : "c") + "-" + d;
    }

    private void setFraction(Connection su, int pct) throws Exception {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        var bucket = DSL.abs(DSL.function("hashtext", SQLDataType.INTEGER, CATALOG_DOCUMENTS.TUMBLER)).mod(100);
        ctx.update(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.SENSITIVE, bucket.lt(pct))
            .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).execute();
        PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
    }

    private Set<String> sensitiveChashes(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        Set<String> out = new HashSet<>();
        for (byte[] c : ctx.select(CATALOG_DOCUMENT_CHUNKS.CHASH).from(CATALOG_DOCUMENT_CHUNKS)
                .join(CATALOG_DOCUMENTS).on(CATALOG_DOCUMENTS.TENANT_ID.eq(CATALOG_DOCUMENT_CHUNKS.TENANT_ID)
                    .and(CATALOG_DOCUMENTS.TUMBLER.eq(CATALOG_DOCUMENT_CHUNKS.DOC_ID)))
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT)).and(CATALOG_DOCUMENTS.SENSITIVE)
                .fetch(CATALOG_DOCUMENT_CHUNKS.CHASH)) {
            out.add(java.util.HexFormat.of().formatHex(c));
        }
        return out;
    }

    // -- measurement -----------------------------------------------------------------------------------

    private boolean overBudget() {
        boolean over = (System.nanoTime() - startedNanos) / 60_000_000_000L >= BUDGET_MINUTES;
        if (over) truncated = true;
        return over;
    }

    private Table<?> fn(String collection, Vector q, int k, boolean exclude) {
        return PLAIN_SEARCH_384.call(q, new String[] {collection}, null, null, k, MODEL, TENANT, exclude);
    }

    /** The session settings of runPlainSearchStatement for the route the collection takes. */
    private void serving(DSLContext ctx, String collection, int k) {
        if (COLL_C.equals(collection)) {
            // the router sends a collection of at most 20,000 rows down the exact route
            PgSession.disableIndexScanForExactFallback(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            return;
        }
        PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
        if (COLL_A.equals(collection)) {
            PgSession.setLocal(ctx, "hnsw.ef_search", "1000");
        } else {
            PgSession.setHnswEfSearch(ctx, k);
        }
        PgSession.setHnswScanBudget(ctx);
        PgSession.setSearchPlanCacheMode(ctx);
    }

    private List<String> runFn(String collection, Vector q, int k, boolean exclude) {
        return scope.withTenant(TENANT, ctx -> {
            serving(ctx, collection, k);
            Table<?> t = fn(collection, q, k, exclude);
            return ctx.select(t.field("id", String.class)).from(t).fetch(t.field("id", String.class));
        });
    }

    private String explain(String collection, Vector q, int k, boolean exclude) {
        return scope.withTenant(TENANT, ctx -> {
            serving(ctx, collection, k);
            return PgContainerHelper.explainPlan(ctx, ctx.select().from(fn(collection, q, k, exclude)));
        });
    }

    private static double ms(long nanos) {
        return nanos / 1_000_000.0;
    }

    private static double pct(long[] sorted, double p) {
        int idx = (int) Math.min(sorted.length - 1, Math.ceil(p * sorted.length) - 1);
        return ms(sorted[Math.max(0, idx)]);
    }

    private void cell(int pct, String name, String collection, int k, Set<String> sensitive) {
        Vector[] qs = queryVectors.stream().map(Vector::of).toArray(Vector[]::new);
        String explainOn = explain(collection, qs[0], k, true);
        VectorSensitivityExcludePlanShapeTest.Parsed parsedOn = VectorSensitivityExcludePlanShapeTest.parse(explainOn);
        String explainOff = explain(collection, qs[0], k, false);
        VectorSensitivityExcludePlanShapeTest.Parsed parsedOff = VectorSensitivityExcludePlanShapeTest.parse(explainOff);
        plans.append("=== pct=").append(pct).append(' ').append(name).append(" k=").append(k)
            .append(" exclude=true form=").append(VectorSensitivityExcludePlanShapeTest.form(parsedOn)).append('\n')
            .append(strip(explainOn)).append("\n=== pct=").append(pct).append(' ').append(name).append(" k=")
            .append(k).append(" exclude=false\n").append(strip(explainOff)).append("\n\n");
        String chunkScanOn = parsedOn.nodes().stream().filter(VectorSensitivityExcludePlanShapeTest.Node::mainPlan)
            .filter(VectorSensitivityExcludePlanShapeTest.Node::scansChunksLeaf).map(n -> n.type() + ":"
                + (n.indexName().contains("pci_") ? "pci" : n.indexName().isEmpty() ? "-" : "leaf"))
            .findFirst().orElse("none");
        String chunkScanOff = parsedOff.nodes().stream().filter(VectorSensitivityExcludePlanShapeTest.Node::mainPlan)
            .filter(VectorSensitivityExcludePlanShapeTest.Node::scansChunksLeaf).map(n -> n.type() + ":"
                + (n.indexName().contains("pci_") ? "pci" : n.indexName().isEmpty() ? "-" : "leaf"))
            .findFirst().orElse("none");
        for (boolean exclude : new boolean[] {false, true}) {
            for (int i = 0; i < qs.length; i++) runFn(collection, qs[i], k, exclude); // warm
            long[] t = new long[qs.length];
            long rows = 0;
            int minRows = Integer.MAX_VALUE;
            int leaks = 0;
            for (int i = 0; i < qs.length; i++) {
                long s = System.nanoTime();
                List<String> ids = runFn(collection, qs[i], k, exclude);
                t[i] = System.nanoTime() - s;
                rows += ids.size();
                minRows = Math.min(minRows, ids.size());
                if (exclude) for (String id : ids) if (sensitive.contains(id)) leaks++;
            }
            Arrays.sort(t);
            csv.append(String.join(",", String.valueOf(pct), name, String.valueOf(k), "fn", String.valueOf(exclude),
                fmt(pct(t, 0.5)), fmt(pct(t, 0.95)), fmt(ms(Arrays.stream(t).sum() / t.length)),
                fmt(rows / (double) qs.length), String.valueOf(minRows), String.valueOf(leaks), "", "", "",
                exclude ? VectorSensitivityExcludePlanShapeTest.form(parsedOn).name() : "NONE",
                exclude ? chunkScanOn : chunkScanOff)).append('\n');
        }
        if (COLL_B.equals(collection)) return;
        for (boolean exclude : new boolean[] {false, true}) {
            String[] texts = new String[qs.length];
            for (int i = 0; i < qs.length; i++) texts[i] = "q" + i;
            for (int i = 0; i < qs.length; i++) repo.searchWithTokens(TENANT, texts[i], List.of(collection), k, null,
                false, null, exclude); // warm
            long[] total = new long[qs.length];
            long[] lookup = new long[qs.length];
            long rows = 0;
            int minRows = Integer.MAX_VALUE;
            long fallbacksBefore = PgVectorRepository.exactFallbackCount();
            for (int i = 0; i < qs.length; i++) {
                hookNanos = 0;
                long s = System.nanoTime();
                var res = repo.searchWithTokens(TENANT, texts[i], List.of(collection), k, null, false, null, exclude)
                    .value();
                long e = System.nanoTime();
                total[i] = e - s;
                lookup[i] = hookNanos == 0 ? 0 : e - hookNanos;
                rows += res.size();
                minRows = Math.min(minRows, res.size());
            }
            long fallbacks = PgVectorRepository.exactFallbackCount() - fallbacksBefore;
            Arrays.sort(total);
            Arrays.sort(lookup);
            csv.append(String.join(",", String.valueOf(pct), name, String.valueOf(k), "repo", String.valueOf(exclude),
                fmt(pct(total, 0.5)), fmt(pct(total, 0.95)), fmt(ms(Arrays.stream(total).sum() / total.length)),
                fmt(rows / (double) qs.length), String.valueOf(minRows), "", String.valueOf(fallbacks),
                fmt(pct(lookup, 0.5)), fmt(pct(lookup, 0.95)), "", "")).append('\n');
        }
    }

    private static String fmt(double v) {
        return String.format(java.util.Locale.ROOT, "%.3f", v);
    }

    private static String strip(String plan) {
        return plan.replaceAll("\\[[-0-9.,eE]{100,}\\]", "[V]");
    }

    @Test
    void measure() throws Exception {
        Random rnd = new Random(SEED);
        double[][] centers = new double[CLUSTERS][];
        for (int i = 0; i < CLUSTERS; i++) centers[i] = gauss(rnd);
        for (int i = 0; i < QUERIES; i++) {
            float[] q = around(centers[rnd.nextInt(CLUSTERS)], rnd);
            queryVectors.add(q);
            queries.byText.put("q" + i, q);
        }
        csv.append("pct_sensitive,collection,k,path,exclude,p50_ms,p95_ms,mean_ms,rows_avg,rows_min,leaks,"
            + "exact_fallbacks,lookup_p50_ms,lookup_p95_ms,sub_plan_form,chunk_scan\n");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(ctx, TENANT, COLL_A);
            PgContainerHelper.insertCollection(ctx, TENANT, COLL_B);
            PgContainerHelper.insertCollection(ctx, TENANT, COLL_C);
            load(ctx, COLL_A, ROWS_A, centers, rnd);
            load(ctx, COLL_B, ROWS_B, centers, rnd);
            load(ctx, COLL_C, ROWS_C, centers, rnd);
            PgContainerHelper.createLeafHnswIndex(su, "nexus", leaf, PCI_INDEX, "embedding_384", COLL_B);
            PgContainerHelper.analyzeTable(su, DSL.table(DSL.name("nexus", leaf)));
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
            LOG.info("event=rdr243_measure_loaded rows_a={} rows_b={} elapsed_s={}", ROWS_A, ROWS_B,
                (System.nanoTime() - startedNanos) / 1_000_000_000L);

            // the floor of the second transaction: BEGIN, the tenant stamp, COMMIT, and nothing else
            for (int i = 0; i < 200; i++) scope.withTenant(TENANT, c -> 1);
            long[] empty = new long[1000];
            for (int i = 0; i < empty.length; i++) {
                long s = System.nanoTime();
                scope.withTenant(TENANT, c -> 1);
                empty[i] = System.nanoTime() - s;
            }
            Arrays.sort(empty);
            csv.append(String.join(",", "-", "-", "-", "empty_tx", "-", fmt(pct(empty, 0.5)), fmt(pct(empty, 0.95)),
                "", "", "", "", "", "", "", "", "")).append('\n');

            for (int p : PCTS) {
                setFraction(su, p);
                Set<String> sensitive = sensitiveChashes(su);
                LOG.info("event=rdr243_measure_fraction pct={} sensitive_chunks={} elapsed_s={}", p, sensitive.size(),
                    (System.nanoTime() - startedNanos) / 1_000_000_000L);
                for (String[] c : new String[][] {{"A_shared", COLL_A}, {"B_pci", COLL_B}, {"C_exact", COLL_C}}) {
                    for (int k : KS) {
                        if (overBudget()) break;
                        cell(p, c[0], c[1], k, sensitive);
                    }
                }
            }
        }
        csv.append("# truncated=").append(truncated).append(" rows_a=").append(ROWS_A).append(" rows_b=")
            .append(ROWS_B).append(" rows_c=").append(ROWS_C).append(" queries=").append(QUERIES).append(" wall_s=")
            .append((System.nanoTime() - startedNanos) / 1_000_000_000L).append('\n');
        String out = System.getenv().getOrDefault("NX_RDR243_PLAN_OUT", "target/rdr243-plan/measure");
        Path csvPath = Path.of(out + ".csv");
        Files.createDirectories(csvPath.toAbsolutePath().getParent());
        Files.writeString(csvPath, csv.toString());
        Files.writeString(Path.of(out + ".plans.txt"), plans.toString());
        LOG.info("event=rdr243_measure_done out={} truncated={}", out, truncated);
    }
}
