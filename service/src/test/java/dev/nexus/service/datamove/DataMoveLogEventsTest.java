// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import dev.nexus.service.log.SecretMask;
import org.jooq.ConnectionProvider;
import org.jooq.SQLDialect;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.DefaultConfiguration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * nexus-f3yqd.2.18 (RDR-246 Failure Modes, Diagnosis: the engine log's {@code event=data_move_*} lines) on real dml_utils
 * runs: every transition a worker pass makes emits exactly one event, carrying the label, the tenant where the transition has
 * one, and nothing of the move's template, verification or {@code on_complete} text. The log is captured the way
 * {@code ChunkReaperIntegrationTest} captures it: an appender on the root logger.
 */
class DataMoveLogEventsTest extends DataMoveIntegrationBase {

    private static final List<String> TENANTS = List.of("default", "acme", "globex", "orphan", "sl/ash");

    /** These tests build workers directly, not through {@code DataMoveWorker.create}, so the credential is added here. */
    @BeforeAll
    void maskTheAdminPassword() {
        SecretMask.add(ADMIN_PASSWORD);
    }

    /** Every message the {@code datamove} loggers write while {@code body} runs, whatever its event name. */
    private static List<String> captureMessages(Runnable body) {
        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.run();
            return List.copyOf(logs.list).stream().filter(e -> e.getLoggerName().startsWith("dev.nexus.service.datamove"))
                .map(ILoggingEvent::getFormattedMessage).toList();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    /** One captured {@code event=data_move_*} line: its name, its key=value fields, and the whole formatted line. */
    record Event(String level, String name, Map<String, String> fields, String line) {
        String get(String key) {
            return fields.get(key);
        }
    }

    private static final Pattern FIELD = Pattern.compile("(\\w+)=(?:\"([^\"]*)\"|(\\S+))");

    static Event parse(ILoggingEvent e) {
        String line = e.getFormattedMessage();
        Matcher m = FIELD.matcher(line);
        var fields = new LinkedHashMap<String, String>();
        while (m.find()) {
            fields.put(m.group(1), m.group(2) != null ? m.group(2) : m.group(3));
        }
        return new Event(e.getLevel().toString(), fields.get("event"), fields, line);
    }

    /** Every {@code event=data_move_*} line logged while {@code body} runs, in order. */
    private static List<Event> capture(Runnable body) {
        Logger root = (Logger) LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME);
        ListAppender<ILoggingEvent> logs = new ListAppender<>();
        logs.start();
        root.addAppender(logs);
        try {
            body.run();
            return List.copyOf(logs.list).stream().filter(e -> e.getFormattedMessage().contains("event=data_move_"))
                .map(DataMoveLogEventsTest::parse).toList();
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }
    }

    private static List<Event> of(List<Event> events, String name, String label) {
        return events.stream().filter(e -> name.equals(e.name()) && label.equals(e.get("label"))).toList();
    }

    private DataMoveWorker realWorker(MoveFinisher finisher) {
        return worker(installed(), null, new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC(),
            Duration.ofSeconds(3)), finisher, true, Clock.systemUTC());
    }

    /**
     * Distinctive pieces of the fixtures' templates and verifications, and the forms the worker builds from them
     * (the table name put where the template had its placeholder): none may appear in any event line, whole or in part.
     */
    private static final List<String> NEVER_LOGGED = List.of("<chunking_clause>", "<driving_table>", "= t.n + 1",
        "touched_by = current_setting", "maybe_fail", "tenant_rows SET", "tenant_plain SET", "dm_test.tenant_rows t",
        "dm_test.tenant_plain t", "t.n = 0", "t.tenant_id = $1", ADMIN_PASSWORD);

    /**
     * No event line carries any of {@code forbidden} (the move's own texts, and tokens unique to them) or any of
     * {@link #NEVER_LOGGED}. A leaked fragment, or a template with its driving table substituted, is caught by the tokens;
     * the whole text by the text.
     */
    private static void assertCarriesNoMoveText(List<Event> events, String... forbidden) {
        assertThat(events).isNotEmpty();
        for (Event e : events) {
            for (String text : forbidden) {
                assertThat(e.line()).as("event line carries %s", text).doesNotContain(text);
            }
            for (String token : NEVER_LOGGED) {
                assertThat(e.line()).as("event line carries %s", token).doesNotContain(token);
            }
        }
    }

    private static List<String> names(List<Event> events, String label) {
        return events.stream().filter(e -> label.equals(e.get("label"))).map(Event::name).toList();
    }

    private static String resource(String name) {
        return DataMoveTestFixtures.resource(name);
    }

    // ── a clean move ─────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aCleanMove_emitsStartedOnce_aRunCompletePerTenant_andCompletedOnce_inThatOrder() {
        register("le-ok", "tenant_rows", 50, 0);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        assertThat(of(events, "data_move_started", "le-ok")).hasSize(1);
        var runs = of(events, "data_move_run_complete", "le-ok");
        assertThat(runs).extracting(e -> e.get("tenant")).containsExactlyInAnyOrderElementsOf(TENANTS);
        assertThat(runs).extracting(e -> e.get("run")).allSatisfy(r -> assertThat(r).startsWith("le-ok/"));
        var completed = of(events, "data_move_completed", "le-ok");
        assertThat(completed).hasSize(1);
        assertThat(completed.get(0).get("on_complete")).isEqualTo("false");
        assertThat(of(events, "data_move_blocked", "le-ok")).isEmpty();
        assertThat(of(events, "data_move_failed", "le-ok")).isEmpty();
        assertThat(of(events, "data_move_verification_failed", "le-ok")).isEmpty();
        var order = names(events, "le-ok");
        assertThat(order.get(0)).isEqualTo("data_move_started");
        assertThat(order.get(order.size() - 1)).isEqualTo("data_move_completed");
        assertCarriesNoMoveText(events, resource("stamp-template.sql"), resource("verification.sql"));
    }

    @Test
    void aMoveWithNoTenantScope_emitsItsRunCompleteWithNoTenant() {
        register("le-plain", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 60_000, null);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var runs = of(events, "data_move_run_complete", "le-plain");
        assertThat(runs).hasSize(1);
        assertThat(runs.get(0).get("tenant")).isEqualTo("-");
        assertThat(runs.get(0).get("run")).isEqualTo("le-plain");
        assertThat(of(events, "data_move_completed", "le-plain")).hasSize(1);
    }

    @Test
    void aCompletionWithOnComplete_saysSo() {
        register("le-oc", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 60_000,
            "on-complete-chunks-marker.sql");
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var completed = of(events, "data_move_completed", "le-oc");
        assertThat(completed).hasSize(1);
        assertThat(completed.get(0).get("on_complete")).isEqualTo("true");
        assertCarriesNoMoveText(events, resource("stamp-template.sql"), resource("verification-tenant-plain.sql"),
            resource("on-complete-chunks-marker.sql"), "ENABLE ROW LEVEL SECURITY", "ADD COLUMN marker");
    }

    // ── a transient failure ──────────────────────────────────────────────────────────────────────────────

    private void fault(String sqlstate, long fromId) {
        fault(sqlstate, fromId, "");
    }

    /** The fault's {@code message} is appended to the text the chunk raises, so it lands in the recorded chunk error. */
    private void fault(String sqlstate, long fromId, String message) {
        ctx().deleteFrom(table("dm_test", "fault")).execute();
        ctx().insertInto(table("dm_test", "fault"))
            .columns(DSL.field(DSL.name("sqlstate"), String.class), DSL.field(DSL.name("from_id"), Long.class),
                DSL.field(DSL.name("message"), String.class))
            .values(sqlstate, fromId, message).execute();
    }

    @Test
    void aTransientChunkError_emitsOneBlocked_withTheTenant_theSqlstateAndTheRetryCount() {
        DataMoveTestFixtures.register(ctx(), "le-transient", "tenant_rows", 50, 0, null, "fault-template.sql");
        fault("40001", 1);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var blocked = of(events, "data_move_blocked", "le-transient");
        assertThat(blocked).as("one transition, one event").hasSize(1);
        Event b = blocked.get(0);
        assertThat(b.level()).isEqualTo("WARN");
        assertThat(b.get("tenant")).isIn(TENANTS);
        assertThat(b.get("retries")).isEqualTo("1");
        assertThat(b.get("sqlstate")).isEqualTo("40001");
        assertThat(b.get("stage")).isEqualTo("CALL");
        assertThat(b.get("reason")).contains("injected fault");
        assertThat(of(events, "data_move_started", "le-transient")).hasSize(1);
        assertThat(of(events, "data_move_failed", "le-transient")).isEmpty();
        assertThat(of(events, "data_move_completed", "le-transient")).isEmpty();
        assertCarriesNoMoveText(events, resource("fault-template.sql"), resource("verification.sql"));
    }

    @Test
    void aMoveWithNoTenantScope_thatBlocks_emitsBlockedWithNoTenant() {
        DataMoveTestFixtures.register(ctx(), "le-transient-plain", "tenant_plain", 50, 0, null, "fault-template.sql");
        fault("57014", 1);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var blocked = of(events, "data_move_blocked", "le-transient-plain");
        assertThat(blocked).hasSize(1);
        assertThat(blocked.get(0).get("tenant")).isEqualTo("-");
        assertThat(blocked.get(0).get("sqlstate")).isEqualTo("57014");
        assertThat(blocked.get(0).get("retries")).isEqualTo("1");
    }

    // ── a permanent failure ──────────────────────────────────────────────────────────────────────────────

    @Test
    void aPermanentChunkError_emitsOneFailed_withTheLabelTheTenantTheErrorAndTheSqlstate() {
        DataMoveTestFixtures.register(ctx(), "le-failed", "tenant_rows", 50, 0, null, "fault-template.sql");
        fault("22012", 1);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var failed = of(events, "data_move_failed", "le-failed");
        assertThat(failed).as("one transition, one event").hasSize(1);
        Event f = failed.get(0);
        assertThat(f.level()).isEqualTo("ERROR");
        assertThat(f.get("tenant")).isIn(TENANTS);
        assertThat(f.get("sqlstate")).isEqualTo("22012");
        assertThat(f.get("error")).contains("injected fault");
        assertThat(of(events, "data_move_blocked", "le-failed")).isEmpty();
        assertCarriesNoMoveText(events, resource("fault-template.sql"), resource("verification.sql"));

        // a failed move is not retried: a second pass logs nothing about it
        List<Event> again = capture(() -> w.runCycle());
        assertThat(names(again, "le-failed")).isEmpty();
    }

    // ── verification found rows ──────────────────────────────────────────────────────────────────────────

    @Test
    void aVerificationThatFindsRows_emitsTheDetailLine_andOneFailedTransition() {
        register("le-late", "tenant_rows", 50, 0);
        runsOnly("le-late");
        insertOldShapeRow("acme", 5000L);
        insertOldShapeRow("globex", 5001L);
        insertOldShapeRow("globex", 5002L);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var vf = of(events, "data_move_verification_failed", "le-late");
        assertThat(vf).as("one detail line").hasSize(1);
        assertThat(vf.get(0).get("found")).contains("tenant acme=1").contains("tenant globex=2").contains("table-wide=3")
            .doesNotContain("tenant orphan");
        var failed = of(events, "data_move_failed", "le-late");
        assertThat(failed).as("and one transition line").hasSize(1);
        assertThat(failed.get(0).get("stage")).isEqualTo("VERIFICATION_NONEMPTY");
        assertThat(failed.get(0).get("sqlstate")).as("no SQLSTATE: nothing raised").isEqualTo("-");
        assertThat(names(events, "le-late")).as("the detail comes first").containsExactly("data_move_started",
            "data_move_verification_failed", "data_move_failed");
        assertThat(of(events, "data_move_run_complete", "le-late")).as("runs already complete are not a transition").isEmpty();
        assertThat(of(events, "data_move_completed", "le-late")).isEmpty();
        assertCarriesNoMoveText(events, resource("stamp-template.sql"), resource("verification.sql"));
    }

    private void insertOldShapeRow(String tenant, long id) {
        ctx().insertInto(table("dm_test", "tenant_rows")).columns(TENANT_ID, DSL.field(DSL.name("id"), Long.class),
            DSL.field(DSL.name("payload"), String.class)).values(tenant, id, "old-" + id).execute();
    }

    // ── what a finisher or a verification raises carries no statement text ───────────────────────────────

    @Test
    void aVerificationThatRaises_logsItsError_butNotItsText() {
        register("le-writes", "tenant_rows", 50, 0, null, "stamp-template.sql", "verification-writes.sql", 60_000, null);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var failed = of(events, "data_move_failed", "le-writes");
        assertThat(failed).hasSize(1);
        assertThat(failed.get(0).get("sqlstate")).isEqualTo("25006");
        assertThat(failed.get(0).get("tenant")).isIn(TENANTS);
        assertCarriesNoMoveText(events, resource("stamp-template.sql"), resource("verification-writes.sql"), "RETURNING 1",
            "n = 0 RETURNING");
    }

    @Test
    void aVerificationTheWorkerRefuses_logsTheRule_notTheText() {
        register("le-refused", "tenant_rows", 50, 0, null, "stamp-template.sql", "verification-question-mark.sql", 60_000,
            null);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        assertThat(of(events, "data_move_failed", "le-refused")).hasSize(1);
        assertThat(of(events, "data_move_failed", "le-refused").get(0).get("error")).contains("'?'");
        assertCarriesNoMoveText(events, resource("stamp-template.sql"), resource("verification-question-mark.sql"),
            "LIKE '%?%'", "t.payload");
    }

    @Test
    void anOnCompleteThatFails_logsItsError_butNotItsText() {
        register("le-oc-fail", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 60_000,
            "on-complete-partial-failure.sql");
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var failed = of(events, "data_move_failed", "le-oc-fail");
        assertThat(failed).hasSize(1);
        assertThat(failed.get(0).get("sqlstate")).isEqualTo("42704");
        assertThat(of(events, "data_move_completed", "le-oc-fail")).isEmpty();
        assertCarriesNoMoveText(events, resource("stamp-template.sql"), resource("verification-tenant-plain.sql"),
            resource("on-complete-partial-failure.sql"), "ADD COLUMN marker", "VALIDATE CONSTRAINT");
    }

    // ── the finisher's own verdicts ──────────────────────────────────────────────────────────────────────

    /** {@code stage=FINISH} is reachable only from a finisher that answers FAILED or BLOCKED; {@link VerifyingFinisher} never does. */
    @Test
    void aFinisherThatSaysFailed_orBlocked_isOneEventEach_noneIsSilent() {
        register("le-seam-failed", "plain_rows", 50, 0);
        var failing = realWorker(c -> MoveFinisher.Result.failed("the finisher's verdict"));
        List<Event> failedEvents = capture(() -> failing.runCycle());
        assertThat(of(failedEvents, "data_move_failed", "le-seam-failed")).hasSize(1);
        assertThat(of(failedEvents, "data_move_failed", "le-seam-failed").get(0).get("error"))
            .isEqualTo("the finisher's verdict");
        assertThat(of(failedEvents, "data_move_failed", "le-seam-failed").get(0).get("stage")).isEqualTo("FINISH");

        ctx().deleteFrom(table("nexus", "data_moves")).execute();
        register("le-seam-blocked", "plain_rows", 50, 0);
        var blocking = realWorker(c -> MoveFinisher.Result.blocked("the finisher waits"));
        List<Event> blockedEvents = capture(() -> blocking.runCycle());
        assertThat(of(blockedEvents, "data_move_blocked", "le-seam-blocked")).hasSize(1);
        assertThat(of(blockedEvents, "data_move_blocked", "le-seam-blocked").get(0).get("reason"))
            .isEqualTo("the finisher waits");
    }

    // ── a credential in any text a failure carries ───────────────────────────────────────────────────────

    private static final String DSN = "postgresql://nexus_admin:" + ADMIN_PASSWORD + "@db/nexus";

    private String registryColumn(String label, String column) {
        return ctx().select(DSL.field(DSL.name(column), String.class)).from(table("nexus", "data_moves"))
            .where(LABEL.eq(label)).fetchOne(0, String.class);
    }

    /** One pass; every {@code event=data_move_*} line, and every message the datamove loggers wrote. */
    private record Logged(List<Event> events, List<String> messages) {
        void assertNoPassword() {
            assertThat(messages).isNotEmpty().allSatisfy(m -> assertThat(m).doesNotContainIgnoringCase(ADMIN_PASSWORD));
            assertThat(events).isNotEmpty().allSatisfy(e -> assertThat(e.line()).doesNotContainIgnoringCase(ADMIN_PASSWORD));
        }
    }

    private static Logged logged(Runnable body) {
        var events = new ArrayList<Event>();
        List<String> messages = captureMessages(() -> events.addAll(capture(body)));
        return new Logged(events, messages);
    }

    @Test
    void aPermanentChunkErrorThatCarriesThePassword_isRedactedInTheLog_theState_andTheFailureColumn() {
        DataMoveTestFixtures.register(ctx(), "le-chunk-pw", "tenant_plain", 50, 0, null, "fault-template.sql");
        fault("22012", 1, "while connecting with " + DSN);
        var w = realWorker(new VerifyingFinisher());

        Logged logged = logged(() -> assertThat(w.runCycle()).isEqualTo(1));

        logged.assertNoPassword();
        var failed = of(logged.events(), "data_move_failed", "le-chunk-pw");
        assertThat(failed).hasSize(1);
        assertThat(failed.get(0).get("error")).contains("injected fault 22012").contains("<redacted>");
        assertThat(w.states().state("le-chunk-pw").orElseThrow().reason()).contains("<redacted>").doesNotContain(ADMIN_PASSWORD);
        assertThat(registryColumn("le-chunk-pw", "failure")).as("the failure column").contains("<redacted>")
            .doesNotContain(ADMIN_PASSWORD);
    }

    @Test
    void aTransientChunkErrorThatCarriesThePassword_isRedactedInTheLog_theState_andLastTransient() {
        DataMoveTestFixtures.register(ctx(), "le-transient-pw", "tenant_plain", 50, 0, null, "fault-template.sql");
        fault("40001", 1, "while connecting with " + DSN.toUpperCase());   // another case: matched all the same
        var w = realWorker(new VerifyingFinisher());

        Logged logged = logged(() -> assertThat(w.runCycle()).isEqualTo(1));

        logged.assertNoPassword();
        var blocked = of(logged.events(), "data_move_blocked", "le-transient-pw");
        assertThat(blocked).hasSize(1);
        assertThat(blocked.get(0).get("reason")).contains("<redacted>");
        assertThat(w.states().state("le-transient-pw").orElseThrow().reason()).contains("<redacted>")
            .doesNotContainIgnoringCase(ADMIN_PASSWORD);
        assertThat(registryColumn("le-transient-pw", "last_transient")).as("last_transient").contains("<redacted>")
            .doesNotContainIgnoringCase(ADMIN_PASSWORD);
    }

    /** A jOOQ context whose every statement fails to get a connection, with the password in the driver's message. */
    private static org.jooq.DSLContext unreachableRegistry() {
        ConnectionProvider provider = new ConnectionProvider() {
            @Override
            public java.sql.Connection acquire() {
                throw new DataAccessException("connection to jdbc:postgresql://db/nexus?user=nexus_admin&password="
                    + ADMIN_PASSWORD + " was lost");
            }

            @Override
            public void release(java.sql.Connection connection) { /* never acquired */ }
        };
        return DSL.using(new DefaultConfiguration().set(SQLDialect.POSTGRES).set(provider));
    }

    @Test
    void aTransientWhoseRecordWriteThrowsTheDriversMessage_leavesThePasswordNowhere() {
        DataMoveTestFixtures.register(ctx(), "le-poison", "tenant_plain", 50, 0, null, "fault-template.sql");
        fault("40001", 1);
        var real = new DataMoveFailureHandler(adminConnection, WORKER_NAME, Clock.systemUTC(), Duration.ofSeconds(3));
        // the handler reads the run's recorded errors and writes the retry count on the pass's connection: here it is down
        FailureHandler poisoned = f -> real.onFailure(new FailureHandler.Failure(f.move(), f.stage(), f.runLabel(),
            f.drivingSchema(), f.drivingTable(), f.tenant(), f.errorMark(), f.raised(), f.detail(), unreachableRegistry()));
        var w = worker(installed(), null, poisoned, new VerifyingFinisher(), true, Clock.systemUTC());

        Logged logged = logged(() -> assertThat(w.runCycle()).isEqualTo(1));

        logged.assertNoPassword();
        var unrecorded = of(logged.events(), "data_move_failure_unrecorded", "le-poison");
        assertThat(unrecorded).as("the handler's own line, with its label quoted").hasSize(1);
        assertThat(unrecorded.get(0).get("cause")).contains("<redacted>");
        var blocked = of(logged.events(), "data_move_blocked", "le-poison");
        assertThat(blocked).hasSize(1);
        assertThat(blocked.get(0).get("reason")).contains("could not be recorded").contains("<redacted>");
        assertThat(w.states().state("le-poison").orElseThrow().reason()).contains("<redacted>").doesNotContain(ADMIN_PASSWORD);
        assertThat(registryColumn("le-poison", "last_transient")).as("nothing was recorded").isNull();
    }

    // ── a run label with a space ─────────────────────────────────────────────────────────────────────────

    @Test
    void aRunLabelHoldingASpace_staysOneQuotedField() {
        register("le-space", "tenant_rows", 50, 0);
        insertOldShapeRow("sp ace", 7000L);
        var w = realWorker(new VerifyingFinisher());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var runs = of(events, "data_move_run_complete", "le-space");
        assertThat(runs).extracting(e -> e.get("tenant")).contains("sp ace");
        assertThat(runs).extracting(e -> e.get("run")).as("bare, this would stop at the space").contains("le-space/sp ace");
        assertThat(of(events, "data_move_completed", "le-space")).hasSize(1);
    }

    // ── the grammar of a line ────────────────────────────────────────────────────────────────────────────

    @Test
    void aValueHoldingDoubleQuotes_isWrittenWithSingleQuotes_andTheLineKeepsOneFieldPerKey() {
        List<Event> events = capture(() -> MoveEvents.failed("le\"quote", "CALL", "t\"1", "XX000", "he said \"no\""));

        assertThat(events).hasSize(1);
        String line = events.get(0).line();
        assertThat(line).contains("label=\"le'quote\"").contains("tenant=\"t'1\"").contains("error=\"he said 'no'\"");
        assertThat(events.get(0).get("error")).isEqualTo("he said 'no'");
        assertThat(events.get(0).get("tenant")).isEqualTo("t'1");
        assertThat(events.get(0).get("label")).isEqualTo("le'quote");
        assertThat(line.split("error=", -1)).as("one error= field").hasSize(2);
        assertThat(line.chars().filter(c -> c == '"').count()).as("each of label, tenant and error opens and closes once")
            .isEqualTo(6);
    }

    @Test
    void aLabelWithASpace_staysOneField() {
        List<Event> events = capture(() -> MoveEvents.started("a label"));

        assertThat(events.get(0).get("label")).isEqualTo("a label");
    }

    // ── the admin password never reaches a line or the registry ──────────────────────────────────────────

    @Test
    void aConnectionFailureWhoseMessageEchoesTheAdminPassword_isLoggedAndRecordedWithoutIt() {
        register("le-secret", "tenant_plain", 50, 0);
        var w = realWorker(new VerifyingFinisher());
        // a driver that echoes its connection string into the message of a failed open
        w.beforeSessionOpenHook = () -> {
            throw new IllegalStateException("could not connect: jdbc:postgresql://db/nexus?user=nexus_admin&password="
                + ADMIN_PASSWORD);
        };

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        var blocked = of(events, "data_move_blocked", "le-secret");
        assertThat(blocked).hasSize(1);
        assertThat(blocked.get(0).get("stage")).isEqualTo("CONNECTION");
        assertThat(blocked.get(0).get("reason")).contains("could not connect").contains("<redacted>");
        for (Event e : events) {
            assertThat(e.line()).doesNotContain(ADMIN_PASSWORD);
        }
        assertThat(w.states().state("le-secret").orElseThrow().reason()).doesNotContain(ADMIN_PASSWORD);
        String recorded = ctx().select(DSL.field(DSL.name("last_transient"), String.class))
            .from(table("nexus", "data_moves")).where(LABEL.eq("le-secret")).fetchOne(0, String.class);
        assertThat(recorded).as("the registry's last_transient").isNotNull().doesNotContain(ADMIN_PASSWORD);
    }

    // ── started: the transition into working ─────────────────────────────────────────────────────────────

    @Test
    void aMoveAlreadyWorking_emitsNoSecondStarted() {
        register("le-working", "tenant_plain", 50, 0, null, "stamp-template.sql", "verification-tenant-plain.sql", 60_000,
            null);
        var w = realWorker(new VerifyingFinisher());
        w.states().working(DataMoveRegistry.reload(ctx(), "le-working").orElseThrow());

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        assertThat(of(events, "data_move_started", "le-working")).isEmpty();
        assertThat(of(events, "data_move_run_complete", "le-working")).hasSize(1);
        assertThat(of(events, "data_move_completed", "le-working")).hasSize(1);
    }

    @Test
    void aBlockingMoveCycledTwice_logsTwoStartedAndTwoBlocked_withRetries1Then2() {
        var clock = new DataMoveFailureIntegrationTest.MutableClock();
        DataMoveTestFixtures.register(ctx(), "le-twice", "tenant_plain", 50, 0, null, "fault-template.sql");
        fault("40001", 1);
        var w = worker(installed(), null, new DataMoveFailureHandler(adminConnection, WORKER_NAME, clock,
            Duration.ofSeconds(3)), new VerifyingFinisher(), true, clock);

        List<Event> events = capture(() -> {
            assertThat(w.runCycle()).isEqualTo(1);
            clock.advance(Duration.ofMinutes(20));   // past the backoff the first failure set
            assertThat(w.runCycle()).isEqualTo(1);
        });

        // the CALL and grant lines between them are not transitions
        assertThat(names(events, "le-twice").stream().filter(n -> !n.equals("data_move_call") && !n.equals("data_move_grant")))
            .containsExactly("data_move_started", "data_move_blocked", "data_move_started", "data_move_blocked");
        assertThat(of(events, "data_move_blocked", "le-twice")).extracting(e -> e.get("retries")).containsExactly("1", "2");
    }

    // ── a worker that stops ──────────────────────────────────────────────────────────────────────────────

    @Test
    void aStopMidPass_logsPassStopped_andNoBlockedOrFailedEvent() {
        register("le-stop", "plain_rows", 5_000, 0);
        var w = realWorker(new VerifyingFinisher());
        w.afterConnectionOpenedHook = w::stop;

        List<Event> events = capture(() -> assertThat(w.runCycle()).isEqualTo(1));

        assertThat(of(events, "data_move_pass_stopped", "le-stop")).hasSize(1);
        assertThat(of(events, "data_move_blocked", "le-stop")).isEmpty();
        assertThat(of(events, "data_move_failed", "le-stop")).isEmpty();
    }

    // ── the event names other texts cite ─────────────────────────────────────────────────────────────────

    private static final Pattern CITED = Pattern.compile("event=(data_move_[a-z_]*[a-z])\\b");
    private static final Pattern EMITTED = Pattern.compile("log\\.\\w+\\(\"event=(data_move_[a-z_]*[a-z])[\\s\"]");

    private static Path repoRoot() {
        Path p = Path.of("").toAbsolutePath();
        while (p != null && !Files.isDirectory(p.resolve("service/src/main/java"))) {
            p = p.getParent();
        }
        assertThat(p).as("the repository root above the working directory").isNotNull();
        return p;
    }

    @Test
    void everyEventNameTheOperatorTextsCite_isEmittedByTheEngine() throws IOException {
        Path root = repoRoot();
        Path datamove = root.resolve("service/src/main/java/dev/nexus/service/datamove");
        var citing = new LinkedHashMap<String, Path>();
        citing.put("MoveReasons", datamove.resolve("MoveReasons.java"));
        citing.put("MoveBootWork", datamove.resolve("MoveBootWork.java"));
        citing.put("data_moves_health.py", root.resolve("src/nexus/data_moves_health.py"));
        citing.put("engine-service-release.md", root.resolve("docs/agents/engine-service-release.md"));

        // every event name the engine emits: a string "event=<name>" opening a log call
        var emitted = new TreeSet<String>();
        List<Path> sources;
        try (var files = Files.walk(root.resolve("service/src/main/java"))) {
            sources = files.filter(f -> f.toString().endsWith(".java")).toList();
        }
        for (Path f : sources) {
            Matcher m = EMITTED.matcher(Files.readString(f));
            while (m.find()) {
                emitted.add(m.group(1));
            }
        }

        int cited = 0;
        var missing = new ArrayList<String>();
        var perFile = new LinkedHashMap<String, Integer>();
        for (var entry : citing.entrySet()) {
            if (!Files.exists(entry.getValue())) {
                continue;   // the release doc is not in every tree
            }
            Matcher m = CITED.matcher(Files.readString(entry.getValue()));
            int n = 0;
            while (m.find()) {
                n++;
                if (!emitted.contains(m.group(1))) {
                    missing.add(entry.getKey() + " cites event=" + m.group(1));
                }
            }
            perFile.put(entry.getKey(), n);
            cited += n;
        }

        assertThat(missing).as("cited and never emitted (emitted: %s)", emitted).isEmpty();
        // non-vacuity: the scan read the texts that cite events, and the engine emits the events this bead added
        assertThat(perFile.get("MoveReasons")).as("citations read in MoveReasons").isGreaterThanOrEqualTo(1);
        assertThat(perFile.get("MoveBootWork")).as("citations read in MoveBootWork").isGreaterThanOrEqualTo(3);
        assertThat(perFile.get("data_moves_health.py")).as("citations read in data_moves_health.py").isGreaterThanOrEqualTo(4);
        assertThat(cited).as("citations checked").isGreaterThanOrEqualTo(10);
        assertThat(emitted).contains("data_move_started", "data_move_run_complete", "data_move_blocked",
            "data_move_verification_failed", "data_move_failed", "data_move_completed", "data_move_call");
    }
}
