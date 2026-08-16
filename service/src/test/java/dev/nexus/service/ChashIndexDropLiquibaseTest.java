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
 *   <li>the SURVIVORS are intact: {@code idx_chunks_tenant_chash} (RDR-191
 *       Phase 4: the former three per-dim probe indexes are now ONE index on
 *       the unified {@code nexus.chunks} table), and the surviving chash
 *       octet CHECKs ({@code chunks_chash_octet_check}, also unified from
 *       three to one, and the manifest's own, still NOT VALID until
 *       nexus-uu4ue). {@code nexus.chash_alias} itself is NO LONGER a
 *       survivor — RDR-180 called it permanent, but nexus-lgdel.l1 dropped
 *       it once its beneficiary population reached zero; this test now pins
 *       that it is ALSO gone (legacy-001-drop-chash-alias.xml), a second,
 *       independent DROP in a later changelog than this test's own subject.</li>
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
            .as("chash_alias is dropped at nexus-lgdel.l1 (legacy-001-drop-chash-alias.xml) "
                + "— RDR-180 called it permanent, but its beneficiary population reached zero")
            .isZero();
        assertThat(intOf(
            "SELECT count(*) FROM information_schema.tables " +
            "WHERE table_schema = 'staging' AND table_name = 'chash_index'"))
            .as("staging.chash_index (dead-sink landing) is dropped at "
                + "nexus-piwya.11 (rdr187-002)")
            .isZero();
        // RDR-191 Phase 4 (repoint-batch lane F1): the three per-dim probe
        // indexes collapsed into ONE idx_chunks_tenant_chash on the unified
        // nexus.chunks table (vectors-004-unify-chunks.xml step 5).
        assertThat(intOf(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'nexus' " +
            "AND indexname = 'idx_chunks_tenant_chash'"))
            .as("the (tenant_id, chash) probe index serves the reroute — must survive")
            .isEqualTo(1);
        // The surviving octet CHECKs: chunks_chash_octet_check is now ONE
        // unified constraint (was three per-dim), plus the manifest's own.
        assertThat(intOf(
            "SELECT count(*) FROM pg_constraint WHERE conname IN (" +
            "'chunks_chash_octet_check', 'catalog_document_chunks_chash_octet_check')"))
            .isEqualTo(2);
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
