// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.DmlUtilsTestFixtures;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.DmlUtilsInstaller;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;
import java.util.concurrent.TimeUnit;

/**
 * nexus-f3yqd.2.13 (RDR-246 Step 4, The move worker steps 1 and 2): the acceptance scenarios on real dml_utils runs. The
 * worker's other behaviour (row-security modes, pause and supersede, stop, orphans, the failure and finisher seams) is
 * {@link DataMoveWorkerBehaviourIntegrationTest}.
 *
 * <p>Out of scope here, with their own beads: the nexus.chunks four-column key (nexus-f3yqd.2.21, dml_utils PR #3),
 * verification and {@code on_complete} (.14), the failure classes, backoff and pause (.15).
 */
class DataMoveWorkerIntegrationTest extends DataMoveIntegrationBase {

    // ── scenario 1: (text, text, bytea) key, 100k rows ───────────────────────────────────────────────────────

    @Test
    void hundredThousandRows_everyRowOnce_doneNeverFallsAndEqualsTotal_andChunksCommitWhileTheCallRuns()
            throws Exception {
        register("m-plain", "plain_rows", 5_000, 20);
        var cycle = async(worker(installed(), null)::runCycle);
        var samples = sampleUntilDone(cycle, "m-plain");

        assertDoneNeverFalls(samples);
        // the procedure's own COMMITs: another session reads chunks as done while the CALL, one statement on a connection
        // that never commits for it, is still running. A CALL held in one transaction shows none until it returns.
        assertThat(samples.stream().anyMatch(s -> s.done() > 0 && s.done() < s.total()))
            .as("chunks seen committed mid-CALL").isTrue();
        var end = progress("m-plain");
        assertThat(end.total()).isEqualTo(20);
        assertThat(end.done()).isEqualTo(end.total());
        assertThat(activeRuns("m-plain")).containsExactly("m-plain");
        assertThat(partitionWiseRuns("m-plain")).isZero();
        assertEveryRowMovedOnce("plain_rows", 100_000);
    }

    // ── the engine is killed after some chunks commit ────────────────────────────────────────────────────────

    @Test
    void killedMidMove_nextBootResumesAtTheNextChunk_andEndsAsAnUninterruptedRun() throws Exception {
        register("m-kill", "plain_rows", 2_000, 100);
        int baselineFree = MovePreflight.freeWorkerSlots(ctx());
        var first = worker(installed(), null);
        var cycle = async(first::runCycle);
        awaitProgress("m-kill", 8);
        int pid = -1;
        for (int i = 0; i < 100 && pid < 0; i++) {
            pid = callPid();
            Thread.sleep(20);
        }
        // the engine dies: its coordinator backend goes with the connection
        ctx().select(DSL.function("pg_terminate_backend", SQLDataType.BOOLEAN, DSL.val(pid))).fetch();
        cycle.get(2, TimeUnit.MINUTES);
        first.close();
        assertThat(first.states().state("m-kill").orElseThrow().kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        // a chunk worker already launched finishes its chunk; the next boot starts after the cluster settled
        long drain = System.nanoTime() + Duration.ofMinutes(1).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) < baselineFree && System.nanoTime() < drain) {
            Thread.sleep(50);
        }
        var before = progress("m-kill");
        assertThat(before.done()).isBetween(1L, before.total() - 1);

        var second = worker(installed(), 5);   // the next boot, with the environment's throttle
        assertThat(second.runCycle()).isEqualTo(1);

        var after = progress("m-kill");
        assertThat(after.done()).isGreaterThanOrEqualTo(before.done()).isEqualTo(after.total());
        assertThat(after.total()).isEqualTo(before.total());
        assertThat(DataMoveRuns.activeRun(ctx(), "m-kill").sleepMs()).as("NX_DATA_MOVE_SLEEP_MS applied to the run").isEqualTo(5);
        assertEveryRowMovedOnce("plain_rows", 100_000);
    }

    // ── tenants under FORCE RLS; a tenant with rows and no token; a tenant id with a slash ──────────────────

    @Test
    void forceRls_oneRunPerTenant_noCrossTenantTouch_aTenantWithoutATokenIsMoved_andASlashInATenantIsKept()
            throws Exception {
        PgContainerHelper.seedServiceToken(ctx(), "tok-acme", "acme", "t");
        PgContainerHelper.seedServiceToken(ctx(), "tok-globex", "globex", "t");
        register("m-rls", "tenant_rows", 50, 0);

        assertThat(MoveTenants.hasTenantColumn(ctx(), "dm_test", "tenant_rows")).isTrue();
        assertThat(MoveTenants.byProbe(ctx(), "dm_test", "tenant_rows"))
            .as("the default tenant, then every other: tokens and the probe")
            .containsExactly("default", "acme", "globex", "orphan", "sl/ash");
        assertThat(ctx().fetchCount(table("nexus", "service_tokens"), TENANT_ID.eq("orphan"))).isZero();

        assertThat(worker(installed(), null).runCycle()).isEqualTo(1);

        assertThat(activeRuns("m-rls")).containsExactly("m-rls/acme", "m-rls/default", "m-rls/globex", "m-rls/orphan",
            "m-rls/sl/ash");
        assertEveryRowMovedOnce("tenant_rows", 1000);
        assertEachRowTouchedByItsOwnTenant("tenant_rows");
        // dml_utils_owner's boundary scan runs under the tenant's setting too: 200 rows of one tenant is four chunks of 50
        for (String run : activeRuns("m-rls")) {
            assertThat(DataMoveRuns.activeRun(ctx(), run).total()).as(run).isEqualTo(4);
        }
        var end = progress("m-rls");
        assertThat(end.done()).isEqualTo(end.total()).isEqualTo(20);
    }

    // ── a list-partitioned (model, tenant) table: one run per leaf of the tenant ─────────────────────────────

    @Test
    void listPartitioned_oneRunPerLeaf_neverPartitionWise_andNoSessionNamesAnotherTenantsLeaf() throws Exception {
        register("m-leaf", "leafed", 50, 0);

        assertThat(LeafCatalog.hasTenantLeaves(ctx(), "dm_test", "leafed")).isTrue();
        assertThat(LeafCatalog.hasTenantLeaves(ctx(), "dm_test", "tenant_rows")).isFalse();
        assertThat(LeafCatalog.tenantLeaves(ctx(), "dm_test", "leafed", "acme")).extracting(l -> l.leaf().name())
            .as("the leaf list is per tenant").containsExactly("leafed_m1_acme", "leafed_m2_acme");
        assertThat(LeafCatalog.tenantLeaves(ctx(), "dm_test", "leafed", null)).hasSize(6);

        assertThat(worker(installed(), null).runCycle()).isEqualTo(1);

        TreeSet<String> expected = new TreeSet<>();
        for (String tenant : List.of("default", "acme", "globex")) {
            for (String model : List.of("m1", "m2")) {
                expected.add("m-leaf/" + tenant + "/leafed_" + model + "_" + tenant);
            }
        }
        assertThat(activeRuns("m-leaf")).isEqualTo(expected);
        assertThat(partitionWiseRuns("m-leaf")).as("no run is partition-wise").isZero();
        // every run's driving table is a leaf whose bound is the tenant in the run's name, never another tenant's
        for (String run : expected) {
            String tenant = run.split("/")[1];
            String drivingTable = ctx().select(DSL.field(DSL.name("driving_table_name"), String.class))
                .from(table("dml_utils_data", "migration_run"))
                .where(DSL.field(DSL.name("label"), String.class).eq(run))
                .fetchOne(0, String.class);
            assertThat(LeafCatalog.tenantLeaves(ctx(), "dm_test", "leafed", tenant)).extracting(l -> l.leaf().name())
                .as("the leaf of %s", run).contains(drivingTable);
            assertThat(DataMoveRuns.activeRun(ctx(), run).total()).as(run).isEqualTo(3);
        }
        assertEveryRowMovedOnce("leafed", 900);
        assertEachRowTouchedByItsOwnTenant("leafed");
    }

    @Test
    void aLeafDroppedMidMove_hasItsActiveRunArchived_andTheMoveIsNotFailed() throws Exception {
        register("m-gone", "leafed", 50, 0);
        assertThat(worker(installed(), null).runCycle()).isEqualTo(1);
        assertThat(activeRuns("m-gone")).contains("m-gone/globex/leafed_m2_globex");
        var before = progress("m-gone");

        DataMoveTestFixtures.dropGlobexLeaf(su);
        assertThat(worker(installed(), null).runCycle()).as("the move is still open and is worked").isEqualTo(1);

        assertThat(activeRuns("m-gone")).hasSize(5).doesNotContain("m-gone/globex/leafed_m2_globex");
        assertThat(ctx().fetchCount(table("dml_utils_data", "migration_run"),
            DSL.field(DSL.name("label"), String.class).eq("m-gone/globex/leafed_m2_globex")
                .and(DSL.field(DSL.name("archived_at")).isNotNull()))).isEqualTo(1);
        var after = progress("m-gone");
        assertThat(after.total()).isEqualTo(before.total() - 3);
        assertThat(after.done()).isEqualTo(after.total());
        assertThat(ctx().fetchCount(table("nexus", "data_moves"),
            DSL.field(DSL.name("failed_at")).isNotNull())).isZero();
    }

    // ── bloat, end to end ────────────────────────────────────────────────────────────────────────────────────

    @Test
    void bloat_vacuumMidMoveReclaimsChunksAlreadyCommitted_andTheCoordinatorHoldsNoSnapshotAcrossChunks()
            throws Exception {
        register("m-bloat", "plain_rows", 1_000, 30);
        long initial = relationSize("dm_test.plain_rows");
        var cycle = async(worker(installed(), null)::runCycle);
        List<Integer> xminAges = new ArrayList<>();
        int samples = 0;
        boolean vacuumedAtThird = false;
        boolean vacuumedAtTwoThirds = false;
        long deadline = System.nanoTime() + Duration.ofMinutes(5).toNanos();
        while (!cycle.isDone() && System.nanoTime() < deadline) {
            long done = progress("m-bloat").done();
            if (done >= 33 && !vacuumedAtThird) {
                DataMoveTestFixtures.vacuumPlainRows(su);
                vacuumedAtThird = true;
            } else if (done >= 66 && !vacuumedAtTwoThirds) {
                DataMoveTestFixtures.vacuumPlainRows(su);
                vacuumedAtTwoThirds = true;
            }
            int pid = callPid();
            if (pid > 0) {
                var age = ctx().select(DSL.function("age", Integer.class, DSL.field(DSL.name("backend_xmin"))))
                    .from(table("pg_catalog", "pg_stat_activity"))
                    .where(DSL.field(DSL.name("pid"), Integer.class).eq(pid)).fetchOne(0, Integer.class);
                samples++;
                if (age != null) {
                    xminAges.add(age);
                }
            }
            Thread.sleep(5);
        }
        cycle.get();
        assertThat(vacuumedAtThird && vacuumedAtTwoThirds).as("both vacuums ran mid-move").isTrue();
        assertThat(progress("m-bloat").done()).isEqualTo(100);
        // A coordinator holding one snapshot across the move would show a backend_xmin older and older, by every
        // transaction the 100 chunk workers commit. Sampled through the run, it stays within a few transactions of now (a
        // null is the best case and is not counted against it).
        assertThat(samples).as("samples of the CALL backend").isGreaterThanOrEqualTo(20);
        assertThat(xminAges).as("age of the coordinator's backend_xmin, in transactions").allMatch(a -> a <= 40);
        long end = relationSize("dm_test.plain_rows");
        // each of the 100000 rows is rewritten once. A coordinator holding one snapshot across the move keeps every dead
        // version "not yet removable", so the heap doubles; vacuums that reclaim committed chunks keep it well below that
        assertThat(end).as("heap after %d initial bytes", initial).isLessThan(initial * 175 / 100);
        assertEveryRowMovedOnce("plain_rows", 100_000);
    }

    // ── installs and slots ───────────────────────────────────────────────────────────────────────────────────

    @Test
    void aMoveRegisteredBeforeTheLibraryRolesExist_isBlocked_thenGrantedAndRunAfterTheRolesAndARestart()
            throws Exception {
        DataMoveTestFixtures.dropLibraryRoles(su);
        var beforeRoles = installed();
        assertThat(beforeRoles.status().state()).isEqualTo(DmlUtilsInstaller.State.SKIPPED);
        register("m-late", "leafed", 50, 0);

        var early = worker(beforeRoles, null);
        assertThat(early.runCycle()).isEqualTo(1);
        var blocked = early.states().state("m-late").orElseThrow();
        assertThat(blocked.kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(blocked.reason()).contains("dml_utils_owner").contains("missing");
        assertThat(countWhere("leafed", N.ne(0))).as("nothing moved").isZero();

        DmlUtilsTestFixtures.bootstrapDmlUtils(su);   // the cloud's bootstrap lands, then the engine restarts
        var restarted = installed();
        assertThat(restarted.status().applied()).isTrue();
        assertThat(LeafCatalog.relationsLackingSelect(ctx(), "dm_test", "leafed", DmlUtilsInstaller.OWNER_ROLE))
            .as("the roles came after the registering release: nothing is granted yet").isNotEmpty();

        assertThat(worker(restarted, null).runCycle()).isEqualTo(1);

        assertThat(LeafCatalog.relationsLackingSelect(ctx(), "dm_test", "leafed", DmlUtilsInstaller.OWNER_ROLE))
            .as("dml_utils_owner reads the parent and every leaf").isEmpty();
        assertEveryRowMovedOnce("leafed", 900);
    }

    @Test
    void noFreeBackgroundWorkerSlot_isBlockedNotFailed_andRunsOnceASlotFrees() throws Exception {
        register("m-slot", "tenant_rows", 50, 0);
        DataMoveTestFixtures.holdWorkerSlots(su);
        long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) > 0 && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(MovePreflight.freeWorkerSlots(ctx())).as("the test holds every slot").isZero();

        var w = worker(installed(), null);
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(w.states().state("m-slot").orElseThrow().reason()).isEqualTo("waiting for a background worker slot");
        assertThat(countWhere("tenant_rows", N.ne(0))).isZero();
        assertThat(ctx().fetchCount(table("nexus", "data_moves"), DSL.field(DSL.name("failed_at")).isNotNull())).isZero();

        DataMoveTestFixtures.releaseWorkerSlots(su);
        long freed = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) < 1 && System.nanoTime() < freed) {
            Thread.sleep(50);
        }
        Thread.sleep(50);   // the blocked move waits out its retry (10 ms in this test)
        assertThat(w.runCycle()).isEqualTo(1);
        assertEveryRowMovedOnce("tenant_rows", 1000);
    }
}
