// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
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
import static dev.nexus.service.jooq.nexus.Tables.PLAIN_SEARCH_384;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-192 Step 4 (bead nexus-wbfpw.9): the EXPLAIN + recall + latency evidence
 * the bead's acceptance criteria requires for {@code nexus.chunk_live_owners},
 * on a moderate-scale (default 500-chunk, one-collection) fixture in the same
 * style as {@code HybridSearchFunctionParityIntegrationTest}'s own corpus (a
 * sibling fixture, per the bead's own instruction, rather than reusing that
 * class's private setup methods directly).
 *
 * <p><b>Round 2 (code-review-expert Critical, T2 nexus/review-wbfpw9-code):
 * the round-1 function, {@code nexus.chunk_is_live(...) RETURNS boolean}, is
 * a SCALAR function whose body is {@code SELECT EXISTS(SELECT 1 FROM ... JOIN
 * ...)}. PostgreSQL's scalar-function inliner ({@code inline_function})
 * requires an EMPTY range table and no SubLink in the function's own body; an
 * EXISTS(...) subquery in the target list is exactly a SubLink, so that shape
 * can never be inlined, no matter how LANGUAGE sql/STABLE/SECURITY
 * INVOKER/no-SET are set. The round-1 EXPLAIN evidence proved this directly:
 * the literal function name appeared in the plan's Filter clause, an opaque
 * per-row call, which is what actually caused the measured ~9x latency
 * regression -- see T2 nexus/review-wbfpw9-code for the full plan text and
 * derivation. Round 2 replaces the function with {@code
 * nexus.chunk_live_owners(...) RETURNS TABLE(doc_id text)} -- a SET-RETURNING
 * function, which PostgreSQL's OTHER inliner ({@code
 * inline_set_returning_function}) DOES tolerate a join/subquery body for,
 * exactly like {@code plain_search_384}'s own inlined anti-join. Callers write
 * {@code EXISTS (SELECT 1 FROM nexus.chunk_live_owners(...))}; the tests below
 * verify (not assume) that this shape genuinely inlines, by asserting the
 * function's OWN NAME is ABSENT from the EXPLAIN plan (proof the FuncExpr was
 * substituted away) and that {@code catalog_document_chunks} is read directly
 * (proof of a real semi-join, not an opaque call).</b>
 *
 * <p><b>Fixture design.</b> {@link #LIVE_COUNT} live chunks (own-collection
 * manifest row, live document) plus one manifest-less "noise" chunk PER QUERY
 * (no manifest row anywhere -- R1's shape), each noise chunk's text set to the
 * EXACT query string it targets so its embedding is (near-)identical to the
 * query vector and it wins rank 1 in a plain nearest-neighbor search --
 * exactly the class of chunk {@code nexus.chunk_live_owners} exists to
 * exclude.
 *
 * <p><b>Recall methodology (avoids needing an independent raw-SQL oracle
 * query, so this file's raw-SQL footprint stays small):</b> {@code
 * nexus.plain_search_384} (today's shipped predicate) at {@code LIMIT
 * K+noise_count} on the FULL corpus (live + noise) gives every noise chunk
 * plus the true top-K live chunks in one call -- every noise chunk is
 * guaranteed present (plain_search's own anti-join has no manifest-less
 * guard, Gap 1 item 1) and guaranteed close (exact text match), so removing
 * ALL known noise chashes from that superset leaves exactly the live-only
 * oracle top-K. {@code plain_search_384} at {@code LIMIT K=10} (today's
 * actual production shape) is "before"; the raw {@code EXISTS(SELECT 1 FROM
 * nexus.chunk_live_owners(...))}-filtered KNN at {@code LIMIT K=10} is
 * "after". Recall@10 is measured against that oracle for both.
 *
 * <p><b>What this file does NOT cover (see the sibling
 * ChunkLiveOwnersMsz9iScaleIntegrationTest):</b> this fixture is small (510
 * chunks) and has no tombstoned documents, so it cannot exercise cost that
 * scales with manifest size or tombstone fraction -- exactly the axis
 * nexus-msz9i's own investigation needed a dedicated 76k-chunk/57k-manifest-
 * row/1k-document fixture with a tombstone-fraction sweep (3/10/30/60%) to
 * characterize. That fixture, the controlled (same-harness, same-binding)
 * before/after latency comparison, and the filtered-HNSW recall-under-
 * selectivity measurement all live in the sibling class.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunkLiveOwnersInlineRecallIntegrationTest {

    private static final String TENANT = "chunkliveowners-explain";
    private static final String COLLECTION = "knowledge__chunkliveowners__minilm-l6-v2-384__v1";

    private static final int LIVE_COUNT =
        Integer.getInteger("nx.chunkliveowners.explain.size", 500);
    private static final int QUERY_COUNT =
        Integer.getInteger("nx.chunkliveowners.explain.queries", 10);
    private static final int K = 10;
    private static final int LATENCY_ROUNDS =
        Integer.getInteger("nx.chunkliveowners.explain.rounds", 5);

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
            corpus.put(String.format("clo-doc-%05d", d), String.join(" ", words) + " doc" + d);
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
            String chash = Chash.ofText("clo-noise-" + q).toHex();
            noiseChashHex.add(chash);
            noiseChashes.add(chash);
            noiseTexts.add(q);
            noiseMetas.add(Map.of());
        }
        pgRepo.upsertChunks(TENANT, COLLECTION, noiseChashes, noiseTexts, noiseMetas);
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
     *  generated typed table function -- not raw SQL, native Vector binding. */
    private List<String> plainSearch384(float[] vec, int n) {
        Table<?> fn = PLAIN_SEARCH_384.call(Vector.of(vec), new String[] {COLLECTION}, null, null, n);
        return tenantScope.withTenant(TENANT, ctx -> ctx.selectFrom(fn)
            .fetch(r -> r.get("id", String.class)));
    }

    /**
     * "after": a raw KNN query with {@code EXISTS (SELECT 1 FROM
     * nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))} in the
     * WHERE clause -- Step 5's not-yet-wired shape, gathered here purely as
     * evidence (this bead does not migrate any production call site).
     * SANCTIONED RAW (nexus-wbfpw.9, TEST-TREE RATCHET): no typed-DSL vector
     * KNN form exists in this codebase's test conventions (every sibling KNN
     * plan-shape test -- GraphHopParityTest, TaxonomyAssignCrossLateralHnswTest
     * -- uses the identical raw-literal-SQL idiom for the same reason: the
     * {@code OPERATOR(nexus.<=>)} vector-distance ORDER BY has no jOOQ DSL
     * operator).
     */
    private List<String> chunkLiveOwnersFilteredKnn(String collection, float[] vec, int n) {
        return knn(collection, vec, n, true);
    }

    private List<String> knn(String collection, float[] vec, int n, boolean liveOnly) {
        Result<Record> rows = tenantScope.withTenant(TENANT, ctx -> ctx.fetch(
            "SELECT encode(c.chash, 'hex') FROM nexus.chunks c"
            + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
            + (liveOnly
                ? " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))"
                : "")
            + " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector"
            + " LIMIT ?",
            collection, vectorLiteral(vec), n));
        List<String> ids = new ArrayList<>(rows.size());
        for (var rec : rows) ids.add(rec.get(0, String.class));
        return ids;
    }

    /** {@link #chunkLiveOwnersFilteredKnn} without any liveness predicate: the
     *  independent baseline the oracle is cut from and the non-vacuity control for
     *  the noise fixture. Since RDR-192 Step 5 {@code plain_search_384} itself uses
     *  live(c), so it can no longer serve as either. */
    private List<String> unfilteredKnn(String collection, float[] vec, int n) {
        return knn(collection, vec, n, false);
    }

    /** EXPLAIN (ANALYZE, BUFFERS) twin of {@link #chunkLiveOwnersFilteredKnn} --
     *  same statement body, ANALYZE-prefixed. SANCTIONED RAW (nexus-wbfpw.9,
     *  TEST-TREE RATCHET). */
    private String explainChunkLiveOwnersFilteredKnn(String collection, float[] vec, int n) {
        return tenantScope.withTenant(TENANT, ctx -> {
            Result<Record> rows = ctx.fetch(
                "EXPLAIN (ANALYZE, BUFFERS) SELECT encode(c.chash, 'hex') FROM nexus.chunks c"
                + " WHERE c.collection = ? AND c.embedding_384 IS NOT NULL"
                + " AND EXISTS (SELECT 1 FROM nexus.chunk_live_owners(c.tenant_id, c.collection, c.chash))"
                + " ORDER BY c.embedding_384 OPERATOR(nexus.<=>) ?::nexus.vector"
                + " LIMIT ?",
                collection, vectorLiteral(vec), n);
            List<String> lines = new ArrayList<>();
            for (var rec : rows) lines.add(rec.get(0, String.class));
            return String.join("\n", lines);
        });
    }

    /** The live-only oracle top-{@link #K}: an UNFILTERED KNN at LIMIT K + the
     *  full noise population's headroom, minus EVERY known noise chash (not only a
     *  single query's own -- a query's text can be semantically close enough to
     *  ANOTHER query's noise chunk, drawn from the same shared word bank, to also
     *  intrude on its top-K window), truncated back to K. */
    private List<String> oracleTop10(float[] vec) {
        List<String> candidates = unfilteredKnn(COLLECTION, vec, K + noiseChashHex.size());
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

    // ── EXPLAIN: chunk_live_owners actually inlines as a semi-join ──────────

    /**
     * Round-2 pin (replaces round-1's non-falsifiable version, T2
     * nexus/review-wbfpw9-code): a genuinely inlined {@code
     * EXISTS(SELECT 1 FROM nexus.chunk_live_owners(...))} must NOT show the
     * function's own name anywhere in the plan -- inlining substitutes the
     * FuncExpr with the function body's query tree, so the deparser has no
     * function name left to print. It MUST show {@code
     * catalog_document_chunks} being read directly (the join the function
     * body performs), proof of a real semi-join against the base tables, not
     * an opaque per-row call.
     *
     * <p>Verified falsifiable: temporarily marking the function {@code
     * SECURITY DEFINER} (which PostgreSQL's inliner explicitly refuses to
     * inline) reproduces round 1's defect exactly -- the function name
     * reappears in the plan's Filter clause and {@code catalog_document_chunks}
     * disappears from it. See T2 nexus/wbfpw9-impl for the mutation run.
     */
    @Test
    void explain_chunkLiveOwnersPredicate_inlinesAsASemiJoin() throws Exception {
        float[] vec = embedQuery(queries.get(0));
        String plan = explainChunkLiveOwnersFilteredKnn(COLLECTION, vec, K);

        System.out.println("[nexus-wbfpw.9 EXPLAIN, " + (LIVE_COUNT + QUERY_COUNT)
            + " chunks] chunk_live_owners-filtered KNN plan:\n" + plan);

        assertThat(plan)
            .as("a genuinely inlined EXISTS(chunk_live_owners(...)) must not show the"
                + " function's own name anywhere in the plan -- its presence is direct"
                + " proof the call stayed opaque (round-1's defect). Plan was:%n%s", plan)
            .doesNotContain("chunk_live_owners");
        assertThat(plan)
            .as("the inlined body must read catalog_document_chunks directly -- proof of"
                + " a real semi-join against the base manifest table, not an opaque"
                + " function call. Plan was:%n%s", plan)
            .contains("catalog_document_chunks");
        assertThat(plan)
            .as("the base table scan must be an index scan, not an unqualified sequential"
                + " scan of nexus.chunks. Plan was:%n%s", plan)
            .contains("Index Scan");
    }

    // ── recall@10: plain_search_384 now carries live(c) (RDR-192 Step 5, nexus-wbfpw.10) ──

    @Test
    void recall_plainSearchExcludesManifestLessNoise_andMatchesChunkLiveOwners() throws Exception {
        List<Double> recallUnfiltered = new ArrayList<>();
        List<Double> recallPlain = new ArrayList<>();

        for (int q = 0; q < queries.size(); q++) {
            float[] vec = embedQuery(queries.get(q));
            List<String> oracle = oracleTop10(vec);
            List<String> unfiltered = unfilteredKnn(COLLECTION, vec, K);
            List<String> plain = plainSearch384(vec, K);
            List<String> live = chunkLiveOwnersFilteredKnn(COLLECTION, vec, K);

            assertThat(unfiltered)
                .as("query %d: control -- an unfiltered KNN returns the query's own"
                    + " manifest-less noise chunk, so the assertions below are not vacuous", q)
                .contains(noiseChashHex.get(q));
            assertThat(plain)
                .as("query %d: plain_search_384 carries live(c) since vectors-019-1 and must"
                    + " exclude the manifest-less noise chunk", q)
                .doesNotContain(noiseChashHex.get(q));
            assertThat(plain)
                .as("query %d: the routed search function returns exactly what the raw"
                    + " live(c) query returns", q)
                .containsExactlyInAnyOrderElementsOf(live);

            recallUnfiltered.add(recallAt10(unfiltered, oracle));
            recallPlain.add(recallAt10(plain, oracle));
        }

        double avgUnfiltered = recallUnfiltered.stream().mapToDouble(Double::doubleValue).average().orElseThrow();
        double avgPlain = recallPlain.stream().mapToDouble(Double::doubleValue).average().orElseThrow();

        System.out.println("[nexus-wbfpw.10 RECALL@10, small fixture] unfiltered=" + avgUnfiltered
            + " plain_search_384(live(c))=" + avgPlain + " over " + queries.size() + " queries");

        assertThat(avgUnfiltered)
            .as("sanity: the noise fixture must degrade an unfiltered KNN's recall against"
                + " the live-only oracle, or this test proves nothing")
            .isLessThan(1.0);
        assertThat(avgPlain)
            .as("plain_search_384 must reach full recall@10 against the live-only oracle")
            .isEqualTo(1.0);
    }

    // ── p50 latency, small fixture: NOT the controlled comparison (see the msz9i sibling) ──

    /**
     * Reported for completeness on this fixture, but this is NOT the controlled
     * before/after comparison the bead's acceptance criteria asks for: {@link
     * #plainSearch384} uses the typed jOOQ table-function call (native {@code
     * Vector} binding) while {@link #chunkLiveOwnersFilteredKnn} is raw SQL
     * with a text-literal vector cast -- two different serialization paths, an
     * uncontrolled confound (T2 nexus/review-wbfpw9-code, Important-1). The
     * genuinely controlled comparison (same raw-SQL harness, same text-literal
     * binding, only the predicate differs) is
     * ChunkLiveOwnersMsz9iScaleIntegrationTest's own latency sweep.
     */
    @Test
    void latency_p50_beforeAndAfter_reported_uncontrolledBindingCaveat() throws Exception {
        for (String q : queries) {
            float[] vec = embedQuery(q);
            plainSearch384(vec, K);
            chunkLiveOwnersFilteredKnn(COLLECTION, vec, K);
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
                chunkLiveOwnersFilteredKnn(COLLECTION, vec, K);
                afterMs.add((System.nanoTime() - t1) / 1_000_000L);
            }
        }

        long p50Before = p50(beforeMs);
        long p50After = p50(afterMs);

        System.out.println("[nexus-wbfpw.9 LATENCY, small fixture, UNCONTROLLED BINDING] samples="
            + beforeMs.size() + " plain_search_384(native-binding).p50=" + p50Before
            + "ms chunk_live_owners(text-literal-binding).p50=" + p50After + "ms"
            + " (fixture: " + (LIVE_COUNT + QUERY_COUNT) + " chunks, one collection)");

        // No hard bound asserted here -- evidence for the close note, not a regression
        // gate. The confound-free number is the msz9i sibling's own sweep.
        assertThat(p50Before).isGreaterThanOrEqualTo(0);
        assertThat(p50After).isGreaterThanOrEqualTo(0);
    }
}
