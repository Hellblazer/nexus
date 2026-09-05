package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.TenantConstants;
import org.testcontainers.containers.PostgreSQLContainer;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-gjv9b PARTs 1+2 (engine half) — capability_census + routing_events
 * TelemetryHandler endpoint tests.
 *
 * <p>PART 1 ({@code capability_census}): the PG home for the per-session
 * capability census, replacing {@code capability_census.jsonl} (Sam
 * directive 2026-08-20). UPSERT on {@code (tenant_id, session_id)} — a
 * SessionEnd re-fire overwrites the row rather than accumulating a
 * duplicate, no client-side dedup-by-tail-read needed any more.
 *
 * <p>PART 2 ({@code routing_events}): the PG home for the RDR-121
 * routing-hook telemetry log, replacing {@code routing_log.jsonl} (same
 * directive). Append-only event log — every fire is a distinct row.
 *
 * <p>Coverage: capability_census record-&gt;query round trip; upsert
 * overwrite; blindspot row stores NULL counts, not zeros; routing_events
 * record-&gt;list round trip; routing_events batch insert; RLS isolation
 * through HTTP on both tables.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class CapabilityCensusAndRoutingEventsHandlerTest {

    private static final String TOKEN = "gjv9b-handler-test-token-abc123";
    private static final String OTHER_TOKEN = "gjv9b-handler-test-token-def456";
    private static final String SVC_ROLE = "svc_gjv9b_handler_test";
    private static final String SVC_PASS = "svc_gjv9b_handler_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final String OTHER_TENANT = "gjv9b-other-tenant";

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {};

    PostgreSQLContainer<?> pg;
    NexusService service;
    HttpClient http;
    com.zaxxer.hikari.HikariDataSource svcDs;
    ObjectMapper mapper;

    @BeforeAll
    void startAll() throws Exception {
        mapper = new ObjectMapper();
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), TOKEN, TENANT, "test-bound");
            PgContainerHelper.seedServiceToken(
                DSL.using(su, SQLDialect.POSTGRES), OTHER_TOKEN, OTHER_TENANT, "test-bound-other");
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (svcDs != null)   svcDs.close();
        if (pg != null)      pg.stop();
    }

    // ── capability_census ────────────────────────────────────────────────────

    @Test
    void census_record_thenQueryRoundTrip() throws Exception {
        var resp = post("/v1/telemetry/capability_census/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-rt-1\",\"ts\":\"2026-09-01T00:00:00Z\","
            + "\"blindspot\":false,\"capabilities\":{\"skill\":3,\"agent\":1,\"serena\":0,"
            + "\"nx_answer\":2,\"search_query\":0,\"other_nx_mcp\":0,\"baseline\":0,\"other\":0},"
            + "\"dispatches\":1,\"total_calls\":6}");
        assertThat(resp.statusCode()).isEqualTo(200);

        var rows = censusRows(TOKEN, TENANT, "sess-rt-1");
        assertThat(rows).hasSize(1);
        var row = rows.get(0);
        assertThat(row.get("session_id")).isEqualTo("sess-rt-1");
        assertThat(row.get("blindspot")).isEqualTo(false);
        assertThat(row.get("dispatches")).isEqualTo(1);
        assertThat(row.get("total_calls")).isEqualTo(6);
        @SuppressWarnings("unchecked")
        var caps = (Map<String, Object>) row.get("capabilities");
        assertThat(caps.get("skill")).isEqualTo(3);
        assertThat(caps.get("nx_answer")).isEqualTo(2);
    }

    @Test
    void census_reRecordingSameSession_upsertsNotDuplicates() throws Exception {
        post("/v1/telemetry/capability_census/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-up\",\"blindspot\":false,"
            + "\"capabilities\":{\"skill\":1},\"dispatches\":0,\"total_calls\":1}");
        post("/v1/telemetry/capability_census/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-up\",\"blindspot\":false,"
            + "\"capabilities\":{\"skill\":5},\"dispatches\":2,\"total_calls\":5}");

        var rows = censusRows(TOKEN, TENANT, "sess-up");
        assertThat(rows)
            .as("upsert on (tenant_id, session_id): one row, not two — the SAME " +
                "collapse the JSONL-era dedup-by-tail-read used to do client-side")
            .hasSize(1);
        @SuppressWarnings("unchecked")
        var caps = (Map<String, Object>) rows.get(0).get("capabilities");
        assertThat(caps.get("skill")).isEqualTo(5);
        assertThat(rows.get(0).get("total_calls")).isEqualTo(5);
    }

    @Test
    void census_blindspotRow_storesNullCountsNotZeros() throws Exception {
        var resp = post("/v1/telemetry/capability_census/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-blind\",\"blindspot\":true,"
            + "\"unmeasurable_reason\":\"no-transcript-found\"}");
        assertThat(resp.statusCode()).isEqualTo(200);

        var rows = censusRows(TOKEN, TENANT, "sess-blind");
        assertThat(rows).hasSize(1);
        var row = rows.get(0);
        assertThat(row.get("blindspot")).isEqualTo(true);
        assertThat(row.get("unmeasurable_reason")).isEqualTo("no-transcript-found");
        assertThat(row.get("dispatches"))
            .as("a blindspot record must never fabricate a measured zero")
            .isNull();
        assertThat(row.get("total_calls")).isNull();
        @SuppressWarnings("unchecked")
        var caps = (Map<String, Object>) row.get("capabilities");
        assertThat(caps.get("skill")).isNull();
    }

    @Test
    void census_rls_otherTenantsRowsInvisible() throws Exception {
        post("/v1/telemetry/capability_census/record", OTHER_TOKEN, OTHER_TENANT,
            "{\"session_id\":\"sess-other\",\"blindspot\":false,"
            + "\"capabilities\":{},\"dispatches\":0,\"total_calls\":0}");

        var rows = censusRows(TOKEN, TENANT, "sess-other");
        assertThat(rows)
            .as("RLS: the default tenant must not see the other tenant's census row")
            .isEmpty();
    }

    @Test
    void census_missingSessionId_rejected400() throws Exception {
        var resp = post("/v1/telemetry/capability_census/record", TOKEN, TENANT,
            "{\"blindspot\":false}");
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("session_id");
    }

    // ── routing_events ───────────────────────────────────────────────────────

    @Test
    void routing_record_thenListRoundTrip() throws Exception {
        var resp = post("/v1/telemetry/routing_events/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-route-1\",\"rule\":\"phase_review_close_requires_gate\","
            + "\"outcome\":\"deny\",\"tool_name\":\"Bash\",\"command_fragment\":\"bd close 1\","
            + "\"escape_reason\":\"\"}");
        assertThat(resp.statusCode()).isEqualTo(200);

        var rows = routingRows(TOKEN, TENANT);
        var mine = rows.stream().filter(r -> "sess-route-1".equals(r.get("session_id"))).toList();
        assertThat(mine).hasSize(1);
        assertThat(mine.get(0).get("rule")).isEqualTo("phase_review_close_requires_gate");
        assertThat(mine.get(0).get("outcome")).isEqualTo("deny");
        assertThat(mine.get(0).get("tool_name")).isEqualTo("Bash");
    }

    @Test
    void routing_repeatedFires_appendDistinctRows() throws Exception {
        post("/v1/telemetry/routing_events/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-append\",\"rule\":\"r1\",\"outcome\":\"allow\"}");
        post("/v1/telemetry/routing_events/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-append\",\"rule\":\"r1\",\"outcome\":\"allow\"}");

        var mine = routingRows(TOKEN, TENANT).stream()
            .filter(r -> "sess-append".equals(r.get("session_id")))
            .toList();
        assertThat(mine)
            .as("routing_events is an event log — every fire is a distinct row, never an upsert")
            .hasSize(2);
    }

    @Test
    void routing_batchInsert_insertsAll() throws Exception {
        var resp = post("/v1/telemetry/routing_events/batch", TOKEN, TENANT,
            "{\"events\":["
            + "{\"session_id\":\"sess-batch\",\"rule\":\"rb\",\"outcome\":\"allow\"},"
            + "{\"session_id\":\"sess-batch\",\"rule\":\"rb\",\"outcome\":\"deny\"},"
            + "{\"session_id\":\"sess-batch\",\"rule\":\"rb\",\"outcome\":\"escape\","
            + "\"escape_reason\":\"deliberate override for testing\"}"
            + "]}");
        assertThat(resp.statusCode()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T).get("inserted")).isEqualTo(3);

        var mine = routingRows(TOKEN, TENANT).stream()
            .filter(r -> "sess-batch".equals(r.get("session_id")))
            .toList();
        assertThat(mine).hasSize(3);
    }

    @Test
    void routing_batchInsert_overCapIsRejected400() throws Exception {
        var events = new StringBuilder("{\"events\":[");
        for (int i = 0; i < 301; i++) {
            if (i > 0) events.append(",");
            events.append("{\"session_id\":\"sess-overcap\",\"rule\":\"r\",\"outcome\":\"allow\"}");
        }
        events.append("]}");
        var resp = post("/v1/telemetry/routing_events/batch", TOKEN, TENANT, events.toString());
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("300");

        var mine = routingRows(TOKEN, TENANT).stream()
            .filter(r -> "sess-overcap".equals(r.get("session_id")))
            .toList();
        assertThat(mine).isEmpty();
    }

    @Test
    void routing_batchInsert_nonObjectElementIsRejected400() throws Exception {
        var resp = post("/v1/telemetry/routing_events/batch", TOKEN, TENANT,
            "{\"events\":[\"not-an-object\"]}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void routing_batchInsert_missingRuleInOneElementIsRejected400() throws Exception {
        var resp = post("/v1/telemetry/routing_events/batch", TOKEN, TENANT,
            "{\"events\":["
            + "{\"session_id\":\"sess-partial\",\"rule\":\"ok\",\"outcome\":\"allow\"},"
            + "{\"session_id\":\"sess-partial\",\"outcome\":\"allow\"}"
            + "]}");
        assertThat(resp.statusCode()).isEqualTo(400);

        // A malformed batch must reject the WHOLE request, not partially
        // apply the valid entries ahead of the bad one.
        var mine = routingRows(TOKEN, TENANT).stream()
            .filter(r -> "sess-partial".equals(r.get("session_id")))
            .toList();
        assertThat(mine).isEmpty();
    }

    @Test
    void routing_batchInsert_nonArrayEventsIsRejected400() throws Exception {
        var resp = post("/v1/telemetry/routing_events/batch", TOKEN, TENANT,
            "{\"events\":\"not-an-array\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
    }

    @Test
    void routing_rls_otherTenantsRowsInvisible() throws Exception {
        post("/v1/telemetry/routing_events/record", OTHER_TOKEN, OTHER_TENANT,
            "{\"session_id\":\"sess-route-other\",\"rule\":\"r\",\"outcome\":\"allow\"}");

        var visible = routingRows(TOKEN, TENANT).stream()
            .map(r -> r.get("session_id"))
            .toList();
        assertThat(visible)
            .as("RLS: the default tenant must not see the other tenant's routing events")
            .doesNotContain("sess-route-other");
    }

    @Test
    void census_trim_deletesOlderThanDays_dryRunPreviewsWithoutDeleting() throws Exception {
        // nexus-gjv9b review fold-in, critique Significant 4: retention for
        // capability_census, same age-only trim discipline as hook_failures.
        post("/v1/telemetry/capability_census/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-trim-old\",\"ts\":\"2026-01-01T00:00:00Z\","
            + "\"blindspot\":false,\"capabilities\":{\"skill\":1},\"dispatches\":0,\"total_calls\":1}");
        post("/v1/telemetry/capability_census/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-trim-fresh\",\"blindspot\":false,"
            + "\"capabilities\":{\"skill\":1},\"dispatches\":0,\"total_calls\":1}");

        // dry_run=true must COUNT without deleting.
        var preview = post("/v1/telemetry/capability_census/trim", TOKEN, TENANT,
            "{\"days\":30,\"dry_run\":true}");
        assertThat(preview.statusCode()).isEqualTo(200);
        var previewBody = mapper.readValue(preview.body(), MAP_T);
        assertThat((Boolean) previewBody.get("dry_run")).isTrue();
        assertThat(((Number) previewBody.get("deleted")).intValue()).isGreaterThanOrEqualTo(1);
        assertThat(censusRows(TOKEN, TENANT, "sess-trim-old"))
            .as("dry_run must not actually delete anything")
            .hasSize(1);

        var real = post("/v1/telemetry/capability_census/trim", TOKEN, TENANT,
            "{\"days\":30}");
        assertThat(real.statusCode()).isEqualTo(200);
        var realBody = mapper.readValue(real.body(), MAP_T);
        assertThat((Boolean) realBody.get("dry_run")).isFalse();
        assertThat(((Number) realBody.get("deleted")).intValue()).isGreaterThanOrEqualTo(1);

        assertThat(censusRows(TOKEN, TENANT, "sess-trim-old"))
            .as("the row older than the cutoff must be gone")
            .isEmpty();
        assertThat(censusRows(TOKEN, TENANT, "sess-trim-fresh"))
            .as("a fresh row within the retention window must survive the trim")
            .hasSize(1);
    }

    @Test
    void routing_trim_deletesOlderThanDays_dryRunPreviewsWithoutDeleting() throws Exception {
        post("/v1/telemetry/routing_events/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-route-trim-old\",\"ts\":\"2026-01-01T00:00:00Z\","
            + "\"rule\":\"r-trim\",\"outcome\":\"allow\"}");
        post("/v1/telemetry/routing_events/record", TOKEN, TENANT,
            "{\"session_id\":\"sess-route-trim-fresh\",\"rule\":\"r-trim\",\"outcome\":\"allow\"}");

        var preview = post("/v1/telemetry/routing_events/trim", TOKEN, TENANT,
            "{\"days\":30,\"dry_run\":true}");
        assertThat(preview.statusCode()).isEqualTo(200);
        var previewBody = mapper.readValue(preview.body(), MAP_T);
        assertThat((Boolean) previewBody.get("dry_run")).isTrue();
        assertThat(((Number) previewBody.get("deleted")).intValue()).isGreaterThanOrEqualTo(1);
        var beforeReal = routingRows(TOKEN, TENANT).stream()
            .filter(r -> "sess-route-trim-old".equals(r.get("session_id"))).toList();
        assertThat(beforeReal)
            .as("dry_run must not actually delete anything")
            .hasSize(1);

        var real = post("/v1/telemetry/routing_events/trim", TOKEN, TENANT,
            "{\"days\":30}");
        assertThat(real.statusCode()).isEqualTo(200);
        var realBody = mapper.readValue(real.body(), MAP_T);
        assertThat((Boolean) realBody.get("dry_run")).isFalse();
        assertThat(((Number) realBody.get("deleted")).intValue()).isGreaterThanOrEqualTo(1);

        var rows = routingRows(TOKEN, TENANT);
        assertThat(rows.stream().anyMatch(r -> "sess-route-trim-old".equals(r.get("session_id"))))
            .as("the row older than the cutoff must be gone")
            .isFalse();
        assertThat(rows.stream().anyMatch(r -> "sess-route-trim-fresh".equals(r.get("session_id"))))
            .as("a fresh row within the retention window must survive the trim")
            .isTrue();
    }

    @Test
    void routing_missingRule_rejected400() throws Exception {
        var resp = post("/v1/telemetry/routing_events/record", TOKEN, TENANT,
            "{\"outcome\":\"allow\"}");
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("rule");
    }

    // ── auth ─────────────────────────────────────────────────────────────────

    @Test
    void noAuth_rejected401() throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + "/v1/telemetry/routing_events/list"))
            .header("X-Nexus-Tenant", TENANT)
            .GET().build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).isEqualTo(401);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> censusRows(String token, String tenant, String sessionId) throws Exception {
        var resp = get("/v1/telemetry/capability_census/query?session_id=" + sessionId, token, tenant);
        assertThat(resp.statusCode()).isEqualTo(200);
        return (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("rows");
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> routingRows(String token, String tenant) throws Exception {
        var resp = get("/v1/telemetry/routing_events/list?limit=1000", token, tenant);
        assertThat(resp.statusCode()).isEqualTo(200);
        return (List<Map<String, Object>>) mapper.readValue(resp.body(), MAP_T).get("rows");
    }

    private HttpResponse<String> post(String path, String token, String tenant, String body) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path, String token, String tenant) throws Exception {
        var req = HttpRequest.newBuilder()
            .uri(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .header("Authorization", "Bearer " + token)
            .header("X-Nexus-Tenant", tenant)
            .GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
