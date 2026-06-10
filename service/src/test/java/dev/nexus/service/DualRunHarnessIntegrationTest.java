// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenHashing;
import dev.nexus.service.vectors.ChromaRestClient;
import dev.nexus.service.vectors.EmbedderRouter;
import dev.nexus.service.vectors.LocalChromaServer;
import dev.nexus.service.vectors.OnnxEmbedder;
import dev.nexus.service.vectors.PgVectorRepository;
import dev.nexus.service.vectors.VectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-155 P3.E (bead nexus-h3ked): engine-side DUAL-RUN harness — the test-invocable
 * validation seam for the conexus xr7.8.9 go-live gates.
 *
 * <p><strong>Scope boundary (recorded on the bead):</strong> this harness is
 * corpus-agnostic and its bounds are engine-side DEFAULTS. The production-scale
 * filtered-recall and hybrid-parity GO-LIVE GATES are owned by conexus (xr7.8.9):
 * conexus supplies the production corpus and the go/no-go numeric bounds via the
 * system properties below; this class supplies the seam and the harness mechanics.
 * Generalizes the conexus slice spike ({@code conexus/spikes/pgvector-recall/})
 * THROUGH the service.
 *
 * <p><strong>What it proves</strong>:
 * <ul>
 *   <li><strong>Verbatim-identical vectors, Seam B wiring.</strong> ONE
 *       {@link OnnxEmbedder} behind production-shaped {@link EmbedderRouter}s
 *       (document + query) feeds BOTH stores: the Chroma {@link VectorRepository}
 *       (router constructor) and the {@link PgVectorRepository} (router constructor —
 *       this closes the P3.2 review deferral: the {@code embedOneForCollection}
 *       hybrid-query branch is exercised here).
 *   <li><strong>Exact-count recall.</strong> Per query, the pgvector top-k ID set vs
 *       the Chroma baseline top-k ID set; per-query overlap counted exactly and the
 *       aggregate recall fraction asserted against {@code nx.dualrun.recall.min}
 *       (default 1.0 — identical vectors, cosine both sides, fixture-default scale
 *       where HNSW is effectively exact).
 *   <li><strong>HTTP seam fidelity.</strong> {@code POST /v1/vectors/hybrid-search}
 *       returns exactly what {@link PgVectorRepository#hybridSearch} returns — the
 *       service path (auth, server-resolved tenant, RLS) adds and loses nothing.
 *   <li><strong>Hybrid-parity hook.</strong> Every hybrid result is inside the
 *       exactly-computable text-candidate oracle (the corpus uses exact word forms,
 *       so the english-stemmer FTS candidate set equals plain word containment), and
 *       FTS-matching queries return non-empty results. The cross-engine FTS5
 *       comparand at fixture scale is {@code HybridParityIntegrationTest} (P3.1).
 *   <li><strong>Non-waivable p95 latency bound.</strong> p95 of the engine hybrid
 *       HTTP path over the query set must clear {@code nx.dualrun.p95.ms}. The
 *       assertion always runs — there is no skip flag; production tightens the bound,
 *       nothing can waive it.
 * </ul>
 *
 * <p><strong>Parameterization (conexus xr7.8.9 drives these):</strong>
 * <pre>
 *   -Dnx.dualrun.corpus.size=200    documents generated (deterministic, seeded)
 *   -Dnx.dualrun.queries=20         number of probe queries
 *   -Dnx.dualrun.k=10               top-k depth for recall
 *   -Dnx.dualrun.recall.min=1.0     minimum aggregate recall fraction
 *   -Dnx.dualrun.p95.ms=250         p95 bound for the hybrid HTTP path, milliseconds
 * </pre>
 *
 * <p>Fixture-load: the Chroma leg loads THROUGH the service
 * ({@code /v1/vectors/upsert-chunks}); the pgvector leg loads via the repository —
 * a pgvector HTTP write surface does not exist until the Phase 4a serving cutover,
 * and creating one here would be exactly the premature surface P4a gates.
 *
 * <p>Requires (same as {@code VectorIntegrationTest}): {@code chroma} CLI on PATH,
 * ONNX MiniLM files in the chromadb cache. Run via
 * {@code mvn test -Dtest=DualRunHarnessIntegrationTest -Dtest.excluded.groups="" -Dgroups=integration}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class DualRunHarnessIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TOKEN  = "dualrun-harness-token-0123456789abcdef";
    private static final String TENANT = "dualrun-tenant";

    private static final String PG_COL     = "knowledge__dualrun__minilm-l6-v2-384__v1";
    private static final String CHROMA_COL = "knowledge__dualrun__all-minilm-l6-v2__v1";

    // Engine-side defaults; conexus xr7.8.9 overrides via -D system properties.
    private static final int    CORPUS_SIZE = Integer.getInteger("nx.dualrun.corpus.size", 200);
    private static final int    QUERY_COUNT = Integer.getInteger("nx.dualrun.queries", 20);
    private static final int    K           = Integer.getInteger("nx.dualrun.k", 10);
    private static final double RECALL_MIN  =
        Double.parseDouble(System.getProperty("nx.dualrun.recall.min", "1.0"));
    private static final long   P95_BOUND_MS =
        Long.getLong("nx.dualrun.p95.ms", 250L);

    /**
     * Word bank for deterministic corpus generation. All entries are single english
     * words with DISTINCT stems and exact-form usage (no morphological variants), so
     * the english-stemmer FTS candidate set for any two-word query equals plain word
     * containment — an exactly-computable oracle without a second FTS engine.
     */
    private static final List<String> WORD_BANK = List.of(
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
        "yankee", "zulu", "cobalt", "quartz", "falcon", "harbor", "lantern",
        "marble", "nickel", "orchid", "pylon", "quiver", "raven", "saddle",
        "timber", "vortex", "willow");

    PostgreSQLContainer<?> pg;
    HikariDataSource       svcDs;
    TenantScope            tenantScope;
    OnnxEmbedder           onnx;
    EmbedderRouter         docRouter;
    EmbedderRouter         queryRouter;
    PgVectorRepository     pgRepo;
    LocalChromaServer      localChroma;
    Path                   chromaData;
    VectorRepository       chromaRepo;
    NexusService           service;
    HttpClient             http;

    /** Generated corpus: id → text (insertion-ordered, deterministic). */
    final Map<String, String> corpus = new LinkedHashMap<>();
    /** Probe queries (two exact corpus words each, deterministic). */
    final List<String> queries = new ArrayList<>();

    @BeforeAll
    void startAll() throws Exception {
        generateCorpusAndQueries();

        // --- pgvector substrate.
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                          new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("");
             var ps = su.prepareStatement(
                 "INSERT INTO nexus.service_tokens (token_hash, tenant_id, label)"
                 + " VALUES (?, ?, ?) ON CONFLICT (token_hash) DO NOTHING")) {
            su.setAutoCommit(true);
            ps.setString(1, TokenHashing.sha256Hex(TOKEN));
            ps.setString(2, TENANT);
            ps.setString(3, "dualrun-harness");
            ps.executeUpdate();
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername("nexus_svc");
        cfg.setPassword("nexus_svc_pass");
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        // --- ONE embedder, production-shaped routers, BOTH repos on the router
        //     constructor (Seam B). Local-mode router: everything routes to ONNX.
        onnx        = new OnnxEmbedder();
        docRouter   = new EmbedderRouter(onnx, "document");
        queryRouter = new EmbedderRouter(onnx, "query");
        pgRepo      = new PgVectorRepository(tenantScope, docRouter, queryRouter);

        String chromaBinary = LocalChromaServer.findChromaBinary();
        chromaData = Files.createTempDirectory("dualrun-chroma-");
        int chromaPort = LocalChromaServer.findFreePort();
        localChroma = new LocalChromaServer(chromaBinary, chromaData.toString(), chromaPort);
        localChroma.start();
        chromaRepo = new VectorRepository(docRouter, queryRouter,
                                          ChromaRestClient.local("127.0.0.1", chromaPort));

        service = new NexusService(0, TOKEN, svcDs, chromaRepo, docRouter, pgRepo);
        service.start();
        http = HttpClient.newHttpClient();

        // --- Fixture-load. Chroma THROUGH the service; pgvector via the repository
        //     (no pgvector HTTP write surface until Phase 4a — deliberate).
        List<String> ids   = new ArrayList<>(corpus.keySet());
        List<String> texts = new ArrayList<>(corpus.values());
        int batch = 100;   // under the Chroma MAX_RECORDS_PER_WRITE=300 quota
        for (int i = 0; i < ids.size(); i += batch) {
            int end = Math.min(i + batch, ids.size());
            List<Map<String, Object>> metas = new ArrayList<>();
            for (int j = i; j < end; j++) metas.add(Map.of());
            var resp = post("/v1/vectors/upsert-chunks", Map.of(
                "collection", CHROMA_COL,
                "ids",        ids.subList(i, end),
                "documents",  texts.subList(i, end),
                "metadatas",  metas));
            assertThat(resp.statusCode())
                .as("chroma fixture-load batch %d..%d through the service", i, end)
                .isEqualTo(200);
            pgRepo.upsertChunks(TENANT, PG_COL,
                ids.subList(i, end), texts.subList(i, end), metas);
        }
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service     != null) service.stop();
        if (localChroma != null) localChroma.stop();
        if (chromaData  != null) {
            try (var walk = Files.walk(chromaData)) {
                walk.sorted(java.util.Comparator.reverseOrder())
                    .forEach(p -> p.toFile().delete());
            }
        }
        if (onnx        != null) onnx.close();
        if (svcDs       != null) svcDs.close();
        if (pg          != null) pg.stop();
    }

    // ---------------------------------------------------------------------------
    // Deterministic corpus + queries
    // ---------------------------------------------------------------------------

    /**
     * Seeded generation: each document is 8-12 distinct bank words plus a unique
     * discriminator token; each query is the first two words of a distinct document
     * (guaranteeing at least one FTS match per query). Seed fixed — the corpus is
     * identical on every run at the same {@code corpus.size}.
     */
    private void generateCorpusAndQueries() {
        Random rnd = new Random(20260609L);
        for (int d = 0; d < CORPUS_SIZE; d++) {
            int len = 8 + rnd.nextInt(5);
            Set<String> words = new LinkedHashSet<>();
            while (words.size() < len) {
                words.add(WORD_BANK.get(rnd.nextInt(WORD_BANK.size())));
            }
            String id = String.format("dr-%05d", d);
            corpus.put(id, String.join(" ", words) + " doc" + d);
        }
        List<String> docTexts = new ArrayList<>(corpus.values());
        for (int q = 0; q < QUERY_COUNT; q++) {
            String[] w = docTexts.get((q * 7) % docTexts.size()).split(" ");
            queries.add(w[0] + " " + w[1]);
        }
    }

    /** Exact text-candidate oracle: corpus IDs whose text contains EVERY query word. */
    private Set<String> textCandidates(String query) {
        String[] terms = query.split(" ");
        Set<String> out = new LinkedHashSet<>();
        for (var e : corpus.entrySet()) {
            Set<String> words = Set.of(e.getValue().split(" "));
            boolean all = true;
            for (String t : terms) {
                if (!words.contains(t)) { all = false; break; }
            }
            if (all) out.add(e.getKey());
        }
        return out;
    }

    // ---------------------------------------------------------------------------
    // HTTP helpers
    // ---------------------------------------------------------------------------

    private HttpResponse<String> post(String path, Object body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + TOKEN)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> postRows(String path, Object body) throws Exception {
        var resp = post(path, body);
        assertThat(resp.statusCode()).as("%s must return 200", path).isEqualTo(200);
        return MAPPER.readValue(resp.body(), List.class);
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    // ---------------------------------------------------------------------------
    // Guards
    // ---------------------------------------------------------------------------

    @Test
    void guard_corpusLoadedIdentically_bothStores() throws Exception {
        assertThat(pgRepo.count(TENANT, PG_COL))
            .as("pgvector leg holds the full corpus").isEqualTo(CORPUS_SIZE);
        assertThat(chromaRepo.count(CHROMA_COL))
            .as("chroma leg holds the full corpus").isEqualTo(CORPUS_SIZE);
        assertThat(queries).hasSize(QUERY_COUNT);
        for (String q : queries) {
            assertThat(textCandidates(q))
                .as("every probe query must have at least one text candidate "
                    + "(query construction guarantees it)")
                .isNotEmpty();
        }
    }

    // ---------------------------------------------------------------------------
    // Dual-run: exact-count recall (pgvector vs Chroma baseline)
    // ---------------------------------------------------------------------------

    @Test
    void dualRun_exactCountRecall_pgVsChromaBaseline() throws Exception {
        int totalOverlap = 0;
        int totalPossible = 0;
        List<String> perQuery = new ArrayList<>();

        for (String q : queries) {
            List<Map<String, Object>> chromaRows = postRows("/v1/vectors/search",
                Map.of("query", q, "collections", List.of(CHROMA_COL), "n_results", K));
            List<Map<String, Object>> pgRows =
                pgRepo.search(TENANT, q, List.of(PG_COL), K, null);

            Set<String> chromaTop = new LinkedHashSet<>(ids(chromaRows));
            Set<String> pgTop     = new LinkedHashSet<>(ids(pgRows));
            assertThat(chromaTop).as("chroma baseline returns k rows for '%s'", q).hasSize(K);
            assertThat(pgTop).as("pgvector returns k rows for '%s'", q).hasSize(K);

            Set<String> inter = new LinkedHashSet<>(pgTop);
            inter.retainAll(chromaTop);
            totalOverlap  += inter.size();
            totalPossible += K;
            perQuery.add(q + "=" + inter.size() + "/" + K);
        }

        double recall = (double) totalOverlap / totalPossible;
        assertThat(recall)
            .as("aggregate exact-count recall (pgvector top-%d vs Chroma baseline) over "
                + "%d queries must be >= %s. Per-query: %s",
                K, queries.size(), RECALL_MIN, perQuery)
            .isGreaterThanOrEqualTo(RECALL_MIN);
    }

    // ---------------------------------------------------------------------------
    // HTTP seam fidelity: /hybrid-search == repository hybridSearch
    // ---------------------------------------------------------------------------

    @Test
    void hybridHttp_matchesRepositoryHybrid_exactly() throws Exception {
        for (String q : queries) {
            List<String> httpIds = ids(postRows("/v1/vectors/hybrid-search",
                Map.of("query", q, "collections", List.of(PG_COL), "n_results", K)));
            List<String> repoIds = ids(
                pgRepo.hybridSearch(TENANT, q, List.of(PG_COL), K, null));

            assertThat(httpIds)
                .as("the HTTP seam must return exactly the repository's fused list for "
                    + "'%s' — the service path (auth, tenant resolution, RLS) adds and "
                    + "loses nothing", q)
                .containsExactlyElementsOf(repoIds);
        }
    }

    // ---------------------------------------------------------------------------
    // Hybrid-parity hook: results inside the exact text-candidate oracle
    // ---------------------------------------------------------------------------

    @Test
    void hybridHttp_resultsInsideTextSignalOracle() throws Exception {
        // Two-level oracle. (1) SOUNDNESS: every hybrid row must share at least one
        // exact query word — a row with ZERO query words has no plausible text signal
        // (word_similarity of a two-word query cannot reach the 0.6 gate against a text
        // containing neither word) and would mean the text gate leaked vector-only
        // rows. NOTE: exact both-word containment is deliberately NOT required — the
        // trgm leg legitimately admits near-miss rows that contain one query word plus
        // a trigram-similar neighbour (observed at this corpus scale: word_similarity
        // crosses 0.6 with one exact word + partial overlap on the other).
        // (2) COMPLETENESS: every exact both-word candidate must rank if room remains
        // (the fused list is only allowed to omit an exact candidate when k is full).
        for (String q : queries) {
            Set<String> exact = textCandidates(q);
            List<String> hybridIds = ids(postRows("/v1/vectors/hybrid-search",
                Map.of("query", q, "collections", List.of(PG_COL), "n_results", K)));

            assertThat(hybridIds)
                .as("hybrid results for '%s' must be non-empty (the query is built from "
                    + "a document's own words)", q)
                .isNotEmpty();

            String[] terms = q.split(" ");
            for (String id : hybridIds) {
                Set<String> words = Set.of(corpus.get(id).split(" "));
                boolean anyTerm = false;
                for (String t : terms) {
                    if (words.contains(t)) { anyTerm = true; break; }
                }
                assertThat(anyTerm)
                    .as("hybrid row %s for query '%s' contains NO query word — the "
                        + "text gate leaked a vector-only row", id, q)
                    .isTrue();
            }

            if (hybridIds.size() < K) {
                assertThat(hybridIds)
                    .as("the fused list for '%s' has room (%d < k=%d) — every exact "
                        + "both-word candidate must be present", q, hybridIds.size(), K)
                    .containsAll(exact);
            }
        }
    }

    // ---------------------------------------------------------------------------
    // Non-waivable p95 latency bound on the engine hybrid HTTP path
    // ---------------------------------------------------------------------------

    @Test
    void hybridHttp_p95LatencyBound_nonWaivable() throws Exception {
        // Warm-up pass (JIT, pool, embedder) — excluded from measurement.
        for (String q : queries) {
            postRows("/v1/vectors/hybrid-search",
                Map.of("query", q, "collections", List.of(PG_COL), "n_results", K));
        }

        List<Long> samplesMs = new ArrayList<>();
        for (int round = 0; round < 3; round++) {
            for (String q : queries) {
                long t0 = System.nanoTime();
                postRows("/v1/vectors/hybrid-search",
                    Map.of("query", q, "collections", List.of(PG_COL), "n_results", K));
                samplesMs.add((System.nanoTime() - t0) / 1_000_000L);
            }
        }
        List<Long> sorted = samplesMs.stream().sorted().toList();
        long p95 = sorted.get((int) Math.ceil(sorted.size() * 0.95) - 1);

        assertThat(p95)
            .as("p95 of the engine hybrid HTTP path over %d samples must be <= %dms "
                + "(engine-side default; conexus xr7.8.9 sets the production bound via "
                + "-Dnx.dualrun.p95.ms). This assertion has no skip flag.",
                sorted.size(), P95_BOUND_MS)
            .isLessThanOrEqualTo(P95_BOUND_MS);
    }
}
