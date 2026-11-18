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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static dev.nexus.service.http.GroupTestEnv.body;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-252 Step 1: {@code serves}, one live group per topic under the per-topic advisory lock, and the deadlines
 * ({@code ack_deadline} and {@code ack_deadline_by_topic}, whole minutes from 5 to 240, keys within {@code serves}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class GroupServesTest {

    GroupTestEnv env;

    @BeforeAll
    void up() throws Exception {
        env = GroupTestEnv.start();
    }

    @AfterAll
    void down() throws Exception {
        env.close();
    }

    private static Map<String, Object> withDeadline(Map<String, Object> m, Object... kv) {
        return IdentityTestEnv.with(m, kv);
    }

    @Test
    void aSecondGroupServingTheSameTopicIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("push-train", a, List.of("nexus-develop"))).status()).isEqualTo(200);

        Resp second = env.form(t, b, body("review-pair", b, List.of("nexus-main", "nexus-develop")));
        assertThat(second.status()).isEqualTo(409);
        assertThat(second.error()).isEqualTo("topic_served");
        assertThat(second.str("topic")).isEqualTo("nexus-develop");
        assertThat(env.read(t, "review-pair").status()).as("the refused write left no record").isEqualTo(404);
        assertThat(env.members(t, "review-pair")).as("and no membership").isEmpty();

        // A topic nobody serves is fine, and so is the same topic once the first group is gone.
        assertThat(env.form(t, b, body("review-pair", b, List.of("nexus-main"))).status()).isEqualTo(200);
        assertThat(env.as(t, a, "POST", "/push-train/disband", Map.of()).status()).isEqualTo(200);
        assertThat(env.as(t, b, "POST", "/review-pair/set", Map.of("serves", List.of("nexus-main", "nexus-develop"))).status())
            .isEqualTo(200);
        assertThat(env.groups.servingGroup(t, "nexus-develop")).hasValueSatisfying(g -> assertThat(g).containsEntry("name", "review-pair"));
    }

    @Test
    void twoGroupsOfDifferentNamesFormingOnOneTopicAtOnceLeaveOneServingIt() throws Exception {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        var pool = Executors.newFixedThreadPool(2);
        try {
            for (int round = 0; round < 5; round++) {
                String topic = "nexus-race-" + round;
                String one = "race-a-" + round;
                String two = "race-b-" + round;
                CountDownLatch go = new CountDownLatch(1);
                Future<Resp> fa = pool.submit(() -> {
                    go.await();
                    return env.form(t, a, body(one, a, List.of(topic)));
                });
                Future<Resp> fb = pool.submit(() -> {
                    go.await();
                    return env.form(t, b, body(two, b, List.of(topic)));
                });
                go.countDown();
                Resp ra = fa.get();
                Resp rb = fb.get();
                int won = (ra.status() == 200 ? 1 : 0) + (rb.status() == 200 ? 1 : 0);
                assertThat(won).as("round %d: %s / %s", round, ra, rb).isEqualTo(1);
                Resp loser = ra.status() == 200 ? rb : ra;
                assertThat(loser.status()).isEqualTo(409);
                assertThat(loser.error()).isEqualTo("topic_served");
                String winner = ra.status() == 200 ? one : two;
                assertThat(env.groups.servingGroup(t, topic)).hasValueSatisfying(g -> assertThat(g).containsEntry("name", winner));
            }
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void anAckDeadlineByTopicKeyOutsideServesIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        Resp form = env.form(t, a, withDeadline(body("push-train", a, List.of("nexus-develop")),
            "ack_deadline_by_topic", Map.of("nexus-main", 30)));
        assertThat(form.status()).isEqualTo(400);
        assertThat(form.error()).isEqualTo("deadline_topic_not_served");
        assertThat(env.read(t, "push-train").status()).isEqualTo(404);

        assertThat(env.form(t, a, body("push-train", a, List.of("nexus-develop"))).status()).isEqualTo(200);
        Resp set = env.as(t, a, "POST", "/push-train/set", Map.of("ack_deadline_by_topic", Map.of("nexus-main", 30)));
        assertThat(set.status()).isEqualTo(400);
        assertThat(set.error()).isEqualTo("deadline_topic_not_served");
        // The check runs on every write: dropping the topic a key names, with the key kept in the same call, is the same refusal.
        assertThat(env.as(t, a, "POST", "/push-train/set",
            Map.of("serves", List.of("nexus-main"), "ack_deadline_by_topic", Map.of("nexus-develop", 30))).error())
            .isEqualTo("deadline_topic_not_served");
    }

    @Test
    void removingATopicFromServesRemovesItsKey() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        assertThat(env.form(t, a, withDeadline(body("push-train", a, List.of("nexus-develop", "nexus-main")),
            "ack_deadline_by_topic", Map.of("nexus-develop", 30, "nexus-main", 45))).status()).isEqualTo(200);
        assertThat(env.read(t, "push-train").map("ack_deadline_by_topic"))
            .containsEntry("nexus-develop", 30).containsEntry("nexus-main", 45);

        Resp r = env.as(t, a, "POST", "/push-train/set", Map.of("serves", List.of("nexus-develop")));
        assertThat(r.status()).as(r.toString()).isEqualTo(200);
        assertThat(r.map("ack_deadline_by_topic")).containsOnlyKeys("nexus-develop").containsEntry("nexus-develop", 30);
        assertThat(env.read(t, "push-train").map("ack_deadline_by_topic")).containsOnlyKeys("nexus-develop");
        // Serving it again does not bring the key back.
        env.as(t, a, "POST", "/push-train/set", Map.of("serves", List.of("nexus-develop", "nexus-main")));
        assertThat(env.read(t, "push-train").map("ack_deadline_by_topic")).containsOnlyKeys("nexus-develop");
    }

    @Test
    void aDeadlineOutsideFiveToTwoHundredFortyIsRefused() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        for (Object bad : new Object[] {4, 241, 0, -5, 20.5, "20", true, 1000000}) {
            Resp r = env.form(t, a, withDeadline(body("push-train", a, List.of("nexus-develop")), "ack_deadline", bad));
            assertThat(r.status()).as("ack_deadline " + bad).isEqualTo(400);
            assertThat(r.error()).isEqualTo("bad_deadline");
            Resp byTopic = env.form(t, a, withDeadline(body("push-train", a, List.of("nexus-develop")),
                "ack_deadline_by_topic", Map.of("nexus-develop", bad)));
            assertThat(byTopic.status()).as("by topic " + bad).isEqualTo(400);
            assertThat(byTopic.error()).isEqualTo("bad_deadline");
        }
        assertThat(env.read(t, "push-train").status()).as("none of them left a record").isEqualTo(404);

        // The bounds themselves are in range.
        assertThat(env.form(t, a, withDeadline(body("push-train", a, List.of("nexus-develop")), "ack_deadline", 5,
            "ack_deadline_by_topic", Map.of("nexus-develop", 240))).status()).isEqualTo(200);
        for (Object bad : new Object[] {4, 241, 20.5, "20"}) {
            Resp r = env.as(t, a, "POST", "/push-train/set", Map.of("ack_deadline", bad));
            assertThat(r.status()).as("set " + bad).isEqualTo(400);
            assertThat(env.as(t, a, "POST", "/push-train/set", Map.of("ack_deadline_by_topic", Map.of("nexus-develop", bad)))
                .status()).as("set by topic " + bad).isEqualTo(400);
        }
        assertThat(env.read(t, "push-train").integer("ack_deadline")).isEqualTo(5);
    }

    @Test
    void theDeadlinesAreStoredAndReadBackAndTheDefaultIsTwentyMinutes() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        assertThat(env.form(t, a, body("plain", a)).integer("ack_deadline")).isEqualTo(20);
        assertThat(env.read(t, "plain").map("ack_deadline_by_topic")).isEmpty();

        env.form(t, b, withDeadline(body("push-train", b, List.of("nexus-develop", "nexus-main")), "ack_deadline", 25,
            "ack_deadline_by_topic", Map.of("nexus-main", 90)));
        Resp read = env.read(t, "push-train");
        assertThat(read.integer("ack_deadline")).isEqualTo(25);
        assertThat(read.map("ack_deadline_by_topic")).containsOnlyKeys("nexus-main").containsEntry("nexus-main", 90);
        assertThat(read.body().get("serves")).isEqualTo(List.of("nexus-develop", "nexus-main"));

        env.as(t, b, "POST", "/push-train/set", Map.of("ack_deadline_by_topic", Map.of("nexus-main", 60, "nexus-develop", 10)));
        assertThat(env.read(t, "push-train").map("ack_deadline_by_topic"))
            .containsEntry("nexus-main", 60).containsEntry("nexus-develop", 10);
    }

    @Test
    void setEndsTakesTheTopicLockAndRefusesWhenAnotherLiveGroupServesTheTopic() {
        String t = IdentityTestEnv.freshTenant();
        String a = env.seat(t, "alpha");
        String b = env.seat(t, "beta");
        String soon = env.ids.clock.instant().plus(Duration.ofHours(1)).toString();
        assertThat(env.form(t, a, withDeadline(body("push-train", a, List.of("nexus-develop")), "ends", soon)).status()).isEqualTo(200);

        env.ids.clock.advance(Duration.ofHours(2)); // push-train's ends has passed: it serves nothing now
        assertThat(env.groups.servingGroup(t, "nexus-develop")).isEmpty();
        env.renew(t, b);
        assertThat(env.form(t, b, body("review-pair", b, List.of("nexus-develop"))).status()).isEqualTo(200);

        String later = env.ids.clock.instant().plus(Duration.ofHours(5)).toString();
        Resp extend = env.as(t, a, "POST", "/push-train/set", Map.of("ends", later));
        assertThat(extend.status()).isEqualTo(409);
        assertThat(extend.error()).isEqualTo("topic_served");
        assertThat(env.groups.servingGroup(t, "nexus-develop")).hasValueSatisfying(g -> assertThat(g).containsEntry("name", "review-pair"));
    }
}
