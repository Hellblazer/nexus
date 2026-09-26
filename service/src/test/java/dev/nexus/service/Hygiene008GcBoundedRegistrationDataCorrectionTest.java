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
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-0rxvg -- proof that {@code hygiene-008-2} corrects the
 * {@code nexus.catalog_collections} rows the catalog-037-1 body of
 * {@code gc_quarantine_orphans_bounded} misregistered.
 *
 * <p>The misregistered sibling is PRODUCED, not hand-seeded: the database is
 * migrated to just before {@code hygiene-008-1}, where the bounded function
 * still carries catalog-037-1's name-parsing body, and that function is called
 * on a real orphan. The test asserts the defect's shape before migrating on
 * (non-vacuity), then applies the rest of the changelog and asserts the
 * correction. A second row with no registered origin is hand-seeded for
 * branch 2 (prefix strip only), since the function cannot produce it.
 */
class Hygiene008GcBoundedRegistrationDataCorrectionTest {

    private static final String PRE_CHANGESET_ID = "hygiene-008-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT = "hygiene008-correction-tenant";

    private static final String ORIGIN = "knowledge__h008-a__bge-base-en-v15-768__v1";
    private static final String QUAR_FROM_FUNCTION = "quarantine-knowledge__h008-a__bge-base-en-v15-768__v1";
    private static final String QUAR_NO_ORIGIN = "quarantine-docs__h008-b__bge-base-en-v15-768__v1";

    @Test
    void correctsSiblingsTheBoundedSweepMisregistered() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_hygiene008_test";
            final String pass = "nexus_admin_hygiene008_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-hygiene008-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                migrateUpTo(adminDs, PRE_CHANGESET_ID);
                try (Connection adminConn = adminDs.getConnection()) {
                    PgContainerHelper.installTestObjects(adminConn);
                }

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    // Origin attributes the sibling's name does not imply
                    // (owner, model_version), so the correction must copy them.
                    insertRow(ctx, ORIGIN, "knowledge", "h008-origin-owner", "bge-base-en-v15-768", "v4", 768,
                        "live");
                    ctx.execute("INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768, metadata) "
                        + "VALUES (?, ?, sha256('h008 orphan'::bytea), 'h008 orphan', ('[1' || repeat(',0', 767) || ']')::nexus.vector, '{}'::jsonb)",
                        TENANT, ORIGIN);
                    ctx.fetch("SELECT * FROM nexus.gc_quarantine_orphans_bounded(768, ?, ?, ?, "
                        + "'2026-09-26T00:00:00Z', 20, 10)", TENANT, ORIGIN, QUAR_FROM_FUNCTION);

                    insertRow(ctx, QUAR_NO_ORIGIN, "quarantine-docs", "h008-b", "bge-base-en-v15-768", "v1", null,
                        "disputed");

                    Row before = readRow(ctx, QUAR_FROM_FUNCTION);
                    assertThat(before.contentType())
                        .as("non-vacuity: the catalog-037-1 body really misregisters the sibling")
                        .isEqualTo("quarantine-knowledge");
                    assertThat(before.dimension()).as("non-vacuity: and leaves dimension unset").isNull();
                }

                applyRemainingChangelog(adminDs);

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

                    Row a = readRow(ctx, QUAR_FROM_FUNCTION);
                    assertThat(a.contentType()).as("branch 1 content_type from the origin").isEqualTo("knowledge");
                    assertThat(a.ownerId()).as("branch 1 owner from the origin").isEqualTo("h008-origin-owner");
                    assertThat(a.modelVersion()).as("branch 1 model_version from the origin").isEqualTo("v4");
                    assertThat(a.dimension()).as("branch 1 dimension from the origin").isEqualTo(768);
                    assertThat(a.lifecycleState()).isEqualTo("quarantine");

                    Row b = readRow(ctx, QUAR_NO_ORIGIN);
                    assertThat(b.contentType()).as("branch 2 strips only the literal prefix").isEqualTo("docs");
                    assertThat(b.lifecycleState()).as("branch 2 never touches lifecycle_state")
                        .isEqualTo("disputed");

                    Row o = readRow(ctx, ORIGIN);
                    assertThat(o.contentType()).as("the origin itself is untouched").isEqualTo("knowledge");
                    assertThat(o.lifecycleState()).isEqualTo("live");
                }

                // Re-applying must run nothing: a checksum mismatch would throw.
                applyRemainingChangelog(adminDs);
            }
        } finally {
            pg.stop();
        }
    }

    private static void insertRow(DSLContext ctx, String name, String contentType, String ownerId,
                                   String embeddingModel, String modelVersion, Integer dimension,
                                   String lifecycleState) {
        ctx.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values(TENANT, name, contentType, ownerId, embeddingModel, modelVersion, dimension,
                lifecycleState)
            .execute();
    }

    private record Row(String contentType, String ownerId, String modelVersion, Integer dimension,
                        String lifecycleState) {}

    private static Row readRow(DSLContext ctx, String name) {
        var r = ctx.select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.DIMENSION,
                CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT))
            .and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne();
        assertThat(r).as("row %s must exist for tenant %s", name, TENANT).isNotNull();
        return new Row(r.get(CATALOG_COLLECTIONS.CONTENT_TYPE), r.get(CATALOG_COLLECTIONS.OWNER_ID),
            r.get(CATALOG_COLLECTIONS.MODEL_VERSION), r.get(CATALOG_COLLECTIONS.DIMENSION),
            r.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE));
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
