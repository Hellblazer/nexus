// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

/**
 * RDR-155 P3.1 (bead nexus-sbvg0): hybrid tsvector + pg_trgm + vector fusion contract suite.
 *
 * <p><strong>TDD-RED: every test in this class fails with
 * {@link UnsupportedOperationException} until bead nexus-eap5l (P3.2) implements
 * {@link PgVectorRepository#hybridSearch}.</strong> The method ships as a signature-only
 * skeleton in this bead; P3.2 fills the body and makes this suite green WITHOUT changing
 * it (the suite is the locked contract — exact orderings, exact exclusions).
 *
 * <p>Fusion contract pinned here (RDR-155 §Query path Hybrid search; bead nexus-sbvg0):
 * <ul>
 *   <li><strong>Text gate, vector rank.</strong> The candidate set is rows matching at
 *       least one text signal — {@code chunk_tsv @@ plainto_tsquery('english', q)} OR
 *       trigram similarity — and candidates are ranked by vector cosine distance ascending.
 *       This is the same fused-reference shape the conexus xr7.8.7 parity harness validated
 *       and the xr7.8.9 go-live gate drives against this engine's seam.
 *   <li><strong>No silent vector fallback.</strong> A query with zero text candidates
 *       returns empty — a vector-close row with no text signal NEVER appears. Semantic-only
 *       retrieval remains {@link PgVectorRepository#search}'s job.
 *   <li><strong>Trigram rescue.</strong> A typo query that matches no FTS lexeme still
 *       returns the trigram-similar rows ({@code pg_trgm} leg is real, not decorative).
 *   <li><strong>Same envelope as search().</strong> RLS tenant scope, per-dim dispatch with
 *       fail-loud on mixed dims / unknown model segments, multi-collection filtered union,
 *       metadata where-predicates ANDed with the text gate, nResults cap, flat row shape.
 * </ul>
 *
 * <p>Non-vacuity (conexus xr7.8.7 pattern): the fixture is designed so FTS-candidate
 * chash ordering != fused ordering != vector-only ordering. Tests assert all three
 * differ, proving both signals are load-bearing.
 *
 * <p>Embedding is the deterministic {@link PgVectorRepositoryContractTest.FakeEmbedder}
 * (unit vectors with exact pairwise cosine distances). Hermetic: Testcontainers
 * pgvector/pgvector:pg17, PER_CLASS lifecycle, fixture seeded once via the GREEN P2
 * upsert path.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorHybridSearchContractTest {

    // Plain-LOGIN service role: NOSUPERUSER, NOT table owner, NO BYPASSRLS.
    private static final String SVC_ROLE = "svc_hybrid_test";
    private static final String SVC_PASS = "svc_hybrid_test_pass";

    private static final String TENANT_A = "tenant-a";
    private static final String TENANT_B = "tenant-b";

    private static final String COL_HY   = "knowledge__hybrid__voyage-context-3__v1";
    private static final String COL_MA   = "knowledge__hyba__voyage-context-3__v1";
    private static final String COL_MB   = "code__hybb__voyage-code-3__v1";
    private static final String COL_WH   = "docs__hybridwhere__bge-base-en-v15-768__v1";
    private static final String COL_384H = "knowledge__hybrid384__minilm-l6-v2-384__v1";
    private static final String COL_MINI = "knowledge__alpha__minilm-l6-v2-384__v1";
    private static final String COL_UNKNOWN = "code__alpha__mystery-model-9000__v1";

    /**
     * The fused-ranking probe query. Embeds to (1, 0); every COL_HY text registers a
     * vector whose first two components make its cosine distance to this query exact.
     */
    private static final String Q = "tenant isolation policy";

    /**
     * Trigram-rescue probe: single-character typo ("isolaton") of a phrase present
     * verbatim in the FTS-matching fixture rows. {@code plainto_tsquery('english',
     * 'tenant isolaton policy')} produces the lexeme 'isolaton' which appears NOWHERE,
     * so the FTS leg matches nothing — only the pg_trgm leg can return candidates
     * (word similarity vs the matching rows is ≈0.9, far above any sane gate; vs the
     * non-matching rows ≈0.1, far below).
     */
    private static final String Q_TYPO = "tenant isolaton policy";

    /** No text signal anywhere in the fixture: no FTS lexeme, no trigram similarity. */
    private static final String Q_JUNK = "zzqq xkcd glorp";

    // COL_HY fixture — (chash, text, unit vector, cosine distance to Q):
    //   hyb-c1  FTS match   (1.0,  0.0)        dist 0.0   (nearest)
    //   hyb-c2  FTS match   (0.6,  0.8)        dist 0.4   (third)
    //   hyb-c3  FTS match   (0.8,  0.6)        dist 0.2   (second)
    //   hyb-c4  NO signal   (0.995, 0.0998749) dist 0.005 (vector-close, text-unrelated)
    //   hyb-c5  NO signal   (0.0,  1.0)        dist 1.0   (excluded both ways)
    //   hyb-c6  FTS match   (-1.0, 0.0)        dist 2.0   (furthest candidate)
    //
    // Fused expected: [c1, c3, c2, c6] — differs from candidate-chash order [c1, c2, c3, c6]
    // (vector signal real) and from vector-only top-4 [c1, c4, c3, c2] (text gate real).
    // Minimum gap between consecutive candidate distances is 0.2 — far above float noise.
    private static final String T_C1 = "the tenant isolation policy guards every row";
    private static final String T_C2 = "tenant isolation policy enforcement in postgres";
    private static final String T_C3 = "a tenant isolation policy for vector chunks";
    private static final String T_C4 = "quantum entanglement spectroscopy experiment";
    private static final String T_C5 = "unrelated cooking recipe for pasta carbonara";
    private static final String T_C6 = "the tenant isolation policy appendix";

    // Canonical 64-hex chash fixtures (RDR-180: full digest, not a hand-padded id).
    private static final String HYB_C1 = dev.nexus.service.db.Chash.ofText("hyb-c1").toHex();
    private static final String HYB_C2 = dev.nexus.service.db.Chash.ofText("hyb-c2").toHex();
    private static final String HYB_C3 = dev.nexus.service.db.Chash.ofText("hyb-c3").toHex();
    private static final String HYB_C4 = dev.nexus.service.db.Chash.ofText("hyb-c4").toHex();
    private static final String HYB_C5 = dev.nexus.service.db.Chash.ofText("hyb-c5").toHex();
    private static final String HYB_C6 = dev.nexus.service.db.Chash.ofText("hyb-c6").toHex();
    private static final String MA_C1  = dev.nexus.service.db.Chash.ofText("ma-c1").toHex();
    private static final String MA_C2  = dev.nexus.service.db.Chash.ofText("ma-c2").toHex();
    private static final String MB_C1  = dev.nexus.service.db.Chash.ofText("mb-c1").toHex();
    private static final String WH_C1  = dev.nexus.service.db.Chash.ofText("wh-c1").toHex();
    private static final String WH_C2  = dev.nexus.service.db.Chash.ofText("wh-c2").toHex();
    private static final String M384_C1 = dev.nexus.service.db.Chash.ofText("m384-c1").toHex();
    private static final String M384_C2 = dev.nexus.service.db.Chash.ofText("m384-c2").toHex();
    private static final String M384_C3 = dev.nexus.service.db.Chash.ofText("m384-c3").toHex();

    private static final List<String> FUSED_EXPECTED = List.of(HYB_C1, HYB_C3, HYB_C2, HYB_C6);

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;

    PgVectorRepositoryContractTest.FakeEmbedder embedder1024;
    PgVectorRepositoryContractTest.FakeEmbedder embedder768;
    PgVectorRepositoryContractTest.FakeEmbedder embedder384;

    PgVectorRepository repo1024;
    PgVectorRepository repo768;
    PgVectorRepository repo384;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // --- Step 1: roles before Liquibase (changeset DO-blocks need them).
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

        // --- Step 2: full master changelog (chunks tables + pgvector + pg_trgm).
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                          new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }

        // --- Step 3: grants for the svc role.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_" + dim + " TO " + SVC_ROLE);
            }
            // ensureCollectionRegistered() in PgVectorRepository.upsertChunks() needs
            // INSERT (for the ON CONFLICT DO NOTHING upsert) and SELECT (implicit in
            // ON CONFLICT clause resolution) on catalog_collections.
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // --- Step 4: svc pool + repositories.
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        embedder1024 = new PgVectorRepositoryContractTest.FakeEmbedder(1024);
        embedder768  = new PgVectorRepositoryContractTest.FakeEmbedder(768);
        embedder384  = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo1024 = new PgVectorRepository(tenantScope, embedder1024, embedder1024);
        repo768  = new PgVectorRepository(tenantScope, embedder768,  embedder768);
        repo384  = new PgVectorRepository(tenantScope, embedder384,  embedder384);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    /** Seed every collection once via the GREEN P2 upsert path; tests are read-only. */
    private void seedFixtures() {
        // Queries embed to (1, 0). Q_TYPO and Q_JUNK fall through to FakeEmbedder's
        // default (1, 0) — registered explicitly anyway for readability.
        embedder1024.register(Q,      1.0f, 0.0f);
        embedder1024.register(Q_TYPO, 1.0f, 0.0f);
        embedder1024.register(Q_JUNK, 1.0f, 0.0f);

        embedder1024.register(T_C1,  1.0f, 0.0f);
        embedder1024.register(T_C2,  0.6f, 0.8f);
        embedder1024.register(T_C3,  0.8f, 0.6f);
        embedder1024.register(T_C4,  0.995f, 0.0998749f);
        embedder1024.register(T_C5,  0.0f, 1.0f);
        embedder1024.register(T_C6, -1.0f, 0.0f);
        repo1024.upsertChunks(TENANT_A, COL_HY,
            List.of(HYB_C1, HYB_C2, HYB_C3, HYB_C4, HYB_C5, HYB_C6),
            List.of(T_C1, T_C2, T_C3, T_C4, T_C5, T_C6),
            List.of(Map.of("kind", "hy"), Map.of("kind", "hy"), Map.of("kind", "hy"),
                    Map.of("kind", "hy"), Map.of("kind", "hy"), Map.of("kind", "hy")));

        // Multi-collection union fixture: two 1024 collections, distances interleave.
        embedder1024.register("tenant isolation policy alpha document", 1.0f, 0.0f);
        embedder1024.register("tenant isolation policy beta document",  0.8f, 0.6f);
        embedder1024.register("tenant isolation policy gamma document", 0.6f, 0.8f);
        repo1024.upsertChunks(TENANT_A, COL_MA,
            List.of(MA_C1, MA_C2),
            List.of("tenant isolation policy alpha document",
                    "tenant isolation policy gamma document"),
            List.of(Map.of(), Map.of()));
        repo1024.upsertChunks(TENANT_A, COL_MB,
            List.of(MB_C1),
            List.of("tenant isolation policy beta document"),
            List.of(Map.of()));

        // where-filter fixture on the 768 table.
        embedder768.register(Q, 1.0f, 0.0f);
        embedder768.register("tenant isolation policy for java services",   1.0f, 0.0f);
        embedder768.register("tenant isolation policy for python services", 0.6f, 0.8f);
        repo768.upsertChunks(TENANT_A, COL_WH,
            List.of(WH_C1, WH_C2),
            List.of("tenant isolation policy for java services",
                    "tenant isolation policy for python services"),
            List.of(Map.of("lang", "java"), Map.of("lang", "py")));

        // 384 dispatch fixture (per-dim DDL is byte-identical; the hybrid query must
        // behave identically on every chunks_<dim> table).
        embedder384.register(Q, 1.0f, 0.0f);
        embedder384.register("tenant isolation policy small model",   1.0f, 0.0f);
        embedder384.register("tenant isolation policy variant",       0.8f, 0.6f);
        embedder384.register("cooking carbonara again",               0.995f, 0.0998749f);
        repo384.upsertChunks(TENANT_A, COL_384H,
            List.of(M384_C1, M384_C2, M384_C3),
            List.of("tenant isolation policy small model",
                    "tenant isolation policy variant",
                    "cooking carbonara again"),
            List.of(Map.of(), Map.of(), Map.of()));
    }

    private static List<String> ids(List<Map<String, Object>> rows) {
        return rows.stream().map(r -> (String) r.get("id")).toList();
    }

    // ---------------------------------------------------------------------------
    // Contract 1: fused ordering — text gate, then vector rank (exact)
    // ---------------------------------------------------------------------------

    @Test
    void hybridSearch_fusedOrdering_textGateThenVectorRank() {
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_A, Q, List.of(COL_HY), 10, null);

        assertThat(ids(rows))
            .as("fused = FTS candidates {c1,c2,c3,c6} ranked by cosine distance "
                + "(0.0, 0.2, 0.4, 2.0) — exact ordering, exact membership")
            .containsExactlyElementsOf(FUSED_EXPECTED);
    }

    @Test
    void hybridSearch_excludesVectorCloseRowWithNoTextSignal() {
        // Differentiation proof: plain vector search DOES surface hyb-c4 (distance
        // 0.005, second-nearest) — the GREEN P2 path establishes the row is there
        // and vector-reachable. Hybrid must exclude it: no text signal, no row.
        List<Map<String, Object>> vectorOnly =
            repo1024.search(TENANT_A, Q, List.of(COL_HY), 10, null);
        assertThat(ids(vectorOnly))
            .as("precondition: vector-only search surfaces the text-unrelated row")
            .contains(HYB_C4);

        List<Map<String, Object>> fused =
            repo1024.hybridSearch(TENANT_A, Q, List.of(COL_HY), 10, null);
        assertThat(ids(fused))
            .as("a vector-close row with no text signal must NEVER appear in hybrid results")
            .doesNotContain(HYB_C4, HYB_C5);
    }

    // ---------------------------------------------------------------------------
    // Contract 2: non-vacuity — both signals are load-bearing (conexus xr7.8.7 pattern)
    // ---------------------------------------------------------------------------

    @Test
    void hybridSearch_orderingDiffersFromChashOrder_vectorSignalReal() {
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_A, Q, List.of(COL_HY), 10, null);

        List<String> fusedOrder = ids(rows);
        List<String> chashOrder = fusedOrder.stream().sorted().toList();

        assertThat(fusedOrder)
            .as("fused ordering must differ from candidate chash ordering — otherwise the "
                + "vector signal has no effect and the fusion assertion is vacuous")
            .isNotEqualTo(chashOrder);
    }

    @Test
    void hybridSearch_vectorOnlyOrderingDiffers_textGateReal() {
        List<Map<String, Object>> vectorOnly =
            repo1024.search(TENANT_A, Q, List.of(COL_HY), 4, null);
        List<Map<String, Object>> fused =
            repo1024.hybridSearch(TENANT_A, Q, List.of(COL_HY), 4, null);

        assertThat(ids(fused))
            .as("vector-only top-4 [c1,c4,c3,c2] must differ from fused top-4 "
                + "[c1,c3,c2,c6] — otherwise the text gate has no effect")
            .isNotEqualTo(ids(vectorOnly));
    }

    // ---------------------------------------------------------------------------
    // Contract 3: pg_trgm leg — typo queries still reach trigram-similar rows
    // ---------------------------------------------------------------------------

    @Test
    void hybridSearch_trgmRescue_typoQueryReturnsCandidates() {
        // 'isolaton' matches no FTS lexeme anywhere — only pg_trgm can gate these
        // rows in. Same candidates, same vector ranking as the clean query.
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_A, Q_TYPO, List.of(COL_HY), 10, null);

        assertThat(ids(rows))
            .as("typo query must trigram-rescue the candidate rows (FTS leg matches "
                + "nothing); ranking stays vector-distance ascending")
            .containsExactlyElementsOf(FUSED_EXPECTED);
    }

    @Test
    void hybridSearch_noTextSignal_returnsEmpty_noVectorFallback() {
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_A, Q_JUNK, List.of(COL_HY), 10, null);

        assertThat(rows)
            .as("zero text candidates returns empty — hybrid must NOT silently fall "
                + "back to vector-only (that is search()'s job)")
            .isEmpty();
    }

    // ---------------------------------------------------------------------------
    // Contract 4: search() envelope carried over
    // ---------------------------------------------------------------------------

    @Test
    void hybridSearch_multiCollection_singleRankedUnion() {
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_A, Q, List.of(COL_MA, COL_MB), 10, null);

        assertThat(ids(rows))
            .as("multi-collection hybrid is ONE ranked list interleaved by distance "
                + "(ma-c1 0.0, mb-c1 0.2, ma-c2 0.4), not per-collection blocks")
            .containsExactly(MA_C1, MB_C1, MA_C2);
    }

    @Test
    void hybridSearch_whereFilter_composesWithTextGate() {
        List<Map<String, Object>> rows =
            repo768.hybridSearch(TENANT_A, Q, List.of(COL_WH), 10, Map.of("lang", "py"));

        assertThat(ids(rows))
            .as("metadata where-predicate ANDs with the text gate: wh-c1 matches the "
                + "text gate but not lang=py and must be filtered out")
            .containsExactly(WH_C2);
    }

    @Test
    void hybridSearch_whereOperatorForm_composesWithTextGate() {
        // nexus-05bfd: operator-form on the hybrid path exercises the dual
        // gate+scope builder. wh-c1 matches the text gate and is lang!=py;
        // {lang:{$ne:py}} must return it (the inverse of the plain-equality test).
        List<Map<String, Object>> rows =
            repo768.hybridSearch(TENANT_A, Q, List.of(COL_WH), 10, Map.of("lang", Map.of("$ne", "py")));

        assertThat(ids(rows))
            .as("{lang:{$ne:py}} keeps the gate-matching non-py row, drops the py row")
            .containsExactly(WH_C1);
    }

    @Test
    void hybridSearch_respectsNResultsLimit() {
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_A, Q, List.of(COL_HY), 2, null);

        assertThat(ids(rows))
            .as("nResults truncates the ranked fused list — top-2 exactly")
            .containsExactly(HYB_C1, HYB_C3);
    }

    @Test
    void hybridSearch_tenantIsolated_otherTenantGetsNothing() {
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_B, Q, List.of(COL_HY), 10, null);

        assertThat(rows)
            .as("RLS must scope the hybrid query exactly like search(): another tenant "
                + "sees 0 of tenant-a's rows")
            .isEmpty();
    }

    @Test
    void hybridSearch_worksOnChunks384() {
        List<Map<String, Object>> rows =
            repo384.hybridSearch(TENANT_A, Q, List.of(COL_384H), 10, null);

        assertThat(ids(rows))
            .as("hybrid dispatches per-dim exactly like search(): same gate + rank "
                + "behaviour on chunks_384, text-unrelated row excluded")
            .containsExactly(M384_C1, M384_C2);
    }

    @Test
    void hybridSearch_rowShape_matchesSearchContract() {
        List<Map<String, Object>> rows =
            repo1024.hybridSearch(TENANT_A, Q, List.of(COL_HY), 1, null);

        assertThat(rows).hasSize(1);
        Map<String, Object> top = rows.get(0);
        assertThat(top.get("id")).isEqualTo(HYB_C1);
        assertThat(top.get("content")).isEqualTo(T_C1);
        assertThat(top.get("collection")).isEqualTo(COL_HY);
        assertThat(((Number) top.get("distance")).doubleValue())
            .as("hyb-c1 embeds to the query vector — cosine distance exactly 0")
            .isCloseTo(0.0, within(1e-6));
        assertThat(top.get("kind"))
            .as("metadata keys flatten into the row, same as search()")
            .isEqualTo("hy");
    }

    // ---------------------------------------------------------------------------
    // Contract 5: dispatch failure modes (same as search())
    // ---------------------------------------------------------------------------

    @Test
    void hybridSearch_mixedDimensions_failLoud() {
        assertThatThrownBy(() ->
                repo1024.hybridSearch(TENANT_A, Q, List.of(COL_HY, COL_MINI), 10, null))
            .as("a 1024-dim and a 384-dim collection cannot share one query vector — "
                + "fail loud, exactly like search()")
            .isInstanceOf(IllegalArgumentException.class)
            .isNotInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void hybridSearch_unknownModelSegment_failsLoud() {
        assertThatThrownBy(() ->
                repo1024.hybridSearch(TENANT_A, Q, List.of(COL_UNKNOWN), 10, null))
            .as("unknown embedding-model segment must fail loud — never a fallback dim")
            .isInstanceOf(IllegalArgumentException.class)
            .isNotInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void hybridSearch_emptyCollectionsList_returnsEmpty() {
        assertThat(repo1024.hybridSearch(TENANT_A, Q, List.of(), 10, null))
            .as("no collections to search returns empty, same as search()")
            .isEmpty();
    }

    @Test
    void hybridSearch_nullCollections_returnsEmpty() {
        assertThat(repo1024.hybridSearch(TENANT_A, Q, null, 10, null))
            .as("null collections returns empty, same as search()")
            .isEmpty();
    }
}
