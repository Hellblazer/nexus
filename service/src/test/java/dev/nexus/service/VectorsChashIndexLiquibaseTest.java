package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
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

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.1 — chash-only probe indexes on the chunk tables
 * (vectors-003-chash-probe-indexes.xml).
 *
 * <p>The chunks PK is {@code (tenant_id, collection, chash)} with collection
 * leading (vectors-001-baseline.xml), so a chash-only probe — "which
 * collections hold this chash for this tenant" — cannot use the PK. The
 * census, alias-resolution, and (later, RDR-187 step 2) rerouted
 * {@code /v1/chash/*} lookups all issue exactly that probe shape, so each
 * chunk table needs a {@code (tenant_id, chash)} btree.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker. Applies the Liquibase
 * master changelog, seeds each chunk table with rows across two collections
 * (the sibling GraphHopParityTest / CombinedQueryParityTest convention —
 * empty-table EXPLAIN doesn't reliably predict populated-table planner
 * choices) and ANALYZEs, then asserts:
 * <ol>
 *   <li>the {@code (tenant_id, chash)} index exists on all three chunk tables</li>
 *   <li>a chash-only probe on each seeded, analyzed table is served by that
 *       index (EXPLAIN), and answers the multi-collection membership question
 *       correctly (a chash shared by two collections returns both rows)</li>
 *   <li>a second Liquibase update is a clean no-op (no duplicate indexes,
 *       MARK_RAN-safe preconditions)</li>
 * </ol>
 *
 * <p>Fixture scale proves plan shape, not production latency: the
 * populated-store (~255k row) perf comparison against the router is
 * RDR-187 step 2's spike (nexus-piwya.2), by design.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class VectorsChashIndexLiquibaseTest {

    private static final int[] DIMS = {384, 768, 1024};

    private static final String TENANT = "probe-tenant";
    private static final String COLL_A = "docs__probe__test__v1";
    private static final String COLL_B = "code__probe__test__v1";

    /** The probed chash: present in BOTH collections of every chunk table. */
    private static final String SHARED_CHASH = "ab".repeat(32);

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // grants-nexus-svc.xml (runAlways, last in the master changelog) grants
        // to nexus_svc; the role must exist before the changelog runs.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        runLiquibaseUpdate();
        seedChunkRows();
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
    }

    /**
     * Seed each chunk table with rows across two collections and ANALYZE, so
     * the EXPLAIN assertions run against real (fixture-scale) statistics
     * rather than empty-table default heuristics. SHARED_CHASH lands in both
     * collections; eight filler chashes per collection give the planner
     * non-trivial cardinality.
     */
    private void seedChunkRows() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String coll : new String[] {COLL_A, COLL_B}) {
                su.createStatement().execute(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) " +
                    "VALUES ('" + TENANT + "', '" + coll + "') " +
                    "ON CONFLICT (tenant_id, name) DO NOTHING");
            }
            for (int dim : DIMS) {
                int filler = 0;
                for (String coll : new String[] {COLL_A, COLL_B}) {
                    insertChunk(su, dim, coll, SHARED_CHASH);
                    for (int i = 0; i < 8; i++) {
                        insertChunk(su, dim, coll,
                            String.format("%064x", 0x1000 * dim + filler++));
                    }
                }
                su.createStatement().execute("ANALYZE nexus.chunks_" + dim);
            }
        }
    }

    private void insertChunk(Connection su, int dim, String collection, String chashHex)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks_" + dim +
            " (tenant_id, collection, chash, chunk_text, embedding) VALUES " +
            "('" + TENANT + "', '" + collection + "', decode('" + chashHex + "', 'hex'), " +
            "'chunk " + chashHex.substring(0, 8) + "', " + zeroVec(dim) + "::vector)" +
            " ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
    }

    private static String zeroVec(int dim) {
        StringBuilder sb = new StringBuilder("'[1");
        for (int i = 1; i < dim; i++) sb.append(",0");
        return sb.append("]'").toString();
    }

    private void runLiquibaseUpdate() throws Exception {
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(), db)
                .update(new Contexts());
        }
    }

    // ── 1. Index existence + shape ──────────────────────────────────────────

    @Test
    void tenantChashIndexExistsOnAllThreeChunkTables() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (int dim : DIMS) {
                String indexName = "idx_chunks_" + dim + "_tenant_chash";
                try (ResultSet rs = su.createStatement().executeQuery(
                        "SELECT indexdef FROM pg_indexes " +
                        "WHERE schemaname = 'nexus' " +
                        "  AND tablename = 'chunks_" + dim + "' " +
                        "  AND indexname = '" + indexName + "'")) {
                    assertThat(rs.next())
                        .as("index %s must exist on nexus.chunks_%d", indexName, dim)
                        .isTrue();
                    String indexdef = rs.getString("indexdef");
                    assertThat(indexdef)
                        .as("index %s must be a btree on (tenant_id, chash)", indexName)
                        .contains("USING btree (tenant_id, chash)");
                }
            }
        }
    }

    @Test
    void tenantChashIndexIsValid() throws Exception {
        // Plain CREATE INDEX inside the Liquibase transaction guarantees
        // valid-or-absent, but assert it explicitly: an INVALID index (the
        // failure mode of an out-of-band CONCURRENTLY build that the MARK_RAN
        // precondition would then mask forever) serves no queries.
        try (Connection su = pg.createConnection("")) {
            for (int dim : DIMS) {
                String indexName = "idx_chunks_" + dim + "_tenant_chash";
                try (ResultSet rs = su.createStatement().executeQuery(
                        "SELECT i.indisvalid FROM pg_index i " +
                        "JOIN pg_class c ON c.oid = i.indexrelid " +
                        "WHERE c.relname = '" + indexName + "'")) {
                    assertThat(rs.next())
                        .as("pg_index row for %s", indexName)
                        .isTrue();
                    assertThat(rs.getBoolean("indisvalid"))
                        .as("index %s must be VALID", indexName)
                        .isTrue();
                }
            }
        }
    }

    // ── 2. The chash-only probe is served by the index ──────────────────────

    @Test
    void chashOnlyProbeUsesTenantChashIndex() throws Exception {
        // The probe shape the index exists for: tenant + chash, NO collection.
        // The PK (tenant_id, collection, chash) cannot serve it past the
        // tenant_id prefix. Tables are seeded and ANALYZEd (see seedChunkRows),
        // so the plan choice reflects real statistics; enable_seqscan=off
        // removes the tiny-table seqscan tie-break that fixture scale can't
        // avoid. Asserting the NEW index's name (not just any index scan)
        // proves the probe is served by (tenant_id, chash) rather than a PK
        // prefix crawl with a chash filter.
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute("SET enable_seqscan = off");
            for (int dim : DIMS) {
                String indexName = "idx_chunks_" + dim + "_tenant_chash";
                String plan = explain(su,
                    "SELECT collection FROM nexus.chunks_" + dim + " " +
                    "WHERE tenant_id = '" + TENANT + "' " +
                    "  AND chash = decode('" + SHARED_CHASH + "', 'hex')");
                assertThat(plan)
                    .as("chash-only probe on chunks_%d must use %s", dim, indexName)
                    .contains(indexName);
            }
        }
    }

    @Test
    void chashOnlyProbeAnswersMultiCollectionMembership() throws Exception {
        // The question the index exists to answer (RDR-187): which collections
        // hold this chash for this tenant. SHARED_CHASH is seeded into both
        // collections of every chunk table.
        try (Connection su = pg.createConnection("")) {
            for (int dim : DIMS) {
                List<String> collections = new ArrayList<>();
                try (ResultSet rs = su.createStatement().executeQuery(
                        "SELECT collection FROM nexus.chunks_" + dim + " " +
                        "WHERE tenant_id = '" + TENANT + "' " +
                        "  AND chash = decode('" + SHARED_CHASH + "', 'hex') " +
                        "ORDER BY collection")) {
                    while (rs.next()) {
                        collections.add(rs.getString(1));
                    }
                }
                assertThat(collections)
                    .as("chash-only probe on chunks_%d must return both collections", dim)
                    .containsExactly(COLL_B, COLL_A);
            }
        }
    }

    // ── 3. Idempotency: second update is a clean no-op ──────────────────────

    @Test
    void secondLiquibaseUpdateIsCleanNoOp() throws Exception {
        runLiquibaseUpdate();
        try (Connection su = pg.createConnection("")) {
            try (ResultSet rs = su.createStatement().executeQuery(
                    "SELECT COUNT(*) FROM pg_indexes " +
                    "WHERE schemaname = 'nexus' " +
                    "  AND indexname LIKE 'idx_chunks_%_tenant_chash'")) {
                rs.next();
                assertThat(rs.getInt(1))
                    .as("exactly one (tenant_id, chash) index per chunk table")
                    .isEqualTo(3);
            }
        }
    }

    private static String explain(Connection c, String sql) throws Exception {
        List<String> lines = new ArrayList<>();
        try (Statement st = c.createStatement();
             ResultSet rs = st.executeQuery("EXPLAIN " + sql)) {
            while (rs.next()) {
                lines.add(rs.getString(1));
            }
        }
        return String.join("\n", lines);
    }
}
