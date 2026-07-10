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
import java.util.List;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * nexus-4m6i0.6 (Tier 2 of the b6qlf/ms57z systemic migration-safety hardening,
 * nexus-4m6i0) — the ONE mechanism that would have caught nexus-ms57z (GH #1390)
 * before it reached production, and a generalized net for the SCHEMA-shaped
 * half of the migration-divergence class.
 *
 * <p><strong>Scope limitation — schema-only (nexus-u5dln).</strong> The old leg
 * starts from an EMPTY database: no rows are seeded before the old-to-HEAD hop.
 * That covers schema-state divergences (missing/extra constraints, the ms57z
 * shape) but structurally cannot reproduce DATA-dependent divergences — e.g.
 * the nexus-1wjmq class (v0.1.33: FORCE-RLS hid pre-existing legacy 64-char
 * chash ROWS from the non-BYPASSRLS migration owner, so a data-normalization
 * changeset silently no-op'd while its later VALIDATE saw the rows and failed).
 * A data-bearing old leg (seed representative legacy-shaped rows before the
 * hop) is scoped as follow-up bead nexus-u5dln; until it lands, do not read
 * this class as "the aged-fleet-state gate" for anything data-shaped.
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
 * exactly the upgrade a real fleet performs.
 *
 * <p><strong>What this test does differently.</strong> It materializes the
 * {@code service/src/main/resources/db/changelog} tree exactly as it existed at
 * {@link #OLD_TAG} (via {@code git archive}, not the classpath), drives {@code
 * liquibase.Liquibase} directly against that historical tree with a filesystem
 * {@link DirectoryResourceAccessor}, injects the verbatim ms57z divergence
 * ({@code DROP CONSTRAINT chunks_384_chash_len_check}), and only then calls the
 * REAL production entry point ({@link SchemaMigrator#migrate}) — which resolves
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
 * old leg reaches the same constraint-bearing state the real fleet did.
 *
 * <p><strong>RED before nexus-4m6i0.1 / GREEN after.</strong> nexus-4m6i0.1 is
 * already merged (commit 1ac12e1f) with its own RED-then-GREEN regression proof
 * (Test 5/6 in {@link SchemaMigratorIntegrationTest}, git-stash verified). Per
 * the nexus-4m6i0.6 design note, re-deriving that RED state here (e.g. by
 * pointing the HEAD leg at the pre-fix commit) would duplicate that proof
 * without adding coverage; this test's unique value is the old-tag-changelog-
 * tree hop itself, which no other test exercises. It remains GREEN only because
 * the guard already landed — if catalog-013-2's precondition regressed, this
 * test would fail exactly like Test 5/6 do.
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
     * (first shipped v0.1.33+, commit e1cd25f1) so the divergence-injection point
     * (catalog-002-hygiene.xml, byte-identical old vs HEAD) is reached the same way
     * a real aged fleet box reached it.
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
     */
    private static final String OLD_TAG = "engine-service-v0.1.17";

    private static final String CHANGELOG_PATHSPEC = "service/src/main/resources/db/changelog";
    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";

    @Test
    void oldEngineChangelogTree_upgradesToHead_afterInjectedDivergence() throws Exception {
        Path repoRoot = repoRoot();

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

        PostgreSQLContainer<?> pg = PgContainerHelper.start();
        final String adminRole = "nexus_admin_rehearsal";
        final String adminPass = "nexus_admin_rehearsal_pass";
        try {
            // Phase A: minimal DBA-equivalent bootstrap (mirrors
            // SchemaMigratorIntegrationTest.bootstrap()). role-001-nexus-svc.xml's
            // self-create branch requires CREATEROLE, which the non-superuser
            // adminRole deliberately lacks (proving the production non-superuser
            // owner path) — so nexus_svc must be pre-created here as superuser,
            // same as production DBA pre-provisioning. role-001-1's IF NOT EXISTS
            // guard then makes it a no-op during the old leg.
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "CREATE ROLE " + adminRole + " LOGIN PASSWORD '" + adminPass
                        + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
                su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + adminRole);
                su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + adminRole);
                su.createStatement().execute(
                    "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' "
                        + "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
                su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
            }

            var cfg = new HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(adminRole);
            cfg.setPassword(adminPass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-rehearsal");

            try (HikariDataSource adminDs = new HikariDataSource(cfg)) {

                // ── OLD LEG: apply the OLD TAG's literal changelog tree via a
                // filesystem ResourceAccessor (NOT SchemaMigrator.migrate — that
                // hardcodes the classpath = HEAD master changelog). ──────────────
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

                    assertThat(constraintValidated(conn, "chunks_768_chash_len_check")).isTrue();
                    assertThat(constraintValidated(conn, "chunks_1024_chash_len_check")).isTrue();
                    assertThat(constraintValidated(conn, "catalog_document_chunks_chash_len_check")).isTrue();
                    assertThat(constraintValidated(conn, "chash_index_chash_len_check")).isTrue();
                    assertThat(constraintExists(conn, "chunks_384_chash_len_check"))
                        .as("the dropped chunks_384_chash_len_check must remain absent, not silently re-added")
                        .isFalse();

                    assertThat(tablesInSchema(conn, "nexus"))
                        .as("core catalog/chunk tables must exist after the old-tag->HEAD hop")
                        .containsAll(Set.of(
                            "chunks_384", "chunks_768", "chunks_1024",
                            "catalog_document_chunks", "chash_index", "memory"));
                }
            }
        } finally {
            pg.stop();
        }
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

    private static Set<String> tablesInSchema(Connection conn, String schema) throws Exception {
        Set<String> names = new java.util.HashSet<>();
        ResultSet rs = conn.getMetaData().getTables(null, schema, null, new String[]{"TABLE"});
        while (rs.next()) {
            names.add(rs.getString("TABLE_NAME").toLowerCase());
        }
        return names;
    }
}
