// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.jooq.DSLContext;
import org.jooq.Name;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

/**
 * Pins the pristine-clone skip of {@link PgContainerHelper#applyProductSchema} (nexus-0ty8z, fix round).
 *
 * <p>The first {@code applyProductSchema} on a clone made by {@code PgContainerHelper.start()} does nothing,
 * on the premise that the clone already is what the changelog would leave: the template was migrated to HEAD,
 * every runAlways grant applied, the {@code nexus_test} objects installed. That premise used to be prose.
 * These tests make it a measured fact and pin the edges of the skip:
 * <ul>
 *   <li><b>(a)</b> a pristine clone's catalog equals the same clone after a second, REAL
 *       {@code applyProductSchema}: nothing the changelog would have changed was missing. The snapshot covers
 *       relations (owner, ACL, reloptions, RLS flags), column ACLs, functions (owner, ACL, config, body hash),
 *       schema and default ACLs, policies, constraints, indexes, triggers, event triggers, extensions, sequences,
 *       views, the database ACL and its settings, roles and memberships, and the changelog journal minus
 *       the columns a rerun legitimately rewrites ({@code EXECTYPE}, {@code DATEEXECUTED}, {@code ORDEREXECUTED}).
 *       It carries a size floor, and a control: a clone with a REVOKE applied differs.</li>
 *   <li><b>(b)</b> a grant revoked on a clone is healed by the second call.</li>
 *   <li><b>(c)</b> an empty database is never skipped.</li>
 *   <li><b>(d)</b> only the first call on a clone is skipped, and a revoke made before it survives it.</li>
 * </ul>
 * Falsified by making {@code SharedCluster.takePristine} always true (a, b, c, d go red: a later call
 * or an empty database is skipped) and never true (a, d, e go red: the first call replays); measured
 * 2026-10-19, recorded in T2 {@code nexus/dev-nexus-0ty8z-fix-round}.
 */
class PristineCloneSkipTest {

    /**
     * One entry per catalog row, prefixed with its section, sorted. The nineteen catalog reads (relations, column
     * ACLs, functions, schemas, default ACLs, policies, constraints, indexes, triggers, event triggers, extensions,
     * sequences, views, materialized views, the database, its settings, roles, memberships and the journal) have no
     * typed jOOQ form (pg_get_indexdef, pg_get_constraintdef, format() over several catalogs), so they live in the
     * test function {@code nexus_test.catalog_snapshot()} (db.changelog-test-objects.xml, nexus-cbo4a batch 8), which
     * every clone carries; this is one typed call of it. The function lists itself in the function section like any
     * other nexus_test function, so a before and after snapshot still compare equal.
     */
    static List<String> snapshot(Connection c) throws SQLException {
        return new ArrayList<>(new TreeSet<>(jooq(c)
            .select(DSL.function(DSL.name("nexus_test", "catalog_snapshot"), String.class))
            .fetch(0, String.class)));
    }

    /** Rows in either list but not both, marked {@code -} (only in a) or {@code +} (only in b). */
    static List<String> difference(List<String> a, List<String> b) {
        List<String> out = new ArrayList<>();
        a.stream().filter(r -> !b.contains(r)).forEach(r -> out.add("- " + r));
        b.stream().filter(r -> !a.contains(r)).forEach(r -> out.add("+ " + r));
        return out;
    }

    private static long count(List<String> snapshot, String section) {
        return snapshot.stream().filter(r -> r.startsWith(section + ": ")).count();
    }

    private static void requireShared() {
        assumeTrue(!"false".equals(System.getProperty("nexus.test.pg.shared")),
            "the pristine skip exists only on a shared-cluster clone; -Dnexus.test.pg.shared=false boots a container");
    }

    private static final Name PG_CLASS = DSL.name("pg_catalog", "pg_class");
    private static final Name PG_NAMESPACE = DSL.name("pg_catalog", "pg_namespace");

    private static DSLContext jooq(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    /** A table in schema {@code nexus} that nexus_svc holds a grant on, chosen the same way every run. */
    private static String grantedTable(Connection su) {
        String table = jooq(su).select(DSL.field(DSL.name("c", "relname"), String.class))
            .from(DSL.table(PG_CLASS).as("c"))
            .join(DSL.table(PG_NAMESPACE).as("n")).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
            .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
            .and(DSL.field(DSL.name("c", "relkind"), String.class).in("r", "p"))
            .and(DSL.field(DSL.name("c", "relacl")).cast(String.class).like("%nexus_svc=%"))
            .orderBy(DSL.field(DSL.name("c", "relname")))
            .limit(1)
            .fetchOne(0, String.class);
        assertThat(table).as("non-vacuity: the template carries a table nexus_svc holds a grant on").isNotNull();
        return table;
    }

    /** The table's ACL entries, whole, so a revoke and a re-grant compare equal only when every entry matches. */
    private static TreeSet<String> acl(Connection su, String table) {
        return PartitionScratch.acl(jooq(su), table);
    }

    private static void revokeSvc(Connection su, String table) {
        jooq(su).revoke(DSL.privilege("ALL")).on(DSL.table(DSL.name("nexus", table))).from(DSL.role("nexus_svc")).execute();
    }

    private static boolean svcHolds(TreeSet<String> acl) {
        return acl.stream().anyMatch(e -> e.startsWith("nexus_svc="));
    }

    // ── (a) the premise: a pristine clone is what a real replay would leave ─────────────────────────────────

    @Test
    void aPristineClonesCatalogEqualsTheSameCloneAfterARealSecondApply() throws Exception {
        requireShared();
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            List<String> pristine = snapshot(su);

            // Size floor: a snapshot of nothing equals a snapshot of nothing. Measured 2026-10-19 on the
            // template clone; the floors sit a few percent under the counts.
            assertThat(pristine.size()).as("rows in the snapshot").isGreaterThanOrEqualTo(FLOOR_TOTAL);
            FLOOR_BY_SECTION.forEach((section, floor) ->
                assertThat(count(pristine, section)).as("%s rows in the snapshot", section).isGreaterThanOrEqualTo(floor));

            PgContainerHelper.applyProductSchema(su); // call 1: the skip
            List<String> afterSkip = snapshot(su);
            assertThat(difference(pristine, afterSkip)).as("the skipped call changes nothing").isEmpty();

            assertThat(rerunRows(su)).as("non-vacuity: nothing has been replayed yet").isZero();
            PgContainerHelper.applyProductSchema(su); // call 2: a real changelog run
            assertThat(rerunRows(su)).as("non-vacuity: the second call really replayed the runAlways changesets")
                .isPositive();

            List<String> afterReal = snapshot(su);
            assertThat(difference(pristine, afterReal))
                .as("a real applyProductSchema over a pristine clone changes nothing the snapshot sees; "
                    + "if this fails the skip is no longer sound")
                .isEmpty();
        } finally {
            pg.stop();
        }
    }

    @Test
    void theSnapshotSeesARevoke() throws Exception {
        requireShared();
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            List<String> before = snapshot(su);
            revokeSvc(su, grantedTable(su));
            assertThat(difference(before, snapshot(su)))
                .as("control: the snapshot differs once a grant is revoked").isNotEmpty();
        } finally {
            pg.stop();
        }
    }

    private static long rerunRows(Connection su) {
        return jooq(su).fetchCount(DSL.table(DSL.name("public", "databasechangelog")),
            DSL.field(DSL.name("exectype"), String.class).eq("RERAN"));
    }

    // ── (b) a revoked grant is healed by the second call ────────────────────────────────────────────────────

    @Test
    void aGrantRevokedAfterTheFirstCallIsHealedByTheSecond() throws Exception {
        requireShared();
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            String table = grantedTable(su);
            TreeSet<String> granted = acl(su, table);
            PgContainerHelper.applyProductSchema(su); // call 1: the skip
            revokeSvc(su, table);
            assertThat(svcHolds(acl(su, table))).as("non-vacuity: the revoke took").isFalse();
            PgContainerHelper.applyProductSchema(su); // call 2: real
            assertThat(acl(su, table)).as("the second call re-ran the grants").isEqualTo(granted);
        } finally {
            pg.stop();
        }
    }

    // ── (c) an empty database is never skipped ──────────────────────────────────────────────────────────────

    @Test
    void anEmptyDatabaseIsNeverSkipped() throws Exception {
        requireShared();
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            assertThat(PgCatalogProbes.tablesInSchema(jooq(su), "nexus"))
                .as("non-vacuity: the database starts without the product schema").isEmpty();
            PgContainerHelper.applyProductSchema(su); // the FIRST call on this database
            assertThat(PgCatalogProbes.tableExists(jooq(su), "nexus", "chunks"))
                .as("the first call on an empty database walked the changelog").isTrue();
        } finally {
            pg.stop();
        }
    }

    // ── (d) only the first call on a clone is skipped ───────────────────────────────────────────────────────

    @Test
    void onlyTheFirstCallOnACloneIsSkipped() throws Exception {
        requireShared();
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            String table = grantedTable(su);
            TreeSet<String> granted = acl(su, table);
            revokeSvc(su, table);
            TreeSet<String> revoked = acl(su, table);
            assertThat(svcHolds(revoked)).as("non-vacuity: the revoke took").isFalse();

            PgContainerHelper.applyProductSchema(su); // call 1: skipped, so the revoke survives it
            assertThat(acl(su, table)).as("the first call on a clone did nothing").isEqualTo(revoked);

            PgContainerHelper.applyProductSchema(su); // call 2: real
            assertThat(acl(su, table)).as("the second call ran the changelog").isEqualTo(granted);

            revokeSvc(su, table);
            PgContainerHelper.applyProductSchema(su); // call 3: real again, the skip is spent
            assertThat(acl(su, table)).as("a third call also ran the changelog").isEqualTo(granted);
        } finally {
            pg.stop();
        }
    }

    @Test
    void eachCloneHasItsOwnFirstCall() throws Exception {
        requireShared();
        PostgreSQLContainer<?> first = PgContainerHelper.start();
        PostgreSQLContainer<?> second = PgContainerHelper.start();
        try (Connection a = first.createConnection(""); Connection b = second.createConnection("")) {
            PgContainerHelper.applyProductSchema(a); // spends a's skip only
            String table = grantedTable(b);
            revokeSvc(b, table);
            TreeSet<String> revoked = acl(b, table);
            PgContainerHelper.applyProductSchema(b);
            assertThat(acl(b, table)).as("b's first call is still skipped after a's").isEqualTo(revoked);
        } finally {
            second.stop();
            first.stop();
        }
    }

    @Test
    void stopForgetsAClonesUnspentFirstCall() throws Exception {
        requireShared();
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        String name = pg.getDatabaseName();
        assertThat(SharedCluster.isPristine(name)).as("non-vacuity: a fresh clone holds its first call").isTrue();
        pg.stop();
        assertThat(SharedCluster.isPristine(name)).as("stop() drops the entry of a clone that never applied").isFalse();
    }

    /**
     * Floors, a few percent under the counts measured on a template clone (2026-10-19, nexus-0ty8z fix round):
     * total 1,847; relation 389, function 257, schema 7, default ACL 4, policy 72, constraint 227, index 291,
     * trigger 10, event trigger 1, extension 3, sequence 20, view 9, database 1, role 17, membership 4,
     * journal 535. The template has no column ACLs, materialized views or database-level settings, so those
     * sections are queried and carry no floor. The schema only grows, so a floor does not need a bump.
     */
    private static final int FLOOR_TOTAL = 1750;
    private static final Map<String, Long> FLOOR_BY_SECTION = Map.ofEntries(
        Map.entry("relation", 370L), Map.entry("function", 240L), Map.entry("schema", 6L),
        Map.entry("defaultacl", 3L), Map.entry("policy", 65L), Map.entry("constraint", 215L),
        Map.entry("index", 275L), Map.entry("trigger", 9L), Map.entry("eventtrigger", 1L),
        Map.entry("extension", 3L), Map.entry("sequence", 18L), Map.entry("view", 8L),
        Map.entry("database", 1L), Map.entry("role", 15L), Map.entry("membership", 3L),
        Map.entry("journal", 500L));
}
