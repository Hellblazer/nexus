// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
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
import java.util.TreeSet;

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
 *
 * <p><strong>SECOND falsified bug (2026-08-19, production-confirmed on
 * engine-service-v0.1.82): {@code nexusDiagCanSelectEveryDiagReadableTable}
 * below.</strong> {@code grants-nexus-diag-3}'s re-grant list is scoped
 * EXACTLY to {@code CHASH_BEARING_TABLES} — it was written to fix the view
 * going dark, never meant as the general view-era allowlist. Every OTHER
 * table nexus_admin owns stays revoked forever once
 * {@code grants-nexus-diag-2}'s per-relation REVOKE loop first strips it
 * (confirmed by a live census: 31 of 36 tables in nexus+t1 unreadable by
 * nexus_diag after a full changelog walk). Most of those 31 are correctly
 * denied (real content or credentials); {@code gc_audit} and
 * {@code search_telemetry} are not — both independently VERIFIED
 * content-free at the row/write-path level (see
 * {@link CatalogRepository#NEXUS_DIAG_READABLE_TABLES}'s javadoc for the
 * per-table evidence). {@code hook_failures} shares
 * {@link CatalogRepository#AUDIT_ONLY_TABLES}'s "no content" CLASSIFICATION
 * (nexus-34wrg option (c)) but is deliberately NOT granted here — that
 * classification was found to be an unverified premise for THIS question
 * (nexus-kgft3's substantive-critic finding: a hook failure's captured
 * exception text can echo document content, which a BYPASSRLS role would
 * then read cross-tenant). Fix lives in {@code grants-nexus-diag-4},
 * era-independent (no view-existence guard needed, unlike -1/-2/-3): it
 * grants SELECT on the two {@code NEXUS_DIAG_READABLE_TABLES} entries
 * {@code grants-nexus-diag-3} does not already cover via
 * {@code CHASH_BEARING_TABLES} ({@code relevance_log} is covered by -3).
 * This test reads {@link CatalogRepository#NEXUS_DIAG_READABLE_TABLES}
 * directly (a set separate from, and answering a different question than,
 * {@code AUDIT_ONLY_TABLES} — see both fields' javadoc) rather than keeping
 * a second, driftable copy — a future table added to that Java set without
 * a matching {@code GRANT} in {@code grants-nexus-diag-4} fails this test
 * against a live schema.
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
            provisionAndMigrate(pg, su);

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
     * The gc_audit-class production defect (2026-08-19, v0.1.82): falsifies against a live
     * schema that {@code nexus_diag} holds SELECT on every table
     * {@link CatalogRepository#NEXUS_DIAG_READABLE_TABLES} names, reading that set directly
     * (not a second hardcoded copy) so a table added there without a matching GRANT in
     * {@code grants-nexus-diag-4} fails HERE rather than reopening the gap silently in
     * production. Non-vacuous: {@code NEXUS_DIAG_READABLE_TABLES} is asserted non-empty
     * first, and this test fails against the pre-fix tree (confirmed: reverting
     * grants-nexus-diag-4 reproduces {@code permission denied for table gc_audit} for
     * exactly the two tables that changeset grants).
     */
    @Test
    void nexusDiagCanSelectEveryDiagReadableTable() throws Exception {
        assertThat(CatalogRepository.NEXUS_DIAG_READABLE_TABLES)
            .as("NEXUS_DIAG_READABLE_TABLES must be non-empty or this test guards nothing")
            .isNotEmpty();

        try (PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
             Connection su = pg.createConnection("")) {
            provisionAndMigrate(pg, su);

            try (Connection diag = DriverManager.getConnection(
                    pg.getJdbcUrl(), DIAG_ROLE, DIAG_PASS)) {
                List<String> denied = new ArrayList<>();
                for (String table : new TreeSet<>(CatalogRepository.NEXUS_DIAG_READABLE_TABLES)) {
                    try {
                        count(diag, "SELECT count(*) FROM nexus." + table);
                    } catch (Exception e) {
                        denied.add(table + ": " + e.getMessage());
                    }
                }
                assertThat(denied)
                    .as("nexus_diag must hold direct SELECT on every "
                        + "CatalogRepository.NEXUS_DIAG_READABLE_TABLES entry — each was "
                        + "independently verified content-free at the write-path level "
                        + "(not merely classified by AUDIT_ONLY_TABLES), live-schema "
                        + "evidence for the production gc_audit InsufficientPrivilege "
                        + "incident")
                    .isEmpty();
            }
        }
    }

    /**
     * Canonical two-phase provisioning split (mirrors
     * GrantsSvcForeignOwnedRelationTest / SchemaMigratorIntegrationTest): a plain
     * non-superuser admin role owns every schema object it creates, matching production.
     * Runs the changelog TWICE — the first boot lands directly in view era (taxonomy-011-8
     * self-heals the view before grants-nexus-diag-1 ever runs in the SAME walk); the
     * second reproduces the steady-state reboot a real cluster experiences and is where the
     * pre-fix bugs actually bite (grants-nexus-diag-2's REVOKE loop re-fires every boot).
     */
    private static void provisionAndMigrate(PostgreSQLContainer<?> pg, Connection su) throws Exception {
        su.setAutoCommit(true);

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

        liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
        liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);
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
