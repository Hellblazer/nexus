// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.ChromaRestClient;
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

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-155 P3.1 (bead nexus-sbvg0): hybrid-parity seam — engine pgvector hybrid vs the
 * legacy FTS5 + Chroma two-path fusion, fixture scale.
 *
 * <p><strong>TDD-RED:</strong> the parity tests call
 * {@link PgVectorRepository#hybridSearch} and fail with
 * {@link UnsupportedOperationException} until bead nexus-eap5l (P3.2) implements it.
 * The fixture guards (corpus seeding, legacy-leg candidate sets, tie margins) are
 * green now — they verify the COMPARAND is correctly constructed before the engine
 * side exists, exactly like the conexus xr7.8.7 fixture guards.
 *
 * <p><strong>The comparand is the real legacy engines, not a reimplementation:</strong>
 * <ul>
 *   <li><strong>Vector leg</strong> — the still-runnable Chroma {@link VectorRepository}
 *       against a live local Chroma server (cosine space), embedding server-side with
 *       the SAME {@link OnnxEmbedder} instance as the pgvector leg, so both stores hold
 *       verbatim-identical vectors (OnnxEmbedder is bit-exactly deterministic; see
 *       {@code VectorIntegrationTest}). Phase 4a may not retire this path before the
 *       P3.G gate runs this suite green (plan invariant 3).
 *   <li><strong>FTS leg</strong> — SQLite FTS5 with the DEFAULT unicode61 tokenizer via
 *       sqlite-jdbc: the same SQLite FTS5 C library the Python T2 path uses
 *       ({@code migrations.py} passes no {@code tokenize=} option, so unicode61 is the
 *       live legacy config).
 *   <li><strong>Legacy fusion</strong> — FTS5-candidate set ranked by Chroma cosine
 *       distance: the gate-then-rank isomorphism of the engine's hybrid, so the measured
 *       delta is exactly the engine delta (english-stemmer tsvector + pg_trgm + pgvector
 *       vs unicode61 FTS5 + Chroma).
 * </ul>
 *
 * <p><strong>Expected-delta classes pinned exactly</strong> (RDR-155 locked anchor,
 * {@code 152-FTS-tokenizer-DECISION} Option B):
 * <ul>
 *   <li>Aligned query (exact word forms both sides): candidate sets identical, vectors
 *       identical, cosine both sides — ordered top-k EQUAL (overlap 1.0).
 *   <li>Stemmer-divergent query: english matches morphological variants unicode61 cannot;
 *       the legacy candidate set is a strict subset. Overlap drops to an exactly
 *       derivable value — asserted as {@code ==}, not {@code >=} noise.
 *   <li>Typo query: pg_trgm rescues, FTS5 is typo-blind — a capability delta (legacy
 *       empty, engine non-empty), excluded from the overlap aggregate.
 * </ul>
 *
 * <p>The aggregate overlap threshold here is the fixture-scale seam assertion this bead
 * owns. The production-scale go/no-go bounds and corpus are conexus-owned (xr7.8.9);
 * the engine-side dual-run harness through the /v1/vectors HTTP seam is P3.E
 * (nexus-h3ked). Do not encode production bounds here.
 *
 * <p>Requires (same as {@code VectorIntegrationTest}): {@code chroma} CLI on PATH (or
 * NX_CHROMA_BINARY), ONNX MiniLM model files in the chromadb cache. Run via
 * {@code mvn test -Dgroups=integration}.
 */
@Tag("integration")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HybridParityIntegrationTest {

    private static final String SVC_ROLE = "svc_parity_test";
    private static final String SVC_PASS = "svc_parity_test_pass";
    private static final String TENANT   = "parity-tenant";

    /** pgvector leg: dispatches to chunks_384 (OnnxEmbedder MiniLM is 384-dim). */
    private static final String PG_COL     = "knowledge__parity__minilm-l6-v2-384__v1";
    /** Chroma leg: separate store, four-segment name, cosine space. */
    private static final String CHROMA_COL = "knowledge__parity__all-minilm-l6-v2__v1";

    private static final int K = 5;

    /**
     * Fixture-scale aggregate overlap threshold (mean Jaccard over the comparable
     * queries QA + QB). Exact per-query values are asserted separately; this constant
     * is the bead's named "overlap >= threshold" seam assertion. Production-scale
     * thresholds are conexus xr7.8.9's to set, not this suite's.
     */
    private static final double PARITY_OVERLAP_THRESHOLD = 0.5;

    // ---------------------------------------------------------------------------
    // Corpus — hand-authored so tokenizer behaviour is exactly analyzable.
    // Vocabulary domains are disjoint: the postgres-transaction rows (QA), the
    // indexing rows (QB), and fillers share no stem-colliding words.
    // ---------------------------------------------------------------------------

    private static final Map<String, String> CORPUS = new LinkedHashMap<>();
    static {
        // QA domain — exact word forms 'postgres' + 'transaction' (no variants anywhere
        // else in the corpus): english and unicode61 derive the SAME candidate set.
        CORPUS.put("par-t1", "postgres transaction semantics with mvcc");
        CORPUS.put("par-t2", "a postgres transaction can roll back");
        CORPUS.put("par-t3", "postgres transaction isolation levels explained");
        // QB domain — morphological variants: 'indexing strategies' / 'index strategy' /
        // 'indexed strategies'. The english stemmer maps all three to 'index' &
        // 'strategi'; unicode61 FTS5 matches only the exact tokens.
        CORPUS.put("par-u1", "indexing strategies for large corpora");
        CORPUS.put("par-u2", "an index strategy for btree lookups");
        CORPUS.put("par-u3", "indexed strategies guide for analytics");
        // Fillers — distinct vocabulary, never text-match any probe query.
        CORPUS.put("par-f1", "chroma etl pipeline with rollback flag");
        CORPUS.put("par-f2", "tokenizer divergence between engines");
        CORPUS.put("par-f3", "hybrid retrieval fusion ranking");
        CORPUS.put("par-f4", "cosine similarity in embedding spaces");
    }

    /** QA — aligned: both tokenizers match exactly {t1, t2, t3}. */
    private static final String QA = "postgres transaction";
    private static final Set<String> QA_LEGACY_CANDIDATES = Set.of("par-t1", "par-t2", "par-t3");

    /** QB — stemmer-divergent: english matches {u1, u2, u3}, unicode61 only {u1}. */
    private static final String QB = "indexing strategies";
    private static final Set<String> QB_LEGACY_CANDIDATES = Set.of("par-u1");
    private static final Set<String> QB_ENGINE_CANDIDATES = Set.of("par-u1", "par-u2", "par-u3");

    /** QC — typo ('transacton'): no FTS lexeme either side; only pg_trgm can rescue. */
    private static final String QC = "transacton isolation";

    PostgreSQLContainer<?> pg;
    HikariDataSource       svcDs;
    TenantScope            tenantScope;
    OnnxEmbedder           onnx;
    PgVectorRepository     pgRepo;
    LocalChromaServer      localChroma;
    VectorRepository       chromaRepo;
    Connection             sqlite;   // in-memory FTS5 lives and dies with this connection

    @BeforeAll
    void startAll() throws Exception {
        // --- pgvector substrate (same steps as PgVectorRepositoryContractTest).
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_384 TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        // --- ONE embedder instance for both legs: verbatim-identical vectors.
        onnx   = new OnnxEmbedder();
        pgRepo = new PgVectorRepository(tenantScope, onnx, onnx);

        // --- Legacy vector leg: live local Chroma (cosine space via ChromaRestClient).
        String chromaBinary = LocalChromaServer.findChromaBinary();
        Path chromaData = Files.createTempDirectory("hybrid-parity-chroma-");
        int chromaPort = LocalChromaServer.findFreePort();
        localChroma = new LocalChromaServer(chromaBinary, chromaData.toString(), chromaPort);
        localChroma.start();
        chromaRepo = new VectorRepository(onnx, onnx, ChromaRestClient.local("127.0.0.1", chromaPort));

        // --- Legacy FTS leg: SQLite FTS5, DEFAULT unicode61 tokenizer (the live T2
        //     config — migrations.py passes no tokenize= option).
        sqlite = DriverManager.getConnection("jdbc:sqlite::memory:");
        sqlite.createStatement().execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(id UNINDEXED, body)");

        // --- Seed the identical corpus into all three stores.
        List<String> ids  = new ArrayList<>(CORPUS.keySet());
        List<String> docs = new ArrayList<>(CORPUS.values());
        List<Map<String, Object>> metas = new ArrayList<>();
        for (int i = 0; i < ids.size(); i++) metas.add(Map.of());

        pgRepo.upsertChunks(TENANT, PG_COL, ids, docs, metas);
        chromaRepo.upsertChunks(CHROMA_COL, ids, docs, metas);
        try (PreparedStatement ps = sqlite.prepareStatement(
                "INSERT INTO chunks_fts (id, body) VALUES (?, ?)")) {
            for (int i = 0; i < ids.size(); i++) {
                ps.setString(1, ids.get(i));
                ps.setString(2, docs.get(i));
                ps.executeUpdate();
            }
        }
    }

    @AfterAll
    void stopAll() throws Exception {
        if (sqlite      != null) sqlite.close();
        if (localChroma != null) localChroma.stop();
        if (onnx        != null) onnx.close();
        if (svcDs       != null) svcDs.close();
        if (pg          != null) pg.stop();
    }

    // ---------------------------------------------------------------------------
    // Legacy two-path fusion (the comparand)
    // ---------------------------------------------------------------------------

    /** FTS5 candidate IDs for a query (unicode61, implicit AND — plainto_tsquery's analog). */
    private Set<String> fts5Candidates(String query) throws SQLException {
        Set<String> out = new LinkedHashSet<>();
        try (PreparedStatement ps = sqlite.prepareStatement(
                "SELECT id FROM chunks_fts WHERE chunks_fts MATCH ?")) {
            ps.setString(1, query);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) out.add(rs.getString(1));
            }
        }
        return out;
    }

    /** Chroma vector ranking over the whole collection (cosine distance ascending). */
    private List<String> chromaVectorRanking(String query) {
        List<Map<String, Object>> rows =
            chromaRepo.search(query, List.of(CHROMA_COL), CORPUS.size(), null);
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    /** Legacy fused top-k: FTS5 candidates ranked by Chroma cosine distance. */
    private List<String> legacyFusedTopK(String query, int k) throws SQLException {
        Set<String> fts = fts5Candidates(query);
        return chromaVectorRanking(query).stream()
                .filter(fts::contains)
                .limit(k)
                .toList();
    }

    /** Engine hybrid top-k IDs (RED until P3.2 implements hybridSearch). */
    private List<String> engineHybridTopK(String query, int k) {
        return pgRepo.hybridSearch(TENANT, query, List.of(PG_COL), k, null).stream()
                .map(r -> (String) r.get("id"))
                .toList();
    }

    private static double jaccard(Set<String> a, Set<String> b) {
        if (a.isEmpty() && b.isEmpty()) return 1.0;
        Set<String> inter = new LinkedHashSet<>(a);
        inter.retainAll(b);
        Set<String> union = new LinkedHashSet<>(a);
        union.addAll(b);
        return (double) inter.size() / union.size();
    }

    // ---------------------------------------------------------------------------
    // Fixture guards (GREEN before P3.2 — they verify the comparand, not the engine)
    // ---------------------------------------------------------------------------

    @Test
    void guard_corpusSeededIdentically_allThreeStores() throws Exception {
        assertThat(pgRepo.count(TENANT, PG_COL))
            .as("pgvector leg holds the full corpus").isEqualTo(CORPUS.size());
        assertThat(chromaVectorRanking(QA))
            .as("chroma leg ranks the full corpus").hasSize(CORPUS.size());
        try (ResultSet rs = sqlite.createStatement()
                .executeQuery("SELECT count(*) FROM chunks_fts")) {
            rs.next();
            assertThat(rs.getInt(1)).as("FTS5 leg holds the full corpus")
                .isEqualTo(CORPUS.size());
        }
    }

    @Test
    void guard_legacyFts5CandidateSets_exact() throws Exception {
        assertThat(fts5Candidates(QA))
            .as("aligned query: unicode61 matches exactly the three exact-word-form rows")
            .containsExactlyInAnyOrderElementsOf(QA_LEGACY_CANDIDATES);
        assertThat(fts5Candidates(QB))
            .as("stemmer-divergent query: unicode61 (no stemming) matches ONLY the row "
                + "with the exact tokens 'indexing' AND 'strategies'")
            .containsExactlyInAnyOrderElementsOf(QB_LEGACY_CANDIDATES);
        assertThat(fts5Candidates(QC))
            .as("typo query: FTS5 is typo-blind — the legacy text leg has NO candidates")
            .isEmpty();
    }

    @Test
    void guard_candidateVectorMargins_noNearTies() {
        // If a future corpus edit creates a near-tie between candidate rows, ordered
        // parity assertions would become float-noise-flaky. Catch it here by name
        // (conexus xr7.8.7 min-gap guard, runtime form — embeddings are real, not
        // hand-authored).
        for (var probe : List.of(Map.entry(QA, QA_LEGACY_CANDIDATES),
                                 Map.entry(QB, QB_ENGINE_CANDIDATES))) {
            List<Map<String, Object>> rows =
                pgRepo.search(TENANT, probe.getKey(), List.of(PG_COL), CORPUS.size(), null);
            List<Double> candidateDists = rows.stream()
                .filter(r -> probe.getValue().contains((String) r.get("id")))
                .map(r -> ((Number) r.get("distance")).doubleValue())
                .toList();
            assertThat(candidateDists)
                .as("all candidates of %s must rank", probe.getKey())
                .hasSize(probe.getValue().size());
            for (int i = 1; i < candidateDists.size(); i++) {
                assertThat(candidateDists.get(i) - candidateDists.get(i - 1))
                    .as("consecutive candidate distances for query '%s' must be separated "
                        + "by > 1e-4 — a near-tie makes ordered parity flaky; reword the "
                        + "fixture text", probe.getKey())
                    .isGreaterThan(1e-4);
            }
        }
    }

    // ---------------------------------------------------------------------------
    // Parity seam (RED until P3.2)
    // ---------------------------------------------------------------------------

    @Test
    void parity_alignedQuery_orderedTopKEqual() throws Exception {
        // Identical candidate sets (guard above), identical vectors (one embedder),
        // cosine both sides: the ordered lists must be EQUAL, not merely overlapping.
        List<String> legacy = legacyFusedTopK(QA, K);
        List<String> engine = engineHybridTopK(QA, K);

        assertThat(legacy)
            .as("non-vacuity: the legacy fused list is non-empty")
            .isNotEmpty();
        assertThat(engine)
            .as("aligned query: engine hybrid ordering must EQUAL the legacy fused "
                + "ordering — any divergence here is engine error, not tokenizer delta")
            .containsExactlyElementsOf(legacy);
    }

    @Test
    void parity_stemmerDivergentQuery_exactExpectedDelta() throws Exception {
        // The locked anchor (152-FTS-tokenizer-DECISION Option B): english-stemmer vs
        // unicode61 divergence is the EXPECTED delta. On this fixture it is exactly
        // derivable: legacy candidates {u1}, engine candidates {u1, u2, u3}.
        List<String> legacy = legacyFusedTopK(QB, K);
        List<String> engine = engineHybridTopK(QB, K);

        assertThat(legacy)
            .as("legacy fused list is exactly the single exact-token row")
            .containsExactly("par-u1");
        assertThat(Set.copyOf(engine))
            .as("engine candidate set is exactly the three stemmed-variant rows")
            .isEqualTo(QB_ENGINE_CANDIDATES);
        assertThat(jaccard(Set.copyOf(legacy), Set.copyOf(engine)))
            .as("the stemmer delta on this fixture is EXACTLY 1/3 — not an inequality")
            .isEqualTo(1.0 / 3.0);
    }

    @Test
    void parity_legacyCandidates_subsetOfEngineCandidates() throws Exception {
        // Stemming and trigram similarity only WIDEN the text gate. Any legacy hit the
        // engine misses is an engine regression, never an expected delta.
        for (String query : List.of(QA, QB)) {
            Set<String> legacy = Set.copyOf(legacyFusedTopK(query, K));
            Set<String> engine = Set.copyOf(engineHybridTopK(query, K));
            assertThat(engine)
                .as("every legacy candidate for '%s' must appear in the engine results",
                    query)
                .containsAll(legacy);
        }
    }

    @Test
    void parity_typoQuery_trgmCapabilityDelta() throws Exception {
        // pg_trgm is a capability the legacy path never had: FTS5 is typo-blind
        // (guard asserts legacy candidates empty), the engine must still reach the
        // row containing both corrected words. Excluded from the overlap aggregate.
        assertThat(legacyFusedTopK(QC, K))
            .as("legacy two-path fusion returns nothing for a typo query")
            .isEmpty();

        List<String> engine = engineHybridTopK(QC, K);
        assertThat(engine)
            .as("engine trgm leg rescues the typo query")
            .isNotEmpty()
            .as("the row containing both corrected words ('transaction isolation') has "
                + "the highest text similarity under any sane gate and must be present")
            .contains("par-t3");
    }

    @Test
    void parity_overlapAggregate_meetsThreshold() throws Exception {
        // The bead's named seam assertion: mean Jaccard overlap across the comparable
        // queries >= PARITY_OVERLAP_THRESHOLD. Non-vacuous: per-query values are
        // exactly pinned above (QA == 1.0, QB == 1/3), both sides non-empty.
        double overlapA = jaccard(Set.copyOf(legacyFusedTopK(QA, K)),
                                  Set.copyOf(engineHybridTopK(QA, K)));
        double overlapB = jaccard(Set.copyOf(legacyFusedTopK(QB, K)),
                                  Set.copyOf(engineHybridTopK(QB, K)));

        assertThat(overlapA).as("aligned-query overlap is exactly 1.0").isEqualTo(1.0);
        assertThat(overlapB).as("stemmer-delta overlap is exactly 1/3").isEqualTo(1.0 / 3.0);
        assertThat((overlapA + overlapB) / 2.0)
            .as("fixture-scale aggregate overlap (exact: 2/3) must clear the seam "
                + "threshold %s", PARITY_OVERLAP_THRESHOLD)
            .isGreaterThanOrEqualTo(PARITY_OVERLAP_THRESHOLD);
    }
}
