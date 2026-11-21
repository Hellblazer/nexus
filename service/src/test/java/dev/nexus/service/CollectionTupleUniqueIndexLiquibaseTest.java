// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CollectionTupleIndexProbe;
import dev.nexus.service.db.SchemaMigrator;
import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-229 P3.8 (nexus-p2m1x.28): {@code collections-001-1} / {@code collections-001-2} give
 * {@code nexus.catalog_collections} a partial UNIQUE index on the live tuple
 * {@code (tenant_id, content_type, owner_id, embedding_model, model_version) WHERE superseded_by = '' AND
 * lifecycle_state <> 'quarantine'}, the candidate set of {@code CatalogRepository.collectionForTuple}, and the
 * changeset that creates it WAITS (RDR-246 "Finishing a move"): a preCondition with {@code onFail="CONTINUE"
 * onError="CONTINUE"} that (a) the {@code nexus.data_moves} row labelled {@link #MOVE} has completed, and (b)
 * {@code nexus.catalog_collection_tuple_duplicates_unscoped()} is 0. A skip records nothing, so every boot
 * re-evaluates it and the index lands the first boot both hold (Sam 2026-11-21, T2
 * nexus/sam-ruling-rdr229-p38-design-gate-v2-2026-11-21).
 *
 * <p>Every test runs as a NOBYPASSRLS, non-superuser owner (the production migration role's shape): the table is
 * FORCE ROW LEVEL SECURITY. The counter is a plain SECURITY INVOKER SQL function that never lifts FORCE (a boot in the
 * cloud must not take ACCESS EXCLUSIVE on the registry, {@link #aWalkWithADuplicateTakesNoAccessExclusiveLock}); it
 * reads every tenant's rows through D2's owner-read policy {@code catalog_collections_owner_read}
 * (RDR-229 D2, changeset vectors-065), which this changeset does not create. D2 is not on this branch, so the tests
 * install {@link #standInOwnerRead}, the policy by the datamove fixture marlowe's move test uses, and
 * {@link #theCounterReadsEveryTenantThroughTheOwnerReadPolicy} pins both halves: blind without the policy, every
 * tenant with it. Rows are seeded as superuser, which bypasses RLS, as an engine writing through a tenant scope does.
 */
class CollectionTupleUniqueIndexLiquibaseTest {

    private static final String ADMIN_ROLE = "nexus_admin_ctuple";
    private static final String ADMIN_PASS = "nexus_admin_ctuple_pass";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";

    static final String HELPER_CHANGESET = "collections-001-1";
    static final String INDEX_CHANGESET = "collections-001-2";
    /** A changeset after collections-001 in the master changelog: the walk went past the skip. */
    private static final String LATER_CHANGESET = "grants-nexus-svc-1";
    /** The label marlowe's re-own move registers under (fixed by the design of record). */
    static final String MOVE = "rdr229-reown-repo-stems-from-curator";

    private static final String INDEX = "catalog_collections_live_tuple_uq";
    private static final String HELPER = "catalog_collection_tuple_duplicates_unscoped";
    private static final String MODEL = "bge-base-en-v15-768";

    // ── (a) the move: absent, open, failed ─────────────────────────────────────

    @Test
    void whileTheMoveIsAbsent_theIndexIsSkippedUnrecorded_theWalkGoesOn_andEveryBootRetriesIt() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);
                // A second walk is the same: still skipped, still unrecorded (CONTINUE, never MARK_RAN).
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aMoveThatIsOpenOrFailed_isNotACompletedMove_theIndexStaysSkipped() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, MOVE, false);
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);

                try (Connection su = pg.createConnection("")) {
                    DSL.using(su, SQLDialect.POSTGRES).execute("UPDATE nexus.data_moves SET failed_at = now(), failure = 'boom' WHERE label = ?", MOVE);
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aCompletedMoveUnderAnotherLabelIsNotTheMove() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, "some-other-move", true);
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);
            }
        } finally {
            pg.stop();
        }
    }

    // ── (b) the duplicate ───────────────────────────────────────────────────────

    @Test
    void aCompletedMoveWithADuplicateTuple_isSkipped_thenTheRetryAppliesOnceTheDuplicateIsGone() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, INDEX_CHANGESET);
                standInOwnerRead(adminDs);
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, MOVE, true);
                    seed(su, "t1", "docs__a", "docs", "owner-1", MODEL, "", "live", "");
                    seed(su, "t1", "docs__b", "docs", "owner-1", MODEL, "", "live", "");
                    assertThat(duplicates(su)).as("premise: the tuple is shared by two live rows").isEqualTo(1L);
                }

                assertThatCode(() -> SchemaMigrator.migrate(adminDs))
                    .as("a duplicate must never halt the boot").doesNotThrowAnyException();
                assertWaiting(pg);
                try (Connection su = pg.createConnection("")) {
                    assertThat(changesetRecorded(su, HELPER_CHANGESET)).isTrue();
                    assertThat(duplicates(su)).as("the changeset left the rows alone").isEqualTo(1L);
                }

                // The operator (or the re-own move) resolves the duplicate; the next boot applies the index.
                try (Connection su = pg.createConnection("")) {
                    DSL.using(su, SQLDialect.POSTGRES).execute("DELETE FROM nexus.catalog_collections WHERE name = 'docs__b'");
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertApplied(pg);
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The counter lifts nothing: it reads whatever the migration role may read. Without an owner-read policy that is
     * no row (a NOBYPASSRLS owner under FORCE sees nothing, so the counter answers 0 and the index build, not the
     * counter, refuses a real duplicate); with D2's policy it is every tenant's rows.
     */
    @Test
    void theCounterReadsEveryTenantThroughTheOwnerReadPolicy() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, INDEX_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    // Two tenants, each with one duplicated tuple; a third tuple that is NOT a duplicate (a
                    // quarantine sibling, a superseded row and a different model_version do not collide).
                    seed(su, "t1", "docs__a", "docs", "owner-1", MODEL, "", "live", "");
                    seed(su, "t1", "docs__b", "docs", "owner-1", MODEL, "", "live", "");
                    seed(su, "t2", "docs__c", "docs", "owner-1", MODEL, "", "live", "");
                    seed(su, "t2", "docs__d", "docs", "owner-1", MODEL, "", "dormant", "");
                    seed(su, "t3", "docs__e", "docs", "owner-9", MODEL, "", "live", "");
                    seed(su, "t3", "quarantine-docs__e", "docs", "owner-9", MODEL, "", "quarantine", "");
                    seed(su, "t3", "docs__e_old", "docs", "owner-9", MODEL, "", "live", "docs__e");
                    seed(su, "t3", "docs__e_v2", "docs", "owner-9", MODEL, "v2", "live", "");
                }
                try (Connection admin = adminDs.getConnection()) {
                    DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
                    assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections"))))
                        .as("premise: a plain count(*) by the NOBYPASSRLS owner reads 0 with no owner-read policy")
                        .isEqualTo(0);
                    assertThat(duplicates(admin)).as("no policy, no rows, no duplicate seen: the counter lifts nothing")
                        .isEqualTo(0L);
                }

                standInOwnerRead(adminDs);

                try (Connection admin = adminDs.getConnection()) {
                    DSLContext ctx = DSL.using(admin, SQLDialect.POSTGRES);
                    assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections"))))
                        .as("the owner-read policy lets the migration role read every tenant").isEqualTo(8);
                    assertThat(duplicates(admin)).as("t1 and t2 each hold one duplicated tuple, t3 none (FORCE RLS on)")
                        .isEqualTo(2L);
                    var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "catalog_collections");
                    assertThat(rls.enabled()).isTrue();
                    assertThat(rls.forced()).as("FORCE ROW LEVEL SECURITY was never lifted").isTrue();
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * A boot in the cloud must never take ACCESS EXCLUSIVE on the registry, even while a duplicate keeps the index
     * waiting. A second connection holds ACCESS SHARE on nexus.catalog_collections for the whole walk: any statement
     * that asks for ACCESS EXCLUSIVE (an ALTER TABLE in the counter) queues behind it and the walk cannot finish.
     * Every earlier changeset is already applied, so only the changesets from the index on can ask.
     */
    @Test
    void aWalkWithADuplicateTakesNoAccessExclusiveLock() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        ExecutorService walker = Executors.newSingleThreadExecutor();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, INDEX_CHANGESET);
                standInOwnerRead(adminDs);
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, MOVE, true);
                    seed(su, "t1", "docs__a", "docs", "owner-1", MODEL, "", "live", "");
                    seed(su, "t1", "docs__b", "docs", "owner-1", MODEL, "", "live", "");
                }
                try (Connection holder = pg.createConnection("")) {
                    holder.setAutoCommit(false);
                    DSL.using(holder, SQLDialect.POSTGRES).execute("LOCK TABLE nexus.catalog_collections IN ACCESS SHARE MODE");
                    Future<?> walk = walker.submit(() -> {
                        SchemaMigrator.migrate(adminDs);
                        return null;
                    });
                    try {
                        walk.get(90, TimeUnit.SECONDS);
                    } catch (TimeoutException e) {
                        String waiting;
                        try (Connection su = pg.createConnection("")) {
                            waiting = DSL.using(su, SQLDialect.POSTGRES).fetch(
                                "SELECT l.mode, a.query FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                                    + "WHERE NOT l.granted AND l.relation = 'nexus.catalog_collections'::regclass").toString();
                        }
                        holder.rollback();
                        walk.get(60, TimeUnit.SECONDS);
                        throw new AssertionError("the walk queued behind an ACCESS SHARE lock on the registry: "
                            + "something asked for ACCESS EXCLUSIVE. Waiting: " + waiting);
                    }
                    holder.rollback();
                }
                assertWaiting(pg);
            }
        } finally {
            walker.shutdownNow();
            pg.stop();
        }
    }

    /** The changeset file holds no ALTER, in a statement or a comment: the counter that needed one is gone. */
    @Test
    void theChangesetFileHoldsNoAlter() throws Exception {
        String xml;
        try (InputStream in = getClass().getClassLoader()
                .getResourceAsStream("db/changelog/collections-001-tuple-unique-index.xml")) {
            assertThat(in).as("the changelog is on the classpath").isNotNull();
            xml = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        assertThat(xml).contains(HELPER_CHANGESET).contains(INDEX_CHANGESET);
        assertThat(xml).doesNotContainPattern("(?i)\\bALTER\\b");
    }

    @Test
    void theCounterIsAPlainSecurityInvokerSqlFunction() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, INDEX_CHANGESET);
                try (Connection admin = adminDs.getConnection()) {
                    var row = DSL.using(admin, SQLDialect.POSTGRES).fetchOne(
                        "SELECT p.prosecdef, l.lanname FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
                            + "WHERE p.pronamespace = 'nexus'::regnamespace AND p.proname = ?", HELPER);
                    assertThat(row).isNotNull();
                    assertThat(row.get(0, Boolean.class)).as("SECURITY INVOKER: it runs as the migration role").isFalse();
                    assertThat(row.get(1, String.class)).as("a plain SQL function, no plpgsql ALTER").isEqualTo("sql");
                }
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void theCounterCountsTuplesNotRows_andAnEmptyRegistryIsZero() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, INDEX_CHANGESET);
                standInOwnerRead(adminDs);
                try (Connection admin = adminDs.getConnection()) {
                    assertThat(duplicates(admin)).isEqualTo(0L);
                }
                try (Connection su = pg.createConnection("")) {
                    // Three live rows on one tuple are ONE duplicated tuple.
                    seed(su, "t1", "docs__a", "docs", "owner-1", MODEL, "", "live", "");
                    seed(su, "t1", "docs__b", "docs", "owner-1", MODEL, "", "live", "");
                    seed(su, "t1", "docs__c", "docs", "owner-1", MODEL, "", "disputed", "");
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThat(duplicates(admin)).isEqualTo(1L);
                }
            }
        } finally {
            pg.stop();
        }
    }

    // ── the index itself ────────────────────────────────────────────────────────

    @Test
    void withTheMoveCompleteAndNoDuplicate_theIndexExists_andRefusesASecondLiveRowOnATuple() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, INDEX_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, MOVE, true);
                    seed(su, "t1", "docs__a", "docs", "owner-1", MODEL, "", "live", "");
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertApplied(pg);

                try (Connection su = pg.createConnection("")) {
                    // A second live row on the tuple is refused, by the unique index.
                    assertThatThrownBy(() -> seed(su, "t1", "docs__b", "docs", "owner-1", MODEL, "", "live", ""))
                        .isInstanceOf(DataAccessException.class)
                        .hasMessageContaining(INDEX);
                    // A dormant or disputed row still holds its tuple (collectionForTuple answers it).
                    assertThatThrownBy(() -> seed(su, "t1", "docs__c", "docs", "owner-1", MODEL, "", "dormant", ""))
                        .isInstanceOf(DataAccessException.class)
                        .hasMessageContaining(INDEX);

                    // What the predicate leaves out is allowed on the same tuple: a quarantine sibling, a
                    // superseded row, a different model_version (a same-model re-embed target), another tenant.
                    assertThatCode(() -> {
                        seed(su, "t1", "quarantine-docs__a", "docs", "owner-1", MODEL, "", "quarantine", "");
                        seed(su, "t1", "docs__old", "docs", "owner-1", MODEL, "", "live", "docs__a");
                        seed(su, "t1", "docs__v2", "docs", "owner-1", MODEL, "v2", "live", "");
                        seed(su, "t2", "docs__a", "docs", "owner-1", MODEL, "", "live", "");
                    }).as("rows the predicate excludes, and a different tenant or version").doesNotThrowAnyException();

                    // A live row can be superseded, and then the tuple is free again.
                    DSL.using(su, SQLDialect.POSTGRES).execute(
                        "UPDATE nexus.catalog_collections SET superseded_by = 'docs__v3', superseded_by_id = 1 WHERE tenant_id = 't1' AND name = 'docs__a'");
                    assertThatCode(() -> seed(su, "t1", "docs__fresh", "docs", "owner-1", MODEL, "", "live", ""))
                        .doesNotThrowAnyException();
                }
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void aFreshInstall_boot1WaitsForTheMove_boot2AppliesTheIndex() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                // Boot 1: the migration runs before the engine registers the move, so the move is absent.
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);
                assertThat(CollectionTupleIndexProbe.present(adminDs)).as("the status probe: waiting").isFalse();

                // Between the boots the engine registers the move and its worker completes it (nothing to move on
                // a fresh install).
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, MOVE, true);
                }

                // Boot 2: both conditions hold.
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertApplied(pg);
                assertThat(CollectionTupleIndexProbe.present(adminDs)).as("the status probe: present").isTrue();

                // And a third boot has nothing left to do.
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertApplied(pg);
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * The migration role does not own the table and holds no privilege on it: the counter is refused
     * ("permission denied"). A precondition that THROWS is an error, not a failed check, and onError="CONTINUE" skips the
     * changeset unrecorded; it never halts the boot.
     */
    @Test
    void aCounterThatIsRefused_skipsTheChangeset_itDoesNotHaltTheBoot() throws Exception {
        String otherOwner = "ctuple_other_owner";
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase(otherOwner);
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
                PgContainerHelper.createLoginRole(su, otherOwner, "ctuple_other_owner_pass", false);
            }
            try (HikariDataSource adminDs = adminPool(pg)) {
                migrateUpTo(adminDs, INDEX_CHANGESET);
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, MOVE, true);
                    PgContainerHelper.alterTableOwner(su, DSL.table(DSL.name("nexus", "catalog_collections")), otherOwner);
                }
                try (Connection admin = adminDs.getConnection()) {
                    assertThatThrownBy(() -> duplicates(admin))
                        .as("non-vacuity: the counter really is refused for a role with no privilege on the table")
                        .hasMessageContaining("permission denied");
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);
            }
        } finally {
            pg.stop();
        }
    }

    @Test
    void theStatusProbe_isNullWhenItCannotRun() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startEmptyDatabase();
        try {
            try (Connection su = pg.createConnection("")) {
                PgContainerHelper.bootstrapNonSuperuserOwner(su, ADMIN_ROLE, ADMIN_PASS);
            }
            HikariDataSource pool = adminPool(pg);
            assertThat(CollectionTupleIndexProbe.present(pool)).as("premise: it runs on a live pool").isFalse();
            pool.close();
            assertThat(CollectionTupleIndexProbe.present(pool)).as("a probe that could not run answers null").isNull();
        } finally {
            pg.stop();
        }
    }

    // ── assertions ──────────────────────────────────────────────────────────────

    /** The helper ran, the index changeset was skipped without a record, the index is absent, the walk went on. */
    private static void assertWaiting(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(changesetRecorded(su, HELPER_CHANGESET)).as("the helper changeset is recorded").isTrue();
            assertThat(PgCatalogProbes.routineExists(ctx, "nexus", HELPER)).isTrue();
            assertThat(PgCatalogProbes.indexExists(ctx, "nexus", INDEX)).as("the index is absent").isFalse();
            assertThat(changesetRecorded(su, INDEX_CHANGESET))
                .as("CONTINUE skips the changeset WITHOUT recording it (MARK_RAN would stop the retry)").isFalse();
            assertThat(changesetRecorded(su, LATER_CHANGESET)).as("the walk went past the skip").isTrue();
            assertThat(PgCatalogProbes.rowSecurity(ctx, "nexus", "catalog_collections").forced())
                .as("the guard left FORCE ROW LEVEL SECURITY on").isTrue();
        }
    }

    private static void assertApplied(PostgreSQLContainer<?> pg) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            assertThat(PgCatalogProbes.indexExists(ctx, "nexus", INDEX)).as("the index exists").isTrue();
            assertThat(changesetRecorded(su, INDEX_CHANGESET)).isTrue();
            String def = PgCatalogProbes.indexDef(ctx, "nexus", INDEX);
            assertThat(def).contains("UNIQUE")
                .contains("tenant_id, content_type, owner_id, embedding_model, model_version")
                .contains("superseded_by")
                .contains("lifecycle_state");
            assertThat(PgCatalogProbes.rowSecurity(ctx, "nexus", "catalog_collections").forced())
                .as("FORCE ROW LEVEL SECURITY stays on").isTrue();
        }
    }

    // ── fixtures ────────────────────────────────────────────────────────────────

    /**
     * STAND-IN for D2's changeset vectors-065 (RDR-229 owner-is-source), which is not on this branch: the owner-read
     * SELECT policy {@code catalog_collections_owner_read} for the migration role, by the same datamove fixture and
     * guard marlowe's {@code Rdr229ReownRepoStemsMoveIntegrationTest.standIn} uses. When vectors-065 lands the
     * migration walk creates the policy itself; swap this for the real changeset and delete it.
     */
    private static void standInOwnerRead(HikariDataSource adminDs) throws Exception {
        try (Connection admin = adminDs.getConnection()) {
            PgContainerHelper.runSuperuserTestChangelog(admin, "datamove/owner-read-policy/policy.xml",
                "databasechangelog_test_ctuple_owner_read",
                Map.of("driving_schema", "nexus", "driving_table", "catalog_collections"));
            assertThat(PgCatalogProbes.policyExists(DSL.using(admin, SQLDialect.POSTGRES), "nexus", "catalog_collections",
                "catalog_collections_owner_read")).as("the stand-in policy is in place (the guard did not skip it)").isTrue();
        }
    }

    private static void registerMove(Connection su, String label, boolean completed) {
        DSL.using(su, SQLDialect.POSTGRES).execute(
            "INSERT INTO nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms, "
                + "statement_timeout_ms, verification, changeset_id, completed_at) "
                + "VALUES (?, 'SELECT 1', 'nexus', 'catalog_collections', 100, 0, 60000, 'SELECT 1 WHERE false', "
                + "'test-fixture', " + (completed ? "now()" : "NULL") + ")",
            label);
    }

    private static void seed(Connection su, String tenant, String name, String contentType, String owner, String model,
                             String modelVersion, String lifecycle, String supersededBy) {
        // P3.1's catalog_collections_superseded_by_id_check: a tombstone (superseded_by <> '') carries a superseded_by_id too
        // (any value: this test judges the live-tuple index, not the successor's id)
        DSL.using(su, SQLDialect.POSTGRES).execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, model_version, "
                + "lifecycle_state, superseded_by, superseded_by_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tenant, name, contentType, owner, model, modelVersion, lifecycle, supersededBy, supersededBy.isEmpty() ? null : 1L);
    }

    private static Long duplicates(Connection conn) {
        return DSL.using(conn, SQLDialect.POSTGRES).select(
            DSL.function(DSL.name("nexus", HELPER), Long.class)).fetchOne(0, Long.class);
    }

    private static HikariDataSource adminPool(PostgreSQLContainer<?> pg) {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(ADMIN_ROLE);
        cfg.setPassword(ADMIN_PASS);
        cfg.setMaximumPoolSize(2);
        cfg.setPoolName("nexus-admin-ctuple");
        return new HikariDataSource(cfg);
    }

    private static void migrateUpTo(HikariDataSource ds, String changesetId) throws Exception {
        try (Connection conn = ds.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<liquibase.changelog.ChangeSet> unrun =
                    liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
                int idx = -1;
                for (int i = 0; i < unrun.size(); i++) {
                    if (changesetId.equals(unrun.get(i).getId())) {
                        idx = i;
                        break;
                    }
                }
                assertThat(idx).as("%s must be pending in the HEAD changelog", changesetId)
                    .isGreaterThanOrEqualTo(0);
                liquibase.update(idx, new Contexts(), new LabelExpression());
            }
        }
    }

    private static boolean changesetRecorded(Connection conn, String changesetId) {
        return DSL.using(conn, SQLDialect.POSTGRES).fetchExists(
            DSL.selectOne().from(DSL.table(DSL.name("public", "databasechangelog")))
                .where(DSL.field(DSL.name("id"), String.class).eq(changesetId)));
    }
}
