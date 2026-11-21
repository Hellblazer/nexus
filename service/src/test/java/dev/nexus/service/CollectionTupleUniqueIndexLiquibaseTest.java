// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
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
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;

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
 * FORCE ROW LEVEL SECURITY, so a plain count by that role sees zero rows on every tenant, and
 * {@link #theCounterReadsEveryTenantWhereAPlainCountReadsNothing} pins that. Rows are seeded as superuser, which
 * bypasses RLS, as an engine writing through a tenant scope does.
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
     * The falsification of a naive guard. The migration role owns the table under FORCE ROW LEVEL SECURITY and has
     * no BYPASSRLS, so a plain count by that role sees nothing on a table that holds rows; the helper lifts FORCE
     * for the count, counts every tenant, and puts FORCE back.
     */
    @Test
    void theCounterReadsEveryTenantWhereAPlainCountReadsNothing() throws Exception {
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
                        .as("a plain count(*) by the NOBYPASSRLS owner reads 0: a guard written that way would "
                            + "call a duplicated registry clean")
                        .isEqualTo(0);
                    assertThat(duplicates(admin)).as("t1 and t2 each hold one duplicated tuple, t3 none").isEqualTo(2L);
                    assertThat(ctx.fetchCount(DSL.table(DSL.name("nexus", "catalog_collections"))))
                        .as("FORCE ROW LEVEL SECURITY is back after the helper: the plain count is blind again")
                        .isEqualTo(0);
                    var rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "catalog_collections");
                    assertThat(rls.enabled()).isTrue();
                    assertThat(rls.forced()).as("FORCE restored by the helper").isTrue();
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
                        "UPDATE nexus.catalog_collections SET superseded_by = 'docs__v3' WHERE tenant_id = 't1' AND name = 'docs__a'");
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

                // Between the boots the engine registers the move and its worker completes it (nothing to move on
                // a fresh install).
                try (Connection su = pg.createConnection("")) {
                    registerMove(su, MOVE, true);
                }

                // Boot 2: both conditions hold.
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertApplied(pg);

                // And a third boot has nothing left to do.
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertApplied(pg);
            }
        } finally {
            pg.stop();
        }
    }

    /** A precondition that THROWS (the migration role is not the table owner) skips the changeset, never halts. */
    @Test
    void aGuardThatThrows_skipsTheChangeset_itDoesNotHaltTheBoot() throws Exception {
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
                        .as("non-vacuity: the helper really throws for a role that does not own the table")
                        .hasMessageContaining("must be owner");
                }
                assertThatCode(() -> SchemaMigrator.migrate(adminDs)).doesNotThrowAnyException();
                assertWaiting(pg);
            }
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
        DSL.using(su, SQLDialect.POSTGRES).execute(
            "INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, model_version, "
                + "lifecycle_state, superseded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tenant, name, contentType, owner, model, modelVersion, lifecycle, supersededBy);
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
