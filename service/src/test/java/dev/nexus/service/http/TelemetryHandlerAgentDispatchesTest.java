// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpContext;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpPrincipal;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.TelemetryRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-mtheb.4 — the per-dispatch T1 put counter: {@code POST /v1/telemetry/agent_dispatches/record} writes
 * one row per (session, agent id) with the number of T1 puts the dispatch made, and
 * {@code GET /v1/telemetry/agent_dispatches/list} reads the rows back with a summary of how many dispatches
 * wrote nothing. The SubagentStop hook is the caller; its payload is session_id, agent_id, agent_type,
 * t1_puts, ts.
 *
 * <p>Drives {@link TelemetryHandler#handle} against a real Postgres: a handler that parsed the count and
 * dropped it before the repository, or a table without the constraint the upsert names, would pass any
 * validation-only test. Model: {@link TelemetryHandlerOutOfStepCostTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TelemetryHandlerAgentDispatchesTest {

    private static final String SVC_ROLE = "svc_tel_agent_dispatches_test";
    private static final String SVC_PASS = "svc_tel_agent_dispatches_test_pass";
    private static final String TENANT   = "tel-agent-dispatches-tenant";
    private static final String OTHER    = "tel-agent-dispatches-other-tenant";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TelemetryHandler handler;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        handler = new TelemetryHandler(new TelemetryRepository(new TenantScope(svcDs)));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── record + list round trip ─────────────────────────────────────────────

    @Test
    void recordThenListReturnsTheDispatchWithItsPutCount() throws Exception {
        String sid = sid("roundtrip");
        Reply r = record(TENANT, sid, "agent-a", "developer", 3, "2026-11-01T12:00:00Z");
        assertThat(r.status).as(r.body).isEqualTo(200);

        Listing l = list(TENANT, "session_id=" + sid);
        assertThat(l.rows).hasSize(1);
        Map<String, Object> row = l.rows.get(0);
        assertThat(row.get("session_id")).isEqualTo(sid);
        assertThat(row.get("agent_id")).isEqualTo("agent-a");
        assertThat(row.get("agent_type")).isEqualTo("developer");
        assertThat(((Number) row.get("t1_puts")).intValue()).isEqualTo(3);
        assertThat((String) row.get("ts")).startsWith("2026-11-01T12:00:00");
        assertThat(l.total).isEqualTo(1);
    }

    @Test
    void aDispatchThatWroteNothingIsAZeroRowNotAMissingOne() throws Exception {
        String sid = sid("zero");
        assertThat(record(TENANT, sid, "agent-z", "code-review-expert", 0, null).status).isEqualTo(200);
        assertThat(record(TENANT, sid, "agent-y", "developer", 2, null).status).isEqualTo(200);

        Listing l = list(TENANT, "session_id=" + sid);
        assertThat(l.rows).as("both dispatches are on record, the silent one included").hasSize(2);
        assertThat(l.summary.get("dispatches")).isEqualTo(2);
        assertThat(l.summary.get("no_t1_put")).as("the zero-put dispatch is counted as a miss").isEqualTo(1);
        assertThat(((Number) l.summary.get("t1_puts")).intValue()).isEqualTo(2);
    }

    @Test
    void aSecondRecordForTheSameDispatchUpdatesTheRowAndNeverLowersTheCount() throws Exception {
        // SubagentStop fires again after the nudge turn: same session and agent id, a later total.
        String sid = sid("upsert");
        assertThat(record(TENANT, sid, "agent-u", "developer", 0, "2026-11-01T12:00:00Z").status).isEqualTo(200);
        assertThat(record(TENANT, sid, "agent-u", "developer", 4, "2026-11-01T12:05:00Z").status).isEqualTo(200);

        Listing after = list(TENANT, "session_id=" + sid);
        assertThat(after.rows).as("one dispatch is one row, however many stops it fired").hasSize(1);
        assertThat(((Number) after.rows.get(0).get("t1_puts")).intValue()).isEqualTo(4);
        assertThat((String) after.rows.get(0).get("ts")).as("the later stop's time").startsWith("2026-11-01T12:05:00");

        // A re-scan can only see the same transcript or a longer one; a lower figure is never newer.
        assertThat(record(TENANT, sid, "agent-u", "developer", 1, "2026-11-01T12:01:00Z").status).isEqualTo(200);
        Listing replay = list(TENANT, "session_id=" + sid);
        assertThat(replay.rows).hasSize(1);
        assertThat(((Number) replay.rows.get(0).get("t1_puts")).intValue()).isEqualTo(4);
        assertThat((String) replay.rows.get(0).get("ts")).startsWith("2026-11-01T12:05:00");
    }

    @Test
    void theSameAgentIdInTwoSessionsIsTwoDispatches() throws Exception {
        String s1 = sid("two-a");
        String s2 = sid("two-b");
        record(TENANT, s1, "shared-agent-id", "developer", 1, null);
        record(TENANT, s2, "shared-agent-id", "developer", 5, null);
        assertThat(list(TENANT, "session_id=" + s1).rows).hasSize(1);
        assertThat(list(TENANT, "session_id=" + s2).rows).hasSize(1);
    }

    @Test
    void agentTypeIsOptionalAndTsDefaultsToNow() throws Exception {
        String sid = sid("optional");
        Reply r = post("/agent_dispatches/record",
            "{\"session_id\":\"" + sid + "\",\"agent_id\":\"agent-o\",\"t1_puts\":1}", TENANT);
        assertThat(r.status).as(r.body).isEqualTo(200);
        Map<String, Object> row = list(TENANT, "session_id=" + sid).rows.get(0);
        assertThat(row).containsKey("agent_type");
        assertThat(row.get("agent_type")).isNull();
        assertThat(row.get("ts")).as("a missing ts is stamped by the engine").isNotNull();
    }

    @Test
    void aBlankAgentTypeNeverReplacesAStoredTypeAndIsNeverStored() throws Exception {
        String sid = sid("blank-type");
        record(TENANT, sid, "b-1", "developer", 1, null);
        record(TENANT, sid, "b-1", "", 2, null);    // the nudge-turn stop of a harness that gives no type
        record(TENANT, sid, "b-2", "   ", 0, null); // a blank on the first record

        Map<String, Map<String, Object>> byId = new java.util.HashMap<>();
        for (Map<String, Object> row : list(TENANT, "session_id=" + sid).rows) {
            byId.put((String) row.get("agent_id"), row);
        }
        assertThat(byId.get("b-1").get("agent_type")).as("a stored type survives a blank one").isEqualTo("developer");
        assertThat(((Number) byId.get("b-1").get("t1_puts")).intValue()).as("the count still updates").isEqualTo(2);
        assertThat(byId.get("b-2").get("agent_type")).as("a blank type is stored as no type").isNull();
        assertThat(ids(list(TENANT, "session_id=" + sid + "&agent_type=developer"))).containsExactly("b-1");
    }

    // ── ts: malformed is a 400, far-future is clamped (nexus-mtheb.7) ────────

    @Test
    void aMalformedTsIsA400NamingTheFieldAndWritesNothing() throws Exception {
        String sid = sid("badts");
        // not ISO, a bad calendar value, a bare date, a naive datetime (no offset), a JSON number
        for (String bad : List.of("\"yesterday\"", "\"2026-13-45T00:00:00Z\"", "\"2026-11-01\"",
                "\"2026-11-01T12:00:00\"", "1730000000")) {
            Reply r = post("/agent_dispatches/record",
                "{\"session_id\":\"" + sid + "\",\"agent_id\":\"a\",\"t1_puts\":1,\"ts\":" + bad + "}", TENANT);
            assertThat(r.status).as("ts=%s: %s", bad, r.body).isEqualTo(400);
            assertThat(r.body).as("the 400 names the field").contains("Field 'ts'");
        }
        assertThat(list(TENANT, "session_id=" + sid).rows).as("a rejected ts writes nothing").isEmpty();
    }

    @Test
    void aMalformedTsOnARepeatNeverMovesTheStoredTimeOrCount() throws Exception {
        // The old behaviour stamped now() for an unparseable ts, and GREATEST(ts) then moved the stored
        // time forward on a record that carried no usable time at all.
        String sid = sid("badts-repeat");
        assertThat(record(TENANT, sid, "r-1", "developer", 1, "2026-01-01T00:00:00Z").status).isEqualTo(200);
        Reply bad = record(TENANT, sid, "r-1", "developer", 9, "not-a-time");
        assertThat(bad.status).isEqualTo(400);
        Map<String, Object> row = list(TENANT, "session_id=" + sid).rows.get(0);
        assertThat((String) row.get("ts")).startsWith("2026-01-01T00:00:00");
        assertThat(((Number) row.get("t1_puts")).intValue()).isEqualTo(1);
    }

    @Test
    void aBlankTsIsAbsentNotMalformed() throws Exception {
        String sid = sid("blankts");
        Reply r = post("/agent_dispatches/record",
            "{\"session_id\":\"" + sid + "\",\"agent_id\":\"a\",\"t1_puts\":1,\"ts\":\"  \"}", TENANT);
        assertThat(r.status).as(r.body).isEqualTo(200);
        assertThat(list(TENANT, "session_id=" + sid).rows.get(0).get("ts")).isNotNull();
    }

    @Test
    void aTsMoreThanFiveMinutesAheadIsClampedToNowSoItCannotPinTheRow() throws Exception {
        String sid = sid("future");
        java.time.OffsetDateTime before = java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC);
        // a day ahead, and an offset form of ten years ahead: both are skew, not time
        assertThat(record(TENANT, sid, "fut-1", "developer", 1, before.plusDays(1).toString()).status).isEqualTo(200);
        assertThat(record(TENANT, sid, "fut-2", "developer", 1,
            before.plusYears(10).withOffsetSameInstant(java.time.ZoneOffset.ofHours(5)).toString()).status).isEqualTo(200);
        java.time.OffsetDateTime after = java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC);

        for (Map<String, Object> row : list(TENANT, "session_id=" + sid).rows) {
            java.time.OffsetDateTime stored = java.time.OffsetDateTime.parse((String) row.get("ts"));
            assertThat(stored).as("%s is stamped when it arrived, not with the skewed clock", row.get("agent_id"))
                .isAfterOrEqualTo(before.minusSeconds(1)).isBeforeOrEqualTo(after.plusSeconds(1));
        }

        // GREATEST(ts) keeps the later of two stops. Unclamped, the far-future time would win for ever.
        assertThat(record(TENANT, sid, "fut-1", "developer", 2, null).status).isEqualTo(200);
        Map<String, Object> row = list(TENANT, "session_id=" + sid + "&agent_type=developer").rows.stream()
            .filter(m -> "fut-1".equals(m.get("agent_id"))).findFirst().orElseThrow();
        assertThat(java.time.OffsetDateTime.parse((String) row.get("ts")))
            .as("a later honest stop moves the time forward from now, not from a day ahead")
            .isBeforeOrEqualTo(java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).plusSeconds(1));
        assertThat(((Number) row.get("t1_puts")).intValue()).isEqualTo(2);
    }

    @Test
    void aTsWithinFiveMinutesAheadIsKeptAsGiven() throws Exception {
        String sid = sid("skew-ok");
        java.time.OffsetDateTime ahead = java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC)
            .plusMinutes(2).truncatedTo(java.time.temporal.ChronoUnit.SECONDS);
        assertThat(record(TENANT, sid, "ok-1", "developer", 1, ahead.toString()).status).isEqualTo(200);
        java.time.OffsetDateTime stored = java.time.OffsetDateTime.parse(
            (String) list(TENANT, "session_id=" + sid).rows.get(0).get("ts"));
        assertThat(stored.toInstant()).as("ordinary clock skew is not touched").isEqualTo(ahead.toInstant());
    }

    // ── filters, caps, tenants ───────────────────────────────────────────────

    @Test
    void listFiltersByAgentTypeAndSince() throws Exception {
        String sid = sid("filters");
        record(TENANT, sid, "f-old", "developer", 1, "2026-01-01T00:00:00Z");
        record(TENANT, sid, "f-new", "developer", 2, "2026-11-01T00:00:00Z");
        record(TENANT, sid, "f-crit", "substantive-critic", 0, "2026-11-01T00:00:00Z");

        assertThat(ids(list(TENANT, "session_id=" + sid + "&agent_type=substantive-critic")))
            .containsExactly("f-crit");
        assertThat(ids(list(TENANT, "session_id=" + sid + "&since=2026-06-01T00:00:00Z")))
            .containsExactlyInAnyOrder("f-new", "f-crit");
        assertThat(ids(list(TENANT, "session_id=" + sid))).hasSize(3);
    }

    @Test
    void limitCapsTheRowsButNotTheTotalOrTheSummary() throws Exception {
        String sid = sid("limit");
        for (int i = 0; i < 5; i++) {
            record(TENANT, sid, "l-" + i, "developer", i, "2026-11-01T00:0" + i + ":00Z");
        }
        Listing l = list(TENANT, "session_id=" + sid + "&limit=2");
        assertThat(l.rows).hasSize(2);
        assertThat(l.rows.get(0).get("agent_id")).as("newest first").isEqualTo("l-4");
        assertThat(l.total).isEqualTo(5);
        assertThat(l.summary.get("dispatches")).isEqualTo(5);
        assertThat(l.summary.get("no_t1_put")).isEqualTo(1);
        assertThat(((Number) l.summary.get("t1_puts")).intValue()).isEqualTo(0 + 1 + 2 + 3 + 4);
    }

    @Test
    void aTenantNeverSeesAnotherTenantsDispatches() throws Exception {
        String sid = sid("tenants");
        record(TENANT, sid, "t-mine", "developer", 1, null);
        record(OTHER, sid, "t-theirs", "developer", 9, null);

        assertThat(ids(list(TENANT, "session_id=" + sid))).containsExactly("t-mine");
        assertThat(ids(list(OTHER, "session_id=" + sid))).containsExactly("t-theirs");
    }

    // ── one snapshot for rows and summary (nexus-mtheb.8) ────────────────────

    @Test
    void aRecordLandingDuringTheListCannotMakeTheSummaryDisagreeWithTheRows() throws Exception {
        String sid = sid("snapshot");
        for (int i = 0; i < 5; i++) {
            record(TENANT, sid, "snap-" + i, "developer", i, "2026-11-01T00:0" + i + ":00Z");
        }
        // A dispatch is recorded on another connection the moment the list's FIRST statement against
        // agent_dispatches has run. With rows and summary in two statements the second one sees it
        // (rows 6, total 5); in one statement there is nothing after the first to see it.
        TelemetryRepository direct = new TelemetryRepository(new TenantScope(svcDs));
        java.util.concurrent.atomic.AtomicInteger fired = new java.util.concurrent.atomic.AtomicInteger();
        javax.sql.DataSource racing = afterFirstDispatchSelect(svcDs, () -> {
            fired.incrementAndGet();
            direct.recordAgentDispatch(TENANT, sid, "snap-late", "developer", null, 7);
        });

        Map<String, Object> out = new TelemetryRepository(new TenantScope(racing))
            .listAgentDispatches(TENANT, sid, "", "", 1000);

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) out.get("rows");
        @SuppressWarnings("unchecked")
        Map<String, Object> summary = (Map<String, Object>) out.get("summary");
        assertThat(fired.get()).as("the concurrent record really ran during the call").isEqualTo(1);
        assertThat(rows).as("an unlimited listing and its total are one snapshot").hasSize(((Number) out.get("total")).intValue());
        assertThat(summary.get("dispatches")).isEqualTo(rows.size());
        assertThat(summary.get("no_t1_put")).isEqualTo((int) rows.stream()
            .filter(m -> ((Number) m.get("t1_puts")).intValue() == 0).count());
        assertThat(((Number) summary.get("t1_puts")).longValue()).isEqualTo(rows.stream()
            .mapToLong(m -> ((Number) m.get("t1_puts")).longValue()).sum());

        // the next call sees the late record: it was written, just not part of that snapshot
        Listing next = list(TENANT, "session_id=" + sid);
        assertThat(next.rows).hasSize(rows.size() + 1);
        assertThat(next.total).isEqualTo(rows.size() + 1);
    }

    @Test
    void anEmptyFilterGivesNoRowsAndAZeroSummary() throws Exception {
        Listing l = list(TENANT, "session_id=" + sid("nothing-here"));
        assertThat(l.rows).isEmpty();
        assertThat(l.total).isZero();
        assertThat(l.summary.get("dispatches")).isEqualTo(0);
        assertThat(l.summary.get("no_t1_put")).isEqualTo(0);
        assertThat(((Number) l.summary.get("t1_puts")).intValue()).isZero();
    }

    // ── validation ───────────────────────────────────────────────────────────

    @Test
    void recordRejectsAMissingSessionAgentOrCountAndWritesNothing() throws Exception {
        String sid = sid("invalid");
        for (String bad : List.of(
                "{\"agent_id\":\"a\",\"t1_puts\":1}",
                "{\"session_id\":\"" + sid + "\",\"t1_puts\":1}",
                "{\"session_id\":\"" + sid + "\",\"agent_id\":\"a\"}",
                "{\"session_id\":\"" + sid + "\",\"agent_id\":\"a\",\"t1_puts\":null}")) {
            Reply r = post("/agent_dispatches/record", bad, TENANT);
            assertThat(r.status).as("%s -> %s", bad, r.body).isEqualTo(400);
        }
        assertThat(list(TENANT, "session_id=" + sid).rows).as("a rejected body writes nothing").isEmpty();
    }

    @Test
    void recordRejectsANegativeOrNonNumericCountAndWritesNothing() throws Exception {
        String sid = sid("badcount");
        for (String bad : List.of("-1", "\"many\"", "true", "[1]", "{\"a\":1}", "1.5")) {
            Reply r = post("/agent_dispatches/record",
                "{\"session_id\":\"" + sid + "\",\"agent_id\":\"a\",\"t1_puts\":" + bad + "}", TENANT);
            assertThat(r.status).as("t1_puts=%s: %s", bad, r.body).isEqualTo(400);
            assertThat(r.body).contains("t1_puts");
        }
        assertThat(list(TENANT, "session_id=" + sid).rows).isEmpty();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /**
     * A DataSource whose connections run {@code hook} once, right after the first SELECT against
     * agent_dispatches has executed on them (any PreparedStatement execute*). Lets a test land a write
     * between the statements of one repository call without sleeping or racing threads.
     */
    private static javax.sql.DataSource afterFirstDispatchSelect(javax.sql.DataSource real, Runnable hook) {
        java.util.concurrent.atomic.AtomicBoolean done = new java.util.concurrent.atomic.AtomicBoolean();
        ClassLoader cl = TelemetryHandlerAgentDispatchesTest.class.getClassLoader();
        return (javax.sql.DataSource) java.lang.reflect.Proxy.newProxyInstance(cl,
            new Class<?>[] {javax.sql.DataSource.class}, (p, m, a) -> {
                Object r = invokeUnwrapped(real, m, a);
                if (!m.getName().equals("getConnection")) return r;
                Connection conn = (Connection) r;
                return java.lang.reflect.Proxy.newProxyInstance(cl, new Class<?>[] {Connection.class}, (cp, cm, ca) -> {
                    Object cr = invokeUnwrapped(conn, cm, ca);
                    if (!cm.getName().equals("prepareStatement")) return cr;
                    String sql = String.valueOf(ca[0]).toLowerCase(java.util.Locale.ROOT);
                    boolean dispatchSelect = sql.startsWith("select") && sql.contains("agent_dispatches");
                    return java.lang.reflect.Proxy.newProxyInstance(cl,
                        new Class<?>[] {java.sql.PreparedStatement.class}, (sp, sm, sa) -> {
                            Object sr = invokeUnwrapped(cr, sm, sa);
                            if (dispatchSelect && sm.getName().startsWith("execute") && done.compareAndSet(false, true)) {
                                hook.run();
                            }
                            return sr;
                        });
                });
            });
    }

    private static Object invokeUnwrapped(Object target, java.lang.reflect.Method m, Object[] args) throws Throwable {
        try {
            return m.invoke(target, args);
        } catch (java.lang.reflect.InvocationTargetException e) {
            throw e.getCause();
        }
    }

    private static String sid(String tag) {
        return "disp-" + tag + "-" + System.nanoTime();
    }

    private record Reply(int status, String body) {}

    private record Listing(List<Map<String, Object>> rows, int total, Map<String, Object> summary) {}

    private Reply record(String tenant, String sid, String agentId, String agentType, int puts, String ts)
            throws Exception {
        String body = "{\"session_id\":\"" + sid + "\",\"agent_id\":\"" + agentId + "\",\"agent_type\":\""
            + agentType + "\",\"t1_puts\":" + puts + (ts == null ? "" : ",\"ts\":\"" + ts + "\"") + "}";
        return post("/agent_dispatches/record", body, tenant);
    }

    @SuppressWarnings("unchecked")
    private Listing list(String tenant, String query) throws Exception {
        Reply r = call("GET", "/agent_dispatches/list?" + query, "", tenant);
        assertThat(r.status).as(r.body).isEqualTo(200);
        Map<String, Object> m = MAPPER.readValue(r.body, Map.class);
        return new Listing((List<Map<String, Object>>) m.get("rows"), ((Number) m.get("total")).intValue(),
            (Map<String, Object>) m.get("summary"));
    }

    private static List<String> ids(Listing l) {
        return l.rows.stream().map(m -> (String) m.get("agent_id")).toList();
    }

    private Reply post(String route, String json, String tenant) throws Exception {
        return call("POST", route, json, tenant);
    }

    private Reply call(String method, String route, String body, String tenant) throws Exception {
        CapturingExchange ex = new CapturingExchange(method, URI.create("/v1/telemetry" + route), body);
        RequestContext.set(new RequestContext.Principal(tenant, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Reply(ex.status, ex.bodyString());
    }

    /** Minimal {@link HttpExchange} that captures the response status and body. */
    private static final class CapturingExchange extends HttpExchange {
        private final String method;
        private final URI uri;
        private final byte[] requestBody;
        private final Headers responseHeaders = new Headers();
        private final ByteArrayOutputStream responseBody = new ByteArrayOutputStream();
        int status = -1;

        CapturingExchange(String method, URI uri, String body) {
            this.method = method;
            this.uri = uri;
            this.requestBody = body.getBytes(StandardCharsets.UTF_8);
        }

        String bodyString() { return responseBody.toString(StandardCharsets.UTF_8); }

        @Override public Headers getRequestHeaders() { return new Headers(); }
        @Override public Headers getResponseHeaders() { return responseHeaders; }
        @Override public URI getRequestURI() { return uri; }
        @Override public String getRequestMethod() { return method; }
        @Override public HttpContext getHttpContext() { return null; }
        @Override public void close() {}
        @Override public InputStream getRequestBody() { return new ByteArrayInputStream(requestBody); }
        @Override public OutputStream getResponseBody() { return responseBody; }
        @Override public void sendResponseHeaders(int rCode, long responseLength) { this.status = rCode; }
        @Override public InetSocketAddress getRemoteAddress() { return null; }
        @Override public int getResponseCode() { return status; }
        @Override public InetSocketAddress getLocalAddress() { return null; }
        @Override public String getProtocol() { return "HTTP/1.1"; }
        @Override public Object getAttribute(String name) { return null; }
        @Override public void setAttribute(String name, Object value) {}
        @Override public void setStreams(InputStream i, OutputStream o) {}
        @Override public HttpPrincipal getPrincipal() { return null; }
    }
}
