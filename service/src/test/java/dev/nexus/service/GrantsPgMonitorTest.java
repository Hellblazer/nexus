// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
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
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-hzhgl (RDR-191 Phase 3/4 pre-flight, conexus relay [22411] ASK B) --
 * {@code grants-004-monitor-wal-visibility} (grants-nexus-svc.xml) grants the
 * built-in {@code pg_monitor} role to {@code nexus_svc} so the engine can read
 * {@code pg_ls_waldir()} / {@code pg_stat_*} for WAL-retention visibility
 * during the Phase 4 always-copy trough window. {@code pg_monitor} does NOT
 * provide filesystem free space -- that is a separate, conexus-side Crunchy
 * Bridge API-key path; this changeset is WAL-retention visibility only.
 *
 * <p><b>The non-obvious hazard this suite exists to pin</b> (found while
 * implementing this changeset, not asked for by the originating bead):
 * granting membership in a role requires the GRANTOR to already hold that
 * role WITH ADMIN OPTION, or be superuser (PostgreSQL GRANT semantics). The
 * real migration role, {@code nexus_admin}, is NOCREATEROLE NOSUPERUSER --
 * the same category of hazard as {@code grants-nexus-svc-1}'s bulk-grant
 * ownership hazard (GH #1402, {@link GrantsSvcForeignOwnedRelationTest}) and
 * {@code nexus_diag}'s BYPASSRLS (grants-nexus-diag.xml): a naive
 * {@code GRANT pg_monitor TO nexus_svc} works fine when a SUPERUSER runs the
 * migration (as most of this suite's shared-cluster tests do, via {@link
 * PgContainerHelper#start()}) but fails loud in PRODUCTION, where the
 * migration connects as the non-superuser {@code nexus_admin}. This class
 * therefore opts OUT of shared-cluster reuse (see {@link
 * PgContainerHelper#startDedicated()}'s roster) and reconstructs the real
 * two-role provisioning split, the same recipe {@link
 * GrantsSvcForeignOwnedRelationTest} uses for its own hazard.
 *
 * <p>{@link #grantFailsLoud_withoutAdminOptionPrerequisite()} proves the
 * hazard is real (falsification, not just argued); {@link
 * #grantSucceeds_withAdminOptionPrerequisite_andNexusSvcCanReadWal()} proves
 * the fix -- {@code src/nexus/db/pg_provision.py}'s new unconditional
 * {@code GRANT pg_monitor TO nexus_admin WITH ADMIN OPTION} bootstrap step --
 * actually closes it, and that the resulting {@code nexus_svc} can both hold
 * the role ({@code pg_has_role}) and use it ({@code pg_ls_waldir()} does not
 * throw).
 */
class GrantsPgMonitorTest {

    private static final String ADMIN_ROLE = "nexus_admin_pgmonitor_replay";
    private static final String ADMIN_PASS = "nexus_admin_pgmonitor_replay_pw";
    private static final String SVC_ROLE   = "nexus_svc";
    private static final String SVC_PASS   = "nexus_svc_pass";

    @Test
    void grantFailsLoud_withoutAdminOptionPrerequisite() throws Exception {
        try (var pg = PgContainerHelper.startDedicated();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            provisionAdminAndSvcRoles(su);
            // Deliberately OMIT the pg_provision.py-equivalent bootstrap step
            // (GRANT pg_monitor TO nexus_admin WITH ADMIN OPTION) -- this is
            // the PRE-FIX production shape (a bring-your-own-Postgres DBA who
            // skipped the docs/configuration.md prerequisite, or a local
            // install that predates pg_provision.py's new bootstrap step).

            // round 2 (code-review-expert Important, 2026-08-13): the changeset now
            // catches the raw "permission denied to grant role" failure and re-raises
            // a SELF-EXPLANATORY, named-remedy error -- assert on that text, not just
            // that SOME exception mentioning "pg_monitor" occurred, so this test would
            // fail if the named-remedy wrapper ever regressed back to a bare trace.
            assertThatThrownBy(() -> liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS))
                .as("grants-004-monitor-wal-visibility must fail loud, with a NAMED REMEDY, "
                    + "when nexus_admin lacks ADMIN OPTION on pg_monitor -- PostgreSQL refuses "
                    + "to let a role grant membership in a role it does not itself hold WITH "
                    + "ADMIN OPTION, and a bare permission-denied trace names no fix")
                .isInstanceOf(Exception.class)
                .hasMessageContaining("grants-004-monitor-wal-visibility")
                .hasMessageContaining("ADMIN OPTION")
                .hasMessageContaining("GRANT pg_monitor TO nexus_admin WITH ADMIN OPTION")
                .hasMessageContaining("docs/configuration.md");
        }
    }

    @Test
    void grantSucceeds_withAdminOptionPrerequisite_andNexusSvcCanReadWal() throws Exception {
        try (var pg = PgContainerHelper.startDedicated();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            provisionAdminAndSvcRoles(su);
            // THE FIX: pg_provision.py's bootstrap step, replayed by hand here
            // (a real DBA would run this once per docs/configuration.md).
            exec(su, "GRANT pg_monitor TO " + ADMIN_ROLE + " WITH ADMIN OPTION");

            liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);

            assertThat(hasRoleMembership(su, SVC_ROLE, "pg_monitor"))
                .as("nexus_svc must hold pg_monitor after grants-004-monitor-wal-visibility")
                .isTrue();

            // nexus-v80f2: nexus_svc is NOINHERIT (matches cloud, measured
            // rolinherit=FALSE, conexus relay [22485]) -- pin it here so this
            // class's own fixture role never silently drifts back to INHERIT.
            assertThat(isNoinherit(su, SVC_ROLE))
                .as("nexus_svc must be NOINHERIT")
                .isTrue();

            try (Connection svcConn = DriverManager.getConnection(pg.getJdbcUrl(), SVC_ROLE, SVC_PASS)) {
                // NOINHERIT means pg_monitor's privileges are NOT ambient on a
                // plain session -- falsify the naive expectation before proving
                // the supported path (nexus-bb5c8's decision of record:
                // src/nexus/db/svc_monitor.py issues SET ROLE unconditionally).
                try (Statement st = svcConn.createStatement()) {
                    assertThatThrownBy(() -> st.executeQuery("SELECT count(*) FROM pg_ls_waldir()"))
                        .as("pg_ls_waldir() must be permission-denied on a plain NOINHERIT "
                            + "nexus_svc session -- pg_monitor membership alone is not usable "
                            + "privilege without SET ROLE")
                        .isInstanceOf(Exception.class)
                        .hasMessageContaining("permission denied");
                }

                try (Statement st = svcConn.createStatement()) {
                    st.execute("SET ROLE pg_monitor");
                }
                try (Statement st = svcConn.createStatement();
                     ResultSet rs = st.executeQuery("SELECT count(*) FROM pg_ls_waldir()")) {
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getLong(1))
                        .as("pg_ls_waldir() must be callable as nexus_svc after SET ROLE "
                            + "pg_monitor and return at least the current WAL segment")
                        .isGreaterThanOrEqualTo(1L);
                }
            }
        }
    }

    /**
     * The minimal DBA-provisioning recipe {@link GrantsSvcForeignOwnedRelationTest}
     * already proved sufficient for a full master-changelog run to succeed
     * (Phase A provisioning there) -- reused verbatim so this class's positive
     * case is not a novel, unproven role/grant combination.
     */
    private static void provisionAdminAndSvcRoles(Connection su) throws Exception {
        exec(su, "CREATE ROLE " + ADMIN_ROLE + " LOGIN PASSWORD '" + ADMIN_PASS
            + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
        // NOINHERIT (nexus-v80f2, 2026-08-15): this class opts OUT of shared-cluster
        // reuse (startDedicated()) and reconstructs nexus_svc by hand, bypassing
        // role-001-nexus-svc.xml's own NOINHERIT bootstrap -- so this literal must
        // carry the same attribute or it silently diverges from both role-001 and
        // the production posture (pg_provision.py's _create_roles / _backfill_svc_
        // noinherit), reintroducing the exact drift nexus-bb5c8 found: a real
        // nexus_svc gets pg_monitor's privileges ambiently, this test's fixture
        // role would not have, and pg_ls_waldir() would pass here for a reason
        // production cannot rely on.
        exec(su, "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS
            + "' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT");
        exec(su, "GRANT CREATE ON DATABASE postgres TO " + ADMIN_ROLE);
        exec(su, "GRANT CREATE ON SCHEMA public TO " + ADMIN_ROLE);
        exec(su, "CREATE EXTENSION IF NOT EXISTS vector");
        exec(su, "CREATE EXTENSION IF NOT EXISTS pg_trgm");
    }

    /**
     * Run the master changelog on a DEDICATED connection (Liquibase leaves
     * session state behind on the connection it flips to autoCommit=false).
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

    private static boolean hasRoleMembership(Connection c, String role, String memberOf) throws Exception {
        try (var ps = c.prepareStatement("SELECT pg_has_role(?, ?, 'member')")) {
            ps.setString(1, role);
            ps.setString(2, memberOf);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getBoolean(1);
            }
        }
    }

    /** nexus-v80f2: {@code rolinherit = false} for *role*. */
    private static boolean isNoinherit(Connection c, String role) throws Exception {
        try (var ps = c.prepareStatement(
            "SELECT NOT rolinherit FROM pg_roles WHERE rolname = ?")) {
            ps.setString(1, role);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getBoolean(1);
            }
        }
    }

    private static void exec(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement()) {
            st.execute(sql);
        }
    }
}
