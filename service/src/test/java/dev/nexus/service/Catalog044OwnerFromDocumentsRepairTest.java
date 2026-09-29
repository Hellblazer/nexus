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
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-6pbwx -- proof that {@code catalog-044-3} repairs the existing
 * {@code nexus.catalog_collections} rows whose {@code owner_id} is not tumbler-shaped,
 * and only those.
 *
 * <p>The database is migrated to just before {@code catalog-044-1}, so the owner triggers
 * do not exist yet and the seeded rows hold exactly the pre-fix shape (a slug stamped by
 * hygiene-002-1 from the collection name). The test asserts that shape first
 * (non-vacuity), then applies the rest of the changelog and reads the rows back.
 */
class Catalog044OwnerFromDocumentsRepairTest {

    private static final String PRE_CHANGESET_ID = "catalog-044-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT = "catalog044-repair-tenant";
    private static final String OTHER_TENANT = "catalog044-repair-other";

    private static final String SLUG_CODE = "code__arcaneum-2ad2825c__voyage-code-3__v1";
    private static final String DEFAULT_DOCS = "docs__default__voyage-context-3__v1";
    private static final String DOTTED_RDR = "rdr__dotted-owner__voyage-context-3__v1";
    private static final String CORRECT_CODE = "code__1-3__voyage-code-3__v1";
    private static final String NO_DOCS_CODE = "code__nodocs-6pbwx__voyage-code-3__v1";
    private static final String KNOWLEDGE = "knowledge__distributed-systems__voyage-context-3__v1";
    private static final String QUARANTINE = "quarantine-code__quar-6pbwx__voyage-code-3__v1";
    private static final String TOMBSTONE_ONLY = "code__tombstone-6pbwx__voyage-code-3__v1";
    private static final String PHANTOM_ONLY = "code__phantom-6pbwx__voyage-code-3__v1";

    @Test
    void repairsNonTumblerOwnersOfCollectionsThatHaveDocuments() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_catalog044_test";
            final String pass = "nexus_admin_catalog044_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-catalog044-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                migrateUpTo(adminDs, PRE_CHANGESET_ID);

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    insertCollection(su, TENANT, SLUG_CODE, "code", "arcaneum-2ad2825c");
                    insertCollection(su, TENANT, DEFAULT_DOCS, "docs", "default");
                    insertCollection(su, TENANT, DOTTED_RDR, "rdr", "1.9");
                    insertCollection(su, TENANT, CORRECT_CODE, "code", "1-3");
                    insertCollection(su, TENANT, NO_DOCS_CODE, "code", "nodocs-6pbwx");
                    insertCollection(su, TENANT, KNOWLEDGE, "knowledge", "distributed-systems");
                    insertCollection(su, TENANT, QUARANTINE, "quarantine-code", "quar-6pbwx");
                    insertCollection(su, TENANT, TOMBSTONE_ONLY, "code", "tombstone-6pbwx");
                    insertCollection(su, TENANT, PHANTOM_ONLY, "code", "phantom-6pbwx");
                    // A second tenant with the same collection name and a different owner:
                    // the walk is per (tenant, collection), never name-only.
                    insertCollection(su, OTHER_TENANT, SLUG_CODE, "code", "arcaneum-2ad2825c");

                    insertDocument(su, TENANT, "1.15.1", SLUG_CODE, false);
                    insertDocument(su, TENANT, "1.15.2", SLUG_CODE, false);
                    insertDocument(su, TENANT, "1.2.1", SLUG_CODE, false);  // minority owner
                    insertDocument(su, TENANT, "1.4.1", DEFAULT_DOCS, false);
                    insertDocument(su, TENANT, "1.9.5", DOTTED_RDR, false);
                    insertDocument(su, TENANT, "1.7.1", CORRECT_CODE, false); // disagrees; must stand
                    insertDocument(su, TENANT, "1.1.7", KNOWLEDGE, false);
                    insertDocument(su, TENANT, "1.8.1", QUARANTINE, false);
                    insertDocument(su, TENANT, "1.30.1", TOMBSTONE_ONLY, true);
                    insertDocument(su, TENANT, "1.5", PHANTOM_ONLY, false);   // two segments: no owner
                    insertDocument(su, TENANT, "rn.1", PHANTOM_ONLY, false);  // not numeric: no owner
                    insertDocument(su, OTHER_TENANT, "1.40.1", SLUG_CODE, false);

                    assertThat(ownerOf(su, TENANT, SLUG_CODE))
                        .as("non-vacuity: before the changeset the slug is what the row holds")
                        .isEqualTo("arcaneum-2ad2825c");
                }

                applyRemainingChangelog(adminDs);

                try (Connection su = pg.createConnection("")) {
                    assertThat(ownerOf(su, TENANT, SLUG_CODE))
                        .as("the owner with the most documents, not the first or the lowest").isEqualTo("1-15");
                    assertThat(ownerOf(su, TENANT, DEFAULT_DOCS))
                        .as("the estate's real case: docs__default__ repaired from its documents").isEqualTo("1-4");
                    assertThat(ownerOf(su, TENANT, DOTTED_RDR))
                        .as("a dotted owner is not the column's design; it becomes the hyphenated segment")
                        .isEqualTo("1-9");
                    assertThat(ownerOf(su, TENANT, CORRECT_CODE))
                        .as("a tumbler-shaped owner is a first registration and stands").isEqualTo("1-3");
                    assertThat(ownerOf(su, TENANT, NO_DOCS_CODE)).as("no documents: left alone")
                        .isEqualTo("nodocs-6pbwx");
                    assertThat(ownerOf(su, TENANT, KNOWLEDGE))
                        .as("knowledge keeps its subject").isEqualTo("distributed-systems");
                    assertThat(ownerOf(su, TENANT, QUARANTINE)).as("quarantine is outside the rule")
                        .isEqualTo("quar-6pbwx");
                    assertThat(ownerOf(su, TENANT, TOMBSTONE_ONLY)).as("a tombstoned document names no owner")
                        .isEqualTo("tombstone-6pbwx");
                    assertThat(ownerOf(su, TENANT, PHANTOM_ONLY))
                        .as("a phantom or non-numeric tumbler names no owner").isEqualTo("phantom-6pbwx");
                    assertThat(ownerOf(su, OTHER_TENANT, SLUG_CODE)).as("per tenant").isEqualTo("1-40");
                }

                // Re-applying must run nothing: a checksum mismatch would throw.
                applyRemainingChangelog(adminDs);
            }
        } finally {
            pg.stop();
        }
    }

    private static void insertCollection(Connection c, String tenant, String name, String contentType,
                                          String ownerId) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, "
            + "embedding_model, model_version, lifecycle_state) VALUES (?, ?, ?, ?, ?, 'v1', ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, name);
            ps.setString(3, contentType);
            ps.setString(4, ownerId);
            ps.setString(5, contentType.contains("code") ? "voyage-code-3" : "voyage-context-3");
            ps.setString(6, contentType.startsWith("quarantine") ? "quarantine" : "live");
            ps.executeUpdate();
        }
    }

    private static void insertDocument(Connection c, String tenant, String tumbler, String collection,
                                        boolean tombstoned) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection, deleted_at) "
            + "VALUES (?, ?, ?, ?, " + (tombstoned ? "now()" : "NULL") + ")")) {
            ps.setString(1, tenant);
            ps.setString(2, tumbler);
            ps.setString(3, "doc " + tumbler);
            ps.setString(4, collection);
            ps.executeUpdate();
        }
    }

    private static String ownerOf(Connection c, String tenant, String name) throws Exception {
        try (var ps = c.prepareStatement(
            "SELECT owner_id FROM nexus.catalog_collections WHERE tenant_id = ? AND name = ?")) {
            ps.setString(1, tenant);
            ps.setString(2, name);
            try (var rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s must exist for tenant %s", name, tenant).isTrue();
                return rs.getString(1);
            }
        }
    }

    private static void applyRemainingChangelog(com.zaxxer.hikari.HikariDataSource adminDs) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        }
    }

    private static void migrateUpTo(com.zaxxer.hikari.HikariDataSource adminDs,
                                     String targetChangesetId) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (targetChangesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as(targetChangesetId + " must be present in the master changelog")
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
