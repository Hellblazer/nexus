// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.AdminDdlSession;
import org.jooq.Record;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;

import java.sql.SQLException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;

/**
 * nexus-f3yqd.2.15 (RDR-246 Step 4, the failure classes) on real dml_utils runs: a recorded chunk error classified by its
 * SQLSTATE ({@code dm_test.maybe_fail} raises a chosen one from inside a chunk), the registry columns the classes write, the
 * backoff read from the row on a clock the test moves, and a failed move that nothing retries. The table of every SQLSTATE
 * is {@link DataMoveFailureClassifierTest}.
 */
class DataMoveFailureIntegrationTest extends DataMoveIntegrationBase {

    private static final String FAULT_TEMPLATE = "fault-template.sql";
    /** dml_utils records a chunk's error through an asynchronous background run: wait generously for the row. */
    private static final Duration RECORDER_WAIT = Duration.ofSeconds(3);

    /** A clock the test moves; the worker, its states and the failure handler all read it. */
    static final class MutableClock extends Clock {
        private volatile Instant now = Instant.now().truncatedTo(ChronoUnit.SECONDS);

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }

    private DataMoveWorker workerOn(MutableClock clock, MoveFinisher finisher) throws Exception {
        return worker(installed(), null, new DataMoveFailureHandler(adminConnection, WORKER_NAME, clock, RECORDER_WAIT),
            finisher, true, clock);
    }

    private DataMoveWorker workerOn(MutableClock clock) throws Exception {
        return workerOn(clock, MoveFinisher.RUNS_ONLY);
    }

    /** A move over {@code tenant_plain} (one run, 150 rows, key (tenant_id, id)) whose template raises the injected fault. */
    private void registerFaulty(String label, int chunkSize) {
        DataMoveTestFixtures.register(ctx(), label, "tenant_plain", chunkSize, 0, null, FAULT_TEMPLATE);
    }

    private void fault(String sqlstate, long fromId) {
        clearFault();
        ctx().insertInto(table("dm_test", "fault"))
            .columns(DSL.field(DSL.name("sqlstate"), String.class), DSL.field(DSL.name("from_id"), Long.class))
            .values(sqlstate, fromId).execute();
    }

    private void clearFault() {
        ctx().deleteFrom(table("dm_test", "fault")).execute();
    }

    private Record row(String label) {
        return ctx().select(DSL.field(DSL.name("failed_at"), OffsetDateTime.class),
                DSL.field(DSL.name("failure"), String.class), DSL.field(DSL.name("transient_retries"), Integer.class),
                DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class),
                DSL.field(DSL.name("last_transient"), String.class), DSL.field(DSL.name("completed_at"), OffsetDateTime.class))
            .from(table("nexus", "data_moves")).where(DSL.field(DSL.name("label"), String.class).eq(label)).fetchOne();
    }

    private static OffsetDateTime failedAt(Record r) {
        return r.get(0, OffsetDateTime.class);
    }

    private static String failure(Record r) {
        return r.get(1, String.class);
    }

    private static int retries(Record r) {
        return r.get(2, Integer.class);
    }

    private static OffsetDateTime lastAt(Record r) {
        return r.get(3, OffsetDateTime.class);
    }

    private static String last(Record r) {
        return r.get(4, String.class);
    }

    private static String reasonOf(DataMoveWorker w, String label) {
        return w.states().state(label).orElseThrow().reason();
    }

    private static MoveStates.Kind kindOf(DataMoveWorker w, String label) {
        return w.states().state(label).orElseThrow().kind();
    }

    // ── a chunk fails ───────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aChunkFails_theMoveIsFailedWithTheError_committedChunksStay_andNothingRetriesIt() throws Exception {
        var clock = new MutableClock();
        registerFaulty("f-bad", 20);
        register("f-next", "unforced_owned", 20, 0);   // registered behind the failing move
        fault("22012", 45);                              // from id 45: the chunks before it commit, the one holding it fails
        var w = workerOn(clock);

        assertThat(w.runCycle()).as("the failing move and the one behind it are both worked").isEqualTo(2);

        assertThat(kindOf(w, "f-bad")).isEqualTo(MoveStates.Kind.FAILED);
        assertThat(reasonOf(w, "f-bad")).contains("22012").contains("injected fault");
        Record r = row("f-bad");
        assertThat(failedAt(r)).as("failed_at is set").isNotNull();
        assertThat(failure(r)).contains("22012");
        assertThat(retries(r)).as("a permanent failure counts no retry").isZero();
        int committed = countWhere("tenant_plain", N.eq(1));
        assertThat(committed).as("the chunks before the failing one stay committed").isBetween(1, 149);

        assertThat(kindOf(w, "f-next")).as("a failed move does not hold up the moves behind it")
            .isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertEveryRowMovedOnce("unforced_owned", 40);

        clock.advance(Duration.ofHours(2));
        assertThat(w.runCycle()).as("the next pass retries nothing: the failed move is final, the other is runs-complete")
            .isZero();
        var later = workerOn(clock);   // a later boot: no memory of the first
        assertThat(later.runCycle()).as("only the open move is looked at").isEqualTo(1);
        assertThat(countWhere("tenant_plain", N.eq(1))).as("the failed move was not run again").isEqualTo(committed);
        assertThat(failedAt(row("f-bad"))).isEqualTo(failedAt(r));
        assertThat(DataMoveRegistry.reload(ctx(), "f-bad")).as("a failed move is not open").isEmpty();

        // a superseding move closes it, for the worker that reported it failed
        assertThat(w.states().state("f-bad")).as("this worker saw the move fail").isPresent();
        clearFault();
        DataMoveTestFixtures.register(ctx(), "f-fix", "tenant_plain", 20, 0, "f-bad", "stamp-template.sql");
        assertThat(w.runCycle()).as("the superseding move runs").isEqualTo(1);
        assertThat(kindOf(w, "f-fix")).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertThat(w.states().state("f-bad")).as("replaced, so no longer reported").isEmpty();
    }

    // ── a transient chunk error ─────────────────────────────────────────────────────────────────────────────

    @Test
    void aTransientChunkError_blocksWithTheCount_waitsItsBackoffFromTheRow_andARunThatEndsCleanResetsIt() throws Exception {
        var clock = new MutableClock();
        registerFaulty("t-rec", 20);
        fault("40001", 0);
        var w = workerOn(clock);

        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(kindOf(w, "t-rec")).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(reasonOf(w, "t-rec")).startsWith("retry 1, last " + clock.instant() + ": ").contains("40001");
        Record r = row("t-rec");
        assertThat(failedAt(r)).as("a transient error never makes a move failed").isNull();
        assertThat(retries(r)).isEqualTo(1);
        assertThat(last(r)).contains("40001");
        assertThat(lastAt(r).toInstant()).isEqualTo(clock.instant());

        clock.advance(Duration.ofSeconds(29));
        assertThat(w.runCycle()).as("not before last_transient_at plus 30 s").isZero();
        clock.advance(Duration.ofSeconds(1));
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(retries(row("t-rec"))).isEqualTo(2);
        assertThat(reasonOf(w, "t-rec")).startsWith("retry 2, last ");

        clock.advance(Duration.ofSeconds(59));
        assertThat(w.runCycle()).as("the second retry waits 60 s").isZero();

        clearFault();   // the cause is fixed
        clock.advance(Duration.ofSeconds(1));
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(kindOf(w, "t-rec")).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        Record done = row("t-rec");
        assertThat(retries(done)).as("a pass without a transient error resets the count").isZero();
        assertThat(lastAt(done)).isNull();
        assertThat(last(done)).isNull();
        assertEveryRowMovedOnce("tenant_plain", 150);
    }

    @Test
    void twentyOneTransientErrors_followTheBackoffToFifteenMinutes_needAnOperatorFromTheTwentieth_andNeverFail()
            throws Exception {
        var clock = new MutableClock();
        registerFaulty("t-cap", 50);
        fault("57014", 0);
        var w = workerOn(clock);

        for (int i = 1; i <= 21; i++) {
            assertThat(w.runCycle()).as("pass %d", i).isEqualTo(1);
            Record r = row("t-cap");
            assertThat(retries(r)).isEqualTo(i);
            assertThat(failedAt(r)).as("never failed").isNull();
            String reason = reasonOf(w, "t-cap");
            assertThat(reason).startsWith("retry " + i + ", last ");
            if (i >= 20) {
                assertThat(reason).endsWith("needs an operator");
            } else {
                assertThat(reason).doesNotContain("needs an operator");
            }
            Duration wait = MoveRetryPolicy.backoff(i);
            assertThat(wait).isEqualTo(Duration.ofSeconds(Math.min(900L, 30L << Math.min(i - 1, 5))));
            clock.advance(wait.minusSeconds(1));
            assertThat(w.runCycle()).as("a second early after retry %d", i).isZero();
            if (i == 2) {
                w = workerOn(clock);   // an engine restart: no memory but the row
                assertThat(w.runCycle()).as("the restarted worker honours the row's stamp").isZero();
            }
            clock.advance(Duration.ofSeconds(1));
        }
        assertThat(MoveRetryPolicy.backoff(21)).isEqualTo(Duration.ofMinutes(15));

        clearFault();
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(kindOf(w, "t-cap")).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertThat(retries(row("t-cap"))).isZero();
        assertThat(lastAt(row("t-cap"))).isNull();
        assertEveryRowMovedOnce("tenant_plain", 150);
    }

    // ── recorded and raised errors are told apart by the error mark ─────────────────────────────────────────

    /** Registers a faulty move, runs it once so its run holds a recorded 40001 row, and returns the run's error mark. */
    private long leaveAnOlderRecordedError(DataMoveWorker w, String label) throws Exception {
        registerFaulty(label, 20);
        fault("40001", 0);
        assertThat(w.runCycle()).isEqualTo(1);
        long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (DataMoveRuns.errorMark(ctx(), label) == 0 && System.nanoTime() < deadline) {
            Thread.sleep(50);   // the recorder is asynchronous
        }
        long mark = DataMoveRuns.errorMark(ctx(), label);
        assertThat(mark).as("the run holds an older error row").isPositive();
        return mark;
    }

    private FailureHandler.Failure callFailure(DataMove move, String label, long mark, String sqlstate,
                                               org.jooq.DSLContext admin) {
        return new FailureHandler.Failure(move, FailureHandler.Stage.CALL, label, "dm_test", "tenant_plain", null, mark,
            new SQLException("raised " + sqlstate, sqlstate), "run " + label + " raised " + sqlstate, admin);
    }

    @Test
    void aRaised42501WithNothingNewRecorded_isTransient_aRecorded42883_isPermanent_andTheOlderRowChangesNeither()
            throws Exception {
        var clock = new MutableClock();
        var w = workerOn(clock);
        long mark = leaveAnOlderRecordedError(w, "r-mark");

        var handler = new DataMoveFailureHandler(adminConnection, WORKER_NAME, clock, RECORDER_WAIT);
        try (var session = adminSession("tenant_plain")) {
            DataMove move = DataMoveRegistry.reload(session.ctx(), "r-mark").orElseThrow();
            var d = handler.onFailure(callFailure(move, "r-mark", mark, "42501", session.ctx()));
            assertThat(d.kind()).as("raised, and no row above the mark").isEqualTo(FailureHandler.Kind.BLOCKED);
            assertThat(d.reason()).startsWith("retry 2, last ").contains("raised 42501")
                .as("the older 40001 row is not this pass's chunk error").doesNotContain("40001");
            assertThat(failedAt(row("r-mark"))).isNull();

            // the same raised error with the mark below the old row: that row is now this pass's recorded 40001, and it wins
            var d2 = handler.onFailure(callFailure(move, "r-mark", 0, "42501", session.ctx()));
            assertThat(d2.kind()).isEqualTo(FailureHandler.Kind.BLOCKED);
            assertThat(d2.reason()).contains("recorded sqlstate=40001").doesNotContain("raised 42501");
        }

        fault("42883", 0);   // a template naming a missing function: recorded as the chunk's own error
        clock.advance(Duration.ofHours(1));
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(kindOf(w, "r-mark")).isEqualTo(MoveStates.Kind.FAILED);
        assertThat(reasonOf(w, "r-mark")).contains("42883");
        assertThat(failedAt(row("r-mark"))).isNotNull();
        assertThat(failure(row("r-mark"))).contains("42883");
    }

    @Test
    void aRaisedPermanentErrorOnARunWithAnOlderRecorded40001_isFailed_notRetriedForever() throws Exception {
        var clock = new MutableClock();
        var w = workerOn(clock);
        var handler = new DataMoveFailureHandler(adminConnection, WORKER_NAME, clock, RECORDER_WAIT);
        for (String sqlstate : new String[] {"22012", "3F000"}) {
            String label = "r-loop-" + sqlstate.toLowerCase();
            long mark = leaveAnOlderRecordedError(w, label);
            try (var session = adminSession("tenant_plain")) {
                DataMove move = DataMoveRegistry.reload(session.ctx(), label).orElseThrow();
                var d = handler.onFailure(callFailure(move, label, mark, sqlstate, session.ctx()));
                assertThat(d.kind()).as("an ignored mark would read the old 40001 row and retry %s forever", sqlstate)
                    .isEqualTo(FailureHandler.Kind.FAILED);
                assertThat(d.reason()).contains("raised " + sqlstate).doesNotContain("40001");
                assertThat(failedAt(row(label))).isNotNull();
                assertThat(failure(row(label))).contains(sqlstate);
            }
        }
    }

    @Test
    void errorsAboveAMark_areThisRunsRowsAboveIt_andNoOtherRunsRows() throws Exception {
        var clock = new MutableClock();
        registerFaulty("e-multi", 20);
        registerFaulty("e-other", 20);
        fault("40001", 0);
        var w = workerOn(clock);
        for (long wait : new long[] {0, 30, 60}) {
            clock.advance(Duration.ofSeconds(wait));
            assertThat(w.runCycle()).as("both moves are worked").isEqualTo(2);
        }
        long deadline = System.nanoTime() + Duration.ofSeconds(30).toNanos();
        while ((DataMoveRuns.errorsAbove(ctx(), "e-multi", 0).size() < 3
                || DataMoveRuns.errorsAbove(ctx(), "e-other", 0).size() < 3) && System.nanoTime() < deadline) {
            Thread.sleep(50);   // the recorder is asynchronous
        }

        var all = DataMoveRuns.errorsAbove(ctx(), "e-multi", 0);
        var other = DataMoveRuns.errorsAbove(ctx(), "e-other", 0);
        assertThat(all).as("one recorded error per pass").hasSizeGreaterThanOrEqualTo(3);
        assertThat(all).extracting(DataMoveRuns.RunError::errorId).isSorted().doesNotHaveDuplicates();
        assertThat(all).extracting(DataMoveRuns.RunError::sqlstate).containsOnly("40001");
        assertThat(other).extracting(DataMoveRuns.RunError::errorId).as("another run's rows are not this run's")
            .doesNotContainAnyElementsOf(all.stream().map(DataMoveRuns.RunError::errorId).toList());

        long first = all.get(0).errorId();
        long last = all.get(all.size() - 1).errorId();
        assertThat(DataMoveRuns.errorsAbove(ctx(), "e-multi", first)).extracting(DataMoveRuns.RunError::errorId)
            .containsExactlyElementsOf(all.stream().skip(1).map(DataMoveRuns.RunError::errorId).toList());
        assertThat(DataMoveRuns.errorsAbove(ctx(), "e-multi", last)).isEmpty();
        assertThat(DataMoveRuns.errorMark(ctx(), "e-multi")).isEqualTo(last);
        assertThat(DataMoveRuns.errorMark(ctx(), "e-other")).isEqualTo(other.get(other.size() - 1).errorId());
    }

    @Test
    void aCallThatReturnsWithoutCompleting_isBlockedThroughTheWorker_andTheNextPassResumesIt() throws Exception {
        var clock = new MutableClock();
        register("i-inc", "tenant_plain", 50, 0);
        var w = workerOn(clock);
        var fired = new java.util.concurrent.atomic.AtomicBoolean();
        // a CALL that returned with its run neither completed nor cancelled: the run's completion is taken back once
        w.afterCallHook = () -> {
            if (fired.compareAndSet(false, true)) {
                ctx().update(table("dml_utils_data", "migration_run"))
                    .set(DSL.field(DSL.name("completed_at"), OffsetDateTime.class), (OffsetDateTime) null)
                    .where(DSL.field(DSL.name("label"), String.class).eq("i-inc")).execute();
            }
        };

        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(fired).isTrue();
        assertThat(kindOf(w, "i-inc")).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(reasonOf(w, "i-inc")).startsWith("retry 1, last ").contains("returned without completing");
        Record r = row("i-inc");
        assertThat(failedAt(r)).as("transient: the next pass resumes the run").isNull();
        assertThat(retries(r)).isEqualTo(1);

        clock.advance(Duration.ofSeconds(30));
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(kindOf(w, "i-inc")).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertThat(retries(row("i-inc"))).isZero();
        assertEveryRowMovedOnce("tenant_plain", 150);
    }

    // ── a connection that cannot be opened is never the move's fault ───────────────────────────────────────

    @Test
    void anAdminSessionThatCannotBeOpened_isBlockedNotFailed_evenWhenTheHandlersOwnOpenSucceeds() throws Exception {
        var clock = new MutableClock();
        register("o-open", "tenant_plain", 50, 0);
        var w = workerOn(clock);
        var opens = new java.util.concurrent.atomic.AtomicInteger();
        // the worker's open fails once (AdminDdlSession.open drops the driver's SQLException: a Refused with no cause); the
        // failure handler then opens a connection of its own, which works
        w.beforeSessionOpenHook = () -> {
            if (opens.getAndIncrement() == 0) {
                throw new AdminDdlSession.Refused(503, "admin_connection_failed",
                    "the constraint run could not open the admin connection: sqlstate=53300");
            }
        };

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(kindOf(w, "o-open")).as("a flake must not fail the move for good").isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(reasonOf(w, "o-open")).startsWith("retry 1, last ").contains("admin connection could not be opened");
        Record r = row("o-open");
        assertThat(failedAt(r)).isNull();
        assertThat(retries(r)).isEqualTo(1);

        clock.advance(Duration.ofSeconds(30));
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(kindOf(w, "o-open")).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertEveryRowMovedOnce("tenant_plain", 150);
    }

    // ── a verification that found rows ───────────────────────────────────────────────────────────────────

    @Test
    void aNonEmptyVerification_isFailed_staysFailedAfterAWorkerRestart_andIsNotVerifiedAgain() throws Exception {
        var clock = new MutableClock();
        register("n-late", "tenant_plain", 50, 0);
        assertThat(workerOn(clock).runCycle()).as("the runs only: no verification yet").isEqualTo(1);
        // a writer puts an old-shape row (n = 0) in a chunk already done
        ctx().insertInto(table("dm_test", "tenant_plain"))
            .columns(TENANT_ID, DSL.field(DSL.name("id"), Long.class)).values("acme", 5000L).execute();

        var first = workerOn(clock, new VerifyingFinisher());
        assertThat(first.runCycle()).isEqualTo(1);

        assertThat(kindOf(first, "n-late")).isEqualTo(MoveStates.Kind.FAILED);
        assertThat(reasonOf(first, "n-late")).contains("table-wide=1");
        Record r = row("n-late");
        assertThat(failedAt(r)).as("failed_at is set").isNotNull();
        assertThat(failure(r)).contains("table-wide=1");
        assertThat(r.get(5, OffsetDateTime.class)).as("completed_at stays NULL").isNull();
        assertThat(retries(r)).isZero();

        // an engine restart: a fresh worker, a fresh state, the same registry
        var verified = new java.util.concurrent.atomic.AtomicInteger();
        var verifying = new VerifyingFinisher();
        MoveFinisher counting = c -> {
            verified.incrementAndGet();
            return verifying.finish(c);
        };
        var restarted = workerOn(clock, counting);
        clock.advance(Duration.ofHours(2));
        assertThat(restarted.runCycle()).as("a failed move is not open, so nothing is worked").isZero();
        assertThat(verified).as("the verification does not run again").hasValue(0);
        assertThat(restarted.states().state("n-late")).isEmpty();
        assertThat(failedAt(row("n-late"))).isEqualTo(failedAt(r));
    }

    // ── a stop is not a failure ───────────────────────────────────────────────────────────────────────────

    @Test
    void aStopMidCall_countsNoRetry_andSetsNoBackoff() throws Exception {
        var clock = new MutableClock();
        register("p-stop", "plain_rows", 1_000, 100);
        var w = workerOn(clock);
        w.start();
        awaitProgress("p-stop", 3);

        w.stop();

        assertThat(w.awaitStopped(Duration.ofSeconds(30))).as("the worker thread ended").isTrue();
        Record r = row("p-stop");
        assertThat(retries(r)).as("the CALL stop cancelled (57014) is not a transient failure").isZero();
        assertThat(lastAt(r)).isNull();
        assertThat(failedAt(r)).isNull();
        assertThat(reasonOf(w, "p-stop")).contains("stop");
    }

    @Test
    void aStopBetweenOpeningTheConnectionAndPublishingIt_countsNoRetry() throws Exception {
        var clock = new MutableClock();
        register("p-race", "plain_rows", 5_000, 0);
        var w = workerOn(clock);
        w.afterConnectionOpenedHook = w::stop;

        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(reasonOf(w, "p-race")).contains("stopped");
        assertThat(retries(row("p-race"))).isZero();
        assertThat(lastAt(row("p-race"))).isNull();
    }

    @Test
    void anyFailureToOpenAMovesSession_isBlocked_andTheMovesBehindItStillRun() throws Exception {
        var clock = new MutableClock();
        register("q-first", "tenant_plain", 50, 0);
        register("q-second", "unforced_owned", 20, 0);
        var w = workerOn(clock);
        var opens = new java.util.concurrent.atomic.AtomicInteger();
        w.beforeSessionOpenHook = () -> {
            if (opens.getAndIncrement() == 0) {
                throw new IllegalStateException("a driver surprise that is not a Refused");
            }
        };

        assertThat(w.runCycle()).isEqualTo(2);

        assertThat(kindOf(w, "q-first")).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(reasonOf(w, "q-first")).startsWith("retry 1, last ").contains("admin connection could not be opened");
        assertThat(failedAt(row("q-first"))).isNull();
        assertThat(kindOf(w, "q-second")).as("the move behind it was not starved").isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertEveryRowMovedOnce("unforced_owned", 40);
    }

    // ── a driving table that does not exist is permanent ───────────────────────────────────────────────────

    @Test
    void aMoveOverAMissingSchemaOrTable_isFailed_notBlockedForever() throws Exception {
        var clock = new MutableClock();
        DataMoveTestFixtures.registerOn(ctx(), "m-noschema", "dm_missing", "rows", 20, 0, null, "stamp-template.sql");
        DataMoveTestFixtures.registerOn(ctx(), "m-notable", "dm_test", "no_such_table", 20, 0, null, "stamp-template.sql");
        var w = workerOn(clock);

        assertThat(w.runCycle()).isEqualTo(2);

        for (String label : new String[] {"m-noschema", "m-notable"}) {
            assertThat(kindOf(w, label)).as(label).isEqualTo(MoveStates.Kind.FAILED);
            assertThat(reasonOf(w, label)).contains("does not exist");
            Record r = row(label);
            assertThat(failedAt(r)).as(label).isNotNull();
            assertThat(failure(r)).contains("does not exist");
            assertThat(retries(r)).isZero();
        }
        clock.advance(Duration.ofHours(1));
        assertThat(w.runCycle()).as("nothing retries a failed move").isZero();
    }

    @Test
    void aCallThatReturnedWithoutCompleting_isBlocked_andAFailedHandlerWriteIsBlockedNotFailed() throws Exception {
        var clock = new MutableClock();
        registerFaulty("r-inc", 20);
        var handler = new DataMoveFailureHandler(adminConnection, WORKER_NAME, clock, RECORDER_WAIT);
        try (var session = adminSession("tenant_plain")) {
            DataMove move = DataMoveRegistry.reload(session.ctx(), "r-inc").orElseThrow();
            var d = handler.onFailure(new FailureHandler.Failure(move, FailureHandler.Stage.INCOMPLETE, "r-inc", "dm_test",
                "tenant_plain", null, 0, null, "run r-inc returned without completing", session.ctx()));
            assertThat(d.kind()).isEqualTo(FailureHandler.Kind.BLOCKED);
            assertThat(d.reason()).startsWith("retry 1, last ").contains("returned without completing");
            assertThat(retries(row("r-inc"))).isEqualTo(1);

            // a permanent failure whose row cannot be written (no admin connection to write on) is not reported failed
            var dead = new DataMoveFailureHandler(new AdminConnection("jdbc:postgresql://127.0.0.1:1/none", "u", "p"),
                WORKER_NAME, clock, Duration.ofMillis(50));
            var unwritable = dead.onFailure(new FailureHandler.Failure(move, FailureHandler.Stage.PASS, null, "dm_test",
                "tenant_plain", null, 0, null, "an unplaceable surprise", null));
            assertThat(unwritable.kind()).isEqualTo(FailureHandler.Kind.BLOCKED);
            assertThat(unwritable.reason()).contains("an unplaceable surprise").contains("could not be recorded");
            assertThat(failedAt(row("r-inc"))).isNull();
        }
    }

    // ── a run an operator cancelled ─────────────────────────────────────────────────────────────────────────

    @Test
    void aRunDmlUtilsRecordsAsCancelled_isFailedNotRetried() throws Exception {
        var clock = new MutableClock();
        registerFaulty("c-run", 20);
        fault("40001", 0);
        var w = workerOn(clock);
        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(kindOf(w, "c-run")).isEqualTo(MoveStates.Kind.BLOCKED);

        ctx().select(DSL.function(DSL.name("dml_utils", "cancel_migration_run"), SQLDataType.BIGINT, DSL.val("c-run")))
            .fetch();
        clock.advance(Duration.ofMinutes(1));
        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(kindOf(w, "c-run")).isEqualTo(MoveStates.Kind.FAILED);
        assertThat(reasonOf(w, "c-run")).contains("c-run cancelled at");
        assertThat(failedAt(row("c-run"))).isNotNull();
        assertThat(failure(row("c-run"))).contains("cancelled at");
        clock.advance(Duration.ofHours(1));
        assertThat(w.runCycle()).isZero();
    }

    // ── no free background worker slot ──────────────────────────────────────────────────────────────────────

    @Test
    void noFreeSlot_isBlockedWithACount_notFailed_andRunsOnceASlotFreesAndTheBackoffPasses() throws Exception {
        var clock = new MutableClock();
        register("s-slot", "tenant_rows", 50, 0);
        DataMoveTestFixtures.holdWorkerSlots(su);
        long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) > 0 && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(MovePreflight.freeWorkerSlots(ctx())).as("the test holds every slot").isZero();
        var w = workerOn(clock);

        assertThat(w.runCycle()).isEqualTo(1);
        assertThat(reasonOf(w, "s-slot")).startsWith("retry 1, last ").endsWith("waiting for a background worker slot");
        Record r = row("s-slot");
        assertThat(failedAt(r)).isNull();
        assertThat(retries(r)).isEqualTo(1);
        assertThat(countWhere("tenant_rows", N.ne(0))).isZero();

        DataMoveTestFixtures.releaseWorkerSlots(su);
        long freed = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) < 1 && System.nanoTime() < freed) {
            Thread.sleep(50);
        }
        clock.advance(Duration.ofSeconds(29));   // far past blockedRetry's 10 ms floor: only the row's 30 s backoff holds it
        assertThat(w.runCycle()).as("a slot is free, but the backoff has not passed").isZero();
        clock.advance(Duration.ofSeconds(1));
        assertThat(w.runCycle()).isEqualTo(1);
        assertEveryRowMovedOnce("tenant_rows", 1000);
        assertThat(retries(row("s-slot"))).isZero();
    }

    // ── a pass that ends cleanly clears the count ─────────────────────────────────────────────────────────────

    @Test
    void aPassWhoseFinisherCompletesTheMove_clearsTheRetryCountToo() throws Exception {
        var clock = new MutableClock();
        registerFaulty("x-clean", 50);
        fault("40001", 0);
        assertThat(workerOn(clock).runCycle()).isEqualTo(1);
        assertThat(retries(row("x-clean"))).isEqualTo(1);

        clearFault();
        clock.advance(Duration.ofSeconds(30));
        var w = workerOn(clock, c -> MoveFinisher.Result.completed());
        assertThat(w.runCycle()).isEqualTo(1);

        assertThat(w.states().state("x-clean")).as("completed: nothing more to report").isEmpty();
        assertThat(retries(row("x-clean"))).isZero();
        assertThat(lastAt(row("x-clean"))).isNull();
    }

    @Test
    void aTransientFailureReportedByTheFinisher_countsAnotherRetry_andIsNeverClearedAfterwards() throws Exception {
        var clock = new MutableClock();
        registerFaulty("x-report", 50);
        fault("40001", 0);
        assertThat(workerOn(clock).runCycle()).isEqualTo(1);
        assertThat(retries(row("x-report"))).as("the starting count").isEqualTo(1);

        clearFault();
        clock.advance(Duration.ofSeconds(30));
        // the runs finish clean, then the finisher cannot finish its verification: a statement timeout, transient
        var w = workerOn(clock, c -> MoveFinisher.Result.report(new FailureHandler.Failure(c.move(),
            FailureHandler.Stage.VERIFICATION, null, "dm_test", "tenant_plain", null, 0,
            new java.sql.SQLException("canceling statement due to statement timeout", "57014"),
            "verification timed out", c.admin())));
        assertThat(w.runCycle()).isEqualTo(1);

        Record r = row("x-report");
        assertThat(retries(r)).as("a finisher-stage transient failure counts a retry; clearRetries must not run after it")
            .isEqualTo(2);
        assertThat(last(r)).contains("verification timed out");
        assertThat(lastAt(r).toInstant()).isEqualTo(clock.instant());
        assertThat(failedAt(r)).isNull();
        assertThat(kindOf(w, "x-report")).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(reasonOf(w, "x-report")).startsWith("retry 2, last ");
    }
}
