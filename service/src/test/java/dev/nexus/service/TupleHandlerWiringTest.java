// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
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
 * RDR-205 Phase 1 Step 4 (bead nexus-em75s.4) — proves the constructor-level
 * half of "the boot check refuses to start" (the other half — {@code
 * TemplateRegistry.loadAtBoot} itself refusing on a breach — is already
 * pinned by {@code TemplateRegistryTest}'s {@code bootCheckRefusesOnEquality}
 * / {@code bootCheckRefusesWhenLogTtlIsShorter}, bead nexus-em75s.3):
 *
 * <ol>
 *   <li>{@code NexusService} constructed WITH a {@link TemplateRegistry}
 *       (the widest constructor, the one {@code Main.java} calls AFTER
 *       {@code TemplateRegistry.loadAtBoot} has already passed its own boot
 *       check) wires {@code /v1/tuples} live.</li>
 *   <li>{@code NexusService} constructed WITHOUT one (every narrower
 *       overload — what every OTHER handler test in this package already
 *       uses) leaves {@code /v1/tuples} unregistered (404), never a
 *       partially-wired NPE.</li>
 * </ol>
 *
 * <p>{@code Main.java}'s own ordering — {@code loadAtBoot} runs, and on a
 * {@code TemplateRegistryException} the process exits BEFORE {@code
 * NexusService} is ever constructed — is a straight-line boot sequence with
 * no branching to unit-test independently of spawning the real process;
 * combined with (1) above (a registry that DID pass its boot check reaches
 * this constructor and wires the route) and the existing loadAtBoot
 * boot-check tests, the two together are the proof the bead requires.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleHandlerWiringTest {

    private static final String TOKEN = "tuple-wiring-test-token-xyz123";
    private static final String SVC_ROLE = "svc_tuple_wiring_test";
    private static final String SVC_PASS = "svc_tuple_wiring_test_pass";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;

    private static final TypeReference<Map<String, Object>> MAP_T = new TypeReference<>() {
    };

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    NexusService withRegistry;
    NexusService withoutRegistry;
    HttpClient http;
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
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        TemplateRegistry registry = TemplateRegistry.loadAtBoot(
                null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);

        withRegistry = new NexusService(0, TOKEN, svcDs, null, null, null, null, registry);
        withRegistry.start();

        withoutRegistry = new NexusService(0, TOKEN, svcDs);
        withoutRegistry.start();

        http = TestHttp.client();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (withRegistry != null) {
            withRegistry.stop();
        }
        if (withoutRegistry != null) {
            withoutRegistry.stop();
        }
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    @Test
    void registryPresent_tuplesRouteLive() throws Exception {
        var resp = get(withRegistry, "/v1/tuples/registry");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body).containsKey("digest");
        assertThat((java.util.List<?>) body.get("templates")).hasSize(2);
    }

    /**
     * nexus-em75s.36: {@code registry()}'s wire form omitted {@code dimensions}
     * entirely (a Phase 2 client could not learn a template's required dims) and
     * gave no hint that a key's value is pinned to a set. The digest stays a
     * SHA-256 hex string throughout -- unchanged in form, even though its value
     * necessarily differs from before this bead (the canonical map now folds in
     * {@code key_values}).
     */
    @Test
    @SuppressWarnings("unchecked")
    void registryPresent_templatesCarryDimensionsAndKeyValues() throws Exception {
        var resp = get(withRegistry, "/v1/tuples/registry");
        assertThat(resp.statusCode()).isEqualTo(200);
        var body = mapper.readValue(resp.body(), MAP_T);

        assertThat((String) body.get("digest")).matches("[0-9a-f]{64}");

        var templates = (java.util.List<Map<String, Object>>) body.get("templates");
        Map<String, Object> ledger = templates.stream()
                .filter(t -> "ledger/<session_id>".equals(t.get("name")))
                .findFirst().orElseThrow();

        var ledgerDims = (Map<String, Object>) ledger.get("dimensions");
        assertThat(ledgerDims).containsKey("agent_type");
        var agentType = (Map<String, Object>) ledgerDims.get("agent_type");
        assertThat(agentType.get("type")).isEqualTo("string");

        var keyValues = (Map<String, Object>) ledger.get("key_values");
        assertThat(keyValues).containsEntry("kind", java.util.List.of("start", "report"));
        assertThat(keyValues).doesNotContainKey("agent_id");

        Map<String, Object> mailbox = templates.stream()
                .filter(t -> "mailbox/<address>".equals(t.get("name")))
                .findFirst().orElseThrow();
        var mailboxDims = (Map<String, Object>) mailbox.get("dimensions");
        assertThat(mailboxDims).containsKeys("from", "kind", "correlation_id", "address_kind");
        // mailbox pins no key value -- key_values is omitted entirely, not an empty map.
        assertThat(mailbox).doesNotContainKey("key_values");
    }

    @Test
    void registryAbsent_tuplesRouteNotRegistered() throws Exception {
        var resp = get(withoutRegistry, "/v1/tuples/registry");
        assertThat(resp.statusCode()).isEqualTo(404);
    }

    /**
     * nexus-em75s.35 (RDR-205 review finding M5): {@code claim_id} is the ack/nack
     * credential — a reader that never won the claim must never be able to read it
     * off a probe/read response. Round-trips a real claim through {@code /out} then
     * {@code /in} (so the row's persisted {@code claim_id} column is genuinely set,
     * not null), then reads it back via {@code /rd} and asserts the rendered tuple
     * carries {@code claimant}/{@code claim_state} but has NO {@code claim_id} key at
     * all — not null, absent — while the claimant who actually won the claim still
     * receives the credential via {@code /in}'s own top-level {@code claim_id} field.
     */
    @SuppressWarnings("unchecked")
    @Test
    void rd_claimedTuple_omitsClaimIdKey_keepsClaimantAndClaimState() throws Exception {
        String to = "wire-shape-test-addr";
        String from = "wire-shape-test-sender";
        String claimant = "wire-shape-test-claimant";

        var outResp = post(withRegistry, "/v1/tuples/out", Map.of(
                "subspace", "mailbox/" + to,
                "keys", Map.of("to", to),
                "dims", Map.of("from", from),
                "body", "hello",
                "nonce", "wire-shape-test-nonce-1"));
        assertThat(outResp.statusCode()).isEqualTo(200);

        var inResp = post(withRegistry, "/v1/tuples/in", Map.of(
                "subspace", "mailbox/" + to,
                "keys_pattern", Map.of("to", to),
                "claimant", claimant,
                "lease_s", 60));
        assertThat(inResp.statusCode()).isEqualTo(200);
        var inJson = mapper.readValue(inResp.body(), MAP_T);
        // The actual credential DOES reach the claimant — via /in's own top-level
        // field, never via the tuple object itself.
        assertThat(inJson.get("claim_id")).isNotNull();

        var rdResp = post(withRegistry, "/v1/tuples/rd", Map.of(
                "subspace", "mailbox/" + to,
                "keys_pattern", Map.of("to", to)));
        assertThat(rdResp.statusCode()).isEqualTo(200);
        var rdJson = mapper.readValue(rdResp.body(), MAP_T);
        var tuples = (java.util.List<Map<String, Object>>) rdJson.get("tuples");
        assertThat(tuples).hasSize(1);
        Map<String, Object> tuple = tuples.get(0);
        assertThat(tuple).containsEntry("claim_state", "claimed");
        assertThat(tuple).containsEntry("claimant", claimant);
        assertThat(tuple).doesNotContainKey("claim_id");
    }

    private HttpResponse<String> get(NexusService svc, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + svc.getPort() + path)
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .GET()
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    // ── RDR-206 Phase 1 Step 2: the ack route's optional reply object ────────

    /** out + in against a fresh mailbox address, returning the claim id. */
    private String outAndClaim(String to, String claimant) throws Exception {
        assertThat(post(withRegistry, "/v1/tuples/out", Map.of(
                "subspace", "mailbox/" + to,
                "keys", Map.of("to", to),
                "dims", Map.of("from", "asker"),
                "body", "the request",
                "nonce", "nonce-" + to)).statusCode()).isEqualTo(200);
        var inResp = post(withRegistry, "/v1/tuples/in", Map.of(
                "subspace", "mailbox/" + to,
                "keys_pattern", Map.of("to", to),
                "claimant", claimant,
                "lease_s", 60));
        assertThat(inResp.statusCode()).isEqualTo(200);
        return (String) mapper.readValue(inResp.body(), MAP_T).get("claim_id");
    }

    @Test
    void ack_withNoReply_returnsANullReplyId() throws Exception {
        String to = "ack-noreply-addr";
        String claimId = outAndClaim(to, "ack-noreply-claimant");

        var resp = post(withRegistry, "/v1/tuples/ack", Map.of(
                "claim_id", claimId, "claimant", "ack-noreply-claimant"));
        assertThat(resp.statusCode()).isEqualTo(200);
        var json = mapper.readValue(resp.body(), MAP_T);
        assertThat(json.get("acked")).isEqualTo(Boolean.TRUE);
        assertThat(json).as("reply_id is present and null, never absent").containsKey("reply_id");
        assertThat(json.get("reply_id")).isNull();
    }

    @Test
    void ack_withAReply_writesItAndReturnsItsHexId() throws Exception {
        String to = "ack-reply-addr";
        String asker = "ack-reply-asker";
        String claimId = outAndClaim(to, "ack-reply-claimant");

        var resp = post(withRegistry, "/v1/tuples/ack", Map.of(
                "claim_id", claimId,
                "claimant", "ack-reply-claimant",
                "reply", Map.of(
                        "subspace", "mailbox/" + asker,
                        "keys", Map.of("to", asker),
                        "dims", Map.of("from", "answerer"),
                        "body", "the answer")));
        assertThat(resp.statusCode()).isEqualTo(200);
        var json = mapper.readValue(resp.body(), MAP_T);
        assertThat(json.get("acked")).isEqualTo(Boolean.TRUE);
        String replyId = (String) json.get("reply_id");
        assertThat(replyId).as("a 64-char lowercase hex id, the same shape out returns")
                .isNotNull().hasSize(64).matches("[0-9a-f]{64}");

        var rdResp = post(withRegistry, "/v1/tuples/rd", Map.of(
                "subspace", "mailbox/" + asker,
                "keys_pattern", Map.of("to", asker)));
        var tuples = (java.util.List<Map<String, Object>>) mapper.readValue(rdResp.body(), MAP_T)
                .get("tuples");
        assertThat(tuples).hasSize(1);
        assertThat(tuples.get(0).get("body")).isEqualTo("the answer");
        assertThat(tuples.get(0).get("id")).isEqualTo(replyId);
    }

    @Test
    void ack_withAReplyCarryingANonce_isRefusedAndLeavesTheRequestClaimed() throws Exception {
        String to = "ack-nonce-addr";
        String asker = "ack-nonce-asker";
        String claimId = outAndClaim(to, "ack-nonce-claimant");

        // The engine sets a reply's nonce to the id of the request it answers. A caller
        // supplying one is refused rather than ignored: silently dropping it would leave
        // the caller believing it had chosen the reply's identity. validateOut cannot
        // catch this -- it checks for a nonce that is MISSING -- and this is the only
        // layer where "absent" and "explicitly supplied" are still distinguishable.
        var resp = post(withRegistry, "/v1/tuples/ack", Map.of(
                "claim_id", claimId,
                "claimant", "ack-nonce-claimant",
                "reply", Map.of(
                        "subspace", "mailbox/" + asker,
                        "keys", Map.of("to", asker),
                        "dims", Map.of("from", "answerer"),
                        "body", "mine",
                        "nonce", "i-picked-this")));
        assertThat(resp.statusCode()).as("SchemaViolation maps to 400").isEqualTo(400);
        assertThat(resp.body()).contains("reply.nonce");

        // The request must still be there and STILL CLAIMED, i.e. not consumed. Read
        // through rd rather than the census, because rd shows the row's own claim state
        // and an ack that had gone through would have removed the row from this read
        // entirely (a consumed row is invisible to rd).
        var requestResp = post(withRegistry, "/v1/tuples/rd", Map.of(
                "subspace", "mailbox/" + to,
                "keys_pattern", Map.of("to", to)));
        var requestRows = (java.util.List<Map<String, Object>>) mapper
                .readValue(requestResp.body(), MAP_T).get("tuples");
        assertThat(requestRows).as("a refused reply must not have consumed the request")
                .hasSize(1);
        assertThat(requestRows.get(0).get("claim_state")).isEqualTo("claimed");

        var rdResp = post(withRegistry, "/v1/tuples/rd", Map.of(
                "subspace", "mailbox/" + asker,
                "keys_pattern", Map.of("to", asker)));
        assertThat((java.util.List<?>) mapper.readValue(rdResp.body(), MAP_T).get("tuples"))
                .as("no reply row").isEmpty();
    }

    @Test
    void ack_withANonObjectReply_isRefused() throws Exception {
        String to = "ack-badreply-addr";
        String claimId = outAndClaim(to, "ack-badreply-claimant");

        var resp = post(withRegistry, "/v1/tuples/ack", Map.of(
                "claim_id", claimId, "claimant", "ack-badreply-claimant",
                "reply", "not an object"));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("reply");
    }

    // ── RDR-206 Phase 1 Step 3: the renew route ──────────────────────────────

    @Test
    void renew_extendsTheLease_andReturnsAnIso8601Deadline() throws Exception {
        String to = "renew-ok-addr";
        String claimId = outAndClaim(to, "renew-ok-claimant");

        var resp = post(withRegistry, "/v1/tuples/renew", Map.of(
                "claim_id", claimId, "claimant", "renew-ok-claimant", "lease_s", 600));
        assertThat(resp.statusCode()).isEqualTo(200);
        String leaseUntil = (String) mapper.readValue(resp.body(), MAP_T).get("lease_until");
        assertThat(leaseUntil).as("MAPPER's JavaTimeModule renders an instant, not an epoch number")
                .isNotNull();
        var parsed = java.time.OffsetDateTime.parse(leaseUntil);

        // The claim's original lease was 60s; a 600s renew must land past that. Compared
        // against the ORIGINAL deadline rather than against a wall-clock guess, so the
        // assertion says nothing about how fast this box is.
        var rdResp = post(withRegistry, "/v1/tuples/rd", Map.of(
                "subspace", "mailbox/" + to, "keys_pattern", Map.of("to", to)));
        var rows = (java.util.List<Map<String, Object>>) mapper.readValue(rdResp.body(), MAP_T)
                .get("tuples");
        assertThat(rows).hasSize(1);
        assertThat(java.time.OffsetDateTime.parse((String) rows.get(0).get("lease_until")))
                .as("the row carries exactly what the route returned").isEqualTo(parsed);
        assertThat(rows.get(0).get("claim_state")).isEqualTo("claimed");
    }

    @Test
    void renew_aboveTheTemplateCap_is400() throws Exception {
        String to = "renew-cap-addr";
        String claimId = outAndClaim(to, "renew-cap-claimant");

        // mailbox.yaml caps max_lease_seconds at 900. Refused, never clamped.
        var resp = post(withRegistry, "/v1/tuples/renew", Map.of(
                "claim_id", claimId, "claimant", "renew-cap-claimant", "lease_s", 901));
        assertThat(resp.statusCode()).isEqualTo(400);
        assertThat(resp.body()).contains("LeaseTooLong");
    }

    @Test
    void renew_requiresPost() throws Exception {
        assertThat(get(withRegistry, "/v1/tuples/renew").statusCode()).isEqualTo(405);
    }

    private HttpResponse<String> post(NexusService svc, String path, Object body) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + svc.getPort() + path)
                .header("Authorization", "Bearer " + TOKEN)
                .header("X-Nexus-Tenant", TENANT)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
