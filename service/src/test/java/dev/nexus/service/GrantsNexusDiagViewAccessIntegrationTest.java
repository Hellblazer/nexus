// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * bead nexus-lhuhe (P1) — the privilege-proof companion to
 * {@code SchemaRollbackRoundTripIntegrationTest}'s
 * {@code eraTransitionRevokesTableSelectWithoutGrowingTheChangelog}
 * (introspection via {@code information_schema.role_table_grants}). This
 * test connects AS {@code nexus_diag} and actually runs the SELECTs a real
 * diagnostic session would — the only thing that proves the grants are
 * USABLE, not merely present in the catalog.
 *
 * <p><strong>The bug this falsifies (fork-verified, RDR-194 continuation).
 * </strong> After a full changelog walk, {@code nexus_diag} held ZERO
 * grants: {@code taxonomy-011-doc-id-bytea.xml} (included BEFORE
 * {@code grants-nexus-diag.xml} in {@code db.changelog-master.xml}) creates
 * {@code nexus.diag_chash_conformance} (WITH {@code security_invoker=true})
 * and grants {@code nexus_diag} SELECT on it via {@code taxonomy-011-8}, but
 * {@code grants-nexus-diag-2}'s later per-relation REVOKE loop (over every
 * relation {@code nexus_admin} owns — which by then includes the view -8
 * just (re)created THIS SAME BOOT) strips that grant again, and never
 * granted the underlying tables to begin with (the legacy branch,
 * {@code grants-nexus-diag-1}, skipped because the view already existed).
 * {@code security_invoker=true} means the view ALSO needs the invoking
 * role's own direct SELECT on every table it reads internally — so even a
 * surviving view-only grant would not be enough. Confirmed against the
 * pre-fix tree: this test failed with {@code permission denied for view
 * nexus.diag_chash_conformance}.
 *
 * <p>Fix lives in {@code grants-nexus-diag-3} (the changeset that runs LAST
 * among the runAlways grants blocks every boot): it re-grants SELECT on the
 * view plus the five {@code CHASH_BEARING_TABLES}
 * (src/nexus/db/chash_tables.py) every boot, making the grants the final
 * word regardless of what -1/-2/taxonomy-011-8 did earlier in the same
 * walk.
 */
class GrantsNexusDiagViewAccessIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_diagview_replay";
    private static final String ADMIN_PASS = "nexus_admin_diagview_replay_pw";
    private static final String DIAG_ROLE = "nexus_diag";
    private static final String DIAG_PASS = "nexus_diag_replay_pw";

    /** The exact CHASH_BEARING_TABLES set (src/nexus/db/chash_tables.py). */
    private static final List<String> UNDERLYING_TABLES = List.of(
        "nexus.chunks",
        "nexus.catalog_document_chunks",
        "nexus.topic_assignments",
        "nexus.frecency",
        "nexus.relevance_log");

    @Test
    void nexusDiagCanSelectTheDiagViewAndEveryUnderlyingTable() throws Exception {
        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // Canonical two-phase provisioning split (mirrors
            // GrantsSvcForeignOwnedRelationTest / SchemaMigratorIntegrationTest):
            // a plain non-superuser admin role owns every schema object it
            // creates, matching production.
            exec(su, "CREATE ROLE " + ADMIN_ROLE + " LOGIN PASSWORD '" + ADMIN_PASS
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
            exec(su, "GRANT CREATE ON DATABASE postgres TO " + ADMIN_ROLE);
            exec(su, "GRANT CREATE ON SCHEMA public TO " + ADMIN_ROLE);
            exec(su, "GRANT pg_monitor TO " + ADMIN_ROLE + " WITH ADMIN OPTION");
            // nexus_svc is superuser-created too (matches dbaBootstrap /
            // GrantsSvcForeignOwnedRelationTest) -- role-001-nexus-svc.xml's
            // own CREATE ROLE is a clean-skip when it already exists; the
            // admin role here never gets CREATEROLE, matching production.
            exec(su, "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' "
                + "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
            exec(su, "CREATE EXTENSION IF NOT EXISTS vector");
            exec(su, "CREATE EXTENSION IF NOT EXISTS pg_trgm");

            // nexus_diag is superuser-created in production (BYPASSRLS
            // requires superuser, nexus-vounk) — never by Liquibase.
            exec(su, "CREATE ROLE " + DIAG_ROLE + " LOGIN PASSWORD '" + DIAG_PASS
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS");

            // Two boots: the first lands directly in view era (taxonomy-011-8
            // self-heals the view before grants-nexus-diag-1 ever runs in the
            // SAME walk); the second reproduces the steady-state reboot a
            // real cluster experiences and is where the pre-fix bug actually
            // bites (grants-nexus-diag-2's REVOKE loop re-fires every boot).
            liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
            liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);

            try (Connection diag = DriverManager.getConnection(
                    pg.getJdbcUrl(), DIAG_ROLE, DIAG_PASS)) {
                assertThatCode(() -> count(diag,
                        "SELECT count(*) FROM nexus.diag_chash_conformance"))
                    .as("nexus_diag must be able to SELECT the diag view after a full "
                        + "changelog walk — this is the exact query nx doctor's "
                        + "chash-poison check and the forensics topic run")
                    .doesNotThrowAnyException();

                List<String> denied = new ArrayList<>();
                for (String table : UNDERLYING_TABLES) {
                    try {
                        count(diag, "SELECT count(*) FROM " + table);
                    } catch (Exception e) {
                        denied.add(table + ": " + e.getMessage());
                    }
                }
                assertThat(denied)
                    .as("nexus_diag must hold direct SELECT on every table "
                        + "nexus.diag_chash_conformance reads internally — the view is "
                        + "WITH (security_invoker = true), so Postgres checks the "
                        + "INVOKING role's own table privileges for each underlying "
                        + "relation, not the view owner's")
                    .isEmpty();
            }
        }
    }

    /**
     * Run the changelog on a DEDICATED connection (Liquibase leaves session
     * state behind on the connection it flips to autoCommit=false). Same
     * idiom as {@code GrantsSvcForeignOwnedRelationTest#liquibaseUpdate}.
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
