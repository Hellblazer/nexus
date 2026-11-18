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
import java.util.Map;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1: a first leader is a live, leased seat; a new leader is also a member of the group. Lapsed,
 * unleased, ephemeral and delegated identities are refused, and a refused write changes nothing.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupLeaderRulesTest {

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
    void aNewLeaderThatIsNotALiveLeasedMemberIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        String d = env.desktopSeat(t, "desktop");
        String lapsing = env.seat(t, "lapsing");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        Map<String, Object> toB = Map.of("leader", b);

        // A live, leased seat that is not a member yet.
        Resp notMember = env.as(t, a, "POST", "/push-train/set", toB);
        assertThat(notMember.status()).isEqualTo(409);
        assertThat(notMember.error()).isEqualTo("leader_not_member");

        // A member that is unleased (a Desktop seat joins with no token) cannot lead.
        assertThat(env.identityRaw(t, "POST", "/" + d + "/memberships", Map.of("group", "push-train", "role", "member")).statusCode())
            .isEqualTo(200);
        Resp unleased = env.as(t, a, "POST", "/push-train/set", Map.of("leader", d));
        assertThat(unleased.status()).isEqualTo(409);
        assertThat(unleased.error()).isEqualTo("bad_leader");

        // A member whose lease lapsed cannot lead either.
        assertThat(env.join(t, lapsing, "push-train", "member").status()).isEqualTo(200);
        assertThat(env.join(t, b, "push-train", "member").status()).isEqualTo(200);
        env.ids.clock.advance(Duration.ofSeconds(301));
        env.renew(t, a);
        env.renew(t, b);
        Resp lapsed = env.as(t, a, "POST", "/push-train/set", Map.of("leader", lapsing));
        assertThat(lapsed.status()).isEqualTo(409);
        assertThat(lapsed.error()).isEqualTo("bad_leader");
        assertThat(env.read(t, "push-train").str("leader")).as("nothing changed").isEqualTo(a);

        // A live, leased member takes it.
        Resp ok = env.as(t, a, "POST", "/push-train/set", toB);
        assertThat(ok.status()).as(ok.toString()).isEqualTo(200);
        assertThat(ok.str("leader")).isEqualTo(b);
        // The old leader's token no longer writes the record: the stored leader is b.
        assertThat(env.as(t, a, "POST", "/push-train/set", Map.of("ack_deadline", 30)).error()).isEqualTo("not_owner");
        assertThat(env.as(t, b, "POST", "/push-train/set", Map.of("ack_deadline", 30)).status()).isEqualTo(200);
    }

    @Test
    void aFirstWriteNamingALapsedOrUnleasedLeaderIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String lapsed = env.seat(t, "lapsed");
        String desktop = env.desktopSeat(t, "desktop");
        String eph = env.ephemeral(t, "scratch");
        env.ids.clock.advance(Duration.ofSeconds(301)); // the lapse is due for everything bound before this

        Resp l = env.form(t, lapsed, body("push-train", lapsed));
        assertThat(l.status()).as("a lapsed holder still writes with its token, but cannot lead: %s", l).isEqualTo(409);
        assertThat(l.error()).isEqualTo("bad_leader");

        String fresh = env.seat(t, "fresh-desktop");
        env.supersede(t, fresh, "unleased");
        Resp u = env.form(t, fresh, body("push-train", fresh));
        assertThat(u.status()).isEqualTo(409);
        assertThat(u.error()).isEqualTo("bad_leader");
        Resp d = env.form(t, desktop, body("push-train", desktop));
        assertThat(d.status()).isEqualTo(409);

        String eph2 = env.ephemeral(t, "scratch-2");
        Resp e = env.form(t, eph2, body("push-train", eph2));
        assertThat(e.status()).as("an ephemeral identity is not a seat: %s", e).isEqualTo(409);
        assertThat(e.error()).isEqualTo("bad_leader");
        assertThat(env.read(t, "push-train").status()).as("no refused write left a record").isEqualTo(404);
        assertThat(eph).isNotBlank();

        String ok = env.seat(t, "live");
        assertThat(env.form(t, ok, body("push-train", ok)).status()).isEqualTo(200);
    }
}
