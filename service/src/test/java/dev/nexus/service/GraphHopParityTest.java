// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import static org.assertj.core.api.Assertions.assertThat;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * RDR-156 P4 follow-on, bead nexus-houg9 — contract suite for the graph-hop combined
 * query function {@code nexus.search_graph_hop_<dim>} (catalog-007, Decision 5
 * "graph-hop + rank").
 *
 * <p>The graph-hop shape retires the {@code query} MCP tool's app-side
 * {@code follow_links} dance: today {@link dev.nexus.service.db.CatalogRepository#graphBFS}
 * does breadth-first traversal app-side, then the caller searches each reachable
 * document's collection and re-joins. catalog-007 folds the whole thing into ONE
 * planner-optimizable SQL function: a {@code WITH RECURSIVE} BFS over
 * {@code nexus.catalog_links} (the typed edge table) collects the reachable doc set,
 * which is then joined to {@code chunks_<dim>} and vector-ranked.
 *
 * <p>Contract pins (RED until catalog-007 + the PgVectorRepository method land):
 * <ul>
 *   <li><strong>Signature</strong> — GROUP 1: query vector is the FIRST argument,
 *       typed {@code vector} (Finding 5a), and the result table carries the matched
 *       chunk's {@code chash} as an output column (audit HIGH: rzqto populates the
 *       RDR-086 {@code chunk_text_hash} from the MATCHED chunk, never a per-doc guess).</li>
 *   <li><strong>Depth-bound BFS</strong> — GROUP 2: depth=1 returns seeds + 1-hop only
 *       (no N+1 leak); depth=2 adds the 2-hop layer. Mirrors graphBFS's
 *       {@code maxDepth} contract.</li>
 *   <li><strong>link_type / direction filters</strong> — GROUP 3: a NULL link_type
 *       follows all edges; a set link_type follows only those; direction in/out/both
 *       matches {@code Catalog.graph}'s default ("both") and graphBFS's dirCond.</li>
 *   <li><strong>Cycle safety</strong> — GROUP 4: a cyclic edge set terminates and the
 *       reachable set is the BFS-visited set (each node once).</li>
 *   <li><strong>Vector rank + tombstone + RLS + per-dim</strong> — GROUPs 5-8, mirroring
 *       {@link CombinedQueryParityTest}.</li>
 *   <li><strong>Parity vs the app-stitch</strong> — GROUP 9: the function equals the
 *       reachable-set-then-rank oracle EXACTLY, ordering included, with a non-vacuity
 *       guard (a wrong impl that ignores the graph would surface the unreachable
 *       vector-closer doc g4).</li>
 *   <li><strong>EXPLAIN</strong> — GROUP 10: the materialize-reached-then-rank shape
 *       engages the HNSW index (the recursive-CTE function itself is non-inlinable —
 *       Function Scan at the call site is ACCEPTED per the audit; what must hold is
 *       that ranking the reached set OUTSIDE the recursive CTE keeps HNSW usable).</li>
 * </ul>
 *
 * <p>Graph fixture (COLL_G, 1024-dim), query probe (1,0), cosine distance = 1 - x:
 * <pre>
 *   doc   embedding   dist   edges
 *   g0    (1.0,0.0)   0.0    --cites-->g1, --relates-->g3   (seed)
 *   g1    (0.8,0.6)   0.2    --cites-->g2
 *   g2    (0.6,0.8)   0.4    --cites-->g1   (cycle g1<->g2)
 *   g3    (0.0,1.0)   1.0
 *   g4    (-1.0,0.0)  2.0    (UNREACHABLE — non-vacuity guard: closest after g0? no, farthest;
 *                             gin is the discriminator instead)
 *   gin   (0.96,0.28) ~0.04  --cites-->g0   (INBOUND to g0)
 * </pre>
 * cites/out/depth1 from g0 → {g0,g1}; depth2 → {g0,g1,g2}; all-types/out/depth1 →
 * {g0,g1,g3}; cites/in/depth1 → {g0,gin}; cites/both/depth1 → {g0,g1,gin}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class GraphHopParityTest {

    // ── Tenant IDs ────────────────────────────────────────────────────────────
    private static final String TENANT_A = "ghp-tenant-a";
    private static final String TENANT_B = "ghp-tenant-b";

    // ── Svc role (NOSUPERUSER, NOBYPASSRLS — subject to FORCE RLS) ───────────
    private static final String SVC_ROLE = "svc_ghp_test";
    private static final String SVC_PASS = "svc_ghp_test_pass";

    // ── Collections (conformant <type>__<owner>__<model>__<version>) ─────────
    private static final String COLL_G     = "knowledge__ghp-graph__voyage-context-3__v1";   // 1024
    private static final String COLL_G_384 = "knowledge__ghp-graph384__minilm-l6-v2-384__v1"; // 384
    private static final String COLL_B     = "knowledge__ghp-b__voyage-context-3__v1";        // 1024, tenant B
    private static final String COLL_EXPLAIN = "knowledge__ghp-explain__voyage-context-3__v1"; // 1024
    private static final int    EXPLAIN_ROWS = 500;

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;

    // ══════════════════════════════════════════════════════════════════════════
    // LIFECYCLE
    // ══════════════════════════════════════════════════════════════════════════

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (String tbl : List.of(
                    "catalog_collections", "catalog_documents", "catalog_document_chunks",
                    "catalog_links", "chunks_384", "chunks_768", "chunks_1024")) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus." + tbl + " TO " + SVC_ROLE);
            }
            su.createStatement().execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        seedFixtures();
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null)    pg.stop();
    }

    private void seedFixtures() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // ----- graph fixture (1024) -----
            insertCollection(su, TENANT_A, COLL_G);
            seedDoc(su, 1024, COLL_G, "g0",  1.0,  0.0);
            seedDoc(su, 1024, COLL_G, "g1",  0.8,  0.6);
            seedDoc(su, 1024, COLL_G, "g2",  0.6,  0.8);
            seedDoc(su, 1024, COLL_G, "g3",  0.0,  1.0);
            seedDoc(su, 1024, COLL_G, "g4", -1.0,  0.0);
            seedDoc(su, 1024, COLL_G, "gin", 0.96, 0.28);
            link(su, TENANT_A, "g0", "g1", "cites");
            link(su, TENANT_A, "g1", "g2", "cites");
            link(su, TENANT_A, "g2", "g1", "cites");   // cycle g1<->g2
            link(su, TENANT_A, "g0", "g3", "relates");
            link(su, TENANT_A, "gin", "g0", "cites");  // inbound to g0
            // g4 has NO edges — unreachable from g0 by any path.

            // ----- graph fixture (384) for per-dim dispatch -----
            insertCollection(su, TENANT_A, COLL_G_384);
            seedDoc(su, 384, COLL_G_384, "h0", 1.0, 0.0);
            seedDoc(su, 384, COLL_G_384, "h1", 0.8, 0.6);
            link(su, TENANT_A, "h0", "h1", "cites");

            // ----- tenant-B fixture (1024) for RLS group -----
            insertCollection(su, TENANT_B, COLL_B);
            seedDoc(su, 1024, COLL_B, "b0", 1.0, 0.0);
            seedDoc(su, 1024, COLL_B, "b1", 0.8, 0.6);
            link(su, TENANT_B, "b0", "b1", "cites");

            // ----- large star fixture for the EXPLAIN group (GROUP 10) -----
            seedExplainFixture(su);

            for (String tbl : List.of("chunks_1024", "catalog_documents",
                    "catalog_document_chunks", "catalog_links")) {
                su.createStatement().execute("ANALYZE nexus." + tbl);
            }
        }
    }

    /**
     * Star graph: seed exseed --cites--> ex1..exN, so cites/out/depth1 reaches all N
     * docs. The volume + the materialize-then-rank shape is what lets the planner pick
     * the HNSW index for the outer vector rank (GROUP 10).
     */
    private void seedExplainFixture(Connection su) throws Exception {
        insertCollection(su, TENANT_A, COLL_EXPLAIN);
        // seed doc
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) " +
            "VALUES ('" + TENANT_A + "', 'exseed', 'Seed', 'exauthor', 2024, 'paper', 'research', '" +
            COLL_EXPLAIN + "')");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) " +
            "SELECT '" + TENANT_A + "', 'ex'||g, 'Doc '||g, 'exauthor', 2024, 'paper', 'research', '" +
            COLL_EXPLAIN + "' FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) " +
            "SELECT '" + TENANT_A + "', 'ex'||g, 0, lpad(g::text, 32, '0'), '" + COLL_EXPLAIN + "' " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) " +
            "SELECT '" + TENANT_A + "', '" + COLL_EXPLAIN + "', lpad(g::text, 32, '0'), 'ex'||g, " +
            "('[' || ((g % 100)::float8 / 100.0) || ',1' || repeat(',0', 1022) || ']')::vector " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
        // edges exseed --cites--> ex1..exN
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) " +
            "SELECT '" + TENANT_A + "', 'exseed', 'ex'||g, 'cites', 'test' " +
            "FROM generate_series(1, " + EXPLAIN_ROWS + ") g");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — signature: query vector first; chash is an output column (audit HIGH)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(10)
    void signature_queryVectorFirst_chashInResult() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT pg_catalog.pg_get_function_arguments(p.oid) AS args, " +
                "       pg_catalog.pg_get_function_result(p.oid)    AS result " +
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace " +
                " WHERE n.nspname = 'nexus' AND p.proname = 'search_graph_hop_1024'");
            assertThat(rs.next())
                .as("nexus.search_graph_hop_1024 must exist (catalog-007)").isTrue();
            String args = rs.getString("args");
            assertThat(args)
                .as("query vector must be the FIRST argument, typed `vector` (Finding 5a)")
                .startsWith("p_query vector");
            assertThat(args)
                .as("graph-hop signature pins seeds[], collections[], link_type, depth, direction, n")
                .contains("p_seeds text[]")
                .contains("p_collections text[]")
                .contains("p_link_type text")
                .contains("p_depth integer")
                .contains("p_direction text")
                .contains("p_n integer");
            assertThat(rs.getString("result"))
                .as("audit HIGH: result table MUST carry the matched chunk's chash so the "
                    + "query() repoint (rzqto) populates RDR-086 chunk_text_hash from the "
                    + "MATCHED chunk, not a per-doc manifest guess")
                .contains("chash text");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — depth-bound BFS (exactly N, no N+1 leak)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(20)
    void depth1_cites_out_seedsPlusOneHopOnly() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 1, "out", 10))
                .as("cites/out/depth1 from g0 → seeds + 1-hop = {g0,g1}, ranked by distance "
                    + "(0.0, 0.2). g2 (2-hop) must NOT leak; g3 (relates) excluded")
                .containsExactly("g0", "g1");
        }
    }

    @Test @Order(21)
    void depth2_cites_out_addsTwoHopLayer() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 2, "out", 10))
                .as("cites/out/depth2 from g0 → {g0,g1,g2} ranked (0.0,0.2,0.4)")
                .containsExactly("g0", "g1", "g2");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — link_type / direction filters
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(30)
    void linkTypeNull_followsAllEdges() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 1024, COLL_G, "g0", null, 1, "out", 10))
                .as("link_type NULL/out/depth1 from g0 → all 1-hop = {g0,g1,g3} "
                    + "ranked (0.0,0.2,1.0); the relates edge to g3 is followed too")
                .containsExactly("g0", "g1", "g3");
        }
    }

    @Test @Order(31)
    void linkTypeFilter_followsOnlyThatType() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 1024, COLL_G, "g0", "relates", 2, "out", 10))
                .as("relates/out/depth2 from g0 → {g0,g3} only (g3 has no further relates "
                    + "edges; the cites chain to g1/g2 is NOT followed)")
                .containsExactly("g0", "g3");
        }
    }

    @Test @Order(32)
    void directionIn_followsInboundEdges() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 1, "in", 10))
                .as("cites/in/depth1 from g0 → {g0,gin} (gin --cites--> g0 is inbound); "
                    + "g1 (outbound) excluded. Ranked (0.0, ~0.04)")
                .containsExactly("g0", "gin");
        }
    }

    @Test @Order(33)
    void directionBoth_followsEitherEnd() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 1, "both", 10))
                .as("cites/both/depth1 from g0 → {g0,gin,g1} ranked (0.0, ~0.04, 0.2)")
                .containsExactly("g0", "gin", "g1");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — cycle safety
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(40)
    void cycle_terminatesAndVisitsEachOnce() throws Exception {
        try (Connection su = pg.createConnection("")) {
            // g1<->g2 is a cycle; depth3 must terminate and the reachable set is the
            // BFS-visited set {g0,g1,g2} — never an infinite loop or duplicate rows.
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 3, "out", 10))
                .as("cites/out/depth3 from g0 across the g1<->g2 cycle → {g0,g1,g2}, "
                    + "terminating, one row per doc")
                .containsExactly("g0", "g1", "g2");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 5 — chash output column correctness
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(50)
    void chashColumn_matchesSeededChunkChash() throws Exception {
        try (Connection su = pg.createConnection("")) {
            ResultSet rs = su.createStatement().executeQuery(
                "SELECT id, chash FROM nexus.search_graph_hop_1024(" +
                queryVecLiteral(1024) + ", ARRAY['g0']::text[], " +
                "ARRAY['" + COLL_G + "']::text[], 'cites', 1, 'out', 10) ORDER BY distance");
            int seen = 0;
            while (rs.next()) {
                String id = rs.getString("id");
                String chash = rs.getString("chash");
                assertThat(chash)
                    .as("the chash column must be the MATCHED chunk's chash for doc " + id)
                    .isEqualTo(validChash(id));
                seen++;
            }
            assertThat(seen).as("expected g0,g1 rows").isEqualTo(2);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 6 — tombstone filtering (both directions, non-vacuous)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(60)
    void tombstone_dropsAndReturns() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 1, "out", 10))
                .as("baseline → {g0,g1}").containsExactly("g0", "g1");

            setDeleted(su, TENANT_A, "g1", true);
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 1, "out", 10))
                .as("tombstoned g1 drops out of graph-hop results (still graph-reachable, "
                    + "but deleted_at IS NULL filters it)")
                .containsExactly("g0");

            setDeleted(su, TENANT_A, "g1", false);
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 1, "out", 10))
                .as("restored g1 returns").containsExactly("g0", "g1");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 7 — RLS isolation (SECURITY INVOKER, caller RLS)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(70)
    void rls_tenantSeesOnlyOwnRows() throws Exception {
        // CONTROL: superuser (BYPASSRLS) sees tenant-B's reachable docs.
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 1024, COLL_B, "b0", "cites", 1, "out", 10))
                .as("CONTROL: superuser sees tenant-B {b0,b1} (foreign rows exist → svc "
                    + "assertions below are non-vacuous)")
                .containsExactly("b0", "b1");
        }
        // svc + GUC=A: cannot traverse into tenant-B at all (catalog_links RLS).
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute(
                "SELECT set_config('nexus.tenant', '" + TENANT_A + "', false)");
            assertThat(callGraph(svc, 1024, COLL_B, "b0", "cites", 1, "out", 10))
                .as("svc GUC=A sees ZERO tenant-B rows — the recursive CTE over "
                    + "catalog_links is also RLS-scoped (SECURITY INVOKER)").isEmpty();
            assertThat(callGraph(svc, 1024, COLL_G, "g0", "cites", 1, "out", 10))
                .as("svc GUC=A still sees its OWN tenant-A graph")
                .containsExactly("g0", "g1");
        }
        // svc + no GUC: nothing.
        try (Connection svc = svcDs.getConnection()) {
            svc.createStatement().execute("RESET nexus.tenant");
            assertThat(callGraph(svc, 1024, COLL_G, "g0", "cites", 1, "out", 10))
                .as("svc with no nexus.tenant GUC sees nothing (RLS matches NULL)")
                .isEmpty();
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 8 — per-dim dispatch (384)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(80)
    void perDim_384_behavesIdentically() throws Exception {
        try (Connection su = pg.createConnection("")) {
            assertThat(callGraph(su, 384, COLL_G_384, "h0", "cites", 1, "out", 10))
                .as("search_graph_hop_384 must exist and behave identically → {h0,h1}")
                .containsExactly("h0", "h1");
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 9 — parity vs the reachable-set-then-rank oracle (non-vacuous)
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(90)
    void parity_equalsReachableThenRankOracle() throws Exception {
        try (Connection su = pg.createConnection("")) {
            List<String> oracle = stitchedGraphOracle(su, 1024, COLL_G, "g0", "cites", 2, "out");
            assertThat(oracle)
                .as("CONTROL: reachable-then-rank oracle for cites/out/depth2 = [g0,g1,g2]")
                .containsExactly("g0", "g1", "g2");
            // Non-vacuity: g4 is vector-present but graph-UNREACHABLE; a broken impl that
            // ignored the graph and ranked the whole collection would surface g4 (and g3,
            // gin). The oracle and the function must BOTH exclude them.
            assertThat(oracle).as("oracle must exclude unreachable g4/g3/gin")
                .doesNotContain("g4", "g3", "gin");
            assertThat(callGraph(su, 1024, COLL_G, "g0", "cites", 2, "out", 10))
                .as("graph-hop function must equal the reachable-then-rank oracle EXACTLY, "
                    + "ordering included")
                .containsExactlyElementsOf(oracle);
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 10 — EXPLAIN: ranking the reached set OUTSIDE the recursive CTE keeps HNSW
    //
    // The recursive-CTE function is non-inlinable (Function Scan at the call site is
    // ACCEPTED per the houg9 audit). What MUST hold is the audit's structural decision:
    // materialize the reachable doc set first, then vector-rank with an HNSW-engaging
    // join — NOT rank inside the recursive CTE. This EXPLAINs that outer shape over a
    // large star graph (reached set = all EXPLAIN_ROWS docs).
    // ══════════════════════════════════════════════════════════════════════════

    @Test @Order(100)
    void explain_reachedThenRank_usesHnswIndex_notSeqScan() throws Exception {
        // The outer shape the function uses once `reached` is materialized: rank the
        // chunks of the reached doc set. Modelled here with the reached set sourced from
        // the (non-recursive) star edges, so the probe vector stays a plan-time literal.
        String inner =
            "SELECT d.tumbler " +
            "  FROM nexus.chunks_1024 c " +
            "  JOIN nexus.catalog_document_chunks m " +
            "    ON m.tenant_id = c.tenant_id AND m.collection = c.collection AND m.chash = c.chash " +
            "  JOIN nexus.catalog_documents d " +
            "    ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id " +
            "  JOIN (SELECT DISTINCT to_tumbler AS tumbler FROM nexus.catalog_links " +
            "         WHERE from_tumbler = 'exseed' AND link_type = 'cites') rd " +
            "    ON rd.tumbler = d.tumbler " +
            " WHERE c.collection = '" + COLL_EXPLAIN + "' AND d.deleted_at IS NULL " +
            " ORDER BY c.embedding <=> " + queryVecLiteral(1024) + " LIMIT 10";
        String plan = explain(inner);
        assertThat(plan)
            .as("materialize-reached-then-rank must use the HNSW index "
                + "idx_chunks_1024_embedding (rank OUTSIDE the recursive CTE keeps the "
                + "probe vector a plan-time literal). Plan was:%n%s", plan)
            .contains("idx_chunks_1024_embedding");
        assertThat(plan)
            .as("the reached-set join must NOT defeat the index into a Seq Scan on "
                + "chunks_1024. Plan was:%n%s", plan)
            .doesNotContain("Seq Scan on chunks_1024");
    }

    // ══════════════════════════════════════════════════════════════════════════
    // CALL HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /** Invoke search_graph_hop_&lt;dim&gt;; returns ids (tumblers) in returned order. */
    private List<String> callGraph(Connection conn, int dim, String collection, String seed,
                                   String linkType, int depth, String direction, int n)
            throws Exception {
        String sql =
            "SELECT id FROM nexus.search_graph_hop_" + dim + "(" +
            queryVecLiteral(dim) + ", " +
            "ARRAY['" + seed + "']::text[], " +
            "ARRAY['" + collection + "']::text[], " +
            sqlText(linkType) + ", " +
            depth + ", " +
            sqlText(direction) + ", " +
            n + ")";
        return runIds(conn, sql);
    }

    private String explain(String inner) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String guc : List.of("enable_seqscan", "enable_bitmapscan",
                    "enable_sort", "enable_hashjoin")) {
                su.createStatement().execute("SET " + guc + " = off");
            }
            ResultSet rs = su.createStatement().executeQuery("EXPLAIN " + inner);
            StringBuilder sb = new StringBuilder();
            while (rs.next()) sb.append(rs.getString(1)).append('\n');
            return sb.toString();
        }
    }

    private static List<String> runIds(Connection conn, String sql) throws Exception {
        List<String> out = new ArrayList<>();
        try (var st = conn.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            while (rs.next()) out.add(rs.getString(1));
        }
        return out;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // STITCHED-PATH ORACLE (the app-side follow_links dance the function retires)
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * Reachable-set-then-rank oracle: a recursive BFS over catalog_links (the explicit
     * traversal {@link dev.nexus.service.db.CatalogRepository#graphBFS} does app-side),
     * then a raw vector rank over the reachable docs' chunks. Tombstone-filtered.
     * Written as the literal SQL the app-stitch is equivalent to, so parity is a real
     * cross-check, not a copy of the function body.
     */
    private List<String> stitchedGraphOracle(Connection conn, int dim, String collection,
                                             String seed, String linkType, int depth,
                                             String direction) throws Exception {
        String dirPred =
            "out".equals(direction)  ? "l.from_tumbler = r.tumbler"
          : "in".equals(direction)   ? "l.to_tumbler = r.tumbler"
          : "(l.from_tumbler = r.tumbler OR l.to_tumbler = r.tumbler)";
        String sql =
            "WITH RECURSIVE reach(tumbler, d) AS (" +
            "    SELECT '" + seed + "', 0 " +
            "  UNION " +
            "    SELECT CASE WHEN l.from_tumbler = r.tumbler THEN l.to_tumbler ELSE l.from_tumbler END, r.d + 1 " +
            "    FROM reach r JOIN nexus.catalog_links l ON " + dirPred + " " +
            "    WHERE r.d < " + depth + " AND l.link_type = " + sqlText(linkType) +
            "), reached AS (SELECT DISTINCT tumbler FROM reach) " +
            "SELECT d.tumbler AS id " +
            "  FROM nexus.chunks_" + dim + " c " +
            "  JOIN nexus.catalog_document_chunks m " +
            "    ON m.tenant_id = c.tenant_id AND m.collection = c.collection AND m.chash = c.chash " +
            "  JOIN nexus.catalog_documents d " +
            "    ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id " +
            "  JOIN reached rd ON rd.tumbler = d.tumbler " +
            " WHERE c.collection = '" + collection + "' AND d.deleted_at IS NULL " +
            " ORDER BY c.embedding <=> " + queryVecLiteral(dim) + " ASC";
        return runIds(conn, sql);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // FIXTURE HELPERS
    // ══════════════════════════════════════════════════════════════════════════

    /** Seed a doc: catalog_documents + manifest + one chunk (2-D unit dir embedding). */
    private void seedDoc(Connection su, int dim, String collection, String tumbler,
                         double x, double y) throws Exception {
        String tenant = tenantFor(collection);
        String chash = validChash(tumbler);
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_documents " +
            "  (tenant_id, tumbler, title, author, year, content_type, corpus, physical_collection) " +
            "VALUES ('" + tenant + "', '" + tumbler + "', 'Doc " + tumbler + "', 'a', 2024, " +
            "'paper', 'research', '" + collection + "') ON CONFLICT (tenant_id, tumbler) DO NOTHING");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) " +
            "VALUES ('" + tenant + "', '" + tumbler + "', 0, '" + chash + "', '" + collection + "') " +
            "ON CONFLICT (tenant_id, doc_id, position) DO NOTHING");
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_" + dim +
            " (tenant_id, collection, chash, chunk_text, embedding) VALUES ('" +
            tenant + "', '" + collection + "', '" + chash + "', '" + tumbler + "', " +
            vec2(dim, x, y) + "::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    private static void link(Connection su, String tenant, String from, String to, String type)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) " +
            "VALUES ('" + tenant + "', '" + from + "', '" + to + "', '" + type + "', 'test') " +
            "ON CONFLICT (tenant_id, from_tumbler, to_tumbler, link_type) DO NOTHING");
    }

    private static String tenantFor(String collection) {
        return COLL_B.equals(collection) ? TENANT_B : TENANT_A;
    }

    private static void insertCollection(Connection su, String tenantId, String name)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('" +
            tenantId + "', '" + name + "') ON CONFLICT (tenant_id, name) DO NOTHING");
    }

    private static void setDeleted(Connection su, String tenantId, String tumbler, boolean deleted)
            throws Exception {
        su.createStatement().execute(
            "UPDATE nexus.catalog_documents SET deleted_at = " +
            (deleted ? "now()" : "NULL") +
            " WHERE tenant_id = '" + tenantId + "' AND tumbler = '" + tumbler + "'");
    }

    // ── value helpers ────────────────────────────────────────────────────────

    private static String queryVecLiteral(int dim) {
        return vec2(dim, 1.0, 0.0) + "::vector";
    }

    private static String vec2(int dim, double x, double y) {
        StringBuilder sb = new StringBuilder("'[");
        sb.append(fmt(x)).append(',').append(fmt(y));
        for (int i = 2; i < dim; i++) sb.append(",0");
        return sb.append("]'").toString();
    }

    private static String fmt(double v) {
        if (v == Math.rint(v)) return Integer.toString((int) v);
        return Double.toString(v);
    }

    private static String sqlText(String v) {
        return v == null ? "NULL" : "'" + v.replace("'", "''") + "'";
    }

    /** Length-32 lowercase-hex chash deterministically derived from seed (catalog-002 CHECK). */
    private static String validChash(String seed) {
        try {
            byte[] h = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(64);
            for (byte b : h) sb.append(String.format("%02x", b));
            return sb.substring(0, 32);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
