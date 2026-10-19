// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * GH #1402 / bead nexus-0gis0 — replay of the production crash-loop.
 *
 * <p>{@code grants-nexus-svc.xml}'s {@code grants-nexus-svc-1} changeset used
 * the bulk {@code GRANT ... ON ALL TABLES IN SCHEMA} form. Bulk GRANT
 * hard-errors on any relation the migration role (nexus_admin, NOSUPERUSER)
 * cannot grant on — specifically the deliberately superuser-owned
 * {@code nexus.diag_chash_conformance} view (RLS-exempt owner required,
 * nexus-vounk) — aborting every engine boot once the view exists.
 *
 * <p>This is a recurrence of the nexus-46yy3 class: {@code grants-nexus-diag.xml}
 * changeset {@code grants-nexus-diag-2} already converted its bulk REVOKE to
 * per-relation, owner-restricted iteration. This test reconstructs the #1402
 * production shape using the canonical two-phase provisioning split modeled
 * by {@code SchemaMigratorIntegrationTest} (DBA-provisioned non-superuser
 * owner role runs the full changelog from scratch, then a superuser-owned
 * view is added afterward) and pins the equivalent fix for the GRANT side:
 * {@code grants-nexus-svc-1} must skip relations it does not own rather than
 * aborting.
 */
class GrantsSvcForeignOwnedRelationTest {

    private static final String ADMIN_ROLE = "nexus_admin_svcgrant_replay";
    private static final String ADMIN_PASS = "nexus_admin_svcgrant_replay_pw";
    private static final String SVC_ROLE = "nexus_svc";

    @Test
    void changelogReplaysCleanly_withForeignOwnedDiagView() throws Exception {
        try (var pg = PgContainerHelper.startEmptyDatabase();
             Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            // Phase A provisioning (DBA / Phase-5 nx step, NOT Liquibase) —
            // the canonical two-phase split modeled by
            // SchemaMigratorIntegrationTest: nexus_admin is a plain
            // non-superuser role that will own every schema/table/sequence it
            // creates; extensions are superuser-installed up front (CREATE
            // EXTENSION requires superuser; neither vector nor pg_trgm is
            // trusted).
            // The admin and nexus_svc roles, the CREATE grants, pg_monitor WITH ADMIN OPTION
            // (nexus-hzhgl: grants-004 passes it on to nexus_svc and PostgreSQL refuses unless
            // the migrating role holds it; GrantsPgMonitorTest is the falsification proof), the
            // extensions and the SECURITY DEFINER relocation helpers search-path-001 calls
            // mid-walk all come from db.changelog-test-nonsuper-owner.xml, run here as the
            // superuser through PgContainerHelper#bootstrapNonSuperuserOwner.
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);

            // 1. Full changelog AS THE NON-SUPERUSER ADMIN ROLE, from
            //    scratch. nexus_admin owns every relation it creates here —
            //    the production shape, and avoids the default-privileges
            //    cross-talk a superuser-first run would introduce (ALTER
            //    DEFAULT PRIVILEGES applies to future objects created BY THE
            //    EXECUTING ROLE; running as superuser first would leak
            //    default grants onto the diag view created below).
            liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);

            // 2. Reconstruct the #1402 production shape: pg_provision's
            //    superuser bootstrap creates a SUPERUSER-owned counts view in
            //    schema nexus (RLS-exempt owner required, nexus-vounk) that
            //    the migration role can never grant on. Use the real diag
            //    view name so this test documents the exact production
            //    relation.
            //
            //    RDR-194 critical fix round (2026-08-17, nexus-i3k3e/Sig-2):
            //    step 1's full changelog now ALSO creates this view via
            //    taxonomy-011-8 (Liquibase-owned, self-healing) -- owned by
            //    ADMIN_ROLE at that point, since nothing else exists yet.
            //    Drop it first (superuser can drop any relation regardless
            //    of owner) so this step can still reconstruct a genuinely
            //    FOREIGN-owned (superuser-owned) view for the replay below.
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            ctx.dropView(DSL.name("nexus", "diag_chash_conformance")).execute();
            ctx.createView(DSL.name("nexus", "diag_chash_conformance"), DSL.name("n"))
                .as(DSL.select(DSL.val(1).as("n")))
                .execute();

            // Sanity: the setup actually leaves a relation the admin role
            // cannot grant on.
            assertThat(PgCatalogProbes.relationOwner(ctx, "nexus", "diag_chash_conformance"))
                .as("diag view must be foreign-owned relative to the admin role")
                .isNotNull()
                .isNotEqualTo(ADMIN_ROLE);

            // 3. THE REPLAY: nexus_admin's next startup. runAlways
            //    changesets fire again. Pre-fix (bulk GRANT ON ALL TABLES)
            //    this throws permission-denied on the foreign-owned view,
            //    aborting the whole update — the GH #1402 crash loop.
            //    Post-fix (per-relation, owner-restricted iteration) it
            //    must succeed.
            liquibaseUpdate(pg.getJdbcUrl(), ADMIN_ROLE, ADMIN_PASS);

            // 4. Post-conditions: nexus_svc got DML on an admin-owned table,
            //    but NOT on the foreign-owned diag view — it does not need it.
            assertThat(hasTablePriv(su, SVC_ROLE, "nexus.chunks", "INSERT"))
                .as("admin-owned table must still be granted")
                .isTrue();
            assertThat(hasTablePriv(su, SVC_ROLE, "nexus.diag_chash_conformance", "SELECT"))
                .as("foreign-owned relation intentionally skipped")
                .isFalse();
        }
    }

    /**
     * Run the changelog on a DEDICATED connection (Liquibase leaves session
     * state behind on the connection it flips to autoCommit=false).
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

    private static boolean hasTablePriv(Connection c, String role, String rel, String priv)
            throws Exception {
        var hasPriv = DSL.function("has_table_privilege", SQLDataType.BOOLEAN,
            DSL.val(role), DSL.val(rel), DSL.val(priv));
        return DSL.using(c, SQLDialect.POSTGRES).select(hasPriv).fetchOne(hasPriv);
    }
}
