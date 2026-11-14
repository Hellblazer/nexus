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
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Bead nexus-6pfgb.42: {@code telemetry-019-1} retires {@code nexus.frecency}. Three cases, each on its own empty
 * database owned by a NOBYPASSRLS, non-superuser role (the production migration role's shape), walked to just before
 * the changeset.
 *
 * <ul>
 *   <li><b>The guard.</b> A view the migrating role does not own that still reads the table is not touched by
 *       taxonomy-011-8, so the DROP would fail with a bare dependency error. The changeset's own check fails first,
 *       names the view and the remedy, drops nothing, and is not recorded; once the view is gone the next boot drops
 *       the table.</li>
 *   <li><b>The self-heal.</b> A five-leg {@code nexus.diag_chash_conformance} the migrating role owns (what
 *       vectors-030 leaves on a long hop) is re-pointed at four legs by taxonomy-011-8 on the next walk, which is
 *       why the guard's remedy for an owned view is one restart.</li>
 *   <li><b>The rollback.</b> The recreated table is read against the table as the older changesets left it: columns
 *       (type, nullability, default, comment), indexes, constraints, policy, RLS flags and owner. It is empty, whatever
 *       the dropped table held.</li>
 * </ul>
 */
class Telemetry019DropFrecencyLiquibaseTest {

    private static final String ADMIN_ROLE = "nexus_admin_frec_retire";
    private static final String ADMIN_PASS = "nexus_admin_frec_retire_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";

    private static final String RETIRE_CHANGESET = "telemetry-019-1";
    /** A changeset that runs after telemetry-019-1 in the master changelog: the walk went past the drop. */
    private static final String LATER_CHANGESET = "grants-nexus-diag-3";

    private static final String SCHEMA = "nexus";
    private static final String FRECENCY = "frecency";
    private static final String DIAG_VIEW = "diag_chash_conformance";
    private static final String FOREIGN_VIEW = "stale_frecency_reader";
    private static final Table<?> FRECENCY_TABLE = DSL.table(DSL.name(SCHEMA, FRECENCY));
    private static final Table<?> CHUNKS_TABLE = DSL.table(DSL.name(SCHEMA, "chunks"));
    private static final Field<String> F_TENANT = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> F_CHUNK = DSL.field(DSL.name("chunk_id"), String.class);

    private static final Table<?> DATABASECHANGELOG = DSL.table(DSL.name("public", "databasechangelog"));
    private static final Field<String> DBCL_ID = DSL.field(DSL.name("id"), String.class);

    // ── the guard ───────────────────────────────────────────────────────────

    @Test
    void aViewAnotherRoleOwnsThatStillReadsTheTableFailsTheChangesetWithTheRemedyNamed_andNothingIsDropped()
            throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, RETIRE_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, FRECENCY)).as("premise: before the drop").isTrue();
                    // Created by the superuser, so the migrating role does not own it and taxonomy-011-8 cannot
                    // touch it: the foreign view of the guard's contract.
                    ctx.createView(DSL.name(SCHEMA, FOREIGN_VIEW)).as(DSL.select(F_CHUNK).from(FRECENCY_TABLE)).execute();
                    assertThat(PgCatalogProbes.relationOwner(ctx, SCHEMA, FOREIGN_VIEW))
                        .as("premise: not owned by the migrating role").isNotEqualTo(ADMIN_ROLE);
                }

                assertThatThrownBy(() -> SchemaMigrator.migrate(adminDs))
                    .as("the guard fails the walk")
                    .satisfies(e -> assertThat(messageChain(e))
                        .as("the guard's own message, not the bare DROP dependency error")
                        .contains("nexus-6pfgb.42: nexus.frecency cannot be dropped while nexus." + FOREIGN_VIEW + " reads it")
                        .contains("restart the engine once")
                        .contains("owning role must re-create it without nexus.frecency")
                        .doesNotContain("cannot drop table nexus.frecency because other objects depend on it"));

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, FRECENCY)).as("nothing was dropped").isTrue();
                    assertThat(changesetRecorded(su, RETIRE_CHANGESET)).as("and nothing was recorded").isFalse();
                    assertThat(changesetRecorded(su, LATER_CHANGESET)).as("the walk stopped there").isFalse();
                    // The owner re-creates the view without the table; the next boot drops it.
                    ctx.dropView(DSL.name(SCHEMA, FOREIGN_VIEW)).execute();
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                try (Connection su = pg.createConnection("")) {
                    assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), SCHEMA, FRECENCY))
                        .as("with the view gone the retry drops the table").isFalse();
                    assertThat(changesetRecorded(su, RETIRE_CHANGESET)).isTrue();
                    assertThat(changesetRecorded(su, LATER_CHANGESET)).as("the walk finished").isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── the self-heal the remedy relies on ──────────────────────────────────

    @Test
    void aFiveLegDiagViewTheMigratingRoleOwnsIsRepointedByTheSameWalk_andTheDropSucceeds() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, RETIRE_CHANGESET);
                try (Connection admin = adminDs.getConnection()) {
                    DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.relationOwner(ctx, SCHEMA, DIAG_VIEW))
                        .as("premise: taxonomy-011-8 made the view, owned by the migrating role").isEqualTo(ADMIN_ROLE);
                    // The view as vectors-030 leaves it on a long hop: a leg that reads nexus.frecency.
                    ctx.createOrReplaceView(DSL.name(SCHEMA, DIAG_VIEW)).as(
                        DSL.select(DSL.inline("nexus.chunks").as("table_name"), DSL.count().as("non_conformant"))
                            .from(CHUNKS_TABLE)
                            .unionAll(DSL.select(DSL.inline("nexus.frecency").as("table_name"), DSL.count().as("non_conformant"))
                                .from(FRECENCY_TABLE))).execute();
                    assertThat(PgCatalogProbes.viewDefinition(ctx, SCHEMA, DIAG_VIEW))
                        .as("non-vacuity: the planted view reads the table").contains("frecency");
                }

                assertThatCode(() -> SchemaMigrator.migrate(adminDs))
                    .as("taxonomy-011-8 re-points the owned view before telemetry-019-1 looks").doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(PgCatalogProbes.tableExists(ctx, SCHEMA, FRECENCY)).as("dropped").isFalse();
                    assertThat(PgCatalogProbes.viewDefinition(ctx, SCHEMA, DIAG_VIEW))
                        .as("the view survives, at four legs, with no frecency leg")
                        .isNotNull().doesNotContain("frecency").contains("relevance_log");
                    assertThat(changesetRecorded(su, RETIRE_CHANGESET)).isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── the rollback ────────────────────────────────────────────────────────

    @Test
    void theRollbackRecreatesTheEmptyTableWithTheShapeItHadBeforeTheDrop() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, RETIRE_CHANGESET);
                tag(adminDs, "before-telemetry-019");
                List<String> original;
                try (Connection su = pg.createConnection("")) {
                    original = frecencyShape(su);
                    // One row, so the drop discards something and the rollback has something to not bring back.
                    DSL.using(su, SQLDialect.POSTGRES).insertInto(FRECENCY_TABLE, F_TENANT, F_CHUNK)
                        .values("frec-retire", "a".repeat(64)).execute();
                }
                assertThat(original).as("non-vacuity: the shape probe read every kind of object")
                    .anyMatch(l -> l.startsWith("col tenant_id text null=NO "))
                    .anyMatch(l -> l.startsWith("col chunk_id text null=NO "))
                    .anyMatch(l -> l.startsWith("col ttl_days integer null=YES "))
                    .anyMatch(l -> l.startsWith("col frecency_score double precision null=NO default=0"))
                    .anyMatch(l -> l.startsWith("comment chunk_id No FK, by GRAIN"))
                    .anyMatch(l -> l.startsWith("idx CREATE INDEX idx_frecency_score"))
                    .anyMatch(l -> l.startsWith("con frecency_pk = PRIMARY KEY"))
                    .anyMatch(l -> l.startsWith("con frecency_ttl_days_positive_chk"))
                    .anyMatch(l -> l.startsWith("con frecency_chunk_id_canonical_check"))
                    .anyMatch(l -> l.startsWith("pol tenant_isolation"))
                    .anyMatch(l -> l.equals("rls enabled=true forced=true"))
                    .anyMatch(l -> l.equals("owner " + ADMIN_ROLE));
                assertThat(original.stream().filter(l -> l.startsWith("col ")).count())
                    .as("seven columns").isEqualTo(7L);
                assertThat(original.stream().filter(l -> l.startsWith("idx ")).count())
                    .as("the primary key's index and the three named ones").isEqualTo(4L);

                SchemaMigrator.migrate(adminDs);
                try (Connection su = pg.createConnection("")) {
                    assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), SCHEMA, FRECENCY))
                        .as("dropped, rows and all").isFalse();
                }

                rollbackTo(adminDs, "before-telemetry-019");
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(changesetRecorded(su, RETIRE_CHANGESET)).isFalse();
                    assertThat(ctx.fetchCount(FRECENCY_TABLE)).as("empty: the rows are not recoverable").isEqualTo(0);
                    assertThat(frecencyShape(su)).as("the recreated table has the shape the drop removed")
                        .containsExactlyElementsOf(original);
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────

    /**
     * The shape of nexus.frecency: columns with type, nullability and default, column comments, indexes,
     * constraints, policies, RLS flags and owner. nexus_svc's table privilege is the runAlways grants block's and is
     * not compared.
     */
    private static List<String> frecencyShape(Connection su) {
        DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
        List<String> shape = new ArrayList<>();
        for (String column : PgCatalogProbes.columnNames(ctx, SCHEMA, FRECENCY)) {
            var info = PgCatalogProbes.columnInfo(ctx, SCHEMA, FRECENCY, column);
            shape.add("col " + column + " " + info.dataType() + " null=" + info.isNullable()
                + " default=" + info.columnDefault());
        }
        for (String column : PgCatalogProbes.columnNames(ctx, SCHEMA, FRECENCY)) {
            String comment = PgCatalogProbes.columnComment(ctx, SCHEMA, FRECENCY, column);
            if (comment != null) {
                shape.add("comment " + column + " " + comment);
            }
        }
        List<String> indexes = new ArrayList<>();
        for (String def : PgCatalogProbes.indexDefs(ctx, SCHEMA, FRECENCY)) {
            indexes.add("idx " + def);
        }
        Collections.sort(indexes);
        shape.addAll(indexes);
        List<String> constraints = new ArrayList<>();
        for (var c : PgCatalogProbes.constraintDefinitionsIn(ctx, List.of(SCHEMA))) {
            if (c.table().equals(FRECENCY)) {
                constraints.add("con " + c.conname() + " = " + c.definition());
            }
        }
        Collections.sort(constraints);
        shape.addAll(constraints);
        for (var p : PgCatalogProbes.policies(ctx, SCHEMA, FRECENCY)) {
            shape.add("pol " + p.policyname() + " " + p.cmd() + " | " + p.qual() + " | " + p.withCheck());
        }
        var rls = PgCatalogProbes.rowSecurity(ctx, SCHEMA, FRECENCY);
        shape.add("rls enabled=" + rls.enabled() + " forced=" + rls.forced());
        shape.add("owner " + PgCatalogProbes.relationOwner(ctx, SCHEMA, FRECENCY));
        return shape;
    }

    /** Every message in the cause chain, joined: the guard's text sits under the migrator's wrapper. */
    private static String messageChain(Throwable t) {
        StringBuilder sb = new StringBuilder();
        for (Throwable c = t; c != null; c = c.getCause()) {
            sb.append(c.getMessage()).append('\n');
        }
        return sb.toString();
    }

    private static HikariDataSource adminPool(PostgreSQLContainer<?> pg) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-frec-retire");
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
