package dev.nexus.service;

import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
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
 * RDR-152 bead nexus-gmiaf.14 — Taxonomy Liquibase schema smoke test.
 *
 * <p>Verifies that the taxonomy-001-baseline.xml changeset applies cleanly
 * and produces the expected tables, columns, and RLS policies. No FTS
 * verification needed (Store 4 explicitly forbids tsvector/GIN).
 */
class TaxonomySchemaLiquibaseTest {

    @Test
    void taxonomyChangeset_appliesAndCreatesExpectedTables() throws Exception {
        try (EmbeddedPostgres pg = EmbeddedPostgres.builder().start()) {

            try (Connection su = pg.getPostgresDatabase().getConnection()) {
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

            try (Connection c = pg.getPostgresDatabase().getConnection()) {
                // All four tables exist in nexus schema
                for (String table : List.of("topics", "taxonomy_meta",
                                            "topic_assignments", "topic_links")) {
                    ResultSet rs = c.createStatement().executeQuery(
                        "SELECT 1 FROM information_schema.tables " +
                        "WHERE table_schema='nexus' AND table_name='" + table + "'");
                    assertThat(rs.next()).as("table nexus." + table + " must exist").isTrue();
                }

                // topics columns
                List<String> topicCols = columnNames(c, "nexus", "topics");
                assertThat(topicCols).containsAll(List.of(
                    "id", "tenant_id", "label", "parent_id", "collection",
                    "centroid_hash", "doc_count", "created_at", "review_status", "terms"));

                // No tsvector column anywhere (Store 4 contract)
                for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                    List<String> cols = columnNames(c, "nexus", table);
                    cols.forEach(col ->
                        assertThat(col).as("No FTS column in " + table)
                            .doesNotContainIgnoringCase("tsvec")
                            .doesNotContainIgnoringCase("tsv_")
                            .doesNotContainIgnoringCase("_fts"));
                }

                // RLS enabled on all four tables
                for (String table : List.of("topics", "taxonomy_meta", "topic_assignments", "topic_links")) {
                    ResultSet rs = c.createStatement().executeQuery(
                        "SELECT relrowsecurity FROM pg_class " +
                        "WHERE relname='" + table + "' AND relnamespace=" +
                        "(SELECT oid FROM pg_namespace WHERE nspname='nexus')");
                    assertThat(rs.next()).as("pg_class entry for " + table).isTrue();
                    assertThat(rs.getBoolean("relrowsecurity"))
                        .as("RLS must be enabled on nexus." + table).isTrue();
                }

                // taxonomy_meta columns
                List<String> metaCols = columnNames(c, "nexus", "taxonomy_meta");
                assertThat(metaCols).containsAll(List.of(
                    "tenant_id", "collection", "last_discover_doc_count", "last_discover_at"));

                // topic_assignments columns
                List<String> assignCols = columnNames(c, "nexus", "topic_assignments");
                assertThat(assignCols).containsAll(List.of(
                    "tenant_id", "doc_id", "topic_id", "assigned_by",
                    "similarity", "assigned_at", "source_collection"));

                // topic_links columns
                List<String> linkCols = columnNames(c, "nexus", "topic_links");
                assertThat(linkCols).containsAll(List.of(
                    "tenant_id", "from_topic_id", "to_topic_id", "link_count", "link_types"));
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
}
