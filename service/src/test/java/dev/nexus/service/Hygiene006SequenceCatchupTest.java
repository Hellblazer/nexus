// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

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
import java.util.List;

import static dev.nexus.service.jooq.nexus.Tables.MEMORY;
import static dev.nexus.service.jooq.nexus.Tables.PLANS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-zhxxd (GH #1489) -- proof that {@code hygiene-006-sequence-
 * catchup.xml}'s {@code hygiene-006-1} changeset advances a BIGSERIAL
 * sequence that sits behind its column's imported ids, and leaves a
 * sequence that is already ahead exactly where it was.
 *
 * <p>The fixture reproduces the shipped shape: rows written with EXPLICIT
 * ids far above the sequence (a fidelity import on engine-service-v0.1.52,
 * before taxonomy-005's advance existed), so the next serial INSERT would
 * collide on the primary key. Same migration-plumbing idiom as {@link
 * Hygiene005GcRegistrationFromOriginRowDataCorrectionTest}: migrate up to
 * (not including) the changeset, seed through the superuser, apply the rest
 * of the changelog, assert. {@code nexus.topics} is the table GH #1489
 * named; {@code nexus.memory} proves the walk is not topics-specific; the
 * untouched {@code nexus.plans} sequence proves an already-ahead sequence
 * is never moved.
 */
class Hygiene006SequenceCatchupTest {
    private static final String PRE_CHANGESET_ID = "hygiene-006-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT = "hygiene006-catchup-tenant";
    private static final String COLLECTION = "rdr__h006__voyage-context-3__v1";
    private static final long IMPORTED_TOPIC_ID = 1_426_145_000L;
    private static final long IMPORTED_MEMORY_ID = 900_000L;

    @Test
    void advancesLaggingSequencesPastImportedIds_leavesAheadSequencesAlone() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_hygiene006_test";
            final String pass = "nexus_admin_hygiene006_test_pass";
            Hygiene001NotNullMigrationRlsTest.bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-hygiene006-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                migrateUpTo(adminDs, PRE_CHANGESET_ID);
                try (Connection adminConn = adminDs.getConnection()) {
                    PgContainerHelper.installTestObjects(adminConn);
                }

                long plansSeqBefore;
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    // topics carries an FK to the collection registry (fk-002).
                    PgContainerHelper.insertCollection(ctx, TENANT, COLLECTION);
                    // Explicit ids, the import's shape: the sequence never moved.
                    ctx.insertInto(TOPICS, TOPICS.ID, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                                   TOPICS.DOC_COUNT, TOPICS.CREATED_AT)
                       .values(IMPORTED_TOPIC_ID, TENANT, "imported", COLLECTION, 3,
                               OffsetDateTime.now())
                       .execute();
                    ctx.insertInto(MEMORY, MEMORY.ID, MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE,
                                   MEMORY.CONTENT, MEMORY.TIMESTAMP)
                       .values(IMPORTED_MEMORY_ID, TENANT, "h006", "imported", "imported row",
                               OffsetDateTime.now())
                       .execute();
                    assertThat(lastValue(ctx, "topics_id_seq"))
                        .as("precondition: the topics sequence sits behind the imported id")
                        .isLessThan(IMPORTED_TOPIC_ID);
                    assertThat(lastValue(ctx, "memory_id_seq"))
                        .as("precondition: the memory sequence sits behind the imported id")
                        .isLessThan(IMPORTED_MEMORY_ID);
                    // A POPULATED table whose sequence is already ahead (the row minted
                    // by the serial itself): the is_called/max arithmetic must leave it
                    // exactly where it is (review of bace74903, Significant 2).
                    ctx.insertInto(PLANS, PLANS.TENANT_ID, PLANS.PROJECT, PLANS.QUERY, PLANS.PLAN_JSON,
                                   PLANS.VERB, PLANS.CREATED_AT)
                       .values(TENANT, "h006", "serial-minted", org.jooq.JSONB.valueOf("{}"), "research",
                               OffsetDateTime.now())
                       .execute();
                    plansSeqBefore = lastValue(ctx, "plans_id_seq");
                    assertThat(plansSeqBefore)
                        .as("precondition: the plans sequence minted its own row and sits at max(id)")
                        .isGreaterThanOrEqualTo(1L);
                }

                applyRemainingChangelog(adminDs);

                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                    assertThat(lastValue(ctx, "topics_id_seq"))
                        .as("topics sequence advanced to the imported max").isEqualTo(IMPORTED_TOPIC_ID);
                    assertThat(lastValue(ctx, "memory_id_seq"))
                        .as("memory sequence advanced to the imported max").isEqualTo(IMPORTED_MEMORY_ID);
                    assertThat(lastValue(ctx, "plans_id_seq"))
                        .as("a populated table whose sequence already sits at max(id) is never moved")
                        .isEqualTo(plansSeqBefore);

                    // THE symptom: a serial INSERT after the walk no longer collides.
                    Long fresh = ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION,
                                                TOPICS.DOC_COUNT, TOPICS.CREATED_AT)
                        .values(TENANT, "discovered", COLLECTION, 1, OffsetDateTime.now())
                        .returningResult(TOPICS.ID)
                        .fetchOne(TOPICS.ID);
                    assertThat(fresh)
                        .as("the first serial id after the walk is past every imported id")
                        .isEqualTo(IMPORTED_TOPIC_ID + 1);
                }
            }
        } finally {
            pg.stop();
        }
    }

    private static long lastValue(DSLContext ctx, String sequence) {
        return ctx.select(DSL.field(DSL.name("last_value"), Long.class))
            .from(DSL.table(DSL.name("nexus", sequence)))
            .fetchOne(0, Long.class);
    }

    // ── Migration plumbing (Hygiene005's own idiom) ─────────────────────────

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
