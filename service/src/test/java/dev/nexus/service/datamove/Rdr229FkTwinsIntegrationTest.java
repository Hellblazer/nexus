// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.RetiredAspectQueueTable;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.sql.Connection;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/**
 * RDR-229 P3.3a (nexus-p2m1x.19): the seven id-twin foreign keys, each beside its name key and NOT VALID, and the validation move that
 * validates it. The base walks the REAL changelog as the production migrating role on the pg_background image, so the twins and the
 * moves are the walk's own, and the REAL worker runs the moves with its production failure handler and finisher.
 *
 * <ul>
 *   <li>the walk leaves every twin NOT VALID, with the name key's ON DELETE action, writes no row of a table, registers one open move per
 *       table (not the retired queue's, whose table is absent) and leaves the name keys alone;</li>
 *   <li>a NOT VALID twin already refuses a new row that names no registry row;</li>
 *   <li>each move completes and leaves its twin validated;</li>
 *   <li>an upgrade boot over populated tables (the changeset file run again by Liquibase) adds the twins and rewrites no row;</li>
 *   <li>the queue (R3): kept with its column, the twin and the move arrive, the move completes and its label holds; kept without the column
 *       nothing is added, the boot does not fail, and the next boot adds both once the column is there; absent, nothing is added and the label
 *       is not applicable.</li>
 * </ul>
 */
class Rdr229FkTwinsIntegrationTest extends DataMoveIntegrationBase {

    private static final Field<String> FAILURE = DSL.field(DSL.name("failure"), String.class);
    private static final Field<String> FILENAME = DSL.field(DSL.name("filename"), String.class);
    private static final Field<OffsetDateTime> REGISTERED_AT = DSL.field(DSL.name("registered_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final String QUEUE = FkTwinSpecs.QUEUE;
    private static final String QUEUE_LABEL = FkTwins.PREFIX + QUEUE;
    private static final String QUEUE_TWIN = FkTwinSpecs.twin(QUEUE).twin();
    private static final String QUEUE_CHECK = "aspect_extraction_queue_collection_id_check";
    private static final Field<Long> COLLECTION_ID = DSL.field(DSL.name("collection_id"), Long.class);

    /** The move rows the walk wrote, by table, exactly as written; null where it wrote none. */
    private static final Map<String, Map<String, Object>> registered = new LinkedHashMap<>();
    /** The latest registered_at of the stamp moves the walk wrote. */
    private static OffsetDateTime lastStampRegisteredAt;
    /** The twins as the walk left them, by name; null where the walk added none. */
    private static final Map<String, PgCatalogProbes.Constraint> twinsAtBoot = new LinkedHashMap<>();

    private final List<String> tenantsUsed = new ArrayList<>();

    @BeforeAll
    void captureTheWalk() {
        for (FkTwins.Row r : FkTwinSpecs.twins()) {
            Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(r.label())).fetchOne();
            registered.put(r.table(), row == null ? null : new LinkedHashMap<>(row.intoMap()));
            twinsAtBoot.put(r.twin(), PgCatalogProbes.foreignKey(ctx(), "nexus", r.twin()));
        }
        lastStampRegisteredAt = ctx().select(DSL.max(REGISTERED_AT)).from(table("nexus", "data_moves"))
            .where(LABEL.like(StampMoveSpecs.LABEL_PREFIX + "%")).fetchOne(0, OffsetDateTime.class);
    }

    @AfterEach
    void removeSeeds() {
        ctx().dropTableIfExists(table("nexus", QUEUE)).execute();
        for (String t : tenantsUsed) {
            StampMoveSeeds.clean(ctx(), t);
        }
        tenantsUsed.clear();
    }

    Stream<String> tables() {
        return FkTwinSpecs.alwaysThere().stream().map(FkTwins.Row::table);
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private String tenantFor(String table, String scenario) {
        String t = "p33a-" + scenario + "-" + table.replace('_', '-');
        tenantsUsed.add(t);
        return t;
    }

    private interface OnAdmin {
        void run(DSLContext admin, Connection conn) throws Exception;
    }

    /** Runs {@code work} as the migrating role, then gives the pool's one connection back. */
    private void asMigratingRole(OnAdmin work) throws Exception {
        try (Connection c = adminPool.getConnection()) {
            work.run(DSL.using(c, SQLDialect.POSTGRES), c);
        }
    }

    /** The twin of {@code table} as a fresh walk makes it: NOT VALID. The changeset's own rollback and forward text, so no DDL string lives here. */
    private void rebuildTwin(String table) {
        int i = FkTwinSpecs.indexOf(table);
        ctx().connection(c -> {
            try {
                PgContainerHelper.replayChangesetRollback(c, FkTwinSpecs.CHANGELOG, FkTwinSpecs.ddlChangeset(i));
                PgContainerHelper.replayChangeset(c, FkTwinSpecs.CHANGELOG, FkTwinSpecs.ddlChangeset(i));
            } catch (java.sql.SQLException | RuntimeException e) {
                throw e;
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }

    /** Puts the registration back as the walk wrote it. */
    private void register(String table) {
        Map<String, Object> row = registered.get(table);
        assertThat(row).as("the walk registers the move for " + table).isNotNull();
        List<Field<?>> columns = new ArrayList<>();
        List<Object> values = new ArrayList<>();
        row.forEach((k, v) -> {
            columns.add(DSL.field(DSL.name(k)));
            values.add(v);
        });
        ctx().insertInto(table("nexus", "data_moves")).columns(columns).values(values).execute();
    }

    private DataMoveWorker productionWorker() {
        return worker(installed(), 0, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()), new VerifyingFinisher());
    }

    private String failure(String label) {
        return ctx().select(FAILURE).from(table("nexus", "data_moves")).where(LABEL.eq(label)).fetchOne(FAILURE);
    }

    private PgCatalogProbes.Constraint twin(String table) {
        return PgCatalogProbes.foreignKey(ctx(), "nexus", FkTwinSpecs.twin(table).twin());
    }

    private CompletionCheck.Result readSet(String... labels) {
        return CompletionCheck.evaluate(ctx(), new CompletionCheck.ReadSet(List.of(labels), List.of()),
            CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
    }

    /**
     * An upgrade boot over a store that already ran this file's changesets once: the twins go (their own rollbacks), the registered moves go, the
     * changesets are forgotten, and the REAL Liquibase walk runs them again, preconditions and all.
     */
    private void upgradeAgain() {
        for (FkTwins.Row r : FkTwinSpecs.twins()) {
            ctx().connection(c -> {
                try {
                    PgContainerHelper.replayChangesetRollback(c, FkTwinSpecs.CHANGELOG, FkTwinSpecs.ddlChangeset(FkTwinSpecs.indexOf(r.table())));
                } catch (java.sql.SQLException | RuntimeException e) {
                    throw e;
                } catch (Exception e) {
                    throw new IllegalStateException(e);
                }
            });
        }
        ctx().deleteFrom(table("nexus", "data_moves")).where(LABEL.like(FkTwins.PREFIX + "%")).execute();
        ctx().deleteFrom(table("public", "databasechangelog")).where(FILENAME.eq(FkTwinSpecs.CHANGELOG)).execute();
        installed();
    }

    // ── 1. the walk ──────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void theWalkAddsEachTwinNotValid_withTheNameKeysDeleteAction_beside_theNameKey_andRegistersOneOpenMovePerTable() {
        assertThat(FkTwinSpecs.alwaysThere()).as("non-vacuity: six tables every install has").hasSize(6);
        for (FkTwins.Row r : FkTwinSpecs.alwaysThere()) {
            var twin = twinsAtBoot.get(r.twin());
            assertThat(twin).as("%s: the walk adds the twin", r.table()).isNotNull();
            assertThat(twin.convalidated()).as("%s: NOT VALID until its move runs", r.table()).isFalse();
            assertThat(twin.confdeltype()).as("%s: the name key's ON DELETE action", r.table()).isEqualTo(FkTwinSpecs.deleteType(r.onDelete()));
            assertThat(twin.confupdtype()).as("%s: ids never change, so ON UPDATE is the default", r.table()).isEqualTo("a");
            assertThat(PgCatalogProbes.foreignKey(ctx(), "nexus", r.nameKey())).as("%s: the name key stays (P3.7 drops it)", r.table()).isNotNull();
            assertThat(PgCatalogProbes.foreignKey(ctx(), "nexus", r.nameKey()).confdeltype())
                .as("%s: and the twin copies its ON DELETE action", r.table()).isEqualTo(twin.confdeltype());
            var move = registered.get(r.table());
            assertThat(move).as("%s: the walk registers the move", r.table()).isNotNull();
            assertThat(move).containsEntry("driving_schema", "nexus").containsEntry("driving_table", r.table())
                .containsEntry("chunk_size", FkTwinSpecs.chunkSize(r.table())).containsEntry("sleep_ms", FkTwinSpecs.sleepMs(r.table()))
                // registered at FkTwinSpecs.STATEMENT_TIMEOUT_MS (vectors-072); vectors-081-1 then drops the bound of a validation that blocks no writer
                .containsEntry("statement_timeout_ms", DataMove.NO_STATEMENT_BOUND_MS)
                .containsEntry("completed_at", null).containsEntry("failed_at", null).containsEntry("paused_at", null).containsEntry("supersedes", null);
            assertThat((OffsetDateTime) move.get("registered_at")).as("%s: registered after the stamp moves, so the worker takes it after", r.table())
                .isAfterOrEqualTo(lastStampRegisteredAt);
        }
        var shipped = ShippedMoveRegistrations.labels().stream().filter(l -> l.startsWith(FkTwins.PREFIX)).toList();
        assertThat(shipped).as("the changelog registers at boot the six moves of tables every install has; the queue's is inside a guard")
            .containsExactlyInAnyOrderElementsOf(FkTwinSpecs.alwaysThere().stream().map(FkTwins.Row::label).toList());
    }

    @Test
    void aFreshInstallHasNoQueue_soNoQueueTwinNoQueueMove_andItsLabelIsNotApplicable_andHoldsGate1() {
        assertThat(PgCatalogProbes.tableExists(ctx(), "nexus", QUEUE)).as("non-vacuity: the table is absent").isFalse();
        assertThat(twinsAtBoot.get(QUEUE_TWIN)).as("the walk added no twin to a table that is not there").isNull();
        assertThat(registered.get(QUEUE)).as("and registered no move for it").isNull();
        var r = readSet(QUEUE_LABEL);
        assertThat(r.labels().get(0).status()).isEqualTo(CompletionCheck.Status.NOT_APPLICABLE);
        assertThat(r.gate1()).as("gate 1 can hold without it").isTrue();
    }

    // ── 2. a NOT VALID twin already guards every new row ────────────────────────────────────────────────────

    @ParameterizedTest(name = "{0}")
    @MethodSource("tables")
    void aNotValidTwinRefusesARowThatNamesAnIdNoRegistryRowCarries(String table) {
        String tenant = tenantFor(table, "enforce");
        StampMoveSeeds.seed(ctx(), table, tenant, StampMoveSeeds.Plan.clean(1));
        rebuildTwin(table);
        assertThat(twin(table).convalidated()).as("non-vacuity: the twin is NOT VALID").isFalse();
        String idColumn = StampMoveSpecs.idColumns(table).get(0);
        assertThatThrownBy(() -> ctx().update(table("nexus", table)).set(DSL.field(DSL.name(idColumn), Long.class), 999_999_999L)
                .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).execute())
            .isInstanceOf(DataAccessException.class).hasMessageContaining(FkTwinSpecs.twin(table).twin());
    }

    // ── 3. each move completes and leaves its twin validated ─────────────────────────────────────────────────

    @ParameterizedTest(name = "{0}")
    @MethodSource("tables")
    void theMoveCompletes_andLeavesItsTwinValidated_andTheNameKeyInPlace(String table) {
        FkTwins.Row r = FkTwinSpecs.twin(table);
        String tenant = tenantFor(table, "move");
        var seeded = StampMoveSeeds.seed(ctx(), table, tenant, StampMoveSeeds.Plan.clean(3));
        var before = StampMoveSeeds.rows(ctx(), table, tenant);
        rebuildTwin(table);
        assertThat(twin(table).convalidated()).as("non-vacuity: the twin is NOT VALID before its move").isFalse();
        register(table);

        assertThat(productionWorker().runCycle()).as("the one registered move is worked").isEqualTo(1);

        assertThat(completedAt(r.label())).as("it completes (failure: %s)", failure(r.label())).isNotNull();
        assertThat(failure(r.label())).as("and records no failure").isNull();
        assertThat(twin(table).convalidated()).as("on_complete validated the twin").isTrue();
        assertThat(PgCatalogProbes.foreignKey(ctx(), "nexus", r.nameKey())).as("the name key is still there").isNotNull();
        var after = StampMoveSeeds.rows(ctx(), table, tenant);
        for (String key : seeded.old()) {
            assertThat(after.get(key).ids().values()).as("%s: the template stamps what the stamp move has not reached", key).doesNotContainNull();
        }
        assertThat(after.get(seeded.stamped())).as("a stamped row is not written again").isEqualTo(before.get(seeded.stamped()));
        var state = readSet(r.label());
        assertThat(state.labels().get(0).status()).as("the completion check counts the label complete: move done and twin validated")
            .isEqualTo(CompletionCheck.Status.COMPLETE);
    }

    // ── 4. an upgrade boot over populated tables ─────────────────────────────────────────────────────────────

    @Test
    void anUpgradeBootAddsTheTwinsAndRegistersTheMoves_andRewritesNoRowOfAnyTable() {
        Map<String, Map<String, StampMoveSeeds.RowState>> before = new LinkedHashMap<>();
        for (String table : tables().toList()) {
            String tenant = tenantFor(table, "upgrade");
            StampMoveSeeds.seed(ctx(), table, tenant, StampMoveSeeds.Plan.clean(2));
            before.put(table, StampMoveSeeds.rows(ctx(), table, tenant));
        }
        int recorded = ctx().fetchCount(table("public", "databasechangelog"), FILENAME.eq(FkTwinSpecs.CHANGELOG));
        assertThat(recorded).as("non-vacuity: the first walk recorded every changeset of the file").isEqualTo(FkTwinSpecs.twins().size() * 2);

        upgradeAgain();

        assertThat(ctx().fetchCount(table("public", "databasechangelog"), FILENAME.eq(FkTwinSpecs.CHANGELOG)))
            .as("the walk ran every changeset again").isEqualTo(recorded);
        for (FkTwins.Row r : FkTwinSpecs.alwaysThere()) {
            assertThat(twin(r.table()).convalidated()).as("%s: the twin is back, NOT VALID", r.table()).isFalse();
            assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL.eq(r.label()).and(COMPLETED_AT.isNull()))).as(r.label()).isEqualTo(1);
            String tenant = "p33a-upgrade-" + r.table().replace('_', '-');
            assertThat(StampMoveSeeds.rows(ctx(), r.table(), tenant)).as("%s: no row written at boot (xmin is the last writer's transaction)", r.table())
                .isEqualTo(before.get(r.table()));
        }
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL.eq(QUEUE_LABEL))).as("no queue move: the table is absent").isZero();
    }

    // ── 5. the retired queue (R3) ────────────────────────────────────────────────────────────────────────────

    /** As an install that kept the table has it: the old-shape rows first, with the registry row and the document they name. */
    private String keptQueueWithAnOldShapeRow() throws Exception {
        String tenant = tenantFor(QUEUE, "queue");
        String collection = StampMoveSeeds.collection(tenant, 0);
        PgContainerHelper.insertCollection(ctx(), tenant, collection, StampMoveSeeds.BGE);
        ctx().insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER, CATALOG_DOCUMENTS.TITLE,
                CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID)
            .values(tenant, "q-doc", "Doc", collection, StampMoveSeeds.id(ctx(), tenant, collection)).execute();
        asMigratingRole((admin, c) -> RetiredAspectQueueTable.restore(admin));
        RetiredAspectQueueTable.seed(ctx(), tenant, collection, "q-doc", "/p/" + tenant + ".md", "pending");
        return tenant;
    }

    @Test
    void aKeptQueueWithItsColumn_theUpgradeBootAddsTheTwinAndRegistersTheMove_whichCompletesAndValidatesIt() throws Exception {
        String tenant = keptQueueWithAnOldShapeRow();
        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, dev.nexus.service.P31CheckReplay.CHANGELOG, "vectors-068-18"));
        assertThat(PgCatalogProbes.constraintExists(ctx(), QUEUE_CHECK)).as("non-vacuity: vectors-068-18 gave the kept table its column and CHECK").isTrue();
        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, StampMoveSpecs.CHANGELOG, "vectors-070-31"));

        upgradeAgain();

        var twin = PgCatalogProbes.foreignKey(ctx(), "nexus", QUEUE_TWIN);
        assertThat(twin).as("the twin is added to the kept table").isNotNull();
        assertThat(twin.convalidated()).isFalse();
        assertThat(twin.confdeltype()).as("the name key's action, which aspects-005-1 made CASCADE").isEqualTo("c");
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL.eq(QUEUE_LABEL).and(COMPLETED_AT.isNull())))
            .as("the move is registered, open").isEqualTo(1);
        assertThat(readSet(QUEUE_LABEL).gate1()).as("registered and open: not complete").isFalse();

        assertThat(productionWorker().runCycle()).as("the boot registered all seven moves; the six over empty tables complete at once")
            .isEqualTo(FkTwinSpecs.twins().size());

        assertThat(completedAt(QUEUE_LABEL)).as("the move completes (failure: %s)", failure(QUEUE_LABEL)).isNotNull();
        assertThat(PgCatalogProbes.foreignKey(ctx(), "nexus", QUEUE_TWIN).convalidated()).as("on_complete validated the twin").isTrue();
        assertThat(ctx().select(COLLECTION_ID).from(table("nexus", QUEUE)).where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant))
            .fetch(COLLECTION_ID)).as("the row takes the id of its registry row").containsExactly(StampMoveSeeds.id(ctx(), tenant, StampMoveSeeds.collection(tenant, 0)));
        var state = readSet(QUEUE_LABEL);
        assertThat(state.labels().get(0).status()).isEqualTo(CompletionCheck.Status.COMPLETE);
        assertThat(state.gate1()).as("gate 1 can hold on an install that kept the queue").isTrue();
    }

    @Test
    void aKeptQueueWithoutItsColumn_theBootAddsNothingAndDoesNotFail_theLabelIsNotApplicable_andTheNextBootAddsBothOnceTheColumnIsThere() throws Exception {
        keptQueueWithAnOldShapeRow();
        assertThat(PgCatalogProbes.columnExists(ctx(), "nexus", QUEUE, "collection_id")).as("non-vacuity: vectors-068-18 was skipped, no column").isFalse();

        upgradeAgain();

        assertThat(PgCatalogProbes.constraintExists(ctx(), QUEUE_TWIN)).as("no twin on a table with no id column").isFalse();
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL.eq(QUEUE_LABEL))).as("and no move").isZero();
        assertThat(ctx().fetchCount(table("public", "databasechangelog"), FILENAME.eq(FkTwinSpecs.CHANGELOG)))
            .as("the queue's two changesets are skipped, unrecorded: the next boot retries them").isEqualTo(FkTwinSpecs.twins().size() * 2 - 2);
        var r = readSet(QUEUE_LABEL);
        assertThat(r.labels().get(0).status()).isEqualTo(CompletionCheck.Status.NOT_APPLICABLE);
        assertThat(r.labels().get(0).why()).contains("collection_id");
        assertThat(r.gate1()).isTrue();

        asMigratingRole((admin, c) -> PgContainerHelper.replayChangeset(c, dev.nexus.service.P31CheckReplay.CHANGELOG, "vectors-068-18"));
        installed();   // the next boot

        assertThat(PgCatalogProbes.constraintExists(ctx(), QUEUE_TWIN)).as("the twin arrives once the column is there").isTrue();
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), LABEL.eq(QUEUE_LABEL))).as("and the move").isEqualTo(1);
        assertThat(ctx().fetchCount(table("public", "databasechangelog"), FILENAME.eq(FkTwinSpecs.CHANGELOG)))
            .as("every changeset of the file is now recorded").isEqualTo(FkTwinSpecs.twins().size() * 2);
    }
}
