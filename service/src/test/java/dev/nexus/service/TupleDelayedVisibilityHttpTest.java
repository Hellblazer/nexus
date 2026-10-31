// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.tuples.TemplateRegistry;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-5r2sc.2.1 (RDR-235 E1) over a real HTTP round trip: the optional {@code delay_s}
 * on {@code /v1/tuples/out} and {@code retry_after_s} on {@code /v1/tuples/nack}. A valid value
 * hides the row from {@code inp} until the delay passes; a negative, fractional or non-numeric
 * value answers 400 and writes nothing; absent behaves exactly as before.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleDelayedVisibilityHttpTest {

    private static final String BOOT = "boot-delayed-vis-http-test";
    private static final String TENANT_TOKEN = "tenant-delayed-vis-http-test";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = TestHttp.client();

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, BOOT, TENANT, TokenStore.ROOT_TOKEN_LABEL,
                    TokenStore.SCOPE_ROOT, null, null);
            PgContainerHelper.seedServiceToken(dsl, TENANT_TOKEN, TENANT, "tenant-bound");
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(
                null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        service = new NexusService(0, BOOT, ds, null, null, null, null, registry);
        service.start();
        port = service.getPort();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private HttpResponse<String> send(String path, Object body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + path)
                .header("Authorization", "Bearer " + TENANT_TOKEN)
                .header("Content-Type", "application/json");
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode post(String path, Object body) throws Exception {
        var r = send(path, body);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    private JsonNode get(String path) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + path)
                .header("Authorization", "Bearer " + TENANT_TOKEN);
        var r = http.send(b.GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    private static Map<String, Object> outBody(String queue, String nonce) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("subspace", "queue/" + queue);
        m.put("keys", Map.of("queue", queue));
        m.put("dims", Map.of("from", "producer"));
        m.put("body", "task");
        m.put("nonce", nonce);
        return m;
    }

    private JsonNode inp(String queue) throws Exception {
        return post("/v1/tuples/inp", Map.of("subspace", "queue/" + queue,
                "keys_pattern", Map.of("queue", queue), "claimant", "w", "lease_s", 60));
    }

    private static String newQueue() {
        return "dv-http-" + UUID.randomUUID().toString().substring(0, 8);
    }

    @Test
    void outWithDelayS_hidesTheTaskFromInp_untilTheDelayHasPassed() throws Exception {
        String q = newQueue();
        Map<String, Object> body = outBody(q, "n-1");
        body.put("delay_s", 2);
        post("/v1/tuples/out", body);
        assertThat(inp(q).get("tuple").isNull()).as("hidden during the delay").isTrue();
        Thread.sleep(2_500);
        assertThat(inp(q).get("tuple").isNull()).as("claimable after the delay").isFalse();
    }

    @Test
    void outWithoutDelayS_isClaimedAtOnce() throws Exception {
        String q = newQueue();
        post("/v1/tuples/out", outBody(q, "n-1"));
        assertThat(inp(q).get("tuple").isNull()).isFalse();
    }

    @Test
    void nackWithRetryAfterS_hidesTheTaskFromInp_untilItHasPassed() throws Exception {
        String q = newQueue();
        post("/v1/tuples/out", outBody(q, "n-1"));
        String claimId = inp(q).get("claim_id").asText();
        post("/v1/tuples/nack", Map.of("claim_id", claimId, "claimant", "w", "retry_after_s", 2));
        assertThat(inp(q).get("tuple").isNull()).as("hidden during the retry delay").isTrue();
        Thread.sleep(2_500);
        assertThat(inp(q).get("tuple").isNull()).as("claimable after the retry delay").isFalse();
    }

    @Test
    void nackWithoutRetryAfterS_makesTheTaskClaimableAtOnce() throws Exception {
        String q = newQueue();
        post("/v1/tuples/out", outBody(q, "n-1"));
        String claimId = inp(q).get("claim_id").asText();
        post("/v1/tuples/nack", Map.of("claim_id", claimId, "claimant", "w"));
        assertThat(inp(q).get("tuple").isNull()).isFalse();
    }

    @Test
    void outRefusesABadDelayS_with400_andWritesNothing() throws Exception {
        String q = newQueue();
        for (Object bad : new Object[] {-1, "x", 1.5, 172_801}) {
            Map<String, Object> body = outBody(q, "n-bad");
            body.put("delay_s", bad);
            var r = send("/v1/tuples/out", body);
            assertThat(r.statusCode()).as("delay_s=%s: %s", bad, r.body()).isEqualTo(400);
        }
        assertThat(post("/v1/tuples/rdp", Map.of("subspace", "queue/" + q,
                "keys_pattern", Map.of("queue", q))).get("tuples").size()).as("nothing written").isZero();
    }

    @Test
    void nackRefusesABadRetryAfterS_with400_andTheClaimStaysLive() throws Exception {
        String q = newQueue();
        post("/v1/tuples/out", outBody(q, "n-1"));
        String claimId = inp(q).get("claim_id").asText();
        for (Object bad : new Object[] {-1, "x", 1.5, 172_801}) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("claim_id", claimId);
            body.put("claimant", "w");
            body.put("retry_after_s", bad);
            var r = send("/v1/tuples/nack", body);
            assertThat(r.statusCode()).as("retry_after_s=%s: %s", bad, r.body()).isEqualTo(400);
        }
        post("/v1/tuples/ack", Map.of("claim_id", claimId, "claimant", "w"));   // still live
    }

    /** nexus-p909o: the wire shape of the two census keys and the row key, from a real engine. */
    @Test
    void theCensusAndTheRenderedRow_tellADelayedTaskFromAClaimableOne() throws Exception {
        String q = newQueue();
        Map<String, Object> delayedBody = outBody(q, "n-delayed");
        delayedBody.put("delay_s", 60);
        post("/v1/tuples/out", delayedBody);

        JsonNode stats = get("/v1/tuples/subspace_stats?subspace=queue/" + q);
        assertThat(stats.get("available").asInt()).as("depth keeps counting the delayed task").isEqualTo(1);
        assertThat(stats.get("delayed").asInt()).isEqualTo(1);
        assertThat(stats.has("oldest_claimable_created_at")).as("the key is present, so an engine that has it is distinguishable").isTrue();
        assertThat(stats.get("oldest_claimable_created_at").isNull()).as("nothing claimable yet").isTrue();

        JsonNode rows = post("/v1/tuples/rdp", Map.of("subspace", "queue/" + q, "keys_pattern", Map.of("queue", q)));
        JsonNode row = rows.get("tuples").get(0);
        assertThat(row.has("available_at")).isTrue();
        assertThat(row.get("available_at").isTextual()).as("an ISO timestamp").isTrue();

        post("/v1/tuples/out", outBody(q, "n-plain"));
        for (JsonNode c : new JsonNode[] {
                get("/v1/tuples/subspace_stats?subspace=queue/" + q),
                findIn(get("/v1/tuples/subspace_list?prefix=queue/" + q).get("subspaces"), "queue/" + q)}) {
            assertThat(c.get("available").asInt()).isEqualTo(2);
            assertThat(c.get("delayed").asInt()).isEqualTo(1);
            assertThat(c.get("oldest_claimable_created_at").isTextual()).as("the plain task").isTrue();
        }
        JsonNode plain = null;
        for (JsonNode t : post("/v1/tuples/rdp", Map.of("subspace", "queue/" + q,
                "keys_pattern", Map.of("queue", q), "n", 10)).get("tuples")) {
            if (t.get("available_at").isNull()) {
                plain = t;
            }
        }
        assertThat(plain).isNotNull();
        assertThat(plain.has("available_at")).as("present and null on a row never delayed").isTrue();
        assertThat(plain.get("available_at").isNull()).isTrue();
    }

    private static JsonNode findIn(JsonNode subspaces, String name) {
        for (JsonNode n : subspaces) {
            if (name.equals(n.get("subspace").asText())) {
                return n;
            }
        }
        throw new AssertionError("subspace_list did not return " + name);
    }

    /** nexus-wy51x: a non-numeric number field is a 400 whose message names the field, never the raw
     *  Java parse text ("For input string"), on every tuple route that reads one. */
    @Test
    void aNonNumericNumberField_isRefusedWith400_namingTheField_onEveryTupleRoute() throws Exception {
        String q = newQueue();
        String sub = "queue/" + q;
        Map<String, String> pat = Map.of("queue", q);
        Map<String, Object> outDelay = outBody(q, "n-x1");
        outDelay.put("delay_s", "x");
        Map<String, Object> outTtl = outBody(q, "n-x2");
        outTtl.put("ttl_seconds", "x");
        Map<String, Object> announceInterval = Map.of("subspaces", java.util.List.of(Map.of(
                "subspace", sub, "keys_pattern", pat, "announce", Map.of("interval_s", "x", "max", 1))));
        Map<String, Object> announceMax = Map.of("subspaces", java.util.List.of(Map.of(
                "subspace", sub, "keys_pattern", pat, "announce", Map.of("interval_s", 1, "max", "x"))));
        Map<String, Object> waitN = Map.of("subspaces", java.util.List.of(Map.of(
                "subspace", sub, "keys_pattern", pat, "n", "x")));
        Map<String, Object> waitTimeout = Map.of("subspaces", java.util.List.of(Map.of(
                "subspace", sub, "keys_pattern", pat)), "timeout_s", "x");
        Object[][] cases = {
            {"/v1/tuples/out", outDelay, "delay_s"},
            {"/v1/tuples/out", outTtl, "ttl_seconds"},
            {"/v1/tuples/nack", Map.of("claim_id", "c", "claimant", "w", "retry_after_s", "x"), "retry_after_s"},
            {"/v1/tuples/inp", Map.of("subspace", sub, "keys_pattern", pat, "claimant", "w", "lease_s", "x"),
                    "lease_s"},
            {"/v1/tuples/in", Map.of("subspace", sub, "keys_pattern", pat, "claimant", "w", "lease_s", "x"),
                    "lease_s"},
            {"/v1/tuples/in", Map.of("subspace", sub, "keys_pattern", pat, "claimant", "w", "timeout_s", "x"),
                    "timeout_s"},
            {"/v1/tuples/rd", Map.of("subspace", sub, "keys_pattern", pat, "n", "x"), "n"},
            {"/v1/tuples/rd", Map.of("subspace", sub, "keys_pattern", pat, "timeout_s", "x"), "timeout_s"},
            {"/v1/tuples/rdp", Map.of("subspace", sub, "keys_pattern", pat, "n", "x"), "n"},
            {"/v1/tuples/renew", Map.of("claim_id", "c", "claimant", "w", "lease_s", "x"), "lease_s"},
            {"/v1/tuples/wait", waitN, "n"},
            {"/v1/tuples/wait", waitTimeout, "timeout_s"},
            {"/v1/tuples/wait", announceInterval, "interval_s"},
            {"/v1/tuples/wait", announceMax, "max"},
        };
        for (Object[] c : cases) {
            var r = send((String) c[0], c[1]);
            String field = (String) c[2];
            assertThat(r.statusCode()).as("%s %s: %s", c[0], field, r.body()).isEqualTo(400);
            String error = MAPPER.readTree(r.body()).get("error").asText();
            assertThat(error).as("%s %s", c[0], field).startsWith(field + " must be a whole number")
                    .endsWith("got: x").doesNotContain("For input string");
        }
        assertThat(post("/v1/tuples/rdp", Map.of("subspace", sub, "keys_pattern", pat))
                .get("tuples").size()).as("nothing written").isZero();
    }
}
