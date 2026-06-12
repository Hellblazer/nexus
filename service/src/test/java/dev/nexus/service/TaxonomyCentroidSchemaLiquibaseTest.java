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
 * <p>Verifies that taxonomy-002-centroids.xml applies cleanly and produces the
 * three per-dim centroid tables (mirroring the chunks_384/768/1024 convention)
 * with the exact column set, primary key, cosine HNSW index, and RLS policy.
 *
 * <p>Exact assertions, not existence-only: the HNSW index must use access method
 * {@code hnsw}, opclass {@code vector_cosine_ops}, and carry the
 * {@code m=16, ef_construction=64} reloptions — the centroid-ANN read path
 * (assign_single / compute_assignments parity) depends on cosine distance.
 */
class TaxonomyCentroidSchemaLiquibaseTest {

    /** dim -> embedding vector dimension; mirrors chunks_&lt;dim&gt;. */
    private static final List<Integer> DIMS = List.of(384, 768, 1024);

    @Test
    void centroidChangeset_appliesAndCreatesPerDimTables() throws Exception {
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
                for (int dim : DIMS) {
                    String table = "taxonomy_centroids_" + dim;
                    String index = "idx_taxonomy_centroids_" + dim + "_embedding";

                    // Table exists in nexus schema
                    ResultSet rs = c.createStatement().executeQuery(
                        "SELECT 1 FROM information_schema.tables " +
                        "WHERE table_schema='nexus' AND table_name='" + table + "'");
                    assertThat(rs.next()).as("table nexus." + table + " must exist").isTrue();

                    // Exact column set
                    List<String> cols = columnNames(c, "nexus", table);
                    assertThat(cols).as("columns of nexus." + table).containsExactlyInAnyOrder(
                        "tenant_id", "collection", "topic_id", "embedding",
                        "label", "doc_count", "created_at");

                    // embedding column is vector(dim)
                    assertThat(vectorDimension(c, "nexus", table, "embedding"))
                        .as("embedding dimension of nexus." + table).isEqualTo(dim);

                    // Primary key is (tenant_id, collection, topic_id) in order
                    assertThat(primaryKeyColumns(c, "nexus", table))
                        .as("PK of nexus." + table)
                        .containsExactly("tenant_id", "collection", "topic_id");

                    // HNSW cosine index: access method, opclass, reloptions
                    assertThat(indexAccessMethod(c, index))
                        .as("access method of " + index).isEqualTo("hnsw");
                    assertThat(indexOpclass(c, index))
                        .as("opclass of " + index).isEqualTo("vector_cosine_ops");
                    List<String> reloptions = indexReloptions(c, index);
                    assertThat(reloptions).as("reloptions of " + index)
                        .contains("m=16", "ef_construction=64");

                    // RLS enabled + FORCED
                    ResultSet rlsRs = c.createStatement().executeQuery(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class " +
                        "WHERE relname='" + table + "' AND relnamespace=" +
                        "(SELECT oid FROM pg_namespace WHERE nspname='nexus')");
                    assertThat(rlsRs.next()).as("pg_class entry for " + table).isTrue();
                    assertThat(rlsRs.getBoolean("relrowsecurity"))
                        .as("RLS enabled on nexus." + table).isTrue();
                    assertThat(rlsRs.getBoolean("relforcerowsecurity"))
                        .as("RLS forced on nexus." + table).isTrue();

                    // tenant_isolation policy present
                    ResultSet polRs = c.createStatement().executeQuery(
                        "SELECT 1 FROM pg_policies " +
                        "WHERE schemaname='nexus' AND tablename='" + table + "' " +
                        "AND policyname='tenant_isolation'");
                    assertThat(polRs.next())
                        .as("tenant_isolation policy on nexus." + table).isTrue();
                }
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
