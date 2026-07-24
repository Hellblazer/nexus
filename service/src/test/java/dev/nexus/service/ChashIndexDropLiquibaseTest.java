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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-187 bead nexus-piwya.9 — the DROP of {@code nexus.chash_index}, the
 * router remnant of the split-store architecture (and, with it, the
 * 292,230 orphaned pointer rows production measured — they die at the DROP
 * by design, subsuming nexus-uu4ue step 2's DELETE).
 *
 * <p>Applies the full Liquibase master changelog to a fresh store and pins:
 * <ol>
 *   <li>{@code nexus.chash_index} does NOT exist (nor its indexes or octet
 *       CHECK — they die with the table)</li>
 *   <li>{@code staging.chash_index} (the dead-sink landing twin) is ALSO
 *       gone — dropped by rdr187-002 at nexus-piwya.11</li>
 *   <li>the SURVIVORS are intact: {@code nexus.chash_alias} (permanent by
 *       RDR-180 decision), the three
 *       {@code idx_chunks_<dim>_tenant_chash} probe indexes, and the four
 *       surviving chash octet CHECKs (3-of-4 validated narrative: the
 *       chunks three are VALIDATED at rekey; the manifest's stays NOT
 *       VALID until nexus-uu4ue)</li>
 *   <li>a second Liquibase update is a clean no-op (MARK_RAN-safe
 *       preconditions)</li>
 * </ol>
 *
 * <p>Hermetic: Testcontainers pgvector, requires Docker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChashIndexDropLiquibaseTest {

    PostgreSQLContainer<?> pg;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
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
    }

    @AfterAll
    void stopAll() {
        if (pg != null) pg.stop();
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

    private int intOf(String sql) throws Exception {
        try (Connection su = pg.createConnection("");
             ResultSet rs = su.createStatement().executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    @Test
    void routerTableIsGone() throws Exception {
        assertThat(intOf(
            "SELECT count(*) FROM information_schema.tables " +
            "WHERE table_schema = 'nexus' AND table_name = 'chash_index'"))
            .as("nexus.chash_index must not exist — the router is retired (RDR-187)")
            .isZero();
        assertThat(intOf(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'chash_index_chash_octet_check'"))
            .as("the router's octet CHECK dies with the table")
            .isZero();
        assertThat(intOf(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'nexus' " +
            "AND indexname LIKE 'idx_chash_index%'"))
            .as("the router's indexes die with the table")
            .isZero();
    }

    @Test
    void survivorsAreIntact() throws Exception {
        assertThat(intOf(
            "SELECT count(*) FROM information_schema.tables " +
            "WHERE table_schema = 'nexus' AND table_name = 'chash_alias'"))
            .as("chash_alias is PERMANENT (RDR-180) — must survive the DROP")
            .isEqualTo(1);
        assertThat(intOf(
            "SELECT count(*) FROM information_schema.tables " +
            "WHERE table_schema = 'staging' AND table_name = 'chash_index'"))
            .as("staging.chash_index (dead-sink landing) is dropped at "
                + "nexus-piwya.11 (rdr187-002)")
            .isZero();
        assertThat(intOf(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'nexus' " +
            "AND indexname LIKE 'idx_chunks_%_tenant_chash'"))
            .as("the (tenant_id, chash) probe indexes serve the reroute — must survive")
            .isEqualTo(3);
        // The four surviving octet CHECKs (the 3-of-4 VALIDATE narrative:
        // manifest's is validated by nexus-uu4ue after the orphan cleanup).
        assertThat(intOf(
            "SELECT count(*) FROM pg_constraint WHERE conname IN (" +
            "'chunks_384_chash_octet_check', 'chunks_768_chash_octet_check', " +
            "'chunks_1024_chash_octet_check', 'catalog_document_chunks_chash_octet_check')"))
            .isEqualTo(4);
    }

    @Test
    void secondUpdateIsCleanNoOp() throws Exception {
        runLiquibaseUpdate();
        assertThat(intOf(
            "SELECT count(*) FROM information_schema.tables " +
            "WHERE table_schema = 'nexus' AND table_name = 'chash_index'"))
            .isZero();
    }
}
