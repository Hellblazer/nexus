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
 * F3 step 2 engine half (bead nexus-fetre): over a real HTTP round trip, {@code
 * /v1/tuples/out} stamps {@code sender_session} from the SERVER-resolved minted
 * session (the {@code X-Nexus-T1-Session} token), never from the request body, and
 * stamps null (without refusing) when no minted session came with the request.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleSenderSessionHttpTest {

    private static final String BOOT = "boot-sender-session-http-test";
    private static final String TENANT_TOKEN = "tenant-sender-session-http-test";
    private static final String TENANT = TenantConstants.DEFAULT_TENANT;
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

        HttpResponse<String> issued = send(BOOT, null, "/v1/service-tokens/issue", Map.of(
                "tenant", TENANT, "label", "ci-sender-writer", "scope", "board-ci"));
        assertThat(issued.statusCode()).as(issued.body()).isEqualTo(200);
        boardCiRaw = MAPPER.readTree(issued.body()).get("token").asText();
    }

    @AfterAll
    void stopAll() throws Exception {
        if (service != null) service.stop();
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    private String mintSession(String sessionId) throws Exception {
        var r = send(TENANT_TOKEN, null, "/v1/sessions/start", Map.of("session_id", sessionId));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        return MAPPER.readTree(r.body()).get("session_token").asText();
    }

    private HttpResponse<String> send(String bearer, String sessionToken, String path, Object body)
            throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + path)
                .header("Authorization", "Bearer " + bearer)
                .header("Content-Type", "application/json");
        if (sessionToken != null) {
            b.header("X-Nexus-T1-Session", sessionToken);
        }
        return http.send(b.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private Map<String, Object> mailboxOut(String to, String nonce) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("subspace", "mailbox/" + to);
        m.put("keys", Map.of("to", to));
        m.put("dims", Map.of("from", "someone"));
        m.put("body", "hello");
        m.put("nonce", nonce);
        return m;
    }

    private JsonNode rdOnly(String bearer, String subspace, String to) throws Exception {
        var r = send(bearer, null, "/v1/tuples/rd", Map.of("subspace", subspace, "keys_pattern", Map.of("to", to)));
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
        JsonNode tuples = MAPPER.readTree(r.body()).get("tuples");
        assertThat(tuples.size()).as(r.body()).isEqualTo(1);
        return tuples.get(0);
    }

    @Test
    void outWithAMintedSessionToken_stampsThatSessionId_andRdRendersIt() throws Exception {
        String sid = "sess-" + UUID.randomUUID();
        String to = "http-" + UUID.randomUUID().toString().substring(0, 8);
        var out = send(TENANT_TOKEN, mintSession(sid), "/v1/tuples/out", mailboxOut(to, "n-1"));
        assertThat(out.statusCode()).as(out.body()).isEqualTo(200);
        JsonNode row = rdOnly(TENANT_TOKEN, "mailbox/" + to, to);
        assertThat(row.has("sender_session")).isTrue();
        assertThat(row.get("sender_session").asText()).isEqualTo(sid);
    }

    /** AuthFilter validates the session header on every route: a token that is not
     *  live is 401 on /v1/tuples/out too (body {"error":"unauthorized"}; the reason,
     *  session_not_minted, is logged only), on this engine and the
     *  ones before it. The client drops the header and retries (nexus-ztwp4). */
    @Test
    void outWithASessionHeaderThatIsNotLive_is401() throws Exception {
        String to = "http-" + UUID.randomUUID().toString().substring(0, 8);
        var out = send(TENANT_TOKEN, "not-a-minted-session-token", "/v1/tuples/out", mailboxOut(to, "n-1"));
        assertThat(out.statusCode()).as(out.body()).isEqualTo(401);
    }

    /** nexus-g2ckl: an ack's reply carries the ACKER's minted session, read from the
     *  request's token, not the requester's and never a body field. */
    @Test
    void ackWithReplyUnderAMintedSession_stampsTheReplyWithTheAckersSession() throws Exception {
        String requester = "sess-" + UUID.randomUUID();
        String acker = "sess-" + UUID.randomUUID();
        String inbox = "http-" + UUID.randomUUID().toString().substring(0, 8);
        String outbox = "http-" + UUID.randomUUID().toString().substring(0, 8);
        var out = send(TENANT_TOKEN, mintSession(requester), "/v1/tuples/out", mailboxOut(inbox, "n-1"));
        assertThat(out.statusCode()).as(out.body()).isEqualTo(200);
        String ackerToken = mintSession(acker);
        var claim = send(TENANT_TOKEN, ackerToken, "/v1/tuples/inp", Map.of(
                "subspace", "mailbox/" + inbox, "keys_pattern", Map.of("to", inbox), "claimant", "w",
                "lease_s", 60));
        assertThat(claim.statusCode()).as(claim.body()).isEqualTo(200);
        String claimId = MAPPER.readTree(claim.body()).get("claim_id").asText();
        Map<String, Object> reply = new LinkedHashMap<>();
        reply.put("subspace", "mailbox/" + outbox);
        reply.put("keys", Map.of("to", outbox));
        reply.put("dims", Map.of("from", "someone"));
        reply.put("body", "answer");
        reply.put("sender_session", "forged");
        var ack = send(TENANT_TOKEN, ackerToken, "/v1/tuples/ack",
                Map.of("claim_id", claimId, "claimant", "w", "reply", reply));
        assertThat(ack.statusCode()).as(ack.body()).isEqualTo(200);
        assertThat(rdOnly(TENANT_TOKEN, "mailbox/" + outbox, outbox).get("sender_session").asText())
                .isEqualTo(acker);
    }

    /** nexus-g2ckl: an ack without a session header writes its reply unstamped and is
     *  not refused, as out is. */
    @Test
    void ackWithReplyWithNoSessionHeader_stampsNull_andIsNotRefused() throws Exception {
        String inbox = "http-" + UUID.randomUUID().toString().substring(0, 8);
        String outbox = "http-" + UUID.randomUUID().toString().substring(0, 8);
        assertThat(send(TENANT_TOKEN, null, "/v1/tuples/out", mailboxOut(inbox, "n-1")).statusCode())
                .isEqualTo(200);
        var claim = send(TENANT_TOKEN, null, "/v1/tuples/inp", Map.of(
                "subspace", "mailbox/" + inbox, "keys_pattern", Map.of("to", inbox), "claimant", "w",
                "lease_s", 60));
        assertThat(claim.statusCode()).as(claim.body()).isEqualTo(200);
        String claimId = MAPPER.readTree(claim.body()).get("claim_id").asText();
        Map<String, Object> reply = new LinkedHashMap<>();
        reply.put("subspace", "mailbox/" + outbox);
        reply.put("keys", Map.of("to", outbox));
        reply.put("dims", Map.of("from", "someone"));
        reply.put("body", "answer");
        var ack = send(TENANT_TOKEN, null, "/v1/tuples/ack",
                Map.of("claim_id", claimId, "claimant", "w", "reply", reply));
        assertThat(ack.statusCode()).as(ack.body()).isEqualTo(200);
        JsonNode row = rdOnly(TENANT_TOKEN, "mailbox/" + outbox, outbox);
        assertThat(row.has("sender_session")).isTrue();
        assertThat(row.get("sender_session").isNull()).isTrue();
    }

    @Test
    void outWithNoSessionHeader_stampsNull_andIsNotRefused() throws Exception {
        String to = "http-" + UUID.randomUUID().toString().substring(0, 8);
        var out = send(TENANT_TOKEN, null, "/v1/tuples/out", mailboxOut(to, "n-1"));
        assertThat(out.statusCode()).as(out.body()).isEqualTo(200);
        JsonNode row = rdOnly(TENANT_TOKEN, "mailbox/" + to, to);
        assertThat(row.has("sender_session")).as("the key is rendered even when null").isTrue();
        assertThat(row.get("sender_session").isNull()).isTrue();
    }

    @Test
    void aBodyFieldSenderSession_isIgnored() throws Exception {
        String sid = "sess-" + UUID.randomUUID();
        String to = "http-" + UUID.randomUUID().toString().substring(0, 8);
        Map<String, Object> req = mailboxOut(to, "n-1");
        req.put("sender_session", "victim-" + UUID.randomUUID());
        // no session header: the forged body field must not be stored
        assertThat(send(TENANT_TOKEN, null, "/v1/tuples/out", req).statusCode()).isEqualTo(200);
        assertThat(rdOnly(TENANT_TOKEN, "mailbox/" + to, to).get("sender_session").isNull()).isTrue();

        // with a minted header: the token's session wins over the body field
        String to2 = "http-" + UUID.randomUUID().toString().substring(0, 8);
        Map<String, Object> req2 = mailboxOut(to2, "n-1");
        req2.put("sender_session", "victim-" + UUID.randomUUID());
        assertThat(send(TENANT_TOKEN, mintSession(sid), "/v1/tuples/out", req2).statusCode()).isEqualTo(200);
        assertThat(rdOnly(TENANT_TOKEN, "mailbox/" + to2, to2).get("sender_session").asText()).isEqualTo(sid);
    }

    @Test
    void aBoardCiTokenOut_stampsNull_andIsNotRefused() throws Exception {
        var out = send(boardCiRaw, null, "/v1/tuples/out", Map.of(
                "subspace", "board/ci/sender-topic",
                "keys", Map.of("topic", "sender-topic"),
                "dims", Map.of("from", "ci-sender-writer"),
                "body", "{\"status\":\"success\"}",
                "nonce", "sender-session-board-ci-1"));
        assertThat(out.statusCode()).as(out.body()).isEqualTo(200);
        // board-ci is write-only, so read the stamp back with the tenant token (same tenant).
        var rd = send(TENANT_TOKEN, null, "/v1/tuples/rd", Map.of("subspace", "board/ci/sender-topic"));
        assertThat(rd.statusCode()).as(rd.body()).isEqualTo(200);
        JsonNode tuples = MAPPER.readTree(rd.body()).get("tuples");
        assertThat(tuples.size()).as(rd.body()).isEqualTo(1);
        assertThat(tuples.get(0).get("sender_session").isNull()).isTrue();
    }
}
