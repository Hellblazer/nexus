// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.RequestLedger;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.sql.Connection;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-222 Phase 1 (bead nexus-yye8l.4): {@code GET /v1/requests/<request_id>} over HTTP, against
 * a full {@link NexusService} on a migrated cluster, through a non-superuser service role so
 * FORCE RLS on {@code nexus.request_ledger} is live.
 *
 * <p>Ledger rows are seeded through {@link RequestLedger} over the same role, in the sequence a
 * keyed route runs: claim, then the commit marker in its own transaction, or the failure marker.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RequestHandlerTest {

    private static final String TOKEN = "request-handler-test-token-abc123";
    private static final String OTHER_TOKEN = "request-handler-test-token-def456";
    private static final String MINT_TOKEN = "request-handler-test-token-mint789";
    private static final String SVC_ROLE = "svc_request_handler_test";
    private static final String SVC_PASS = "svc_request_handler_test_pass";
    private static final String TENANT = "request-route-tenant";
    private static final String OTHER_TENANT = "request-route-other-tenant";
    private static final String ROUTE = "/v1/vectors/gc/quarantine-orphans";
    private static final long BOUND_MS = PgSession.DEFAULT_GC_QUARANTINE_BOUNDED_STATEMENT_TIMEOUT_MS;

    private final ObjectMapper mapper = new ObjectMapper();
    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private NexusService service;
    private HttpClient http;
    private TenantScope scope;
    private RequestLedger ledger;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            var dsl = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.seedServiceToken(dsl, TOKEN, TENANT, "request-route",
                TokenStore.SCOPE_TENANT, null, null);
            PgContainerHelper.seedServiceToken(dsl, OTHER_TOKEN, OTHER_TENANT, "request-route-other",
                TokenStore.SCOPE_TENANT, null, null);
            PgContainerHelper.seedServiceToken(dsl, MINT_TOKEN, TENANT, "request-route-mint",
                TokenStore.SCOPE_MINT, null, null);
        }
        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        ledger = new RequestLedger(scope, svcDs);

        service = new NexusService(0, TOKEN, svcDs);
        service.start();
        http = HttpClient.newHttpClient();
    }

    @AfterAll
    void stopAll() {
        if (service != null) service.stop();
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private HttpResponse<String> send(String method, String path, String token) throws Exception {
        var b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + service.getPort() + path))
            .method(method, HttpRequest.BodyPublishers.noBody());
        if (token != null) {
            b.header("Authorization", "Bearer " + token);
        }
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode okJson(UUID id, String token) throws Exception {
        HttpResponse<String> r = send("GET", "/v1/requests/" + id, token);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode body = mapper.readTree(r.body());
        assertThat(body.get("request_id").asText()).isEqualTo(id.toString());
        return body;
    }

    private static byte[] sha(int seed) {
        byte[] b = new byte[32];
        b[0] = (byte) seed;
        return b;
    }

    private UUID committed(int seed, String response) {
        UUID id = UUID.randomUUID();
        var c = (RequestLedger.Claimed) ledger.claim(TENANT, id, ROUTE, sha(seed), BOUND_MS);
        scope.withTenant(TENANT, ctx -> {
            ledger.commitMarker(ctx, TENANT, id, c.nonce(), 200, response);
            return null;
        });
        return id;
    }

    // ── the four states ──────────────────────────────────────────────────────

    @Test
    void committed_returns200WithStoredStatusAndResponse() throws Exception {
        UUID id = committed(1, "{\"moved\": 3, \"sample\": [\"a\"]}");

        JsonNode body = okJson(id, TOKEN);

        assertThat(body.get("state").asText()).isEqualTo("committed");
        assertThat(body.get("response_status").asInt()).isEqualTo(200);
        assertThat(body.get("response")).isEqualTo(mapper.readTree("{\"moved\": 3, \"sample\": [\"a\"]}"));
    }

    @Test
    void inFlight_returns200WithStartedAtAndLeaseUntil() throws Exception {
        UUID id = UUID.randomUUID();
        ledger.claim(TENANT, id, ROUTE, sha(2), BOUND_MS);

        JsonNode body = okJson(id, TOKEN);

        assertThat(body.get("state").asText()).isEqualTo("in_flight");
        assertThat(java.time.OffsetDateTime.parse(body.get("lease_until").asText()))
            .isAfter(java.time.OffsetDateTime.parse(body.get("started_at").asText()));
        assertThat(body.has("response")).isFalse();
    }

    @Test
    void failed_returns200RolledBack() throws Exception {
        UUID id = UUID.randomUUID();
        var c = (RequestLedger.Claimed) ledger.claim(TENANT, id, ROUTE, sha(3), BOUND_MS);
        ledger.failMarker(TENANT, id, c.nonce(), 503);

        JsonNode body = okJson(id, TOKEN);

        assertThat(body.get("state").asText()).isEqualTo("rolled_back");
        assertThat(body.size()).as("rolled_back carries only request_id and state").isEqualTo(2);
    }

    @Test
    void noRow_returns200Unknown() throws Exception {
        JsonNode body = okJson(UUID.randomUUID(), TOKEN);
        assertThat(body.get("state").asText()).isEqualTo("unknown");
        assertThat(body.size()).isEqualTo(2);
    }

    @Test
    void anotherTenantsRequestId_reads200Unknown() throws Exception {
        UUID id = committed(4, "{}");
        assertThat(okJson(id, OTHER_TOKEN).get("state").asText())
            .as("RDR Test Plan Phase 1 RLS item 2: tenant B never sees tenant A's request")
            .isEqualTo("unknown");
        assertThat(okJson(id, TOKEN).get("state").asText()).isEqualTo("committed");
    }

    // ── refusals ─────────────────────────────────────────────────────────────

    @Test
    void malformedRequestId_returns400_neverA404() throws Exception {
        for (String tail : new String[] {"not-a-uuid", "1-1-1-1-1", "", UUID.randomUUID() + "/extra",
                                         UUID.randomUUID().toString().substring(1)}) {
            HttpResponse<String> r = send("GET", "/v1/requests/" + tail, TOKEN);
            assertThat(r.statusCode()).as("tail=" + tail).isEqualTo(400);
            assertThat(mapper.readTree(r.body()).get("error").asText()).isEqualTo("malformed_request_id");
        }
        HttpResponse<String> bare = send("GET", "/v1/requests", TOKEN);
        assertThat(bare.statusCode()).isEqualTo(400);
    }

    @Test
    void nonGet_returns405() throws Exception {
        for (String method : new String[] {"POST", "PUT", "DELETE"}) {
            assertThat(send(method, "/v1/requests/" + UUID.randomUUID(), TOKEN).statusCode())
                .as(method).isEqualTo(405);
        }
    }

    @Test
    void auth_tenantScopeTokenReaches_noTokenRefused_mintScopeForbidden() throws Exception {
        String path = "/v1/requests/" + UUID.randomUUID();
        assertThat(send("GET", path, TOKEN).statusCode()).isEqualTo(200);
        assertThat(send("GET", path, null).statusCode()).isEqualTo(401);
        assertThat(send("GET", path, MINT_TOKEN).statusCode()).isEqualTo(403);
    }
}
