// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
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
 * RDR-229 P3.1 (nexus-p2m1x.17), linda's ruling 2026-11-21 item (e): the changeset that adds
 * {@code catalog_collections_superseded_by_id_check} waits (RDR-246 {@code onFail="CONTINUE"}) while any registry row has
 * {@code superseded_by <> ''} AND {@code superseded_by_id IS NULL} AND {@code model_version = ''}, the rows D2's
 * {@code repair-catalog-collection-model-version} UPDATE could violate. Only that CHECK waits: the id column and every other
 * Expand CHECK go ahead, because the dual write sets {@code superseded_by_id} whether or not the CHECK exists.
 *
 * <p>The precondition reads the registry through the migrating role's owner-read policy ({@code
 * catalog_collections_owner_read}, vectors-065 on D2's branch, not in this tree yet): the registry is FORCE row-secured, so
 * without it the migrating role counts no row. The test creates the same policy before the walk, as the final tree will have
 * it. The structural half (every changeset that adds the constraint carries the precondition) is
 * {@code tests/test_p31_superseded_by_check_precondition_lint.py}.
 */
class Rdr229P31SupersededByCheckWaitTest {

    private static final String ADMIN_ROLE = "nexus_admin_p31_supby";
    private static final String ADMIN_PASS = "nexus_admin_p31_supby_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String FIRST = "vectors-068-1";
    /** The changeset that adds the CHECK: its own, so the id column (vectors-068-2) is never skipped with it. */
    private static final String CHECK_CHANGESET = "vectors-068-19";
    private static final String COLUMN_CHANGESET = "vectors-068-2";
    private static final String CHECK = "catalog_collections_superseded_by_id_check";
    private static final String OTHER_CHECK = "chunks_collection_id_check";
    private static final String TENANT = "p31-supby";

    @Test
    void aBlankModelVersionTombstoneHoldsOnlyTheCheckBack_theColumnAndTheOtherChecksApply() throws Exception {
        PostgreSQLContainer<?> pg = start();
        try (HikariDataSource ds = adminPool(pg)) {
            seedBeforeWalk(pg, ds, "tomb-blank", "");
            SchemaMigrator.migrate(ds);
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).as("the CHECK waits while the tombstone could be hit").isZero();
                assertThat(recorded(ctx, CHECK_CHANGESET)).as("a CONTINUE skip records nothing").isFalse();
                assertThat(PgCatalogProbes.columnExists(ctx, "nexus", "catalog_collections", "superseded_by_id"))
                    .as("the id column is not behind the precondition: the dual write sets it").isTrue();
                assertThat(recorded(ctx, COLUMN_CHANGESET)).isTrue();
                assertThat(PgCatalogProbes.constraintCount(ctx, OTHER_CHECK)).as("the other Expand CHECKs go ahead").isGreaterThan(0);
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void theCheckLandsOnTheNextBootOnceTheIdIsFilled() throws Exception {
        PostgreSQLContainer<?> pg = start();
        try (HikariDataSource ds = adminPool(pg)) {
            seedBeforeWalk(pg, ds, "tomb-blank", "");
            SchemaMigrator.migrate(ds);
            SchemaMigrator.migrate(ds); // a second walk with the row still waiting changes nothing
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).isZero();
                // What the stamp does to a tombstone whose successor is gone (R7): its own id.
                ctx.update(DSL.table(DSL.name("nexus", "catalog_collections")))
                    .set(DSL.field(DSL.name("superseded_by_id"), Long.class), DSL.field(DSL.name("collection_id"), Long.class))
                    .where(DSL.field(DSL.name("name"), String.class).eq("tomb-blank")).execute();
            }
            SchemaMigrator.migrate(ds);
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).as("the CHECK lands once the id is filled").isEqualTo(1);
                assertThat(recorded(ctx, CHECK_CHANGESET)).isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aTombstoneWithAModelVersionDoesNotWait_andNeitherDoesALiveRow() throws Exception {
        PostgreSQLContainer<?> pg = start();
        try (HikariDataSource ds = adminPool(pg)) {
            seedBeforeWalk(pg, ds, "tomb-versioned", "v1");
            SchemaMigrator.migrate(ds);
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                // Non-vacuity: the tombstone is there, unstamped, and the CHECK still landed over it (NOT VALID checks no row).
                assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections")),
                    DSL.field(DSL.name("superseded_by"), String.class).ne(""))).isEqualTo(1);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).isEqualTo(1);
                assertThat(recorded(ctx, CHECK_CHANGESET)).isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    // ---- fixtures ----------------------------------------------------------------------------------------------------

    private static PostgreSQLContainer<?> start() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        return pg;
    }

    /**
     * Walks to just before the expand, adds the owner-read policy vectors-065 will add, and seeds one live row and one
     * tombstone with the given model_version, as a store from before 0.1.200 would hold them.
     */
    private static void seedBeforeWalk(PostgreSQLContainer<?> pg, HikariDataSource ds, String tombstone, String modelVersion)
            throws Exception {
        migrateUpTo(ds, FIRST);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.createPolicyUsingTrue(su, CATALOG_COLLECTIONS, "catalog_collections_owner_read",
                PgContainerHelper.PolicyKind.PERMISSIVE, "SELECT", ADMIN_ROLE);
            PgContainerHelper.insertCollection(ctx, TENANT, "live-one");
            PgContainerHelper.insertCollection(ctx, TENANT, tombstone);
            var name = DSL.field(DSL.name("name"), String.class);
            ctx.update(DSL.table(DSL.name("nexus", "catalog_collections")))
                .set(DSL.field(DSL.name("superseded_by"), String.class), "successor-gone")
                .set(DSL.field(DSL.name("model_version"), String.class), modelVersion)
                .where(name.eq(tombstone)).execute();
            assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections")),
                DSL.field(DSL.name("superseded_by"), String.class).ne(""))).as("non-vacuity: one tombstone seeded").isEqualTo(1);
        }
    }

    private static HikariDataSource adminPool(PostgreSQLContainer<?> pg) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-p31-supby");
        return new HikariDataSource(cfg);
    }

    /** Applies the HEAD changelog up to, but not including, {@code changesetId}. */
    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId).isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    private static boolean recorded(DSLContext ctx, String id) {
        return ctx.fetchExists(DSL.selectOne().from(DSL.table(DSL.name("public", "databasechangelog")))
            .where(DSL.field(DSL.name("id"), String.class).eq(id)));
    }
}
