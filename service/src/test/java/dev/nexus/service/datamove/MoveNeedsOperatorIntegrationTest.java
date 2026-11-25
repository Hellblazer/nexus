// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import dev.nexus.service.BootWorkRegistry;
import dev.nexus.service.TestHttp;
import dev.nexus.service.http.StatusHandler;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * RDR-246 (nexus-p2m1x.22): a move that keeps failing transiently pages a human, on real PG, through the real worker, failure
 * handler, registry rows, {@link MoveBootWork} sampler and {@code GET /v1/status}. The state is entered at 20 consecutive
 * transients, or at 3 consecutive lock or statement timeouts at {@code on_complete} ({@link MoveRetryPolicy#needsOperator}); on
 * entering it the engine logs one ERROR {@code event=data_move_needs_operator}, and the status lists the move until it
 * completes or its retries reset. A clock the test moves stands in for the hours the backoff would take; a finisher the test
 * writes raises what {@code on_complete} would, so the loop is not bounded by a real statement timeout. The real finisher's
 * step and bound are in {@link #theRealOnCompleteTimeout_namesItsStepAndBound_atTheTwentiethTransient}.
 */
class MoveNeedsOperatorIntegrationTest extends DataMoveIntegrationBase {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Pattern FIELD = Pattern.compile("(\\w+)=(?:\"([^\"]*)\"|(\\S+))");
    private static final String CHUNK_FAMILY_ON_COMPLETE = "on-complete-chunks.sql";
    private static final Duration PAST_ANY_BACKOFF = Duration.ofMinutes(16);

    private final HttpClient http = TestHttp.client();

    /** One {@code event=data_move_needs_operator} line. */
    record Event(String level, Map<String, String> fields) {
        String get(String key) {
            return fields.get(key);
        }
    }

    private static List<Event> capture(Runnable body) {
        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.run();
            List<Event> out = new ArrayList<>();
            for (ILoggingEvent e : List.copyOf(logs.list)) {
                String line = e.getFormattedMessage();
                if (!line.contains("event=data_move_needs_operator")) {
                    continue;
                }
                Matcher m = FIELD.matcher(line);
                var fields = new LinkedHashMap<String, String>();
                while (m.find()) {
                    fields.put(m.group(1), m.group(2) != null ? m.group(2) : m.group(3));
                }
                out.add(new Event(e.getLevel().toString(), fields));
            }
            return out;
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    /**
     * A finisher whose every pass reports one failure of {@code stage} with {@code sqlstate}, as {@code on_complete} would.
     * {@code buildsEnded} is what the real finisher reports: true once it has ended the index builds, which it does for a chunk-family
     * {@code on_complete} that got past the completion UPDATE and the re-check.
     */
    private static MoveFinisher failing(FailureHandler.Stage stage, String sqlstate, boolean buildsEnded) {
        return c -> MoveFinisher.Result.report(new FailureHandler.Failure(c.move(), stage, null, "dm_test", "tenant_plain",
            null, 0, new SQLException("canceling statement due to statement timeout", sqlstate),
            "on_complete of " + c.move().label() + " did not complete: sqlstate=" + sqlstate
                + " canceling statement due to statement timeout", c.admin(), buildsEnded));
    }

    private static MoveFinisher failing(FailureHandler.Stage stage, String sqlstate) {
        return failing(stage, sqlstate, false);
    }

    /** {@link #failing} at {@code on_complete}, over a chunk-family move: the finisher ended the builds. */
    private static MoveFinisher onCompleteFailing(String sqlstate) {
        return failing(FailureHandler.Stage.ON_COMPLETE, sqlstate, true);
    }

    private void registerMove(String label, String onCompleteResource) {
        register(label, "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 1_000,
            onCompleteResource);
    }

    private DataMoveFailureIntegrationTest.MutableClock clock = new DataMoveFailureIntegrationTest.MutableClock();

    private DataMoveWorker workerOn(MoveFinisher finisher) throws Exception {
        return worker(installed(), null,
            new DataMoveFailureHandler(adminConnection, WORKER_NAME, clock, Duration.ofMillis(50)), finisher, true, clock);
    }

    /** One pass over the open moves after the backoff has run out. */
    private void cycle(DataMoveWorker w) {
        clock.advance(PAST_ANY_BACKOFF);
        assertThat(w.runCycle()).as("the move is due and worked").isPositive();
    }

    private int retries(String label) {
        return ctx().select(DSL.field(DSL.name("transient_retries"), Integer.class)).from(table("nexus", "data_moves"))
            .where(LABEL.eq(label)).fetchOne(0, Integer.class);
    }

    /** The pages the sampler logged on the last {@link #status} call. */
    private List<Event> sampled = List.of();

    /** The real status body after one sample of {@code w}'s states, which must page nothing (the worker did, or nothing needs it). */
    private JsonNode status(DataMoveWorker w) throws Exception {
        JsonNode body = status(w.states());
        assertThat(sampled).as("the sampler logs no page the worker already logged").isEmpty();
        return body;
    }

    /** The real status body after one sample over {@code states}; the pages the sampler logged are in {@link #sampled}. */
    private JsonNode status(MoveStates states) throws Exception {
        var registry = new BootWorkRegistry(clock);
        var reporter = new MoveBootWork(registry, states, adminConnection, WORKER_NAME + "-bootwork", clock,
            Duration.ofMinutes(15));
        try {
            sampled = capture(() -> assertThat(reporter.publish()).as("the registry was read").isNotNegative());
            HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/v1/status", new StatusHandler(null, null).bootWork(registry::snapshot)
                .dataMovesNeedOperator(registry::dataMovesNeedOperator));
            server.start();
            try {
                HttpResponse<String> resp = http.send(
                    TestHttp.request("http://127.0.0.1:" + server.getAddress().getPort() + "/v1/status").GET().build(),
                    HttpResponse.BodyHandlers.ofString());
                assertThat(resp.statusCode()).isEqualTo(200);
                return MAPPER.readTree(resp.body());
            } finally {
                server.stop(0);
            }
        } finally {
            reporter.close();
        }
    }

    private static List<String> labels(JsonNode status) {
        var out = new ArrayList<String>();
        status.get("data_moves_need_operator_labels").forEach(n -> out.add(n.asText()));
        return out;
    }

    private static JsonNode entry(JsonNode status, String label) {
        for (JsonNode m : status.get("data_moves_need_operator_moves")) {
            if (label.equals(m.get("label").asText())) {
                return m;
            }
        }
        return null;
    }

    private static boolean flag(JsonNode status) {
        return status.get("data_moves_need_operator").asBoolean();
    }

    private void resetRow(String label) {
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("transient_retries"), Integer.class), 0)
            .set(DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class), (OffsetDateTime) null)
            .set(DSL.field(DSL.name("last_transient"), String.class), (String) null).where(LABEL.eq(label)).execute();
    }

    private void seedRow(String label, int retries, String lastTransient) {
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("transient_retries"), Integer.class), retries)
            .set(DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class),
                OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC))
            .set(DSL.field(DSL.name("last_transient"), String.class), lastTransient).where(LABEL.eq(label)).execute();
    }

    private static MoveStates.State stateOf(DataMoveWorker w, String label) {
        return w.states().state(label).orElse(null);
    }

    // ── 3 consecutive on_complete lock or statement timeouts ─────────────────────────────────────────────────

    @Test
    void threeOnCompleteStatementTimeouts_enterTheState_withOneErrorEventAndAStatusEntry() throws Exception {
        registerMove("no-fast", CHUNK_FAMILY_ON_COMPLETE);
        registerMove("no-control", CHUNK_FAMILY_ON_COMPLETE);
        var controlCalls = new AtomicInteger();
        // the control fails once and then completes: below any threshold
        var w = workerOn(c -> "no-control".equals(c.move().label())
            ? (controlCalls.getAndIncrement() == 0 ? onCompleteFailing("57014").finish(c) : MoveFinisher.Result.completed())
            : onCompleteFailing("57014").finish(c));

        List<Event> first = capture(() -> assertThat(w.runCycle()).isEqualTo(2));
        assertThat(first).as("one transient: no page").isEmpty();
        assertThat(retries("no-fast")).isEqualTo(1);
        cycle(w);
        assertThat(retries("no-fast")).isEqualTo(2);
        JsonNode atTwo = status(w);
        assertThat(flag(atTwo)).as("two timeouts: not yet").isFalse();
        assertThat(labels(atTwo)).isEmpty();

        List<Event> third = capture(() -> cycle(w));
        assertThat(retries("no-fast")).isEqualTo(3);
        assertThat(third).as("the third consecutive on_complete timeout pages, once").hasSize(1);
        Event e = third.get(0);
        assertThat(e.level()).isEqualTo("ERROR");
        assertThat(e.get("label")).isEqualTo("no-fast");
        assertThat(e.get("step")).isEqualTo("on_complete");
        assertThat(e.get("sqlstate")).isEqualTo("57014");
        assertThat(e.get("bound_ms")).as("the completion's statement_timeout_ms").isEqualTo("1000");
        assertThat(e.get("retries")).isEqualTo("3");
        assertThat(e.get("terminates_index_builds")).isEqualTo("true");
        assertThat(e.get("reason")).contains("index builds");

        JsonNode status = status(w);
        assertThat(flag(status)).isTrue();
        assertThat(labels(status)).containsExactly("no-fast");
        JsonNode m = entry(status, "no-fast");
        assertThat(m.get("retries").asInt()).isEqualTo(3);
        assertThat(m.get("sqlstate").asText()).isEqualTo("57014");
        assertThat(m.get("statement_timeout_ms").asInt()).isEqualTo(1_000);
        assertThat(m.get("terminates_index_builds").asBoolean()).isTrue();
        assertThat(m.get("step").asText()).isEqualTo("on_complete");
        assertThat(m.get("bound_ms").asInt()).isEqualTo(1_000);
        assertThat(m.get("retry_in_progress").asBoolean()).as("between passes").isFalse();
        var lastAt = clock.instant();
        assertThat(m.get("last_attempt_at").asText()).isEqualTo(lastAt.toString());
        assertThat(m.get("next_retry_at").asText()).as("last attempt plus the engine's backoff for 3 retries")
            .isEqualTo(lastAt.plus(MoveRetryPolicy.backoff(3)).toString());

        assertThat(capture(() -> {
            cycle(w);
            cycle(w);
        })).as("the 4th and 5th retry do not page again").isEmpty();
        assertThat(flag(status(w))).as("it stays while it keeps failing").isTrue();
        assertThat(stateOf(w, "no-control")).as("the control completed and left no state").isNull();
        assertThat(entry(status(w), "no-control")).as("the control move is never listed").isNull();
    }

    @Test
    void twoOnCompleteStatementTimeouts_doNotEnterTheState() throws Exception {
        registerMove("no-two", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(onCompleteFailing("57014"));

        List<Event> events = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
        });

        assertThat(retries("no-two")).isEqualTo(2);
        assertThat(events).isEmpty();
        assertThat(stateOf(w, "no-two").needsOperator()).isFalse();
        assertThat(flag(status(w))).isFalse();
    }

    @Test
    void threeOnCompleteLockTimeouts_behaveLikeStatementTimeouts_andShowTheirOwnBound() throws Exception {
        registerMove("no-lock", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(onCompleteFailing("55P03"));

        List<Event> events = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
            cycle(w);
        });

        assertThat(events).hasSize(1);
        assertThat(events.get(0).get("sqlstate")).isEqualTo("55P03");
        assertThat(events.get(0).get("step")).isEqualTo("on_complete");
        assertThat(events.get(0).get("bound_ms")).as("the lock timeout is half of statement_timeout_ms").isEqualTo("500");
        assertThat(events.get(0).get("terminates_index_builds")).isEqualTo("true");
        JsonNode status = status(w);
        assertThat(labels(status)).containsExactly("no-lock");
        JsonNode m = entry(status, "no-lock");
        assertThat(m.get("sqlstate").asText()).isEqualTo("55P03");
        assertThat(m.get("bound_ms").asInt()).as("the lock timeout, not statement_timeout_ms").isEqualTo(500);
        assertThat(m.get("statement_timeout_ms").asInt()).isEqualTo(1_000);
        assertThat(m.get("step").asText()).isEqualTo("on_complete");
    }

    @Test
    void anOnCompleteRunOnAMoveOutsideTheChunkFamily_pagesToo_butTheFinisherEndedNoIndexBuild() throws Exception {
        registerMove("no-plain", null);
        var w = workerOn(failing(FailureHandler.Stage.ON_COMPLETE, "57014", false));   // what the real finisher reports there

        List<Event> events = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
            cycle(w);
        });

        assertThat(events).hasSize(1);
        assertThat(events.get(0).get("terminates_index_builds")).as("nothing to end: the move is not over the chunk family")
            .isEqualTo("false");
        assertThat(events.get(0).get("reason")).doesNotContain("index builds");
        assertThat(entry(status(w), "no-plain").get("terminates_index_builds").asBoolean()).isFalse();
    }

    @Test
    void aMixedRun_isNotAnOnCompleteRun_andNeedsThreeInARowAfterTheBreak() throws Exception {
        registerMove("no-mixed", CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new AtomicReference<MoveFinisher>(onCompleteFailing("57014"));
        var w = workerOn(c -> finisher.get().finish(c));
        var events = new ArrayList<Event>();

        events.addAll(capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);          // retry 1: on_complete 57014
            cycle(w);                                        // retry 2: on_complete 57014
            finisher.set(failing(FailureHandler.Stage.VERIFICATION, "57014"));
            cycle(w);                                        // retry 3: another stage breaks the run
            finisher.set(onCompleteFailing("40001"));
            cycle(w);                                        // retry 4: another SQLSTATE at on_complete is not a bound either
            finisher.set(onCompleteFailing("57014"));
            cycle(w);                                        // retry 5: run of 1
            cycle(w);                                        // retry 6: run of 2
        }));
        assertThat(retries("no-mixed")).isEqualTo(6);
        assertThat(events).as("six transients, never three bound ones in a row").isEmpty();
        assertThat(flag(status(w))).isFalse();

        events.addAll(capture(() -> cycle(w)));              // retry 7: run of 3
        assertThat(events).hasSize(1);
        assertThat(events.get(0).get("retries")).isEqualTo("7");
        assertThat(labels(status(w))).containsExactly("no-mixed");
    }

    // ── the latch ────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void thePageLatches_anotherFailureAndThreeMoreTimeoutsNeitherClearItNorPageAgain() throws Exception {
        registerMove("no-latch", CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new AtomicReference<MoveFinisher>(onCompleteFailing("57014"));
        var w = workerOn(c -> finisher.get().finish(c));
        assertThat(capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
            cycle(w);
        })).as("the page at 3").hasSize(1);
        assertThat(flag(status(w))).isTrue();

        finisher.set(onCompleteFailing("40001"));            // retry 4: a failure that is not a timeout
        List<Event> other = capture(() -> cycle(w));

        assertThat(other).as("no second ERROR").isEmpty();
        assertThat(retries("no-latch")).isEqualTo(4);
        JsonNode afterOther = status(w);
        assertThat(flag(afterOther)).as("the flag stays while the move is still failing").isTrue();
        assertThat(entry(afterOther, "no-latch").get("step").asText()).isEqualTo("on_complete");
        assertThat(entry(afterOther, "no-latch").get("bound_ms").isNull()).as("40001 missed no bound").isTrue();
        assertThat(stateOf(w, "no-latch").reason()).endsWith("; needs an operator");

        finisher.set(onCompleteFailing("57014"));            // retries 5, 6, 7: three timeouts in a row again
        List<Event> again = capture(() -> {
            cycle(w);
            cycle(w);
            cycle(w);
        });

        assertThat(retries("no-latch")).isEqualTo(7);
        assertThat(again).as("three more timeouts do not page a second time").isEmpty();
        assertThat(flag(status(w))).isTrue();
    }

    @Test
    void aResetOfTheRowCount_clearsTheLatchedPage_whetherTheNextPassFailsOrLeavesTheMoveOpen() throws Exception {
        registerMove("no-reset", CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new AtomicReference<MoveFinisher>(onCompleteFailing("57014"));
        var w = workerOn(c -> finisher.get().finish(c));
        assertThat(w.runCycle()).isEqualTo(1);
        cycle(w);
        cycle(w);
        assertThat(flag(status(w))).isTrue();

        resetRow("no-reset");                                // an operator's retry-now UPDATE
        finisher.set(onCompleteFailing("57014"));
        List<Event> next = capture(() -> cycle(w));

        assertThat(retries("no-reset")).as("the count started over").isEqualTo(1);
        assertThat(next).isEmpty();
        assertThat(flag(status(w))).as("the latch did not carry across the reset").isFalse();

        cycle(w);
        cycle(w);                                            // 2 and 3 after the reset: a fresh run of three pages again
        assertThat(flag(status(w))).isTrue();

        resetRow("no-reset");
        finisher.set(c -> MoveFinisher.Result.leftOpen());   // a clean pass that leaves the move open (a verification waits)
        cycle(w);

        assertThat(retries("no-reset")).isZero();
        assertThat(stateOf(w, "no-reset").kind()).isEqualTo(MoveStates.Kind.RUNS_COMPLETE);
        assertThat(flag(status(w))).as("cleared by a clean pass that leaves the move open").isFalse();
    }

    @Test
    void theFlagClears_whenTheMoveCompletes() throws Exception {
        registerMove("no-clear", CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new AtomicReference<MoveFinisher>(onCompleteFailing("57014"));
        var w = workerOn(c -> finisher.get().finish(c));
        assertThat(w.runCycle()).isEqualTo(1);
        cycle(w);
        cycle(w);
        assertThat(labels(status(w))).containsExactly("no-clear");

        finisher.set(c -> MoveFinisher.Result.completed());
        cycle(w);

        assertThat(retries("no-clear")).as("a pass that ends clean resets the count").isZero();
        JsonNode status = status(w);
        assertThat(flag(status)).isFalse();
        assertThat(labels(status)).isEmpty();
        assertThat(status.get("data_moves_need_operator_moves")).isEmpty();
    }

    @Test
    void aRetryInProgress_isListedAsSuch() throws Exception {
        registerMove("no-working", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(onCompleteFailing("57014"));
        assertThat(w.runCycle()).isEqualTo(1);
        cycle(w);
        cycle(w);

        w.states().working(DataMoveRegistry.reload(ctx(), "no-working").orElseThrow());   // the retry pass has begun

        JsonNode m = entry(status(w), "no-working");
        assertThat(m.get("retry_in_progress").asBoolean()).isTrue();
        assertThat(m.get("step").asText()).as("the page is kept while the retry runs").isEqualTo("on_complete");
        assertThat(m.get("terminates_index_builds").asBoolean()).isTrue();
    }

    // ── 20 consecutive transients, any stage ─────────────────────────────────────────────────────────────

    @Test
    void threeStatementTimeoutsAtAnotherStage_keepThe20Rule_andPageExactlyOnceAtTheTwentieth() throws Exception {
        registerMove("no-slow-verify", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(failing(FailureHandler.Stage.VERIFICATION, "57014"));

        List<Event> early = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            for (int i = 2; i <= 19; i++) {
                cycle(w);
            }
        });
        assertThat(retries("no-slow-verify")).isEqualTo(19);
        assertThat(early).as("not at 3, and not at 19").isEmpty();
        assertThat(flag(status(w))).isFalse();
        assertThat(stateOf(w, "no-slow-verify").reason()).doesNotContain("needs an operator");

        List<Event> twentieth = capture(() -> cycle(w));
        assertThat(retries("no-slow-verify")).isEqualTo(20);
        assertThat(twentieth).hasSize(1);
        Event e = twentieth.get(0);
        assertThat(e.level()).isEqualTo("ERROR");
        assertThat(e.get("label")).isEqualTo("no-slow-verify");
        assertThat(e.get("step")).isEqualTo("verification");
        assertThat(e.get("sqlstate")).isEqualTo("57014");
        assertThat(e.get("bound_ms")).as("verify_statement_timeout_ms").isEqualTo("60000");
        assertThat(e.get("retries")).isEqualTo("20");
        assertThat(e.get("terminates_index_builds")).as("a verification ends no build, even on a chunk-family move")
            .isEqualTo("false");
        assertThat(stateOf(w, "no-slow-verify").reason()).endsWith("; needs an operator");

        JsonNode status = status(w);
        assertThat(flag(status)).isTrue();
        assertThat(labels(status)).containsExactly("no-slow-verify");
        JsonNode m = entry(status, "no-slow-verify");
        assertThat(m.get("retries").asInt()).isEqualTo(20);
        assertThat(m.get("terminates_index_builds").asBoolean()).isFalse();
        assertThat(m.get("step").asText()).isEqualTo("verification");
        assertThat(m.get("bound_ms").asInt()).as("not statement_timeout_ms").isEqualTo(60_000);

        assertThat(capture(() -> cycle(w))).as("the 21st retry does not page again").isEmpty();
        assertThat(retries("no-slow-verify")).isEqualTo(21);
        assertThat(flag(status(w))).isTrue();
    }

    @Test
    void aChunkFamilyOnCompleteWithANonTimeoutSqlstate_keepsThe20Rule_butSaysItEndsIndexBuilds() throws Exception {
        registerMove("no-serial", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(onCompleteFailing("40001"));

        List<Event> early = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            for (int i = 2; i <= 19; i++) {
                cycle(w);
            }
        });
        assertThat(early).as("40001 is not a timeout: not at 3").isEmpty();
        assertThat(flag(status(w))).isFalse();

        List<Event> twentieth = capture(() -> cycle(w));

        assertThat(twentieth).hasSize(1);
        assertThat(twentieth.get(0).get("step")).isEqualTo("on_complete");
        assertThat(twentieth.get(0).get("sqlstate")).isEqualTo("40001");
        assertThat(twentieth.get(0).get("bound_ms")).as("no timeout, no bound").isEqualTo("-");
        assertThat(twentieth.get(0).get("terminates_index_builds")).as("the attempt did end the builds").isEqualTo("true");
        JsonNode m = entry(status(w), "no-serial");
        assertThat(m.get("terminates_index_builds").asBoolean()).isTrue();
        assertThat(m.get("bound_ms").isNull()).isTrue();
        assertThat(m.get("step").asText()).isEqualTo("on_complete");
    }

    // ── a move already in the state when the engine starts ───────────────────────────────────────────────

    @Test
    void aMoveAlreadyAtTwentyAtBoot_isListedAndPagedOnceByTheSampler_notAgainOnLaterSamples() throws Exception {
        registerMove("no-restart", CHUNK_FAMILY_ON_COMPLETE);
        seedRow("no-restart", 20, "on_complete of no-restart did not complete: sqlstate=57014 x");
        var states = new MoveStates(clock);   // a process that has seen nothing of the move

        JsonNode first = status(states);

        assertThat(labels(first)).containsExactly("no-restart");
        JsonNode m = entry(first, "no-restart");
        assertThat(m.get("sqlstate").asText()).isEqualTo("57014");
        assertThat(m.get("step").isNull()).as("this process has seen no failure of it").isTrue();
        assertThat(m.get("terminates_index_builds").asBoolean()).as("by the move's definition").isTrue();
        assertThat(sampled).as("the ERROR the worker's transition never logs for a move that was already there").hasSize(1);
        assertThat(sampled.get(0).level()).isEqualTo("ERROR");
        assertThat(sampled.get(0).get("label")).isEqualTo("no-restart");
        assertThat(sampled.get(0).get("retries")).isEqualTo("20");
        assertThat(sampled.get(0).get("sqlstate")).isEqualTo("57014");

        status(states);
        assertThat(sampled).as("not on a later sample").isEmpty();
        status(states);
        assertThat(sampled).isEmpty();
    }

    @Test
    void aPageTheWorkerLogged_isNotLoggedAgainBySamplesAfterIt() throws Exception {
        registerMove("no-once", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(onCompleteFailing("57014"));
        List<Event> worker = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
            cycle(w);
        });
        assertThat(worker).hasSize(1);

        status(w.states());
        assertThat(sampled).as("the sampler shares the worker's announce set").isEmpty();
        status(w.states());
        assertThat(sampled).isEmpty();
    }

    // ── the real finisher ────────────────────────────────────────────────────────────────────────────────

    @Test
    void theRealOnCompleteTimeout_namesItsStepAndBound_atTheTwentiethTransient() throws Exception {
        register("no-real", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 1_000,
            "on-complete-slow.sql");
        seedRow("no-real", 19, "an earlier transient");
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class),
            OffsetDateTime.ofInstant(clock.instant().minus(Duration.ofHours(1)), ZoneOffset.UTC)).where(LABEL.eq("no-real"))
            .execute();
        var w = workerOn(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        assertThat(retries("no-real")).isEqualTo(20);
        assertThat(events).hasSize(1);
        assertThat(events.get(0).get("step")).isEqualTo("on_complete");
        assertThat(events.get(0).get("sqlstate")).isEqualTo("57014");
        assertThat(events.get(0).get("bound_ms")).isEqualTo("1000");
        assertThat(events.get(0).get("terminates_index_builds")).as("a move outside the chunk family ends no build")
            .isEqualTo("false");
        assertThat(entry(status(w), "no-real").get("sqlstate").asText()).isEqualTo("57014");
    }

    @Test
    void threeRealOnCompleteTimeoutsOnAChunkFamilyMove_latchAndSayTheAttemptEndedIndexBuilds() throws Exception {
        register("no-real-chunks", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 1_000,
            "on-complete-chunks-slow.sql");
        var w = workerOn(new VerifyingFinisher());

        List<Event> events = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
            cycle(w);
        });

        assertThat(retries("no-real-chunks")).isEqualTo(3);
        assertThat(events).as("the real finisher's three 57014s page once").hasSize(1);
        assertThat(events.get(0).get("step")).isEqualTo("on_complete");
        assertThat(events.get(0).get("sqlstate")).isEqualTo("57014");
        assertThat(events.get(0).get("bound_ms")).isEqualTo("1000");
        assertThat(events.get(0).get("terminates_index_builds")).as("the finisher ran terminateOrThrow").isEqualTo("true");
        JsonNode m = entry(status(w), "no-real-chunks");
        assertThat(m.get("terminates_index_builds").asBoolean()).isTrue();
        assertThat(m.get("bound_ms").asInt()).isEqualTo(1_000);
    }

    // ── the keys the Python doctor reads ─────────────────────────────────────────────────────────────────

    /** The status body's keys and value types are those of the fixture the doctor's tests read, so the languages cannot drift. */
    @Test
    void theStatusBody_hasTheKeysAndTypesOfTheFixtureThePythonDoctorReads() throws Exception {
        registerMove("no-shape", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(onCompleteFailing("55P03"));
        assertThat(w.runCycle()).isEqualTo(1);
        cycle(w);
        cycle(w);
        JsonNode real = status(w);
        JsonNode fixture = MAPPER.readTree(Files.readString(repoRoot()
            .resolve("tests/fixtures/data_moves_need_operator_status/paged.json")));

        assertThat(shape(real.get("data_moves_need_operator"))).isEqualTo(shape(fixture.get("data_moves_need_operator")));
        assertThat(shape(real.get("data_moves_need_operator_labels").get(0)))
            .isEqualTo(shape(fixture.get("data_moves_need_operator_labels").get(0)));
        JsonNode r = real.get("data_moves_need_operator_moves").get(0);
        JsonNode f = fixture.get("data_moves_need_operator_moves").get(0);
        var realKeys = new ArrayList<String>();
        r.fieldNames().forEachRemaining(realKeys::add);
        var fixtureKeys = new ArrayList<String>();
        f.fieldNames().forEachRemaining(fixtureKeys::add);
        assertThat(realKeys).containsExactlyInAnyOrderElementsOf(fixtureKeys);
        for (String key : fixtureKeys) {
            assertThat(shape(r.get(key))).as("the type of " + key).isEqualTo(shape(f.get(key)));
        }
    }

    // ── nexus-p2m1x.27 row 23: the name-key drop move waits for index builds; a wait is not a failure, a page or a mystery ──

    private static JsonNode bootWorkEntry(JsonNode status, String label) {
        for (JsonNode e : status.get("boot_work")) {
            if (e.get("id").asText().equals(MoveBootWork.ID_PREFIX + label)) {
                return e;
            }
        }
        throw new AssertionError("no boot_work entry for " + label + " in " + status.get("boot_work"));
    }

    /** The drop move's label is fixed, so an earlier test's finished run would be resumed; archive it, as the other label-reusing tests do. */
    private void withNoEarlierRunOf(String label) {
        for (String run : activeRuns(label)) {
            DataMoveRuns.archive(ctx(), run);
        }
        assertThat(activeRuns(label)).as("no run of %s is left from an earlier test", label).isEmpty();
    }

    @Test
    void theDropMoveWhileItWaitsForABuild_countsNothingPagesNoOne_andReadsAsAWait_thenOverdue_thenCompletes() throws Exception {
        withNoEarlierRunOf(NameKeyDropMove.LABEL);
        registerMove(NameKeyDropMove.LABEL, CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new VerifyingFinisher();
        finisher.buildWait.clock = clock;
        var w = workerOn(finisher);

        try (InFlightIndexBuild build = InFlightIndexBuild.start(this, "row23-worker", "nexus-pci-builder-row23-w", "row23_worker_idx")) {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
            cycle(w);

            assertThat(retries(NameKeyDropMove.LABEL)).as("repeated deferrals count no failure").isZero();
            assertThat(ctx().select(DSL.field(DSL.name("last_transient"), String.class)).from(table("nexus", "data_moves"))
                .where(LABEL.eq(NameKeyDropMove.LABEL)).fetchOne(0, String.class)).as("and write no failure text").isNull();
            assertThat(stateOf(w, NameKeyDropMove.LABEL).kind()).isEqualTo(MoveStates.Kind.BLOCKED);
            assertThat(stateOf(w, NameKeyDropMove.LABEL).reason()).startsWith(BuildWait.REASON_PREFIX);
            JsonNode status = status(w);
            assertThat(flag(status)).as("no page").isFalse();
            assertThat(labels(status)).isEmpty();
            assertThat(status.get("data_moves_need_operator_moves")).isEmpty();
            JsonNode entry = bootWorkEntry(status, NameKeyDropMove.LABEL);
            assertThat(entry.get("what").asText()).startsWith("blocked: " + NameKeyDropMove.LABEL
                + ": waiting for in-flight index builds to finish (1 running)").doesNotContain("engine log").doesNotContain("overdue");
            assertThat(entry.get("reason_class").asText()).isEqualTo("waiting_for_builds");

            clock.advance(Duration.ofHours(7));   // the same wait, now past six hours
            cycle(w);

            status = status(w);
            entry = bootWorkEntry(status, NameKeyDropMove.LABEL);
            assertThat(entry.get("what").asText()).contains("(1 running)").contains("overdue, more than 6 h").contains("builder pids ")
                .containsPattern("waiting for 7 h \\d+ m");
            assertThat(entry.get("reason_class").asText()).isEqualTo("waiting_for_builds_overdue");
            assertThat(retries(NameKeyDropMove.LABEL)).as("still no count").isZero();
            assertThat(flag(status)).as("still no page").isFalse();
        }

        cycle(w);   // the build has finished: the drop completes, and the run of deferrals ends
        assertThat(completedAt(NameKeyDropMove.LABEL)).isNotNull();
        assertThat(finisher.buildWait.age(NameKeyDropMove.LABEL)).isNull();
    }

    @Test
    void aLockTimeoutOnTheDropWithNoBuildRunning_isCountedAsOnAnyMove_notWaitedOut() throws Exception {
        withNoEarlierRunOf(NameKeyDropMove.LABEL);
        registerMove(NameKeyDropMove.LABEL, CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(new VerifyingFinisher());
        var props = new Properties();
        props.setProperty("user", ADMIN);
        props.setProperty("password", ADMIN_PASSWORD);
        props.setProperty("ApplicationName", "nexus-pci-builder-row23-holder");   // builder-named, and building nothing
        try (Connection holder = DriverManager.getConnection(pg.getJdbcUrl(), props)) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.one()).from(table("nexus", "chunks")).forShare().fetch();

            assertThat(w.runCycle()).isEqualTo(1);

            assertThat(retries(NameKeyDropMove.LABEL)).as("a 55P03 with no build in flight is a counted transient").isEqualTo(1);
            assertThat(stateOf(w, NameKeyDropMove.LABEL).reason()).doesNotStartWith(BuildWait.REASON_PREFIX);
            holder.rollback();
        }
    }

    @Test
    void aDropMoveLatchedByThreeCountedLockTimeouts_thenDeferredOnABuild_keepsItsPage_andLogsNoSecondOne() throws Exception {
        withNoEarlierRunOf(NameKeyDropMove.LABEL);
        registerMove(NameKeyDropMove.LABEL, CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new VerifyingFinisher();
        finisher.buildWait.clock = clock;
        var w = workerOn(finisher);
        var props = new Properties();
        props.setProperty("user", ADMIN);
        props.setProperty("password", ADMIN_PASSWORD);
        props.setProperty("ApplicationName", "nexus-pci-builder-row23-page-holder");   // builder-named, and building nothing
        List<Event> latching;
        try (Connection holder = DriverManager.getConnection(pg.getJdbcUrl(), props)) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.one()).from(table("nexus", "chunks")).forShare().fetch();
            latching = capture(() -> {
                assertThat(w.runCycle()).isEqualTo(1);   // three counted 55P03s at on_complete: the early rule latches
                cycle(w);
                cycle(w);
            });
            holder.rollback();
        }
        assertThat(retries(NameKeyDropMove.LABEL)).as("three counted lock timeouts").isEqualTo(3);
        assertThat(latching).as("the third pages once").hasSize(1);
        JsonNode before = status(w);
        assertThat(labels(before)).as("non-vacuity: the latched move is listed before the deferral").containsExactly(NameKeyDropMove.LABEL);
        JsonNode pagedBefore = entry(before, NameKeyDropMove.LABEL);
        assertThat(pagedBefore.get("step").asText()).isEqualTo("on_complete");

        try (InFlightIndexBuild build = InFlightIndexBuild.start(this, "row23-page", "nexus-pci-builder-row23-page", "row23_page_idx")) {
            List<Event> deferring = capture(() -> cycle(w));   // the drop now finds a build in flight: a deferral

            assertThat(stateOf(w, NameKeyDropMove.LABEL).kind()).isEqualTo(MoveStates.Kind.BLOCKED);
            assertThat(stateOf(w, NameKeyDropMove.LABEL).reason()).as("a deferral").startsWith(BuildWait.REASON_PREFIX);
            assertThat(retries(NameKeyDropMove.LABEL)).as("a deferral counts and clears nothing").isEqualTo(3);
            assertThat(deferring).as("no second data_move_needs_operator line from the worker").isEmpty();

            JsonNode after = status(w);   // and none from the sampler: status() asserts it logged no page
            assertThat(flag(after)).as("/v1/status still pages the move").isTrue();
            assertThat(labels(after)).containsExactly(NameKeyDropMove.LABEL);
            JsonNode m = entry(after, NameKeyDropMove.LABEL);
            assertThat(m.get("retries").asInt()).isEqualTo(3);
            assertThat(m.get("step").asText()).as("the page keeps the step of the failure that latched it").isEqualTo("on_complete");
            assertThat(m.get("bound_ms").asInt()).isEqualTo(pagedBefore.get("bound_ms").asInt());
            assertThat(m.get("terminates_index_builds").asBoolean()).isEqualTo(pagedBefore.get("terminates_index_builds").asBoolean());
            assertThat(bootWorkEntry(after, NameKeyDropMove.LABEL).get("reason_class").asText())
                .as("the same entry reads as a wait").isEqualTo("waiting_for_builds");
        }
    }

    private static String shape(JsonNode n) {
        return n.getNodeType().toString();
    }

    private static Path repoRoot() {
        Path p = Path.of("").toAbsolutePath();
        while (p != null && !Files.isDirectory(p.resolve("service/src/main/java"))) {
            p = p.getParent();
        }
        assertThat(p).as("the repository root above the working directory").isNotNull();
        return p;
    }
}
