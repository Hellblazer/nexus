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
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-hbnml (RDR-235 step B of nexus-bpikw): {@code aspects-005-1} / {@code aspects-005-2} retire
 * {@code nexus.aspect_extraction_queue}. The drop is guarded by a preCondition that the table holds no row, with
 * {@code onFail="CONTINUE" onError="CONTINUE"}: a populated table is left alone, the boot goes on, and the
 * changeset is retried at the next boot.
 *
 * <p>Every test runs on an empty database owned by a NOBYPASSRLS, non-superuser role, the production migration
 * role's shape: under FORCE ROW LEVEL SECURITY with no tenant GUC that role reads ZERO rows of the queue table
 * whatever it holds (the nexus-1wjmq class), so a guard written as a plain {@code count(*)} would call a populated
 * table empty and drop it. {@link #aPlainCountByTheMigrationRoleReadsZeroOnAPopulatedTable_theHelperReadsTheRow}
 * pins that, and the populated-table walk then proves the helper-based guard keeps the row. Rows are seeded as
 * superuser (BYPASSRLS), as old clients' rows were.
 *
 * <p>Cases: the fresh walk drops the table and the helper; an empty table is dropped; a one-row table is kept with
 * its row, the walk goes on to the end of the changelog and does not halt, a second walk does not either, and a
 * third walk after the row is gone drops the table (the retry); a precondition that THROWS (the migration role does
 * not own the table) skips the changeset without halting; and the rollback of {@code aspects-005-2} recreates the empty table with the
 * shape the earlier changesets left (columns, defaults, indexes, constraints, policy, RLS flags), the helper
 * included, so an update after that rollback drops it again.
 */
class AspectQueueRetireLiquibaseTest {

    private static final String ADMIN_ROLE = "nexus_admin_aspq_retire";
    private static final String ADMIN_PASS = "nexus_admin_aspq_retire_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";

    private static final String CHANGELOG = "db/changelog/aspects-005-retire-aspect-queue-table.xml";
    private static final String STUB_CHANGELOG = "db/changelog-test/db.changelog-test-aspect-queue-precondition-stub.xml";

    static final String GUARD_CHANGESET = "aspects-005-1";
    static final String DROP_CHANGESET = "aspects-005-2";
    /** A changeset that runs after aspects-005 in the master changelog: the walk went past the skip. */
    private static final String LATER_CHANGESET = "vectors-053-1";

    private static final String OTHER_OWNER = "aspq_other_owner";
    private static final String TENANT = "aspq-retire";
    private static final String COLLECTION = "aspq-retire-coll";
    private static final String DOC = "aspq.1";

    private static final String SCHEMA = "nexus";
    private static final String QUEUE = "aspect_extraction_queue";
    private static final String HELPER = "aspect_queue_rows_unscoped";
    private static final Table<?> QUEUE_TABLE = DSL.table(DSL.name(SCHEMA, QUEUE));
    private static final Field<String> Q_TENANT = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> Q_COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final Field<String> Q_SOURCE_PATH = DSL.field(DSL.name("source_path"), String.class);
    private static final Field<String> Q_DOC_ID = DSL.field(DSL.name("doc_id"), String.class);
    private static final Field<OffsetDateTime> Q_ENQUEUED_AT = DSL.field(DSL.name("enqueued_at"), OffsetDateTime.class);
    private static final Field<Long> HELPER_ROWS = DSL.function(DSL.name(SCHEMA, HELPER), Long.class);

    private static final Table<?> DATABASECHANGELOG = DSL.table(DSL.name("public", "databasechangelog"));
    private static final Field<String> DBCL_ID = DSL.field(DSL.name("id"), String.class);

    // ── the tests ───────────────────────────────────────────────────────────

    @Test
    void aFreshWalkDropsTheTableAndTheHelper_andBothChangesetsAreRecorded() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, QUEUE)).as("the queue table is gone").isFalse();
                    assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER)).as("the helper is gone").isFalse();
                    assertThat(PgCatalogProbes.constraintExists(ctx, "aspect_extraction_queue_collection_fk"))
                        .as("the collection foreign key went with the table").isFalse();
                    assertThat(PgCatalogProbes.constraintExists(ctx, "fk_aspect_queue_catalog_doc"))
                        .as("the document foreign key went with the table").isFalse();
                    assertThat(changesetRecorded(su, GUARD_CHANGESET)).isTrue();
                    assertThat(changesetRecorded(su, DROP_CHANGESET))
                        .as("an empty table is dropped, so the changeset ran and is recorded").isTrue();
                }

                // A second walk has nothing to do for either changeset and does not recreate anything.
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                try (Connection su = pg.createConnection("")) {
                    assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), SCHEMA, QUEUE)).isFalse();
                }
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void anEmptyTableIsDropped_theHelperCountsZero() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, QUEUE)).as("before the drop").isTrue();
                    assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER)).as("aspects-005-1 ran").isTrue();
                    assertThat(changesetRecorded(su, DROP_CHANGESET)).isFalse();
                    assertThat(helperRows(ctx)).as("the helper counts an empty table as 0").isEqualTo(0L);
                }

                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, QUEUE)).isFalse();
                    assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER)).isFalse();
                    assertThat(changesetRecorded(su, DROP_CHANGESET)).isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The falsification of a naive guard. The migration role owns the table under FORCE ROW LEVEL SECURITY and
     * has no BYPASSRLS, so a plain count by that role sees nothing at all on a table that holds a row; the helper
     * lifts FORCE for the count and puts it back.
     */
    @Test
    void aPlainCountByTheMigrationRoleReadsZeroOnAPopulatedTable_theHelperReadsTheRow() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    seedOneRow(su);
                    assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(QUEUE_TABLE))
                        .as("superuser sees the seeded row").isEqualTo(1);
                }
                try (Connection admin = adminDs.getConnection()) {
                    DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
                    assertThat(ctx.fetchCount(QUEUE_TABLE))
                        .as("a plain count(*) by the NOBYPASSRLS owner reads 0 on a table holding a row: a guard "
                            + "written that way would drop it")
                        .isEqualTo(0);
                    assertThat(helperRows(ctx)).as("the helper reads the row").isEqualTo(1L);
                    assertThat(ctx.fetchCount(QUEUE_TABLE))
                        .as("FORCE ROW LEVEL SECURITY is back after the helper: the plain count is blind again")
                        .isEqualTo(0);
                    var rls = PgCatalogProbes.rowSecurity(ctx, SCHEMA, QUEUE);
                    assertThat(rls).isNotNull();
                    assertThat(rls.enabled()).isTrue();
                    assertThat(rls.forced()).as("FORCE restored by the helper").isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aPopulatedTableIsKept_theWalkGoesOn_andTheTableIsDroppedOnceItIsEmpty() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    seedOneRow(su);
                }

                // Walk one: the guard sees the row, the changeset is skipped, nothing halts.
                assertThatCode(() -> SchemaMigrator.migrate(adminDs))
                    .as("a populated table must never halt the boot").doesNotThrowAnyException();
                assertKeptWithItsRow(pg);

                // Walk two: still kept, still no halt, and still not recorded (retried again, not MARK_RAN).
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertKeptWithItsRow(pg);

                // The operator empties the table; the next boot drops it (the retry).
                try (Connection su = pg.createConnection("")) {
                    assertThat(DSL.using(su, SQLDialect.POSTGRES).deleteFrom(QUEUE_TABLE).execute()).isEqualTo(1);
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, QUEUE))
                        .as("the retry drops the now-empty table").isFalse();
                    assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER)).isFalse();
                    assertThat(changesetRecorded(su, DROP_CHANGESET)).isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    private static void assertKeptWithItsRow(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, QUEUE)).as("the populated table is kept").isTrue();
            assertThat(ctx.fetchCount(QUEUE_TABLE)).as("with its row").isEqualTo(1);
            assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER))
                .as("the helper stays until the table is dropped").isTrue();
            assertThat(changesetRecorded(su, GUARD_CHANGESET)).isTrue();
            assertThat(changesetRecorded(su, DROP_CHANGESET))
                .as("CONTINUE skips the changeset WITHOUT recording it (MARK_RAN would stop the retry)").isFalse();
            assertThat(changesetRecorded(su, LATER_CHANGESET)).isTrue();
            assertThat(PgCatalogProbes.rowSecurity(ctx, SCHEMA, QUEUE).forced())
                .as("the guard left FORCE ROW LEVEL SECURITY on").isTrue();
        }
    }

    /**
     * The helper puts FORCE ROW LEVEL SECURITY back only when it found it on (the {@code v_forced} branch). An
     * operator who had turned FORCE off keeps it off: the helper counts across tenants, returns the row, and does
     * not switch a setting it did not switch. (The other branch, FORCE found on and restored, is
     * {@link #aPlainCountByTheMigrationRoleReadsZeroOnAPopulatedTable_theHelperReadsTheRow}.)
     */
    @Test
    void theHelperLeavesForceRowLevelSecurityOffWhenItFoundItOff() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection admin = adminDs.getConnection()) {
                    PgContainerHelper.installTestObjects(admin);
                    PgContainerHelper.setForceRls(admin, QUEUE_TABLE, false);
                }
                try (Connection su = pg.createConnection("")) {
                    seedOneRow(su);
                }
                try (Connection admin = adminDs.getConnection()) {
                    DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.rowSecurity(ctx, SCHEMA, QUEUE).forced())
                        .as("premise: FORCE is off before the call").isFalse();
                    assertThat(helperRows(ctx)).isEqualTo(1L);
                    var rls = PgCatalogProbes.rowSecurity(ctx, SCHEMA, QUEUE);
                    assertThat(rls.enabled()).isTrue();
                    assertThat(rls.forced()).as("the helper does not turn FORCE on").isFalse();
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * Liquibase ends the precondition's transaction before it runs the body, so a count in the precondition does
     * not hold the table against a writer. The body counts again. This replaces the helper with one that answers
     * 0 to the precondition's top-level {@code SELECT} (the table already holds a row, as if it landed just after
     * that count) and counts for real inside the body's {@code DO} block, then walks: the body must raise, the
     * changeset must stay unrecorded (retried at the next boot), the row must survive and the walk must go on.
     */
    @Test
    void aRowThatLandsAfterThePreconditionIsKept_theBodyCountsAgainAndTheChangesetIsRetried() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    seedOneRow(su);
                }
                try (Connection admin = adminDs.getConnection()) {
                    PgContainerHelper.runSuperuserTestChangelog(admin, STUB_CHANGELOG, "databasechangelog_test_aspq_stub",
                        java.util.Map.of());
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs))
                    .as("a body that raises must not halt the boot (failOnError=false)").doesNotThrowAnyException();
                assertKeptWithItsRow(pg);
            }
        } finally {
            pg.stop();
        }
    }

    /** The body of aspects-005-2 run by itself on a populated table raises and drops nothing. */
    @Test
    void theDropBodyOnItsOwnRefusesAPopulatedTable() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection admin = adminDs.getConnection()) {
                    assertThatCode(() -> PgContainerHelper.replayChangeset(admin, CHANGELOG, DROP_CHANGESET))
                        .as("an empty table is dropped").doesNotThrowAnyException();
                    assertThat(PgCatalogProbes.tableExists(DSL.using(admin, SQLDialect.POSTGRES), SCHEMA, QUEUE))
                        .as("non-vacuity: the body dropped it").isFalse();
                }
            }
        } finally {
            pg.stop();
        }
        pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    seedOneRow(su);
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThatThrownBy(() -> PgContainerHelper.replayChangeset(admin, CHANGELOG, DROP_CHANGESET))
                        .hasMessageContaining("holds 1 row(s)");
                }
                try (Connection su = pg.createConnection("")) {
                    assertThat(DSL.using(su, SQLDialect.POSTGRES).fetchCount(QUEUE_TABLE)).isEqualTo(1);
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * A precondition that THROWS is an error, not a failed check, and {@code onError="CONTINUE"} turns it into the
     * same skip. The table is handed to another role here, so the migration role may not {@code ALTER} it and the
     * helper raises {@code must be owner of table}: the sqlCheck throws, the changeset is skipped, the boot goes on.
     */
    @Test
    void aGuardThatThrowsSkipsTheChangeset_itDoesNotHaltTheBoot() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(OTHER_OWNER);
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
                PgContainerHelper.createLoginRole(su, OTHER_OWNER, "aspq_other_owner_pass", false);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, DROP_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    PgContainerHelper.alterTableOwner(su, QUEUE_TABLE, OTHER_OWNER);
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThatThrownBy(() -> helperRows(DSL.using(admin, SQLDialect.POSTGRES)))
                        .as("non-vacuity: the guard really throws for a role that does not own the table")
                        .hasMessageContaining("must be owner");
                }

                assertThatCode(() -> SchemaMigrator.migrate(adminDs))
                    .as("a throwing guard must not halt the boot").doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, QUEUE)).as("nothing was dropped").isTrue();
                    assertThat(changesetRecorded(su, DROP_CHANGESET)).as("and nothing was recorded").isFalse();
                    assertThat(changesetRecorded(su, LATER_CHANGESET)).as("the walk went on").isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The rollback of aspects-005-2 brings the table back EMPTY with the structure aspects-001-5/-6, aspects-002-1,
     * fk-001-4, fk-003-2/-8 and hygiene-001-2 leave, and brings the helper back with it, so an update after that
     * rollback drops the table again. The shape is read before the drop and after the rollback and must be equal
     * (nexus_svc's table privilege is the runAlways grants block's, not compared). A second rollback, of
     * aspects-005-1, leaves the structure and removes the helper.
     */
    @Test
    void theRollbackRecreatesTheEmptyTableWithTheShapeItHadBeforeTheDrop() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, GUARD_CHANGESET);
                tag(adminDs, "before-aspects-005");
                List<String> original;
                try (Connection su = pg.createConnection("")) {
                    original = queueShape(su);
                }
                assertThat(original).as("non-vacuity: the shape probe read columns, indexes, constraints, a policy")
                    .anyMatch(l -> l.startsWith("col id "))
                    .anyMatch(l -> l.startsWith("col next_retry_at "))
                    .anyMatch(l -> l.startsWith("idx "))
                    .anyMatch(l -> l.startsWith("con fk_aspect_queue_catalog_doc"))
                    .anyMatch(l -> l.startsWith("con aspect_extraction_queue_collection_fk"))
                    .anyMatch(l -> l.startsWith("pol tenant_isolation"))
                    .anyMatch(l -> l.startsWith("rls "));
                assertThat(original.stream().filter(l -> l.startsWith("col ")).count())
                    .as("thirteen columns, next_retry_at last").isEqualTo(13L);

                migrateUpTo(adminDs, DROP_CHANGESET);
                tag(adminDs, "after-aspects-005-1");
                List<String> afterGuard;
                try (Connection su = pg.createConnection("")) {
                    afterGuard = queueShape(su);
                }
                String fkPrefix = "con aspect_extraction_queue_collection_fk";
                assertThat(afterGuard.stream().filter(l -> !l.startsWith(fkPrefix)).toList())
                    .as("aspects-005-1 changes the collection foreign key and nothing else")
                    .containsExactlyElementsOf(original.stream().filter(l -> !l.startsWith(fkPrefix)).toList());
                assertThat(afterGuard.stream().filter(l -> l.startsWith(fkPrefix)).toList())
                    .singleElement().asString().contains("ON UPDATE CASCADE").contains("ON DELETE CASCADE")
                    .as("validated: the block validates the swapped constraint").doesNotContain("NOT VALID");
                assertThat(original.stream().filter(l -> l.startsWith(fkPrefix)).toList())
                    .singleElement().asString().contains("ON DELETE RESTRICT").doesNotContain("CASCADE");
                SchemaMigrator.migrate(adminDs);
                try (Connection su = pg.createConnection("")) {
                    assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), SCHEMA, QUEUE)).isFalse();
                }

                // Roll back the drop (and the runAlways blocks after it): the table is back, empty, same shape.
                rollbackTo(adminDs, "after-aspects-005-1");
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(changesetRecorded(su, DROP_CHANGESET)).isFalse();
                    assertThat(changesetRecorded(su, GUARD_CHANGESET)).isTrue();
                    assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER))
                        .as("the rollback brings the helper back: the state after aspects-005-1").isTrue();
                    assertThat(ctx.fetchCount(QUEUE_TABLE)).as("empty").isEqualTo(0);
                    assertThat(queueShape(su)).as("the recreated table has the shape the drop removed")
                        .containsExactlyElementsOf(afterGuard);
                }

                // An update after the rollback drops it again (the helper is there for the guard).
                SchemaMigrator.migrate(adminDs);
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, QUEUE)).isFalse();
                    assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER)).isFalse();
                    assertThat(changesetRecorded(su, DROP_CHANGESET)).isTrue();
                }

                // Roll back to before aspects-005-1: the table is there in the original shape and no helper.
                rollbackTo(adminDs, "before-aspects-005");
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(changesetRecorded(su, GUARD_CHANGESET)).isFalse();
                    assertThat(PgCatalogProbes.routineExists(ctx, SCHEMA, HELPER)).isFalse();
                    assertThat(queueShape(su)).containsExactlyElementsOf(original);
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /** The shape of the queue table: columns with type, nullability and default; indexes; constraints; policy; RLS. */
    private static List<String> queueShape(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        List<String> shape = new ArrayList<>();
        for (String column : PgCatalogProbes.columnNames(ctx, SCHEMA, QUEUE)) {
            var info = PgCatalogProbes.columnInfo(ctx, SCHEMA, QUEUE, column);
            shape.add("col " + column + " " + info.dataType() + " null=" + info.isNullable()
                + " default=" + info.columnDefault());
        }
        List<String> indexes = new ArrayList<>();
        for (String def : PgCatalogProbes.indexDefs(ctx, SCHEMA, QUEUE)) {
            indexes.add("idx " + def);
        }
        java.util.Collections.sort(indexes);
        shape.addAll(indexes);
        List<String> constraints = new ArrayList<>();
        for (var c : PgCatalogProbes.constraintDefinitionsIn(ctx, List.of(SCHEMA))) {
            if (c.table().equals(QUEUE)) {
                constraints.add("con " + c.conname() + " = " + c.definition());
            }
        }
        java.util.Collections.sort(constraints);
        shape.addAll(constraints);
        for (var p : PgCatalogProbes.policies(ctx, SCHEMA, QUEUE)) {
            shape.add("pol " + p.policyname() + " " + p.cmd() + " | " + p.qual() + " | " + p.withCheck());
        }
        var rls = PgCatalogProbes.rowSecurity(ctx, SCHEMA, QUEUE);
        shape.add("rls enabled=" + rls.enabled() + " forced=" + rls.forced());
        return shape;
    }

    private static Long helperRows(DSLContext ctx) {
        return ctx.select(HELPER_ROWS).fetchOne(HELPER_ROWS);
    }

    /** One queue row, seeded as superuser (BYPASSRLS) with the two parents its foreign keys name. */
    private static void seedOneRow(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
        PgContainerHelper.insertCatalogDocument(ctx, TENANT, DOC);
        ctx.insertInto(QUEUE_TABLE, Q_TENANT, Q_COLLECTION, Q_SOURCE_PATH, Q_DOC_ID, Q_ENQUEUED_AT)
            .values(TENANT, COLLECTION, "/legacy/one.pdf", DOC, OffsetDateTime.now())
            .execute();
    }

    private static HikariDataSource adminPool(PostgreSQLContainer<?> pg) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-aspq-retire");
        return new HikariDataSource(cfg);
    }

    /** Applies the HEAD changelog up to, but not including, {@code changesetId}. */
    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun =
                    liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId)
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    private static void tag(HikariDataSource ds, String tag) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.tag(tag);
            }
        }
    }

    private static void rollbackTo(HikariDataSource ds, String tag) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.rollback(tag, new Contexts(), new LabelExpression());
            }
        }
    }

    private static boolean changesetRecorded(Connection conn, String changesetId) {
        return DSL.using(conn, SQLDialect.POSTGRES).fetchExists(
            DSL.selectOne().from(DATABASECHANGELOG).where(DBCL_ID.eq(changesetId)));
    }
}
