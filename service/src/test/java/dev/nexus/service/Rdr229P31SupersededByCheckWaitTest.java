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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), linda's ruling 2026-11-21 item (e): the changeset that adds
 * {@code catalog_collections_superseded_by_id_check} waits (RDR-246 {@code onFail="CONTINUE"}) while any registry row has
 * {@code superseded_by <> ''} AND {@code superseded_by_id IS NULL} AND {@code model_version = ''}, the rows D2's
 * {@code repair-catalog-collection-model-version} UPDATE could violate. Only that CHECK waits: the id column and every other
 * Expand CHECK go ahead, because the dual write sets {@code superseded_by_id} whether or not the CHECK exists.
 *
 * <p>The precondition reads the registry through the migrating role's owner-read policy ({@code
 * catalog_collections_owner_read}, D2's vectors-065, which the walk to {@code vectors-068-1} applies): the registry is FORCE
 * row-secured, so without it the migrating role counts no row. The structural half (every changeset that adds the constraint carries the precondition) is
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

    /**
     * Seam 2 (tamsin, 2026-11-21): on a skip-upgrade install the CHECK is added on a LATER boot, after the registry stamp move has completed and its
     * on_complete has run. Nothing else would validate it then, and the completion check's gate 1 could never hold. Its precondition has
     * cleared the blank-model_version tombstones and the stamp filled the rest, so the changeset validates the CHECK itself.
     */
    @Test
    void theCheckAddedOnALaterBootIsValidatedByItsOwnChangeset_soGate1HoldsOnceTheStampsAreDone() throws Exception {
        PostgreSQLContainer<?> pg = start();
        try (HikariDataSource ds = adminPool(pg)) {
            seedBeforeWalk(pg, ds, "tomb-blank", "");
            SchemaMigrator.migrate(ds);
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).as("non-vacuity: the CHECK waits at expand time").isZero();
                // the stamp runs: the tombstone whose successor is gone takes its own id (R7)
                ctx.update(DSL.table(DSL.name("nexus", "catalog_collections")))
                    .set(DSL.field(DSL.name("superseded_by_id"), Long.class), DSL.field(DSL.name("collection_id"), Long.class))
                    .where(DSL.field(DSL.name("name"), String.class).eq("tomb-blank")).execute();
            }
            SchemaMigrator.migrate(ds);   // the later boot adds the CHECK
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).as("the later boot adds it").isEqualTo(1);
                assertThat(PgCatalogProbes.constraintValidated(ctx, CHECK)).as("and the same changeset validates it: nothing else would").isTrue();

                // every gate-1 move is complete and every other constraint it validates is VALID, as the moves leave them: gate 1 then holds.
                // Gate 1 of the boot read set (CompletionCheck.ReadSet.boot()) is P3.1's stamp labels plus P3.3a's id-twin validation labels
                // (rdr229-fkvalidate-*, db/rdr229/p33a-fk-twins.tsv), so both families are driven to completion here.
                var moves = DSL.table(DSL.name("nexus", "data_moves"));
                var label = DSL.field(DSL.name("label"), String.class);
                var gate1Moves = label.like("rdr229-stamp-%").or(label.like("rdr229-fkvalidate-%"));
                var tables = ctx.select(DSL.field(DSL.name("driving_table"), String.class)).from(moves)
                    .where(gate1Moves).fetch(DSL.field(DSL.name("driving_table"), String.class));
                assertThat(ctx.fetchCount(moves, label.like("rdr229-stamp-%"))).as("non-vacuity: the walk registered the stamp moves").isPositive();
                assertThat(ctx.fetchCount(moves, label.like("rdr229-fkvalidate-%"))).as("non-vacuity: the walk registered P3.3a's validation moves")
                    .isPositive();
                for (var def : PgCatalogProbes.constraintDefinitionsIn(ctx, List.of("nexus"))) {
                    if (def.definition().endsWith("NOT VALID") && tables.contains(def.table())) {
                        P31TestSql.validate(su, DSL.table(DSL.name("nexus", def.table())), def.conname());
                    }
                }
                ctx.update(moves).set(DSL.field(DSL.name("completed_at"), java.time.OffsetDateTime.class), java.time.OffsetDateTime.now())
                    .where(gate1Moves).execute();
                var result = dev.nexus.service.datamove.CompletionCheck.evaluate(ctx);
                assertThat(result.supersededByCheckPresent()).isTrue();
                // Gate 2 (P3.3c's swap and child-validation moves) is registered at run time by the engine, never by the walk, so in this world
                // exactly those labels are open; every gate-1 label is complete or not applicable.
                assertThat(result.incomplete()).extracting(l -> l.label()).as("only gate 2's labels are open")
                    .containsExactlyInAnyOrderElementsOf(dev.nexus.service.datamove.CompletionCheck.ReadSet.boot().gate2Labels());
                assertThat(result.gate1()).as("gate 1 holds: the CHECKs are present and validated").isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    /** The registry holds no violating row at expand time (a live row only): the CHECK is added and validated by its own changeset at once. */
    @Test
    void aRegistryWithNoViolatingRow_hasTheCheckAddedAndValidatedAtOnce() throws Exception {
        PostgreSQLContainer<?> pg = start();
        try (HikariDataSource ds = adminPool(pg)) {
            migrateUpTo(ds, FIRST);
            SchemaMigrator.migrate(ds);
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).isEqualTo(1);
                assertThat(PgCatalogProbes.constraintValidated(ctx, CHECK)).isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * An ordinary upgrade: a tombstone with a model_version and no superseded_by_id is unstamped when the CHECK is added, so a VALIDATE would raise 23514
     * and halt the boot. The changeset leaves the CHECK NOT VALID there and the stamp move's on_complete validates it. (The precondition counts only the
     * blank-model_version tombstones, so it does not guarantee the data is clean.)
     */
    @Test
    void anUnstampedTombstoneWithAModelVersion_leavesTheCheckNotValidAndDoesNotHaltTheBoot() throws Exception {
        PostgreSQLContainer<?> pg = start();
        try (HikariDataSource ds = adminPool(pg)) {
            seedBeforeWalk(pg, ds, "tomb-versioned", "v1");
            SchemaMigrator.migrate(ds);
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(PgCatalogProbes.constraintCount(ctx, CHECK)).isEqualTo(1);
                assertThat(PgCatalogProbes.constraintValidated(ctx, CHECK)).as("a violating row: left for the stamp's on_complete").isFalse();
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
     * Walks to just before the expand (vectors-065's owner-read policy is in by then) and seeds one live row and one
     * tombstone with the given model_version, as a store from before 0.1.200 would hold them.
     */
    private static void seedBeforeWalk(PostgreSQLContainer<?> pg, HikariDataSource ds, String tombstone, String modelVersion)
            throws Exception {
        migrateUpTo(ds, FIRST);
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
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
