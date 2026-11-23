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
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
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

    private final HttpClient http = HttpClient.newHttpClient();

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

    /** A finisher whose every pass reports one failure of {@code stage} with {@code sqlstate}, as {@code on_complete} would. */
    private static MoveFinisher failing(FailureHandler.Stage stage, String sqlstate) {
        return c -> MoveFinisher.Result.report(new FailureHandler.Failure(c.move(), stage, null, "dm_test", "tenant_plain",
            null, 0, new SQLException("canceling statement due to statement timeout", sqlstate),
            "on_complete of " + c.move().label() + " did not complete: sqlstate=" + sqlstate
                + " canceling statement due to statement timeout", c.admin()));
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

    /** The registry's list after one sample of {@code w}'s states, and the real status body that list is served in. */
    private JsonNode status(DataMoveWorker w) throws Exception {
        return status(w.states());
    }

    private JsonNode status(MoveStates states) throws Exception {
        var registry = new BootWorkRegistry(clock);
        var reporter = new MoveBootWork(registry, states, adminConnection, WORKER_NAME + "-bootwork", clock,
            Duration.ofMinutes(15));
        try {
            assertThat(reporter.publish()).as("the registry was read").isNotNegative();
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

    // ── 3 consecutive on_complete lock or statement timeouts ─────────────────────────────────────────────────

    @Test
    void threeOnCompleteStatementTimeouts_enterTheState_withOneErrorEventAndAStatusEntry() throws Exception {
        registerMove("no-fast", CHUNK_FAMILY_ON_COMPLETE);
        registerMove("no-control", CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new AtomicReference<MoveFinisher>(failing(FailureHandler.Stage.ON_COMPLETE, "57014"));
        // the control fails once and then completes: below any threshold
        var controlCalls = new java.util.concurrent.atomic.AtomicInteger();
        var w = workerOn(c -> "no-control".equals(c.move().label())
            ? (controlCalls.getAndIncrement() == 0 ? failing(FailureHandler.Stage.ON_COMPLETE, "57014").finish(c)
                : MoveFinisher.Result.completed())
            : finisher.get().finish(c));

        List<Event> first = capture(() -> assertThat(w.runCycle()).isEqualTo(2));
        assertThat(first).as("one transient: no page").isEmpty();
        assertThat(retries("no-fast")).isEqualTo(1);
        cycle(w);
        assertThat(retries("no-fast")).isEqualTo(2);
        JsonNode atTwo = status(w);
        assertThat(atTwo.get("data_moves_need_operator").asBoolean()).as("two timeouts: not yet").isFalse();
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
        assertThat(status.get("data_moves_need_operator").asBoolean()).isTrue();
        assertThat(labels(status)).containsExactly("no-fast");
        JsonNode m = entry(status, "no-fast");
        assertThat(m.get("retries").asInt()).isEqualTo(3);
        assertThat(m.get("sqlstate").asText()).isEqualTo("57014");
        assertThat(m.get("statement_timeout_ms").asInt()).isEqualTo(1_000);
        assertThat(m.get("terminates_index_builds").asBoolean()).isTrue();
        var lastAt = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC).toInstant();
        assertThat(m.get("last_attempt_at").asText()).isEqualTo(lastAt.toString());
        assertThat(m.get("next_retry_at").asText()).as("last attempt plus the engine's backoff for 3 retries")
            .isEqualTo(lastAt.plus(MoveRetryPolicy.backoff(3)).toString());

        assertThat(capture(() -> {
            cycle(w);
            cycle(w);
        })).as("the 4th and 5th retry do not page again").isEmpty();
        assertThat(status(w).get("data_moves_need_operator").asBoolean()).as("it stays while it keeps failing").isTrue();
        assertThat(states(w, "no-control")).as("the control completed and left no state").isNull();
        assertThat(entry(status(w), "no-control")).as("the control move is never listed").isNull();
    }

    private static MoveStates.State states(DataMoveWorker w, String label) {
        return w.states().state(label).orElse(null);
    }

    @Test
    void twoOnCompleteStatementTimeouts_doNotEnterTheState() throws Exception {
        registerMove("no-two", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(failing(FailureHandler.Stage.ON_COMPLETE, "57014"));

        List<Event> events = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            cycle(w);
        });

        assertThat(retries("no-two")).isEqualTo(2);
        assertThat(events).isEmpty();
        assertThat(w.states().state("no-two").orElseThrow().needsOperator()).isFalse();
        assertThat(status(w).get("data_moves_need_operator").asBoolean()).isFalse();
    }

    @Test
    void threeOnCompleteLockTimeouts_behaveLikeStatementTimeouts() throws Exception {
        registerMove("no-lock", CHUNK_FAMILY_ON_COMPLETE);
        var w = workerOn(failing(FailureHandler.Stage.ON_COMPLETE, "55P03"));

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
        assertThat(entry(status, "no-lock").get("sqlstate").asText()).isEqualTo("55P03");
    }

    @Test
    void anOnCompleteRunOnAMoveOutsideTheChunkFamily_pagesToo_butDoesNotClaimItEndsIndexBuilds() throws Exception {
        registerMove("no-plain", null);
        var w = workerOn(failing(FailureHandler.Stage.ON_COMPLETE, "57014"));

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
        var finisher = new AtomicReference<MoveFinisher>(failing(FailureHandler.Stage.ON_COMPLETE, "57014"));
        var w = workerOn(c -> finisher.get().finish(c));
        var events = new ArrayList<Event>();

        events.addAll(capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);          // retry 1: on_complete 57014
            cycle(w);                                        // retry 2: on_complete 57014
            finisher.set(failing(FailureHandler.Stage.VERIFICATION, "57014"));
            cycle(w);                                        // retry 3: another stage breaks the run
            finisher.set(failing(FailureHandler.Stage.ON_COMPLETE, "40001"));
            cycle(w);                                        // retry 4: another SQLSTATE at on_complete is not a bound either
            finisher.set(failing(FailureHandler.Stage.ON_COMPLETE, "57014"));
            cycle(w);                                        // retry 5: run of 1
            cycle(w);                                        // retry 6: run of 2
        }));
        assertThat(retries("no-mixed")).isEqualTo(6);
        assertThat(events).as("six transients, never three bound ones in a row").isEmpty();
        assertThat(status(w).get("data_moves_need_operator").asBoolean()).isFalse();

        events.addAll(capture(() -> cycle(w)));              // retry 7: run of 3
        assertThat(events).hasSize(1);
        assertThat(events.get(0).get("retries")).isEqualTo("7");
        assertThat(labels(status(w))).containsExactly("no-mixed");
    }

    @Test
    void theFlagClears_whenTheMoveCompletes() throws Exception {
        registerMove("no-clear", CHUNK_FAMILY_ON_COMPLETE);
        var finisher = new AtomicReference<MoveFinisher>(failing(FailureHandler.Stage.ON_COMPLETE, "57014"));
        var w = workerOn(c -> finisher.get().finish(c));
        assertThat(w.runCycle()).isEqualTo(1);
        cycle(w);
        cycle(w);
        assertThat(labels(status(w))).containsExactly("no-clear");

        finisher.set(c -> MoveFinisher.Result.completed());
        cycle(w);

        assertThat(retries("no-clear")).as("a pass that ends clean resets the count").isZero();
        JsonNode status = status(w);
        assertThat(status.get("data_moves_need_operator").asBoolean()).isFalse();
        assertThat(labels(status)).isEmpty();
        assertThat(status.get("data_moves_need_operator_moves")).isEmpty();
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
        assertThat(status(w).get("data_moves_need_operator").asBoolean()).isFalse();
        assertThat(w.states().state("no-slow-verify").orElseThrow().reason()).doesNotContain("needs an operator");

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
        assertThat(e.get("terminates_index_builds")).isEqualTo("false");
        assertThat(w.states().state("no-slow-verify").orElseThrow().reason()).endsWith("; needs an operator");

        JsonNode status = status(w);
        assertThat(status.get("data_moves_need_operator").asBoolean()).isTrue();
        assertThat(labels(status)).containsExactly("no-slow-verify");
        assertThat(entry(status, "no-slow-verify").get("retries").asInt()).isEqualTo(20);
        assertThat(entry(status, "no-slow-verify").get("terminates_index_builds").asBoolean()).isFalse();

        assertThat(capture(() -> cycle(w))).as("the 21st retry does not page again").isEmpty();
        assertThat(retries("no-slow-verify")).isEqualTo(21);
        assertThat(status(w).get("data_moves_need_operator").asBoolean()).isTrue();
    }

    @Test
    void aTwentyRetryRowIsListedAfterARestart_becauseTheRowHoldsTheCount() throws Exception {
        registerMove("no-restart", null);
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("transient_retries"), Integer.class), 20)
            .set(DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class),
                OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC))
            .set(DSL.field(DSL.name("last_transient"), String.class), "on_complete of no-restart did not complete: sqlstate=57014 x")
            .where(LABEL.eq("no-restart")).execute();

        JsonNode status = status(new MoveStates(clock));   // a process that has seen nothing of the move

        assertThat(labels(status)).containsExactly("no-restart");
        assertThat(entry(status, "no-restart").get("sqlstate").asText()).isEqualTo("57014");
    }

    @Test
    void theRealOnCompleteTimeout_namesItsStepAndBound_atTheTwentiethTransient() throws Exception {
        register("no-real", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 1_000,
            "on-complete-slow.sql");
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("transient_retries"), Integer.class), 19)
            .set(DSL.field(DSL.name("last_transient_at"), OffsetDateTime.class),
                OffsetDateTime.ofInstant(clock.instant().minus(Duration.ofHours(1)), ZoneOffset.UTC))
            .set(DSL.field(DSL.name("last_transient"), String.class), "an earlier transient").where(LABEL.eq("no-real")).execute();
        var w = workerOn(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        assertThat(retries("no-real")).isEqualTo(20);
        assertThat(events).hasSize(1);
        assertThat(events.get(0).get("step")).isEqualTo("on_complete");
        assertThat(events.get(0).get("sqlstate")).isEqualTo("57014");
        assertThat(events.get(0).get("bound_ms")).isEqualTo("1000");
        assertThat(entry(status(w), "no-real").get("sqlstate").asText()).isEqualTo("57014");
    }
}
