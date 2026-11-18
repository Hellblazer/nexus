// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.time.Duration;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1: the operator override. The tenant credential, with no identity token, sets a new leader or
 * {@code ended_at}, only while the leader's identity cannot write the record ({@code ended}, {@code unheld},
 * {@code lapsed} with a recorded gone, or {@code live} with lease {@code unleased}); it is audited as
 * {@code group_override} in the same transaction.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupOverrideTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    GroupTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = GroupTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    private Resp override(String t, String name, Map<String, Object> body) {
        return env.tokenless(t, "POST", "/" + name + "/override", body);
    }

    /** A group led by a fresh seat with a second member, b, ready to take over. */
    private String[] crew(String t, String name) {
        String a = env.seat(t, "lead-" + name);
        String b = env.seat(t, "next-" + name);
        assertThat(env.form(t, a, body(name, a, List.of("topic-" + name))).status()).isEqualTo(200);
        assertThat(env.join(t, b, name, "member").status()).isEqualTo(200);
        return new String[] {a, b};
    }

    @Test
    void theOverrideIsAcceptedAndAuditedWhenTheLeaderEnded() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String[] ab = crew(t, "push-train");
        env.end(t, ab[0]);

        Resp r = override(t, "push-train", Map.of("leader", ab[1]));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(r.str("leader")).isEqualTo(ab[1]);
        assertThat(env.read(t, "push-train").str("leader")).isEqualTo(ab[1]);

        var rows = env.ids.audit(t, "group_override");
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).get(0)).startsWith("operator:tenant");
        JsonNode d = JSON.readTree(rows.get(0).get(1));
        assertThat(d.get("group").asText()).isEqualTo("push-train");
        assertThat(d.get("leader_state").asText()).isEqualTo("ended");
        assertThat(d.get("old").get("leader").asText()).isEqualTo(ab[0]);
        assertThat(d.get("new").get("leader").asText()).isEqualTo(ab[1]);
        // The new leader writes the record now; the ended one's token does not.
        assertThat(env.as(t, ab[1], "POST", "/push-train/set", Map.of("ack_deadline", 30)).status()).isEqualTo(200);
    }

    @Test
    void theOverrideCanSetEndedAtAndThatDisbandsWithItsCascade() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String[] ab = crew(t, "push-train");
        env.release(t, ab[0]); // unheld

        Resp r = override(t, "push-train", Map.of("ended_at", true));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(r.body().get("ended_at")).isNotNull();
        assertThat(env.members(t, "push-train")).isEmpty();
        var rows = env.ids.audit(t, "group_override");
        assertThat(rows).hasSize(1);
        JsonNode d = JSON.readTree(rows.get(0).get(1));
        assertThat(d.get("leader_state").asText()).isEqualTo("unheld");
        assertThat(d.get("memberships_removed").asInt()).isEqualTo(2);
        assertThat(override(t, "push-train", Map.of("ended_at", true)).error()).as("set once").isEqualTo("group_disbanded");
    }

    @Test
    void theOverrideIsAcceptedForALeaderLiveWithLeaseUnleased() {
        String t = IdentityTestEnv.freshTenant();
        String[] ab = crew(t, "push-train");
        env.supersede(t, ab[0], "unleased"); // a Desktop session holds the leader's seat now: liveness unknown, no token
        assertThat(env.ids.show(t, ab[0])).containsEntry("state", "live").containsEntry("lease", "unleased");

        Resp r = override(t, "push-train", Map.of("leader", ab[1]));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(r.str("leader")).isEqualTo(ab[1]);
        assertThat(env.ids.audit(t, "group_override")).hasSize(1);
    }

    @Test
    void theOverrideForALapsedLeaderNeedsARecordedGone() {
        String t = IdentityTestEnv.freshTenant();
        String[] ab = crew(t, "push-train");
        env.ids.clock.advance(Duration.ofSeconds(301));
        env.renew(t, ab[1]);

        Resp notGone = override(t, "push-train", Map.of("leader", ab[1]));
        assertThat(notGone.status()).as("lapsed, no verdict: the holder may only be asleep").isEqualTo(409);
        assertThat(notGone.error()).isEqualTo("leader_not_overridable");
        assertThat(env.ids.audit(t, "group_override")).isEmpty();

        env.gone(t, ab[0]);
        assertThat(override(t, "push-train", Map.of("leader", ab[1])).status()).isEqualTo(200);
        assertThat(env.ids.audit(t, "group_override")).hasSize(1);
    }

    @Test
    void theOverrideIsRefusedWhileTheLeaderIsLiveAndLeased() {
        String t = IdentityTestEnv.freshTenant();
        String[] ab = crew(t, "push-train");
        Resp r = override(t, "push-train", Map.of("leader", ab[1]));
        assertThat(r.status()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("leader_not_overridable");
        assertThat(override(t, "push-train", Map.of("ended_at", true)).error()).isEqualTo("leader_not_overridable");
        assertThat(env.read(t, "push-train").str("leader")).isEqualTo(ab[0]);
        assertThat(env.read(t, "push-train").body().get("ended_at")).isNull();
        assertThat(env.members(t, "push-train")).hasSize(2);
        assertThat(env.ids.audit(t, "group_override")).as("a refusal writes no audit row").isEmpty();
    }

    @Test
    void onlyTheTenantCredentialOverridesAndItNeedsTheRightFields() {
        String t = IdentityTestEnv.freshTenant();
        String[] ab = crew(t, "push-train");
        env.end(t, ab[0]);

        Resp data = env.callAs(IdentityTestEnv.DATA, t, null, 0, "POST", "/push-train/override", Map.of("leader", ab[1]));
        assertThat(data.status()).isEqualTo(403);
        assertThat(env.read(t, "push-train").str("leader")).isEqualTo(ab[0]);

        assertThat(override(t, "push-train", Map.of()).error()).isEqualTo("bad_override");
        assertThat(override(t, "push-train", Map.of("leader", ab[1], "ended_at", true)).error()).isEqualTo("bad_override");
        assertThat(override(t, "no-such-group", Map.of("ended_at", true)).status()).isEqualTo(404);

        // The new leader has the leader rules too: a seat that is not a member, and one that is unleased, are refused.
        String c = env.seat(t, "outsider");
        assertThat(override(t, "push-train", Map.of("leader", c)).error()).isEqualTo("leader_not_member");
        String d = env.desktopSeat(t, "desktop");
        env.identityRaw(t, "POST", "/" + d + "/memberships", Map.of("group", "push-train", "role", "member"));
        assertThat(override(t, "push-train", Map.of("leader", d)).error()).isEqualTo("bad_leader");
        assertThat(env.ids.audit(t, "group_override")).isEmpty();
    }

    @Test
    void theOverrideSetsANewLeaderForAnUnheldLeader() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String[] ab = crew(t, "push-train");
        env.release(t, ab[0]);
        assertThat(env.ids.show(t, ab[0])).containsEntry("state", "unheld");

        Resp r = override(t, "push-train", Map.of("leader", ab[1]));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(r.str("leader")).isEqualTo(ab[1]);
        JsonNode d = JSON.readTree(env.ids.audit(t, "group_override").get(0).get(1));
        assertThat(d.get("leader_state").asText()).isEqualTo("unheld");
    }

    @Test
    void theOverrideIsAcceptedWhenTheLeadersIdentityRowIsGone() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String b = env.seat(t, "next-ghost");
        assertThat(env.join(t, b, "ghost-crew", "member").status()).isEqualTo(200);
        // A group record whose leader identity does not exist (purged, or never there): the override's reason to exist.
        env.ids.scope.withTenant(t, ctx -> ctx.insertInto(dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS)
            .set(dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS.TENANT_ID, t)
            .set(dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS.NAME, "ghost-crew")
            .set(dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS.LEADER, "i424242")
            .set(dev.nexus.service.jooq.nexus.Tables.GROUP_RECORDS.FORMED_AT, java.time.OffsetDateTime.now()).execute());

        Resp r = override(t, "ghost-crew", Map.of("leader", b));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(r.str("leader")).isEqualTo(b);
        JsonNode d = JSON.readTree(env.ids.audit(t, "group_override").get(0).get(1));
        assertThat(d.get("leader_state").asText()).isEqualTo("gone");
        assertThat(d.get("old").get("leader").asText()).isEqualTo("i424242");
    }
}
