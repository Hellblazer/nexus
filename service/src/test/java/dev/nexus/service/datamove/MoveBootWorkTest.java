// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.BootWorkRegistry;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * nexus-f3yqd.2.17: what {@link MoveBootWork} says, on a fake clock and a fake reader (no database): the text it carries, the
 * high-water and clamp of {@code done}, the stall timer and its bound, and a close that does not wait for a hung read. The
 * same behaviour against real runs is {@link DataMoveBootWorkIntegrationTest}.
 */
class MoveBootWorkTest {

    /** A clock the test moves. */
    static final class FakeClock extends Clock {
        Instant now = Instant.parse("2026-11-17T00:00:00Z");

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

        void advance(Duration d) {
            now = now.plus(d);
        }
    }

    /** A reader the test programs: the moves, the failed moves and each move's runs, as the next sample reads them. */
    static final class FakeReader implements MoveBootWork.Reader {
        volatile List<DataMove> open = List.of();
        volatile List<DataMoveRegistry.FailedMove> failed = List.of();
        volatile Map<String, DataMoveRuns.Progress> progress = Map.of();
        volatile CountDownLatch holdRead;
        final CountDownLatch readEntered = new CountDownLatch(1);
        final AtomicInteger reads = new AtomicInteger();
        final AtomicBoolean aborted = new AtomicBoolean();
        final AtomicBoolean closed = new AtomicBoolean();

        @Override
        public MoveBootWork.Snapshot read() {
            reads.incrementAndGet();
            CountDownLatch hold = holdRead;
            if (hold != null) {
                readEntered.countDown();
                try {
                    hold.await();   // a read on a silent connection
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                throw new IllegalStateException("the read was aborted");
            }
            return new MoveBootWork.Snapshot(open, failed, progress);
        }

        @Override
        public void abort() {
            aborted.set(true);
            CountDownLatch hold = holdRead;
            if (hold != null) {
                hold.countDown();
            }
        }

        @Override
        public void close() {
            closed.set(true);
        }
    }

    private static DataMove move(String label) {
        return new DataMove(label, "t", "s", "t", 10, 0, 1000, "v", null, null, null, null, 0, null, null, DataMove.DEFAULT_VERIFY_STATEMENT_TIMEOUT_MS);
    }

    private final FakeClock clock = new FakeClock();
    private final BootWorkRegistry registry = new BootWorkRegistry(clock);
    private final MoveStates states = new MoveStates(clock);
    private final FakeReader reader = new FakeReader();

    private MoveBootWork work(Duration stallAfter) {
        return new MoveBootWork(registry, states, reader, clock, stallAfter);
    }

    private BootWorkRegistry.Entry entry(String label) {
        return registry.snapshot().stream().filter(e -> e.id().equals(MoveBootWork.ID_PREFIX + label)).findFirst()
            .orElseThrow(() -> new AssertionError("no entry for " + label));
    }

    // ── done: high-water and clamp ───────────────────────────────────────────────────────────────────────────

    @Test
    void shown_neverFalls_andNeverExceedsTheTotal() {
        assertThat(MoveBootWork.shown(null, 5L, 20L)).isEqualTo(5L);
        assertThat(MoveBootWork.shown(10L, 9L, 15L)).as("a leaf's run left the sum: the high-water holds").isEqualTo(10L);
        assertThat(MoveBootWork.shown(18L, 15L, 15L)).as("the high-water is clamped to the total after a leaf's total left")
            .isEqualTo(15L);
        assertThat(MoveBootWork.shown(7L, null, null)).as("the runs cannot be read: what was shown stays").isEqualTo(7L);
        assertThat(MoveBootWork.shown(null, null, null)).isNull();
        assertThat(MoveBootWork.shown(null, 25L, 20L)).as("a raw sum above the total").isEqualTo(20L);
    }

    @Test
    void anEntrysDone_followsTheHighWaterAndTheClampAcrossSamples() {
        states.working(move("m"));
        reader.open = List.of(move("m"));
        var w = work(Duration.ofMinutes(15));

        reader.progress = Map.of("m", new DataMoveRuns.Progress(10, 18));
        w.publish();
        assertThat(entry("m").done()).isEqualTo(10L);

        reader.progress = Map.of("m", new DataMoveRuns.Progress(9, 15));   // a partly done leaf's run was archived
        w.publish();
        assertThat(entry("m").done()).as("done fell with the archived run's chunks: the entry keeps its high-water")
            .isEqualTo(10L);
        assertThat(entry("m").total()).isEqualTo(15L);

        reader.progress = Map.of("m", new DataMoveRuns.Progress(15, 15));
        w.publish();
        assertThat(entry("m").done()).isEqualTo(15L).isEqualTo(entry("m").total());

        reader.progress = Map.of("m", new DataMoveRuns.Progress(15, 12));   // a completed leaf's run was archived too
        w.publish();
        assertThat(entry("m").done()).as("never above the total").isEqualTo(12L);
    }

    // ── the stall timer and its bound ───────────────────────────────────────────────────────────────────────

    @Test
    void aDoneAdvanceRestartsTheStallTimer_andTheBoundIsFromTheLaterOfTheCallStartAndTheLastAdvance() {
        var m = move("m");
        reader.open = List.of(m);
        states.working(m);
        states.callStarted(m, "m/default", 4242);
        var w = work(Duration.ofMinutes(15));
        reader.progress = Map.of("m", new DataMoveRuns.Progress(5, 20));

        w.publish();
        clock.advance(Duration.ofMinutes(10));
        w.publish();
        assertThat(entry("m").what()).as("10 m without a chunk is under the bound").startsWith("working: m");

        clock.advance(Duration.ofMinutes(4));
        reader.progress = Map.of("m", new DataMoveRuns.Progress(6, 20));   // a chunk completed at 14 m
        w.publish();
        clock.advance(Duration.ofMinutes(6));   // 20 m since the call began, 6 m since the chunk
        w.publish();
        assertThat(entry("m").what()).as("the advance restarted the timer: 6 m is under the bound").startsWith("working: m");

        clock.advance(Duration.ofMinutes(10));   // 16 m since the last chunk
        w.publish();
        assertThat(entry("m").what()).startsWith("stalled: m: no chunk has completed for 16 m");
        assertThat(entry("m").step().what()).isEqualTo("CALL in flight");

        reader.progress = Map.of("m", new DataMoveRuns.Progress(7, 20));
        w.publish();
        assertThat(entry("m").what()).as("a chunk ends the stall").startsWith("working: m");
    }

    @Test
    void theStallBound_comesFromTheEnvironmentWithADefaultAndAClamp() {
        assertThat(MoveBootWork.stallFromEnv(k -> null)).isEqualTo(Duration.ofMinutes(15));
        assertThat(MoveBootWork.stallFromEnv(k -> "")).isEqualTo(Duration.ofMinutes(15));
        assertThat(MoveBootWork.stallFromEnv(k -> "30")).isEqualTo(Duration.ofMinutes(30));
        assertThat(MoveBootWork.stallFromEnv(k -> "0")).as("clamped up").isEqualTo(Duration.ofMinutes(1));
        assertThat(MoveBootWork.stallFromEnv(k -> "99999")).as("clamped down").isEqualTo(Duration.ofMinutes(1440));
        assertThat(MoveBootWork.stallFromEnv(k -> "soon")).as("not a number: the default").isEqualTo(Duration.ofMinutes(15));
        var seen = new AtomicReference<String>();
        MoveBootWork.stallFromEnv(k -> {
            seen.set(k);
            return null;
        });
        assertThat(seen.get()).isEqualTo("NX_DATA_MOVE_STALL_MINUTES");
    }

    // ── what the entry says ─────────────────────────────────────────────────────────────────────────────────

    @Test
    void anEntryNamesNoRunTenantLeafPidOrServerMessage() {
        var m = move("m");
        reader.open = List.of(m);
        var w = work(Duration.ofMinutes(15));
        String secret = "duplicate key value violates unique constraint; Key (tenant_id)=(acme-corp) already exists";
        states.blocked(m, "run m/acme-corp/leafed_m1_acme-corp raised sqlstate=23505: " + secret);

        w.publish();

        String what = entry("m").what();
        assertThat(what).startsWith("blocked: m: a CALL raised an error sqlstate=23505");
        assertThat(what).doesNotContain("acme").doesNotContain("leafed").doesNotContain("duplicate").doesNotContain("m/");

        states.callStarted(m, "m/acme-corp/leafed_m1_acme-corp", 424242);
        states.working(m);
        w.publish();
        var e = entry("m");
        assertThat(e.what() + " " + e.step().what()).doesNotContain("424242").doesNotContain("acme").doesNotContain("leafed");

        states.failed(m, "run m/globex/leaf raised sqlstate=42883: function secret_fn(text) does not exist");
        w.publish();
        assertThat(entry("m").what()).startsWith("failed: m: a CALL raised an error sqlstate=42883")
            .contains("failure FROM nexus.data_moves WHERE label = 'm'")
            .doesNotContain("globex").doesNotContain("secret_fn");
    }

    @Test
    void aFailureOnTheRegistryRow_isReducedTheSameWay() {
        reader.failed = List.of(new DataMoveRegistry.FailedMove("f",
            "run f/acme/leaf raised sqlstate=22023: Key (tenant_id)=(acme) bad"));
        var w = work(Duration.ofMinutes(15));

        w.publish();

        assertThat(entry("f").what()).startsWith("failed: f: a CALL raised an error sqlstate=22023")
            .doesNotContain("acme").doesNotContain("leaf");
    }

    @Test
    void publicForm_keepsTheFixedPhrasesTheRetryCountAndNeedsAnOperator_andReducesTheRest() {
        assertThat(MoveReasons.publicForm("waiting for a background worker slot")).isEqualTo("waiting for a background worker slot");
        assertThat(MoveReasons.publicForm("library role dml_utils_owner, dml_utils_caller is missing"))
            .isEqualTo("library role dml_utils_owner, dml_utils_caller is missing");
        assertThat(MoveReasons.publicForm("pg_background is not installed")).isEqualTo("pg_background is not installed");
        assertThat(MoveReasons.publicForm("retry 21, last 2026-11-17T01:00:00Z: run m/acme/x raised sqlstate=40001: oops; needs an operator"))
            .isEqualTo("retry 21: a CALL raised an error sqlstate=40001; needs an operator");
        assertThat(MoveReasons.publicForm("run m/acme/x returned without completing")).isEqualTo("a CALL returned without completing its run");
        assertThat(MoveReasons.publicForm("some surprise with Key (tenant_id)=(acme)")).doesNotContain("acme");
        assertThat(MoveReasons.publicForm(null)).isEqualTo("stopped for a reason kept in the engine log");
    }

    @Test
    void publicForm_reducesTheFinishersAndTheClassifiersText_theVerificationCountsNamedTenants() {
        assertThat(MoveReasons.publicForm("verification of m found old-shape rows in s.t: acme=12, globex=3, sl/ash=500+"))
            .doesNotContain("acme").doesNotContain("globex").doesNotContain("sl/ash").contains("old-shape rows");
        assertThat(MoveReasons.publicForm("the verification of m was not run: it does not name a bind parameter"))
            .isEqualTo("the verification text was not run (see the engine log)");
        assertThat(MoveReasons.publicForm("the table-wide verification connection carries nexus.tenant=acme, so it would read"))
            .doesNotContain("acme");
        assertThat(MoveReasons.publicForm("on_complete of m did not complete: sqlstate=23514 new row for relation violates check"))
            .isEqualTo("on_complete did not complete sqlstate=23514");
        assertThat(MoveReasons.publicForm("chunk 4 recorded sqlstate=40001: could not serialize access due to Key (tenant_id)=(acme)"))
            .isEqualTo("a chunk recorded an error sqlstate=40001");
        assertThat(MoveReasons.publicForm("the driving table dm_test.nope does not exist")).isEqualTo("the driving table does not exist");
        assertThat(MoveReasons.publicForm("retry 7, last 2026-11-17T01:00:00Z: chunk 4 recorded sqlstate=40001: oops"))
            .isEqualTo("retry 7: a chunk recorded an error sqlstate=40001");
    }

    /** The failure handler puts "retry N, last <instant>: " in front of every transient reason; the phrases still read. */
    @Test
    void publicForm_readsTheReasonBehindTheRetryPrefix_forEveryPreflightAndFinishReasonItKeeps() {
        String retry = "retry 2, last 2026-11-17T01:00:00Z: ";
        assertThat(MoveReasons.publicForm(retry + "library role dml_utils_owner, dml_utils_caller is missing"))
            .isEqualTo("retry 2: library role dml_utils_owner, dml_utils_caller is missing");
        assertThat(MoveReasons.publicForm(retry + "pg_background is not installed"))
            .isEqualTo("retry 2: pg_background is not installed");
        assertThat(MoveReasons.publicForm(retry + "dml_utils.run_migration_chunks_committing is not installed"))
            .isEqualTo("retry 2: dml_utils.run_migration_chunks_committing is not installed");
        assertThat(MoveReasons.publicForm(retry + "the library install has not run"))
            .isEqualTo("retry 2: the library install has not run");
        assertThat(MoveReasons.publicForm(retry + "no separate admin role (NX_DB_ADMIN_* unset): the engine's one role holds "
            + "no dml_utils_owner grant")).isEqualTo("retry 2: the library install was skipped: no separate admin role "
            + "(NX_DB_ADMIN_* unset)");
        String privilege = "nexus_svc holds a privilege on nexus.data_moves, which the engine's serving role must not hold "
            + "(RDR-246): the boot's grants step takes it back only where the migrating role owns the table, and its owner here "
            + "is nexus_admin. As that role run: REVOKE ALL ON TABLE nexus.data_moves FROM nexus_svc";
        assertThat(MoveReasons.publicForm(retry + privilege)).isEqualTo("retry 2: " + privilege);
        assertThat(MoveReasons.publicForm(retry + "on_complete of m did not complete: sqlstate=55P03 canceling statement "
            + "due to lock timeout")).isEqualTo("retry 2: on_complete did not complete sqlstate=55P03");
        assertThat(MoveReasons.publicForm(retry + "waiting for a background worker slot"))
            .isEqualTo("retry 2: waiting for a background worker slot");
        assertThat(MoveReasons.publicForm("retry 20, last 2026-11-17T01:00:00Z: library role dml_utils_owner is missing; "
            + "needs an operator")).isEqualTo("retry 20: library role dml_utils_owner is missing; needs an operator");
    }

    /** From the 20th retry the policy appends "; needs an operator", which must not hide a phrase anchored at its end. */
    @Test
    void publicForm_readsEveryKeptReasonWithTheNeedsAnOperatorSuffix() {
        String retry = "retry 20, last 2026-11-17T01:00:00Z: ";
        String operator = "; needs an operator";
        Map<String, String> reasons = new java.util.LinkedHashMap<>();
        reasons.put("pg_background is not installed", "pg_background is not installed");
        reasons.put("dml_utils.run_migration_chunks_committing is not installed",
            "dml_utils.run_migration_chunks_committing is not installed");
        reasons.put("library role dml_utils_owner is missing", "library role dml_utils_owner is missing");
        reasons.put("the library install has not run", "the library install has not run");
        reasons.put("no separate admin role (NX_DB_ADMIN_* unset): the engine's one role holds no dml_utils_owner grant",
            "the library install was skipped: no separate admin role (NX_DB_ADMIN_* unset)");
        reasons.put("waiting for a background worker slot", "waiting for a background worker slot");
        reasons.put("on_complete of m did not complete: sqlstate=55P03 canceling statement due to lock timeout",
            "on_complete did not complete sqlstate=55P03");
        reasons.put("run m/acme/x raised sqlstate=40001: oops", "a CALL raised an error sqlstate=40001");
        reasons.put("chunk 4 recorded sqlstate=40001: oops", "a chunk recorded an error sqlstate=40001");
        reasons.put("run m/acme/x returned without completing", "a CALL returned without completing its run");
        reasons.put("the admin connection could not be opened: connection refused",
            "the admin connection could not be opened (see the engine log)");
        reasons.put("a library changelog failed: Validation Failed: 1 changesets check sum "
            + "db/vendor/ddl_utils/changes/changes.xml::001-create-schema::lee was: 9:a but is now: 9:b", null);
        reasons.forEach((reason, expected) -> {
            String published = MoveReasons.publicForm(retry + reason + operator);
            assertThat(published).as(reason).startsWith("retry 20: ").endsWith(operator)
                .doesNotContain("stopped for a reason kept in the engine log").doesNotContain("acme");
            if (expected != null) {
                assertThat(published).as(reason).isEqualTo("retry 20: " + expected + operator);
            } else {
                assertThat(published).as(reason).contains("checksum validation").contains("001-create-schema");
            }
        });
        assertThat(MoveReasons.publicForm(retry + "pg_background is not installed" + operator))
            .isEqualTo("retry 20: pg_background is not installed; needs an operator");
    }

    // ── no worker ───────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void reportWorkerNotRunning_registersOneEntryNxDoctorTurnsRed() {
        MoveBootWork.reportWorkerNotRunning(registry);

        assertThat(registry.snapshot()).singleElement().satisfies(e -> {
            assertThat(e.id()).isEqualTo(MoveBootWork.WORKER_ID);
            assertThat(e.what()).startsWith("blocked: the data-move worker is not running");
            assertThat(e.safeToStop()).isTrue();
        });
    }

    // ── close ───────────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void close_doesNotWaitBehindAReadHungOnASilentConnection_andEndsTheRead() throws Exception {
        reader.holdRead = new CountDownLatch(1);
        var w = work(Duration.ofMinutes(15));
        CompletableFuture<Integer> sample = CompletableFuture.supplyAsync(w::publish);
        assertThat(reader.readEntered.await(10, TimeUnit.SECONDS)).as("the sample is inside its read").isTrue();

        CompletableFuture<Void> closing = CompletableFuture.runAsync(w::close);
        closing.get(5, TimeUnit.SECONDS);   // a close that waited for the read would not return: nothing else ends it

        assertThat(reader.aborted).as("the read was cancelled").isTrue();
        assertThat(reader.closed).isTrue();
        assertThat(sample.get(5, TimeUnit.SECONDS)).as("the sample ends once its read does").isEqualTo(-1);
        assertThat(registry.snapshot()).as("no entry is left, and the late sample put none back").isEmpty();
    }

    @Test
    void close_takesTheEntriesOff() {
        reader.open = List.of(move("m"));
        reader.progress = Map.of("m", new DataMoveRuns.Progress(1, 2));
        var w = work(Duration.ofMinutes(15));
        w.publish();
        assertThat(registry.snapshot()).hasSize(1);

        int readsBefore = reader.reads.get();
        w.close();

        assertThat(registry.snapshot()).isEmpty();
        assertThat(w.publish()).as("a sample after close does nothing").isEqualTo(-1);
        assertThat(registry.snapshot()).isEmpty();
        assertThat(reader.reads.get()).as("and does not read: a read after close would open a session nobody closes")
            .isEqualTo(readsBefore);
    }

    @Test
    void aReporterThatCouldNotStart_isOneEntryNxDoctorTurnsRed() {
        MoveBootWork.reportReporterNotRunning(registry);

        assertThat(registry.snapshot()).singleElement().satisfies(e -> {
            assertThat(e.id()).isEqualTo(MoveBootWork.REPORTER_ID);
            assertThat(e.what()).startsWith("blocked: the data-move boot_work reporter is not running");
        });
    }

    // ── the restart path: the worker has said nothing, the row carries last boot's transient failure ────────

    @Test
    void aTransientFailureReadFromTheRowAfterARestart_isReducedLikeAnyOther() {
        var restarted = new DataMove("m", "t", "s", "t", 10, 0, 1000, "v", null, null, null, null, 3,
            java.time.OffsetDateTime.parse("2026-11-16T23:00:00Z"),
            "retry 3, last 2026-11-16T23:00:00Z: run m/acme-corp/leafed_m1_acme-corp raised sqlstate=40001: "
                + "could not serialize access; Key (tenant_id)=(acme-corp) conflicts",
            DataMove.DEFAULT_VERIFY_STATEMENT_TIMEOUT_MS);
        reader.open = List.of(restarted);
        var w = work(Duration.ofMinutes(15));

        w.publish();

        String what = entry("m").what();
        assertThat(what).startsWith("blocked: m: retry 3: a CALL raised an error sqlstate=40001")
            .contains("from before this engine started");
        assertThat(what).doesNotContain("acme").doesNotContain("leafed").doesNotContain("serialize")
            .doesNotContain("Key (").doesNotContain("m/");
    }

    @Test
    void theChecksumBranch_isTheInstallsOwnReasonAndNeverRunText() {
        String install = "a library changelog failed: liquibase.exception.ValidationFailedException: Validation Failed: "
            + "1 changesets check sum db/vendor/ddl_utils/changes/changes.xml::001-create-schema::lee was: 9:a but is now: 9:b";
        assertThat(MoveReasons.publicForm(install)).contains("changeset 001-create-schema (db/vendor/ddl_utils/changes/changes.xml)");
        assertThat(MoveReasons.publicForm("retry 2, last 2026-11-16T23:00:00Z: " + install))
            .startsWith("retry 2: a library changelog failed Liquibase's checksum validation");

        String run = "run m/acme/leaf raised sqlstate=23505: check sum a/acme/x.xml::tenant::row mismatch";
        assertThat(MoveReasons.publicForm(run)).as("run text that says check sum is a CALL's error, not an install's")
            .isEqualTo("a CALL raised an error sqlstate=23505").doesNotContain("acme").doesNotContain("x.xml");
        assertThat(MoveReasons.publicForm("some failure with check sum and a/b::c::d in it"))
            .as("not the install's reason").isEqualTo("stopped for a reason kept in the engine log");
        assertThat(MoveReasons.publicForm("a library changelog failed: check sum failed at tenant-x::acme::row"))
            .as("two :: that do not name a changelog file are not a changeset").doesNotContain("acme")
            .contains("a changeset");
    }

    // ── nexus-f3yqd.2.23: the class of a blocked entry, for nx doctor ─────────────────────────────────────

    @Test
    void aMoveBlockedOnAMissingPrerequisite_carriesTheClass_andNoOtherEntryDoes() {
        var m = move("m");
        var other = move("o");
        var failing = move("f");
        reader.open = List.of(m, other, failing);
        var w = work(Duration.ofMinutes(15));
        states.blocked(m, "pg_background is not installed");
        states.blocked(other, "waiting for a background worker slot");
        states.failed(failing, "pg_background is not installed");

        w.publish();

        assertThat(entry("m").what()).startsWith("blocked: m: pg_background is not installed");
        assertThat(entry("m").reasonClass()).isEqualTo("missing_prerequisite");
        assertThat(entry("o").reasonClass()).isNull();
        assertThat(entry("f").what()).startsWith("failed: f:");
        assertThat(entry("f").reasonClass()).as("a failed move is never classed").isNull();
    }

    @Test
    void theDmlUtilsEntryPointMissCarriesNoClass_aRetryPrefixedExtensionMissDoes_andTheCapDropsIt() {
        var a = move("a");
        var b = move("b");
        var c = move("c");
        reader.open = List.of(a, b, c);
        var w = work(Duration.ofMinutes(15));
        states.blocked(a, "dml_utils.run_migration_chunks_committing is not installed");
        states.blocked(b, "retry 3, last 2026-11-16T23:00:00Z: pg_background is not installed");
        states.blocked(c, "retry 20, last 2026-11-16T23:00:00Z: pg_background is not installed; needs an operator");

        w.publish();

        assertThat(entry("a").what()).startsWith("blocked: a: dml_utils.run_migration_chunks_committing is not installed");
        assertThat(entry("a").reasonClass()).as("stays red").isNull();
        assertThat(entry("b").reasonClass()).isEqualTo("missing_prerequisite");
        assertThat(entry("c").what()).contains("needs an operator");
        assertThat(entry("c").reasonClass()).as("past the retry cap the row turns red again").isNull();
    }

    @Test
    void theClassClearsWhenTheMoveStopsBeingBlocked_andComesFromTheRowAfterARestart() {
        var m = move("m");
        reader.open = List.of(m);
        var w = work(Duration.ofMinutes(15));
        states.blocked(m, "pg_background is not installed");
        w.publish();
        assertThat(entry("m").reasonClass()).isEqualTo("missing_prerequisite");

        states.working(m);
        w.publish();
        assertThat(entry("m").reasonClass()).isNull();

        var restarted = new DataMove("r", "t", "s", "t", 10, 0, 1000, "v", null, null, null, null, 3,
            java.time.OffsetDateTime.parse("2026-11-16T23:00:00Z"),
            "retry 3, last 2026-11-16T23:00:00Z: pg_background is not installed",
            DataMove.DEFAULT_VERIFY_STATEMENT_TIMEOUT_MS);
        reader.open = List.of(restarted);
        w.publish();
        assertThat(entry("r").what()).startsWith("blocked: r:");
        assertThat(entry("r").reasonClass()).isEqualTo("missing_prerequisite");
    }
}
