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
import org.jooq.JSONB;
import org.jooq.Query;

import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_1024;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_768;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 live(c) recall, extended (beads nexus-wbfpw.44 and nexus-wbfpw.45).
 *
 * <p>{@link ChunkLiveOwnersMsz9iScaleIntegrationTest} (nexus-wbfpw.36) gates live(c)
 * recall@10 at 0.95, but its fixture is one tenant, one collection, 384-d, 76k rows, and
 * it deletes at most 60%. The critique that produced this class
 * (T2 nexus/critique-nexus-wbfpw.36-recall-floor S2) named what it cannot see:
 *
 * <ul>
 *   <li>.44: correlated deletion above 60% dead, where pgvector's
 *       {@code hnsw.max_scan_tuples} (compiled default 20000, never set by production)
 *       can run out before an iterative scan has admitted K live rows;</li>
 *   <li>.45: several collections on ONE shared HNSW index, at 768 and 1024 dimensions and
 *       above 76k rows, where the collection predicate stacks on top of liveness.</li>
 * </ul>
 *
 * <p>This is a MEASUREMENT, not a merge gate: it is slow and it exists to record where
 * recall falls, so it is skipped unless {@code -Dnx.recallExt.run=true}. Floor
 * assertions are opt-in ({@code -Dnx.recallExt.assertFloor=true}); the fixture pins
 * (live population matches the model, the oracle really bypasses HNSW, the unfiltered
 * baseline can reach 0.9, a starved search loses recall) always run once enabled.
 *
 * <p>What is measured is production's own statement: the generated
 * {@code nexus.plain_search_<dim>} function (live(c) and the collection filter are inside
 * it), under the GUCs {@code PgVectorRepository#searchWithTokens} sets
 * ({@code hnsw.iterative_scan=relaxed_order}, {@code hnsw.ef_search}, the search
 * statement timeout, forced custom plans). Two things are reported side by side because
 * production has a rescue the bare statement lacks: when the index-ordered attempt returns
 * NO rows, production reruns it with index scans disabled (nexus-bq06h,
 * {@code exactOnUnderReturn}). A short but non-empty result gets no rescue. So each row
 * reports raw recall, the count of empty and short results, and recall with the empty
 * rescue applied; the rescue itself is timed on a sample.
 *
 * <p>Configuration is by system property, one dimension per JVM (one dimension is one
 * index): {@code nx.recallExt.dim} (384/768/1024), {@code .collections}, {@code .rows}
 * (per collection), {@code .fractions} (percent dead, comma list), {@code .mode}
 * ({@code correlated}|{@code scattered}), {@code .deadIn} ({@code all} collections or
 * {@code one}: only the first collection loses documents, the others stay fully live),
 * {@code .settings} (extra HNSW session profiles to sweep beside production's, see
 * {@link #settings}), {@code .queries} (per collection per query kind). Every collection is drawn
 * from the same distribution and the deleted cap sits at the same anchor in each, the
 * worst case for a shared index: the neighbourhood a query lands in is full of
 * other-collection rows as well as dead ones.
 *
 * <p><b>Measured 2026-09-30</b> (pgvector 0.8.6, production session settings, 25 to 100
 * queries per cell; the record is T2 nexus/rdr-192-livec-recall-extended-2026-09-30).
 * Correlated deletion holds recall@10 at or above 0.95 on average through 95% dead at 384-d
 * (76k rows) and through 95% at 768-d and 1024-d (4 collections x 25k rows on one index).
 * It falls below 0.95 at 98% dead (0.85 to 0.89 at 768/1024-d) and 99% at 384-d (0.94), with
 * short and empty results and single queries near 0. Neither {@code hnsw.max_scan_tuples}
 * nor {@code hnsw.scan_mem_multiplier} restores it alone. Together they do
 * ({@code hnsw.max_scan_tuples=200000}, {@code hnsw.scan_mem_multiplier=4} or more gave 1.000
 * in every failing cell, at a p50 of about 70 to 95 ms against about 40 ms at 98% dead): the
 * iterative scan otherwise stops on its own memory bound (work_mem x scan_mem_multiplier)
 * before it reaches the cap. The 7k-row real-embedding corpus (nexus-wbfpw.46) is exact at
 * every fraction under the default cap because the cap exceeds the row count.
 */
@Tag("integration")
@EnabledIfSystemProperty(named = "nx.recallExt.run", matches = "true")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkLiveOwnersRecallExtendedIntegrationTest {

    private static final String TENANT = "clo-rext";
    private static final int K = 10;
    private static final int LOW_RANK = 16;
    private static final double FLOOR = 0.95;

    private static final int DIM = Integer.getInteger("nx.recallExt.dim", 384);
    private static final int COLLECTIONS = Integer.getInteger("nx.recallExt.collections", 1);
    private static final int ROWS = Integer.getInteger("nx.recallExt.rows", 76_000);
    /** msz9i's 57k manifest rows over 76k chunks: a quarter of the chunks own no document. */
    private static final int MANIFEST = (int) ((long) ROWS * 57 / 76);
    /** msz9i's 1k documents over 76k chunks. */
    private static final int DOCS = Math.max(20, ROWS / 76);
    private static final int QUERIES = Integer.getInteger("nx.recallExt.queries", 20);
    private static final boolean CORRELATED =
        !"scattered".equals(System.getProperty("nx.recallExt.mode", "correlated"));
    private static final boolean DEAD_IN_ONE = "one".equals(System.getProperty("nx.recallExt.deadIn", "all"));
    private static final boolean ASSERT_FLOOR = Boolean.getBoolean("nx.recallExt.assertFloor");
    /** Measure once on the incrementally built graph, REINDEX the HNSW index (a fresh bulk
     *  build), then measure again: separates a recall loss that belongs to the graph from one
     *  that belongs to the scan settings. */
    private static final boolean REINDEX = Boolean.getBoolean("nx.recallExt.reindex");
    private static final int FALLBACK_SAMPLES = Integer.getInteger("nx.recallExt.fallbackSamples", 3);

    /** Real-embedding mode (nexus-wbfpw.46): comma list of directories whose markdown is chunked
     *  and embedded with the provisioned bge-base ONNX model. Requires dim=768, one collection. */
    private static final String CORPUS = System.getProperty("nx.recallExt.corpus");
    private static final int CORPUS_CHUNK_CHARS = 1500;
    private static final int CORPUS_MIN_CHARS = 200;
    /** Every Nth corpus passage is held out of the index and used as a query. */
    private static final int HELD_OUT_EVERY = 15;

    private static final List<Integer> FRACTIONS = ints(System.getProperty("nx.recallExt.fractions", "80,90,95"));

    private static final float[][] PROJECTION = gaussianMatrix(new Random(20260930099L), DIM, LOW_RANK);

    /** One HNSW session profile. A null {@code maxScan}, {@code workMem} or {@code memMultiplier}
     *  leaves the server default. {@code workMem} and {@code memMultiplier} bound the iterative
     *  scan's own memory (pgvector stops an iterative scan that outgrows work_mem times
     *  hnsw.scan_mem_multiplier), a second stop condition beside max_scan_tuples. */
    record Setting(String name, String iterativeScan, int efSearch, Integer maxScan,
                   String workMem, String memMultiplier) {
        Setting(String name, String iterativeScan, int efSearch, Integer maxScan) {
            this(name, iterativeScan, efSearch, maxScan, null, null);
        }
    }

    /** One measured query: which collection it filters to, and which kind it is. */
    record Probe(int collection, String kind, float[] vector) { }

    /** One query's outcome under one setting. {@code rows == -1} marks a statement timeout. */
    record Outcome(double recall, int rows, long millis) { }

    /** Label printed on every report line: "built" (as seeded) or "reindexed". */
    private String phase = "built";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;

    final List<String> collections = new ArrayList<>();
    /** Per collection: document tumblers in spatial order (a prefix is one cap). */
    final List<List<String>> docIds = new ArrayList<>();
    final List<List<String>> scatteredOrder = new ArrayList<>();
    /** Per collection: owning document index per chunk, -1 for a manifest-less chunk. */
    final List<int[]> chunkDoc = new ArrayList<>();
    final List<List<float[]>> vectors = new ArrayList<>();
    final List<List<String>> chashHex = new ArrayList<>();
    /** Per collection: tombstoned document indices right now. */
    final List<Set<Integer>> deadDocs = new ArrayList<>();
    final List<List<String>> texts = new ArrayList<>();
    /** Per collection: the order the CORRELATED mode kills documents in. */
    final List<List<String>> corrOrder = new ArrayList<>();
    /** Corpus mode: held-out passage embeddings (never indexed) and the document each came from. */
    final List<float[]> heldOut = new ArrayList<>();
    final List<Integer> heldOutDoc = new ArrayList<>();

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

    private static String modelToken() {
        return switch (DIM) {
            case 384 -> "minilm-l6-v2-384";
            case 768 -> "bge-base-en-v15-768";
            case 1024 -> "voyage-context-3";
            default -> throw new IllegalArgumentException("unsupported dim " + DIM);
        };
    }

    private void log(String msg) {
        System.out.println("[recallExt " + java.time.LocalTime.now().withNano(0) + "] " + msg);
    }

    private void seedFixture() throws Exception {
        long t0 = System.nanoTime();
        float[] anchor = fixtureVector(new Random(20260930097L));
        try (Connection reg = pg.createConnection("")) {
            reg.setAutoCommit(true);
            for (int c = 0; c < COLLECTIONS; c++) {
                String name = "knowledge__rext-c" + c + "__" + modelToken() + "__v1";
                collections.add(name);
                PgContainerHelper.insertCollection(DSL.using(reg, SQLDialect.POSTGRES), TENANT, name);
            }
        }

        if (CORPUS != null) {
            generateCorpus();
        } else {
            generateSynthetic(anchor);
        }
        log("vectors generated " + COLLECTIONS + "x" + vectors.get(0).size() + " dim=" + DIM
            + (CORPUS != null ? " corpus=" + CORPUS + " docs=" + docIds.get(0).size() + " heldOut=" + heldOut.size() : "")
            + " in " + (System.nanoTime() - t0) / 1_000_000_000L + "s");

        // Chunks, interleaved across collections batch by batch so the shared graph is built
        // the way concurrent writers would build it, not one collection at a time.
        var pgRepo = new PgVectorRepository(tenantScope, (Embedder) null, (Embedder) null);
        int batch = 1000;
        long seedStart = System.nanoTime();
        int rows = vectors.get(0).size();
        for (int start = 0; start < rows; start += batch) {
            int end = Math.min(start + batch, rows);
            for (int c = 0; c < COLLECTIONS; c++) {
                List<Map<String, Object>> metas = new ArrayList<>(end - start);
                for (int i = start; i < end; i++) metas.add(Map.of());
                pgRepo.upsertChunksWithVectors(TENANT, collections.get(c),
                    chashHex.get(c).subList(start, end), texts.get(c).subList(start, end),
                    vectors.get(c).subList(start, end), metas);
            }
            if ((start / batch) % 10 == 9) {
                long secs = (System.nanoTime() - seedStart) / 1_000_000_000L;
                log("seeded " + end * COLLECTIONS + "/" + rows * COLLECTIONS + " chunks in " + secs + "s");
            }
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            for (int c = 0; c < COLLECTIONS; c++) {
                List<Query> docQueries = new ArrayList<>(docIds.get(c).size());
                for (String id : docIds.get(c)) {
                    docQueries.add(ctx.insertInto(CATALOG_DOCUMENTS,
                            CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                            CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
                        .values(TENANT, id, "Doc", "prose", collections.get(c)));
                }
                ctx.batch(docQueries).execute();
            }
            su.commit();
            for (int c = 0; c < COLLECTIONS; c++) {
                int[] doc = chunkDoc.get(c);
                int[] nextPosition = new int[docIds.get(c).size()];
                int manifestBatch = 5000;
                List<Query> pending = new ArrayList<>(manifestBatch);
                int owned = 0;
                for (int v : doc) if (v >= 0) owned++;
                int written = 0;
                for (int i = 0; i < doc.length; i++) {
                    if (doc[i] < 0) continue;
                    written++;
                    pending.add(ctx.insertInto(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                            CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                            CATALOG_DOCUMENT_CHUNKS.COLLECTION)
                        .values(TENANT, docIds.get(c).get(doc[i]), nextPosition[doc[i]]++,
                            HexFormat.of().parseHex(chashHex.get(c).get(i)), collections.get(c)));
                    if (pending.size() == manifestBatch || written == owned) {
                        ctx.batch(pending).execute();
                        su.commit();
                        pending.clear();
                    }
                }
            }
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENT_CHUNKS);
        }
        log("fixture ready in " + (System.nanoTime() - t0) / 1_000_000_000L + "s");
    }

    private void generateSynthetic(float[] anchor) {
        for (int c = 0; c < COLLECTIONS; c++) {
            List<String> ids = new ArrayList<>(DOCS);
            for (int i = 0; i < DOCS; i++) ids.add(String.format("rext-c%d-doc-%05d", c, i));
            docIds.add(ids);
            corrOrder.add(ids);
            List<String> shuffled = new ArrayList<>(ids);
            Collections.shuffle(shuffled, new Random(20260930098L + c));
            scatteredOrder.add(shuffled);
            deadDocs.add(new HashSet<>());

            Random rnd = new Random(20260930101L + c);
            List<float[]> vs = new ArrayList<>(ROWS);
            List<String> hx = new ArrayList<>(ROWS);
            List<String> tx = new ArrayList<>(ROWS);
            double[] sim = new double[ROWS];
            for (int i = 0; i < ROWS; i++) {
                hx.add(Chash.ofText("rext-c" + c + "-chunk-" + i).toHex());
                tx.add("recallExt fixture c" + c + " chunk " + i);
                float[] v = fixtureVector(rnd);
                vs.add(v);
                double dot = 0;
                for (int d = 0; d < DIM; d++) dot += v[d] * anchor[d];
                sim[i] = dot;
            }
            vectors.add(vs);
            chashHex.add(hx);
            texts.add(tx);

            // Manifest chunks sorted by cosine to the shared anchor, nearest first, cut into
            // DOCS runs: a prefix of the document list is one spherical cap (the msz9i shape).
            Integer[] bySim = new Integer[MANIFEST];
            for (int i = 0; i < MANIFEST; i++) bySim[i] = i;
            Arrays.sort(bySim, Comparator.comparingDouble(i -> -sim[i]));
            int[] doc = new int[ROWS];
            Arrays.fill(doc, -1);
            for (int k = 0; k < MANIFEST; k++) doc[bySim[k]] = (int) ((long) k * DOCS / MANIFEST);
            chunkDoc.add(doc);
        }
    }

    /** Real text, real embeddings (nexus-wbfpw.46): markdown files are the documents, packed
     *  paragraphs are the chunks, the provisioned bge-base ONNX model embeds them (cached on
     *  disk by content). Every {@link #HELD_OUT_EVERY}th passage is held out as a query, so a
     *  query is a real passage whose neighbours are its siblings, not a stored vector matching
     *  itself. Correlated deletion kills the documents whose centroid is nearest a seed
     *  document's, the topical-batch shape. No chunk is manifest-less here (the synthetic
     *  fixture keeps msz9i's quarter of them). */
    private void generateCorpus() throws Exception {
        assertThat(DIM).as("corpus mode embeds with bge-base").isEqualTo(768);
        assertThat(COLLECTIONS).as("corpus mode is one collection").isEqualTo(1);
        List<Path> files = new ArrayList<>();
        for (String root : CORPUS.split(",")) {
            try (var walk = Files.walk(Path.of(root.trim()))) {
                walk.filter(f -> Files.isRegularFile(f) && f.toString().endsWith(".md")).sorted().forEach(files::add);
            }
        }
        List<String> passages = new ArrayList<>();
        List<Integer> passageFile = new ArrayList<>();
        for (int f = 0; f < files.size(); f++) {
            String text = Files.readString(files.get(f), StandardCharsets.UTF_8).replace("\0", "");
            StringBuilder cur = new StringBuilder();
            for (String para : text.split("\n\\s*\n")) {
                if (cur.length() > 0 && cur.length() + para.length() > CORPUS_CHUNK_CHARS) {
                    if (cur.length() >= CORPUS_MIN_CHARS) { passages.add(cur.toString()); passageFile.add(f); }
                    cur.setLength(0);
                }
                cur.append(cur.length() > 0 ? "\n\n" : "").append(para);
            }
            if (cur.length() >= CORPUS_MIN_CHARS) { passages.add(cur.toString()); passageFile.add(f); }
        }
        float[][] emb = embedCached(passages);

        // Split into indexed and held-out; drop indexed duplicates (same chash collapses in T3).
        List<String> ixText = new ArrayList<>();
        List<float[]> ixVec = new ArrayList<>();
        List<Integer> ixFile = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        List<Integer> heldFile = new ArrayList<>();
        for (int i = 0; i < passages.size(); i++) {
            if (i % HELD_OUT_EVERY == 7) {
                heldOut.add(emb[i]);
                heldFile.add(passageFile.get(i));
            } else if (seen.add(Chash.ofText(passages.get(i)).toHex())) {
                ixText.add(passages.get(i));
                ixVec.add(emb[i]);
                ixFile.add(passageFile.get(i));
            }
        }
        // Documents = files that kept at least one indexed passage.
        Map<Integer, Integer> docOfFile = new LinkedHashMap<>();
        for (int f : ixFile) docOfFile.putIfAbsent(f, docOfFile.size());
        int docCount = docOfFile.size();
        List<String> ids = new ArrayList<>(docCount);
        for (int d = 0; d < docCount; d++) ids.add(String.format("rext-c0-doc-%05d", d));
        int[] doc = new int[ixText.size()];
        List<String> hx = new ArrayList<>(ixText.size());
        double[][] centroid = new double[docCount][DIM];
        for (int i = 0; i < ixText.size(); i++) {
            doc[i] = docOfFile.get(ixFile.get(i));
            hx.add(Chash.ofText(ixText.get(i)).toHex());
            float[] v = ixVec.get(i);
            double n = 0;
            for (float x : v) n += x * x;
            n = Math.sqrt(n);
            for (int d = 0; d < DIM; d++) centroid[doc[i]][d] += v[d] / n;
        }
        for (int i = 0; i < heldOut.size(); i++) {
            Integer d = docOfFile.get(heldFile.get(i));
            heldOutDoc.add(d == null ? -1 : d);
        }
        // Seed document: a seeded pick among documents with at least 8 passages.
        int[] passagesPerDoc = new int[docCount];
        for (int d : doc) passagesPerDoc[d]++;
        List<Integer> big = new ArrayList<>();
        for (int d = 0; d < docCount; d++) if (passagesPerDoc[d] >= 8) big.add(d);
        int seedDoc = big.get(new Random(20260930700L).nextInt(big.size()));
        double[] anchorC = centroid[seedDoc];
        double an = 0;
        for (double x : anchorC) an += x * x;
        an = Math.sqrt(an);
        double[] simToSeed = new double[docCount];
        for (int d = 0; d < docCount; d++) {
            double dot = 0, dn = 0;
            for (int k = 0; k < DIM; k++) { dot += centroid[d][k] * anchorC[k]; dn += centroid[d][k] * centroid[d][k]; }
            simToSeed[d] = dot / (an * Math.sqrt(dn));
        }
        Integer[] byTopic = new Integer[docCount];
        for (int d = 0; d < docCount; d++) byTopic[d] = d;
        Arrays.sort(byTopic, Comparator.comparingDouble(d -> -simToSeed[d]));
        List<String> topical = new ArrayList<>(docCount);
        for (Integer d : byTopic) topical.add(ids.get(d));

        docIds.add(ids);
        corrOrder.add(topical);
        List<String> shuffled = new ArrayList<>(ids);
        Collections.shuffle(shuffled, new Random(20260930098L));
        scatteredOrder.add(shuffled);
        deadDocs.add(new HashSet<>());
        vectors.add(ixVec);
        chashHex.add(hx);
        texts.add(ixText);
        chunkDoc.add(doc);
        log("corpus: " + files.size() + " files, " + passages.size() + " passages, " + ixText.size()
            + " indexed in " + docCount + " documents, " + heldOut.size() + " held out; seed document has "
            + passagesPerDoc[seedDoc] + " passages");
    }

    /**
     * Cache key for {@link #embedCached}: the passages AND the identity of the model that
     * embeds them (SHA-256 of the ONNX file and the tokenizer file). Keyed on the passages
     * alone, a cache file that outlives a model bump (it persists on a self-hosted runner,
     * unlike a hosted VM) would serve embeddings from the old model and the recall test
     * would pass against it.
     */
    static String cacheKey(List<String> passages, Path model, Path tokenizer) throws Exception {
        java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
        for (String t : passages) md.update(t.getBytes(StandardCharsets.UTF_8));
        md.update((byte) 0);
        md.update(fileDigest(model));
        md.update((byte) 0);
        md.update(fileDigest(tokenizer));
        return HexFormat.of().formatHex(md.digest()).substring(0, 16);
    }

    private static byte[] fileDigest(Path file) throws Exception {
        java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
        try (var in = Files.newInputStream(file)) {
            byte[] buf = new byte[1 << 16];
            for (int n; (n = in.read(buf)) > 0; ) md.update(buf, 0, n);
        }
        return md.digest();
    }

    /** Embeds {@code passages} with the provisioned bge-base ONNX model, cached by content and model. */
    private float[][] embedCached(List<String> passages) throws Exception {
        // The key needs the model on disk, so the provisioning gate runs BEFORE the cache read.
        OrtTestModel.requireBgeOrSkip();
        String key = cacheKey(passages, Path.of(Bge768Embedder.DEFAULT_MODEL_PATH),
            Path.of(Bge768Embedder.DEFAULT_TOKENIZER_PATH));
        Path cache = Path.of(System.getProperty("nx.recallExt.cacheDir", System.getProperty("java.io.tmpdir")),
            "recallExt-bge768-" + key + ".bin");
        float[][] out = new float[passages.size()][];
        if (Files.isRegularFile(cache)) {
            try (var in = new java.io.DataInputStream(new java.io.BufferedInputStream(Files.newInputStream(cache)))) {
                for (int i = 0; i < out.length; i++) {
                    out[i] = new float[DIM];
                    for (int d = 0; d < DIM; d++) out[i][d] = in.readFloat();
                }
            }
            log("embeddings read from cache " + cache);
            return out;
        }
        Bge768Embedder embedder = new Bge768Embedder();
        try {
            long t0 = System.nanoTime();
            int batch = 32;
            for (int s0 = 0; s0 < passages.size(); s0 += batch) {
                int e0 = Math.min(s0 + batch, passages.size());
                List<float[]> part = embedder.embed(passages.subList(s0, e0));
                for (int i = s0; i < e0; i++) out[i] = part.get(i - s0);
                if ((s0 / batch) % 50 == 49) {
                    log("embedded " + e0 + "/" + passages.size() + " in " + (System.nanoTime() - t0) / 1_000_000_000L + "s");
                }
            }
        } finally {
            embedder.close();
        }
        try (var o = new java.io.DataOutputStream(new java.io.BufferedOutputStream(Files.newOutputStream(cache)))) {
            for (float[] v : out) for (float x : v) o.writeFloat(x);
        }
        return out;
    }

    /** Resets every document to live, then tombstones a prefix of each dead collection's
     *  document order sized to {@code percent}. */
    private void setDeadFraction(int percent) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.update(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.DELETED_AT, (java.time.OffsetDateTime) null)
                .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT))
                .execute();
            for (int c = 0; c < COLLECTIONS; c++) {
                deadDocs.get(c).clear();
                if (percent == 0 || (DEAD_IN_ONE && c > 0)) continue;
                List<String> order = CORRELATED ? corrOrder.get(c) : scatteredOrder.get(c);
                int count = (int) Math.ceil(percent / 100.0 * order.size());
                List<String> dead = order.subList(0, count);
                ctx.update(CATALOG_DOCUMENTS)
                    .set(CATALOG_DOCUMENTS.DELETED_AT, DSL.currentOffsetDateTime())
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.in(dead)))
                    .execute();
                Map<String, Integer> index = new LinkedHashMap<>();
                for (int i = 0; i < docIds.get(c).size(); i++) index.put(docIds.get(c).get(i), i);
                for (String id : dead) deadDocs.get(c).add(index.get(id));
            }
            PgContainerHelper.analyzeTable(su, CATALOG_DOCUMENTS);
        }
    }

    private static float[] fixtureVector(Random rnd) {
        double[] z = new double[LOW_RANK];
        for (int k = 0; k < LOW_RANK; k++) z[k] = rnd.nextGaussian();
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

    private static List<Integer> ints(String csv) {
        List<Integer> out = new ArrayList<>();
        for (String s : csv.split(",")) {
            if (!s.isBlank()) out.add(Integer.parseInt(s.trim()));
        }
        return out;
    }

    private static String vectorLiteral(float[] v) {
        StringBuilder sb = new StringBuilder(v.length * 8 + 2).append('[');
        for (int i = 0; i < v.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(v[i]);
        }
        return sb.append(']').toString();
    }

    // ── production statement, and the exact oracle ──────────────────────────

    private Table<?> plainSearch(float[] q, String collection) {
        Vector vec = Vector.of(q);
        String[] colls = {collection};
        return switch (DIM) {
            case 384 -> PLAIN_SEARCH_384.call(vec, colls, (JSONB) null, (String) null, K, modelToken(), TENANT);
            case 768 -> PLAIN_SEARCH_768.call(vec, colls, (JSONB) null, (String) null, K, modelToken(), TENANT);
            case 1024 -> PLAIN_SEARCH_1024.call(vec, colls, (JSONB) null, (String) null, K, modelToken(), TENANT);
            default -> throw new IllegalArgumentException("unsupported dim " + DIM);
        };
    }

    /** The "prod" profile: the GUCs PgVectorRepository#searchWithTokens sets before plain_search
     *  (nexus-wbfpw.47: including the scan budget). Any other profile is a sweep knob layered on
     *  the pgvector defaults, with {@code hnsw.max_scan_tuples} etc. set through set_config. */
    private static void productionSession(DSLContext ctx, Setting s) {
        if (s.name().equals("prod")) {
            // nexus-wbfpw.47: the "prod" profile IS whatever the repository sets, through the
            // same PgSession helpers PgVectorRepository#searchWithTokens calls, so a change to
            // the serving settings moves this measurement with it instead of leaving a stale copy.
            PgSession.setLocal(ctx, "hnsw.iterative_scan", "relaxed_order");
            PgSession.setHnswEfSearch(ctx, K);
            PgSession.setHnswScanBudget(ctx);
            PgSession.setSearchStatementTimeout(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            return;
        }
        PgSession.setLocal(ctx, "hnsw.iterative_scan", s.iterativeScan());
        PgSession.setLocal(ctx, "hnsw.ef_search", Integer.toString(s.efSearch()));
        if (s.maxScan() != null) {
            ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                    DSL.val("hnsw.max_scan_tuples"), DSL.val(Integer.toString(s.maxScan())), DSL.inline(true)))
                .fetch();
        }
        if (s.workMem() != null) setGuc(ctx, "work_mem", s.workMem());
        if (s.memMultiplier() != null) setGuc(ctx, "hnsw.scan_mem_multiplier", s.memMultiplier());
        PgSession.setSearchStatementTimeout(ctx);
        PgSession.setSearchPlanCacheMode(ctx);
    }

    /** A transaction-local GUC outside PgSession's whitelist (test-only sweep knobs). */
    private static void setGuc(DSLContext ctx, String name, String value) {
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val(name), DSL.val(value), DSL.inline(true)))
            .fetch();
    }

    private static List<String> ids(Result<? extends Record> rows) {
        List<String> out = new ArrayList<>(rows.size());
        for (Record r : rows) out.add(r.get("id", String.class));
        return out;
    }

    /** Production's statement under {@code s}. Returns null on a statement timeout. */
    private List<String> runProd(Probe q, Setting s, boolean exactFallback) {
        try {
            return tenantScope.withTenant(TENANT, ctx -> {
                productionSession(ctx, s);
                if (exactFallback) PgSession.disableIndexScanForExactFallback(ctx);
                return ids(ctx.selectFrom(plainSearch(q.vector(), collections.get(q.collection()))).fetch());
            });
        } catch (org.jooq.exception.DataAccessException e) {
            if (String.valueOf(e.getMessage()).contains("57014")
                || String.valueOf(e.getMessage()).contains("statement timeout")) {
                return null;
            }
            throw e;
        }
    }

    private static String exactSql() {
        return "SELECT encode(c.chash, 'hex') AS id FROM nexus.chunks c"
            + " WHERE c.collection = ? AND c.embedding_" + DIM + " IS NOT NULL"
            + " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))"
            + " ORDER BY (c.embedding_" + DIM + " OPERATOR(nexus.<=>) ?::nexus.vector) + 0"
            + " LIMIT ?";
    }

    /** The one raw-SQL read in this class: the exact oracle, its EXPLAIN and the live-count pin
     *  all go through it. SANCTIONED RAW (nexus-wbfpw.44): {@code ORDER BY (distance) + 0} and a
     *  set-returning function inside {@code EXISTS} have no typed jOOQ form, and the oracle must
     *  be the same predicate text as the statement it grades. */
    private Result<Record> rawRows(String sql, Object... binds) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.fetch(sql, binds));
    }

    private List<String> runExact(Probe q) {
        return ids(rawRows(exactSql(), collections.get(q.collection()), vectorLiteral(q.vector()), K));
    }

    /** Full-population live count for one collection, for the fixture pin. */
    private long liveCount(int c) {
        return rawRows("SELECT count(*) FROM nexus.chunks c WHERE c.collection = ?"
            + " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))",
            collections.get(c)).get(0).get(0, Long.class);
    }

    private long expectedLive(int c) {
        long n = 0;
        int[] doc = chunkDoc.get(c);
        for (int i = 0; i < doc.length; i++) {
            if (doc[i] >= 0 && !deadDocs.get(c).contains(doc[i])) n++;
        }
        return n;
    }

    private String explainProd(Probe q, Setting s) {
        return tenantScope.withTenant(TENANT, ctx -> {
            productionSession(ctx, s);
            return ctx.explain(ctx.selectFrom(plainSearch(q.vector(), collections.get(q.collection())))).plan();
        });
    }

    private String explainExact(Probe q) {
        StringBuilder sb = new StringBuilder();
        for (Record r : rawRows("EXPLAIN " + exactSql(), collections.get(q.collection()), vectorLiteral(q.vector()), K)) {
            sb.append(r.get(0, String.class)).append('\n');
        }
        return sb.toString();
    }

    private String pgvectorVersion() {
        return tenantScope.withTenant(TENANT, ctx -> ctx.select(DSL.field("extversion", String.class))
            .from(DSL.table("pg_catalog.pg_extension")).where(DSL.field("extname").eq("vector"))
            .fetch().get(0).get(0, String.class));
    }

    /** {@code current_setting(guc, true)}: null-safe for a GUC this pgvector build lacks. */
    private String currentSettingOrAbsent(String guc) {
        String v = tenantScope.withTenant(TENANT, ctx -> ctx.select(DSL.function("current_setting",
            SQLDataType.VARCHAR, DSL.inline(guc), DSL.inline(true))).fetch().get(0).get(0, String.class));
        return v == null ? "ABSENT" : v;
    }

    private String currentSetting(String guc) {
        return tenantScope.withTenant(TENANT, ctx -> ctx.select(DSL.function("current_setting",
            SQLDataType.VARCHAR, DSL.inline(guc))).fetch().get(0).get(0, String.class));
    }

    // ── queries ─────────────────────────────────────────────────────────────

    private List<Probe> uniformQueries(long seed) {
        Random rnd = new Random(seed);
        List<Probe> out = new ArrayList<>();
        if (CORPUS != null) {
            for (int q = 0; q < QUERIES; q++) out.add(new Probe(0, "uniform", heldOut.get(rnd.nextInt(heldOut.size()))));
            return out;
        }
        for (int c = 0; c < COLLECTIONS; c++) {
            for (int q = 0; q < QUERIES; q++) out.add(new Probe(c, "uniform", fixtureVector(rnd)));
        }
        return out;
    }

    /** Queries aimed into the deleted cap: the embeddings of seeded-random chunks owned by
     *  tombstoned documents. A query for collection c is aimed at c's own dead chunks when c
     *  lost documents, else at the first dead collection's (deadIn=one: the live collections
     *  are searched exactly where a neighbour collection's notes were retracted). */
    private List<Probe> inCapQueries(long seed) {
        Random rnd = new Random(seed);
        List<Probe> out = new ArrayList<>();
        if (CORPUS != null) {
            List<Integer> inDead = new ArrayList<>();
            for (int i = 0; i < heldOut.size(); i++) if (deadDocs.get(0).contains(heldOutDoc.get(i))) inDead.add(i);
            assertThat(inDead).as("no held-out passage belongs to a tombstoned document").isNotEmpty();
            for (int q = 0; q < QUERIES; q++) out.add(new Probe(0, "inCap", heldOut.get(inDead.get(rnd.nextInt(inDead.size())))));
            return out;
        }
        for (int c = 0; c < COLLECTIONS; c++) {
            int src = deadDocs.get(c).isEmpty() ? 0 : c;
            List<Integer> dead = new ArrayList<>();
            int[] doc = chunkDoc.get(src);
            for (int i = 0; i < doc.length; i++) {
                if (doc[i] >= 0 && deadDocs.get(src).contains(doc[i])) dead.add(i);
            }
            assertThat(dead).as("no tombstone-owned chunks to aim at in collection %d", src).isNotEmpty();
            for (int q = 0; q < QUERIES; q++) {
                out.add(new Probe(c, "inCap", vectors.get(src).get(dead.get(rnd.nextInt(dead.size())))));
            }
        }
        return out;
    }

    private static double recallAt(List<String> approx, List<String> oracle) {
        if (oracle.isEmpty()) return 1.0;
        Set<String> top = new HashSet<>(oracle.subList(0, Math.min(K, oracle.size())));
        long hits = approx.stream().limit(K).filter(top::contains).count();
        return (double) hits / top.size();
    }

    // ── measurement ─────────────────────────────────────────────────────────

    /** The production profile first, then the sweep: {@code nx.recallExt.settings} is a comma
     *  list of {@code name:iterativeScan:efSearch[:maxScanTuples[:workMem[:scanMemMultiplier]]]}
     *  (empty fields keep the server default); the default varies one knob
     *  at a time (max_scan_tuples, ef_search, strict_order) away from production. */
    private List<Setting> settings() {
        List<Setting> out = new ArrayList<>();
        out.add(new Setting("prod", "relaxed_order", 200, null));
        if (Boolean.getBoolean("nx.recallExt.legacy")) {
            // The serving settings BEFORE nexus-wbfpw.47 (pgvector's 20000-tuple, 1x-memory
            // defaults): the control that shows what the raised budget buys.
            out.add(new Setting("legacy", "relaxed_order", 200, null));
        }
        String spec = System.getProperty("nx.recallExt.settings",
            "scan1000000:relaxed_order:200:1000000,strict200:strict_order:200,"
                + "ef1000:relaxed_order:1000,strict1000:strict_order:1000");
        for (String one : spec.split(",")) {
            if (one.isBlank()) continue;
            String[] f = one.trim().split(":");
            out.add(new Setting(f[0], f[1], Integer.parseInt(f[2]),
                f.length > 3 && !f[3].isEmpty() ? Integer.valueOf(f[3]) : null,
                f.length > 4 && !f[4].isEmpty() ? f[4] : null,
                f.length > 5 && !f[5].isEmpty() ? f[5] : null));
        }
        return out;
    }

    private static final Setting STARVED = new Setting("starved", "off", K, null);

    /** Measures every query in {@code queries} under {@code s}, against the precomputed oracle. */
    private List<Outcome> measure(List<Probe> queries, List<List<String>> oracles, Setting s) {
        List<Outcome> out = new ArrayList<>(queries.size());
        for (int i = 0; i < queries.size(); i++) {
            long t0 = System.nanoTime();
            List<String> approx = runProd(queries.get(i), s, false);
            long ms = (System.nanoTime() - t0) / 1_000_000L;
            out.add(approx == null
                ? new Outcome(0.0, -1, ms)
                : new Outcome(recallAt(approx, oracles.get(i)), approx.size(), ms));
        }
        return out;
    }

    private static double avg(List<Double> xs) {
        return xs.stream().mapToDouble(Double::doubleValue).average().orElse(Double.NaN);
    }

    private static String pctile(List<Long> ms, double p) {
        List<Long> sorted = ms.stream().sorted().toList();
        return Long.toString(sorted.get(Math.min(sorted.size() - 1, (int) Math.ceil(sorted.size() * p) - 1)));
    }

    /** One report line for a (fraction, setting, kind, collection-scope) cell. */
    private void report(int pct, Setting s, String kind, String scope, boolean hnsw, List<Outcome> os) {
        List<Double> raw = os.stream().map(Outcome::recall).toList();
        long empty = os.stream().filter(o -> o.rows() == 0).count();
        long timeout = os.stream().filter(o -> o.rows() == -1).count();
        long shortN = os.stream().filter(o -> o.rows() > 0 && o.rows() < K).count();
        long below90 = raw.stream().filter(r -> r < 0.9).count();
        // Production reruns an EMPTY result exactly, so an empty result there is recall 1.0.
        double rescued = avg(os.stream().map(o -> o.rows() == 0 ? 1.0 : o.recall()).toList());
        // What a rescue on ANY under-return (fewer than K rows, empty included) would give.
        double rescuedShort = avg(os.stream().map(o -> o.rows() >= 0 && o.rows() < K ? 1.0 : o.recall()).toList());
        List<Long> ms = os.stream().map(Outcome::millis).toList();
        System.out.printf(
            "[recallExt] phase=%s dim=%d colls=%d rows/coll=%d deadIn=%s mode=%s dead=%d%% kind=%-7s scope=%-4s setting=%-11s"
                + " n=%d recall=%.3f min=%.2f below0.9=%d empty=%d short=%d timeout=%d recallWithEmptyRescue=%.3f"
                + " recallWithShortRescue=%.3f p50ms=%s maxms=%s hnswPlan=%s%n",
            phase, DIM, COLLECTIONS, vectors.get(0).size(), DEAD_IN_ONE ? "one" : "all", CORRELATED ? "corr" : "scat", pct, kind, scope,
            s.name(), os.size(), avg(raw), raw.stream().mapToDouble(Double::doubleValue).min().orElse(Double.NaN),
            below90, empty, shortN, timeout, rescued, rescuedShort, pctile(ms, 0.5), pctile(ms, 1.0), hnsw);
        if (ASSERT_FLOOR && s.name().equals("prod")) {
            assertThat(avg(raw)).as("live(c) recall@%d %s/%s at %d%% dead, production settings", K, kind, scope, pct)
                .isGreaterThanOrEqualTo(FLOOR);
        }
    }

    @Test
    void recall_extended() throws Exception {
        log("dim=" + DIM + " collections=" + COLLECTIONS + " rows/coll=" + vectors.get(0).size()
            + " docs/coll=" + docIds.get(0).size() + " queries/coll/kind=" + QUERIES + " fractions=" + FRACTIONS
            + " deadIn=" + (DEAD_IN_ONE ? "one" : "all") + " mode=" + (CORRELATED ? "correlated" : "scattered")
            + " hnsw.max_scan_tuples(default)=" + currentSetting("hnsw.max_scan_tuples")
            + " hnsw.ef_search(default)=" + currentSetting("hnsw.ef_search")
            + " hnsw.iterative_scan(default)=" + currentSetting("hnsw.iterative_scan")
            + " work_mem(default)=" + currentSetting("work_mem")
            + " pgvector=" + pgvectorVersion()
            + " hnsw.scan_mem_multiplier(default)=" + currentSettingOrAbsent("hnsw.scan_mem_multiplier"));

        // Pin 1: the oracle must not be served by HNSW, or it is not exact.
        setDeadFraction(0);
        Probe probe = uniformQueries(20260930300L).get(0);
        String exactPlan = explainExact(probe);
        assertThat(exactPlan).as("the exact oracle must not use HNSW. Plan:%n%s", exactPlan)
            .doesNotContain("idx_chunks_embedding_" + DIM);

        // Baseline (0% dead, live(c) only removes the manifest-less quarter): the fixture
        // must let production settings reach 0.9 on every collection, or a low number later
        // cannot be told from the fixture's own approximation error.
        {
            List<Probe> qs = uniformQueries(20260930310L);
            List<List<String>> oracles = new ArrayList<>();
            for (Probe q : qs) oracles.add(runExact(q));
            List<Outcome> os = measure(qs, oracles, settings().get(0));
            report(0, settings().get(0), "uniform", "all", true, os);
            assertThat(avg(os.stream().map(Outcome::recall).toList()))
                .as("unfiltered-by-deletion baseline must reach 0.9 or the fixture cannot separate loss causes")
                .isGreaterThanOrEqualTo(0.9);
        }

        double starvedAtTop = sweep("built");
        if (REINDEX) {
            reindex();
            starvedAtTop = sweep("reindexed");
        }
        assertThat(starvedAtTop)
            .as("positive control: a starved search (iterative_scan=off, ef_search=K) must lose recall at the highest fraction")
            .isLessThan(0.9);
        log("DONE");
    }

    private void reindex() throws Exception {
        long t0 = System.nanoTime();
        try (Connection su = pg.createConnection(""); java.sql.Statement st = su.createStatement()) {
            // Serial build: a parallel build needs a /dev/shm segment of about maintenance_work_mem,
            // and the container's default shm is 64MB ("No space left on device").
            String sql = "SET max_parallel_maintenance_workers = 0; SET maintenance_work_mem = '1GB'; "
                + "REINDEX INDEX nexus.idx_chunks_embedding_" + DIM;
            st.execute(sql);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.analyzeTable(su, CHUNKS);
        }
        log("REINDEX idx_chunks_embedding_" + DIM + " took " + (System.nanoTime() - t0) / 1_000_000_000L + "s");
    }

    /** One pass over {@link #FRACTIONS}; returns the starved control's recall at the top fraction. */
    private double sweep(String phaseName) throws Exception {
        phase = phaseName;
        double starvedAtTop = Double.NaN;
        for (int pct : FRACTIONS) {
            setDeadFraction(pct);
            for (int c = 0; c < COLLECTIONS; c++) {
                assertThat(liveCount(c)).as("live(c) population of collection %d at %d%% dead", c, pct)
                    .isEqualTo(expectedLive(c));
            }
            for (int c = 0; c < COLLECTIONS; c++) {
                log("dead=" + pct + "%: collection " + c + " has " + deadDocs.get(c).size() + " of "
                    + docIds.get(c).size() + " documents tombstoned, live(c) chunks " + expectedLive(c)
                    + " of " + vectors.get(c).size());
            }
            long tOracle = System.nanoTime();
            for (String kind : List.of("uniform", "inCap")) {
                List<Probe> qs = kind.equals("uniform") ? uniformQueries(20260930400L + pct) : inCapQueries(20260930500L + pct);
                List<List<String>> oracles = new ArrayList<>(qs.size());
                for (Probe q : qs) oracles.add(runExact(q));
                boolean anyFull = oracles.stream().anyMatch(o -> o.size() == K);
                assertThat(anyFull).as("no query had K live neighbours at %d%% dead: recall would be vacuous", pct).isTrue();

                for (Setting s : settings()) {
                    String plan = explainProd(qs.get(0), s);
                    boolean hnsw = plan.contains("idx_chunks_embedding_" + DIM);
                    List<Outcome> os = measure(qs, oracles, s);
                    report(pct, s, kind, "all", hnsw, os);
                    if (s.name().equals("prod") && COLLECTIONS > 1) {
                        for (int c = 0; c < COLLECTIONS; c++) {
                            final int cc = c;
                            List<Outcome> mine = new ArrayList<>();
                            for (int i = 0; i < qs.size(); i++) if (qs.get(i).collection() == cc) mine.add(os.get(i));
                            report(pct, s, kind, "c" + c, hnsw, mine);
                        }
                    }
                    if (s.name().equals("prod")) {
                        // Time production's empty-result rescue on a sample of the empties.
                        int sampled = 0;
                        for (int i = 0; i < qs.size() && sampled < FALLBACK_SAMPLES; i++) {
                            if (os.get(i).rows() != 0) continue;
                            sampled++;
                            long t0 = System.nanoTime();
                            List<String> exact = runProd(qs.get(i), s, true);
                            long ms = (System.nanoTime() - t0) / 1_000_000L;
                            System.out.printf("[recallExt] dim=%d dead=%d%% kind=%s emptyRescue query#%d ms=%d %s%n",
                                DIM, pct, kind, i, ms,
                                exact == null ? "TIMEOUT(statement_timeout)" : "rows=" + exact.size()
                                    + " recallVsOracle=" + String.format("%.2f", recallAt(exact, oracles.get(i))));
                        }
                    }
                }
                if (pct == FRACTIONS.get(FRACTIONS.size() - 1) && kind.equals("inCap")) {
                    List<Outcome> os = measure(qs, oracles, STARVED);
                    starvedAtTop = avg(os.stream().map(Outcome::recall).toList());
                    report(pct, STARVED, kind, "all", true, os);
                }
            }
            log("dead=" + pct + "% done, oracle+measure " + (System.nanoTime() - tOracle) / 1_000_000_000L + "s");
        }
        return starvedAtTop;
    }
}
