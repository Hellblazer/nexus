// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-156 Decision 5 (bead nexus-ubnwk) — proof that
 * {@code aspects-004-doc-id-backfill.xml}'s FORCE-RLS-toggled UPDATE actually
 * stamps {@code nexus.document_aspects.doc_id} for a genuine PRE-EXISTING
 * legacy row, rather than silently no-op'ing under FORCE ROW LEVEL SECURITY
 * (the "FORCE-RLS no-ops migration DML" hazard the changeset's own header
 * documents: {@code nexus_admin} is NOSUPERUSER/NOBYPASSRLS and owns both
 * FORCE-RLS tables, and no {@code nexus.tenant} GUC is set at migration
 * time, so a bare tenant-spanning UPDATE would silently match zero rows for
 * every tenant if the changeset had not toggled {@code NO FORCE} for its
 * duration).
 *
 * <p>Uses a DEDICATED container (never the shared nexus-yhmav template,
 * {@link PgContainerHelper#startDedicated()}) and Liquibase's
 * changeset-count-limited {@code update(int, ...)} (the exact mechanism
 * {@code SchemaMigratorIntegrationTest}'s "aged box" tests use to inject a
 * divergence before a specific changeset runs) to migrate up to — but NOT
 * including — {@code aspects-004-1}, seed a legacy {@code document_aspects}
 * row with {@code doc_id IS NULL} and a {@code source_uri} that exactly
 * matches a catalog document, THEN apply the remainder of the changelog
 * (including {@code aspects-004-1} itself) and assert the row was stamped.
 * A shared, already-fully-migrated-to-HEAD template is unusable for this:
 * by the time such a template exists, the backfill has already run against
 * an empty table and there is nothing left to observe.
 *
 * <p>{@code backfill_stampsPreExistingNullDocIdRow_whenSourceUriMatches} DELETED
 * (hygiene-001 step 1, nexus-tk070.p6a follow-on): its non-matching-source_uri
 * "orphan stays doc_id IS NULL forever" companion row is no longer representable
 * post-migration — hygiene-001-1 DELETEs every still-NULL-doc_id document_aspects
 * row later in the same changelog walk this test applies, and that DELETE's own
 * FORCE-RLS-toggle correctness is Hygiene001NotNullMigrationRlsTest's territory.
 */
class AspectDocIdBackfillTest {

    private static final String TARGET_CHANGESET_ID = "aspects-004-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";

    @Test
    void backfill_neverOverwritesAnAlreadyStampedDocId() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_aspect_backfill_test2";
            final String pass = "nexus_admin_aspect_backfill_test2_pass";
            bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-aspect-backfill-test2");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                migrateUpTo(adminDs, TARGET_CHANGESET_ID);

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_collections (tenant_id, name) "
                        + "VALUES ('backfill-tenant-2', 'knowledge__bf2__voyage-context-3__v1')");
                    // The row the aspects row is ALREADY (pre-migration) correctly
                    // attributed to — a distinct source_uri from the one below, so a
                    // guard failure (re-matching on source_uri) would be observable
                    // as a CHANGE, not a coincidental no-op.
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_documents "
                        + "  (tenant_id, tumbler, title, source_uri, physical_collection) "
                        + "VALUES ('backfill-tenant-2', 'bf2-doc-preexisting', 'Preexisting Doc', "
                        + "'file:///legacy/preexisting-source.md', 'knowledge__bf2__voyage-context-3__v1')");
                    // A DIFFERENT live document whose source_uri exactly matches the
                    // aspects row's source_uri — if the backfill's WHERE doc_id IS NULL
                    // guard were broken, this is the (wrong) doc_id it would repoint to.
                    su.createStatement().execute(
                        "INSERT INTO nexus.catalog_documents "
                        + "  (tenant_id, tumbler, title, source_uri, physical_collection) "
                        + "VALUES ('backfill-tenant-2', 'bf2-doc-correct', 'Correct Doc', "
                        + "'file:///legacy/already-stamped.md', 'knowledge__bf2__voyage-context-3__v1')");
                    su.createStatement().execute(
                        "INSERT INTO nexus.document_aspects "
                        + "  (tenant_id, collection, source_path, proposed_method, "
                        + "   extracted_at, model_version, extractor_name, source_uri, doc_id) "
                        + "VALUES ('backfill-tenant-2', 'knowledge__bf2__voyage-context-3__v1', "
                        + "'legacy/already-stamped.md', 'legacy extraction', "
                        + "'2025-01-01T00:00:00+00'::timestamptz, 'legacy-model', "
                        + "'legacy-extractor', 'file:///legacy/already-stamped.md', "
                        + "'bf2-doc-preexisting')");
                }

                try (Connection conn = adminDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(
                            MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                        liquibase.update(new Contexts(), new LabelExpression());
                    }
                }

                try (Connection su = pg.createConnection("")) {
                    ResultSet rs = su.createStatement().executeQuery(
                        "SELECT doc_id FROM nexus.document_aspects "
                        + "WHERE tenant_id = 'backfill-tenant-2' AND source_path = 'legacy/already-stamped.md'");
                    assertThat(rs.next()).isTrue();
                    assertThat(rs.getString("doc_id"))
                        .as("the backfill's WHERE doc_id IS NULL guard must leave an "
                            + "already-stamped row untouched, even though its source_uri "
                            + "ALSO matches a (different) live catalog document")
                        .isEqualTo("bf2-doc-preexisting");
                }
            }
        } finally {
            pg.stop();
        }
    }

    /** Minimal DBA-equivalent bootstrap, mirroring SchemaMigratorIntegrationTest's aged-box fixtures. */
    private static void bootstrapAdminRole(PostgreSQLContainer<?> pg, String role, String pass)
            throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                    + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
            su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
            su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
            su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
            su.createStatement().execute(
                "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' "
                + "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
        }
    }

    /**
     * Apply the master changelog's changesets UP TO, but NOT INCLUDING, {@code
     * targetChangesetId} — the same {@code listUnrunChangeSets} + index +
     * changeset-count-limited {@code update(int, ...)} idiom
     * {@code SchemaMigratorIntegrationTest}'s aged-box tests use to inject state
     * before a specific changeset runs. Index-based, not a hardcoded count: robust
     * against any OTHER changeset being added earlier in the chain.
     */
    private static void migrateUpTo(com.zaxxer.hikari.HikariDataSource adminDs,
                                    String targetChangesetId) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(
                    new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (targetChangesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx)
                    .as(targetChangesetId + " must be present in the master changelog")
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
