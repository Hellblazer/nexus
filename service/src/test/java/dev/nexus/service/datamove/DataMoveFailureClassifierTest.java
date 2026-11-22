// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.datamove.FailureHandler.Stage;
import dev.nexus.service.db.AdminDdlSession;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
import java.util.List;

/**
 * nexus-f3yqd.2.15: the failure classes of RDR-246 (The move worker, the failure paragraph) as a table. Every SQLSTATE the
 * RDR names is a row, in both lists (recorded chunk errors and raised errors), with a representative of each class that is
 * in neither, and unknown, null and blank.
 */
class DataMoveFailureClassifierTest {

    private static DataMoveRuns.RunError recorded(String sqlstate) {
        return new DataMoveRuns.RunError(7, 3, sqlstate, "chunk said " + sqlstate);
    }

    private static SQLException raised(String sqlstate) {
        return new SQLException("raised " + sqlstate, sqlstate);
    }

    // ── a chunk error dml_utils recorded ────────────────────────────────────────────────────────────────────

    @ParameterizedTest(name = "recorded {0} -> transient={1}")
    @CsvSource({
        // live writers: the next pass re-claims the boundary
        "40001, true", "40P01, true", "55P03, true", "57014, true",
        // a busy or restarting host: class 53 whole, and the three shutdown and startup states
        "53000, true", "53100, true", "53200, true", "53300, true", "53400, true",
        "57P01, true", "57P02, true", "57P03, true",
        // the template's own fault, even where the same SQLSTATE raised outside a chunk is transient
        "42501, false", "42883, false", "42P01, false", "42601, false",
        // a chunk records its own statement's error, not the coordinator's connection
        "08000, false", "08006, false", "08003, false",
        "22012, false", "22023, false", "23502, false", "23505, false", "3F000, false", "P0001, false", "XX000, false",
        "55000, false", "57P04, false", "57000, false", "40002, false", "40003, false", "54000, false",
        "ZZZZZ, false"
    })
    void aRecordedChunkError_isTransientOnlyForTheListedStates(String sqlstate, boolean transientExpected) {
        assertThat(DataMoveFailureClassifier.transientRecorded(sqlstate)).isEqualTo(transientExpected);
        var verdict = DataMoveFailureClassifier.classify(Stage.CALL, raised("XX000"), List.of(recorded(sqlstate)), "d");
        assertThat(verdict.permanent()).isEqualTo(!transientExpected);
        assertThat(verdict.reason()).contains(sqlstate).contains("chunk said " + sqlstate);
    }

    @Test
    void aRecordedError_beatsWhatTheCallRaised() {
        // the CALL raised a state that is transient when raised alone; the recorded row is the chunk's own and says permanent
        var verdict = DataMoveFailureClassifier.classify(Stage.CALL, raised("57014"), List.of(recorded("22012")), "d");
        assertThat(verdict.permanent()).isTrue();
        // and the other way round
        verdict = DataMoveFailureClassifier.classify(Stage.CALL, raised("22012"), List.of(recorded("40001")), "d");
        assertThat(verdict.permanent()).isFalse();
    }

    @Test
    void severalRecordedErrors_aPermanentOneMakesTheFailurePermanent_andIsTheOneReported() {
        var verdict = DataMoveFailureClassifier.classify(Stage.CALL, raised("XX000"),
            List.of(recorded("40001"), recorded("22012"), recorded("40P01")), "d");
        assertThat(verdict.permanent()).isTrue();
        assertThat(verdict.reason()).contains("22012");

        verdict = DataMoveFailureClassifier.classify(Stage.CALL, raised("XX000"),
            List.of(recorded("40001"), recorded("40P01")), "d");
        assertThat(verdict.permanent()).isFalse();
        assertThat(verdict.reason()).as("the latest transient error is the one reported").contains("40P01");
    }

    // ── an error the CALL raised that dml_utils did not record ──────────────────────────────────────────────

    @ParameterizedTest(name = "raised {0} -> transient={1}")
    @CsvSource({
        "40001, true", "40P01, true", "55P03, true", "57014, true",
        "53000, true", "53200, true", "57P01, true", "57P02, true", "57P03, true",
        "08000, true", "08001, true", "08003, true", "08006, true", "08P01, true",
        // a grant not yet made, an install that vanished between the check and the CALL
        "42501, true", "42883, true",
        // the rest: class 22 data, 23 integrity, the rest of 42, 3F, P0, 55000, and anything else
        "22023, false", "22012, false", "23505, false", "42P01, false", "42601, false", "3F000, false", "P0001, false",
        "55000, false", "57P04, false", "XX000, false", "40002, false", "28000, false", "ZZZZZ, false"
    })
    void aRaisedErrorWithNoRecordedChunkError_isTransientOnlyForTheListedStates(String sqlstate, boolean transientExpected) {
        assertThat(DataMoveFailureClassifier.transientRaised(sqlstate)).isEqualTo(transientExpected);
        var verdict = DataMoveFailureClassifier.classify(Stage.CALL, raised(sqlstate), List.of(), "run r raised " + sqlstate);
        assertThat(verdict.permanent()).isEqualTo(!transientExpected);
        assertThat(verdict.reason()).contains(sqlstate);
    }

    @ParameterizedTest(name = "[{index}] sqlstate={0}")
    @CsvSource(nullValues = "NULL", value = {"NULL", "''", "'  '", "4000", "400011", "garbage"})
    void anUnknownNullOrBlankSqlstate_isPermanent_soNothingLoops(String sqlstate) {
        assertThat(DataMoveFailureClassifier.transientRaised(sqlstate)).isFalse();
        assertThat(DataMoveFailureClassifier.transientRecorded(sqlstate)).isFalse();
        assertThat(DataMoveFailureClassifier.classify(Stage.CALL, raised(sqlstate), List.of(), "d").permanent()).isTrue();
        assertThat(DataMoveFailureClassifier.classify(Stage.CALL, raised(sqlstate), List.of(recorded(sqlstate)), "d")
            .permanent()).isTrue();
    }

    @Test
    void aCallThatRaisedNothingWeCanRead_isPermanent() {
        var verdict = DataMoveFailureClassifier.classify(Stage.CALL, null, List.of(), "the call failed in some way");
        assertThat(verdict.permanent()).isTrue();
        assertThat(verdict.reason()).contains("the call failed in some way");
    }

    // ── the other stages ────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aConnectionThatCannotBeOpened_isAlwaysTransient_whateverWasRaised() {
        assertThat(DataMoveFailureClassifier.classify(Stage.CONNECTION, null, List.of(), "no connection").permanent())
            .isFalse();
        assertThat(DataMoveFailureClassifier.classify(Stage.CONNECTION, raised("28P01"), List.of(), "no connection")
            .permanent()).isFalse();
        assertThat(DataMoveFailureClassifier.classify(Stage.CONNECTION, raised("3F000"), List.of(), "no connection")
            .permanent()).isFalse();
        assertThat(DataMoveFailureClassifier.classify(Stage.CONNECTION, null, List.of(), "no connection").reason())
            .isEqualTo("no connection");
    }

    @Test
    void aPreflightMissTheWorkerNames_isTransient_whereverItCarriesNoSqlException() {
        // no install, a missing role, no free slot, an admin that cannot see every row
        var verdict = DataMoveFailureClassifier.classify(Stage.PREFLIGHT, null, List.of(), "waiting for a background worker slot");
        assertThat(verdict.permanent()).isFalse();
        assertThat(verdict.reason()).isEqualTo("waiting for a background worker slot");
    }

    @ParameterizedTest(name = "a preflight check that raised {0} -> transient={1}")
    @CsvSource({"40001, true", "57014, true", "53300, true", "57P01, true", "08006, true", "42501, true", "42883, true",
        // a missing driving schema or table, and anything else the check cannot place
        "3F000, false", "42P01, false", "22023, false", "28P01, false", "XX000, false"})
    void aPreflightCheckThatItselfRaised_followsTheRaisedList(String sqlstate, boolean transientExpected) {
        assertThat(DataMoveFailureClassifier.classify(Stage.PREFLIGHT, raised(sqlstate), List.of(), "check failed").permanent())
            .isEqualTo(!transientExpected);
    }

    @Test
    void aSessionThatCouldNotBeOpened_isPlacedAsAConnectionException_soAFlakeIsNeverUnplaceable() {
        var refused = new AdminDdlSession.Refused(503, "admin_connection_failed",
            "the constraint run could not open the admin connection: sqlstate=53300");
        assertThat(DataMoveFailureClassifier.sqlExceptionOf(refused).getSQLState()).isEqualTo("08001");
        assertThat(DataMoveFailureClassifier.classify(Stage.PASS, DataMoveFailureClassifier.sqlExceptionOf(refused), List.of(),
            "the pass could not run").permanent()).isFalse();
        assertThat(DataMoveFailureClassifier.classify(Stage.CONNECTION, DataMoveFailureClassifier.sqlExceptionOf(refused),
            List.of(), "the admin connection could not be opened").permanent()).isFalse();
    }

    @Test
    void aRefusalThatNamesAStatementsSqlstate_carriesIt_andAnyOtherRefusalCarriesNone() {
        var statement = new AdminDdlSession.Refused(409, "lock", "lock timeout", "55P03");
        assertThat(DataMoveFailureClassifier.sqlExceptionOf(statement).getSQLState()).isEqualTo("55P03");
        var other = new AdminDdlSession.Refused(409, "constraint_run_in_progress", "another run holds the lock");
        assertThat((Object) DataMoveFailureClassifier.sqlExceptionOf(other)).isNull();
        assertThat(DataMoveFailureClassifier.classify(Stage.PASS, DataMoveFailureClassifier.sqlExceptionOf(other), List.of(),
            "d").permanent()).as("nothing to place it by").isTrue();
        var wrapped = new RuntimeException("wrap", statement);
        assertThat(DataMoveFailureClassifier.sqlExceptionOf(wrapped).getSQLState()).isEqualTo("55P03");
    }

    @ParameterizedTest
    @CsvSource({"PROBE", "PASS"})
    void aSurpriseFromAQuery_followsTheRaisedList(Stage stage) {
        assertThat(DataMoveFailureClassifier.classify(stage, raised("08006"), List.of(), "lost").permanent()).isFalse();
        assertThat(DataMoveFailureClassifier.classify(stage, raised("57P01"), List.of(), "shutdown").permanent()).isFalse();
        assertThat(DataMoveFailureClassifier.classify(stage, raised("42P01"), List.of(), "gone").permanent()).isTrue();
        assertThat(DataMoveFailureClassifier.classify(stage, null, List.of(), "an NPE").permanent())
            .as("nothing to place it by: unclassified, so permanent").isTrue();
    }

    // ── the finisher's stages (nexus-f3yqd.2.14) ─────────────────────────────────────────────────────────

    @ParameterizedTest
    @CsvSource({"VERIFICATION_NONEMPTY", "VERIFICATION_REFUSED"})
    void aVerificationThatFoundRowsOrWasRefused_isPermanent_whateverWasRaised(Stage stage) {
        assertThat(DataMoveFailureClassifier.classify(stage, null, List.of(), "old-shape rows: table-wide=3").permanent())
            .isTrue();
        assertThat(DataMoveFailureClassifier.classify(stage, raised("40001"), List.of(), "d").permanent()).isTrue();
        assertThat(DataMoveFailureClassifier.classify(stage, null, List.of(), "old-shape rows: table-wide=3").reason())
            .isEqualTo("old-shape rows: table-wide=3");
    }

    @ParameterizedTest(name = "a verification that raised {0} -> transient={1}")
    @CsvSource(nullValues = "NULL", value = {"NULL, true", "57014, true", "08000, true", "08006, true", "08001, true",
        "57P01, true", "57P02, true", "57P03, true", "53200, true", "53300, true", "53100, true",
        "42501, false", "42883, false", "40001, false", "40P01, false", "55P03, false",
        "42P01, false", "22012, false", "3F000, false", "XX000, false"})
    void aVerificationThatCouldNotFinish_isTransientOnlyForATimeoutALostConnectionAShutdownOrABusyHost(String sqlstate,
                                                                                            boolean expected) {
        SQLException error = sqlstate == null ? null : raised(sqlstate);
        assertThat(DataMoveFailureClassifier.classify(Stage.VERIFICATION, error, List.of(), "verification could not run")
            .permanent()).isEqualTo(!expected);
    }

    @ParameterizedTest(name = "on_complete raised {0} -> transient={1}")
    @CsvSource(nullValues = "NULL", value = {"55P03, true", "57014, true", "08000, true", "08006, true", "08001, true",
        "57P01, true", "57P02, true", "57P03, true", "40P01, true", "40001, true", "53200, true", "53300, true",
        "42501, false", "42883, false", "42P01, false", "23514, false",
        "22012, false", "XX000, false", "NULL, false"})
    void anOnCompleteFailure_isTransientForItsTimeoutsAConflictALostConnectionAShutdownOrABusyHost(String sqlstate, boolean expected) {
        SQLException error = sqlstate == null ? null : raised(sqlstate);
        assertThat(DataMoveFailureClassifier.classify(Stage.ON_COMPLETE, error, List.of(), "on_complete failed").permanent())
            .isEqualTo(!expected);
    }

    @Test
    void aCancelledRun_isPermanent() {
        var verdict = DataMoveFailureClassifier.classify(Stage.CANCELLED, null, List.of(), "run r cancelled at 2026-11-17");
        assertThat(verdict.permanent()).isTrue();
        assertThat(verdict.reason()).isEqualTo("run r cancelled at 2026-11-17");
    }

    @Test
    void aCallThatReturnedWithoutCompleting_isTransient_unlessItRecordedAPermanentChunkError() {
        assertThat(DataMoveFailureClassifier.classify(Stage.INCOMPLETE, null, List.of(), "returned without completing")
            .permanent()).isFalse();
        assertThat(DataMoveFailureClassifier.classify(Stage.INCOMPLETE, null, List.of(recorded("40001")), "d").permanent())
            .isFalse();
        assertThat(DataMoveFailureClassifier.classify(Stage.INCOMPLETE, null, List.of(recorded("22012")), "d").permanent())
            .isTrue();
    }

    // ── the recorder runs asynchronously ────────────────────────────────────────────────────────────────────

    @ParameterizedTest(name = "{0} raised, none recorded yet -> look again: {1}")
    @CsvSource({"42501, true", "42883, true", "08006, true", "08001, true", "40001, false", "57014, false", "53200, false",
        "22012, false", "XX000, false"})
    void onlyTheRaisedStatesARecordedErrorWouldReclassify_areReadAgain(String sqlstate, boolean again) {
        assertThat(DataMoveFailureClassifier.needsRecorderRecheck(Stage.CALL, raised(sqlstate))).isEqualTo(again);
    }

    @Test
    void noStageButACallIsReadAgain() {
        for (Stage stage : Stage.values()) {
            if (stage != Stage.CALL) {
                assertThat(DataMoveFailureClassifier.needsRecorderRecheck(stage, raised("42501"))).as("%s", stage).isFalse();
            }
        }
        assertThat(DataMoveFailureClassifier.needsRecorderRecheck(Stage.CALL, null)).isFalse();
    }

    // ── the cause behind a runtime exception ────────────────────────────────────────────────────────────────

    @Test
    void theSqlExceptionBehindAWrappedFailure_isFound() {
        var sql = raised("08006");
        // SQLException is itself an Iterable, so AssertJ needs the object overload named
        assertThat((Object) DataMoveFailureClassifier.sqlExceptionOf(new RuntimeException("wrap", new RuntimeException(sql))))
            .isSameAs(sql);
        assertThat((Object) DataMoveFailureClassifier.sqlExceptionOf(sql)).isSameAs(sql);
        assertThat((Object) DataMoveFailureClassifier.sqlExceptionOf(new IllegalStateException("none"))).isNull();
        assertThat((Object) DataMoveFailureClassifier.sqlExceptionOf(null)).isNull();
    }

    // ── cadence ─────────────────────────────────────────────────────────────────────────────────────────────

    @ParameterizedTest(name = "retry {0} waits {1} s")
    @CsvSource({"1, 30", "2, 60", "3, 120", "4, 240", "5, 480", "6, 900", "7, 900", "20, 900", "21, 900", "1000000, 900"})
    void theBackoffDoublesFrom30SecondsAndStopsAt15Minutes(int retries, long seconds) {
        assertThat(MoveRetryPolicy.backoff(retries)).isEqualTo(Duration.ofSeconds(seconds));
    }

    @Test
    void aMoveWithNoRetriesWaitsNothing() {
        assertThat(MoveRetryPolicy.backoff(0)).isEqualTo(Duration.ZERO);
    }

    @Test
    void aMoveIsDueOnceItsBackoffHasPassed() {
        Instant last = Instant.parse("2026-11-17T10:00:00Z");
        assertThat(MoveRetryPolicy.due(last, 1, last.plusSeconds(29))).isFalse();
        assertThat(MoveRetryPolicy.due(last, 1, last.plusSeconds(30))).isTrue();
        assertThat(MoveRetryPolicy.due(last, 6, last.plusSeconds(899))).isFalse();
        assertThat(MoveRetryPolicy.due(last, 6, last.plusSeconds(900))).isTrue();
        assertThat(MoveRetryPolicy.due(null, 0, last)).as("never failed transiently").isTrue();
    }

    @Test
    void theBlockedReasonShowsTheCountAndTheTime_andFromTheCapAddsNeedsAnOperator() {
        Instant at = Instant.parse("2026-11-17T10:00:00Z");
        assertThat(MoveRetryPolicy.blockedReason(1, at, "no slot")).isEqualTo("retry 1, last 2026-11-17T10:00:00Z: no slot");
        assertThat(MoveRetryPolicy.blockedReason(19, at, "no slot")).doesNotContain("needs an operator");
        assertThat(MoveRetryPolicy.blockedReason(20, at, "no slot")).contains("retry 20").endsWith("needs an operator");
        assertThat(MoveRetryPolicy.blockedReason(21, at, "no slot")).contains("retry 21").endsWith("needs an operator");
    }
}
