// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import dev.nexus.service.jooq.binding.Vector;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-c0g6e (GH #1529) -- proof that {@code hygiene-007-doc-count-
 * recount.xml}'s {@code hygiene-007-1} changeset recounts {@code
 * nexus.topics.doc_count} from {@code nexus.topic_assignments}, correcting
 * a topic whose stored count sits above (or below, including down to zero)
 * its real assignment count, and leaving an already-correct topic alone.
 *
 * <p>The fixture reproduces the reported shape: a topic imported with an
 * explicit {@code doc_count} that disagrees with the assignment rows
 * actually carried across (the 6.18.1-era fidelity import class hygiene-
 * 006-1's header documents for the sibling sequence-lag defect). Since
 * {@code doc_count} is trigger-maintained on every live INSERT/DELETE
 * against {@code topic_assignments} (taxonomy-013), a real assignment
 * insert self-corrects it immediately -- so the drifted shape can only be
 * produced the way the import itself produced it: seed the real assignment
 * rows first (the trigger sets the true count), then overwrite {@code
 * doc_count} with a plain {@code UPDATE nexus.topics} that does not fire
 * the topic_assignments trigger at all, exactly like a bulk import writing
 * both tables from a snapshot whose counts never agreed. Same migration-
 * plumbing idiom as {@link Hygiene006SequenceCatchupTest}: migrate up to
 * (not including) the changeset, seed through the superuser, apply the
 * rest of the changelog, assert.
 */
class Hygiene007DocCountRecountTest {
    private static final String PRE_CHANGESET_ID = "hygiene-007-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT = "hygiene007-doccount-tenant";
    private static final String COLLECTION = "rdr__h007__voyage-context-3__v1";

    private static final long OVER_COUNTED_TOPIC_ID = 500L;   // doc_count too high
    private static final long CORRECT_TOPIC_ID = 501L;        // doc_count already right
    private static final long ZEROED_TOPIC_ID = 502L;         // doc_count drifts to 0 (no assignments)

    @Test
    void recountsDriftedTopics_leavesCorrectTopicsAlone() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_hygiene007_test";
            final String pass = "nexus_admin_hygiene007_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-hygiene007-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                migrateUpTo(adminDs, PRE_CHANGESET_ID);
                try (Connection adminConn = adminDs.getConnection()) {
                    PgContainerHelper.installTestObjects(adminConn);
                }

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

                    // topics carries an FK to the collection registry (fk-002); topic_
                    // assignments carries an FK to nexus.chunks (RDR-194 P3d).
                    PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);

                    // Over-counted: 2 real assignment rows (taxonomy-013's trigger sets
                    // doc_count=2 on insert), then a plain UPDATE overwrites it to 5 --
                    // the reported shape (an import writing topics/topic_assignments
                    // from a snapshot whose counts never agreed, no trigger involved).
                    insertTopic(ctx, OVER_COUNTED_TOPIC_ID, "over-counted", 0);
                    seedAssignment(ctx, OVER_COUNTED_TOPIC_ID, "h007-over-1");
                    seedAssignment(ctx, OVER_COUNTED_TOPIC_ID, "h007-over-2");
                    forceDocCount(ctx, OVER_COUNTED_TOPIC_ID, 5);

                    // Already correct: 3 real assignment rows, doc_count left exactly as
                    // the trigger set it (3) -- must stay untouched by the walk.
                    insertTopic(ctx, CORRECT_TOPIC_ID, "already-correct", 0);
                    seedAssignment(ctx, CORRECT_TOPIC_ID, "h007-correct-1");
                    seedAssignment(ctx, CORRECT_TOPIC_ID, "h007-correct-2");
                    seedAssignment(ctx, CORRECT_TOPIC_ID, "h007-correct-3");

                    // Zero case: NO real assignment rows at all, then a plain UPDATE
                    // sets doc_count to 7 -- must correct down to 0, not be skipped as
                    // "no rows found".
                    insertTopic(ctx, ZEROED_TOPIC_ID, "zeroed", 0);
                    forceDocCount(ctx, ZEROED_TOPIC_ID, 7);

                    assertThat(docCount(ctx, OVER_COUNTED_TOPIC_ID))
                        .as("precondition: over-counted topic's doc_count sits above its real assignment count")
                        .isEqualTo(5);
                    assertThat(docCount(ctx, CORRECT_TOPIC_ID))
                        .as("precondition: already-correct topic's doc_count matches its real assignment count")
                        .isEqualTo(3);
                    assertThat(docCount(ctx, ZEROED_TOPIC_ID))
                        .as("precondition: zeroed topic's doc_count carries drift with no backing assignments")
                        .isEqualTo(7);
                }

                applyRemainingChangelog(adminDs);

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

                    assertThat(docCount(ctx, OVER_COUNTED_TOPIC_ID))
                        .as("over-counted topic's doc_count corrected down to its real assignment count")
                        .isEqualTo(2);
                    assertThat(docCount(ctx, CORRECT_TOPIC_ID))
                        .as("an already-correct topic's doc_count is never touched")
                        .isEqualTo(3);
                    assertThat(docCount(ctx, ZEROED_TOPIC_ID))
                        .as("a topic with no backing assignments corrects to 0, not skipped")
                        .isEqualTo(0);
                }
            }
        } finally {
            pg.stop();
        }
    }

    private static Integer docCount(DSLContext ctx, long topicId) {
        return ctx.select(TOPICS.DOC_COUNT).from(TOPICS)
            .where(TOPICS.TENANT_ID.eq(TENANT)).and(TOPICS.ID.eq(topicId))
            .fetchOne(TOPICS.DOC_COUNT);
    }

    private static void insertTopic(DSLContext ctx, long id, String label, int seededDocCount) {
        ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                       TOPICS.DOC_COUNT, TOPICS.CREATED_AT)
           .values(id, TENANT, label, COLLECTION, seededDocCount, OffsetDateTime.now())
           .execute();
    }

    /** A plain UPDATE against nexus.topics -- does NOT fire taxonomy-013's topic_
     *  assignments-side trigger, so this reproduces the import shape (doc_count
     *  written directly, disagreeing with the real assignment rows) rather than
     *  anything the trigger itself could ever produce live. */
    private static void forceDocCount(DSLContext ctx, long id, int docCount) {
        ctx.update(TOPICS).set(TOPICS.DOC_COUNT, docCount)
           .where(TOPICS.TENANT_ID.eq(TENANT)).and(TOPICS.ID.eq(id))
           .execute();
    }

    /** Seed one topic_assignments row backed by a real chunk (topic_assignments_chunk_fk,
     *  RDR-194 P3d) -- {@code seed} must be unique across calls so each assignment gets
     *  its own chash-keyed chunk row. */
    private static void seedAssignment(DSLContext ctx, long topicId, String seed) {
        byte[] chashBytes = HexFormat.of().parseHex(hexChash(seed));
        ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                       CHUNKS.EMBEDDING_384)
           .values(TENANT, COLLECTION, chashBytes, "hygiene007 fixture chunk", vector(384))
           .onConflictDoNothing()
           .execute();
        ctx.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                       TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                       TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, TOPIC_ASSIGNMENTS.ASSIGNED_AT)
           .values(TENANT, chashBytes, topicId, "hdbscan", COLLECTION, OffsetDateTime.now())
           .execute();
    }

    private static Vector vector(int dim) {
        float[] v = new float[dim];
        java.util.Arrays.fill(v, 0.1f);
        return Vector.of(v);
    }

    private static String hexChash(String seed) {
        try {
            byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(seed.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    // ── Migration plumbing (Hygiene006's own idiom) ─────────────────────────

    private static void applyRemainingChangelog(com.zaxxer.hikari.HikariDataSource adminDs) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                liquibase.update(new Contexts(), new LabelExpression());
            }
        }
    }

    private static void migrateUpTo(com.zaxxer.hikari.HikariDataSource adminDs,
                                     String targetChangesetId) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(
                    new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (targetChangesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx)
                    .as(targetChangesetId + " must be present in the master changelog")
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }
}
