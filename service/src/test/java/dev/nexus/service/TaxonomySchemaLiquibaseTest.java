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
 * RDR-152 bead nexus-gmiaf.14 — Taxonomy Liquibase schema smoke test.
 *
 * <p>Verifies that the taxonomy-001-baseline.xml changeset applies cleanly
 * and produces the expected tables, columns, and RLS policies. No FTS
 * verification needed (Store 4 explicitly forbids tsvector/GIN).
 */
class TaxonomySchemaLiquibaseTest {

    @Test
    void taxonomyChangeset_appliesAndCreatesExpectedTables() throws Exception {
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

    /**
     * RDR-154 P0 (bead nexus-i7ivk): taxonomy-003 doc_count trigger changeset.
     * Asserts the two recompute functions exist and are SECURITY INVOKER
     * (prosecdef=false), both statement-level triggers exist on
     * topic_assignments, and the trigger-maintained COMMENT is recorded on
     * topics.doc_count.
     */
    @Test
    void docCountTrigger_functionsTriggersAndComment() throws Exception {
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
                // Both recompute functions exist and are SECURITY INVOKER (prosecdef=false).
                ResultSet fns = c.createStatement().executeQuery(
                    "SELECT p.proname, p.prosecdef FROM pg_proc p " +
                    "JOIN pg_namespace n ON n.oid = p.pronamespace " +
                    "WHERE n.nspname = 'nexus' AND p.proname IN " +
                    "('topics_doc_count_recount_ins','topics_doc_count_recount_del') " +
                    "ORDER BY p.proname");
                List<String> invokerFns = new ArrayList<>();
                while (fns.next()) {
                    assertThat(fns.getBoolean("prosecdef"))
                        .as("function %s MUST be SECURITY INVOKER (prosecdef=false)",
                            fns.getString("proname"))
                        .isFalse();
                    invokerFns.add(fns.getString("proname"));
                }
                assertThat(invokerFns).containsExactly(
                    "topics_doc_count_recount_del", "topics_doc_count_recount_ins");

                // Both statement-level triggers exist on nexus.topic_assignments.
                ResultSet trg = c.createStatement().executeQuery(
                    "SELECT t.tgname FROM pg_trigger t " +
                    "JOIN pg_class cl ON cl.oid = t.tgrelid " +
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace " +
                    "WHERE n.nspname = 'nexus' AND cl.relname = 'topic_assignments' " +
                    "AND NOT t.tgisinternal ORDER BY t.tgname");
                List<String> triggers = new ArrayList<>();
                while (trg.next()) triggers.add(trg.getString("tgname"));
                assertThat(triggers).contains(
                    "trg_topic_assignments_doc_count_del",
                    "trg_topic_assignments_doc_count_ins");

                // Trigger-maintained COMMENT recorded on topics.doc_count.
                ResultSet cmt = c.createStatement().executeQuery(
                    "SELECT pgd.description FROM pg_description pgd " +
                    "JOIN pg_class cl ON cl.oid = pgd.objoid " +
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace " +
                    "JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = pgd.objsubid " +
                    "WHERE n.nspname = 'nexus' AND cl.relname = 'topics' " +
                    "AND a.attname = 'doc_count'");
                assertThat(cmt.next()).as("doc_count must carry a COMMENT").isTrue();
                assertThat(cmt.getString("description")).contains("Trigger-maintained");
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
