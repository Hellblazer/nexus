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
 * ({@code ORDEREXECUTED}), NOT in master-file order. The tree has exactly five
 * {@code runAlways} changesets, and they re-execute on every boot, so on any
 * cluster that has booted more than once they float to the tail of execution
 * order (master positions as of this commit, which the floor changeset shifted
 * by one):
 *
 * <pre>
 *   master pos 194  staging-4-svc-grants
 *   master pos 205  grants-nexus-svc-1
 *   master pos 206  grants-002-changelog-read
 *   master pos 207  grants-nexus-diag-1
 *   master pos 208  grants-nexus-diag-2
 * </pre>
 *
 * That is exactly, and in order, the five changesets the manual {@code
 * rollbackCount(10)} repro rolled back before dying on staging-4 — it was run
 * against a re-booted cluster, where staging-4 had floated from master position
 * 194 to execution depth 5.
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
 * <p><strong>To a tag, not a count.</strong> {@code rollbackCount(N)} is wrong
 * three ways here: N must be re-derived whenever a changeset lands, it covers
 * the wrong set the first time one is INSERTED rather than appended, and it
 * counts DATABASECHANGELOG ROWS — so on any cluster carrying the nexus-ixsxa
 * duplicate rows it reaches a different depth than intended, drifting further
 * every boot. {@code floor-001-rollback-floor.xml} tags the boundary instead.
 *
 * <p><strong>Where the floor sits, and why not at the extensions.</strong> The
 * tag is included second in master, immediately after the must-be-first role
 * bootstrap. It cannot sit just above the extension baseline, which would be the
 * intuitive place: {@code vectors-001} is MID-tree — role-001, memory-001,
 * plans-001, telemetry-*, t1-001, taxonomy-001, aspects-* , chash-001 and
 * catalog-001 all precede it — so no position is both above the extensions and
 * below every migration-owned table. A floor placed there leaves 26 tables
 * beneath it, never rolled back, while the rollback still reports success
 * (measured, 2026-07-27). So the floor goes at the bottom and the two changesets
 * that are genuinely DBA-owned provisioning declare their own rollbacks no-ops:
 * {@code role-001-nexus-svc} (roles are cluster-level) and {@code vectors-001-1}
 * (untrusted extensions a NOSUPERUSER role can neither create nor drop).
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
     * The rollback floor, applied by {@code floor-1-rollback-floor} in
     * {@code floor-001-rollback-floor.xml} (include #2 of master). The only
     * changeset below it is {@code role-001-1}, the must-be-first role
     * bootstrap — so a rollback to this tag reverts every migration-owned
     * table.
     *
     * <p>The extensions are NOT below the floor. {@code vectors-001} is include
     * #19, well above it; {@code vector} and {@code pg_trgm} survive a rollback
     * because {@code vectors-001-1} declares its rollback irreversible, not
     * because of any positional relationship. (An earlier design did put the
     * floor at the extensions; it cannot work — see the class javadoc.)
     *
     * <p><strong>FRESH-INIT ONLY — see nexus-9vg5g.</strong> {@code tagDatabase}
     * tags the MOST RECENTLY EXECUTED DATABASECHANGELOG row, not its own. On a
     * fresh database that row is {@code role-001-1}, which is what makes this
     * test's rollback reach the bottom (and why the floor changeset is itself
     * rolled back — visible in the run log). On an ALREADY-MIGRATED cluster the
     * floor changeset is unrun, so when it executes the most recent row is the
     * previous boot's tail, and the tag lands there; a rollback to it reverts
     * only the handful of changesets since. This harness always starts fresh
     * and therefore cannot observe that. Do not read a green run here as
     * evidence that rollback-to-tag works on a deployed cluster.
     */
    private static final String ROLLBACK_FLOOR_TAG = "rollback-floor";

    /**
     * The five {@code runAlways} changesets, in master order. Their identity is
     * asserted (not merely their count) so that adding or removing a
     * {@code runAlways} changeset forces a deliberate look at this test rather
     * than silently changing which changesets the rollback leg reaches first.
     */
    private static final List<String> RUN_ALWAYS_IDS = List.of(
        "staging-4-svc-grants",
        "grants-nexus-svc-1",
        "grants-002-changelog-read",
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
     * <p><strong>What this test found on its first run (nexus-ixsxa).</strong>
     * Four of the five {@code runAlways} changesets are recorded {@code RERAN}
     * and update in place. {@code grants-nexus-diag-2} resolves its precondition
     * {@code onFail="MARK_RAN"} wherever {@code nexus.diag_chash_conformance}
     * is absent (the view comes from the superuser provisioning path, not from
     * Liquibase) — and Liquibase records a MARK_RAN by INSERTING a row, so the
     * changelog grows by one row on every boot of such a cluster, without
     * bound. That is a forward-path defect tracked on its own bead, not
     * something this test asserts; it is logged loudly here instead, because it
     * also skews any rollback-depth arithmetic computed from row counts.
     */
    @Test
    void runAlwaysChangesetsFloatToTheExecutionTail() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
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
                // nexus-ixsxa, found by this test's first-ever run: a runAlways
                // changeset whose precondition resolves onFail=MARK_RAN gets a
                // NEW row per boot instead of an in-place update, so the
                // changelog grows without bound wherever that precondition is
                // unmet. Reported here rather than asserted, because the fix is
                // a forward-path change tracked on its own bead — but reported
                // LOUDLY rather than tolerated in silence, since it also skews
                // any rollback-depth arithmetic done by row count.
                List<String> duplicated;
                try (Connection c = ds.getConnection()) {
                    duplicated = query(c,
                        "SELECT id || ' (' || author || ') x' || count(*) FROM databasechangelog "
                            + "GROUP BY id, author HAVING count(*) > 1 ORDER BY 1");
                }
                if (afterSecond != afterFirst) {
                    log.warn("event=changelog_row_growth_on_reboot bead=nexus-ixsxa "
                            + "first={} second={} duplicated={}",
                        afterFirst, afterSecond, duplicated);
                }
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
        PostgreSQLContainer<?> pg = PgContainerHelper.start();
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
                assertThatCode(() -> rollbackToFloor(ds))
                    .as("the ENTIRE migration-owned rollback chain must execute, down to the "
                        + "'%s' tag. A failure here names the first changeset whose <rollback> is "
                        + "broken, missing, or references an object a later changeset retired — "
                        + "the staging-4 class, invisible to every other signal we have",
                        ROLLBACK_FLOOR_TAG)
                    .doesNotThrowAnyException();

                try (Connection c = ds.getConnection()) {
                    // EXACTLY one, not merely fewer. role-001-1 is the sole
                    // changeset below the floor (role-001-nexus-svc.xml holds
                    // one changeset, and it is pinned must-be-first in master),
                    // and the floor changeset itself is rolled back because
                    // tagDatabase tagged role-001-1's row rather than its own.
                    // `isLessThan(applied)` was satisfied by rolling back a
                    // SINGLE changeset — it did not check depth at all.
                    assertThat(changelogRowCount(c))
                        .as("rolling back to '%s' must leave EXACTLY role-001-1 — a rollback that "
                            + "silently stops early and reports success is the failure mode this "
                            + "test exists to catch", ROLLBACK_FLOOR_TAG)
                        .isEqualTo(1);
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
     * Roll the whole migration-owned schema back to {@link #ROLLBACK_FLOOR_TAG}.
     * There is deliberately no production code path for this — the engine only
     * ever calls {@code update} — so the test drives {@code liquibase.Liquibase}
     * itself, the same way an operator would from the CLI.
     *
     * <p>To a TAG, not a count. A numeric depth would have to be re-derived
     * every time a changeset landed, and would silently cover the wrong set the
     * first time one was inserted rather than appended. It would also be wrong
     * on any cluster carrying the nexus-ixsxa duplicate rows, since
     * {@code rollbackCount} counts ROWS. The tag is immune to both.
     */
    private static void rollbackToFloor(HikariDataSource ds) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG_RELATIVE,
                    new ClassLoaderResourceAccessor(),
                    database)) {
                liquibase.rollback(ROLLBACK_FLOOR_TAG, new Contexts(), new LabelExpression());
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
