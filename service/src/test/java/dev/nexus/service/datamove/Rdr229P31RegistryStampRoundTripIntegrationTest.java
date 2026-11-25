// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CollectionTombstones;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RDR-229 P3.1 (nexus-p2m1x.17), the evidence under Sam's ask {@code p31-r5-window-registry-stamp}: the full round trip AFTER the registry's own
 * stamp move has completed.
 *
 * <ol>
 *   <li>(a) P3.1 is migrated and {@code rdr229-stamp-catalog_collections} runs to completion (its CHECK is validated); a stamp move of a large
 *       table is registered and open;</li>
 *   <li>(b) the operator down-step as the runbook orders it: pause the open stamp moves, then run {@code p31-downstep.sql};</li>
 *   <li>(c) the PREVIOUS engine writes the pre-P3.1 shapes: a collection rename that supersedes X with Y (a tombstone with
 *       {@code superseded_by = Y} and {@code superseded_by_id} NULL) and a new collection registration;</li>
 *   <li>(d) the redeploy: the generated re-add ({@code docs/runbooks/rdr-229-p31-readd.sql}), the release of the pause, and
 *       {@code nexus.rdr229_restamp('catalog_collections')} as the migrating role (the registry stamp is complete and does not run again);</li>
 *   <li>(e) the tombstone gets Y's id (R7), the new collection has its id, no tombstone violates the invariant, every CHECK is present again,
 *       the registry's validates once the re-registered move completes, and gate 1 holds for the registry label.</li>
 * </ol>
 * If a step fails here, the ask's reading (the window closes when the registry stamp completes, and a stamp that already completed can be re-run)
 * is wrong as stated.
 */
class Rdr229P31RegistryStampRoundTripIntegrationTest extends DataMoveIntegrationBase {

    private static final String REGISTRY_LABEL = StampMoveSpecs.LABEL_PREFIX + "catalog_collections";
    private static final String CHUNKS_LABEL = StampMoveSpecs.LABEL_PREFIX + "chunks";
    private static final String SUPERSEDED_CHECK = "catalog_collections_superseded_by_id_check";
    private static final String DOWN_STEP = "db/changelog-test/db.changelog-test-p31-downstep.xml";
    private static final String READD = "db/changelog-test/db.changelog-test-p31-readd.xml";
    /** What the runbook's pause statement writes into paused_by, so the release touches only what the pause held (scripts/rdr229_p31_downstep.py). */
    private static final String PAUSED_BY = "rdr229 down-step";
    private static final String TENANT = "p31-roundtrip";
    private static final Field<String> LABEL_F = DSL.field(DSL.name("label"), String.class);
    private static final Field<OffsetDateTime> PAUSED_AT = DSL.field(DSL.name("paused_at"), SQLDataType.TIMESTAMPWITHTIMEZONE);
    private static final Field<String> PAUSED_BY_F = DSL.field(DSL.name("paused_by"), String.class);

    private static final Map<String, Map<String, Object>> registered = new LinkedHashMap<>();

    @BeforeAll
    void captureRegistrations() {
        for (String label : List.of(REGISTRY_LABEL, CHUNKS_LABEL)) {
            Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(label)).fetchOne();
            registered.put(label, row == null ? null : new LinkedHashMap<>(row.intoMap()));
        }
    }

    @AfterEach
    void removeSeeds() {
        StampMoveSeeds.clean(ctx(), TENANT);
        // the next case registers the same labels again: a finished run of an earlier case would make its move look done
        for (String label : List.of(REGISTRY_LABEL, REGISTRY_LABEL + "-rerun-1", CHUNKS_LABEL)) {
            for (String run : new ArrayList<>(activeRuns(label))) {
                ctx().select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT, DSL.val(run))).fetch();
            }
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private void register(String label) {
        Map<String, Object> row = registered.get(label);
        assertThat(row).as("the walk registers the move " + label).isNotNull();
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

    private String name(String what) {
        return StampMoveSeeds.collection(TENANT, 0).replace("x0__", "x" + what.toLowerCase() + "__");
    }

    private static final Field<String> MODEL_VERSION = DSL.field(DSL.name("model_version"), String.class);

    /** A collection as a running engine registers it: with its model_version (a blank one is the legacy tombstone D2's repair move fixes). */
    private void register(String what, String modelVersion) {
        PgContainerHelper.insertCollection(ctx(), TENANT, name(what), StampMoveSeeds.BGE);
        ctx().update(CATALOG_COLLECTIONS).set(MODEL_VERSION, modelVersion)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name(what))).execute();
    }

    private Long id(String what) {
        return StampMoveSeeds.id(ctx(), TENANT, name(what));
    }

    private Long supersededId(String what) {
        return ctx().select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name(what))).fetchOne(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID);
    }

    /** The runbook's pause statement (scripts/rdr229_p31_downstep.py generate_pause), typed: every open, not yet paused stamp move is held. */
    private int pauseOpenStampMoves() {
        return ctx().update(table("nexus", "data_moves")).set(PAUSED_AT, OffsetDateTime.now()).set(PAUSED_BY_F, PAUSED_BY)
            .where(LABEL_F.like(StampMoveSpecs.LABEL_PREFIX + "%")).and(COMPLETED_AT.isNull()).and(PAUSED_AT.isNull()).execute();
    }

    /** The runbook's release (generate_unpause): only what this procedure's own pause held. */
    private int releaseThePause() {
        return ctx().update(table("nexus", "data_moves")).set(PAUSED_AT, (OffsetDateTime) null).set(PAUSED_BY_F, (String) null)
            .where(LABEL_F.like(StampMoveSpecs.LABEL_PREFIX + "%")).and(PAUSED_BY_F.eq(PAUSED_BY)).execute();
    }

    private void runDownStep() throws Exception {
        PgContainerHelper.runSuperuserTestChangelog(su, DOWN_STEP, "databasechangelog_test_p31_downstep", Map.of());
    }

    /** The generated re-add (the classpath copy of docs/runbooks/rdr-229-p31-readd.sql), in one transaction. */
    private void runReAdd() throws Exception {
        Database db = DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su));
        db.setDatabaseChangeLogTableName("databasechangelog_test_p31_readd");
        db.setDatabaseChangeLogLockTableName("databasechangelog_test_p31_readd_lock");
        var accessor = new ClassLoaderResourceAccessor();
        new Liquibase(READD, accessor, db).update(new Contexts());
        su.setAutoCommit(true);
    }

    /** {@code nexus.rdr229_restamp(table)} as the migrating role, the only role that may call it. */
    private String restampAsMigratingRole(String table) throws Exception {
        try (Connection c = adminPool.getConnection()) {
            return DSL.using(c, SQLDialect.POSTGRES).select(DSL.function(DSL.name("nexus", "rdr229_restamp"), SQLDataType.VARCHAR, DSL.val(table)))
                .fetchOne(0, String.class);
        }
    }

    private String failure(String label) {
        return ctx().select(DSL.field(DSL.name("failure"), String.class)).from(table("nexus", "data_moves")).where(LABEL_F.eq(label))
            .fetchOne(DSL.field(DSL.name("failure"), String.class));
    }

    private void cycleUntil(String label) {
        for (int i = 0; i < 20 && completedAt(label) == null; i++) {
            productionWorker().runCycle();
        }
    }

    private CompletionCheck.Result gate1ForTheRegistryLabel() {
        return CompletionCheck.evaluate(ctx(), new CompletionCheck.ReadSet(List.of(REGISTRY_LABEL), List.of()),
            CompletionCheck.Gate3Source.NOT_INSTALLED, CompletionCheck.DIRECT);
    }

    // ── the round trip ──────────────────────────────────────────────────────────────────────────────────────

    @Test
    void afterTheRegistryStampCompleted_theDownStepTheOldEnginesWritesTheReAddAndTheRestampLeaveTheRegistryStamped() throws Exception {
        register("x", "v1");
        register("y", "v1");

        // (a) the registry stamp runs to completion; the stamp move of a large table is registered after, and stays open
        register(REGISTRY_LABEL);
        assertThat(productionWorker().runCycle()).as("the registry stamp is worked").isEqualTo(1);
        assertThat(completedAt(REGISTRY_LABEL)).as("(a) the registry stamp completed").isNotNull();
        assertThat(PgCatalogProbes.constraintValidated(ctx(), SUPERSEDED_CHECK)).as("(a) its on_complete validated the CHECK").isTrue();
        assertThat(gate1ForTheRegistryLabel().gate1()).as("(a) control: gate 1 holds for the registry label before the round trip").isTrue();
        register(CHUNKS_LABEL);
        assertThat(completedAt(CHUNKS_LABEL)).as("non-vacuity: the large table's stamp is open").isNull();

        // (b) the runbook's order: pause the open stamp moves, then the down-step
        assertThat(pauseOpenStampMoves()).as("(b) the open stamp move is held, the completed one is not").isEqualTo(1);
        runDownStep();
        for (var check : StampMoveSpecs.checks()) {
            if (check.validated()) {
                assertThat(PgCatalogProbes.constraintCount(ctx(), check.constraint())).as("(b) %s is gone after the down-step", check.constraint()).isZero();
            }
        }

        // (c) the previous engine: a rename supersedes X with Y (superseded_by set, superseded_by_id NULL), and a new collection is registered
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, name("y"))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name("x"))).execute();
        register("new", "v1");
        assertThat(supersededId("x")).as("(c) non-vacuity: the old engine's tombstone has no superseded_by_id").isNull();
        assertThat(CollectionTombstones.invariantViolations(ctx(), TENANT)).as("(c) and it breaks the invariant").isEqualTo(1);

        // (d) the redeploy: re-add the CHECKs, release the pause, restamp the registry as the migrating role
        runReAdd();
        for (var check : StampMoveSpecs.checks()) {
            if (check.validated() && PgCatalogProbes.tableExists(ctx(), "nexus", check.table())) {
                assertThat(PgCatalogProbes.constraintCount(ctx(), check.constraint())).as("(d) %s is back after the re-add", check.constraint()).isPositive();
            }
        }
        assertThat(PgCatalogProbes.constraintValidated(ctx(), SUPERSEDED_CHECK)).as("(d) the re-added CHECK is NOT VALID").isFalse();
        assertThat(gate1ForTheRegistryLabel().gate1()).as("(d) while the registry stamp is complete but its CHECK is NOT VALID, gate 1 does not hold").isFalse();
        assertThat(releaseThePause()).as("(d) the release touches the one move the pause held").isEqualTo(1);
        String answer = restampAsMigratingRole("catalog_collections");
        assertThat(answer).as("(d) the restamp registers the registry's stamp move once more").isEqualTo("registered: " + REGISTRY_LABEL + "-rerun-1");
        cycleUntil(REGISTRY_LABEL + "-rerun-1");
        cycleUntil(CHUNKS_LABEL);

        // (e)
        assertThat(completedAt(REGISTRY_LABEL + "-rerun-1")).as("(e) the re-registered registry move completed").isNotNull();
        assertThat(supersededId("x")).as("(e) the tombstone takes Y's id (R7)").isEqualTo(id("y")).isNotNull();
        assertThat(id("new")).as("(e) the new collection has its collection_id").isNotNull();
        assertThat(CollectionTombstones.invariantViolations(ctx(), TENANT)).as("(e) no tombstone violates the invariant").isZero();
        for (var check : StampMoveSpecs.checks()) {
            if (check.validated() && PgCatalogProbes.tableExists(ctx(), "nexus", check.table())) {
                assertThat(PgCatalogProbes.constraintCount(ctx(), check.constraint())).as("(e) %s is present", check.constraint()).isPositive();
            }
        }
        assertThat(PgCatalogProbes.constraintValidated(ctx(), SUPERSEDED_CHECK)).as("(e) the registry's CHECK ends VALID once the restamp completes").isTrue();
        for (String constraint : StampMoveSpecs.constraintsByTable().get("chunks")) {
            assertThat(PgCatalogProbes.constraintValidated(ctx(), constraint)).as("(e) %s validated by the released stamp of its table", constraint).isTrue();
        }
        var gate = gate1ForTheRegistryLabel();
        assertThat(gate.gate1()).as("(e) gate 1 holds for the registry label").isTrue();
        assertThat(gate.labels().get(0).status()).isEqualTo(CompletionCheck.Status.COMPLETE);
    }

    /**
     * Item 4: if the previous engine's tombstone has a blank model_version, the re-add does not add the superseded_by CHECK (vectors-068-19's
     * precondition: D2's repair move would meet 23514 on that row), says so, and raises nothing; every other CHECK is added. Once D2's move has
     * repaired the row the operator runs the re-add again and the CHECK comes back NOT VALID, so the restamp can finish the registry.
     */
    @Test
    void aTombstoneWithABlankModelVersion_holdsOnlyTheSupersededByCheckBackFromTheReAdd_untilItIsRepaired() throws Exception {
        register("x", "");
        register("y", "v1");
        register(REGISTRY_LABEL);
        assertThat(productionWorker().runCycle()).isEqualTo(1);
        assertThat(completedAt(REGISTRY_LABEL)).isNotNull();
        runDownStep();
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, name("y"))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name("x"))).execute();

        runReAdd();   // must not raise

        assertThat(PgCatalogProbes.constraintCount(ctx(), SUPERSEDED_CHECK)).as("held back: D2's repair would meet 23514").isZero();
        for (var check : StampMoveSpecs.checks()) {
            if (check.validated() && !check.constraint().equals(SUPERSEDED_CHECK) && PgCatalogProbes.tableExists(ctx(), "nexus", check.table())) {
                assertThat(PgCatalogProbes.constraintCount(ctx(), check.constraint())).as("%s is added as before", check.constraint()).isPositive();
            }
        }
        // D2's repair has run: the row has a model_version, and running the re-add again adds the CHECK NOT VALID
        ctx().update(CATALOG_COLLECTIONS).set(MODEL_VERSION, "v1")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(TENANT)).and(CATALOG_COLLECTIONS.NAME.eq(name("x"))).execute();
        runReAdd();
        assertThat(PgCatalogProbes.constraintValidated(ctx(), SUPERSEDED_CHECK)).as("the second run adds it, NOT VALID").isFalse();
        assertThat(restampAsMigratingRole("catalog_collections")).isEqualTo("registered: " + REGISTRY_LABEL + "-rerun-1");
        cycleUntil(REGISTRY_LABEL + "-rerun-1");
        assertThat(completedAt(REGISTRY_LABEL + "-rerun-1")).as("the re-registered move completed (failure: %s)", failure(REGISTRY_LABEL + "-rerun-1")).isNotNull();
        assertThat(supersededId("x")).isEqualTo(id("y")).isNotNull();
        assertThat(PgCatalogProbes.constraintValidated(ctx(), SUPERSEDED_CHECK)).isTrue();
    }
}
