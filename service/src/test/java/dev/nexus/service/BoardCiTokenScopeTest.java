// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantConstants;
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
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-r3ur5 — the board-ci writer scope, end to end through the real
 * {@link NexusService} (AuthFilter + TupleHandler share the live registry and
 * TokenCache). Precedent: nexus-xidcq's mint-locked scope
 * ({@code DataTokenHandlerTest}/{@code TokenAdminHandlerTest}/{@code AuthFilterTest}).
 *
 * <p>Security rationale (bead nexus-r3ur5): board-ci replaces a tenant-scope
 * token (full corpus read/write/delete) sitting in a public repo's CI and a
 * public Lambda with a credential confined to {@code POST /v1/tuples/out}
 * against the {@code board/ci/<topic>} template only. Every other route,
 * method, and tuples op is refused — this class is the proof.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class BoardCiTokenScopeTest {

    private static final String BOOT = "boot-board-ci-scope-test";
    private static final ObjectMapper MAPPER = new ObjectMapper();

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    NexusService service;
    int port;
    final HttpClient http = TestHttp.client();

    String boardCiRaw;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.seedServiceToken(DSL.using(su, SQLDialect.POSTGRES), BOOT,
                TenantConstants.DEFAULT_TENANT, dev.nexus.service.db.TokenStore.ROOT_TOKEN_LABEL,
                dev.nexus.service.db.TokenStore.SCOPE_ROOT, null, null);
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

        // Operator issues a board-ci credential bound to a tenant, mirroring how
        // conexus-4b actually provisions the CI board writer.
        JsonNode issued = postJsonAs(BOOT, "/v1/service-tokens/issue",
            "{\"tenant\":\"ci-board-writer\",\"label\":\"ci-board-writer\",\"scope\":\"board-ci\"}");
        boardCiRaw = issued.get("token").asText();
        assertThat(boardCiRaw).isNotBlank();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // ── the one thing it CAN do ──────────────────────────────────────────────

    @Test
    void out_toBoardCiTopic_succeeds() throws Exception {
        var resp = sendAs(boardCiRaw, "POST", "/v1/tuples/out", Map.of(
            "subspace", "board/ci/nexus-develop",
            "keys", Map.of("topic", "nexus-develop"),
            "dims", Map.of("from", "ci-board-writer"),
            "body", "{\"status\":\"success\"}",
            "nonce", "board-ci-test-nonce-1"));
        assertThat(resp.statusCode()).as("board-ci out to its own template: %s", resp.body())
            .isEqualTo(200);
    }

    @Test
    void out_toBoardCiTopic_repeatedWrites_eachSucceed() throws Exception {
        // Not a single-shot fluke: two different topics under the template both work.
        for (String topic : new String[] {"nexus-develop", "nexus-feature-x"}) {
            var resp = sendAs(boardCiRaw, "POST", "/v1/tuples/out", Map.of(
                "subspace", "board/ci/" + topic,
                "keys", Map.of("topic", topic),
                "dims", Map.of("from", "ci-board-writer"),
                "nonce", "board-ci-test-nonce-" + topic));
            assertThat(resp.statusCode()).as("topic=%s: %s", topic, resp.body()).isEqualTo(200);
        }
    }

    // ── wrong template on the one route it can reach ────────────────────────

    @Test
    void out_toTwoSegmentBoardTopic_is403() throws Exception {
        // board/nexus-dev resolves to the DIFFERENT two-segment board/<topic>
        // template, not board/ci/<topic> — refused even though the path/method
        // are exactly the surface AuthFilter admits.
        var resp = sendAs(boardCiRaw, "POST", "/v1/tuples/out", Map.of(
            "subspace", "board/nexus-dev",
            "keys", Map.of("topic", "nexus-dev"),
            "dims", Map.of("from", "ci-board-writer")));
        assertThat(resp.statusCode()).isEqualTo(403);
        assertThat(resp.body()).contains("board-ci").contains("board/ci/<topic>");
    }

    @Test
    void out_toMailbox_is403() throws Exception {
        var resp = sendAs(boardCiRaw, "POST", "/v1/tuples/out", Map.of(
            "subspace", "mailbox/some-address",
            "keys", Map.of("to", "some-address"),
            "dims", Map.of("from", "ci-board-writer")));
        assertThat(resp.statusCode()).isEqualTo(403);
    }

    @Test
    void out_toQueue_is403() throws Exception {
        var resp = sendAs(boardCiRaw, "POST", "/v1/tuples/out", Map.of(
            "subspace", "queue/some-queue",
            "keys", Map.of("name", "some-queue")));
        assertThat(resp.statusCode()).isEqualTo(403);
    }

    // ── every other tuples op, even against its OWN template ───────────────

    @Test
    void everyOtherTuplesOp_is403() throws Exception {
        // Seed one real board/ci row first (via the root token) so rd/in have
        // something to find, proving the 403 is the scope guard and not a
        // coincidental "nothing here" 200/empty result.
        var seedResp = sendAs(BOOT, "POST", "/v1/tuples/out", Map.of(
            "subspace", "board/ci/nexus-scope-guard-probe",
            "keys", Map.of("topic", "nexus-scope-guard-probe"),
            "dims", Map.of("from", "seed"),
            "nonce", "board-ci-scope-guard-seed-nonce"));
        assertThat(seedResp.statusCode()).isEqualTo(200);

        Map<String, String> pattern = Map.of("topic", "nexus-scope-guard-probe");
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/rd", Map.of(
            "subspace", "board/ci/nexus-scope-guard-probe", "keys_pattern", pattern))
            .statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/rdp", Map.of(
            "subspace", "board/ci/nexus-scope-guard-probe", "keys_pattern", pattern))
            .statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/in", Map.of(
            "subspace", "board/ci/nexus-scope-guard-probe", "keys_pattern", pattern,
            "claimant", "board-ci-cannot-claim", "lease_s", 60))
            .statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/inp", Map.of(
            "subspace", "board/ci/nexus-scope-guard-probe", "keys_pattern", pattern,
            "claimant", "board-ci-cannot-claim"))
            .statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/wait", Map.of(
            "subspaces", java.util.List.of(Map.of(
                "subspace", "board/ci/nexus-scope-guard-probe", "keys_pattern", pattern))))
            .statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/ack", Map.of(
            "claim_id", "0000000000000000000000000000000000000000000000000000000000000000",
            "claimant", "x")).statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/nack", Map.of(
            "claim_id", "0000000000000000000000000000000000000000000000000000000000000000",
            "claimant", "x")).statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/renew", Map.of(
            "claim_id", "0000000000000000000000000000000000000000000000000000000000000000",
            "claimant", "x", "lease_s", 60)).statusCode()).isEqualTo(403);
        assertThat(sendAs(boardCiRaw, "POST", "/v1/tuples/release", Map.of(
            "claim_id", "0000000000000000000000000000000000000000000000000000000000000000",
            "claimant", "x")).statusCode()).isEqualTo(403);
        assertThat(get(boardCiRaw, "/v1/tuples/registry").statusCode()).isEqualTo(403);
        assertThat(get(boardCiRaw, "/v1/tuples/subspace_list?prefix=board/ci/").statusCode())
            .isEqualTo(403);
        assertThat(get(boardCiRaw, "/v1/tuples/subspace_stats?subspace=board/ci/"
                + "nexus-scope-guard-probe").statusCode()).isEqualTo(403);
        assertThat(get(boardCiRaw, "/v1/tuples/park_stats").statusCode()).isEqualTo(403);
    }

    @Test
    void getOnTuplesOut_is403() throws Exception {
        var req = TestHttp.request(base() + "/v1/tuples/out")
            .header("Authorization", "Bearer " + boardCiRaw).GET().build();
        assertThat(http.send(req, HttpResponse.BodyHandlers.ofString()).statusCode())
            .isEqualTo(403);
    }

    // ── every non-tuples route ───────────────────────────────────────────────

    @Test
    void vectorsRoute_is403() throws Exception {
        assertThat(get(boardCiRaw, "/v1/vectors").statusCode()).isEqualTo(403);
    }

    @Test
    void catalogRoute_is403() throws Exception {
        assertThat(get(boardCiRaw, "/v1/catalog").statusCode()).isEqualTo(403);
    }

    @Test
    void adminIssueRoute_is403() throws Exception {
        assertThat(sendAs(boardCiRaw, "POST", "/v1/service-tokens/issue",
            Map.of("tenant", "some-other-tenant")).statusCode()).isEqualTo(403);
    }

    @Test
    void dataTokenMintRoute_is403() throws Exception {
        assertThat(sendAs(boardCiRaw, "POST", "/v1/data-tokens/mint",
            Map.of("tenant", "ci-board-writer")).statusCode()).isEqualTo(403);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private String base() {
        return "http://127.0.0.1:" + port;
    }

    private HttpResponse<String> sendAs(String bearer, String method, String path, Object body)
            throws Exception {
        var req = TestHttp.request(base() + path)
            .header("Authorization", "Bearer " + bearer)
            .header("Content-Type", "application/json")
            .method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
            .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String bearer, String path) throws Exception {
        var req = TestHttp.request(base() + path)
            .header("Authorization", "Bearer " + bearer).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode postJsonAs(String bearer, String path, String body) throws Exception {
        var req = TestHttp.request(base() + path)
            .header("Authorization", "Bearer " + bearer)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
        var resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertThat(resp.statusCode()).as("POST %s -> %s", path, resp.body()).isEqualTo(200);
        return MAPPER.readTree(resp.body());
    }
}
