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

    // ── RDR-253 Step 2 (nexus-hczdo.1.2): the stamp of an identity token ─────────────────────────────────────

    private HttpResponse<String> sendIdentity(String identityToken, String sessionToken, String method, String path,
                                              Object body) throws Exception {
        var b = TestHttp.request("http://127.0.0.1:" + port + path)
                .header("Authorization", "Bearer " + TENANT_TOKEN)
                .header("Content-Type", "application/json");
        if (identityToken != null) {
            b.header("X-Nexus-Identity-Token", identityToken);
        }
        if (sessionToken != null) {
            b.header("X-Nexus-T1-Session", sessionToken);
        }
        var req = "GET".equals(method) ? b.GET().build()
                : b.method(method, HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))).build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static Map<String, Object> process(String sid, int pid) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("session_id", sid);
        m.put("host", "h1");
        m.put("pid", pid);
        m.put("started_at", 5000L + pid);
        m.put("proc_start", "2026-11-17T10:00:00Z");
        return m;
    }

    private JsonNode createSeatAndBind(String sid, int pid) throws Exception {
        String name = "stamp-" + UUID.randomUUID().toString().substring(0, 8);
        var created = sendIdentity(null, null, "POST", "/v1/identity", Map.of("kind", "seat", "name", name, "created_by", "sam"));
        assertThat(created.statusCode()).as(created.body()).isEqualTo(200);
        String id = MAPPER.readTree(created.body()).get("id").asText();
        Map<String, Object> bind = new LinkedHashMap<>(process(sid, pid));
        bind.put("event", "start");
        bind.put("expected_generation", 0);
        var bound = sendIdentity(null, null, "POST", "/v1/identity/" + id + "/bind", bind);
        assertThat(bound.statusCode()).as(bound.body()).isEqualTo(200);
        return MAPPER.readTree(bound.body());
    }

    private void bindClear(String id, String sid, int pid) throws Exception {
        Map<String, Object> bind = new LinkedHashMap<>(process(sid, pid));
        bind.put("event", "clear");
        bind.put("expected_generation", 1);
        var r = sendIdentity(null, null, "POST", "/v1/identity/" + id + "/bind", bind);
        assertThat(r.statusCode()).as(r.body()).isEqualTo(200);
    }

    private String stampOfOut(String identityToken, String sessionToken) throws Exception {
        String to = "http-" + UUID.randomUUID().toString().substring(0, 8);
        var out = sendIdentity(identityToken, sessionToken, "POST", "/v1/tuples/out", mailboxOut(to, "n-1"));
        assertThat(out.statusCode()).as(out.body()).isEqualTo(200);
        JsonNode stamp = rdOnly(TENANT_TOKEN, "mailbox/" + to, to).get("sender_session");
        return stamp.isNull() ? null : stamp.asText();
    }

    /** The open item of RDR-253 Step 2: the stamp of an identity token is the session id of the identity's newest
     *  binding row under the token's generation, so a tuple written after /clear carries the NEW session id with
     *  the token the MCP server already holds; and the stamp maps back to the writer's identity and generation
     *  through the session index (RDR-252's owner stamper reads it that way). */
    @Test
    void outWithAnIdentityToken_isStampedWithTheIdentitysCurrentSession_acrossAClear() throws Exception {
        String sid1 = "sess-" + UUID.randomUUID();
        String sid2 = "sess-" + UUID.randomUUID();
        JsonNode bound = createSeatAndBind(sid1, 31);
        String id = bound.get("id").asText();
        String token = bound.get("identity_token").asText();

        assertThat(stampOfOut(token, null)).as("before the clear").isEqualTo(sid1);
        bindClear(id, sid2, 31);
        assertThat(stampOfOut(token, null)).as("the same token, after the clear").isEqualTo(sid2);

        var lookup = sendIdentity(null, null, "GET", "/v1/identity/sessions/" + sid2, null);
        assertThat(lookup.statusCode()).as(lookup.body()).isEqualTo(200);
        JsonNode mapped = MAPPER.readTree(lookup.body());
        assertThat(mapped.get("identity").asText()).as("the stamp maps to the writer's identity").isEqualTo(id);
        assertThat(mapped.get("generation").asInt()).isEqualTo(1);
    }

    @Test
    void anIdentityTokenWinsOverAMintedSessionAndASupersededGenerationStampsItsOwnLastSession() throws Exception {
        String sid1 = "sess-" + UUID.randomUUID();
        String minted = "sess-" + UUID.randomUUID();
        JsonNode bound = createSeatAndBind(sid1, 32);
        String id = bound.get("id").asText();
        String token = bound.get("identity_token").asText();
        assertThat(stampOfOut(token, mintSession(minted))).as("both on one request: the identity's session").isEqualTo(sid1);
        assertThat(stampOfOut(null, mintSession(minted))).as("the minted session alone, as before").isEqualTo(minted);

        // A new process takes the seat (the client found the old holder gone): generation 2.
        String sid3 = "sess-" + UUID.randomUUID();
        Map<String, Object> resume = new LinkedHashMap<>(process(sid3, 33));
        resume.put("event", "resume");
        resume.put("expected_generation", 1);
        resume.put("holder_gone", process(sid1, 32).entrySet().stream().filter(e -> !e.getKey().equals("session_id"))
                .collect(java.util.stream.Collectors.toMap(Map.Entry::getKey, Map.Entry::getValue)));
        var second = sendIdentity(null, null, "POST", "/v1/identity/" + id + "/bind", resume);
        assertThat(second.statusCode()).as(second.body()).isEqualTo(200);
        String newToken = MAPPER.readTree(second.body()).get("identity_token").asText();
        assertThat(stampOfOut(newToken, null)).as("the new process").isEqualTo(sid3);
        assertThat(stampOfOut(token, null)).as("a send is open to a superseded token; its stamp is its own generation's session")
                .isEqualTo(sid1);
    }

    @Test
    void anAckReplyUnderAnIdentityToken_isStampedWithTheIdentitysCurrentSession() throws Exception {
        String sid1 = "sess-" + UUID.randomUUID();
        String sid2 = "sess-" + UUID.randomUUID();
        JsonNode bound = createSeatAndBind(sid1, 34);
        String id = bound.get("id").asText();
        String token = bound.get("identity_token").asText();
        String outbox = "http-" + UUID.randomUUID().toString().substring(0, 8);
        assertThat(send(TENANT_TOKEN, null, "/v1/tuples/out", mailboxOut(id, "n-1")).statusCode()).isEqualTo(200);
        var claim = sendIdentity(token, null, "POST", "/v1/tuples/inp", Map.of(
                "subspace", "mailbox/" + id, "keys_pattern", Map.of("to", id), "claimant", "w", "lease_s", 60));
        assertThat(claim.statusCode()).as(claim.body()).isEqualTo(200);
        String claimId = MAPPER.readTree(claim.body()).get("claim_id").asText();
        bindClear(id, sid2, 34);
        Map<String, Object> reply = new LinkedHashMap<>();
        reply.put("subspace", "mailbox/" + outbox);
        reply.put("keys", Map.of("to", outbox));
        reply.put("dims", Map.of("from", "someone"));
        reply.put("body", "answer");
        var ack = sendIdentity(token, null, "POST", "/v1/tuples/ack", Map.of("claim_id", claimId, "claimant", "w", "reply", reply));
        assertThat(ack.statusCode()).as(ack.body()).isEqualTo(200);
        assertThat(rdOnly(TENANT_TOKEN, "mailbox/" + outbox, outbox).get("sender_session").asText()).isEqualTo(sid2);
    }
}
