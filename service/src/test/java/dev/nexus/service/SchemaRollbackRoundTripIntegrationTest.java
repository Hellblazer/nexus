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
import org.junit.jupiter.api.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * nexus-a0m60 — the missing twin of {@link SchemaUpgradeRehearsalIntegrationTest}.
 * That test covers the UPGRADE axis; this one covers the ROLLBACK axis, which
 * nothing in the build or CI had ever executed.
 *
 * <p><strong>The asymmetry this closes.</strong> {@code SchemaMigrator} calls
 * {@code liquibase.update(...)} and nothing else — the engine has no Liquibase
 * rollback path at all (its {@code conn.rollback()} is a plain JDBC transaction
 * rollback inside the FORCE-RLS handling). So:
 *
 * <pre>
 *   forward SQL  — runs on every deploy, every CI run, every dev box
 *   rollback SQL — ran approximately NEVER, until an incident
 * </pre>
 *
 * That is how {@code staging-4-svc-grants} shipped a {@code <rollback>} that
 * could not execute at all (raw text with a {@code DO $$} body, split on the
 * {@code ;} inside its {@code DECLARE} → "Unterminated dollar quote"), taking
 * out the ENTIRE rollback chain while every green signal stayed green. It was
 * found by hand, not by a test.
 *
 * <p><strong>Why {@code tests/test_changelog_rollback_lint.py} is not a
 * substitute.</strong> The lint checks SHAPE — a {@code $$} body must sit inside
 * {@code <sql splitStatements="false">}. A rollback can carry that attribute,
 * parse cleanly, and still restore the WRONG THING: drop an index and forget to
 * recreate it, or revert a column to a different definition. Only execution
 * proves otherwise.
 *
 * <p><strong>WHY THIS TEST BOOTS TWICE BEFORE ROLLING BACK — order fidelity.</strong>
 * Liquibase rolls back in DATABASECHANGELOG <em>execution</em> order
 * ({@code ORDEREXECUTED}), NOT in master-file order. The tree has exactly six
 * {@code runAlways} changesets (nexus-0ys55 added {@code
 * grants-003-purge-vacuum-maintain}), and they re-execute on every boot, so on
 * any cluster that has booted more than once they float to the tail of
 * execution order (master positions as of this commit, which the floor
 * changeset shifted by one):
 *
 * <pre>
 *   master pos 194  staging-4-svc-grants
 *   master pos 205  grants-nexus-svc-1
 *   master pos 206  grants-002-changelog-read
 *   master pos 207  grants-003-purge-vacuum-maintain
 *   master pos 208  grants-nexus-diag-1
 *   master pos 209  grants-nexus-diag-2
 * </pre>
 *
 * That is exactly, and in order, the five changesets the manual {@code
 * rollbackCount(10)} repro rolled back before dying on staging-4 — it was run
 * against a re-booted cluster, where staging-4 had floated from master position
 * 194 to execution depth 5 (now depth 6, after nexus-0ys55's addition).
 *
 * <p>Rolling back to a TAG reaches every changeset above the floor regardless of
 * execution order, so the second boot is NOT what makes staging-4 reachable —
 * an earlier draft of this test used a bounded {@code rollbackCount} where it
 * would have been, and that rationale outlived the design. What the second boot
 * buys is ORDER FIDELITY: it reproduces the execution order a real cluster
 * actually has, which is where order-dependent rollback failures live (a
 * rollback whose target was already removed by a changeset that, on a fresh
 * single-pass apply, would have been reverted first).
 * {@link #runAlwaysChangesetsFloatToTheExecutionTail} pins the mechanism
 * explicitly rather than leaving it as an assumption.
 *
 * <p><strong>All the way down, by count.</strong> The rollback goes to ZERO —
 * every DATABASECHANGELOG row. That is the guarantee this test exists to give:
 * if the chain can walk all the way back, it can certainly walk back to any
 * intermediate point, which is what "a future release can get back to here"
 * means in practice.
 *
 * <p>Full depth is reachable at all only because the three changesets with no
 * executable inverse declare themselves irreversible: {@code role-001-1} (roles
 * are cluster-level), {@code vectors-001-1} (untrusted extensions a NOSUPERUSER
 * role can neither create nor drop) and {@code catalog-016-0} (dedup tombstones,
 * with no honest inverse to write).
 *
 * <p><strong>There is deliberately no floor TAG.</strong> One existed for a few
 * hours on 2026-07-27 and was removed — {@code tagDatabase} tags the most
 * recently EXECUTED row rather than its own, so a floor retrofitted into an
 * already-migrated cluster lands at the tail and a rollback to it reverts almost
 * nothing while exiting 0. It behaved as documented only on a freshly
 * initialised database, which is the one case that does not matter. Schema
 * rollback is not an operational path here in any event — the engine never calls
 * it — so a named operator target bought nothing and promised something false.
 * See nexus-9vg5g before re-introducing one. Counting rows is honest by
 * comparison: the count is read from the database the test just built, so it
 * neither rots as changesets land nor cares about nexus-ixsxa's duplicate rows.
 *
 * <p><strong>What this found on its first runs.</strong> Every defect below
 * passed the shape lint, the full Java suite, and every deploy:
 * {@code catalog-016-0} had no inverse at all and aborted the chain; four
 * rollbacks ({@code chash-001-2}, {@code fk-002-4}, {@code catalog-013-1},
 * {@code rdr180-11}) referenced {@code nexus.chash_index} after
 * {@code rdr187-2} retired it, where {@code DROP CONSTRAINT IF EXISTS} guards
 * the constraint but not the table; and {@code vectors-001-1} tried to drop
 * extensions it does not own. That is the argument for execution over shape
 * checking, as evidence rather than as reasoning.
 *
 * <p><strong>Assertions are on SCHEMA SHAPE, never on DATABASECHANGELOG row
 * equality.</strong> {@code runAlways} makes the round trip a non-identity at
 * the bookkeeping level by construction — those five rows legitimately carry new
 * {@code DATEEXECUTED}/{@code ORDEREXECUTED} values after the re-apply. What must
 * be identical is the database the schema describes: tables, index definitions,
 * generated-column expressions and table grants.
 *
 * <p><strong>Checksum safety.</strong> Liquibase excludes {@code <rollback>} from
 * the changeset md5sum (measured: staging-4 kept
 * {@code 9:84da10127f33beb3b1602f9cb0b30163} across its fix), so exercising and
 * editing rollbacks needs no {@code validCheckSum} ceremony and does not disturb
 * any deployed cluster.
 *
 * <p><strong>CI placement, stated at its real cost.</strong> {@code service-ci.yml}
 * is path-gated on {@code service/**} — NOT on the changelog subtree — so this
 * runs on every service PR, not only on changelog changes. Two testcontainers
 * (one per {@code @Test}), five Liquibase {@code update} passes and a
 * 208-statement rollback, ~13s locally. Accepted deliberately rather than
 * defaulted: it needs no new workflow, and a changelog-only filter would miss
 * the case where Java code and a changeset land together. If that cost stops
 * being worth it, the lever is a job-level {@code dorny/paths-filter} on
 * {@code service/src/main/resources/db/changelog/**} — not deleting the test.
 */
class SchemaRollbackRoundTripIntegrationTest {

    private static final Logger log =
        LoggerFactory.getLogger(SchemaRollbackRoundTripIntegrationTest.class);

    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";

    private static final String ADMIN_ROLE = "nexus_admin_rollback";
    private static final String ADMIN_PASS = "nexus_admin_rollback_pass";


    /**
     * The six {@code runAlways} changesets, in master order (nexus-0ys55 added
     * {@code grants-003-purge-vacuum-maintain}, formerly five). Their identity is
     * asserted (not merely their count) so that adding or removing a
     * {@code runAlways} changeset forces a deliberate look at this test rather
     * than silently changing which changesets the rollback leg reaches first.
     */
    private static final List<String> RUN_ALWAYS_IDS = List.of(
        "staging-4-svc-grants",
        "grants-nexus-svc-1",
        "grants-002-changelog-read",
        "grants-003-purge-vacuum-maintain",
        "grants-nexus-diag-1",
        "grants-nexus-diag-2");

    /**
     * Pins the mechanism the rollback leg depends on: a second {@code migrate}
     * re-executes the {@code runAlways} changesets and re-stamps their
     * {@code ORDEREXECUTED}, floating them to the tail of execution order.
     *
     * <p>Separate from the round trip on purpose. If Liquibase's re-run
     * bookkeeping ever changes, this fails with a precise message about
     * execution order instead of the round trip failing somewhere deep in a
     * rollback with a misleading cause.
     *
     * <p><strong>What this test found on its first run, and now guards
     * (nexus-ixsxa).</strong> A {@code runAlways} changeset whose
     * {@code <preConditions onFail="MARK_RAN">} is unmet grows the changelog by
     * one row per boot, without bound: {@code ExecType.MARK_RAN} carries
     * {@code ranBefore=false} and {@code MarkChangeSetRanGenerator} branches on
     * that flag to {@code InsertStatement}, where {@code RERAN}
     * ({@code ranBefore=true}) updates in place. Both {@code grants-nexus-diag}
     * changesets were shaped that way, era-exclusive on the same probe, so
     * exactly one of them accumulated on every cluster — {@code -2} in the
     * legacy era, {@code -1} in the view era. Fixed by moving both era tests
     * into the {@code DO $$} bodies; the row-count invariant is asserted here
     * (legacy era) and in {@link #eraTransitionRevokesTableSelectWithoutGrowingTheChangelog}
     * (view era, and the transition between them). It is a hard assertion
     * rather than a log line because it also skews any rollback-depth
     * arithmetic computed from row counts.
     */
    @Test
    void runAlwaysChangesetsFloatToTheExecutionTail() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                dbaBootstrap(su);
            }
            try (HikariDataSource ds = newAdminPool(pg, "nexus-admin-rollback-order")) {
                SchemaMigrator.migrate(ds);
                int afterFirst;
                List<String> tailAfterFirst;
                try (Connection c = ds.getConnection()) {
                    afterFirst = changelogRowCount(c);
                    tailAfterFirst = executionTail(c, RUN_ALWAYS_IDS.size());
                }
                assertThat(afterFirst)
                    .as("a fresh apply must record a nonzero changeset count — an empty "
                        + "changelog would make every assertion below vacuous")
                    .isGreaterThan(0);
                assertThat(tailAfterFirst)
                    .as("on a FRESH single-pass apply, execution order == master order, so the "
                        + "execution tail is the master tail — staging-4 (master pos 194) is NOT "
                        + "here yet, which is exactly why one boot is not enough")
                    .doesNotContain("staging-4-svc-grants");

                // The load-bearing second boot.
                SchemaMigrator.migrate(ds);

                int afterSecond;
                List<String> tailAfterSecond;
                try (Connection c = ds.getConnection()) {
                    afterSecond = changelogRowCount(c);
                    tailAfterSecond = executionTail(c, RUN_ALWAYS_IDS.size());
                }
                // nexus-ixsxa: re-booting must RE-STAMP the runAlways rows, not
                // append new ones. This container has no
                // nexus.diag_chash_conformance (it comes from the superuser
                // provisioning path, never from Liquibase), so it is the LEGACY
                // era — the arm where grants-nexus-diag-2 used to accumulate.
                List<String> duplicated;
                try (Connection c = ds.getConnection()) {
                    duplicated = duplicateChangelogRows(c);
                }
                assertThat(duplicated)
                    .as("no changeset may occupy more than one DATABASECHANGELOG row: a "
                        + "runAlways changeset with an unmet <preConditions onFail=\"MARK_RAN\"> "
                        + "INSERTS per boot instead of updating in place (nexus-ixsxa)")
                    .isEmpty();
                assertThat(afterSecond)
                    .as("a second boot must re-stamp the runAlways rows in place, leaving the "
                        + "row count unchanged — unbounded growth also skews every rollback "
                        + "depth computed by row count (nexus-ixsxa)")
                    .isEqualTo(afterFirst);
                assertThat(tailAfterSecond)
                    .as("after a second boot the %d runAlways changesets must occupy the LAST %d "
                        + "execution slots — this is what puts staging-4-svc-grants (master pos "
                        + "193) within reach of a rollback, and the whole rollback leg depends "
                        + "on it", RUN_ALWAYS_IDS.size(), RUN_ALWAYS_IDS.size())
                    .containsExactlyInAnyOrderElementsOf(RUN_ALWAYS_IDS);
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The nexus_diag ERA TRANSITION, executed for the first time: legacy grants
     * → the superuser provisioning path creates
     * {@code nexus.diag_chash_conformance} → the view-era REVOKE fires. Two
     * properties are asserted together because they share one cause.
     *
     * <p><strong>The boundary (RDR-182 s5).</strong> {@code grants-nexus-diag-2}
     * is what turns the diagnostic role's content boundary from a product-level
     * lint into a DB-enforced one, by revoking the direct table SELECT that
     * {@code grants-nexus-diag-1} granted in the legacy era. Nothing had ever
     * executed that transition. It is also precisely what a {@code runOnChange}
     * "fix" for nexus-ixsxa would have silently forfeited:
     * {@code ShouldRunChangeSetFilter} rejects an already-ran
     * {@code runOnChange} changeset outright, so the era would never be
     * re-evaluated and the revoke would never fire.
     *
     * <p><strong>The row invariant (nexus-ixsxa).</strong> The view era is the
     * arm nobody had measured — the bead recorded it as safe because
     * {@code grants-nexus-diag-2}'s precondition passes there. It does, and
     * {@code grants-nexus-diag-1}'s, being the exclusive complement on the same
     * probe, then fails: that era accumulated too, just under a different id.
     */
    @Test
    void eraTransitionRevokesTableSelectWithoutGrowingTheChangelog() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                dbaBootstrap(su);
                // BYPASSRLS is why this role is superuser-created and never
                // created by the changelog (nexus-vounk): a policy-subject
                // session with no nexus.tenant GUC counts ZERO rows.
                su.createStatement().execute(
                    "CREATE ROLE nexus_diag LOGIN PASSWORD 'nexus_diag_pass' "
                        + "NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS");
            }
            try (HikariDataSource ds = newAdminPool(pg, "nexus-admin-era-transition")) {
                // ── LEGACY era: no counts view, so diag-1 grants table SELECT.
                SchemaMigrator.migrate(ds);
                try (Connection c = ds.getConnection()) {
                    assertThat(diagBaseTableGrants(c))
                        .as("legacy era: grants-nexus-diag-1 must grant nexus_diag direct table "
                            + "SELECT — without this the revoke asserted below would pass "
                            + "vacuously against a role that never had the grant")
                        .isNotEmpty();
                }

                // ── The provisioning path creates the view. SUPERUSER-owned on
                // purpose: under FORCE RLS a counts view sees every tenant's
                // rows only when its owner is RLS-exempt, and diag-2's revoke
                // loop is owner-restricted precisely so it never touches this
                // foreign-owned relation (nexus-46yy3, a live-reproduced P0).
                try (Connection su = pg.createConnection("")) {
                    su.createStatement().execute(
                        "CREATE VIEW nexus.diag_chash_conformance AS "
                            + "SELECT 'stub'::text AS table_name, 0::bigint AS non_conformant");
                    su.createStatement().execute(
                        "GRANT SELECT ON nexus.diag_chash_conformance TO nexus_diag");
                }

                // ── VIEW era: diag-2 fires, diag-1 stands down.
                int viewEraRows;
                SchemaMigrator.migrate(ds);
                try (Connection c = ds.getConnection()) {
                    assertThat(diagBaseTableGrants(c))
                        .as("view era: grants-nexus-diag-2 must revoke every direct BASE TABLE "
                            + "SELECT, leaving the counts view as nexus_diag's only content "
                            + "path — the RDR-182 s5 boundary becoming structural")
                        .isEmpty();
                    viewEraRows = changelogRowCount(c);
                }

                SchemaMigrator.migrate(ds);
                try (Connection c = ds.getConnection()) {
                    assertThat(duplicateChangelogRows(c))
                        .as("view era: grants-nexus-diag-1 is the exclusive complement of "
                            + "grants-nexus-diag-2 on the same probe, so as a MARK_RAN "
                            + "precondition it appended a row here on every boot (nexus-ixsxa)")
                        .isEmpty();
                    assertThat(changelogRowCount(c))
                        .as("view era: a reboot must re-stamp the runAlways rows in place")
                        .isEqualTo(viewEraRows);
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The round trip: update → update → roll back to the floor → update,
     * asserting the schema the database ends with is the schema it started with.
     *
     * <p>NOT "everything", and the distinction matters: 49 of the 208 changesets
     * carry an empty {@code <rollback/>} and are by construction not reverted.
     * What is asserted is that the full TABLE set goes and comes back
     * identically, minus that declared-irreversible cohort. Nothing currently
     * bounds that cohort, and this test structurally REWARDS growing it — a new
     * changeset creating an index, view or function with an empty
     * {@code <rollback/>} passes every assertion here. Tracked separately.
     */
    @Test
    void fullChangelog_rollsBackCompletely_andReappliesToTheSameSchema() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                dbaBootstrap(su);
            }
            try (HikariDataSource ds = newAdminPool(pg, "nexus-admin-rollback-trip")) {

                // ── FORWARD, twice: the second boot floats runAlways to the
                //    execution tail, reproducing a real cluster (see javadoc). ──
                SchemaMigrator.migrate(ds);
                SchemaMigrator.migrate(ds);

                int applied;
                Map<String, List<String>> before;
                try (Connection c = ds.getConnection()) {
                    applied = changelogRowCount(c);
                    before = schemaShape(c);
                }
                assertThat(applied).as("nonzero changesets must be applied before rolling back")
                    .isGreaterThan(0);
                // EVERY category must be populated, not just the two that were
                // guarded originally. containsExactlyElementsOf() passes
                // trivially when both sides are empty, so a category whose query
                // silently returns nothing (a renamed catalog view, a typo)
                // would compare empty-to-empty and keep passing forever.
                for (String category : before.keySet()) {
                    assertThat(before.get(category))
                        .as("schemaShape category '%s' is EMPTY before the rollback — the round "
                            + "trip would then compare nothing to nothing and pass vacuously "
                            + "forever. Either the query is broken or the objects it covers are "
                            + "gone; both are findings", category)
                        .isNotEmpty();
                }
                log.info("event=rollback_roundtrip_forward_done applied={} tables={} indexes={}",
                    applied, before.get("tables").size(), before.get("indexes").size());

                // ── ROLLBACK: everything down to the floor tag. This is the
                //    leg nothing had ever executed. A changeset whose
                //    <rollback> cannot run fails HERE, naming itself. ───────
                assertThatCode(() -> rollbackEverything(ds, applied))
                    .as("the ENTIRE rollback chain must execute, all %d rows of it. A failure "
                        + "here names the first changeset whose <rollback> is broken, missing, or "
                        + "references an object a later changeset retired — the staging-4 class, "
                        + "invisible to every other signal we have", applied)
                    .doesNotThrowAnyException();

                try (Connection c = ds.getConnection()) {
                    // ZERO, not merely fewer. This is the whole guarantee: from
                    // any state the chain can walk all the way back, so it can
                    // certainly walk back to any intermediate point — which is
                    // what "we can get back to here" means for future releases.
                    // The earlier `isLessThan(applied)` was satisfied by rolling
                    // back a SINGLE changeset; it did not check depth at all.
                    assertThat(changelogRowCount(c))
                        .as("a full rollback must leave DATABASECHANGELOG EMPTY — a rollback that "
                            + "silently stops early and reports success is the failure mode this "
                            + "test exists to catch")
                        .isZero();
                    for (String schema : new String[] {"nexus", "staging"}) {
                        assertThat(tablesInSchema(c, schema))
                            .as("every migration-owned table in the %s schema must be gone after "
                                + "rolling back to the floor; survivors mean some rollback dropped "
                                + "its bookkeeping without dropping its object", schema)
                            .isEmpty();
                    }
                    assertThat(query(c, "SELECT extname FROM pg_extension ORDER BY 1"))
                        .as("the DBA-owned extensions must SURVIVE a rollback to the floor — that "
                            + "boundary is the whole reason the floor exists, and a rollback that "
                            + "reached past it would be uninstalling the DBA's provisioning")
                        .contains("vector", "pg_trgm");
                    // THE BEAD'S OWN ACCEPTANCE CRITERION, which the first cut
                    // of this test omitted: prove staging-4-svc-grants' DO block
                    // actually EXECUTED, not merely parsed. Its rollback REVOKEs
                    // nexus_svc's schema USAGE and table privileges; all five
                    // runAlways changesets are idempotent going forward, so a
                    // rollback that reverts NOTHING still round-trips to an
                    // identical schema. Without this, "does not throw" was the
                    // only thing proven for exactly the changeset that motivated
                    // the bead.
                    assertThat(query(c,
                            "SELECT grantee || ' ' || privilege_type "
                                + "FROM information_schema.role_table_grants "
                                + "WHERE grantee = 'nexus_svc' AND table_schema = 'staging'"))
                        .as("staging-4-svc-grants' rollback must have EXECUTED, not merely parsed "
                            + "— nexus_svc must hold zero privileges in the staging schema after "
                            + "the rollback. This is the assertion the manual repro used and the "
                            + "only thing that distinguishes a real revert from a silent no-op")
                        .isEmpty();
                }

                // ── FORWARD AGAIN: the schema must come back identical. ─────
                assertThatCode(() -> SchemaMigrator.migrate(ds))
                    .as("the changelog must re-apply cleanly onto the rolled-back database — a "
                        + "rollback that leaves residue only shows up on the way back up")
                    .doesNotThrowAnyException();

                Map<String, List<String>> after;
                try (Connection c = ds.getConnection()) {
                    after = schemaShape(c);
                }
                // Compared per-category so a failure names WHICH part of the
                // schema failed to come back, not just "the maps differ".
                for (String category : before.keySet()) {
                    assertThat(after.get(category))
                        .as("%s must be identical after update -> rollback -> update. A "
                            + "difference here is a rollback that reverted the WRONG THING — "
                            + "parseable, executable, and still wrong (the case the shape lint "
                            + "structurally cannot catch)", category)
                        .containsExactlyElementsOf(before.get(category));
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── Liquibase drive ──────────────────────────────────────────────────────

    /**
     * Roll the ENTIRE chain back — every row in DATABASECHANGELOG. There is
     * deliberately no production code path for this: the engine only ever calls
     * {@code update}, so the test drives {@code liquibase.Liquibase} itself.
     *
     * <p>By COUNT, and to zero. An earlier revision rolled back to a
     * {@code tagDatabase} floor; that tag was removed (nexus-9vg5g) because
     * {@code tagDatabase} tags the most recently EXECUTED row rather than its
     * own, so a retrofitted floor is positionally meaningless on exactly the
     * clusters that already exist. Counting rows is honest here because the test
     * reads the count from the database it just built rather than hard-coding a
     * depth — so it neither rots as changesets land nor cares about the
     * nexus-ixsxa duplicate rows, which are simply more rows to revert.
     *
     * <p>Full depth is reachable at all only because the two changesets that had
     * no executable inverse now declare themselves irreversible:
     * {@code catalog-016-0} (dedup tombstones, no honest inverse to write) and
     * {@code vectors-001-1} (untrusted extensions a NOSUPERUSER role can neither
     * create nor drop). {@code role-001-1} already declared the same for roles.
     */
    private static void rollbackEverything(HikariDataSource ds, int rows) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG_RELATIVE,
                    new ClassLoaderResourceAccessor(),
                    database)) {
                liquibase.rollback(rows, new Contexts(), new LabelExpression());
            }
        }
    }

    // ── Schema-shape capture (NOT DATABASECHANGELOG state) ───────────────────

    /**
     * The database's observable shape, as ordered string lists per category so a
     * mismatch names both the category and the exact differing entry.
     *
     * <p>Chosen to cover what the changesets under test actually manipulate:
     * generated columns and GIN indexes (memory-002 / catalog-017) and role
     * grants (staging-4-svc-grants / grants-*).
     */
    private static Map<String, List<String>> schemaShape(Connection c) throws Exception {
        Map<String, List<String>> shape = new LinkedHashMap<>();
        shape.put("tables", query(c,
            "SELECT schemaname || '.' || tablename FROM pg_tables "
                + "WHERE schemaname IN ('nexus','staging') ORDER BY 1"));
        shape.put("indexes", query(c,
            "SELECT schemaname || '.' || indexname || ' = ' || indexdef FROM pg_indexes "
                + "WHERE schemaname IN ('nexus','staging') ORDER BY 1"));
        // Generated-column expressions: the exact thing the FTS rollbacks revert.
        // pg_get_expr renders the stored expression, so a rollback that restores
        // a DIFFERENT expression is caught, not just a missing column.
        shape.put("generatedColumns", query(c,
            "SELECT n.nspname || '.' || cl.relname || '.' || a.attname || ' = ' "
                + "|| pg_get_expr(d.adbin, d.adrelid) "
                + "FROM pg_attrdef d "
                + "JOIN pg_class cl ON cl.oid = d.adrelid "
                + "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                + "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
                + "WHERE n.nspname IN ('nexus','staging') AND a.attgenerated <> '' "
                + "ORDER BY 1"));
        shape.put("constraints", query(c,
            "SELECT n.nspname || '.' || cl.relname || '.' || con.conname || ' = ' "
                + "|| pg_get_constraintdef(con.oid) "
                + "FROM pg_constraint con "
                + "JOIN pg_class cl ON cl.oid = con.conrelid "
                + "JOIN pg_namespace n ON n.oid = cl.relnamespace "
                + "WHERE n.nspname IN ('nexus','staging') ORDER BY 1"));
        shape.put("grants", query(c,
            "SELECT grantee || ' ' || privilege_type || ' ON ' "
                + "|| table_schema || '.' || table_name "
                + "FROM information_schema.role_table_grants "
                + "WHERE table_schema IN ('nexus','staging') "
                + "AND grantee NOT IN ('PUBLIC', current_user) ORDER BY 1"));
        // RLS is the highest-value category for THIS codebase and was missing
        // from the first cut. chash-001-2's rollback (rewritten in this commit)
        // does DROP POLICY / NO FORCE / DISABLE ROW LEVEL SECURITY, and
        // catalog-016-0 brackets its UPDATE with NO FORCE / FORCE. Without these
        // two categories a round trip that ends with FORCE RLS off on a tenant
        // table, or a policy whose USING expression drifted, passes green — and
        // "FORCE-RLS silently no-ops migration DML" is already a recorded
        // incident class here.
        shape.put("policies", query(c,
            "SELECT schemaname || '.' || tablename || '.' || policyname || ' = ' "
                + "|| coalesce(qual,'') || ' | ' || coalesce(with_check,'') "
                + "FROM pg_policies WHERE schemaname IN ('nexus','staging') ORDER BY 1"));
        shape.put("rlsFlags", query(c,
            "SELECT n.nspname || '.' || cl.relname || ' rls=' || cl.relrowsecurity "
                + "|| ' force=' || cl.relforcerowsecurity "
                + "FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace "
                + "WHERE n.nspname IN ('nexus','staging') AND cl.relkind = 'r' ORDER BY 1"));
        // rdr180-3..7 are ALTER COLUMN ... TYPE bytea conversions carrying empty
        // <rollback/>, and the octet_length CHECK renders identically for text
        // and bytea — so nothing else here would notice a column that came back
        // as the wrong type.
        shape.put("columns", query(c,
            "SELECT table_schema || '.' || table_name || '.' || column_name || ' ' "
                + "|| data_type || ' null=' || is_nullable "
                + "FROM information_schema.columns "
                + "WHERE table_schema IN ('nexus','staging') ORDER BY 1"));
        return shape;
    }

    private static List<String> query(Connection c, String sql) throws Exception {
        List<String> out = new ArrayList<>();
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            while (rs.next()) {
                out.add(rs.getString(1));
            }
        }
        return out;
    }

    /** The last {@code n} changeset ids by execution order (newest first). */
    private static List<String> executionTail(Connection c, int n) throws Exception {
        return query(c,
            "SELECT id FROM databasechangelog ORDER BY orderexecuted DESC LIMIT " + n);
    }

    /**
     * Changeset ids occupying more than one DATABASECHANGELOG row, with their
     * row counts — the direct signal for nexus-ixsxa. Empty is the invariant.
     */
    private static List<String> duplicateChangelogRows(Connection c) throws Exception {
        return query(c,
            "SELECT id || ' (' || author || ') x' || count(*) FROM databasechangelog "
                + "GROUP BY id, author HAVING count(*) > 1 ORDER BY 1");
    }

    /**
     * nexus_diag's direct SELECT grants on BASE TABLES only. The relkind filter
     * is load-bearing: the counts view is granted by its superuser owner and
     * must SURVIVE the view-era revoke, so counting it would make the
     * post-revoke assertion unsatisfiable.
     */
    private static List<String> diagBaseTableGrants(Connection c) throws Exception {
        return query(c,
            "SELECT g.table_schema || '.' || g.table_name "
                + "FROM information_schema.role_table_grants g "
                + "JOIN pg_namespace n ON n.nspname = g.table_schema "
                + "JOIN pg_class cl ON cl.relname = g.table_name "
                + "  AND cl.relnamespace = n.oid "
                + "WHERE g.grantee = 'nexus_diag' AND g.privilege_type = 'SELECT' "
                + "AND g.table_schema IN ('nexus','t1') AND cl.relkind IN ('r','p') "
                + "ORDER BY 1");
    }

    private static int changelogRowCount(Connection c) throws Exception {
        try (Statement st = c.createStatement();
             ResultSet rs = st.executeQuery(
                 "SELECT count(*) FROM databasechangelog")) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private static List<String> tablesInSchema(Connection c, String schema) throws Exception {
        return query(c,
            "SELECT tablename FROM pg_tables WHERE schemaname = '" + schema + "' ORDER BY 1");
    }

    // ── Container bootstrap (mirrors SchemaUpgradeRehearsalIntegrationTest) ──

    private static void dbaBootstrap(Connection su) throws Exception {
        su.setAutoCommit(true);
        su.createStatement().execute(
            "CREATE ROLE " + ADMIN_ROLE + " LOGIN PASSWORD '" + ADMIN_PASS
                + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
        su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + ADMIN_ROLE);
        su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + ADMIN_ROLE);
        su.createStatement().execute(
            "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' "
                + "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
        su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
        su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
    }

    private static HikariDataSource newAdminPool(PostgreSQLContainer<?> pg, String poolName) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName(poolName);
        return new HikariDataSource(cfg);
    }
}
