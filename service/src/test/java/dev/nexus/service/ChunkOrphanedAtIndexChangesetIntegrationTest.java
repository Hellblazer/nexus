// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.SchemaMigrator;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.junit.jupiter.api.Timeout;
import org.slf4j.LoggerFactory;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.fail;

/**
 * RDR-229 P3.3c (nexus-p2m1x.21): {@code vectors-075-1}, the self-retrying changeset that builds
 * {@code idx_chunk_orphaned_at_tenant_chash} on {@code nexus.chunk_orphaned_at (tenant_id, chash)}. A boot must never hang or halt on it,
 * and a killed build heals on the next boot: the precondition (CONTINUE) says no VALID index of the name exists, so whatever exists is
 * INVALID and the body drops it before it builds, each statement CONCURRENTLY under a 30 s {@code lock_timeout}; a failing body is skipped
 * (failOnError false), nothing is recorded, and the next boot retries.
 *
 * <p>One container, run in order, as the production migration role's shape (a non-superuser owner): a fresh walk builds it; a valid index
 * with no record is skipped and left standing; a held writer lock past the timeout ends the wait, the walk finishes, the changeset is
 * unrecorded and an INVALID leftover stays (the state a killed build leaves); the next boot drops the leftover and builds a valid index.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class ChunkOrphanedAtIndexChangesetIntegrationTest {

    private static final String ADMIN_ROLE = "nexus_admin_coaidx";
    private static final String ADMIN_PASS = "nexus_admin_coaidx_pass";
    private static final String INDEX = "idx_chunk_orphaned_at_tenant_chash";
    private static final String CHANGESET = "vectors-075-1";
    /** The self-retrying changeset a fresh install always carries (it waits for a data move): the count the walk reports apart. */
    private static final int WAITING_ON_ITS_OWN = 1;

    private PostgreSQLContainer<?> pg;
    private HikariDataSource adminDs;

    @BeforeAll
    void start() throws Exception {
        pg = PgContainerHelper.startEmptyDatabase();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-coaidx");
        adminDs = new HikariDataSource(cfg);
    }

    @AfterAll
    void stop() {
        if (adminDs != null) adminDs.close();
        if (pg != null) pg.stop();
    }

    private static List<String> captureLogs(Runnable body) {
        var logger = (ch.qos.logback.classic.Logger) LoggerFactory.getLogger(SchemaMigrator.class);
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        Level before = logger.getLevel();
        logger.setLevel(Level.INFO);
        logger.addAppender(appender);
        try {
            body.run();
        } finally {
            logger.detachAppender(appender);
            logger.setLevel(before);
        }
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    // ── the catalog ─────────────────────────────────────────────────────────────────────────────────────────

    /** {@code indisvalid} of the index, or null when there is none. */
    private Boolean indexValid() throws Exception {
        try (Connection su = pg.createConnection("")) {
            var i = DSL.table(DSL.name("pg_catalog", "pg_index")).as("i");
            var c = DSL.table(DSL.name("pg_catalog", "pg_class")).as("c");
            var n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            Field<Boolean> valid = DSL.field(DSL.name("i", "indisvalid"), Boolean.class);
            return DSL.using(su, SQLDialect.POSTGRES).select(valid).from(i)
                .join(c).on(DSL.field(DSL.name("c", "oid")).eq(DSL.field(DSL.name("i", "indexrelid"))))
                .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("c", "relnamespace"))))
                .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                .and(DSL.field(DSL.name("c", "relname"), String.class).eq(INDEX))
                .fetchOne(valid);
        }
    }

    private boolean recorded() throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).fetchExists(DSL.selectOne()
                .from(DSL.table(DSL.name("public", "databasechangelog")))
                .where(DSL.field(DSL.name("id"), String.class).eq(CHANGESET)));
        }
    }

    private void forgetTheChangeset() throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSL.using(su, SQLDialect.POSTGRES).deleteFrom(DSL.table(DSL.name("public", "databasechangelog")))
                .where(DSL.field(DSL.name("id"), String.class).eq(CHANGESET)).execute();
        }
    }

    private void dropTheIndex() throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            // the changeset's own rollback text (DROP INDEX CONCURRENTLY IF EXISTS), so no DDL string lives here
            PgContainerHelper.replayChangesetRollback(su, "db/changelog/vectors-075-chunk-orphaned-at-chash-index.xml", CHANGESET);
        }
    }

    // ── 1: a fresh walk ─────────────────────────────────────────────────────────────────────────────────────

    @Test
    @Order(1)
    void aFreshWalkBuildsTheIndexValidAndRecordsTheChangeset() throws Exception {
        var first = SchemaMigrator.migrate(adminDs);

        assertThat(indexValid()).as("the index exists and is VALID").isTrue();
        assertThat(recorded()).as("the changeset ran, so it is recorded").isTrue();
        assertThat(first.skippedRetryingChangesets()).as("only the changeset that waits for its data move is skipped").isEqualTo(WAITING_ON_ITS_OWN);
        try (Connection su = pg.createConnection("")) {
            assertThat(PgCatalogProbes.indexDef(DSL.using(su, SQLDialect.POSTGRES), "nexus", INDEX))
                .as("the child-side lookup of chunk_orphaned_at_chunk_id_fk").contains("(tenant_id, chash)");
        }
        assertThat(SchemaMigrator.migrate(adminDs).skippedRetryingChangesets()).as("a second walk: nothing new to retry").isEqualTo(WAITING_ON_ITS_OWN);
    }

    // ── 2: a valid index with no record ─────────────────────────────────────────────────────────────────────

    @Test
    @Order(2)
    void aValidIndexWithNoRecordIsSkippedUnrecordedAndLeftStanding_aVisibleRetryingMember() throws Exception {
        forgetTheChangeset();

        var outcome = new SchemaMigrator.MigrationOutcome[1];
        List<String> logs = captureLogs(() -> outcome[0] = SchemaMigrator.migrate(adminDs));

        assertThat(indexValid()).as("a VALID index is never dropped").isTrue();
        assertThat(recorded()).as("CONTINUE skips without a record").isFalse();
        assertThat(outcome[0].skippedRetryingChangesets()).as("a second visible member of the retrying set beside the data-move one").isEqualTo(WAITING_ON_ITS_OWN + 1);
        // the interaction with the per-collection index builders: a self-retrying changeset is not schema work, so a running build is kept, and the boot names it
        assertThat(logs).as("no builder is ended for it").noneMatch(l -> l.contains("event=pci_builders_terminated"));
        assertThat(logs).anyMatch(l -> l.contains("event=pci_builders_kept") && l.contains("retrying=" + (WAITING_ON_ITS_OWN + 1)));
        assertThat(logs).as("the retrying set is exactly the two").filteredOn(l -> l.contains("event=pci_build_kept_for_retrying"))
            .hasSize(WAITING_ON_ITS_OWN + 1)
            .anyMatch(l -> l.contains("vectors-075-chunk-orphaned-at-chash-index.xml::vectors-075-1") && l.contains("reason=self_retrying_precondition"))
            .anyMatch(l -> l.contains("collections-001-tuple-unique-index.xml::collections-001-2"));
    }

    // ── 3: the wait ends, the boot does not fail, the next boot builds it ───────────────────────────────────

    /**
     * A writer holds ROW EXCLUSIVE on {@code nexus.chunk_orphaned_at} for longer than the 30 s lock_timeout, so the build's wait for it ends and the
     * build is abandoned with its INVALID entry in the catalog, the state a killed build leaves. The walk must still return and finish (the later
     * changesets ran), the changeset must be unrecorded and reported in the retrying count; after the writer is gone the next boot drops the INVALID
     * leftover and builds a valid index and records the changeset.
     */
    @Test
    @Order(3)
    @Timeout(value = 150, unit = TimeUnit.SECONDS)   // a wait with no bound hangs the walk: end the test, do not hang the fork
    void aWaitPastTheTimeoutIsASkippedBootNotAFailedOne_leavesAnInvalidIndex_andTheNextBootHealsIt() throws Exception {
        dropTheIndex();   // the record is already gone (test 2): the changeset is pending and its precondition passes
        assertThat(indexValid()).as("precondition: no index").isNull();
        assertThat(recorded()).as("precondition: pending").isFalse();

        Instant started = Instant.now();
        try (Connection writer = pg.createConnection("")) {
            writer.setAutoCommit(false);
            var orphaned = DSL.table(DSL.name("nexus", "chunk_orphaned_at"));
            Field<String> tenant = DSL.field(DSL.name("tenant_id"), String.class);
            DSL.using(writer, SQLDialect.POSTGRES).update(orphaned).set(tenant, tenant).where(DSL.falseCondition()).execute();   // ROW EXCLUSIVE, no row

            // the walk runs on its own thread: a wait with no bound must fail this test, not hang it (a blocked JDBC read ignores an interrupt)
            ExecutorService walker = Executors.newSingleThreadExecutor();
            SchemaMigrator.MigrationOutcome[] skipped = new SchemaMigrator.MigrationOutcome[1];
            try {
                Future<SchemaMigrator.MigrationOutcome> walk = walker.submit(() -> SchemaMigrator.migrate(adminDs));
                try {
                    skipped[0] = walk.get(100, TimeUnit.SECONDS);
                } catch (TimeoutException e) {
                    fail("the walk is still waiting after 100 s: the build's wait has no bound");
                } catch (ExecutionException e) {
                    fail("the boot failed on the wait", e.getCause());
                }
            } finally {
                writer.rollback();   // frees a walk that is still waiting, so the executor can end
                walker.shutdownNow();
            }

            Duration took = Duration.between(started, Instant.now());
            assertThat(took).as("the wait ended at the 30 s lock_timeout, not at the walk's end or never").isGreaterThanOrEqualTo(Duration.ofSeconds(25));
            assertThat(skipped[0]).isNotNull();
            assertThat(skipped[0].skippedRetryingChangesets()).as("reported in the walk's retrying set").isEqualTo(WAITING_ON_ITS_OWN + 1);
            assertThat(skipped[0].reexecutedChangesets()).as("the walk went on past it: the runAlways grants ran").isPositive();
            assertThat(recorded()).as("no DATABASECHANGELOG row for a skipped changeset").isFalse();
            assertThat(indexValid()).as("the abandoned build left an INVALID entry").isFalse();
        }

        var healed = SchemaMigrator.migrate(adminDs);

        assertThat(indexValid()).as("the next boot dropped the INVALID leftover and built a VALID index").isTrue();
        assertThat(recorded()).as("and recorded the changeset").isTrue();
        assertThat(healed.skippedRetryingChangesets()).isEqualTo(WAITING_ON_ITS_OWN);
    }
}
