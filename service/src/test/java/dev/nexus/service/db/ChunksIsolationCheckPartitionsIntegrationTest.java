// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

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
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-225 Phase 2 Step 3 (nexus-3wh8d.16): the startup isolation check covers every relation of the partition
 * tree of {@code nexus.chunks} and {@code nexus.taxonomy_centroids}, not only the two parents.
 *
 * <p>PostgreSQL inherits neither row-level-security flags nor policies down a partition tree, and a leaf can be
 * queried directly, so the parents being right proves nothing about a model partition or a leaf. The check asserts
 * on each relation of the tree: row security enabled, FORCE set, and a policy set identical to the parent's.
 *
 * <p>Every defect below is injected as the schema owner on one relation of a freshly migrated, correct schema and
 * must make {@link ChunksIsolationCheck#verifyAtStartup} refuse naming the relation; the same relation is then
 * repaired and the same call must pass, so each refusal is shown to come from the injected defect and not from
 * the fixture.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChunksIsolationCheckPartitionsIntegrationTest {

    private static final String BOOT = "boot-p16-isolation";
    private static final String TENANT = "p16-tenant";
    private static final List<String> PARENTS = List.of("chunks", "taxonomy_centroids");

    PostgreSQLContainer<?> pg;
    HikariDataSource svc;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            // The service_tokens trigger creates each tenant's leaves under every model partition of both parents.
            PgContainerHelper.seedServiceToken(ctx, BOOT, TenantConstants.DEFAULT_TENANT, "boot");
            PgContainerHelper.seedServiceToken(ctx, "tok-" + TENANT, TENANT, "p16");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(2);
        cfg.setAutoCommit(true);
        svc = new HikariDataSource(cfg);
    }

    @AfterAll
    void stopAll() {
        if (svc != null) svc.close();
        if (pg != null) pg.stop();
    }

    // ── what the fixture holds ───────────────────────────────────────────────────────────────────────────────

    /** parent -> its model partitions -> their leaves, read straight from the catalog. */
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

    /** Names of the direct partitions of nexus.{@code parentRel}, by name. */
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

    private int expectedRelations() throws SQLException {
        int n = 0;
        for (String p : PARENTS) {
            Tree t = tree(p);
            n += 1 + t.models().size() + t.leaves().size();
        }
        return n;
    }

    @Test
    void aMigratedSchemaHasNoGaps_andTheScanSawEveryRelationOfBothTrees() throws Exception {
        int expected = expectedRelations();
        // Non-vacuity: two tenants x four models x two parents of leaves, four model partitions per parent, two parents.
        assertThat(expected).as("the fixture really has a tree to check").isGreaterThanOrEqualTo(2 + 8 + 16);
        try (Connection c = svc.getConnection()) {
            var report = ChunksIsolationCheck.structure(DSL.using(c, SQLDialect.POSTGRES));
            assertThat(report.gaps()).isEmpty();
            assertThat(report.relationsInspected())
                .as("the scan covered the parents, every model partition and every leaf, and no other relation")
                .isEqualTo(expected);
        }
        ChunksIsolationCheck.verifyAtStartup(svc);
    }

    // ── injected defects ─────────────────────────────────────────────────────────────────────────────────────

    enum Level { PARENT, MODEL_PARTITION, LEAF }

    /** DDL on one relation, run as the schema owner through the nexus_test.* wrappers (no SQL text in this tree). */
    @FunctionalInterface
    interface Ddl {
        void run(DSLContext ctx, String relation);
    }

    private static String q(String relation) {
        return "nexus." + relation;
    }

    private static final Ddl DROP_TENANT_POLICY = (c, r) -> Routines.dropPolicy(c.configuration(), q(r), "tenant_isolation");

    enum Defect {
        NO_FORCE((c, r) -> Routines.setForceRls(c.configuration(), q(r), false),
            (c, r) -> Routines.setForceRls(c.configuration(), q(r), true), "FORCE"),
        NO_RLS((c, r) -> Routines.setRowSecurity(c.configuration(), q(r), false),
            (c, r) -> Routines.setRowSecurity(c.configuration(), q(r), true), "not enabled"),
        POLICY_DROPPED(DROP_TENANT_POLICY, null, "tenant_isolation"),
        POLICY_WIDENED((c, r) -> Routines.widenPolicy(c.configuration(), q(r), "tenant_isolation"), null, "tenant_isolation"),
        POLICY_ADDED((c, r) -> Routines.addPermissivePolicy(c.configuration(), q(r), "rogue_p16"),
            (c, r) -> Routines.dropPolicy(c.configuration(), q(r), "rogue_p16"), "rogue_p16");

        final Ddl inject;
        final Ddl repair;          // null: repaired by the engine's own re-mirror, partition_sync_access
        final String mentions;

        Defect(Ddl inject, Ddl repair, String mentions) {
            this.inject = inject;
            this.repair = repair;
            this.mentions = mentions;
        }
    }

    static Stream<Object[]> defects() {
        List<Object[]> out = new ArrayList<>();
        for (String parent : PARENTS) {
            for (Level level : Level.values()) {
                for (Defect d : Defect.values()) {
                    out.add(new Object[] {parent, level, d});
                }
            }
        }
        return out.stream();
    }

    private String relationAt(Tree t, Level level) {
        return switch (level) {
            case PARENT -> t.parent();
            case MODEL_PARTITION -> t.models().get(0);
            case LEAF -> t.leaves().get(t.leaves().size() - 1);
        };
    }

    private void ddl(Ddl ddl, String relation) throws SQLException {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            ddl.run(DSL.using(su, SQLDialect.POSTGRES), relation);
        }
    }

    private void setForce(String relation, boolean force) throws SQLException {
        ddl((c, r) -> Routines.setForceRls(c.configuration(), q(r), force), relation);
    }

    private void repair(String parent, Defect d, String relation) throws SQLException {
        if (d.repair != null) {
            ddl(d.repair, relation);
            return;
        }
        // The remedy the refusal names: re-mirror the parent onto the whole tree. For a defect on the parent
        // itself the parent is put right first (the function copies FROM the parent).
        if (relation.equals(parent)) {
            ddl((c, r) -> Routines.resetTenantPolicy(c.configuration(), q(r)), relation);
        }
        ddl((c, r) -> dev.nexus.service.jooq.nexus.Routines.partitionSyncAccess(c.configuration(), q(parent), true),
            relation);
    }

    @ParameterizedTest(name = "{0} {1} {2}")
    @MethodSource("defects")
    void aDefectOnAnyRelationOfEitherTree_isRefusedAtStartup_andPassesOnceRepaired(String parent, Level level, Defect d)
            throws Exception {
        String relation = relationAt(tree(parent), level);
        // Control: the fixture is clean right before the injection.
        ChunksIsolationCheck.verifyAtStartup(svc);
        ddl(d.inject, relation);
        try {
            assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svc))
                .as("%s on %s must refuse the boot", d, relation)
                .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
                .hasMessageContaining(d.mentions)
                // A defect on a parent shows against every relation that mirrors it (policy defects) or against the
                // parent itself (flag defects): either way the parent's name is in the message.
                .hasMessageContaining("nexus." + (level == Level.PARENT && isPolicyDefect(d) ? parent : relation));
        } finally {
            repair(parent, d, relation);
        }
        ChunksIsolationCheck.verifyAtStartup(svc);
        try (Connection c = svc.getConnection()) {
            assertThat(ChunksIsolationCheck.structure(DSL.using(c, SQLDialect.POSTGRES)).gaps())
                .as("repaired").isEmpty();
        }
    }

    private static boolean isPolicyDefect(Defect d) {
        return d == Defect.POLICY_DROPPED || d == Defect.POLICY_WIDENED || d == Defect.POLICY_ADDED;
    }

    @Test
    void theRefusalNamesTheRemedy_andCountsTheRest() throws Exception {
        Tree t = tree("chunks");
        List<String> hit = t.leaves().subList(0, ChunksIsolationCheck.REFUSAL_GAPS_SHOWN + 1);
        for (String leaf : hit) {
            setForce(leaf, false);
        }
        try {
            assertThatThrownBy(() -> ChunksIsolationCheck.verifyAtStartup(svc))
                .isInstanceOf(ChunksIsolationCheck.IsolationException.class)
                .hasMessageContaining("partition_sync_access")
                .hasMessageContaining("1 more gap(s) follow");
        } finally {
            for (String leaf : hit) {
                setForce(leaf, true);
            }
        }
        ChunksIsolationCheck.verifyAtStartup(svc);
    }

    // ── GET /v1/status ───────────────────────────────────────────────────────────────────────────────────────

    private static final class MutableClock extends Clock {
        Instant now = Instant.parse("2026-10-07T12:00:00Z");
        @Override public ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(ZoneId zone) { return this; }
        @Override public Instant instant() { return now; }
    }

    @Test
    void theStatusFieldGoesFalseWhenOneLeafLosesForce_andTrueAgainWhenRepaired() throws Exception {
        var clock = new MutableClock();
        var queued = new ArrayDeque<Runnable>();
        var status = ChunksIsolationCheck.statusSupplier(svc, clock, queued::add);
        while (!queued.isEmpty()) queued.poll().run();
        assertThat(status.get()).as("clean").isTrue();

        String leaf = tree("taxonomy_centroids").leaves().get(0);
        setForce(leaf, false);
        try {
            clock.now = clock.now.plusMillis(ChunksIsolationCheck.STATUS_TTL_MILLIS + 1);
            status.get();
            while (!queued.isEmpty()) queued.poll().run();
            assertThat(status.get()).as("one leaf of taxonomy_centroids lost FORCE").isFalse();
        } finally {
            setForce(leaf, true);
        }
        clock.now = clock.now.plusMillis(ChunksIsolationCheck.STATUS_TTL_MILLIS + 1);
        status.get();
        while (!queued.isEmpty()) queued.poll().run();
        assertThat(status.get()).as("repaired").isTrue();
    }
}
