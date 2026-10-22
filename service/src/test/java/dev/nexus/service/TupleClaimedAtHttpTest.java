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
 * Bead nexus-x907o over a real HTTP round trip: {@code claimed_at} is rendered on every tuple
 * (null while unclaimed), a claim's response and a later {@code rd} agree on it, a renew
 * moves {@code lease_until} but not it, and a release clears it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleClaimedAtHttpTest {

    private static final String BOOT = "boot-claimed-at-http-test";
    private static final String TENANT_TOKEN = "tenant-claimed-at-http-test";
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

    private HttpResponse<String> send(String bearer, String path, Object body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + path)
                .header("Authorization", "Bearer " + bearer)
                .header("Content-Type", "application/json");
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode post(String path, Object body) throws Exception {
        var r = send(TENANT_TOKEN, path, body);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body());
    }

    private JsonNode rdOnly(String subspace, String to) throws Exception {
        JsonNode tuples = post("/v1/tuples/rd",
                Map.of("subspace", subspace, "keys_pattern", Map.of("to", to))).get("tuples");
        assertThat(tuples.size()).isEqualTo(1);
        return tuples.get(0);
    }

    @Test
    void claimedAtIsRenderedNullWhileUnclaimed_setByTheClaim_keptByRenew_clearedByRelease() throws Exception {
        String to = "http-" + UUID.randomUUID().toString().substring(0, 8);
        String subspace = "mailbox/" + to;
        post("/v1/tuples/out", Map.of("subspace", subspace, "keys", Map.of("to", to),
                "dims", Map.of("from", "someone"), "body", "hello", "nonce", "n-1"));

        JsonNode unclaimed = rdOnly(subspace, to);
        assertThat(unclaimed.has("claimed_at")).as("the key is always rendered").isTrue();
        assertThat(unclaimed.get("claimed_at").isNull()).isTrue();

        JsonNode claim = post("/v1/tuples/inp", Map.of("subspace", subspace,
                "keys_pattern", Map.of("to", to), "claimant", "w", "lease_s", 60));
        String claimId = claim.get("claim_id").asText();
        String claimedAt = claim.get("tuple").get("claimed_at").asText();
        assertThat(claimedAt).isNotBlank();

        JsonNode held = rdOnly(subspace, to);
        assertThat(held.get("claimed_at").asText()).as("the claim response and a read agree").isEqualTo(claimedAt);
        assertThat(held.has("claim_id")).as("claim_id is still never rendered by rd").isFalse();

        Thread.sleep(25);
        JsonNode renewed = post("/v1/tuples/renew", Map.of("claim_id", claimId, "claimant", "w", "lease_s", 600));
        JsonNode afterRenew = rdOnly(subspace, to);
        assertThat(afterRenew.get("lease_until").asText()).isEqualTo(renewed.get("lease_until").asText());
        assertThat(afterRenew.get("lease_until").asText()).isNotEqualTo(held.get("lease_until").asText());
        assertThat(afterRenew.get("claimed_at").asText()).as("renew leaves claimed_at").isEqualTo(claimedAt);

        post("/v1/tuples/release", Map.of("claim_id", claimId, "claimant", "w"));
        assertThat(rdOnly(subspace, to).get("claimed_at").isNull()).as("release clears it").isTrue();
    }
}
