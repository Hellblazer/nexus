// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.Record;
import org.jooq.Result;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-156 P5.1 (bead nexus-70r3c.17) — TDD-RED contract suite for the NOT-YET-EXISTING
 * server-side RRF-fusion functions {@code nexus.hybrid_search_384/768/1024} (Decision 3,
 * Finding 4/5a). P5.2 (next bead) implements the function bodies against this suite
 * WITHOUT changing it — this class IS the locked contract.
 *
 * <p><strong>Every test below fails today.</strong> The guard test
 * {@link #guard_hybridSearchFunctionExists_perDim} is the unmistakable RED signal (a
 * clean pg_proc absence assertion, not a stack trace); every behavioral test additionally
 * fails at runtime with a real {@code SQLSTATE 42883 function ... does not exist} because
 * it genuinely calls {@code nexus.hybrid_search_<dim>(...)} — that is by design, not a
 * fixture defect (the fixture-only guard {@link #guard_fixtureLoadedCorrectly} proves the
 * harness itself is sound independent of the missing function).
 *
 * <p><strong>Lineage.</strong> The bead's original instruction ("extend
 * DualRunHarnessIntegrationTest / HybridParityIntegrationTest") targets two classes
 * DELETED at 7bcf29c67 (Chroma-dependent, RDR-155 P4b). This suite is modeled on
 * {@link CombinedQueryParityIntegrationTest} (pgvector-only function-vs-app-stitch shape)
 * for its fixture/RLS/Liquibase boilerplate, and borrows the DELETED DualRun harness's
 * seeded-corpus numbers verbatim (seed {@code 20260609L}, 200 docs, 20 queries, k=10,
 * recall floors, p95 methodology) per the bead's explicit instruction to carry the
 * NUMBERS forward without resurrecting the Chroma-era code.
 *
 * <p><strong>The parity baseline is the SHIPPED Java-side fusion</strong>
 * ({@link PgVectorRepository#hybridSearch}, bead nexus-lcogi/nexus-eap5l — "the lcogi
 * path"): a hard text gate ({@code chunk_tsv @@ plainto_tsquery('english',?) OR ?
 * <% chunk_text}, {@code pg_trgm.word_similarity_threshold = 0.6}) followed by an exact
 * cosine-distance sort, with a selectivity-aware dispatch (materialize-then-rank below
 * {@code SELECTIVE_GATE_MAX}, HNSW-first above it). P5's go/no-go (RDR-156 §Approach
 * P5 annotation, 2026-08-18 cross-walk) is to MEASURE the round-trip win of a unified
 * server-side RRF fusion against this already-shipped fix, not merely to prove the new
 * function "works" — hence this suite reports BOTH paths' p95, not just the function's.
 *
 * <h2>Contract this suite DEFINES (binding on P5.2)</h2>
 * <ul>
 *   <li><strong>Signature (per dim, {@code <dim>} ∈ {384,768,1024}):</strong>
 *       {@code nexus.hybrid_search_<dim>(p_query vector(<dim>), p_query_text text,
 *       p_collections text[], p_where jsonb, p_n int) RETURNS TABLE(id text, content
 *       text, collection text, score float8)}. {@code id} is the chunk chash (hex) —
 *       chunk-level retrieval, same identity convention as {@code search_topic_scoped_
 *       <dim>} (catalog-006/vectors-005), NOT the document tumbler
 *       {@code search_metadata_scoped_<dim>} returns. The query vector is a typed
 *       function ARGUMENT (never join-sourced) — Research Finding 5a: literal/parameter
 *       binding lets the HNSW index engage (~2ms Index Scan); a join-sourced probe
 *       vector forces a Seq Scan (~340ms) because the planner cannot fold a subquery
 *       result into the index-scan cost model.</li>
 *   <li><strong>{@code score}, not {@code distance} — DELIBERATE naming break from the
 *       sibling combined-query functions.</strong> RRF fusion combines a vector-distance
 *       RANK and a text-relevance RANK via {@code 1/(k+rank)}, summed; the result is
 *       higher-is-better and orders DESC. Every sibling function ({@code
 *       search_metadata_scoped_*}, {@code search_topic_scoped_*}, {@code
 *       search_graph_hop_*}) returns a raw ascending cosine {@code distance}. Reusing
 *       that column name for a DESC-ordered fused score would silently invert every
 *       existing distance-consumer's assumption if the two were ever conflated by a
 *       generic row-mapper. P5.2 must adapt {@code PgVectorRepository}'s row-hydration
 *       for this one function rather than coerce the score into a fake "distance".</li>
 *   <li><strong>Text-gated candidate set (continuity with the lcogi invariant).</strong>
 *       A row with NO text signal ({@code chunk_tsv @@ plainto_tsquery} nor
 *       {@code word_similarity >= pg_trgm.word_similarity_threshold}) never appears,
 *       however close its vector — "no silent vector fallback" carries over from
 *       {@link PgVectorRepository#hybridSearch}'s javadoc. RRF fuses the RANKS of
 *       vector-distance and text-relevance WITHIN that gated candidate set; it does not
 *       widen eligibility beyond the gate. This is why function-vs-lcogi parity is
 *       measured as SET recall (this suite), not exact order equality — same eligible
 *       set, different internal ranking formula.</li>
 *   <li><strong>{@code pg_trgm.word_similarity_threshold} is CALLER-set, exactly like
 *       the lcogi path.</strong> {@code hybrid_search_<dim>} is (like its catalog-006/
 *       vectors-005 siblings) a plannable {@code LANGUAGE sql} function — it cannot
 *       {@code SET LOCAL} its own GUC. The caller (repository / this test) pins
 *       {@code pg_trgm.word_similarity_threshold} transaction-locally via
 *       {@link PgSession#setLocal} BEFORE calling the function, identically to
 *       {@link PgVectorRepository#hybridSearch}'s own {@code SET LOCAL}. {@link
 *       #hybridSearch_wordSimilarityCalibration_matchesLcogiSixtyThreshold} pins this at
 *       0.6 (the lcogi calibration) and cross-checks a looser bound.</li>
 *   <li><strong>Tombstone-filtered.</strong> A chunk belonging ONLY to tombstoned
 *       documents is excluded, matching the {@code live_chunks} predicate convention
 *       (RDR-156 Decision 6) every chunk-level function in this schema applies inline
 *       (never a JOIN to the {@code live_chunks} view — that would drop the HNSW/GIN
 *       index binds on the base table, see {@code PgVectorRepository.liveChunksPredicate}
 *       javadoc). A manifest-less note chunk (no catalog row at all) stays live.</li>
 *   <li><strong>RLS/tenant isolation.</strong> {@code SECURITY INVOKER} +
 *       {@code FORCE ROW LEVEL SECURITY} on {@code nexus.chunks} — a caller scoped to
 *       tenant A via {@code nexus.tenant} never sees tenant B's rows, same envelope as
 *       every other search path in this schema.</li>
 * </ul>
 *
 * <h2>What this suite can and cannot prove (scale boundary, same honesty as
 * {@code CombinedQueryParityIntegrationTest} / {@code HybridSelectiveGateTest})</h2>
 * At fixture scale (a few hundred rows) pgvector's planner routinely seq-scans; the
 * EXPLAIN test below forces index paths via {@code enable_seqscan = off} to prove the
 * STRUCTURAL claim that the function inlines (not defeated by a Function-Scan boundary,
 * so the calling query's other index scans survive the call). It does NOT require the
 * plan to reach {@code idx_chunks_embedding_<dim>} — amended 2026-08-18 (orchestrator
 * completion review): for a SELECTIVE gate, this function's whole reason to exist
 * (RDR-156 Decision 3, the lcogi regression class), never touching the HNSW index is the
 * CORRECT shape, matching {@code HybridSelectiveGateTest}'s established precedent for the
 * Java-side selective-gate plan — see
 * {@link #explain_hybridSearchInlines_selectiveGateExactPlanNoHnsw}'s own comment for the
 * full reasoning and the p95 cost that motivated dropping the earlier always-paid ANN
 * reachability probe. This suite does not reproduce the >20k-row starvation dynamics
 * nexus-lcogi fixed. That production-scale verification is P5.G's job (RDR-156
 * §Approach), against nexus-lcogi's already-shipped fix as the bar to beat, not this
 * suite's.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HybridSearchFunctionParityIntegrationTest {

    private static final String TENANT_A = "hsparity-tenant-a";
    private static final String TENANT_B = "hsparity-tenant-b";

    private static final String COL_MAIN   = "knowledge__hsparity__minilm-l6-v2-384__v1";
    private static final String COL_NARROW = "knowledge__hsnarrow__minilm-l6-v2-384__v1";
    private static final String COL_CALIB  = "knowledge__hscalib__minilm-l6-v2-384__v1";
    private static final int DIM = 384;

    private static final String TOMB_TUMBLER = "hsp-tomb";

    // Engine-side defaults; conexus xr7.8.9-style callers override via -D system
    // properties. Namespace is NEW (nx.hybridparity.*) — nothing in-tree reads
    // nx.dualrun.* any more (that harness is deleted); mirrors nx.cqparity.* style.
    private static final int    CORPUS_SIZE = Integer.getInteger("nx.hybridparity.size", 200);
    private static final int    QUERY_COUNT = Integer.getInteger("nx.hybridparity.queries", 20);
    private static final int    K           = Integer.getInteger("nx.hybridparity.k", 10);
    private static final double RECALL_MIN  =
        Double.parseDouble(System.getProperty("nx.hybridparity.recall.min", "1.0"));
    private static final double RECALL_QUERY_MIN =
        Double.parseDouble(System.getProperty("nx.hybridparity.recall.query.min", "0.5"));
    private static final long   P95_BOUND_MS =
        Long.getLong("nx.hybridparity.p95.ms", 250L);

    /** Word bank carried verbatim from the deleted DualRunHarnessIntegrationTest
     *  (git show 7bcf29c67^: same file) — distinct stems, exact-form usage, so the
     *  english-stemmer FTS candidate set for any query equals plain word containment. */
    private static final List<String> WORD_BANK = List.of(
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
        "yankee", "zulu", "cobalt", "quartz", "falcon", "harbor", "lantern",
        "marble", "nickel", "orchid", "pylon", "quiver", "raven", "saddle",
        "timber", "vortex", "willow");

    // Narrow-collection (Decision 5 "==N" regime) fixture: total corpus smaller than
    // any plausible hnsw.ef_search, a rare token selecting an EXACT known subset.
    private static final int NARROW_TOTAL = 12;
    private static final int NARROW_MATCHES = 3;
    private static final String NARROW_TOKEN = "znarrowtok";

    // Trigram-calibration fixture (word_similarity values verified against a live
    // pgvector/pgvector:pg17 + pg_trgm probe before authoring this suite — see the
    // bead's RED-authoring notes; NOT guessed):
    //   "quartz falcn"  vs CALIB_TEXT -> word_similarity ~0.846 (passes 0.6)
    //   "qrtz falcn"    vs CALIB_TEXT -> word_similarity ~0.545 (fails 0.6, passes 0.5)
    //   neither has a real FTS lexeme match against CALIB_TEXT (ts_rank ~1e-20) --
    //   pure trigram-leg probes, isolated from the FTS leg exactly like the existing
    //   PgVectorHybridSearchContractTest Q_TYPO design.
    private static final String CALIB_TEXT =
        "a rare harbor lantern quartz falcon marble craft";
    private static final String CALIB_PASS_AT_060 = "quartz falcn";
    private static final String CALIB_FAILS_AT_060_PASSES_AT_050 = "qrtz falcn";
    private static final String CALIB_JUNK = "zzqq xkcd glorp";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    OnnxEmbedder onnx;
    EmbedderRouter docRouter;
    EmbedderRouter queryRouter;
    PgVectorRepository pgRepo;

    /** Main corpus: tumbler -> text (insertion-ordered, deterministic). */
    final Map<String, String> corpus = new LinkedHashMap<>();
    final Map<String, String> corpusChash = new LinkedHashMap<>();
    final List<String> queries = new ArrayList<>();
    String tombChash;

    final List<String> narrowChashes = new ArrayList<>();
    final Set<String> narrowMatchChashes = new LinkedHashSet<>();

    String calibChash;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') "
                + "THEN CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; "
                + "END IF; END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase lb = new Liquibase("db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                lb.update(new Contexts());
            }
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("ALTER ROLE nexus_svc SET search_path TO nexus, public");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(6);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        onnx        = new OnnxEmbedder();
        docRouter   = new EmbedderRouter(onnx, "document");
        queryRouter = new EmbedderRouter(onnx, "query");
        pgRepo      = new PgVectorRepository(tenantScope, docRouter, queryRouter);

        seedMainCorpusAndTombstone();
        seedNarrowCollection();
        seedCalibrationCollection();
        seedCrossTenantProbe();
    }

    @AfterAll
    void stopAll() {
        if (onnx  != null) onnx.close();
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    // ── fixtures ─────────────────────────────────────────────────────────────

    /** Seeded generation carried verbatim from the deleted DualRunHarnessIntegrationTest
     *  (seed 20260609L, 8-12 bank words + a unique discriminator per doc, query = first
     *  two words of every 7th document mod corpus size). */
    private void seedMainCorpusAndTombstone() throws Exception {
        Random rnd = new Random(20260609L);
        for (int d = 0; d < CORPUS_SIZE; d++) {
            int len = 8 + rnd.nextInt(5);
            Set<String> words = new LinkedHashSet<>();
            while (words.size() < len) words.add(WORD_BANK.get(rnd.nextInt(WORD_BANK.size())));
            String id = String.format("hsp-doc-%05d", d);
            corpus.put(id, String.join(" ", words) + " doc" + d);
        }
        List<String> docTexts = new ArrayList<>(corpus.values());
        for (int q = 0; q < QUERY_COUNT; q++) {
            String[] w = docTexts.get((q * 7) % docTexts.size()).split(" ");
            queries.add(w[0] + " " + w[1]);
        }

        for (var e : corpus.entrySet()) {
            corpusChash.put(e.getKey(), Chash.ofText(e.getKey()).toHex());
        }
        // Tombstone probe: text == first query, so it is a top vector AND text match --
        // both hybrid_search and the lcogi baseline must exclude it (deleted_at guard).
        tombChash = Chash.ofText(TOMB_TUMBLER).toHex();
        corpus.put(TOMB_TUMBLER, queries.get(0) + " tombstoned-probe-only");
        corpusChash.put(TOMB_TUMBLER, tombChash);

        List<String> ids   = new ArrayList<>(corpus.keySet());
        List<String> texts = new ArrayList<>(corpus.values());
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < ids.size(); i++) metas.add(Map.of());
        pgRepo.upsertChunks(TENANT_A, COL_MAIN, ids.stream().map(corpusChash::get).toList(), texts, metas);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String id : ids) {
                boolean tombstoned = id.equals(TOMB_TUMBLER);
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_documents "
                    + "(tenant_id, tumbler, title, author, content_type, physical_collection, deleted_at) "
                    + "VALUES ('" + TENANT_A + "', '" + id + "', 'Doc', 'ada', 'paper', '" + COL_MAIN + "', "
                    + (tombstoned ? "now()" : "NULL") + ") ON CONFLICT (tenant_id, tumbler) DO NOTHING");
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_document_chunks "
                    + "(tenant_id, doc_id, position, chash, collection) "
                    + "VALUES ('" + TENANT_A + "', '" + id + "', 0, decode('" + corpusChash.get(id)
                    + "', 'hex'), '" + COL_MAIN + "') ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
            }
        }
    }

    /** Decision 5 "==N" regime: a small collection where a rare token selects an EXACT,
     *  known-in-advance subset — regardless of whether HNSW physically engages at this
     *  scale, the assertion is exact-count, not a >= floor. */
    private void seedNarrowCollection() throws Exception {
        List<String> ids = new ArrayList<>();
        List<String> texts = new ArrayList<>();
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < NARROW_TOTAL; i++) {
            String chash = Chash.ofText("hsn-" + i).toHex();
            boolean matches = i < NARROW_MATCHES;
            String text = matches
                ? (NARROW_TOKEN + " selective narrow-collection target " + i)
                : ("plain filler document number " + i + " alpha bravo charlie delta");
            narrowChashes.add(chash);
            if (matches) narrowMatchChashes.add(chash);
            ids.add(chash);
            texts.add(text);
            metas.add(Map.of());
        }
        pgRepo.upsertChunks(TENANT_A, COL_NARROW, ids, texts, metas);
    }

    private void seedCalibrationCollection() throws Exception {
        calibChash = Chash.ofText("hsp-calib-1").toHex();
        pgRepo.upsertChunks(TENANT_A, COL_CALIB,
            List.of(calibChash), List.of(CALIB_TEXT), List.of(Map.of()));
    }

    /** Second tenant, distinct chash, same collection name -- the (tenant_id, collection,
     *  chash) key means this coexists cleanly with TENANT_A's rows. */
    private void seedCrossTenantProbe() throws Exception {
        String chash = Chash.ofText("hsp-tenantb-1").toHex();
        pgRepo.upsertChunks(TENANT_B, COL_MAIN, List.of(chash),
            List.of(queries.get(0) + " tenant-b-only-row"), List.of(Map.of()));
    }

    // ── raw function-call plumbing (the function does not exist yet; every call
    //    below is expected to throw until P5.2 lands) ──────────────────────────

    private float[] embedQuery(String collection, String text) {
        return queryRouter.embedOneForCollection(collection, text);
    }

    /** pgvector cast-safe text literal: {@code [f1,f2,...]} (copy of
     *  PgVectorRepository's private helper -- kept local since the production method
     *  is private and this suite must stay a pure external-contract caller). */
    private static String vectorLiteral(float[] vec) {
        StringBuilder sb = new StringBuilder(vec.length * 8 + 2).append('[');
        for (int i = 0; i < vec.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vec[i]);
        }
        return sb.append(']').toString();
    }

    private static String placeholders(int n) {
        return String.join(",", java.util.Collections.nCopies(n, "?"));
    }

    /**
     * Calls {@code nexus.hybrid_search_<dim>(p_query, p_query_text, p_collections,
     * p_where, p_n)} under the given tenant, with {@code pg_trgm.word_similarity_threshold}
     * pinned exactly like {@link PgVectorRepository#hybridSearch} does. Returns rows in
     * the contract shape ({@code id, content, collection, score}).
     *
     * @throws org.jooq.exception.DataAccessException today, always -- the function does
     *         not exist. That is the RED signal every behavioral test below relies on.
     */
    private List<Map<String, Object>> callHybridSearch(String tenant, int dim,
            String queryText, List<String> collections, double trgmThreshold, int n) {
        float[] vec = embedQuery(collections.get(0), queryText);
        String sql = "SELECT id, content, collection, score FROM nexus.hybrid_search_" + dim
            + "(?::vector, ?, ARRAY[" + placeholders(collections.size()) + "]::text[], NULL::jsonb, ?)";
        List<Object> binds = new ArrayList<>();
        binds.add(vectorLiteral(vec));
        binds.add(queryText);
        binds.addAll(collections);
        binds.add(n);
        return tenantScope.withTenant(tenant, ctx -> {
            PgSession.setLocal(ctx, "pg_trgm.word_similarity_threshold",
                Double.toString(trgmThreshold));
            Result<Record> result = ctx.fetch(sql, binds.toArray());
            List<Map<String, Object>> rows = new ArrayList<>(result.size());
            for (Record rec : result) {
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("id",         rec.get("id", String.class));
                row.put("content",    rec.get("content", String.class));
                row.put("collection", rec.get("collection", String.class));
                row.put("score",      rec.get("score", Double.class));
                rows.add(row);
            }
            return rows;
        });
    }

    private List<Map<String, Object>> callHybridSearch(String tenant, int dim,
            String queryText, List<String> collections, int n) {
        return callHybridSearch(tenant, dim, queryText, collections, 0.6, n);
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    /** EXPLAIN with seqscan disabled so index access paths are chosen at fixture scale
     *  (mirrors {@code HybridSelectiveGateTest#explain}). */
    private String explain(String inner) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(false);
            su.createStatement().execute("SET LOCAL enable_seqscan = off");
            su.createStatement().execute("SELECT set_config('nexus.tenant', '" + TENANT_A + "', true)");
            su.createStatement().execute("SELECT set_config('pg_trgm.word_similarity_threshold', '0.6', true)");
            StringBuilder sb = new StringBuilder();
            try (ResultSet rs = su.createStatement().executeQuery("EXPLAIN " + inner)) {
                while (rs.next()) sb.append(rs.getString(1)).append('\n');
            }
            su.rollback();
            return sb.toString();
        }
    }

    /** Exact text-candidate oracle: corpus tumblers whose text contains EVERY query
     *  word (carried from the deleted DualRunHarnessIntegrationTest). */
    private Set<String> textCandidateChashes(String query) {
        String[] terms = query.split(" ");
        Set<String> out = new LinkedHashSet<>();
        for (var e : corpus.entrySet()) {
            if (e.getKey().equals(TOMB_TUMBLER)) continue;   // live-only oracle
            Set<String> words = Set.of(e.getValue().split(" "));
            boolean all = true;
            for (String t : terms) if (!words.contains(t)) { all = false; break; }
            if (all) out.add(corpusChash.get(e.getKey()));
        }
        return out;
    }

    // ════════════════════════════════════════════════════════════════════════
    // Guards
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void guard_fixtureLoadedCorrectly() throws Exception {
        // Must be GREEN independent of hybrid_search's existence -- proves a RED
        // elsewhere in this suite is about the missing function, not a fixture bug.
        assertThat(QUERY_COUNT).isGreaterThanOrEqualTo(1);
        assertThat(pgRepo.count(TENANT_A, COL_MAIN))
            .as("main corpus + tombstone probe loaded").isEqualTo(CORPUS_SIZE + 1);
        assertThat(pgRepo.count(TENANT_A, COL_NARROW)).isEqualTo(NARROW_TOTAL);
        assertThat(pgRepo.count(TENANT_A, COL_CALIB)).isEqualTo(1);
        assertThat(narrowMatchChashes).hasSize(NARROW_MATCHES);
        assertThat(queries).hasSize(QUERY_COUNT);
        for (String q : queries) {
            assertThat(textCandidateChashes(q))
                .as("every probe query must have >=1 text candidate (construction guarantees it)")
                .isNotEmpty();
        }
        // Precondition: the tombstoned probe row is physically present with EXACT text
        // equal to queries.get(0) (so it would be an unbeatable vector+text match if
        // not filtered). Checked via a RAW row read that bypasses live_chunks entirely
        // -- both PgVectorRepository#search AND #hybridSearch now inline the SAME
        // live_chunks predicate (RDR-156 Decision 6 / nexus-3ck2g, nexus-msz9i), so
        // neither repo method can serve as a pre-filter precondition any more (an
        // older sibling suite's precondition idiom of calling plain search() predates
        // that fold-in and would make this precondition vacuously pass either way).
        try (Connection su = pg.createConnection("")) {
            try (var rs = su.createStatement().executeQuery(
                    "SELECT chunk_text FROM nexus.chunks WHERE tenant_id = '" + TENANT_A
                    + "' AND collection = '" + COL_MAIN + "' AND chash = decode('" + tombChash + "', 'hex')")) {
                assertThat(rs.next())
                    .as("precondition: tombstoned chunk row must physically exist pre-filter")
                    .isTrue();
                assertThat(rs.getString(1))
                    .as("precondition: tombstoned chunk text must equal queries.get(0) "
                        + "exactly, so it is an unbeatable vector+text match if not filtered")
                    .isEqualTo(queries.get(0) + " tombstoned-probe-only");
            }
        }
    }

    @Test
    void guard_hybridSearchFunctionExists_perDim() throws Exception {
        try (Connection su = pg.createConnection("")) {
            List<String> found = new ArrayList<>();
            try (var rs = su.createStatement().executeQuery(
                    "SELECT p.proname FROM pg_proc p "
                    + "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    + "WHERE n.nspname = 'nexus' AND p.proname LIKE 'hybrid_search_%' "
                    + "ORDER BY p.proname")) {
                while (rs.next()) found.add(rs.getString(1));
            }
            assertThat(found)
                .as("nexus.hybrid_search_384/768/1024 must exist (RDR-156 P5.2 not yet "
                    + "landed -- this is the expected P5.1 TDD-RED failure, not a bug in "
                    + "this suite). Found instead: %s", found)
                .containsExactlyInAnyOrder(
                    "hybrid_search_384", "hybrid_search_768", "hybrid_search_1024");
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // Structural: the function inlines; the selective-gate plan never touches HNSW
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void explain_hybridSearchInlines_selectiveGateExactPlanNoHnsw() throws Exception {
        // AMENDED 2026-08-18 (orchestrator completion review, T1/nexus-70r3c.18 handoff):
        // the original P5.1 version of this test additionally required
        // idx_chunks_embedding_<dim> to appear in the plan ("HNSW reachable"). P5.2's first
        // implementation satisfied that by carrying an extra, deliberately UNFILTERED
        // top-5000-row ANN probe purely to touch the index -- the probe never fed the RRF
        // score (tie-break only) and measurably cost roughly HALF of hybrid_search_384's
        // total p95 (see hybridSearch_p95Latency_functionUnderBound_bothPathsReported's
        // reported numbers, before/after removal, in that test's own comment) for zero
        // functional benefit in the overwhelming common case.
        //
        // Removed. For a SELECTIVE gate -- which is what this function exists to fix
        // (RDR-156 Decision 3, the lcogi regression class this bead names explicitly) --
        // NEVER touching the HNSW index is the CORRECT shape, not a compromise:
        // HybridSelectiveGateTest already establishes exactly this precedent for the
        // Java-side selective-gate plan (that class's own javadoc: the fix's whole point
        // is that the HNSW index is "never touched" for a selective gate -- ranking a
        // small, already-materialized candidate set by exact distance has no reason to
        // consult an approximate-nearest-neighbor index at all). Finding 5a (HNSW engages
        // when the probe vector is a plan-time argument) remains proven end-to-end by the
        // sibling combined-query functions (search_metadata_scoped_<dim> etc.,
        // catalog-006/vectors-005), which exercise that exact shape on every call via
        // their own un-gated `ORDER BY <=> LIMIT` -- it did not need a second, redundant,
        // always-paid proof point riding inside hybrid_search's own hot path.
        //
        // What is still asserted, and still load-bearing: hybrid_search_<dim> must be
        // plannable/INLINABLE (no 'Function Scan' node) so the calling query's other index
        // scans (the GIN text-gate indexes) survive the call -- that half of the original
        // contract is unchanged.
        String q = queries.get(0);
        float[] vec = embedQuery(COL_MAIN, q);
        String sql = "SELECT * FROM nexus.hybrid_search_" + DIM + "('" + vectorLiteral(vec)
            + "'::vector, '" + q + "', ARRAY['" + COL_MAIN + "']::text[], NULL::jsonb, " + K + ")";
        String plan = explain(sql);
        assertThat(plan)
            .as("hybrid_search_%d must be plannable/INLINABLE (LANGUAGE sql, STABLE, "
                + "SECURITY INVOKER, not STRICT -- catalog-006 discipline) so the calling "
                + "query's other index scans survive the call; a 'Function Scan' node means "
                + "the planner boxed it opaquely. Plan was:%n%s", DIM, plan)
            .doesNotContain("Function Scan");
        assertThat(plan)
            .as("hybrid_search_%d's exact-over-gate vector rank must NOT touch the "
                + "embedding_%d HNSW index (idx_chunks_embedding_%d) -- for a SELECTIVE "
                + "gate (this function's whole reason to exist, RDR-156 Decision 3) that is "
                + "the CORRECT plan shape, matching HybridSelectiveGateTest's established "
                + "precedent for the Java-side selective-gate plan. Plan was:%n%s",
                DIM, DIM, DIM, plan)
            .doesNotContain("idx_chunks_embedding_" + DIM);
    }

    // ════════════════════════════════════════════════════════════════════════
    // Parity: function vs the shipped Java lcogi fusion (dense regime -- the
    // 200-doc/40-word-bank corpus gives every query gate dozens of matches, well
    // above SELECTIVE_GATE_MAX territory in relative terms for this fixture).
    //
    // ORCHESTRATOR RULING (2026-08-18, RDR-156 P5.2 completion review, T1/nexus-70r3c.18
    // handoff) -- REPLACES the original P5.1 exact-set-recall-vs-K assertion below with
    // three narrower, individually-defensible checks, after P5.2's implementation review
    // found TWO independent problems with the original design:
    //
    //   1. DENOMINATOR BUG. The original per-query/aggregate recall divided overlap by a
    //      FIXED K=10, but 3/20 seeded queries ("marble romeo", "zulu nickel", "willow
    //      bravo") have a natural lcogi gate size BELOW K (8, 6, 8 matches respectively --
    //      verified via a temporary, fully-reverted debug instrumentation pass during
    //      P5.2). Dividing by a fixed K makes recall=1.0 for those 3 queries mathematically
    //      UNREACHABLE by ANY implementation, correct or not -- even lcogi compared against
    //      itself could not clear a fixed-K aggregate floor of 1.0 with these three queries
    //      in the mix. An assertion unachievable by construction is a test defect, not an
    //      implementation gap.
    //   2. WRONG COMPARISON SHAPE. Exact-set-recall-vs-lcogi-top-K treats RRF's re-ordering
    //      of the SAME gated candidate set as a defect to be minimized toward zero. That is
    //      backwards: RDR-156 Decision 3 deliberately fuses THREE independent per-leg ranks
    //      (vector / tsvector / trigram) specifically so ts_rank_cd/word_similarity signals
    //      can outrank a purely-closer vector match within the gate -- reordering IS the
    //      feature the function was built to deliver, not noise to suppress. Proof (same
    //      debug pass): scoring hybrid_search_384 by the VECTOR LEG ALONE (degenerately
    //      reproducing lcogi's own ranking criterion) returns a candidate SET with ZERO
    //      difference from lcogi's own top-K on all 20 queries -- proving the gate
    //      predicate and the vector-rank computation are exactly correct. Restoring the
    //      full 3-leg RRF measurably re-orders the SAME correct candidates and legitimately
    //      drops exact-set-recall to ~0.80-0.84 (one per-query floor violation observed at
    //      4/10 for "cobalt quebec" under the ORIGINAL 0.5 floor). RECALL_MIN=1.0 /
    //      RECALL_QUERY_MIN=0.5 were carried forward VERBATIM from the deleted DualRun
    //      harness (see class javadoc), which measured a different comparison entirely and
    //      could never have been empirically validated against THIS specific
    //      3-leg-RRF-vs-lcogi comparison: P5.1 is TDD-RED by construction, so the function
    //      did not exist when those thresholds were chosen.
    //
    // The replacement (three tests below) asks three narrower, independently-defensible
    // questions instead of one over-broad "is the top-K set roughly the same" question:
    //   (a) hybridSearch_denseRegime_gateContainment       -- soundness: every function
    //       result is inside the SAME candidate set lcogi computes (never outside it).
    //   (b) hybridSearch_denseRegime_selectiveSubcaseExactMatch -- for the sub-population
    //       of queries whose gate is <= K (both paths must return the WHOLE gate, so
    //       reordering cannot matter -- this is exactly the Decision-5 "==N" regime,
    //       verified in miniature by hybridSearch_narrowCollection_exactRecallEqualsN
    //       already; this asserts it also holds for the naturally-occurring small-gate
    //       queries inside the dense-regime fixture).
    //   (c) hybridSearch_denseRegime_oracleQualityNonRegression -- the metric that
    //       actually matters for a fusion algorithm: retrieval QUALITY against a
    //       deterministic ground truth (textCandidateChashes -- literal every-query-word
    //       containment, the DualRun oracle design), not agreement with one particular
    //       OTHER algorithm's internal ranking choice. Thresholds pinned from an empirical
    //       measurement pass (see that test's javadoc for the actual numbers and margin).
    // A REPORTED (never asserted) function-vs-java top-K set-overlap number is kept in (c)
    // for P5.G, matching the p95 test's report-both-numbers convention.
    // ════════════════════════════════════════════════════════════════════════

    /**
     * Full (unbounded-by-K) lcogi candidate set for a query against {@code COL_MAIN} --
     * {@code n=CORPUS_SIZE} is a safe upper bound since the live population of COL_MAIN
     * is exactly {@code CORPUS_SIZE} (the tombstone probe is excluded by the live_chunks
     * guard both paths share). This is the REAL SQL text gate (FTS-OR-trgm), not the
     * deterministic {@link #textCandidateChashes} oracle -- the two coincide for most
     * queries in this fixture (the word bank's distinct-stem design makes the FTS gate
     * equal literal containment, per the class's WORD_BANK javadoc) but are not
     * definitionally identical (trigram similarity can admit a fuzzy match the literal
     * oracle would not), so they are kept as two distinct concepts on purpose.
     */
    private Set<String> fetchFullJavaGate(String tenant, String q) {
        List<Map<String, Object>> rows =
            pgRepo.hybridSearch(tenant, q, List.of(COL_MAIN), CORPUS_SIZE, null);
        return new LinkedHashSet<>(ids(rows));
    }

    @Test
    void hybridSearch_denseRegime_gateContainment() {
        // (a) Soundness: hybrid_search_384's top-K is always a SUBSET of the full lcogi
        // gate for the same query -- RRF fuses ranks WITHIN the gate (class javadoc "Text-
        // gated candidate set"), it never admits a row the gate itself would exclude.
        for (String q : queries) {
            Set<String> fullGate = fetchFullJavaGate(TENANT_A, q);
            assertThat(fullGate)
                .as("non-vacuity: the lcogi gate must be non-empty for '%s' (queries are "
                    + "built from corpus words) so gate containment is meaningful", q)
                .isNotEmpty();

            Set<String> fnTop = new LinkedHashSet<>(
                ids(callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), K)));
            assertThat(fnTop)
                .as("hybrid_search_%d row set for '%s' must be a SUBSET of the full lcogi "
                    + "text gate %s -- a row outside the gate would mean RRF silently "
                    + "widened eligibility past the text-gate contract", DIM, q, fullGate)
                .isSubsetOf(fullGate);
        }
    }

    @Test
    void hybridSearch_denseRegime_selectiveSubcaseExactMatch() {
        // (b) For the naturally-occurring small-gate queries in this fixture (gate <= K --
        // "marble romeo"=8, "zulu nickel"=6, "willow bravo"=8, verified via the P5.2 debug
        // pass referenced above), BOTH paths must return the WHOLE gate: with fewer
        // candidates than K, reordering cannot drop anything, so set-equality is not just
        // achievable but REQUIRED regardless of which ranking formula is used.
        int selectiveQueries = 0;
        for (String q : queries) {
            Set<String> fullGate = fetchFullJavaGate(TENANT_A, q);
            if (fullGate.size() > K) continue;
            selectiveQueries++;

            Set<String> javaTop = new LinkedHashSet<>(
                ids(pgRepo.hybridSearch(TENANT_A, q, List.of(COL_MAIN), K, null)));
            Set<String> fnTop = new LinkedHashSet<>(
                ids(callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), K)));

            assertThat(fnTop)
                .as("selective sub-case (gate size %d <= K=%d) for '%s': hybrid_search_%d "
                    + "top-K must SET-EQUAL the lcogi top-K -- both must return the WHOLE "
                    + "gate, so a mismatch here cannot be attributed to RRF re-ordering",
                    fullGate.size(), K, q, DIM)
                .isEqualTo(javaTop);
        }
        assertThat(selectiveQueries)
            .as("this fixture is known (P5.2 debug pass) to contain >=1 naturally-selective "
                + "query (gate <= K) -- if this ever hits 0, the fixture's random seed or "
                + "corpus size changed and this sub-case test has gone vacuous")
            .isGreaterThanOrEqualTo(1);
    }

    @Test
    void hybridSearch_denseRegime_oracleQualityNonRegression() {
        // (c) The metric that actually matters for a fusion algorithm: retrieval quality
        // against a DETERMINISTIC ground truth (textCandidateChashes -- literal
        // every-query-word containment, the DualRun oracle design), not agreement with one
        // particular OTHER algorithm's internal ranking choice (that was the original,
        // now-replaced, exact-set-recall-vs-lcogi assertion -- see the parity-test-group
        // javadoc above for why that comparison was the wrong shape).
        //
        // Per-query oracle-recall@K = |topK ∩ oracle| / min(K, |oracle|) -- same
        // min(K, denominator) fix as the containment/selective tests above, so a
        // naturally-small oracle can never make perfect recall mathematically unreachable.
        //
        // THRESHOLDS PINNED FROM AN EMPIRICAL MEASUREMENT PASS (2026-08-18, P5.2 completion
        // review, orchestrator-directed "measure first, then pin with margin" process --
        // run twice with thresholds neutralized (AGGREGATE_ORACLE_TOLERANCE=1.0,
        // PER_QUERY_ORACLE_FLOOR=0.0) to confirm the numbers are deterministic before
        // pinning -- both runs produced BYTE-IDENTICAL output, as expected: embeddings
        // (fixed ONNX model), FTS/trgm scoring, and the RRF rank computation are all
        // deterministic, so there is no JIT/warmup jitter to margin against here, only
        // headroom for a genuine future behavior change. This suite only seeds a dim=384
        // fixture, so the measurement covers all 20 queries at dim=384; 768/1024 have no
        // behavioral fixture in this suite, same scope as every other behavioral test here
        // -- guard_hybridSearchFunctionExists_perDim is the only cross-dim check.
        //
        // ACTUAL measured numbers (both runs, verbatim):
        //   aggregate: fnOracleRecall=0.9841269841269841  javaOracleRecall=0.9735449735449735
        //     (denomSum=200) -- hybrid_search_384 is NOT a quality regression vs lcogi on
        //     this fixture; its aggregate oracle-recall is HIGHER (+0.0106) than lcogi's own.
        //   worst per-query fn oracle-recall: "papa charlie" fn=8/10=0.800, java=7/10=0.700
        //     (oracle size 11, denom=min(10,11)=10) -- fn is BETTER than java on its own
        //     worst query. Across all 20 queries fn is never worse than java per-query:
        //     tied on 18, strictly better on 2 ("papa charlie" 0.8 vs 0.7, "kilo whiskey"
        //     1.0 vs 0.9). The exact-set-recall-vs-lcogi metric this test replaces showed
        //     "cobalt quebec" as its worst case (4/10, a per-query floor violation) -- under
        //     the oracle metric that SAME query scores 10/10 for BOTH paths, proving the
        //     original failure was internal ranking disagreement between two otherwise-
        //     correct algorithms, not a quality problem in either one.
        // Pinned with margin below the measured floor (not AT it, so a genuine but modest
        // future behavior change -- e.g. a Postgres version bump nudging ts_rank_cd's
        // internal tie-breaking -- does not flap this test on a fluctuation that isn't a
        // real regression):
        //   AGGREGATE_ORACLE_TOLERANCE = 0.05 (measured gap is actually +0.0106 in fn's
        //     favor; this tolerance is pure downside headroom, not chasing an observed gap)
        //   PER_QUERY_ORACLE_FLOOR     = 0.65 (measured worst case 0.800; ~0.15 margin)
        // If a future run's data shows RRF genuinely losing MORE oracle recall than this
        // (not a fluctuation but a real regression), this test is SUPPOSED to go red --
        // per the orchestrator ruling, do not loosen these floors to chase green; that is
        // a real P5.G value finding (RRF underperforming lcogi on ground-truth quality),
        // not a test-calibration problem.
        final double AGGREGATE_ORACLE_TOLERANCE = 0.05;
        final double PER_QUERY_ORACLE_FLOOR = 0.65;

        int fnOracleHitSum = 0, javaOracleHitSum = 0, denomSum = 0;
        int setOverlapSum = 0, setOverlapPossible = 0;
        List<String> perQuery = new ArrayList<>();
        String worstQuery = null;
        double worstFnRecall = Double.MAX_VALUE;

        for (String q : queries) {
            Set<String> oracle = textCandidateChashes(q);
            assertThat(oracle)
                .as("non-vacuity: the deterministic oracle must be non-empty for '%s' "
                    + "(construction guarantees it, mirrors guard_fixtureLoadedCorrectly)", q)
                .isNotEmpty();

            List<Map<String, Object>> javaRows =
                pgRepo.hybridSearch(TENANT_A, q, List.of(COL_MAIN), K, null);
            List<Map<String, Object>> fnRows =
                callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), K);
            Set<String> javaTop = new LinkedHashSet<>(ids(javaRows));
            Set<String> fnTop   = new LinkedHashSet<>(ids(fnRows));

            int denom = Math.min(K, oracle.size());
            int fnHits   = (int) fnTop.stream().filter(oracle::contains).count();
            int javaHits = (int) javaTop.stream().filter(oracle::contains).count();
            double fnRecallQ = (double) fnHits / denom;

            assertThat(fnRecallQ)
                .as("per-query oracle-recall@%d floor for '%s': hybrid_search_%d scored "
                    + "%d/%d (%.3f) against the deterministic oracle (size %d) -- must be "
                    + ">= %.2f (pinned from the 2026-08-18 measurement pass, see test "
                    + "javadoc)", K, q, DIM, fnHits, denom, fnRecallQ, oracle.size(),
                    PER_QUERY_ORACLE_FLOOR)
                .isGreaterThanOrEqualTo(PER_QUERY_ORACLE_FLOOR);

            if (fnRecallQ < worstFnRecall) { worstFnRecall = fnRecallQ; worstQuery = q; }
            fnOracleHitSum += fnHits;
            javaOracleHitSum += javaHits;
            denomSum += denom;
            perQuery.add(q + " oracle=" + oracle.size() + " fn=" + fnHits + "/" + denom
                + " java=" + javaHits + "/" + denom);

            // REPORTED ONLY (P5.G wants this number; never asserted -- see parity-test-group
            // javadoc for why exact-set-agreement-with-lcogi is the wrong thing to assert on).
            Set<String> inter = new LinkedHashSet<>(fnTop);
            inter.retainAll(javaTop);
            int setDenom = Math.min(K, javaTop.size());
            setOverlapSum += inter.size();
            setOverlapPossible += setDenom;
        }

        double fnOracleRecall   = (double) fnOracleHitSum / denomSum;
        double javaOracleRecall = (double) javaOracleHitSum / denomSum;
        double setOverlapRecall = (double) setOverlapSum / setOverlapPossible;

        System.out.println("[hybrid-search-parity P5.G] fnOracleRecall=" + fnOracleRecall
            + " javaOracleRecall=" + javaOracleRecall
            + " setOverlapRecall(fn-vs-java top-K, REPORTED ONLY, not asserted)="
            + setOverlapRecall + " worstQuery='" + worstQuery + "' worstFnRecall="
            + worstFnRecall);
        System.out.println("[hybrid-search-parity P5.G per-query] " + perQuery);

        assertThat(fnOracleRecall)
            .as("aggregate oracle-recall@%d (hybrid_search_%d vs the deterministic oracle) "
                + "over %d queries was %.4f; lcogi's own aggregate oracle-recall was %.4f -- "
                + "hybrid_search_%d must be within %.2f of lcogi's own quality (pinned from "
                + "the 2026-08-18 measurement pass, see test javadoc). Per-query: %s",
                K, DIM, queries.size(), fnOracleRecall, javaOracleRecall, DIM,
                AGGREGATE_ORACLE_TOLERANCE, perQuery)
            .isGreaterThanOrEqualTo(javaOracleRecall - AGGREGATE_ORACLE_TOLERANCE);
    }

    @Test
    void hybridSearch_resultsStayInsideTextSignalOracle_noSilentVectorFallback() {
        // Soundness half of the DualRun "inside text-signal oracle" check: every
        // returned row must share >=1 exact query word -- a row with ZERO query words
        // would mean the text gate leaked a vector-only row (RRF fusing ranks does NOT
        // license widening eligibility past the gate -- see class javadoc).
        for (String q : queries) {
            List<Map<String, Object>> rows = callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), K);
            String[] terms = q.split(" ");
            for (Map<String, Object> r : rows) {
                String content = (String) r.get("content");
                boolean anyTerm = false;
                for (String t : terms) {
                    if (content != null && content.contains(t)) { anyTerm = true; break; }
                }
                assertThat(anyTerm)
                    .as("hybrid_search_%d row %s for query '%s' contains NO query word -- "
                        + "the text gate leaked a vector-only row", DIM, r.get("id"), q)
                    .isTrue();
            }
        }
    }

    // ════════════════════════════════════════════════════════════════════════
    // Tombstone filtering (RDR-156 Decision 6)
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void hybridSearch_excludesTombstonedDoc_evenWhenTopRanked() {
        String q = queries.get(0);
        List<Map<String, Object>> rows = callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), K);
        assertThat(ids(rows))
            .as("hybrid_search_%d must EXCLUDE the tombstoned chunk despite it being an "
                + "exact text+vector match for '%s' -- proves the live_chunks-equivalent "
                + "deleted_at guard fires (RDR-156 Decision 6)", DIM, q)
            .doesNotContain(tombChash);
    }

    // ════════════════════════════════════════════════════════════════════════
    // RLS / tenant isolation
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void hybridSearch_crossTenantIsolation_neverReturnsOtherTenantRows() {
        String q = queries.get(0);
        List<Map<String, Object>> tenantARows =
            callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), CORPUS_SIZE);
        assertThat(tenantARows)
            .as("tenant A scope must never surface a tenant B row (SECURITY INVOKER + "
                + "FORCE RLS on nexus.chunks)")
            .allSatisfy(r -> assertThat((String) r.get("id")).isNotEqualTo(
                Chash.ofText("hsp-tenantb-1").toHex()));

        List<Map<String, Object>> tenantBRows =
            callHybridSearch(TENANT_B, DIM, q, List.of(COL_MAIN), CORPUS_SIZE);
        assertThat(ids(tenantBRows))
            .as("tenant B, scoped to its own single row, must see exactly that row and "
                + "nothing from tenant A's 200-doc corpus")
            .containsExactly(Chash.ofText("hsp-tenantb-1").toHex());
    }

    // ════════════════════════════════════════════════════════════════════════
    // Decision 5 "==N" regime: narrow collection, EXACT recall (not a >= floor)
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void hybridSearch_narrowCollection_exactRecallEqualsN_selectiveRegime() {
        List<Map<String, Object>> rows =
            callHybridSearch(TENANT_A, DIM, NARROW_TOKEN, List.of(COL_NARROW), NARROW_TOTAL);
        assertThat(ids(rows))
            .as("a narrow collection (%d total rows, well under any plausible "
                + "hnsw.ef_search) with a selective %d-row gate must return EXACTLY "
                + "those %d rows, not a >= threshold approximation (RDR-156 Decision 5)",
                NARROW_TOTAL, NARROW_MATCHES, NARROW_MATCHES)
            .containsExactlyInAnyOrderElementsOf(narrowMatchChashes);
        assertThat(rows).as("exact count, no filler").hasSize(NARROW_MATCHES);
    }

    // ════════════════════════════════════════════════════════════════════════
    // word_similarity 0.6 calibration cross-check against the lcogi threshold
    // ════════════════════════════════════════════════════════════════════════

    @Test
    void hybridSearch_wordSimilarityCalibration_matchesLcogiSixtyThreshold() {
        // At the lcogi calibration (0.6): the ~0.846-similarity typo passes, the
        // ~0.545-similarity typo does NOT, junk does not. Neither typo has a real FTS
        // lexeme match against CALIB_TEXT (verified: ts_rank ~1e-20) -- pure trigram-leg
        // probes, isolated exactly like PgVectorHybridSearchContractTest's Q_TYPO design.
        List<Map<String, Object>> pass = callHybridSearch(
            TENANT_A, DIM, CALIB_PASS_AT_060, List.of(COL_CALIB), 0.6, 5);
        assertThat(ids(pass))
            .as("word_similarity ~0.846 for '%s' must clear the lcogi 0.6 threshold",
                CALIB_PASS_AT_060)
            .containsExactly(calibChash);

        List<Map<String, Object>> weakAt060 = callHybridSearch(
            TENANT_A, DIM, CALIB_FAILS_AT_060_PASSES_AT_050, List.of(COL_CALIB), 0.6, 5);
        assertThat(ids(weakAt060))
            .as("word_similarity ~0.545 for '%s' must NOT clear the lcogi 0.6 threshold "
                + "-- if it did, the function's trigram leg would be miscalibrated "
                + "looser than the Java path it must match", CALIB_FAILS_AT_060_PASSES_AT_050)
            .isEmpty();

        // Cross-check: the SAME weak query DOES pass once the caller relaxes the
        // GUC to 0.5 -- proves the 0.6 exclusion above is a real threshold effect
        // (caller-controlled, exactly like PgVectorRepository.hybridSearch's own
        // SET LOCAL), not the trigram leg being dead/always-empty.
        List<Map<String, Object>> weakAt050 = callHybridSearch(
            TENANT_A, DIM, CALIB_FAILS_AT_060_PASSES_AT_050, List.of(COL_CALIB), 0.5, 5);
        assertThat(ids(weakAt050))
            .as("at a looser caller-set threshold (0.5 < 0.545 similarity), '%s' MUST "
                + "now match -- otherwise the 0.6 exclusion above proves nothing "
                + "(the leg could simply be non-functional)", CALIB_FAILS_AT_060_PASSES_AT_050)
            .containsExactly(calibChash);

        List<Map<String, Object>> junk = callHybridSearch(
            TENANT_A, DIM, CALIB_JUNK, List.of(COL_CALIB), 0.6, 5);
        assertThat(junk)
            .as("'%s' has no text signal at all against CALIB_TEXT (sanity negative "
                + "control) -- must return empty regardless of threshold", CALIB_JUNK)
            .isEmpty();
    }

    // ════════════════════════════════════════════════════════════════════════
    // p95 latency: function path bound, BOTH paths' numbers reported (P5.G measurement)
    // ════════════════════════════════════════════════════════════════════════

    /**
     * BEFORE/AFTER note (2026-08-18, orchestrator completion review): the FIRST P5.2
     * implementation carried an extra {@code ann_probe} CTE (unfiltered top-5000-row ANN
     * scan) solely to make the (now-amended, see
     * {@link #explain_hybridSearchInlines_selectiveGateExactPlanNoHnsw}) EXPLAIN test touch
     * {@code idx_chunks_embedding_384} -- that probe never fed the RRF score (tie-break
     * only). Measured cost of that probe on this fixture: {@code hybrid_search_384.p95}
     * 7-9ms WITH the probe vs 4ms WITHOUT it (identical to {@code lcogi(java).p95}=4ms) --
     * roughly HALF of the function's total latency was the vestigial reachability probe,
     * for zero functional benefit in the overwhelming common case. Removed in
     * {@code vectors-007-hybrid-search-functions.xml}.
     */
    @Test
    void hybridSearch_p95Latency_functionUnderBound_bothPathsReported() {
        // Warm-up (JIT, pool, embedder cache) -- excluded from measurement, both paths.
        for (String q : queries) {
            pgRepo.hybridSearch(TENANT_A, q, List.of(COL_MAIN), K, null);
            callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), K);
        }

        List<Long> fnSamplesMs = new ArrayList<>();
        List<Long> javaSamplesMs = new ArrayList<>();
        for (int round = 0; round < 3; round++) {
            for (String q : queries) {
                long t0 = System.nanoTime();
                callHybridSearch(TENANT_A, DIM, q, List.of(COL_MAIN), K);
                fnSamplesMs.add((System.nanoTime() - t0) / 1_000_000L);

                long t1 = System.nanoTime();
                pgRepo.hybridSearch(TENANT_A, q, List.of(COL_MAIN), K, null);
                javaSamplesMs.add((System.nanoTime() - t1) / 1_000_000L);
            }
        }

        long fnP95   = p95(fnSamplesMs);
        long javaP95 = p95(javaSamplesMs);

        // Labeled measurement line for the P5.G go/no-go: the whole point of P5 (per
        // the RDR-156 2026-08-18 cross-walk) is to justify server-side RRF fusion
        // against nexus-lcogi's ALREADY-SHIPPED fix on ITS OWN merits, not merely to
        // clear an absolute bound -- both numbers must be visible together.
        System.out.println("[hybrid-search-parity P5.G] samples=" + fnSamplesMs.size()
            + " hybrid_search_" + DIM + ".p95=" + fnP95 + "ms lcogi(java).p95=" + javaP95 + "ms"
            + " bound=" + P95_BOUND_MS + "ms");

        assertThat(fnP95)
            .as("p95 of hybrid_search_%d over %d samples must be <= %dms (engine-side "
                + "default; -Dnx.hybridparity.p95.ms overrides). lcogi(java) p95 over the "
                + "identical query set was %dms -- P5.G's go/no-go compares these two "
                + "numbers, not just this bound.", DIM, fnSamplesMs.size(), P95_BOUND_MS, javaP95)
            .isLessThanOrEqualTo(P95_BOUND_MS);
    }

    private static long p95(List<Long> samplesMs) {
        List<Long> sorted = samplesMs.stream().sorted().toList();
        return sorted.get((int) Math.ceil(sorted.size() * 0.95) - 1);
    }
}
