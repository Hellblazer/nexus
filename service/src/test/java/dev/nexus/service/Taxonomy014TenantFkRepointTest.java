// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.exception.LiquibaseException;
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
import java.sql.SQLException;
import java.sql.Statement;
import java.sql.Types;
import java.util.Comparator;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-tk070.p5a (RDR-194 § D4) — {@code taxonomy-014-topics-tenant-unique.xml}'s
 * two load-bearing behaviors, proven live rather than only claimed in prose:
 *
 * <ol>
 *   <li>a cross-tenant {@code nexus.topics.parent_id} reference is ACCEPTED by
 *       the pre-repoint schema ({@code topics_parent_fk}, tenant-blind) and
 *       REJECTED by the post-repoint schema ({@code fk_topics_parent_tenant},
 *       tenant-scoped) — the SAME assertion shape run against both schema
 *       states, so the "REJECTED after the repoint" claim is demonstrably
 *       non-vacuous: it FAILS if run against the pre-repoint state (the
 *       bead's own TDD requirement — red before green);</li>
 *   <li>a cross-tenant population PRESENT before the repoint changeset runs
 *       makes {@code taxonomy-014-2} fail loud — {@code RAISE EXCEPTION}
 *       naming the exact count and the quarantine remedy — rather than
 *       silently deleting or coercing the corrupt row, and the whole
 *       changeset (DROP old FK, ADD new FK NOT VALID, anti-join check) rolls
 *       back atomically: the OLD tenant-blind FK is left exactly as it was,
 *       not a half-migrated schema.</li>
 * </ol>
 *
 * <p><strong>Mechanism.</strong> An earlier revision of this test drove
 * {@link Liquibase#rollback(int, Contexts, LabelExpression)} to reach the
 * pre-repoint schema state. That does not work here: {@code rollback(int)}
 * reverts the last N EXECUTED rows by {@code databasechangelog} order, and
 * this changelog has OTHER {@code runAlways} changesets outside
 * {@code taxonomy-014}/the grants files (e.g.
 * {@code staging-001-landing-tables.xml::staging-4-svc-grants},
 * {@code taxonomy-011-doc-id-bytea.xml::taxonomy-011-8}) interleaved into
 * the tail of execution order in a way that made a filename-scoped row COUNT
 * roll back the wrong SET of changesets (empirically verified: 2 of
 * {@code taxonomy-014}'s own 5 changesets survived a same-size rollback
 * while two unrelated files' changesets were reverted instead). Rather than
 * chase that ordering, this test builds the pre-repoint schema directly:
 * {@link #buildPreRepointChangelogRoot()} copies the real
 * {@code service/src/main/resources/db/changelog} tree to a temp directory
 * with ONLY the {@code <include>} line for
 * {@code taxonomy-014-topics-tenant-unique.xml} removed from the copied
 * master, then a {@link DirectoryResourceAccessor} runs Liquibase against
 * that copy — every OTHER file's real, on-disk SQL runs unmodified (no SQL
 * is duplicated or hand-copied into this test), only the repoint's own file
 * is skipped. A subsequent {@code migrateFull} against the REAL classpath
 * master (unmodified) then applies exactly {@code taxonomy-014}'s five
 * pending changesets, using their own real forward SQL, exercising the
 * genuine production changeset bodies for both the "succeeds cleanly" and
 * "fails loud" cases below.
 *
 * <p>Only {@code topics.parent_id} ({@code fk_topics_parent_tenant},
 * changeset {@code taxonomy-014-2}) is exercised here — the smallest,
 * self-contained repro (one table, self-referential, no chunk-seeding
 * prerequisite unlike {@code topic_assignments}' repoint since RDR-194 P3d
 * made {@code topic_assignments} rows require a matching {@code nexus.chunks}
 * row). The other three FKs ({@code topic_assignments.topic_id},
 * {@code topic_links.from_topic_id}/{@code .to_topic_id}) share byte-identical
 * DDL shape (same file, same catalog-029 three-step pattern, same fail-loud
 * anti-join) and are additionally exercised structurally by
 * {@link SchemaUpgradeRehearsalIntegrationTest}'s seed-coverage legs
 * (existence + VALIDATED at HEAD, FORCE RLS restored) and
 * {@link SchemaRollbackRoundTripIntegrationTest}'s full-chain rollback/
 * reapply round trip.
 */
class Taxonomy014TenantFkRepointTest {

    private static final String MASTER_CHANGELOG_RELATIVE = "db/changelog/db.changelog-master.xml";
    private static final String TAXONOMY_014_INCLUDE =
        "<include file=\"db/changelog/taxonomy-014-topics-tenant-unique.xml\"/>";

    @Test
    void crossTenantParentReference_acceptedPreRepoint_rejectedPostRepoint() throws Exception {
        Path preRepointRoot = buildPreRepointChangelogRoot();
        // startDedicated(), not start(): this test asserts on the migration PROCESS
        // itself (a specific fresh single-pass apply history distinguishing pre- vs
        // post-repoint schema state) -- PgContainerHelper.start()'s default shared-
        // cluster handle (nexus-yhmav) hands back an ALREADY-fully-migrated database
        // (built from the real classpath master, taxonomy-014 included), which would
        // make every assertion below vacuous regardless of preRepointRoot's content.
        // See PgContainerHelper.startDedicated()'s own javadoc roster of tests with
        // this identical requirement.
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                migratePreRepoint(su, preRepointRoot);
            }

            // RED: pre-repoint schema (topics_parent_fk, tenant-blind) accepts a
            // cross-tenant parent reference. If this assertion itself fails, the
            // "rejected after the repoint" claim below would be vacuously true
            // (nothing changed the schema could ever have accepted) -- proving
            // this half first is the bead's explicit red-before-green requirement.
            long topicA;
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                topicA = seedTopic(su, "tenant-a", null);
            }
            long parentForB = topicA;
            assertThatCode(() -> {
                try (Connection su2 = pg.createConnection("")) {
                    su2.setAutoCommit(true);
                    seedTopic(su2, "tenant-b", parentForB);
                }
            }).as("pre-repoint schema: a cross-tenant parent_id reference must be ACCEPTED "
                + "-- topics_parent_fk carries no tenant scoping yet")
                .doesNotThrowAnyException();

            // cleanup: remove both seeded rows before completing the migration to HEAD
            // so nothing trips taxonomy-014-2's fail-loud anti-join.
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "DELETE FROM nexus.topics WHERE tenant_id IN ('tenant-a', 'tenant-b')");
            }

            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                migrateFull(su);
            }

            // GREEN: post-repoint schema (fk_topics_parent_tenant, tenant-scoped)
            // rejects the identical shape of reference.
            long topicA2;
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                topicA2 = seedTopic(su, "tenant-a", null);
            }
            long parentForB2 = topicA2;
            assertThatThrownBy(() -> {
                try (Connection su2 = pg.createConnection("")) {
                    su2.setAutoCommit(true);
                    seedTopic(su2, "tenant-b", parentForB2);
                }
            }).as("post-repoint schema: a cross-tenant parent_id reference must be REJECTED "
                + "-- fk_topics_parent_tenant is tenant-scoped (tenant_id, parent_id) -> "
                + "(tenant_id, id)")
                .isInstanceOf(SQLException.class)
                .satisfies(t -> assertThat(((SQLException) t).getSQLState()).isEqualTo("23503"));
        } finally {
            pg.stop();
            deleteRecursively(preRepointRoot);
        }
    }

    @Test
    void crossTenantParentReference_presentBeforeRepoint_failsLoudNotSilently() throws Exception {
        Path preRepointRoot = buildPreRepointChangelogRoot();
        // startDedicated(): see the sibling test's identical rationale above.
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                migratePreRepoint(su, preRepointRoot);
            }

            long topicA;
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                topicA = seedTopic(su, "tenant-a", null);
            }
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                // the cross-tenant corruption, deliberately left in place for the
                // forward migration below to discover
                seedTopic(su, "tenant-b", topicA);
            }

            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                assertThatThrownBy(() -> migrateFull(su))
                    .as("taxonomy-014-2 must fail loud on a nonzero cross-tenant anti-join, "
                        + "naming the count and the quarantine remedy -- not silently delete "
                        + "or coerce the corrupt row")
                    .isInstanceOf(LiquibaseException.class)
                    .hasMessageContaining("1 cross-tenant nexus.topics.parent_id reference")
                    .hasMessageContaining("quarantine")
                    .hasMessageContaining("CORRUPTION");
            }

            // Atomicity: the failed changeset's DROP + ADD NOT VALID rolled back too
            // -- the new composite FK must NOT exist, and the OLD tenant-blind FK must
            // still be exactly where it was (fail-loud leaves quarantine + re-run as
            // the human-directed remedy, never a half-migrated schema).
            try (Connection su = pg.createConnection("")) {
                assertThat(count(su,
                    "SELECT count(*) FROM pg_constraint WHERE conname = 'fk_topics_parent_tenant'"))
                    .as("the failed changeset must leave no half-created composite FK behind")
                    .isEqualTo(0);
                assertThat(count(su,
                    "SELECT count(*) FROM pg_constraint WHERE conname = 'topics_parent_fk'"))
                    .as("the OLD tenant-blind FK must survive the failed changeset unchanged")
                    .isEqualTo(1);
                assertThat(count(su,
                    "SELECT count(*) FROM nexus.topics WHERE tenant_id IN ('tenant-a', 'tenant-b')"))
                    .as("fail-loud must not have deleted the offending rows either -- they stay "
                        + "in place for the human-directed quarantine, not silently reaped")
                    .isEqualTo(2);
            }
        } finally {
            pg.stop();
            deleteRecursively(preRepointRoot);
        }
    }

    @Test
    void crossTenantTopicAssignment_preRepoint_docCountTriggerStaysTenantScoped() throws Exception {
        // Restores INDEPENDENT trigger-predicate coverage (substantive-critic
        // SIGNIFICANT, T2 [22965]): TaxonomyRepositoryTest.docCountTrigger_
        // crossTenantIsolation used to prove nexus.topics_doc_count_recount_ins's
        // own `t.tenant_id = a.tenant_id` defense-in-depth predicate kept a
        // cross-tenant topic_assignments INSERT from mutating the wrong tenant's
        // doc_count -- that test was rewritten (this same phase) to assert the FK
        // now REJECTS the insert outright, which is correct for the POST-repoint
        // schema but leaves the trigger's own predicate with no live coverage of
        // its own. Both layers now need independent proof: the FK test above
        // must not be the only thing standing. This test exercises the trigger
        // DIRECTLY against the PRE-repoint schema (relevant during the ROLLBACK
        // window, which restores the exact old tenant-blind FK, and the SAME
        // pre-repoint machinery the sibling FK tests already use) -- pre-repoint,
        // the insert this test performs SUCCEEDS (topics_parent_fk's siblings
        // are all tenant-blind), so the trigger's own predicate is what is being
        // proven, not the FK.
        Path preRepointRoot = buildPreRepointChangelogRoot();
        // startDedicated(): see the sibling tests' identical rationale above.
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                migratePreRepoint(su, preRepointRoot);
            }

            long topicB;
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                topicB = seedTopicWithDocCount(su, "tenant-b", "coll", 7);
            }

            // Tenant-a inserts an assignment pointing at tenant-b's topic id.
            // Pre-repoint, topic_assignments_topic_id_fkey is tenant-blind, so
            // this succeeds -- topic_assignments_chunk_fk (from an EARLIER,
            // already-applied phase, RDR-194 P3d) is keyed on the ASSIGNMENT's
            // OWN tenant_id (tenant-a here), not the referenced topic's tenant,
            // so seeding the chunk under tenant-a satisfies it without touching
            // what this test is isolating.
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                seedChunkForAssignment(su, "tenant-a", "coll", XT_DOC_ID_HEX);
                insertTopicAssignment(su, "tenant-a", XT_DOC_ID_HEX, topicB, "coll");
            }

            // The trigger's OWN tenant_id predicate -- not any FK -- is what
            // must keep tenant-b's doc_count untouched: the FK does not
            // reference doc_count at all, and pre-repoint there is no FK
            // blocking the cross-tenant reference in the first place.
            try (Connection su = pg.createConnection("")) {
                assertThat(count(su, "SELECT doc_count FROM nexus.topics WHERE id = " + topicB))
                    .as("nexus.topics_doc_count_recount_ins's own t.tenant_id = a.tenant_id "
                        + "predicate must keep a cross-tenant assignment from mutating the "
                        + "referenced topic's doc_count, independently of any FK")
                    .isEqualTo(7);
                assertThat(count(su,
                    "SELECT count(*) FROM nexus.topics WHERE tenant_id = 'tenant-a' AND id = " + topicB))
                    .as("tenant-a must not have gained a topic row under tenant-b's id either")
                    .isEqualTo(0);
            }
        } finally {
            pg.stop();
            deleteRecursively(preRepointRoot);
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private static void migrateFull(Connection su) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(su));
        try (Liquibase liquibase = new Liquibase(
                MASTER_CHANGELOG_RELATIVE, new ClassLoaderResourceAccessor(), database)) {
            liquibase.update(new Contexts(), new LabelExpression());
        }
    }

    private static void migratePreRepoint(Connection su, Path preRepointRoot) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(su));
        try (Liquibase liquibase = new Liquibase(
                MASTER_CHANGELOG_RELATIVE,
                new DirectoryResourceAccessor(preRepointRoot.toFile()),
                database)) {
            liquibase.update(new Contexts(), new LabelExpression());
        }
    }

    /**
     * Copies the real {@code service/src/main/resources/db/changelog} tree to a
     * temp directory with the {@code <include>} line for
     * {@code taxonomy-014-topics-tenant-unique.xml} removed from the copied
     * master changelog. Every OTHER file is byte-identical to the real,
     * on-disk source -- no SQL is duplicated or hand-transcribed anywhere in
     * this test; only the ONE include line naming the file under test is
     * skipped, which is exactly "the full schema minus this phase's own
     * repoint" (D4's pre-repoint baseline).
     */
    private static Path buildPreRepointChangelogRoot() throws IOException, InterruptedException {
        Path repoRoot = repoRoot();
        Path realChangelogDir = repoRoot.resolve("service/src/main/resources/db/changelog");
        Path tempRoot = Files.createTempDirectory("nexus-tk070-p5a-prerepoint-");
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
        if (!masterText.contains(TAXONOMY_014_INCLUDE)) {
            throw new IllegalStateException(
                "expected <include> line for taxonomy-014-topics-tenant-unique.xml not found in "
                + "db.changelog-master.xml -- this test's needle text must match the real "
                + "<include> line verbatim; update TAXONOMY_014_INCLUDE if it changed");
        }
        masterText = masterText.replace(
            TAXONOMY_014_INCLUDE,
            "<!-- taxonomy-014-topics-tenant-unique.xml excluded here: pre-repoint test fixture, "
            + "Taxonomy014TenantFkRepointTest -->");
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

    /**
     * Insert a topic as tenant {@code tenant}, with {@code parentId} (nullable).
     * Superuser connection — implicit BYPASSRLS — models raw schema/FK behavior
     * directly, without needing a per-statement {@code nexus.tenant} GUC (this
     * test is about referential integrity, not RLS write-path enforcement).
     * {@code collection} is registered first (fk-002's collection-registry FK
     * on {@code topics.collection} predates this phase and is unrelated to it,
     * but must be satisfied for the INSERT to reach the parent_id FK at all).
     */
    private static long seedTopic(Connection c, String tenant, Long parentId) throws Exception {
        try (PreparedStatement reg = c.prepareStatement(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, 'coll') "
                + "ON CONFLICT (tenant_id, name) DO NOTHING")) {
            reg.setString(1, tenant);
            reg.executeUpdate();
        }
        try (PreparedStatement ps = c.prepareStatement(
                "INSERT INTO nexus.topics (tenant_id, label, parent_id, collection, created_at) "
                + "VALUES (?, ?, ?, 'coll', now()) RETURNING id")) {
            ps.setString(1, tenant);
            ps.setString(2, tenant + "-topic");
            if (parentId == null) {
                ps.setNull(3, Types.BIGINT);
            } else {
                ps.setLong(3, parentId);
            }
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    private static int count(Connection c, String sql) throws Exception {
        try (Statement st = c.createStatement(); ResultSet rs = st.executeQuery(sql)) {
            rs.next();
            return rs.getInt(1);
        }
    }

    /** Genuine 64-lowercase-hex sha256 chash for the doc_count-trigger test's
     *  cross-tenant assignment row (topic_assignments.doc_id is bytea since
     *  RDR-194 P3c; TaxonomyHandlerAssignFkTest uses the identical pattern). */
    private static final String XT_DOC_ID_HEX = hexChash("xt-doc-cross-tenant-trigger-test");

    private static String hexChash(String seed) throws RuntimeException {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * Insert a topic as {@code tenant} with an EXPLICIT {@code docCount} seed
     * (bypassing the doc_count trigger, which only fires on {@code
     * topic_assignments} INSERT/DELETE) -- the known baseline the doc_count-
     * trigger test asserts stays untouched by a cross-tenant assignment.
     */
    private static long seedTopicWithDocCount(
            Connection c, String tenant, String collection, int docCount) throws Exception {
        try (PreparedStatement reg = c.prepareStatement(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                + "ON CONFLICT (tenant_id, name) DO NOTHING")) {
            reg.setString(1, tenant);
            reg.setString(2, collection);
            reg.executeUpdate();
        }
        try (PreparedStatement ps = c.prepareStatement(
                "INSERT INTO nexus.topics (tenant_id, label, collection, doc_count, created_at) "
                + "VALUES (?, ?, ?, ?, now()) RETURNING id")) {
            ps.setString(1, tenant);
            ps.setString(2, tenant + "-doc-count-topic");
            ps.setString(3, collection);
            ps.setInt(4, docCount);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    /**
     * A {@code nexus.chunks} row satisfying {@code topic_assignments_chunk_fk}
     * (RDR-194 P3d) for {@code (tenant, collection, chashHex)} -- required for
     * ANY {@code topic_assignments} insert regardless of this phase's own
     * repoint (that FK predates P5a and is keyed on the assignment's own
     * tenant, per the sibling {@code TaxonomyHandlerAssignFkTest.seedChunk}).
     */
    private static void seedChunkForAssignment(
            Connection c, String tenant, String collection, String chashHex) throws Exception {
        int dim = 384;
        try (Statement st = c.createStatement()) {
            st.execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + tenant + "', '" + collection + "') ON CONFLICT (tenant_id, name) DO NOTHING");
            st.execute(
                "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_" + dim + ") "
                + "VALUES ('" + tenant + "', '" + collection + "', decode('" + chashHex + "', 'hex'), "
                + "'doc-count-trigger-test chunk', ('[" + "0.1,".repeat(dim - 1) + "0.1]')::vector) "
                + "ON CONFLICT (tenant_id, collection, chash) DO NOTHING");
        }
    }

    /** A {@code nexus.topic_assignments} row -- {@code doc_id} is bytea (RDR-194
     *  P3c), {@code source_collection} is NOT NULL (RDR-194 P3b). */
    private static void insertTopicAssignment(
            Connection c, String tenant, String docIdHex, long topicId, String sourceCollection) throws Exception {
        try (PreparedStatement ps = c.prepareStatement(
                "INSERT INTO nexus.topic_assignments "
                + "(tenant_id, doc_id, topic_id, assigned_by, source_collection) "
                + "VALUES (?, decode(?, 'hex'), ?, 'manual', ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, docIdHex);
            ps.setLong(3, topicId);
            ps.setString(4, sourceCollection);
            ps.executeUpdate();
        }
    }
}
