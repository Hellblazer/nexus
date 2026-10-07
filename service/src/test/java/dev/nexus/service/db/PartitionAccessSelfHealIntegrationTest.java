// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.jooq.test.Routines;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-225 (nexus-3wh8d.16, review item M2): {@code SchemaMigrator.migrate} re-mirrors partition access drift on
 * every boot, as the schema owner, so the isolation check at startup is a pure assertion for the drift the
 * repair can fix.
 *
 * <p>Production shape: a NOSUPERUSER schema-owner role migrates, so the partition functions (revoked from PUBLIC)
 * are owned by, and executable by, the migrating role. Every defect is injected by owner DDL through the
 * {@code nexus_test.*} wrappers on a store the migration already walked, then {@code migrate} runs again.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PartitionAccessSelfHealIntegrationTest {

    private static final String ADMIN = "nexus_admin_selfheal";
    private static final String ADMIN_PASS = "nexus_admin_selfheal_pass";
    private static final String TENANT = "selfheal-tenant";
    private static final List<String> PARENTS = List.of("chunks", "taxonomy_centroids");
    private static final String WARN_EVENT = "event=partition_access_drift_repaired";
    private static final String ERROR_EVENT = "event=partition_access_drift_unrepaired";

    PostgreSQLContainer<?> pg;
    HikariDataSource adminDs;
    HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.startDedicated();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN, ADMIN_PASS);
        }
        adminDs = pool(ADMIN, ADMIN_PASS, 2);
        SchemaMigrator.migrate(adminDs);
        try (Connection owner = adminDs.getConnection()) {
            PgContainerHelper.installTestObjects(owner);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(ctx, "boot-selfheal", TenantConstants.DEFAULT_TENANT, "boot");
            PgContainerHelper.seedServiceToken(ctx, "tok-" + TENANT, TENANT, "selfheal");
        }
        svcDs = pool(PgContainerHelper.SVC_USERNAME, PgContainerHelper.SVC_PASSWORD, 2);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    private HikariDataSource pool(String user, String pass, int size) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(user);
        cfg.setPassword(pass);
        cfg.setMaximumPoolSize(size);
        cfg.setAutoCommit(true);
        return new HikariDataSource(cfg);
    }

    // ── the tree, read from the catalog ──────────────────────────────────────────────────────────────────────

    private record Tree(String parent, List<String> models, List<String> leaves) {}

    private Tree tree(String parent) throws SQLException {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            List<String> models = new ArrayList<>();
            List<String> leaves = new ArrayList<>();
            for (String m : children(ctx, parent)) {
                models.add(m);
                leaves.addAll(children(ctx, m));
            }
            return new Tree(parent, models, leaves);
        }
    }

    private static List<String> children(DSLContext ctx, String parentRel) {
        var pgClass = DSL.table(DSL.name("pg_catalog", "pg_class"));
        var inherits = DSL.table(DSL.name("pg_catalog", "pg_inherits")).as("i");
        var child = pgClass.as("c");
        var parent = pgClass.as("p");
        var ns = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
        return ctx.select(DSL.field(DSL.name("c", "relname"), String.class))
            .from(inherits)
            .join(child).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "inhrelid"))))
            .join(parent).on(DSL.field(DSL.name("p", "oid")).eq(DSL.field(DSL.name("i", "inhparent"))))
            .join(ns).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("p", "relname"), String.class).eq(parentRel))
            .orderBy(DSL.field(DSL.name("c", "relname")))
            .fetch(r -> r.get(0, String.class));
    }

    /**
     * Everything the repair could touch, as text: per relation of both trees its flags, owner and ACL, and per
     * policy its OID (a dropped-and-recreated policy has a new one) and definition.
     */
    private List<String> catalogSnapshot() throws SQLException {
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
            var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            List<String> out = new ArrayList<>();
            for (String parent : PARENTS) {
                Tree t = tree(parent);
                List<String> rels = new ArrayList<>();
                rels.add(parent);
                rels.addAll(t.models());
                rels.addAll(t.leaves());
                for (String rel : rels) {
                    out.add("class " + ctx.select(DSL.field(DSL.name("c", "relname"), String.class),
                                DSL.field(DSL.name("c", "relrowsecurity"), Boolean.class),
                                DSL.field(DSL.name("c", "relforcerowsecurity"), Boolean.class),
                                DSL.field(DSL.name("c", "relowner")).cast(String.class),
                                DSL.field(DSL.name("c", "relacl")).cast(String.class))
                        .from(c).join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                        .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                        .and(DSL.field(DSL.name("c", "relname"), String.class).eq(rel))
                        .fetchOne().formatCSV());
                }
            }
            var p = DSL.table(DSL.name("pg_catalog", "pg_policy")).as("p");
            var pc = DSL.table(DSL.name("pg_catalog", "pg_class")).as("pc");
            out.addAll(ctx.select(DSL.field(DSL.name("p", "oid")).cast(String.class),
                        DSL.field(DSL.name("pc", "relname"), String.class),
                        DSL.field(DSL.name("p", "polname"), String.class),
                        DSL.field(DSL.name("p", "polcmd")).cast(String.class),
                        DSL.field(DSL.name("p", "polroles")).cast(String.class))
                .from(p).join(pc).on(DSL.field(DSL.name("pc", "oid")).eq(DSL.field(DSL.name("p", "polrelid"))))
                .join(n.as("pn")).on(DSL.field(DSL.name("pn", "oid")).eq(DSL.field(DSL.name("pc", "relnamespace"))))
                .where(DSL.field(DSL.name("pn", "nspname"), String.class).eq("nexus"))
                .orderBy(DSL.field(DSL.name("pc", "relname")), DSL.field(DSL.name("p", "polname")))
                .fetch(r -> "policy " + r.formatCSV()));
            return out;
        }
    }

    // ── log capture around one migrate ───────────────────────────────────────────────────────────────────────

    private record Logged(Level level, String message) {}

    private List<Logged> migrateCapturingLogs() {
        var root = (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        var logs = new ListAppender<ILoggingEvent>();
        logs.start();
        root.addAppender(logs);
        try {
            SchemaMigrator.migrate(adminDs);
            return logs.list.stream().map(e -> new Logged(e.getLevel(), e.getFormattedMessage())).toList();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    private static List<Logged> at(List<Logged> all, Level level, String event) {
        return all.stream().filter(l -> l.level() == level && l.message().contains(event)).toList();
    }

    private List<ChunksIsolationCheck.Gap> gaps() throws SQLException {
        try (Connection c = svcDs.getConnection()) {
            return ChunksIsolationCheck.structure(DSL.using(c, SQLDialect.POSTGRES)).gaps();
        }
    }

    // ── injected defects ─────────────────────────────────────────────────────────────────────────────────────

    private static String q(String relation) {
        return "nexus." + relation;
    }

    private void ownerDdl(java.util.function.BiConsumer<DSLContext, String> ddl, String relation) throws SQLException {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            ddl.accept(DSL.using(su, SQLDialect.POSTGRES), relation);
        }
    }

    enum Level2 { MODEL_PARTITION, LEAF }

    enum Defect {
        NO_FORCE, POLICY_DROPPED, NO_RLS
    }

    private void inject(Defect d, String relation) throws SQLException {
        switch (d) {
            case NO_FORCE -> ownerDdl((c, r) -> Routines.setForceRls(c.configuration(), q(r), false), relation);
            case NO_RLS -> ownerDdl((c, r) -> Routines.setRowSecurity(c.configuration(), q(r), false), relation);
            case POLICY_DROPPED -> ownerDdl((c, r) -> Routines.dropPolicy(c.configuration(), q(r), "tenant_isolation"), relation);
        }
    }

    static Stream<Object[]> defects() {
        return Stream.of(
            new Object[] {"chunks", Level2.LEAF, Defect.NO_FORCE},
            new Object[] {"chunks", Level2.MODEL_PARTITION, Defect.POLICY_DROPPED},
            new Object[] {"taxonomy_centroids", Level2.LEAF, Defect.POLICY_DROPPED},
            new Object[] {"taxonomy_centroids", Level2.MODEL_PARTITION, Defect.NO_RLS});
    }

    @Test
    void aHealthyStore_isReadOnceAndNothingChanges_noWarn() throws Exception {
        assertThat(gaps()).as("control: the fixture is clean before the migrate").isEmpty();
        List<String> before = catalogSnapshot();
        assertThat(before.size()).as("non-vacuity: the snapshot covers both trees").isGreaterThan(30);

        List<Logged> logs = migrateCapturingLogs();

        assertThat(catalogSnapshot()).as("a healthy boot changes no relation flag, ACL or policy").isEqualTo(before);
        assertThat(logs.stream().filter(l -> l.level().isGreaterOrEqual(Level.WARN)
                    && l.message().contains("partition_access_drift")).toList())
            .as("no drift event at WARN or above on a healthy boot").isEmpty();
        assertThat(logs.stream().filter(l -> l.message().contains("event=partition_access_checked")).toList())
            .as("one info line carrying the inspected count").hasSize(1);
    }

    @ParameterizedTest(name = "{0} {1} {2}")
    @MethodSource("defects")
    void aDefectOnAModelPartitionOrLeaf_isRepairedByMigrate_andTheStartupCheckPasses(
            String parent, Level2 level, Defect d) throws Exception {
        Tree t = tree(parent);
        String relation = level == Level2.LEAF ? t.leaves().get(t.leaves().size() - 1) : t.models().get(0);
        assertThat(gaps()).as("control: clean before the injection").isEmpty();
        inject(d, relation);
        assertThat(gaps()).as("the injection is a real gap on nexus.%s", relation)
            .extracting(ChunksIsolationCheck.Gap::relation).contains(relation);

        List<Logged> logs = migrateCapturingLogs();

        assertThat(gaps()).as("migrate re-mirrored the tree").isEmpty();
        List<Logged> warns = at(logs, Level.WARN, WARN_EVENT);
        assertThat(warns).as("one WARN naming the repaired relation").hasSize(1);
        assertThat(warns.get(0).message()).contains(relation).containsPattern("count=[1-9]");
        assertThat(at(logs, Level.ERROR, ERROR_EVENT)).as("nothing left over").isEmpty();
        ChunksIsolationCheck.verifyAtStartup(svcDs);
    }

    @Test
    void aDriftOnAParent_isNotRepairable_migrateLogsAnErrorAndDoesNotThrow_theStartupCheckRefuses() throws Exception {
        assertThat(gaps()).isEmpty();
        ownerDdl((c, r) -> Routines.setForceRls(c.configuration(), q(r), false), "chunks");
        try {
            List<Logged> logs = migrateCapturingLogs();
            List<Logged> errors = at(logs, Level.ERROR, ERROR_EVENT);
            assertThat(errors).as("the gap the copy cannot reach is reported, not thrown").hasSize(1);
            assertThat(errors.get(0).message()).contains("chunks");
            assertThat(gaps()).as("the parent is still wrong: the sync copies FROM it")
                .extracting(ChunksIsolationCheck.Gap::relation).containsExactly("chunks");
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svcDs))
                .isInstanceOf(ChunksIsolationCheck.IsolationException.class);
        } finally {
            ownerDdl((c, r) -> Routines.setForceRls(c.configuration(), q(r), true), "chunks");
        }
        assertThat(gaps()).as("restored").isEmpty();
    }
}
