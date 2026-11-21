// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 (bead nexus-igea9.4): a {@code kind=ack} write to a {@code board/ci/<topic>--owner} subspace is stamped with
 * the identity token's id as its {@code from}. The owner decision counts an ack by the session stamp, never by
 * {@code from}, but {@code nx group ack-times} reads {@code from} (marginalia, 2026-11-20, sha ae619ef: an alias in
 * {@code from} was accepted and then counted as no ack). Over a real HTTP round trip through the real
 * {@code AuthFilter}: an absent or blank {@code from} is filled, a matching one is unchanged, a different one (an alias)
 * is overwritten and the write accepted; nothing is refused. A write with no identity token is unchanged, and other
 * kinds and other topics are untouched.
 */
class OwnerAckFromTest extends IdentityFenceHarness {

    private static String topic() {
        return "ackfrom" + UUID.randomUUID().toString().substring(0, 8) + "--owner";
    }

    private Map<String, Object> ackOut(String topic, Map<String, String> dims) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("subspace", "board/ci/" + topic);
        m.put("keys", Map.of("topic", topic));
        m.put("dims", dims);
        m.put("body", "{\"note\":\"mine\"}");
        m.put("nonce", UUID.randomUUID().toString());
        return m;
    }

    private static Map<String, String> dims(String from, String kind, String sha) {
        Map<String, String> d = new LinkedHashMap<>();
        if (from != null) {
            d.put("from", from);
        }
        d.put("kind", kind);
        d.put("sha", sha);
        return d;
    }

    private JsonNode rows(String subspace) {
        Resp r = post(null, "/v1/tuples/rdp", Map.of("subspace", subspace, "keys_pattern", Map.of(), "n", 50));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        return r.json().get("tuples");
    }

    @Test
    void anAliasInFromIsOverwrittenWithTheTokensIdentityAndTheWriteIsAccepted() {
        Seat alpha = bindSeat("ackfrom-a", 901);
        String topic = topic();

        Resp r = post(alpha.token(), "/v1/tuples/out", ackOut(topic, dims("marginalia", "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        JsonNode row = rows("board/ci/" + topic).get(0);
        assertThat(row.get("dims").get("from").asText()).as("the alias is replaced by the identity id").isEqualTo(alpha.id());
        assertThat(row.get("dims").get("sha").asText()).isEqualTo("S1");
        assertThat(row.get("sender_session").asText()).isEqualTo(alpha.sessionId());
    }

    @Test
    void anotherSeatsIdInFromIsOverwrittenToo() {
        Seat alpha = bindSeat("ackfrom-b", 902);
        Seat carol = bindSeat("ackfrom-c", 903);
        String topic = topic();

        Resp r = post(alpha.token(), "/v1/tuples/out", ackOut(topic, dims(carol.id(), "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(rows("board/ci/" + topic).get(0).get("dims").get("from").asText()).isEqualTo(alpha.id());
    }

    @Test
    void anAliasOnAReplaceWriteIsOverwrittenToo() {
        Seat alpha = bindSeat("ackfrom-d", 904);
        String topic = topic();
        Map<String, Object> body = ackOut(topic, dims("marginalia", "ack", "S1"));
        body.put("mode", "replace");

        Resp r = post(alpha.token(), "/v1/tuples/out", body);

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(rows("board/ci/" + topic).get(0).get("dims").get("from").asText()).isEqualTo(alpha.id());
    }

    @Test
    void fromEngineIsStillRefusedBeforeAnyStamping() {
        Seat alpha = bindSeat("ackfrom-j", 910);
        String topic = topic();

        Resp r = post(alpha.token(), "/v1/tuples/out", ackOut(topic, dims("engine", "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(403);
        assertThat(r.raw()).contains("reserved_dim");
    }

    @Test
    void anAbsentFromIsStampedWithTheTokensIdentity() {
        Seat alpha = bindSeat("ackfrom-e", 905);
        String topic = topic();

        Resp r = post(alpha.token(), "/v1/tuples/out", ackOut(topic, dims(null, "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        JsonNode row = rows("board/ci/" + topic).get(0);
        assertThat(row.get("dims").get("from").asText()).isEqualTo(alpha.id());
        assertThat(row.get("dims").get("kind").asText()).isEqualTo("ack");
        assertThat(row.get("sender_session").asText()).as("the stamp the owner decision maps to the identity")
            .isEqualTo(alpha.sessionId());
    }

    @Test
    void aMatchingFromIsAccepted() {
        Seat alpha = bindSeat("ackfrom-f", 906);
        String topic = topic();

        Resp r = post(alpha.token(), "/v1/tuples/out", ackOut(topic, dims(alpha.id(), "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(rows("board/ci/" + topic).get(0).get("dims").get("from").asText()).isEqualTo(alpha.id());
    }

    private String mintSession(String sessionId) {
        Resp r = call(TENANT_TOKEN, null, null, "POST", "/v1/sessions/start", Map.of("session_id", sessionId));
        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        return r.text("session_token");
    }

    private Resp outWithSession(String sessionToken, Map<String, Object> body) {
        return call(TENANT_TOKEN, null, sessionToken, "POST", "/v1/tuples/out", body);
    }

    /**
     * A token of a superseded generation still resolves, and {@code IdentityFence.senderSession} still finds the
     * session of the newest binding row under that old generation (rows are kept), which the session index still maps
     * to the identity. So the stamp and the label agree: both name the identity. This pins that the label comes from the
     * row's own stamp, not from the token: a stamp the index does not map leaves {@code from} as sent
     * ({@link #anAckFromAMintedSessionNoIdentityHoldsKeepsItsFromAsSent}).
     */
    @Test
    void aSupersededGenerationTokenIsLabelledWithTheIdentityItsOwnStampMapsTo() {
        Seat old = bindSeat("ackfrom-k", 911);
        Seat current = resumeInNewProcess(old, 912, false);
        String topic = topic();

        Resp stale = post(old.token(), "/v1/tuples/out", ackOut(topic, dims("marginalia", "ack", "S1")));

        assertThat(stale.status()).as(stale.raw()).isEqualTo(200);
        JsonNode row = rows("board/ci/" + topic).get(0);
        assertThat(row.get("sender_session").asText()).as("the old process's own session").isEqualTo(old.sessionId());
        assertThat(row.get("dims").get("from").asText()).isEqualTo(old.id());
        String topic2 = topic();
        assertThat(post(current.token(), "/v1/tuples/out", ackOut(topic2, dims("marginalia", "ack", "S1"))).status()).isEqualTo(200);
        JsonNode row2 = rows("board/ci/" + topic2).get(0);
        assertThat(row2.get("sender_session").asText()).isEqualTo(current.sessionId());
        assertThat(row2.get("dims").get("from").asText()).isEqualTo(old.id());
    }

    @Test
    void aDelegatedChildsAckIsStampedWithItsParentBecauseThatIsWhoIsCounted() {
        Seat parent = bindSeat("ackfrom-l", 913);
        String childSession = "sess-child-" + UUID.randomUUID();
        Resp child = post(null, "/v1/identity/delegated", Map.of("parent", parent.id(), "display_name", "child",
            "bind", evidence(childSession, 914, null)));
        assertThat(child.status()).as(child.raw()).isEqualTo(200);
        String topic = topic();

        Resp r = post(child.text("identity_token"), "/v1/tuples/out", ackOut(topic, dims("marginalia", "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        JsonNode row = rows("board/ci/" + topic).get(0);
        assertThat(row.get("dims").get("from").asText()).as("the parent's id, not the child's").isEqualTo(parent.id());
        assertThat(row.get("sender_session").asText()).isEqualTo(childSession);
    }

    @Test
    void aTokenlessAckFromAMintedSessionTheIndexMapsIsStamped() {
        Seat alpha = bindSeat("ackfrom-m", 915);
        String topic = topic();

        Resp r = outWithSession(mintSession(alpha.sessionId()), ackOut(topic, dims("marginalia", "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        JsonNode row = rows("board/ci/" + topic).get(0);
        assertThat(row.get("sender_session").asText()).isEqualTo(alpha.sessionId());
        assertThat(row.get("dims").get("from").asText()).isEqualTo(alpha.id());
    }

    @Test
    void anAckFromAMintedSessionNoIdentityHoldsKeepsItsFromAsSent() {
        String session = "sess-nobody-" + UUID.randomUUID();
        String topic = topic();

        Resp r = outWithSession(mintSession(session), ackOut(topic, dims("marginalia", "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        JsonNode row = rows("board/ci/" + topic).get(0);
        assertThat(row.get("sender_session").asText()).isEqualTo(session);
        assertThat(row.get("dims").get("from").asText()).isEqualTo("marginalia");
    }

    @Test
    void anAckToAnOwnerNamedSubspaceUnderAnotherTemplateIsNotStamped() {
        Seat alpha = bindSeat("ackfrom-n", 916);
        String topic = "x" + UUID.randomUUID().toString().substring(0, 8) + "--owner";
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("subspace", "board/" + topic);
        body.put("keys", Map.of("topic", topic));
        body.put("dims", Map.of("from", "marginalia", "kind", "ack"));
        body.put("body", "{}");
        body.put("nonce", UUID.randomUUID().toString());

        Resp r = post(alpha.token(), "/v1/tuples/out", body);

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(rows("board/" + topic).get(0).get("dims").get("from").asText())
            .as("board/<topic> is another template: only board/ci/<topic>--owner acks are stamped").isEqualTo("marginalia");
    }

    @Test
    void aBlankFromIsStampedLikeAnAbsentOne() {
        Seat alpha = bindSeat("ackfrom-g", 907);
        String topic = topic();

        Resp r = post(alpha.token(), "/v1/tuples/out", ackOut(topic, dims("  ", "ack", "S1")));

        assertThat(r.status()).as(r.raw()).isEqualTo(200);
        assertThat(rows("board/ci/" + topic).get(0).get("dims").get("from").asText()).isEqualTo(alpha.id());
    }

    @Test
    void aWriteWithNoIdentityTokenKeepsToday() {
        String topic = topic();

        Resp r = post(null, "/v1/tuples/out", ackOut(topic, dims("marginalia", "ack", "S1")));

        assertThat(r.status()).as("RDR-252 sets no token requirement on an ack: a tenant credential's ack is unchanged").isEqualTo(200);
        assertThat(rows("board/ci/" + topic).get(0).get("dims").get("from").asText()).isEqualTo("marginalia");
    }

    @Test
    void otherKindsAndOtherTopicsAreUntouched() {
        Seat alpha = bindSeat("ackfrom-h", 908);
        String ownerTopic = topic();

        // A kind other than ack on an --owner topic keeps its from.
        Resp note = post(alpha.token(), "/v1/tuples/out", ackOut(ownerTopic, dims("marginalia", "note", "S1")));
        assertThat(note.status()).as(note.raw()).isEqualTo(200);
        assertThat(rows("board/ci/" + ownerTopic).get(0).get("dims").get("from").asText()).isEqualTo("marginalia");

        // An ack on a topic that is not --owner keeps its from, absent or not.
        String plain = "ackfrom" + UUID.randomUUID().toString().substring(0, 8);
        Resp onPlain = post(alpha.token(), "/v1/tuples/out", ackOut(plain, dims("marginalia", "ack", "S1")));
        assertThat(onPlain.status()).as(onPlain.raw()).isEqualTo(200);
        assertThat(rows("board/ci/" + plain).get(0).get("dims").get("from").asText()).isEqualTo("marginalia");
    }

    @Test
    void anAckReplyIsStampedLikeAnOut() {
        Seat alpha = bindSeat("ackfrom-i", 909);
        String topic = topic();
        String inbox = "ackinbox" + UUID.randomUUID().toString().substring(0, 8);
        Resp sent = post(null, "/v1/tuples/out", Map.of("subspace", "mailbox/" + inbox, "keys", Map.of("to", inbox),
            "dims", Map.of("from", "someone"), "body", "work", "nonce", UUID.randomUUID().toString()));
        assertThat(sent.status()).as(sent.raw()).isEqualTo(200);
        Resp claim = post(null, "/v1/tuples/inp", Map.of("subspace", "mailbox/" + inbox, "keys_pattern", Map.of("to", inbox),
            "claimant", "w", "lease_s", 60));
        assertThat(claim.status()).as(claim.raw()).isEqualTo(200);
        String claimId = claim.text("claim_id");

        Resp stamped = post(alpha.token(), "/v1/tuples/ack", Map.of("claim_id", claimId, "claimant", "w", "reply", Map.of(
            "subspace", "board/ci/" + topic, "keys", Map.of("topic", topic), "dims", dims("marginalia", "ack", "S1"), "body", "{}")));
        assertThat(stamped.status()).as(stamped.raw()).isEqualTo(200);
        assertThat(rows("board/ci/" + topic).get(0).get("dims").get("from").asText()).isEqualTo(alpha.id());
    }
}
