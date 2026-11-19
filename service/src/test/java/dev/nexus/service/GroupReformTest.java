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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1: a disbanded name is re-formed in place, by the former leader while it is live and by any live,
 * leased seat when it is not. The re-form is a first write: the writer is the leader and the only member, every field
 * comes from the call, and {@code gc_audit} keeps the history with the old record's fields.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupReformTest {

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

    private Resp disband(String t, String id, String name) {
        return env.as(t, id, "POST", "/" + name + "/disband", Map.of());
    }

    @Test
    void theFormerLeaderReformsADisbandedName() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        assertThat(env.form(t, a, body("push-train", a, List.of("nexus-develop"))).status()).isEqualTo(200);
        // A live record is not re-formed: the name is taken.
        Resp taken = env.form(t, a, body("push-train", a));
        assertThat(taken.status()).isEqualTo(409);
        assertThat(taken.error()).isEqualTo("group_exists");

        assertThat(disband(t, a, "push-train").status()).isEqualTo(200);
        assertThat(env.read(t, "push-train").body().get("ended_at")).isNotNull();
        assertThat(env.groups.servingGroup(t, "nexus-develop")).isEmpty();

        Resp again = env.form(t, a, IdentityTestEnv.with(body("push-train", a, List.of("nexus-main")), "purpose", "second life"));
        assertThat(again.status()).as(again.toString()).isEqualTo(200);
        assertThat(again.body().get("ended_at")).as("ended_at is cleared by a re-form").isNull();
        assertThat(again.str("purpose")).isEqualTo("second life");
        assertThat(again.body().get("serves")).as("every field comes from the form call").isEqualTo(List.of("nexus-main"));
        assertThat(again.body().get("live")).isEqualTo(true);
    }

    @Test
    void anotherSeatMayReformOnlyWhenTheFormerLeaderIsNotLive() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a)).status()).isEqualTo(200);
        assertThat(disband(t, a, "push-train").status()).isEqualTo(200);

        Resp refused = env.form(t, b, body("push-train", b));
        assertThat(refused.status()).isEqualTo(403);
        assertThat(refused.error()).isEqualTo("reform_needs_former_leader");
        assertThat(env.read(t, "push-train").body().get("ended_at")).as("still disbanded").isNotNull();

        // The former leader lapses (300 seconds without a renewal) and its client records that the holder is gone.
        env.ids.clock.advance(Duration.ofSeconds(301));
        env.renew(t, b);
        Resp lapsed = env.form(t, b, body("push-train", b));
        assertThat(lapsed.status()).as("lapsed is not live: %s", lapsed).isEqualTo(200);
        assertThat(lapsed.str("leader")).isEqualTo(b);

        // A former leader that ended is not live either.
        assertThat(disband(t, b, "push-train").status()).isEqualTo(200);
        String c = env.seat(t, "gamma");
        assertThat(env.form(t, c, body("push-train", c)).error()).as("b is live again: only b may").isEqualTo("reform_needs_former_leader");
        env.end(t, b);
        assertThat(env.form(t, c, body("push-train", c)).status()).isEqualTo(200);
    }

    @Test
    void aReformStartsWithOnlyTheLeaderAsMember() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        String c = env.seat(t, "gamma");
        String soon = env.ids.clock.instant().plus(Duration.ofHours(1)).toString();
        assertThat(env.form(t, a, IdentityTestEnv.with(body("push-train", a), "ends", soon)).status()).isEqualTo(200);
        assertThat(env.join(t, b, "push-train", "member").status()).isEqualTo(200);
        assertThat(env.join(t, c, "push-train", "member").status()).isEqualTo(200);

        // ends passes: the record is disbanded for joins and re-forms, but ended_at is null, so no cascade ran.
        env.ids.clock.advance(Duration.ofHours(2));
        assertThat(env.members(t, "push-train")).containsExactly(a, b, c);
        env.renew(t, a);
        Resp r = env.form(t, a, body("push-train", a));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(env.members(t, "push-train")).containsExactly(a);
        assertThat(env.roster(t, "push-train").get(0)).containsEntry("role", "leader");
        assertThat(env.ids.get(t, "/" + b + "/memberships").list("memberships")).isEmpty();
    }

    @Test
    void twoConcurrentReformsOfOneDisbandedNameLeaveOne() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        var pool = Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < 4; round++) {
                String a = env.seat(t, "alpha" + round);
                String b = env.seat(t, "beta" + round);
                String c = env.seat(t, "gamma" + round);
                String name = "reform-" + round;
                assertThat(env.form(t, a, body(name, a)).status()).isEqualTo(200);
                assertThat(disband(t, a, name).status()).isEqualTo(200);
                env.end(t, a); // the former leader is not live, so any live, leased seat may re-form
                CountDownLatch go = new CountDownLatch(1);
                Future<Resp> fb = pool.submit(() -> {
                    go.await();
                    return env.form(t, b, body(name, b));
                });
                Future<Resp> fc = pool.submit(() -> {
                    go.await();
                    return env.form(t, c, body(name, c));
                });
                go.countDown();
                Resp rb = fb.get();
                Resp rc = fc.get();
                assertThat((rb.status() == 200 ? 1 : 0) + (rc.status() == 200 ? 1 : 0)).as("%s / %s", rb, rc).isEqualTo(1);
                Resp loser = rb.status() == 200 ? rc : rb;
                assertThat(loser.status()).isEqualTo(409);
                assertThat(loser.error()).isEqualTo("group_exists");
                String winner = rb.status() == 200 ? b : c;
                assertThat(env.read(t, name).str("leader")).isEqualTo(winner);
                assertThat(env.members(t, name)).containsExactly(winner);
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void gcAuditHoldsTheDisbandAndTheReformWithTheOldRecordsFields() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        Resp formed = env.form(t, a, IdentityTestEnv.with(body("push-train", a, List.of("nexus-develop")),
            "purpose", "first", "ack_deadline", 30));
        assertThat(formed.status()).isEqualTo(200);
        assertThat(disband(t, a, "push-train").status()).isEqualTo(200);
        assertThat(env.form(t, a, IdentityTestEnv.with(body("push-train", a), "purpose", "second")).status()).isEqualTo(200);

        var disbands = env.ids.audit(t, "group_disband");
        assertThat(disbands).hasSize(1);
        assertThat(disbands.get(0).get(0)).isEqualTo("identity:" + a);
        JsonNode d = JSON.readTree(disbands.get(0).get(1));
        assertThat(d.get("group").asText()).isEqualTo("push-train");
        assertThat(d.get("old").get("purpose").asText()).isEqualTo("first");
        assertThat(d.get("old").get("leader").asText()).isEqualTo(a);
        assertThat(d.get("old").get("ack_deadline").asInt()).isEqualTo(30);
        assertThat(d.get("old").get("serves").get(0).asText()).isEqualTo("nexus-develop");
        assertThat(d.get("memberships_removed").asInt()).isEqualTo(1);

        var reforms = env.ids.audit(t, "group_reform");
        assertThat(reforms).hasSize(1);
        JsonNode r = JSON.readTree(reforms.get(0).get(1));
        assertThat(r.get("old").get("purpose").asText()).isEqualTo("first");
        assertThat(r.get("old").get("ended_at").isNull()).as("the old record was disbanded").isFalse();
        assertThat(r.get("old").get("ack_deadline").asInt()).isEqualTo(30);
        assertThat(r.get("new").get("purpose").asText()).isEqualTo("second");
        assertThat(r.get("new").get("ended_at").isNull()).isTrue();
    }
}
