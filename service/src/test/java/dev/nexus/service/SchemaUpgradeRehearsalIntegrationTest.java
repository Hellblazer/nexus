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
 * data leg, the v0.1.17-to-HEAD hop adds exactly six changelog files
 * (catalog-012/-013/-014, migration-001, service-tokens-003/-004; verified via
 * {@code git ls-tree} diff), of which exactly two contain migration-time
 * row-DML against FORCE-RLS tables: catalog-013's chash normalization and
 * catalog-014-0's manifest collection stamp — both genuinely execute during
 * the HEAD leg here. (taxonomy-004's root-topic dedup is already IN the
 * v0.1.17 tree — the old leg applies it and its unique root-topic index, so
 * duplicate-root seeding is neither possible nor a real fleet exposure.)
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

        PostgreSQLContainer<?> pg = PgContainerHelper.start();
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

                    // RDR-180 era end-state: the TEXT-era len_checks are gone
                    // (rdr180-2 drops all five — the injected divergence is
                    // tolerated via DROP IF EXISTS), replaced by the octet
                    // CHECKs added NOT VALID (validated ONLY by the client
                    // rung's admin connection post-rekey, never at boot —
                    // the GH #1390 crash-loop class, retired by design).
                    for (String t : new String[] {
                        "chunks_384", "chunks_768", "chunks_1024",
                        "catalog_document_chunks"}) {
                        assertThat(constraintExists(conn, t + "_chash_len_check"))
                            .as("%s_chash_len_check must be gone post-rdr180-2", t)
                            .isFalse();
                        assertThat(constraintExists(conn, t + "_chash_octet_check"))
                            .as("%s_chash_octet_check must exist post-rdr180-11", t)
                            .isTrue();
                        assertThat(constraintValidated(conn, t + "_chash_octet_check"))
                            .as("%s octet CHECK stays NOT VALID at boot (rung validates)", t)
                            .isFalse();
                    }

                    assertThat(tablesInSchema(conn, "nexus"))
                        .as("core catalog/chunk tables must exist after the old-tag->HEAD hop")
                        .containsAll(Set.of(
                            "chunks_384", "chunks_768", "chunks_1024",
                            "catalog_document_chunks", "memory"));
                    // RDR-187 (nexus-piwya.9): the router died at the DROP —
                    // the hop's END STATE has no chash_index at all.
                    assertThat(tablesInSchema(conn, "nexus"))
                        .as("chash_index must be GONE at HEAD (rdr187-2)")
                        .doesNotContain("chash_index");
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
     * inputs here — catalog-013 and catalog-014-0 are the ONLY two in the
     * current v0.1.17-to-HEAD hop ({@code git ls-tree} diff, see class
     * javadoc). The toggle-wrapped discipline itself is additionally enforced
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

        PostgreSQLContainer<?> pg = PgContainerHelper.start();
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
                // SEED-COVERAGE-END ─────────────────────────────────────────────
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    // FK parents (fk-002/fk-003's NOT VALID FKs, applied by the
                    // old leg, enforce on new writes).
                    for (String[] tc : new String[][]{
                        {"t1", "code__x"}, {"t1", "code__y"}, {"t2", "code__z"}}) {
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

                    assertThat(count(su, "SELECT count(*) FROM nexus.chash_index"))
                        .as("superuser ground truth after seeding").isEqualTo(5);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks WHERE collection IS NULL"))
                        .as("superuser ground truth after seeding").isEqualTo(2);
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
                    // owning document's physical_collection, none left NULL.
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks "
                        + "WHERE collection = 'code__x'"))
                        .as("catalog-014-0's manifest_backfill() must have stamped both "
                            + "seeded rows from the owning doc's physical_collection")
                        .isEqualTo(2);
                    assertThat(count(su,
                        "SELECT count(*) FROM nexus.catalog_document_chunks "
                        + "WHERE collection IS NULL"))
                        .as("no manifest row may remain un-stamped after catalog-014-0")
                        .isEqualTo(0);

                    // Both fixes must RESTORE FORCE within their own changeset.
                    // (chash_index left the toggled-set observation with the
                    // DROP — RDR-187; the two surviving toggled tables pin it.)
                    assertThat(count(su,
                        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND oid IN ("
                        + "'nexus.catalog_document_chunks'::regclass, "
                        + "'nexus.catalog_documents'::regclass)"))
                        .as("FORCE ROW LEVEL SECURITY restored on every toggled surviving table")
                        .isEqualTo(2);

                    // Contrast pin vs the schema-divergence test: with no injected
                    // divergence all five constraints exist, so catalog-013-2's
                    // precondition passes and it EXECUTES (not MARK_RAN).
                    assertThat(changesetExecType(su, "catalog-013-2", "nexus-e0hd2"))
                        .as("with all five constraints present, catalog-013-2 must EXECUTE for real")
                        .isEqualTo("EXECUTED");
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
