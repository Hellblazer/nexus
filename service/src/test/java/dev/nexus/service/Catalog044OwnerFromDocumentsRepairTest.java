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
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
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
    // nexus-6pbwx fix round: curator-typed owners are replaceable, repo owners are not.
    private static final String MIXED = "code__mixed-6pbwx__voyage-code-3__v1";
    private static final String CURATOR_SEGMENT = "code__curseg-6pbwx__voyage-code-3__v1";
    private static final String CURATOR_ONLY = "docs__curonly-6pbwx__voyage-context-3__v1";

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
                    insertOwner(su, TENANT, "1.14", "default", "curator");
                    insertOwner(su, TENANT, "1.32", "knowledge", "curator");
                    insertOwner(su, TENANT, "1.33", "some-repo", "repo");
                    insertCollection(su, TENANT, DEFAULT_DOCS, "docs", "default");
                    insertCollection(su, TENANT, MIXED, "code", "mixed-6pbwx");
                    insertCollection(su, TENANT, CURATOR_SEGMENT, "code", "1-32");
                    insertCollection(su, TENANT, CURATOR_ONLY, "docs", "1-32");
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
                    // The estate's real case: docs__default, owner "default", every document
                    // under the curator 1.14. Curator documents are not excluded outright.
                    insertDocument(su, TENANT, "1.14.1", DEFAULT_DOCS, false);
                    insertDocument(su, TENANT, "1.14.2", DEFAULT_DOCS, false);
                    insertDocument(su, TENANT, "1.14.3", DEFAULT_DOCS, false);
                    // Three curator documents and one repo document: the repo owner is ranked first.
                    insertDocument(su, TENANT, "1.32.1", MIXED, false);
                    insertDocument(su, TENANT, "1.32.2", MIXED, false);
                    insertDocument(su, TENANT, "1.32.3", MIXED, false);
                    insertDocument(su, TENANT, "1.33.1", MIXED, false);
                    insertDocument(su, TENANT, "1.33.2", CURATOR_SEGMENT, false);
                    insertDocument(su, TENANT, "1.32.9", CURATOR_ONLY, false);
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
                        .as("the estate's real case: docs__default__ repaired from its curator documents")
                        .isEqualTo("1-14");
                    assertThat(ownerOf(su, TENANT, MIXED))
                        .as("a repo owner outranks a curator with more documents").isEqualTo("1-33");
                    assertThat(ownerOf(su, TENANT, CURATOR_SEGMENT))
                        .as("a curator's segment is replaceable though it is tumbler-shaped").isEqualTo("1-33");
                    assertThat(ownerOf(su, TENANT, CURATOR_ONLY))
                        .as("a curator-only collection already on the curator's segment is unchanged")
                        .isEqualTo("1-32");
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
                                          String ownerId) {
        DSL.using(c, SQLDialect.POSTGRES)
            .insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.CONTENT_TYPE,
                CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(tenant, name, contentType, ownerId,
                contentType.contains("code") ? "voyage-code-3" : "voyage-context-3", "v1",
                contentType.startsWith("quarantine") ? "quarantine" : "live")
            .execute();
    }

    private static void insertOwner(Connection c, String tenant, String prefix, String name,
                                     String ownerType) {
        DSL.using(c, SQLDialect.POSTGRES)
            .insertInto(CATALOG_OWNERS,
                CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME,
                CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.NEXT_SEQ)
            .values(tenant, prefix, name, ownerType, "", 0L)
            .execute();
    }

    private static void insertDocument(Connection c, String tenant, String tumbler, String collection,
                                        boolean tombstoned) {
        DSL.using(c, SQLDialect.POSTGRES)
            .insertInto(CATALOG_DOCUMENTS,
                CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.DELETED_AT)
            .values(tenant, tumbler, "doc " + tumbler, collection,
                tombstoned ? java.time.OffsetDateTime.now() : null)
            .execute();
    }

    private static String ownerOf(Connection c, String tenant, String name) {
        String owner = DSL.using(c, SQLDialect.POSTGRES)
            .select(CATALOG_COLLECTIONS.OWNER_ID)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
            .and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID);
        assertThat(owner).as("row %s must exist for tenant %s", name, tenant).isNotNull();
        return owner;
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
