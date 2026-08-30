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
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.sql.Types;
import java.time.OffsetDateTime;
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
 * ({@code ORDEREXECUTED}), NOT in master-file order. The tree has exactly eight
 * {@code runAlways} changesets (RDR-191 Phase 4 unify added {@code
 * grants-005-chunks-unify-maintain} in the SAME grants-nexus-svc.xml file,
 * immediately after nexus-hzhgl's {@code grants-004-monitor-wal-visibility},
 * which itself followed nexus-0ys55's {@code grants-003-purge-vacuum-maintain}),
 * and they re-execute on every boot, so on any cluster that has booted more
 * than once they float to the tail of execution order (approximate master
 * positions, already drifting further with every changeset RDR-191 Phase 4
 * inserts ahead of grants-nexus-svc.xml — exact numbers are not load-bearing
 * here, only the runAlways set and its relative order):
 *
 * <pre>
 *   master pos ~194  staging-4-svc-grants
 *   master pos ~205  grants-nexus-svc-1
 *   master pos ~206  grants-002-changelog-read
 *   master pos ~207  grants-003-purge-vacuum-maintain
 *   master pos ~208  grants-004-monitor-wal-visibility
 *   master pos ~209  grants-005-chunks-unify-maintain
 *   master pos ~210  grants-nexus-diag-1
 *   master pos ~211  grants-nexus-diag-2
 * </pre>
 *
 * That is exactly, and in order, the five changesets the manual {@code
 * rollbackCount(10)} repro rolled back before dying on staging-4 — it was run
 * against a re-booted cluster, where staging-4 had floated from master position
 * 194 to execution depth 5 (now depth 8, after nexus-0ys55's, nexus-hzhgl's,
 * and RDR-191 Phase 4's additions).
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
 * <p><strong>A window between two full-depth rollbacks (nexus-lelhx, found by
 * {@link #typeHygieneRollback_restoresExactDataAndRoundTripsForward}'s stage
 * 2).</strong> {@code rdr180-3/4/5-convert-chunks-*} (in {@code
 * rdr180-001-bytea-chash.xml}) used to carry an EMPTY {@code <rollback/>} for
 * {@code chunks_384/768/1024.chash} — safe only when paired with a rollback
 * deep enough to later drop those tables outright ({@code
 * vectors-001-baseline}'s own rollback, which a full rollback-to-zero always
 * reaches). {@code vectors-004-unify-chunks.xml}'s rollback recreates those
 * same three tables with {@code chash BYTEA} (correct for its own immediate
 * pre-drop state). A rollback deep enough to undo {@code vectors-004} but not
 * deep enough to undo {@code vectors-001-baseline} — the window {@code
 * catalog-002-1-temporal-typing} sits in — used to leave {@code chash}
 * permanently {@code BYTEA}, so a full forward re-apply failed re-executing
 * {@code catalog-006-4} (written for {@code TEXT chash}) with "operator does
 * not exist: text = bytea." Fixed by giving {@code rdr180-3/4/5} a real,
 * guarded rollback (restores {@code TEXT} via the changeset's own
 * reversibility lemma — see that changeset's inline comment). So the claim
 * this class's title makes — every prefix of the full rollback-to-zero
 * sequence is itself safe to roll back to AND forward-reapply from — is true
 * again for every changeset except the three declared-irreversible ones named
 * above ({@code catalog-016-0}, {@code vectors-001-1}, {@code role-001-1}),
 * which are irreversible by declaration, not by omission.
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
 * any deployed cluster. Independently re-verified for this codebase's Liquibase
 * 4.29.0 as part of nexus-lelhx: a throwaway probe applied the full changelog,
 * recorded {@code rdr180-3-convert-chunks-384}'s {@code MD5SUM}, edited only its
 * {@code <rollback>} in a scratch copy of the changelog tree, and re-ran {@code
 * update()} against that scratch tree — the {@code MD5SUM} was unchanged and no
 * {@code ValidationFailedException} was raised.
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
     * The ELEVEN {@code runAlways} changesets, in master order. Formerly ten after
     * RDR-194's critical fix round (2026-08-17) added {@code taxonomy-011-8} —
     * Liquibase-owned {@code nexus.diag_chash_conformance} view creation +
     * conditional nexus_diag grant, self-healing every boot exactly like
     * the grants-nexus-diag changesets; see that changeset's own comment
     * in {@code taxonomy-011-doc-id-bytea.xml}. Formerly nine after
     * nexus-8yz1p added {@code grants-nexus-diag-3} — a THIRD,
     * era-independent nexus_diag changeset for staging-schema SELECT,
     * deliberately its own changeset rather than folded into
     * grants-nexus-diag-1's era-gated body; see that changeset's own
     * comment. Formerly eight after RDR-191 Phase 4 unify added
     * {@code grants-005-chunks-unify-maintain}, formerly seven after
     * nexus-hzhgl added {@code grants-004-monitor-wal-visibility}, formerly
     * six after nexus-0ys55 added {@code grants-003-purge-vacuum-maintain},
     * formerly five. 2026-08-19 (production gc_audit InsufficientPrivilege
     * incident, engine-service-v0.1.82) added {@code grants-nexus-diag-4} —
     * a FOURTH, era-independent nexus_diag changeset that grants SELECT on
     * {@link dev.nexus.service.db.CatalogRepository#NEXUS_DIAG_READABLE_TABLES}
     * (search_telemetry, gc_audit — relevance_log already covered by
     * grants-nexus-diag-3; hook_failures deliberately excluded, see that
     * changeset's own comment); see that changeset's own comment.
     * Their identity is asserted (not merely their count) so
     * that adding or removing a {@code runAlways} changeset forces a
     * deliberate look at this test rather than silently changing which
     * changesets the rollback leg reaches first.
     */
    private static final List<String> RUN_ALWAYS_IDS = List.of(
        "staging-4-svc-grants",
        "taxonomy-011-8",
        "grants-nexus-svc-1",
        "grants-002-changelog-read",
        "grants-003-purge-vacuum-maintain",
        "grants-004-monitor-wal-visibility",
        "grants-005-chunks-unify-maintain",
        "grants-nexus-diag-1",
        "grants-nexus-diag-2",
        "grants-nexus-diag-3",
        "grants-nexus-diag-4");

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
     * The nexus_diag ERA TRANSITION. Two properties, asserted together
     * because they share one cause.
     *
     * <p><strong>REVISED 2026-08-17 (RDR-194 critical fix round, critic
     * Sig-2 / bead nexus-i3k3e's Sig-2 finding, taxonomy-011-8).</strong>
     * The "legacy era" this test originally exercised naturally (view
     * absent when {@code grants-nexus-diag-1} first ran, until a SEPARATE
     * superuser step created the view) is now STRUCTURALLY UNREACHABLE from
     * ANY fresh {@code migrate()} call: {@code taxonomy-011-8}, placed
     * BEFORE {@code grants-nexus-diag.xml} in the master changelog,
     * self-heals {@code nexus.diag_chash_conformance} into existence on
     * EVERY walk (creating it if absent, tolerating a foreign owner if
     * already present) — so by the time {@code grants-nexus-diag-1}'s own
     * era guard runs, the view has ALWAYS already been (re)created in the
     * SAME walk, on EVERY cluster, fresh or upgrading. This is the intended
     * effect of closing nexus-i3k3e's Sig-2 gap, not a regression: nothing
     * exercises legacy grants "for real" anymore. Two things are now
     * asserted instead of the old two-phase narrative:
     * <ol>
     *   <li>The NEW invariant — a single fresh {@code migrate()} lands
     *       DIRECTLY in view era (no base-table grants ever fire), proven
     *       below.</li>
     *   <li>{@code grants-nexus-diag-1}'s LEGACY BRANCH's own SQL LOGIC is
     *       still correct, even though it is now practically unreachable
     *       via a live walk — proven by extracting and directly replaying
     *       its {@code <sql>} text (the same technique
     *       {@link Taxonomy010BackfillDirectIntegrationTest} uses for its
     *       own structurally-unreachable-via-rehearsal arm) against a
     *       connection where the view has been dropped out-of-band. This
     *       is DEFENSE IN DEPTH, not a claim that a real cluster can reach
     *       this state: a real cluster's next boot re-heals the view via
     *       {@code taxonomy-011-8} before {@code grants-nexus-diag-1} ever
     *       sees it absent again.</li>
     * </ol>
     *
     * <p><strong>The row invariant (nexus-ixsxa), unchanged.</strong> A
     * reboot must re-stamp the {@code runAlways} rows in place, not grow
     * the changelog.
     *
     * <p><strong>REVISED AGAIN (2026-08-17, bead nexus-lhuhe, P1).</strong>
     * The 2026-08-17 revision above asserted {@link #diagBaseTableGrants}
     * is EMPTY in view era and called that "the content boundary" — that
     * assertion LOCKED IN a real bug as intended behavior. Fork-verified:
     * after a full walk, {@code taxonomy-011-doc-id-bytea.xml} (included
     * BEFORE this file in {@code db.changelog-master.xml}) creates the
     * view and grants {@code nexus_diag} SELECT on it via
     * {@code taxonomy-011-8}, but {@code grants-nexus-diag-2}'s later
     * per-relation REVOKE loop strips that grant again in the SAME boot
     * (it owns the view by then too) — leaving {@code nexus_diag} with
     * ZERO SELECT anywhere in the content boundary, not even on the view
     * itself. {@code security_invoker=true} means the view ALSO needs
     * direct SELECT on every table it reads, so "view era" grants are
     * correctly NON-empty: the five {@code CHASH_BEARING_TABLES}
     * (src/nexus/db/chash_tables.py) — see {@code grants-nexus-diag-3}'s
     * own comment for the fix. {@link #diagBaseTableGrants} filters
     * {@code relkind IN ('r','p')} so it never counts the view itself.
     *
     * <p><strong>REVISED AGAIN (2026-08-19, production gc_audit
     * InsufficientPrivilege incident, engine-service-v0.1.82).</strong>
     * {@code grants-nexus-diag-4} adds two more, era-independent:
     * {@code search_telemetry} and {@code gc_audit} — exactly
     * {@code CatalogRepository.NEXUS_DIAG_READABLE_TABLES}, each
     * independently verified content-free at the write-path level (NOT
     * {@code AUDIT_ONLY_TABLES}'s classification — {@code hook_failures}
     * shares that classification but is deliberately excluded from the
     * grant; see grants-nexus-diag-4's own comment). The expected set
     * below is now seven tables, not five.
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
                // ── A single fresh migrate() now lands DIRECTLY in view
                // era: taxonomy-011-8 (earlier in the master changelog)
                // self-heals the view into existence before diag-1 ever
                // runs in this SAME walk, so diag-1's legacy branch never
                // fires on a fresh cluster (2026-08-17 fix; see javadoc).
                int viewEraRows;
                SchemaMigrator.migrate(ds);
                try (Connection c = ds.getConnection()) {
                    assertThat(count(c,
                        "SELECT count(*) FROM pg_class cl JOIN pg_namespace n "
                        + "ON n.oid = cl.relnamespace WHERE n.nspname = 'nexus' "
                        + "AND cl.relname = 'diag_chash_conformance'"))
                        .as("taxonomy-011-8 must have created the view in this SAME "
                            + "fresh walk, before grants-nexus-diag-1 ever ran")
                        .isEqualTo(1);
                    assertThat(diagBaseTableGrants(c))
                        .as("a FRESH cluster must land DIRECTLY in view era — "
                            + "grants-nexus-diag-1's legacy branch must NOT have fired "
                            + "(its bulk ALL-TABLES grant would produce a much wider set "
                            + "than the seven below) — but grants-nexus-diag-3 must have "
                            + "re-granted exactly the tables security_invoker=true "
                            + "requires, restoring what grants-nexus-diag-2's own "
                            + "REVOKE loop stripped earlier in this SAME boot "
                            + "(nexus-lhuhe), and grants-nexus-diag-4 must have granted "
                            + "the two NEXUS_DIAG_READABLE_TABLES it does not already "
                            + "cover (2026-08-19 gc_audit incident; hook_failures "
                            + "deliberately excluded)")
                        .containsExactly(
                            "nexus.catalog_document_chunks",
                            "nexus.chunks",
                            "nexus.frecency",
                            "nexus.gc_audit",
                            "nexus.relevance_log",
                            "nexus.search_telemetry",
                            "nexus.topic_assignments");
                    viewEraRows = changelogRowCount(c);
                }

                SchemaMigrator.migrate(ds);
                try (Connection c = ds.getConnection()) {
                    assertThat(duplicateChangelogRows(c))
                        .as("a reboot must not grow the changelog with duplicate "
                            + "runAlways rows (nexus-ixsxa)")
                        .isEmpty();
                    assertThat(changelogRowCount(c))
                        .as("a reboot must re-stamp the runAlways rows in place")
                        .isEqualTo(viewEraRows);
                    assertThat(diagBaseTableGrants(c))
                        .as("view era holds across a reboot too — the same seven tables, "
                            + "re-granted fresh by grants-nexus-diag-3 and grants-nexus-diag-4 "
                            + "every boot (nexus-lhuhe; gc_audit incident 2026-08-19)")
                        .containsExactly(
                            "nexus.catalog_document_chunks",
                            "nexus.chunks",
                            "nexus.frecency",
                            "nexus.gc_audit",
                            "nexus.relevance_log",
                            "nexus.search_telemetry",
                            "nexus.topic_assignments");
                }
            }

            // ── DEFENSE IN DEPTH (see javadoc): grants-nexus-diag-1's
            // legacy branch is now practically unreachable via a live walk,
            // but its OWN SQL logic must still be correct for the day it
            // matters again (a cluster whose changelog predates
            // taxonomy-011-8, upgrading through this exact changeset).
            // Direct-replay proof, bypassing Liquibase's own ordering
            // entirely — mirrors Taxonomy010BackfillDirectIntegrationTest's
            // extractChangesetSql technique.
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute("DROP VIEW nexus.diag_chash_conformance");
                String sql = extractChangesetSql(
                    "db/changelog/grants-nexus-diag.xml", "grants-nexus-diag-1");
                su.createStatement().execute(sql);
                assertThat(diagBaseTableGrants(su))
                    .as("grants-nexus-diag-1's own legacy-branch SQL, replayed directly "
                        + "against a connection where the view genuinely does not exist, "
                        + "must still grant nexus_diag direct table SELECT — the branch's "
                        + "logic itself is unbroken even though a live walk can no longer "
                        + "reach it")
                    .isNotEmpty();
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * Read the direct {@code <sql>} children of {@code <changeSet id="changesetId">}
     * out of {@code changelogFile} (classpath resource), concatenated in document
     * order. Same technique as
     * {@code Taxonomy010BackfillDirectIntegrationTest#extractChangesetSql}
     * (duplicated locally rather than shared — no common test-utility class
     * exists for this yet).
     */
    private static String extractChangesetSql(String changelogFile, String changesetId) throws Exception {
        org.w3c.dom.Document doc;
        try (var in = SchemaRollbackRoundTripIntegrationTest.class.getClassLoader()
                .getResourceAsStream(changelogFile)) {
            if (in == null) {
                throw new IllegalStateException("changelog not found on classpath: " + changelogFile);
            }
            var factory = javax.xml.parsers.DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            doc = factory.newDocumentBuilder().parse(in);
        }
        org.w3c.dom.NodeList changeSets = doc.getElementsByTagNameNS(
            "http://www.liquibase.org/xml/ns/dbchangelog", "changeSet");
        for (int i = 0; i < changeSets.getLength(); i++) {
            org.w3c.dom.Element cs = (org.w3c.dom.Element) changeSets.item(i);
            if (changesetId.equals(cs.getAttribute("id"))) {
                StringBuilder sb = new StringBuilder();
                org.w3c.dom.NodeList children = cs.getChildNodes();
                for (int j = 0; j < children.getLength(); j++) {
                    org.w3c.dom.Node n = children.item(j);
                    if (n.getNodeType() == org.w3c.dom.Node.ELEMENT_NODE && "sql".equals(n.getLocalName())) {
                        sb.append(n.getTextContent()).append('\n');
                    }
                }
                return sb.toString();
            }
        }
        throw new IllegalStateException("changeset not found: " + changesetId + " in " + changelogFile);
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

    // ── Data-fidelity round trip (nexus-cck6z) ───────────────────────────────
    //
    // The round trip above proves the rollback SQL RUNS; it seeds no rows, so
    // the restore expressions of the schema type-hygiene arc (epic
    // nexus-cefa1: catalog-031, telemetry-004, aspects-003, plans-002,
    // taxonomy-008 — 14 columns across 8 tables) PLUS the RDR-156 template
    // they copy (catalog-002-hygiene.xml: catalog_collections.created_at /
    // .superseded_at — 2 more columns) are never asserted against DATA. This
    // closes that gap: seed representative typed rows on the CURRENT
    // (post-arc) schema, then roll back in TWO STAGES, run one AFTER the
    // OTHER's complete round trip (never nested/continued deeper from the
    // first stage's already-rolled-back state).
    //
    // Stage 1 rolls back through catalog-031-1-documents-temporal (the
    // earliest of the arc's five family changesets — reaching it also rolls
    // back telemetry-004, aspects-003, plans-002, taxonomy-008 and
    // everything executed after all of them), asserts the arc's 14 columns,
    // then re-applies the FULL remaining chain forward
    // ({@code SchemaMigrator.migrate}) and re-asserts those 14 columns.
    // This restores the schema to full HEAD before stage 2 begins.
    //
    // Stage 2 THEN rolls back through catalog-002-1-temporal-typing (the
    // RDR-156 template the arc copies — far earlier in master order than
    // catalog-031) and asserts its 2 columns, but does NOT re-apply the
    // full chain forward — see the KNOWN GAP note at stage 2's call site
    // for why (a genuine, pre-existing, unrelated migration-chain defect
    // this test discovered: rdr180-3/4/5's empty rollback plus vectors-004-
    // unify-chunks.xml's bytea-typed recreate leaves catalog-006-4
    // unre-applicable from any rollback depth between vectors-001-baseline
    // and vectors-004, a window catalog-002 sits inside). Stage 2 instead
    // re-applies FORWARD BY COUNT ({@link #reapplyForward}, Liquibase's
    // {@code update(int,...)}) for exactly the one changeset it rolled back
    // to reach, proving catalog-002's OWN forward re-apply without walking
    // into that unrelated gap.
    //
    // The two stages run in this order — never one continuing deeper from
    // the other's rolled-back state — specifically so stage 1's forward
    // re-apply assertions run against a schema stage 2 has not yet touched.
    // An earlier revision of this test rolled back to catalog-002 directly
    // from stage 1's already-rolled-back state; catalog-020-index-run-
    // fence.xml's catalog-020-1 (which ADDS catalog_documents
    // .index_started_at, and sits BETWEEN catalog-002 and catalog-031 in
    // master order) has an unconditional DROP COLUMN rollback (the column
    // did not exist before catalog-020-1), so that continuation dropped the
    // column out from under stage 1's own index_started_at assertions
    // before they could run — the present two-independent-stages structure
    // fixes that ordering hazard as a side effect.
    //
    // Each stage's rollback is safe regardless of depth: each is a PREFIX
    // of the exact sequence
    // {@link #fullChangelog_rollsBackCompletely_andReappliesToTheSameSchema}
    // already proves rolls back to zero without exception, and neither
    // touches a table this test seeds (catalog_documents.index_started_at
    // is a COLUMN, not a table).
    //
    // Both rollback depths are computed DYNAMICALLY from DATABASECHANGELOG's
    // live orderexecuted (see {@link #rollbackDepthThrough}), never a
    // hardcoded changeset count — a future changeset appended after any of
    // these targets is absorbed automatically, by construction, without this
    // test needing to change.
    //
    // jsonb-family assertions never hand-guess Postgres's own object-key
    // canonicalization (verified empirically elsewhere in this tree —
    // PlanRepositoryTest.savePlan_planJson_jsonbCanonicalizesWhitespaceAndKeyOrder:
    // shorter keys sort first, ties broken lexically, and whitespace
    // normalizes to `": "` / `", "`); {@link #canonicalJsonbText} computes
    // the expected TEXT live, off the same Postgres, via a scratch
    // {@code ::jsonb::text} cast on the ORIGINAL literal. Timestamptz-family
    // assertions likewise never hand-guess a rendering: catalog-031/
    // telemetry-004/etc.'s arc rollbacks use a FIXED-WIDTH
    // {@code to_char(...,'.US"+00:00"')} format, but catalog-002's own
    // (RDR-156-era, unmodified by the arc) rollback is a bare
    // {@code col::text} cast — Postgres's DEFAULT timestamptz-to-text
    // rendering, which trims trailing fractional zeros and uses a bare
    // {@code +00} offset rather than the arc's fixed 6-digit
    // {@code .000000+00:00}. {@link #defaultTimestamptzText} computes THAT
    // shape live, off the same Postgres, so this test asserts the two
    // rollback families' genuinely different TEXT shapes without guessing
    // either one. That keeps every assertion honest about what it protects:
    // not a specific spelling, but that the rollback's {@code USING} clause
    // reads the RIGHT column through the RIGHT cast.
    @Test
    void typeHygieneRollback_restoresExactDataAndRoundTripsForward() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                dbaBootstrap(su);
            }
            try (HikariDataSource ds = newAdminPool(pg, "nexus-admin-rollback-fidelity")) {
                SchemaMigrator.migrate(ds);

                long topicA;
                long topicB;
                try (Connection su = pg.createConnection("")) {
                    seedTypeHygieneFixtures(su);
                    long[] topicIds = seedTopicLinksFixture(su);
                    topicA = topicIds[0];
                    topicB = topicIds[1];
                }

                // Stage 1: roll back through catalog-031-1 (the earliest of the ARC's five
                // family changesets) and assert the arc's 14 columns. Deliberately a
                // SEPARATE, SHALLOWER stage from catalog-002 below — catalog-020-index-
                // run-fence.xml's catalog-020-1 (which ADDS catalog_documents
                // .index_started_at) sits BETWEEN catalog-002 and catalog-031 in master
                // order, and its rollback is `DROP COLUMN IF EXISTS index_started_at`
                // (unconditional, not "restore to the old shape" — the column did not
                // exist before catalog-020-1). A single rollback deep enough to reach
                // catalog-002 would drop that column out from under this stage's own
                // assertions, so index_started_at's restore-to-TEXT behaviour is checked
                // HERE, at the shallower depth, while the column still exists.
                int depthToArc;
                try (Connection c = ds.getConnection()) {
                    depthToArc = rollbackDepthThrough(c, "catalog-031-1-documents-temporal");
                }
                assertThat(depthToArc)
                    .as("catalog-031-1-documents-temporal must have executed exactly once before "
                        + "this rollback — a zero depth means the id was not found in "
                        + "DATABASECHANGELOG, which would make the targeted rollback below a no-op "
                        + "and every assertion after it vacuous")
                    .isGreaterThan(0);

                assertThatCode(() -> rollbackEverything(ds, depthToArc))
                    .as("rolling back the %d changesets through catalog-031-1-documents-temporal "
                        + "must execute cleanly — a failure here names the first broken <rollback> "
                        + "in that range, same diagnostic value as the full round trip above",
                        depthToArc)
                    .doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    assertRolledBackColumnShapesAndValues(su, topicA, topicB);
                }

                assertThatCode(() -> SchemaMigrator.migrate(ds))
                    .as("the changelog must re-apply cleanly onto the rolled-back fixtures")
                    .doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    assertForwardRoundTrip(su, topicA, topicB);
                }

                // Stage 2, run AFTER stage 1's own complete round trip (the schema is back
                // at full HEAD here) rather than continuing deeper from stage 1's already-
                // rolled-back state: rolls back through catalog-002-1-temporal-typing (the
                // RDR-156 template the arc copies — far earlier in master order than
                // catalog-031) and asserts its 2 columns.
                //
                // FORMERLY-BROKEN WINDOW, FIXED by nexus-lelhx (was: KNOWN GAP, discovered by
                // this test, filed rather than fixed under nexus-cck6z's own narrower scope).
                // rdr180-3/4/5-convert-chunks-* (in rdr180-001-bytea-chash.xml) used to carry
                // an EMPTY <rollback/> for the three per-dimension chunk tables' chash columns
                // — safe ONLY when paired with a deeper rollback that later drops those tables
                // entirely (e.g. vectors-001-baseline's own rollback, which a FULL rollback to
                // zero always reaches). vectors-004-unify-chunks.xml's rollback (later, master
                // position ~497) RECREATES those same three tables with chash BYTEA (correct
                // for ITS OWN immediate pre-drop state). A rollback deep enough to undo
                // vectors-004 (position ~497) but NOT deep enough to undo vectors-001-baseline
                // (position ~82) — exactly the window catalog-002, position ~139, sits in —
                // used to leave that chash column permanently BYTEA, because rdr180-3's
                // rollback was a no-op. A FULL forward re-apply from that state then failed
                // re-executing catalog-006-4 (position ~167, checksum-frozen, written for TEXT
                // chash) with "operator does not exist: text = bytea" — confirmed by running
                // exactly that and capturing the error (nexus-lelhx's own reproduction).
                //
                // THE FIX: rdr180-3/4/5 now carry a real, guarded rollback — ALTER TABLE IF
                // EXISTS ... TYPE TEXT USING the changeset's own file-header reversibility
                // lemma (octet_length=16 -> encode(...,'hex'), else convert_from(...,'UTF8'),
                // the same CASE idiom as ChashSqlIdioms.OLD_REF_LEMMA) — restoring the exact
                // pre-conversion TEXT shape so a subsequent forward re-apply of catalog-006-4
                // sees chash genuinely TEXT again, same as a fresh install would at that master
                // position. Checksum-safe per this class's own javadoc (Liquibase excludes
                // <rollback> from the changeset md5sum) — independently re-verified for THIS
                // codebase's Liquibase 4.29.0 as part of nexus-lelhx (before/after MD5SUM
                // identical, no ValidationFailedException on reapply against the edited
                // changelog).
                //
                // So, UNLIKE the pre-fix version of this test, this stage no longer stops at a
                // targeted count=1 reapply to dodge catalog-006-4's territory — it keeps that
                // targeted reapply (still a valid, independent proof that catalog-002-1's OWN
                // forward re-apply is exact) AND THEN continues with a FULL {@code
                // SchemaMigrator.migrate(ds)} to walk the rest of the chain — including
                // catalog-006-4 and rdr180-3/4/5 — all the way back to HEAD. That full-migrate
                // call is nexus-lelhx's exact reproduction, now green.
                int depthToTemplate;
                try (Connection c = ds.getConnection()) {
                    depthToTemplate = rollbackDepthThrough(c, "catalog-002-1-temporal-typing");
                }
                assertThat(depthToTemplate)
                    .as("catalog-002-1-temporal-typing must have executed exactly once before "
                        + "this second rollback stage — a zero depth means the id was not found "
                        + "in DATABASECHANGELOG")
                    .isGreaterThan(0);

                assertThatCode(() -> rollbackEverything(ds, depthToTemplate))
                    .as("rolling back the %d changesets through catalog-002-1-temporal-typing "
                        + "must execute cleanly", depthToTemplate)
                    .doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    assertCatalog002ColumnShapesAndValues(su);
                }

                assertThatCode(() -> reapplyForward(ds, 1))
                    .as("catalog-002-1-temporal-typing (the single lowest-numbered missing "
                        + "changeset after the rollback above) must re-apply cleanly on its own — "
                        + "a targeted count=1 update, independent of and strictly weaker than the "
                        + "full-migrate proof that follows below")
                    .doesNotThrowAnyException();

                try (Connection su = pg.createConnection("")) {
                    assertCatalog002ForwardRoundTrip(su);
                }

                // nexus-lelhx's exact reproduction, now green: continue forward from here —
                // still rolled back everywhere past catalog-002-1-temporal-typing — with a
                // FULL SchemaMigrator.migrate(ds) rather than another targeted count. This
                // walks straight through catalog-006-4 (position ~167) and rdr180-3/4/5
                // (position ~276-278), the exact sequence that used to raise "operator does
                // not exist: text = bytea" when rdr180-3/4/5's rollback was empty.
                assertThatCode(() -> SchemaMigrator.migrate(ds))
                    .as("the REMAINDER of the chain (catalog-006-4 through HEAD, including "
                        + "rdr180-3/4/5 and vectors-004-unify-chunks.xml) must re-apply cleanly "
                        + "from here — before nexus-lelhx's fix, chunks_384/768/1024.chash was "
                        + "stuck BYTEA after the rollback above (vectors-004's rollback recreates "
                        + "it BYTEA; rdr180-3/4/5's rollback was a no-op), and catalog-006-4's "
                        + "TEXT-chash function body failed to (re)create with \"operator does not "
                        + "exist: text = bytea\" when Liquibase replayed it forward at its master "
                        + "position")
                    .doesNotThrowAnyException();

                // Re-check catalog-002's own 2 columns again here (not
                // assertForwardRoundTrip's full 14-column arc check): this
                // rollback depth also passes through catalog-020-1
                // (catalog-020-index-run-fence.xml), whose rollback is an
                // UNCONDITIONAL `DROP COLUMN IF EXISTS index_started_at` —
                // by design, not a data restore (see this test's own
                // comment above stage 1's depthToArc, and stage 2's opening
                // comment on why stage 1/2 are kept separate). A full
                // migrate() from here legitimately CANNOT bring
                // index_started_at's seeded value back (catalog-020-1
                // forward re-ADDs the column NULL, no data), so re-running
                // assertForwardRoundTrip here would fail on a known,
                // already-documented, unrelated data-loss-by-design fact —
                // not a regression. catalog-002's own columns carry no such
                // hazard (nothing between catalog-002-1 and HEAD drops or
                // re-adds catalog_collections.created_at/superseded_at), so
                // re-asserting them here is a genuine, stronger proof that
                // catalog-002-1's fidelity survives the FULL remaining
                // chain, not just the targeted count=1 update above.
                try (Connection su = pg.createConnection("")) {
                    assertCatalog002ForwardRoundTrip(su);
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── Fixtures ──────────────────────────────────────────────────────────────

    private static final String FIXTURE_TENANT = "cck6z-rollback-fidelity";

    // Zero-microsecond and nonzero-microsecond instants, all with an explicit
    // +00:00 offset so to_char's literal "+00:00" suffix (catalog-031's rollback
    // format never computes an offset field — it is a quoted literal) matches
    // regardless of the container's session timezone.
    private static final OffsetDateTime TS_ZERO_MICROS =
        OffsetDateTime.parse("2026-03-04T05:06:07+00:00");
    private static final OffsetDateTime TS_NONZERO_MICROS_A =
        OffsetDateTime.parse("2026-03-04T05:06:07.123456+00:00");
    private static final OffsetDateTime TS_NONZERO_MICROS_B =
        OffsetDateTime.parse("2026-05-06T07:08:09.654321+00:00");
    private static final OffsetDateTime TS_LINKS_ZERO_MICROS =
        OffsetDateTime.parse("2026-07-08T09:10:11+00:00");

    // catalog-002-hygiene.xml (RDR-156 template, unmodified by the arc): its rollback
    // is a bare col::text cast, NOT the arc's fixed-width to_char — so, deliberately,
    // there is no EXPECTED_TEXT_* constant for these two; the expected TEXT is
    // computed live via defaultTimestamptzText (see the round-trip test's javadoc).
    private static final OffsetDateTime TS_COLLECTIONS_CREATED_AT =
        OffsetDateTime.parse("2026-09-10T11:12:13.987654+00:00");
    private static final OffsetDateTime TS_COLLECTIONS_SUPERSEDED_AT =
        OffsetDateTime.parse("2026-11-12T13:14:15.111222+00:00");

    private static final String EXPECTED_TEXT_ZERO_MICROS = "2026-03-04T05:06:07.000000+00:00";
    private static final String EXPECTED_TEXT_NONZERO_MICROS_A = "2026-03-04T05:06:07.123456+00:00";
    private static final String EXPECTED_TEXT_NONZERO_MICROS_B = "2026-05-06T07:08:09.654321+00:00";
    private static final String EXPECTED_TEXT_LINKS_ZERO_MICROS = "2026-07-08T09:10:11.000000+00:00";

    private static final String HOOK_BATCH_DOC_IDS_JSON = "[\"doc-a\",\"doc-b\"]";
    private static final String ASPECTS_EXTRAS_JSON = "{\"alpha\":1,\"zeta\":2}";
    private static final String ASPECTS_SALIENT_JSON = "[\"first sentence\",\"second sentence\"]";
    private static final String PLAN_JSON_A =
        "{\"steps\":[{\"op\":\"search\"}],\"meta\":{\"alpha\":1,\"zeta\":2}}";
    private static final String PLAN_JSON_B = "{\"steps\":[]}";
    private static final String PLAN_DEFAULT_BINDINGS_B = "{\"alpha\":1,\"zeta\":2}";
    private static final String TOPIC_LINK_TYPES_JSON = "[\"cites\",\"implements\"]";

    /**
     * Seeds two representative rows per changeset family (except topic_links,
     * which needs a {@code topics} FK parent first — see
     * {@link #seedTopicLinksFixture}), via the real Postgres superuser
     * connection so RLS (FORCE or otherwise) never enters into it — this test
     * is about type conversions, not access control.
     *
     * <p>Each pair deliberately covers BOTH branches of every column's
     * rollback: a populated value (proving the round-trip content is exact)
     * and, for every nullable column, an explicit NULL (proving the '' vs
     * NULL sentinel handling documented per-column in each changeset's
     * header — some COALESCE to '', some do not, and the two must not be
     * confused).
     */
    private static void seedTypeHygieneFixtures(Connection su) throws Exception {
        su.setAutoCommit(true);

        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_documents "
                    + "(tenant_id, tumbler, title, indexed_at, bib_enriched_at, index_started_at) "
                    + "VALUES (?, ?, ?, ?, ?, ?)")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z.doc.1");
            ps.setString(3, "cck6z doc: zero-micros indexed_at, never bib-enriched");
            ps.setObject(4, TS_ZERO_MICROS);
            ps.setNull(5, Types.TIMESTAMP_WITH_TIMEZONE);
            ps.setObject(6, TS_NONZERO_MICROS_A);
            ps.executeUpdate();

            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z.doc.2");
            ps.setString(3, "cck6z doc: never indexed, nonzero-micros bib_enriched_at");
            ps.setNull(4, Types.TIMESTAMP_WITH_TIMEZONE);
            ps.setObject(5, TS_NONZERO_MICROS_B);
            ps.setNull(6, Types.TIMESTAMP_WITH_TIMEZONE);
            ps.executeUpdate();
        }

        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_links "
                    + "(tenant_id, from_tumbler, to_tumbler, link_type, created_by, created_at) "
                    + "VALUES (?, ?, ?, ?, ?, ?)")) {
            // hygiene-001-7 (nexus-tk070.p6a follow-on) SUPERSEDES this row's
            // original NULL-created_at seed: catalog_links.created_at is
            // NOT NULL now, so the "NULL survives a catalog-031-2 rollback,
            // does not get COALESCEd to ''" scenario this row used to probe
            // is permanently unreachable (a real timestamp is required at
            // seed time, and stays required through the whole test's
            // migrate/rollback/re-migrate cycle). Seeded with a real
            // zero-microsecond value instead -- the round-trip assertions
            // below now check ITS fidelity through rollback/re-migrate,
            // same mechanism the second row (cck6z.doc.2/cites-back)
            // already exercised.
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z.doc.1");
            ps.setString(3, "cck6z.doc.2");
            ps.setString(4, "cites");
            ps.setString(5, "cck6z-test");
            ps.setObject(6, TS_ZERO_MICROS);
            ps.executeUpdate();

            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z.doc.2");
            ps.setString(3, "cck6z.doc.1");
            ps.setString(4, "cites-back");
            ps.setString(5, "cck6z-test");
            ps.setObject(6, TS_LINKS_ZERO_MICROS);
            ps.executeUpdate();
        }

        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_collections "
                    + "(tenant_id, name, legacy_grandfathered, created_at, superseded_at) "
                    + "VALUES (?, ?, ?, ?, ?)")) {
            // catalog-002-1-temporal-typing (RDR-156 template, nexus-70r3c.2): created_at
            // populated, nonzero micros; superseded_at NULL — the common case (a
            // collection that has never been superseded).
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-legacy-true");
            ps.setBoolean(3, true);
            ps.setObject(4, TS_COLLECTIONS_CREATED_AT);
            ps.setNull(5, Types.TIMESTAMP_WITH_TIMEZONE);
            ps.executeUpdate();

            // catalog-002-1-temporal-typing: created_at NULL (never set), superseded_at
            // populated, nonzero micros — the complementary branch.
            // hygiene-001-6 (nexus-tk070.p6a follow-on) SUPERSEDES this row's
            // original NULL-created_at seed: catalog_collections.created_at is
            // NOT NULL now (backfilled + DEFAULT now()), so "a NULL created_at
            // COALESCEs to '' through the catalog-002-1 rollback" is
            // permanently unreachable. Seeded with a real zero-microsecond
            // value instead; the assertions below now check ITS round-trip
            // fidelity through the SAME rollback path.
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-legacy-false");
            ps.setBoolean(3, false);
            ps.setObject(4, TS_ZERO_MICROS);
            ps.setObject(5, TS_COLLECTIONS_SUPERSEDED_AT);
            ps.executeUpdate();

            // fk-003-1 / fk-003-3 (nexus-dcqml): document_aspects.collection and
            // topics.collection both FK to catalog_collections(tenant_id, name) —
            // register the collection those two families' fixtures below use.
            // Neither created_at/superseded_at value is asserted for this row.
            // hygiene-001-6: created_at is NOT NULL now -- a real value is
            // required even though nothing asserts on it.
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-coll");
            ps.setBoolean(3, false);
            ps.setObject(4, TS_ZERO_MICROS);
            ps.setNull(5, Types.TIMESTAMP_WITH_TIMEZONE);
            ps.executeUpdate();
        }

        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.hook_failures (tenant_id, hook_name, is_batch, batch_doc_ids) "
                    + "VALUES (?, ?, ?, ?::jsonb)")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-hook-a");
            ps.setBoolean(3, true);
            ps.setString(4, HOOK_BATCH_DOC_IDS_JSON);
            ps.executeUpdate();

            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-hook-b");
            ps.setBoolean(3, false);
            ps.setString(4, null);
            ps.executeUpdate();
        }

        // hygiene-001-1 (nexus-tk070.p6a follow-on): document_aspects.doc_id
        // and .source_uri are NOT NULL now (reverses fk-001-2's nullable
        // conversion) -- neither field was set by this fixture originally
        // (it exists to probe extras/salient_sentences jsonb round-trip,
        // unrelated to doc_id/source_uri), so it needs a real catalog
        // document to attribute to now.
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
                    + "VALUES (?, ?, ?) ON CONFLICT (tenant_id, tumbler) DO NOTHING")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z.aspects.doc");
            ps.setString(3, "cck6z aspects fixture doc");
            ps.executeUpdate();
        }
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.document_aspects "
                    + "(tenant_id, collection, source_path, extracted_at, model_version, "
                    + " extractor_name, extras, salient_sentences, source_uri, doc_id) "
                    + "VALUES (?, ?, ?, now(), ?, ?, ?::jsonb, ?::jsonb, ?, ?)")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-coll");
            ps.setString(3, "cck6z/doc1");
            ps.setString(4, "v1");
            ps.setString(5, "cck6z-extractor");
            ps.setString(6, ASPECTS_EXTRAS_JSON);
            ps.setString(7, null);
            ps.setString(8, "file:///cck6z/doc1");
            ps.setString(9, "cck6z.aspects.doc");
            ps.executeUpdate();

            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-coll");
            ps.setString(3, "cck6z/doc2");
            ps.setString(4, "v1");
            ps.setString(5, "cck6z-extractor");
            ps.setString(6, null);
            ps.setString(7, ASPECTS_SALIENT_JSON);
            ps.setString(8, "file:///cck6z/doc2");
            ps.setString(9, "cck6z.aspects.doc");
            ps.executeUpdate();
        }

        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.aspect_promotion_log "
                    + "(tenant_id, field_name, sql_type, column_added, pruned, promoted_at) "
                    + "VALUES (?, ?, ?, ?, ?, now())")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z_field_a");
            ps.setString(3, "text");
            ps.setBoolean(4, true);
            ps.setBoolean(5, false);
            ps.executeUpdate();

            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z_field_b");
            ps.setString(3, "text");
            ps.setBoolean(4, false);
            ps.setBoolean(5, true);
            ps.executeUpdate();
        }

        // hygiene-001-11 (nexus-tk070.p6a follow-on): plans.verb is NOT NULL
        // now -- this fixture exists to probe plan_json/default_bindings
        // jsonb round-trip, unrelated to verb, so a real verb is required.
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.plans "
                    + "(tenant_id, project, query, plan_json, created_at, default_bindings, verb) "
                    + "VALUES (?, ?, ?, ?::jsonb, now(), ?::jsonb, 'research')")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-proj");
            ps.setString(3, "cck6z plan query 1");
            ps.setString(4, PLAN_JSON_A);
            ps.setString(5, null);
            ps.executeUpdate();

            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-proj");
            ps.setString(3, "cck6z plan query 2");
            ps.setString(4, PLAN_JSON_B);
            ps.setString(5, PLAN_DEFAULT_BINDINGS_B);
            ps.executeUpdate();
        }
    }

    /** Seeds two {@code topics} parents (the FK topic_links requires) plus one link row. */
    private static long[] seedTopicLinksFixture(Connection su) throws Exception {
        long topicA;
        long topicB;
        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) "
                    + "VALUES (?, ?, ?, now()) RETURNING id")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-topic-a");
            ps.setString(3, "cck6z-coll");
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                topicA = rs.getLong(1);
            }

            ps.setString(1, FIXTURE_TENANT);
            ps.setString(2, "cck6z-topic-b");
            ps.setString(3, "cck6z-coll");
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                topicB = rs.getLong(1);
            }
        }

        try (PreparedStatement ps = su.prepareStatement(
                "INSERT INTO nexus.topic_links (tenant_id, from_topic_id, to_topic_id, link_types) "
                    + "VALUES (?, ?, ?, ?::jsonb)")) {
            ps.setString(1, FIXTURE_TENANT);
            ps.setLong(2, topicA);
            ps.setLong(3, topicB);
            ps.setString(4, TOPIC_LINK_TYPES_JSON);
            ps.executeUpdate();
        }
        return new long[] {topicA, topicB};
    }

    /** How many trailing (by execution order) DATABASECHANGELOG rows reach {@code changesetId}. */
    private static int rollbackDepthThrough(Connection c, String changesetId) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT count(*) FROM databasechangelog WHERE orderexecuted >= "
                    + "(SELECT orderexecuted FROM databasechangelog WHERE id = ?)")) {
            ps.setString(1, changesetId);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getInt(1);
            }
        }
    }

    /**
     * Live Postgres oracle for a jsonb literal's canonical TEXT rendering — never a
     * hand-typed guess (see class-level javadoc on the round-trip test above).
     */
    private static String canonicalJsonbText(Connection c, String jsonLiteral) throws Exception {
        try (PreparedStatement ps = c.prepareStatement("SELECT (?::jsonb)::text")) {
            ps.setString(1, jsonLiteral);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getString(1);
            }
        }
    }

    /**
     * Live Postgres oracle for a timestamptz's DEFAULT {@code ::text} rendering — the
     * shape catalog-002-hygiene.xml's rollback produces (a bare {@code col::text}
     * cast), which is DIFFERENT from the arc's fixed-width
     * {@code to_char(...,'.US"+00:00"')} format: trailing fractional zeros are
     * trimmed (a zero-microsecond instant has no fractional part at all) and the
     * offset renders as a bare {@code +00}, not {@code +00:00}. Computed live,
     * never hand-typed, for the same reason as {@link #canonicalJsonbText}.
     */
    private static String defaultTimestamptzText(Connection c, OffsetDateTime instant) throws Exception {
        try (PreparedStatement ps = c.prepareStatement("SELECT (?::timestamptz)::text")) {
            ps.setObject(1, instant);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getString(1);
            }
        }
    }

    private static void bind(PreparedStatement ps, Object... params) throws Exception {
        for (int i = 0; i < params.length; i++) {
            ps.setObject(i + 1, params[i]);
        }
    }

    private static String queryOneNullableString(Connection c, String sql, Object... params)
            throws Exception {
        try (PreparedStatement ps = c.prepareStatement(sql)) {
            bind(ps, params);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("expected exactly one row: " + sql).isTrue();
                return rs.getString(1);
            }
        }
    }

    private static int queryOneInt(Connection c, String sql, Object... params) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(sql)) {
            bind(ps, params);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("expected exactly one row: " + sql).isTrue();
                return rs.getInt(1);
            }
        }
    }

    private static Boolean queryOneNullableBoolean(Connection c, String sql, Object... params)
            throws Exception {
        try (PreparedStatement ps = c.prepareStatement(sql)) {
            bind(ps, params);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("expected exactly one row: " + sql).isTrue();
                boolean v = rs.getBoolean(1);
                return rs.wasNull() ? null : v;
            }
        }
    }

    private static void assertNullColumn(Connection c, String sql, Object... params) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(sql)) {
            bind(ps, params);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("expected exactly one row: " + sql).isTrue();
                rs.getObject(1);
                assertThat(rs.wasNull()).as("expected NULL: " + sql).isTrue();
            }
        }
    }

    private static void assertTimestampEquals(Connection c, String sql, OffsetDateTime expected,
            Object... params) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(sql)) {
            bind(ps, params);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("expected exactly one row: " + sql).isTrue();
                OffsetDateTime actual = rs.getObject(1, OffsetDateTime.class);
                assertThat(actual).as("expected instant %s: %s", expected, sql).isNotNull();
                assertThat(actual.toInstant())
                    .as("expected instant %s: %s", expected, sql)
                    .isEqualTo(expected.toInstant());
            }
        }
    }

    /** information_schema shape check post-rollback: TEXT/INTEGER, NOT NULL/DEFAULT restored. */
    private static void assertColumnRestoredShape(Connection c, String table, String column,
            String expectedDataType, boolean expectNullable, boolean expectDefault) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                    + "WHERE table_schema='nexus' AND table_name=? AND column_name=?")) {
            ps.setString(1, table);
            ps.setString(2, column);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next())
                    .as("nexus.%s.%s must exist after rollback", table, column).isTrue();
                assertThat(rs.getString("data_type"))
                    .as("nexus.%s.%s data_type after rollback", table, column)
                    .isEqualTo(expectedDataType);
                assertThat(rs.getString("is_nullable"))
                    .as("nexus.%s.%s is_nullable after rollback", table, column)
                    .isEqualTo(expectNullable ? "YES" : "NO");
                String columnDefault = rs.getString("column_default");
                if (expectDefault) {
                    assertThat(columnDefault)
                        .as("nexus.%s.%s must have its pre-migration DEFAULT restored — a "
                            + "rollback that DROPped DEFAULT but forgot to SET it again leaves "
                            + "this NULL", table, column)
                        .isNotNull();
                } else {
                    assertThat(columnDefault)
                        .as("nexus.%s.%s must have NO default (it never carried one "
                            + "pre-migration) — a stray SET DEFAULT here is itself a bug",
                            table, column)
                        .isNull();
                }
            }
        }
    }

    /** information_schema shape check post-reapply: jsonb/boolean/timestamptz restored. */
    private static void assertColumnForwardShape(Connection c, String table, String column,
            String expectedDataType) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT data_type FROM information_schema.columns "
                    + "WHERE table_schema='nexus' AND table_name=? AND column_name=?")) {
            ps.setString(1, table);
            ps.setString(2, column);
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next())
                    .as("nexus.%s.%s must exist after forward re-apply", table, column).isTrue();
                assertThat(rs.getString("data_type"))
                    .as("nexus.%s.%s data_type after forward re-apply", table, column)
                    .isEqualTo(expectedDataType);
            }
        }
    }

    /**
     * Asserts the raw TEXT/INTEGER values the rollback actually produced, one block per
     * changeset family, plus the NOT NULL/DEFAULT restoration for all 14 columns.
     */
    private static void assertRolledBackColumnShapesAndValues(Connection su, long topicA, long topicB)
            throws Exception {
        // ── catalog-031-1: catalog_documents.indexed_at / bib_enriched_at / index_started_at ──
        assertThat(queryOneNullableString(su,
            "SELECT indexed_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.1"))
            .as("catalog-031-1 indexed_at rollback: to_char(indexed_at AT TIME ZONE 'UTC', "
                + "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'), NO COALESCE — a zero-microsecond "
                + "instant must render with 6 zero digits; swapping the 'US' token for 'MS' "
                + "(millis) or dropping the zero-pad would turn this red")
            .isEqualTo(EXPECTED_TEXT_ZERO_MICROS);
        assertThat(queryOneNullableString(su,
            "SELECT indexed_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.2"))
            .as("catalog-031-1 indexed_at rollback has NO COALESCE(...,'') — unlike "
                + "bib_enriched_at/index_started_at below, a NULL indexed_at must STAY NULL "
                + "after rollback, not become ''. Adding a COALESCE here would turn this red")
            .isNull();

        assertThat(queryOneNullableString(su,
            "SELECT bib_enriched_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.1"))
            .as("catalog-031-1 bib_enriched_at rollback: COALESCE(to_char(...),'') plus SET NOT "
                + "NULL SET DEFAULT '' — a NULL bib_enriched_at (the 99.86%%-of-rows case per "
                + "this changeset's own header) must restore to '' exactly, not NULL. Dropping "
                + "the COALESCE would turn this red")
            .isEqualTo("");
        assertThat(queryOneNullableString(su,
            "SELECT bib_enriched_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.2"))
            .as("catalog-031-1 bib_enriched_at rollback: a populated value must round-trip "
                + "exactly, including nonzero microseconds")
            .isEqualTo(EXPECTED_TEXT_NONZERO_MICROS_B);

        assertThat(queryOneNullableString(su,
            "SELECT index_started_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.1"))
            .as("catalog-031-1 index_started_at rollback: same COALESCE-to-'' shape as "
                + "bib_enriched_at; a populated nonzero-microsecond value must round-trip exactly")
            .isEqualTo(EXPECTED_TEXT_NONZERO_MICROS_A);
        assertThat(queryOneNullableString(su,
            "SELECT index_started_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.2"))
            .as("catalog-031-1 index_started_at rollback: NULL must COALESCE to '' (this column "
                + "had NOT NULL DEFAULT '' before the arc) — dropping the COALESCE would leave "
                + "this NULL instead of ''")
            .isEqualTo("");

        // ── catalog-031-2: catalog_links.created_at ──
        // hygiene-001-7 SUPERSEDES the original nexus-cck6z NULL-created_at
        // FINDING-CHECK here (see seedTypeHygieneFixtures's comment on this
        // row): created_at is NOT NULL now, so this now checks the same
        // fixed-width zero-microsecond rollback fidelity the second row
        // (cck6z.doc.2/cites-back) already covers.
        assertThat(queryOneNullableString(su,
            "SELECT created_at FROM nexus.catalog_links "
                + "WHERE tenant_id=? AND from_tumbler=? AND link_type=?",
            FIXTURE_TENANT, "cck6z.doc.1", "cites"))
            .as("catalog-031-2 created_at rollback: a populated zero-microsecond value must "
                + "render with 6 zero digits")
            .isEqualTo(EXPECTED_TEXT_ZERO_MICROS);
        assertThat(queryOneNullableString(su,
            "SELECT created_at FROM nexus.catalog_links "
                + "WHERE tenant_id=? AND from_tumbler=? AND link_type=?",
            FIXTURE_TENANT, "cck6z.doc.2", "cites-back"))
            .as("catalog-031-2 created_at rollback: a populated zero-microsecond value must "
                + "render with 6 zero digits, same format as catalog-031-1")
            .isEqualTo(EXPECTED_TEXT_LINKS_ZERO_MICROS);

        // ── catalog-031-3: catalog_collections.legacy_grandfathered ──
        assertThat(queryOneInt(su,
            "SELECT legacy_grandfathered FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-true"))
            .as("catalog-031-3 rollback: CASE WHEN legacy_grandfathered THEN 1 ELSE 0 END — true "
                + "must restore to exactly 1")
            .isEqualTo(1);
        assertThat(queryOneInt(su,
            "SELECT legacy_grandfathered FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-false"))
            .as("catalog-031-3 rollback: false must restore to exactly 0")
            .isEqualTo(0);

        // ── telemetry-004-1: hook_failures.is_batch / .batch_doc_ids ──
        assertThat(queryOneInt(su,
            "SELECT is_batch FROM nexus.hook_failures WHERE tenant_id=? AND hook_name=?",
            FIXTURE_TENANT, "cck6z-hook-a"))
            .as("telemetry-004-1 is_batch rollback: CASE WHEN ... THEN 1 ELSE 0 END — true -> 1")
            .isEqualTo(1);
        assertThat(queryOneInt(su,
            "SELECT is_batch FROM nexus.hook_failures WHERE tenant_id=? AND hook_name=?",
            FIXTURE_TENANT, "cck6z-hook-b"))
            .as("telemetry-004-1 is_batch rollback: false -> 0")
            .isEqualTo(0);
        String expectedBatchDocIds = canonicalJsonbText(su, HOOK_BATCH_DOC_IDS_JSON);
        assertThat(queryOneNullableString(su,
            "SELECT batch_doc_ids FROM nexus.hook_failures WHERE tenant_id=? AND hook_name=?",
            FIXTURE_TENANT, "cck6z-hook-a"))
            .as("telemetry-004-1 batch_doc_ids rollback: plain USING batch_doc_ids::text (no "
                + "COALESCE — nullable, no-default column pre-migration) — the restored TEXT "
                + "must equal the ORIGINAL array's canonical jsonb text, computed live via a "
                + "scratch ::jsonb::text cast rather than hand-typed. Casting the wrong column, "
                + "or substituting a hardcoded literal for batch_doc_ids::text, would turn this red")
            .isEqualTo(expectedBatchDocIds);
        assertThat(queryOneNullableString(su,
            "SELECT batch_doc_ids FROM nexus.hook_failures WHERE tenant_id=? AND hook_name=?",
            FIXTURE_TENANT, "cck6z-hook-b"))
            .as("telemetry-004-1 batch_doc_ids rollback: jsonb NULL must cast back to TEXT NULL, "
                + "never ''")
            .isNull();

        // ── aspects-003-1: document_aspects.extras / .salient_sentences ──
        String expectedExtras = canonicalJsonbText(su, ASPECTS_EXTRAS_JSON);
        assertThat(queryOneNullableString(su,
            "SELECT extras FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            FIXTURE_TENANT, "cck6z-coll", "cck6z/doc1"))
            .as("aspects-003-1 extras rollback: plain USING extras::text — restored TEXT must "
                + "equal the original object's canonical jsonb text (key reorder + whitespace "
                + "verified live, not guessed)")
            .isEqualTo(expectedExtras);
        assertThat(queryOneNullableString(su,
            "SELECT salient_sentences FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            FIXTURE_TENANT, "cck6z-coll", "cck6z/doc1"))
            .as("aspects-003-1 salient_sentences rollback: jsonb NULL -> TEXT NULL, never ''")
            .isNull();
        assertThat(queryOneNullableString(su,
            "SELECT extras FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            FIXTURE_TENANT, "cck6z-coll", "cck6z/doc2"))
            .as("aspects-003-1 extras rollback: jsonb NULL -> TEXT NULL, never ''")
            .isNull();
        String expectedSalient = canonicalJsonbText(su, ASPECTS_SALIENT_JSON);
        assertThat(queryOneNullableString(su,
            "SELECT salient_sentences FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            FIXTURE_TENANT, "cck6z-coll", "cck6z/doc2"))
            .as("aspects-003-1 salient_sentences rollback: array element ORDER must survive — "
                + "jsonb does not reorder array elements the way it reorders object keys")
            .isEqualTo(expectedSalient);

        // ── aspects-003-2: aspect_promotion_log.column_added / .pruned ──
        assertThat(queryOneInt(su,
            "SELECT column_added FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_a"))
            .as("aspects-003-2 column_added rollback: true -> 1").isEqualTo(1);
        assertThat(queryOneInt(su,
            "SELECT pruned FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_a"))
            .as("aspects-003-2 pruned rollback: false -> 0").isEqualTo(0);
        assertThat(queryOneInt(su,
            "SELECT column_added FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_b"))
            .as("aspects-003-2 column_added rollback: false -> 0").isEqualTo(0);
        assertThat(queryOneInt(su,
            "SELECT pruned FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_b"))
            .as("aspects-003-2 pruned rollback: true -> 1").isEqualTo(1);

        // ── plans-002-1: plans.plan_json / .default_bindings ──
        String expectedPlanJsonA = canonicalJsonbText(su, PLAN_JSON_A);
        assertThat(queryOneNullableString(su,
            "SELECT plan_json FROM nexus.plans WHERE tenant_id=? AND project=? AND query=?",
            FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 1"))
            .as("plans-002-1 plan_json rollback: plain USING plan_json::text — nested-object "
                + "canonicalization (key reorder inside 'meta') must survive")
            .isEqualTo(expectedPlanJsonA);
        assertThat(queryOneNullableString(su,
            "SELECT default_bindings FROM nexus.plans WHERE tenant_id=? AND project=? AND query=?",
            FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 1"))
            .as("plans-002-1 default_bindings rollback: jsonb NULL -> TEXT NULL, never ''")
            .isNull();
        String expectedPlanJsonB = canonicalJsonbText(su, PLAN_JSON_B);
        assertThat(queryOneNullableString(su,
            "SELECT plan_json FROM nexus.plans WHERE tenant_id=? AND project=? AND query=?",
            FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 2"))
            .as("plans-002-1 plan_json rollback: minimal object must also round-trip exactly")
            .isEqualTo(expectedPlanJsonB);
        String expectedDefaultBindingsB = canonicalJsonbText(su, PLAN_DEFAULT_BINDINGS_B);
        assertThat(queryOneNullableString(su,
            "SELECT default_bindings FROM nexus.plans WHERE tenant_id=? AND project=? AND query=?",
            FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 2"))
            .as("plans-002-1 default_bindings rollback: a populated value must equal the "
                + "canonical jsonb text of the original object")
            .isEqualTo(expectedDefaultBindingsB);

        // ── taxonomy-008-1: topic_links.link_types ──
        String expectedLinkTypes = canonicalJsonbText(su, TOPIC_LINK_TYPES_JSON);
        assertThat(queryOneNullableString(su,
            "SELECT link_types FROM nexus.topic_links "
                + "WHERE tenant_id=? AND from_topic_id=? AND to_topic_id=?",
            FIXTURE_TENANT, topicA, topicB))
            .as("taxonomy-008-1 link_types rollback: DROP DEFAULT -> TYPE TEXT USING "
                + "link_types::text -> SET DEFAULT '[]' — array element order must survive")
            .isEqualTo(expectedLinkTypes);

        // ── NOT NULL / DEFAULT restored (information_schema), all 14 columns ──
        assertColumnRestoredShape(su, "catalog_documents", "indexed_at", "text", true, false);
        assertColumnRestoredShape(su, "catalog_documents", "bib_enriched_at", "text", false, true);
        assertColumnRestoredShape(su, "catalog_documents", "index_started_at", "text", false, true);
        assertColumnRestoredShape(su, "catalog_links", "created_at", "text", true, false);
        assertColumnRestoredShape(su, "catalog_collections", "legacy_grandfathered", "integer", false, true);
        assertColumnRestoredShape(su, "hook_failures", "is_batch", "integer", false, true);
        assertColumnRestoredShape(su, "hook_failures", "batch_doc_ids", "text", true, false);
        assertColumnRestoredShape(su, "document_aspects", "extras", "text", true, false);
        assertColumnRestoredShape(su, "document_aspects", "salient_sentences", "text", true, false);
        assertColumnRestoredShape(su, "aspect_promotion_log", "column_added", "integer", false, true);
        assertColumnRestoredShape(su, "aspect_promotion_log", "pruned", "integer", false, true);
        assertColumnRestoredShape(su, "plans", "plan_json", "text", false, false);
        assertColumnRestoredShape(su, "plans", "default_bindings", "text", true, false);
        assertColumnRestoredShape(su, "topic_links", "link_types", "text", false, true);
    }

    /**
     * Asserts catalog-002-1-temporal-typing's (RDR-156 template) rollback for
     * catalog_collections.created_at / .superseded_at — called after STAGE 2 (the
     * deeper rollback), separately from {@link #assertRolledBackColumnShapesAndValues}
     * (STAGE 1), because these two columns are still timestamptz at the end of stage 1
     * (catalog-002 has not been rolled back yet).
     *
     * <p>A DIFFERENT rollback shape from the arc: {@code ALTER COLUMN ... TYPE TEXT
     * USING COALESCE(col::text, '')} plus {@code SET NOT NULL SET DEFAULT ''} for BOTH
     * columns (unlike catalog-031-1's indexed_at / catalog_links.created_at, which have
     * no {@code COALESCE} at all). The {@code col::text} cast itself is bare (no
     * {@code to_char}), so the expected TEXT is Postgres's DEFAULT timestamptz
     * rendering, computed live — see {@link #defaultTimestamptzText}.
     */
    private static void assertCatalog002ColumnShapesAndValues(Connection su) throws Exception {
        String expectedCollectionsCreatedAt = defaultTimestamptzText(su, TS_COLLECTIONS_CREATED_AT);
        assertThat(queryOneNullableString(su,
            "SELECT created_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-true"))
            .as("catalog-002-1 created_at rollback: COALESCE(created_at::text, '') — a populated "
                + "value must round-trip to Postgres's DEFAULT timestamptz-to-text rendering "
                + "(trailing-zero-trimmed microseconds, bare '+00' offset) — NOT the arc's "
                + "fixed-width to_char format used elsewhere in this test. Swapping the bare "
                + "col::text cast for a to_char(...) call would turn this red")
            .isEqualTo(expectedCollectionsCreatedAt);
        assertThat(queryOneNullableString(su,
            "SELECT superseded_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-true"))
            .as("catalog-002-1 superseded_at rollback: COALESCE(superseded_at::text, '') — a NULL "
                + "superseded_at (the common case: a collection never superseded) must restore to "
                + "'' exactly, not NULL. Dropping the COALESCE would turn this red")
            .isEqualTo("");

        String expectedCollectionsCreatedAtFalse = defaultTimestamptzText(su, TS_ZERO_MICROS);
        assertThat(queryOneNullableString(su,
            "SELECT created_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-false"))
            .as("catalog-002-1 created_at rollback: a populated value must round-trip to "
                + "Postgres's DEFAULT timestamptz-to-text rendering, same oracle as the "
                + "cck6z-legacy-true row above")
            .isEqualTo(expectedCollectionsCreatedAtFalse);
        String expectedCollectionsSupersededAt = defaultTimestamptzText(su, TS_COLLECTIONS_SUPERSEDED_AT);
        assertThat(queryOneNullableString(su,
            "SELECT superseded_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-false"))
            .as("catalog-002-1 superseded_at rollback: a populated value must round-trip to "
                + "Postgres's DEFAULT timestamptz-to-text rendering, same oracle as created_at above")
            .isEqualTo(expectedCollectionsSupersededAt);

        assertColumnRestoredShape(su, "catalog_collections", "created_at", "text", false, true);
        assertColumnRestoredShape(su, "catalog_collections", "superseded_at", "text", false, true);
    }

    /**
     * Asserts catalog-002-1-temporal-typing's targeted, count=1 forward re-apply (via
     * {@link #reapplyForward}) restored timestamptz for catalog_collections.created_at /
     * .superseded_at, and that the values round-tripped intact.
     */
    private static void assertCatalog002ForwardRoundTrip(Connection su) throws Exception {
        assertColumnForwardShape(su, "catalog_collections", "created_at", "timestamp with time zone");
        assertColumnForwardShape(su, "catalog_collections", "superseded_at", "timestamp with time zone");

        assertTimestampEquals(su,
            "SELECT created_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            TS_COLLECTIONS_CREATED_AT, FIXTURE_TENANT, "cck6z-legacy-true");
        assertNullColumn(su,
            "SELECT superseded_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-true");
        assertTimestampEquals(su,
            "SELECT created_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            TS_ZERO_MICROS, FIXTURE_TENANT, "cck6z-legacy-false");
        assertTimestampEquals(su,
            "SELECT superseded_at FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            TS_COLLECTIONS_SUPERSEDED_AT, FIXTURE_TENANT, "cck6z-legacy-false");
    }

    /**
     * Asserts the forward re-apply restored jsonb/boolean/timestamptz types for all 14
     * columns, and that every value this test seeded round-tripped intact.
     */
    private static void assertForwardRoundTrip(Connection su, long topicA, long topicB)
            throws Exception {
        assertColumnForwardShape(su, "catalog_documents", "indexed_at", "timestamp with time zone");
        assertColumnForwardShape(su, "catalog_documents", "bib_enriched_at", "timestamp with time zone");
        assertColumnForwardShape(su, "catalog_documents", "index_started_at", "timestamp with time zone");
        assertColumnForwardShape(su, "catalog_links", "created_at", "timestamp with time zone");
        assertColumnForwardShape(su, "catalog_collections", "legacy_grandfathered", "boolean");
        assertColumnForwardShape(su, "hook_failures", "is_batch", "boolean");
        assertColumnForwardShape(su, "hook_failures", "batch_doc_ids", "jsonb");
        assertColumnForwardShape(su, "document_aspects", "extras", "jsonb");
        assertColumnForwardShape(su, "document_aspects", "salient_sentences", "jsonb");
        assertColumnForwardShape(su, "aspect_promotion_log", "column_added", "boolean");
        assertColumnForwardShape(su, "aspect_promotion_log", "pruned", "boolean");
        assertColumnForwardShape(su, "plans", "plan_json", "jsonb");
        assertColumnForwardShape(su, "plans", "default_bindings", "jsonb");
        assertColumnForwardShape(su, "topic_links", "link_types", "jsonb");

        assertTimestampEquals(su,
            "SELECT indexed_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            TS_ZERO_MICROS, FIXTURE_TENANT, "cck6z.doc.1");
        assertNullColumn(su,
            "SELECT indexed_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.2");

        assertNullColumn(su,
            "SELECT bib_enriched_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.1");
        assertTimestampEquals(su,
            "SELECT bib_enriched_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            TS_NONZERO_MICROS_B, FIXTURE_TENANT, "cck6z.doc.2");

        assertTimestampEquals(su,
            "SELECT index_started_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            TS_NONZERO_MICROS_A, FIXTURE_TENANT, "cck6z.doc.1");
        assertNullColumn(su,
            "SELECT index_started_at FROM nexus.catalog_documents WHERE tenant_id=? AND tumbler=?",
            FIXTURE_TENANT, "cck6z.doc.2");

        assertTimestampEquals(su,
            "SELECT created_at FROM nexus.catalog_links "
                + "WHERE tenant_id=? AND from_tumbler=? AND link_type=?",
            TS_ZERO_MICROS, FIXTURE_TENANT, "cck6z.doc.1", "cites");
        assertTimestampEquals(su,
            "SELECT created_at FROM nexus.catalog_links "
                + "WHERE tenant_id=? AND from_tumbler=? AND link_type=?",
            TS_LINKS_ZERO_MICROS, FIXTURE_TENANT, "cck6z.doc.2", "cites-back");

        assertThat(queryOneNullableBoolean(su,
            "SELECT legacy_grandfathered FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-true")).isTrue();
        assertThat(queryOneNullableBoolean(su,
            "SELECT legacy_grandfathered FROM nexus.catalog_collections WHERE tenant_id=? AND name=?",
            FIXTURE_TENANT, "cck6z-legacy-false")).isFalse();

        assertThat(queryOneNullableBoolean(su,
            "SELECT is_batch FROM nexus.hook_failures WHERE tenant_id=? AND hook_name=?",
            FIXTURE_TENANT, "cck6z-hook-a")).isTrue();
        assertThat(queryOneNullableBoolean(su,
            "SELECT is_batch FROM nexus.hook_failures WHERE tenant_id=? AND hook_name=?",
            FIXTURE_TENANT, "cck6z-hook-b")).isFalse();
        assertThat(queryOneNullableBoolean(su,
            "SELECT (batch_doc_ids = ?::jsonb) FROM nexus.hook_failures "
                + "WHERE tenant_id=? AND hook_name=?",
            HOOK_BATCH_DOC_IDS_JSON, FIXTURE_TENANT, "cck6z-hook-a"))
            .as("hook_failures.batch_doc_ids must round-trip forward to the same JSON content")
            .isTrue();
        assertNullColumn(su,
            "SELECT batch_doc_ids FROM nexus.hook_failures WHERE tenant_id=? AND hook_name=?",
            FIXTURE_TENANT, "cck6z-hook-b");

        assertThat(queryOneNullableBoolean(su,
            "SELECT (extras = ?::jsonb) FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            ASPECTS_EXTRAS_JSON, FIXTURE_TENANT, "cck6z-coll", "cck6z/doc1"))
            .as("document_aspects.extras must round-trip forward to the same JSON content")
            .isTrue();
        assertNullColumn(su,
            "SELECT salient_sentences FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            FIXTURE_TENANT, "cck6z-coll", "cck6z/doc1");
        assertNullColumn(su,
            "SELECT extras FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            FIXTURE_TENANT, "cck6z-coll", "cck6z/doc2");
        assertThat(queryOneNullableBoolean(su,
            "SELECT (salient_sentences = ?::jsonb) FROM nexus.document_aspects "
                + "WHERE tenant_id=? AND collection=? AND source_path=?",
            ASPECTS_SALIENT_JSON, FIXTURE_TENANT, "cck6z-coll", "cck6z/doc2"))
            .as("document_aspects.salient_sentences must round-trip forward, array order intact")
            .isTrue();

        assertThat(queryOneNullableBoolean(su,
            "SELECT column_added FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_a")).isTrue();
        assertThat(queryOneNullableBoolean(su,
            "SELECT pruned FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_a")).isFalse();
        assertThat(queryOneNullableBoolean(su,
            "SELECT column_added FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_b")).isFalse();
        assertThat(queryOneNullableBoolean(su,
            "SELECT pruned FROM nexus.aspect_promotion_log WHERE tenant_id=? AND field_name=?",
            FIXTURE_TENANT, "cck6z_field_b")).isTrue();

        assertThat(queryOneNullableBoolean(su,
            "SELECT (plan_json = ?::jsonb) FROM nexus.plans "
                + "WHERE tenant_id=? AND project=? AND query=?",
            PLAN_JSON_A, FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 1"))
            .as("plans.plan_json must round-trip forward to the same JSON content").isTrue();
        assertNullColumn(su,
            "SELECT default_bindings FROM nexus.plans WHERE tenant_id=? AND project=? AND query=?",
            FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 1");
        assertThat(queryOneNullableBoolean(su,
            "SELECT (plan_json = ?::jsonb) FROM nexus.plans "
                + "WHERE tenant_id=? AND project=? AND query=?",
            PLAN_JSON_B, FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 2"))
            .isTrue();
        assertThat(queryOneNullableBoolean(su,
            "SELECT (default_bindings = ?::jsonb) FROM nexus.plans "
                + "WHERE tenant_id=? AND project=? AND query=?",
            PLAN_DEFAULT_BINDINGS_B, FIXTURE_TENANT, "cck6z-proj", "cck6z plan query 2"))
            .as("plans.default_bindings must round-trip forward to the same JSON content")
            .isTrue();

        assertThat(queryOneNullableBoolean(su,
            "SELECT (link_types = ?::jsonb) FROM nexus.topic_links "
                + "WHERE tenant_id=? AND from_topic_id=? AND to_topic_id=?",
            TOPIC_LINK_TYPES_JSON, FIXTURE_TENANT, topicA, topicB))
            .as("topic_links.link_types must round-trip forward, array order intact")
            .isTrue();
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

    /**
     * Re-applies FORWARD by count — the {@code update(int,...)} counterpart to
     * {@link #rollbackEverything}, applying exactly the next {@code changesToApply}
     * PENDING changesets in master order and stopping, rather than walking the full
     * remaining chain to HEAD (what {@code SchemaMigrator.migrate} always does).
     *
     * <p>Used by stage 2 of {@link #typeHygieneRollback_restoresExactDataAndRoundTripsForward}
     * to re-apply catalog-002-1-temporal-typing on its own, deliberately never reaching
     * catalog-006-4's territory — see that call site's KNOWN GAP note.
     */
    private static void reapplyForward(HikariDataSource ds, int changesToApply) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG_RELATIVE,
                    new ClassLoaderResourceAccessor(),
                    database)) {
                liquibase.update(changesToApply, new Contexts(), new LabelExpression());
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

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
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
        // nexus-hzhgl: mirrors pg_provision.py's bootstrap-only GRANT pg_monitor TO
        // nexus_admin WITH ADMIN OPTION -- required since grants-004-monitor-wal-
        // visibility (grants-nexus-svc.xml) grants pg_monitor onward to nexus_svc, and
        // PostgreSQL refuses that GRANT unless the migration role already holds
        // pg_monitor WITH ADMIN OPTION (or is superuser). See GrantsPgMonitorTest for
        // the falsification proof of this exact prerequisite.
        su.createStatement().execute("GRANT pg_monitor TO " + ADMIN_ROLE + " WITH ADMIN OPTION");
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
