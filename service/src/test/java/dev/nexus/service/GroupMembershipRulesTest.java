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

import java.time.Duration;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1, the three rules a group record adds to RDR-253's membership routes: a join to a group that has a
 * record needs a seat; a join to a disbanded group is refused (with {@code ended_at} set, and with {@code ends}
 * passed); removing the current leader's membership is refused. A disband's cascade is exempt from all three.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupMembershipRulesTest {

    GroupTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = GroupTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    @Test
    void anEphemeralOrADelegatedIdentityCannotJoinAGroupThatHasARecord() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        String eph = env.ephemeral(t, "scratch-session");
        String kid = env.delegated(t, a, "child");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);

        for (String who : new String[] {eph, kid}) {
            Resp r = env.join(t, who, "push-train", "member");
            assertThat(r.status()).as(who).isEqualTo(403);
            assertThat(r.error()).as(who).isEqualTo("seat_required");
        }
        assertThat(env.join(t, b, "push-train", "member").status()).as("a seat joins").isEqualTo(200);
        assertThat(env.members(t, "push-train")).containsExactly(a, b);

        // The rule is for a group that has a record: a name nobody formed takes any identity, as before.
        assertThat(env.join(t, eph, "no-record-yet", "member").status()).isEqualTo(200);
        assertThat(env.join(t, kid, "no-record-yet", "member").status()).isEqualTo(200);
    }

    @Test
    void theLeaderCannotLeave() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        assertThat(env.join(t, b, "push-train", "member").status()).isEqualTo(200);

        Resp r = env.leave(t, a, "push-train");
        assertThat(r.status()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("leader_cannot_leave");
        assertThat(env.members(t, "push-train")).containsExactly(a, b);

        assertThat(env.leave(t, b, "push-train").integer("removed")).as("a member leaves").isEqualTo(1);
        // The leader hands the group on first: b is not a member any more, so it joins, and then takes it.
        assertThat(env.join(t, b, "push-train", "member").status()).isEqualTo(200);
        assertThat(env.as(t, a, "POST", "/push-train/set", Map.of("leader", b)).status()).isEqualTo(200);
        assertThat(env.leave(t, a, "push-train").integer("removed")).as("a is an ordinary member now").isEqualTo(1);
        Resp nowLeader = env.leave(t, b, "push-train");
        assertThat(nowLeader.status()).isEqualTo(409);
        // A group nobody formed: any identity leaves, and leaving one never joined removes nothing.
        assertThat(env.leave(t, a, "never-formed").integer("removed")).isZero();
    }

    @Test
    void aJoinToADisbandedGroupIsRefusedWithEndedAtSet() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        assertThat(env.as(t, a, "POST", "/push-train/disband", Map.of()).status()).isEqualTo(200);

        Resp r = env.join(t, b, "push-train", "member");
        assertThat(r.status()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("group_disbanded");
        assertThat(env.members(t, "push-train")).isEmpty();
    }

    @Test
    void aJoinToADisbandedGroupIsRefusedWithEndsPassedAndItsMembershipRowsStayUntilEndedAtIsSet() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        String c = env.seat(t, "gamma");
        String soon = env.ids.clock.instant().plus(Duration.ofHours(1)).toString();
        assertThat(env.form(t, a, IdentityTestEnv.with(body("push-train", a), "ends", soon)).status()).isEqualTo(200);
        assertThat(env.join(t, b, "push-train", "member").status()).isEqualTo(200);

        env.ids.clock.advance(Duration.ofHours(2));
        Resp r = env.join(t, c, "push-train", "member");
        assertThat(r.status()).isEqualTo(409);
        assertThat(r.error()).isEqualTo("group_disbanded");
        assertThat(env.read(t, "push-train").body().get("live")).isEqualTo(false);
        assertThat(env.members(t, "push-train")).as("no cascade: ended_at is null").containsExactly(a, b);
        assertThat(env.leave(t, a, "push-train").error()).as("the leader still hands the group on first").isEqualTo("leader_cannot_leave");

        // The leader sets ended_at: the cascade runs now.
        env.renew(t, a);
        Resp ended = env.as(t, a, "POST", "/push-train/set", Map.of("ended_at", true));
        assertThat(ended.status()).as(ended.toString()).isEqualTo(200);
        assertThat(env.members(t, "push-train")).isEmpty();
    }

    @Test
    void disbandRemovesEveryMembershipTheLeadersIncluded() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        String c = env.seat(t, "gamma");
        assertThat(env.form(t, a, body("push-train", a, List.of("nexus-develop"))).status()).isEqualTo(200);
        env.join(t, b, "push-train", "member");
        env.join(t, c, "push-train", "member");
        env.join(t, b, "other-crew", "member"); // another group's row is not touched
        assertThat(env.members(t, "push-train")).containsExactly(a, b, c);

        Resp r = env.as(t, a, "POST", "/push-train/disband", Map.of());
        assertThat(r.status()).isEqualTo(200);
        assertThat(r.body().get("ended_at")).isNotNull();
        assertThat(r.body().get("live")).isEqualTo(false);
        assertThat(env.members(t, "push-train")).as("the cascade removed the leader's row too").isEmpty();
        assertThat(env.members(t, "other-crew")).containsExactly(b);
        assertThat(env.ids.get(t, "/" + a + "/memberships").list("memberships")).isEmpty();

        // ended_at is set once: a second disband, and any update, is refused until the name is re-formed.
        Resp again = env.as(t, a, "POST", "/push-train/disband", Map.of());
        assertThat(again.status()).isEqualTo(409);
        assertThat(again.error()).isEqualTo("group_disbanded");
        assertThat(env.as(t, a, "POST", "/push-train/set", Map.of("ack_deadline", 30)).error()).isEqualTo("group_disbanded");
    }

    @Test
    void aMemberThatJoinsAgainKeepsItsJoinTimeAndALeaveThenJoinStartsAFreshOne() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        String first = env.ids.clock.instant().toString();
        assertThat(env.join(t, b, "push-train", "member").str("joined_at")).isEqualTo(first);

        env.ids.clock.advance(Duration.ofHours(3));
        // The same identity joins again (a role change, an idempotent retry): the join time stays, so the roster keeps its order.
        Resp again = env.join(t, b, "push-train", "leader");
        assertThat(again.status()).isEqualTo(200);
        assertThat(again.str("joined_at")).as("the join time of a membership that exists").isEqualTo(first);
        assertThat(again.str("role")).isEqualTo("leader");
        assertThat(env.roster(t, "push-train")).extracting(m -> m.get("identity")).containsExactly(a, b);
        assertThat(env.roster(t, "push-train").get(1)).containsEntry("joined_at", first);

        // Leaving removes the row; a later join is a new membership with the time it was made.
        assertThat(env.leave(t, b, "push-train").integer("removed")).isEqualTo(1);
        String later = env.ids.clock.instant().toString();
        assertThat(env.join(t, b, "push-train", "member").str("joined_at")).isEqualTo(later).isNotEqualTo(first);
    }
}
