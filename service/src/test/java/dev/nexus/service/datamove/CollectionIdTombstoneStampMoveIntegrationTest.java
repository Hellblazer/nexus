// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.P31CheckReplay;
import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CollectionTombstones;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * RDR-229 P3.1 stage 3 (nexus-p2m1x.17), the registry's own stamp move {@code rdr229-stamp-catalog_collections} and residual R7
 * (nexus-p2m1x.46): a tombstone's {@code superseded_by_id} is the id of the live row its {@code superseded_by} names in the same tenant, and
 * its own {@code collection_id} when no such row exists ("retired, no successor"); a live row ({@code superseded_by = ''}) keeps NULL. After the
 * move {@link CollectionTombstones#invariantViolations} is zero for every tenant, which is the answer the writers keep from then on.
 *
 * <p>The registry's {@code collection_id} is filled by the changeset (BY DEFAULT identity), so this move fills only {@code superseded_by_id}.
 * Its CHECK may not exist when the move completes: the changeset that adds it waits while an unstamped tombstone could violate it (068-2, a data
 * precondition the stamp clears), so {@code on_complete} validates the CHECK where it exists and is a no-op where it does not, and the move
 * still completes (a plain VALIDATE of a missing constraint is 42704, permanent).
 */
class CollectionIdTombstoneStampMoveIntegrationTest extends DataMoveIntegrationBase {

    private static final String LABEL_TEXT = StampMoveSpecs.LABEL_PREFIX + "catalog_collections";
    private static final String CHECK = "catalog_collections_superseded_by_id_check";
    private static final Field<String> FAILURE = DSL.field(DSL.name("failure"), String.class);

    private static final String T1 = "p31-tomb-a";
    private static final String T2 = "p31-tomb-b";

    private static Map<String, Object> registered;

    @BeforeAll
    void captureRegistration() throws Exception {
        Record row = ctx().selectFrom(table("nexus", "data_moves")).where(LABEL.eq(LABEL_TEXT)).fetchOne();
        registered = row == null ? null : new LinkedHashMap<>(row.intoMap());
    }

    @AfterEach
    void removeSeeds() {
        for (String t : List.of(T1, T2)) {
            StampMoveSeeds.clean(ctx(), t);
        }
        for (String run : new ArrayList<>(activeRuns(LABEL_TEXT))) {
            ctx().select(DSL.function(DSL.name("dml_utils", "archive_migration_run"), SQLDataType.BIGINT, DSL.val(run))).fetch();
        }
        if (!PgCatalogProbes.constraintExists(ctx(), CHECK)) {
            P31CheckReplay.restore(ctx(), P31CheckReplay.COLLECTIONS_SUPERSEDED);
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    private void register(Map<String, Object> changes) {
        assertThat(registered).as("the walk registers the move " + LABEL_TEXT).isNotNull();
        Map<String, Object> row = new LinkedHashMap<>(registered);
        row.putAll(changes);
        List<Field<?>> columns = new ArrayList<>();
        List<Object> values = new ArrayList<>();
        row.forEach((k, v) -> {
            columns.add(DSL.field(DSL.name(k)));
            values.add(v);
        });
        ctx().insertInto(table("nexus", "data_moves")).columns(columns).values(values).execute();
    }

    private DataMoveWorker productionWorker(Integer sleepOverrideMs) {
        return worker(installed(), sleepOverrideMs, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC()),
            new VerifyingFinisher());
    }

    private String name(String tenant, String what) {
        return StampMoveSeeds.collection(tenant, 0).replace("x0__", "x" + what.toLowerCase() + "__");
    }

    private void live(String tenant, String what) {
        PgContainerHelper.insertCollection(ctx(), tenant, name(tenant, what), StampMoveSeeds.BGE);
    }

    /** {@code what} retired under {@code successor}, with the id left NULL as an upgrade leaves it (the CHECK is lifted by the caller). */
    private void tombstone(String tenant, String what, String successor) {
        PgContainerHelper.insertCollection(ctx(), tenant, name(tenant, what), StampMoveSeeds.BGE);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name(tenant, what))).execute();
    }

    private Long id(String tenant, String what) {
        return StampMoveSeeds.id(ctx(), tenant, name(tenant, what));
    }

    private record State(Long supersededId, String xmin) {}

    private State state(String tenant, String what) {
        Field<String> xmin = DSL.field(DSL.name("xmin")).cast(SQLDataType.VARCHAR);
        var r = ctx().select(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, xmin).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name(tenant, what))).fetchOne();
        return new State(r.value1(), r.value2());
    }

    /**
     * The shapes the census and the writers leave: a live row; a tombstone of a live successor; a dangling tombstone (the successor was
     * deleted, or a backup named one that never existed); a chain; a tombstone the dual write already stamped; and, in the other tenant, a
     * tombstone naming a row that exists only in the first tenant (a different tenant's name resolves to nothing).
     */
    private void seedShapes() {
        P31CheckReplay.lift(ctx(), P31CheckReplay.COLLECTIONS_SUPERSEDED);
        try {
            live(T1, "live");
            live(T1, "succ");
            tombstone(T1, "toLive", name(T1, "succ"));
            tombstone(T1, "dangling", name(T1, "gone"));
            tombstone(T1, "chainA", name(T1, "chainB"));
            tombstone(T1, "chainB", name(T1, "succ"));
            live(T2, "live");
            tombstone(T2, "crossTenant", name(T1, "succ"));   // names a row that exists only under T1
        } finally {
            P31CheckReplay.restore(ctx(), P31CheckReplay.COLLECTIONS_SUPERSEDED);
        }
        // as the dual write leaves a tombstone: both columns set, the CHECK enforced
        PgContainerHelper.insertCollection(ctx(), T1, name(T1, "stamped"), StampMoveSeeds.BGE);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.SUPERSEDED_BY, name(T1, "succ"))
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, id(T1, "succ"))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T1)).and(CATALOG_COLLECTIONS.NAME.eq(name(T1, "stamped"))).execute();
    }

    private long verificationRows(String tenant) {
        assertThat(registered).as("the walk registers the move " + LABEL_TEXT).isNotNull();
        String verification = registered.get("verification").toString();
        long[] n = new long[1];
        ctx().connection(c -> n[0] = MoveSql.countRows(c, verification, tenant, VerifyingFinisher.COUNT_CAP));
        return n[0];
    }

    /** The table-wide run as the worker makes it: the migrating role's own connection, no tenant (see CollectionIdStampMovesIntegrationTest). */
    private long adminTableWideRows() throws Exception {
        String verification = registered.get("verification").toString();
        try (var c = adminPool.getConnection()) {
            return MoveSql.countRows(c, verification, null, VerifyingFinisher.COUNT_CAP);
        }
    }

    // ── the R7 rule ─────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aTombstoneTakesItsLiveSuccessorsId_elseItsOwn_aLiveRowKeepsNull_andTheInvariantHoldsForEveryTenant() throws Exception {
        seedShapes();
        assertThat(CollectionTombstones.invariantViolations(ctx(), T1)).as("non-vacuity: four unstamped tombstones break the invariant").isEqualTo(4);
        assertThat(CollectionTombstones.invariantViolations(ctx(), T2)).as("and one in the other tenant").isEqualTo(1);
        assertThat(verificationRows(T1)).as("the verification looks for tombstones with no id").isEqualTo(4);
        assertThat(adminTableWideRows()).as("non-vacuity: the migrating role's table-wide run sees all five, in both tenants").isEqualTo(5);
        var before = Map.of("live", state(T1, "live"), "succ", state(T1, "succ"), "stamped", state(T1, "stamped"), "liveB", state(T2, "live"));
        register(Map.of());

        assertThat(productionWorker(0).runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL_TEXT)).as("the move completes").isNotNull();
        assertThat(state(T1, "toLive").supersededId()).as("a tombstone of a live successor takes that row's id").isEqualTo(id(T1, "succ"));
        assertThat(state(T1, "chainA").supersededId()).as("a chain resolves link by link: A names B").isEqualTo(id(T1, "chainB"));
        assertThat(state(T1, "chainB").supersededId()).as("and B names the live successor").isEqualTo(id(T1, "succ"));
        assertThat(state(T1, "dangling").supersededId()).as("R7: a tombstone whose successor is no row takes its own id, retired with no successor")
            .isEqualTo(id(T1, "dangling"));
        assertThat(state(T2, "crossTenant").supersededId()).as("a name is resolved in the tenant of the row: another tenant's row is no successor")
            .isEqualTo(id(T2, "crossTenant"));
        assertThat(state(T1, "live")).as("a live row keeps NULL and is not written").isEqualTo(before.get("live"));
        assertThat(state(T1, "succ")).isEqualTo(before.get("succ"));
        assertThat(state(T2, "live")).isEqualTo(before.get("liveB"));
        assertThat(state(T1, "stamped")).as("a tombstone the dual write stamped is not written again").isEqualTo(before.get("stamped"));
        assertThat(CollectionTombstones.invariantViolations(ctx(), T1)).as("the invariant, tenant 1").isZero();
        assertThat(CollectionTombstones.invariantViolations(ctx(), T2)).as("the invariant, tenant 2").isZero();
        assertThat(verificationRows(T1)).isZero();
        assertThat(verificationRows(T2)).isZero();
        assertThat(verificationRows(null)).as("table-wide").isZero();
        assertThat(adminTableWideRows()).as("table-wide, as the migrating role").isZero();
        assertThat(PgCatalogProbes.constraintValidated(ctx(), CHECK)).as("on_complete validated the CHECK").isTrue();
    }

    @Test
    void theMoveCompletesWhereTheCheckIsNotThereYet_whichTheChangesetThatAddsItWaitsForTheStampToClear() {
        seedShapes();
        P31CheckReplay.lift(ctx(), P31CheckReplay.COLLECTIONS_SUPERSEDED);   // 068-2 has not run: an unstamped tombstone could violate it
        assertThat(PgCatalogProbes.constraintExists(ctx(), CHECK)).as("non-vacuity: the CHECK is absent").isFalse();
        register(Map.of());

        assertThat(productionWorker(0).runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL_TEXT)).as("completed, not failed with 42704 (failure: %s)",
            ctx().select(FAILURE).from(table("nexus", "data_moves")).where(LABEL.eq(LABEL_TEXT)).fetchOne(FAILURE)).isNotNull();
        assertThat(PgCatalogProbes.constraintExists(ctx(), CHECK)).as("the move adds no constraint: that is the changeset's").isFalse();
        assertThat(CollectionTombstones.invariantViolations(ctx(), T1)).as("the stamp ran").isZero();
        assertThat(CollectionTombstones.invariantViolations(ctx(), T2)).isZero();
    }

    // ── resumable ───────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aRunKilledAfterSomeChunksResumesAtTheNextChunk_andDoesNotWriteWhatItAlreadyStamped() throws Exception {
        List<String> whats = new ArrayList<>();
        P31CheckReplay.lift(ctx(), P31CheckReplay.COLLECTIONS_SUPERSEDED);
        try {
            live(T1, "succ");
            for (int i = 0; i < 8; i++) {
                whats.add("t" + i);
                tombstone(T1, "t" + i, name(T1, "succ"));
            }
        } finally {
            P31CheckReplay.restore(ctx(), P31CheckReplay.COLLECTIONS_SUPERSEDED);
        }
        register(Map.of("chunk_size", 1));
        int baselineFree = MovePreflight.freeWorkerSlots(ctx());
        var first = worker(installed(), 300);   // the engine that dies: the runs only, as the base's kill test builds it
        var cycle = async(first::runCycle);
        long deadline = System.nanoTime() + Duration.ofMinutes(2).toNanos();
        while (whats.stream().noneMatch(w -> state(T1, w).supersededId() != null) && System.nanoTime() < deadline) {
            Thread.sleep(20);
        }
        int pid = -1;
        for (int i = 0; i < 100 && pid < 0; i++) {
            pid = callPid();
            Thread.sleep(20);
        }
        ctx().select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.val(pid))).fetch();
        cycle.get(2, TimeUnit.MINUTES);
        first.close();
        long drain = System.nanoTime() + Duration.ofMinutes(1).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) < baselineFree && System.nanoTime() < drain) {
            Thread.sleep(50);
        }
        Map<String, State> midway = new LinkedHashMap<>();
        whats.forEach(w -> midway.put(w, state(T1, w)));
        long stamped = midway.values().stream().filter(s -> s.supersededId() != null).count();
        assertThat(stamped).as("some chunks committed before the kill, and not all").isBetween(1L, 7L);
        assertThat(completedAt(LABEL_TEXT)).isNull();

        assertThat(productionWorker(0).runCycle()).isEqualTo(1);

        assertThat(completedAt(LABEL_TEXT)).isNotNull();
        for (String w : whats) {
            assertThat(state(T1, w).supersededId()).as(w).isEqualTo(id(T1, "succ"));
            if (midway.get(w).supersededId() != null) {
                assertThat(state(T1, w).xmin()).as("%s was stamped before the kill and is not written again", w).isEqualTo(midway.get(w).xmin());
            }
        }
        assertThat(CollectionTombstones.invariantViolations(ctx(), T1)).isZero();
    }
}
