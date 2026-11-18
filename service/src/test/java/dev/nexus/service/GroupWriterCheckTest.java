// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.http.IdentityTestEnv.Resp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpResponse;
import java.util.Map;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1, the level-1 writer check on {@code group/<name>}: for an existing record the writer's identity
 * token names the stored leader at its current generation; for a first write the writer is the leader it names; a
 * write with no token is refused even when the leader is {@code unleased}. RDR-253's unattested exception covers
 * memberships only. A re-form of a disbanded name is checked as a first write, not against the old leader.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupWriterCheckTest {

    GroupTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = GroupTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    private static final Map<String, Object> SET = Map.of("ack_deadline", 30);

    @Test
    void aGroupWriteByANonLeaderIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);

        for (String route : new String[] {"/push-train/set", "/push-train/disband"}) {
            Resp r = env.as(t, b, "POST", route, SET);
            assertThat(r.status()).as(route).isEqualTo(403);
            assertThat(r.error()).as(route).isEqualTo("not_owner");
        }
        // A first write that names a leader other than the writer is the same refusal.
        Resp other = env.form(t, b, body("review-pair", a));
        assertThat(other.status()).isEqualTo(403);
        assertThat(other.error()).isEqualTo("not_owner");
        assertThat(env.read(t, "push-train").integer("ack_deadline")).as("nothing changed").isEqualTo(20);
        assertThat(env.read(t, "push-train").body().get("ended_at")).isNull();
        assertThat(env.read(t, "review-pair").status()).isEqualTo(404);
    }

    @Test
    void aSupersededProcessOfTheLeaderIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);

        env.supersede(t, a); // the seat's generation is 2 now; a token of generation 1 is the old process's
        Resp old = env.callAs(IdentityTestEnv.TENANT, t, a, 1, "POST", "/push-train/set", SET);
        assertThat(old.status()).isEqualTo(409);
        assertThat(old.error()).isEqualTo("generation_superseded");
        assertThat(env.read(t, "push-train").integer("ack_deadline")).isEqualTo(20);

        assertThat(env.as(t, a, "POST", "/push-train/set", SET).status()).as("the current process").isEqualTo(200);
        assertThat(env.read(t, "push-train").integer("ack_deadline")).isEqualTo(30);
    }

    @Test
    void aTokenOfALeaderThatEndedFencesTheGroupToo() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        env.end(t, a);
        Resp r = env.as(t, a, "POST", "/push-train/set", SET);
        assertThat(r.status()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("identity_not_held");
    }

    @Test
    void aTokenlessGroupWriteIsRefusedEvenWhenTheLeaderIsUnleased() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);

        Resp leased = env.tokenless(t, "POST", "/push-train/set", SET);
        assertThat(leased.status()).isEqualTo(403);
        assertThat(leased.error()).isEqualTo("identity_token_required");

        // The leader's seat is held by a Desktop session now: RDR-253 lets it write its own membership with no
        // token, and this RDR does not extend that to the group record.
        env.supersede(t, a, "unleased");
        assertThat(env.ids.show(t, a)).containsEntry("lease", "unleased").containsEntry("state", "live");
        for (String route : new String[] {"/push-train/set", "/push-train/disband"}) {
            Resp r = env.tokenless(t, "POST", route, SET);
            assertThat(r.status()).as(route).isEqualTo(403);
            assertThat(r.error()).as(route).isEqualTo("identity_token_required");
        }
        Resp form = env.tokenless(t, "POST", "", body("review-pair", a));
        assertThat(form.status()).isEqualTo(403);
        assertThat(form.error()).isEqualTo("identity_token_required");
        assertThat(env.read(t, "push-train").integer("ack_deadline")).isEqualTo(20);
        assertThat(env.read(t, "push-train").body().get("ended_at")).isNull();
    }

    @Test
    void aMembershipWrittenByAnotherIdentityIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        Resp r = env.ids.asOwner(t, b, "POST", "/" + a + "/memberships", Map.of("group", "push-train", "role", "member"));
        assertThat(r.status()).isEqualTo(403);
        assertThat(r.error()).isEqualTo("not_owner");
        Resp leave = env.ids.asOwner(t, b, "DELETE", "/" + a + "/memberships/push-train", null);
        assertThat(leave.status()).isEqualTo(403);
        assertThat(env.members(t, "push-train")).containsExactly(a);
    }

    @Test
    void aMembershipWriteForAnUnleasedIdentityIsAcceptedAndLabelledUnattested() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String d = env.desktopSeat(t, "desktop");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);

        HttpResponse<String> join = env.identityRaw(t, "POST", "/" + d + "/memberships",
            Map.of("group", "push-train", "role", "member"));
        assertThat(join.statusCode()).as(join.body()).isEqualTo(200);
        assertThat(join.headers().firstValue("X-Nexus-Unattested")).contains("true");
        assertThat(env.members(t, "push-train")).containsExactly(a, d);
    }

    @Test
    void aReformByASeatThatWasNotTheLeaderPassesTheCheckWhenTheOldLeaderIsNotLive() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        assertThat(env.as(t, a, "POST", "/push-train/disband", Map.of()).status()).isEqualTo(200);
        env.end(t, a);

        // b is not the stored leader, and the stored leader's token would not be b's: the check for a re-form is the
        // first write's, the writer names itself.
        Resp r = env.form(t, b, body("push-train", b));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(r.str("leader")).isEqualTo(b);
        assertThat(env.members(t, "push-train")).containsExactly(b);
        // And a write that names someone else is still refused, as for any first write.
        env.end(t, b);
        String c = env.seat(t, "gamma");
        assertThat(env.as(t, c, "POST", "", body("review-pair", b)).error()).isEqualTo("not_owner");
    }
}
