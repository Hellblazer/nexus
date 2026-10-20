package dev.nexus.service;

import org.jooq.impl.DSL;
import org.jooq.SQLDialect;
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
import liquibase.resource.DirectoryResourceAccessor;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.ResultSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.ASPECT_EXTRACTION_QUEUE;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;
import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.PLANS;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TAXONOMY_CENTROIDS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_LINKS;
import static dev.nexus.service.jooq.nexus.Tables.TUPLES;
import static dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * nexus-4m6i0.6 (Tier 2 of the b6qlf/ms57z systemic migration-safety hardening,
 * nexus-4m6i0) — the ONE mechanism that would have caught nexus-ms57z (GH #1390)
 * before it reached production, and a generalized net for the migration-
 * divergence class. Two legs:
 *
 * <ul>
 *   <li>{@link #oldEngineChangelogTree_upgradesToHead_afterInjectedDivergence}
 *       — SCHEMA-shaped divergence (the verbatim ms57z missing-constraint
 *       shape) injected between the old leg and the HEAD leg.</li>
 *   <li>{@link #oldEngineChangelogTree_withLegacySeededRows_dataChangesetsActuallyExecute}
 *       — DATA-shaped state (nexus-u5dln, the nexus-1wjmq class): the old
 *       leg is seeded with legacy-shaped ROWS before the hop, so the HEAD
 *       leg's data-dependent changesets run against a populated, FORCE-RLS
 *       database exactly as they do on a real aged fleet box — where the
 *       Liquibase role (a NOBYPASSRLS owner, unlike Testcontainers' usual
 *       superuser) sees ZERO rows through every RLS policy.</li>
 * </ul>
 *
 * <p><strong>Why the existing harnesses cannot catch this class of bug.</strong>
 * {@code tests/e2e/migration-rehearsal/run.sh} builds a fresh DATABASECHANGELOG
 * on every run (COLD_TAG defaults to a single recent engine tag, then always
 * upgrades to HEAD from that one synthetic starting point). {@link
 * SchemaMigratorIntegrationTest}'s aged-box tests (5/6/7) inject a divergence by
 * partially applying the <em>current</em> (HEAD) changelog tree with a
 * changeset-count limit, then resuming — a real and valuable regression test for
 * the nexus-4m6i0.1 fix, but it never touches an actual historical changelog
 * <em>tree</em>. Neither harness proves that Liquibase can walk from the literal
 * changelog files an old, currently-deployed engine tag shipped with, through
 * every intermediate revision, to the current master changelog — which is
 * exactly the upgrade a real fleet performs. And {@link Catalog013RlsReplayTest}
 * replays the v0.1.33 incident wall against the HEAD changelog only — it proves
 * the catalog-013-1b fix, not the old-tag-tree hop.
 *
 * <p><strong>What these tests do differently.</strong> They materialize the
 * {@code service/src/main/resources/db/changelog} tree exactly as it existed at
 * {@link #OLD_TAG} (via {@code git archive}, not the classpath), drive {@code
 * liquibase.Liquibase} directly against that historical tree with a filesystem
 * {@link DirectoryResourceAccessor} — running as a production-shaped
 * NOSUPERUSER/NOBYPASSRLS owner role, never the superuser — inject the
 * divergence (schema drop or legacy-shaped rows), and only then call the REAL
 * production entry point ({@link SchemaMigrator#migrate}) — which resolves
 * against the classpath (HEAD) master changelog — to complete the upgrade.
 *
 * <p><strong>{@link #OLD_TAG} choice.</strong> {@code engine-service-v0.1.17} is
 * the pinned old reference tag (see the field javadoc on {@link #OLD_TAG})
 * and its changelog tree predates {@code catalog-013-chash-checks-validate.xml}
 * entirely (first appears at v0.1.33+, introduced by commit {@code e1cd25f1}) —
 * confirmed by {@code git ls-tree engine-service-v0.1.17 -- .../db/changelog},
 * which lists only up to {@code catalog-011-collection-health-stale-age.xml}.
 * {@code catalog-002-hygiene.xml} (which unconditionally ADDs the five
 * chash-length CHECK constraints, the root of the ms57z divergence per
 * nexus-4m6i0.1's analysis) is identical between v0.1.17 and HEAD apart from a
 * checksum-neutral DATA EFFECT comment (commit {@code 729c61d5c}), so the
 * old leg reaches the same constraint-bearing state the real fleet did. For the
 * data leg, the v0.1.17-to-HEAD hop keeps growing (originally six changelog
 * files — catalog-012/-013/-014, migration-001, service-tokens-003/-004 —
 * verified via {@code git ls-tree} diff at the time this leg was first
 * written; RDR-191 Phase 4's vectors-004-1/taxonomy-007-1 are the latest
 * additions). The subset that genuinely carries migration-time row-DML
 * against FORCE-RLS tables is NOT restated here as a count (a stale number
 * is worse than none) — the data leg's own SEED-COVERAGE contract block is
 * the source of truth (the Python seed-coverage lint that once enforced it
 * was deleted at cleanup step 10b). (taxonomy-004's
 * root-topic dedup is already IN the v0.1.17 tree — the old leg applies it
 * and its unique root-topic index, so duplicate-root seeding is neither
 * possible nor a real fleet exposure.)
 *
 * <p><strong>RED before nexus-4m6i0.1 / GREEN after.</strong> nexus-4m6i0.1 is
 * already merged (commit 1ac12e1f) with its own RED-then-GREEN regression proof
 * (Test 5/6 in {@link SchemaMigratorIntegrationTest}, git-stash verified). Per
 * the nexus-4m6i0.6 design note, re-deriving that RED state here (e.g. by
 * pointing the HEAD leg at the pre-fix commit) would duplicate that proof
 * without adding coverage; this test's unique value is the old-tag-changelog-
 * tree hop itself, which no other test exercises. It remains GREEN only because
 * the guard already landed — if catalog-013-2's precondition regressed, this
 * test would fail exactly like Test 5/6 do. The data leg is the same posture:
 * it is GREEN only because catalog-013-1b's and catalog-014-0's FORCE toggles
 * already landed — remove either toggle and the leg goes RED (013-2's VALIDATE
 * fails on the un-normalized rows / the stamp assertion fails on NULL), which
 * is precisely the v0.1.33-incident mechanism it pins on the real old-tag hop.
 * Both RED paths were verified by temporarily neutering each toggle
 * (2026-07-10, nexus-u5dln): the 013-1b neuter reproduces the incident's
 * VALIDATE failure verbatim; the 014-0 neuter is the nastier shape — the
 * migration COMPLETES with the stamp silently un-applied, and only this leg's
 * effect assertion catches it. Later changesets (catalog-025-0's NULL-row
 * delete, hygiene-001-5/-8's legacy-width delete) came to erase the 014-0
 * evidence before HEAD, and the 014-0 neuter went GREEN (measured 2026-10-18,
 * nexus-iu7hp); its assertion now runs at a partial-walk checkpoint just after
 * catalog-014-0.
 *
 * <p><strong>Tag availability.</strong> {@code git archive} needs {@link
 * #OLD_TAG} present locally. The test attempts a shallow {@code git fetch
 * --depth 1 origin tag} fallback before giving up; if the tag is still
 * unavailable (e.g. a checkout that skipped tag-fetching entirely) the test
 * SKIPS with a loud reason via {@link Assumptions#abort} rather than silently
 * passing. {@code service-ci.yml} sets {@code fetch-tags: false} and fetches exactly this
 * tag in its own step (read from {@link #OLD_TAG} below), so this skip path is never
 * exercised in CI.
 */
class SchemaUpgradeRehearsalIntegrationTest {


    private static final Logger log = LoggerFactory.getLogger(SchemaUpgradeRehearsalIntegrationTest.class);

    /**
     * Old reference tag: predates {@code catalog-013-chash-checks-validate.xml}
     * (first shipped v0.1.33+, commit e1cd25f1) AND {@code catalog-014-manifest-collection-stamp.xml}
     * so the divergence-injection point (catalog-002-hygiene.xml, identical old
     * vs HEAD apart from a checksum-neutral comment) is reached the same way a
     * real aged fleet box reached it, and the hop's row-DML changesets genuinely run.
     *
     * <p><strong>PINNED, never rotated (Sam 2026-10-17, nexus-dodjd).</strong>
     * Sam's decision: keep this tag as a long real upgrade path for a dormant
     * install. A box last upgraded at v0.1.17 walks every changeset shipped
     * since, so this hop exercises all of them against an old-shaped database.
     * (Older tags exist: v0.1.10 through v0.1.17 share one changelog tree, and
     * tags before v0.1.10 have fewer changelog files. Moving earlier is a new
     * decision, not a rotation.) The former nexus-7z6s7 rotation
     * policy (advance to the previously-deployed tag at each deploy) is
     * retired for this test. It could never fire anyway: every tag from
     * {@code engine-service-v0.1.33} onward contains {@code
     * catalog-013-chash-checks-validate.xml} (v0.1.32 lacks it, v0.1.33 has
     * it; catalog-014 follows at v0.1.35), so a rotated tag fails the schema
     * leg's {@code changesetApplied(..., "catalog-013-2", ...).isFalse()}
     * precondition and empties the data leg's row-DML coverage. Rotation
     * checks on 2026-09-13 (v0.1.117), 2026-09-14 (v0.1.119), 2026-09-18
     * (v0.1.128) and 2026-10-07 (v0.1.149) all reached that result. The
     * divergence-injection file {@code catalog-002-hygiene.xml} differs from
     * v0.1.17 only by a checksum-neutral 3-line DATA EFFECT comment (commit
     * {@code 729c61d5c}), so the old leg still reaches the same
     * constraint-bearing state. If a future guard-bearing changeset becomes
     * the injection point, that is a new rehearsal generation with its own
     * old tag, not a rotation of this one.
     */
    private static final String OLD_TAG = "engine-service-v0.1.17";

    private static final String CHANGELOG_PATHSPEC = "service/src/main/resources/db/changelog";
    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";

    private static final String ADMIN_ROLE = "nexus_admin_rehearsal";
    private static final String ADMIN_PASS = "nexus_admin_rehearsal_pass";

    @Test
    void oldEngineChangelogTree_upgradesToHead_afterInjectedDivergence() throws Exception {
        Path oldChangelogRoot = ensureOldTreeMaterialized(repoRoot());

        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                dbaBootstrap(su);
            }

            try (HikariDataSource adminDs = newAdminPool(pg, "nexus-admin-rehearsal")) {

                int oldLegApplied = applyOldLeg(adminDs, oldChangelogRoot);

                try (Connection conn = adminDs.getConnection()) {
                    assertThat(changesetApplied(conn, "catalog-002-2-chash-checks", "nexus-70r3c.2"))
                        .as("old leg must have reached catalog-002-2-chash-checks (adds the chash "
                            + "CHECK constraints) — otherwise the injected divergence below is meaningless")
                        .isTrue();
                    assertThat(changesetApplied(conn, "catalog-013-2", "nexus-e0hd2"))
                        .as("old tag %s must genuinely PREDATE catalog-013-2 — if this is already applied "
                            + "by the old leg, the rehearsal is not exercising an old->HEAD hop at all", OLD_TAG)
                        .isFalse();
                }

                // ── DIVERGENCE: the verbatim ms57z shape (GH #1390). ─────────────
                try (Connection conn = adminDs.getConnection()) {
                    dsl(conn).alterTable(DSL.table(DSL.name("nexus", "chunks_384")))
                        .dropConstraintIfExists(DSL.name("chunks_384_chash_len_check"))
                        .execute();
                }

                // ── HEAD LEG: the REAL production entry point, resolving the
                // classpath (HEAD) master changelog. This is the RED/GREEN hinge:
                // before nexus-4m6i0.1's fix this throws MigrationException
                // (crash-loop); the fix (already merged) makes it complete cleanly. ─
                assertThatCode(() -> SchemaMigrator.migrate(adminDs))
                    .as("migration from the OLD TAG %s's changelog tree to HEAD must not crash-loop "
                        + "when the aged-box divergence (missing chunks_384_chash_len_check) is present", OLD_TAG)
                    .doesNotThrowAnyException();

                int headLegApplied;
                try (Connection conn = adminDs.getConnection()) {
                    headLegApplied = changelogRowCount(conn);
                }
                assertThat(headLegApplied)
                    .as("HEAD leg must have applied MORE changesets than the old leg alone — a chain "
                        + "that silently no-ops (old tree already == HEAD) must fail, not skip-pass")
                    .isGreaterThan(oldLegApplied);

                try (Connection conn = adminDs.getConnection()) {
                    assertThat(changesetExecType(conn, "catalog-013-2", "nexus-e0hd2"))
                        .as("catalog-013-2 must be recorded MARK_RAN (skipped-and-marked, never retried) "
                            + "on the old->HEAD hop, exactly as it is on the fresh-DB path")
                        .isEqualTo("MARK_RAN");

                    // RDR-180 era end-state, RE-DERIVED for RDR-191 Phase 4
                    // unify: the TEXT-era len_checks are gone (rdr180-2 drops
                    // all five — the injected divergence is tolerated via
                    // DROP IF EXISTS), replaced by the octet CHECKs added NOT
                    // VALID (validated ONLY by the client rung's admin
                    // connection post-rekey, never at boot — the GH #1390
                    // crash-loop class, retired by design). vectors-004-1
                    // then runs LATER in the same old-tag->HEAD hop and
                    // collapses chunks_384/768/1024 into ONE nexus.chunks
                    // table via DROP TABLE ... CASCADE, so their per-table
                    // octet CHECKs die with them too — only the unified
                    // chunks_chash_octet_check survives in their place.
                    // catalog_document_chunks is untouched by the unify.
                    assertThat(constraintExists(conn, "chunks_chash_octet_check"))
                        .as("unified nexus.chunks carries the octet CHECK post-RDR-191-unify, "
                            + "reached at the end of this same old-tag->HEAD hop")
                        .isTrue();
                    assertThat(constraintValidated(conn, "chunks_chash_octet_check"))
                        .as("chunks_chash_octet_check stays NOT VALID at boot (rung validates)")
                        .isFalse();
                    for (String t : new String[] {"chunks_384", "chunks_768", "chunks_1024"}) {
                        assertThat(constraintExists(conn, t + "_chash_len_check"))
                            .as("%s no longer exists post-vectors-004-1 unify -- no len_check to find", t)
                            .isFalse();
                        assertThat(constraintExists(conn, t + "_chash_octet_check"))
                            .as("%s's own octet CHECK died with its table at the unify DROP", t)
                            .isFalse();
                    }
                    assertThat(constraintExists(conn, "catalog_document_chunks_chash_len_check"))
                        .as("catalog_document_chunks_chash_len_check must be gone post-rdr180-2")
                        .isFalse();
                    assertThat(constraintExists(conn, "catalog_document_chunks_chash_octet_check"))
                        .as("catalog_document_chunks_chash_octet_check must exist post-rdr180-11")
                        .isTrue();
                    assertThat(constraintValidated(conn, "catalog_document_chunks_chash_octet_check"))
                        .as("catalog_document_chunks octet CHECK stays NOT VALID at boot")
                        .isFalse();

                    assertThat(tablesInSchema(conn, "nexus"))
                        .as("core catalog/chunk tables must exist after the old-tag->HEAD hop")
                        .containsAll(Set.of("chunks", "catalog_document_chunks", "memory"));
                    // RDR-187 (nexus-piwya.9): chash_index died at the DROP —
                    // the hop's END STATE has no chash_index at all. RDR-191
                    // Phase 4 (vectors-004-1): the three per-dim chunks tables
                    // likewise die at the DROP, collapsed into nexus.chunks.
                    assertThat(tablesInSchema(conn, "nexus"))
                        .as("chash_index and chunks_384/768/1024 must all be GONE at HEAD "
                            + "(rdr187-2 and vectors-004-1 respectively)")
                        .doesNotContain("chash_index", "chunks_384", "chunks_768", "chunks_1024");
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * nexus-u5dln — the DATA-bearing leg (Tier 2b). Same old-tag-tree hop, but
     * instead of a schema divergence the old leg is seeded with legacy-shaped
     * ROWS before the HEAD leg runs:
     *
     * <ul>
     *   <li>legacy 64-char {@code chash_index} rows (the SQLite-era verbatim ETL
     *       copies — the exact nexus-1wjmq / v0.1.33-incident population,
     *       fixture mirrored from {@link Catalog013RlsReplayTest}: both dedupe
     *       collision classes plus a cross-tenant row), exercising
     *       catalog-013-0/-1b's normalization and making catalog-013-2's
     *       VALIDATE non-vacuous;</li>
     *   <li>un-stamped {@code catalog_document_chunks} manifest rows
     *       ({@code collection} NULL — the exact nexus-x6kdz live-tenant
     *       population) under a real {@code catalog_documents} row, exercising
     *       catalog-014-0's toggle-wrapped {@code manifest_backfill()} stamp.</li>
     * </ul>
     *
     * <p>The load-bearing detail is the ROLE: the HEAD leg runs as the same
     * NOSUPERUSER/NOBYPASSRLS owner production uses ({@code nexus_admin}), so
     * every FORCE-RLS policy hides every seeded row from the migration's row-DML
     * (asserted explicitly below before the hop). A changeset whose DML silently
     * no-ops under that visibility — and whose downstream backstop then trips on
     * the untouched rows — fails THIS test instead of a fleet box. That is the
     * mechanism the v0.1.33 outage proved and the empty-database rehearsal above
     * structurally cannot reproduce.
     *
     * <p>Scope: only the hop's migration-time row-DML changesets get seeded
     * inputs here — see the SEED-COVERAGE contract block inside this method
     * for the current, exact set (restating the count in prose here is how
     * it drifted stale before; the block is parsed and enforced
     * mechanically, so it cannot). The toggle-wrapped discipline itself is additionally enforced
     * statically for every current and future changeset by
     * {@code tests/test_changelog_rls_lint.py} (nexus-php10); this leg is the
     * dynamic proof that the discipline actually WORKS on the real hop, and
     * the template to extend when a future hop gains a new row-DML changeset
     * (seed its input shape, assert its effect). That extension is
     * now by convention only: the Python lint that derived the hop's
     * FORCE-RLS row-DML changeset set from an OLD_TAG snapshot was deleted at
     * cleanup step 10b.
     */
    @Test
    void oldEngineChangelogTree_withLegacySeededRows_dataChangesetsActuallyExecute() throws Exception {
        Path oldChangelogRoot = ensureOldTreeMaterialized(repoRoot());

        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                dbaBootstrap(su);
            }

            try (HikariDataSource adminDs = newAdminPool(pg, "nexus-admin-rehearsal-data")) {

                applyOldLeg(adminDs, oldChangelogRoot);

                try (Connection conn = adminDs.getConnection()) {
                    assertThat(changesetApplied(conn, "catalog-013-0", "nexus-e0hd2"))
                        .as("old tag %s must PREDATE catalog-013's chash normalization — otherwise "
                            + "seeding legacy 64-char rows exercises nothing", OLD_TAG)
                        .isFalse();
                    assertThat(changesetApplied(conn, "catalog-014-0", "nexus-x6kdz"))
                        .as("old tag %s must PREDATE catalog-014's manifest collection stamp — "
                            + "otherwise seeding un-stamped manifest rows exercises nothing", OLD_TAG)
                        .isFalse();
                    assertThat(changesetApplied(conn, "catalog-016-0", "nexus-78n33"))
                        .as("old tag %s must PREDATE catalog-016's source_uri dedup backfill — "
                            + "otherwise seeding duplicate live source_uri rows exercises nothing",
                            OLD_TAG)
                        .isFalse();
                    assertThat(changesetApplied(conn, "aspects-004-1", "nexus-ubnwk"))
                        .as("old tag %s must PREDATE aspects-004's doc_id backfill — otherwise "
                            + "seeding a legacy NULL-doc_id aspects row exercises nothing", OLD_TAG)
                        .isFalse();
                    assertThat(changesetApplied(conn, "catalog-034-0", "nexus-v3w9n"))
                        .as("old tag %s must PREDATE catalog-034's tumbler-grammar data "
                            + "remediation — otherwise seeding a legacy sub-3-segment "
                            + "tumbler row exercises nothing", OLD_TAG)
                        .isFalse();
                }

                // ── SEED, as superuser (implicit BYPASSRLS): models rows written
                // by old clients through the service role WITH a tenant GUC set —
                // exactly the population a real aged box carries into an upgrade.
                //
                // SEED-COVERAGE-BEGIN (every hop
                // changeset whose row-DML this leg seeds inputs for and
                // effect-asserts, as "<id> <author>" lines; the Python lint
                // that once parsed this block was deleted at cleanup step 10b):
                //   catalog-013-0 nexus-e0hd2
                //   catalog-013-1b nexus-1wjmq
                //   catalog-014-0 nexus-x6kdz
                //   catalog-016-0 nexus-78n33
                //   catalog-025-0 nexus-71gw2
                //   vectors-004-1 nexus-o8dil.12
                //   taxonomy-007-1 nexus-jv3ue
                //   catalog-029-1 nexus-o8dil.29
                //   fk-004-0-reconcile-precount nexus-iq0qr
                //   fk-004-1-reconcile nexus-o8dil.49
                //   catalog-032-1 nexus-tk070.p1
                //   legacy-001-1 nexus-lgdel.l1
                //   legacy-001-2 nexus-lgdel.l1
                //   taxonomy-010-1 nexus-tk070.p3b
                //   taxonomy-011-1 nexus-tk070.p3c
                //   taxonomy-012-2 nexus-tk070.p3d
                //   taxonomy-014-2 nexus-tk070.p5a
                //   taxonomy-014-3 nexus-tk070.p5a
                //   taxonomy-014-4 nexus-tk070.p5a
                //   taxonomy-014-5 nexus-tk070.p5a
                //   memory-003-1 nexus-tk070.p6a
                //   plans-003-1 nexus-tk070.p6a
                //   telemetry-006-1 nexus-tk070.p6b
                //   telemetry-006-2 nexus-tk070.p6b
                //   aspects-004-1 nexus-ubnwk
                //   catalog-034-0 nexus-v3w9n
                //   hygiene-001-1 nexus-tk070.p6a
                //   hygiene-001-2 nexus-tk070.p6a
                //   hygiene-001-3 nexus-tk070.p6a
                //   hygiene-001-4 nexus-tk070.p6a
                //   hygiene-001-5 nexus-tk070.p6a
                //   hygiene-001-6 nexus-tk070.p6a
                //   hygiene-001-7 nexus-tk070.p6a
                //   hygiene-001-8 nexus-tk070.p6a
                //   hygiene-001-9 nexus-tk070.p6a
                //   hygiene-001-9b nexus-tk070.p6a
                //   hygiene-001-10 nexus-tk070.p6a
                //   hygiene-001-11 nexus-tk070.p6a
                //   hygiene-002-1 nexus-ft04v.4
                //   hygiene-004-1 nexus-ztafa
                //   hygiene-005-3 nexus-uxd2a
                //   tuples-003-2 nexus-r7xao
                //   tuples-004-1 nexus-8zoyp
                //   pipeline-002-2 nexus-edjmu
                //   hygiene-008-2 nexus-0rxvg
                //   catalog-044-3 nexus-6pbwx
                // SEED-COVERAGE-END ─────────────────────────────────────────────
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    // FK parents (fk-002/fk-003's NOT VALID FKs, applied by the
                    // old leg, enforce on new writes).
                    for (String[] tc : new String[][]{
                        {"t1", "code__x"}, {"t1", "code__y"}, {"t2", "code__z"},
                        // RDR-225 (nexus-3wh8d.13): a centroid is copied under its collection's model, and a
                        // vector whose dimension disagrees with that model fails the model partition's CHECK
                        // (legacy data shapes are not supported). The 2-segment names above are registered by
                        // hygiene-002 under the grandfathered fallback bge-base-en-v15-768, so only the 768-d
                        // centroid may live there; the 384-d and 1024-d taxonomy-007-1 centroids are seeded in
                        // collections whose 4-segment names register them under a model of their own width.
                        {"t1", "code__cx__minilm-l6-v2-384__v1"}, {"t2", "code__cz__voyage-code-3__v1"},
                        // nexus-tk070.p3b: two collections DEDICATED to taxonomy-010-1's
                        // ambiguous-arm seed below, distinct from code__x/code__y so its
                        // fresh chunks_384/768 rows don't perturb vectors-004-1's own
                        // per-collection row-count assertions further down.
                        {"t1", "code__p3ba"}, {"t1", "code__p3bb"}}) {
                        registerCollection(su, tc[0], tc[1]);
                    }
                    // Legacy 64-char chash_index rows — Catalog013RlsReplayTest's
                    // fixture verbatim.
                    String p32a = "a".repeat(32);
                    // dedupe class 1: a 64-char row whose [:32] collides with an
                    // existing 32-char row on the natural key
                    seedChashRow(su, "t1", p32a, "code__x");
                    seedChashRow(su, "t1", p32a + "b".repeat(32), "code__x");
                    // dedupe class 2: two 64-char rows sharing a [:32] prefix
                    String p32c = "c".repeat(32);
                    seedChashRow(su, "t1", p32c + "d".repeat(32), "code__y");
                    seedChashRow(su, "t1", p32c + "e".repeat(32), "code__y");
                    // plain legacy row, second tenant
                    seedChashRow(su, "t2", "f".repeat(32) + "0".repeat(32), "code__z");

                    // Un-stamped manifest rows (collection NULL — the nexus-x6kdz
                    // live-tenant population) under a real document, for
                    // catalog-014-0's manifest_backfill() stamp. fk-001-5's
                    // immediately-valid FK requires the parent document row.
                    seedDocument(su, "t1", "1.1.100", "seeded doc", "code__x");
                    seedManifestRow(su, "t1", "1.1.100", 0, "1".repeat(32));
                    seedManifestRow(su, "t1", "1.1.100", 1, "2".repeat(32));
                    // nexus-j862l (RDR-191 GATE-2 follow-up, item 3): catalog-025 runs
                    // LATER in this SAME hop and deletes any non-NULL-collection
                    // manifest row whose chash has no matching chunks_384/768/1024 row
                    // for its (tenant_id, collection) -- the "dangling" check. A real
                    // legacy tenant's manifest row is never written without its
                    // matching content landing in the same transaction, so give this
                    // fixture the same shape: matching chunks_384 content for both
                    // seeded chashes, so catalog-014-0's stamp is still there to
                    // observe once the hop reaches catalog-025 and beyond (diagnosed:
                    // without this, catalog-014-0's backfill DOES run correctly, but
                    // catalog-025's independently-correct dangling cleanup then removes
                    // the content-less rows before this test's later assertion runs --
                    // see the assertion's own comment below for the full trace).
                    seedChunk384LegacyContent(su, "t1", "code__x", "1".repeat(32), "legacy chunk 1 text");
                    seedChunk384LegacyContent(su, "t1", "code__x", "2".repeat(32), "legacy chunk 2 text");

                    // nexus-j862l (RDR-191 GATE-2, seed-coverage follow-up):
                    // catalog-025-0 is FORCE-RLS row-DML (its own NO FORCE/FORCE
                    // toggle around the DELETE+resync+SET NOT NULL body) and was
                    // previously undercovered by this leg -- the two rows above only
                    // prove the KEEP arm (backed content survives). A third, DELIBERATELY
                    // content-less row proves the DELETE arm actually fires under
                    // FORCE-RLS, not merely that it no-ops safely: manifest_backfill()
                    // stamps it 'code__x' exactly like the other two (so it is NOT
                    // removed by catalog-025-0's NULL-row step), but it has no matching
                    // chunks_384/768/1024 content, so catalog-025-0's DANGLING-row step
                    // must delete it. A separate doc keeps the KEEP-arm and DELETE-arm
                    // proofs independently legible.
                    seedDocument(su, "t1", "1.1.101", "dangling-row doc", "code__x");
                    seedManifestRow(su, "t1", "1.1.101", 0, "3".repeat(32));
                    // Deliberately NO seedChunk384LegacyContent call for this chash.

                    // Duplicate LIVE source_uri rows (the RDR-156 P0 audit's
                    // 201-uri ghost class) for catalog-016-0's dedup backfill:
                    // 1.1.202 wins (most chunks); 1.1.201 must be tombstoned.
                    seedDocumentWithUri(su, "t1", "1.1.201", "dup loser", "code__x",
                        "file:///seed/dup.md", 1);
                    seedDocumentWithUri(su, "t1", "1.1.202", "dup winner", "code__x",
                        "file:///seed/dup.md", 7);

                    // aspects-004-1 (nexus-ubnwk, RDR-156 Decision 5): a legacy
                    // document_aspects row whose doc_id predates fk-001-2's
                    // go-forward stamping (NULL), with a source_uri that
                    // EXACTLY matches a live, non-alias, non-tombstoned catalog
                    // document — aspects-004-1's own join-and-stamp input
                    // shape. 1.1.204 (next free in the 20x family this leg
                    // already uses for catalog-016-0's dedup fixture).
                    seedDocumentWithUri(su, "t1", "1.1.204", "aspect seed doc", "code__x",
                        "file:///seed/ubnwk-aspect.md", 1);
                    seedAspectRow(su, "t1", "code__x", "seed/ubnwk-aspect.md",
                        "file:///seed/ubnwk-aspect.md");

                    // catalog-034-0 (nexus-v3w9n): the segment-count tumbler-grammar
                    // data remediation's own input shape — the real 2026-08-28
                    // production census found exactly two live catalog_documents
                    // rows with a 2-segment tumbler ("1.1"/"1.2"), phantom
                    // registrations under a nonexistent 1-segment owner. "1.5"
                    // mirrors that shape (2 dot-separated segments) -- the DELETE
                    // (tombstone) arm. A conforming 3-segment control row
                    // ("1.1.205", next free in the 20x family this leg already
                    // uses for catalog-016-0/aspects-004-1's dedup/aspect
                    // fixtures) proves the predicate is selective, not a blanket
                    // tombstone -- the KEEP arm. RLS toggle-wrapped: catalog_documents
                    // is FORCE ROW LEVEL SECURITY and the Liquibase role is not
                    // BYPASSRLS, the same silent-no-op hazard as catalog-014-0/
                    // catalog-016-0/catalog-025-0/aspects-004-1 -- its FORCE-restored
                    // state is proven by the existing generic catalog_documents check
                    // below (aspects-004-1's own toggle-wrap block), no duplicate
                    // FORCE-restored assertion needed.
                    seedDocument(su, "t1", "1.5", "ghost 2-segment tumbler", "code__x");
                    seedDocument(su, "t1", "1.1.205", "conforming control doc", "code__x");

                    // vectors-004-1 / taxonomy-007-1 (nexus-97gii seed-coverage
                    // follow-up, RDR-191 Phase 4 unify): straddling per-dim
                    // content, the reference semantics of
                    // VectorsUnifyChunksIntegrationTest#straddlingDistribution_
                    // rowsLandInCorrectTypedColumn /
                    // VectorsUnifyCentroidsIntegrationTest's analogous test.
                    // Both changesets are FORCE-RLS row-DML solely via their
                    // own NO FORCE toggle (step 0's DML-blindness defuse before
                    // the copy) -- this leg is the dynamic proof that toggle
                    // actually lets the NOBYPASSRLS owner's copy see and move
                    // real rows, on the SAME old-tag-to-HEAD hop as every other
                    // leg here. chunks_384 is already covered by the j862l rows
                    // seeded above (collection code__x, tenant t1); only 768
                    // and 1024 need fresh rows to complete the straddle.
                    // taxonomy_centroids_<dim> carries no FK to
                    // catalog_collections or nexus.topics (verified against
                    // taxonomy-002-centroids.xml, mirroring
                    // VectorsUnifyCentroidsIntegrationTest#seedCentroid's own
                    // note), so no stub registration is needed for it.
                    seedChunkDimLegacyContent(su, 768, "t1", "code__y",
                        "6".repeat(32), "legacy chunk dim768");
                    seedChunkDimLegacyContent(su, 1024, "t2", "code__z",
                        "7".repeat(32), "legacy chunk dim1024");
                    seedTaxonomyCentroidLegacyContent(su, 384, "t1", "code__cx__minilm-l6-v2-384__v1", 900L, "centroid-384");
                    seedTaxonomyCentroidLegacyContent(su, 768, "t1", "code__y", 901L, "centroid-768");
                    seedTaxonomyCentroidLegacyContent(su, 1024, "t2", "code__cz__voyage-code-3__v1", 902L, "centroid-1024");

                    // catalog-032-1 (nexus-tk070.p1, RDR-194 § D2): catalog_links carries
                    // NO FK on the old leg's tree, so both rows below write freely.
                    // KEEP arm -- a real edge between two documents that both exist and
                    // survive the whole hop (1.1.100/1.1.101 are already seeded above for
                    // catalog-014-0/catalog-025-0; catalog-025-0's dangling-row cleanup
                    // only removes 1.1.101's MANIFEST row, never its catalog_documents
                    // row, so it remains a valid link target throughout).
                    seedLink(su, "t1", "1.1.100", "1.1.101", "cites");
                    // DELETE arm -- an edge whose to_tumbler was NEVER registered as a
                    // document, modeling the real aged-fleet population (277 rows,
                    // nexus-ysrwi, 2026-07-25) catalog-032-1's anti-join must clean up.
                    seedLink(su, "t1", "1.1.100", "1.1.999-ghost", "cites");

                    // legacy-001-1/legacy-001-2 (nexus-lgdel.l1, THE DELETE): a
                    // legacy-width (non-64-hex) chunk_id row per table -- the DELETE
                    // arm -- plus a canonical-width row -- the KEEP arm, proving the
                    // shape-agnostic `!~ '^[0-9a-f]{64}$'` predicate is selective, not
                    // a blanket wipe. "e" x32 / "9" x32 are legacy-width by construction
                    // (32 hex chars, never matches ^[0-9a-f]{64}$); "d" x64 / "f" x64 are
                    // canonical-width (64 hex chars, all valid hex digits) -- same
                    // repeat-char-literal style this file already uses for chash_index's
                    // seed values above.
                    seedFrecencyRow(su, "t1", "e".repeat(32));
                    seedFrecencyRow(su, "t1", "d".repeat(64));
                    seedRelevanceLogRow(su, "t1", "9".repeat(32), "legacy-001-seed-query");
                    seedRelevanceLogRow(su, "t1", "f".repeat(64), "legacy-001-seed-query");

                    // taxonomy-010-1 (nexus-tk070.p3b, RDR-194 § D1 steps b/c/d):
                    // topic_assignments.source_collection remediation. THREE
                    // source_collection-NULL assignments, one per DELETE arm,
                    // plus a FOURTH, non-NULL-source_collection assignment
                    // added 2026-08-17 (critical fix round, nexus-i3k3e -- the
                    // cc4 wedge class), under a shared topic (PK is (tenant_id, doc_id, topic_id),
                    // so sharing topic_id across distinct doc_id values is
                    // fine). The positive (b) UNIQUE-RESOLUTION backfill arm is
                    // NOT exercisable in THIS old-tag hop: chunks_384/768/1024
                    // (the ONLY pre-hop route into nexus.chunks -- it does not
                    // exist until vectors-004-1 copies these tables, mid-hop)
                    // carry a `length(chash) = 32` CHECK inherited from the OLD
                    // leg's tree (catalog-002-hygiene), so every chash this
                    // fixture CAN seed is legacy-width (16 bytes post-decode)
                    // and therefore EXCLUDED from backfill candidacy by
                    // taxonomy-010-1's own `doc_id ~ '^[0-9a-f]{64}$'` shape
                    // guard -- exactly the LEGACY-SHAPE-COINCIDENTAL-MATCH arm
                    // below, not a KEEP arm. The KEEP arm is proven separately,
                    // free of that legacy-schema constraint, by
                    // Taxonomy010BackfillDirectIntegrationTest (fresh 64-hex
                    // content seeded straight into nexus.chunks post-HEAD).
                    long p3bTopicId = seedTopic(su, "t1", "code__x", "rehearsal-p3b-topic");
                    // (c) AMBIGUOUS arm: the SAME (legacy-width) chash exists
                    // under TWO distinct collections (code__p3ba via
                    // chunks_384, code__p3bb via chunks_768 -- dedicated
                    // collections registered above so these fresh rows don't
                    // perturb vectors-004-1's own per-collection row-count
                    // assertions on code__x/code__y further down) -- must be
                    // DELETED, unattributable. The ambiguous-DELETE predicate
                    // does not gate on doc_id shape, so 32-hex content
                    // exercises it exactly as well as 64-hex would.
                    seedChunk384LegacyContent(su, "t1", "code__p3ba", "b".repeat(32), "p3b ambiguous chunk a");
                    seedChunkDimLegacyContent(su, 768, "t1", "code__p3bb", "b".repeat(32), "p3b ambiguous chunk b");
                    seedTopicAssignment(su, "t1", "b".repeat(32), p3bTopicId);
                    // (c) UNRESOLVABLE (anti-join) arm: doc_id is canonical
                    // 64-hex SHAPE but has NO backing chunk anywhere -- must be
                    // DELETED, proving the anti-join arm fires independently of
                    // the shape predicate (this doc_id passes the shape check).
                    seedTopicAssignment(su, "t1", "c".repeat(64), p3bTopicId);
                    // (c) LEGACY-SHAPE-COINCIDENTAL-MATCH arm (the cc4/HAL
                    // no-wedge proof): doc_id = "1"x32, the SAME legacy 32-hex
                    // chash already seeded above for the j862l chunks_384
                    // fixture (tenant t1, collection code__x) -- an anti-join
                    // alone WOULD find a match here (encode(chash,'hex') round-
                    // trips to "1"x32), but taxonomy-010-1's backfill excludes
                    // it on shape ALONE (never reaches the anti-join), and the
                    // explicit doc_id !~ '^[0-9a-f]{64}$' arm of the
                    // UNRESOLVABLE delete catches it regardless of the
                    // (irrelevant here) anti-join outcome. This is the exact
                    // shape cc4's 5,896-row cloud population takes (T2
                    // nexus/rdr194-cc4-census-2026-08-16): a non-conformant
                    // doc_id that happens to text-match real chunk content
                    // must still be treated as unresolvable garbage, never
                    // silently backfilled or left behind.
                    seedTopicAssignment(su, "t1", "1".repeat(32), p3bTopicId);
                    // (c) UNRESOLVABLE (shape-invalid, NON-NULL
                    // source_collection) arm — the cc4/nexus-i3k3e cloud
                    // wedge fixture (critical fix round, 2026-08-17). The
                    // cc4 census's exact 1,262-row class: a legacy 32-hex
                    // (non-canonical-shape) doc_id whose source_collection
                    // was ALREADY set by the pre-P3a projection/
                    // cross-collection writer branch. Before the
                    // 2026-08-17 fix, this row's non-NULL source_collection
                    // would have made it SURVIVE taxonomy-010-1's
                    // NULL-scoped delete untouched, then deterministically
                    // RAISE EXCEPTION on taxonomy-011-1's guard a few
                    // changesets later in this SAME migration walk — this
                    // fixture proves the full walk now survives that row.
                    seedTopicAssignment(su, "t1", "8".repeat(32), p3bTopicId, "code__x");

                    // memory-003-1 / plans-003-1 (nexus-tk070.p6a, RDR-194 § D5):
                    // ttl -> ttl_days rename + counted DELETE of ttl=0 rows, the
                    // SAME NO FORCE/FORCE toggle-wrap shape as catalog-013-1b/
                    // catalog-014-0/catalog-025-0/catalog-029-1/catalog-032-1/
                    // legacy-001-1/legacy-001-2. nexus.memory and nexus.plans are
                    // baseline tables (memory-001-baseline.xml/plans-001-baseline.xml
                    // both predate OLD_TAG), so both accept rows freely on the old
                    // leg. DELETE arm: a ttl=0 row per table (memory-003-1/
                    // plans-003-1's whole reason to exist -- ttl=0 is
                    // unrepresentable under the new CHECK). KEEP arm: a NULL-ttl
                    // (permanent) row per table, proving the DELETE is selective on
                    // ttl=0 specifically, not a blanket wipe.
                    seedMemoryRow(su, "t1", "p6a-proj", "p6a-zero", 0);
                    seedMemoryRow(su, "t1", "p6a-proj", "p6a-permanent", null);
                    seedPlanRow(su, "t1", "p6a-proj", "p6a zero query", 0);
                    seedPlanRow(su, "t1", "p6a-proj", "p6a permanent query", null);

                    // telemetry-006-1/telemetry-006-2 (nexus-tk070.p6b, RDR-194 § D5):
                    // frecency.ttl_days 0 -> NULL counted UPDATE (not a DELETE --
                    // these rows are RECOVERED by converting the sentinel, not
                    // discarded), the same NO FORCE/FORCE toggle-wrap shape as
                    // memory-003-1/plans-003-1/legacy-001-1/legacy-001-2. Both
                    // nexus.frecency and staging.frecency are baseline-shaped
                    // tables at OLD_TAG: nexus.frecency predates OLD_TAG
                    // (telemetry-001-baseline.xml, already exercised above by
                    // legacy-001-1's own seed rows "e"x32/"d"x64); staging.frecency
                    // does NOT exist at OLD_TAG (staging-001-landing-tables.xml is
                    // itself part of THIS hop, created before telemetry-006-2 runs)
                    // -- see telemetry-006-2's own coverage note below for why it
                    // needs no fresh seed. CONVERSION arm: a ttl_days=0 row. KEEP
                    // arm: a POSITIVE ttl_days row that must survive UNCHANGED
                    // (not converted -- proves the UPDATE's WHERE ttl_days = 0
                    // predicate is selective, not a blanket rewrite). Unlike
                    // memory-003-1/plans-003-1 (whose KEEP arm is a NULL row), a
                    // NULL KEEP arm CANNOT be seeded here: at OLD_TAG, nexus.
                    // frecency.ttl_days is STILL `NOT NULL DEFAULT 0`
                    // (telemetry-006-1 is the changeset that first permits NULL),
                    // so nothing in this old-leg seed phase can write NULL into
                    // it -- the permanent-sentinel arm is proven instead by
                    // TelemetryRepositoryTest's upsertFrecency_ttlDaysNull_
                    // isPermanentAndAccepted (a fresh HEAD-schema test), the same
                    // "arm proven separately, outside this fixture" split
                    // taxonomy-010-1's own coverage note above already uses for
                    // its un-seedable positive-resolution arm. Fresh chunk_id
                    // values ("6"x64/"7"x64), distinct from legacy-001-1's own
                    // "e"x32 (legacy-width, deleted by legacy-001-1 earlier in
                    // this same hop) and "d"x64 (canonical, survives with its
                    // DEFAULT ttl_days=0 -- also converted to NULL by
                    // telemetry-006-1, effect-asserted below alongside the
                    // dedicated fixture).
                    seedFrecencyTtlRow(su, "t1", "6".repeat(64), 0);
                    seedFrecencyTtlRow(su, "t1", "7".repeat(64), 45);

                    // hygiene-001-1 (nexus-tk070.p6a follow-on, review round item 2 --
                    // nexus-gm38i seed-coverage lint): document_aspects.doc_id/
                    // source_uri NOT NULL. aspects-004-1's own seed above (the
                    // ubnwk-aspect.md row) does NOT exercise this changeset's own
                    // DELETE/UPDATE arms -- that row's doc_id is NULL at insert time
                    // but its source_uri deliberately MATCHES a real catalog
                    // document, so aspects-004-1 (which runs earlier in this same
                    // hop) already converts it to a real doc_id before hygiene-001-1
                    // ever sees it. Two DEDICATED rows, unconvertible by aspects-004-1
                    // by construction:
                    //   DELETE arm: doc_id NULL, source_uri that matches NO real
                    //   catalog document (aspects-004-1's join misses, doc_id stays
                    //   NULL all the way to hygiene-001-1).
                    //   UPDATE arm: doc_id already set (bypasses aspects-004-1
                    //   entirely, its predicate is doc_id IS NULL), source_uri NULL.
                    seedAspect(su, "t1", "code__x", "seed/hygiene1-orphan.md", "rehearsal-seeded",
                        "file:///seed/hygiene1-unconvertable.md", null);
                    seedAspect(su, "t1", "code__x", "seed/hygiene1-legit.md", "rehearsal-seeded",
                        null, "1.1.100");

                    // hygiene-001-2 (aspect_extraction_queue.doc_id NOT NULL):
                    // reverses fk-001-4's nullable conversion (already applied at
                    // OLD_TAG) -- doc_id NULL is representable at this seed point.
                    dsl(su).insertInto(ASPECT_EXTRACTION_QUEUE)
                        .set(ASPECT_EXTRACTION_QUEUE.TENANT_ID, "t1")
                        .set(ASPECT_EXTRACTION_QUEUE.COLLECTION, "code__x")
                        .set(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH, "seed/hygiene2-orphan.md")
                        .set(ASPECT_EXTRACTION_QUEUE.DOC_ID, (String) null)
                        .set(ASPECT_EXTRACTION_QUEUE.ENQUEUED_AT, DSL.currentOffsetDateTime())
                        .execute();

                    // hygiene-001-3 (document_highlights.source_uri/collection NOT
                    // NULL): source_uri/collection are nullable at baseline (no
                    // separate fk-00X conversion needed for THOSE columns), but
                    // doc_id itself carries a real FK to catalog_documents by this
                    // point in the changelog -- reuse the already-registered
                    // 1.1.101 document (no OTHER document_highlights row claims it)
                    // rather than a fabricated identity.
                    dsl(su).insertInto(DOCUMENT_HIGHLIGHTS)
                        .set(DOCUMENT_HIGHLIGHTS.TENANT_ID, "t1")
                        .set(DOCUMENT_HIGHLIGHTS.DOC_ID, "1.1.101")
                        .set(DOCUMENT_HIGHLIGHTS.SOURCE_URI, (String) null)
                        .set(DOCUMENT_HIGHLIGHTS.COLLECTION, (String) null)
                        .set(DOCUMENT_HIGHLIGHTS.INGESTED_AT, DSL.currentOffsetDateTime())
                        .execute();

                    // hygiene-001-9b (taxonomy_centroids.label NOT NULL): a dedicated
                    // collection/topic_id, distinct from taxonomy-007-1's own
                    // code__x/code__y/code__z rows above, so this row does not
                    // perturb THEIR per-collection count assertions. No stub
                    // registration needed (taxonomy_centroids_<dim> carries no FK to
                    // catalog_collections or nexus.topics, per taxonomy-007-1's own
                    // coverage note above). label=null binds SQL NULL through the
                    // typed jOOQ insert.
                    seedTaxonomyCentroidLegacyContent(
                        su, 384, "t1", "code__hyg9b", 999L, null);

                    // hygiene-001-10/-11 (nexus-tk070.p6a follow-on, coordinator scope
                    // addition): plans TTL catch-up sweep + plans.verb NOT NULL. Two
                    // DEDICATED rows, each isolated from the OTHER changeset's own
                    // predicate so this rehearsal proves each independently:
                    //   -10 row: verb set (dodges -11's verb-IS-NULL delete), ttl=1
                    //   (survives plans-003-1's earlier ttl=0 delete, above), but
                    //   created_at backdated 10 days -- age (10 days) > ttl_days (1)
                    //   at hygiene-001-10's own predicate, so it is expired and
                    //   deleted by THIS changeset specifically, not plans-003-1's
                    //   ttl=0 arm.
                    //   -11 row: verb NULL, ttl NULL (permanent -- dodges -10's
                    //   `ttl_days IS NOT NULL` predicate entirely), so only
                    //   hygiene-001-11's verb-IS-NULL delete can remove it.
                    seedPlan(su, "t1", "p6a-proj", "hygiene10 expired query", 1, "research",
                        DSL.val(java.time.OffsetDateTime.now().minusDays(10)));
                    seedPlan(su, "t1", "p6a-proj", "hygiene11 verbless query", null, null,
                        DSL.currentOffsetDateTime());

                    // hygiene-002-1 (nexus-ft04v.4, RDR-204 Phase 1 item 1, seed-coverage
                    // lint follow-up): the one-time catalog_collections attribute
                    // backfill walk's four name-shape branches (see
                    // hygiene-002-collection-attributes-walk.xml's own header). Four
                    // DEDICATED rows, one per branch, registered via registerCollection()
                    // -- at this point in the hop lifecycle_state does not exist yet
                    // (added by catalog-036-4, well inside this SAME hop but AFTER this
                    // seed phase runs), so PgContainerHelper.insertCollection falls back
                    // to its bare (tenant_id, name) insert, leaving content_type/
                    // owner_id/embedding_model at their catalog-001-5 DEFAULT '' -- the
                    // exact blank input shape hygiene-002-1's walk exists to fix.
                    //
                    // The "one stored dimension agrees with the name's token" sub-arm of
                    // branches A/C is NOT exercisable in this fixture: hygiene-001-5
                    // (this same changelog file, runs strictly before hygiene-002-1)
                    // unconditionally deletes every nexus.chunks row with
                    // octet_length(chash) <> 32, and every chash this pre-hop seed phase
                    // can write is legacy-width (16-byte, decoded from a 32-hex literal
                    // against the OLD_TAG chunks_384/768/1024 schema) -- the identical
                    // "every chash seedable pre-hop is legacy-width" constraint
                    // taxonomy-010-1's own coverage note above already documents. By the
                    // time hygiene-002-1's DO block runs, nexus.chunks is structurally
                    // empty for every collection this fixture seeds, so every row below
                    // resolves through the zero-chunks arm of its branch (dimension
                    // NULL). The with-dimension arm (agreement and disagreement) is
                    // proven separately, free of that constraint, by
                    // Hygiene002CollectionAttributesWalkTest's HEAD-schema fixture.
                    //
                    // Branch A (4-segment conformant, KNOWN token): zero chunks -> live,
                    // dimension NULL, embedding_model keeps the name's own (known) token.
                    registerCollection(su, "t1", "code__h002a__bge-base-en-v15-768__v1");
                    // Branch B (quarantine-<ct>__...): lifecycle_state = 'quarantine'
                    // UNCONDITIONALLY, independent of chunk count or dimension agreement.
                    registerCollection(su, "t1", "quarantine-code__h002b__bge-base-en-v15-768__v1");
                    // Branch C (grandfathered 2-segment, no model token): zero chunks ->
                    // live, embedding_model the fallback (the profile is always empty at
                    // walk time).
                    registerCollection(su, "t1", "code__h002c");
                    // Branch D (no "__" separator at all): content_type 'unknown',
                    // owner_id the row's own tenant_id, lifecycle_state 'disputed'
                    // UNCONDITIONALLY, embedding_model the fallback.
                    registerCollection(su, "t1", "h002d-no-separator");

                    // hygiene-004-1 (nexus-ztafa, RDR-204 engine, seed-coverage lint
                    // follow-up): a 4-segment conformant name whose OWNER SEGMENT
                    // carries an underscore -- hygiene-002-1's own owner grammar
                    // ([a-zA-Z0-9-]+) cannot match this, so on THIS SAME hop it first
                    // misfiles the row into branch D exactly like h002d-no-separator
                    // above, before hygiene-004-1 (later in this same changelog file)
                    // re-files it into branch A using the widened owner grammar
                    // ([a-zA-Z0-9_-]+). Registered the same way as the other four
                    // hygiene-002-1 fixtures -- lifecycle_state does not exist yet at
                    // seed time, so the bare (tenant_id, name) insert leaves every
                    // attribute column blank, same starting shape. The
                    // misfile-then-refile TRANSITION itself is proven separately,
                    // free of this fixture's aged-fleet chash-width constraint, by
                    // Hygiene004OwnerGrammarUnderscoreTest's HEAD-schema fixtures;
                    // this leg only needs to prove the row survives the FULL
                    // aged-fleet hop correctly classified under real RLS.
                    registerCollection(su, "t1", "code__h004_owner__bge-base-en-v15-768__v1");

                    // hygiene-005-3 (nexus-uxd2a, RDR-204 engine, seed-coverage lint
                    // follow-up): the one-off data correction for
                    // nexus.catalog_collections rows a live buggy gc_quarantine_orphans
                    // self-registration (hygiene-002-2's OWN shipped defect, fixed by
                    // this same hop's hygiene-005-1) already mislabelled. This leg
                    // cannot reproduce that RUNTIME shape directly (gc_quarantine_orphans
                    // is called by application code at runtime, never by a Liquibase
                    // changeset), so it seeds the one shape reachable through a pure
                    // migration-only hop instead: a SUFFIX-shaped quarantine sibling name
                    // ("<name>__quarantine" -- see hygiene-005's own header for why the
                    // suffix shape is covered defensively alongside the real PREFIX
                    // production shape) never matches hygiene-002-1's OR hygiene-004-1's
                    // m4/mq/m2 regexes (both require a "__v<n>" ending; this name ends
                    // in "__quarantine"), so it is always classified content_type =
                    // 'unknown' / lifecycle_state = 'disputed' by hygiene-002-1's branch D
                    // -- exactly hygiene-005-3's own correction predicate
                    // (content_type = 'unknown' AND a quarantine-shaped name). Two
                    // DEDICATED rows, registered the same bare (tenant_id, name) way as
                    // the hygiene-002-1/hygiene-004-1 fixtures above (lifecycle_state does
                    // not exist yet at seed time):
                    //   -uxd2a1: a suffix-shaped sibling WHOSE ORIGIN (the name with the
                    //   suffix stripped) IS ALSO seeded below and correctly classified
                    //   'live'/'code' by hygiene-002-1's own branch A -- hygiene-005-3
                    //   must copy the origin's attributes and set lifecycle_state =
                    //   'quarantine'.
                    //   -uxd2a2: a suffix-shaped sibling with NO registered origin --
                    //   hygiene-005-3's "no origin" branch leaves content_type = 'unknown'
                    //   unchanged (no literal "quarantine-" prefix to strip) and never
                    //   touches lifecycle_state (stays 'disputed').
                    // The FULL branch-selection logic itself (both name shapes, the
                    // origin-copy vs no-origin-strip split, the dormant-promotion branch)
                    // is proven separately, free of this fixture's aged-fleet constraints,
                    // by Hygiene005GcRegistrationFromOriginRowDataCorrectionTest's
                    // HEAD-schema fixtures; this leg only needs to prove the row survives
                    // the FULL aged-fleet hop correctly classified under real RLS.
                    registerCollection(su, "t1", "code__uxd2a1__bge-base-en-v15-768__v1");
                    registerCollection(su, "t1", "code__uxd2a1__bge-base-en-v15-768__v1__quarantine");
                    registerCollection(su, "t1", "code__uxd2a2__bge-base-en-v15-768__v1__quarantine");

                    // catalog-044-3 (nexus-6pbwx): a slug-owned code collection holding a
                    // document under 1.15.x -- the shape a legacy repo collection has, since
                    // hygiene-002-1 (earlier in this hop) stamps owner_id from the NAME. The
                    // repair must re-derive it as 1-15. The KEEP arm is a knowledge
                    // collection whose document lives under 1.1.x, which must keep its
                    // subject. Bare inserts, like every fixture above (lifecycle_state does
                    // not exist yet at seed time). The full rule is proven separately by
                    // Catalog044OwnerFromDocumentsRepairTest's HEAD-schema fixtures.
                    registerCollection(su, "t1", "code__h044slug__bge-base-en-v15-768__v1");
                    seedDocument(su, "t1", "1.15.301", "owner-derive doc", "code__h044slug__bge-base-en-v15-768__v1");
                    registerCollection(su, "t1", "knowledge__h044subject__bge-base-en-v15-768__v1");
                    seedDocument(su, "t1", "1.1.302", "knowledge keep doc", "knowledge__h044subject__bge-base-en-v15-768__v1");

                    assertThat(dsl(su).fetchCount(CHASH_INDEX_RETIRED))
                        .as("superuser ground truth after seeding").isEqualTo(5);
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.COLLECTION.isNull()))
                        .as("superuser ground truth after seeding — 2 content-backed "
                            + "(1.1.100) + 1 deliberately content-less (1.1.101, the "
                            + "dangling-row DELETE proof)")
                        .isEqualTo(3);
                }

                // ── Lock in the 1wjmq mechanism itself: FORCE RLS hides EVERY
                // seeded row from the NOBYPASSRLS owner the HEAD leg runs as.
                // If this ever starts seeing rows, the data leg has silently
                // stopped testing what it exists to test. ────────────────────────
                try (Connection admin = adminDs.getConnection()) {
                    assertThat(dsl(admin).fetchCount(CHASH_INDEX_RETIRED))
                        .as("FORCE RLS must hide all chash_index rows from the non-BYPASSRLS owner")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(CATALOG_DOCUMENT_CHUNKS))
                        .as("FORCE RLS must hide all manifest rows from the non-BYPASSRLS owner")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(CATALOG_DOCUMENTS))
                        .as("FORCE RLS must hide the seeded document from the non-BYPASSRLS owner "
                            + "— the join side of catalog-014-0's stamp, the both-tables lesson")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(CATALOG_LINKS))
                        .as("FORCE RLS must hide both seeded catalog_links rows from the "
                            + "non-BYPASSRLS owner — catalog-032-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(FRECENCY))
                        .as("FORCE RLS must hide all four seeded frecency rows from the "
                            + "non-BYPASSRLS owner — legacy-001-1's own toggle-wrap target "
                            + "(2 rows) plus telemetry-006-1's own toggle-wrap target (2 more)")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(RELEVANCE_LOG))
                        .as("FORCE RLS must hide both seeded relevance_log rows from the "
                            + "non-BYPASSRLS owner — legacy-001-2's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(TOPIC_ASSIGNMENTS))
                        .as("FORCE RLS must hide all five seeded topic_assignments rows from "
                            + "the non-BYPASSRLS owner — taxonomy-010-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(MEMORY))
                        .as("FORCE RLS must hide both seeded memory rows from the "
                            + "non-BYPASSRLS owner — memory-003-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(PLANS))
                        .as("FORCE RLS must hide both seeded plans rows from the "
                            + "non-BYPASSRLS owner — plans-003-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(dsl(admin).fetchCount(DOCUMENT_ASPECTS))
                        .as("FORCE RLS must hide the seeded document_aspects row from the "
                            + "non-BYPASSRLS owner — aspects-004-1's own toggle-wrap target")
                        .isEqualTo(0);
                }

                // ── catalog-014-0 checkpoint (nexus-iu7hp): the stamp can only be
                // observed here, right after 014-0. Later in the hop catalog-025-0
                // deletes any manifest row still collection NULL, and hygiene-001-5/-8
                // delete every legacy-width (16-byte) row, which every row seeded at
                // OLD_TAG is. So at HEAD a neutered 014-0 and a working one leave the
                // same final state; here, a neutered one leaves all three rows NULL. ──
                migrateUpTo(adminDs, "catalog-015-0");
                try (Connection su = pg.createConnection("")) {
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq("t1")
                                .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq("code__x"))))
                        .as("catalog-014-0 must stamp all three seeded manifest rows (1.1.100 x2, "
                            + "1.1.101) with code__x under FORCE-RLS; fewer means its toggle-wrapped "
                            + "manifest_backfill() no-oped for the NOBYPASSRLS owner")
                        .isEqualTo(3);
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.COLLECTION.isNull()))
                        .as("no manifest row may remain un-stamped right after catalog-014-0")
                        .isEqualTo(0);
                }

                // ── nexus-r7xao seed-coverage follow-up: tuples-003-2's target table
                // (nexus.tuples) does not exist until PARTWAY through this SAME hop,
                // unlike every table seeded above -- migrate up to just before
                // tuples-003-2, seed its input rows directly (see migrateUpTo's own
                // javadoc for why this is safe to resume from), THEN let the normal
                // whole-hop migrate below finish tuples-003-2 through the rest of
                // HEAD exactly as it would have run in one shot. ────────────────────
                byte[] tuplesOversizedId = HexFormat.of()
                    .parseHex("c0ffee0000000000000000000000000000000000000000000000000000f00d");
                byte[] tuplesAtCapId = HexFormat.of()
                    .parseHex("c0ffee1111111111111111111111111111111111111111111111111111f00d");
                String tuplesTenant = "tuples-rehearsal-tenant";
                String tuplesLegacyClaimId = "rehearsal-legacy-claim-id";
                migrateUpTo(adminDs, "tuples-003-2");
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seedTupleBodySizeLimitRows(su, tuplesTenant, tuplesOversizedId, tuplesAtCapId,
                        tuplesLegacyClaimId);
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThat(dsl(admin).fetchCount(TUPLES))
                        .as("FORCE RLS must hide both seeded nexus.tuples rows from the "
                            + "non-BYPASSRLS owner -- tuples-003-2's own toggle-wrap target")
                        .isEqualTo(0);
                }

                // ── nexus-8zoyp seed-coverage follow-up: tuples-004-1's UPDATE
                // (nulling body on already-consumed rows) -- same shape as
                // tuples-003-2's own follow-up above, one changeset further --
                // migrate up to just before tuples-004-1 (this also applies
                // tuples-003-2/3/4, still pending from the migrateUpTo call
                // above, so chk_tuples_body_size already exists and is
                // validated by this point), seed a consumed row WITH a body and
                // an unconsumed row WITH a body directly (both well under the
                // 4096-byte cap), THEN let the normal whole-hop migrate below
                // finish tuples-004-1 through the rest of HEAD exactly as it
                // would have run in one shot. ──────────────────────────────────
                byte[] tuplesConsumedId = HexFormat.of()
                    .parseHex("c0ffee4444444444444444444444444444444444444444444444444444f00d");
                byte[] tuplesUnconsumedId = HexFormat.of()
                    .parseHex("c0ffee5555555555555555555555555555555555555555555555555555f00d");
                migrateUpTo(adminDs, "tuples-004-1");
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seedTupleConsumedBodyCleanupRows(su, tuplesTenant, tuplesConsumedId, tuplesUnconsumedId);
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThat(dsl(admin).fetchCount(TUPLES))
                        .as("FORCE RLS must hide every seeded nexus.tuples row from the "
                            + "non-BYPASSRLS owner -- tuples-004-1's own toggle-wrap target")
                        .isEqualTo(0);
                }

                // ── nexus-edjmu seed-coverage follow-up: pipeline-002-2's backfill
                // (pdf_pages/pdf_chunks.pipeline_id from the owning pdf_pipeline
                // row by (tenant_id, content_hash), then DELETE of parentless WAL
                // rows, then DROP COLUMN content_hash) -- the pipeline tables do
                // not exist at OLD_TAG (pipeline-001 landed at v0.1.47), so
                // migrate up to just before pipeline-002-1 (this also applies
                // tuples-004-1 through tuples-007, still pending from the
                // migrateUpTo call above), seed the pipeline-001 shape directly:
                // two tenants sharing one content_hash (each WAL row must attach
                // to ITS tenant's parent, never the other's) plus one parentless
                // WAL family, THEN let the whole-hop migrate below finish
                // pipeline-002-1/2/3 through the rest of HEAD. ──────────────────
                migrateUpTo(adminDs, "pipeline-002-1");
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seedPipelinePreRowIdentityRows(su);
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThat(DSL.using(admin, SQLDialect.POSTGRES).fetchCount(PIPELINE_PAGES))
                        .as("FORCE RLS must hide every seeded nexus.pdf_pages row from the "
                            + "non-BYPASSRLS owner -- pipeline-002-2's own toggle-wrap target")
                        .isEqualTo(0);
                }

                // ── nexus-0rxvg seed-coverage follow-up: hygiene-008-2 corrects
                // quarantine siblings the catalog-037-1 bounded sweep registered
                // by parsing the name (content_type 'quarantine-<ct>', dimension
                // NULL). That shape only arises from a runtime call, never from a
                // changeset, and hygiene-005-3 earlier in this same hop corrects
                // any such row seeded at OLD_TAG, so the rows are seeded here,
                // after hygiene-005-3 and just before hygiene-008-2 (this also
                // applies pipeline-002-1 onward through hygiene-008-1), THEN the
                // whole-hop migrate below runs hygiene-008-2. ─────────────────────
                migrateUpTo(adminDs, "hygiene-008-2");
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    seedBoundedQuarantineMisregistrations(su);
                }

                // ── HEAD LEG over a populated database. This is the leg the
                // v0.1.33 outage proved was untested: catalog-013-0's naked DML
                // no-ops under RLS here exactly as it did in production; only
                // 013-1b's toggle-wrapped re-run makes 013-2's VALIDATE pass.
                // catalog-014-0's stamp likewise only works because BOTH its
                // tables are toggled. ────────────────────────────────────────────
                assertThatCode(() -> SchemaMigrator.migrate(adminDs))
                    .as("the old-tag->HEAD hop over a DATA-BEARING old leg must complete: every "
                        + "row-DML changeset in the hop must actually take effect for the "
                        + "NOBYPASSRLS owner, not silently no-op into a failing backstop")
                    .doesNotThrowAnyException();

                // ── hygiene-008-2's own effect (nexus-0rxvg): the prefix-shaped
                // sibling with a registered origin is re-filed from it; the one
                // with no origin has only its literal prefix stripped, and its
                // lifecycle_state is left alone. ───────────────────────────────────
                try (Connection su = pg.createConnection("")) {
                    var h008 = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(h008.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("quarantine-code__rxvg1__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("code"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("rxvg1-origin-owner"))
                            .and(CATALOG_COLLECTIONS.MODEL_VERSION.eq("v4"))
                            .and(CATALOG_COLLECTIONS.DIMENSION.eq(768))
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("quarantine"))))
                        .as("hygiene-008-2 must re-file the misregistered prefix-shaped sibling "
                            + "from its registered origin's row, under FORCE RLS for the "
                            + "NOBYPASSRLS owner")
                        .isEqualTo(1);
                    assertThat(h008.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("quarantine-docs__rxvg2__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("docs"))
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("disputed"))))
                        .as("hygiene-008-2 with no registered origin strips only the literal "
                            + "quarantine- prefix and never touches lifecycle_state")
                        .isEqualTo(1);
                }

                // ── catalog-044-3's own effect (nexus-6pbwx): the slug-owned code
                // collection takes its document's owner segment, the knowledge
                // collection keeps its subject, both under FORCE RLS for the
                // NOBYPASSRLS owner. ────────────────────────────────────────────────
                try (Connection su = pg.createConnection("")) {
                    var c044 = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(c044.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("code__h044slug__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("1-15"))))
                        .as("catalog-044-3 must re-derive the slug owner from the document's tumbler")
                        .isEqualTo(1);
                    assertThat(c044.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("knowledge__h044subject__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("h044subject"))))
                        .as("a knowledge collection keeps its subject")
                        .isEqualTo(1);
                }

                // ── tuples-003-2's own effect, mirroring TuplesBaselineSchemaLiquibaseTest's
                // dedicated Liquibase-level proof: the over-cap row is gone, the at-cap
                // row survives untouched, the referencing claim-log row survives with
                // tuple_id nulled (tuple_claim_log_tuple_fk's ON DELETE SET NULL), and
                // chk_tuples_body_size ends VALIDATED. ───────────────────────────────
                try (Connection su = pg.createConnection("")) {
                    var ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(ctx.fetchExists(ctx.selectFrom(
                        dev.nexus.service.jooq.nexus.Tables.TUPLES)
                        .where(dev.nexus.service.jooq.nexus.Tables.TUPLES.ID.eq(tuplesOversizedId))))
                        .as("the legacy oversized row must be deleted by tuples-003-2")
                        .isFalse();

                    var atCapRow = ctx.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLES)
                        .where(dev.nexus.service.jooq.nexus.Tables.TUPLES.ID.eq(tuplesAtCapId))
                        .fetchOne();
                    assertThat(atCapRow).as("the at-cap row must survive tuples-003-2 untouched").isNotNull();
                    assertThat(atCapRow.getBody()).hasSize(4096);

                    var tupleLogRow = ctx.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG)
                        .where(dev.nexus.service.jooq.nexus.Tables.TUPLE_CLAIM_LOG.CLAIM_ID.eq(tuplesLegacyClaimId))
                        .fetchOne();
                    assertThat(tupleLogRow)
                        .as("the referencing tuple_claim_log row must survive the tuple's deletion")
                        .isNotNull();
                    assertThat(tupleLogRow.getTupleId())
                        .as("tuple_claim_log_tuple_fk's ON DELETE SET NULL must have fired")
                        .isNull();

                    assertThat(PgCatalogProbes.constraintExists(ctx, "chk_tuples_body_size")).isTrue();
                    assertThat(PgCatalogProbes.constraintValidated(ctx, "chk_tuples_body_size"))
                        .as("VALIDATE must succeed once tuples-003-2 has removed the only violator")
                        .isTrue();
                }

                // ── tuples-004-1's own effect: the pre-existing consumed row's body
                // is nulled, the pre-existing unconsumed row's body survives
                // untouched. ─────────────────────────────────────────────────────
                try (Connection su = pg.createConnection("")) {
                    var ctx = DSL.using(su, SQLDialect.POSTGRES);
                    var consumedRow = ctx.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLES)
                        .where(dev.nexus.service.jooq.nexus.Tables.TUPLES.ID.eq(tuplesConsumedId))
                        .fetchOne();
                    assertThat(consumedRow)
                        .as("the pre-existing consumed row must survive tuples-004-1").isNotNull();
                    assertThat(consumedRow.getBody())
                        .as("tuples-004-1 must null the body of an already-consumed row")
                        .isNull();

                    var unconsumedRow = ctx.selectFrom(dev.nexus.service.jooq.nexus.Tables.TUPLES)
                        .where(dev.nexus.service.jooq.nexus.Tables.TUPLES.ID.eq(tuplesUnconsumedId))
                        .fetchOne();
                    assertThat(unconsumedRow)
                        .as("the pre-existing unconsumed row must survive tuples-004-1 untouched")
                        .isNotNull();
                    assertThat(unconsumedRow.getBody())
                        .as("tuples-004-1 must leave an unconsumed row's body intact")
                        .isEqualTo("still-here");
                }

                // ── pipeline-002-2's own effect: every surviving WAL row carries
                // the pipeline_id of the pdf_pipeline row with ITS tenant and
                // content_hash; the parentless family is gone; content_hash is
                // no longer a column of either WAL table. A toggle-less backfill
                // would have matched zero rows for the NOBYPASSRLS owner and the
                // SET NOT NULL that follows would have failed the hop above.
                try (Connection su = pg.createConnection("")) {
                    var ctx = DSL.using(su, SQLDialect.POSTGRES);
                    for (var table : List.of(PIPELINE_PAGES, PIPELINE_CHUNKS)) {
                        assertThat(ctx.meta().getTables(table.getQualifiedName()).get(0).field("content_hash"))
                            .as(table + ": pipeline-002-2 drops content_hash from the WAL tables").isNull();
                        assertThat(ctx.fetchCount(table))
                            .as(table + ": two parents x two rows survive, the parentless family is gone")
                            .isEqualTo(4);
                        assertThat(ctx.selectCount()
                                .from(table.as("w"))
                                .join(PIPELINE_ROWS.as("p"))
                                .on(DSL.field(DSL.name("p", "tenant_id"), String.class)
                                        .eq(DSL.field(DSL.name("w", "tenant_id"), String.class))
                                    .and(DSL.field(DSL.name("p", "pipeline_id"), Long.class)
                                        .eq(DSL.field(DSL.name("w", "pipeline_id"), Long.class))))
                                .where(DSL.field(DSL.name("p", "content_hash"), String.class).eq(PIPELINE_SHARED_HASH)
                                    .and(DSL.field(DSL.name("p", "keyed_by"), String.class).eq("content_hash")))
                                .fetchOne(0, int.class))
                            .as(table + ": every survivor attaches to its own tenant's parent")
                            .isEqualTo(4);
                    }
                    assertThat(ctx.select(DSL.countDistinct(DSL.field(DSL.name("pipeline_id"), Long.class)))
                            .from(PIPELINE_CHUNKS).fetchOne(0, int.class))
                        .as("the two tenants' WAL rows attach to two DISTINCT parents").isEqualTo(2);
                }

                // ── Ground truth as superuser: the DML took EFFECT (rows changed),
                // not merely "the migration didn't crash". ───────────────────────
                try (Connection su = pg.createConnection("")) {
                    // catalog-013 (013-1b) leg STILL RUNS on this hop (it
                    // precedes rdr180 in the same update): normalized +
                    // deduped on the text schema, THEN rdr180-7 converted
                    // the column to bytea (32-hex decodes to 16 bytes — the
                    // mid-migration legacy state the /v1/remap/rekey rung
                    // later rekeys). Composition proof:
                    // RDR-187 (nexus-piwya.9): the router died at the DROP at
                    // the END of this same hop — the 013-1b dedupe and the
                    // rdr180-7 conversion still executed EN ROUTE (the
                    // doesNotThrowAnyException above carries the proof: had
                    // the RLS-blind DML no-op'd, 013-2's VALIDATE would have
                    // crashed on the 64-char rows), and their end product was
                    // then dropped with the table. Post-hop observability of
                    // the DML-took-effect property rides the MANIFEST witness
                    // below.
                    assertThat((PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), "nexus", "chash_index") ? 1 : 0))
                        .as("chash_index gone at HEAD (rdr187-2)")
                        .isEqualTo(0);
                    assertThat(PgCatalogProbes.constraintCountLike(DSL.using(su, SQLDialect.POSTGRES), "chash_index_chash%"))
                        .as("its constraints died with it")
                        .isEqualTo(0);

                    // catalog-014-0 leg: every seeded manifest row stamped with the
                    // owning document's physical_collection, none left NULL, and
                    // (now that the fixture backs each row with real content) still
                    // present after catalog-025's dangling-row cleanup runs EARLIER
                    // in this same hop -- but hygiene-001-5/-8 (nexus-tk070.p6a
                    // follow-on, review round: the delete predicate was corrected
                    // to key directly on octet_length(chash), not on a
                    // metadata/chunk_index NULL proxy) now ALSO runs in this same
                    // migrate() call, LATER, and unconditionally deletes every
                    // legacy-width (16-byte, un-rekeyed) row regardless of stamp
                    // or content -- both seeded chashes ("1"x32, "2"x32) are
                    // exactly that. So the manifest_backfill()/catalog-025
                    // survival this assertion used to pin is real but no longer
                    // observable AT HEAD in a single full-hop migrate() call; it
                    // remains directly observable at the intermediate checkpoint
                    // this comment describes (proven by the two isEqualTo(0)
                    // assertions below, which are the fact that survives to HEAD).
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq("code__x")))
                        .as("both 1.1.100 manifest rows are un-rekeyed legacy-width "
                            + "(16-byte) chash -- hygiene-001-5's FK-safety cleanup "
                            + "removes them (ahead of the chunks parent DELETE) by the "
                            + "time this full hop reaches HEAD")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.COLLECTION.isNull()))
                        .as("no manifest row may be un-stamped at HEAD (catalog-025-0 deletes NULL "
                            + "rows, so this cannot tell a working catalog-014-0 from a neutered one; "
                            + "the catalog-015-0 checkpoint above does)")
                        .isEqualTo(0);

                    // catalog-025-0 leg (nexus-j862l, seed-coverage lint follow-up): the
                    // DELETE arm actually fired under FORCE-RLS. 1.1.101's row was
                    // stamped 'code__x' by catalog-014-0's backfill too (same mechanism
                    // just proven above for 1.1.100) but carries no matching chunks_384
                    // content, so it must be GONE entirely by now -- not merely
                    // un-stamped (the NULL-count assertion above already proves no row
                    // anywhere is left NULL) and not merely absent from the 'code__x'
                    // count above (which only distinguishes 1.1.100's rows from
                    // everything else). This is the one assertion in the whole leg that
                    // would fail if catalog-025-0's DELETE silently no-op'd under
                    // FORCE-RLS (the exact failure class nexus-1wjmq/nexus-php10 exist
                    // to catch) while the KEEP-arm assertions above stayed green.
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq("t1")
                                .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("1.1.101"))))
                        .as("catalog-025-0's dangling-row DELETE must actually remove a "
                            + "correctly-stamped-but-content-less row under FORCE-RLS, "
                            + "proving the DELETE arm fires, not just the KEEP arm")
                        .isEqualTo(0);

                    // catalog-016-0 leg: the dedup backfill saw the seeded rows
                    // through its FORCE-RLS toggle and tombstoned the loser —
                    // exactly one LIVE row per (tenant, source_uri) survives,
                    // and it is the most-chunk-bearing winner.
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENTS,
                            CATALOG_DOCUMENTS.SOURCE_URI.eq("file:///seed/dup.md")
                                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())))
                        .as("catalog-016-0 must tombstone the duplicate-uri loser "
                            + "(an RLS-blind no-op would leave 2 live rows and fail "
                            + "016-1's unique-index creation)")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENTS,
                            CATALOG_DOCUMENTS.TUMBLER.eq("1.1.202")
                                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())))
                        .as("the most-chunk-bearing row must be the surviving winner")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENTS,
                            CATALOG_DOCUMENTS.TUMBLER.eq("1.1.201")
                                .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())))
                        .as("the loser must be TOMBSTONED, never hard-deleted")
                        .isEqualTo(1);
                    assertThat((PgCatalogProbes.indexExists(DSL.using(su, SQLDialect.POSTGRES), "nexus", "ux_catalog_documents_live_source_uri") ? 1 : 0))
                        .as("catalog-016-1's partial unique index exists at HEAD")
                        .isEqualTo(1);

                    // aspects-004-1 leg (nexus-ubnwk, RDR-156 Decision 5): the
                    // seeded legacy NULL-doc_id row must be stamped with the
                    // matching catalog document's tumbler under FORCE-RLS —
                    // the exact silent-no-op hazard the changeset's own toggle
                    // exists to defeat (nexus_admin is NOSUPERUSER/NOBYPASSRLS
                    // and owns both FORCE-RLS tables; no nexus.tenant GUC is
                    // set at migration time, so an untoggled UPDATE would
                    // match zero rows for every tenant, silently).
                    List<String> ubnwkDocIds = dsl(su).select(DOCUMENT_ASPECTS.DOC_ID).from(DOCUMENT_ASPECTS)
                        .where(DOCUMENT_ASPECTS.TENANT_ID.eq("t1")
                            .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("seed/ubnwk-aspect.md")))
                        .fetch(DOCUMENT_ASPECTS.DOC_ID);
                    assertThat(ubnwkDocIds).isNotEmpty();
                    assertThat(ubnwkDocIds.get(0))
                        .as("aspects-004-1 must stamp doc_id = the matching catalog "
                            + "document's tumbler under FORCE-RLS, not silently no-op")
                        .isEqualTo("1.1.204");
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.document_aspects", "nexus.catalog_documents"))
                        .as("aspects-004-1 must RESTORE FORCE ROW LEVEL SECURITY on both "
                            + "toggled tables within its own changeset")
                        .isEqualTo(2);

                    // Both fixes must RESTORE FORCE within their own changeset.
                    // (chash_index left the toggled-set observation with the
                    // DROP — RDR-187; the two surviving toggled tables pin it.)
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.catalog_document_chunks", "nexus.catalog_documents"))
                        .as("FORCE ROW LEVEL SECURITY restored on every toggled surviving table")
                        .isEqualTo(2);

                    // vectors-004-1 leg (nexus-o8dil.12): the straddling
                    // per-dim content seeded above landed in the unified
                    // nexus.chunks under the CORRECT typed embedding_<dim>
                    // column -- proving the changeset's NOBYPASSRLS-owner
                    // copy actually saw and moved rows for EVERY dim, not
                    // merely completed without throwing. chunks_384's rows
                    // (the j862l seed, collection code__x) prove the same for
                    // that dim without a fresh seed.
                    //
                    // hygiene-001-5 (nexus-tk070.p6a follow-on, review round)
                    // runs LATER in this SAME migrate() call and unconditionally
                    // deletes every legacy-width (16-byte, un-rekeyed) chunks
                    // row -- all four dim-seeded chashes here ("1"/"2"/"6"/"7"
                    // x32) are exactly that, so by the time this full hop
                    // reaches HEAD every one of them is gone, regardless of
                    // which typed embedding column vectors-004-1 correctly
                    // placed them under moments earlier in the same walk. The
                    // isEqualTo(0) below is the fact that survives to HEAD; the
                    // per-dim PLACEMENT property these comments describe is
                    // real and was true at the intermediate point vectors-004-1
                    // left it, just not independently observable from a single
                    // full-hop migrate() call once a later, unrelated cleanup
                    // also touches the same rows.
                    assertThat(dsl(su).fetchCount(CHUNKS,
                            CHUNKS.TENANT_ID.eq("t1")
                                .and(CHUNKS.COLLECTION.eq("code__x"))
                                .and(CHUNKS.EMBEDDING_384.isNotNull())
                                .and(CHUNKS.EMBEDDING_768.isNull())
                                .and(CHUNKS.EMBEDDING_1024.isNull())))
                        .as("the two j862l chunks_384 rows are legacy-width chash -- "
                            + "hygiene-001-5 deletes them by HEAD")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(CHUNKS,
                            CHUNKS.TENANT_ID.eq("t1")
                                .and(CHUNKS.COLLECTION.eq("code__y"))
                                .and(CHUNKS.EMBEDDING_768.isNotNull())
                                .and(CHUNKS.EMBEDDING_384.isNull())
                                .and(CHUNKS.EMBEDDING_1024.isNull())))
                        .as("the seeded chunks_768 row is legacy-width chash -- "
                            + "hygiene-001-5 deletes it by HEAD")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(CHUNKS,
                            CHUNKS.TENANT_ID.eq("t2")
                                .and(CHUNKS.COLLECTION.eq("code__z"))
                                .and(CHUNKS.EMBEDDING_1024.isNotNull())
                                .and(CHUNKS.EMBEDDING_384.isNull())
                                .and(CHUNKS.EMBEDDING_768.isNull())))
                        .as("the seeded chunks_1024 row is legacy-width chash -- "
                            + "hygiene-001-5 deletes it by HEAD")
                        .isEqualTo(0);
                    for (String t : new String[] {"chunks_384", "chunks_768", "chunks_1024"}) {
                        assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), "nexus", t))
                            .as(t + " gone at HEAD (vectors-004-1 unify, same old-tag->HEAD hop)")
                            .isFalse();
                    }

                    // taxonomy-007-1 leg (nexus-jv3ue): the analogous straddle
                    // for nexus.taxonomy_centroids, all three dims fresh-seeded
                    // above (no chunks-style j862l reuse available here).
                    assertThat(dsl(su).fetchCount(TAXONOMY_CENTROIDS,
                            TAXONOMY_CENTROIDS.TENANT_ID.eq("t1")
                                .and(TAXONOMY_CENTROIDS.COLLECTION.eq("code__cx__minilm-l6-v2-384__v1"))
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_384.isNotNull())
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_768.isNull())
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_1024.isNull())))
                        .as("the seeded taxonomy_centroids_384 row landed under embedding_384 only")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(TAXONOMY_CENTROIDS,
                            TAXONOMY_CENTROIDS.TENANT_ID.eq("t1")
                                .and(TAXONOMY_CENTROIDS.COLLECTION.eq("code__y"))
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_768.isNotNull())
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_384.isNull())
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_1024.isNull())))
                        .as("the seeded taxonomy_centroids_768 row landed under embedding_768 only")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(TAXONOMY_CENTROIDS,
                            TAXONOMY_CENTROIDS.TENANT_ID.eq("t2")
                                .and(TAXONOMY_CENTROIDS.COLLECTION.eq("code__cz__voyage-code-3__v1"))
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_1024.isNotNull())
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_384.isNull())
                                .and(TAXONOMY_CENTROIDS.EMBEDDING_768.isNull())))
                        .as("the seeded taxonomy_centroids_1024 row landed under embedding_1024 only")
                        .isEqualTo(1);
                    for (String t : new String[] {
                            "taxonomy_centroids_384", "taxonomy_centroids_768", "taxonomy_centroids_1024"}) {
                        assertThat(PgCatalogProbes.tableExists(DSL.using(su, SQLDialect.POSTGRES), "nexus", t))
                            .as(t + " gone at HEAD (taxonomy-007-1 unify, same old-tag->HEAD hop)")
                            .isFalse();
                    }

                    // Contrast pin vs the schema-divergence test: with no injected
                    // divergence all five constraints exist, so catalog-013-2's
                    // precondition passes and it EXECUTES (not MARK_RAN).
                    assertThat(changesetExecType(su, "catalog-013-2", "nexus-e0hd2"))
                        .as("with all five constraints present, catalog-013-2 must EXECUTE for real")
                        .isEqualTo("EXECUTED");

                    // catalog-029-1 leg (nexus-o8dil.29, RDR-191 Phase 5, seed-coverage
                    // lint follow-up): fk_catalog_chunks_chunk's own NO FORCE/FORCE
                    // toggle around the anti-join DELETE (catalog-029-1) is the SAME
                    // toggle-wrap shape catalog-013-1b/catalog-014-0/catalog-025-0
                    // already prove work under the real NOBYPASSRLS hop -- this
                    // reuses the 1.1.100 KEEP-arm fixture already seeded above for
                    // catalog-014-0/catalog-025-0 rather than adding a fresh dangling
                    // row of its own: a manifest row that would be dangling by
                    // catalog-029-1's (tenant, collection, chash) anti-join is, by
                    // construction, ALSO dangling under catalog-025-0's structurally
                    // identical anti-join (against the pre-unify chunks_384/768/1024
                    // tables that vectors-004-1 copies verbatim into nexus.chunks a
                    // few changesets later in this SAME hop) -- so any row that would
                    // exercise catalog-029-1's DELETE arm is necessarily removed by
                    // catalog-025-0 first, and a fresh dangling seed here would be
                    // dead by the time catalog-029-1 runs. The KEEP-arm proof is the
                    // one this hop CAN carry end to end: if the toggle silently failed
                    // to turn FORCE off (the nexus-1wjmq/nexus-php10 failure class),
                    // the anti-join would see zero rows in nexus.chunks and WRONGLY
                    // delete the two content-backed 1.1.100 rows too (over-delete),
                    // and/or catalog-029-2's VALIDATE would fail against whatever
                    // survived -- either failure mode was caught below.
                    //
                    // COVERAGE NOTE (nexus-tk070.p6a follow-on, review round):
                    // hygiene-001-5 now ALSO deletes these same two rows, later in
                    // this same migrate() call (see the catalog-014-0/catalog-025-0
                    // comment above for the full rationale) -- so a working
                    // catalog-029-1 toggle (count stays 2, then hygiene-001-5 takes
                    // it to 0) and a BROKEN one (over-delete takes it to 0 directly)
                    // now converge on the SAME final count at HEAD, and the count
                    // assertion below can no longer distinguish them from a single
                    // full-hop migrate() call. catalog-029-1's toggle-correctness
                    // mechanism itself stays covered independently by
                    // ManifestChunkFkTest; the constraintExists/constraintValidated
                    // checks above still hold as a (weaker, non-distinguishing)
                    // sanity check.
                    assertThat(constraintExists(su, "fk_catalog_chunks_chunk"))
                        .as("fk_catalog_chunks_chunk must exist at HEAD (catalog-029-0)")
                        .isTrue();
                    assertThat(constraintValidated(su, "fk_catalog_chunks_chunk"))
                        .as("fk_catalog_chunks_chunk must be VALIDATED at HEAD -- catalog-029-2's "
                            + "VALIDATE only succeeds if catalog-029-1's anti-join DELETE actually "
                            + "ran (toggle correctly off) and left no dangling row behind")
                        .isTrue();
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENT_CHUNKS,
                            CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq("t1")
                                .and(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("1.1.100"))))
                        .as("the two 1.1.100 manifest rows are legacy-width chash -- "
                            + "hygiene-001-5 deletes them by HEAD regardless of "
                            + "catalog-029-1's own (separately covered) toggle outcome")
                        .isEqualTo(0);

                    // hygiene-001-1: the DELETE-arm (doc_id NULL, unconvertible by
                    // aspects-004-1) row must be gone; the UPDATE-arm row's
                    // pre-existing doc_id must be untouched and its NULL source_uri
                    // backfilled to ''.
                    assertThat(dsl(su).fetchCount(DOCUMENT_ASPECTS,
                            DOCUMENT_ASPECTS.TENANT_ID.eq("t1")
                                .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("seed/hygiene1-orphan.md"))))
                        .as("hygiene-001-1's DELETE WHERE doc_id IS NULL must remove the "
                            + "unconvertible orphan row")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(DOCUMENT_ASPECTS,
                            DOCUMENT_ASPECTS.TENANT_ID.eq("t1")
                                .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("seed/hygiene1-legit.md"))
                                .and(DOCUMENT_ASPECTS.DOC_ID.eq("1.1.100"))
                                .and(DOCUMENT_ASPECTS.SOURCE_URI.eq(""))))
                        .as("hygiene-001-1's UPDATE must backfill the legit row's NULL "
                            + "source_uri to '', leaving its pre-existing doc_id untouched")
                        .isEqualTo(1);

                    // hygiene-001-2: the doc_id-NULL queue row must be gone.
                    assertThat(dsl(su).fetchCount(ASPECT_EXTRACTION_QUEUE,
                            ASPECT_EXTRACTION_QUEUE.TENANT_ID.eq("t1")
                                .and(ASPECT_EXTRACTION_QUEUE.SOURCE_PATH.eq("seed/hygiene2-orphan.md"))))
                        .as("hygiene-001-2's DELETE WHERE doc_id IS NULL must remove the "
                            + "orphan queue row")
                        .isEqualTo(0);

                    // hygiene-001-3: the source_uri-and-collection-NULL highlight row
                    // must be gone.
                    assertThat(dsl(su).fetchCount(DOCUMENT_HIGHLIGHTS,
                            DOCUMENT_HIGHLIGHTS.TENANT_ID.eq("t1")
                                .and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq("1.1.101"))))
                        .as("hygiene-001-3's DELETE WHERE source_uri IS NULL OR collection "
                            + "IS NULL must remove the row")
                        .isEqualTo(0);

                    // hygiene-001-4: the 1.1.100 catalog_documents row (seeded above
                    // with only tenant_id/tumbler/title/physical_collection) must have
                    // its other four columns backfilled to their DEFAULT ''/0.
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENTS,
                            CATALOG_DOCUMENTS.TENANT_ID.eq("t1")
                                .and(CATALOG_DOCUMENTS.TUMBLER.eq("1.1.100"))
                                .and(CATALOG_DOCUMENTS.CONTENT_TYPE.eq(""))
                                .and(CATALOG_DOCUMENTS.CORPUS.eq(""))
                                .and(CATALOG_DOCUMENTS.FILE_PATH.eq(""))
                                .and(CATALOG_DOCUMENTS.CHUNK_COUNT.eq(0))))
                        .as("hygiene-001-4 must backfill catalog_documents' NULL "
                            + "content_type/corpus/file_path/chunk_count to ''/0")
                        .isEqualTo(1);

                    // hygiene-001-6: the code__x catalog_collections row (registered
                    // above via registerCollection, which never sets created_at) must
                    // end up with a non-null created_at.
                    assertThat(dsl(su).fetchCount(CATALOG_COLLECTIONS,
                            CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                                .and(CATALOG_COLLECTIONS.NAME.eq("code__x"))
                                .and(CATALOG_COLLECTIONS.CREATED_AT.isNotNull())))
                        .as("hygiene-001-6 must backfill catalog_collections.created_at, "
                            + "never leave it NULL")
                        .isEqualTo(1);

                    // hygiene-001-7: the 1.1.100->1.1.101 catalog_links row (seeded via
                    // seedLink, which never sets created_at) must end up non-null.
                    assertThat(dsl(su).fetchCount(CATALOG_LINKS,
                            CATALOG_LINKS.TENANT_ID.eq("t1")
                                .and(CATALOG_LINKS.FROM_TUMBLER.eq("1.1.100"))
                                .and(CATALOG_LINKS.TO_TUMBLER.eq("1.1.101"))
                                .and(CATALOG_LINKS.CREATED_AT.isNotNull())))
                        .as("hygiene-001-7 must backfill catalog_links.created_at to "
                            + "now(), never leave it NULL")
                        .isEqualTo(1);

                    // hygiene-001-9: the canonical-width ("f"x64) relevance_log row
                    // (legacy-001-2's own KEEP-arm seed, collection never set) must end
                    // up with collection = ''.
                    assertThat(dsl(su).fetchCount(RELEVANCE_LOG,
                            RELEVANCE_LOG.TENANT_ID.eq("t1")
                                .and(RELEVANCE_LOG.CHUNK_ID.eq("f".repeat(64)))
                                .and(RELEVANCE_LOG.COLLECTION.eq(""))))
                        .as("hygiene-001-9 must backfill relevance_log.collection to '', "
                            + "never leave it NULL")
                        .isEqualTo(1);

                    // hygiene-001-9b: the dedicated code__hyg9b taxonomy_centroids row
                    // (label=null at seed time) must end up with label = ''.
                    // RDR-225: code__hyg9b has no registry row, so the walk does not copy its centroid (derived
                    // data, taxonomy rebuilds it); it stays in the retired table, which still proves the backfill.
                    assertThat(dsl(su).fetchCount(CENTROIDS_RETIRED_225,
                            RETIRED_TENANT_ID.eq("t1").and(RETIRED_COLLECTION.eq("code__hyg9b"))
                                .and(RETIRED_TOPIC_ID.eq(999L)).and(RETIRED_LABEL.eq(""))))
                        .as("hygiene-001-9b must backfill taxonomy_centroids.label to "
                            + "'', never leave it NULL")
                        .isEqualTo(1);

                    // hygiene-001-10/-11: both dedicated plans rows must be gone --
                    // -10's expired-but-non-null-verb row via the TTL catch-up sweep,
                    // -11's permanent-but-verb-null row via the verb backstop -- each
                    // independent of the other changeset's own predicate.
                    assertThat(dsl(su).fetchCount(PLANS,
                            PLANS.TENANT_ID.eq("t1")
                                .and(PLANS.QUERY.eq("hygiene10 expired query"))))
                        .as("hygiene-001-10's TTL catch-up sweep must remove the expired, "
                            + "non-null-verb plan row")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(PLANS,
                            PLANS.TENANT_ID.eq("t1")
                                .and(PLANS.QUERY.eq("hygiene11 verbless query"))))
                        .as("hygiene-001-11's verb backstop must remove the permanent, "
                            + "verb-null plan row")
                        .isEqualTo(0);

                    // fk-004-0-reconcile-precount leg (nexus-iq0qr, RDR-191 Phase 5
                    // follow-up, seed-coverage lint follow-up): this READ-ONLY audit
                    // changeset runs BEFORE fk-004-1-reconcile in file order, in this
                    // SAME walk, and RAISE NOTICEs the anti-join pre-count of
                    // unregistered (tenant_id, collection) pairs fk-004-1-reconcile is
                    // about to insert. By the identical structural reasoning as the
                    // fk-004-1-reconcile leg immediately below (fk-002, already applied
                    // at OLD_TAG, enforces collection registration on every per-dim
                    // chunk write from before this hop even starts, and vectors-004-1
                    // only COPIES already-FK-compliant rows into nexus.chunks) this
                    // pre-count is ALWAYS zero in this hop too -- an
                    // observation-count assertion on it would prove nothing (a broken
                    // toggle would ALSO see zero, indistinguishably -- the exact reason
                    // this pre-count changeset's own header rejects a POST-count re-run:
                    // that shape can NEVER observe a nonzero count on ANY install,
                    // structurally, not merely in this hop's fixture). What IS
                    // observable and load-bearing here: (a) the changeset EXECUTES
                    // cleanly under its own RLS toggle; (b) that toggle correctly
                    // RESTORES FORCE ROW LEVEL SECURITY on both nexus.chunks and
                    // nexus.catalog_collections before it ends, proven the same way as
                    // the catalog-029-1 leg above proves it for its own two tables.
                    assertThat(changesetExecType(su, "fk-004-0-reconcile-precount", "nexus-iq0qr"))
                        .as("fk-004-0-reconcile-precount must EXECUTE at HEAD")
                        .isEqualTo("EXECUTED");
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.chunks", "nexus.catalog_collections"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables "
                            + "fk-004-0-reconcile-precount toggled")
                        .isEqualTo(2);

                    // fk-004-1-reconcile leg (nexus-o8dil.49, RDR-191 Phase 5, seed-coverage
                    // lint follow-up): fk-002 (already applied at OLD_TAG -- the "FK parents"
                    // registerCollection calls above exist BECAUSE of this) enforces
                    // collection registration on every chunks_384/768/1024 write from before
                    // this hop even starts, and vectors-004-1 only COPIES already-FK-compliant
                    // rows into nexus.chunks. So by construction NO row this hop can produce
                    // ever has an unregistered collection -- fk-004-1-reconcile's additive
                    // INSERT...SELECT...ON CONFLICT DO NOTHING therefore always registers ZERO
                    // new rows here (there is nothing left for it to find), the honest
                    // structural fact fk-004-chunks-collection-registry.xml's own file header
                    // and this bead's T2 write-back both record -- an observation-count
                    // assertion on it would prove nothing (a broken toggle would ALSO see zero
                    // inserts, indistinguishably). What IS observable and load-bearing: (a) the
                    // chunks_collection_fk VALIDATE (fk-004-2) actually succeeds at HEAD, which
                    // requires fk-004-1-reconcile to have run without throwing under its own
                    // RLS toggle; (b) that toggle correctly RESTORES FORCE ROW LEVEL SECURITY on
                    // both nexus.chunks and nexus.catalog_collections before it ends, rather
                    // than leaving either NO FORCE (the nexus-1wjmq/nexus-php10 failure class) --
                    // proven the same way as the catalog-029-1 leg above proves it for its own
                    // two tables.
                    assertThat(constraintExists(su, "chunks_collection_fk"))
                        .as("chunks_collection_fk must exist at HEAD (fk-004-0)")
                        .isTrue();
                    assertThat(constraintValidated(su, "chunks_collection_fk"))
                        .as("chunks_collection_fk must be VALIDATED at HEAD -- fk-004-2's VALIDATE "
                            + "only succeeds if fk-004-1-reconcile ran to completion under its own "
                            + "RLS toggle without erroring")
                        .isTrue();
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.chunks", "nexus.catalog_collections"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables fk-004-1-reconcile toggled")
                        .isEqualTo(2);

                    // catalog-032-1 leg (nexus-tk070.p1, RDR-194 § D2, seed-coverage lint
                    // follow-up): the anti-join DELETE removes the dangling seed row
                    // (from=1.1.100, to=1.1.999-ghost — never a registered document) and
                    // leaves the KEEP-arm row (from=1.1.100, to=1.1.101, both real
                    // documents) intact. A broken toggle (FORCE never turned off) would
                    // see zero nexus.catalog_documents rows and WRONGLY delete BOTH seeded
                    // links as false-dangling — either failure mode is caught below,
                    // mirroring the catalog-029-1 leg's KEEP/DELETE dual-arm shape.
                    assertThat(dsl(su).fetchCount(CATALOG_LINKS,
                            CATALOG_LINKS.TENANT_ID.eq("t1")
                                .and(CATALOG_LINKS.TO_TUMBLER.eq("1.1.999-ghost"))))
                        .as("catalog-032-1's anti-join DELETE must actually remove the "
                            + "never-registered-endpoint link under FORCE-RLS, proving the "
                            + "DELETE arm fires, not just the KEEP arm")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(CATALOG_LINKS,
                            CATALOG_LINKS.TENANT_ID.eq("t1")
                                .and(CATALOG_LINKS.FROM_TUMBLER.eq("1.1.100"))
                                .and(CATALOG_LINKS.TO_TUMBLER.eq("1.1.101"))))
                        .as("the real edge between two live documents must survive catalog-032-1's "
                            + "remediation — a broken toggle would wrongly delete this too")
                        .isEqualTo(1);
                    assertThat(constraintExists(su, "fk_catalog_links_from_document"))
                        .as("fk_catalog_links_from_document must exist at HEAD (catalog-032-0)")
                        .isTrue();
                    assertThat(constraintExists(su, "fk_catalog_links_to_document"))
                        .as("fk_catalog_links_to_document must exist at HEAD (catalog-032-0)")
                        .isTrue();
                    assertThat(constraintValidated(su, "fk_catalog_links_from_document"))
                        .as("fk_catalog_links_from_document must be VALIDATED at HEAD — catalog-032-2's "
                            + "VALIDATE only succeeds if the anti-join DELETE actually ran and left no "
                            + "dangling from_tumbler behind")
                        .isTrue();
                    assertThat(constraintValidated(su, "fk_catalog_links_to_document"))
                        .as("fk_catalog_links_to_document must be VALIDATED at HEAD — catalog-032-3's "
                            + "VALIDATE only succeeds if the anti-join DELETE actually ran and left no "
                            + "dangling to_tumbler behind")
                        .isTrue();
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.catalog_links", "nexus.catalog_documents"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables catalog-032-1 toggled")
                        .isEqualTo(2);

                    // legacy-001-1/legacy-001-2 leg (nexus-lgdel.l1, THE DELETE,
                    // seed-coverage lint follow-up): the shape-agnostic DELETE removes
                    // the legacy-width seed row from EACH table and leaves the
                    // canonical-width row intact — the DELETE/KEEP dual-arm proof,
                    // mirroring catalog-032-1's own shape immediately above. A CHECK
                    // constraint (added in the SAME migration walk, legacy-001-3) then
                    // makes the deleted shape unwritable going forward; VALIDATE-free
                    // (a plain CHECK, not a VALIDATE CONSTRAINT), so no separate
                    // constraint-existence assertion is needed the way catalog-032's
                    // FK VALIDATEs required one — the row-level effect below is the
                    // whole proof.
                    assertThat(dsl(su).fetchCount(FRECENCY,
                            FRECENCY.TENANT_ID.eq("t1")
                                .and(FRECENCY.CHUNK_ID.eq("e".repeat(32)))))
                        .as("legacy-001-1's shape-agnostic DELETE must actually remove the "
                            + "legacy-width frecency row under FORCE-RLS, proving the DELETE "
                            + "arm fires, not just the KEEP arm")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(FRECENCY,
                            FRECENCY.TENANT_ID.eq("t1")
                                .and(FRECENCY.CHUNK_ID.eq("d".repeat(64)))))
                        .as("the canonical-width frecency row must survive legacy-001-1's "
                            + "remediation — a shape-agnostic-gone-wrong DELETE would wrongly "
                            + "remove this too")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(RELEVANCE_LOG,
                            RELEVANCE_LOG.TENANT_ID.eq("t1")
                                .and(RELEVANCE_LOG.CHUNK_ID.eq("9".repeat(32)))))
                        .as("legacy-001-2's shape-agnostic DELETE must actually remove the "
                            + "legacy-width relevance_log row under FORCE-RLS, proving the "
                            + "DELETE arm fires, not just the KEEP arm")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(RELEVANCE_LOG,
                            RELEVANCE_LOG.TENANT_ID.eq("t1")
                                .and(RELEVANCE_LOG.CHUNK_ID.eq("f".repeat(64)))))
                        .as("the canonical-width relevance_log row must survive legacy-001-2's "
                            + "remediation — a shape-agnostic-gone-wrong DELETE would wrongly "
                            + "remove this too")
                        .isEqualTo(1);

                    // taxonomy-010-1 leg (nexus-tk070.p3b, RDR-194 § D1 steps b/c/d,
                    // seed-coverage lint follow-up): all three DELETE arms,
                    // effect-asserted -- every seeded row is gone (none of the
                    // four text-match a UNIQUE collection: the ambiguous arm
                    // matches two, the unresolvable arm matches none, the
                    // legacy-shape arm is excluded by shape before matching is
                    // even attempted, and the fourth -- non-NULL source_collection,
                    // shape-invalid, the cc4/nexus-i3k3e wedge fixture added
                    // 2026-08-17 -- is excluded by shape UNCONDITIONALLY on
                    // source_collection). GET DIAGNOSTICS ROW_COUNT drives each
                    // RAISE NOTICE 1:1 from the same DELETE statements these
                    // row-state assertions observe, so proving the row set is
                    // exactly this shape is equivalent evidence to the NOTICE
                    // counts themselves (JDBC has no listener attached to
                    // Liquibase's own internal migration connection to capture
                    // the NOTICE text directly). The positive (b) backfill arm
                    // is proven separately by
                    // Taxonomy010BackfillDirectIntegrationTest -- see the SEED
                    // block's own comment for why this old-tag-hop fixture
                    // structurally cannot construct that arm.
                    assertThat(dsl(su).fetchCount(TOPIC_ASSIGNMENTS,
                            TOPIC_ASSIGNMENTS.TENANT_ID.eq("t1")
                                .and(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION.isNull())))
                        .as("no source_collection-NULL row may survive taxonomy-010-1 -- SET NOT "
                            + "NULL would otherwise have failed the whole migration walk")
                        .isEqualTo(0);
                    // RDR-194 P3c (nexus-tk070.p3c): doc_id is bytea at HEAD now (this
                    // leg runs the FULL hop, which includes taxonomy-011-1's ALTER) --
                    // these comparisons bind the decoded bytes (HexFormat#parseHex) as a
                    // typed bytea value. A raw `doc_id = '<64-char-string>'` against a
                    // bytea column would NOT be a type error (Postgres reads it as legacy
                    // "escape format" input, the hex-looking characters becoming their
                    // own ASCII bytes) and would ALWAYS be false, making an isEqualTo(0)
                    // assertion vacuously true even if the targeted DELETE left the row
                    // behind. The decoded-bytes bind compares the real value, so it fails
                    // loud if a row this arm was supposed to delete survives.
                    assertThat(dsl(su).fetchCount(TOPIC_ASSIGNMENTS,
                            TOPIC_ASSIGNMENTS.TENANT_ID.eq("t1")
                                .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(HexFormat.of().parseHex("b".repeat(32))))))
                        .as("the ambiguous arm's row must be DELETED by taxonomy-010-1 -- its chash "
                            + "resolves to two distinct collections and cannot be attributed")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(TOPIC_ASSIGNMENTS,
                            TOPIC_ASSIGNMENTS.TENANT_ID.eq("t1")
                                .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(HexFormat.of().parseHex("c".repeat(64))))))
                        .as("the unresolvable (anti-join) arm's row must be DELETED -- its doc_id is "
                            + "canonical 64-hex SHAPE but no nexus.chunks row matches it at all, "
                            + "proving the anti-join arm fires independently of the shape predicate")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(TOPIC_ASSIGNMENTS,
                            TOPIC_ASSIGNMENTS.TENANT_ID.eq("t1")
                                .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(HexFormat.of().parseHex("1".repeat(32))))))
                        .as("the legacy-shape-coincidental-match arm's row must be DELETED despite "
                            + "a real chunk existing at the same text-matched chash -- the "
                            + "doc_id !~ '^[0-9a-f]{64}$' shape predicate must win over a coincidental "
                            + "anti-join match, the exact cc4/HAL no-wedge proof")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(TOPIC_ASSIGNMENTS,
                            TOPIC_ASSIGNMENTS.TENANT_ID.eq("t1")
                                .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(HexFormat.of().parseHex("8".repeat(32))))))
                        .as("RDR-194 critical fix round (nexus-i3k3e, 2026-08-17): the "
                            + "non-NULL-source_collection shape-invalid row (the cc4 census's "
                            + "1,262-row wedge class) must be DELETED DESPITE its already-set "
                            + "source_collection -- direct proof the fixed UNRESOLVABLE delete's "
                            + "shape branch is unconditional on source_collection. Before the fix "
                            + "this row would have survived taxonomy-010-1 and this test would "
                            + "never have reached this assertion at all -- taxonomy-011-1's guard, "
                            + "a few changesets later in this SAME migration walk, would have "
                            + "RAISE EXCEPTIONed the whole walk first")
                        .isEqualTo(0);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.topic_assignments", "nexus.chunks"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-010-1 toggled")
                        .isEqualTo(2);
                    assertThat(columnMatches(DSL.using(su, SQLDialect.POSTGRES), "nexus", "topic_assignments", "source_collection", col -> col != null && "NO".equals(col.isNullable())))
                        .as("source_collection must be SET NOT NULL at HEAD -- taxonomy-010-1's "
                            + "final DDL step, only reachable if all three DML steps ahead of it "
                            + "left no NULL row behind")
                        .isEqualTo(1);

                    // taxonomy-011-1 leg (nexus-tk070.p3c, RDR-194 § D1 step (e), seed-
                    // coverage lint follow-up). This changeset carries NO INSERT/UPDATE/
                    // DELETE -- its own guard is a read-only SELECT COUNT(*) wrapped in
                    // the SAME NO FORCE/FORCE toggle shape as fk-004-0-reconcile-precount
                    // (nexus-iq0qr, see that entry's own comment in the former Python
                    // DECLARED_SEED_COVERAGE (deleted at cleanup step 10b) for the identical structural reasoning),
                    // which trips this lint's rule (b) regardless of carrying no literal
                    // DML. No NEW seed data is needed: taxonomy-010-1's own seeded rows
                    // (asserted immediately above) are EXACTLY the population the guard
                    // walks, and by construction every surviving row is canonical 64-hex
                    // (the three DELETE arms just proven above removed everything that
                    // was not) -- so the guard is expected to find zero bad rows and let
                    // the ALTER proceed, which is what "this test reaches this line at
                    // all" already proves (a RAISE EXCEPTION here would abort the whole
                    // migration walk before Liquibase's UPDATE SUMMARY, well before this
                    // JDBC connection could run a single assertion). The two checks below
                    // are the POSITIVE, independently-verifiable proof the ALTER itself
                    // (not just the guard) actually ran, matching fk-004-0-reconcile-
                    // precount's own "changeset EXECUTES" bar.
                    assertThat(columnMatches(DSL.using(su, SQLDialect.POSTGRES), "nexus", "topic_assignments", "doc_id", col -> col != null && "bytea".equals(col.udtName())))
                        .as("topic_assignments.doc_id must be bytea at HEAD -- taxonomy-011-1's "
                            + "ALTER COLUMN TYPE, only reachable if the guard above found zero "
                            + "non-canonical rows")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.topic_assignments"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.topic_assignments after "
                            + "taxonomy-011-1's own NO FORCE/FORCE toggle around its guard")
                        .isEqualTo(1);

                    // taxonomy-012-2 leg (nexus-tk070.p3d, RDR-194 § D1, seed-coverage
                    // lint follow-up): the composite-FK anti-join DELETE on
                    // nexus.topic_assignments (tenant_id, source_collection, doc_id) ->
                    // nexus.chunks (tenant_id, collection, chash), the SAME NO FORCE/
                    // FORCE toggle-wrap shape as catalog-013-1b/catalog-014-0/
                    // catalog-025-0/catalog-029-1/catalog-032-1/legacy-001-1/
                    // legacy-001-2. No NEW seed data is needed: taxonomy-010-1's own
                    // seed above is EXACTLY the population that would exercise this
                    // arm, and it is ALREADY DRAINED by the time this changeset runs --
                    // all four seeded rows were removed by taxonomy-010-1's own three
                    // DELETE arms (asserted above: the ambiguous, unresolvable, and
                    // both shape-invalid rows are all isEqualTo(0)), so
                    // nexus.topic_assignments for tenant t1 is already empty. This
                    // mirrors catalog-029-1's own reasoning verbatim (its own entry in
                    // the former Python DECLARED_SEED_COVERAGE): "any row that would exercise
                    // the DELETE arm is, by construction, ALREADY dangling under a
                    // structurally identical anti-join earlier in this same hop" --
                    // here that earlier anti-join is taxonomy-010-1's own
                    // source_collection-IS-NULL-scoped unresolvable arm, which (once
                    // combined with the two shape-unconditional arms) leaves nothing a
                    // later, source_collection-agnostic anti-join could still catch.
                    // Effect-asserted the same minimal way as fk-004-0-reconcile-
                    // precount / taxonomy-011-1: the changeset EXECUTES (a dangling row
                    // surviving into VALIDATE would instead abort the whole migration
                    // walk with SQLSTATE 23503, well before this JDBC connection could
                    // run a single assertion), the population it would have deleted is
                    // still (independently) zero, the FK it makes possible to VALIDATE
                    // exists and IS validated at HEAD, and FORCE ROW LEVEL SECURITY is
                    // restored on both tables its own toggle-wrap covers.
                    assertThat(dsl(su).fetchCount(TOPIC_ASSIGNMENTS,
                            TOPIC_ASSIGNMENTS.TENANT_ID.eq("t1")))
                        .as("taxonomy-012-2's own anti-join population is empty -- every row "
                            + "this fixture seeded was already removed by taxonomy-010-1's three "
                            + "DELETE arms above, so taxonomy-012-2 by construction deletes nothing "
                            + "new in this hop")
                        .isEqualTo(0);
                    assertThat((Boolean.TRUE.equals(PgCatalogProbes.constraintValidated(DSL.using(su, SQLDialect.POSTGRES), "topic_assignments_chunk_fk")) ? 1 : 0))
                        .as("topic_assignments_chunk_fk must exist and be VALIDATED at HEAD -- "
                            + "only reachable if taxonomy-012-1's NOT VALID add, taxonomy-012-2's "
                            + "remediation, and taxonomy-012-3's VALIDATE all ran in this same walk")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.topic_assignments", "nexus.chunks"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-012-2's own "
                            + "toggle-wrap covers")
                        .isEqualTo(2);

                    // taxonomy-014-2/-3/-4/-5 legs (nexus-tk070.p5a, RDR-194 § D4, seed-
                    // coverage lint follow-up): each repoints one of the four tenant-blind
                    // FKs onto nexus.topics' new UNIQUE (tenant_id, id) (taxonomy-014-1),
                    // the SAME catalog-029 three-step shape (DROP old -> ADD new NOT VALID
                    // -> fail-loud anti-join -> VALIDATE) as taxonomy-012-2 above, but
                    // FAIL-LOUD on a nonzero cross-tenant population instead of deleting it
                    // (D4: a cross-tenant topic reference is corruption, not a population to
                    // remediate silently). No NEW seed data is needed for ANY of the four:
                    // the only nexus.topics row this hop ever seeds is seedTopic's own
                    // single tenant-'t1' row (used by taxonomy-010-1 above, parent_id left
                    // NULL), nexus.topic_assignments for tenant 't1' is already proven empty
                    // (isEqualTo(0), asserted for the taxonomy-012-2 leg immediately above),
                    // and nexus.topic_links is never seeded anywhere in this hop -- so all
                    // four anti-joins are structurally zero by construction, the same
                    // "always zero in this hop" reasoning as fk-004-0-reconcile-precount /
                    // taxonomy-011-1 above. Effect-asserted the same minimal way: the
                    // changeset EXECUTES (a nonzero anti-join would RAISE EXCEPTION and abort
                    // the whole migration walk before this JDBC connection could run a single
                    // assertion here), the anti-join population is independently still zero,
                    // each new composite FK exists and is VALIDATED at HEAD, and FORCE ROW
                    // LEVEL SECURITY is restored on every table each leg's own toggle-wrap
                    // covers.
                    assertThat(topicsWithMissingParent(su))
                        .as("taxonomy-014-2's own anti-join population is empty -- the sole "
                            + "nexus.topics row this hop seeds carries a NULL parent_id, so the "
                            + "self-referential anti-join by construction finds nothing")
                        .isEqualTo(0);
                    assertThat((Boolean.TRUE.equals(PgCatalogProbes.constraintValidated(DSL.using(su, SQLDialect.POSTGRES), "fk_topics_parent_tenant")) ? 1 : 0))
                        .as("fk_topics_parent_tenant must exist and be VALIDATED at HEAD -- only "
                            + "reachable if taxonomy-014-2's DROP, ADD NOT VALID, anti-join, and "
                            + "VALIDATE all ran in this same walk")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.topics"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.topics after "
                            + "taxonomy-014-2's own toggle-wrap")
                        .isEqualTo(1);

                    assertThat(assignmentsWithMissingTopic(su))
                        .as("taxonomy-014-3's own anti-join population is empty -- "
                            + "nexus.topic_assignments for tenant t1 was already drained to zero "
                            + "by taxonomy-010-1's three DELETE arms above")
                        .isEqualTo(0);
                    assertThat((Boolean.TRUE.equals(PgCatalogProbes.constraintValidated(DSL.using(su, SQLDialect.POSTGRES), "fk_topic_assignments_topic_tenant")) ? 1 : 0))
                        .as("fk_topic_assignments_topic_tenant must exist and be VALIDATED at HEAD")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.topic_assignments", "nexus.topics"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-014-3's own "
                            + "toggle-wrap covers")
                        .isEqualTo(2);

                    assertThat(topicLinksWithMissingTopic(su, true))
                        .as("taxonomy-014-4's own anti-join population is empty -- "
                            + "nexus.topic_links is never seeded anywhere in this hop")
                        .isEqualTo(0);
                    assertThat((Boolean.TRUE.equals(PgCatalogProbes.constraintValidated(DSL.using(su, SQLDialect.POSTGRES), "fk_topic_links_from_topic_tenant")) ? 1 : 0))
                        .as("fk_topic_links_from_topic_tenant must exist and be VALIDATED at HEAD")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.topic_links", "nexus.topics"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-014-4's own "
                            + "toggle-wrap covers")
                        .isEqualTo(2);

                    assertThat(topicLinksWithMissingTopic(su, false))
                        .as("taxonomy-014-5's own anti-join population is empty -- "
                            + "nexus.topic_links is never seeded anywhere in this hop")
                        .isEqualTo(0);
                    assertThat((Boolean.TRUE.equals(PgCatalogProbes.constraintValidated(DSL.using(su, SQLDialect.POSTGRES), "fk_topic_links_to_topic_tenant")) ? 1 : 0))
                        .as("fk_topic_links_to_topic_tenant must exist and be VALIDATED at HEAD")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.topic_links", "nexus.topics"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-014-5's own "
                            + "toggle-wrap covers")
                        .isEqualTo(2);

                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "topics_tenant_id_unique") ? 1 : 0))
                        .as("topics_tenant_id_unique must exist at HEAD -- taxonomy-014-1's "
                            + "unconditional UNIQUE (tenant_id, id), the precondition every "
                            + "repoint above builds on")
                        .isEqualTo(1);

                    // memory-003-1 / plans-003-1 legs (nexus-tk070.p6a, RDR-194 § D5,
                    // seed-coverage lint follow-up): the ttl=0 DELETE arm must actually
                    // fire under FORCE-RLS (not silently no-op like the v0.1.33 class),
                    // the NULL-ttl KEEP arm must survive untouched (selective, not a
                    // blanket wipe), the column must be renamed ttl -> ttl_days, and the
                    // CHECK must exist at HEAD.
                    assertThat(dsl(su).fetchCount(MEMORY,
                            MEMORY.TENANT_ID.eq("t1")
                                .and(MEMORY.PROJECT.eq("p6a-proj"))
                                .and(MEMORY.TITLE.eq("p6a-zero"))))
                        .as("memory-003-1's counted DELETE must actually remove the seeded "
                            + "ttl=0 row -- the DELETE arm")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(MEMORY,
                            MEMORY.TENANT_ID.eq("t1")
                                .and(MEMORY.PROJECT.eq("p6a-proj"))
                                .and(MEMORY.TITLE.eq("p6a-permanent"))
                                .and(MEMORY.TTL_DAYS.isNull())))
                        .as("the NULL-ttl (permanent) decoy row must survive memory-003-1's "
                            + "DELETE untouched -- the KEEP arm, proving the DELETE is "
                            + "selective on ttl=0 and not a blanket wipe")
                        .isEqualTo(1);
                    assertThat(columnMatches(DSL.using(su, SQLDialect.POSTGRES), "nexus", "memory", "ttl_days", col -> col != null))
                        .as("nexus.memory.ttl must be renamed to ttl_days at HEAD")
                        .isEqualTo(1);
                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "memory_ttl_days_positive_chk") ? 1 : 0))
                        .as("memory_ttl_days_positive_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.memory"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.memory after "
                            + "memory-003-1's own toggle-wrap")
                        .isEqualTo(1);

                    assertThat(dsl(su).fetchCount(PLANS,
                            PLANS.TENANT_ID.eq("t1")
                                .and(PLANS.PROJECT.eq("p6a-proj"))
                                .and(PLANS.QUERY.eq("p6a zero query"))))
                        .as("plans-003-1's counted DELETE must actually remove the seeded "
                            + "ttl=0 row -- the DELETE arm")
                        .isEqualTo(0);
                    assertThat(dsl(su).fetchCount(PLANS,
                            PLANS.TENANT_ID.eq("t1")
                                .and(PLANS.PROJECT.eq("p6a-proj"))
                                .and(PLANS.QUERY.eq("p6a permanent query"))
                                .and(PLANS.TTL_DAYS.isNull())))
                        .as("the NULL-ttl (permanent) decoy row must survive plans-003-1's "
                            + "DELETE untouched -- the KEEP arm")
                        .isEqualTo(1);
                    assertThat(columnMatches(DSL.using(su, SQLDialect.POSTGRES), "nexus", "plans", "ttl_days", col -> col != null))
                        .as("nexus.plans.ttl must be renamed to ttl_days at HEAD")
                        .isEqualTo(1);
                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "plans_ttl_days_positive_chk") ? 1 : 0))
                        .as("plans_ttl_days_positive_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.plans"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.plans after "
                            + "plans-003-1's own toggle-wrap")
                        .isEqualTo(1);

                    // telemetry-006-1 leg (nexus-tk070.p6b, RDR-194 § D5, seed-coverage
                    // lint follow-up): the ttl_days=0 CONVERSION arm must actually fire
                    // under FORCE-RLS (converted to NULL, not deleted -- this is an
                    // UPDATE, not a DELETE, unlike memory-003-1/plans-003-1 above), the
                    // NULL-ttl_days KEEP arm must survive untouched, and the CHECK must
                    // exist at HEAD. Also covers legacy-001-1's own "d"x64 KEEP-arm row
                    // (seeded with the table's DEFAULT ttl_days=0, since seedFrecencyRow
                    // never set it explicitly) -- it too gets converted here, proving
                    // the UPDATE reaches every ttl_days=0 row in this hop, not merely
                    // the dedicated fixture.
                    assertThat(dsl(su).fetchCount(FRECENCY,
                            FRECENCY.TENANT_ID.eq("t1")
                                .and(FRECENCY.CHUNK_ID.eq("6".repeat(64)))
                                .and(FRECENCY.TTL_DAYS.isNull())))
                        .as("telemetry-006-1's counted UPDATE must convert the seeded "
                            + "ttl_days=0 row to NULL -- the CONVERSION arm (not deleted, "
                            + "unlike memory-003-1/plans-003-1's DELETE arms)")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(FRECENCY,
                            FRECENCY.TENANT_ID.eq("t1")
                                .and(FRECENCY.CHUNK_ID.eq("7".repeat(64)))
                                .and(FRECENCY.TTL_DAYS.eq(45))))
                        .as("the positive-ttl_days decoy row must survive telemetry-006-1's "
                            + "UPDATE unchanged -- the KEEP arm, proving the UPDATE's "
                            + "WHERE ttl_days = 0 predicate is selective")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(FRECENCY,
                            FRECENCY.TENANT_ID.eq("t1")
                                .and(FRECENCY.CHUNK_ID.eq("d".repeat(64)))
                                .and(FRECENCY.TTL_DAYS.isNull())))
                        .as("legacy-001-1's own canonical-width KEEP-arm row (DEFAULT "
                            + "ttl_days=0) must ALSO be converted to NULL by "
                            + "telemetry-006-1 -- the UPDATE is unconditional across every "
                            + "row in this hop, not scoped to the dedicated fixture")
                        .isEqualTo(1);
                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "frecency_ttl_days_positive_chk") ? 1 : 0))
                        .as("frecency_ttl_days_positive_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.frecency"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.frecency after "
                            + "telemetry-006-1's own toggle-wrap")
                        .isEqualTo(1);

                    // telemetry-006-2 leg (nexus-tk070.p6b) + staging-6-drop-landing-schema
                    // (nexus-z0o2p.27): staging.frecency has NO fresh seed data (see the
                    // SEED-COVERAGE contract block's own note above) -- staging-001-
                    // landing-tables.xml creates the table fresh EARLIER in this SAME hop
                    // and nothing populates it before telemetry-006-2 runs, so its UPDATE
                    // structurally operates over zero rows in this rehearsal. The changeset
                    // EXECUTES (doesNotThrowAnyException already covers this); and the same
                    // hop then DROPS the whole staging schema (the /v1/staging routes were
                    // retired), so at HEAD there is nothing left of it to inspect. That
                    // absence IS the effect assertion for both: the drop ran over the
                    // post-telemetry-006-2 shape and the runAlways grants that name
                    // staging (staging-4, grants-nexus-diag-3) did not abort the walk.
                    assertThat(PgCatalogProbes.tablesInSchema(DSL.using(su, SQLDialect.POSTGRES), "staging"))
                        .as("the staging landing schema must be gone at HEAD "
                            + "(staging-6-drop-landing-schema)")
                        .isEmpty();

                    // catalog-034-0 leg (nexus-v3w9n, tumbler-grammar data
                    // remediation, seed-coverage lint follow-up): the seeded
                    // 2-segment ghost tumbler must be TOMBSTONED (deleted_at set),
                    // never physically deleted, under FORCE-RLS -- the same
                    // silent-no-op hazard as catalog-014-0/catalog-016-0/
                    // catalog-025-0/aspects-004-1 (catalog_documents is FORCE ROW
                    // LEVEL SECURITY and the Liquibase role is not BYPASSRLS). The
                    // conforming 3-segment control row must survive UNTOUCHED,
                    // proving the predicate is selective, not a blanket tombstone.
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENTS,
                            CATALOG_DOCUMENTS.TENANT_ID.eq("t1")
                                .and(CATALOG_DOCUMENTS.TUMBLER.eq("1.5"))
                                .and(CATALOG_DOCUMENTS.DELETED_AT.isNotNull())))
                        .as("catalog-034-0 must tombstone the seeded 2-segment ghost "
                            + "tumbler under FORCE-RLS, not silently no-op")
                        .isEqualTo(1);
                    assertThat(dsl(su).fetchCount(CATALOG_DOCUMENTS,
                            CATALOG_DOCUMENTS.TENANT_ID.eq("t1")
                                .and(CATALOG_DOCUMENTS.TUMBLER.eq("1.1.205"))
                                .and(CATALOG_DOCUMENTS.DELETED_AT.isNull())))
                        .as("the conforming 3-segment control row must survive "
                            + "catalog-034-0 untouched -- the KEEP arm")
                        .isEqualTo(1);

                    // hygiene-002-1 leg (nexus-ft04v.4, RDR-204 Phase 1 item 1,
                    // seed-coverage lint follow-up): each of the four seeded
                    // catalog_collections rows above must have every blank
                    // content_type/owner_id/embedding_model column filled from its
                    // own name (per its branch), dimension left NULL (this
                    // fixture's own chunk-content constraint, see the seed-side
                    // comment above), and lifecycle_state set per branch -- under
                    // FORCE ROW LEVEL SECURITY, the same silent-no-op hazard as
                    // catalog-014-0/catalog-016-0/catalog-025-0/catalog-034-0/
                    // aspects-004-1 (catalog_collections is FORCE ROW LEVEL
                    // SECURITY and the Liquibase role is not BYPASSRLS -- an
                    // untoggled UPDATE would leave every row exactly as blank as
                    // it was seeded).
                    // Typed jOOQ DSL on CATALOG_COLLECTIONS (nexus-cbo4a/nexus-zrcj7 RawSqlGateTest
                    // reduce-only ratchet), like every other count in this method.
                    org.jooq.DSLContext h002Dsl = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("code__h002a__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("code"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("h002a"))
                            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("bge-base-en-v15-768"))
                            .and(CATALOG_COLLECTIONS.DIMENSION.isNull())
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))))
                        .as("hygiene-002-1 branch A (4-segment conformant, known token) must "
                            + "backfill content_type/owner_id/embedding_model from the name and "
                            + "set lifecycle_state = 'live' (zero chunks)")
                        .isEqualTo(1);
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("quarantine-code__h002b__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("code"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("h002b"))
                            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("bge-base-en-v15-768"))
                            .and(CATALOG_COLLECTIONS.DIMENSION.isNull())
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("quarantine"))))
                        .as("hygiene-002-1 branch B (quarantine-<ct>__...) must strip the "
                            + "quarantine- prefix from content_type and set "
                            + "lifecycle_state = 'quarantine' unconditionally")
                        .isEqualTo(1);
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("code__h002c"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("code"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("h002c"))
                            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("bge-base-en-v15-768"))
                            .and(CATALOG_COLLECTIONS.DIMENSION.isNull())
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))))
                        .as("hygiene-002-1 branch C (grandfathered 2-segment) must "
                            + "backfill content_type/owner_id from the name, embedding_model "
                            + "to the fallback (the profile is always empty at walk time), "
                            + "and lifecycle_state = 'live' (zero chunks)")
                        .isEqualTo(1);
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("h002d-no-separator"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("unknown"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("t1"))
                            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("bge-base-en-v15-768"))
                            .and(CATALOG_COLLECTIONS.DIMENSION.isNull())
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("disputed"))))
                        .as("hygiene-002-1 branch D (no separator) must set "
                            + "content_type = 'unknown', owner_id = the row's own tenant_id, "
                            + "embedding_model = the fallback, and "
                            + "lifecycle_state = 'disputed' unconditionally")
                        .isEqualTo(1);

                    // hygiene-004-1 (nexus-ztafa): the underscored-owner fixture must
                    // land EXACTLY where hygiene-002-1 alone would have landed a
                    // non-underscored branch-A conformant name -- 'live', the name's
                    // own content_type/owner_id/embedding_model, dimension NULL (this
                    // fixture's own zero-chunks constraint, same as the four
                    // hygiene-002-1 fixtures above) -- NOT hygiene-002-1's branch D
                    // ('unknown' / disputed), which is where the walk lands it before
                    // hygiene-004-1 re-files it, later in this same hop.
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("code__h004_owner__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("code"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("h004_owner"))
                            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("bge-base-en-v15-768"))
                            .and(CATALOG_COLLECTIONS.DIMENSION.isNull())
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))))
                        .as("hygiene-004-1 must re-file the underscored-owner 4-segment "
                            + "name hygiene-002-1's own owner grammar cannot match -- 'live' "
                            + "with the name's own attributes, not branch D's 'unknown' / "
                            + "disputed")
                        .isEqualTo(1);

                    // hygiene-005-3 (nexus-uxd2a): the suffix-shaped sibling WITH a
                    // registered origin must have every attribute copied from that
                    // origin's own row and lifecycle_state set to 'quarantine'.
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("code__uxd2a1__bge-base-en-v15-768__v1__quarantine"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("code"))
                            .and(CATALOG_COLLECTIONS.OWNER_ID.eq("uxd2a1"))
                            .and(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq("bge-base-en-v15-768"))
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("quarantine"))))
                        .as("hygiene-005-3 must copy the suffix-shaped sibling's attributes "
                            + "from its registered origin sibling and set lifecycle_state = "
                            + "'quarantine' -- never leave it at branch D's 'unknown' / disputed")
                        .isEqualTo(1);
                    // The origin itself is untouched by hygiene-005-3 (it was never
                    // quarantine-shaped, so no branch's WHERE ever selects it).
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("code__uxd2a1__bge-base-en-v15-768__v1"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("code"))
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))))
                        .as("the origin sibling itself is untouched -- hygiene-005-3 never "
                            + "selects a non-quarantine-shaped name")
                        .isEqualTo(1);
                    // The suffix-shaped sibling with NO registered origin: content_type
                    // stays 'unknown' (no literal "quarantine-" prefix to strip),
                    // lifecycle_state is never touched (stays 'disputed').
                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.TENANT_ID.eq("t1")
                            .and(CATALOG_COLLECTIONS.NAME.eq("code__uxd2a2__bge-base-en-v15-768__v1__quarantine"))
                            .and(CATALOG_COLLECTIONS.CONTENT_TYPE.eq("unknown"))
                            .and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("disputed"))))
                        .as("hygiene-005-3 with no registered origin sibling leaves "
                            + "content_type = 'unknown' unchanged (nothing to strip) and "
                            + "never touches lifecycle_state")
                        .isEqualTo(1);

                    assertThat(h002Dsl.fetchCount(CATALOG_COLLECTIONS,
                        CATALOG_COLLECTIONS.CONTENT_TYPE.eq("")
                            .or(CATALOG_COLLECTIONS.OWNER_ID.eq(""))
                            .or(CATALOG_COLLECTIONS.EMBEDDING_MODEL.eq(""))))
                        .as("no catalog_collections row may survive hygiene-002-1 with a "
                            + "blank content_type/owner_id/embedding_model -- the invariant "
                            + "the CHECK constraints immediately below depend on")
                        .isEqualTo(0);
                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "catalog_collections_content_type_chk") ? 1 : 0))
                        .as("catalog_collections_content_type_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "catalog_collections_owner_id_chk") ? 1 : 0))
                        .as("catalog_collections_owner_id_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "catalog_collections_embedding_model_chk") ? 1 : 0))
                        .as("catalog_collections_embedding_model_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat((Boolean.TRUE.equals(PgCatalogProbes.constraintValidated(DSL.using(su, SQLDialect.POSTGRES), "catalog_collections_embedding_model_fk")) ? 1 : 0))
                        .as("catalog_collections_embedding_model_fk must exist and be "
                            + "VALIDATED at HEAD -- only reachable if every row's "
                            + "embedding_model was already a real nexus.embedding_models "
                            + "row when the FK was added, immediately after the walk")
                        .isEqualTo(1);
                    assertThat((PgCatalogProbes.constraintExists(DSL.using(su, SQLDialect.POSTGRES), "catalog_collections_lifecycle_state_chk") ? 1 : 0))
                        .as("catalog_collections_lifecycle_state_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat(columnMatches(DSL.using(su, SQLDialect.POSTGRES), "nexus", "catalog_collections", "lifecycle_state", col -> col != null && "NO".equals(col.isNullable())))
                        .as("nexus.catalog_collections.lifecycle_state must be NOT NULL at HEAD")
                        .isEqualTo(1);
                    assertThat(PgCatalogProbes.forcedRowSecurityCount(DSL.using(su, SQLDialect.POSTGRES), "nexus.catalog_collections", "nexus.chunks", "nexus.catalog_document_chunks", "nexus.catalog_documents"))
                        .as("FORCE ROW LEVEL SECURITY restored on all four tables "
                            + "hygiene-002-1's own toggle-wrap covers")
                        .isEqualTo(4);
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── Helpers: shared rehearsal plumbing ───────────────────────────────────

    /**
     * The materialized old-tag tree is immutable input shared by both legs —
     * cached per-class so the {@code git archive | tar} cost is paid once, and
     * cleaned up by a shutdown hook (tests run sequentially; see
     * feedback_no_parallel_tests).
     */
    private static volatile Path cachedOldTreeRoot;

    /**
     * Tag-availability gate + {@code git archive} materialization of the old
     * tag's changelog tree. Skips loudly (never silently passes) when the tag
     * is unavailable even after the shallow-fetch fallback.
     */
    private static Path ensureOldTreeMaterialized(Path repoRoot) throws Exception {
        Path cached = cachedOldTreeRoot;
        if (cached != null && Files.exists(cached.resolve(MASTER_CHANGELOG_RELATIVE))) {
            return cached;
        }
        if (!tagExists(repoRoot, OLD_TAG)) {
            log.warn("event=schema_upgrade_rehearsal_tag_missing tag={} attempting shallow fetch fallback", OLD_TAG);
            fetchTagShallow(repoRoot, OLD_TAG);
        }
        if (!tagExists(repoRoot, OLD_TAG)) {
            log.error("event=schema_upgrade_rehearsal_tag_unavailable tag={} — SKIPPING (loud, non-silent). "
                + "service-ci.yml fetches this tag in its own step precisely so this never fires in CI; "
                + "a persistent local skip means that step regressed.", OLD_TAG);
            Assumptions.abort(
                "Old tag " + OLD_TAG + " unavailable locally and shallow fetch fallback failed; "
                + "skipping schema-upgrade rehearsal. This must NOT happen in CI — verify "
                + "service-ci.yml still fetches the OLD_TAG tag.");
        }

        Path oldChangelogRoot = materializeOldChangelogTree(repoRoot, OLD_TAG);
        assertThat(Files.exists(oldChangelogRoot.resolve(MASTER_CHANGELOG_RELATIVE)))
            .as("git archive must have materialized the old tag's master changelog at %s — "
                + "an empty/missing tree must fail loudly, never skip-pass", oldChangelogRoot)
            .isTrue();
        cachedOldTreeRoot = oldChangelogRoot;
        return oldChangelogRoot;
    }

    /**
     * Minimal DBA-equivalent bootstrap (mirrors
     * SchemaMigratorIntegrationTest.bootstrap()). role-001-nexus-svc.xml's
     * self-create branch requires CREATEROLE, which the non-superuser
     * {@link #ADMIN_ROLE} deliberately lacks (proving the production
     * non-superuser owner path) — so nexus_svc must exist before the walk, as
     * production DBA pre-provisioning leaves it. On the per-fork shared cluster
     * the template migration has already made it, so the bootstrap's
     * CREATE ROLE IF NOT EXISTS is a no-op and role-001-1's guard is a no-op too.
     * The role is NOBYPASSRLS (the CREATE ROLE default, which role-001 keeps) — load-bearing
     * for the data leg: FORCE-RLS policies apply to it even as table owner, exactly as
     * they do to production's nexus_admin.
     */
    private static void dbaBootstrap(Connection su) throws Exception {
        // The admin role, CREATE grants, pg_monitor WITH ADMIN OPTION (nexus-hzhgl), extensions and the
        // SECURITY DEFINER relocation helpers all come from db.changelog-test-nonsuper-owner.xml
        // (PgContainerHelper#bootstrapNonSuperuserOwner); nexus_svc is the cluster's existing role.
        PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
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

    /**
     * OLD LEG: apply the OLD TAG's literal changelog tree via a filesystem
     * ResourceAccessor (NOT SchemaMigrator.migrate — that hardcodes the
     * classpath = HEAD master changelog). Returns the number of changesets
     * applied, asserting it is nonzero and matches what Liquibase reported
     * pending (a silently-empty old tree must fail, not skip-pass).
     */
    private static int applyOldLeg(HikariDataSource adminDs, Path oldChangelogRoot) throws Exception {
        int oldLegPending;
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG_RELATIVE,
                    new DirectoryResourceAccessor(oldChangelogRoot.toFile()),
                    database)) {

                List<liquibase.changelog.ChangeSet> unrun =
                    liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                oldLegPending = unrun.size();
                assertThat(oldLegPending)
                    .as("old tag %s's changelog tree must contain a NONZERO number of pending "
                        + "changesets — a silently-empty old tree must fail, not skip-pass", OLD_TAG)
                    .isGreaterThan(0);

                liquibase.update(new Contexts(), new LabelExpression());
            }
        }

        int oldLegApplied;
        try (Connection conn = adminDs.getConnection()) {
            oldLegApplied = changelogRowCount(conn);
        }
        assertThat(oldLegApplied)
            .as("old leg must have applied exactly the changesets it reported pending")
            .isEqualTo(oldLegPending);
        return oldLegApplied;
    }

    /**
     * HEAD LEG, PARTIAL: runs the classpath (HEAD) master changelog up to
     * (but NOT including) {@code changesetId} -- the same {@code migrateUpTo}
     * idiom {@code VectorsUnifyChunksIntegrationTest}/{@code
     * Hygiene001NotNullMigrationRlsTest} already use, retargeted at {@link
     * dev.nexus.service.db.SchemaMigrator#MASTER_CHANGELOG}'s own classpath
     * resource (a {@link ClassLoaderResourceAccessor}, not the {@link
     * DirectoryResourceAccessor} {@link #applyOldLeg} uses for the OLD tree)
     * so it sees this checkout's OWN changelog, exactly what a later {@link
     * SchemaMigrator#migrate} call over the SAME connection pool resumes
     * from -- Liquibase's own {@code databasechangelog} bookkeeping makes that
     * resumption safe (idempotent re-application of already-run changesets
     * is the whole premise a production upgrade relies on).
     *
     * <p>nexus-r7xao seed-coverage follow-up (tuples-003-2): a hop changeset
     * whose target table does not exist until PARTWAY through the SAME hop
     * (unlike every other {@code DECLARED_SEED_COVERAGE} entry, which seeds
     * rows for a table the OLD leg already created) cannot be seeded in the
     * single SEED block above {@link #applyOldLeg} and the whole-hop {@link
     * SchemaMigrator#migrate} call -- the table simply is not there yet at
     * that point. This helper is the template for a table created MID-WALK:
     * migrate up to just before the changeset that needs a specific
     * pre-existing population, seed directly (nexus.tuples/tuple_claim_log
     * are freshly created by tuples-001-baseline/tuples-002-* by this point,
     * with no CHECK constraint yet -- an oversized body inserts cleanly), then
     * let the normal {@code SchemaMigrator.migrate(adminDs)} call finish the
     * rest of the hop exactly as it would have run the whole thing in one
     * shot.
     */
    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG_RELATIVE,
                    new ClassLoaderResourceAccessor(),
                    database)) {
                List<liquibase.changelog.ChangeSet> unrun =
                    liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx)
                    .as("%s must be present and still pending in the HEAD changelog", changesetId)
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    /**
     * nexus-r7xao seed-coverage follow-up: tuples-003-2's own input shape --
     * an over-cap body row, an at-cap body row, and a claim-log row
     * referencing the over-cap tuple -- mirroring {@code
     * TuplesBaselineSchemaLiquibaseTest#
     * tuplesBodySizeCheckCleanup_deletesLegacyOversizedRows_keepsAtCapRow_thenValidates}'s
     * fixture exactly, the dedicated Liquibase-level test for this same
     * changeset. Runs on a superuser connection (bypasses FORCE RLS
     * automatically, same as every other seed helper in this file) at the
     * point in the hop where nexus.tuples/tuple_claim_log exist but carry no
     * CHECK constraint yet, so the oversized insert succeeds cleanly --
     * modeling a genuine legacy row written before this bead's engine-side
     * cap ever existed.
     */
    private static void seedTupleBodySizeLimitRows(Connection su, String tenant, byte[] oversizedId,
                                                    byte[] atCapId, String legacyClaimId) throws Exception {
        java.time.OffsetDateTime now = java.time.OffsetDateTime.now();
        var ctx = dsl(su);
        insertTuple(ctx, oversizedId, tenant, "mailbox/rehearsal-oversized", "x".repeat(5000), now);
        insertTuple(ctx, atCapId, tenant, "mailbox/rehearsal-at-cap", "x".repeat(4096), now);
        ctx.insertInto(TUPLE_CLAIM_LOG)
            .set(TUPLE_CLAIM_LOG.TENANT_ID, tenant)
            .set(TUPLE_CLAIM_LOG.SUBSPACE, "mailbox/rehearsal-oversized")
            .set(TUPLE_CLAIM_LOG.TEMPLATE, "mailbox/<address>")
            .set(TUPLE_CLAIM_LOG.TUPLE_ID, oversizedId)
            .set(TUPLE_CLAIM_LOG.CLAIM_ID, legacyClaimId)
            .set(TUPLE_CLAIM_LOG.CLAIMANT, "rehearsal-claimant")
            .set(TUPLE_CLAIM_LOG.TRANSITION, "claim")
            .set(TUPLE_CLAIM_LOG.AT, now)
            .set(TUPLE_CLAIM_LOG.EXPIRES_AT, now.plusDays(180))
            .execute();
    }

    /** One {@code nexus.tuples} row with an empty keys object, created {@code now}, expiring in a week. */
    private static org.jooq.InsertSetMoreStep<dev.nexus.service.jooq.nexus.tables.records.TuplesRecord>
            tupleInsert(org.jooq.DSLContext ctx, byte[] id, String tenant, String subspace, String body,
                        java.time.OffsetDateTime now) {
        return ctx.insertInto(TUPLES)
            .set(TUPLES.ID, id)
            .set(TUPLES.TENANT_ID, tenant)
            .set(TUPLES.SUBSPACE, subspace)
            .set(TUPLES.TEMPLATE, "mailbox/<address>")
            .set(TUPLES.KEYS, org.jooq.JSONB.jsonb("{}"))
            .set(TUPLES.BODY, body)
            .set(TUPLES.EXPIRES_AT, now.plusDays(7))
            .set(TUPLES.CREATED_AT, now);
    }

    private static void insertTuple(org.jooq.DSLContext ctx, byte[] id, String tenant, String subspace,
                                    String body, java.time.OffsetDateTime now) {
        tupleInsert(ctx, id, tenant, subspace, body, now).execute();
    }

    /**
     * nexus-8zoyp seed-coverage: tuples-004-1's own input shape -- a consumed
     * row carrying a body, and an unconsumed row carrying a body -- modeling
     * the population tuples-004-1's one-shot cleanup exists to fix: rows
     * consumed before {@code TupleRepository.consumeClaim} started nulling
     * body itself. Runs on a superuser connection (bypasses FORCE RLS
     * automatically, same as every other seed helper in this file); both
     * bodies are well under the 4096-byte {@code chk_tuples_body_size} cap,
     * already enforced by this point in the walk (tuples-003-3/4 ran as part
     * of the {@code migrateUpTo("tuples-004-1")} call above).
     */
    /** One content_hash shared by two tenants, plus a parentless WAL family
     *  (pipeline-002-2's DELETE arm). Typed handles rather than the generated
     *  classes: those describe the POST-002 schema, where the WAL tables have
     *  no content_hash column (RawSqlGateTest: no raw SQL text in tests). */
    private static final String PIPELINE_SHARED_HASH = "rehearsal-shared-" + "0".repeat(24);
    private static final String PIPELINE_ORPHAN_HASH = "rehearsal-orphan-" + "0".repeat(24);
    private static final org.jooq.Table<?> PIPELINE_ROWS = DSL.table(DSL.name("nexus", "pdf_pipeline"));
    private static final org.jooq.Table<?> PIPELINE_PAGES = DSL.table(DSL.name("nexus", "pdf_pages"));
    private static final org.jooq.Table<?> PIPELINE_CHUNKS = DSL.table(DSL.name("nexus", "pdf_chunks"));

    /** The pipeline-001 shape (WAL keyed by content_hash), as superuser. */
    private static void seedPipelinePreRowIdentityRows(Connection su) {
        var ctx = DSL.using(su, SQLDialect.POSTGRES);
        var tenantId = DSL.field(DSL.name("tenant_id"), String.class);
        var contentHash = DSL.field(DSL.name("content_hash"), String.class);
        var createdAt = DSL.field(DSL.name("created_at"), java.time.OffsetDateTime.class);
        var now = java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC);
        for (String tenant : List.of("t1", "t2")) {
            ctx.insertInto(PIPELINE_ROWS, tenantId, contentHash,
                    DSL.field(DSL.name("pdf_path"), String.class),
                    DSL.field(DSL.name("collection"), String.class),
                    DSL.field(DSL.name("status"), String.class),
                    DSL.field(DSL.name("started_at"), java.time.OffsetDateTime.class),
                    DSL.field(DSL.name("updated_at"), java.time.OffsetDateTime.class))
               .values(tenant, PIPELINE_SHARED_HASH, "/" + tenant + "/shared.pdf",
                       "knowledge__rehearsal", "completed", now, now)
               .execute();
        }
        for (String[] w : new String[][] {
                {"t1", PIPELINE_SHARED_HASH}, {"t2", PIPELINE_SHARED_HASH}, {"t1", PIPELINE_ORPHAN_HASH}}) {
            for (int i = 0; i < 2; i++) {
                ctx.insertInto(PIPELINE_PAGES, tenantId, contentHash,
                        DSL.field(DSL.name("page_index"), Integer.class),
                        DSL.field(DSL.name("page_text"), String.class), createdAt)
                   .values(w[0], w[1], i, "p" + i, now)
                   .execute();
                ctx.insertInto(PIPELINE_CHUNKS, tenantId, contentHash,
                        DSL.field(DSL.name("chunk_index"), Integer.class),
                        DSL.field(DSL.name("chunk_text"), String.class),
                        DSL.field(DSL.name("chunk_id"), String.class), createdAt)
                   .values(w[0], w[1], i, "c" + i, "cid-" + w[0] + "-" + i, now)
                   .execute();
            }
        }
    }

    private static void seedTupleConsumedBodyCleanupRows(Connection su, String tenant, byte[] consumedId,
                                                          byte[] unconsumedId) throws Exception {
        java.time.OffsetDateTime now = java.time.OffsetDateTime.now();
        var ctx = dsl(su);
        tupleInsert(ctx, consumedId, tenant, "mailbox/rehearsal-consumed", "already-consumed", now)
            .set(TUPLES.CONSUMED_AT, now)
            .set(TUPLES.CONSUMED_BY, "rehearsal-claimant")
            .execute();
        insertTuple(ctx, unconsumedId, tenant, "mailbox/rehearsal-unconsumed", "still-here", now);
    }

    // ── Helpers: git plumbing ────────────────────────────────────────────────

    private static Path repoRoot() throws IOException, InterruptedException {
        String out = runAndCapture(List.of("git", "rev-parse", "--show-toplevel"), new File("."));
        return Path.of(out.trim());
    }

    private static boolean tagExists(Path repoRoot, String tag) throws IOException, InterruptedException {
        Process p = new ProcessBuilder("git", "rev-parse", "-q", "--verify", "refs/tags/" + tag + "^{commit}")
            .directory(repoRoot.toFile())
            .redirectOutput(ProcessBuilder.Redirect.DISCARD)
            .redirectError(ProcessBuilder.Redirect.DISCARD)
            .start();
        return p.waitFor() == 0;
    }

    private static void fetchTagShallow(Path repoRoot, String tag) throws IOException, InterruptedException {
        Process p = new ProcessBuilder("git", "fetch", "--depth", "1", "origin", "tag", tag)
            .directory(repoRoot.toFile())
            .redirectOutput(ProcessBuilder.Redirect.INHERIT)
            .redirectError(ProcessBuilder.Redirect.INHERIT)
            .start();
        p.waitFor(); // best-effort; re-checked by the caller via tagExists()
    }

    /**
     * {@code git archive <tag> -- <changelog pathspec> | tar -x -C <extractDir>},
     * returning the extracted {@code service/src/main/resources} root (the
     * directory {@code db/changelog/db.changelog-master.xml} is relative to,
     * mirroring how {@link liquibase.resource.ClassLoaderResourceAccessor} roots
     * the same relative path at the classpath resources directory).
     */
    private static Path materializeOldChangelogTree(Path repoRoot, String tag) throws IOException, InterruptedException {
        Path extractDir = Files.createTempDirectory("nexus-schema-rehearsal-");
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try (var walk = Files.walk(extractDir)) {
                walk.sorted(java.util.Comparator.reverseOrder()).forEach(p -> p.toFile().delete());
            } catch (IOException ignored) {
                // best-effort temp cleanup; the OS tmp reaper is the backstop
            }
        }));

        ProcessBuilder archive = new ProcessBuilder("git", "archive", tag, "--", CHANGELOG_PATHSPEC)
            .directory(repoRoot.toFile())
            .redirectError(ProcessBuilder.Redirect.INHERIT);
        ProcessBuilder untar = new ProcessBuilder("tar", "-x", "-C", extractDir.toString())
            .redirectError(ProcessBuilder.Redirect.INHERIT);

        List<Process> pipeline = ProcessBuilder.startPipeline(List.of(archive, untar));
        int archiveExit = pipeline.get(0).waitFor();
        int untarExit = pipeline.get(pipeline.size() - 1).waitFor();
        if (archiveExit != 0 || untarExit != 0) {
            throw new IllegalStateException(
                "git archive | tar pipeline failed for tag " + tag
                    + " (archiveExit=" + archiveExit + ", untarExit=" + untarExit + ")");
        }

        return extractDir.resolve("service/src/main/resources");
    }

    private static String runAndCapture(List<String> cmd, File cwd) throws IOException, InterruptedException {
        Process p = new ProcessBuilder(cmd)
            .directory(cwd)
            .redirectError(ProcessBuilder.Redirect.INHERIT)
            .start();
        String out = new String(p.getInputStream().readAllBytes());
        int exit = p.waitFor();
        if (exit != 0) {
            throw new IllegalStateException("Command failed (" + exit + "): " + String.join(" ", cmd));
        }
        return out;
    }

    // ── Helpers: seeding (data leg) ──────────────────────────────────────────

    /**
     * nexus-0rxvg: the catalog-037-1 bounded sweep's misregistration shape,
     * seeded directly just before hygiene-008-2. The origin carries attributes
     * its name does not imply (owner, model_version, dimension), so only a copy
     * from the origin row, not a name parse, satisfies the effect asserts.
     */
    private static void seedBoundedQuarantineMisregistrations(Connection su) throws Exception {
        var ctx = DSL.using(su, SQLDialect.POSTGRES);
        ctx.insertInto(CATALOG_COLLECTIONS,
                CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION,
                CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
            .values("t1", "code__rxvg1__bge-base-en-v15-768__v1", "code", "rxvg1-origin-owner",
                "bge-base-en-v15-768", "v4", 768, "live")
            .values("t1", "quarantine-code__rxvg1__bge-base-en-v15-768__v1", "quarantine-code", "rxvg1",
                "bge-base-en-v15-768", "v1", null, "quarantine")
            .values("t1", "quarantine-docs__rxvg2__bge-base-en-v15-768__v1", "quarantine-docs", "rxvg2",
                "bge-base-en-v15-768", "v1", null, "disputed")
            .execute();
    }

    private static void registerCollection(Connection c, String tenant, String name) throws Exception {
        // RDR-204 nexus-ft04v.4/.5: delegates to PgContainerHelper.insertCollection,
        // safely falling back to this exact bare insert at any migration depth
        // where lifecycle_state does not exist yet.
        PgContainerHelper.insertCollection(DSL.using(c, SQLDialect.POSTGRES), tenant, name);
    }


    private static void seedChashRow(Connection c, String tenant, String chash,
                                     String collection) throws Exception {
        dsl(c).insertInto(CHASH_INDEX_RETIRED)
            .set(DSL.field(DSL.name("tenant_id"), String.class), tenant)
            .set(DSL.field(DSL.name("chash"), String.class), chash)
            .set(DSL.field(DSL.name("physical_collection"), String.class), collection)
            .set(DSL.field(DSL.name("created_at"), java.time.OffsetDateTime.class), DSL.currentOffsetDateTime())
            .execute();
    }

    /**
     * nexus-j862l (RDR-191 GATE-2 follow-up): a matching {@code chunks_384}
     * content row for a manifest pointer seeded via {@link #seedManifestRow},
     * at the OLD (pre-rdr180) TEXT-chash schema shape — the SAME transform
     * ({@code decode(chash, 'hex')}) rdr180-4/rdr180-6 later apply to both
     * this row and the manifest row converts them to the IDENTICAL bytea
     * value, so catalog-025's dangling-row check (which requires a matching
     * {@code chunks_384}/768/1024 row for the manifest row's {@code (tenant_id,
     * collection, chash)}) finds a match and leaves the manifest row alone —
     * exactly as it would for a real tenant's legacy row, which never existed
     * without its content landing alongside it.
     *
     * <p><strong>RDR-191 Phase 4 unify — verified PRE-hop, unchanged.</strong>
     * This helper is invoked from the SEED block, which runs strictly between
     * {@link #applyOldLeg} (the old {@link #OLD_TAG} tree, ending before the
     * unify) and the HEAD-leg {@link SchemaMigrator#migrate}. At the moment
     * this INSERT executes, {@code nexus.chunks_384} is the table
     * {@code vectors-001-baseline.xml} created in the OLD leg — it still has
     * the old per-dim shape and has not yet been touched by vectors-004-1
     * (which only runs later, inside the HEAD leg, and copies this very row
     * into the unified {@code nexus.chunks.embedding_384} column before
     * dropping this table). This call targets the correct, live schema for
     * its position in the hop and needs no change for the unify.
     */
    private static void seedChunk384LegacyContent(Connection c, String tenant, String collection,
                                                   String chash, String text) throws Exception {
        // hygiene-001-5 (nexus-tk070.p6a follow-on): metadata is included
        // (non-NULL) -- these rows carry the DELIBERATE 16-byte legacy
        // chash shape rdr180-001's own header documents as
        // un-rekeyed-and-tolerated (its octet CHECK stays NOT VALID for
        // exactly such rows). Review round (critic census finding):
        // hygiene-001-5's DELETE now keys DIRECTLY on
        // octet_length(chash) <> 32, not on a metadata-NULL proxy, so
        // this row IS deleted by hygiene-001-5 by the time a full hop
        // reaches HEAD regardless of its metadata -- non-NULL metadata
        // here no longer means "survives", it only means "this row was
        // never a candidate for the metadata BACKFILL half of that
        // changeset" (a real, distinct fact this test also exercises).
        // The DELETE path itself is covered both here (see
        // SchemaUpgradeRehearsalIntegrationTest's own isEqualTo(0)
        // assertions against this exact seed) and by
        // Hygiene001NotNullMigrationRlsTest's dedicated per-tenant
        // legacy-width deletion coverage.
        insertLegacyChunk(c, 384, tenant, collection, chash, text);
    }

    /**
     * nexus-97gii seed-coverage follow-up (RDR-191 Phase 4 unify,
     * vectors-004-1): legacy content row for {@code nexus.chunks_768} or
     * {@code nexus.chunks_1024}, the OLD per-dim schema shape -- generalized
     * over {@code dim} since these two need no j862l-specific dangling-row
     * companion, only vectors-004-1's straddle coverage (chunks_384 is
     * already covered by {@link #seedChunk384LegacyContent}'s existing
     * rows). Same PRE-hop timing determination applies verbatim: called from
     * the SEED block, strictly between {@link #applyOldLeg} and the HEAD-leg
     * {@link SchemaMigrator#migrate}, so {@code nexus.chunks_<dim>} is still
     * the live OLD-schema table at INSERT time.
     */
    private static void seedChunkDimLegacyContent(Connection c, int dim, String tenant, String collection,
                                                   String chash, String text) throws Exception {
        // hygiene-001-5: metadata included non-NULL -- see
        // seedChunk384LegacyContent's identical, fuller comment. This
        // row IS deleted by hygiene-001-5's chash-width DELETE by HEAD
        // regardless of its metadata (review round correction).
        insertLegacyChunk(c, dim, tenant, collection, chash, text);
    }

    /**
     * One row of the OLD per-dim {@code nexus.chunks_<dim>} table (TEXT chash, {@code embedding vector(<dim>)},
     * non-NULL {@code metadata}), the dim's components all zero, {@code ON CONFLICT DO NOTHING} on the natural key.
     * The vector goes in as a {@code real[]} bind (see {@link #legacyEmbedding}).
     */
    private static void insertLegacyChunk(Connection c, int dim, String tenant, String collection,
                                          String chash, String text) {
        var tenantId = DSL.field(DSL.name("tenant_id"), String.class);
        var coll = DSL.field(DSL.name("collection"), String.class);
        var chashCol = DSL.field(DSL.name("chash"), String.class);
        dsl(c).insertInto(DSL.table(DSL.name("nexus", "chunks_" + dim)))
            .set(tenantId, tenant)
            .set(coll, collection)
            .set(chashCol, chash)
            .set(DSL.field(DSL.name("chunk_text"), String.class), text)
            .set(LEGACY_EMBEDDING, legacyEmbedding(dim, 0f))
            .set(DSL.field(DSL.name("metadata"), org.jooq.JSONB.class), org.jooq.JSONB.jsonb("{}"))
            .onConflict(tenantId, coll, chashCol)
            .doNothing()
            .execute();
        // Positive control: the "deleted by HEAD" isEqualTo(0) assertions downstream are vacuous
        // unless the legacy row actually landed (ON CONFLICT DO NOTHING would hide a non-insert).
        assertThat(dsl(c).fetchCount(DSL.table(DSL.name("nexus", "chunks_" + dim)),
                tenantId.eq(tenant).and(coll.eq(collection)).and(chashCol.eq(chash))))
            .as("legacy chunks_%d seed %s/%s/%s must be present", dim, tenant, collection, chash)
            .isEqualTo(1);
    }

    /**
     * nexus-97gii seed-coverage follow-up (RDR-191 Phase 4 unify,
     * taxonomy-007-1): legacy centroid row for {@code
     * nexus.taxonomy_centroids_<dim>}, the OLD per-dim schema shape. No
     * collection-FK stub needed first -- unlike chunks, {@code
     * taxonomy_centroids_<dim>} carries no FK to {@code catalog_collections}
     * or {@code nexus.topics} (verified directly against
     * taxonomy-002-centroids.xml; mirrors
     * {@code VectorsUnifyCentroidsIntegrationTest#seedCentroid}'s own note).
     */
    private static void seedTaxonomyCentroidLegacyContent(Connection c, int dim, String tenant,
                                                           String collection, long topicId, String label)
            throws Exception {
        dsl(c).insertInto(DSL.table(DSL.name("nexus", "taxonomy_centroids_" + dim)))
            .set(DSL.field(DSL.name("tenant_id"), String.class), tenant)
            .set(DSL.field(DSL.name("collection"), String.class), collection)
            .set(DSL.field(DSL.name("topic_id"), Long.class), topicId)
            .set(LEGACY_EMBEDDING, legacyEmbedding(dim, 0.01f))
            .set(DSL.field(DSL.name("label"), String.class), label)
            .set(DSL.field(DSL.name("doc_count"), Integer.class), 3)
            .execute();
        assertThat(dsl(c).fetchCount(DSL.table(DSL.name("nexus", "taxonomy_centroids_" + dim)),
                DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)
                    .and(DSL.field(DSL.name("topic_id"), Long.class).eq(topicId))))
            .as("legacy taxonomy_centroids_%d seed %s/%d must be present", dim, tenant, topicId)
            .isEqualTo(1);
    }

    /**
     * The embedding column of the OLD per-dim tables ({@code chunks_<dim>}, {@code taxonomy_centroids_<dim>}),
     * bound as a {@code real[]}. At seed time the pgvector extension is still in {@code public} (the relocation
     * into {@code nexus} happens later in the HEAD leg), so the generated {@code Vector} binding, which renders
     * {@code ::nexus.vector}, does not resolve. pgvector's {@code real[]} to {@code vector} cast is an
     * ASSIGNMENT cast ({@code CREATE CAST (real[] AS vector) WITH FUNCTION array_to_vector(real[], integer, boolean)
     * AS ASSIGNMENT}), looked up by type oid rather than by name, so the INSERT converts the bind in whichever
     * schema the extension sits.
     */
    private static final org.jooq.Field<Float[]> LEGACY_EMBEDDING = VectorSeeds.LEGACY_EMBEDDING;

    private static Float[] legacyEmbedding(int dim, float fill) {
        return VectorSeeds.legacyEmbedding(dim, fill);
    }

    private static void seedDocument(Connection c, String tenant, String tumbler,
                                     String title, String physicalCollection) throws Exception {
        dsl(c).insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, title)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, physicalCollection)
            .execute();
    }

    /** Document row WITH a source_uri + chunk_count (catalog-016-0's dedup input shape). */
    private static void seedDocumentWithUri(Connection c, String tenant, String tumbler,
                                            String title, String physicalCollection,
                                            String sourceUri, int chunkCount) throws Exception {
        dsl(c).insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENTS.TUMBLER, tumbler)
            .set(CATALOG_DOCUMENTS.TITLE, title)
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, physicalCollection)
            .set(CATALOG_DOCUMENTS.SOURCE_URI, sourceUri)
            .set(CATALOG_DOCUMENTS.CHUNK_COUNT, chunkCount)
            .execute();
    }

    /**
     * A legacy {@code nexus.document_aspects} row with {@code doc_id} NULL
     * (nexus-ubnwk, aspects-004-1) — the pre-go-forward-stamp shape
     * fk-001-2's nullable conversion allows. {@code sourceUri} should match
     * a live catalog document's own {@code source_uri} exactly (aspects-004's
     * join key) for the CONVERSION arm.
     */
    private static void seedAspectRow(Connection c, String tenant, String collection,
                                      String sourcePath, String sourceUri) throws Exception {
        seedAspect(c, tenant, collection, sourcePath, "rehearsal-seeded extraction", sourceUri, null);
    }

    /** A {@code nexus.document_aspects} row; {@code sourceUri} and {@code docId} may be NULL (the legacy shapes). */
    private static void seedAspect(Connection c, String tenant, String collection, String sourcePath,
                                   String proposedMethod, String sourceUri, String docId) {
        dsl(c).insertInto(DOCUMENT_ASPECTS)
            .set(DOCUMENT_ASPECTS.TENANT_ID, tenant)
            .set(DOCUMENT_ASPECTS.COLLECTION, collection)
            .set(DOCUMENT_ASPECTS.SOURCE_PATH, sourcePath)
            .set(DOCUMENT_ASPECTS.PROPOSED_METHOD, proposedMethod)
            .set(DOCUMENT_ASPECTS.EXTRACTED_AT, DSL.currentOffsetDateTime())
            .set(DOCUMENT_ASPECTS.MODEL_VERSION, "rehearsal-model")
            .set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "rehearsal-extractor")
            .set(DOCUMENT_ASPECTS.SOURCE_URI, sourceUri)
            .set(DOCUMENT_ASPECTS.DOC_ID, docId)
            .execute();
    }

    /**
     * Manifest row with {@code collection} deliberately NULL (pre-catalog-014
     * shape). chunk_index is included, set equal to position (hygiene-001-8,
     * nexus-tk070.p6a follow-on: chunk_index is NOT NULL now, and this row's
     * 32-hex chash is the DELIBERATE 16-byte legacy shape rdr180-001's own
     * header documents as un-rekeyed-and-tolerated -- its octet CHECK stays
     * NOT VALID for exactly such rows). Review round (critic census
     * finding): hygiene-001-5/-8's DELETEs now key DIRECTLY on
     * octet_length(chash) &lt;&gt; 32, not on a chunk_index/metadata-NULL
     * proxy -- every caller of this helper passes a legacy-width chash, so
     * every row it seeds IS deleted by HEAD (hygiene-001-5's FK-safety
     * cleanup for the ones referencing a doomed chunks row, hygiene-001-8's
     * own DELETE for any orphan). Supplying chunk_index up front is no
     * longer about survival; it only means "this row was never a candidate
     * for the chunk_index BACKFILL half of hygiene-001-8" (a real, distinct
     * fact, same as chunks.metadata above). The DELETE path is covered both
     * here (this test's own isEqualTo(0) assertions against these exact
     * seeds) and by Hygiene001NotNullMigrationRlsTest's dedicated
     * per-tenant legacy-width deletion coverage.
     */
    private static void seedManifestRow(Connection c, String tenant, String docId,
                                        int position, String chash) throws Exception {
        // chash is TEXT on the old leg (generated field: bytea), so it goes in through a named column.
        dsl(c).insertInto(CATALOG_DOCUMENT_CHUNKS)
            .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, tenant)
            .set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, docId)
            .set(CATALOG_DOCUMENT_CHUNKS.POSITION, position)
            .set(DSL.field(DSL.name("chash"), String.class), chash)
            .set(CATALOG_DOCUMENT_CHUNKS.CHUNK_INDEX, position)
            .execute();
    }

    /**
     * A catalog_links row, written pre-catalog-032 (no FK exists on the old
     * leg's tree) so {@code toTumbler} need not resolve to a
     * catalog_documents row — the shape catalog-032-1's anti-join remediation
     * (nexus-tk070.p1, RDR-194 § D2) must clean up on a real aged fleet box.
     */
    private static void seedLink(Connection c, String tenant, String fromTumbler,
                                 String toTumbler, String linkType) throws Exception {
        dsl(c).insertInto(CATALOG_LINKS)
            .set(CATALOG_LINKS.TENANT_ID, tenant)
            .set(CATALOG_LINKS.FROM_TUMBLER, fromTumbler)
            .set(CATALOG_LINKS.TO_TUMBLER, toTumbler)
            .set(CATALOG_LINKS.LINK_TYPE, linkType)
            .set(CATALOG_LINKS.CREATED_BY, "rehearsal-seed")
            .execute();
    }

    /**
     * A legacy- or canonical-width {@code nexus.frecency} row (nexus-lgdel.l1,
     * legacy-001-1). {@code chunkId} is passed through verbatim — the caller
     * chooses legacy-width (a 32-hex string) or canonical (64-hex) to exercise
     * either the DELETE or the KEEP arm of legacy-001-1's anti-shape DELETE.
     */
    private static void seedFrecencyRow(Connection c, String tenant, String chunkId) throws Exception {
        dsl(c).insertInto(FRECENCY)
            .set(FRECENCY.TENANT_ID, tenant)
            .set(FRECENCY.CHUNK_ID, chunkId)
            .execute();
    }

    /**
     * A {@code nexus.frecency} row with an EXPLICIT {@code ttl_days}
     * (nexus-tk070.p6b, telemetry-006-1). {@code 0} seeds the CONVERSION
     * arm (rewritten to {@code NULL}); {@code null} seeds the KEEP arm
     * (already permanent, must survive untouched). Distinct from {@link
     * #seedFrecencyRow}, which relies on the column's own DEFAULT
     * {@code ttl_days=0} — that row (legacy-001-1's canonical-width KEEP
     * fixture) is ALSO converted by telemetry-006-1, effect-asserted
     * separately alongside this method's own dedicated fixture.
     */
    private static void seedFrecencyTtlRow(Connection c, String tenant, String chunkId, Integer ttlDays)
            throws Exception {
        dsl(c).insertInto(FRECENCY)
            .set(FRECENCY.TENANT_ID, tenant)
            .set(FRECENCY.CHUNK_ID, chunkId)
            .set(FRECENCY.TTL_DAYS, ttlDays)
            .execute();
    }

    /**
     * A legacy- or canonical-width {@code nexus.relevance_log} row
     * (nexus-lgdel.l1, legacy-001-2) — same DELETE/KEEP-arm choice as
     * {@link #seedFrecencyRow}.
     */
    private static void seedRelevanceLogRow(Connection c, String tenant, String chunkId, String query)
            throws Exception {
        dsl(c).insertInto(RELEVANCE_LOG)
            .set(RELEVANCE_LOG.TENANT_ID, tenant)
            .set(RELEVANCE_LOG.QUERY, query)
            .set(RELEVANCE_LOG.CHUNK_ID, chunkId)
            .set(RELEVANCE_LOG.ACTION, "view")
            .set(RELEVANCE_LOG.TIMESTAMP, DSL.currentOffsetDateTime())
            .execute();
    }

    /** {@code ttl} on {@code nexus.memory} / {@code nexus.plans}: the OLD column, renamed {@code ttl_days} by -003-1. */
    private static final org.jooq.Field<Integer> LEGACY_TTL = DSL.field(DSL.name("ttl"), Integer.class);

    /**
     * A {@code nexus.memory} row (nexus-tk070.p6a, memory-003-1). {@code ttl}
     * null seeds the KEEP arm (permanent); {@code 0} seeds the DELETE arm
     * (unrepresentable under the new {@code ttl_days} CHECK).
     */
    private static void seedMemoryRow(Connection c, String tenant, String project,
                                       String title, Integer ttl) throws Exception {
        dsl(c).insertInto(MEMORY)
            .set(MEMORY.TENANT_ID, tenant)
            .set(MEMORY.PROJECT, project)
            .set(MEMORY.TITLE, title)
            .set(MEMORY.CONTENT, "content")
            .set(MEMORY.TIMESTAMP, DSL.currentOffsetDateTime())
            .set(LEGACY_TTL, ttl)
            .execute();
    }

    /**
     * A {@code nexus.plans} row (nexus-tk070.p6a, plans-003-1) — same
     * DELETE/KEEP-arm {@code ttl} choice as {@link #seedMemoryRow}.
     */
    private static void seedPlanRow(Connection c, String tenant, String project,
                                     String query, Integer ttl) throws Exception {
        // hygiene-001-11 (nexus-tk070.p6a follow-on) DELETEs any verb-IS-NULL
        // plans row (the pre-nexus-fiovt shakeout-residue class) regardless of
        // ttl_days -- an independent, LATER sweep from plans-003-1's own
        // ttl=0 DELETE this helper's two callers exist to probe. A real verb
        // keeps both rows out of hygiene-001-11's WHERE clause so this
        // helper continues to isolate plans-003-1's ttl selectivity alone.
        seedPlan(c, tenant, project, query, ttl, "research", DSL.currentOffsetDateTime());
    }

    /** A {@code nexus.plans} row at the OLD shape ({@code ttl}, not {@code ttl_days}). */
    private static void seedPlan(Connection c, String tenant, String project, String query, Integer ttl,
                                 String verb, org.jooq.Field<java.time.OffsetDateTime> createdAt) {
        dsl(c).insertInto(PLANS)
            .set(PLANS.TENANT_ID, tenant)
            .set(PLANS.PROJECT, project)
            .set(PLANS.QUERY, query)
            .set(PLANS.PLAN_JSON, org.jooq.JSONB.jsonb("{}"))
            .set(PLANS.CREATED_AT, createdAt)
            .set(LEGACY_TTL, ttl)
            .set(PLANS.VERB, verb)
            .execute();
    }

    /**
     * A {@code nexus.topics} row (nexus-tk070.p3b, taxonomy-010-1's FK parent
     * -- {@code topic_assignments.topic_id} REFERENCES {@code topics(id)} ON
     * DELETE CASCADE). Returns the generated {@code id} for
     * {@link #seedTopicAssignment} to reference.
     */
    private static long seedTopic(Connection c, String tenant, String collection, String label)
            throws Exception {
        return dsl(c).insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, tenant)
            .set(TOPICS.LABEL, label)
            .set(TOPICS.COLLECTION, collection)
            .set(TOPICS.CREATED_AT, DSL.currentOffsetDateTime())
            .returning(TOPICS.ID)
            .fetchOne(TOPICS.ID);
    }

    /**
     * A {@code nexus.topic_assignments} row with {@code source_collection}
     * deliberately NULL (nexus-tk070.p3b, taxonomy-010-1's remediation input
     * shape) -- the pre-P3a writer state every arm of taxonomy-010-1's
     * backfill/delete resolves.
     */
    private static void seedTopicAssignment(Connection c, String tenant, String docId, long topicId)
            throws Exception {
        seedTopicAssignment(c, tenant, docId, topicId, null);
    }

    /**
     * A {@code nexus.topic_assignments} row with an EXPLICIT, non-NULL
     * {@code source_collection} (RDR-194 critical fix round, 2026-08-17,
     * nexus-i3k3e) -- the cc4 census's 1,262-row wedge class. See
     * {@link Taxonomy010BackfillDirectIntegrationTest}'s identical overload
     * for the full derivation.
     */
    private static void seedTopicAssignment(
            Connection c, String tenant, String docId, long topicId, String sourceCollection)
            throws Exception {
        // doc_id is TEXT on the old leg (generated field: bytea), so it goes in through a named column.
        var ins = dsl(c).insertInto(TOPIC_ASSIGNMENTS)
            .set(TOPIC_ASSIGNMENTS.TENANT_ID, tenant)
            .set(DSL.field(DSL.name("doc_id"), String.class), docId)
            .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topicId)
            .set(TOPIC_ASSIGNMENTS.ASSIGNED_BY, "rehearsal-seed");
        if (sourceCollection != null) {
            ins = ins.set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, sourceCollection);
        }
        ins.execute();
    }

    // ── Helpers: schema assertions ───────────────────────────────────────────

    /** Liquibase's own bookkeeping table, schema-qualified (not left to search_path); no generated form. */
    private static final org.jooq.Table<?> DATABASECHANGELOG = DSL.table(DSL.name("public", "databasechangelog"));
    private static final org.jooq.Field<String> DBCL_ID = DSL.field(DSL.name("id"), String.class);
    private static final org.jooq.Field<String> DBCL_AUTHOR = DSL.field(DSL.name("author"), String.class);
    private static final org.jooq.Field<String> DBCL_EXECTYPE = DSL.field(DSL.name("exectype"), String.class);

    private static int changelogRowCount(Connection conn) throws Exception {
        return dsl(conn).fetchCount(DATABASECHANGELOG);
    }

    private static boolean changesetApplied(Connection conn, String id, String author) throws Exception {
        return dsl(conn).fetchExists(DATABASECHANGELOG, DBCL_ID.eq(id).and(DBCL_AUTHOR.eq(author)));
    }

    private static String changesetExecType(Connection conn, String id, String author) throws Exception {
        return dsl(conn).select(DBCL_EXECTYPE).from(DATABASECHANGELOG)
            .where(DBCL_ID.eq(id).and(DBCL_AUTHOR.eq(author)))
            .fetchOne(DBCL_EXECTYPE);
    }

    private static boolean constraintExists(Connection conn, String conname) throws Exception {
        return PgCatalogProbes.constraintExists(DSL.using(conn, SQLDialect.POSTGRES), conname);
    }

    private static boolean constraintValidated(Connection conn, String conname) throws Exception {
        return Boolean.TRUE.equals(
            PgCatalogProbes.constraintValidated(DSL.using(conn, SQLDialect.POSTGRES), conname));
    }


    /** One-column predicate over {@link PgCatalogProbes#columnInfo}; 1 when it holds, else 0 (absent column included). */
    private static int columnMatches(org.jooq.DSLContext ctx, String schema, String table, String column,
                                     java.util.function.Predicate<PgCatalogProbes.ColumnInfo> predicate) {
        return predicate.test(PgCatalogProbes.columnInfo(ctx, schema, table, column)) ? 1 : 0;
    }

    private static org.jooq.DSLContext dsl(Connection c) {
        return DSL.using(c, SQLDialect.POSTGRES);
    }

    /** nexus.chash_index: present on the old leg, dropped at HEAD (rdr187-2), so no generated table. */
    private static final org.jooq.Table<?> CHASH_INDEX_RETIRED = DSL.table(DSL.name("nexus", "chash_index"));

    /** nexus.taxonomy_centroids_retired_225 is excluded from codegen (pom.xml), so it is addressed by quoted name. */
    private static final org.jooq.Table<?> CENTROIDS_RETIRED_225 =
        DSL.table(DSL.name("nexus", "taxonomy_centroids_retired_225"));
    private static final org.jooq.Field<String> RETIRED_TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final org.jooq.Field<String> RETIRED_COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final org.jooq.Field<Long> RETIRED_TOPIC_ID = DSL.field(DSL.name("topic_id"), Long.class);
    private static final org.jooq.Field<String> RETIRED_LABEL = DSL.field(DSL.name("label"), String.class);

    /** taxonomy-014-2's anti-join: topics whose parent_id names no topic of the same tenant. */
    private static int topicsWithMissingParent(Connection c) {
        var t = TOPICS.as("t");
        var p = TOPICS.as("p");
        return dsl(c).fetchCount(t, t.PARENT_ID.isNotNull().and(DSL.notExists(
            DSL.selectOne().from(p).where(p.ID.eq(t.PARENT_ID)).and(p.TENANT_ID.eq(t.TENANT_ID)))));
    }

    /** taxonomy-014-3's anti-join: assignments whose topic_id names no topic of the same tenant. */
    private static int assignmentsWithMissingTopic(Connection c) {
        var a = TOPIC_ASSIGNMENTS.as("a");
        var t = TOPICS.as("t");
        return dsl(c).fetchCount(a, DSL.notExists(
            DSL.selectOne().from(t).where(t.ID.eq(a.TOPIC_ID)).and(t.TENANT_ID.eq(a.TENANT_ID))));
    }

    /** taxonomy-014-4/-5's anti-join: topic_links whose from (or to) topic names no topic of the same tenant. */
    private static int topicLinksWithMissingTopic(Connection c, boolean fromSide) {
        var l = TOPIC_LINKS.as("l");
        var t = TOPICS.as("t");
        var endpoint = fromSide ? l.FROM_TOPIC_ID : l.TO_TOPIC_ID;
        return dsl(c).fetchCount(l, DSL.notExists(
            DSL.selectOne().from(t).where(t.ID.eq(endpoint)).and(t.TENANT_ID.eq(l.TENANT_ID))));
    }

    private static Set<String> tablesInSchema(Connection conn, String schema) throws Exception {
        Set<String> names = new java.util.HashSet<>();
        ResultSet rs = conn.getMetaData().getTables(null, schema, null, new String[]{"TABLE", "PARTITIONED TABLE"});
        while (rs.next()) {
            names.add(rs.getString("TABLE_NAME").toLowerCase());
        }
        return names;
    }
}
