// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.time.Duration;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * nexus-f3yqd.2.13: how the worker decides and what it keeps, on the pg_background image (the acceptance scenarios are
 * {@link DataMoveWorkerIntegrationTest}): which tables get a run per tenant, the connection's tenant setting between
 * moves, a pause and a superseding row set while another move runs, stop, orphans of a killed engine, and the failure and
 * finisher seams the later beads fill in.
 */
class DataMoveWorkerBehaviourIntegrationTest extends DataMoveIntegrationBase {

    private static String reasonOf(DataMoveWorker w, String label) {
        return w.states().state(label).orElseThrow().reason();
    }

    // ── row security decides the mode ────────────────────────────────────────────────────────────────────────

    @Test
    void aTenantColumnWithRowSecurityOff_getsOneRun_notOnePerTenant() throws Exception {
        register("b-plain", "tenant_plain", 20, 0);

        assertThat(MoveTenants.hasTenantColumn(ctx(), "dm_test", "tenant_plain")).isTrue();
        assertThat(worker(installed(), null).runCycle()).isEqualTo(1);

        assertThat(activeRuns("b-plain")).as("a run per tenant would process every row three times")
            .containsExactly("b-plain");
        assertEveryRowMovedOnce("tenant_plain", 150);
    }

    @Test
    void whetherRowSecurityHidesRowsFromTheAdmin_decidesBlockedOrProceed() throws Exception {
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();
        FailureHandler capture = f -> {
            seen.add(f);
            return FailureHandler.BLOCK_ALWAYS.onFailure(f);
        };
        register("b-forced", "forced_noread", 20, 0);
        register("b-foreign", "unforced_foreign", 20, 0);
        register("b-owned", "unforced_owned", 20, 0);

        var w = worker(installed(), null, capture, MoveFinisher.RUNS_ONLY);
        assertThat(w.runCycle()).isEqualTo(3);

        // FORCE and no policy for the migrating role, and row security on but not forced and owned by someone else:
        // the admin session reads no tenant's rows, so the table-wide probe would report an empty table as clean
        assertThat(reasonOf(w, "b-forced")).contains("does not read every tenant's rows");
        assertThat(reasonOf(w, "b-foreign")).contains("does not read every tenant's rows");
        assertThat(seen).extracting(FailureHandler.Failure::stage).containsOnly(FailureHandler.Stage.PREFLIGHT);
        assertThat(countWhere("forced_noread", N.ne(0))).isZero();
        assertThat(countWhere("unforced_foreign", N.ne(0))).isZero();
        // not forced and owned: the admin is exempt and proceeds, but dml_utils_owner's boundary scan is not, so the move still
        // runs once per tenant (run once, it would cut no chunk and finish with every row untouched)
        assertThat(w.states().state("b-owned").orElseThrow().kind()).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertThat(activeRuns("b-owned")).containsExactly("b-owned/acme", "b-owned/default", "b-owned/globex");
        assertEveryRowMovedOnce("unforced_owned", 40);
        assertEachRowTouchedByItsOwnTenant("unforced_owned");
    }

    @Test
    void aTenantSettingNeverOutlivesItsRun_soTheNextMovesRunCarriesNone() throws Exception {
        register("b-first", "tenant_rows", 50, 0);
        register("b-second", "plain_rows", 50_000, 0);

        assertThat(worker(installed(), null).runCycle()).isEqualTo(2);

        assertEveryRowMovedOnce("plain_rows", 100_000);
        // plain_rows ran on the connection tenant_rows' last tenant used: its rows carry that setting unless it was reset
        assertThat(countWhere("plain_rows", TOUCHED_BY.isNotNull().and(TOUCHED_BY.ne("")))).isZero();
    }

    // ── stop ─────────────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void stop_cancelsTheCallInFlight_andTheThreadEnds() throws Exception {
        register("b-stop", "plain_rows", 1_000, 100);
        var w = worker(installed(), null);
        w.start();
        awaitProgress("b-stop", 3);
        assertThat(callPid()).as("the CALL is in flight").isPositive();

        w.stop();

        assertThat(w.awaitStopped(Duration.ofSeconds(30))).as("the worker thread ended").isTrue();
        long gone = System.nanoTime() + Duration.ofSeconds(15).toNanos();
        while (callPid() > 0 && System.nanoTime() < gone) {
            Thread.sleep(50);
        }
        assertThat(callPid()).as("the CALL's backend is gone").isEqualTo(-1);
        var stopped = progress("b-stop");
        assertThat(stopped.done()).isLessThan(stopped.total());
        Thread.sleep(500);
        assertThat(progress("b-stop").done()).as("no chunk is launched after the stop").isLessThanOrEqualTo(stopped.done() + 1);
    }

    // ── a pause and a supersede set while the worker is busy ──────────────────────────────────────────────────

    @Test
    void aPauseSetWhileARunIsInFlight_startsNoFurtherRun_andClearingItResumes() throws Exception {
        register("b-pause", "tenant_rows", 10, 40);
        var w = worker(installed(), null);
        var cycle = async(w::runCycle);
        long deadline = System.nanoTime() + Duration.ofSeconds(60).toNanos();
        while (activeRuns("b-pause").isEmpty() && System.nanoTime() < deadline) {
            Thread.sleep(10);
        }
        DataMoveTestFixtures.pause(ctx(), "b-pause", true);
        cycle.get();

        var paused = w.states().state("b-pause").orElseThrow();
        assertThat(paused.kind()).isEqualTo(MoveStates.Kind.PAUSED);
        assertThat(paused.pausedBy()).isEqualTo("test");
        assertThat(activeRuns("b-pause")).as("a run in flight ends; no tenant after it starts").hasSizeLessThan(5);

        DataMoveTestFixtures.pause(ctx(), "b-pause", false);
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(activeRuns("b-pause")).hasSize(5);
        assertEveryRowMovedOnce("tenant_rows", 1000);
    }

    @Test
    void aPausedMove_isForgottenByTheFinisher_soWhatItKeptPerMoveDoesNotOutliveThePause() {
        register("b-forget", "plain_rows", 10, 0);
        DataMoveTestFixtures.pause(ctx(), "b-forget", true);
        List<String> forgotten = new CopyOnWriteArrayList<>();
        MoveFinisher recording = new MoveFinisher() {
            @Override
            public Result finish(Context context) {
                return Result.leftOpen();
            }

            @Override
            public void forget(String label) {
                forgotten.add(label);
            }
        };

        worker(installed(), null, FailureHandler.BLOCK_ALWAYS, recording).runCycle();

        assertThat(forgotten).as("the worker skipped finish() for the paused move and told the finisher").contains("b-forget");
    }

    @Test
    void aSupersedeInsertedWhileAnotherMoveRuns_isSeenAtTheSupersededMovesTurn() throws Exception {
        register("b-a", "plain_rows", 2_000, 100);
        register("b-b", "tenant_rows", 50, 0);
        var w = worker(installed(), null);
        var cycle = async(w::runCycle);
        awaitProgress("b-a", 2);
        DataMoveTestFixtures.register(ctx(), "b-c", "tenant_rows", 50, 0, "b-b", "stamp-template.sql");
        assertThat(cycle.get()).isEqualTo(2);

        assertThat(countWhere("tenant_rows", N.ne(0))).as("b-b was replaced before its turn: nothing of it ran").isZero();
        assertThat(w.states().state("b-b")).isEmpty();
        assertThat(w.runCycle()).as("b-c is new to this cycle and runs").isEqualTo(1);
        assertEveryRowMovedOnce("tenant_rows", 1000);
        assertThat(activeRuns("b-b")).isEmpty();
    }

    // ── what the state keeps ──────────────────────────────────────────────────────────────────────────────────

    @Test
    void aBlockedMoveKeepsTheTimeItFirstWasBlocked_acrossRetries() throws Exception {
        register("b-since", "tenant_rows", 50, 0);
        DataMoveTestFixtures.holdWorkerSlots(su);
        long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) > 0 && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        var w = worker(installed(), null);
        assertThat(w.runCycle()).isEqualTo(1);
        var first = w.states().state("b-since").orElseThrow();
        Thread.sleep(30);   // past the retry
        assertThat(w.runCycle()).isEqualTo(1);
        var second = w.states().state("b-since").orElseThrow();

        assertThat(second.reason()).isEqualTo(first.reason());
        // the retry itself was working (nexus-f3yqd.2.17), so `since` restarted with it; the first blocked time is what stays
        assertThat(first.firstBlockedAt()).isNotNull();
        assertThat(second.firstBlockedAt()).as("blocked since").isEqualTo(first.firstBlockedAt());
        assertThat(second.updatedAt()).isAfter(first.updatedAt());
    }

    @Test
    void theRegistrySessionIsKeptBetweenCycles_andClosedWithTheWorker() throws Exception {
        var w = worker(installed(), null);
        for (int i = 0; i < 3; i++) {
            assertThat(w.runCycle()).isZero();
        }
        assertThat(sessionsNamed(WORKER_NAME)).as("one admin session for the registry, not one per cycle").isEqualTo(1);
        w.close();
        long gone = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (sessionsNamed(WORKER_NAME) > 0 && System.nanoTime() < gone) {
            Thread.sleep(50);
        }
        assertThat(sessionsNamed(WORKER_NAME)).isZero();
    }

    // ── orphans of a killed engine ────────────────────────────────────────────────────────────────────────────

    @Test
    void startEndsADataMoveBackendOfAnotherBoot_andLeavesItsOwn() throws Exception {
        Properties other = new Properties();
        other.setProperty("user", ADMIN);
        other.setProperty("password", ADMIN_PASSWORD);
        other.setProperty("ApplicationName", "nexus-data-move-oldnonce-call");
        Properties own = new Properties();
        own.setProperty("user", ADMIN);
        own.setProperty("password", ADMIN_PASSWORD);
        own.setProperty("ApplicationName", CALL_APPLICATION_NAME);
        try (Connection orphan = DriverManager.getConnection(pg.getJdbcUrl(), other);
             Connection mine = DriverManager.getConnection(pg.getJdbcUrl(), own)) {
            assertThat(sessionsNamed("nexus-data-move-oldnonce-call")).isEqualTo(1);

            var w = worker(installed(), null);
            assertThat(w.terminateOrphans()).isEqualTo(1);

            long gone = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (sessionsNamed("nexus-data-move-oldnonce-call") > 0 && System.nanoTime() < gone) {
                Thread.sleep(50);
            }
            assertThat(sessionsNamed("nexus-data-move-oldnonce-call")).as("the other boot's backend").isZero();
            assertThat(sessionsNamed(CALL_APPLICATION_NAME)).as("this worker's own").isEqualTo(1);
            assertThat(mine.isClosed()).isFalse();
        }
    }

    @Test
    void withoutTheSingletonLock_noBackendOfAnotherBootIsEnded() throws Exception {
        Properties other = new Properties();
        other.setProperty("user", ADMIN);
        other.setProperty("password", ADMIN_PASSWORD);
        other.setProperty("ApplicationName", "nexus-data-move-livenonce-call");
        try (Connection live = DriverManager.getConnection(pg.getJdbcUrl(), other)) {
            // an engine that runs without the lock (the operator opted out, or a pooler): the other nonce may be a live engine
            var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, MoveFinisher.RUNS_ONLY, false);
            assertThat(w.terminateOrphans()).isZero();
            Thread.sleep(300);
            assertThat(sessionsNamed("nexus-data-move-livenonce-call")).isEqualTo(1);
            assertThat(live.isValid(5)).isTrue();
        }
    }

    @Test
    void aStopBetweenOpeningTheConnectionAndPublishingIt_startsNoCall() throws Exception {
        register("b-race", "plain_rows", 5_000, 0);
        var w = worker(installed(), null);
        w.afterConnectionOpenedHook = w::stop;   // stop() finds no connection yet to cancel

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(activeRuns("b-race")).as("the CALL never started: no run was created").isEmpty();
        assertThat(countWhere("plain_rows", N.ne(0))).isZero();
        assertThat(reasonOf(w, "b-race")).contains("stopped");
        assertThat(callPid()).isEqualTo(-1);
    }

    // ── the seams the later beads fill in ─────────────────────────────────────────────────────────────────────

    @Test
    void aFailedDisposition_keepsTheMoveFailedUntilASupersedingMoveReplacesIt() throws Exception {
        var seen = new CopyOnWriteArrayList<FailureHandler.Failure>();
        FailureHandler failing = f -> {
            seen.add(f);
            return FailureHandler.Disposition.failed("the handler's verdict");
        };
        DataMoveTestFixtures.register(ctx(), "b-bad", "leafed", 50, 0, null, "no-clause-template.sql");
        var w = worker(installed(), null, failing, MoveFinisher.RUNS_ONLY);

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(seen).hasSize(1);
        var f = seen.get(0);
        assertThat(f.stage()).isEqualTo(FailureHandler.Stage.CALL);
        assertThat(f.runLabel()).isEqualTo("b-bad/default/leafed_m1_default");
        assertThat(f.drivingSchema()).isEqualTo("dm_test");
        assertThat(f.drivingTable()).as("the leaf the CALL named").isEqualTo("leafed_m1_default");
        assertThat(f.tenant()).isEqualTo("default");
        assertThat(f.raised().getSQLState()).as("a template without its chunking clause").isEqualTo("22023");
        assertThat(f.errorMark()).isZero();
        assertThat(f.admin()).isNotNull();
        var state = w.states().state("b-bad").orElseThrow();
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.FAILED);
        assertThat(state.reason()).isEqualTo("the handler's verdict");

        assertThat(w.runCycle()).as("a failed move is not worked again by this process").isZero();
        assertThat(seen).hasSize(1);
        assertThat(w.states().state("b-bad").orElseThrow().kind()).isEqualTo(MoveStates.Kind.FAILED);

        DataMoveTestFixtures.register(ctx(), "b-fix", "leafed", 50, 0, "b-bad", "stamp-template.sql");
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(w.states().state("b-bad")).as("replaced, so no longer reported").isEmpty();
        assertEveryRowMovedOnce("leafed", 900);
    }

    @Test
    void theFinisherIsGivenTheTenantsAndTheAdminValues_andItsOutcomeIsReported() throws Exception {
        var seen = new CopyOnWriteArrayList<MoveFinisher.Context>();
        MoveFinisher finisher = c -> {
            seen.add(c);
            return MoveFinisher.Result.failed("verification found old-shape rows");
        };
        register("b-fin", "tenant_rows", 50, 0);
        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, finisher);

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(seen).hasSize(1);
        var c = seen.get(0);
        assertThat(c.tenants()).containsExactly("default", "acme", "globex", "orphan", "sl/ash");
        assertThat(c.adminConnection()).isEqualTo(adminConnection);
        assertThat(c.admin()).isNotNull();
        var state = w.states().state("b-fin").orElseThrow();
        assertThat(state.kind()).isEqualTo(MoveStates.Kind.FAILED);
        assertThat(state.reason()).isEqualTo("verification found old-shape rows");
    }

    @Test
    void aSurpriseInOneMovesPass_isThatMovesFailure_andTheNextMoveStillRuns() throws Exception {
        MoveFinisher boom = c -> {
            if (c.move().label().equals("b-boom")) {
                throw new IllegalStateException("surprise");
            }
            return MoveFinisher.Result.leftOpen();
        };
        register("b-boom", "tenant_plain", 50, 0);
        register("b-after", "unforced_owned", 20, 0);
        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, boom);

        assertThat(w.runCycle()).isEqualTo(2);

        assertThat(reasonOf(w, "b-boom")).contains("the pass could not run").contains("surprise");
        assertThat(w.states().state("b-after").orElseThrow().kind()).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertEveryRowMovedOnce("unforced_owned", 40);
    }

    @Test
    void theProbeOfATableWithoutALeafMode_isBoundedAndFindsTheTenantsOfTheRows() throws Exception {
        PgContainerHelper.seedServiceToken(ctx(), "tok-only", "only-a-token", "t");
        try (var session = adminSession("tenant_plain")) {
            assertThat(MoveTenants.byProbe(session, "dm_test", "tenant_plain"))
                .containsExactly("default", "acme", "globex", "only-a-token");
            assertThat(MoveTenants.byLeafBounds(session.ctx(), List.of("acme", "globex")))
                .as("leaf bounds, no scan: bounds, the tokens and the default")
                .containsExactly("default", "acme", "globex", "only-a-token");
            assertThat(session.ctx().select(DSL.function("current_setting", String.class, DSL.val("statement_timeout")))
                .fetchOne(0, String.class)).as("the bound does not outlive the probe")
                .isNotEqualTo(MoveTenants.PROBE_STATEMENT_TIMEOUT);
        }
    }
}
