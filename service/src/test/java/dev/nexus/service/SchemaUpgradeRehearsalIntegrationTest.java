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
import java.sql.Statement;
import java.util.List;
import java.util.Set;

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
 * the long-running cloud-deployed reference (T2 {@code deployed-engine-version})
 * and its changelog tree predates {@code catalog-013-chash-checks-validate.xml}
 * entirely (first appears at v0.1.33+, introduced by commit {@code e1cd25f1}) —
 * confirmed by {@code git ls-tree engine-service-v0.1.17 -- .../db/changelog},
 * which lists only up to {@code catalog-011-collection-health-stale-age.xml}.
 * {@code catalog-002-hygiene.xml} (which unconditionally ADDs the five
 * chash-length CHECK constraints, the root of the ms57z divergence per
 * nexus-4m6i0.1's analysis) is byte-identical between v0.1.17 and HEAD, so the
 * old leg reaches the same constraint-bearing state the real fleet did. For the
 * data leg, the v0.1.17-to-HEAD hop keeps growing (originally six changelog
 * files — catalog-012/-013/-014, migration-001, service-tokens-003/-004 —
 * verified via {@code git ls-tree} diff at the time this leg was first
 * written; RDR-191 Phase 4's vectors-004-1/taxonomy-007-1 are the latest
 * additions). The subset that genuinely carries migration-time row-DML
 * against FORCE-RLS tables is NOT restated here as a count (a stale number
 * is worse than none) — the data leg's own SEED-COVERAGE contract block is
 * the mechanically-enforced source of truth (nexus-gm38i,
 * {@code tests/test_rehearsal_seed_coverage_lint.py}). (taxonomy-004's
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
 * migration COMPLETES with the stamp silently un-applied (0 of 2 rows), and
 * only this leg's effect assertion catches it.
 *
 * <p><strong>Tag availability.</strong> {@code git archive} needs {@link
 * #OLD_TAG} present locally. The test attempts a shallow {@code git fetch
 * --depth 1 origin tag} fallback before giving up; if the tag is still
 * unavailable (e.g. a checkout that skipped tag-fetching entirely) the test
 * SKIPS with a loud reason via {@link Assumptions#abort} rather than silently
 * passing. {@code service-ci.yml}'s checkout step sets {@code fetch-tags: true}
 * specifically so this skip path is never exercised in CI.
 */
class SchemaUpgradeRehearsalIntegrationTest {

    private static final Logger log = LoggerFactory.getLogger(SchemaUpgradeRehearsalIntegrationTest.class);

    /**
     * Old reference tag: long-running cloud-deployed reference (T2
     * {@code deployed-engine-version}), predates {@code catalog-013-chash-checks-validate.xml}
     * (first shipped v0.1.33+, commit e1cd25f1) AND {@code catalog-014-manifest-collection-stamp.xml}
     * so the divergence-injection point (catalog-002-hygiene.xml, byte-identical
     * old vs HEAD) is reached the same way a real aged fleet box reached it, and
     * the hop's row-DML changesets genuinely run.
     *
     * <p>ROTATION POLICY (nexus-7z6s7): this pin rots. As HEAD advances the
     * old-to-HEAD hop grows unboundedly, and once the fleet fully moves past
     * this tag the "real aged box" justification goes stale. Advance it to the
     * PREVIOUSLY-deployed engine tag whenever the cloud deployment reference
     * (T2 {@code deployed-engine-version}) materially advances — the
     * engine-release skill's post-deploy "bump downstream refs" step is the
     * trigger point. When bumping, re-verify the two structural facts this
     * test's design leans on: the tag's changelog tree must predate the
     * newest guard-bearing changelog under test, and the divergence-injection
     * file must be byte-identical between the tag and HEAD (see class javadoc).
     * For the data leg, also re-verify the tag predates the newest row-DML
     * changeset being exercised (currently catalog-013 / catalog-014) — a tag
     * that already contains them turns that leg's assertions vacuous (the
     * in-test changesetApplied gates fail loudly if that happens). Rotation
     * also requires regenerating the OLD_TAG changeset snapshot
     * ({@code uv run python scripts/gen_rehearsal_hop_manifest.py}) and
     * re-deriving the data leg's seed coverage — the Python seed-coverage
     * lint (nexus-gm38i) fails loudly until both are done.
     */
    private static final String OLD_TAG = "engine-service-v0.1.17";

    private static final String CHANGELOG_PATHSPEC = "service/src/main/resources/db/changelog";
    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";

    private static final String ADMIN_ROLE = "nexus_admin_rehearsal";
    private static final String ADMIN_PASS = "nexus_admin_rehearsal_pass";

    @Test
    void oldEngineChangelogTree_upgradesToHead_afterInjectedDivergence() throws Exception {
        Path oldChangelogRoot = ensureOldTreeMaterialized(repoRoot());

        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
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
                    conn.createStatement().execute(
                        "ALTER TABLE nexus.chunks_384 DROP CONSTRAINT IF EXISTS chunks_384_chash_len_check");
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
     * mechanically enforced, not conventional (nexus-gm38i):
     * {@code tests/test_rehearsal_seed_coverage_lint.py} derives the hop's
     * FORCE-RLS row-DML changeset set from the HEAD changelog minus the
     * OLD_TAG snapshot ({@code tests/data/rehearsal_old_tag_changesets.json})
     * and fails Python CI whenever this leg's declared seed coverage drifts
     * from it.
     */
    @Test
    void oldEngineChangelogTree_withLegacySeededRows_dataChangesetsActuallyExecute() throws Exception {
        Path oldChangelogRoot = ensureOldTreeMaterialized(repoRoot());

        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
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
                }

                // ── SEED, as superuser (implicit BYPASSRLS): models rows written
                // by old clients through the service role WITH a tenant GUC set —
                // exactly the population a real aged box carries into an upgrade.
                //
                // SEED-COVERAGE-BEGIN (nexus-gm38i contract — parsed by
                // tests/test_rehearsal_seed_coverage_lint.py; every hop
                // changeset whose row-DML this leg seeds inputs for and
                // effect-asserts, as "<id> <author>" lines; the lint fails if
                // this block, its Python declaration, and the derived hop set
                // ever disagree):
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
                // SEED-COVERAGE-END ─────────────────────────────────────────────
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    // FK parents (fk-002/fk-003's NOT VALID FKs, applied by the
                    // old leg, enforce on new writes).
                    for (String[] tc : new String[][]{
                        {"t1", "code__x"}, {"t1", "code__y"}, {"t2", "code__z"},
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

                    // nexus-j862l (RDR-191 GATE-2, seed-coverage lint follow-up,
                    // tests/test_rehearsal_seed_coverage_lint.py::
                    // test_hop_row_dml_changesets_equal_declared_rehearsal_seed_coverage):
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
                    seedTaxonomyCentroidLegacyContent(su, 384, "t1", "code__x", 900L, "centroid-384");
                    seedTaxonomyCentroidLegacyContent(su, 768, "t1", "code__y", 901L, "centroid-768");
                    seedTaxonomyCentroidLegacyContent(su, 1024, "t2", "code__z", 902L, "centroid-1024");

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

                    assertThat(count(su, "SELECT count(*) FROM nexus.chash_index"))
                        .as("superuser ground truth after seeding").isEqualTo(5);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks WHERE collection IS NULL"))
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
                    assertThat(count(admin, "SELECT count(*) FROM nexus.chash_index"))
                        .as("FORCE RLS must hide all chash_index rows from the non-BYPASSRLS owner")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.catalog_document_chunks"))
                        .as("FORCE RLS must hide all manifest rows from the non-BYPASSRLS owner")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.catalog_documents"))
                        .as("FORCE RLS must hide the seeded document from the non-BYPASSRLS owner "
                            + "— the join side of catalog-014-0's stamp, the both-tables lesson")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.catalog_links"))
                        .as("FORCE RLS must hide both seeded catalog_links rows from the "
                            + "non-BYPASSRLS owner — catalog-032-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.frecency"))
                        .as("FORCE RLS must hide both seeded frecency rows from the "
                            + "non-BYPASSRLS owner — legacy-001-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.relevance_log"))
                        .as("FORCE RLS must hide both seeded relevance_log rows from the "
                            + "non-BYPASSRLS owner — legacy-001-2's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.topic_assignments"))
                        .as("FORCE RLS must hide all five seeded topic_assignments rows from "
                            + "the non-BYPASSRLS owner — taxonomy-010-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.memory"))
                        .as("FORCE RLS must hide both seeded memory rows from the "
                            + "non-BYPASSRLS owner — memory-003-1's own toggle-wrap target")
                        .isEqualTo(0);
                    assertThat(count(admin, "SELECT count(*) FROM nexus.plans"))
                        .as("FORCE RLS must hide both seeded plans rows from the "
                            + "non-BYPASSRLS owner — plans-003-1's own toggle-wrap target")
                        .isEqualTo(0);
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
                    assertThat(count(su,
                        "SELECT count(*) FROM information_schema.tables "
                        + "WHERE table_schema = 'nexus' AND table_name = 'chash_index'"))
                        .as("chash_index gone at HEAD (rdr187-2)")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname LIKE 'chash_index_chash%'"))
                        .as("its constraints died with it")
                        .isEqualTo(0);

                    // catalog-014-0 leg: every seeded manifest row stamped with the
                    // owning document's physical_collection, none left NULL, and
                    // (now that the fixture backs each row with real content) still
                    // present after catalog-025's dangling-row cleanup runs later in
                    // this same hop.
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks "
                        + "WHERE collection = 'code__x'"))
                        .as("catalog-014-0's manifest_backfill() must have stamped both "
                            + "seeded rows from the owning doc's physical_collection, and "
                            + "the stamp must survive catalog-025's later dangling-row "
                            + "cleanup now that each row has matching chunks_384 content")
                        .isEqualTo(2);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks "
                        + "WHERE collection IS NULL"))
                        .as("no manifest row may remain un-stamped after catalog-014-0")
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks "
                        + "WHERE tenant_id = 't1' AND doc_id = '1.1.101'"))
                        .as("catalog-025-0's dangling-row DELETE must actually remove a "
                            + "correctly-stamped-but-content-less row under FORCE-RLS, "
                            + "proving the DELETE arm fires, not just the KEEP arm")
                        .isEqualTo(0);

                    // catalog-016-0 leg: the dedup backfill saw the seeded rows
                    // through its FORCE-RLS toggle and tombstoned the loser —
                    // exactly one LIVE row per (tenant, source_uri) survives,
                    // and it is the most-chunk-bearing winner.
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_documents "
                        + "WHERE source_uri = 'file:///seed/dup.md' AND deleted_at IS NULL"))
                        .as("catalog-016-0 must tombstone the duplicate-uri loser "
                            + "(an RLS-blind no-op would leave 2 live rows and fail "
                            + "016-1's unique-index creation)")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_documents "
                        + "WHERE tumbler = '1.1.202' AND deleted_at IS NULL"))
                        .as("the most-chunk-bearing row must be the surviving winner")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_documents "
                        + "WHERE tumbler = '1.1.201' AND deleted_at IS NOT NULL"))
                        .as("the loser must be TOMBSTONED, never hard-deleted")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_indexes "
                        + "WHERE schemaname = 'nexus' "
                        + "AND indexname = 'ux_catalog_documents_live_source_uri'"))
                        .as("catalog-016-1's partial unique index exists at HEAD")
                        .isEqualTo(1);

                    // Both fixes must RESTORE FORCE within their own changeset.
                    // (chash_index left the toggled-set observation with the
                    // DROP — RDR-187; the two surviving toggled tables pin it.)
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.catalog_document_chunks'::regclass, "
                        + "'nexus.catalog_documents'::regclass)"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.chunks WHERE tenant_id = 't1' "
                        + "AND collection = 'code__x' AND embedding_384 IS NOT NULL "
                        + "AND embedding_768 IS NULL AND embedding_1024 IS NULL"))
                        .as("the two j862l chunks_384 rows landed under embedding_384 only")
                        .isEqualTo(2);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.chunks WHERE tenant_id = 't1' "
                        + "AND collection = 'code__y' AND embedding_768 IS NOT NULL "
                        + "AND embedding_384 IS NULL AND embedding_1024 IS NULL"))
                        .as("the seeded chunks_768 row landed under embedding_768 only")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.chunks WHERE tenant_id = 't2' "
                        + "AND collection = 'code__z' AND embedding_1024 IS NOT NULL "
                        + "AND embedding_384 IS NULL AND embedding_768 IS NULL"))
                        .as("the seeded chunks_1024 row landed under embedding_1024 only")
                        .isEqualTo(1);
                    for (String t : new String[] {"chunks_384", "chunks_768", "chunks_1024"}) {
                        assertThat(count(su,
                            "SELECT count(*) FROM information_schema.tables "
                            + "WHERE table_schema = 'nexus' AND table_name = '" + t + "'"))
                            .as(t + " gone at HEAD (vectors-004-1 unify, same old-tag->HEAD hop)")
                            .isEqualTo(0);
                    }

                    // taxonomy-007-1 leg (nexus-jv3ue): the analogous straddle
                    // for nexus.taxonomy_centroids, all three dims fresh-seeded
                    // above (no chunks-style j862l reuse available here).
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.taxonomy_centroids WHERE tenant_id = 't1' "
                        + "AND collection = 'code__x' AND embedding_384 IS NOT NULL "
                        + "AND embedding_768 IS NULL AND embedding_1024 IS NULL"))
                        .as("the seeded taxonomy_centroids_384 row landed under embedding_384 only")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.taxonomy_centroids WHERE tenant_id = 't1' "
                        + "AND collection = 'code__y' AND embedding_768 IS NOT NULL "
                        + "AND embedding_384 IS NULL AND embedding_1024 IS NULL"))
                        .as("the seeded taxonomy_centroids_768 row landed under embedding_768 only")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.taxonomy_centroids WHERE tenant_id = 't2' "
                        + "AND collection = 'code__z' AND embedding_1024 IS NOT NULL "
                        + "AND embedding_384 IS NULL AND embedding_768 IS NULL"))
                        .as("the seeded taxonomy_centroids_1024 row landed under embedding_1024 only")
                        .isEqualTo(1);
                    for (String t : new String[] {
                            "taxonomy_centroids_384", "taxonomy_centroids_768", "taxonomy_centroids_1024"}) {
                        assertThat(count(su,
                            "SELECT count(*) FROM information_schema.tables "
                            + "WHERE table_schema = 'nexus' AND table_name = '" + t + "'"))
                            .as(t + " gone at HEAD (taxonomy-007-1 unify, same old-tag->HEAD hop)")
                            .isEqualTo(0);
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
                    // survived -- either failure mode is caught below.
                    assertThat(constraintExists(su, "fk_catalog_chunks_chunk"))
                        .as("fk_catalog_chunks_chunk must exist at HEAD (catalog-029-0)")
                        .isTrue();
                    assertThat(constraintValidated(su, "fk_catalog_chunks_chunk"))
                        .as("fk_catalog_chunks_chunk must be VALIDATED at HEAD -- catalog-029-2's "
                            + "VALIDATE only succeeds if catalog-029-1's anti-join DELETE actually "
                            + "ran (toggle correctly off) and left no dangling row behind")
                        .isTrue();
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks "
                        + "WHERE tenant_id = 't1' AND doc_id = '1.1.100'"))
                        .as("the two content-backed 1.1.100 manifest rows must still exist at HEAD -- "
                            + "a broken catalog-029-1 toggle (FORCE never turned off) would see zero "
                            + "nexus.chunks rows and wrongly delete these two as false-dangling")
                        .isEqualTo(2);

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
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.chunks'::regclass, 'nexus.catalog_collections'::regclass)"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.chunks'::regclass, 'nexus.catalog_collections'::regclass)"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_links "
                        + "WHERE tenant_id = 't1' AND to_tumbler = '1.1.999-ghost'"))
                        .as("catalog-032-1's anti-join DELETE must actually remove the "
                            + "never-registered-endpoint link under FORCE-RLS, proving the "
                            + "DELETE arm fires, not just the KEEP arm")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_links "
                        + "WHERE tenant_id = 't1' AND from_tumbler = '1.1.100' AND to_tumbler = '1.1.101'"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.catalog_links'::regclass, 'nexus.catalog_documents'::regclass)"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.frecency WHERE tenant_id = 't1' AND chunk_id = '"
                        + "e".repeat(32) + "'"))
                        .as("legacy-001-1's shape-agnostic DELETE must actually remove the "
                            + "legacy-width frecency row under FORCE-RLS, proving the DELETE "
                            + "arm fires, not just the KEEP arm")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.frecency WHERE tenant_id = 't1' AND chunk_id = '"
                        + "d".repeat(64) + "'"))
                        .as("the canonical-width frecency row must survive legacy-001-1's "
                            + "remediation — a shape-agnostic-gone-wrong DELETE would wrongly "
                            + "remove this too")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.relevance_log WHERE tenant_id = 't1' AND chunk_id = '"
                        + "9".repeat(32) + "'"))
                        .as("legacy-001-2's shape-agnostic DELETE must actually remove the "
                            + "legacy-width relevance_log row under FORCE-RLS, proving the "
                            + "DELETE arm fires, not just the KEEP arm")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.relevance_log WHERE tenant_id = 't1' AND chunk_id = '"
                        + "f".repeat(64) + "'"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_assignments "
                        + "WHERE tenant_id = 't1' AND source_collection IS NULL"))
                        .as("no source_collection-NULL row may survive taxonomy-010-1 -- SET NOT "
                            + "NULL would otherwise have failed the whole migration walk")
                        .isEqualTo(0);
                    // RDR-194 P3c (nexus-tk070.p3c): doc_id is bytea at HEAD now (this
                    // leg runs the FULL hop, which includes taxonomy-011-1's ALTER) --
                    // these three comparisons use encode(doc_id,'hex') rather than a
                    // bare string literal. A raw `doc_id = '<64-char-string>'` against a
                    // bytea column is NOT a type error (Postgres accepts it as legacy
                    // "escape format" input, silently reinterpreting the hex-looking
                    // characters as their own raw ASCII bytes) -- it would compile and
                    // ALWAYS evaluate false regardless of the row's real content, making
                    // an isEqualTo(0) assertion vacuously true even if the targeted
                    // DELETE had a bug and left the row behind. encode(doc_id,'hex')
                    // keeps the comparison on the TEXT side, so it fails loud if a row
                    // this arm was supposed to delete unexpectedly survives.
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_assignments "
                        + "WHERE tenant_id = 't1' AND encode(doc_id, 'hex') = '" + "b".repeat(32) + "'"))
                        .as("the ambiguous arm's row must be DELETED by taxonomy-010-1 -- its chash "
                            + "resolves to two distinct collections and cannot be attributed")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_assignments "
                        + "WHERE tenant_id = 't1' AND encode(doc_id, 'hex') = '" + "c".repeat(64) + "'"))
                        .as("the unresolvable (anti-join) arm's row must be DELETED -- its doc_id is "
                            + "canonical 64-hex SHAPE but no nexus.chunks row matches it at all, "
                            + "proving the anti-join arm fires independently of the shape predicate")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_assignments "
                        + "WHERE tenant_id = 't1' AND encode(doc_id, 'hex') = '" + "1".repeat(32) + "'"))
                        .as("the legacy-shape-coincidental-match arm's row must be DELETED despite "
                            + "a real chunk existing at the same text-matched chash -- the "
                            + "doc_id !~ '^[0-9a-f]{64}$' shape predicate must win over a coincidental "
                            + "anti-join match, the exact cc4/HAL no-wedge proof")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_assignments "
                        + "WHERE tenant_id = 't1' AND encode(doc_id, 'hex') = '" + "8".repeat(32) + "'"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.topic_assignments'::regclass, 'nexus.chunks'::regclass)"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-010-1 toggled")
                        .isEqualTo(2);
                    assertThat(count(su,
                        "SELECT count(*) FROM information_schema.columns "
                        + "WHERE table_schema = 'nexus' AND table_name = 'topic_assignments' "
                        + "AND column_name = 'source_collection' AND is_nullable = 'NO'"))
                        .as("source_collection must be SET NOT NULL at HEAD -- taxonomy-010-1's "
                            + "final DDL step, only reachable if all three DML steps ahead of it "
                            + "left no NULL row behind")
                        .isEqualTo(1);

                    // taxonomy-011-1 leg (nexus-tk070.p3c, RDR-194 § D1 step (e), seed-
                    // coverage lint follow-up). This changeset carries NO INSERT/UPDATE/
                    // DELETE -- its own guard is a read-only SELECT COUNT(*) wrapped in
                    // the SAME NO FORCE/FORCE toggle shape as fk-004-0-reconcile-precount
                    // (nexus-iq0qr, see that entry's own comment in the Python
                    // DECLARED_SEED_COVERAGE for the identical structural reasoning),
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
                    assertThat(count(su,
                        "SELECT count(*) FROM information_schema.columns "
                        + "WHERE table_schema = 'nexus' AND table_name = 'topic_assignments' "
                        + "AND column_name = 'doc_id' AND udt_name = 'bytea'"))
                        .as("topic_assignments.doc_id must be bytea at HEAD -- taxonomy-011-1's "
                            + "ALTER COLUMN TYPE, only reachable if the guard above found zero "
                            + "non-canonical rows")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity "
                        + "AND oid = 'nexus.topic_assignments'::regclass"))
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
                    // the Python DECLARED_SEED_COVERAGE): "any row that would exercise
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_assignments WHERE tenant_id = 't1'"))
                        .as("taxonomy-012-2's own anti-join population is empty -- every row "
                            + "this fixture seeded was already removed by taxonomy-010-1's three "
                            + "DELETE arms above, so taxonomy-012-2 by construction deletes nothing "
                            + "new in this hop")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname = 'topic_assignments_chunk_fk' AND convalidated"))
                        .as("topic_assignments_chunk_fk must exist and be VALIDATED at HEAD -- "
                            + "only reachable if taxonomy-012-1's NOT VALID add, taxonomy-012-2's "
                            + "remediation, and taxonomy-012-3's VALIDATE all ran in this same walk")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.topic_assignments'::regclass, 'nexus.chunks'::regclass)"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topics t WHERE t.parent_id IS NOT NULL "
                        + "AND NOT EXISTS (SELECT 1 FROM nexus.topics p WHERE p.id = t.parent_id "
                        + "AND p.tenant_id = t.tenant_id)"))
                        .as("taxonomy-014-2's own anti-join population is empty -- the sole "
                            + "nexus.topics row this hop seeds carries a NULL parent_id, so the "
                            + "self-referential anti-join by construction finds nothing")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname = 'fk_topics_parent_tenant' AND convalidated"))
                        .as("fk_topics_parent_tenant must exist and be VALIDATED at HEAD -- only "
                            + "reachable if taxonomy-014-2's DROP, ADD NOT VALID, anti-join, and "
                            + "VALIDATE all ran in this same walk")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity "
                        + "AND oid = 'nexus.topics'::regclass"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.topics after "
                            + "taxonomy-014-2's own toggle-wrap")
                        .isEqualTo(1);

                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_assignments a WHERE NOT EXISTS "
                        + "(SELECT 1 FROM nexus.topics t WHERE t.id = a.topic_id "
                        + "AND t.tenant_id = a.tenant_id)"))
                        .as("taxonomy-014-3's own anti-join population is empty -- "
                            + "nexus.topic_assignments for tenant t1 was already drained to zero "
                            + "by taxonomy-010-1's three DELETE arms above")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname = 'fk_topic_assignments_topic_tenant' AND convalidated"))
                        .as("fk_topic_assignments_topic_tenant must exist and be VALIDATED at HEAD")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.topic_assignments'::regclass, 'nexus.topics'::regclass)"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-014-3's own "
                            + "toggle-wrap covers")
                        .isEqualTo(2);

                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_links l WHERE NOT EXISTS "
                        + "(SELECT 1 FROM nexus.topics t WHERE t.id = l.from_topic_id "
                        + "AND t.tenant_id = l.tenant_id)"))
                        .as("taxonomy-014-4's own anti-join population is empty -- "
                            + "nexus.topic_links is never seeded anywhere in this hop")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname = 'fk_topic_links_from_topic_tenant' AND convalidated"))
                        .as("fk_topic_links_from_topic_tenant must exist and be VALIDATED at HEAD")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.topic_links'::regclass, 'nexus.topics'::regclass)"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-014-4's own "
                            + "toggle-wrap covers")
                        .isEqualTo(2);

                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.topic_links l WHERE NOT EXISTS "
                        + "(SELECT 1 FROM nexus.topics t WHERE t.id = l.to_topic_id "
                        + "AND t.tenant_id = l.tenant_id)"))
                        .as("taxonomy-014-5's own anti-join population is empty -- "
                            + "nexus.topic_links is never seeded anywhere in this hop")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname = 'fk_topic_links_to_topic_tenant' AND convalidated"))
                        .as("fk_topic_links_to_topic_tenant must exist and be VALIDATED at HEAD")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.topic_links'::regclass, 'nexus.topics'::regclass)"))
                        .as("FORCE ROW LEVEL SECURITY restored on both tables taxonomy-014-5's own "
                            + "toggle-wrap covers")
                        .isEqualTo(2);

                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint WHERE conname = 'topics_tenant_id_unique'"))
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
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.memory WHERE tenant_id = 't1' "
                        + "AND project = 'p6a-proj' AND title = 'p6a-zero'"))
                        .as("memory-003-1's counted DELETE must actually remove the seeded "
                            + "ttl=0 row -- the DELETE arm")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.memory WHERE tenant_id = 't1' "
                        + "AND project = 'p6a-proj' AND title = 'p6a-permanent' "
                        + "AND ttl_days IS NULL"))
                        .as("the NULL-ttl (permanent) decoy row must survive memory-003-1's "
                            + "DELETE untouched -- the KEEP arm, proving the DELETE is "
                            + "selective on ttl=0 and not a blanket wipe")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = "
                        + "'nexus' AND table_name = 'memory' AND column_name = 'ttl_days'"))
                        .as("nexus.memory.ttl must be renamed to ttl_days at HEAD")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname = 'memory_ttl_days_positive_chk'"))
                        .as("memory_ttl_days_positive_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity "
                        + "AND oid = 'nexus.memory'::regclass"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.memory after "
                            + "memory-003-1's own toggle-wrap")
                        .isEqualTo(1);

                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.plans WHERE tenant_id = 't1' "
                        + "AND project = 'p6a-proj' AND query = 'p6a zero query'"))
                        .as("plans-003-1's counted DELETE must actually remove the seeded "
                            + "ttl=0 row -- the DELETE arm")
                        .isEqualTo(0);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.plans WHERE tenant_id = 't1' "
                        + "AND project = 'p6a-proj' AND query = 'p6a permanent query' "
                        + "AND ttl_days IS NULL"))
                        .as("the NULL-ttl (permanent) decoy row must survive plans-003-1's "
                            + "DELETE untouched -- the KEEP arm")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = "
                        + "'nexus' AND table_name = 'plans' AND column_name = 'ttl_days'"))
                        .as("nexus.plans.ttl must be renamed to ttl_days at HEAD")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_constraint "
                        + "WHERE conname = 'plans_ttl_days_positive_chk'"))
                        .as("plans_ttl_days_positive_chk must exist at HEAD")
                        .isEqualTo(1);
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity "
                        + "AND oid = 'nexus.plans'::regclass"))
                        .as("FORCE ROW LEVEL SECURITY restored on nexus.plans after "
                            + "plans-003-1's own toggle-wrap")
                        .isEqualTo(1);
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
                + "service-ci.yml's checkout step sets fetch-tags:true precisely so this never fires in CI; "
                + "a persistent local skip means that checkout convention regressed.", OLD_TAG);
            Assumptions.abort(
                "Old tag " + OLD_TAG + " unavailable locally and shallow fetch fallback failed; "
                + "skipping schema-upgrade rehearsal. This must NOT happen in CI — verify "
                + "service-ci.yml's checkout step still sets fetch-tags: true.");
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
     * non-superuser owner path) — so nexus_svc must be pre-created here as
     * superuser, same as production DBA pre-provisioning. role-001-1's IF NOT
     * EXISTS guard then makes it a no-op during the old leg. The role is also
     * NOBYPASSRLS (the CREATE ROLE default) — load-bearing for the data leg:
     * FORCE-RLS policies apply to it even as table owner, exactly as they do
     * to production's nexus_admin.
     */
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

    private static void registerCollection(Connection c, String tenant, String name) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_collections (tenant_id, name) "
            + "VALUES (?, ?) ON CONFLICT DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, name);
            ps.executeUpdate();
        }
    }

    private static void seedChashRow(Connection c, String tenant, String chash,
                                     String collection) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
            + "VALUES (?, ?, ?, now())")) {
            ps.setString(1, tenant);
            ps.setString(2, chash);
            ps.setString(3, collection);
            ps.executeUpdate();
        }
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
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.chunks_384 (tenant_id, collection, chash, chunk_text, embedding) "
            + "VALUES (?, ?, ?, ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setString(3, chash);
            ps.setString(4, text);
            ps.setString(5, "[" + "0,".repeat(383) + "0]");
            ps.executeUpdate();
        }
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
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.chunks_" + dim
            + " (tenant_id, collection, chash, chunk_text, embedding) "
            + "VALUES (?, ?, ?, ?, ?::vector) ON CONFLICT (tenant_id, collection, chash) DO NOTHING")) {
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setString(3, chash);
            ps.setString(4, text);
            ps.setString(5, "[" + "0,".repeat(dim - 1) + "0]");
            ps.executeUpdate();
        }
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
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.taxonomy_centroids_" + dim
            + " (tenant_id, collection, topic_id, embedding, label, doc_count) "
            + "VALUES (?, ?, ?, ?::vector, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, collection);
            ps.setLong(3, topicId);
            ps.setString(4, "[" + "0.01,".repeat(dim - 1) + "0.01]");
            ps.setString(5, label);
            ps.setInt(6, 3);
            ps.executeUpdate();
        }
    }

    private static void seedDocument(Connection c, String tenant, String tumbler,
                                     String title, String physicalCollection) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
            + "VALUES (?, ?, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, tumbler);
            ps.setString(3, title);
            ps.setString(4, physicalCollection);
            ps.executeUpdate();
        }
    }

    /** Document row WITH a source_uri + chunk_count (catalog-016-0's dedup input shape). */
    private static void seedDocumentWithUri(Connection c, String tenant, String tumbler,
                                            String title, String physicalCollection,
                                            String sourceUri, int chunkCount) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_documents "
            + "(tenant_id, tumbler, title, physical_collection, source_uri, chunk_count) "
            + "VALUES (?, ?, ?, ?, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, tumbler);
            ps.setString(3, title);
            ps.setString(4, physicalCollection);
            ps.setString(5, sourceUri);
            ps.setInt(6, chunkCount);
            ps.executeUpdate();
        }
    }

    /** Manifest row with {@code collection} deliberately NULL (pre-catalog-014 shape). */
    private static void seedManifestRow(Connection c, String tenant, String docId,
                                        int position, String chash) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
            + "VALUES (?, ?, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, docId);
            ps.setInt(3, position);
            ps.setString(4, chash);
            ps.executeUpdate();
        }
    }

    /**
     * A catalog_links row, written pre-catalog-032 (no FK exists on the old
     * leg's tree) so {@code toTumbler} need not resolve to a
     * catalog_documents row — the shape catalog-032-1's anti-join remediation
     * (nexus-tk070.p1, RDR-194 § D2) must clean up on a real aged fleet box.
     */
    private static void seedLink(Connection c, String tenant, String fromTumbler,
                                 String toTumbler, String linkType) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by) "
            + "VALUES (?, ?, ?, ?, 'rehearsal-seed')")) {
            ps.setString(1, tenant);
            ps.setString(2, fromTumbler);
            ps.setString(3, toTumbler);
            ps.setString(4, linkType);
            ps.executeUpdate();
        }
    }

    /**
     * A legacy- or canonical-width {@code nexus.frecency} row (nexus-lgdel.l1,
     * legacy-001-1). {@code chunkId} is passed through verbatim — the caller
     * chooses legacy-width (a 32-hex string) or canonical (64-hex) to exercise
     * either the DELETE or the KEEP arm of legacy-001-1's anti-shape DELETE.
     */
    private static void seedFrecencyRow(Connection c, String tenant, String chunkId) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.frecency (tenant_id, chunk_id) VALUES (?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, chunkId);
            ps.executeUpdate();
        }
    }

    /**
     * A legacy- or canonical-width {@code nexus.relevance_log} row
     * (nexus-lgdel.l1, legacy-001-2) — same DELETE/KEEP-arm choice as
     * {@link #seedFrecencyRow}.
     */
    private static void seedRelevanceLogRow(Connection c, String tenant, String chunkId, String query)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, action, timestamp) "
            + "VALUES (?, ?, ?, 'view', now())")) {
            ps.setString(1, tenant);
            ps.setString(2, query);
            ps.setString(3, chunkId);
            ps.executeUpdate();
        }
    }

    /**
     * A {@code nexus.memory} row (nexus-tk070.p6a, memory-003-1). {@code ttl}
     * null seeds the KEEP arm (permanent); {@code 0} seeds the DELETE arm
     * (unrepresentable under the new {@code ttl_days} CHECK).
     */
    private static void seedMemoryRow(Connection c, String tenant, String project,
                                       String title, Integer ttl) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp, ttl) "
            + "VALUES (?, ?, ?, 'content', now(), ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, project);
            ps.setString(3, title);
            if (ttl == null) {
                ps.setNull(4, java.sql.Types.INTEGER);
            } else {
                ps.setInt(4, ttl);
            }
            ps.executeUpdate();
        }
    }

    /**
     * A {@code nexus.plans} row (nexus-tk070.p6a, plans-003-1) — same
     * DELETE/KEEP-arm {@code ttl} choice as {@link #seedMemoryRow}.
     */
    private static void seedPlanRow(Connection c, String tenant, String project,
                                     String query, Integer ttl) throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.plans (tenant_id, project, query, plan_json, created_at, ttl) "
            + "VALUES (?, ?, ?, '{}'::jsonb, now(), ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, project);
            ps.setString(3, query);
            if (ttl == null) {
                ps.setNull(4, java.sql.Types.INTEGER);
            } else {
                ps.setInt(4, ttl);
            }
            ps.executeUpdate();
        }
    }

    /**
     * A {@code nexus.topics} row (nexus-tk070.p3b, taxonomy-010-1's FK parent
     * -- {@code topic_assignments.topic_id} REFERENCES {@code topics(id)} ON
     * DELETE CASCADE). Returns the generated {@code id} for
     * {@link #seedTopicAssignment} to reference.
     */
    private static long seedTopic(Connection c, String tenant, String collection, String label)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) "
            + "VALUES (?, ?, ?, now()) RETURNING id")) {
            ps.setString(1, tenant);
            ps.setString(2, label);
            ps.setString(3, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    /**
     * A {@code nexus.topic_assignments} row with {@code source_collection}
     * deliberately NULL (nexus-tk070.p3b, taxonomy-010-1's remediation input
     * shape) -- the pre-P3a writer state every arm of taxonomy-010-1's
     * backfill/delete resolves.
     */
    private static void seedTopicAssignment(Connection c, String tenant, String docId, long topicId)
            throws Exception {
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by) "
            + "VALUES (?, ?, ?, 'rehearsal-seed')")) {
            ps.setString(1, tenant);
            ps.setString(2, docId);
            ps.setLong(3, topicId);
            ps.executeUpdate();
        }
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
        try (var ps = c.prepareStatement(
            "INSERT INTO nexus.topic_assignments "
            + "(tenant_id, doc_id, topic_id, assigned_by, source_collection) "
            + "VALUES (?, ?, ?, 'rehearsal-seed', ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, docId);
            ps.setLong(3, topicId);
            ps.setString(4, sourceCollection);
            ps.executeUpdate();
        }
    }

    // ── Helpers: schema assertions ───────────────────────────────────────────

    private static int changelogRowCount(Connection conn) throws Exception {
        ResultSet rs = conn.createStatement().executeQuery(
            "SELECT COUNT(*) FROM public.\"databasechangelog\"");
        rs.next();
        return rs.getInt(1);
    }

    private static boolean changesetApplied(Connection conn, String id, String author) throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT 1 FROM databasechangelog WHERE id = ? AND author = ?")) {
            ps.setString(1, id);
            ps.setString(2, author);
            return ps.executeQuery().next();
        }
    }

    private static String changesetExecType(Connection conn, String id, String author) throws Exception {
        try (var ps = conn.prepareStatement(
                "SELECT exectype FROM databasechangelog WHERE id = ? AND author = ?")) {
            ps.setString(1, id);
            ps.setString(2, author);
            ResultSet rs = ps.executeQuery();
            return rs.next() ? rs.getString("exectype") : null;
        }
    }

    private static boolean constraintExists(Connection conn, String conname) throws Exception {
        try (var ps = conn.prepareStatement("SELECT 1 FROM pg_constraint WHERE conname = ?")) {
            ps.setString(1, conname);
            return ps.executeQuery().next();
        }
    }

    private static boolean constraintValidated(Connection conn, String conname) throws Exception {
        try (var ps = conn.prepareStatement("SELECT convalidated FROM pg_constraint WHERE conname = ?")) {
            ps.setString(1, conname);
            ResultSet rs = ps.executeQuery();
            return rs.next() && rs.getBoolean("convalidated");
        }
    }

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private static Set<String> tablesInSchema(Connection conn, String schema) throws Exception {
        Set<String> names = new java.util.HashSet<>();
        ResultSet rs = conn.getMetaData().getTables(null, schema, null, new String[]{"TABLE"});
        while (rs.next()) {
            names.add(rs.getString("TABLE_NAME").toLowerCase());
        }
        return names;
    }
}
