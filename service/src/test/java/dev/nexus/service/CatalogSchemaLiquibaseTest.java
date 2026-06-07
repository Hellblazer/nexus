package dev.nexus.service;

import io.zonky.test.db.postgres.embedded.EmbeddedPostgres;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.ResultSet;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-152 bead nexus-gmiaf.18 — Catalog Liquibase schema apply test.
 *
 * <p>Starts an embedded PG, applies the full master changelog, then verifies
 * that all 6 catalog tables exist in the nexus schema with the expected columns.
 */
class CatalogSchemaLiquibaseTest {

    @Test
    void catalogSchemaAppliesCleanly() throws Exception {
        try (var pg = EmbeddedPostgres.builder().start();
             Connection su = pg.getPostgresDatabase().getConnection()) {

            // Apply full master changelog (includes catalog-001-baseline.xml)
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        // Re-open a fresh connection after changelog committed to verify schema
        try (var pg = EmbeddedPostgres.builder().start();
             Connection su = pg.getPostgresDatabase().getConnection()) {

            // Apply
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());

            // Verify all 6 catalog tables exist in nexus schema
            for (String table : new String[]{
                "catalog_owners", "catalog_documents", "catalog_links",
                "catalog_document_chunks", "catalog_collections", "catalog_meta"}) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT to_regclass('nexus." + table + "')");
                rs.next();
                assertThat(rs.getString(1))
                    .as("table nexus." + table + " should exist after Liquibase")
                    .isEqualTo("nexus." + table);
            }

            // Spot-check catalog_documents has fts_vector column
            ResultSet ftsCheck = su.createStatement().executeQuery(
                "SELECT column_name FROM information_schema.columns " +
                "WHERE table_schema='nexus' AND table_name='catalog_documents' " +
                "AND column_name='fts_vector'");
            assertThat(ftsCheck.next())
                .as("catalog_documents.fts_vector column should exist")
                .isTrue();

            // Spot-check catalog_links has BIGSERIAL id
            ResultSet idCheck = su.createStatement().executeQuery(
                "SELECT column_name, data_type FROM information_schema.columns " +
                "WHERE table_schema='nexus' AND table_name='catalog_links' " +
                "AND column_name='id'");
            assertThat(idCheck.next())
                .as("catalog_links.id column should exist")
                .isTrue();

            // Spot-check catalog_links UNIQUE constraint
            ResultSet uqCheck = su.createStatement().executeQuery(
                "SELECT constraint_name FROM information_schema.table_constraints " +
                "WHERE table_schema='nexus' AND table_name='catalog_links' " +
                "AND constraint_type='UNIQUE'");
            assertThat(uqCheck.next())
                .as("catalog_links UNIQUE constraint should exist")
                .isTrue();

            // Spot-check RLS is enabled on catalog_documents
            ResultSet rlsCheck = su.createStatement().executeQuery(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class " +
                "JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace " +
                "WHERE nspname='nexus' AND relname='catalog_documents'");
            assertThat(rlsCheck.next()).isTrue();
            assertThat(rlsCheck.getBoolean("relrowsecurity"))
                .as("RLS ENABLE on catalog_documents")
                .isTrue();
            assertThat(rlsCheck.getBoolean("relforcerowsecurity"))
                .as("RLS FORCE on catalog_documents")
                .isTrue();
        }
    }
}
