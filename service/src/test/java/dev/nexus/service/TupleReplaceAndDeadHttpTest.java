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
 * Bead nexus-5r2sc.2.3 (RDR-235 E4) over a real HTTP round trip: the optional boolean {@code dead}
 * on {@code /v1/tuples/nack} (non-boolean is a 400).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleReplaceAndDeadHttpTest {

    private static final String BOOT = "boot-replace-dead-http-test";
    private static final String TENANT_TOKEN = "tenant-replace-dead-http-test";
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

    private static Map<String, Object> outBody(String queue, String nonce, String taskBody) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("subspace", "queue/" + queue);
        m.put("keys", Map.of("queue", queue));
        m.put("dims", Map.of("from", "producer"));
        m.put("body", taskBody);
        m.put("nonce", nonce);
        return m;
    }

    private JsonNode inp(String queue) throws Exception {
        return post("/v1/tuples/inp", Map.of("subspace", "queue/" + queue,
                "keys_pattern", Map.of("queue", queue), "claimant", "w", "lease_s", 60));
    }

    private static String newQueue() {
        return "rd-http-" + UUID.randomUUID().toString().substring(0, 8);
    }

    // ── nack dead ────────────────────────────────────────────────────────────

    @Test
    void nackDeadTrue_answers200_andDeadLettersAtOnce() throws Exception {
        String q = newQueue();
        post("/v1/tuples/out", outBody(q, "n-1", "v1"));
        String claimId = inp(q).get("claim_id").asText();
        JsonNode r = post("/v1/tuples/nack", Map.of("claim_id", claimId, "claimant", "w", "dead", true));
        assertThat(r.get("nacked").asBoolean()).isTrue();
        assertThat(inp(q).get("tuple").isNull()).as("dead at once: not claimable").isTrue();
        JsonNode stats = MAPPER.readTree(http.send(
                TestHttp.request("http://127.0.0.1:" + port + "/v1/tuples/subspace_stats?subspace=queue/" + q)
                        .header("Authorization", "Bearer " + TENANT_TOKEN).GET().build(),
                HttpResponse.BodyHandlers.ofString()).body());
        assertThat(stats.get("dead").asInt()).isEqualTo(1);
    }

    @Test
    void nackDeadFalseOrAbsent_isAPlainNack() throws Exception {
        String q = newQueue();
        post("/v1/tuples/out", outBody(q, "n-1", "v1"));
        String claimId = inp(q).get("claim_id").asText();
        post("/v1/tuples/nack", Map.of("claim_id", claimId, "claimant", "w", "dead", false));
        String again = inp(q).get("claim_id").asText();
        post("/v1/tuples/nack", Map.of("claim_id", again, "claimant", "w"));
        assertThat(inp(q).get("tuple").isNull()).as("two plain nacks of three attempts: still claimable").isFalse();
    }

    @Test
    void nackRefusesANonBooleanDead_with400_andTheClaimStaysLive() throws Exception {
        String q = newQueue();
        post("/v1/tuples/out", outBody(q, "n-1", "v1"));
        String claimId = inp(q).get("claim_id").asText();
        for (Object bad : new Object[] {"yes", "true", 1, 0}) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("claim_id", claimId);
            body.put("claimant", "w");
            body.put("dead", bad);
            var r = send("/v1/tuples/nack", body);
            assertThat(r.statusCode()).as("dead=%s: %s", bad, r.body()).isEqualTo(400);
        }
        post("/v1/tuples/ack", Map.of("claim_id", claimId, "claimant", "w"));   // still live
    }
}
