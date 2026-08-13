package dev.nexus.service;

import org.testcontainers.containers.PostgreSQLContainer;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-156 bead nexus-t1hnc.1 — pgvector taxonomy-centroid Liquibase schema test.
 *
 * <p>RDR-191 Phase 4 (bead nexus-jv3ue, taxonomy-007-unify-centroids.xml): the
 * three per-dim centroid tables this test originally verified
 * ({@code nexus.taxonomy_centroids_384/768/1024}) are unified into ONE
 * {@code nexus.taxonomy_centroids} table with three nullable typed
 * {@code embedding_<dim>} columns, mirroring {@code nexus.chunks}'s shape. This
 * test now verifies the UNIFIED table: one PK, one RLS policy, three per-dim
 * HNSW indexes on the three embedding columns.
 *
 * <p>Exact assertions, not existence-only: each HNSW index must use access method
 * {@code hnsw}, opclass {@code vector_cosine_ops}, and carry the
 * {@code m=16, ef_construction=64} reloptions — the centroid-ANN read path
 * (assign_single / compute_assignments parity) depends on cosine distance.
 */
class TaxonomyCentroidSchemaLiquibaseTest {

    private static final String TABLE = "taxonomy_centroids";

    /** dim -> embedding vector dimension; selects embedding_&lt;dim&gt;. */
    private static final List<Integer> DIMS = List.of(384, 768, 1024);

    @Test
    void centroidChangeset_appliesAndCreatesUnifiedTable() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.start()) {

            try (Connection su = pg.createConnection("")) {
                su.createStatement().execute(
                    "DO $$ BEGIN " +
                    "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                    "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                    "  END IF; " +
                    "END $$");

                Database db = DatabaseFactory.getInstance()
                    .findCorrectDatabaseImplementation(new JdbcConnection(su));
                Liquibase lb = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db);
                lb.update(new Contexts());
            }

            try (Connection c = pg.createConnection("")) {
                // Table exists in nexus schema
                ResultSet rs = c.createStatement().executeQuery(
                    "SELECT 1 FROM information_schema.tables " +
                    "WHERE table_schema='nexus' AND table_name='" + TABLE + "'");
                assertThat(rs.next()).as("table nexus." + TABLE + " must exist").isTrue();

                // Exact column set: three nullable embedding_<dim> columns, no chash
                // (taxonomy-007's own DIVERGENCE 1 note: centroids have no content-hash
                // concept at all).
                List<String> cols = columnNames(c, "nexus", TABLE);
                assertThat(cols).as("columns of nexus." + TABLE).containsExactlyInAnyOrder(
                    "tenant_id", "collection", "topic_id",
                    "embedding_384", "embedding_768", "embedding_1024",
                    "label", "doc_count", "created_at");

                // Primary key is (tenant_id, collection, topic_id) in order
                assertThat(primaryKeyColumns(c, "nexus", TABLE))
                    .as("PK of nexus." + TABLE)
                    .containsExactly("tenant_id", "collection", "topic_id");

                // exactly-one-embedding CHECK constraint present
                assertThat(constraintExists(c, "taxonomy_centroids_exactly_one_embedding"))
                    .as("taxonomy_centroids_exactly_one_embedding CHECK must exist").isTrue();

                for (int dim : DIMS) {
                    String column = "embedding_" + dim;
                    String index = "idx_taxonomy_centroids_embedding_" + dim;

                    // embedding_<dim> column is vector(dim)
                    assertThat(vectorDimension(c, "nexus", TABLE, column))
                        .as("dimension of nexus." + TABLE + "." + column).isEqualTo(dim);

                    // HNSW cosine index: access method, opclass, reloptions
                    assertThat(indexAccessMethod(c, index))
                        .as("access method of " + index).isEqualTo("hnsw");
                    assertThat(indexOpclass(c, index))
                        .as("opclass of " + index).isEqualTo("vector_cosine_ops");
                    List<String> reloptions = indexReloptions(c, index);
                    assertThat(reloptions).as("reloptions of " + index)
                        .contains("m=16", "ef_construction=64");
                }

                // RLS enabled + FORCED (once, on the unified table)
                ResultSet rlsRs = c.createStatement().executeQuery(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class " +
                    "WHERE relname='" + TABLE + "' AND relnamespace=" +
                    "(SELECT oid FROM pg_namespace WHERE nspname='nexus')");
                assertThat(rlsRs.next()).as("pg_class entry for " + TABLE).isTrue();
                assertThat(rlsRs.getBoolean("relrowsecurity"))
                    .as("RLS enabled on nexus." + TABLE).isTrue();
                assertThat(rlsRs.getBoolean("relforcerowsecurity"))
                    .as("RLS forced on nexus." + TABLE).isTrue();

                // tenant_isolation policy present (once, on the unified table)
                ResultSet polRs = c.createStatement().executeQuery(
                    "SELECT 1 FROM pg_policies " +
                    "WHERE schemaname='nexus' AND tablename='" + TABLE + "' " +
                    "AND policyname='tenant_isolation'");
                assertThat(polRs.next())
                    .as("tenant_isolation policy on nexus." + TABLE).isTrue();
            }
        }
    }

    private static List<String> columnNames(Connection c, String schema, String table) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT column_name FROM information_schema.columns " +
            "WHERE table_schema='" + schema + "' AND table_name='" + table + "'");
        List<String> cols = new ArrayList<>();
        while (rs.next()) cols.add(rs.getString("column_name"));
        return cols;
    }

    /** pgvector stores the declared dimension in atttypmod (no -4 adjustment for vector). */
    private static int vectorDimension(Connection c, String schema, String table, String column) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT a.atttypmod FROM pg_attribute a " +
            "JOIN pg_class cl ON a.attrelid = cl.oid " +
            "JOIN pg_namespace n ON cl.relnamespace = n.oid " +
            "WHERE n.nspname='" + schema + "' AND cl.relname='" + table + "' " +
            "AND a.attname='" + column + "'");
        assertThat(rs.next()).as("atttypmod row for " + table + "." + column).isTrue();
        return rs.getInt("atttypmod");
    }

    private static List<String> primaryKeyColumns(Connection c, String schema, String table) throws Exception {
        // Ordered by key position via the conkey array.
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT a.attname FROM pg_constraint con " +
            "JOIN pg_class cl ON con.conrelid = cl.oid " +
            "JOIN pg_namespace n ON cl.relnamespace = n.oid " +
            "JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true " +
            "JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = k.attnum " +
            "WHERE con.contype='p' AND n.nspname='" + schema + "' AND cl.relname='" + table + "' " +
            "ORDER BY k.ord");
        List<String> cols = new ArrayList<>();
        while (rs.next()) cols.add(rs.getString("attname"));
        return cols;
    }

    private static boolean constraintExists(Connection c, String conname) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT 1 FROM pg_constraint WHERE conname='" + conname + "'");
        return rs.next();
    }

    private static String indexAccessMethod(Connection c, String index) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT am.amname FROM pg_class i " +
            "JOIN pg_am am ON i.relam = am.oid WHERE i.relname='" + index + "'");
        assertThat(rs.next()).as("index " + index + " must exist").isTrue();
        return rs.getString("amname");
    }

    private static String indexOpclass(Connection c, String index) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT opc.opcname FROM pg_index ix " +
            "JOIN pg_class i ON ix.indexrelid = i.oid " +
            "JOIN pg_opclass opc ON opc.oid = ix.indclass[0] " +
            "WHERE i.relname='" + index + "'");
        assertThat(rs.next()).as("opclass row for " + index).isTrue();
        return rs.getString("opcname");
    }

    private static List<String> indexReloptions(Connection c, String index) throws Exception {
        ResultSet rs = c.createStatement().executeQuery(
            "SELECT unnest(reloptions) AS opt FROM pg_class WHERE relname='" + index + "'");
        List<String> opts = new ArrayList<>();
        while (rs.next()) opts.add(rs.getString("opt"));
        return opts;
    }
}
