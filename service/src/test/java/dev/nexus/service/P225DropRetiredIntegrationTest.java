// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Phase 3 Step 3 (nexus-3wh8d.22): {@code vectors-036-1} drops {@code nexus.chunks_retired_225} and
 * {@code nexus.taxonomy_centroids_retired_225}, the pre-walk tables {@code vectors-030-1} kept as a recovery source.
 *
 * <p>Each test seeds a store through the pre-walk layout, applies the changelog up to, not including, the drop
 * (so the walk has run and the retired tables hold the seed), and then applies the drop alone with
 * {@code update(1)}. The migrating role is the production one: NOSUPERUSER, NOBYPASSRLS. The retired tables'
 * shape is read from the catalog, not written down by hand, so the rollback is held to what the walk leaves.
 */
class P225DropRetiredIntegrationTest {

    private static final String ADMIN = "nexus_admin_p225drop";
    private static final String ADMIN_PASS = "nexus_admin_p225drop_pass";
    private static final String MASTER = "db/changelog/db.changelog-master.xml";
    private static final String DROP = "vectors-036-1";
    private static final List<String> RETIRED = List.of("chunks_retired_225", "taxonomy_centroids_retired_225");

    @Test
    void theDropRemovesBothTables_logsTheirRowCounts_andItsRollbackRestoresTheShapeEmpty() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, "vectors-030-1");
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    P225MigrationWalkIntegrationTest.seedStore(DSL.using(su, SQLDialect.POSTGRES));
                }
                migrateUpTo(ds, DROP);

                List<String> before = new ArrayList<>();
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String t : RETIRED) {
                        assertThat(PgCatalogProbes.tableExists(ctx, "nexus", t)).as(t + " exists after the walk").isTrue();
                        before.addAll(shape(ctx, t));
                    }
                    // The snapshot is not vacuous: it saw columns, indexes, constraints, policies and FORCE.
                    String all = String.join("\n", before);
                    assertThat(all).contains("column ", "index ", "constraint ", "policy ", "rls true/true", "owner " + ADMIN);
                    assertThat(shapeCount(before, "chunks_retired_225", "index ")).isEqualTo(7);
                    assertThat(shapeCount(before, "taxonomy_centroids_retired_225", "index ")).isEqualTo(4);
                }

                List<String> log = new CopyOnWriteArrayList<>();
                withEngineLog(log, () -> applyDrop(ds));

                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String t : RETIRED) {
                        assertThat(PgCatalogProbes.tableExists(ctx, "nexus", t)).as(t + " dropped").isFalse();
                    }
                    assertThat(PgCatalogProbes.tablesInSchema(ctx, "nexus").stream().filter(n -> n.contains("retired_225")).toList())
                        .as("no relation named for the retired tables is left").isEmpty();
                    assertThat(PgCatalogProbes.pgClassRowsNamed(ctx, "chunks_pk_retired_225")).as("their indexes went with them").isZero();
                    assertThat(PgCatalogProbes.constraintCountLike(ctx, "%\\_retired\\_225")).as("and their constraints").isZero();
                    // The live tables are untouched.
                    assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "chunks")).isTrue();
                    assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "taxonomy_centroids")).isTrue();
                }
                String all = String.join("\n", log);
                assertThat(all).contains("vectors-036-1: dropping nexus.chunks_retired_225 (12 row(s))")
                    .contains("vectors-036-1: dropping nexus.taxonomy_centroids_retired_225 (6 row(s))");

                rollback(ds, 1);
                List<String> after = new ArrayList<>();
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String t : RETIRED) {
                        assertThat(PgCatalogProbes.tableExists(ctx, "nexus", t)).as(t + " is back").isTrue();
                        assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", t)))).as(t + " comes back empty").isZero();
                        after.addAll(shape(ctx, t));
                    }
                }
                assertThat(after).as("the recreated tables have the shape vectors-030-1 leaves").isEqualTo(before);

                // And forward again, the drop applies to the recreated (empty) tables.
                withEngineLog(log, () -> applyDrop(ds));
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String t : RETIRED) {
                        assertThat(PgCatalogProbes.tableExists(ctx, "nexus", t)).isFalse();
                    }
                }
                assertThat(String.join("\n", log)).contains("dropping nexus.chunks_retired_225 (0 row(s))");
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void theDropIsANoOpWhenTheTablesAreAlreadyAbsent() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, DROP);
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String t : RETIRED) {
                        ctx.dropTable(DSL.name("nexus", t)).execute();
                    }
                }
                List<String> log = new CopyOnWriteArrayList<>();
                withEngineLog(log, () -> applyDrop(ds));
                assertThat(String.join("\n", log)).doesNotContain("vectors-036-1: dropping");
                try (Connection su = pg.createConnection("")) {
                    assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), "nexus", "chunks")).isTrue();
                }
                // Rolling it back where the tables were never there still recreates them: the rollback is
                // unconditional shape, so the older rollbacks that need the retired tables find them.
                rollback(ds, 1);
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String t : RETIRED) {
                        assertThat(PgCatalogProbes.tableExists(ctx, "nexus", t)).as(t).isTrue();
                    }
                }
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aViewStillReadingARetiredTableFailsTheDropLoudly_andLeavesBothTablesInPlace() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, ADMIN, ADMIN_PASS);
            try (HikariDataSource ds = pool(pg)) {
                migrateUpTo(ds, DROP);
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    ctx.createView(DSL.name("nexus", "p225_drop_blocker"))
                        .as(ctx.select(DSL.field(DSL.name("tenant_id"), String.class)).from(DSL.table(DSL.name("nexus", "chunks_retired_225"))))
                        .execute();
                }
                assertThatThrownBy(() -> applyDrop(ds)).hasStackTraceContaining("p225_drop_blocker");
                try (Connection su = pg.createConnection("")) {
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (String t : RETIRED) {
                        assertThat(PgCatalogProbes.tableExists(ctx, "nexus", t)).as(t + " untouched").isTrue();
                    }
                    // The NO FORCE the count needed rolled back with the failed changeset.
                    assertThat(PgCatalogProbes.rowSecurity(ctx, "nexus", "chunks_retired_225").forced()).isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ═══════════════════════════ the shape snapshot ═══════════════════════════

    /**
     * Everything the catalog says about one nexus relation that a rollback must restore: columns in order (type,
     * NOT NULL, default, generated expression, identity, storage), indexes (name and definition, primary flag),
     * constraints (name, type, definition, validated, deferrable, backing index), policies, RLS enabled and
     * FORCEd, reloptions, ACL, owner, kind, persistence, replica identity, tablespace, non-internal triggers,
     * extended statistics and the table comment. Each line starts with the relation name.
     */
    static List<String> shape(DSLContext ctx, String rel) {
        List<String> out = new ArrayList<>();
        var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
        var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        Condition inRel = DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus")
            .and(DSL.field(DSL.name("c", "relname"), String.class).eq(rel))
            .and(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))));

        // columns, in order
        var a = DSL.table(DSL.name("pg_catalog", "pg_attribute")).as("a");
        var d = DSL.table(DSL.name("pg_catalog", "pg_attrdef")).as("d");
        Field<String> type = DSL.function("format_type", String.class, DSL.field(DSL.name("a", "atttypid")), DSL.field(DSL.name("a", "atttypmod")));
        Field<String> dflt = DSL.function("pg_get_expr", String.class, DSL.field(DSL.name("d", "adbin")), DSL.field(DSL.name("d", "adrelid")));
        for (Record r : ctx.select(DSL.field(DSL.name("a", "attname"), String.class), type,
                DSL.field(DSL.name("a", "attnotnull"), Boolean.class), dflt,
                DSL.field(DSL.name("a", "attgenerated"), String.class), DSL.field(DSL.name("a", "attidentity"), String.class),
                DSL.field(DSL.name("a", "attstorage"), String.class), DSL.field(DSL.name("a", "attstattarget"), Integer.class))
            .from(c).join(n).on(DSL.trueCondition())
            .join(a).on(DSL.field(DSL.name("a", "attrelid")).eq(DSL.field(DSL.name("c", "oid"))))
            .leftJoin(d).on(DSL.field(DSL.name("d", "adrelid")).eq(DSL.field(DSL.name("a", "attrelid")))
                .and(DSL.field(DSL.name("d", "adnum")).eq(DSL.field(DSL.name("a", "attnum")))))
            .where(inRel)
            .and(DSL.field(DSL.name("a", "attnum"), Integer.class).gt(0))
            .and(DSL.not(DSL.field(DSL.name("a", "attisdropped"), Boolean.class)))
            .orderBy(DSL.field(DSL.name("a", "attnum")))
            .fetch()) {
            out.add(rel + " column " + r.intoList());
        }

        // indexes
        var ix = DSL.table(DSL.name("pg_catalog", "pg_indexes")).as("ix");
        var ic = DSL.table(DSL.name("pg_catalog", "pg_class")).as("ic");
        var pi = DSL.table(DSL.name("pg_catalog", "pg_index")).as("pi");
        for (Record r : ctx.select(DSL.field(DSL.name("ix", "indexname"), String.class), DSL.field(DSL.name("ix", "indexdef"), String.class),
                DSL.field(DSL.name("pi", "indisprimary"), Boolean.class), DSL.field(DSL.name("pi", "indisvalid"), Boolean.class),
                DSL.field(DSL.name("ic", "reloptions")).cast(String.class))
            .from(ix)
            .join(ic).on(DSL.field(DSL.name("ic", "relname")).eq(DSL.field(DSL.name("ix", "indexname"))))
            .join(pi).on(DSL.field(DSL.name("pi", "indexrelid")).eq(DSL.field(DSL.name("ic", "oid"))))
            .where(DSL.field(DSL.name("ix", "schemaname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("ix", "tablename"), String.class).eq(rel))
            .orderBy(DSL.field(DSL.name("ix", "indexname")))
            .fetch()) {
            out.add(rel + " index " + r.intoList());
        }

        // constraints, with the index each is backed by
        var k = DSL.table(DSL.name("pg_catalog", "pg_constraint")).as("k");
        var kc = DSL.table(DSL.name("pg_catalog", "pg_class")).as("kc");
        var bi = DSL.table(DSL.name("pg_catalog", "pg_class")).as("bi");
        Field<String> def = DSL.function("pg_get_constraintdef", String.class, DSL.field(DSL.name("k", "oid")));
        for (Record r : ctx.select(DSL.field(DSL.name("k", "conname"), String.class), DSL.field(DSL.name("k", "contype"), String.class), def,
                DSL.field(DSL.name("k", "convalidated"), Boolean.class), DSL.field(DSL.name("k", "condeferrable"), Boolean.class),
                DSL.field(DSL.name("k", "condeferred"), Boolean.class), DSL.field(DSL.name("bi", "relname"), String.class))
            .from(k)
            .join(kc).on(DSL.field(DSL.name("kc", "oid")).eq(DSL.field(DSL.name("k", "conrelid"))))
            .leftJoin(bi).on(DSL.field(DSL.name("bi", "oid")).eq(DSL.field(DSL.name("k", "conindid"))).and(DSL.field(DSL.name("k", "contype"), String.class).in("p", "u", "x")))
            .where(DSL.field(DSL.name("kc", "relname"), String.class).eq(rel))
            .and(DSL.field(DSL.name("kc", "relnamespace")).eq(ctx.select(DSL.field(DSL.name("nn", "oid")))
                .from(DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("nn")).where(DSL.field(DSL.name("nn", "nspname"), String.class).eq("nexus"))))
            .orderBy(DSL.field(DSL.name("k", "conname")))
            .fetch()) {
            out.add(rel + " constraint " + r.intoList());
        }

        // policies, RLS
        PartitionScratch.policies(ctx, rel).forEach(p -> out.add(rel + " policy " + p));
        var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", rel);
        out.add(rel + " rls " + rls.enabled() + "/" + rls.forced());

        // table-level facts
        Record t = ctx.select(DSL.field(DSL.name("c", "relkind"), String.class), DSL.field(DSL.name("c", "relpersistence"), String.class),
                DSL.field(DSL.name("c", "relreplident"), String.class), DSL.field(DSL.name("c", "reltablespace")).cast(String.class),
                DSL.field(DSL.name("c", "reloptions")).cast(String.class), DSL.field(DSL.name("c", "relispartition"), Boolean.class),
                                DSL.function("obj_description", String.class, DSL.field(DSL.name("c", "oid")), DSL.inline("pg_class")))
            .from(c, n).where(inRel).fetchOne();
        out.add(rel + " table " + t.intoList());
        out.add(rel + " acl " + PartitionScratch.acl(ctx, rel));
        out.add(rel + " owner " + PgCatalogProbes.relationOwner(ctx, "nexus", rel));

        // triggers and extended statistics
        var tg = DSL.table(DSL.name("pg_catalog", "pg_trigger")).as("tg");
        out.add(rel + " triggers " + ctx.select(DSL.field(DSL.name("tg", "tgname"), String.class)).from(tg, c, n).where(inRel)
            .and(DSL.field(DSL.name("tg", "tgrelid")).eq(DSL.field(DSL.name("c", "oid"))))
            .and(DSL.not(DSL.field(DSL.name("tg", "tgisinternal"), Boolean.class)))
            .orderBy(DSL.field(DSL.name("tg", "tgname"))).fetch(0));
        var se = DSL.table(DSL.name("pg_catalog", "pg_statistic_ext")).as("se");
        out.add(rel + " ext_statistics " + ctx.select(DSL.field(DSL.name("se", "stxname"), String.class)).from(se, c, n).where(inRel)
            .and(DSL.field(DSL.name("se", "stxrelid")).eq(DSL.field(DSL.name("c", "oid")))).orderBy(DSL.field(DSL.name("se", "stxname"))).fetch(0));
        return out;
    }

    private static long shapeCount(List<String> shape, String rel, String kind) {
        return shape.stream().filter(l -> l.startsWith(rel + " " + kind)).count();
    }

    // ═══════════════════════════ plumbing ═══════════════════════════

    private static HikariDataSource pool(PostgreSQLContainer<?> c) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(c.getJdbcUrl());
        cfg.setUsername(ADMIN);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(3);
        return new HikariDataSource(cfg);
    }

    /** Captures what the engine's log would carry (Liquibase logs through java.util.logging; a NOTICE arrives as a SQLWarning). */
    private static void withEngineLog(List<String> sink, ThrowingRunnable body) throws Exception {
        java.util.logging.Logger jul = java.util.logging.Logger.getLogger("");
        java.util.logging.Handler capture = new java.util.logging.Handler() {
            @Override public void publish(java.util.logging.LogRecord r) {
                sink.add(r.getLevel() + " " + r.getLoggerName() + " " + new java.util.logging.SimpleFormatter().formatMessage(r));
            }
            @Override public void flush() { }
            @Override public void close() { }
        };
        jul.addHandler(capture);
        try {
            body.run();
        } finally {
            jul.removeHandler(capture);
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    /**
     * Applies the changelog up to and including {@code vectors-036-1}. The unrun list also holds the runAlways
     * changesets that sit before it, which run again first; the drop is the last one executed, so
     * {@code rollback(1)} rolls it back.
     */
    private static void applyDrop(HikariDataSource ds) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = lb.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = unrun.stream().map(ChangeSet::getId).toList().indexOf(DROP);
                assertThat(idx).as(DROP + " is unrun").isGreaterThanOrEqualTo(0);
                lb.update(idx + 1, new Contexts(), new LabelExpression());
            }
        }
    }

    private static void rollback(HikariDataSource ds, int count) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), database)) {
                lb.rollback(count, new Contexts(), new LabelExpression());
            }
        }
    }

    /** Applies the changelog up to, not including, {@code target}. */
    private static void migrateUpTo(HikariDataSource ds, String target) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase lb = new Liquibase(MASTER, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = lb.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (target.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as(target + " must be in the master changelog").isGreaterThanOrEqualTo(0);
                lb.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
