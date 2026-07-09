package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-1wjmq — replay of the 2026-07-08 v0.1.33 cloud-deploy incident
 * (conexus-ml1z): catalog-013-2's VALIDATE failed in production because
 * catalog-013-0's normalization DML silently no-op'd — the Liquibase role
 * (nexus_admin) is the table OWNER but has NO BYPASSRLS, and chash-001 put
 * FORCE ROW LEVEL SECURITY on chash_index, so the DELETE/UPDATE saw zero
 * rows while VALIDATE (DDL, never row-filtered) saw the un-normalized
 * legacy 64-char rows. Every other Liquibase test runs as the container
 * superuser (implicit BYPASSRLS), which is exactly why the suite was green
 * while production failed.
 *
 * <p>This test reconstructs the production wall precisely: full changelog
 * applied, then the cloud's pre-fix chash_index state (legacy 64-char rows
 * present, the length CHECK re-added NOT VALID as 013-1 left it, changesets
 * catalog-013-1b/013-2 pending), then Liquibase re-run as a
 * production-shaped role — NOSUPERUSER, NOBYPASSRLS, owner of the nexus
 * tables. The fix changeset (013-1b: FORCE toggled off around the
 * idempotent re-normalization) must let 013-2's VALIDATE pass.
 */
class Catalog013RlsReplayTest {

    private static final String ADMIN_ROLE = "nexus_admin_replay";
    private static final String ADMIN_PASS = "nexus_admin_replay_pw";

    @Test
    void changelogReplaysCleanly_asNonBypassRlsOwner_withLegacy64Rows() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // DBA bootstrap: the grants changeset (runAlways) requires nexus_svc.
            exec(su,
                "DO $$ BEGIN "
                + "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; "
                + "  END IF; "
                + "END $$");

            // Production-shaped migration role — created up front (mirrors
            // the peer tests' ordering; the DBA bootstraps roles before any
            // migration runs in production too).
            exec(su, "CREATE ROLE " + ADMIN_ROLE + " LOGIN PASSWORD '"
                + ADMIN_PASS + "' NOSUPERUSER NOBYPASSRLS");

            // 1. Full changelog as superuser — the state every tenant reached
            //    through normal deploys (constraint present + validated).
            liquibaseUpdate(pg.getJdbcUrl(),
                PgContainerHelper.USERNAME, PgContainerHelper.PASSWORD);

            // 2. Reconstruct the cloud pre-fix state on chash_index:
            //    legacy 64-char rows (the SQLite-era verbatim ETL copies) and
            //    the CHECK exactly as 013-1 left it: present, NOT VALID.
            exec(su, "ALTER TABLE nexus.chash_index "
                + "DROP CONSTRAINT chash_index_chash_len_check");
            // chash_index carries an FK to catalog_collections
            // (tenant_id, physical_collection) — register the parents first.
            for (String[] tc : new String[][] {
                {"t1", "code__x"}, {"t1", "code__y"}, {"t2", "code__z"}}) {
                try (var ps = su.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) "
                    + "VALUES (?, ?) ON CONFLICT DO NOTHING")) {
                    ps.setString(1, tc[0]);
                    ps.setString(2, tc[1]);
                    ps.executeUpdate();
                }
            }
            String p32 = "a".repeat(32);
            // dedupe class 1: a 64-char row whose [:32] collides with an
            // existing 32-char row on the natural key
            seedRow(su, "t1", p32, "code__x");
            seedRow(su, "t1", p32 + "b".repeat(32), "code__x");
            // dedupe class 2: two 64-char rows sharing a [:32] prefix
            String p32c = "c".repeat(32);
            seedRow(su, "t1", p32c + "d".repeat(32), "code__y");
            seedRow(su, "t1", p32c + "e".repeat(32), "code__y");
            // plain legacy row, second tenant (cross-tenant coverage: RLS
            // would hide BOTH tenants from a GUC-less non-bypass role)
            String p32f = "f".repeat(32);
            seedRow(su, "t2", p32f + "0".repeat(32), "code__z");
            exec(su, "ALTER TABLE nexus.chash_index "
                + "ADD CONSTRAINT chash_index_chash_len_check "
                + "CHECK (length(chash) = 32) NOT VALID");
            // Make 013-1b + 013-2 pending, as they are on the failed tenant
            // (013-2 failed = never recorded; 013-1b is new in this release).
            exec(su, "DELETE FROM public.databasechangelog "
                + "WHERE id IN ('catalog-013-1b', 'catalog-013-2')");

            // 3. Make the role production-shaped: OWNER of the nexus/t1
            //    tables (as nexus_admin is in prod), changelog-table access.
            exec(su, "GRANT USAGE, CREATE ON SCHEMA nexus, t1, public TO " + ADMIN_ROLE);
            exec(su,
                "DO $$ DECLARE r record; BEGIN "
                + "  FOR r IN SELECT schemaname, tablename FROM pg_tables "
                + "           WHERE schemaname IN ('nexus', 't1') LOOP "
                + "    EXECUTE format('ALTER TABLE %I.%I OWNER TO " + ADMIN_ROLE + "', "
                + "                   r.schemaname, r.tablename); "
                + "  END LOOP; "
                + "  FOR r IN SELECT schemaname, sequencename FROM pg_sequences "
                + "           WHERE schemaname IN ('nexus', 't1') LOOP "
                + "    EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO " + ADMIN_ROLE + "', "
                + "                   r.schemaname, r.sequencename); "
                + "  END LOOP; "
                + "END $$");
            exec(su, "GRANT ALL ON TABLE public.databasechangelog, "
                + "public.databasechangeloglock TO " + ADMIN_ROLE);

            String url = pg.getJdbcUrl();
            try (Connection admin = DriverManager.getConnection(url, ADMIN_ROLE, ADMIN_PASS)) {
                admin.setAutoCommit(true);

                // 4. Lock in conexus Finding 2 — the incident's diagnostic
                //    trap: with FORCE RLS and no tenant GUC, the non-bypass
                //    OWNER sees ZERO rows on a table that superuser sees 5 in.
                assertThat(count(su, "SELECT count(*) FROM nexus.chash_index"))
                    .as("superuser ground truth").isEqualTo(5);
                assertThat(count(admin, "SELECT count(*) FROM nexus.chash_index"))
                    .as("FORCE RLS hides every row from the non-BYPASSRLS owner "
                        + "— the reason 013-0 no-op'd AND the documented remedy "
                        + "query returned a false all-clear")
                    .isEqualTo(0);

            }

            // 5. THE REPLAY: run Liquibase as the production-shaped role.
            //    Pre-fix (no 013-1b) this reproduces the incident verbatim:
            //    VALIDATE fails on the rows the role cannot see. With
            //    013-1b the run must complete.
            liquibaseUpdate(url, ADMIN_ROLE, ADMIN_PASS);

            // 6. Post-conditions (superuser view = ground truth):
            //    every row normalized to 32, dedupe classes collapsed,
            //    constraint VALIDATED.
            assertThat(count(su,
                "SELECT count(*) FROM nexus.chash_index WHERE length(chash) <> 32"))
                .isEqualTo(0);
            // class 1 collapsed onto the pre-existing 32-char row; class 2
            // kept one of two; plain row truncated: 32a/code__x, 32c/code__y,
            // 32f/code__z = 3 rows.
            assertThat(count(su, "SELECT count(*) FROM nexus.chash_index")).isEqualTo(3);
            assertThat(count(su,
                "SELECT count(*) FROM pg_constraint "
                + "WHERE conname = 'chash_index_chash_len_check' AND convalidated"))
                .as("013-2's VALIDATE must have run and stuck")
                .isEqualTo(1);
            // The FORCE toggle must be restored by 013-1b itself.
            assertThat(count(su,
                "SELECT count(*) FROM pg_class "
                + "WHERE oid = 'nexus.chash_index'::regclass AND relforcerowsecurity"))
                .as("FORCE ROW LEVEL SECURITY restored after the normalization")
                .isEqualTo(1);
        }
    }

    /**
     * Run the changelog on a DEDICATED connection. Liquibase flips its
     * connection to autoCommit=false and leaves the transaction/session
     * state behind — reusing the caller's connection afterwards makes every
     * subsequent statement invisible to other sessions (this test's first
     * two failures: an "uncommitted" CREATE ROLE failing password auth, and
     * uncommitted GRANTs reading as permission-denied). Peer tests use the
     * same dedicated-connection pattern.
     */
    private static void liquibaseUpdate(String url, String user, String pass) throws Exception {
        try (Connection conn = DriverManager.getConnection(url, user, pass)) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(conn)));
            lb.update(new Contexts());
        }
    }

    private static void seedRow(Connection c, String tenant, String chash,
                                String collection) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
            + "VALUES (?, ?, ?, now())")) {
            ps.setString(1, tenant);
            ps.setString(2, chash);
            ps.setString(3, collection);
            ps.executeUpdate();
        }
    }

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private static void exec(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement()) {
            st.execute(sql);
        }
    }
}
