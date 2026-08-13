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
 * RDR-187 bead nexus-piwya.1 — chash-only probe index on the chunks table
 * (vectors-003-chash-probe-indexes.xml originally; RDR-191 Phase 4
 * (vectors-004-unify-chunks.xml) later collapses the three per-dim indexes
 * this changeset created into ONE {@code idx_chunks_tenant_chash} on the
 * unified {@code nexus.chunks} table — this test targets that unified,
 * post-RDR-191 shape).
 *
 * <p>The chunks PK is {@code (tenant_id, collection, chash)} with collection
 * leading, so a chash-only probe — "which collections hold this chash for
 * this tenant" — cannot use the PK. The census, alias-resolution, and (later,
 * RDR-187 step 2) rerouted {@code /v1/chash/*} lookups all issue exactly that
 * probe shape, so nexus.chunks needs a {@code (tenant_id, chash)} btree.
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker. Applies the Liquibase
 * master changelog, seeds the unified chunks table with rows spanning all
 * three typed embedding columns across two collections (the sibling
 * GraphHopParityTest / CombinedQueryParityTest convention — empty-table
 * EXPLAIN doesn't reliably predict populated-table planner choices) and
 * ANALYZEs, then asserts:
 * <ol>
 *   <li>the ONE {@code (tenant_id, chash)} index exists on nexus.chunks</li>
 *   <li>a chash-only probe, run once per embedding dim (RDR-191: the unified
 *       PK has no dim column, so each dim's probed row uses its OWN chash —
 *       see {@link #sharedChashForDim}), is served by that index (EXPLAIN),
 *       and answers the multi-collection membership question correctly (a
 *       chash shared by two collections returns both rows) — the contract is
 *       dim-agnostic: the same single index must serve every embedding
 *       column</li>
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

    /**
     * The probed chash for a given dim: present in BOTH collections of the
     * unified nexus.chunks table, with that dim's embedding column populated.
     * RDR-191 unify: the PK is (tenant_id, collection, chash) with NO dim
     * component, so a single chash value reused across dims within the same
     * (tenant, collection) would collide (the N7 cross-shard PK collision
     * guard vectors-004-unify-chunks.xml's own migration guards against) —
     * each dim therefore gets its own distinct probed chash.
     */
    private static String sharedChashForDim(int dim) {
        return switch (dim) {
            case 384  -> "38".repeat(32);
            case 768  -> "76".repeat(32);
            case 1024 -> "a0".repeat(32);
            default   -> throw new IllegalArgumentException("unsupported dim " + dim);
        };
    }

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
     * Seed the unified nexus.chunks table with rows spanning all three
     * embedding dims across two collections and ANALYZE once, so the EXPLAIN
     * assertions run against real (fixture-scale) statistics rather than
     * empty-table default heuristics. Each dim's {@link #sharedChashForDim}
     * value lands in both collections (with that dim's embedding column
     * populated, the other two NULL — the exactly_one_embedding CHECK);
     * eight filler chashes per collection per dim give the planner
     * non-trivial cardinality. The {@code 0x1000 * dim} offset keeps filler
     * chashes from colliding across dims within a collection.
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
                    insertChunk(su, dim, coll, sharedChashForDim(dim));
                    for (int i = 0; i < 8; i++) {
                        insertChunk(su, dim, coll,
                            String.format("%064x", 0x1000 * dim + filler++));
                    }
                }
            }
            su.createStatement().execute("ANALYZE nexus.chunks");
        }
    }

    private void insertChunk(Connection su, int dim, String collection, String chashHex)
            throws Exception {
        su.createStatement().execute(
            "INSERT INTO nexus.chunks" +
            " (tenant_id, collection, chash, chunk_text, embedding_" + dim + ") VALUES " +
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
    void tenantChashIndexExistsOnChunksTable() throws Exception {
        try (Connection su = pg.createConnection("")) {
            String indexName = "idx_chunks_tenant_chash";
            try (ResultSet rs = su.createStatement().executeQuery(
                    "SELECT indexdef FROM pg_indexes " +
                    "WHERE schemaname = 'nexus' " +
                    "  AND tablename = 'chunks' " +
                    "  AND indexname = '" + indexName + "'")) {
                assertThat(rs.next())
                    .as("index %s must exist on nexus.chunks", indexName)
                    .isTrue();
                String indexdef = rs.getString("indexdef");
                assertThat(indexdef)
                    .as("index %s must be a btree on (tenant_id, chash)", indexName)
                    .contains("USING btree (tenant_id, chash)");
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
            String indexName = "idx_chunks_tenant_chash";
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

    // ── 2. The chash-only probe is served by the index ──────────────────────

    @Test
    void chashOnlyProbeUsesTenantChashIndex() throws Exception {
        // The probe shape the index exists for: tenant + chash, NO collection.
        // The PK (tenant_id, collection, chash) cannot serve it past the
        // tenant_id prefix. The unified table is seeded and ANALYZEd once (see
        // seedChunkRows), so the plan choice reflects real statistics;
        // enable_seqscan=off removes the tiny-table seqscan tie-break that
        // fixture scale can't avoid. Asserting the index's name (not just any
        // index scan) proves the probe is served by (tenant_id, chash) rather
        // than a PK prefix crawl with a chash filter. Run once per dim (each
        // dim's row uses its own chash, per sharedChashForDim) to prove the
        // SAME single index serves every embedding column — the contract is
        // dim-agnostic now.
        try (Connection su = pg.createConnection("")) {
            su.createStatement().execute("SET enable_seqscan = off");
            String indexName = "idx_chunks_tenant_chash";
            for (int dim : DIMS) {
                String plan = explain(su,
                    "SELECT collection FROM nexus.chunks " +
                    "WHERE tenant_id = '" + TENANT + "' " +
                    "  AND chash = decode('" + sharedChashForDim(dim) + "', 'hex')");
                assertThat(plan)
                    .as("chash-only probe (dim %d row) on nexus.chunks must use %s", dim, indexName)
                    .contains(indexName);
            }
        }
    }

    @Test
    void chashOnlyProbeAnswersMultiCollectionMembership() throws Exception {
        // The question the index exists to answer (RDR-187): which collections
        // hold this chash for this tenant. Each dim's sharedChashForDim value
        // is seeded into both collections of the unified table (that dim's
        // embedding column populated); run once per dim to prove the
        // dim-agnostic contract holds for every embedding column.
        try (Connection su = pg.createConnection("")) {
            for (int dim : DIMS) {
                List<String> collections = new ArrayList<>();
                try (ResultSet rs = su.createStatement().executeQuery(
                        "SELECT collection FROM nexus.chunks " +
                        "WHERE tenant_id = '" + TENANT + "' " +
                        "  AND chash = decode('" + sharedChashForDim(dim) + "', 'hex') " +
                        "ORDER BY collection")) {
                    while (rs.next()) {
                        collections.add(rs.getString(1));
                    }
                }
                assertThat(collections)
                    .as("chash-only probe (dim %d row) on nexus.chunks must return both collections", dim)
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
                    "  AND indexname = 'idx_chunks_tenant_chash'")) {
                rs.next();
                assertThat(rs.getInt(1))
                    .as("exactly one (tenant_id, chash) index on the unified nexus.chunks table")
                    .isEqualTo(1);
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
