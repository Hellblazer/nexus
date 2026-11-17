// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.DmlUtilsTestFixtures;
import dev.nexus.service.NexusService;
import dev.nexus.service.TestHttp;
import dev.nexus.service.db.DmlUtilsInstaller;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/**
 * nexus-f3yqd.2.17 (RDR-246 Step 4, Progress and Day 2 Operations): what {@code /v1/status} {@code boot_work} says of
 * each data move, read through the real {@link NexusService} route, on the pg_background image. One entry per open or
 * failed move; {@code blocked:} with the reason, {@code failed:} with the failure; {@code done} never falls and ends
 * equal to {@code total}; a retry that runs is {@code working:}, not its old blocked text; a CALL with no chunk done for
 * too long is {@code stalled:}; the serving role holding a privilege on the registry blocks every move.
 */
class DataMoveBootWorkIntegrationTest extends DataMoveIntegrationBase {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Duration DEFAULT_STALL = Duration.ofMinutes(15);

    private BootWorkRegistry registry;
    private NexusService service;
    private final HttpClient http = TestHttp.client();
    private final List<MoveBootWork> reporters = new ArrayList<>();

    @BeforeEach
    void startRoute() throws Exception {
        registry = new BootWorkRegistry(Clock.systemUTC());
        service = new NexusService(0, "boot-work-moves-token", adminPool);
        service.bootWork(registry);
        service.start();
    }

    @AfterEach
    void stopRoute() {
        reporters.forEach(MoveBootWork::close);
        reporters.clear();
        if (service != null) {
            service.stop();
        }
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    MoveBootWork reporter(DataMoveWorker w, Duration stallAfter) {
        var r = new MoveBootWork(registry, w.states(), adminConnection, WORKER_NAME + "-bootwork", Clock.systemUTC(),
            stallAfter);
        reporters.add(r);
        return r;
    }

    /** {@code boot_work} as the wire carries it. */
    JsonNode bootWork() throws Exception {
        HttpResponse<String> resp = http.send(
            TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/status").GET().build(),
            HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(200);
        return MAPPER.readTree(resp.body()).get("boot_work");
    }

    /** The move's entry on the wire, or null. */
    JsonNode entry(String label) throws Exception {
        for (JsonNode e : bootWork()) {
            if (e.get("id").asText().equals(MoveBootWork.ID_PREFIX + label)) {
                return e;
            }
        }
        return null;
    }

    String what(String label) throws Exception {
        JsonNode e = entry(label);
        assertThat(e).as("an entry for %s", label).isNotNull();
        return e.get("what").asText();
    }

    // ── acceptance: scenario 1, read through /v1/status ─────────────────────────────────────────────────────

    @Test
    void hundredThousandRows_oneEntry_doneNeverFalls_andEndsEqualToTotal() throws Exception {
        register("m-plain", "plain_rows", 5_000, 20);
        var w = worker(installed(), null);
        var reporter = reporter(w, DEFAULT_STALL);
        CompletableFuture<Integer> cycle = async(w::runCycle);

        List<Long> done = new ArrayList<>();
        boolean midRun = false;
        boolean working = false;
        long deadline = System.nanoTime() + Duration.ofMinutes(5).toNanos();
        while (!cycle.isDone()) {
            reporter.publish();
            JsonNode e = entry("m-plain");
            if (e != null) {
                assertThat(e.get("unit").asText()).isEqualTo("chunks");
                assertThat(e.get("safe_to_stop").asBoolean()).isTrue();
                assertThat(e.get("on_stop").asText()).isEqualTo("resumes from the last committed batch");
                assertThat(bootWork()).as("one entry for the one move").hasSize(1);
                done.add(e.get("done").asLong());
                working |= e.get("what").asText().startsWith("working: m-plain");
                midRun |= e.get("done").asLong() > 0 && e.get("done").asLong() < e.get("total").asLong();
            }
            assertThat(System.nanoTime()).as("the cycle ended").isLessThan(deadline);
            Thread.sleep(20);
        }
        cycle.get();
        reporter.publish();

        assertThat(done).as("samples taken while the move ran").hasSizeGreaterThan(3);
        long previous = 0;
        for (long d : done) {
            assertThat(d).as("done fell from %d", previous).isGreaterThanOrEqualTo(previous);
            previous = d;
        }
        assertThat(working).as("reported working while it ran").isTrue();
        assertThat(midRun).as("a sample between the first chunk and the last").isTrue();
        JsonNode end = entry("m-plain");
        assertThat(end.get("total").asLong()).isEqualTo(20);
        assertThat(end.get("done").asLong()).isEqualTo(end.get("total").asLong());
        assertThat(end.get("what").asText()).startsWith("runs complete: m-plain");
        assertEveryRowMovedOnce("plain_rows", 100_000);
    }

    // ── acceptance: every open move is blocked, with the reason ─────────────────────────────────────────────

    @Test
    void aLibraryRoleIsMissing_thenALibraryChangelogFails_everyOpenMoveIsBlockedWithTheReason() throws Exception {
        DataMoveTestFixtures.dropLibraryRoles(su);
        var skipped = installed();
        register("m-one", "leafed", 50, 0);
        register("m-two", "tenant_rows", 50, 0);
        var early = worker(skipped, null);
        var reporter = reporter(early, DEFAULT_STALL);

        assertThat(early.runCycle()).isEqualTo(2);
        reporter.publish();

        for (String label : List.of("m-one", "m-two")) {
            assertThat(what(label)).as("every open move says why").startsWith("blocked: " + label + ": ")
                .contains("dml_utils_owner").contains("missing");
        }
        assertThat(bootWork()).hasSize(2);

        // the roles arrive; then a re-pin changes a changeset the install had applied
        DmlUtilsTestFixtures.bootstrapDmlUtils(su);
        assertThat(installed().status().applied()).isTrue();
        var tracking = DSL.table(DSL.name("liquibase", "ddl_utils_databasechangelog"));
        var md5 = DSL.field(DSL.name("md5sum"), String.class);
        var id = DSL.field(DSL.name("id"), String.class);
        String original = ctx().select(md5).from(tracking).where(id.eq("001-create-schema")).fetchOne(md5);
        int tampered = ctx().update(tracking).set(md5, "9:00000000000000000000000000000dead")
            .where(id.eq("001-create-schema")).execute();
        assertThat(tampered).as("non-vacuity: one tracking row was changed").isEqualTo(1);
        try {
            var drifted = installed();
            assertThat(drifted.status().state()).isEqualTo(DmlUtilsInstaller.State.FAILED);

            var later = worker(drifted, null);
            var laterReporter = reporter(later, DEFAULT_STALL);
            assertThat(later.runCycle()).isEqualTo(2);
            laterReporter.publish();

            for (String label : List.of("m-one", "m-two")) {
                assertThat(what(label)).startsWith("blocked: " + label + ": ").containsIgnoringCase("checksum")
                    .contains("001-create-schema").contains("recovery:").contains("not runOnChange");
            }
        } finally {
            ctx().update(tracking).set(md5, original).where(id.eq("001-create-schema")).execute();   // the next scenario's install
        }
    }

    // ── the real failure handler: the retry prefix does not hide the reason ───────────────────────────────────

    @Test
    void throughTheRealFailureHandler_aLibraryRoleMissingSurvivesTheRetryPrefixOnStatus() throws Exception {
        DataMoveTestFixtures.dropLibraryRoles(su);
        var skipped = installed();
        register("h-real", "leafed", 50, 0);
        var handler = new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC());
        var w = worker(skipped, null, handler, MoveFinisher.RUNS_ONLY);
        var reporter = reporter(w, DEFAULT_STALL);

        assertThat(w.runCycle()).isEqualTo(1);
        reporter.publish();

        assertThat(w.states().state("h-real").orElseThrow().reason())
            .as("non-vacuity: the handler prefixed the reason with the retry count").startsWith("retry 1, last ");
        assertThat(what("h-real")).startsWith("blocked: h-real: retry 1: library role ").contains("dml_utils_owner")
            .contains("is missing").doesNotContain("stopped for a reason kept in the engine log");
    }

    // ── acceptance: a failed move shows failed: with its failure, and drops off once superseded ────────────

    @Test
    void aFailedMove_showsFailedWithItsError_staysAfterARestart_andDropsOffOnceSuperseded() throws Exception {
        FailureHandler failing = f -> FailureHandler.Disposition.failed("the handler's verdict");
        DataMoveTestFixtures.register(ctx(), "f-bad", "leafed", 50, 0, null, "no-clause-template.sql");
        var w = worker(installed(), null, failing, MoveFinisher.RUNS_ONLY);
        var reporter = reporter(w, DEFAULT_STALL);

        assertThat(w.runCycle()).isEqualTo(1);
        reporter.publish();
        assertThat(what("f-bad")).startsWith("failed: f-bad: stopped for a reason kept in the engine log")
            .contains("failure FROM nexus.data_moves WHERE label = 'f-bad'").doesNotContain("verdict");

        // the registry row says it failed, as the failure-class bead records it: a new process knows it from the row
        ctx().update(DSL.table(DSL.name("nexus", "data_moves")))
            .set(DSL.field(DSL.name("failed_at"), java.time.OffsetDateTime.class), java.time.OffsetDateTime.now())
            .set(DSL.field(DSL.name("failure"), String.class), "verification found 3 old-shape rows")
            .where(DSL.field(DSL.name("label"), String.class).eq("f-bad")).execute();
        var restarted = worker(installed(), null);
        var afterRestart = reporter(restarted, DEFAULT_STALL);
        assertThat(restarted.runCycle()).as("a failed move is not worked").isZero();
        afterRestart.publish();
        assertThat(what("f-bad")).startsWith("failed: f-bad: stopped for a reason kept in the engine log")
            .doesNotContain("old-shape");
        assertThat(bootWork()).hasSize(1);

        DataMoveTestFixtures.register(ctx(), "f-fix", "leafed", 50, 0, "f-bad", "stamp-template.sql");
        afterRestart.publish();
        assertThat(entry("f-bad")).as("superseded: no longer reported").isNull();
        assertThat(what("f-fix")).startsWith("queued: f-fix");
    }

    // ── M3: a retry that is running is working, not its stale blocked text ──────────────────────────────────

    @Test
    void aBlockedMovesRetryThatRuns_isWorking_notTheOldBlockedText_andKeepsTheFirstBlockedTime() throws Exception {
        register("r-retry", "plain_rows", 2_000, 60);
        DataMoveTestFixtures.holdWorkerSlots(su);
        long slots = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (MovePreflight.freeWorkerSlots(ctx()) > 0 && System.nanoTime() < slots) {
            Thread.sleep(50);
        }
        var w = worker(installed(), null);
        var reporter = reporter(w, DEFAULT_STALL);

        assertThat(w.runCycle()).isEqualTo(1);
        reporter.publish();
        assertThat(what("r-retry")).startsWith("blocked: r-retry: waiting for a background worker slot")
            .contains("(blocked since ");
        var firstBlocked = w.states().state("r-retry").orElseThrow().firstBlockedAt();
        assertThat(firstBlocked).isNotNull();

        DataMoveTestFixtures.releaseWorkerSlots(su);
        Thread.sleep(30);   // past the retry wait
        CompletableFuture<Integer> retry = async(w::runCycle);
        long started = System.nanoTime() + Duration.ofMinutes(1).toNanos();
        while (w.states().state("r-retry").orElseThrow().kind() == MoveStates.Kind.BLOCKED && System.nanoTime() < started) {
            Thread.sleep(5);   // the retry has not begun: its state is still the old block
        }
        boolean workingText = false;
        long deadline = System.nanoTime() + Duration.ofMinutes(5).toNanos();
        while (!retry.isDone() && System.nanoTime() < deadline) {
            reporter.publish();
            String text = what("r-retry");
            assertThat(text).as("a retry in flight never reports the old blocked reason")
                .doesNotContain("waiting for a background worker slot");
            workingText |= text.startsWith("working: r-retry, retrying after being blocked since ");
            Thread.sleep(20);
        }
        retry.get();
        assertThat(workingText).as("the entry said working while the retry ran").isTrue();
        assertThat(w.states().state("r-retry").orElseThrow().kind()).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertThat(w.states().state("r-retry").orElseThrow().firstBlockedAt())
            .as("cleared once the runs are complete").isNull();
    }

    // ── a stalled CALL is visible ───────────────────────────────────────────────────────────────────────────

    @Test
    void aCallWithNoChunkDoneForTheStallBound_isStalledWithItsPid_andNotBeforeIt() throws Exception {
        register("s-slow", "plain_rows", 50_000, 2_500);   // two chunks, a pause after each: the CALL idles between them
        var w = worker(installed(), null);
        var quick = reporter(w, Duration.ofMillis(400));
        var patient = reporter(w, DEFAULT_STALL);
        CompletableFuture<Integer> cycle = async(w::runCycle);

        String stalled = null;
        boolean patientEverStalled = false;
        long deadline = System.nanoTime() + Duration.ofMinutes(2).toNanos();
        while (!cycle.isDone() && System.nanoTime() < deadline) {
            patient.publish();
            patientEverStalled |= what("s-slow").startsWith("stalled:");
            quick.publish();
            String text = what("s-slow");
            if (text.startsWith("stalled:")) {
                stalled = text;
                JsonNode e = entry("s-slow");
                assertThat(e.at("/step/what").asText()).isEqualTo("CALL in flight");
                break;
            }
            Thread.sleep(50);
        }
        w.stop();   // ends the CALL at the server; the run resumes at the next boot
        cycle.get();
        assertThat(stalled).as("stalled while the CALL idled").isNotNull()
            .contains("no chunk has completed for").contains("pg_cancel_backend").contains("event=data_move_call");
        assertThat(patientEverStalled).as("not stalled before the bound").isFalse();
    }

    // ── queued, paused ───────────────────────────────────────────────────────────────────────────────────────

    @Test
    void anOpenMoveBehindAnotherMovesCall_isQueued_andAPausedMoveSaysWhoPausedIt() throws Exception {
        register("q-first", "plain_rows", 2_000, 60);
        register("q-second", "tenant_rows", 50, 0);
        register("q-held", "leafed", 50, 0);
        DataMoveTestFixtures.pause(ctx(), "q-held", true);
        var w = worker(installed(), null);
        var reporter = reporter(w, DEFAULT_STALL);
        CompletableFuture<Integer> cycle = async(w::runCycle);

        long deadline = System.nanoTime() + Duration.ofMinutes(2).toNanos();
        while (w.states().call("q-first").isEmpty() && System.nanoTime() < deadline) {
            Thread.sleep(20);
        }
        reporter.publish();
        assertThat(what("q-first")).startsWith("working: q-first");
        assertThat(what("q-second")).startsWith("queued: q-second");
        assertThat(what("q-held")).isEqualTo("paused: q-held by test (an operator holds it; nothing is lost)");
        w.stop();
        cycle.get();
    }

    // ── /v1/status is unauthenticated: no tenant, leaf, pid or server message ─────────────────────────────

    @Test
    void theStatusBodyNamesNoTenantLeafPidOrServerMessage_forAFailedCallAndAStalledOne() throws Exception {
        // a per-tenant-leaf move whose first CALL raises: the run is named x-leak/<tenant>/<leaf> and the server's message
        // is in the worker's own reason
        DataMoveTestFixtures.register(ctx(), "x-leak", "leafed", 50, 0, null, "no-clause-template.sql");
        var w = worker(installed(), null);
        var reporter = reporter(w, Duration.ofMillis(400));
        assertThat(w.runCycle()).isEqualTo(1);
        String full = w.states().state("x-leak").orElseThrow().reason();
        assertThat(full).as("non-vacuity: the worker's own reason names the run, the leaf and the server's message")
            .contains("x-leak/default/leafed_m1_default").contains("sqlstate=22023");
        String serverMessage = full.substring(full.indexOf("sqlstate=22023: ") + "sqlstate=22023: ".length());
        assertThat(serverMessage).isNotBlank();
        reporter.publish();

        String body = statusBody();
        assertThat(body).contains("x-leak").contains("sqlstate=22023");
        assertThat(body).doesNotContain("x-leak/").doesNotContain("leafed_m").doesNotContain("acme")
            .doesNotContain("globex").doesNotContain("/default").doesNotContain(serverMessage);

        // a stalled CALL: the pid is in the engine log, not in the status
        register("x-slow", "plain_rows", 50_000, 2_500);
        DataMoveTestFixtures.register(ctx(), "x-gone", "leafed", 50, 0, "x-leak", "stamp-template.sql");   // closes x-leak
        var w2 = worker(installed(), null);
        var quick = reporter(w2, Duration.ofMillis(400));
        CompletableFuture<Integer> cycle = async(w2::runCycle);
        String stalledBody = null;
        String stalledEntry = null;
        int pid = -1;
        long deadline = System.nanoTime() + Duration.ofMinutes(2).toNanos();
        while (!cycle.isDone() && System.nanoTime() < deadline) {
            quick.publish();
            String text = what("x-slow");
            if (text.startsWith("stalled:")) {
                pid = callPid();
                stalledBody = statusBody();
                JsonNode e = entry("x-slow");
                stalledEntry = e.get("what").asText() + " | " + e.at("/step/what").asText();
                break;
            }
            Thread.sleep(50);
        }
        w2.stop();
        cycle.get();
        assertThat(stalledBody).as("a stalled entry was published").isNotNull();
        assertThat(pid).as("non-vacuity: the CALL's backend pid").isPositive();
        assertThat(stalledBody).contains("stalled: x-slow").doesNotContain("pid=").doesNotContain("x-slow/")
            .doesNotContain("plain_rows");
        // what and step alone (the entry's other fields carry timestamps): no pid, as a word or as a number, no run label, and
        // no run of three or more digits (a pid; the minutes in the text are one or two)
        assertThat(stalledEntry).as("the entry's own text, where a pid would be").doesNotContain("pid")
            .doesNotContain(String.valueOf(pid)).doesNotContain("x-slow/").doesNotContainPattern("\\d{3,}");
    }

    String statusBody() throws Exception {
        return http.send(TestHttp.request("http://127.0.0.1:" + service.getPort() + "/v1/status").GET().build(),
            HttpResponse.BodyHandlers.ofString()).body();
    }

    // ── a leaf's run archived mid-move: done does not exceed total ──────────────────────────────────────────

    @Test
    void aLeafsRunArchivedAfterTheMoveRan_leavesDoneEqualToTotal_notAboveIt() throws Exception {
        register("am-1", "leafed", 50, 0);
        var w = worker(installed(), null);
        var reporter = reporter(w, DEFAULT_STALL);
        assertThat(w.runCycle()).isEqualTo(1);
        reporter.publish();
        assertThat(entry("am-1").get("done").asLong()).isEqualTo(18L).isEqualTo(entry("am-1").get("total").asLong());

        DataMoveRuns.archive(ctx(), "am-1/globex/leafed_m2_globex");   // a tenant removal: the run leaves both sums
        assertThat(progress("am-1").total()).as("non-vacuity: the raw sum fell").isEqualTo(15L);
        reporter.publish();

        JsonNode e = entry("am-1");
        assertThat(e.get("total").asLong()).isEqualTo(15L);
        assertThat(e.get("done").asLong()).as("the high-water of 18 is clamped to the total").isEqualTo(15L);
    }

    // ── the serving role holds a privilege on the registry ──────────────────────────────────────────────────

    @Test
    void whileNexusSvcHoldsAPrivilegeOnTheRegistry_everyOpenMoveIsBlocked_andProceedsOnceItIsRevoked() throws Exception {
        register("g-one", "tenant_rows", 50, 0);
        var role = DSL.role(DSL.name("nexus_svc"));
        var registryTable = DSL.table(DSL.name("nexus", "data_moves"));
        var installer = installed();   // the boot's own REVOKE has run; the grant below is what a rollback leaves
        ctx().grant(DSL.privilege("SELECT")).on(registryTable).to(role).execute();
        var w = worker(installer, null);
        var reporter = reporter(w, DEFAULT_STALL);
        try {
            assertThat(w.runCycle()).isEqualTo(1);
            reporter.publish();
            assertThat(what("g-one")).startsWith("blocked: g-one: nexus_svc holds a privilege on nexus.data_moves")
                .contains("REVOKE").contains("data_moves FROM nexus_svc");
            assertThat(countWhere("tenant_rows", N.ne(0))).as("nothing moved").isZero();
        } finally {
            ctx().revoke(DSL.privilege("SELECT")).on(registryTable).from(role).execute();
        }

        Thread.sleep(30);   // past the retry wait
        assertThat(w.runCycle()).isEqualTo(1);
        reporter.publish();
        assertThat(what("g-one")).startsWith("runs complete: g-one");
        assertEveryRowMovedOnce("tenant_rows", 1000);
    }
}
