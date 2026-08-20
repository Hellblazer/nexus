// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import liquibase.resource.DirectoryResourceAccessor;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.Comparator;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;

/**
 * nexus-tk070.p5b (RDR-194 § D4, REWORKED 2026-08-20) —
 * {@code migration-002-tenant-pk.xml}'s {@code DROP TABLE
 * nexus.migration_jobs}, proven live.
 *
 * <p><strong>Supersedes {@code MigrationJobsTenantPkTest} (deleted).</strong>
 * That test proved a PK-widening this table never needed in production:
 * substantive-critic's stacked-review finding (T2
 * {@code substantive-critique-tk070-p5b-2026-08-20}) established the table
 * is DEAD — {@code MigrationHandler.java} / {@code MigrationJobRepository
 * .java} were deleted at commit {@code 7bcf29c67} (2026-07-24), zero
 * producers or consumers remain. Sam's disposition (2026-08-20): DROP the
 * table outright rather than widen its dead PK.
 *
 * <p>Two behaviors:
 * <ol>
 *   <li>{@code nexus.migration_jobs} does NOT exist after a full
 *       {@code migrate()} to HEAD — red before green: proven against the
 *       pre-drop schema first (table exists, per
 *       {@code migration-001-baseline.xml}), then against HEAD (table
 *       gone). Same pre-repoint-changelog-copy technique as
 *       {@link Taxonomy014TenantFkRepointTest} /
 *       the deleted {@code MigrationJobsTenantPkTest}, applied to an
 *       exclusion of {@code migration-002-tenant-pk.xml} itself.</li>
 *   <li>The drop changeset is SHAPE-AGNOSTIC and applies cleanly from the
 *       ONLY state a real upgrade path can reach it in: the table PRESENT
 *       (created by {@code migration-001-baseline.xml} earlier in the same
 *       walk). A bare full {@code migrateFull()} against the real,
 *       unmodified classpath master IS this proof — Liquibase applies
 *       {@code migration-001-1} (create) before {@code migration-002-1}
 *       (drop) in the same pass, in master file order, on every fresh
 *       box.</li>
 * </ol>
 */
class MigrationJobsDroppedTest {

    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";
    private static final String MIGRATION_002_INCLUDE =
        "<include file=\"db/changelog/migration-002-tenant-pk.xml\"/>";

    @Test
    void migrationJobsTable_existsPreDrop_absentPostDrop() throws Exception {
        Path preDropRoot = buildPreDropChangelogRoot();
        // startDedicated(): same rationale as Taxonomy014TenantFkRepointTest's
        // identical choice — this test asserts on the migration PROCESS itself
        // (a specific fresh single-pass apply history distinguishing pre- vs
        // post-drop schema state); the shared-cluster start() hands back an
        // already-fully-migrated database (migration-002-1 already applied),
        // which would make every assertion below vacuous.
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                migratePreDrop(su, preDropRoot);
            }

            // RED: pre-drop schema (migration-002-tenant-pk.xml excluded) still has
            // the table — migration-001-baseline.xml created it, and nothing
            // upstream of migration-002-1 removes it. If this assertion itself
            // fails, the "absent after the drop" claim below would be vacuously
            // true (nothing ever had the table to begin with).
            try (Connection su = pg.createConnection("")) {
                assertThat(tableExists(su, "migration_jobs"))
                    .as("pre-drop schema: nexus.migration_jobs must still EXIST -- "
                        + "migration-001-baseline.xml created it and migration-002-1 "
                        + "(excluded here) is the only changeset that removes it")
                    .isTrue();
            }

            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                migrateFull(su);
            }

            // GREEN: post-drop schema (real HEAD) no longer has the table.
            try (Connection su = pg.createConnection("")) {
                assertThat(tableExists(su, "migration_jobs"))
                    .as("post-drop schema: nexus.migration_jobs must be GONE -- "
                        + "migration-002-1 drops it (dead table, nexus-tk070.p5b)")
                    .isFalse();
            }
        } finally {
            pg.stop();
            deleteRecursively(preDropRoot);
        }
    }

    @Test
    void dropChangeset_shapeAgnostic_appliesCleanlyFromBaselineWhereTableExists() throws Exception {
        // A real upgrade path always reaches migration-002-1 with the table
        // PRESENT (migration-001-baseline.xml runs first, master order, every
        // walk). This proves the drop changeset itself -- the pg_class
        // existence check, the pre-count NOTICE, DROP TABLE IF EXISTS -- runs
        // cleanly against exactly that state, using the real classpath master
        // unmodified: a bare migrateFull() from a fresh box IS this changeset
        // applying moments after migration-001-1 created the table in the SAME
        // walk.
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            assertThatCode(() -> {
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    migrateFull(su);
                }
            }).as("migration-002-1 must apply cleanly on a fresh box where "
                + "migration-001-baseline.xml has just created the table moments "
                + "earlier in the SAME walk")
                .doesNotThrowAnyException();

            try (Connection su = pg.createConnection("")) {
                assertThat(tableExists(su, "migration_jobs"))
                    .as("the shape-agnostic apply must still leave the table dropped")
                    .isFalse();
            }
        } finally {
            pg.stop();
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static boolean tableExists(Connection c, String table) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT to_regclass('nexus.' || ?) IS NOT NULL")) {
            ps.setString(1, table);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getBoolean(1);
            }
        }
    }

    private static void migrateFull(Connection su) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(su));
        try (Liquibase liquibase = new Liquibase(
                MASTER_CHANGELOG_RELATIVE, new ClassLoaderResourceAccessor(), database)) {
            liquibase.update(new Contexts(), new LabelExpression());
        }
    }

    private static void migratePreDrop(Connection su, Path preDropRoot) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(su));
        try (Liquibase liquibase = new Liquibase(
                MASTER_CHANGELOG_RELATIVE,
                new DirectoryResourceAccessor(preDropRoot.toFile()),
                database)) {
            liquibase.update(new Contexts(), new LabelExpression());
        }
    }

    /**
     * Copies the real {@code service/src/main/resources/db/changelog} tree to a
     * temp directory with the {@code <include>} line for
     * {@code migration-002-tenant-pk.xml} removed from the copied master
     * changelog. Every OTHER file is byte-identical to the real, on-disk
     * source — no SQL is duplicated or hand-transcribed anywhere in this test;
     * only the ONE include line naming the file under test is skipped, which is
     * exactly "the full schema minus this phase's own drop" (the pre-drop
     * baseline).
     */
    private static Path buildPreDropChangelogRoot() throws IOException, InterruptedException {
        Path repoRoot = repoRoot();
        Path realChangelogDir = repoRoot.resolve("service/src/main/resources/db/changelog");
        Path tempRoot = Files.createTempDirectory("nexus-tk070-p5b-predrop-");
        Path tempChangelogDir = tempRoot.resolve("db").resolve("changelog");
        Files.createDirectories(tempChangelogDir);
        try (Stream<Path> files = Files.list(realChangelogDir)) {
            for (Path f : (Iterable<Path>) files::iterator) {
                if (Files.isRegularFile(f)) {
                    Files.copy(f, tempChangelogDir.resolve(f.getFileName()), StandardCopyOption.REPLACE_EXISTING);
                }
            }
        }
        Path masterCopy = tempChangelogDir.resolve("db.changelog-master.xml");
        String masterText = Files.readString(masterCopy);
        if (!masterText.contains(MIGRATION_002_INCLUDE)) {
            throw new IllegalStateException(
                "expected <include> line for migration-002-tenant-pk.xml not found in "
                + "db.changelog-master.xml -- this test's needle text must match the real "
                + "<include> line verbatim; update MIGRATION_002_INCLUDE if it changed");
        }
        masterText = masterText.replace(
            MIGRATION_002_INCLUDE,
            "<!-- migration-002-tenant-pk.xml excluded here: pre-drop test fixture, "
            + "MigrationJobsDroppedTest -->");
        Files.writeString(masterCopy, masterText);
        return tempRoot;
    }

    private static Path repoRoot() throws IOException, InterruptedException {
        Process p = new ProcessBuilder("git", "rev-parse", "--show-toplevel")
            .redirectErrorStream(false)
            .start();
        String out;
        try (var in = p.getInputStream()) {
            out = new String(in.readAllBytes()).trim();
        }
        p.waitFor();
        return Path.of(out);
    }

    private static void deleteRecursively(Path root) {
        if (root == null) {
            return;
        }
        try (Stream<Path> walk = Files.walk(root)) {
            walk.sorted(Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (IOException ignored) {
                    // best-effort cleanup only
                }
            });
        } catch (IOException ignored) {
            // best-effort cleanup only
        }
    }
}
